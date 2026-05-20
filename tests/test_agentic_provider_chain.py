"""Project A2: provider-chain + Retry-After + kill-switch + breaker tests.

Covers the new ``_create_message_with_retry`` behavior and ``run_step``'s
chain pinning when a provider_chain kwarg is supplied.  All tests use fake
client doubles — no real httpx, no real LLM API calls (per SD-008).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.user import User
from app.services import agentic
from app.services.agentic import (
    FallbackBreakerOpen,
    LLMPhaseError,
    ProviderFallbackKilled,
    SameProviderFallbackNoop,
    UnsupportedChainAnthropicToDeepseek,
    _classify_provider_error,
    _clamp_retry_after,
    _create_message_with_retry,
    _dedupe_chain,
    _FallbackBreaker,
    _resolve_provider_chain,
    run_step,
)
from app.services.tools.registry import READ_PHASE_TOOLS
from app.services.turn_context import TurnContext
from tests.conftest import FakePool


pytestmark = pytest.mark.anyio


USAGE = {
    "input_tokens": 10,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "output_tokens": 2,
}


# ── fakes ───────────────────────────────────────────────────────────────────

def _response(text: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(**USAGE),
    )


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


class _FakeProviderError(Exception):
    """Mimics httpx.HTTPStatusError / anthropic.APIStatusError shape."""

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.response = _FakeResponse(status, headers)


class _ScriptedMessages:
    """A messages.create stand-in that returns scripted outcomes per call."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("unexpected provider call (no outcomes left)")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _ScriptedClient:
    def __init__(self, outcomes: list) -> None:
        self.messages = _ScriptedMessages(outcomes)


def _ctx(pool: FakePool | None = None, *, bot_id: str = "mediator") -> TurnContext:
    pool = pool or FakePool()
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    return TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id=bot_id,
        current_step="read",
        hot_context_rendered="ctx",
    )


def _patch_clients(monkeypatch, *, deepseek=None, anthropic=None):
    """Patch the per-hop client builders inside agentic."""
    if deepseek is not None:
        monkeypatch.setattr(agentic, "DeepSeekClient", lambda: deepseek)
    if anthropic is not None:
        monkeypatch.setattr(
            agentic.anthropic, "AsyncAnthropic", lambda api_key=None: anthropic
        )


@pytest.fixture(autouse=True)
def _reset_breaker():
    agentic._FALLBACK_BREAKER.reset()
    yield
    agentic._FALLBACK_BREAKER.reset()


# ── classifier + Retry-After parsing ────────────────────────────────────────

def test_classify_provider_error_rate_limited_with_retry_after():
    exc = _FakeProviderError(429, {"retry-after": "2"})
    klass, retry_after = _classify_provider_error(exc, "deepseek")
    assert klass == "rate_limited"
    assert retry_after == 2


def test_classify_provider_error_overloaded_defaults_retry_after():
    exc = _FakeProviderError(529)
    klass, retry_after = _classify_provider_error(exc, "anthropic")
    assert klass == "overloaded"
    assert retry_after == 2


def test_classify_provider_error_transient_5xx():
    exc = _FakeProviderError(503)
    klass, _ = _classify_provider_error(exc, "deepseek")
    assert klass == "transient"


def test_classify_provider_error_bad_request_400():
    exc = _FakeProviderError(400)
    klass, _ = _classify_provider_error(exc, "deepseek")
    assert klass == "bad_request"


def test_classify_provider_error_parses_http_date_retry_after():
    future = datetime.now(UTC) + timedelta(seconds=15)
    # RFC-7231 IMF-fixdate: e.g. "Sun, 06 Nov 1994 08:49:37 GMT"
    http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    exc = _FakeProviderError(429, {"retry-after": http_date})
    klass, retry_after = _classify_provider_error(exc, "deepseek")
    assert klass == "rate_limited"
    assert retry_after is not None
    assert 10 <= retry_after <= 20


def test_clamp_retry_after_under_cap():
    assert _clamp_retry_after(2, 30) == 2


def test_clamp_retry_after_over_cap_returns_none():
    assert _clamp_retry_after(120, 30) is None


def test_clamp_retry_after_none_passes_through():
    assert _clamp_retry_after(None, 30) is None


# ── chain deduping + resolution ─────────────────────────────────────────────

