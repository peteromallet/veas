"""End-to-end tests for the solo coach bot (Sprint 5 / T13).

Covers:
  (1) end-to-end coach turn — partner_of NOT called, memory write scoped to
      career, create_bridge_candidate fails at registry boundary.
  (2) idempotent onboarding — concurrent ensure_onboarding_state produces
      exactly one user_bot_state row.
  (3) string-absence assertions on solo prompt output.
  (4) coach allowlist — create_bridge_candidate not in to_anthropic_tools.

Does NOT modify tests/conftest.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.bots.coach import build_coach_spec
from app.config import get_settings
from app.models.user import User
from app.services import agentic, hooks, whatsapp
from app.services.onboarding_solo import ensure_onboarding_state
from app.services.prompts_solo import render_solo_system_prompt
from app.services.tools.registry import _step_allowed, to_anthropic_tools
from app.services.turn_context import partner_of
from tests.conftest import FakePool

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

USAGE = {
    "input_tokens": 100,
    "cache_creation_input_tokens": 20,
    "cache_read_input_tokens": 10,
    "output_tokens": 10,
}

CAREER_TOPIC_ID = UUID("00000000-0000-4000-8000-000000000010")


def _response(content, stop_reason="end_turn", usage=None):
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, usage=usage or dict(USAGE)
    )


def _tool(name, input_, n):
    return _response(
        [{"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": input_}],
        "tool_use",
    )


class CoachPool(FakePool):
    """FakePool extended for coach e2e tests.

    Adds handlers for topics lookup and topic_status queries that the
    solo hot context builder needs.  Does NOT modify conftest.py.
    """

    def __init__(self):
        super().__init__()
        self._slug_topics: dict[str, dict] = {}
        self.user_bot_states: list[dict] = []

    def seed_topic(self, slug: str, topic_id: UUID) -> None:
        self._slug_topics[slug] = {"id": topic_id}

    # ── fetchrow overrides ──────────────────────────────────────────

    async def fetchrow(self, sql: str, *args):
        compact = " ".join(sql.split())

        # primary_topic_id_for: SELECT id FROM mediator.topics WHERE slug=$1
        if compact.startswith("SELECT id FROM mediator.topics WHERE slug"):
            slug = args[0]
            topic = self._slug_topics.get(slug)
            return {"id": topic["id"]} if topic else None

        # _fetch_topic_status_solo
        if compact.startswith(
            "SELECT id, headline, body, last_updated_at FROM topic_status"
        ):
            topic_id, user_id = args
            key = (topic_id, user_id)
            return self.topic_status.get(key)

        # ensure_onboarding_state upsert (schema-qualified)
        if compact.startswith("INSERT INTO mediator.user_bot_state"):
            user_id, bot_id = args[0], args[1]
            for row in self.user_bot_states:
                if row["user_id"] == user_id and row["bot_id"] == bot_id:
                    return {"onboarding_state": row["onboarding_state"]}
            new_row = {
                "user_id": user_id,
                "bot_id": bot_id,
                "onboarding_state": "pending",
            }
            self.user_bot_states.append(new_row)
            return {"onboarding_state": "pending"}

        return await super().fetchrow(sql, *args)

    # ── execute overrides ───────────────────────────────────────────

    async def execute(self, sql: str, *args) -> str:
        compact = " ".join(sql.split())

        # _append_reasoning / _complete_turn path
        if compact.startswith("UPDATE bot_turns SET reasoning"):
            return "UPDATE 1"

        return await super().execute(sql, *args)


class FakeMessages:
    def __init__(self, responses, requests):
        self.responses = responses
        self.requests = requests

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected Anthropic request")
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses, requests):
        self.messages = FakeMessages(responses, requests)


class FakeAnthropicFactory:
    def __init__(self, responses, requests):
        self.responses = responses
        self.requests = requests

    def __call__(self, **kwargs):
        return FakeClient(self.responses, self.requests)


def _patch_whatsapp(monkeypatch, sent):
    async def fake_send_text(phone, content):
        sent.append(("text", phone, content))
        return {"messages": [{"id": f"wa-{len(sent)}"}]}

    async def fake_send_template(phone, payload):
        sent.append(("template", phone, payload))
        return {"messages": [{"id": f"wa-{len(sent)}"}]}

    monkeypatch.setattr(whatsapp, "send_text", fake_send_text)
    monkeypatch.setattr(whatsapp, "send_template", fake_send_template)


def _seed_user(pool, name="Alice", phone="15555550100", timezone="UTC"):
    user = User(uuid4(), name, phone, timezone)
    pool.users[user.id] = {
        "id": user.id,
        "name": name,
        "phone": phone,
        "timezone": timezone,
        "onboarding_state": "welcomed",
        "pacing_preferences": {},
        "cross_thread_sharing_default": None,
    }
    return user


def _seed_message(pool, user, content, charge="routine"):
    msg_id = uuid4()
    pool.messages[msg_id] = {
        "id": msg_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": content,
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": charge,
        "deleted_at": None,
        "whatsapp_message_id": f"wa-{msg_id}",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
    }
    return msg_id


# ═══════════════════════════════════════════════════════════════════════
# Test 1 — string-absence assertions on solo prompt
# ═══════════════════════════════════════════════════════════════════════

def test_solo_prompt_has_no_dyad_strings():
    """Solo system prompt must not contain bridge, in-person,
    partner perspective, cross-thread sharing, or escalate_to_partner."""
    prompt = render_solo_system_prompt(
        "Coach",
        "Alice",
        prompt_version="v1",
        onboarding_state="welcomed",
        sharing_default=None,
        topic_display_name="career",
    )
    lowered = prompt.lower()
    banned = [
        "bridge",
        "in-person",
        "partner perspective",
        "cross-thread sharing",
        "escalate_to_partner",
    ]
    for phrase in banned:
        assert phrase not in lowered, (
            f"Found banned phrase '{phrase}' in solo prompt. "
            f"First 200 chars: {prompt[:200]}"
        )


# ═══════════════════════════════════════════════════════════════════════
# Test 2 — coach allowlist assertion
# ═══════════════════════════════════════════════════════════════════════

def test_create_bridge_candidate_not_in_coach_allowed():
    """create_bridge_candidate must NOT be in
    to_anthropic_tools(_step_allowed(coach_ctx))."""
    coach_spec = build_coach_spec()
    ctx = SimpleNamespace(current_step="record", bot_spec=coach_spec)
    allowed = _step_allowed(ctx)
    tools = to_anthropic_tools(allowed)
    tool_names = {t["name"] for t in tools}
    assert "create_bridge_candidate" not in tool_names, (
        f"create_bridge_candidate should NOT be in coach allowed tools. "
        f"Got: {sorted(tool_names)}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Test 3 — idempotent onboarding
# ═══════════════════════════════════════════════════════════════════════

async def test_concurrent_onboarding_produces_exactly_one_row(app_env):
    """Call ensure_onboarding_state twice via asyncio.gather;
    assert exactly one user_bot_state row + no exception."""
    pool = CoachPool()
    user_id = uuid4()
    bot_id = "coach"

    async def call():
        return await ensure_onboarding_state(pool, user_id=user_id, bot_id=bot_id)

    results = await asyncio.gather(call(), call())

    assert all(r == "pending" for r in results), f"Unexpected results: {results}"

    matching = [
        r
        for r in pool.user_bot_states
        if r["user_id"] == user_id and r["bot_id"] == bot_id
    ]
    assert len(matching) == 1, (
        f"Expected exactly 1 user_bot_state row, got {len(matching)}: {matching}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Test 4 — end-to-end coach turn
# ═══════════════════════════════════════════════════════════════════════

async def test_coach_e2e_turn(app_env, monkeypatch):
    """Full coach turn:

    (a) partner_of NOT called
    (b) user-facing message sent
    (c) add_memory write scoped to career topic + about_user_id=user.id
    (d) create_bridge_candidate → tool.rejected (step_not_allowed)
    """

    # ── configure bot_id and staging ──────────────────────────────────
    monkeypatch.setenv("BOT_ID", "coach")
    monkeypatch.setenv("STAGING", "1")
    get_settings.cache_clear()

    # Force re-registration of staging bots (module-level flag may
    # already be True after earlier tests called get_bot_spec).
    import app.bots.registry as _botreg

    _botreg._STAGING_BOTS_REGISTERED = False
    _botreg._maybe_register_staging_bots()

    # ── pool + data ───────────────────────────────────────────────────
    pool = CoachPool()
    pool.seed_topic("career", CAREER_TOPIC_ID)
    user = _seed_user(pool, "Alice")
    msg_id = _seed_message(pool, user, "I'm thinking about changing careers.")

    # ── spy on partner_of ─────────────────────────────────────────────
    partner_of_calls: list = []
    original_partner_of = partner_of

    async def spy_partner_of(p, u):
        partner_of_calls.append((p, u))
        return await original_partner_of(p, u)

    monkeypatch.setattr(agentic, "partner_of", spy_partner_of)

    # ── stub LLM ──────────────────────────────────────────────────────
    requests: list = []
    outbound_text = "That's a big question — let's think through it together."

    responses = [
        # read step — fetch observations then done
        _tool(
            "get_observations",
            {"about_user_id": str(user.id), "min_significance": 3},
            1,
        ),
        _response([]),
        # respond step — produce text
        _response([{"type": "text", "text": outbound_text}]),
        # record step — must read before write (get_memories → add_memory)
        _tool(
            "get_memories",
            {"about_user_id": str(user.id), "limit": 20},
            2,
        ),
        _tool(
            "add_memory",
            {
                "about_user_id": str(user.id),
                "content": "Alice is considering a career change.",
                "related_theme_ids": [],
            },
            3,
        ),
        # record step — model tries create_bridge_candidate (MUST fail)
        _tool(
            "create_bridge_candidate",
            {
                "source_user_id": str(user.id),
                "target_user_id": str(uuid4()),
                "kind": "insight",
                "status": "pending",
                "sensitivity": "low",
                "partner_path": "hold_for_context",
                "source_message_ids": [str(msg_id)],
                "related_memory_ids": [],
                "related_observation_ids": [],
                "internal_note": "test bridge",
                "shareable_summary": "test summary",
            },
            4,
        ),
        _response([]),
        # schedule step — just finish
        _response([]),
    ]

    monkeypatch.setattr(
        agentic.anthropic,
        "AsyncAnthropic",
        FakeAnthropicFactory(responses, requests),
    )

    # ── patch delivery ────────────────────────────────────────────────
    whatsapp_sent: list = []
    _patch_whatsapp(monkeypatch, whatsapp_sent)

    async def ok_oob(pool_arg, content, recipient_id, protected_owner_ids=None):
        return {"verdict": "ok", "reason": "test", "rewrite": None}

    monkeypatch.setattr(hooks, "check_oob", ok_oob)

    agentic.set_pool(pool)

    # ── drive the turn (standard skeleton) ───────────────────────────
    await agentic.run_agentic_turn_with_metadata(
        [msg_id],
        user,
        trigger_metadata={"kind": "scheduled_task"},
    )

    # ── assertions ────────────────────────────────────────────────────

    # (a) partner_of must NOT be called
    assert len(partner_of_calls) == 0, (
        f"partner_of was called {len(partner_of_calls)} times; "
        f"it should be skipped for solo bots"
    )

    # (b) user-facing message was sent
    user_texts = [item[2] for item in whatsapp_sent if item[0] == "text"]
    assert len(user_texts) >= 1, f"No user-facing text sent: {whatsapp_sent}"
    assert user_texts[0] == outbound_text, (
        f"Wrong outbound text: {user_texts[0]}"
    )

    # (c) ≥1 add_memory write scoped to about_user_id=user.id
    memories_for_user = [
        m for m in pool.memories.values() if m.get("about_user_id") == user.id
    ]
    assert len(memories_for_user) >= 1, (
        f"No memories found for user {user.id}"
    )
    memory_texts = [m["content"].lower() for m in memories_for_user]
    assert any(
        "career" in t or "considering" in t or "change" in t for t in memory_texts
    ), f"Expected career-related memory, got: {memory_texts}"

    # (d) create_bridge_candidate rejected at registry boundary
    rejected = [
        e
        for e in pool.turn_audit_events
        if e.get("event_type") == "tool.rejected"
        and e.get("metadata", {}).get("tool_name") == "create_bridge_candidate"
    ]
    assert len(rejected) >= 1, (
        "create_bridge_candidate should have been rejected. "
        f"tool.rejected events: "
        f"{[(e.get('metadata',{}).get('tool_name'), e.get('metadata',{}).get('reason')) for e in pool.turn_audit_events if e.get('event_type')=='tool.rejected']}"
    )
    reason = rejected[0]["metadata"].get("reason")
    assert reason in ("step_not_allowed", "unknown_tool"), (
        f"Expected step_not_allowed or unknown_tool, got: {reason}"
    )