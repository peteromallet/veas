from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services import agentic, hooks, whatsapp
from app.services.tools import write_tools
from tests.conftest import FakePool

pytestmark = pytest.mark.anyio


USAGE = {
    "input_tokens": 100,
    "cache_creation_input_tokens": 20,
    "cache_read_input_tokens": 10,
    "output_tokens": 10,
}


class TrackingPool(FakePool):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[int, str]] = []
        self.spend_records: list[Decimal] = []

    def mark(self, label: str) -> None:
        self.events.append((len(self.events) + 1, label))

    def labels(self) -> list[str]:
        return [label for _, label in self.events]

    def seq(self, label: str) -> int:
        return next(seq for seq, event_label in self.events if event_label == label)

    async def fetchrow(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO bot_turns"):
            self.mark("turn:open")
        if compact.startswith("INSERT INTO messages") and "direction, recipient_id" in compact:
            self.mark("outbound:insert")
        if compact.startswith("UPDATE observations SET"):
            self.mark("write:update_observation")
            observation_id = args[-1]
            self.observations.setdefault(observation_id, {"id": observation_id, "status": "active"})
            self.observations[observation_id]["last_reinforced_at"] = datetime.now(UTC)
            if args and isinstance(args[0], str):
                self.observations[observation_id]["content"] = args[0]
            return {"id": observation_id}
        if compact.startswith("INSERT INTO watch_items"):
            self.mark("write:add_watch_item")
        if compact.startswith("INSERT INTO scheduled_jobs"):
            self.mark("write:schedule_checkin")
        return await super().fetchrow(sql, *args)

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT id, sender_id") and "sent_at, content" in compact and "FROM messages" in compact:
            self.mark("read:search_messages")
            rows = [
                row
                for row in self.messages.values()
                if row.get("deleted_at") is None and row.get("direction") == "inbound"
            ]
            rows.sort(key=lambda row: row["sent_at"], reverse=True)
            return rows[: args[-1]]
        if "FROM observations" in compact and "supporting_message_ids" in compact:
            self.mark("read:get_observations")
            return [
                {
                    **row,
                    "supporting_message_ids": row.get("supporting_message_ids", []),
                    "created_at": row.get("created_at", datetime.now(UTC)),
                    "last_reinforced_at": row.get("last_reinforced_at"),
                    "surfaced_count": row.get("surfaced_count", 0),
                }
                for row in self.observations.values()
                if row.get("status", "active") == "active"
            ]
        if compact.startswith("SELECT id, title, status") and "FROM themes" in compact:
            self.mark("read:list_themes")
            return list(self.themes.values())[: args[-1]]
        if "FROM watch_items" in compact:
            self.mark("read:list_watch_items")
            return []
        if "FROM bot_turns bt" in compact and "LEFT JOIN tool_calls" in compact:
            self.mark("read:get_bot_actions")
            return [
                {
                    "turn_id": row["id"],
                    "started_at": row["started_at"],
                    "user_in_context": row["user_in_context"],
                    "triggered_by_message_id": row["triggered_by_message_id"],
                    "final_output_message_id": row.get("final_output_message_id"),
                    "triggering_content": None,
                    "final_outbound_content": None,
                    "reasoning": row.get("reasoning", ""),
                    "tool_calls": [
                        tool_call for tool_call in self.tool_calls if tool_call["turn_id"] == row["id"]
                    ],
                }
                for row in self.bot_turns.values()
            ][: args[-1]]
        return await super().fetch(sql, *args)

    async def execute(self, sql: str, *args) -> str:
        compact = " ".join(sql.split())
        if compact.startswith("UPDATE messages SET processing_state='processed' WHERE id = ANY"):
            self.mark("messages:processed")
        if compact.startswith("INSERT INTO llm_spend_log"):
            self.spend_records.append(Decimal(str(args[1])))
        if compact.startswith("INSERT INTO tool_calls"):
            self.mark(f"tool_call:{args[1]}")
        if compact.startswith("UPDATE bot_turns SET final_output_message_id"):
            self.mark("turn:complete")
        if compact.startswith("UPDATE bot_turns SET reasoning") and args and "ESCALATION:" in str(args[0]):
            self.mark("reason:escalation")
        return await super().execute(sql, *args)


class FakeMessages:
    def __init__(self, responses: list[SimpleNamespace], requests: list[dict], pool: TrackingPool) -> None:
        self.responses = responses
        self.requests = requests
        self.pool = pool

    async def create(self, **kwargs):
        self.pool.mark("llm:call")
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected Anthropic request")
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[SimpleNamespace], requests: list[dict], pool: TrackingPool) -> None:
        self.messages = FakeMessages(responses, requests, pool)