def test_dedupe_chain_collapses_consecutive_duplicates():
    assert _dedupe_chain(("anthropic", "anthropic")) == ("anthropic",)
    assert _dedupe_chain(("deepseek", "anthropic")) == ("deepseek", "anthropic")
    assert _dedupe_chain(("a", "a", "b", "b", "a")) == ("a", "b", "a")


def test_resolve_provider_chain_returns_bot_spec_chain(app_env):
    from app.config import get_settings

    settings = get_settings()
    user = User(uuid4(), "Anyone", "15555550100", "UTC")

    class _Spec:
        provider_chain = ("deepseek", "anthropic")

    assert _resolve_provider_chain(_Spec(), user, settings) == (
        "deepseek",
        "anthropic",
    )


def test_resolve_provider_chain_anthropic_only_unchanged(app_env):
    from app.config import get_settings

    settings = get_settings()
    user = User(uuid4(), "Anyone", "15555550100", "UTC")

    class _Spec:
        provider_chain = ("anthropic",)

    assert _resolve_provider_chain(_Spec(), user, settings) == ("anthropic",)


# ── _create_message_with_retry: chain behaviour ─────────────────────────────

async def test_deepseek_429_with_short_retry_after_retries_same_provider(
    app_env, monkeypatch
):
    ctx = _ctx()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(agentic.asyncio, "sleep", fake_sleep)
    deepseek = _ScriptedClient([
        _FakeProviderError(429, {"retry-after": "2"}),
        _response("deepseek ok"),
    ])
    anthropic_client = _ScriptedClient([])  # should never be called
    _patch_clients(monkeypatch, deepseek=deepseek, anthropic=anthropic_client)

    response, effective = await _create_message_with_retry(
        None,
        ctx=ctx,
        system=[{"type": "text", "text": "sys"}],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
        provider_chain=("deepseek", "anthropic"),
    )

    assert effective == "deepseek"
    assert slept == [2]
    assert len(deepseek.messages.calls) == 2
    assert len(anthropic_client.messages.calls) == 0


async def test_deepseek_429_with_oversize_retry_after_advances_to_anthropic(
    app_env, monkeypatch
):
    ctx = _ctx()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(agentic.asyncio, "sleep", fake_sleep)
    deepseek = _ScriptedClient([
        _FakeProviderError(429, {"retry-after": "120"}),
    ])
    anthropic_client = _ScriptedClient([_response("anthropic ok")])
    _patch_clients(monkeypatch, deepseek=deepseek, anthropic=anthropic_client)

    response, effective = await _create_message_with_retry(
        None,
        ctx=ctx,
        system=[{"type": "text", "text": "sys"}],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
        provider_chain=("deepseek", "anthropic"),
    )

    assert effective == "anthropic"
    assert slept == []  # no wait — over cap
    assert len(deepseek.messages.calls) == 1
    assert len(anthropic_client.messages.calls) == 1


async def test_deepseek_429_with_http_date_retry_after_parses_and_clamps(
    app_env, monkeypatch
):
    ctx = _ctx()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(agentic.asyncio, "sleep", fake_sleep)
    future = datetime.now(UTC) + timedelta(seconds=5)
    http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    deepseek = _ScriptedClient([
        _FakeProviderError(429, {"retry-after": http_date}),
        _response("deepseek ok"),
    ])
    anthropic_client = _ScriptedClient([])
    _patch_clients(monkeypatch, deepseek=deepseek, anthropic=anthropic_client)

    await _create_message_with_retry(
        None,
        ctx=ctx,
        system=[{"type": "text", "text": "sys"}],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
        provider_chain=("deepseek", "anthropic"),
    )
    assert len(slept) == 1
    assert 0 < slept[0] <= 30


async def test_deepseek_529_falls_back_to_anthropic(app_env, monkeypatch):
    ctx = _ctx()
    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agentic.asyncio, "sleep", _no_sleep)
    deepseek = _ScriptedClient([_FakeProviderError(529)])
    anthropic_client = _ScriptedClient([_response("anthropic ok")])
    _patch_clients(monkeypatch, deepseek=deepseek, anthropic=anthropic_client)

    _, effective = await _create_message_with_retry(
        None,
        ctx=ctx,
        system=[{"type": "text", "text": "sys"}],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
        provider_chain=("deepseek", "anthropic"),
    )
    assert effective == "anthropic"


