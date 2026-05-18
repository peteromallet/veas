"""Agentic turn lifecycle orchestration."""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json
import logging
import re
import time
from collections import deque
from datetime import UTC, datetime
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal, Mapping
from uuid import UUID

import anthropic

# Defensive imports for Anthropic SDK exception classes.  Anthropic >= 0.40
# exports RateLimitError and APIStatusError; future SDK renames must not
# break module import.  When absent, isinstance(...) calls against the
# empty-tuple sentinels short-circuit to False and the status-code path
# (getattr(exc, "status_code", None)) is used instead.
try:  # pragma: no cover - exercised whenever Anthropic SDK is present
    from anthropic import APIStatusError as _AnthropicAPIStatusError
except ImportError:  # pragma: no cover
    _AnthropicAPIStatusError = ()  # type: ignore[assignment]
try:  # pragma: no cover
    from anthropic import RateLimitError as _AnthropicRateLimitError
except ImportError:  # pragma: no cover
    _AnthropicRateLimitError = ()  # type: ignore[assignment]

from app.bots.registry import get_bot_spec, primary_topic_id_for
from app.config import get_settings
from app.models.user import User, claim_onboarding_welcome
from app.services import discord, hooks, system_state
from app.services.deepseek import DeepSeekClient
from app.services.system_state import is_recovery_v2_killed
from app.services.hot_context import build_hot_context, render_hot_context
from app.services.hot_context_solo import (
    build_hot_context_solo,
    render_hot_context_solo,
)
from app.services import inbound_queue, metrics
from app.services.messaging import send_outbound, sent_contents_for_turn
from app.services.partner_sharing import get_partner_share
from app.services.spend import is_under_cap, record_llm_cost
from app.services.crypto import encrypt_value
from app.services.scope import InboundScope
from app.services.text_safety import clean_user_facing_text
from app.services.tools.registry import (
    STEP_ALLOWED_TOOLS,
    call_tool,
    to_anthropic_tools,
)
from app.services.turn_audit import record_turn_event
from app.services.turn_plan import make_turn_plan, orient_summary, pick_default_skeleton
from app.services.turn_context import (
    BeforePacedSend,
    TurnContext,
    obs_fields,
    partner_of,
)

import tool_schemas as _tool_schemas_module

_TOOL_SCHEMA_VERSION: str = hashlib.sha1(
    open(_tool_schemas_module.__file__, "rb").read()
).hexdigest()[:12]

# Must match the value accepted by the trigger installed in
# migration 0046 (mediator.assert_lifecycle_columns_writer).
# The trigger only accepts 'inbound_queue' — 'agentic' was a dead
# string that caused B1 (Hector inbox stall, 2026-05-17).
_AGENTIC_LIFECYCLE_WRITER_SQL = (
    "SELECT set_config('app.lifecycle_writer', 'inbound_queue', true)"
)

logger = logging.getLogger(__name__)

_pool: Any | None = None


class AgenticTurnError(Exception):
    failure_reason = "crashed"


class SpendCapExceeded(Exception):
    failure_reason = "spend_cap"


class NewerInboundBeforeFinalSend(Exception):
    pass


class LLMPhaseError(Exception):
    failure_reason = "llm_timeout"


class BoundedLoopExceeded(Exception):
    failure_reason: str

    def __init__(self, message: str = "bounded_loop_exceeded") -> None:
        super().__init__(message)
        self.failure_reason = message


# ── Project A2 provider-chain exceptions ─────────────────────────────────────
# All four assign ``failure_reason`` on the instance so that the outer turn
# failure handler at the bottom of ``_run_agentic`` resolves
# ``getattr(exc, "failure_reason", "crashed")`` to the per-instance attribute
# instead of the class-level default ``"llm_timeout"`` on LLMPhaseError.
class ProviderFallbackKilled(LLMPhaseError):
    def __init__(self) -> None:
        super().__init__("provider_fallback_killed")
        self.failure_reason = "provider_fallback_killed"


class SameProviderFallbackNoop(LLMPhaseError):
    def __init__(self) -> None:
        super().__init__("same_provider_fallback_noop")
        self.failure_reason = "same_provider_fallback_noop"


class FallbackBreakerOpen(LLMPhaseError):
    def __init__(self, bot_id: str) -> None:
        super().__init__(f"fallback_breaker_open bot_id={bot_id}")
        self.failure_reason = "fallback_breaker_open"


class _ZeroClaimAbort(Exception):
    """Raised inside a conn.transaction() when zero triggering messages are
    claimable.  asyncpg rolls back the bot_turn INSERT, leaving no orphan row."""


class RespondCapNoOutput(LLMPhaseError):
    def __init__(self) -> None:
        super().__init__("respond_cap_no_output")
        self.failure_reason = "respond_cap_no_output"


class UnsupportedChainAnthropicToDeepseek(LLMPhaseError):
    """Configuration error: cannot fall back from Anthropic-shaped messages
    to a DeepSeek provider hop.  Per-instance failure_reason so the outer
    handler routes through FAILURE_REASON_TO_CLASS to ``"infra_bug"``."""

    def __init__(self) -> None:
        super().__init__("unsupported_chain_anthropic_to_deepseek")
        self.failure_reason = "unsupported_chain_anthropic_to_deepseek"


class _PostSendPhaseCapExceeded(BoundedLoopExceeded):
    """Internal control-flow marker for post-send (record/schedule) cap.

    NEVER propagated to the outer turn-failure handler; caught locally inside
    ``_run_agentic`` so the run continues with the next plan step and
    ``bot_turns.failure_reason`` stays NULL.  Not registered in
    ``FAILURE_REASON_TO_CLASS``.
    """

    def __init__(self, step: str, cap: int, tool_iteration_count: int) -> None:
        super().__init__(f"post_send_phase_cap_exceeded step={step}")
        self.step = step
        self.cap = cap
        self.tool_iteration_count = tool_iteration_count


REACTION_DIRECTIVE_RE = re.compile(
    r"^\s*\[react:\s*(?P<emoji>[^\]\s]+)\s*\]\s*$", re.IGNORECASE
)
PACING_CONTEXT_KEYS = (
    "action",
    "reason",
    "wait_s",
    "wait_ms",
    "reaction",
    "source",
    "message_count",
    "typing_active",
    "latest_message_age_s",
    "contains_question",
    "contains_ack",
    "contains_closure",
    "has_media",
    "charge",
    "charges",
)
PACING_SIGNAL_KEYS = (
    "source",
    "message_count",
    "typing_active",
    "latest_message_age_s",
    "contains_question",
    "contains_ack",
    "contains_closure",
    "has_media",
    "charge",
    "charges",
)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _compact_json_value(value: Any, *, text_limit: int = 180) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= text_limit else value[: text_limit - 3] + "..."
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            compact[str(key)] = _compact_json_value(item, text_limit=text_limit)
        return compact
    if isinstance(value, (list, tuple, set)):
        return [
            _compact_json_value(item, text_limit=text_limit) for item in list(value)[:8]
        ]
    return str(value)


def _compact_pacing_context(pacing_context: Any) -> dict[str, Any] | None:
    if pacing_context is None:
        return None

    compact: dict[str, Any] = {}
    for key in PACING_CONTEXT_KEYS:
        value = _attr(pacing_context, key)
        if value is not None:
            compact[key] = _compact_json_value(value)

    signal_snapshot = _attr(pacing_context, "signal_snapshot")
    if isinstance(signal_snapshot, Mapping):
        signal_compact = {
            key: _compact_json_value(signal_snapshot[key])
            for key in PACING_SIGNAL_KEYS
            if key in signal_snapshot and signal_snapshot[key] is not None
        }
        if signal_compact:
            compact["signals"] = signal_compact

    preference_snapshot = _attr(pacing_context, "preference_snapshot")
    if isinstance(preference_snapshot, Mapping):
        preference_keys = (
            "conversation_pace",
            "allow_reactions",
            "min_wait_s",
            "max_wait_s",
        )
        preferences = {
            key: _compact_json_value(preference_snapshot[key])
            for key in preference_keys
            if key in preference_snapshot and preference_snapshot[key] is not None
        }
        if preferences:
            compact["preferences"] = preferences

    llm_judgement = _attr(pacing_context, "llm_judgement")
    if isinstance(llm_judgement, Mapping):
        judgement_keys = ("action", "reason", "wait_s", "reaction", "fallback")
        judgement = {
            key: _compact_json_value(llm_judgement[key])
            for key in judgement_keys
            if key in llm_judgement and llm_judgement[key] is not None
        }
        if judgement:
            compact["llm"] = judgement

    if not compact and isinstance(pacing_context, Mapping):
        compact = {
            str(key): _compact_json_value(value)
            for key, value in pacing_context.items()
            if key in PACING_CONTEXT_KEYS and value is not None
        }

    return compact or None


def _trigger_metadata_with_pacing(
    trigger_metadata: Mapping[str, Any] | None,
    pacing_context: Any,
) -> dict[str, Any] | None:
    compact_pacing = _compact_pacing_context(pacing_context)
    if compact_pacing is None:
        return dict(trigger_metadata) if trigger_metadata is not None else None

    metadata = dict(trigger_metadata or {})
    context = dict(metadata.get("context") or {})
    context["pacing"] = compact_pacing
    metadata["context"] = context
    metadata["pacing"] = compact_pacing
    metadata.setdefault("kind", "inbound")
    return metadata


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return dict(block)
    block_type = _attr(block, "type")
    data: dict[str, Any] = {"type": block_type}
    if block_type == "text":
        data["text"] = _attr(block, "text", "")
    elif block_type == "tool_use":
        data["id"] = _attr(block, "id")
        data["name"] = _attr(block, "name")
        data["input"] = _attr(block, "input", {}) or {}
    elif block_type == "openai_assistant_message":
        data["message"] = _attr(block, "message", {}) or {}
    elif block_type == "reasoning_content":
        data["reasoning_content"] = _attr(block, "reasoning_content", "")
    return data