class FakeAnthropicFactory:
    def __init__(self, responses: list[SimpleNamespace], requests: list[dict], pool: TrackingPool) -> None:
        self.responses = responses
        self.requests = requests
        self.pool = pool

    def __call__(self, **kwargs):
        return FakeClient(self.responses, self.requests, self.pool)


def _response(content: list[dict], stop_reason: str = "end_turn", usage: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage or dict(USAGE))


def _tool(name: str, input_: dict, n: int) -> SimpleNamespace:
    return _response([{"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": input_}], "tool_use")


def _seed_pair(pool: TrackingPool, *, charge: str = "charged", content: str = "I need help"):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    message_id = uuid4()
    pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": content,
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": charge,
        "deleted_at": None,
        "whatsapp_message_id": "wa-trigger",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
    }
    return user, partner, message_id


def _seed_context_rows(pool: TrackingPool, user: User) -> tuple[UUID, UUID]:
    observation_id = uuid4()
    theme_id = uuid4()
    pool.observations[observation_id] = {
        "id": observation_id,
        "about_user_id": user.id,
        "content": "Maya wants repair attempts to be direct.",
        "confidence": "medium",
        "significance": 3,
        "status": "active",
        "related_theme_ids": [theme_id],
        "supporting_message_ids": [],
        "created_at": datetime.now(UTC),
        "last_reinforced_at": None,
    }
    pool.themes[theme_id] = {
        "id": theme_id,
        "title": "Repair timing",
        "description": "They are trying to recover faster after tense moments.",
        "status": "active",
        "sentiment": "mixed",
        "health": "tender",
        "last_reinforced_at": datetime.now(UTC),
        "last_active_at": datetime.now(UTC),
    }
    for idx in range(2):
        msg_id = uuid4()
        pool.messages[msg_id] = {
            "id": msg_id,
            "direction": "inbound",
            "sender_id": user.id,
            "recipient_id": None,
            "content": f"prior repair message {idx}",
            "processing_state": "processed",
            "sent_at": datetime.now(UTC) - timedelta(minutes=idx + 1),
            "charge": "routine",
            "deleted_at": None,
            "whatsapp_message_id": f"prior-{idx}",
            "media_type": None,
            "media_url": None,
            "media_duration_seconds": None,
            "media_analysis": None,
            "edit_history": None,
            "edited_at": None,
        }
    return observation_id, theme_id


def _patch_whatsapp(monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, str, object]]) -> None:
    async def fake_send_text(phone: str, content: str):
        sent.append(("text", phone, content))
        return {"messages": [{"id": f"wa-{len(sent)}"}]}

    async def fake_send_template(phone: str, payload):
        sent.append(("template", phone, payload))
        return {"messages": [{"id": f"wa-{len(sent)}"}]}

    monkeypatch.setattr(whatsapp, "send_text", fake_send_text)
    monkeypatch.setattr(whatsapp, "send_template", fake_send_template)