async def test_deepseek_400_falls_back_to_anthropic(app_env, monkeypatch):
    """bad_request from the primary advances to fallback (no same-provider retry)."""
    ctx = _ctx()
    deepseek = _ScriptedClient([_FakeProviderError(400)])
    anthropic_client = _ScriptedClient([_response("anthropic ok")])
    _patch_clients(monkeypatch, deepseek=deepseek, anthropic=anthropic_client)

    _, effective = await _create_message_with_retry(
        None,
        ctx=ctx,
        system=[{"type": "text", "text": "sys"}],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
        provider_chain=("deepseek", "anthropic"),
    )
    assert effective == "anthropic"
    assert len(deepseek.messages.calls) == 1  # no same-provider retry on 400


async def test_anthropic_400_as_final_hop_raises_llm_phase_error(
    app_env, monkeypatch
):
    ctx = _ctx()
    anthropic_client = _ScriptedClient([_FakeProviderError(400)])
    _patch_clients(monkeypatch, anthropic=anthropic_client)

    with pytest.raises(LLMPhaseError) as excinfo:
        await _create_message_with_retry(
            None,
            ctx=ctx,
            system=[{"type": "text", "text": "sys"}],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
            provider_chain=("anthropic",),
        )
    # Length-1 spec falls through to LLMPhaseError, NOT SameProviderFallbackNoop.
    assert not isinstance(excinfo.value, SameProviderFallbackNoop)
    # The class-level default applies — failure_reason="llm_phase_failed"
    # (catch-all for chain-exhausted without a more specific reason; NOT
    # a real clock timeout, which would surface as "llm_timeout").
    assert getattr(excinfo.value, "failure_reason", None) == "llm_phase_failed"


async def test_chain_anthropic_anthropic_deduped_to_one_raises_noop(
    app_env, monkeypatch
):
    """Original length>1, deduped to length 1 → SameProviderFallbackNoop."""
    ctx = _ctx()
    anthropic_client = _ScriptedClient([])
    _patch_clients(monkeypatch, anthropic=anthropic_client)

    with pytest.raises(SameProviderFallbackNoop) as excinfo:
        await _create_message_with_retry(
            None,
            ctx=ctx,
            system=[{"type": "text", "text": "sys"}],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
            provider_chain=("anthropic", "anthropic"),
        )
    assert excinfo.value.failure_reason == "same_provider_fallback_noop"
    # No provider call should have been attempted.
    assert len(anthropic_client.messages.calls) == 0


async def test_anthropic_to_deepseek_chain_raises_unsupported_chain(
    app_env, monkeypatch
):
    ctx = _ctx()
    with pytest.raises(UnsupportedChainAnthropicToDeepseek) as excinfo:
        await _create_message_with_retry(
            None,
            ctx=ctx,
            system=[{"type": "text", "text": "sys"}],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
            provider_chain=("anthropic", "deepseek"),
        )
    assert (
        excinfo.value.failure_reason == "unsupported_chain_anthropic_to_deepseek"
    )
    # Sanity: this reason maps to infra_bug.
    from app.services.inbound_queue import FAILURE_REASON_TO_CLASS

    assert (
        FAILURE_REASON_TO_CLASS["unsupported_chain_anthropic_to_deepseek"]
        == "infra_bug"
    )


# ── kill-switch ─────────────────────────────────────────────────────────────

async def test_kill_switch_disables_fallback(app_env, monkeypatch):
    ctx = _ctx()

    async def _killed(_pool):
        return True

    monkeypatch.setattr(agentic, "is_recovery_v2_killed", _killed)
    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agentic.asyncio, "sleep", _no_sleep)
    deepseek = _ScriptedClient([_FakeProviderError(503)])
    anthropic_client = _ScriptedClient([_response("anthropic ok")])  # must NOT be called
    _patch_clients(monkeypatch, deepseek=deepseek, anthropic=anthropic_client)

    with pytest.raises(ProviderFallbackKilled) as excinfo:
        await _create_message_with_retry(
            None,
            ctx=ctx,
            system=[{"type": "text", "text": "sys"}],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
            provider_chain=("deepseek", "anthropic"),
        )
    assert excinfo.value.failure_reason == "provider_fallback_killed"
    # Primary call was attempted (once, since 503 is transient).
    assert len(deepseek.messages.calls) == 1
    # Fallback hop was NOT attempted.
    assert len(anthropic_client.messages.calls) == 0


# ── effective-provider pinning inside run_step ──────────────────────────────