def _system_blocks(
    system_prompt: str, hot_context_rendered: str
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": hot_context_rendered},
    ]
    if len(hot_context_rendered) // 4 >= 1024:
        blocks[1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _anthropic_tools(allowed_tools: set[str]) -> list[dict[str, Any]]:
    tools = [dict(tool) for tool in to_anthropic_tools(allowed_tools)]
    if tools:
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def _usage_tokens(usage: Any, field: str) -> int:
    value = _attr(usage, field, 0) or 0
    return int(value)


async def _record_response_cost(
    pool: Any,
    usage: Any,
    *,
    input_price: float,
    output_price: float,
) -> None:
    input_rate = Decimal(str(input_price))
    output_rate = Decimal(str(output_price))
    input_tokens = _usage_tokens(usage, "input_tokens")
    cache_create = _usage_tokens(usage, "cache_creation_input_tokens")
    cache_read = _usage_tokens(usage, "cache_read_input_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens")
    regular_input_tokens = max(0, input_tokens - cache_create - cache_read)
    dollars = (
        regular_input_tokens * input_rate
        + cache_create * input_rate * Decimal("1.25")
        + cache_read * input_rate * Decimal("0.10")
        + output_tokens * output_rate
    ) / Decimal("1000000")
    if dollars > 0:
        await record_llm_cost(pool, "text", dollars)


def _deepseek_user_names(settings: Any) -> set[str]:
    return {
        name.strip().casefold()
        for name in settings.deepseek_enabled_user_names.split(",")
        if name.strip()
    }


def _llm_client_and_model_for_user(user: User) -> tuple[Any, str, str]:
    settings = get_settings()
    if user.name.strip().casefold() in _deepseek_user_names(settings):
        return DeepSeekClient(), settings.deepseek_conversational_model, "deepseek"
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value()
    )
    return client, settings.conversational_model, "anthropic"


_ProviderErrorClass = Literal[
    "rate_limited", "overloaded", "transient", "bad_request"
]


def _exc_status_code(exc: Exception) -> int | None:
    """Best-effort status-code extraction across Anthropic SDK + httpx errors."""
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):  # pragma: no cover
            pass
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status is not None:
            try:
                return int(status)
            except (TypeError, ValueError):  # pragma: no cover
                pass
    return None


def _exc_retry_after(exc: Exception) -> int | None:
    """Parse Retry-After (delta-seconds or HTTP-date) from an exception response.

    Returns the integer seconds-from-now, or None if absent/unparseable.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after")
    except Exception:  # pragma: no cover - defensive
        return None
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    # Try integer seconds first.
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        pass
    # Fall back to HTTP-date.
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = (parsed - datetime.now(UTC)).total_seconds()
    if delta < 0:
        return 0
    return int(delta)


def _classify_provider_error(
    exc: Exception, provider: str
) -> tuple[_ProviderErrorClass, int | None]:
    """Classify a provider exception into a coarse category + Retry-After.

    Routing:
      429        -> rate_limited
      529        -> overloaded
      408/5xx    -> transient
      400/422    -> bad_request
      other      -> transient (conservative; one provider-level retry then
                    advance to next hop)
    """
    status = _exc_status_code(exc)
    retry_after = _exc_retry_after(exc)
    # Anthropic SDK exception class checks (when truthy) for extra signal.
    if _AnthropicRateLimitError and isinstance(exc, _AnthropicRateLimitError):
        if retry_after is None:
            retry_after = 2
        return "rate_limited", retry_after
    if status == 429:
        if retry_after is None:
            retry_after = 2
        return "rate_limited", retry_after
    if status == 529:
        if retry_after is None:
            retry_after = 2
        return "overloaded", retry_after
    if status in (408, 500, 502, 503, 504):
        return "transient", retry_after
    if status in (400, 422):
        return "bad_request", retry_after
    return "transient", retry_after


def _clamp_retry_after(retry_after_s: int | None, cap: int) -> int | None:
    """Clamp Retry-After: None or >cap means skip the wait and advance."""
    if retry_after_s is None:
        return None
    if retry_after_s < 0:
        return 0
    if retry_after_s > cap:
        return None
    return retry_after_s


class _FallbackBreaker:
    """Per-bot fallback-rate circuit breaker.

    Keeps a bounded deque of ``(monotonic_timestamp, fell_back)`` samples per
    bot_id; trims entries older than the configured window.  Opens when the
    sample count >= min_samples AND the observed fall-back rate >= threshold.

    Scope is per-process (in-memory).  Per SD-006 + the plan, this is an
    accepted tradeoff for the current single-instance Railway deployment;
    the breaker is a degraded-mode signal, not a correctness invariant.  If
    a second incident proves per-process scope inadequate, persisted/shared
    state can be revisited in A3 per SD-009.
    """

    def __init__(self) -> None:
        self._samples: dict[str, deque[tuple[float, bool]]] = {}

    def _prune(self, bot_id: str, window_seconds: int) -> deque[tuple[float, bool]]:
        dq = self._samples.setdefault(bot_id, deque())
        cutoff = time.monotonic() - window_seconds
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        return dq

    def record(self, bot_id: str, fell_back: bool) -> None:
        settings = get_settings()
        dq = self._prune(bot_id, settings.provider_fallback_breaker_window_seconds)
        dq.append((time.monotonic(), fell_back))

    def is_open(self, bot_id: str) -> bool:
        settings = get_settings()
        dq = self._prune(bot_id, settings.provider_fallback_breaker_window_seconds)
        if len(dq) < settings.provider_fallback_breaker_min_samples:
            return False
        rate = sum(1 for _, fell_back in dq if fell_back) / len(dq)
        return rate >= settings.provider_fallback_breaker_threshold

    def reset(self, bot_id: str | None = None) -> None:
        """Test helper: clear samples for a bot or all bots."""
        if bot_id is None:
            self._samples.clear()
        else:
            self._samples.pop(bot_id, None)


_FALLBACK_BREAKER = _FallbackBreaker()


def _build_provider_client(provider: str) -> Any:
    settings = get_settings()
    if provider == "deepseek":
        return DeepSeekClient()
    if provider == "anthropic":
        return anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value()
        )
    raise ValueError(f"_build_provider_client: unknown provider {provider!r}")


def _provider_model(provider: str, model_for_provider: dict[str, str] | None) -> str:
    settings = get_settings()
    if model_for_provider and provider in model_for_provider:
        return model_for_provider[provider]
    if provider == "deepseek":
        return settings.deepseek_conversational_model
    return settings.conversational_model


def _dedupe_chain(chain: tuple[str, ...]) -> tuple[str, ...]:
    """Collapse consecutive duplicate providers in the chain."""
    out: list[str] = []
    for entry in chain:
        if not out or out[-1] != entry:
            out.append(entry)
    return tuple(out)


def _resolve_provider_chain(
    bot_spec: Any, user: User, settings: Any
) -> tuple[str, ...]:
    """Resolve the effective provider chain for (bot, user).

    Combines ``bot_spec.provider_chain`` with the case-folded
    ``deepseek_enabled_user_names`` allowlist: if the user is NOT in the
    allowlist, ``deepseek`` is dropped from the chain.  Casefold semantics
    are preserved exactly from the legacy lookup at
    ``_deepseek_user_names``.

    The returned chain is non-empty; if the dedupe/demotion would leave it
    empty (no providers configured), falls back to ``("anthropic",)``.
    """
    raw_chain = getattr(bot_spec, "provider_chain", None) or ("anthropic",)
    user_allowed = user.name.strip().casefold() in _deepseek_user_names(settings)
    filtered = tuple(
        provider
        for provider in raw_chain
        if provider != "deepseek" or user_allowed
    )
    if not filtered:
        return ("anthropic",)
    return filtered


async def _attempt_provider_call(
    *,
    client: Any,
    provider: str,
    ctx: TurnContext,
    system: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    model: str,
    max_tokens: int,
) -> Any:
    """Single provider call.  Spend-cap guarded.  Records cost on success."""
    settings = get_settings()
    if not await is_under_cap(ctx.pool, "text"):
        raise SpendCapExceeded("text LLM spend cap exceeded")
    # Anthropic cannot consume DeepSeek-native blocks; strip them at the boundary.
    payload_messages = (
        _anthropic_safe_messages(messages) if provider == "anthropic" else messages
    )
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=payload_messages,
        tools=tools,
    )
    if provider == "deepseek":
        input_price = settings.deepseek_input_usd_per_mtok
        output_price = settings.deepseek_output_usd_per_mtok
    else:
        input_price = settings.anthropic_input_usd_per_mtok
        output_price = settings.anthropic_output_usd_per_mtok
    await _record_response_cost(
        ctx.pool,
        _attr(response, "usage", {}),
        input_price=input_price,
        output_price=output_price,
    )
    return response


# Canonical InternalMessage IR (app/llm/internal_message.py) is available
# but disabled by default. When a third provider lands, set
# provider_use_canonical_ir=True and route conversions through
# internal_message rather than _anthropic_safe_messages. The sanitize
# boundary stays the default to avoid destabilizing A2's tested fallback.
async def _create_message_with_retry(
    client: Any,
    *,
    ctx: TurnContext,
    system: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    model: str | None = None,
    provider: str = "anthropic",
    provider_chain: tuple[str, ...] | None = None,
    model_for_provider: dict[str, str] | None = None,
    max_tokens: int = 1200,
) -> tuple[Any, str]:
    """Provider-chain aware message creation with Retry-After + breaker.

    Iterates over a deduped provider chain.  Per hop: ONE primary attempt;
    on rate_limited/overloaded with a clamp-able Retry-After, sleeps then
    does ONE retry on the same provider before advancing.  Before each
    fallback advance, consults ``is_recovery_v2_killed`` (kill switch) and
    the per-bot fallback breaker.

    Behavior notes:
    - Flipping ``recovery_v2_killed`` post-A2 forces single-provider mode
      with no fallback, no breaker bypass, and no Retry-After sleeps beyond
      the existing one-retry-per-provider pattern.
    - Total in-step wall-time bound per call is approximately
      ``len(deduped_chain) * (provider_call_timeout + retry_after_cap)``;
      with default settings (chain length 2, timeout 120, cap 30) this is
      worst-case ~300s but typically << 1s in the happy path.
    - SameProviderFallbackNoop is raised only when the ORIGINAL pre-dedup
      chain length was > 1 AND the dedupe collapsed it to length 1, since
      that indicates a misconfigured-but-intentful fallback.  A genuine
      length-1 spec (e.g. consult_perspective passing provider="anthropic")
      falls through to the underlying error normally.

    Returns
    -------
    tuple[Any, str]
        (response, effective_provider).  Callers may use
        ``effective_provider`` to pin subsequent in-step iterations to the
        same provider.
    """
    settings = get_settings()
    # Back-compat: when no provider_chain supplied, derive one from provider=.
    if provider_chain is None:
        raw_chain: tuple[str, ...] = (provider,)
    else:
        raw_chain = tuple(provider_chain)
    if not raw_chain:
        raise LLMPhaseError("empty provider_chain")
    deduped_chain = _dedupe_chain(raw_chain)
    pre_dedup_was_multi = len(raw_chain) > 1
    if pre_dedup_was_multi and len(deduped_chain) == 1:
        raise SameProviderFallbackNoop()

    # Reject Anthropic-then-DeepSeek which cannot work after sanitization.
    for i in range(len(deduped_chain) - 1):
        if deduped_chain[i] == "anthropic" and deduped_chain[i + 1] == "deepseek":
            raise UnsupportedChainAnthropicToDeepseek()

    bot_id = ctx.bot_id or "unknown"
    retry_after_cap = settings.provider_retry_after_cap_seconds
    last_error: Exception | None = None
    fell_back = False

    for hop_index, hop_provider in enumerate(deduped_chain):
        # Reuse the caller-provided client only when it matches the first hop
        # AND the caller didn't pin via provider_chain.  Otherwise build fresh.
        if (
            hop_index == 0
            and client is not None
            and provider_chain is None
            and hop_provider == provider
        ):
            hop_client = client
        else:
            hop_client = _build_provider_client(hop_provider)
        hop_model = _provider_model(hop_provider, model_for_provider) if (
            model is None or hop_index > 0
        ) else model

        for attempt_index in (0, 1):
            try:
                response = await _attempt_provider_call(
                    client=hop_client,
                    provider=hop_provider,
                    ctx=ctx,
                    system=system,
                    tools=tools,
                    messages=messages,
                    model=hop_model,
                    max_tokens=max_tokens,
                )
                _FALLBACK_BREAKER.record(bot_id, fell_back)
                return response, hop_provider
            except SpendCapExceeded:
                raise
            except (LLMPhaseError, SameProviderFallbackNoop,
                    UnsupportedChainAnthropicToDeepseek, ProviderFallbackKilled,
                    FallbackBreakerOpen):
                raise
            except Exception as exc:
                last_error = exc
                error_class, retry_after_s = _classify_provider_error(
                    exc, hop_provider
                )
                logger.warning(
                    "%s provider call failed (hop=%d/%d, attempt=%d, class=%s): %s",
                    hop_provider,
                    hop_index + 1,
                    len(deduped_chain),
                    attempt_index,
                    error_class,
                    exc,
                    extra=obs_fields(ctx),
                )
                if attempt_index == 0 and error_class in {
                    "rate_limited",
                    "overloaded",
                }:
                    clamped = _clamp_retry_after(retry_after_s, retry_after_cap)
                    if clamped is not None:
                        await asyncio.sleep(clamped)
                        continue  # ONE retry on the same provider
                # Either we already retried once, or the class doesn't
                # warrant a same-provider retry — advance to the next hop.
                break

        # Out of attempts on this hop — try to advance to the next hop.
        if hop_index + 1 >= len(deduped_chain):
            # Final hop exhausted; map to LLMPhaseError so the outer handler
            # routes to retryable_pre_send via FAILURE_REASON_TO_CLASS.
            _FALLBACK_BREAKER.record(bot_id, fell_back)
            raise LLMPhaseError(str(last_error or f"{hop_provider} failed"))

        # Before falling back: kill switch then breaker.
        if await is_recovery_v2_killed(ctx.pool):
            _FALLBACK_BREAKER.record(bot_id, fell_back)
            raise ProviderFallbackKilled()
        if _FALLBACK_BREAKER.is_open(bot_id):
            _FALLBACK_BREAKER.record(bot_id, fell_back)
            raise FallbackBreakerOpen(bot_id)
        fell_back = True
        # A3 work item 6: fallback hop is about to fire — emit one
        # provider_fallback_invoked observation labelled with the (from, to)
        # pair, the current phase, and the bot.
        next_hop_provider = deduped_chain[hop_index + 1]
        metrics.incr(
            "provider_fallback_invoked",
            **{
                "from": hop_provider,
                "to": next_hop_provider,
                "phase": str(ctx.current_step or "unknown"),
                "bot": bot_id,
            },
        )

    # Unreachable: the loop either returns or raises.
    raise LLMPhaseError(str(last_error or "provider chain exhausted"))


def _anthropic_safe_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip provider-native blocks that Anthropic's API cannot accept."""
    safe: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            safe.append(dict(message))
            continue
        filtered = [
            block
            for block in content
            if not (
                isinstance(block, dict)
                and block.get("type")
                in {"openai_assistant_message", "reasoning_content"}
            )
        ]
        if filtered:
            next_message = dict(message)
            next_message["content"] = filtered
            safe.append(next_message)
    return safe