async def test_agentic_e2e_ordering_cache_spend_and_oob(app_env, monkeypatch):
    pool = TrackingPool()
    user, partner, message_id = _seed_pair(pool, charge="charged")
    observation_id, _ = _seed_context_rows(pool, user)
    outbound = "That sounds tender. I can help you phrase the next step."
    check_oob_calls: list[tuple[str, UUID, list[UUID] | None]] = []
    whatsapp_sent: list[tuple[str, str, object]] = []
    requests: list[dict] = []
    when = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    responses = [
        _tool("get_observations", {"about_user_id": str(user.id), "min_significance": 3}, 1),
        _tool("search_messages", {"text_contains": "repair", "limit": 5}, 2),
        _tool("list_themes", {"active_only": True, "sort_by": "last_reinforced", "limit": 10}, 3),
        _tool("list_watch_items", {"owner_user_id": str(user.id)}, 4),
        _response([]),
        _response([{"type": "text", "text": outbound}]),
        _tool("update_observation", {"observation_id": str(observation_id), "content": "Direct repair still matters."}, 5),
        _tool("add_watch_item", {"owner_user_id": str(user.id), "content": "Check whether the repair conversation happened."}, 6),
        _response([]),
        _tool(
            "schedule_checkin",
            {"user_id": str(user.id), "when": when, "about_what": "repair conversation", "reason": "follow up"},
            7,
        ),
        _response([]),
    ]

    async def counting_oob(pool_arg, content: str, recipient_id: UUID, protected_owner_ids=None) -> dict:
        check_oob_calls.append((content, recipient_id, protected_owner_ids))
        return {"verdict": "ok", "reason": "test", "rewrite": None}

    monkeypatch.setattr(hooks, "check_oob", counting_oob)
    _patch_whatsapp(monkeypatch, whatsapp_sent)
    monkeypatch.setattr(agentic.anthropic, "AsyncAnthropic", FakeAnthropicFactory(responses, requests, pool))
    agentic.set_pool(pool)

    await agentic.run_agentic_turn([message_id], user)

    turn = next(iter(pool.bot_turns.values()))
    labels = pool.labels()
    read_seqs = [
        pool.seq("read:get_observations"),
        pool.seq("read:search_messages"),
        pool.seq("read:list_themes"),
        pool.seq("read:list_watch_items"),
    ]
    write_seqs = [
        pool.seq("write:update_observation"),
        pool.seq("write:add_watch_item"),
        pool.seq("write:schedule_checkin"),
    ]
    assert pool.seq("llm:call") < pool.seq("messages:processed")
    assert max(read_seqs) < pool.seq("outbound:insert") < min(write_seqs)
    assert pool.messages[message_id]["processing_state"] == "processed"
    assert turn["completed_at"] is not None
    assert turn["failure_reason"] is None
    assert turn["final_output_message_id"] is not None
    assert turn["system_prompt_version"] == "v3"
    assert "## Recent messages" in turn["prompt_snapshot"]
    assert turn["tool_call_count"] == 7
    assert [row["tool_name"] for row in pool.tool_calls] == [
        "update_observation",
        "add_watch_item",
        "schedule_checkin",
    ]
    assert check_oob_calls == [
        (outbound, user.id, [user.id, partner.id]),
        (outbound, user.id, [user.id, partner.id]),
    ]
    assert pool.spend_records and all(value > 0 for value in pool.spend_records)
    assert sum(pool.spend_records[:5], Decimal("0")) == Decimal("0.002190")
    assert sum(pool.spend_records[5:], Decimal("0")) == Decimal("0.002628")
    assert requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert requests[0]["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert requests[5]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert requests[5]["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert labels.index("read:get_observations") < labels.index("outbound:insert") < labels.index("tool_call:update_observation")


async def test_agentic_why_query_uses_get_bot_actions(app_env, monkeypatch):
    pool = TrackingPool()
    user, _, message_id = _seed_pair(pool, charge="routine", content="Why did you tell her that?")
    requests: list[dict] = []
    whatsapp_sent: list[tuple[str, str, object]] = []
    responses = [
        _tool("get_bot_actions", {}, 1),
        _response([]),
        _response([{"type": "text", "text": "I checked the action log before answering."}]),
        _response([]),
    ]

    _patch_whatsapp(monkeypatch, whatsapp_sent)
    monkeypatch.setattr(hooks, "check_oob", lambda content, recipient_id: {"verdict": "ok", "reason": "test", "rewrite": None})

    async def async_oob(content: str, recipient_id: UUID) -> dict:
        return {"verdict": "ok", "reason": "test", "rewrite": None}

    monkeypatch.setattr(hooks, "check_oob", async_oob)
    monkeypatch.setattr(agentic.anthropic, "AsyncAnthropic", FakeAnthropicFactory(responses, requests, pool))
    agentic.set_pool(pool)

    await agentic.run_agentic_turn([message_id], user)

    assert "read:get_bot_actions" in pool.labels()


async def test_agentic_uses_oob_rewrite_before_sending(app_env, monkeypatch):
    pool = TrackingPool()
    user, _, message_id = _seed_pair(pool, charge="routine", content="Can you help?")
    requests: list[dict] = []
    whatsapp_sent: list[tuple[str, str, object]] = []
    oob_calls: list[str] = []
    responses = [
        _response([{"type": "text", "text": "Draft with protected detail."}]),
        _response([]),
    ]

    async def rewriting_oob(content: str, recipient_id: UUID) -> dict:
        oob_calls.append(content)
        if content == "Draft with protected detail.":
            return {
                "verdict": "rewrite",
                "reason": "too specific",
                "suggested_rewrite": "Safer version.",
                "checker_failed": False,
            }
        return {"verdict": "ok", "reason": "safe", "suggested_rewrite": None, "checker_failed": False}

    _patch_whatsapp(monkeypatch, whatsapp_sent)
    monkeypatch.setattr(hooks, "check_oob", rewriting_oob)
    monkeypatch.setattr(agentic.anthropic, "AsyncAnthropic", FakeAnthropicFactory(responses, requests, pool))
    agentic.set_pool(pool)

    await agentic.run_agentic_turn([message_id], user)

    outbound = next(row for row in pool.messages.values() if row.get("direction") == "outbound")
    assert outbound["content"] == "Safer version."
    assert whatsapp_sent[0][2] == "Safer version."
    assert oob_calls[:2] == ["Draft with protected detail.", "Safer version."]
    assert "Outbound rewritten by OOB checker before send" in next(iter(pool.bot_turns.values()))["reasoning"]


async def test_agentic_current_user_oob_block_prevents_provider_delivery(app_env, monkeypatch):
    pool = TrackingPool()
    user, partner, message_id = _seed_pair(pool, charge="routine", content="Can you help?")
    requests: list[dict] = []
    whatsapp_sent: list[tuple[str, str, object]] = []
    oob_calls: list[tuple[str, UUID, list[UUID] | None]] = []
    blocked_text = "Draft with current-user protected detail."
    responses = [
        _response([{"type": "text", "text": blocked_text}]),
        _response([]),
    ]

    async def blocking_oob(pool_arg, content: str, recipient_id: UUID, protected_owner_ids=None) -> dict:
        oob_calls.append((content, recipient_id, protected_owner_ids))
        return {
            "verdict": "block",
            "reason": "current-user firm OOB",
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    _patch_whatsapp(monkeypatch, whatsapp_sent)
    monkeypatch.setattr(hooks, "check_oob", blocking_oob)
    monkeypatch.setattr(agentic.anthropic, "AsyncAnthropic", FakeAnthropicFactory(responses, requests, pool))
    agentic.set_pool(pool)

    await agentic.run_agentic_turn([message_id], user)

    assert whatsapp_sent == []
    assert oob_calls == [(blocked_text, user.id, [user.id, partner.id])]
    assert not any(row.get("direction") == "outbound" for row in pool.messages.values())
    assert "Outbound blocked before send by OOB checker" in next(iter(pool.bot_turns.values()))["reasoning"]


async def test_agentic_crisis_escalation_routes_to_partner_with_template(app_env, monkeypatch):
    pool = TrackingPool()
    user, partner, message_id = _seed_pair(pool, charge="crisis", content="I might hurt myself")
    requests: list[dict] = []
    whatsapp_sent: list[tuple[str, str, object]] = []
    escalation_spy: list[tuple[User, str, object, UUID | None, list[UUID] | None]] = []
    escalation_content = "Maya may need immediate support right now."
    responses = [
        _response([{"type": "text", "text": "I am here with you. If you might act on this now, contact local emergency support or a trusted person nearby."}]),
        _tool(
            "escalate_to_partner",
            {
                "from_user_id": str(uuid4()),
                "to_user_id": str(uuid4()),
                "content": escalation_content,
                "reason": "crisis charge",
                "is_crisis": True,
            },
            1,
        ),
        _response([]),
    ]

    _patch_whatsapp(monkeypatch, whatsapp_sent)
    monkeypatch.setattr(hooks, "check_oob", lambda content, recipient_id: {"verdict": "ok", "reason": "test", "rewrite": None})

    async def async_oob(content: str, recipient_id: UUID) -> dict:
        return {"verdict": "ok", "reason": "test", "rewrite": None}

    monkeypatch.setattr(hooks, "check_oob", async_oob)
    original_send = write_tools.send_outbound

    async def spy_send(pool_arg, recipient, content, template_fallback=None, bot_turn_id=None, protected_owner_ids=None):
        escalation_spy.append((recipient, content, template_fallback, bot_turn_id, protected_owner_ids))
        return await original_send(
            pool_arg,
            recipient,
            content,
            template_fallback=template_fallback,
            bot_turn_id=bot_turn_id,
            protected_owner_ids=protected_owner_ids,
        )

    monkeypatch.setattr(write_tools, "send_outbound", spy_send)
    monkeypatch.setattr(agentic.anthropic, "AsyncAnthropic", FakeAnthropicFactory(responses, requests, pool))
    agentic.set_pool(pool)

    await agentic.run_agentic_turn([message_id], user)

    supportive_outbound = next(
        row for row in pool.messages.values() if row.get("recipient_id") == user.id and row.get("direction") == "outbound"
    )
    banned = ("attachment", "adhd", "avoidant")
    assert all(token not in supportive_outbound["content"].lower() for token in banned)
    assert escalation_spy
    recipient, content, template_fallback, turn_id, protected_owner_ids = escalation_spy[0]
    assert recipient.id == partner.id
    assert recipient.id != user.id
    assert content == escalation_content
    assert template_fallback.name == "escalation"
    assert template_fallback.params == [partner.name, user.name, escalation_content]
    assert protected_owner_ids == [user.id, partner.id]
    assert turn_id in pool.bot_turns
    turn = pool.bot_turns[turn_id]
    assert "ESCALATION_SENT gate=crisis reason=crisis charge" in turn["reasoning"]