async def test_run_step_pins_effective_provider_after_fallback(
    app_env, monkeypatch
):
    """When run_step's first call falls back to Anthropic, the second
    iteration must request Anthropic only — the active chain must shrink to
    (effective_provider,) so message shapes don't mix mid-step.
    """
    ctx = _ctx()
    ctx.current_step = "read"
    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agentic.asyncio, "sleep", _no_sleep)
    # First call: deepseek fails with 503 (transient — no same-provider
    # retry; immediately advances to fallback hop), anthropic returns
    # tool_use.  Second call: deepseek must NOT be called — anthropic
    # returns end_turn.
    deepseek = _ScriptedClient([
        _FakeProviderError(503),
        AssertionError("deepseek must not be called after fallback pin"),
    ])
    anthropic_client = _ScriptedClient([
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="toolu_1",
                    name="update_turn_plan",
                    input={},
                )
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(**USAGE),
        ),
        _response("final"),
    ])
    _patch_clients(monkeypatch, deepseek=deepseek, anthropic=anthropic_client)

    async def fake_call_tool(name, args, ctx):  # noqa: ARG001
        return {"ok": True}

    monkeypatch.setattr(agentic, "call_tool", fake_call_tool)

    text, _, _ = await run_step(
        None,
        ctx,
        "system",
        "context",
        READ_PHASE_TOOLS | {"update_turn_plan"},
        [{"role": "user", "content": "hi"}],
        provider_chain=("deepseek", "anthropic"),
    )
    assert text == "final"
    # DeepSeek only called once (the first failed primary attempt).
    assert len(deepseek.messages.calls) == 1
    # Anthropic served both the initial fallback and the second iteration.
    assert len(anthropic_client.messages.calls) == 2


# ── breaker ─────────────────────────────────────────────────────────────────

def test_breaker_opens_after_threshold_samples(app_env):
    breaker = _FallbackBreaker()
    bot_id = "mediator"
    # Below min samples → not open even if rate is 1.0
    for _ in range(5):
        breaker.record(bot_id, fell_back=True)
    assert breaker.is_open(bot_id) is False
    # Top up to 10 samples, all fell_back=True → 1.0 rate → opens.
    for _ in range(5):
        breaker.record(bot_id, fell_back=True)
    assert breaker.is_open(bot_id) is True


def test_breaker_does_not_open_when_rate_below_threshold(app_env):
    breaker = _FallbackBreaker()
    bot_id = "mediator"
    # 10 samples, 4 fallbacks → 0.4 rate < 0.5 threshold.
    for _ in range(4):
        breaker.record(bot_id, fell_back=True)
    for _ in range(6):
        breaker.record(bot_id, fell_back=False)
    assert breaker.is_open(bot_id) is False


def test_breaker_evicts_old_samples_outside_window(app_env, monkeypatch):
    breaker = _FallbackBreaker()
    bot_id = "mediator"
    # Mock monotonic so we can drive eviction deterministically.
    t = {"now": 1000.0}

    def fake_monotonic() -> float:
        return t["now"]

    monkeypatch.setattr(agentic.time, "monotonic", fake_monotonic)
    for _ in range(10):
        breaker.record(bot_id, fell_back=True)
    assert breaker.is_open(bot_id) is True
    # Advance well past the window (default 300s).
    t["now"] += 10_000
    # is_open prunes lazily; after pruning the deque is empty.
    assert breaker.is_open(bot_id) is False


async def test_create_message_raises_breaker_open_before_fallback_hop(
    app_env, monkeypatch
):
    """Pre-trip the breaker; the fallback advance must raise FallbackBreakerOpen."""
    ctx = _ctx(bot_id="hector")
    # Pre-trip the global breaker for this bot.
    for _ in range(15):
        agentic._FALLBACK_BREAKER.record("hector", fell_back=True)
    assert agentic._FALLBACK_BREAKER.is_open("hector") is True

    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agentic.asyncio, "sleep", _no_sleep)
    deepseek = _ScriptedClient([_FakeProviderError(503)])
    anthropic_client = _ScriptedClient([])  # must not be called
    _patch_clients(monkeypatch, deepseek=deepseek, anthropic=anthropic_client)

    with pytest.raises(FallbackBreakerOpen) as excinfo:
        await _create_message_with_retry(
            None,
            ctx=ctx,
            system=[{"type": "text", "text": "sys"}],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
            provider_chain=("deepseek", "anthropic"),
        )
    assert excinfo.value.failure_reason == "fallback_breaker_open"
    assert len(anthropic_client.messages.calls) == 0