async def run_step(
    client: Any,
    ctx: TurnContext,
    system_prompt: str,
    hot_context_rendered: str,
    allowed_tools: set[str],
    seed_messages: list[dict[str, Any]],
    model: str | None = None,
    provider: str = "anthropic",
    provider_chain: tuple[str, ...] | None = None,
    model_for_provider: dict[str, str] | None = None,
    max_tokens: int = 1200,
    max_tool_iterations: int | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    settings = get_settings()
    if client is None and provider_chain is None:
        if provider == "deepseek":
            client = DeepSeekClient()
            model = model or settings.deepseek_conversational_model
        else:
            client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value()
            )

    system = _system_blocks(system_prompt, hot_context_rendered)
    tools = _anthropic_tools(allowed_tools)
    messages = list(seed_messages)
    tool_call_count = 0
    tool_iteration_count = 0
    consecutive_recoverable_errors = 0
    # Active chain shrinks to (effective_provider,) once a fallback has occurred
    # so subsequent iterations of THIS run_step don't mix provider-native shapes.
    active_chain: tuple[str, ...] | None = (
        tuple(provider_chain) if provider_chain is not None else None
    )

    while True:
        response, effective_provider = await _create_message_with_retry(
            client,
            ctx=ctx,
            system=system,
            tools=tools,
            messages=messages,
            model=model,
            provider=provider,
            provider_chain=active_chain,
            model_for_provider=model_for_provider,
            max_tokens=max_tokens,
        )
        if active_chain is not None and effective_provider != active_chain[0]:
            # Pin to the provider that actually returned for the rest of THIS
            # run_step invocation so message shapes don't mix.
            active_chain = (effective_provider,)
        content_blocks = [
            _block_to_dict(block) for block in (_attr(response, "content", []) or [])
        ]
        messages.append({"role": "assistant", "content": content_blocks})
        tool_uses = [
            block for block in content_blocks if block.get("type") == "tool_use"
        ]
        if not tool_uses or _attr(response, "stop_reason") != "tool_use":
            final_text = "\n".join(
                str(block.get("text", "")).strip()
                for block in content_blocks
                if block.get("type") == "text" and str(block.get("text", "")).strip()
            )
            return final_text, messages, tool_call_count

        tool_iteration_count += 1
        if (
            max_tool_iterations is not None
            and tool_iteration_count > max_tool_iterations
        ):
            current_step = ctx.current_step
            if current_step in {"read", "consult"}:
                # Read / consult are pre-send context-gathering phases.  The
                # spec only named ``read`` explicitly; ``consult`` is treated
                # identically because both must not fail a turn.
                logger.warning(
                    "%s step tool iteration cap reached; advancing without failing turn "
                    "turn_id=%s cap=%d",
                    current_step,
                    ctx.turn_id,
                    max_tool_iterations,
                    extra=obs_fields(ctx),
                )
                return "", messages, tool_call_count
            if current_step == "respond":
                # Respond cap: prefer an early-stop on any prior user-visible
                # send.  Otherwise attempt ONE Anthropic-only emergency hop.
                if ctx.sent_message_parts:
                    logger.warning(
                        "respond step tool iteration cap reached but prior "
                        "send_message_part output exists; treating as success "
                        "turn_id=%s cap=%d",
                        ctx.turn_id,
                        max_tool_iterations,
                        extra=obs_fields(ctx),
                    )
                    return "", messages, tool_call_count
                logger.warning(
                    "respond step tool iteration cap reached with no prior "
                    "user-visible output; attempting Anthropic-only emergency "
                    "fallback hop turn_id=%s cap=%d",
                    ctx.turn_id,
                    max_tool_iterations,
                    extra=obs_fields(ctx),
                )
                try:
                    emergency_response, _ = await _create_message_with_retry(
                        None,
                        ctx=ctx,
                        system=system,
                        tools=tools,
                        messages=messages,
                        model=_provider_model("anthropic", model_for_provider),
                        provider="anthropic",
                        provider_chain=("anthropic",),
                        model_for_provider=model_for_provider,
                        max_tokens=max_tokens,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "respond emergency fallback hop failed: %s",
                        exc,
                        extra=obs_fields(ctx),
                    )
                    raise RespondCapNoOutput() from exc
                emergency_blocks = [
                    _block_to_dict(block)
                    for block in (_attr(emergency_response, "content", []) or [])
                ]
                messages.append({"role": "assistant", "content": emergency_blocks})
                emergency_text = "\n".join(
                    str(block.get("text", "")).strip()
                    for block in emergency_blocks
                    if block.get("type") == "text"
                    and str(block.get("text", "")).strip()
                )
                if emergency_text:
                    return emergency_text, messages, tool_call_count
                raise RespondCapNoOutput()
            if current_step in {"record", "schedule"}:
                # Post-send caps are turn-survivable.  Surface a local marker
                # that ``_run_agentic`` catches; do NOT mutate
                # ``bot_turns.failure_reason`` or inbound rows.
                raise _PostSendPhaseCapExceeded(
                    step=current_step,
                    cap=max_tool_iterations,
                    tool_iteration_count=tool_iteration_count,
                )
            raise BoundedLoopExceeded(
                f"tool iteration cap exceeded: {max_tool_iterations}"
            )
        tool_results: list[dict[str, Any]] = []
        for tool_use in tool_uses:
            if tool_use["name"] != "update_turn_plan":
                tool_call_count += 1
            result = await call_tool(tool_use["name"], tool_use.get("input") or {}, ctx)
            is_error = bool(result.get("is_error") or result.get("error"))
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use["id"],
                    "content": json.dumps(result, default=str),
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": tool_results})

        # ── Recoverable validation-error correction cap ─────────────────
        _iter_has_recoverable = any(
            bool(tr.get("is_error"))
            for tr in tool_results
            if _tool_result_payload(tr).get("retryable") is True
        )
        if _iter_has_recoverable:
            consecutive_recoverable_errors += 1
        else:
            consecutive_recoverable_errors = 0

        if consecutive_recoverable_errors >= 2:
            _last_failed = tool_results[-1] if tool_results else {}
            _last_payload = _tool_result_payload(_last_failed)
            await record_turn_event(
                ctx.pool,
                ctx.turn_id,
                "tool.validation_cap_exceeded",
                step=ctx.current_step,
                severity="error",
                actor="tool",
                metadata={
                    "tool_name": _last_payload.get("tool_name", "unknown"),
                    "error_code": _last_payload.get("error_code"),
                    "field": _last_payload.get("field"),
                    "correction_hint": _last_payload.get("correction_hint"),
                    "consecutive_recoverable_errors": consecutive_recoverable_errors,
                },
            )
            raise BoundedLoopExceeded(
                "tool_validation_recoverable_exhausted"
            )


def _tool_result_payload(tr: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON content of a tool_result dict back into a payload dict."""
    raw = tr.get("content", "{}")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def set_pool(pool: Any) -> None:
    global _pool
    _pool = pool


def _trigger_charge(hot_context: Any) -> str | None:
    messages = hot_context.trigger_metadata.get("messages", [])
    for message in messages:
        charge = message.get("charge")
        if charge in {"crisis", "charged"}:
            return charge
    return messages[0].get("charge") if messages else None


def _explicit_partner_alert_requested(hot_context: Any) -> bool:
    if bool(hot_context.trigger_metadata.get("explicit_partner_alert_requested")):
        return True
    messages = hot_context.trigger_metadata.get("messages", [])
    for message in messages:
        content = str(message.get("content") or "").lower()
        if not content:
            continue
        asks_to_alert = any(
            phrase in content for phrase in ("tell", "alert", "let", "message", "ask")
        )
        names_partner = any(
            phrase in content for phrase in ("partner", "him", "her", "them")
        )
        if asks_to_alert and names_partner:
            return True
    return False


def _collect_reasoning(messages: list[dict[str, Any]], final_text: str = "") -> str:
    fragments: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        blocks = (
            content
            if isinstance(content, list)
            else [{"type": "text", "text": content}]
        )
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", "")).strip()
                if text and text != final_text:
                    fragments.append(text)
    return "\n".join(fragments)


async def _append_reasoning(pool: Any, turn_id: UUID, note: str) -> None:
    if not note:
        return
    existing = await pool.fetchval(
        "SELECT COALESCE(reasoning, '') FROM bot_turns WHERE id=$1", turn_id
    )
    updated = f"{existing or ''}\n{note}"
    await pool.execute(
        "UPDATE bot_turns SET reasoning=$1, reasoning_encrypted=$2 WHERE id=$3",
        updated,
        encrypt_value(updated),
        turn_id,
    )


def _extract_reaction_directive(text: str) -> tuple[str | None, str]:
    emoji: str | None = None
    kept_lines: list[str] = []
    for raw_line in text.splitlines():
        match = REACTION_DIRECTIVE_RE.match(raw_line)
        if match and emoji is None:
            emoji = match.group("emoji").strip()
            continue
        kept_lines.append(raw_line)
    return emoji, "\n".join(kept_lines).strip()


async def _react_to_triggering_message(
    pool: Any,
    user: User,
    triggering_message_ids: list[UUID],
    emoji: str,
    *,
    bot_id: str,
) -> bool:
    settings = get_settings()
    if (
        settings.messaging_provider.strip().lower() != "discord"
        or not triggering_message_ids
    ):
        return False
    row = await pool.fetchrow(
        """
        SELECT whatsapp_message_id
        FROM messages
        WHERE id=$1 AND direction='inbound' AND sender_id=$2
        """,
        triggering_message_ids[-1],
        user.id,
    )
    if row is None or not row.get("whatsapp_message_id"):
        return False
    await discord.add_reaction(
        user.phone, row["whatsapp_message_id"], emoji, bot_id=bot_id
    )
    return True


async def _check_outbound_oob(
    pool: Any,
    content: str,
    recipient_id: UUID,
    protected_owner_ids: list[UUID] | None = None,
    *,
    scope: InboundScope,
) -> dict[str, Any]:
    hook = hooks.check_oob
    if hook is None:
        return {
            "verdict": "ok",
            "reason": "OOB hook disabled",
            "suggested_rewrite": None,
            "checker_failed": False,
        }
    try:
        verdict = await hook(
            pool,
            content,
            recipient_id,
            protected_owner_ids=protected_owner_ids,
            bot_id=scope.bot_id,
            topic_id=scope.topic_id,
        )
    except TypeError:
        try:
            verdict = await hook(
                pool, content, recipient_id, protected_owner_ids=protected_owner_ids
            )
        except TypeError:
            try:
                verdict = await hook(pool, content, recipient_id)
            except TypeError:
                verdict = await hook(content, recipient_id)
    if hasattr(verdict, "model_dump"):
        verdict = verdict.model_dump(mode="json")
    verdict.setdefault("suggested_rewrite", verdict.get("rewrite"))
    verdict.setdefault("reason", "")
    verdict.setdefault("checker_failed", False)
    return verdict


async def _resolve_outbound_text(
    pool: Any,
    turn_id: UUID,
    user: User,
    content: str,
    protected_owner_ids: list[UUID] | None = None,
    *,
    scope: InboundScope,
) -> str | None:
    verdict = await _check_outbound_oob(
        pool, content, user.id, protected_owner_ids, scope=scope
    )
    if verdict["verdict"] == "ok":
        if verdict.get("checker_failed"):
            await _append_reasoning(
                pool,
                turn_id,
                f"OOB checker failed open before send: {verdict['reason']}",
            )
        return content
    if verdict["verdict"] == "block":
        await _append_reasoning(
            pool,
            turn_id,
            f"Outbound blocked before send by OOB checker: {verdict['reason']}",
        )
        return None
    suggested = (verdict.get("suggested_rewrite") or "").strip()
    if not suggested:
        await _append_reasoning(
            pool,
            turn_id,
            f"Outbound rewrite requested but no rewrite was supplied: {verdict['reason']}",
        )
        return None
    second = await _check_outbound_oob(
        pool, suggested, user.id, protected_owner_ids, scope=scope
    )
    if second["verdict"] != "ok":
        await _append_reasoning(
            pool,
            turn_id,
            f"Outbound rewrite was not sendable: first={verdict['reason']} second={second['reason']}",
        )
        return None
    await _append_reasoning(
        pool,
        turn_id,
        f"Outbound rewritten by OOB checker before send: {verdict['reason']}",
    )
    return suggested


async def _open_turn(
    conn: Any,
    triggering_message_ids: list[UUID],
    user: User,
    prompt_snapshot: str,
    model_version: str,
    system_prompt_version: str,
    *,
    bot_id: str,
    topic_id: UUID | None,
    bot_spec_version: str,
    hot_context_builder_version: str,
    tool_schema_version: str,
) -> tuple[UUID, datetime]:
    row = await conn.fetchrow(
        """
        INSERT INTO bot_turns (
            triggered_by_message_id, triggering_message_ids, user_in_context,
            system_prompt_version, model_version, prompt_snapshot, prompt_snapshot_encrypted, started_at,
            bot_id, topic_id, bot_spec_version, hot_context_builder_version, tool_schema_version
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, now(), $8, $9, $10, $11, $12)
        RETURNING id, started_at
        """,
        triggering_message_ids[0] if triggering_message_ids else None,
        triggering_message_ids,
        user.id,
        system_prompt_version,
        model_version,
        prompt_snapshot,
        encrypt_value(prompt_snapshot),
        bot_id,
        topic_id,
        bot_spec_version,
        hot_context_builder_version,
        tool_schema_version,
    )
    try:
        started_at = row["started_at"]
    except KeyError:
        started_at = datetime.now(UTC)
    return row["id"], started_at


async def _complete_turn(
    conn: Any,
    turn_id: UUID,
    started_at: datetime,
    final_output_message_id: UUID | None,
    tool_call_count: int,
    reasoning: str,
) -> None:
    duration_ms = max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000))
    existing = await conn.fetchval(
        "SELECT COALESCE(reasoning, '') FROM bot_turns WHERE id=$1", turn_id
    )
    note = f"\n{reasoning}" if reasoning else ""
    updated_reasoning = f"{existing or ''}{note}"
    await conn.execute(
        """
        UPDATE bot_turns
        SET final_output_message_id=$1,
            reasoning=$2,
            reasoning_encrypted=$3,
            completed_at=now(),
            duration_ms=$4,
            tool_call_count=$5
        WHERE id=$6
        """,
        final_output_message_id,
        updated_reasoning,
        encrypt_value(updated_reasoning),
        duration_ms,
        tool_call_count,
        turn_id,
    )
    await record_turn_event(
        conn,
        turn_id,
        "turn.completed",
        duration_ms=duration_ms,
        metadata={
            "final_output_message_id": final_output_message_id,
            "tool_call_count": tool_call_count,
        },
    )


async def _finalize_turn_atomically(
    pool: Any,
    turn_id: UUID,
    started_at: datetime,
    final_output_message_id: UUID | None,
    tool_call_count: int,
    reasoning: str,
    *,
    message_ids: list[UUID] | None = None,
    outcome: str,
    scope: InboundScope,
    primary_topic_id: UUID | None = None,
    failure_reason: str | None = None,
    failure_class: str | None = None,
    processing_error: str | None = None,
) -> None:
    """Complete a bot_turn and mark its inbound messages in one transaction.

    Acquires a connection, opens a transaction, sets LIFECYCLE WRITER to
    'inbound_queue', then calls _complete_turn followed by either
    _complete_messages_in_tx or _fail_messages_in_tx.

    outcome='failed' triggers _fail_messages_in_tx (requires failure_class,
    failure_reason, processing_error).  All other outcomes use
    _complete_messages_in_tx.

    When message_ids is None or empty, only _complete_turn runs (SpendCap
    path where messages were already deferred).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_AGENTIC_LIFECYCLE_WRITER_SQL)
            await _complete_turn(
                conn,
                turn_id,
                started_at,
                final_output_message_id,
                tool_call_count,
                reasoning,
            )
            # Stamp failure_reason on the bot_turn when this is a
            # pre-send failure path.  _complete_turn already set
            # completed_at=now(), so the turn is terminal regardless.
            if failure_reason:
                await conn.execute(
                    "UPDATE bot_turns SET failure_reason=$1 WHERE id=$2",
                    failure_reason,
                    turn_id,
                )
                await record_turn_event(
                    conn,
                    turn_id,
                    "turn.failed",
                    severity="error",
                    metadata={
                        "failure_reason": failure_reason,
                        "failure_class": failure_class or "infra_bug",
                    },
                )
            if message_ids:
                if outcome == "failed":
                    await inbound_queue._fail_messages_in_tx(
                        conn,
                        message_ids,
                        processing_error=processing_error or "finalize_turn_atomically",
                        handled_by_turn_id=turn_id,
                        bot_id=scope.bot_id,
                        topic_id=primary_topic_id,
                        failure_class=failure_class or "infra_bug",
                        failure_reason=failure_reason,
                    )
                else:
                    await inbound_queue._complete_messages_in_tx(
                        conn,
                        message_ids,
                        handling_result=outcome,
                        handled_by_turn_id=turn_id,
                        bot_id=scope.bot_id,
                        topic_id=primary_topic_id,
                    )


async def _record_turn_final_output(
    pool: Any, turn_id: UUID, final_output_message_id: UUID
) -> None:
    await pool.execute(
        """
        UPDATE bot_turns
        SET final_output_message_id=$1
        WHERE id=$2
        """,
        final_output_message_id,
        turn_id,
    )


def _failure_class_for(failure_reason: str) -> str:
    """Map a failure_reason string to a durable-queue failure class.

    Delegates to the 3-class taxonomy defined in
    ``app.services.inbound_queue.FAILURE_REASON_TO_CLASS``.  Unknown reasons
    fall through to ``"infra_bug"`` (the recovery-v2 safest default).
    """
    return inbound_queue.FAILURE_REASON_TO_CLASS.get(failure_reason, "infra_bug")


async def _defer_for_text_cap(
    pool: Any,
    user: User,
    message_ids: list[UUID],
    *,
    bot_id: str | None = None,
    topic_id: UUID | None = None,
) -> bool:
    if message_ids and bot_id is not None and topic_id is not None:
        await inbound_queue.defer_messages(
            pool,
            message_ids,
            bot_id=bot_id,
            topic_id=topic_id,
        )
    context_payload: dict[str, Any] = {
        "triggering_message_ids": [str(message_id) for message_id in message_ids],
        "reason": "text_spend_cap",
    }
    if bot_id is not None:
        context_payload["bot_id"] = bot_id
    if topic_id is not None:
        context_payload["topic_id"] = str(topic_id)
    row = await pool.fetchrow(
        """
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status, bot_id, topic_id)
        SELECT $1, 'deferred_turn', $2, $3::jsonb, 'pending', $4, $5
        WHERE NOT EXISTS (
            SELECT 1 FROM scheduled_jobs
            WHERE user_id = $1 AND job_type = 'deferred_turn' AND status = 'pending'
        )
        RETURNING id, scheduled_for
        """,
        user.id,
        datetime.now(UTC) + timedelta(days=1),
        context_payload,
        bot_id,
        topic_id,
    )
    return row is not None


async def _newer_inbound_exists(
    pool: Any,
    user: User,
    triggering_message_ids: list[UUID],
    *,
    fallback_started_at: datetime | None = None,
    bot_id: str,
) -> bool:
    boundary = fallback_started_at
    if triggering_message_ids:
        trigger_boundary = await pool.fetchval(
            "SELECT MAX(sent_at) FROM messages WHERE id = ANY($1::uuid[])",
            triggering_message_ids,
        )
        if trigger_boundary is not None:
            boundary = trigger_boundary
    if boundary is None:
        return False
    return bool(
        await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM messages
                WHERE direction='inbound'
                  AND sender_id=$1
                  AND sent_at > $2
                  AND NOT (id = ANY($3::uuid[]))
                  AND bot_id = $4
            )
            """,
            user.id,
            boundary,
            triggering_message_ids,
            bot_id,
        )
    )


STEP_ITERATION_CAPS = {
    "read": 6,
    "consult": 1,
    "respond": 4,
    "record": 8,
    "schedule": 4,
    "done": 0,
}


def _allowed_tools_for_step(ctx: TurnContext) -> set[str]:
    allowed = set(STEP_ALLOWED_TOOLS.get(ctx.current_step, set())) | {
        "update_turn_plan"
    }
    if ctx.current_step == "respond" and not ctx.incremental_sending_enabled:
        allowed.discard("send_message_part")
    return allowed


def _sent_summary(
    delivered_parts: list[str], assistant_text: str, reaction_emoji: str | None
) -> str:
    if delivered_parts:
        return (
            f"You actually sent {len(delivered_parts)} message"
            f"{'' if len(delivered_parts) == 1 else 's'}:\n"
            + "\n\n".join(
                f"{idx + 1}. {content}" for idx, content in enumerate(delivered_parts)
            )
        )
    return f"You sent: {f'[reaction {reaction_emoji}]' if reaction_emoji else (assistant_text or '[silence]')}"


def _build_hot_context_signals(hot_context: Any) -> dict[str, Any]:
    return {
        "recent_message_count": len(getattr(hot_context, "recent_messages", []) or []),
        "open_watch_item_count": len(
            getattr(hot_context, "open_watch_items", []) or []
        ),
        "active_oob_count": len(getattr(hot_context, "active_oob", []) or []),
    }


async def _run_agentic(
    triggering_message_ids: list[UUID],
    user: User,
    *,
    scope: InboundScope,
    trigger_metadata: dict[str, Any] | None = None,
    pool: Any | None = None,
    prompt_version: str | None = None,
    before_paced_send: BeforePacedSend | None = None,
) -> None:
    active_pool = pool or _pool
    if active_pool is not None and await system_state.is_paused(active_pool):
        return
    if active_pool is None:
        raise RuntimeError("agentic pool has not been set")

    settings = get_settings()
    bot_spec = get_bot_spec(scope.bot_id)
    primary_topic_id = scope.topic_id or await primary_topic_id_for(
        active_pool, bot_spec
    )
    selected_prompt_version = prompt_version or settings.system_prompt_version
    # Resolve the per-bot provider chain (bot_spec + per-user allowlist gate).
    resolved_chain = _resolve_provider_chain(bot_spec, user, settings)
    # First-hop model for the turn-open log + the back-compat conversational_model.
    first_hop_provider = resolved_chain[0]
    conversational_model = _provider_model(first_hop_provider, None)
    llm_provider = first_hop_provider
    model_for_provider = {
        "anthropic": settings.conversational_model,
        "deepseek": settings.deepseek_conversational_model,
    }
    # We no longer hold a single llm_client; _create_message_with_retry builds
    # provider clients per-hop.  Pass ``None`` and let it construct as needed.
    llm_client: Any = None
    send_typing_indicator = not bool(
        trigger_metadata and trigger_metadata.get("pacing")
    )
    turn_id: UUID | None = None
    started_at = datetime.now(UTC)
    responded_to_user = False
    # Initialized here so the outer except-handler can pass them to
    # _finalize_turn_atomically even if an exception fires before the
    # try block re-assigns them mid-turn.
    final_output_message_id: UUID | None = None
    tool_call_count = 0
    reasoning = ""

    # ── Hot-context build + atomic claim+open ───────────────────────
    # Hot context and prompt_snapshot are computed before the write
    # transaction per FLAG-RR-009 strategy (a): all read-heavy work
    # completes before acquiring the write connection.
    try:
        if bot_spec.participants_shape == "solo":
            partner = None
            hot_context = await build_hot_context_solo(
                active_pool,
                user,
                triggering_message_ids,
                trigger_metadata,
                primary_topic_id=primary_topic_id,
                bot_id=scope.bot_id,
                allow_cross_topic_peek=getattr(
                    bot_spec.read_scopes, "allow_cross_topic_peek", False
                ),
            )
            rendered_hot_context = render_hot_context_solo(hot_context)
        else:
            partner = await partner_of(active_pool, user)
            hot_context = await build_hot_context(
                active_pool,
                user,
                partner,
                triggering_message_ids,
                trigger_metadata,
                primary_topic_id=primary_topic_id,
                allow_cross_topic_peek=getattr(
                    bot_spec.read_scopes, "allow_cross_topic_peek", False
                ),
                allow_cross_topic_status_injection=getattr(
                    bot_spec.read_scopes, "allow_cross_topic_status_injection", False
                ),
            )
            rendered_hot_context = render_hot_context(hot_context)
        current_user_partner_share = await get_partner_share(
            active_pool,
            user_id=user.id,
            bot_id=scope.bot_id,
        )
        partner_partner_share = (
            await get_partner_share(
                active_pool, user_id=partner.id, bot_id=scope.bot_id
            )
            if partner is not None
            else None
        )
        hot_context_current_user = getattr(hot_context, "current_user", {}) or {}
        hot_context_partner_user = getattr(hot_context, "partner_user", {}) or {}
        current_user_partner_sharing_state = hot_context_current_user.get(
            "partner_sharing_state"
        )
        partner_partner_sharing_state = (
            hot_context_partner_user.get("partner_sharing_state")
            if partner is not None
            else None
        )
        system_prompt = bot_spec.render_system_prompt(
            assistant_name=settings.assistant_name,
            user=user,
            partner=partner,
            prompt_version=selected_prompt_version,
            current_user_partner_share=current_user_partner_share,
            partner_partner_share=partner_partner_share,
            current_user_partner_sharing_state=current_user_partner_sharing_state,
            partner_partner_sharing_state=partner_partner_sharing_state,
        )
        prompt_snapshot = f"{system_prompt}\n\n{rendered_hot_context}"
        bot_spec_version = hashlib.sha1(repr(bot_spec).encode()).hexdigest()[:12]

        # ── Atomic claim+open ────────────────────────────────────
        # Open the bot_turn and claim triggering messages in a single
        # transaction.  Zero-claim raises _ZeroClaimAbort inside the tx,
        # rolling back the bot_turn INSERT.  bot_turn_id is stamped on
        # messages by the claim CTE (in-flight ownership); the legacy
        # UPDATE messages SET handled_by_turn_id is gone per the
        # column-semantics contract (handled_by_turn_id is terminal-only).
        claimed_message_ids: list[UUID] = []
        if triggering_message_ids:
            async with active_pool.acquire() as claim_conn:
                async with claim_conn.transaction():
                    await claim_conn.execute(_AGENTIC_LIFECYCLE_WRITER_SQL)
                    turn_id, started_at = await _open_turn(
                        claim_conn,
                        triggering_message_ids,
                        user,
                        prompt_snapshot,
                        conversational_model,
                        selected_prompt_version,
                        bot_id=scope.bot_id,
                        topic_id=primary_topic_id,
                        bot_spec_version=bot_spec_version,
                        hot_context_builder_version=bot_spec.hot_context_builder_version,
                        tool_schema_version=_TOOL_SCHEMA_VERSION,
                    )
                    claimed_message_ids = (
                        await inbound_queue._claim_messages_for_turn_in_tx(
                            claim_conn,
                            triggering_message_ids,
                            bot_id=scope.bot_id,
                            topic_id=primary_topic_id,
                            new_bot_turn_id=turn_id,
                        )
                    )
                    if not claimed_message_ids:
                        raise _ZeroClaimAbort()
        else:
            # No triggering messages (scheduled job, etc.) — open turn
            # without claiming.
            async with active_pool.acquire() as claim_conn:
                async with claim_conn.transaction():
                    await claim_conn.execute(_AGENTIC_LIFECYCLE_WRITER_SQL)
                    turn_id, started_at = await _open_turn(
                        claim_conn,
                        triggering_message_ids,
                        user,
                        prompt_snapshot,
                        conversational_model,
                        selected_prompt_version,
                        bot_id=scope.bot_id,
                        topic_id=primary_topic_id,
                        bot_spec_version=bot_spec_version,
                        hot_context_builder_version=bot_spec.hot_context_builder_version,
                        tool_schema_version=_TOOL_SCHEMA_VERSION,
                    )

        await record_turn_event(
            active_pool,
            turn_id,
            "turn.opened",
            metadata={
                "triggered_by_message_id": (
                    triggering_message_ids[0] if triggering_message_ids else None
                ),
                "triggering_message_count": len(triggering_message_ids),
                "user_in_context": user.id,
                "model_version": conversational_model,
                "llm_provider": "->".join(resolved_chain),
                "system_prompt_version": selected_prompt_version,
                "bot_id": scope.bot_id,
                "topic_id": str(primary_topic_id) if primary_topic_id else None,
                "channel_id": scope.channel_id,
                "binding_id": (
                    str(scope.binding_id) if scope.binding_id is not None else None
                ),
                "dyad_id": str(scope.dyad_id) if scope.dyad_id is not None else None,
                "transport": scope.transport,
            },
        )
        charge = _trigger_charge(hot_context)
        explicit_partner_alert_requested = _explicit_partner_alert_requested(
            hot_context
        )
        hot_context_signals = _build_hot_context_signals(hot_context)
        hot_context_signals["bot_id"] = scope.bot_id
        hot_context_signals["primary_topic_slug"] = bot_spec.primary_topic_slug
        skeleton_name = pick_default_skeleton(
            trigger_metadata=hot_context.trigger_metadata,
            charge=charge,
            hot_context_signals=hot_context_signals,
        )
        turn_plan = make_turn_plan(skeleton_name)
        ctx = TurnContext.from_scope(
            scope=scope,
            turn_id=turn_id,
            pool=active_pool,
            user=user,
            partner=partner,
            triggering_message_ids=triggering_message_ids,
            bot_spec=bot_spec,
            participants_shape=bot_spec.participants_shape,
            primary_topic_slug=bot_spec.primary_topic_slug,
            read_scopes=bot_spec.read_scopes,
            write_scopes=bot_spec.write_scopes,
            cross_topic_policy=bot_spec.cross_topic_policy,
            current_step=turn_plan.current,
            turn_plan=turn_plan,
            trigger_charge=charge,
            explicit_partner_alert_requested=explicit_partner_alert_requested,
            turn_started_at=started_at,
            incremental_sending_enabled=(
                settings.messaging_provider.strip().lower() == "discord"
                and settings.discord_multi_message_enabled
            ),
            protected_owner_ids=[user.id] if partner is None else [user.id, partner.id],
            send_typing_indicator=send_typing_indicator,
            before_paced_send=before_paced_send,
            sent_message_parts=[],
            hot_context_rendered=rendered_hot_context,
            trigger_metadata=hot_context.trigger_metadata,
        )
        seed_messages = bot_spec.build_initial_seed(
            trigger_metadata=hot_context.trigger_metadata,
            triggering_message_ids=triggering_message_ids,
            charge=charge,
            orient_header=orient_summary(
                trigger_metadata=hot_context.trigger_metadata,
                charge=charge,
                hot_context_signals=hot_context_signals,
            ),
            plan=turn_plan,
        )
        messages = seed_messages
        tool_call_count = 0
        assistant_text = ""
        respond_text = ""
        reaction_emoji: str | None = None
        sent_summary_for_record: str | None = None
        final_output_message_id: UUID | None = None
        provider_send_failed: bool = False
        reasoning_parts: list[str] = []
        delivered_parts: list[str] = []

        while turn_plan.current != "done":
            ctx.current_step = turn_plan.current
            step_started_at = datetime.now(UTC)
            await record_turn_event(
                active_pool,
                turn_id,
                "step.started",
                step=ctx.current_step,
                metadata={"skeleton_name": turn_plan.skeleton_name},
            )
            try:
                step_text, messages, step_tool_count = await run_step(
                    llm_client,
                    ctx,
                    system_prompt,
                    rendered_hot_context,
                    _allowed_tools_for_step(ctx),
                    messages,
                    model=conversational_model,
                    provider=llm_provider,
                    provider_chain=resolved_chain,
                    model_for_provider=model_for_provider,
                    max_tool_iterations=STEP_ITERATION_CAPS.get(ctx.current_step, 4),
                )
            except _PostSendPhaseCapExceeded as cap_exc:
                # Post-send cap (record / schedule).  Audit-events only; do
                # NOT touch bot_turns.failure_reason or inbound rows so the
                # turn continues to look like a successful 'replied' turn.
                await record_turn_event(
                    active_pool,
                    turn_id,
                    "phase_cap.post_send_exceeded",
                    step=ctx.current_step,
                    severity="warning",
                    metadata={
                        "cap": cap_exc.cap,
                        "tool_iteration_count": cap_exc.tool_iteration_count,
                    },
                )
                step_text = ""
                step_tool_count = 0
            except Exception as exc:
                await record_turn_event(
                    active_pool,
                    turn_id,
                    "step.failed",
                    step=ctx.current_step,
                    severity="error",
                    duration_ms=max(
                        0,
                        int(
                            (datetime.now(UTC) - step_started_at).total_seconds() * 1000
                        ),
                    ),
                    metadata={"exception_type": type(exc).__name__},
                )
                raise
            await record_turn_event(
                active_pool,
                turn_id,
                "step.completed",
                step=ctx.current_step,
                duration_ms=max(
                    0, int((datetime.now(UTC) - step_started_at).total_seconds() * 1000)
                ),
                metadata={
                    "tool_call_count": step_tool_count,
                    "assistant_text_present": bool(step_text),
                },
            )
            tool_call_count += step_tool_count

            if ctx.current_step == "respond":
                assistant_text = step_text
                # Note: inbound messages are now marked terminal by
                # inbound_queue.complete_messages / fail_messages after the
                # turn completes (see the normal-path finally and exception
                # handler below).  The old early raw→processed UPDATE has
                # been removed (durable-inbound-queue-hardening T4).

                sent_parts = ctx.sent_message_parts or []
                final_output_message_id = (
                    sent_parts[-1]["message_id"]
                    if sent_parts
                    else final_output_message_id
                )
                responded_to_user = responded_to_user or bool(sent_parts)
                if sent_parts and assistant_text:
                    await _append_reasoning(
                        active_pool,
                        turn_id,
                        "Suppressed final respond text because send_message_part already delivered user-visible text.",
                    )
                    assistant_text = ""
                elif assistant_text:
                    assistant_text = clean_user_facing_text(assistant_text)
                    reaction_emoji, assistant_text = _extract_reaction_directive(
                        assistant_text
                    )
                    if reaction_emoji is not None:
                        if await _react_to_triggering_message(
                            active_pool,
                            user,
                            triggering_message_ids,
                            reaction_emoji,
                            bot_id=scope.bot_id,
                        ):
                            await _append_reasoning(
                                active_pool,
                                turn_id,
                                f"Reacted to triggering message with {reaction_emoji}.",
                            )
                            await claim_onboarding_welcome(active_pool, user.id)
                            responded_to_user = True
                    if assistant_text:
                        dyad_owner_ids = ctx.protected_owner_ids
                        sendable_text = await _resolve_outbound_text(
                            active_pool,
                            turn_id,
                            user,
                            assistant_text,
                            dyad_owner_ids,
                            scope=scope,
                        )
                        already_sent = [part["content"] for part in sent_parts]
                        if sendable_text is None:
                            await record_turn_event(
                                active_pool,
                                turn_id,
                                "outbound.withheld",
                                step=ctx.current_step,
                                severity="warning",
                                actor="delivery",
                                message="Final outbound was not sendable after safety checks.",
                                metadata={"reason": "safety_check"},
                            )
                        elif sendable_text and sendable_text not in already_sent:
                            if await _newer_inbound_exists(
                                active_pool,
                                user,
                                triggering_message_ids,
                                fallback_started_at=started_at,
                                bot_id=ctx.bot_id,
                            ):
                                await _append_reasoning(
                                    active_pool,
                                    turn_id,
                                    "Final outbound skipped because a newer inbound message arrived before send.",
                                )
                                await record_turn_event(
                                    active_pool,
                                    turn_id,
                                    "outbound.withheld",
                                    step=ctx.current_step,
                                    severity="warning",
                                    actor="delivery",
                                    message="Final outbound skipped because a newer inbound arrived.",
                                    metadata={"reason": "newer_inbound_before_send"},
                                )
                                assistant_text = ""
                            else:

                                async def before_final_provider_send(
                                    text: str = sendable_text,
                                ) -> None:
                                    if (
                                        before_paced_send is not None
                                        and not send_typing_indicator
                                    ):
                                        await before_paced_send(
                                            text, send_kind="final", part_index=None
                                        )
                                    if await _newer_inbound_exists(
                                        active_pool,
                                        user,
                                        triggering_message_ids,
                                        fallback_started_at=started_at,
                                        bot_id=ctx.bot_id,
                                    ):
                                        raise NewerInboundBeforeFinalSend()

                                try:
                                    send_result = await send_outbound(
                                        active_pool,
                                        user,
                                        sendable_text,
                                        bot_turn_id=turn_id,
                                        protected_owner_ids=dyad_owner_ids,
                                        send_typing_indicator=send_typing_indicator,
                                        scope=scope,
                                        before_provider_send=(
                                            before_final_provider_send
                                            if before_paced_send is not None
                                            and not send_typing_indicator
                                            else None
                                        ),
                                    )
                                    final_output_message_id = send_result["message_id"]
                                    provider_send_failed = send_result["status"] == "provider_failed"
                                    provider_visible = send_result["visible_to_user"]
                                except NewerInboundBeforeFinalSend:
                                    await _append_reasoning(
                                        active_pool,
                                        turn_id,
                                        "Final outbound skipped because a newer inbound message arrived during paced send.",
                                    )
                                    await record_turn_event(
                                        active_pool,
                                        turn_id,
                                        "outbound.withheld",
                                        step=ctx.current_step,
                                        severity="warning",
                                        actor="delivery",
                                        message="Final outbound skipped because a newer inbound arrived during paced send.",
                                        metadata={
                                            "reason": "newer_inbound_during_send"
                                        },
                                    )
                                    assistant_text = ""
                                else:
                                    if send_result["status"] == "provider_failed":
                                        if send_result["visible_to_user"]:
                                            # At least one chunk was delivered before
                                            # the failure.  Record the last successful
                                            # chunk as final output and treat the
                                            # inbound as terminal replied.
                                            await _record_turn_final_output(
                                                active_pool,
                                                turn_id,
                                                final_output_message_id,
                                            )
                                            await record_turn_event(
                                                active_pool,
                                                turn_id,
                                                "outbound.sent_partial",
                                                step=ctx.current_step,
                                                actor="delivery",
                                                metadata={
                                                    "message_id": final_output_message_id,
                                                    "send_kind": "final",
                                                    "partial_failure": True,
                                                },
                                            )
                                            await claim_onboarding_welcome(
                                                active_pool, user.id
                                            )
                                            assistant_text = sendable_text
                                            responded_to_user = True
                                        else:
                                            # No chunk was visible to the user.
                                            # Clear final_output_message_id so
                                            # _complete_turn does not record a
                                            # failed outbound row as the turn's
                                            # final output.
                                            final_output_message_id = None
                                    else:
                                        await _record_turn_final_output(
                                            active_pool, turn_id, final_output_message_id
                                        )
                                        await record_turn_event(
                                            active_pool,
                                            turn_id,
                                            "outbound.sent",
                                            step=ctx.current_step,
                                            actor="delivery",
                                            metadata={
                                                "message_id": final_output_message_id,
                                                "send_kind": "final",
                                            },
                                        )
                                        await claim_onboarding_welcome(active_pool, user.id)
                                        assistant_text = sendable_text
                                        responded_to_user = True
                        elif sendable_text:
                            assistant_text = sendable_text
                elif charge in {"charged", "crisis"}:
                    await _append_reasoning(
                        active_pool,
                        turn_id,
                        "silence; charged trigger but no justification produced",
                    )
                    logger.warning(
                        "charged/crisis trigger produced silence without model justification turn_id=%s",
                        turn_id,
                        extra=obs_fields(ctx),
                    )

                respond_text = assistant_text
                delivered_parts = [
                    part["content"] for part in (ctx.sent_message_parts or [])
                ]
                if not delivered_parts and turn_id is not None:
                    delivered_parts = await sent_contents_for_turn(active_pool, turn_id)
                sent_summary_for_record = _sent_summary(
                    delivered_parts, respond_text, reaction_emoji
                )

            if step_text:
                reasoning_parts.append(
                    _collect_reasoning(
                        messages, step_text if ctx.current_step == "respond" else ""
                    )
                )

            previous_step = ctx.current_step
            next_step = turn_plan.advance()
            if next_step != "done":
                messages.append(
                    bot_spec.build_step_transition_message(
                        plan=turn_plan,
                        sent_summary=(
                            sent_summary_for_record
                            if next_step in {"record", "schedule"}
                            else None
                        ),
                    )
                )
            if previous_step == next_step:
                raise BoundedLoopExceeded(
                    f"turn plan did not advance from step {previous_step}"
                )

        reasoning = "\n".join(part for part in reasoning_parts if part)
        executed_plan = (
            f"Executed turn plan ({turn_plan.skeleton_name}): {turn_plan.trace()}"
        )
        reasoning = "\n".join(part for part in (reasoning, executed_plan) if part)

        # ── Finalize turn + messages atomically ────────────────────────
        if claimed_message_ids:
            if responded_to_user:
                handling_result = "replied"
            elif provider_send_failed:
                # Provider failed and no user-visible delivery occurred at all.
                # Mark as failed for retry (not terminal 'replied').
                failure_reason = "provider_send_failed"
                failure_class = _failure_class_for(failure_reason)
                error_detail = (
                    f"provider_send_failed"
                    f" [failure_class={failure_class}, retryable=true]"
                )
                await _finalize_turn_atomically(
                    active_pool,
                    turn_id,
                    started_at,
                    final_output_message_id,
                    tool_call_count,
                    reasoning,
                    message_ids=claimed_message_ids,
                    outcome="failed",
                    scope=scope,
                    primary_topic_id=primary_topic_id,
                    failure_reason=failure_reason,
                    failure_class=failure_class,
                    processing_error=error_detail,
                )
                return
            elif assistant_text and not responded_to_user:
                # Bot produced text but it was withheld (newer inbound, OOB block)
                handling_result = "withheld_newer_inbound"
            elif not assistant_text and not responded_to_user:
                # Bot intentionally stayed silent
                handling_result = "silent"
            else:
                handling_result = "no_action"
            await _finalize_turn_atomically(
                active_pool,
                turn_id,
                started_at,
                final_output_message_id,
                tool_call_count,
                reasoning,
                message_ids=claimed_message_ids,
                outcome=handling_result,
                scope=scope,
                primary_topic_id=primary_topic_id,
            )
        else:
            await _finalize_turn_atomically(
                active_pool,
                turn_id,
                started_at,
                final_output_message_id,
                tool_call_count,
                reasoning,
                outcome="no_action",
                scope=scope,
                primary_topic_id=primary_topic_id,
            )
        return

    except _ZeroClaimAbort:
        logger.info(
            "_run_agentic: zero of %d triggering messages claimable "
            "bot_id=%s topic_id=%s — aborting (bot_turn rolled back)",
            len(triggering_message_ids),
            scope.bot_id,
            str(primary_topic_id),
        )
        return

    except SpendCapExceeded:
        if turn_id is not None:
            scheduled = await _defer_for_text_cap(
                active_pool,
                user,
                triggering_message_ids,
                bot_id=scope.bot_id,
                topic_id=primary_topic_id,
            )
            final_output_message_id = None
            if scheduled:
                fallback_text = "I'm running into limits today, will catch up tomorrow."

                async def before_fallback_provider_send(
                    text: str = fallback_text,
                ) -> None:
                    if before_paced_send is not None and not send_typing_indicator:
                        await before_paced_send(
                            text, send_kind="final", part_index=None
                        )
                    if await _newer_inbound_exists(
                        active_pool,
                        user,
                        triggering_message_ids,
                        fallback_started_at=started_at,
                        bot_id=ctx.bot_id,
                    ):
                        raise NewerInboundBeforeFinalSend()

                if await _newer_inbound_exists(
                    active_pool,
                    user,
                    triggering_message_ids,
                    fallback_started_at=started_at,
                    bot_id=ctx.bot_id,
                ):
                    await _append_reasoning(
                        active_pool,
                        turn_id,
                        "Spend cap fallback skipped because a newer inbound message arrived before send.",
                    )
                else:
                    try:
                        fallback_result = await send_outbound(
                            active_pool,
                            user,
                            fallback_text,
                            bot_turn_id=turn_id,
                            send_typing_indicator=send_typing_indicator,
                            scope=scope,
                            before_provider_send=(
                                before_fallback_provider_send
                                if before_paced_send is not None
                                and not send_typing_indicator
                                else None
                            ),
                        )
                        final_output_message_id = fallback_result["message_id"]
                    except NewerInboundBeforeFinalSend:
                        await _append_reasoning(
                            active_pool,
                            turn_id,
                            "Spend cap fallback skipped because a newer inbound message arrived during paced send.",
                        )
            await _finalize_turn_atomically(
                active_pool,
                turn_id,
                started_at,
                final_output_message_id,
                0,
                "Text LLM spend cap hit; deferred original trigger messages for next-day retry.",
                outcome="replied",
                scope=scope,
                primary_topic_id=primary_topic_id,
            )
            return
        # No turn was opened before spend cap was hit — defer the messages
        # instead of failing them so they can be retried later.
        if claimed_message_ids:
            await inbound_queue.defer_messages(
                active_pool,
                claimed_message_ids,
                bot_id=scope.bot_id,
                topic_id=primary_topic_id,
            )
            logger.info(
                "SpendCapExceeded before turn opened: deferred %d claimed messages"
                " bot_id=%s topic_id=%s",
                len(claimed_message_ids),
                scope.bot_id,
                str(primary_topic_id),
            )
        return
    except Exception as exc:
        failure_reason = getattr(exc, "failure_reason", "crashed")
        # Collect structured metadata from the exception when available
        fail_metadata: dict[str, Any] | None = None
        if hasattr(exc, "result"):
            exc_result = getattr(exc, "result") or {}
            if isinstance(exc_result, dict):
                fail_metadata = {
                    k: v
                    for k in (
                        "error_code",
                        "field",
                        "retryable",
                        "correction_hint",
                        "failure_class",
                        "tool_name",
                    )
                    if (v := exc_result.get(k)) is not None
                }
        elif hasattr(exc, "__cause__") and exc.__cause__ is not None:
            # Chain: extract from cause exception
            cause = exc.__cause__
            if hasattr(cause, "result"):
                cause_result = getattr(cause, "result") or {}
                if isinstance(cause_result, dict):
                    fail_metadata = {
                        k: v
                        for k in (
                            "error_code",
                            "field",
                            "retryable",
                            "correction_hint",
                            "failure_class",
                            "tool_name",
                        )
                        if (v := cause_result.get(k)) is not None
                    }

        if claimed_message_ids:
            failure_class = _failure_class_for(failure_reason)
            retryable = (
                fail_metadata.get("retryable", True)
                if fail_metadata
                else True
            )
            exc_name = type(exc).__name__
            exc_msg = str(exc)
            if exc_msg:
                error_detail = (
                    f"{exc_name}: {exc_msg}"
                    f" [failure_class={failure_class}, retryable={retryable}]"
                )
            else:
                error_detail = (
                    f"{exc_name}"
                    f" [failure_class={failure_class}, retryable={retryable}]"
                )

            if responded_to_user:
                # A user-visible response already occurred — mark messages
                # terminal as 'replied' but stamp the turn failure so the
                # audit trail is complete.  _finalize_turn_atomically sets
                # completed_at + failure_reason on the turn in the same tx.
                await _finalize_turn_atomically(
                    active_pool,
                    turn_id,
                    started_at,
                    final_output_message_id,
                    tool_call_count,
                    reasoning,
                    message_ids=claimed_message_ids,
                    outcome="replied",
                    scope=scope,
                    primary_topic_id=primary_topic_id,
                    failure_reason=failure_reason,
                    failure_class=failure_class,
                )
                logger.warning(
                    "agentic turn failed after outbound was sent: %s",
                    exc,
                    extra=obs_fields(ctx),
                )
                return
            else:
                # No user-visible response — mark messages failed for
                # potential retry.  Turn completed_at + failure_reason
                # stamped atomically with the message updates.
                await _finalize_turn_atomically(
                    active_pool,
                    turn_id,
                    started_at,
                    final_output_message_id,
                    tool_call_count,
                    reasoning,
                    message_ids=claimed_message_ids,
                    outcome="failed",
                    scope=scope,
                    primary_topic_id=primary_topic_id,
                    failure_reason=failure_reason,
                    failure_class=failure_class,
                    processing_error=error_detail[:500],
                )
        raise


async def run_agentic_turn(
    triggering_message_ids: list[UUID], user: User, *, scope: InboundScope
) -> None:
    if not triggering_message_ids:
        logger.warning(
            "run_agentic_turn called without triggering messages for user_id=%s",
            user.id,
            extra={"user_id": str(user.id)},
        )
        return
    await _run_agentic(triggering_message_ids, user, scope=scope)


async def run_agentic_turn_with_metadata(
    triggering_message_ids: list[UUID],
    user: User,
    *,
    pacing_context: Any | None = None,
    trigger_metadata: Mapping[str, Any] | None = None,
    before_paced_send: BeforePacedSend | None = None,
    scope: InboundScope,
) -> None:
    if not triggering_message_ids:
        logger.warning(
            "run_agentic_turn_with_metadata called without triggering messages for user_id=%s",
            user.id,
            extra={"user_id": str(user.id)},
        )
        return
    await _run_agentic(
        triggering_message_ids,
        user,
        scope=scope,
        trigger_metadata=_trigger_metadata_with_pacing(
            trigger_metadata, pacing_context
        ),
        before_paced_send=before_paced_send,
    )


async def run_agentic_job(
    user: User, trigger_metadata: dict[str, Any], *, scope: InboundScope
) -> None:
    await _run_agentic([], user, scope=scope, trigger_metadata=trigger_metadata)


async def run_agentic_turn_with_pool(
    pool: Any,
    triggering_message_ids: list[UUID],
    user: User,
    *,
    scope: InboundScope,
    prompt_version: str,
) -> None:
    if not triggering_message_ids:
        logger.warning(
            "run_agentic_turn_with_pool called without triggering messages for user_id=%s",
            user.id,
            extra={"user_id": str(user.id)},
        )
        return
    await _run_agentic(
        triggering_message_ids,
        user,
        scope=scope,
        pool=pool,
        prompt_version=prompt_version,
    )


async def run_agentic_job_with_pool(
    pool: Any,
    user: User,
    trigger_metadata: dict[str, Any],
    *,
    scope: InboundScope,
    prompt_version: str,
) -> None:
    await _run_agentic(
        [],
        user,
        scope=scope,
        trigger_metadata=trigger_metadata,
        pool=pool,
        prompt_version=prompt_version,
    )
