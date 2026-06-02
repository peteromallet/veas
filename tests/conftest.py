import sys
import types
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import app


class _UndefinedTableError(Exception):
    """Simulates asyncpg.UndefinedTableError for stale-DB fallback testing."""


def _coerce_jsonb(value):
    """Mirror the asyncpg jsonb codec for FakePool: accept dicts, decode strings, pass None."""
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return value


REQUIRED_ENV = {
    "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
    "DATABASE_SCHEMA": "public",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "dummy-service-role",
    "ANTHROPIC_API_KEY": "dummy-anthropic",
    "OPENAI_API_KEY": "dummy-openai",
    "GROQ_API_KEY": "dummy-groq",
    "WHATSAPP_TOKEN": "dummy-whatsapp",
    "WHATSAPP_BEARER_TOKEN": "dummy-whatsapp",
    "WHATSAPP_PHONE_NUMBER_ID": "12345",
    "WHATSAPP_VERIFY_TOKEN": "dummy-verify",
    "WHATSAPP_APP_SECRET": "dummy-secret",
    "WHATSAPP_API_VERSION": "v20.0",
    "MESSAGING_PROVIDER": "meta",
    "ADMIN_PASSWORD": "dummy-admin",
    "PARTNER_PHONE_A": "15555550100",
    "PARTNER_PHONE_B": "15555550101",
    "DISCORD_PARTNER_USER_ID_A": "",
    "DISCORD_PARTNER_USER_ID_B": "",
    "SUPABASE_STORAGE_BUCKET": "mediator-media",
    "MEDIA_FETCH_TIMEOUT_S": "30",
    "DEFAULT_USER_TIMEZONE": "UTC",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def _seed_relationship_topic_id() -> None:
    """Seed the relationship topic id so get_relationship_topic_id() returns a
    stable UUID in every test without requiring a real DB round-trip.

    T5 (Step 4.1): this autouse session-scoped fixture is what keeps existing
    tests green after T4 adds topic_id default-resolution to build_hot_context,
    run_decay_housekeeping, check_oob_with_policy, summarize_partner_oob, and
    rescore_observations.
    """
    import app.bots.registry as _reg

    _reg._RELATIONSHIP_TOPIC_ID = UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
def app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeConnection:
    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool

    async def execute(self, sql: str, *args) -> str:
        self.pool.connection_events.append(
            ("execute", self.pool.transaction_depth, " ".join(sql.split()), args)
        )
        return await self.pool.execute(sql, *args)

    async def fetchrow(self, sql: str, *args):
        self.pool.connection_events.append(
            ("fetchrow", self.pool.transaction_depth, " ".join(sql.split()), args)
        )
        return await self.pool.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args):
        self.pool.connection_events.append(
            ("fetchval", self.pool.transaction_depth, " ".join(sql.split()), args)
        )
        return await self.pool.fetchval(sql, *args)

    async def fetch(self, sql: str, *args):
        self.pool.connection_events.append(
            ("fetch", self.pool.transaction_depth, " ".join(sql.split()), args)
        )
        return await self.pool.fetch(sql, *args)

    def transaction(self) -> "FakeTransactionContext":
        return FakeTransactionContext(self.pool)


class FakeTransactionContext:
    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool

    async def __aenter__(self) -> None:
        self.pool.transaction_depth += 1
        self.pool.transaction_entries += 1
        self.pool.connection_events.append(
            ("transaction_enter", self.pool.transaction_depth, "", ())
        )
        return None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.pool.connection_events.append(
            ("transaction_exit", self.pool.transaction_depth, "", ())
        )
        self.pool.transaction_depth -= 1
        return False


class FakeAcquireContext:
    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool

    async def __aenter__(self) -> FakeConnection:
        return FakeConnection(self.pool)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakePool:
    """In-memory FakePool for offline tests.

    Trigger-equivalent enforcement only covers the claim CTE path
    (fetch handler at the _claim_messages_for_turn_in_tx simulator).
    Bare-UPDATE bot_turn_id paths are not predicate-checked by FakePool;
    Step 13.6 real-PG smoke is authoritative for those paths.
    """

    def __init__(self) -> None:
        self.closed = False
        self.users = {}
        self.messages = {}
        self.message_embeddings = {}
        self.embed_jobs = {}
        self.connection_events: list[tuple[str, int, str, tuple[Any, ...]]] = []
        self.transaction_depth = 0
        self.transaction_entries = 0
        self.bot_turns = {}
        self.turn_audit_events = []
        self.llm_spend_log = {}
        self.tool_calls = []
        self.memories = {}
        self.themes = {}
        self.conversations = {}
        self.conversation_notes = {}
        self.watch_items = {}
        self.observations = {}
        self.distillations = {}
        self.conversation_artifacts = {}
        self.out_of_bounds = {}
        self.withheld_outbound_reviews = {}
        self.bridge_candidates = {}
        self.pacing_events = {}
        self.scheduled_jobs = {}
        self.eval_runs = {}
        self.eval_results = {}
        self.system_state = {
            "global_pause": {"key": "global_pause", "paused_at": None, "value": {}}
        }
        self.feedback = {}
        self.artifact_topics: dict[tuple[str, UUID], UUID] = {}
        # S4: topic_status rows keyed by (topic_id, dyad_id) or (topic_id, user_id)
        self.topic_status: dict[tuple[UUID, UUID], dict[str, Any]] = {}
        # S6: user_bot_state rows keyed by (user_id, bot_id)
        self.user_bot_state: dict[tuple[UUID, str], dict[str, Any]] = {}
        # Per-bot partner sharing uses dyads/dyad_members for partner existence.
        self.dyad_partners: dict[UUID, UUID] = {}
        # S6: topics rows keyed by slug
        self.topics: dict[str, dict[str, Any]] = {}
        # S6 T5: artifact_topics_rows recorded during multi-topic writes
        self.artifact_topics_rows: list[dict[str, Any]] = []
        # S7 fix 2: user_identities rows keyed by (transport, address) -> user_id.
        # Tests that want resolve_user_address to return a non-phone value seed
        # rows here directly.  Default empty so existing tests fall back to phone.
        self.user_identities: dict[tuple[str, str], UUID] = {}
        # Multi-gateway: channels rows keyed by (transport, address).
        self.channels: dict[tuple[str, str], dict[str, Any]] = {}
        self._raise_undefined_table_on_channels: bool = False
        # S7: Hector fitness — commitments and events tables (T15).
        self.commitments: dict[UUID, dict[str, Any]] = {}
        self.events: dict[UUID, dict[str, Any]] = {}
        self.fetch_sqls: list[str] = []

    def _canonical_searchable_text(self, row: dict[str, Any]) -> str:
        if "canonical_text" in row:
            return row.get("canonical_text") or ""
        analysis = row.get("media_analysis") or {}
        if isinstance(analysis, str):
            try:
                analysis = json.loads(analysis)
            except json.JSONDecodeError:
                analysis = {"summary": analysis}
        analysis_parts = []
        if isinstance(analysis, dict):
            analysis_parts = [
                str(analysis.get(key) or "")
                for key in ("explanation", "description", "summary")
                if analysis.get(key)
            ]
        return "\n".join(
            [
                row.get("content") or "",
                "\n".join(analysis_parts),
                row.get("transcript") or "",
            ]
        )

    def _searchable_message_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if row.get("deleted_at") is not None:
            return None
        if row.get("search_suppressed_at") is not None:
            return None
        direction = row.get("direction")
        if row.get("thread_owner_user_id") is not None:
            thread_owner_user_id = row.get("thread_owner_user_id")
        elif direction == "inbound" and row.get("sender_id") is not None:
            thread_owner_user_id = row.get("sender_id")
        elif direction == "outbound" and row.get("recipient_id") is not None:
            thread_owner_user_id = row.get("recipient_id")
        else:
            thread_owner_user_id = row.get("sender_id") or row.get("recipient_id")
        bot_id = row["bot_id"] if "bot_id" in row else "mediator"
        partner_share = row.get("thread_owner_partner_share")
        if partner_share is None and thread_owner_user_id is not None:
            partner_share = (
                self.user_bot_state.get((thread_owner_user_id, bot_id), {}).get(
                    "partner_share"
                )
            )
        return {
            "message_id": row["id"],
            "canonical_text": self._canonical_searchable_text(row),
            "sent_at": row.get("sent_at"),
            "sender_id": row.get("sender_id"),
            "recipient_id": row.get("recipient_id"),
            "bot_id": bot_id,
            "topic_id": row.get(
                "topic_id", UUID("00000000-0000-4000-8000-000000000001")
            ),
            "dyad_id": row.get("dyad_id"),
            "thread_owner_user_id": thread_owner_user_id,
            "thread_owner_partner_share": partner_share or row.get("partner_share", "unset"),
            "active_oob_severity": row.get("active_oob_severity"),
        }

    def _searchable_content_row(
        self, source_type: str, source_id: UUID
    ) -> dict[str, Any] | None:
        default_topic_id = UUID("00000000-0000-4000-8000-000000000001")
        if source_type == "message":
            row = self.messages.get(source_id)
            searchable = self._searchable_message_row(row) if row is not None else None
            if searchable is None:
                return None
            sent_at = searchable.get("sent_at")
            return {
                **searchable,
                "source_type": "message",
                "source_id": source_id,
                "message_id": searchable["message_id"],
                "direction": row.get("direction"),
                "charge": row.get("charge", "routine") or "routine",
                "edited_at": row.get("edited_at"),
                "edit_history": row.get("edit_history"),
                "content": row.get("content"),
                "media_type": row.get("media_type"),
                "media_analysis": row.get("media_analysis"),
                "sort_at": sent_at,
                "primary_topic_id": searchable.get("topic_id"),
                "topic_ids": [searchable["topic_id"]] if searchable.get("topic_id") else [],
                "source_created_at": sent_at,
                "source_updated_at": row.get("edited_at") or sent_at,
            }

        if source_type == "memory":
            row = self.memories.get(source_id)
            if row is None or row.get("status", "active") != "active":
                return None
            if row.get("visibility") == "dyad_shareable":
                return None
            text = row.get("content") or row.get("summary") or row.get("memory") or ""
            created_at = row.get("created_at")
            updated_at = row.get("last_referenced_at") or created_at
            topic_id = self.artifact_topics.get(("memories", source_id)) or row.get("topic_id")
            return {
                "source_type": "memory",
                "source_id": source_id,
                "message_id": None,
                "direction": None,
                "canonical_text": text,
                "sent_at": created_at,
                "sender_id": row.get("about_user_id"),
                "recipient_id": None,
                "bot_id": row.get("recorded_by_bot_id", "mediator"),
                "topic_id": topic_id,
                "dyad_id": None,
                "thread_owner_user_id": row.get("about_user_id"),
                "thread_owner_partner_share": None,
                "active_oob_severity": None,
                "charge": "routine",
                "edited_at": None,
                "edit_history": None,
                "content": text,
                "media_type": None,
                "media_analysis": None,
                "sort_at": created_at,
                "primary_topic_id": topic_id,
                "topic_ids": [topic_id] if topic_id else [],
                "source_created_at": created_at,
                "source_updated_at": updated_at,
            }

        if source_type == "distillation":
            row = self.distillations.get(source_id)
            if row is None or row.get("status", "active") != "active":
                return None
            if row.get("visibility") == "dyad_shareable":
                return None
            text = row.get("content") or row.get("summary") or row.get("distillation") or ""
            created_at = row.get("created_at")
            updated_at = row.get("updated_at") or created_at
            topic_id = self.artifact_topics.get(("distillations", source_id)) or row.get("topic_id")
            return {
                "source_type": "distillation",
                "source_id": source_id,
                "message_id": None,
                "direction": None,
                "canonical_text": text,
                "sent_at": created_at,
                "sender_id": None,
                "recipient_id": None,
                "bot_id": row.get("recorded_by_bot_id"),
                "topic_id": topic_id,
                "dyad_id": None,
                "thread_owner_user_id": None,
                "thread_owner_partner_share": None,
                "active_oob_severity": None,
                "charge": "routine",
                "edited_at": None,
                "edit_history": None,
                "content": text,
                "media_type": None,
                "media_analysis": None,
                "sort_at": created_at,
                "primary_topic_id": topic_id,
                "topic_ids": [topic_id] if topic_id else [],
                "source_created_at": created_at,
                "source_updated_at": updated_at,
            }

        if source_type == "observation":
            row = self.observations.get(source_id)
            if row is None or row.get("status", "active") != "active":
                return None
            if row.get("significance", 3) < 3:
                return None
            text = row.get("content") or row.get("summary") or row.get("observation") or ""
            created_at = row.get("created_at") or row.get("observed_at")
            updated_at = row.get("last_reinforced_at") or created_at
            topic_id = self.artifact_topics.get(("observations", source_id)) or row.get("topic_id")
            return {
                "source_type": "observation",
                "source_id": source_id,
                "message_id": None,
                "direction": None,
                "canonical_text": text,
                "sent_at": created_at,
                "sender_id": row.get("about_user_id"),
                "recipient_id": None,
                "bot_id": row.get("recorded_by_bot_id", "mediator"),
                "topic_id": topic_id,
                "dyad_id": None,
                "thread_owner_user_id": row.get("about_user_id"),
                "thread_owner_partner_share": None,
                "active_oob_severity": None,
                "charge": "routine",
                "edited_at": None,
                "edit_history": None,
                "content": text,
                "media_type": None,
                "media_analysis": None,
                "sort_at": created_at,
                "primary_topic_id": topic_id,
                "topic_ids": [topic_id] if topic_id else [],
                "source_created_at": created_at,
                "source_updated_at": updated_at,
            }

        if source_type == "artifact":
            row = self.conversation_artifacts.get(source_id)
            if row is None or row.get("deleted_at") is not None:
                return None
            expires_at = row.get("expires_at")
            if expires_at is not None and expires_at <= datetime.now(UTC):
                return None
            payload = row.get("payload") or {}
            text = self._canonical_artifact_text(row)
            if not text:
                return None
            created_at = row.get("created_at")
            topic_id = row.get("topic_id", default_topic_id)
            media_analysis = {
                "artifact_type": row.get("artifact_type"),
                "payload_version": row.get("payload_version"),
                "revision_number": row.get("revision_number"),
                "conversation_id": row.get("conversation_id"),
                "created_by_turn_id": row.get("created_by_turn_id"),
                "expires_at": row.get("expires_at"),
            }
            return {
                "source_type": "artifact",
                "source_id": source_id,
                "message_id": None,
                "direction": None,
                "canonical_text": text,
                "sent_at": created_at,
                "sender_id": row.get("user_id"),
                "recipient_id": None,
                "bot_id": row.get("bot_id", "mediator"),
                "topic_id": topic_id,
                "dyad_id": None,
                "thread_owner_user_id": row.get("user_id"),
                "thread_owner_partner_share": None,
                "active_oob_severity": None,
                "charge": "routine",
                "edited_at": None,
                "edit_history": None,
                "content": text,
                "media_type": None,
                "media_analysis": media_analysis,
                "sort_at": created_at,
                "primary_topic_id": topic_id,
                "topic_ids": [topic_id] if topic_id else [],
                "source_created_at": created_at,
                "source_updated_at": created_at,
                "payload": payload,
            }

        if source_type == "conversation_note":
            row = self.conversation_notes.get(source_id)
            if row is None:
                return None
            text = row.get("text") or ""
            if not text.strip():
                return None
            created_at = row.get("created_at")
            conversation = getattr(self, "conversations", {}).get(
                row.get("conversation_id"), {}
            )
            topic_id = row.get("topic_id") or conversation.get("topic_id")
            bot_id = row.get("bot_id") or conversation.get("bot_id")
            user_id = row.get("user_id") or conversation.get("user_id")
            partner_share = row.get("thread_owner_partner_share")
            if partner_share is None and user_id is not None and bot_id is not None:
                partner_share = self.user_bot_state.get((user_id, bot_id), {}).get(
                    "partner_share"
                )
            return {
                "source_type": "conversation_note",
                "source_id": source_id,
                "message_id": None,
                "direction": None,
                "canonical_text": text,
                "sent_at": created_at,
                "sender_id": user_id,
                "recipient_id": None,
                "bot_id": bot_id,
                "topic_id": topic_id,
                "dyad_id": conversation.get("dyad_id"),
                "thread_owner_user_id": user_id,
                "thread_owner_partner_share": partner_share or "unset",
                "active_oob_severity": None,
                "charge": "routine",
                "edited_at": None,
                "edit_history": None,
                "content": text,
                "media_type": None,
                "media_analysis": None,
                "sort_at": created_at,
                "primary_topic_id": topic_id,
                "topic_ids": [topic_id] if topic_id else [],
                "source_created_at": created_at,
                "source_updated_at": created_at,
            }

        if source_type == "theme":
            row = self.themes.get(source_id)
            if row is None or row.get("status", "active") != "active":
                return None
            title = row.get("title")
            description = row.get("description")
            parts = [part for part in (title, description) if part is not None]
            text = "\n".join(str(part) for part in parts).strip()
            if not text:
                return None
            created_at = row.get("created_at") or row.get("first_seen_at")
            updated_at = row.get("updated_at") or created_at
            topic_id = self.artifact_topics.get(("themes", source_id)) or row.get("topic_id")
            return {
                "source_type": "theme",
                "source_id": source_id,
                "message_id": None,
                "direction": None,
                "canonical_text": text,
                "sent_at": created_at,
                "sender_id": row.get("about_user_id"),
                "recipient_id": None,
                "bot_id": row.get("recorded_by_bot_id"),
                "topic_id": topic_id,
                "dyad_id": None,
                "thread_owner_user_id": row.get("about_user_id"),
                "thread_owner_partner_share": None,
                "active_oob_severity": None,
                "charge": "routine",
                "edited_at": None,
                "edit_history": None,
                "content": text,
                "media_type": None,
                "media_analysis": None,
                "sort_at": created_at,
                "primary_topic_id": topic_id,
                "topic_ids": [topic_id] if topic_id else [],
                "source_created_at": created_at,
                "source_updated_at": updated_at,
            }

        return None

    def _canonical_artifact_text(self, row: dict[str, Any]) -> str:
        payload = row.get("payload") or {}

        def flatten(value: Any) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                parts: list[str] = []
                for item in value:
                    parts.extend(flatten(item))
                return parts
            if isinstance(value, dict):
                parts = []
                for key in (
                    "title",
                    "intent",
                    "ask",
                    "done_when",
                    "summary",
                    "notes",
                    "review_summary",
                    "prep_summary",
                    "text",
                ):
                    parts.extend(flatten(value.get(key)))
                return parts
            return [str(value)]

        keys_by_type = {
            "live_prep_brief": ("agenda", "notes"),
            "live_debrief": (
                "review_summary",
                "live_debrief",
                "what_heard",
                "what_decided",
                "still_open",
                "what_to_remember",
                "durable_write_summary",
                "open_questions",
            ),
            "review_summary": ("review_summary", "summary", "live_debrief", "review"),
            "agenda_revision": ("prep_summary", "agenda", "summary", "notes", "items"),
            "transcript_reflection": ("transcript_reflection", "summary", "notes"),
        }
        keys = keys_by_type.get(row.get("artifact_type"), tuple(payload.keys()))
        parts: list[str] = []
        for key in keys:
            parts.extend(flatten(payload.get(key)))
        return "\n".join(part.strip() for part in parts if part and part.strip())

    def _all_searchable_content_rows(self) -> list[dict[str, Any]]:
        source_maps: tuple[tuple[str, dict[UUID, dict[str, Any]]], ...] = (
            ("message", self.messages),
            ("memory", self.memories),
            ("observation", self.observations),
            ("distillation", self.distillations),
            ("artifact", self.conversation_artifacts),
            ("conversation_note", self.conversation_notes),
            ("theme", self.themes),
        )
        rows: list[dict[str, Any]] = []
        for source_type, source_rows in source_maps:
            for source_id in source_rows:
                searchable = self._searchable_content_row(source_type, source_id)
                if searchable is not None:
                    rows.append(searchable)
        return rows

    def _render_searchable_projection(
        self, message: dict[str, Any], searchable: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "message_id": searchable["message_id"],
            "sender_id": searchable.get("sender_id"),
            "recipient_id": searchable.get("recipient_id"),
            "thread_owner_user_id": searchable.get("thread_owner_user_id"),
            "thread_owner_partner_share": searchable.get(
                "thread_owner_partner_share"
            ),
            "bot_id": searchable.get("bot_id"),
            "topic_id": searchable.get("topic_id"),
            "dyad_id": searchable.get("dyad_id"),
            "direction": message.get("direction"),
            "sent_at": searchable.get("sent_at"),
            "content": message.get("content"),
            "media_type": message.get("media_type"),
            "media_analysis": message.get("media_analysis"),
            "charge": message.get("charge", "routine") or "routine",
            "edited_at": message.get("edited_at"),
            "edit_history": message.get("edit_history"),
        }

    def _match_searchable_scope(
        self,
        searchable: dict[str, Any],
        *,
        bot_id: str | None,
        viewer_id: UUID | None,
        participants: set[UUID],
        topic_id: UUID | None,
        thread_owner_user_id: UUID | None,
        dyad_id: UUID | None,
    ) -> bool:
        if bot_id is not None and searchable.get("bot_id") != bot_id:
            return False
        if participants:
            if searchable.get("thread_owner_user_id") not in participants:
                return False
            if (
                searchable.get("sender_id") not in participants
                and searchable.get("recipient_id") not in participants
            ):
                return False
        if (
            viewer_id is not None
            and searchable.get("thread_owner_user_id") != viewer_id
            and searchable.get("thread_owner_partner_share") != "opt_in"
        ):
            return False
        if topic_id is not None and searchable.get("topic_id") != topic_id:
            return False
        if (
            thread_owner_user_id is not None
            and searchable.get("thread_owner_user_id") != thread_owner_user_id
        ):
            return False
        if dyad_id is not None and searchable.get("dyad_id") not in (None, dyad_id):
            return False
        if searchable.get("active_oob_severity") in {"firm", "hard"}:
            return False
        return True

    def _project_searchable_rows(
        self, compact: str, *args: Any
    ) -> list[dict[str, Any]]:
        bot_id = args[0] if args and isinstance(args[0], str) else None
        viewer_id = args[1] if len(args) > 1 and isinstance(args[1], UUID) else None
        participants = (
            set(args[2])
            if len(args) > 2
            and isinstance(args[2], list)
            and all(isinstance(item, UUID) for item in args[2])
            else set()
        )
        idx = 3
        topic_id = None
        if "m.topic_id = $" in compact and idx < len(args) and isinstance(args[idx], UUID):
            topic_id = args[idx]
            idx += 1
        explicit_thread_owner = compact.count("m.thread_owner_user_id = $") > 1
        thread_owner_user_id = None
        if explicit_thread_owner and idx < len(args) and isinstance(args[idx], UUID):
            thread_owner_user_id = args[idx]
            idx += 1
        dyad_id = None
        if "m.dyad_id = $" in compact and idx < len(args) and isinstance(args[idx], UUID):
            dyad_id = args[idx]
            idx += 1
        lower_bound = None
        upper_bound = None
        if "m.sent_at >=" in compact and idx < len(args) and isinstance(args[idx], datetime):
            lower_bound = args[idx]
            idx += 1
        if "m.sent_at < $" in compact and idx < len(args) and isinstance(args[idx], datetime):
            upper_bound = args[idx]
            idx += 1
        anchor_message_id = None
        if (
            "m.message_id = $" in compact
            and "JOIN mediator.v_searchable_messages m ON m.message_id = ranked_ids.message_id"
            not in compact
            and idx < len(args)
            and isinstance(args[idx], UUID)
        ):
            anchor_message_id = args[idx]
            idx += 1
        anchor_sent_at = None
        tuple_operator = None
        if "(m.sent_at, m.message_id) <=" in compact:
            tuple_operator = "<="
        elif "(m.sent_at, m.message_id) <" in compact:
            tuple_operator = "<"
        elif "(m.sent_at, m.message_id) >" in compact:
            tuple_operator = ">"
        anchor_tuple_id = None
        if tuple_operator is not None and idx + 1 < len(args):
            anchor_sent_at = args[idx]
            anchor_tuple_id = args[idx + 1]
            idx += 2
        ranked_ids = None
        if (
            "WITH ranked_ids AS" in compact
            and idx < len(args)
            and isinstance(args[idx], list)
            and all(isinstance(item, UUID) for item in args[idx])
        ):
            ranked_ids = list(args[idx])
            idx += 1
        limit = args[idx] if idx < len(args) and isinstance(args[idx], int) else None

        rows: list[dict[str, Any]] = []
        for message in self.messages.values():
            searchable = self._searchable_message_row(message)
            if searchable is None:
                continue
            if not self._match_searchable_scope(
                searchable,
                bot_id=bot_id,
                viewer_id=viewer_id,
                participants=participants,
                topic_id=topic_id,
                thread_owner_user_id=thread_owner_user_id,
                dyad_id=dyad_id,
            ):
                continue
            sent_at = searchable.get("sent_at")
            if lower_bound is not None and sent_at is not None and sent_at < lower_bound:
                continue
            if upper_bound is not None and sent_at is not None and sent_at >= upper_bound:
                continue
            if anchor_message_id is not None and searchable.get("message_id") != anchor_message_id:
                continue
            if tuple_operator is not None and anchor_sent_at is not None and anchor_tuple_id is not None:
                row_key = (searchable.get("sent_at"), searchable.get("message_id"))
                anchor_key = (anchor_sent_at, anchor_tuple_id)
                if tuple_operator == "<" and not (row_key < anchor_key):
                    continue
                if tuple_operator == "<=" and not (row_key <= anchor_key):
                    continue
                if tuple_operator == ">" and not (row_key > anchor_key):
                    continue
            rows.append(self._render_searchable_projection(message, searchable))

        if ranked_ids is not None:
            rows_by_id = {row["message_id"]: row for row in rows}
            ordered = [rows_by_id[message_id] for message_id in ranked_ids if message_id in rows_by_id]
            return ordered

        reverse = "ORDER BY m.sent_at DESC, m.message_id DESC" in compact
        rows.sort(
            key=lambda row: (
                row["sent_at"] or datetime.min.replace(tzinfo=UTC),
                row["message_id"],
            ),
            reverse=reverse,
        )
        if limit is not None:
            return rows[:limit]
        return rows

    def _row_matches_retrieval_visibility(
        self, row: dict[str, Any], args: tuple[Any, ...]
    ) -> bool:
        values = list(args)
        row_bot_id = row.get("bot_id")
        bot_id = next(
            (
                value
                for value in values
                if isinstance(value, str) and value == row_bot_id
            ),
            None,
        )
        uuid_values = [value for value in values if isinstance(value, UUID)]
        uuid_lists = [
            set(value)
            for value in values
            if isinstance(value, list) and all(isinstance(item, UUID) for item in value)
        ]
        topic_id = uuid_values[1] if len(uuid_values) > 1 else None
        dyad_id = uuid_values[2] if len(uuid_values) > 2 else None
        thread_owner = uuid_values[3] if len(uuid_values) > 3 else None
        participants = uuid_lists[0] if uuid_lists else None

        if bot_id is not None and row.get("bot_id") != bot_id:
            return False
        if topic_id is not None and row.get("topic_id") != topic_id:
            return False
        if dyad_id is not None and row.get("dyad_id") not in (None, dyad_id):
            return False
        if thread_owner is not None and row.get("thread_owner_user_id") != thread_owner:
            return False
        if participants is not None:
            if row.get("thread_owner_user_id") not in participants:
                return False
            if row.get("sender_id") not in participants and row.get("recipient_id") not in participants:
                return False
        if row.get("active_oob_severity") in {"firm", "hard"}:
            return False
        return True

    def link_topic(
        self, artifact_table: str, artifact_id: UUID, topic_id: UUID
    ) -> None:
        """Register a topic link for an artifact row.

        Called by tests that need the FakePool to enforce per-topic scoping
        (decay, hot-context property tests, etc.).  Rows without a link_topic
        entry fall back to global behaviour per _row_matches_topic.
        """
        self.artifact_topics[(artifact_table, artifact_id)] = topic_id

    def _row_matches_topic(self, table: str, row_id: UUID, topic_id: UUID) -> bool:
        """True when *row_id* belongs to *topic_id*, or when no link exists.

        Backward-compat fallback: rows without a link_topic entry update globally.
        New tests asserting per-topic decay MUST call pool.link_topic for every
        seeded artifact, or the scoping bug they're checking will silently pass.
        """
        key = (table, row_id)
        if key in self.artifact_topics:
            return self.artifact_topics[key] == topic_id
        return True

    def _message_matches_bot_topic(
        self, row: dict[str, Any], bot_id: str | None, topic_id: UUID | None
    ) -> bool:
        """Match scoped message reads while preserving pre-scope test fixtures.

        Rows with absent bot/topic keys are legacy fixture rows and default to
        mediator/relationship. Rows with explicit NULL stay unmatched, matching
        production behavior for legacy DB rows hidden from partner raw reads.
        """
        if bot_id is None or topic_id is None:
            return False
        row_bot_id = row["bot_id"] if "bot_id" in row else "mediator"
        row_topic_id = (
            row["topic_id"]
            if "topic_id" in row
            else UUID("00000000-0000-4000-8000-000000000001")
        )
        return row_bot_id == bot_id and row_topic_id == topic_id

    def acquire(self) -> FakeAcquireContext:
        return FakeAcquireContext(self)

    async def close(self) -> None:
        self.closed = True

    def channels_raise_undefined_table(self) -> None:
        """Next channels query will raise UndefinedTableError."""
        self._raise_undefined_table_on_channels = True

    async def fetchrow(self, sql: str, *args):
        compact = " ".join(sql.split())
        if (
            compact.startswith("SELECT m.message_id,")
            and "FROM mediator.v_searchable_messages m" in compact
        ):
            rows = self._project_searchable_rows(compact, *args)
            return rows[0] if rows else None
        if "WITH ranked_ids AS" in compact and "JOIN mediator.v_searchable_messages m" in compact:
            rows = self._project_searchable_rows(compact, *args)
            return rows[0] if rows else None
        if compact.startswith("SELECT message_id, canonical_text FROM mediator.v_searchable_messages"):
            row = self.messages.get(args[0])
            return self._searchable_message_row(row) if row is not None else None
        if compact.startswith(
            "SELECT source_type, source_id, message_id, canonical_text FROM mediator.v_searchable_content"
        ):
            source_type, source_id = args
            searchable = self._searchable_content_row(source_type, source_id)
            if searchable is None:
                return None
            return {
                "source_type": source_type,
                "source_id": source_id,
                "message_id": searchable["message_id"],
                "canonical_text": searchable["canonical_text"],
            }
        if compact.startswith("SELECT id, deleted_at, search_suppressed_at FROM mediator.messages"):
            row = self.messages.get(args[0])
            if row is None:
                return None
            return {
                "id": row["id"],
                "deleted_at": row.get("deleted_at"),
                "search_suppressed_at": row.get("search_suppressed_at"),
            }
        if compact.startswith("SELECT id, source_type, source_id, message_id, job_kind") and "FROM mediator.embed_jobs" in compact:
            source_type, source_id, job_kind, content_hash_value = args
            matches = [
                job
                for job in self.embed_jobs.values()
                if job.get("source_type", "message") == source_type
                and job.get("source_id", job["message_id"]) == source_id
                and job["job_kind"] == job_kind
                and job["status"] in {"pending", "processing"}
                and job.get("content_hash") == content_hash_value
            ]
            matches.sort(key=lambda row: (row.get("created_at"), str(row["id"])))
            return matches[0] if matches else None
        if compact.startswith("INSERT INTO mediator.embed_jobs"):
            source_type, source_id, message_id, job_kind, model, dimension, content_hash_value, now = args
            job_id = uuid4()
            row = {
                "id": job_id,
                "source_type": source_type,
                "source_id": source_id,
                "message_id": message_id,
                "job_kind": job_kind,
                "status": "pending",
                "model": model,
                "dimension": dimension,
                "content_hash": content_hash_value,
                "attempts": 0,
                "last_error": None,
                "next_attempt_at": now,
                "locked_at": None,
                "locked_by": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            self.embed_jobs[job_id] = row
            return row
        if compact.startswith("INSERT INTO users"):
            name, phone, timezone = args
            existing = next(
                (u for u in self.users.values() if u["phone"] == phone), None
            )
            if existing is not None:
                existing["name"] = name
                return existing
            row = {
                "id": uuid4(),
                "name": name,
                "phone": phone,
                "timezone": timezone,
                "onboarding_state": "pending",
                "pacing_preferences": {},
                "pregnancy_edd": None,
                "pregnancy_dating_basis": None,
                "pregnancy_lmp_date": None,
                "pregnancy_scan_date": None,
                "pregnancy_scan_corrected_at": None,
                "pregnancy_started_at": None,
                "pregnancy_ended_at": None,
                "pregnancy_outcome": None,
            }
            self.users[row["id"]] = row
            return row
        if (
            compact.startswith("SELECT id, name, phone, timezone FROM users WHERE id")
            or compact.startswith(
                "SELECT id, name, phone, timezone, onboarding_state FROM users WHERE id"
            )
            or compact.startswith(
                "SELECT id, name, phone, timezone, onboarding_state, pacing_preferences FROM users WHERE id"
            )
            or compact.startswith(
                "SELECT id, name, phone, timezone, onboarding_state, pacing_preferences, pregnancy_edd, pregnancy_dating_basis, pregnancy_lmp_date, pregnancy_scan_date, pregnancy_scan_corrected_at, pregnancy_started_at, pregnancy_ended_at, pregnancy_outcome FROM users WHERE id"
            )
        ):
            return self.users[args[0]]
        if compact.startswith("SELECT pacing_preferences FROM users WHERE id"):
            user = self.users.get(args[0])
            if user is None:
                return None
            return {"pacing_preferences": user.get("pacing_preferences", {})}
        if compact.startswith("SELECT phone FROM users WHERE id"):
            user = self.users.get(args[0])
            if user is None:
                return None
            return {"phone": user.get("phone")}
        if compact.startswith("SELECT address FROM user_identities WHERE user_id"):
            user_id_arg, transport_arg = args[0], args[1]
            for (transport, address), owner in self.user_identities.items():
                if owner == user_id_arg and transport == transport_arg:
                    return {"address": address}
            return None
        if compact.startswith("UPDATE users SET pacing_preferences"):
            user_id, preferences_json = args
            preferences = _coerce_jsonb(preferences_json)
            self.users.setdefault(
                user_id,
                {
                    "id": user_id,
                    "name": "User",
                    "phone": "1",
                    "timezone": "UTC",
                    "onboarding_state": "pending",
                    "pacing_preferences": {},
                    "pregnancy_edd": None,
                    "pregnancy_dating_basis": None,
                    "pregnancy_lmp_date": None,
                    "pregnancy_scan_date": None,
                    "pregnancy_scan_corrected_at": None,
                    "pregnancy_started_at": None,
                    "pregnancy_ended_at": None,
                    "pregnancy_outcome": None,
                },
            )
            self.users[user_id]["pacing_preferences"] = preferences
            return {"pacing_preferences": preferences}
        if compact.startswith("UPDATE users SET onboarding_state='welcomed'"):
            user_id = args[0]
            user = self.users.get(user_id)
            if user is None or user.get("onboarding_state", "pending") != "pending":
                return None
            user["onboarding_state"] = "welcomed"
            return {"id": user_id}
        if compact.startswith("SELECT id, name, phone, timezone, COALESCE(style_notes"):
            row = dict(self.users[args[0]])
            row.setdefault("style_notes", "")
            return row
        if compact.startswith("WITH bounds AS"):
            user_id = args[0]
            now = datetime.now(UTC)
            period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            period_end = period_start + timedelta(days=1)
            messages = [
                row
                for row in self.messages.values()
                if row.get("deleted_at") is None
                and (
                    row.get("sender_id") == user_id
                    or row.get("recipient_id") == user_id
                )
                and period_start <= row["sent_at"] < period_end
            ]
            return {
                "period_start": period_start,
                "period_end": period_end,
                "inbound_count": sum(
                    1 for row in messages if row.get("direction") == "inbound"
                ),
                "outbound_count": sum(
                    1 for row in messages if row.get("direction") == "outbound"
                ),
                "total_count": len(messages),
            }
        if compact.startswith(
            "SELECT whatsapp_message_id FROM messages WHERE id=$1 AND direction='inbound'"
        ):
            message_id, sender_id = args
            row = self.messages.get(message_id)
            if (
                row is None
                or row.get("direction") != "inbound"
                or row.get("sender_id") != sender_id
            ):
                return None
            return {"whatsapp_message_id": row.get("whatsapp_message_id")}
        if compact.startswith(
            "SELECT id, processing_state, whatsapp_message_id, content FROM messages WHERE outbound_part_key"
        ):
            part_key = args[0]
            for row in self.messages.values():
                if row.get("outbound_part_key") == part_key:
                    return {
                        "id": row["id"],
                        "processing_state": row.get("processing_state"),
                        "whatsapp_message_id": row.get("whatsapp_message_id"),
                        "content": row.get("content"),
                    }
            return None
        if compact.startswith("SELECT id FROM messages WHERE outbound_part_key"):
            part_key = args[0]
            for row in self.messages.values():
                if row.get("outbound_part_key") == part_key:
                    return {"id": row["id"]}
            return None
        if compact.startswith(
            "SELECT processing_state, whatsapp_message_id FROM messages WHERE id=$1 AND direction='outbound'"
        ):
            row = self.messages.get(args[0])
            if row is None or row.get("direction") != "outbound":
                return None
            return {
                "processing_state": row.get("processing_state"),
                "whatsapp_message_id": row.get("whatsapp_message_id"),
            }
        if compact.startswith(
            "SELECT id, media_type, media_url FROM messages WHERE id=$1"
        ):
            row = self.messages.get(args[0])
            if row is None or row.get("deleted_at") is not None:
                return None
            return {
                "id": row["id"],
                "media_type": row.get("media_type"),
                "media_url": row.get("media_url"),
            }
        if compact.startswith(
            "SELECT id, direction, sender_id, recipient_id, content, whatsapp_message_id, deleted_at, media_analysis FROM messages WHERE id=$1 AND ( sender_id = ANY($2::uuid[]) OR recipient_id = ANY($2::uuid[]) )"
        ):
            message_id, user_ids = args[:2]
            bot_filter = args[2] if len(args) > 2 else None
            topic_filter = args[3] if len(args) > 3 else None
            row = self.messages.get(message_id)
            if row is None:
                return None
            if (
                row.get("sender_id") not in user_ids
                and row.get("recipient_id") not in user_ids
            ):
                return None
            return {
                "id": row["id"],
                "direction": row.get("direction"),
                "sender_id": row.get("sender_id"),
                "recipient_id": row.get("recipient_id"),
                "content": row.get("content"),
                "whatsapp_message_id": row.get("whatsapp_message_id"),
                "deleted_at": row.get("deleted_at"),
                "media_analysis": row.get("media_analysis"),
            }
        if (
            compact.startswith(
                "SELECT id, direction, sender_id, recipient_id, content, media_type, media_url, media_analysis, deleted_at"
            )
            and "FROM messages" in compact
        ):
            message_id, user_ids = args[:2]
            bot_filter = args[2] if len(args) > 2 else None
            topic_filter = args[3] if len(args) > 3 else None
            row = self.messages.get(message_id)
            if row is None:
                return None
            if (
                row.get("sender_id") not in user_ids
                and row.get("recipient_id") not in user_ids
            ):
                return None
            if (
                "bot_id=$3" in compact
                and "topic_id=$4" in compact
                and not self._message_matches_bot_topic(row, bot_filter, topic_filter)
            ):
                return None
            return row
        if compact.startswith("WITH target AS ( SELECT id, content AS old_content"):
            new_content = args[0]
            content_encrypted = args[1] if len(args) == 3 else None
            wa_id = args[-1]
            for message in self.messages.values():
                if message["whatsapp_message_id"] != wa_id:
                    continue
                old_content = message.get("content")
                message["edit_history"] = [
                    {
                        "content": old_content,
                        "at": datetime.now(UTC).isoformat(),
                    }
                ]
                message["content"] = new_content
                if content_encrypted is not None:
                    message["content_encrypted"] = content_encrypted
                message["edited_at"] = datetime.now(UTC)
                return {
                    "id": message["id"],
                    "content": message.get("content"),
                    "media_analysis": message.get("media_analysis"),
                    "old_content": old_content,
                }
            return None
        if compact.startswith("UPDATE messages SET deleted_at"):
            wa_id = args[0]
            for message in self.messages.values():
                if message["whatsapp_message_id"] != wa_id:
                    continue
                message["deleted_at"] = datetime.now(UTC)
                return {"id": message["id"]}
            return None
        if compact.startswith("INSERT INTO messages"):
            if "direction, recipient_id" in compact:
                # Outbound: basic form appends (bot_id, topic_id).
                # Incremental-send form appends (bot_turn_id, outbound_part_key, outbound_part_index, bot_id, topic_id).
                recipient_id, content, _content_encrypted, state, *part_args = args
                if "bot_turn_id, outbound_part_key, outbound_part_index" in compact:
                    bot_turn_id = part_args[0] if len(part_args) >= 1 else None
                    outbound_part_key = part_args[1] if len(part_args) >= 2 else None
                    outbound_part_index = part_args[2] if len(part_args) >= 3 else None
                    outbound_bot_id = part_args[3] if len(part_args) >= 4 else None
                    outbound_topic_id = part_args[4] if len(part_args) >= 5 else None
                else:
                    bot_turn_id = None
                    outbound_part_key = None
                    outbound_part_index = None
                    outbound_bot_id = part_args[0] if len(part_args) >= 1 else None
                    outbound_topic_id = part_args[1] if len(part_args) >= 2 else None
                if outbound_part_key is not None:
                    existing = next(
                        (
                            row
                            for row in self.messages.values()
                            if row.get("outbound_part_key") == outbound_part_key
                        ),
                        None,
                    )
                    if existing is not None:
                        return None
                row = {
                    "id": uuid4(),
                    "direction": "outbound",
                    "sender_id": None,
                    "recipient_id": recipient_id,
                    "content": content,
                    "processing_state": state,
                    "sent_at": datetime.now(UTC),
                    "charge": None,
                    "whatsapp_message_id": None,
                    "media_type": None,
                    "media_url": None,
                    "media_duration_seconds": None,
                    "media_analysis": None,
                    "edit_history": None,
                    "edited_at": None,
                    "deleted_at": None,
                    "bot_turn_id": bot_turn_id,
                    "outbound_part_key": outbound_part_key,
                    "outbound_part_index": outbound_part_index,
                    "bot_id": outbound_bot_id,
                    "topic_id": outbound_topic_id,
                }
                self.messages[row["id"]] = row
                return {"id": row["id"]}
            # Inbound. Accept both the legacy 10-arg form (no content_encrypted)
            # and the 11-arg form that includes the AES-GCM ciphertext column.
            if "content_encrypted" in compact:
                (
                    user_id,
                    content,
                    _content_encrypted,
                    wa_id,
                    sent_at,
                    media_type,
                    media_url,
                    duration,
                    media_analysis,
                    *rest,
                ) = args
            else:
                (
                    user_id,
                    content,
                    wa_id,
                    sent_at,
                    media_type,
                    media_url,
                    duration,
                    media_analysis,
                    *rest,
                ) = args
            charge = rest[0] if rest else None
            inbound_bot_id = rest[1] if len(rest) > 1 else None
            inbound_topic_id = rest[2] if len(rest) > 2 else None
            if any(m["whatsapp_message_id"] == wa_id and m["bot_id"] == inbound_bot_id for m in self.messages.values()):
                return None
            row = {
                "id": uuid4(),
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": None,
                "content": content,
                "processing_state": "raw",
                "sent_at": sent_at,
                "charge": charge,
                "whatsapp_message_id": wa_id,
                "media_type": media_type,
                "media_url": media_url,
                "media_duration_seconds": duration,
                "media_analysis": media_analysis,
                "bot_id": inbound_bot_id,
                "topic_id": inbound_topic_id,
                "edit_history": None,
                "edited_at": None,
                "deleted_at": None,
                # 0041 inbound queue metadata
                "handled_at": None,
                "handled_by_turn_id": None,
                "handling_result": None,
                "processing_started_at": None,
                "processing_error": None,
                "processing_attempts": 0,
                # 0049 in-flight owner pointer
                "bot_turn_id": None,
            }
            self.messages[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("INSERT INTO bot_turns"):
            (
                triggered_by_message_id,
                triggering_message_ids,
                user_in_context,
                system_prompt_version,
                model_version,
                prompt_snapshot,
                prompt_snapshot_encrypted,
                bot_id,
                topic_id,
                bot_spec_version,
                hot_context_builder_version,
                tool_schema_version,
            ) = args
            row = {
                "id": uuid4(),
                "triggered_by_message_id": triggered_by_message_id,
                "triggering_message_ids": list(triggering_message_ids),
                "user_in_context": user_in_context,
                "system_prompt_version": system_prompt_version,
                "model_version": model_version,
                "prompt_snapshot": prompt_snapshot,
                "prompt_snapshot_encrypted": prompt_snapshot_encrypted,
                "started_at": datetime.now(UTC),
                "completed_at": None,
                "failure_reason": None,
                "reasoning": "",
                "reasoning_encrypted": None,
                "final_output_message_id": None,
                "tool_call_count": 0,
                "duration_ms": None,
                "bot_id": bot_id,
                "topic_id": topic_id,
                "bot_spec_version": bot_spec_version,
                "hot_context_builder_version": hot_context_builder_version,
                "tool_schema_version": tool_schema_version,
            }
            self.bot_turns[row["id"]] = row
            return {"id": row["id"], "started_at": row["started_at"]}
        if compact.startswith("INSERT INTO turn_audit_events"):
            (
                turn_id,
                event_type,
                step,
                severity,
                occurred_at,
                duration_ms,
                actor,
                message,
                metadata,
                sensitive_metadata_encrypted,
            ) = args
            event_seq = 1 + max(
                [
                    row["event_seq"]
                    for row in self.turn_audit_events
                    if row["turn_id"] == turn_id
                ],
                default=0,
            )
            row = {
                "id": uuid4(),
                "turn_id": turn_id,
                "event_seq": event_seq,
                "event_type": event_type,
                "step": step,
                "severity": severity,
                "occurred_at": occurred_at,
                "duration_ms": duration_ms,
                "actor": actor,
                "message": message,
                "metadata": _coerce_jsonb(metadata) or {},
                "sensitive_metadata_encrypted": sensitive_metadata_encrypted,
            }
            self.turn_audit_events.append(row)
            return {"id": row["id"], "event_seq": event_seq}
        if compact.startswith("INSERT INTO public.eval_runs"):
            (
                prompt_version,
                scenarios_passed,
                scenarios_failed,
                total_cost_usd,
                git_sha,
                notes,
            ) = args
            row = {
                "id": uuid4(),
                "run_at": datetime.now(UTC),
                "prompt_version": prompt_version,
                "scenarios_passed": scenarios_passed,
                "scenarios_failed": scenarios_failed,
                "total_cost_usd": Decimal(str(total_cost_usd)),
                "git_sha": git_sha,
                "notes": notes,
            }
            self.eval_runs[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("INSERT INTO public.eval_results"):
            (
                run_id,
                scenario_name,
                status,
                judge_verdicts,
                tool_calls,
                failure_reason,
            ) = args
            row = {
                "id": uuid4(),
                "run_id": run_id,
                "scenario_name": scenario_name,
                "status": status,
                "judge_verdicts": _coerce_jsonb(judge_verdicts),
                "tool_calls": _coerce_jsonb(tool_calls),
                "failure_reason": failure_reason,
                "created_at": datetime.now(UTC),
            }
            self.eval_results[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("UPDATE users SET style_notes"):
            notes, user_id = args
            self.users.setdefault(
                user_id,
                {"id": user_id, "name": "User", "phone": "1", "timezone": "UTC"},
            )
            self.users[user_id]["style_notes"] = notes
            return {"user_id": user_id, "updated_at": datetime.now(UTC)}
        # --- Pregnancy tool handlers ---
        if compact.startswith(
            "SELECT id, pregnancy_edd, pregnancy_ended_at FROM users WHERE id"
        ):
            user_id = args[0]
            user = self.users.get(
                user_id,
                {"id": user_id, "pregnancy_edd": None, "pregnancy_ended_at": None},
            )
            return {
                "id": user_id,
                "pregnancy_edd": user.get("pregnancy_edd"),
                "pregnancy_ended_at": user.get("pregnancy_ended_at"),
            }
        if compact.startswith(
            "SELECT id, pregnancy_edd, pregnancy_ended_at, pregnancy_dating_basis, pregnancy_started_at FROM users WHERE id"
        ):
            user_id = args[0]
            user = self.users.get(
                user_id,
                {
                    "id": user_id,
                    "pregnancy_edd": None,
                    "pregnancy_ended_at": None,
                    "pregnancy_dating_basis": None,
                    "pregnancy_started_at": None,
                },
            )
            return {
                "id": user_id,
                "pregnancy_edd": user.get("pregnancy_edd"),
                "pregnancy_ended_at": user.get("pregnancy_ended_at"),
                "pregnancy_dating_basis": user.get("pregnancy_dating_basis"),
                "pregnancy_started_at": user.get("pregnancy_started_at"),
            }
        if compact.startswith(
            "SELECT id, pregnancy_edd, pregnancy_ended_at, pregnancy_outcome FROM users WHERE id"
        ):
            user_id = args[0]
            user = self.users.get(
                user_id,
                {
                    "id": user_id,
                    "pregnancy_edd": None,
                    "pregnancy_ended_at": None,
                    "pregnancy_outcome": None,
                },
            )
            return {
                "id": user_id,
                "pregnancy_edd": user.get("pregnancy_edd"),
                "pregnancy_ended_at": user.get("pregnancy_ended_at"),
                "pregnancy_outcome": user.get("pregnancy_outcome"),
            }
        if (
            "UPDATE users SET pregnancy_edd" in compact
            and "pregnancy_scan_date = COALESCE" in compact
        ):
            # correct_pregnancy_edd (MUST come before simpler "UPDATE users SET pregnancy_edd")
            user_id, edd_val, dating_basis, scan_date, scan_corrected_at = args
            user = self.users.get(user_id)
            if user is not None:
                user["pregnancy_edd"] = edd_val
                user["pregnancy_dating_basis"] = dating_basis
                if scan_date is not None:
                    user["pregnancy_scan_date"] = scan_date
                if scan_corrected_at is not None:
                    user["pregnancy_scan_corrected_at"] = scan_corrected_at
            return {"id": user_id}
        if compact.startswith("UPDATE users SET pregnancy_edd"):
            # set_pregnancy_edd: UPDATE users SET pregnancy_edd = $2, pregnancy_dating_basis = $3, ...
            user_id, edd_val, dating_basis, lmp_date, scan_date, started_at = args
            self.users.setdefault(
                user_id,
                {
                    "id": user_id,
                    "name": "User",
                    "phone": "1",
                    "timezone": "UTC",
                    "pregnancy_edd": None,
                    "pregnancy_dating_basis": None,
                    "pregnancy_lmp_date": None,
                    "pregnancy_scan_date": None,
                    "pregnancy_started_at": None,
                    "pregnancy_ended_at": None,
                    "pregnancy_outcome": None,
                },
            )
            self.users[user_id].update(
                {
                    "pregnancy_edd": edd_val,
                    "pregnancy_dating_basis": dating_basis,
                    "pregnancy_lmp_date": lmp_date,
                    "pregnancy_scan_date": scan_date,
                    "pregnancy_started_at": started_at,
                }
            )
            return {"id": user_id}
        if compact.startswith("UPDATE users SET pregnancy_ended_at"):
            # end_pregnancy: UPDATE users SET pregnancy_ended_at = $2, pregnancy_outcome = $3 ...
            user_id, ended_at, outcome = args
            user = self.users.get(user_id)
            if user is not None:
                user["pregnancy_ended_at"] = ended_at
                user["pregnancy_outcome"] = outcome
            return {"id": user_id}
        if compact.startswith("INSERT INTO bridge_candidates"):
            if "partner_path" in compact:
                (
                    source_user_id,
                    target_user_id,
                    kind,
                    status,
                    sensitivity,
                    partner_path,
                    source_message_ids,
                    related_memory_ids,
                    related_observation_ids,
                    internal_note,
                    shareable_summary,
                    *_rest,
                ) = args
            else:
                (
                    source_user_id,
                    target_user_id,
                    kind,
                    status,
                    sensitivity,
                    source_message_ids,
                    related_memory_ids,
                    related_observation_ids,
                    internal_note,
                    shareable_summary,
                    *_rest,
                ) = args
                partner_path = "message_partner"
            now = datetime.now(UTC)
            row = {
                "id": uuid4(),
                "source_user_id": source_user_id,
                "target_user_id": target_user_id,
                "kind": kind,
                "status": status,
                "sensitivity": sensitivity,
                "partner_path": partner_path,
                "source_message_ids": list(source_message_ids or []),
                "related_memory_ids": list(related_memory_ids or []),
                "related_observation_ids": list(related_observation_ids or []),
                "internal_note": internal_note,
                "shareable_summary": shareable_summary,
                "sent_message_id": None,
                "created_at": now,
                "updated_at": now,
                "resolved_at": (
                    now
                    if status in {"sent", "declined", "blocked", "addressed", "expired"}
                    else None
                ),
            }
            self.bridge_candidates[row["id"]] = row
            return {
                **dict(row),
                "partner_path": row.get("partner_path", "message_partner"),
            }
        if (
            compact.startswith("SELECT id, source_user_id, target_user_id")
            and "FROM bridge_candidates" in compact
        ):
            candidate_id, user_id, partner_id = args
            row = self.bridge_candidates.get(candidate_id)
            if row is None:
                return None
            if {row["source_user_id"], row["target_user_id"]} != {user_id, partner_id}:
                return None
            return {
                **dict(row),
                "partner_path": row.get("partner_path", "message_partner"),
            }
        if compact.startswith("UPDATE bridge_candidates SET kind=COALESCE"):
            if "partner_path=COALESCE" in compact:
                (
                    candidate_id,
                    kind,
                    status,
                    sensitivity,
                    partner_path,
                    source_message_ids,
                    related_memory_ids,
                    related_observation_ids,
                    internal_note,
                    shareable_summary,
                ) = args
            else:
                (
                    candidate_id,
                    kind,
                    status,
                    sensitivity,
                    source_message_ids,
                    related_memory_ids,
                    related_observation_ids,
                    internal_note,
                    shareable_summary,
                ) = args
                partner_path = None
            row = self.bridge_candidates[candidate_id]
            if kind is not None:
                row["kind"] = kind
            if status is not None:
                row["status"] = status
                if (
                    status in {"sent", "declined", "blocked", "addressed", "expired"}
                    and row.get("resolved_at") is None
                ):
                    row["resolved_at"] = datetime.now(UTC)
            if sensitivity is not None:
                row["sensitivity"] = sensitivity
            if partner_path is not None:
                row["partner_path"] = partner_path
            if source_message_ids is not None:
                row["source_message_ids"] = list(source_message_ids)
            if related_memory_ids is not None:
                row["related_memory_ids"] = list(related_memory_ids)
            if related_observation_ids is not None:
                row["related_observation_ids"] = list(related_observation_ids)
            if internal_note is not None:
                row["internal_note"] = internal_note
            if shareable_summary is not None:
                row["shareable_summary"] = shareable_summary
            row["updated_at"] = datetime.now(UTC)
            return {
                **dict(row),
                "partner_path": row.get("partner_path", "message_partner"),
            }
        if compact.startswith("UPDATE bridge_candidates SET status=$2"):
            candidate_id, status, sent_message_id, internal_note = args
            row = self.bridge_candidates[candidate_id]
            row["status"] = status
            if sent_message_id is not None:
                row["sent_message_id"] = sent_message_id
            if internal_note is not None:
                row["internal_note"] = internal_note
            if (
                status in {"sent", "declined", "blocked", "addressed", "expired"}
                and row.get("resolved_at") is None
            ):
                row["resolved_at"] = datetime.now(UTC)
            row["updated_at"] = datetime.now(UTC)
            return {
                **dict(row),
                "partner_path": row.get("partner_path", "message_partner"),
            }
        if compact.startswith("SELECT id, content, status, visibility FROM memories WHERE id"):
            row = self.memories.get(args[0])
            if row is None:
                return None
            return {
                "id": row["id"],
                "content": row.get("content"),
                "status": row.get("status"),
                "visibility": row.get("visibility"),
            }
        if compact.startswith("SELECT id, content, status, significance FROM observations WHERE id"):
            row = self.observations.get(args[0])
            if row is None:
                return None
            return {
                "id": row["id"],
                "content": row.get("content"),
                "status": row.get("status"),
                "significance": row.get("significance"),
            }
        if compact.startswith("WITH new_artifact AS ( INSERT INTO memories"):
            (
                about_user_id,
                content,
                content_encrypted,
                visibility,
                shareable_summary,
                shareable_summary_encrypted,
                related_theme_ids,
                bot_id,
                topic_id_list,
                reason,
            ) = args
            row = {
                "id": uuid4(),
                "about_user_id": about_user_id,
                "content": content,
                "content_encrypted": content_encrypted,
                "visibility": visibility,
                "shareable_summary": shareable_summary,
                "shareable_summary_encrypted": shareable_summary_encrypted,
                "related_theme_ids": list(related_theme_ids or []),
                "recorded_by_bot_id": bot_id,
                "status": "active",
                "supersedes_memory_id": None,
                "created_at": datetime.now(UTC),
                "last_referenced_at": None,
            }
            self.memories[row["id"]] = row
            for tid in list(topic_id_list or []):
                self.artifact_topics_rows.append(
                    {
                        "artifact_table": "memories",
                        "artifact_id": row["id"],
                        "topic_id": tid,
                        "reason": reason,
                    }
                )
            return {"id": row["id"]}
        if compact.startswith("INSERT INTO memories (about_user_id"):
            # (about_user_id, content, content_encrypted, related_theme_ids)
            about_user_id, content, _content_encrypted, related_theme_ids = args
            row = {
                "id": uuid4(),
                "about_user_id": about_user_id,
                "content": content,
                "visibility": "private",
                "shareable_summary": None,
                "shareable_summary_encrypted": None,
                "related_theme_ids": list(related_theme_ids or []),
                "status": "active",
                "supersedes_memory_id": None,
                "created_at": datetime.now(UTC),
                "last_referenced_at": None,
            }
            self.memories[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("UPDATE memories SET"):
            memory_id = args[-1]
            row = self.memories.setdefault(memory_id, {"id": memory_id, "status": "active"})
            lowered = compact.lower()
            arg_index = 0
            if "content=$" in lowered:
                row["content"] = args[arg_index]
                arg_index += 1
            if "content_encrypted=$" in lowered:
                row["content_encrypted"] = args[arg_index]
                arg_index += 1
            if "related_theme_ids=$" in lowered:
                row["related_theme_ids"] = list(args[arg_index] or [])
                arg_index += 1
            if "status=$" in lowered:
                row["status"] = args[arg_index]
            return {"id": memory_id}
        if compact.startswith("WITH old AS ( UPDATE memories SET status='superseded'"):
            # (old_id, new_content, content_encrypted, related_theme_ids)
            old_id, new_content, _content_encrypted, related_theme_ids, *_rest = args
            old = self.memories[old_id]
            old["status"] = "superseded"
            new = {
                "id": uuid4(),
                "about_user_id": old["about_user_id"],
                "content": new_content,
                "visibility": "private",
                "shareable_summary": None,
                "shareable_summary_encrypted": None,
                "related_theme_ids": list(related_theme_ids or []),
                "status": "active",
                "supersedes_memory_id": old_id,
                "created_at": datetime.now(UTC),
                "last_referenced_at": None,
            }
            self.memories[new["id"]] = new
            return {"new_id": new["id"], "old_id": old_id}
        if compact.startswith("WITH new_artifact AS ( INSERT INTO themes"):
            title, description, sentiment, health, _bot_id, topic_id_list, reason = args
            row = {
                "id": uuid4(),
                "title": title,
                "description": description,
                "status": "active",
                "sentiment": sentiment,
                "health": health,
                "last_reinforced_at": datetime.now(UTC),
                "last_active_at": datetime.now(UTC),
            }
            self.themes[row["id"]] = row
            for tid in list(topic_id_list or []):
                self.artifact_topics_rows.append(
                    {
                        "artifact_table": "themes",
                        "artifact_id": row["id"],
                        "topic_id": tid,
                        "reason": reason,
                    }
                )
            return {"id": row["id"]}
        if compact.startswith("INSERT INTO themes"):
            title, description, sentiment, health = args
            row = {
                "id": uuid4(),
                "title": title,
                "description": description,
                "status": "active",
                "sentiment": sentiment,
                "health": health,
                "last_reinforced_at": datetime.now(UTC),
                "last_active_at": datetime.now(UTC),
            }
            self.themes[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("UPDATE themes SET"):
            theme_id = args[-1]
            self.themes.setdefault(theme_id, {"id": theme_id, "status": "active"})
            return {"id": theme_id}
        if compact.startswith("WITH new_artifact AS ( INSERT INTO watch_items"):
            (
                owner_user_id,
                content,
                due_at,
                related_theme_ids,
                bot_id,
                topic_id_list,
                reason,
            ) = args
            row = {
                "id": uuid4(),
                "owner_user_id": owner_user_id,
                "content": content,
                "due_at": due_at,
                "related_theme_ids": list(related_theme_ids or []),
                "status": "open",
            }
            self.watch_items[row["id"]] = row
            for tid in list(topic_id_list or []):
                self.artifact_topics_rows.append(
                    {
                        "artifact_table": "watch_items",
                        "artifact_id": row["id"],
                        "topic_id": tid,
                        "reason": reason,
                    }
                )
            return {"id": row["id"]}
        if compact.startswith("INSERT INTO watch_items"):
            owner_user_id, content, due_at, related_theme_ids = args
            row = {
                "id": uuid4(),
                "owner_user_id": owner_user_id,
                "content": content,
                "due_at": due_at,
                "related_theme_ids": list(related_theme_ids or []),
                "status": "open",
            }
            self.watch_items[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("UPDATE watch_items SET status='addressed'"):
            note, watch_item_id = args
            self.watch_items.setdefault(watch_item_id, {"id": watch_item_id})
            self.watch_items[watch_item_id].update(
                status="addressed", addressing_note=note, addressed_at=datetime.now(UTC)
            )
            return {
                "id": watch_item_id,
                "addressed_at": self.watch_items[watch_item_id]["addressed_at"],
            }
        if compact.startswith("UPDATE watch_items SET"):
            watch_item_id = args[-1]
            self.watch_items.setdefault(watch_item_id, {"id": watch_item_id})
            return {"id": watch_item_id}
        if compact.startswith("WITH new_artifact AS ( INSERT INTO observations"):
            # (content, content_encrypted, about_user_id, confidence, significance, scoring_prompt_version, related_theme_ids, supporting_message_ids, bot_id, topic_id_list, reason)
            (
                content,
                _content_encrypted,
                about_user_id,
                confidence,
                significance,
                scoring_prompt_version,
                related_theme_ids,
                supporting_message_ids,
                bot_id,
                topic_id_list,
                reason,
            ) = args
            row = {
                "id": uuid4(),
                "content": content,
                "about_user_id": about_user_id,
                "confidence": confidence,
                "significance": significance,
                "scoring_prompt_version": scoring_prompt_version,
                "related_theme_ids": list(related_theme_ids or []),
                "supporting_message_ids": list(supporting_message_ids or []),
                "status": "active",
            }
            self.observations[row["id"]] = row
            for tid in list(topic_id_list or []):
                self.artifact_topics_rows.append(
                    {
                        "artifact_table": "observations",
                        "artifact_id": row["id"],
                        "topic_id": tid,
                        "reason": reason,
                    }
                )
            return {"id": row["id"]}
        if compact.startswith("INSERT INTO observations"):
            # (content, content_encrypted, about_user_id, confidence, significance, scoring_prompt_version, related_theme_ids, supporting_message_ids)
            (
                content,
                _content_encrypted,
                about_user_id,
                confidence,
                significance,
                scoring_prompt_version,
                related_theme_ids,
                supporting_message_ids,
            ) = args
            row = {
                "id": uuid4(),
                "content": content,
                "about_user_id": about_user_id,
                "confidence": confidence,
                "significance": significance,
                "scoring_prompt_version": scoring_prompt_version,
                "related_theme_ids": list(related_theme_ids or []),
                "supporting_message_ids": list(supporting_message_ids or []),
                "status": "active",
            }
            self.observations[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("UPDATE observations SET"):
            if (
                "significance = $1" in compact
                and "scoring_prompt_version = $2" in compact
            ):
                significance, scoring_prompt_version, observation_id = args
                self.observations.setdefault(
                    observation_id, {"id": observation_id, "status": "active"}
                )
                self.observations[observation_id]["significance"] = significance
                self.observations[observation_id][
                    "scoring_prompt_version"
                ] = scoring_prompt_version
                self.observations[observation_id]["last_reinforced_at"] = (
                    self.observations[observation_id].get("last_reinforced_at")
                    or datetime.now(UTC)
                )
                return {"id": observation_id}
            observation_id = args[-1]
            row = self.observations.setdefault(
                observation_id, {"id": observation_id, "status": "active"}
            )
            lowered = compact.lower()
            arg_index = 0
            if "content=$" in lowered:
                row["content"] = args[arg_index]
                arg_index += 1
            if "content_encrypted=$" in lowered:
                row["content_encrypted"] = args[arg_index]
                arg_index += 1
            if "confidence=$" in lowered:
                row["confidence"] = args[arg_index]
                arg_index += 1
            if "significance=$" in lowered:
                row["significance"] = args[arg_index]
                arg_index += 1
            if "status=$" in lowered:
                row["status"] = args[arg_index]
                arg_index += 1
            if "related_theme_ids=$" in lowered:
                row["related_theme_ids"] = list(args[arg_index] or [])
            return {"id": observation_id}
        if compact.startswith("WITH new_artifact AS ( INSERT INTO distillations"):
            (
                content,
                content_encrypted,
                confidence,
                sensitivity,
                visibility,
                shareable_summary,
                shareable_summary_encrypted,
                source_user_ids,
                related_memory_ids,
                related_observation_ids,
                related_theme_ids,
                supporting_message_ids,
                triggering_message_id,
                bot_id,
                topic_id_list,
                reason,
            ) = args
            row = {
                "id": uuid4(),
                "content": content,
                "content_encrypted": content_encrypted,
                "confidence": confidence,
                "status": "active",
                "sensitivity": sensitivity,
                "visibility": visibility,
                "shareable_summary": shareable_summary,
                "shareable_summary_encrypted": shareable_summary_encrypted,
                "source_user_ids": list(source_user_ids or []),
                "related_memory_ids": list(related_memory_ids or []),
                "related_observation_ids": list(related_observation_ids or []),
                "related_theme_ids": list(related_theme_ids or []),
                "supporting_message_ids": list(supporting_message_ids or []),
                "created_from_tool_call_id": None,
                "triggering_message_id": triggering_message_id,
                "supersedes_distillation_id": None,
                "superseded_by_distillation_id": None,
                "revision_note": None,
                "revision_count": 0,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "revised_at": None,
                "retired_at": None,
                "recorded_by_bot_id": bot_id,
            }
            self.distillations[row["id"]] = row
            for tid in list(topic_id_list or []):
                self.artifact_topics_rows.append(
                    {
                        "artifact_table": "distillations",
                        "artifact_id": row["id"],
                        "topic_id": tid,
                        "reason": reason,
                    }
                )
            return {"id": row["id"]}
        if compact.startswith("INSERT INTO distillations"):
            (
                content,
                content_encrypted,
                confidence,
                sensitivity,
                visibility,
                shareable_summary,
                shareable_summary_encrypted,
                source_user_ids,
                related_memory_ids,
                related_observation_ids,
                related_theme_ids,
                supporting_message_ids,
                triggering_message_id,
                *rest,
            ) = args
            recorded_by_bot_id = rest[0] if rest else None
            row = {
                "id": uuid4(),
                "content": content,
                "content_encrypted": content_encrypted,
                "confidence": confidence,
                "status": "active",
                "sensitivity": sensitivity,
                "visibility": visibility,
                "shareable_summary": shareable_summary,
                "shareable_summary_encrypted": shareable_summary_encrypted,
                "source_user_ids": list(source_user_ids or []),
                "related_memory_ids": list(related_memory_ids or []),
                "related_observation_ids": list(related_observation_ids or []),
                "related_theme_ids": list(related_theme_ids or []),
                "supporting_message_ids": list(supporting_message_ids or []),
                "created_from_tool_call_id": None,
                "triggering_message_id": triggering_message_id,
                "supersedes_distillation_id": None,
                "superseded_by_distillation_id": None,
                "revision_note": None,
                "revision_count": 0,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "revised_at": None,
                "retired_at": None,
                "recorded_by_bot_id": recorded_by_bot_id,
            }
            self.distillations[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("UPDATE distillations SET"):
            distillation_id = args[-1]
            row = self.distillations.setdefault(
                distillation_id,
                {
                    "id": distillation_id,
                    "status": "active",
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                },
            )
            fields = [
                "content",
                "confidence",
                "status",
                "sensitivity",
                "visibility",
                "shareable_summary",
                "source_user_ids",
                "related_memory_ids",
                "related_observation_ids",
                "related_theme_ids",
                "supporting_message_ids",
                "revision_note",
            ]
            param_index = 0
            for field in fields:
                if f"{field}=$" not in compact:
                    continue
                row[field] = args[param_index]
                param_index += 1
                if field in {"content", "shareable_summary"}:
                    encrypted_field = (
                        f"{field}_encrypted"
                        if field == "content"
                        else "shareable_summary_encrypted"
                    )
                    row[encrypted_field] = args[param_index]
                    param_index += 1
            if "retired_at=COALESCE" in compact:
                row["retired_at"] = row.get("retired_at") or datetime.now(UTC)
            row["updated_at"] = datetime.now(UTC)
            return {"id": distillation_id}
        if (
            "WITH old AS (" in compact
            and "revision_count FROM distillations" in compact
        ):
            (
                old_id,
                new_content,
                new_content_encrypted,
                confidence,
                sensitivity,
                visibility,
                shareable_summary,
                shareable_summary_encrypted,
                source_user_ids,
                related_memory_ids,
                related_observation_ids,
                related_theme_ids,
                supporting_message_ids,
                triggering_message_id,
                revision_note,
                recorded_by_bot_id,
                *_rest,
            ) = args
            old = self.distillations.get(old_id)
            if old is None or old.get("status") != "active":
                return None
            old_revision_count = old.get("revision_count", 0)
            new = {
                "id": uuid4(),
                "content": new_content,
                "content_encrypted": new_content_encrypted,
                "confidence": confidence,
                "status": "active",
                "sensitivity": sensitivity,
                "visibility": visibility,
                "shareable_summary": shareable_summary,
                "shareable_summary_encrypted": shareable_summary_encrypted,
                "source_user_ids": list(source_user_ids or []),
                "related_memory_ids": list(related_memory_ids or []),
                "related_observation_ids": list(related_observation_ids or []),
                "related_theme_ids": list(related_theme_ids or []),
                "supporting_message_ids": list(supporting_message_ids or []),
                "created_from_tool_call_id": None,
                "triggering_message_id": triggering_message_id,
                "supersedes_distillation_id": old_id,
                "superseded_by_distillation_id": None,
                "revision_note": revision_note,
                "revision_count": old_revision_count + 1,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "revised_at": None,
                "retired_at": None,
                "recorded_by_bot_id": recorded_by_bot_id,
            }
            self.distillations[new["id"]] = new
            old["status"] = "revised"
            old["superseded_by_distillation_id"] = new["id"]
            old["revision_note"] = revision_note
            old["revision_count"] = old_revision_count + 1
            old["revised_at"] = datetime.now(UTC)
            old["updated_at"] = datetime.now(UTC)
            return {"new_id": new["id"], "old_id": old_id}
        if (
            compact.startswith("SELECT id, content, status, visibility, superseded_by_distillation_id")
            and "FROM distillations WHERE id = $1" in compact
        ):
            row = self.distillations.get(args[0])
            if row is None:
                return None
            return {
                "id": row["id"],
                "content": row.get("content"),
                "status": row.get("status", "active"),
                "visibility": row.get("visibility", "private"),
                "superseded_by_distillation_id": row.get(
                    "superseded_by_distillation_id"
                ),
                "revised_at": row.get("revised_at"),
                "retired_at": row.get("retired_at"),
            }
        if compact.startswith("WITH new_artifact AS ( INSERT INTO out_of_bounds"):
            # (owner_id, sensitive_core, sensitive_core_encrypted, shareable_context, severity, review_at, bot_id, topic_id_list, reason)
            (
                owner_id,
                sensitive_core,
                sensitive_core_encrypted,
                shareable_context,
                severity,
                review_at,
                _bot_id,
                topic_id_list,
                reason,
            ) = args
            row = {
                "id": uuid4(),
                "owner_id": owner_id,
                "sensitive_core": sensitive_core,
                "sensitive_core_encrypted": sensitive_core_encrypted,
                "shareable_context": shareable_context,
                "severity": severity,
                "review_at": review_at,
                "status": "active",
            }
            self.out_of_bounds[row["id"]] = row
            for tid in list(topic_id_list or []):
                self.artifact_topics_rows.append(
                    {
                        "artifact_table": "out_of_bounds",
                        "artifact_id": row["id"],
                        "topic_id": tid,
                        "reason": reason,
                    }
                )
            return {"id": row["id"]}
        if compact.startswith("INSERT INTO out_of_bounds"):
            # (owner_id, sensitive_core, sensitive_core_encrypted, shareable_context, severity, review_at)
            (
                owner_id,
                sensitive_core,
                sensitive_core_encrypted,
                shareable_context,
                severity,
                review_at,
            ) = args
            row = {
                "id": uuid4(),
                "owner_id": owner_id,
                "sensitive_core": sensitive_core,
                "sensitive_core_encrypted": sensitive_core_encrypted,
                "shareable_context": shareable_context,
                "severity": severity,
                "review_at": review_at,
                "status": "active",
            }
            self.out_of_bounds[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("UPDATE out_of_bounds SET status='lifted'"):
            oob_id = args[0]
            self.out_of_bounds.setdefault(oob_id, {"id": oob_id})
            self.out_of_bounds[oob_id]["status"] = "lifted"
            return {"id": oob_id, "lifted_at": datetime.now(UTC)}
        if compact.startswith("UPDATE out_of_bounds SET"):
            oob_id = args[-1]
            self.out_of_bounds.setdefault(oob_id, {"id": oob_id})
            return {"id": oob_id}
        if compact.startswith("UPDATE scheduled_jobs SET status='superseded'"):
            user_id = args[0]
            for job in self.scheduled_jobs.values():
                if (
                    job["user_id"] == user_id
                    and job["job_type"] == "checkin"
                    and job["status"] == "pending"
                ):
                    job["status"] = "superseded"
                    return {"id": job["id"]}
            return None
        if (
            compact.startswith("INSERT INTO scheduled_jobs")
            and "SELECT NULL, 'heartbeat'" in compact
        ):
            scheduled_for = args[0]
            if any(
                job["job_type"] == "heartbeat" and job["status"] == "pending"
                for job in self.scheduled_jobs.values()
            ):
                return None
            row = {
                "id": uuid4(),
                "user_id": None,
                "job_type": "heartbeat",
                "scheduled_for": scheduled_for,
                "context": {},
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 2,
                "delayed": False,
                "claimed_at": None,
                "claimed_by": None,
            }
            self.scheduled_jobs[row["id"]] = row
            return {"id": row["id"], "scheduled_for": scheduled_for}
        if (
            compact.startswith("INSERT INTO scheduled_jobs")
            and "'deferred_turn'" in compact
        ):
            if len(args) >= 5:
                user_id, scheduled_for, context_json, bot_id, topic_id = args[:5]
            else:
                user_id, scheduled_for, context_json = args[:3]
                bot_id = None
                topic_id = None
            if any(
                job["user_id"] == user_id
                and job["job_type"] == "deferred_turn"
                and job["status"] == "pending"
                for job in self.scheduled_jobs.values()
            ):
                return None
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "job_type": "deferred_turn",
                "scheduled_for": scheduled_for,
                "context": _coerce_jsonb(context_json),
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 2,
                "delayed": False,
                "claimed_at": None,
                "claimed_by": None,
                "bot_id": bot_id,
                "topic_id": topic_id,
            }
            self.scheduled_jobs[row["id"]] = row
            return {"id": row["id"], "scheduled_for": scheduled_for}
        if (
            compact.startswith("INSERT INTO scheduled_jobs")
            and "'scheduled_task'" in compact
            and "WHERE NOT EXISTS" in compact
        ):
            user_id, scheduled_for, context_json = args[:3]
            source_job_id = args[-1]
            if any(
                job.get("job_type") == "scheduled_task"
                and job.get("status") == "pending"
                and str(job.get("context", {}).get("source_job_id"))
                == str(source_job_id)
                for job in self.scheduled_jobs.values()
            ):
                return None
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "job_type": "scheduled_task",
                "scheduled_for": scheduled_for,
                "context": _coerce_jsonb(context_json),
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 2,
                "delayed": False,
                "claimed_at": None,
                "claimed_by": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            self.scheduled_jobs[row["id"]] = row
            return {"id": row["id"], "scheduled_for": scheduled_for}
        if (
            compact.startswith("INSERT INTO scheduled_jobs")
            and "'scheduled_task'" in compact
        ):
            user_id, scheduled_for, context_json = args[:3]
            bot_id = args[3] if len(args) > 3 else None
            topic_id = args[4] if len(args) > 4 else None
            context = _coerce_jsonb(context_json) or {}
            # Partner-nudge: emulate unique partial index
            # (user_id, bot_id, context->>'originating_user_id') WHERE
            # status='pending' AND kind='partner_nudge'.
            if context.get("kind") == "partner_nudge":
                originating_user_id = context.get("originating_user_id")
                for existing in self.scheduled_jobs.values():
                    if (
                        existing.get("status") == "pending"
                        and existing.get("job_type") == "scheduled_task"
                        and existing.get("user_id") == user_id
                        and existing.get("bot_id") == bot_id
                        and (existing.get("context") or {}).get("kind") == "partner_nudge"
                        and str(
                            (existing.get("context") or {}).get(
                                "originating_user_id"
                            )
                        )
                        == str(originating_user_id)
                    ):
                        # Simulate asyncpg UniqueViolationError on the
                        # idx_scheduled_jobs_one_pending_partner_nudge index.
                        UniqueViolationError = type(
                            "UniqueViolationError", (Exception,), {}
                        )
                        err = UniqueViolationError(
                            "duplicate key value violates unique constraint"
                        )
                        err.sqlstate = "23505"
                        err.pgcode = "23505"
                        raise err
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "job_type": "scheduled_task",
                "scheduled_for": scheduled_for,
                "context": context,
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 2,
                "delayed": False,
                "claimed_at": None,
                "claimed_by": None,
                "bot_id": bot_id,
                "topic_id": topic_id,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            self.scheduled_jobs[row["id"]] = row
            return {
                "job_id": row["id"],
                "scheduled_for": scheduled_for,
                "context": row["context"],
            }
        if compact.startswith(
            "SELECT id, status, context FROM scheduled_jobs WHERE id"
        ):
            row = self.scheduled_jobs.get(args[0])
            if row is None or row.get("job_type") != "scheduled_task":
                return None
            return {
                "id": row["id"],
                "status": row["status"],
                "context": row.get("context") or {},
            }
        if compact.startswith(
            "UPDATE scheduled_jobs SET status='cancelled' WHERE id=$1 AND status='pending' AND context->>'originating_user_id'=$2 AND context->>'kind'='partner_nudge'"
        ):
            job_id, originating_user_id = args
            row = self.scheduled_jobs.get(job_id)
            if (
                row is None
                or row.get("status") != "pending"
                or (row.get("context") or {}).get("kind") != "partner_nudge"
                or str((row.get("context") or {}).get("originating_user_id"))
                != str(originating_user_id)
            ):
                return None
            row["status"] = "cancelled"
            row["updated_at"] = datetime.now(UTC)
            return {"id": row["id"]}
        if compact.startswith(
            "SELECT id, user_id, scheduled_for, context, status FROM scheduled_jobs WHERE id"
        ):
            row = self.scheduled_jobs.get(args[0])
            return dict(row) if row is not None else None
        if compact.startswith("INSERT INTO scheduled_jobs") and (
            "'watch_item_due'" in compact
            or "VALUES ($1, $2, $3, $4::jsonb, 'pending'" in compact
            and args[1] == "watch_item_due"
        ):
            if len(args) >= 5:
                user_id, job_type, scheduled_for, context_json = args[:4]
            elif len(args) == 4:
                user_id, job_type, scheduled_for, context_json = args
            else:
                user_id, scheduled_for, context_json = args[:3]
                job_type = "watch_item_due"
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "job_type": job_type,
                "scheduled_for": scheduled_for,
                "context": _coerce_jsonb(context_json),
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 2,
                "delayed": False,
                "claimed_at": None,
                "claimed_by": None,
            }
            self.scheduled_jobs[row["id"]] = row
            return {"id": row["id"], "scheduled_for": scheduled_for}
        if compact.startswith("INSERT INTO scheduled_jobs") and (
            "'oob_review'" in compact
            or "VALUES ($1, $2, $3, $4::jsonb, 'pending'" in compact
            and args[1] == "oob_review"
        ):
            if len(args) >= 5:
                user_id, job_type, scheduled_for, context_json = args[:4]
            elif len(args) == 4:
                user_id, job_type, scheduled_for, context_json = args
            else:
                user_id, scheduled_for, context_json = args[:3]
                job_type = "oob_review"
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "job_type": job_type,
                "scheduled_for": scheduled_for,
                "context": _coerce_jsonb(context_json),
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 2,
                "delayed": False,
                "claimed_at": None,
                "claimed_by": None,
            }
            self.scheduled_jobs[row["id"]] = row
            return {"id": row["id"], "scheduled_for": scheduled_for}
        if compact.startswith("INSERT INTO scheduled_jobs"):
            user_id, scheduled_for, context_json = args[:3]
            bot_id = args[3] if len(args) > 3 else None
            topic_id = args[4] if len(args) > 4 else None
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "job_type": "checkin",
                "scheduled_for": scheduled_for,
                "context": _coerce_jsonb(context_json),
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 2,
                "delayed": False,
                "claimed_at": None,
                "claimed_by": None,
                "bot_id": bot_id,
                "topic_id": topic_id,
                "created_at": datetime.now(UTC),
            }
            self.scheduled_jobs[row["id"]] = row
            return {"job_id": row["id"], "scheduled_for": scheduled_for}
        if compact.startswith("UPDATE scheduled_jobs SET status='cancelled'"):
            if "job_type='scheduled_task'" in compact:
                user_id, target_job_id, target_task_id, reason = args
                for job in self.scheduled_jobs.values():
                    if (
                        job.get("user_id") == user_id
                        and job.get("job_type") == "scheduled_task"
                        and job.get("status") == "pending"
                        and (
                            (
                                target_job_id is not None
                                and str(job["id"]) == str(target_job_id)
                            )
                            or (
                                target_task_id is not None
                                and str(job.get("context", {}).get("task_id"))
                                == str(target_task_id)
                            )
                        )
                    ):
                        job["status"] = "cancelled"
                        job["cancellation_reason"] = reason
                        job["updated_at"] = datetime.now(UTC)
                        return {"job_id": job["id"], "context": job.get("context", {})}
                return None
            user_id = args[0]
            for job in self.scheduled_jobs.values():
                if (
                    job["user_id"] == user_id
                    and job["job_type"] == "checkin"
                    and job["status"] == "pending"
                ):
                    job["status"] = "cancelled"
                    return {"id": job["id"]}
            return None
        if compact.startswith("UPDATE scheduled_jobs SET scheduled_for=COALESCE") and "job_type='scheduled_task'" in compact:
            user_id, target_job_id, target_task_id, scheduled_for, context_patch = args
            patch = _coerce_jsonb(context_patch) or {}
            for job in self.scheduled_jobs.values():
                if (
                    job.get("user_id") == user_id
                    and job.get("job_type") == "scheduled_task"
                    and job.get("status") == "pending"
                    and (
                        (
                            target_job_id is not None
                            and str(job["id"]) == str(target_job_id)
                        )
                        or (
                            target_task_id is not None
                            and str(job.get("context", {}).get("task_id"))
                            == str(target_task_id)
                        )
                    )
                ):
                    if scheduled_for is not None:
                        job["scheduled_for"] = scheduled_for
                    job.setdefault("context", {}).update(patch)
                    job["updated_at"] = datetime.now(UTC)
                    return {
                        "job_id": job["id"],
                        "scheduled_for": job["scheduled_for"],
                        "context": job["context"],
                    }
            return None
        if (
            compact.startswith("UPDATE scheduled_jobs SET context=COALESCE")
            and "job_type='scheduled_task'" in compact
        ):
            user_id, target_job_id, context_patch = args
            patch = _coerce_jsonb(context_patch) or {}
            for job in self.scheduled_jobs.values():
                if (
                    job.get("user_id") == user_id
                    and job.get("job_type") == "scheduled_task"
                    and job.get("status") == "pending"
                    and str(job["id"]) == str(target_job_id)
                ):
                    job.setdefault("context", {}).update(patch)
                    job["updated_at"] = datetime.now(UTC)
                    return {"job_id": job["id"], "context": job["context"]}
            return None
        if compact.startswith("SELECT ( SELECT COUNT(*) FROM messages"):
            user_id = args[0]
            conversation_count = sum(
                1
                for message in self.messages.values()
                if message.get("deleted_at") is None
                and (
                    message.get("sender_id") == user_id
                    or message.get("recipient_id") == user_id
                )
            )
            ongoing_count = sum(
                1
                for theme in self.themes.values()
                if theme.get("status", "active") == "active"
            )
            ongoing_count += sum(
                1
                for item in self.watch_items.values()
                if item.get("owner_user_id") == user_id
                and item.get("status", "open") == "open"
            )
            return {
                "conversation_count": conversation_count,
                "ongoing_count": ongoing_count,
            }
        if (
            "FROM watch_items" in compact
            and "owner_user_id" in compact
            and "due_at" in compact
            and "WHERE id" in compact
        ):
            return self.watch_items.get(args[0])
        if compact.startswith("INSERT INTO feedback"):
            if len(args) >= 8:
                # log_feedback: from_user_id, target_type, target_id, sentiment, content, source, bot_id, topic_id
                (
                    from_user_id,
                    target_type,
                    target_id,
                    sentiment,
                    content,
                    source,
                    _bot_id,
                    _topic_id,
                ) = args[:8]
            elif len(args) == 6:
                # discord reaction: from_user_id, target_id, sentiment, content, bot_id, topic_id
                from_user_id, target_id, sentiment, content, _bot_id, _topic_id = args
                target_type = "message"
                source = "reaction"
            else:
                from_user_id, target_id, sentiment, content = args
                target_type = "message"
                source = "reaction"
            row = {
                "id": uuid4(),
                "from_user_id": from_user_id,
                "target_type": target_type,
                "target_id": target_id,
                "sentiment": sentiment,
                "content": content,
                "source": source,
                "created_at": datetime.now(UTC),
            }
            self.feedback[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("INSERT INTO withheld_outbound_reviews"):
            (
                recipient_id,
                sender_id,
                outbound_id,
                original_content,
                suggested_rewrite,
                reason,
                verdict,
                checker_failed,
                status,
            ) = args
            row = {
                "id": uuid4(),
                "recipient_id": recipient_id,
                "sender_id": sender_id,
                "outbound_id": outbound_id,
                "original_content": original_content,
                "suggested_rewrite": suggested_rewrite,
                "reason": reason,
                "verdict": verdict,
                "checker_failed": checker_failed,
                "status": status,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            self.withheld_outbound_reviews[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("INSERT INTO pacing_events"):
            (
                user_id,
                message_ids,
                source,
                decision,
                reason,
                signal_snapshot,
                preference_snapshot,
                wait_ms,
                reaction,
                llm_judgement,
            ) = args
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "message_ids": list(message_ids or []),
                "source": source,
                "decision": decision,
                "reason": reason,
                "signal_snapshot": _coerce_jsonb(signal_snapshot),
                "preference_snapshot": _coerce_jsonb(preference_snapshot),
                "wait_ms": wait_ms,
                "reaction": reaction,
                "llm_judgement": _coerce_jsonb(llm_judgement),
                "created_at": datetime.now(UTC),
            }
            self.pacing_events[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith(
            "SELECT id, headline, body, last_updated_at FROM topic_status WHERE topic_id"
        ):
            # S4: topic_status fetch. FakePool stores rows in self.topic_status keyed by (topic_id, dyad_id|user_id).
            topic_id = args[0]
            scope_id = args[1]
            row = self.topic_status.get((topic_id, scope_id))
            return dict(row) if row else None
        if compact.startswith(
            "SELECT id, topic_id FROM messages WHERE whatsapp_message_id"
        ):
            wa_id = args[0]
            bot_id = args[1] if len(args) > 1 else None
            for row in self.messages.values():
                if (
                    row.get("whatsapp_message_id") == wa_id
                    and row.get("direction") == "outbound"
                    and (
                        bot_id is None
                        or self._message_matches_bot_topic(
                            row,
                            bot_id,
                            (
                                row["topic_id"]
                                if "topic_id" in row
                                else UUID("00000000-0000-4000-8000-000000000001")
                            ),
                        )
                    )
                ):
                    return {"id": row["id"], "topic_id": row.get("topic_id")}
            return None
        if compact.startswith(
            "SELECT id, sender_id AS user_id, sender_id, bot_id, topic_id"
        ):
            row = self.messages.get(args[0])
            if row is None:
                return None
            return {
                "id": row["id"],
                "user_id": row.get("sender_id"),
                "sender_id": row.get("sender_id"),
                "bot_id": row.get("bot_id"),
                "topic_id": row.get("topic_id"),
                "channel_id": row.get("channel_id"),
                "binding_id": row.get("binding_id"),
                "dyad_id": row.get("dyad_id"),
            }
        if compact.startswith(
            "SELECT dm_other.dyad_id, dm_other.user_id AS partner_user_id"
        ):
            partner_user_id = self.dyad_partners.get(args[0])
            if partner_user_id is None:
                return None
            return {"dyad_id": uuid4(), "partner_user_id": partner_user_id}
        if compact.startswith("SELECT id, name, timezone FROM users WHERE id"):
            row = self.users.get(args[0])
            if row is None:
                return None
            return {
                "id": row["id"],
                "name": row.get("name"),
                "timezone": row.get("timezone"),
            }
        # ── Hector fitness: mediator.commitments ──────────────────────────────
        if compact.startswith("SELECT id FROM mediator.commitments WHERE id =") and "user_id = " in compact and "bot_id = " in compact:
            # log_event commitment existence check
            cid = UUID(args[0]) if not isinstance(args[0], UUID) else args[0]
            scope_user = args[1]
            scope_topic = args[2]
            scope_bot = args[3]
            row = self.commitments.get(cid)
            if row is not None and row["user_id"] == scope_user and row["topic_id"] == scope_topic and row["bot_id"] == scope_bot:
                return {"id": row["id"]}
            return None
        if compact.startswith("INSERT INTO mediator.commitments") and "RETURNING id, label, cadence, created_at" in compact:
            now = datetime.now(UTC)
            row_id = uuid4()
            row = {
                "id": row_id,
                "user_id": args[0],
                "topic_id": args[1],
                "bot_id": args[2],
                "label": args[3],
                "kind": args[4],
                "cadence": args[5],
                "days_of_week": list(args[6]) if args[6] else [],
                "target_count": args[7],
                "start_date": args[8],
                "end_date": args[9],
                "schedule_rule": args[10] if isinstance(args[10], dict) else {},
                "pressure_style": args[11],
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }
            self.commitments[row_id] = row
            return row
        if compact.startswith("UPDATE mediator.commitments SET") and "RETURNING id, updated_at" in compact and "status = " not in compact:
            # update_commitment (partial UPDATE, last 4 args are scope + id)
            scope_user = args[-4]
            scope_topic = args[-3]
            scope_bot = args[-2]
            commitment_id = UUID(args[-1]) if not isinstance(args[-1], UUID) else args[-1]
            row = self.commitments.get(commitment_id)
            if row is None or row["user_id"] != scope_user or row["topic_id"] != scope_topic or row["bot_id"] != scope_bot:
                return None
            set_values = args[:-4]
            set_start = compact.find("SET ") + 4
            set_end = compact.find(" WHERE ")
            set_clause = compact[set_start:set_end]
            parts = [p.strip() for p in set_clause.split(",")]
            for i, part in enumerate(parts):
                col = part.split(" = ")[0].strip()
                if col in ("label", "kind", "cadence", "pressure_style"):
                    row[col] = set_values[i]
                elif col == "days_of_week":
                    row[col] = list(set_values[i]) if set_values[i] else []
                elif col in ("target_count", "start_date", "end_date"):
                    row[col] = set_values[i]
                elif col == "schedule_rule":
                    row[col] = set_values[i] if isinstance(set_values[i], dict) else {}
            row["updated_at"] = datetime.now(UTC)
            return {"id": row["id"], "updated_at": row["updated_at"]}
        if compact.startswith("UPDATE mediator.commitments SET status = ") and "RETURNING id, status, updated_at" in compact:
            scope_user = args[0]
            scope_topic = args[1]
            scope_bot = args[2]
            commitment_id = UUID(args[3]) if not isinstance(args[3], UUID) else args[3]
            new_status = args[4]
            row = self.commitments.get(commitment_id)
            if row is None or row["user_id"] != scope_user or row["topic_id"] != scope_topic or row["bot_id"] != scope_bot:
                return None
            row["status"] = new_status
            row["updated_at"] = datetime.now(UTC)
            return {"id": row["id"], "status": row["status"], "updated_at": row["updated_at"]}
        # ── Hector fitness: mediator.events ──────────────────────────────────
        if compact.startswith("INSERT INTO mediator.events") and "RETURNING id, commitment_id, metric_key, adherence_status, observed_at" in compact:
            now = datetime.now(UTC)
            row_id = uuid4()
            row = {
                "id": row_id,
                "commitment_id": args[0],
                "user_id": args[1],
                "topic_id": args[2],
                "bot_id": args[3],
                "metric_key": args[4],
                "adherence_status": args[5],
                "value_numeric": args[6],
                "value_text": args[7],
                "unit": args[8],
                "observed_at": args[9],
                "note": args[10],
                "source_message_ids": list(args[11]) if args[11] else [],
                "created_at": now,
            }
            self.events[row_id] = row
            return row
        if compact.startswith(
            "UPDATE scheduled_jobs SET scheduled_for=COALESCE($5, scheduled_for),"
            " context=COALESCE(context, '{}'::jsonb) || $6::jsonb,"
            " updated_at=now() WHERE user_id=$1 AND id=$2 AND job_type='checkin'"
            " AND status='pending' AND bot_id=$3 AND topic_id=$4"
            " RETURNING id AS job_id, scheduled_for, context"
        ):
            user_id_arg, job_id_arg, bot_id_arg, topic_id_arg, scheduled_for, context_patch = args
            row = self.scheduled_jobs.get(job_id_arg)
            if (
                row is None
                or row.get("user_id") != user_id_arg
                or row.get("job_type") != "checkin"
                or row.get("status") != "pending"
                or row.get("bot_id") != bot_id_arg
                or row.get("topic_id") != topic_id_arg
            ):
                return None
            if scheduled_for is not None:
                row["scheduled_for"] = scheduled_for
            ctx = row.get("context") or {}
            if isinstance(context_patch, dict) and context_patch:
                ctx.update(context_patch)
            row["context"] = ctx
            row["updated_at"] = datetime.now(UTC)
            return {
                "job_id": row["id"],
                "scheduled_for": row["scheduled_for"],
                "context": row["context"],
            }
        raise AssertionError(f"unhandled fetchrow SQL: {compact}")

    async def fetchval(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact == "SELECT 1":
            return 1
        if compact.startswith("SELECT timezone FROM users WHERE id"):
            row = self.users.get(args[0])
            return row.get("timezone") if row else None
        if (
            compact.startswith("SELECT 1 FROM scheduled_jobs")
            and "job_type = 'scheduled_task'" in compact
            and "context->>'kind' = 'weekly_reflection'" in compact
        ):
            user_id = args[0]
            for job in self.scheduled_jobs.values():
                if (
                    job.get("user_id") == user_id
                    and job.get("job_type") == "scheduled_task"
                    and job.get("status") == "pending"
                    and (job.get("context") or {}).get("kind") == "weekly_reflection"
                ):
                    return 1
            return None
        if compact.startswith("SELECT MAX(sent_at) FROM messages WHERE id = ANY"):
            wanted = set(args[0] or [])
            sent = [m["sent_at"] for m in self.messages.values() if m["id"] in wanted]
            return max(sent) if sent else None
        if compact.startswith("SELECT MAX(sent_at) FROM messages"):
            user_id = args[0]
            sent = [
                m["sent_at"]
                for m in self.messages.values()
                if m["sender_id"] == user_id and m["direction"] == "inbound"
            ]
            return max(sent) if sent else None
        if compact.startswith("SELECT total_usd FROM llm_spend_log"):
            value = self.llm_spend_log.get(args[0], Decimal("0"))
            if isinstance(value, dict):
                return value.get("total_usd", Decimal("0"))
            return value
        if compact.startswith("SELECT warned_80_at FROM llm_spend_log"):
            value = self.llm_spend_log.get(args[0])
            if isinstance(value, dict):
                return value.get("warned_80_at")
            return None
        if compact.startswith("SELECT sender_id FROM messages WHERE id"):
            return self.messages[args[0]]["sender_id"]
        if compact.startswith(
            "SELECT EXISTS ( SELECT 1 FROM messages WHERE direction='inbound'"
        ):
            user_id, since, triggering_message_ids, *rest = args
            bot_id_filter = rest[0] if rest else None
            triggering = set(triggering_message_ids or [])
            return any(
                row.get("direction") == "inbound"
                and row.get("sender_id") == user_id
                and row.get("sent_at") > since
                and row["id"] not in triggering
                and (
                    bot_id_filter is None
                    or self._message_matches_bot_topic(
                        row,
                        bot_id_filter,
                        (
                            row["topic_id"]
                            if "topic_id" in row
                            else UUID("00000000-0000-4000-8000-000000000001")
                        ),
                    )
                )
                for row in self.messages.values()
            )
        if compact.startswith(
            "SELECT m.whatsapp_message_id FROM messages m JOIN user_identities ui ON ui.user_id = m.sender_id"
        ):
            identifier = args[0]
            rows = [
                message
                for message in self.messages.values()
                if message.get("direction") == "inbound"
                and message.get("whatsapp_message_id") is not None
                # In test FakePool, user_identities rows mirror users.phone,
                # so fall through to matching the identifier against users.
                and self.users.get(message.get("sender_id"), {}).get("phone")
                == identifier
            ]
            if not rows:
                return None
            return max(rows, key=lambda row: row["sent_at"])["whatsapp_message_id"]
        if compact.startswith(
            "SELECT m.whatsapp_message_id FROM messages m JOIN users u ON u.id = m.sender_id"
        ):
            phone = args[0]
            rows = [
                message
                for message in self.messages.values()
                if message.get("direction") == "inbound"
                and message.get("whatsapp_message_id") is not None
                and self.users.get(message.get("sender_id"), {}).get("phone") == phone
            ]
            if not rows:
                return None
            return max(rows, key=lambda row: row["sent_at"])["whatsapp_message_id"]
        if (
            "SELECT owner_user_id" in compact
            and "FROM watch_items" in compact
            and "WHERE" in compact
        ):
            row = self.watch_items.get(args[0])
            return row.get("owner_user_id") if row else None
        if (
            "SELECT owner_id" in compact
            and "FROM out_of_bounds" in compact
            and "WHERE" in compact
        ):
            row = self.out_of_bounds.get(args[0])
            return row.get("owner_id") if row else None
        if compact.startswith("SELECT about_user_id FROM memories WHERE id"):
            row = self.memories.get(args[0])
            return row.get("about_user_id") if row else None
        if compact.startswith("SELECT about_user_id FROM observations WHERE id"):
            row = self.observations.get(args[0])
            return row.get("about_user_id") if row else None
        if compact.startswith("SELECT phone FROM users WHERE id"):
            # Used by app.services.user_identity.resolve_user_address fallback.
            row = self.users.get(args[0])
            return row.get("phone") if row else None
        if compact.startswith("SELECT id FROM messages WHERE whatsapp_message_id"):
            wa_id = args[0]
            for row in self.messages.values():
                if (
                    row.get("whatsapp_message_id") == wa_id
                    and row.get("direction") == "outbound"
                ):
                    return row["id"]
            return None
        if compact.startswith(
            "SELECT paused_at FROM system_state WHERE key = 'global_pause'"
        ):
            return self.system_state["global_pause"].get("paused_at")
        if compact.startswith(
            "SELECT value FROM system_state WHERE key = 'recovery_v2_kill'"
        ):
            return self.system_state.get("recovery_v2_kill", {}).get("value")
        if compact.startswith("SELECT paused FROM user_bot_state WHERE user_id"):
            # S2a: per-(user,bot) pause read path — always not paused in tests
            return None
        if compact.startswith("SELECT partner_share FROM user_bot_state WHERE user_id"):
            row = self.user_bot_state.get((args[0], args[1]), {})
            return row.get("partner_share")
        if compact.startswith(
            "SELECT count(*) FROM scheduled_jobs WHERE bot_id=$1 AND job_type='scheduled_task' AND context->>'kind'='partner_nudge' AND context->>'originating_user_id'=$2 AND created_at > now() - interval '24 hours'"
        ):
            bot_id, originating_user_id = args
            cutoff = datetime.now(UTC) - timedelta(hours=24)
            count = 0
            for row in self.scheduled_jobs.values():
                if (
                    row.get("job_type") == "scheduled_task"
                    and row.get("bot_id") == bot_id
                    and (row.get("context") or {}).get("kind") == "partner_nudge"
                    and str((row.get("context") or {}).get("originating_user_id"))
                    == str(originating_user_id)
                ):
                    created_at = row.get("created_at")
                    if created_at is None or created_at > cutoff:
                        count += 1
            return count
        if compact.startswith("SELECT COALESCE(reasoning, '') FROM bot_turns WHERE id"):
            return self.bot_turns[args[0]].get("reasoning") or ""
        if compact.startswith("SELECT 1 FROM distillations d JOIN artifact_topics"):
            row_id = args[0]
            topic_id = args[1]
            if row_id in self.distillations:
                d_row = self.distillations[row_id]
                if d_row.get("status") == "active" and self._row_matches_topic(
                    "distillations", row_id, topic_id
                ):
                    return 1
            return None
        # ── 0049: recovery crashed-turn passive release CTE ─────────────
        # WITH released AS (UPDATE messages …), turn_done AS (UPDATE bot_turns …)
        # SELECT count(*) FROM released
        if "WITH released AS ( UPDATE messages" in compact and "turn_done AS ( UPDATE bot_turns" in compact:
            turn_id = args[0]
            count = 0
            for msg in self.messages.values():
                if (msg.get("bot_turn_id") == turn_id
                        and msg.get("processing_state") in ("processing", "deferred")):
                    msg["processing_state"] = "raw"
                    msg["bot_turn_id"] = None
                    msg["processing_started_at"] = None
                    count += 1
            if turn_id in self.bot_turns:
                turn = self.bot_turns[turn_id]
                if turn.get("completed_at") is None:
                    turn["completed_at"] = datetime.now(UTC)
                if turn.get("failure_reason") is None:
                    turn["failure_reason"] = "crashed"
            return count
        raise AssertionError(f"unhandled fetchval SQL: {compact}")

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        self.fetch_sqls.append(compact)
        if (
            compact.startswith("SELECT m.message_id,")
            and "FROM mediator.v_searchable_messages m" in compact
        ):
            return self._project_searchable_rows(compact, *args)
        if "WITH ranked_ids AS" in compact and "JOIN mediator.v_searchable_messages m" in compact:
            return self._project_searchable_rows(compact, *args)
        if "WITH ranked_sources AS" in compact and "JOIN mediator.v_searchable_content sc" in compact:
            source_types = args[-2]
            source_ids = args[-1]
            bot_id = args[0] if args and isinstance(args[0], str) else None
            topic_id = args[1] if "sc.topic_id = $" in compact and len(args) > 1 else None
            thread_owner_user_id = (
                args[1]
                if "sc.thread_owner_user_id = $" in compact and len(args) > 1
                else None
            )
            dyad_id = (
                args[2]
                if "(sc.dyad_id = $" in compact and len(args) > 2 and isinstance(args[2], UUID)
                else None
            )
            rows = []
            for source_type, source_id in zip(source_types, source_ids, strict=True):
                searchable = self._searchable_content_row(source_type, source_id)
                if searchable is None or searchable.get("source_type") == "message":
                    continue
                if not self._match_searchable_scope(
                    searchable,
                    bot_id=bot_id,
                    viewer_id=None,
                    participants=set(),
                    topic_id=topic_id,
                    thread_owner_user_id=thread_owner_user_id,
                    dyad_id=dyad_id,
                ):
                    continue
                rows.append(
                    {
                        "source_type": searchable["source_type"],
                        "source_id": searchable["source_id"],
                        "message_id": searchable["message_id"],
                        "sender_id": searchable.get("sender_id"),
                        "recipient_id": searchable.get("recipient_id"),
                        "thread_owner_user_id": searchable.get("thread_owner_user_id"),
                        "thread_owner_partner_share": searchable.get(
                            "thread_owner_partner_share"
                        ),
                        "bot_id": searchable.get("bot_id"),
                        "topic_id": searchable.get("topic_id"),
                        "dyad_id": searchable.get("dyad_id"),
                        "sent_at": searchable.get("sent_at"),
                        "source_created_at": searchable.get("source_created_at")
                        or searchable.get("sent_at"),
                        "source_updated_at": searchable.get("source_updated_at")
                        or searchable.get("sent_at"),
                        "sort_at": searchable.get("sort_at")
                        or searchable.get("sent_at"),
                        "content": searchable.get("canonical_text"),
                    }
                )
            return rows
        if "FROM mediator.memories" in compact and "source_id" in compact:
            source_ids = set(args[0])
            return [
                {
                    "source_type": "memory",
                    "source_id": row["id"],
                    "status": row.get("status", "active"),
                    "visibility": row.get("visibility", "private"),
                    "bot_id": row.get("recorded_by_bot_id", "mediator"),
                    "content": row.get("content"),
                }
                for row in self.memories.values()
                if row.get("id") in source_ids
            ]
        if "FROM mediator.observations" in compact and "source_id" in compact:
            source_ids = set(args[0])
            return [
                {
                    "source_type": "observation",
                    "source_id": row["id"],
                    "status": row.get("status", "active"),
                    "bot_id": row.get("recorded_by_bot_id", "mediator"),
                    "supporting_message_ids": row.get("supporting_message_ids", []),
                    "content": row.get("content"),
                }
                for row in self.observations.values()
                if row.get("id") in source_ids
            ]
        if "FROM mediator.distillations" in compact and "source_id" in compact:
            source_ids = set(args[0])
            return [
                {
                    "source_type": "distillation",
                    "source_id": row["id"],
                    "status": row.get("status", "active"),
                    "visibility": row.get("visibility", "private"),
                    "bot_id": row.get("recorded_by_bot_id", "mediator"),
                    "supporting_message_ids": row.get("supporting_message_ids", []),
                    "content": row.get("content"),
                }
                for row in self.distillations.values()
                if row.get("id") in source_ids
            ]
        if "FROM mediator.conversation_artifacts" in compact and "source_id" in compact:
            source_ids = set(args[0])
            return [
                {
                    "source_type": "artifact",
                    "source_id": row["id"],
                    "bot_id": row.get("bot_id", "mediator"),
                    "artifact_type": row.get("artifact_type"),
                    "deleted_at": row.get("deleted_at"),
                }
                for row in getattr(self, "conversation_artifacts", {}).values()
                if row.get("id") in source_ids
            ]
        if (
            "FROM mediator.v_searchable_messages m" in compact
            or "JOIN mediator.v_searchable_messages m" in compact
            or "FROM mediator.v_searchable_content sc" in compact
            or "JOIN mediator.v_searchable_content sc" in compact
        ):
            if "SELECT m.message_id AS id," in compact:
                rows = []
                query_text = next(
                    (
                        arg
                        for arg in args
                        if isinstance(arg, str) and "%" in arg
                    ),
                    "",
                )
                query_terms = [
                    term.casefold()
                    for term in query_text.replace("%", " ").replace('"', " ").split()
                    if term.strip()
                ]
                bot_id = next(
                    (
                        arg
                        for arg in args
                        if isinstance(arg, str) and "%" not in arg
                    ),
                    None,
                )
                uuid_values = [arg for arg in args if isinstance(arg, UUID)]
                uuid_lists = [
                    list(arg)
                    for arg in args
                    if isinstance(arg, list) and all(isinstance(item, UUID) for item in arg)
                ]
                viewer_id = uuid_values[0] if uuid_values else None
                topic_id = uuid_values[1] if len(uuid_values) > 1 else None
                thread_owner = None
                dyad_id = None
                has_explicit_thread_owner = compact.count("m.thread_owner_user_id = $") > 1
                has_dyad_filter = "m.dyad_id = $" in compact
                if has_explicit_thread_owner and has_dyad_filter:
                    thread_owner = uuid_values[2] if len(uuid_values) > 2 else None
                    dyad_id = uuid_values[3] if len(uuid_values) > 3 else None
                elif has_explicit_thread_owner:
                    thread_owner = uuid_values[2] if len(uuid_values) > 2 else None
                elif has_dyad_filter:
                    dyad_id = uuid_values[2] if len(uuid_values) > 2 else None
                participants = set(uuid_lists[0]) if uuid_lists else set()
                datetimes = [arg for arg in args if isinstance(arg, datetime)]
                lower_bound = datetimes[0] if datetimes else None
                upper_bound = datetimes[1] if len(datetimes) > 1 else None

                for message in self.messages.values():
                    searchable = self._searchable_message_row(message)
                    if searchable is None:
                        continue
                    if bot_id is not None and searchable.get("bot_id") != bot_id:
                        continue
                    if viewer_id is not None and searchable.get("thread_owner_user_id") != viewer_id:
                        if searchable.get("thread_owner_partner_share") != "opt_in":
                            continue
                    if participants:
                        if searchable.get("thread_owner_user_id") not in participants:
                            continue
                        if (
                            searchable.get("sender_id") not in participants
                            and searchable.get("recipient_id") not in participants
                        ):
                            continue
                    if topic_id is not None and searchable.get("topic_id") != topic_id:
                        continue
                    if thread_owner is not None and searchable.get("thread_owner_user_id") != thread_owner:
                        continue
                    if dyad_id is not None and searchable.get("dyad_id") not in (None, dyad_id):
                        continue
                    if searchable.get("active_oob_severity") in {"firm", "hard"}:
                        continue
                    sent_at = searchable.get("sent_at")
                    if lower_bound is not None and sent_at is not None and sent_at < lower_bound:
                        continue
                    if upper_bound is not None and sent_at is not None and sent_at > upper_bound:
                        continue
                    canonical_text = searchable["canonical_text"].casefold()
                    if query_terms and not any(term in canonical_text for term in query_terms):
                        continue
                    rows.append(
                        {
                            "id": searchable["message_id"],
                            "sender_id": searchable.get("sender_id"),
                            "recipient_id": searchable.get("recipient_id"),
                            "sent_at": searchable.get("sent_at"),
                            "content": message.get("content"),
                            "media_type": message.get("media_type"),
                            "media_analysis": message.get("media_analysis"),
                            "bot_id": searchable.get("bot_id"),
                            "topic_id": searchable.get("topic_id"),
                            "charge": message.get("charge", "routine") or "routine",
                            "direction": message.get("direction"),
                        }
                    )
                rows.sort(
                    key=lambda row: (
                        row["sent_at"] or datetime.min.replace(tzinfo=UTC),
                        row["id"],
                    ),
                    reverse=True,
                )
                limit = next((arg for arg in reversed(args) if isinstance(arg, int)), len(rows))
                return rows[:limit]
            rows = []
            is_semantic = (
                "FROM mediator.content_embeddings e" in compact
                or "FROM mediator.message_embeddings e" in compact
            )
            if is_semantic:
                query_terms = []
            else:
                query_text = next((arg for arg in args if isinstance(arg, str)), "")
                query_terms = [
                    term.casefold()
                    for term in query_text.replace('"', " ").split()
                    if term.strip()
                ]
            semantic_searchables: list[tuple[dict[str, Any], dict[str, Any]]] = []
            if is_semantic:
                for embedding in self.message_embeddings.values():
                    source_type = embedding.get("source_type", "message")
                    source_id = embedding.get("source_id") or embedding.get("message_id")
                    if source_id is None:
                        continue
                    searchable = self._searchable_content_row(source_type, source_id)
                    if searchable is None:
                        continue
                    semantic_searchables.append((searchable, embedding))

            iterable = (
                [(searchable, None) for searchable, _embedding in semantic_searchables]
                if is_semantic
                else [(searchable, None) for searchable in self._all_searchable_content_rows()]
            )
            for searchable, message in iterable:
                if searchable is None:
                    continue
                if not self._row_matches_retrieval_visibility(searchable, args):
                    continue
                if is_semantic:
                    embedding = next(
                        stored
                        for candidate, stored in semantic_searchables
                        if candidate is searchable
                    )
                    model_args = [arg for arg in args if isinstance(arg, str)]
                    int_args = [arg for arg in args if isinstance(arg, int)]
                    model = model_args[0] if model_args else embedding.get("model")
                    dimension = int_args[0] if int_args else embedding.get("dimension")
                    if embedding.get("model") != model or embedding.get("dimension") != dimension:
                        continue
                    rows.append(
                        {
                            "source_type": searchable["source_type"],
                            "source_id": searchable["source_id"],
                            "message_id": searchable["message_id"],
                            "sent_at": searchable["sent_at"],
                            "cosine_distance": embedding.get("cosine_distance", 0.0),
                            "semantic_rank": 0,
                        }
                    )
                else:
                    canonical_text = searchable["canonical_text"].casefold()
                    if query_terms and not any(term in canonical_text for term in query_terms):
                        continue
                    rows.append(
                        {
                            "source_type": searchable["source_type"],
                            "source_id": searchable["source_id"],
                            "message_id": searchable["message_id"],
                            "sent_at": searchable["sent_at"],
                            "keyword_score": searchable.get("keyword_score", 1.0),
                            "keyword_rank": 0,
                        }
                    )
            if is_semantic:
                rows.sort(
                    key=lambda row: (
                        row["cosine_distance"],
                        row["sent_at"] or datetime.min.replace(tzinfo=UTC),
                        str(row["source_type"]),
                        str(row["source_id"]),
                    ),
                    reverse=False,
                )
            else:
                rows.sort(
                    key=lambda row: (
                        -float(row["keyword_score"]),
                        row["sent_at"] or datetime.min.replace(tzinfo=UTC),
                        str(row["source_type"]),
                        str(row["source_id"]),
                    ),
                    reverse=True,
                )
            for index, row in enumerate(rows, start=1):
                if is_semantic:
                    row["semantic_rank"] = index
                else:
                    row["keyword_rank"] = index
            limit = next((arg for arg in reversed(args) if isinstance(arg, int)), len(rows))
            return rows[:limit]
        if "FROM mediator.message_embeddings e" in compact or "FROM mediator.content_embeddings e" in compact:
            # Keep vector-specific ANN behavior explicit: shared FakePool only
            # supports the production ANN shape joined through the searchable view.
            raise AssertionError(
                "FakePool semantic retrieval must join mediator.v_searchable_content"
            )
        if "FROM mediator.embed_jobs" in compact and "FOR UPDATE SKIP LOCKED" in compact:
            now, limit, worker_id = args
            due = [
                job
                for job in self.embed_jobs.values()
                if job["status"] == "pending" and job["next_attempt_at"] <= now
            ]
            due.sort(key=lambda row: (row["next_attempt_at"], row["created_at"], str(row["id"])))
            rows = []
            for job in due[:limit]:
                job.update(
                    status="processing",
                    attempts=job.get("attempts", 0) + 1,
                    locked_at=now,
                    locked_by=worker_id,
                    updated_at=now,
                )
                rows.append(
                    {
                        "id": job["id"],
                        "source_type": job.get("source_type", "message"),
                        "source_id": job.get("source_id", job["message_id"]),
                        "message_id": job["message_id"],
                        "job_kind": job["job_kind"],
                        "model": job["model"],
                        "dimension": job["dimension"],
                        "content_hash": job["content_hash"],
                        "attempts": job["attempts"],
                        "locked_by": job["locked_by"],
                    }
                )
            return rows
        # ── 0041/0049: claim_messages_for_turn CTE ───────────────────────
        # 0049 adds new_bot_turn_id parameter (args[3]).
        # Mirror the real CTE WHERE clause:
        #   AND (bot_turn_id IS NULL OR $4::uuid IS NULL OR bot_turn_id = $4::uuid)
        # COALESCE($4::uuid, bot_turn_id) sets the in-flight owner when provided.
        # Trigger-equivalent enforcement lives here (claim CTE path only);
        # bare-UPDATE bot_turn_id paths are not predicate-checked by FakePool.
        if "WITH claimed AS ( UPDATE messages" in compact and "RETURNING id ) SELECT id FROM claimed" in compact:
            message_ids = set(args[0])
            bot_id_arg = args[1]
            topic_id_arg = args[2]
            new_bot_turn_id = args[3] if len(args) > 3 else None
            claimed = []
            for msg_id, msg in list(self.messages.items()):
                if msg_id not in message_ids:
                    continue
                if msg.get("direction") != "inbound":
                    continue
                if msg.get("processing_state") not in ("raw", "deferred"):
                    continue
                ps = msg.get("processing_started_at")
                if ps is not None and ps >= datetime.now(UTC) - timedelta(minutes=5):
                    continue
                # Backward-compat: legacy messages without bot_id/topic_id
                # default to mediator/relationship (matches _message_matches_bot_topic).
                row_bid = msg["bot_id"] if "bot_id" in msg else "mediator"
                row_tid = msg["topic_id"] if "topic_id" in msg else UUID("00000000-0000-4000-8000-000000000001")
                if row_bid != bot_id_arg:
                    continue
                if row_tid != topic_id_arg:
                    continue
                # ── 0049 bot_turn_id constraint ─────────────────────────
                existing_bt = msg.get("bot_turn_id")
                if existing_bt is not None and new_bot_turn_id is not None and existing_bt != new_bot_turn_id:
                    # WHERE clause blocks: different in-flight owner
                    continue
                msg["processing_state"] = "processing"
                msg["processing_started_at"] = datetime.now(UTC)
                msg["processing_attempts"] = msg.get("processing_attempts", 0) + 1
                msg["processing_error"] = None
                # Recovery-v2: claim NULLs the retry pointer via writer marker.
                msg["next_retry_at"] = None
                # 0049: set in-flight owner (COALESCE behaviour)
                if new_bot_turn_id is not None:
                    msg["bot_turn_id"] = new_bot_turn_id
                claimed.append({"id": msg_id})
            return claimed
        # ── 0041: _recovery_scopes query ─────────────────────────────────
        if ("SELECT DISTINCT bot_id, topic_id FROM messages"
                in compact and "direction = 'inbound'" in compact):
            rows = []
            seen = set()
            for msg in self.messages.values():
                if msg.get("direction") != "inbound":
                    continue
                if msg.get("processing_state") not in ("raw", "processing", "failed"):
                    continue
                bid = msg.get("bot_id")
                tid = msg.get("topic_id")
                if bid is None or tid is None:
                    continue
                key = (bid, tid)
                if key not in seen:
                    seen.add(key)
                    rows.append({"bot_id": bid, "topic_id": tid})
            return rows
        if compact.startswith("SELECT m.id, m.sender_id AS user_id, m.bot_id, m.topic_id FROM messages m"):
            max_retries = args[0] if args else 3
            rows = []
            for msg in self.messages.values():
                if msg.get("direction") != "inbound":
                    continue
                if msg.get("processing_state") != "raw":
                    continue
                sent_at = msg.get("sent_at")
                if sent_at is not None and sent_at >= datetime.now(UTC) - timedelta(seconds=30):
                    continue
                # Recovery-v2: respect next_retry_at gate and terminal classes.
                fclass = msg.get("failure_class")
                if fclass in ("terminal_post_send", "infra_bug"):
                    continue
                nra = msg.get("next_retry_at")
                if nra is not None and nra > datetime.now(UTC):
                    continue
                has_prior_turn = any(
                    msg["id"] in (turn.get("triggering_message_ids") or [])
                    for turn in self.bot_turns.values()
                )
                retryable_failed_raw = (
                    msg.get("processing_attempts", 0) < max_retries
                    and (
                        msg.get("handling_result") == "failed"
                        or any(
                            msg["id"] in (turn.get("triggering_message_ids") or [])
                            and turn.get("failure_reason") is not None
                            and turn.get("final_output_message_id") is None
                            and msg.get("processing_attempts", 0) > 0
                            for turn in self.bot_turns.values()
                        )
                    )
                )
                if has_prior_turn and not retryable_failed_raw:
                    continue
                rows.append(
                    {
                        "id": msg["id"],
                        "user_id": msg.get("sender_id"),
                        "bot_id": msg.get("bot_id"),
                        "topic_id": msg.get("topic_id"),
                    }
                )
            return rows
        if (
            compact.startswith("SELECT id FROM messages WHERE id = ANY")
            and "sender_id=$2 OR recipient_id=$2" in compact
        ):
            wanted = set(args[0])
            source_user_id = args[1]
            bot_filter = args[2] if len(args) > 2 else None
            topic_filter = args[3] if len(args) > 3 else None
            return [
                {"id": row["id"]}
                for row in self.messages.values()
                if row["id"] in wanted
                and row.get("deleted_at") is None
                and (
                    row.get("sender_id") == source_user_id
                    or row.get("recipient_id") == source_user_id
                )
                and (
                    "bot_id=$3" not in compact
                    or "topic_id=$4" not in compact
                    or self._message_matches_bot_topic(row, bot_filter, topic_filter)
                )
            ]
        if compact.startswith("SELECT id FROM messages WHERE id = ANY"):
            wanted = set(args[0])
            has_bot_scope_23 = "bot_id = $2" in compact and "topic_id = $3" in compact
            has_bot_scope_34 = "bot_id=$3" in compact and "topic_id=$4" in compact
            if has_bot_scope_23:
                bot_filter = args[1] if len(args) > 1 else None
                topic_filter = args[2] if len(args) > 2 else None
            elif has_bot_scope_34:
                bot_filter = args[2] if len(args) > 2 else None
                topic_filter = args[3] if len(args) > 3 else None
            else:
                bot_filter = None
                topic_filter = None
            return [
                {"id": row["id"]}
                for row in self.messages.values()
                if row["id"] in wanted
                and (
                    not (has_bot_scope_23 or has_bot_scope_34)
                    or self._message_matches_bot_topic(row, bot_filter, topic_filter)
                )
            ]
        if (
            "FROM themes" in compact
            and "WHERE id = ANY" in compact
            and "SELECT id" in compact[:25]
        ):
            wanted = set(args[0])
            return [
                {"id": row["id"]} for row in self.themes.values() if row["id"] in wanted
            ]
        if (
            "FROM observations" in compact
            and "WHERE id = ANY" in compact
            and "SELECT id" in compact[:25]
        ):
            wanted = set(args[0])
            return [
                {"id": row["id"]}
                for row in self.observations.values()
                if row["id"] in wanted
            ]
        if (
            "FROM memories" in compact
            and "WHERE id = ANY" in compact
            and "SELECT id" in compact[:25]
        ):
            wanted = set(args[0])
            return [
                {"id": row["id"]}
                for row in self.memories.values()
                if row["id"] in wanted
            ]
        if compact.startswith(
            "SELECT id, name, phone, timezone FROM users WHERE id <>"
        ):
            return [row for user_id, row in self.users.items() if user_id != args[0]]
        if compact.startswith(
            "SELECT id, name, phone, timezone, onboarding_state, pacing_preferences, pregnancy_edd, pregnancy_dating_basis, pregnancy_lmp_date, pregnancy_scan_date, pregnancy_scan_corrected_at, pregnancy_started_at, pregnancy_ended_at, pregnancy_outcome FROM users WHERE id <>"
        ):
            return [row for user_id, row in self.users.items() if user_id != args[0]]
        if compact.startswith("SELECT bot_id, address FROM channels WHERE transport"):
            if self._raise_undefined_table_on_channels:
                self._raise_undefined_table_on_channels = False
                raise _UndefinedTableError('relation "channels" does not exist')
            transport_val = args[0] if args else "discord"
            return [
                {"bot_id": row["bot_id"], "address": row["address"]}
                for (_t, addr), row in self.channels.items()
                if _t == transport_val
            ]
        if compact.startswith(
            "SELECT transport, address FROM user_identities WHERE user_id"
        ):
            uid = args[0]
            return [
                {"transport": transport, "address": address}
                for (transport, address), owner in self.user_identities.items()
                if owner == uid
            ]
        if compact.startswith("SELECT id, timezone FROM users"):
            return [
                {"id": row["id"], "timezone": row.get("timezone", "UTC")}
                for row in self.users.values()
            ]
        if compact.startswith("SELECT u.id AS user_id"):
            start, end, *rest = args
            has_bot_scope = "m.bot_id = $3" in compact and "m.topic_id = $4" in compact
            if has_bot_scope:
                bot_filter = rest[0] if len(rest) > 0 else None
                topic_filter = rest[1] if len(rest) > 1 else None
                allowed_users = set(rest[2]) if len(rest) > 2 else set(self.users)
            else:
                bot_filter = None
                topic_filter = None
                allowed_users = set(rest[0]) if rest else set(self.users)
            rows = []
            for user in self.users.values():
                if user["id"] not in allowed_users:
                    continue
                messages = [
                    message
                    for message in self.messages.values()
                    if message.get("deleted_at") is None
                    and start <= message["sent_at"] <= end
                    and (
                        message.get("sender_id") == user["id"]
                        or message.get("recipient_id") == user["id"]
                    )
                    and (
                        not has_bot_scope
                        or self._message_matches_bot_topic(
                            message, bot_filter, topic_filter
                        )
                    )
                ]
                latest = (
                    max(messages, key=lambda message: message["sent_at"])
                    if messages
                    else None
                )
                rows.append(
                    {
                        "user_id": user["id"],
                        "user_name": user["name"],
                        "message_count": len(messages),
                        "last_message_at": latest["sent_at"] if latest else None,
                        "latest_content": latest["content"] if latest else None,
                    }
                )
            rows.sort(
                key=lambda row: (
                    row["last_message_at"] is None,
                    row["last_message_at"] or datetime.min.replace(tzinfo=UTC),
                    row["user_name"],
                )
            )
            return rows
        if (
            "FROM bridge_candidates" in compact
            and "WHERE target_user_id=$1 AND source_user_id=$2" in compact
        ):
            target_user_id, source_user_id = args
            rows = [
                {
                    **dict(row),
                    "partner_path": row.get("partner_path", "message_partner"),
                }
                for row in self.bridge_candidates.values()
                if row["target_user_id"] == target_user_id
                and row["source_user_id"] == source_user_id
                and row["status"] == "ready"
                and row.get("partner_path", "message_partner") == "message_partner"
            ]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return rows[:5]
        if "FROM bridge_candidates" in compact:
            if "partner_path" in compact:
                (
                    user_id,
                    partner_id,
                    source_filter,
                    target_filter,
                    status_filter,
                    partner_path_filter,
                    limit,
                ) = args
            else:
                (
                    user_id,
                    partner_id,
                    source_filter,
                    target_filter,
                    status_filter,
                    limit,
                ) = args
                partner_path_filter = None
            rows = [
                {
                    **dict(row),
                    "partner_path": row.get("partner_path", "message_partner"),
                }
                for row in self.bridge_candidates.values()
                if {row["source_user_id"], row["target_user_id"]}
                == {user_id, partner_id}
                and (source_filter is None or row["source_user_id"] == source_filter)
                and (target_filter is None or row["target_user_id"] == target_filter)
                and (status_filter is None or row["status"] == status_filter)
                and (
                    partner_path_filter is None
                    or row.get("partner_path", "message_partner") == partner_path_filter
                )
            ]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return rows[:limit]
        # JOIN artifact_topics filter is honored at the FakePool level via self.artifact_topics; matchers below ignore the JOIN clause.
        if "FROM out_of_bounds" in compact:
            owner_filter = None
            if "owner_id = ANY" in compact:
                owner_filter = set(args[0])
            elif "owner_id =" in compact:
                owner_filter = {args[0]}
            return [
                {
                    "id": row["id"],
                    "owner_id": row["owner_id"],
                    "sensitive_core": row["sensitive_core"],
                    "shareable_context": row["shareable_context"],
                    "severity": row["severity"],
                    "review_at": row.get("review_at"),
                    "status": row.get("status", "active"),
                    "created_at": row.get("created_at", datetime.now(UTC)),
                }
                for row in self.out_of_bounds.values()
                if row.get("status", "active") == "active"
                and (owner_filter is None or row["owner_id"] in owner_filter)
            ]
        if "WITH partner_rows AS" in compact:
            owner_user_id = args[0]
            limit = args[1]
            current_bot_id = args[2]
            rows = []
            for row in self.memories.values():
                if (
                    row.get("status", "active") == "active"
                    and row.get("visibility") == "dyad_shareable"
                    and row.get("shareable_summary")
                    and row.get("about_user_id") == owner_user_id
                    and row.get("recorded_by_bot_id") is not None
                    and row.get("recorded_by_bot_id") != current_bot_id
                    and self.user_bot_state.get(
                        (owner_user_id, row.get("recorded_by_bot_id")), {}
                    ).get("partner_share")
                    == "opt_in"
                ):
                    rows.append(
                        {
                            "kind": "memory",
                            "id": row["id"],
                            "bot_id": row.get("recorded_by_bot_id"),
                            "shareable_summary": row.get("shareable_summary"),
                            "occurred_at": row.get("last_referenced_at")
                            or row.get("created_at", datetime.now(UTC)),
                        }
                    )
            for row in self.distillations.values():
                bot_id = row.get("recorded_by_bot_id") or self.messages.get(
                    row.get("triggering_message_id"), {}
                ).get("bot_id")
                if (
                    row.get("status", "active") == "active"
                    and row.get("visibility") == "dyad_shareable"
                    and row.get("shareable_summary")
                    and owner_user_id in row.get("source_user_ids", [])
                    and bot_id is not None
                    and bot_id != current_bot_id
                    and self.user_bot_state.get((owner_user_id, bot_id), {}).get(
                        "partner_share"
                    )
                    == "opt_in"
                ):
                    rows.append(
                        {
                            "kind": "distillation",
                            "id": row["id"],
                            "bot_id": bot_id,
                            "shareable_summary": row.get("shareable_summary"),
                            "occurred_at": row.get("updated_at")
                            or row.get("created_at", datetime.now(UTC)),
                        }
                    )
            rows.sort(key=lambda item: item["occurred_at"], reverse=True)
            return rows[:limit]
        # JOIN artifact_topics filter is honored at the FakePool level via self.artifact_topics; matchers below ignore the JOIN clause.
        if "FROM memories" in compact:
            if "m.about_user_id = $1" in compact and args:
                owner_filter = {args[0]}
            elif "m.about_user_id = $2" in compact and len(args) > 1:
                owner_filter = {args[1]}
            elif args and isinstance(args[0], (list, tuple, set)):
                owner_filter = set(args[0])
            elif "m.status = $1" in compact:
                owner_filter = None
            else:
                owner_filter = {args[0]} if args else None
            return [
                {
                    "id": row["id"],
                    "about_user_id": row.get("about_user_id"),
                    "content": row.get("content", ""),
                    "status": row.get("status", "active"),
                    "visibility": row.get("visibility", "private"),
                    "shareable_summary": row.get("shareable_summary"),
                    "recorded_by_bot_id": row.get("recorded_by_bot_id"),
                    "related_theme_ids": row.get("related_theme_ids", []),
                    "created_at": row.get("created_at", datetime.now(UTC)),
                    "last_referenced_at": row.get("last_referenced_at"),
                }
                for row in self.memories.values()
                if row.get("status", "active") == "active"
                and (
                    owner_filter is None
                    or row.get("about_user_id") in owner_filter
                    or row.get("about_user_id") is None
                )
            ]
        if "FROM themes" in compact:
            return list(self.themes.values())[:10]
        # JOIN artifact_topics filter is honored at the FakePool level via self.artifact_topics; matchers below ignore the JOIN clause.
        if "FROM watch_items" in compact:
            return [
                {
                    "id": row["id"],
                    "owner_user_id": row["owner_user_id"],
                    "content": row["content"],
                    "due_at": row.get("due_at"),
                    "status": row.get("status", "open"),
                    "addressing_note": row.get("addressing_note"),
                    "created_at": row.get("created_at", datetime.now(UTC)),
                    "addressed_at": row.get("addressed_at"),
                    "related_theme_ids": row.get("related_theme_ids", []),
                }
                for row in self.watch_items.values()
                if row.get("status", "open") == "open"
                and (not args or row.get("owner_user_id") == args[0])
            ]
        # JOIN artifact_topics filter is honored at the FakePool level via self.artifact_topics; matchers below ignore the JOIN clause.
        if "FROM observations" in compact:
            if (
                ("SELECT id, content" in compact or "SELECT o.id, o.content" in compact)
                and "scoring_prompt_version" in compact
            ):
                threshold = args[0]
                return [
                    {"id": row["id"], "content": row.get("content", "")}
                    for row in self.observations.values()
                    if row.get("scoring_prompt_version") is None
                    or row.get("scoring_prompt_version") < threshold
                    or str(row.get("scoring_prompt_version", "")).endswith("failed")
                    or row.get("needs_rescoring") is True
                ]
            return [
                {
                    "id": row["id"],
                    "about_user_id": row.get("about_user_id"),
                    "content": row.get("content", ""),
                    "confidence": row.get("confidence", "medium"),
                    "significance": row.get("significance", 3),
                    "status": row.get("status", "active"),
                    "related_theme_ids": row.get("related_theme_ids", []),
                    "supporting_message_ids": row.get("supporting_message_ids", []),
                    "last_reinforced_at": row.get("last_reinforced_at"),
                    "created_at": row.get("created_at", datetime.now(UTC)),
                    "surfaced_count": row.get("surfaced_count", 0),
                }
                for row in self.observations.values()
                if row.get("status", "active") == "active"
                and row.get("significance", 0) >= 3
            ]
        # JOIN artifact_topics filter is honored at the FakePool level via self.artifact_topics; matchers below ignore the JOIN clause.
        if "FROM distillations" in compact:
            return [
                {
                    "id": row["id"],
                    "content": row.get("content", ""),
                    "confidence": row.get("confidence", "medium"),
                    "status": row.get("status", "active"),
                    "sensitivity": row.get("sensitivity", "medium"),
                    "visibility": row.get("visibility", "private"),
                    "shareable_summary": row.get("shareable_summary"),
                    "source_user_ids": row.get("source_user_ids", []),
                    "related_memory_ids": row.get("related_memory_ids", []),
                    "related_observation_ids": row.get("related_observation_ids", []),
                    "related_theme_ids": row.get("related_theme_ids", []),
                    "supporting_message_ids": row.get("supporting_message_ids", []),
                    "created_from_tool_call_id": row.get("created_from_tool_call_id"),
                    "triggering_message_id": row.get("triggering_message_id"),
                    "supersedes_distillation_id": row.get("supersedes_distillation_id"),
                    "superseded_by_distillation_id": row.get(
                        "superseded_by_distillation_id"
                    ),
                    "revision_note": row.get("revision_note"),
                    "revision_count": row.get("revision_count", 0),
                    "created_at": row.get("created_at", datetime.now(UTC)),
                    "updated_at": row.get("updated_at", datetime.now(UTC)),
                    "revised_at": row.get("revised_at"),
                    "retired_at": row.get("retired_at"),
                    "recorded_by_bot_id": row.get("recorded_by_bot_id"),
                    "visibility_bot_id": row.get("recorded_by_bot_id")
                    or self.messages.get(row.get("triggering_message_id"), {}).get(
                        "bot_id"
                    ),
                }
                for row in self.distillations.values()
                if row.get("status", "active") == "active"
            ]
        if "FROM messages" in compact and "WHERE id = ANY" in compact:
            message_ids = set(args[0])
            has_bot_scope = "bot_id = $2" in compact and "topic_id = $3" in compact
            bot_filter = args[1] if has_bot_scope and len(args) > 1 else None
            topic_filter = args[2] if has_bot_scope and len(args) > 2 else None
            return [
                {
                    "id": row["id"],
                    "direction": row.get("direction"),
                    "sender_id": row.get("sender_id"),
                    "recipient_id": row.get("recipient_id"),
                    "charge": row.get("charge") or "routine",
                    "sent_at": row["sent_at"],
                    "content": row.get("content"),
                    "media_type": row.get("media_type"),
                    "media_analysis": row.get("media_analysis"),
                    "media_duration_seconds": row.get("media_duration_seconds"),
                    "bot_id": row.get("bot_id"),
                    "topic_id": row.get("topic_id"),
                }
                for row in self.messages.values()
                if row["id"] in message_ids
                and (
                    not has_bot_scope
                    or self._message_matches_bot_topic(row, bot_filter, topic_filter)
                )
            ]
        if (
            "FROM messages" in compact
            and "SELECT id, sender_id" in compact
            and "sent_at, content" in compact
        ):
            params = list(args)
            limit = params[-1]
            has_bot_scope = "bot_id =" in compact and "topic_id =" in compact
            allowed = params[0] if params else None
            bot_filter = params[1] if has_bot_scope and len(params) > 2 else None
            topic_filter = params[2] if has_bot_scope and len(params) > 2 else None
            text_filter = next(
                (
                    arg.strip("%").lower()
                    for arg in params
                    if isinstance(arg, str) and arg.startswith("%")
                ),
                None,
            )
            datetimes = [arg for arg in params if isinstance(arg, datetime)]
            start = datetimes[0] if len(datetimes) >= 1 else None
            end = datetimes[1] if len(datetimes) >= 2 else None
            rows = []
            for row in self.messages.values():
                if row.get("deleted_at") is not None:
                    continue
                if row.get("search_suppressed_at") is not None:
                    continue
                if has_bot_scope and not self._message_matches_bot_topic(
                    row, bot_filter, topic_filter
                ):
                    continue
                if start is not None and row["sent_at"] < start:
                    continue
                if end is not None and row["sent_at"] >= end:
                    continue
                analysis = row.get("media_analysis") or {}
                analysis_text = " ".join(
                    str(analysis.get(key) or "")
                    for key in ("explanation", "description", "summary")
                    if isinstance(analysis, dict)
                )
                if (
                    text_filter
                    and text_filter
                    not in f"{row.get('content') or ''} {analysis_text}".lower()
                ):
                    continue
                if isinstance(allowed, list):
                    if (
                        row.get("sender_id") not in allowed
                        and row.get("recipient_id") not in allowed
                    ):
                        continue
                elif allowed is not None and (
                    row.get("sender_id") != allowed
                    and row.get("recipient_id") != allowed
                ):
                    continue
                rows.append(
                    {
                        "id": row["id"],
                        "sender_id": row.get("sender_id"),
                        "sent_at": row["sent_at"],
                        "content": row.get("content"),
                        "media_type": row.get("media_type"),
                        "media_analysis": row.get("media_analysis"),
                        "charge": row.get("charge") or "routine",
                        "direction": row.get("direction"),
                        "recipient_id": row.get("recipient_id"),
                        "bot_id": row.get("bot_id"),
                        "topic_id": row.get("topic_id"),
                    }
                )
            rows.sort(key=lambda row: row["sent_at"], reverse=True)
            return rows[:limit]
        if compact.startswith("SELECT content FROM messages WHERE bot_turn_id"):
            turn_id = args[0]
            rows = [
                row
                for row in self.messages.values()
                if row.get("bot_turn_id") == turn_id
                and row.get("direction") == "outbound"
                and row.get("processing_state") == "processed"
                and row.get("outbound_part_index") is not None
            ]
            rows.sort(
                key=lambda row: (
                    row.get("outbound_part_index") or 0,
                    row.get("sent_at"),
                )
            )
            return [{"content": row.get("content")} for row in rows]
        if "FROM messages" in compact and "direction='inbound'" in compact:
            user_id, since = args
            rows = [
                row
                for row in self.messages.values()
                if row.get("direction") == "inbound"
                and row.get("sender_id") == user_id
                and str(row.get("sent_at")) >= str(since)
            ]
            rows.sort(key=lambda row: row["sent_at"])
            return rows
        if compact.startswith("UPDATE bot_turns SET failure_reason='crashed'"):
            rows = []
            for turn in self.bot_turns.values():
                if (
                    turn["completed_at"] is None
                    and turn["failure_reason"] is None
                    and turn.get("final_output_message_id") is None
                ):
                    turn["failure_reason"] = "crashed"
                    rows.append(
                        {
                            "id": turn["id"],
                            "triggering_message_ids": turn["triggering_message_ids"],
                            "user_id": turn.get("user_in_context"),
                            "bot_id": turn.get("bot_id"),
                            "topic_id": turn.get("topic_id"),
                        }
                    )
            return rows
        if (
            compact.startswith(
                "SELECT id, triggering_message_ids, bot_id"
            )
            and "FROM bot_turns" in compact
            and "failure_reason = 'crashed'" in compact
        ):
            # Recovery-v2: SELECT crashed bot_turns for passive release.
            rows = []
            cutoff = datetime.now(UTC) - timedelta(minutes=5)
            for turn in self.bot_turns.values():
                if turn.get("failure_reason") != "crashed":
                    continue
                if turn.get("completed_at") is not None:
                    continue
                if turn.get("final_output_message_id") is not None:
                    continue
                started_at = turn.get("started_at")
                if started_at is not None and started_at >= cutoff:
                    continue
                rows.append(
                    {
                        "id": turn["id"],
                        "triggering_message_ids": turn["triggering_message_ids"],
                        "bot_id": turn.get("bot_id"),
                    }
                )
            return rows
        if (
            compact.startswith(
                "SELECT id, triggering_message_ids, user_in_context AS user_id"
            )
            and "FROM bot_turns" in compact
            and "failure_reason = 'crashed'" in compact
        ):
            # Legacy: SELECT crashed bot_turns for re-dispatch (pre-T8).
            rows = []
            cutoff = datetime.now(UTC) - timedelta(minutes=5)
            for turn in self.bot_turns.values():
                if turn.get("failure_reason") != "crashed":
                    continue
                if turn.get("completed_at") is not None:
                    continue
                if turn.get("final_output_message_id") is not None:
                    continue
                started_at = turn.get("started_at")
                if started_at is not None and started_at >= cutoff:
                    continue
                rows.append(
                    {
                        "id": turn["id"],
                        "triggering_message_ids": turn["triggering_message_ids"],
                        "user_id": turn.get("user_in_context"),
                        "bot_id": turn.get("bot_id"),
                        "topic_id": turn.get("topic_id"),
                    }
                )
            return rows
        if compact.startswith("WITH due AS"):
            now, limit, heartbeat_only, worker_id = args
            rows = []
            due = [
                job
                for job in self.scheduled_jobs.values()
                if job["status"] == "pending"
                and job["scheduled_for"] <= now
                and (not heartbeat_only or job["job_type"] == "heartbeat")
                and job.get("claimed_at") is None
            ]
            due.sort(key=lambda job: job["scheduled_for"])
            for job in due[:limit]:
                job["claimed_at"] = now
                job["claimed_by"] = worker_id
                rows.append(
                    {
                        "id": job["id"],
                        "user_id": job.get("user_id"),
                        "job_type": job["job_type"],
                        "scheduled_for": job["scheduled_for"],
                        "context": job.get("context", {}),
                        "status": job["status"],
                        "attempt_count": job.get("attempt_count", 0),
                        "max_attempts": job.get("max_attempts", 2),
                        "delayed": job.get("delayed", False),
                        "bot_id": job.get("bot_id"),
                        "topic_id": job.get("topic_id"),
                    }
                )
            return rows
        if compact.startswith("SELECT m.id, m.sender_id"):
            referenced = {
                message_id
                for turn in self.bot_turns.values()
                for message_id in turn.get("triggering_message_ids", [])
            }
            return [
                {
                    "id": m["id"],
                    "user_id": m["sender_id"],
                    "sender_id": m["sender_id"],
                    "bot_id": m.get("bot_id"),
                    "topic_id": m.get("topic_id"),
                    "channel_id": m.get("channel_id"),
                    "binding_id": m.get("binding_id"),
                    "dyad_id": m.get("dyad_id"),
                }
                for m in self.messages.values()
                if m["processing_state"] == "raw"
                and m["direction"] == "inbound"
                and m["id"] not in referenced
            ]
        if "FROM messages" in compact and not args:
            rows = list(self.messages.values())
            rows.sort(key=lambda row: row["sent_at"], reverse=True)
            return rows[:100]
        if "FROM messages" in compact and "SELECT id, direction" in compact:
            user_filter = args[0]
            has_bot_scope = "bot_id = $2" in compact and "topic_id = $3" in compact
            bot_filter = args[1] if has_bot_scope and len(args) > 1 else None
            topic_filter = args[2] if has_bot_scope and len(args) > 2 else None
            user_ids = (
                set(user_filter) if isinstance(user_filter, list) else {user_filter}
            )
            rows = [
                row
                for row in self.messages.values()
                if row.get("deleted_at") is None
                and (
                    row.get("sender_id") in user_ids
                    or row.get("recipient_id") in user_ids
                )
                and (
                    not has_bot_scope
                    or self._message_matches_bot_topic(row, bot_filter, topic_filter)
                )
            ]
            rows.sort(key=lambda row: row["sent_at"], reverse=True)
            return rows[:20]
        if "FROM users" in compact and "onboarding_state" in compact:
            return list(self.users.values())
        if (
            compact.startswith("SELECT event_seq")
            and "FROM turn_audit_events" in compact
        ):
            turn_id = args[0] if args else None
            rows = [
                {
                    "event_seq": row["event_seq"],
                    "event_type": row["event_type"],
                    "step": row["step"],
                    "severity": row["severity"],
                    "occurred_at": row["occurred_at"],
                    "duration_ms": row["duration_ms"],
                    "actor": row["actor"],
                    "message": row["message"],
                    "metadata": row["metadata"],
                }
                for row in self.turn_audit_events
                if turn_id is None or str(row.get("turn_id")) == str(turn_id)
            ]
            rows.sort(key=lambda row: row["event_seq"])
            return rows
        if "FROM feedback f JOIN messages m" in compact:
            user_id, now_utc = args[:2]
            has_bot_scope = "m.bot_id = $3" in compact and "m.topic_id = $4" in compact
            bot_filter = args[2] if has_bot_scope and len(args) > 2 else None
            topic_filter = args[3] if has_bot_scope and len(args) > 3 else None
            previous_completed_at = max(
                (
                    turn["completed_at"]
                    for turn in self.bot_turns.values()
                    if turn.get("user_in_context") == user_id
                    and turn.get("completed_at") is not None
                ),
                default=None,
            )
            if previous_completed_at is None:
                return []
            rows = []
            for feedback in self.feedback.values():
                message = self.messages.get(feedback.get("target_id"))
                if message is None:
                    continue
                if feedback.get("from_user_id") != user_id:
                    continue
                if (
                    feedback.get("target_type") != "message"
                    or feedback.get("source") != "reaction"
                ):
                    continue
                if (
                    message.get("direction") != "outbound"
                    or message.get("recipient_id") != user_id
                ):
                    continue
                if has_bot_scope and (
                    message.get("bot_id") != bot_filter
                    or message.get("topic_id") != topic_filter
                ):
                    continue
                if not previous_completed_at < feedback["created_at"] <= now_utc:
                    continue
                rows.append(
                    {
                        "id": feedback["id"],
                        "sentiment": feedback["sentiment"],
                        "content": feedback["content"],
                        "created_at": feedback["created_at"],
                        "message_id": message["id"],
                        "message_content": message.get("content"),
                        "message_sent_at": message["sent_at"],
                    }
                )
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return rows[:5]
        if "FROM v_bot_actions" in compact or "FROM bot_turns" in compact:
            # Project B work item 3: the real query is `SELECT ... FROM
            # v_bot_actions WHERE bot_id = $1 AND ...`.  Args[0] is the
            # caller's bot_id; we honour the bot-scoping filter here so
            # FakePool tests get the same semantics as Postgres.
            bot_filter = args[0] if args else None
            rows = []
            for turn in self.bot_turns.values():
                # If the FakePool turn was seeded without a bot_id (legacy
                # tests pre-date the v_bot_actions refactor), don't drop
                # it — production turns always have bot_id NOT NULL.
                turn_bot_id = turn.get("bot_id")
                if (
                    bot_filter is not None
                    and turn_bot_id is not None
                    and turn_bot_id != bot_filter
                ):
                    continue
                trigger = self.messages.get(turn.get("triggered_by_message_id"))
                outbound = self.messages.get(turn.get("final_output_message_id"))
                audit_events = [
                    {
                        "id": row["id"],
                        "turn_id": row["turn_id"],
                        "event_seq": row["event_seq"],
                        "event_type": row["event_type"],
                        "step": row["step"],
                        "severity": row["severity"],
                        "occurred_at": row["occurred_at"],
                        "duration_ms": row["duration_ms"],
                        "actor": row["actor"],
                        "message": row["message"],
                        "metadata": row["metadata"],
                    }
                    for row in self.turn_audit_events
                    if row["turn_id"] == turn["id"]
                ]
                rows.append(
                    {
                        **turn,
                        "turn_id": turn["id"],
                        "triggering_content": (
                            trigger.get("content") if trigger else None
                        ),
                        "triggering_handling_result": (
                            trigger.get("handling_result") if trigger else None
                        ),
                        "triggering_processing_error": (
                            trigger.get("processing_error") if trigger else None
                        ),
                        "final_outbound_content": (
                            outbound.get("content") if outbound else None
                        ),
                        "tool_calls": [
                            tc for tc in self.tool_calls if tc["turn_id"] == turn["id"]
                        ],
                        "audit_events": audit_events,
                    }
                )
            rows.sort(key=lambda row: row["started_at"], reverse=True)
            limit = args[-1] if args and isinstance(args[-1], int) else 50
            return rows[:limit]
        if "FROM scheduled_jobs" in compact and "job_type='scheduled_task'" in compact:
            user_id, include_recurring, limit = args
            rows = []
            for job in self.scheduled_jobs.values():
                recurrence = job.get("context", {}).get("recurrence")
                if (
                    job.get("user_id") == user_id
                    and job.get("job_type") == "scheduled_task"
                    and job.get("status") == "pending"
                    and (include_recurring or recurrence is None)
                ):
                    rows.append(
                        {
                            "job_id": job["id"],
                            "scheduled_for": job["scheduled_for"],
                            "context": job.get("context", {}),
                            "delayed": job.get("delayed", False),
                            "created_at": job.get("created_at", datetime.now(UTC)),
                        }
                    )
            rows.sort(key=lambda row: (row["scheduled_for"], row["created_at"]))
            return rows[:limit]
        if "FROM feedback" in compact:
            rows = list(self.feedback.values())
            rows.sort(
                key=lambda row: row.get("created_at", datetime.now(UTC)), reverse=True
            )
            return rows[:50]
        if "FROM public.eval_runs" in compact:
            rows = list(self.eval_runs.values())
            rows.sort(key=lambda row: row["run_at"], reverse=True)
            limit = args[0] if args else 25
            return rows[:limit]
        if "FROM public.eval_results" in compact:
            run_id = args[0]
            rows = [
                row for row in self.eval_results.values() if row["run_id"] == run_id
            ]
            rows.sort(key=lambda row: row["scenario_name"])
            return rows
        if compact.startswith(
            "SELECT id AS job_id, bot_id, topic_id, scheduled_for, context, created_at FROM scheduled_jobs WHERE user_id=$1 AND bot_id=$2 AND job_type='checkin' AND status='pending'"
        ):
            user_id, bot_id, limit = args
            matches = [
                {
                    "job_id": row["id"],
                    "bot_id": row.get("bot_id"),
                    "topic_id": row.get("topic_id"),
                    "scheduled_for": row["scheduled_for"],
                    "context": row.get("context") or {},
                    "created_at": row.get("created_at"),
                }
                for row in self.scheduled_jobs.values()
                if row.get("user_id") == user_id
                and row.get("bot_id") == bot_id
                and row.get("job_type") == "checkin"
                and row.get("status") == "pending"
            ]
            matches.sort(key=lambda r: r["scheduled_for"])
            return matches[: int(limit)]
        if compact.startswith(
            "SELECT id, job_type, scheduled_for, context FROM scheduled_jobs"
            " WHERE user_id=$1 AND bot_id=$2 AND topic_id=$3"
            " AND status='pending'"
            " AND job_type IN ('scheduled_task', 'checkin')"
            " ORDER BY scheduled_for ASC"
        ):
            user_id_arg, bot_id_arg, topic_id_arg = args
            matches = [
                {
                    "id": row["id"],
                    "job_type": row["job_type"],
                    "scheduled_for": row["scheduled_for"],
                    "context": row.get("context") or {},
                }
                for row in self.scheduled_jobs.values()
                if row.get("user_id") == user_id_arg
                and row.get("bot_id") == bot_id_arg
                and row.get("topic_id") == topic_id_arg
                and row.get("status") == "pending"
                and row.get("job_type") in ("scheduled_task", "checkin")
            ]
            matches.sort(key=lambda r: r["scheduled_for"])
            return matches
        if "FROM scheduled_jobs" in compact:
            return sorted(
                self.scheduled_jobs.values(),
                key=lambda row: row["scheduled_for"],
                reverse=True,
            )[:50]
        if compact.startswith(
            "SELECT user_id, bot_id, paused, updated_at, onboarding_state FROM mediator.user_bot_state"
        ):
            # S6: admin user-bot-pauses page. Return rows from self.user_bot_state dict.
            rows = []
            for key, row in self.user_bot_state.items():
                _user_id, _bot_id = key
                rows.append(
                    {
                        "user_id": _user_id,
                        "bot_id": _bot_id,
                        "paused": row.get("paused", False),
                        "updated_at": row.get("updated_at", datetime.now(UTC)),
                        "onboarding_state": row.get("onboarding_state", "pending"),
                    }
                )
            rows.sort(key=lambda r: (r["bot_id"], str(r["user_id"])))
            return rows
        if "FROM llm_spend_log" in compact:
            rows = []
            for provider, value in self.llm_spend_log.items():
                if isinstance(value, dict):
                    rows.append({"provider": provider, **value})
                else:
                    rows.append(
                        {
                            "provider": provider,
                            "day": datetime.now(UTC).date(),
                            "total_usd": value,
                            "warned_80_at": None,
                        }
                    )
            return rows
        if (
            compact.startswith(
                "SELECT id, topic_id, headline, body, last_updated_at FROM topic_status WHERE"
            )
            and "topic_id <> $2" in compact
        ):
            # S6: fetch_cross_topic_status — filter by dyad_id or user_id, exclude topic_id.
            scope_id = args[0]
            exclude_topic_id = args[1]
            cap = args[2]
            rows = []
            for key, row in self.topic_status.items():
                _topic_id, _scope = key
                if _topic_id == exclude_topic_id:
                    continue
                if _scope == scope_id:
                    # Enrich with slug from topics dict
                    slug = "unknown"
                    for s, t in self.topics.items():
                        if t["id"] == _topic_id:
                            slug = s
                            break
                    enriched = dict(row)
                    enriched["slug"] = slug
                    rows.append(enriched)
            rows.sort(
                key=lambda r: r.get(
                    "last_updated_at", datetime.min.replace(tzinfo=UTC)
                ),
                reverse=True,
            )
            return rows[:cap]
        if (
            compact.startswith(
                "SELECT t.id AS topic_id, t.slug, t.display_name, MAX(ts.last_updated_at) AS last_active_at FROM topics t JOIN topic_status ts ON ts.topic_id = t.id WHERE"
            )
            and "topic_id <> $2" in compact
        ):
            # S6: peek_other_topics — filter by dyad_id or user_id, exclude topic_id, within window.
            scope_id = args[0]
            exclude_topic_id = args[1]
            since = args[2]
            cap = args[3]
            rows = []
            for key, row in self.topic_status.items():
                _topic_id, _scope = key
                if _topic_id == exclude_topic_id:
                    continue
                if _scope == scope_id:
                    last = row.get("last_updated_at")
                    if last is not None and last >= since:
                        # Enrich with slug from topics dict
                        slug = "unknown"
                        display_name = "Unknown"
                        for s, t in self.topics.items():
                            if t["id"] == _topic_id:
                                slug = s
                                display_name = t.get("display_name", s)
                                break
                        rows.append(
                            {
                                "topic_id": _topic_id,
                                "slug": slug,
                                "display_name": display_name,
                                "last_active_at": last,
                            }
                        )
            rows.sort(
                key=lambda r: r.get("last_active_at", datetime.min.replace(tzinfo=UTC)),
                reverse=True,
            )
            return rows[:cap]
        if compact.startswith("SELECT id, slug FROM") and "topics" in compact:
            slugs = args[0]
            result = []
            for slug in slugs:
                if slug not in self.topics:
                    self.topics[slug] = {
                        "id": uuid4(),
                        "slug": slug,
                        "display_name": slug.title(),
                    }
                result.append({"id": self.topics[slug]["id"], "slug": slug})
            return result
        # ── Hector fitness: mediator.commitments ──────────────────────────────
        if "FROM mediator.commitments" in compact:
            # list_commitments / get_adherence commitments query
            # args[0]=user_id, args[1]=topic_id.  args[2]=bot_id when parameterised,
            # but _format_fitness_block hardcodes bot_id='hector' in the SQL literal.
            scope_user = args[0]
            scope_topic = args[1]
            if "bot_id = 'hector'" in compact:
                scope_bot = "hector"
            elif len(args) >= 3 and isinstance(args[2], str):
                scope_bot = args[2]
            else:
                scope_bot = args[2] if len(args) >= 3 else "hector"
            # Optional status filter (args[3] if present)
            status_filter = None
            extra_args_start = 3
            if "status = " in compact and "status = 'active'" in compact:
                status_filter = "active"
            elif "status = " in compact and "status = $4" in compact:
                status_filter = args[3] if len(args) > 3 else None
                extra_args_start = 4
            # Optional commitment_ids filter for get_adherence
            commitment_ids_filter = None
            if "id = ANY" in compact:
                commitment_ids_filter = set(args[-1]) if args else None
            rows = [
                row for row in self.commitments.values()
                if row["user_id"] == scope_user
                and row["topic_id"] == scope_topic
                and row["bot_id"] == scope_bot
                and (status_filter is None or row["status"] == status_filter)
                and (commitment_ids_filter is None or row["id"] in commitment_ids_filter)
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=("DESC" in compact))
            if "LIMIT 50" in compact:
                rows = rows[:50]
            return rows
        # ── Hector fitness: mediator.events ───────────────────────────────────
        if "FROM mediator.events" in compact:
            scope_user = args[0]
            scope_topic = args[1]
            # bot_id may be hardcoded in SQL literal (e.g. bot_id = 'hector')
            # or passed as a parameter (args[2]).
            if "bot_id = 'hector'" in compact:
                scope_bot = "hector"
            elif len(args) >= 3 and isinstance(args[2], str):
                scope_bot = args[2]
            else:
                scope_bot = "hector"
            # Optional commitment_id filter
            commitment_id_filter = None
            if "commitment_id = " in compact and "::uuid" in compact:
                # args[3] = commitment_id, args[4] = limit or observed_at
                commitment_id_filter = args[3]
            # Optional before filter
            before_filter = None
            if "observed_at < " in compact:
                # could be at position args[3] or args[4]
                for a in args[3:]:
                    if isinstance(a, datetime):
                        before_filter = a
                        break
            # Optional observed_at >= cutoff (get_adherence)
            cutoff_filter = None
            if "observed_at >= " in compact:
                for a in args[3:]:
                    if isinstance(a, datetime) and not before_filter:
                        cutoff_filter = a
                        break
                    elif isinstance(a, datetime):
                        cutoff_filter = a
                        break
            # Optional commitment_id = ANY (get_adherence)
            cids_filter = None
            if "commitment_id = ANY" in compact:
                for a in args[3:]:
                    if isinstance(a, (list, set)):
                        cids_filter = set(a)
                        break
            limit = 20
            for a in reversed(args):
                if isinstance(a, int):
                    limit = a
                    break
            rows = [
                row for row in self.events.values()
                if row["user_id"] == scope_user
                and row["topic_id"] == scope_topic
                and row["bot_id"] == scope_bot
                and (commitment_id_filter is None or str(row["commitment_id"]) == str(commitment_id_filter))
                and (before_filter is None or row["observed_at"] < before_filter)
                and (cutoff_filter is None or row["observed_at"] >= cutoff_filter)
                and (cids_filter is None or row["commitment_id"] in cids_filter)
            ]
            rows.sort(key=lambda r: r["observed_at"], reverse=True)
            rows = rows[:limit]
            return rows
        raise AssertionError(f"unhandled fetch SQL: {compact}")

    async def execute(self, sql: str, *args) -> str:
        compact = " ".join(sql.split())
        if compact.startswith("SET LOCAL hnsw.ef_search"):
            return "SET"
        if compact.startswith("INSERT INTO mediator.message_embeddings") or compact.startswith(
            "INSERT INTO mediator.content_embeddings"
        ):
            if compact.startswith("INSERT INTO mediator.content_embeddings"):
                source_type, source_id, vector, model, dimension, content_hash_value, now = args
                message_id = source_id if source_type == "message" else None
                key = source_id
            else:
                message_id, vector, model, dimension, content_hash_value, now = args
                source_type = "message"
                source_id = message_id
                key = message_id
            self.message_embeddings[key] = {
                "source_type": source_type,
                "source_id": source_id,
                "message_id": message_id,
                "embedding": vector,
                "model": model,
                "dimension": dimension,
                "content_hash": content_hash_value,
                "embedded_at": now,
            }
            return "INSERT 0 1"
        if compact.startswith("DELETE FROM mediator.message_embeddings") or compact.startswith(
            "DELETE FROM mediator.content_embeddings"
        ):
            if compact.startswith("DELETE FROM mediator.content_embeddings"):
                _, source_id = args
                key = source_id
            else:
                key = args[0]
            existed = key in self.message_embeddings
            self.message_embeddings.pop(key, None)
            return f"DELETE {1 if existed else 0}"
        if compact.startswith("UPDATE mediator.embed_jobs SET status = $1"):
            status, last_error, now, job_id, worker_id = args
            job = self.embed_jobs.get(job_id)
            if (
                job is not None
                and job.get("status") == "processing"
                and job.get("locked_by") == worker_id
            ):
                job.update(
                    status=status,
                    last_error=last_error,
                    locked_at=None,
                    locked_by=None,
                    updated_at=now,
                    completed_at=now,
                )
                return "UPDATE 1"
            return "UPDATE 0"
        if compact.startswith("UPDATE mediator.embed_jobs SET status = 'pending'"):
            last_error, next_attempt_at, now, job_id, worker_id = args
            job = self.embed_jobs.get(job_id)
            if (
                job is not None
                and job.get("status") == "processing"
                and job.get("locked_by") == worker_id
            ):
                job.update(
                    status="pending",
                    last_error=last_error,
                    next_attempt_at=next_attempt_at,
                    locked_at=None,
                    locked_by=None,
                    updated_at=now,
                )
                return "UPDATE 1"
            return "UPDATE 0"
        if "UPDATE mediator.embed_jobs" in compact and "superseded by drop job" in compact:
            source_type, source_id, now = args
            affected = 0
            for job in self.embed_jobs.values():
                if (
                    job.get("source_type", "message") == source_type
                    and job.get("source_id", job["message_id"]) == source_id
                    and job["job_kind"] in {"embed", "reembed"}
                    and job["status"] == "pending"
                ):
                    job.update(
                        status="cancelled",
                        last_error="superseded by drop job",
                        locked_at=None,
                        locked_by=None,
                        updated_at=now,
                        completed_at=now,
                    )
                    affected += 1
            return f"UPDATE {affected}"
        if "UPDATE mediator.embed_jobs" in compact and "superseded by newer content hash" in compact:
            source_type, source_id, content_hash_value, now = args
            affected = 0
            for job in self.embed_jobs.values():
                if (
                    job.get("source_type", "message") == source_type
                    and job.get("source_id", job["message_id"]) == source_id
                    and job["job_kind"] in {"embed", "reembed"}
                    and job["status"] == "pending"
                    and job.get("content_hash") != content_hash_value
                ):
                    job.update(
                        status="superseded",
                        last_error="superseded by newer content hash",
                        locked_at=None,
                        locked_by=None,
                        updated_at=now,
                        completed_at=now,
                    )
                    affected += 1
            return f"UPDATE {affected}"
        if compact.startswith("SET search_path TO"):
            return "SET"
        if compact == "SELECT 1":
            return "SELECT 1"
        if compact.startswith("SELECT set_config("):
            return "SELECT 1"
        if (
            compact.startswith("INSERT INTO system_state")
            and "paused_at = EXCLUDED.paused_at" in compact
        ):
            paused_at, paused_by_user_id = args
            self.system_state["global_pause"].update(
                paused_at=paused_at,
                paused_by_user_id=paused_by_user_id,
                updated_at=paused_at,
            )
            return "INSERT 0 1"
        if (
            compact.startswith("INSERT INTO system_state")
            and "paused_at = NULL" in compact
        ):
            now = args[0]
            self.system_state["global_pause"].update(
                paused_at=None,
                paused_by_user_id=None,
                updated_at=now,
            )
            return "INSERT 0 1"
        if (
            compact.startswith("INSERT INTO system_state")
            and "'recovery_v2_kill'" in compact
        ):
            # Engage / disengage the recovery-v2 kill switch.
            now = args[0]
            value = {"on": "true" in compact.split("VALUES")[1].split("ON CONFLICT")[0]}
            self.system_state["recovery_v2_kill"] = {
                "key": "recovery_v2_kill",
                "value": value,
                "updated_at": now,
            }
            return "INSERT 0 1"
        if compact.startswith("INSERT INTO llm_spend_log"):
            provider, dollars = args
            current = self.llm_spend_log.get(provider, Decimal("0"))
            if isinstance(current, dict):
                current["total_usd"] = current.get("total_usd", Decimal("0")) + Decimal(
                    dollars
                )
            else:
                self.llm_spend_log[provider] = current + Decimal(dollars)
            return "INSERT 0 1"
        if compact.startswith("INSERT INTO user_identities"):
            transport, address, user_id, *_rest = args
            self.user_identities[(transport, address)] = user_id
            return "INSERT 0 1"
        if compact.startswith("UPDATE observations SET related_theme_ids ="):
            theme_id, observation_ids = args
            for observation_id in observation_ids:
                row = self.observations[observation_id]
                row["related_theme_ids"] = list(
                    {*row.get("related_theme_ids", []), theme_id}
                )
            return f"UPDATE {len(observation_ids)}"
        if compact.startswith("UPDATE memories SET related_theme_ids ="):
            theme_id, memory_ids = args
            for memory_id in memory_ids:
                row = self.memories[memory_id]
                row["related_theme_ids"] = list(
                    {*row.get("related_theme_ids", []), theme_id}
                )
            return f"UPDATE {len(memory_ids)}"
        if compact.startswith("UPDATE llm_spend_log SET warned_80_at"):
            provider = args[0]
            current = self.llm_spend_log.get(provider, Decimal("0"))
            if isinstance(current, dict):
                current["warned_80_at"] = current.get("warned_80_at") or datetime.now(
                    UTC
                )
            else:
                self.llm_spend_log[provider] = {
                    "total_usd": current,
                    "day": datetime.now(UTC).date(),
                    "warned_80_at": datetime.now(UTC),
                }
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET media_type='voice'"):
            media_url, duration, message_id = args
            self.messages[message_id].update(
                media_type="voice",
                media_url=media_url,
                media_duration_seconds=duration,
            )
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET media_type='image'"):
            media_url, message_id = args
            self.messages[message_id].update(media_type="image", media_url=media_url)
            return "UPDATE 1"
        if compact.startswith(
            "UPDATE messages SET content=$1, content_encrypted=$2 WHERE id"
        ):
            content, content_encrypted, message_id = args
            self.messages[message_id]["content"] = content
            self.messages[message_id]["content_encrypted"] = content_encrypted
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET content=$1 WHERE id"):
            content, message_id = args
            self.messages[message_id]["content"] = content
            return "UPDATE 1"
        if compact.startswith(
            "UPDATE messages SET content=$1, content_encrypted=$2, media_analysis=$3"
        ):
            content, content_encrypted, analysis, message_id = args
            self.messages[message_id]["content"] = content
            self.messages[message_id]["content_encrypted"] = content_encrypted
            self.messages[message_id]["media_analysis"] = analysis
            if "processing_state='expired'" in compact:
                self.messages[message_id]["processing_state"] = "expired"
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET content=$1, media_analysis=$2"):
            content, analysis, message_id = args
            self.messages[message_id]["content"] = content
            self.messages[message_id]["media_analysis"] = analysis
            if "processing_state='expired'" in compact:
                self.messages[message_id]["processing_state"] = "expired"
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET media_analysis=$1"):
            analysis, message_id = args
            self.messages[message_id]["media_analysis"] = analysis
            if "processing_state='expired'" in compact:
                self.messages[message_id]["processing_state"] = "expired"
            return "UPDATE 1"
        if compact.startswith(
            "UPDATE messages SET media_analysis = COALESCE(media_analysis"
        ):
            error_str, message_id = args
            existing = self.messages[message_id].get("media_analysis") or {}
            existing["_pipeline"] = {"attempts": 2, "last_error": error_str}
            self.messages[message_id]["media_analysis"] = existing
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET processing_state='expired'"):
            if len(args) == 1:
                self.messages[args[0]]["processing_state"] = "expired"
            else:
                error, message_id = args
                self.messages[message_id]["processing_state"] = "expired"
                self.messages[message_id]["media_analysis"] = {
                    "_pipeline": {"attempts": 2, "last_error": error}
                }
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET whatsapp_message_id"):
            wa_id, message_id = args
            self.messages[message_id]["whatsapp_message_id"] = wa_id
            self.messages[message_id]["processing_state"] = "processed"
            return "UPDATE 1"
        if compact.startswith(
            "UPDATE messages SET processing_state='processed' WHERE id = ANY"
        ):
            message_ids = set(args[0])
            for message_id in message_ids:
                if (
                    message_id in self.messages
                    and self.messages[message_id]["processing_state"] == "raw"
                ):
                    self.messages[message_id]["processing_state"] = "processed"
            return "UPDATE 1"
        if compact.startswith(
            "UPDATE messages SET processing_state='processed' WHERE id=$1"
        ):
            self.messages[args[0]]["processing_state"] = "processed"
            return "UPDATE 1"
        if compact.startswith(
            "UPDATE messages SET processing_state='deferred' WHERE id = ANY"
        ):
            message_ids = set(args[0])
            for message_id in message_ids:
                if message_id in self.messages:
                    self.messages[message_id]["processing_state"] = "deferred"
            return "UPDATE 1"
        if compact.startswith(
            "UPDATE messages SET processing_state='raw' WHERE id = ANY"
        ):
            message_ids = set(args[0])
            for message_id in message_ids:
                if message_id in self.messages:
                    self.messages[message_id]["processing_state"] = "raw"
            return "UPDATE 1"
        # ── 0041: inbound_queue helpers ─────────────────────────────────
        # complete_messages → processed + handling metadata
        if ("handling_result" in compact and "handled_by_turn_id" in compact
                and "handled_at = now()" in compact):
            message_ids = set(args[0])
            bot_id_arg = args[1]
            topic_id_arg = args[2]
            handling_result = args[3]
            handled_by_turn_id = args[4]
            count = 0
            for message_id in message_ids:
                if message_id in self.messages:
                    msg = self.messages[message_id]
                    if msg.get("direction") != "inbound":
                        continue
                    # Backward-compat: legacy messages without bot_id/topic_id
                    # default to mediator/relationship (matches claim CTE simulator).
                    row_bid = msg["bot_id"] if "bot_id" in msg else "mediator"
                    row_tid = msg["topic_id"] if "topic_id" in msg else UUID("00000000-0000-4000-8000-000000000001")
                    if row_bid != bot_id_arg:
                        continue
                    if row_tid != topic_id_arg:
                        continue
                    msg["processing_state"] = "processed"
                    msg["handling_result"] = handling_result
                    msg["handled_at"] = datetime.now(UTC)
                    msg["handled_by_turn_id"] = handled_by_turn_id
                    # Recovery-v2: complete_messages NULLs both lifecycle cols.
                    msg["next_retry_at"] = None
                    msg["failure_class"] = None
                    # 0049: clear in-flight owner at terminal completion
                    msg["bot_turn_id"] = None
                    count += 1
            return f"UPDATE {count}"
        # fail_messages → failed + error
        if ("processing_error" in compact
                and "handling_result = 'failed'" in compact):
            message_ids = set(args[0])
            bot_id_arg = args[1]
            topic_id_arg = args[2]
            processing_error = args[3]
            handled_by_turn_id = args[4] if len(args) > 4 else None
            failure_class = args[5] if len(args) > 5 else None
            backoff_base = args[6] if len(args) > 6 else None
            backoff_cap = args[7] if len(args) > 7 else None
            count = 0
            for message_id in message_ids:
                if message_id in self.messages:
                    msg = self.messages[message_id]
                    if msg.get("direction") != "inbound":
                        continue
                    # Backward-compat: legacy messages without bot_id/topic_id
                    # default to mediator/relationship (matches claim CTE simulator).
                    row_bid = msg["bot_id"] if "bot_id" in msg else "mediator"
                    row_tid = msg["topic_id"] if "topic_id" in msg else UUID("00000000-0000-4000-8000-000000000001")
                    if row_bid != bot_id_arg:
                        continue
                    if row_tid != topic_id_arg:
                        continue
                    msg["processing_state"] = "failed"
                    msg["processing_error"] = processing_error
                    msg["handling_result"] = "failed"
                    if handled_by_turn_id is not None:
                        msg["handled_by_turn_id"] = handled_by_turn_id
                        msg["handled_at"] = datetime.now(UTC)
                    # 0049: clear in-flight owner at terminal failure
                    msg["bot_turn_id"] = None
                    # Recovery-v2 lifecycle columns: failure_class + backoff.
                    if failure_class is not None:
                        msg["failure_class"] = failure_class
                        if (
                            failure_class == "retryable_pre_send"
                            and backoff_base is not None
                            and backoff_cap is not None
                        ):
                            attempts = msg.get("processing_attempts", 0)
                            seconds = min(
                                float(backoff_base) * (2 ** max(attempts - 1, 0)),
                                float(backoff_cap),
                            )
                            msg["next_retry_at"] = datetime.now(UTC) + timedelta(
                                seconds=seconds
                            )
                        else:
                            msg["next_retry_at"] = None
                    count += 1
            return f"UPDATE {count}"
        # defer_messages (0041 queue helper with direction='inbound' guard)
        if ("UPDATE messages SET processing_state = 'deferred'"
                in compact and "direction = 'inbound'" in compact):
            message_ids = set(args[0])
            bot_id_arg = args[1]
            topic_id_arg = args[2]
            count = 0
            for message_id in message_ids:
                if message_id in self.messages:
                    msg = self.messages[message_id]
                    if msg.get("direction") != "inbound":
                        continue
                    # Backward-compat: legacy messages without bot_id/topic_id
                    # default to mediator/relationship (matches claim CTE simulator).
                    row_bid = msg["bot_id"] if "bot_id" in msg else "mediator"
                    row_tid = msg["topic_id"] if "topic_id" in msg else UUID("00000000-0000-4000-8000-000000000001")
                    if row_bid != bot_id_arg:
                        continue
                    if row_tid != topic_id_arg:
                        continue
                    msg["processing_state"] = "deferred"
                    count += 1
            return f"UPDATE {count}"
        # expire_messages → expired terminal (per-message with id=ANY)
        if ("handling_result = 'expired'" in compact
                and "handled_at = now()" in compact
                and "WHERE id = ANY" in compact):
            message_ids = set(args[0])
            bot_id_arg = args[1]
            topic_id_arg = args[2]
            count = 0
            for message_id in message_ids:
                if message_id in self.messages:
                    msg = self.messages[message_id]
                    if msg.get("direction") != "inbound":
                        continue
                    if msg.get("bot_id") != bot_id_arg:
                        continue
                    if msg.get("topic_id") != topic_id_arg:
                        continue
                    msg["processing_state"] = "expired"
                    msg["handling_result"] = "expired"
                    msg["handled_at"] = datetime.now(UTC)
                    count += 1
            return f"UPDATE {count}"
        # Bulk expire in recovery (sent_at < cutoff, no id=ANY)
        if ("handling_result = 'expired'" in compact
                and "handled_at = now()" in compact
                and "sent_at <" in compact
                and "direction = 'inbound'" in compact):
            cutoff = args[0]
            count = 0
            for msg_id, msg in list(self.messages.items()):
                if msg.get("direction") != "inbound":
                    continue
                if ("processing_state = 'raw'" in compact
                        and msg.get("processing_state") != "raw"):
                    continue
                if "processing_state IN ('failed', 'processing')" in compact:
                    if msg.get("processing_state") not in ("failed", "processing"):
                        continue
                st = msg.get("sent_at")
                if st is None:
                    continue
                if st < cutoff:
                    msg["processing_state"] = "expired"
                    msg["handling_result"] = "expired"
                    msg["handled_at"] = datetime.now(UTC)
                    count += 1
            return f"UPDATE {count}"
        # agentic.py: stamp handled_by_turn_id after _open_turn
        if "UPDATE messages SET handled_by_turn_id" in compact:
            turn_id = args[0]
            message_ids = set(args[1])
            count = 0
            for message_id in message_ids:
                if message_id in self.messages:
                    self.messages[message_id]["handled_by_turn_id"] = turn_id
                    count += 1
            return f"UPDATE {count}"
        # recover_stale_processing / recover_retryable_failed:
        # UPDATE ... SET processing_state='raw', processing_started_at=NULL
        # WHERE id IN (SELECT ...)
        if ("processing_state = 'raw'" in compact
                and "processing_started_at = NULL" in compact
                and "WHERE id IN ( SELECT id FROM messages" in compact):
            count = 0
            for msg_id, msg in list(self.messages.items()):
                if msg.get("direction") != "inbound":
                    continue
                if "processing_state = 'processing'" in compact:
                    if msg.get("processing_state") != "processing":
                        continue
                    bot_arg = args[0] if args else None
                    topic_arg = args[1] if len(args) > 1 else None
                    if bot_arg and msg.get("bot_id") != bot_arg:
                        continue
                    if topic_arg and msg.get("topic_id") != topic_arg:
                        continue
                    ps = msg.get("processing_started_at")
                    if ps is not None:
                        stale_limit = datetime.now(UTC) - (args[2] if len(args) > 2 and isinstance(args[2], timedelta) else timedelta(seconds=300))
                        if ps >= stale_limit:
                            continue
                    msg["processing_state"] = "raw"
                    msg["processing_started_at"] = None
                    count += 1
                elif "processing_state = 'failed'" in compact:
                    if msg.get("processing_state") != "failed":
                        continue
                    bot_arg = args[0] if args else None
                    topic_arg = args[1] if len(args) > 1 else None
                    if bot_arg and msg.get("bot_id") != bot_arg:
                        continue
                    if topic_arg and msg.get("topic_id") != topic_arg:
                        continue
                    max_retries = args[2] if len(args) > 2 and isinstance(args[2], int) else 3
                    if msg.get("processing_attempts", 0) >= max_retries:
                        continue
                    # Recovery-v2: skip terminal classes + honour next_retry_at gate.
                    fclass = msg.get("failure_class")
                    if fclass in ("terminal_post_send", "infra_bug"):
                        continue
                    nra = msg.get("next_retry_at")
                    if nra is not None and nra > datetime.now(UTC):
                        continue
                    msg["processing_state"] = "raw"
                    msg["processing_started_at"] = None
                    count += 1
            return f"UPDATE {count}"
        if compact.startswith("UPDATE messages SET edit_history"):
            if len(args) == 4:
                _reason, new_content, content_encrypted, target = args
            else:
                new_content = args[0]
                content_encrypted = args[1] if len(args) == 3 else None
                target = args[-1]
            for message_id, message in self.messages.items():
                if message_id == target or message["whatsapp_message_id"] == target:
                    message["edit_history"] = [
                        {
                            "content": message["content"],
                            "at": datetime.now(UTC).isoformat(),
                        }
                    ]
                    message["content"] = new_content
                    if content_encrypted is not None:
                        message["content_encrypted"] = content_encrypted
                    message["edited_at"] = datetime.now(UTC)
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET deleted_at"):
            target = args[0]
            for message_id, message in self.messages.items():
                if message_id == target or message["whatsapp_message_id"] == target:
                    message["deleted_at"] = datetime.now(UTC)
                    if "processing_state='expired'" in compact:
                        message["processing_state"] = "expired"
            return "UPDATE 1"
        if compact.startswith("UPDATE scheduled_jobs SET status = 'superseded'"):
            if "job_type = ANY" in compact:
                job_types = set(args[1])
                for job in self.scheduled_jobs.values():
                    if (
                        job.get("status") == "pending"
                        and job.get("job_type") in job_types
                    ):
                        job.update(
                            status="superseded",
                            cancellation_reason=job.get("cancellation_reason")
                            or "global pause",
                            claimed_at=None,
                            claimed_by=None,
                        )
                return "UPDATE 1"
            user_id = args[0]
            for job in self.scheduled_jobs.values():
                if (
                    job.get("user_id") == user_id
                    and job.get("job_type") == "checkin"
                    and job.get("status") == "pending"
                ):
                    job["status"] = "superseded"
            return "UPDATE 1"
        if (
            compact.startswith("UPDATE scheduled_jobs SET status='superseded'")
            and "context->>" in compact
        ):
            job_type, context_key, context_id = args
            for job in self.scheduled_jobs.values():
                if (
                    job.get("job_type") == job_type
                    and job.get("status") == "pending"
                    and str(job.get("context", {}).get(context_key)) == str(context_id)
                ):
                    job["status"] = "superseded"
            return "UPDATE 1"
        if (
            compact.startswith("UPDATE scheduled_jobs SET status = 'cancelled'")
            and "interval '24 hours'" in compact
        ):
            now = args[0]
            for job in self.scheduled_jobs.values():
                if job["status"] == "pending" and job[
                    "scheduled_for"
                ] < now - timedelta(hours=24):
                    job["status"] = "cancelled"
                    job["cancellation_reason"] = "too stale"
                    job["claimed_at"] = None
                    job["claimed_by"] = None
            return "UPDATE 1"
        if (
            compact.startswith("UPDATE scheduled_jobs SET status = 'cancelled'")
            and "context->>'kind' = 'commitment_checkin'" in compact
        ):
            user_id, bot_id, topic_id, commitment_id = args
            for job in self.scheduled_jobs.values():
                context = job.get("context") or {}
                if (
                    job.get("user_id") == user_id
                    and job.get("bot_id") == bot_id
                    and job.get("topic_id") == topic_id
                    and job.get("job_type") == "scheduled_task"
                    and job.get("status") == "pending"
                    and context.get("kind") == "commitment_checkin"
                    and str(context.get("commitment_id")) == str(commitment_id)
                ):
                    job["status"] = "cancelled"
                    job["cancellation_reason"] = "commitment_closed"
                    job["updated_at"] = datetime.now(UTC)
            return "UPDATE 1"
        if compact.startswith("UPDATE scheduled_jobs SET context = jsonb_set"):
            now = args[0]
            for job in self.scheduled_jobs.values():
                if (
                    job["status"] == "pending"
                    and job["scheduled_for"] < now - timedelta(hours=1)
                    and job["scheduled_for"] >= now - timedelta(hours=24)
                ):
                    job.setdefault("context", {})["delayed"] = True
                    job["delayed"] = True
                    job["claimed_at"] = None
                    job["claimed_by"] = None
            return "UPDATE 1"
        if (
            compact.startswith("UPDATE scheduled_jobs SET claimed_at = NULL")
            and "interval '1 hour'" in compact
        ):
            now = args[0]
            for job in self.scheduled_jobs.values():
                if (
                    job["status"] == "pending"
                    and job["scheduled_for"] < now
                    and job["scheduled_for"] >= now - timedelta(hours=1)
                ):
                    job["claimed_at"] = None
                    job["claimed_by"] = None
            return "UPDATE 1"
        if compact.startswith("UPDATE scheduled_jobs SET status = 'fired'"):
            now, job_id = args
            self.scheduled_jobs[job_id].update(
                status="fired", fired_at=now, claimed_at=None, claimed_by=None
            )
            return "UPDATE 1"
        if compact.startswith("UPDATE scheduled_jobs SET status = 'withheld'"):
            now, job_id = args
            self.scheduled_jobs[job_id].update(
                status="withheld", claimed_at=None, claimed_by=None, updated_at=now
            )
            return "UPDATE 1"
        if compact.startswith("UPDATE scheduled_jobs SET attempt_count = $1"):
            attempt_count, error, now, job_id = args
            self.scheduled_jobs[job_id].update(
                attempt_count=attempt_count,
                last_error=error,
                claimed_at=None,
                claimed_by=None,
            )
            return "UPDATE 1"
        if (
            compact.startswith("UPDATE scheduled_jobs SET status = 'cancelled'")
            and "attempt_count = $1" in compact
        ):
            attempt_count, error, reason, now, job_id = args
            self.scheduled_jobs[job_id].update(
                status="cancelled",
                attempt_count=attempt_count,
                last_error=error,
                cancellation_reason=reason,
                claimed_at=None,
                claimed_by=None,
            )
            return "UPDATE 1"
        if compact.startswith("UPDATE themes SET status = 'dormant'"):
            now = args[0]
            topic_id = args[-1] if "FROM artifact_topics" in compact else None
            for theme in self.themes.values():
                last = theme.get("last_reinforced_at") or theme.get("first_seen_at")
                if (
                    theme.get("status") == "active"
                    and last is not None
                    and last <= now - timedelta(weeks=6)
                ):
                    if topic_id is not None and not self._row_matches_topic(
                        "themes", theme["id"], topic_id
                    ):
                        continue
                    theme["status"] = "dormant"
                    theme["updated_at"] = now
            return "UPDATE 1"
        if compact.startswith("UPDATE themes SET status = 'resolved_by_time'"):
            now = args[0]
            topic_id = args[-1] if "FROM artifact_topics" in compact else None
            for theme in self.themes.values():
                if (
                    theme.get("status") == "dormant"
                    and theme.get("updated_at") is not None
                    and theme["updated_at"] <= now - timedelta(days=120)
                ):
                    if topic_id is not None and not self._row_matches_topic(
                        "themes", theme["id"], topic_id
                    ):
                        continue
                    theme["status"] = "resolved_by_time"
                    theme["updated_at"] = now
            return "UPDATE 1"
        if compact.startswith("UPDATE observations SET status = 'stale'"):
            now = args[0]
            topic_id = args[-1] if "FROM artifact_topics" in compact else None
            for observation in self.observations.values():
                last = observation.get("last_reinforced_at") or observation.get(
                    "created_at"
                )
                if (
                    observation.get("status") == "active"
                    and last is not None
                    and last <= now - timedelta(days=183)
                ):
                    if topic_id is not None and not self._row_matches_topic(
                        "observations", observation["id"], topic_id
                    ):
                        continue
                    observation["status"] = "stale"
            return "UPDATE 1"
        if compact.startswith(
            "UPDATE observations SET confidence = CASE observations.confidence"
        ) or compact.startswith("UPDATE observations SET confidence = CASE confidence"):
            now = args[0]
            topic_id = args[-1] if "FROM artifact_topics" in compact else None
            for observation in self.observations.values():
                last = observation.get("last_reinforced_at") or observation.get(
                    "created_at"
                )
                if (
                    observation.get("status") == "active"
                    and last is not None
                    and last <= now - timedelta(days=91)
                    and last > now - timedelta(days=183)
                    and observation.get("confidence") in {"high", "medium"}
                ):
                    if topic_id is not None and not self._row_matches_topic(
                        "observations", observation["id"], topic_id
                    ):
                        continue
                    observation["confidence"] = (
                        "medium" if observation["confidence"] == "high" else "low"
                    )
            return "UPDATE 1"
        if compact.startswith("UPDATE observations SET significance = $1"):
            significance, scoring_prompt_version, observation_id = args
            observation = self.observations[observation_id]
            observation["significance"] = significance
            observation["scoring_prompt_version"] = scoring_prompt_version
            observation["last_reinforced_at"] = observation.get(
                "last_reinforced_at"
            ) or datetime.now(UTC)
            if significance is not None:
                observation["needs_rescoring"] = False
            return "UPDATE 1"
        if compact.startswith("UPDATE watch_items SET status = 'expired'"):
            now = args[0]
            topic_id = args[-1] if "FROM artifact_topics" in compact else None
            for item in self.watch_items.values():
                if (
                    item.get("status") == "open"
                    and item.get("due_at") is not None
                    and item.get("addressed_at") is None
                    and item["due_at"] <= now - timedelta(days=30)
                ):
                    if topic_id is not None and not self._row_matches_topic(
                        "watch_items", item["id"], topic_id
                    ):
                        continue
                    item["status"] = "expired"
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET content='[deleted]'"):
            content_encrypted = args[0] if args else None
            for message in self.messages.values():
                if (
                    message["deleted_at"] is not None
                    and message["content"] != "[deleted]"
                ):
                    message["content"] = "[deleted]"
                    if content_encrypted is not None:
                        message["content_encrypted"] = content_encrypted
            return "UPDATE 1"
        if compact.startswith(
            "UPDATE bot_turns SET failure_reason='crashed_after_send'"
        ):
            for turn in self.bot_turns.values():
                if (
                    turn["completed_at"] is None
                    and turn["failure_reason"] is None
                    and turn.get("final_output_message_id") is not None
                ):
                    turn["failure_reason"] = "crashed_after_send"
            return "UPDATE 1"
        if compact.startswith("UPDATE bot_turns SET reasoning"):
            reasoning, reasoning_encrypted, turn_id = args
            self.bot_turns[turn_id]["reasoning"] = reasoning
            self.bot_turns[turn_id]["reasoning_encrypted"] = reasoning_encrypted
            return "UPDATE 1"
        if compact.startswith(
            "UPDATE bot_turns SET final_output_message_id=$1 WHERE id=$2"
        ):
            final_output_message_id, turn_id = args
            self.bot_turns[turn_id]["final_output_message_id"] = final_output_message_id
            return "UPDATE 1"
        if compact.startswith("UPDATE bot_turns SET final_output_message_id"):
            (
                final_output_message_id,
                reasoning,
                reasoning_encrypted,
                duration_ms,
                tool_call_count,
                turn_id,
            ) = args
            self.bot_turns[turn_id].update(
                final_output_message_id=final_output_message_id,
                reasoning=reasoning,
                reasoning_encrypted=reasoning_encrypted,
                completed_at=datetime.now(UTC),
                duration_ms=duration_ms,
                tool_call_count=tool_call_count,
            )
            return "UPDATE 1"
        if compact.startswith("UPDATE bot_turns SET failure_reason=$1"):
            failure_reason, turn_id = args
            self.bot_turns[turn_id]["failure_reason"] = failure_reason
            return "UPDATE 1"
        if compact.startswith("UPDATE public.eval_runs SET scenarios_passed"):
            scenarios_passed, scenarios_failed, total_cost_usd, notes, run_id = args
            self.eval_runs[run_id].update(
                scenarios_passed=scenarios_passed,
                scenarios_failed=scenarios_failed,
                total_cost_usd=Decimal(str(total_cost_usd)),
            )
            if notes is not None:
                self.eval_runs[run_id]["notes"] = notes
            return "UPDATE 1"
        if compact.startswith("INSERT INTO tool_calls"):
            # New schema (migration 0039) adds kind + summary; old call
            # sites still pass 6 args, so handle both shapes.
            if len(args) == 8:
                (
                    turn_id,
                    tool_name,
                    arguments,
                    result,
                    called_at,
                    duration_ms,
                    kind,
                    summary,
                ) = args
            else:
                turn_id, tool_name, arguments, result, called_at, duration_ms = args
                kind, summary = "write", None
            self.tool_calls.append(
                {
                    "turn_id": turn_id,
                    "tool_name": tool_name,
                    "arguments": _coerce_jsonb(arguments),
                    "result": _coerce_jsonb(result),
                    "called_at": called_at,
                    "duration_ms": duration_ms,
                    "kind": kind,
                    "summary": summary,
                }
            )
            return "INSERT 0 1"
        if compact.startswith("INSERT INTO user_bot_state") or compact.startswith(
            "INSERT INTO mediator.user_bot_state"
        ):
            user_id, bot_id, value = args[:3]
            existing = self.user_bot_state.get((user_id, bot_id), {})
            row = {
                "user_id": user_id,
                "bot_id": bot_id,
                "updated_at": datetime.now(UTC),
                "onboarding_state": existing.get("onboarding_state", "pending"),
                "paused": existing.get("paused"),
                "partner_share": existing.get("partner_share"),
            }
            if "partner_share" in compact:
                row["partner_share"] = value
            else:
                # S6: set_user_bot_paused — store in self.user_bot_state keyed by (user_id, bot_id).
                row["paused"] = value
            self.user_bot_state[(user_id, bot_id)] = row
            return "INSERT 0 1"
        if compact.startswith("DELETE FROM llm_spend_log"):
            self.llm_spend_log.clear()
            return "DELETE 0"
        if compact.startswith(
            "UPDATE mediator.conversations SET status = 'prep_failed'"
        ) and "prep_error" in compact:
            # Startup recovery sweeps orphaned live-prep rows. Most FakePool
            # tests do not model conversations, so this is a harmless no-op
            # unless a test has explicitly added a conversations store.
            cutoff = args[0]
            count = 0
            conversations = getattr(self, "conversations", {})
            for row in conversations.values():
                if row.get("status") not in {"prepping", "preparing"}:
                    continue
                started_at = (row.get("session_fields") or {}).get("prep_started_at")
                if isinstance(started_at, str):
                    started_at = datetime.fromisoformat(
                        started_at.replace("Z", "+00:00")
                    )
                started_at = started_at or row.get("created_at")
                if started_at is not None and started_at < cutoff:
                    row["status"] = "prep_failed"
                    session_fields = dict(row.get("session_fields") or {})
                    session_fields["prep_error"] = "orphaned"
                    row["session_fields"] = session_fields
                    count += 1
            return f"UPDATE {count}"
        raise AssertionError(f"unhandled execute SQL: {compact}")


@pytest.fixture
def fake_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool()

    async def create_pool(database_url: str, **kwargs) -> FakePool:
        assert database_url == REQUIRED_ENV["DATABASE_URL"]
        assert kwargs.get("statement_cache_size") == 0
        return pool

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        types.SimpleNamespace(create_pool=create_pool),
    )


@pytest.fixture
def fake_pool(app_env: None) -> FakePool:
    return FakePool()


@pytest.fixture
def make_inbound_scope():
    """Build an InboundScope for tests that call scope-only runtime APIs."""
    from app.models.user import User
    from app.services.scope import InboundScope

    def _make(
        user: User | None = None,
        *,
        bot_id: str = "mediator",
        transport: str = "discord",
        user_id: UUID | None = None,
        topic_id: UUID | None = None,
        channel_id: str | None = None,
        binding_id: UUID | None = None,
        dyad_id: UUID | None = None,
    ) -> InboundScope:
        resolved_user_id = user_id or (user.id if user is not None else uuid4())
        resolved_dyad_id = dyad_id
        if resolved_dyad_id is None and bot_id != "tante_rosi":
            resolved_dyad_id = uuid4()
        return InboundScope(
            bot_id=bot_id,
            transport=transport,
            user_id=resolved_user_id,
            topic_id=topic_id or uuid4(),
            channel_id=channel_id,
            binding_id=binding_id or uuid4(),
            dyad_id=resolved_dyad_id,
        )

    return _make


@pytest.fixture
async def async_client(app_env: None, fake_asyncpg: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


# ---------------------------------------------------------------------------
# Project B.1: real-Postgres fixtures (see tests/fixtures/postgres.py).
#
# Tests opt in with ``pytestmark = pytest.mark.postgres`` and request the
# ``pg_pool`` / ``pg_dsn`` fixtures.  The fixtures auto-skip on hosts that
# have neither Docker nor ``TEST_DATABASE_URL`` set.
# ---------------------------------------------------------------------------

from tests.fixtures.postgres import (  # noqa: E402  (re-export at end of conftest)
    pg_dsn as pg_dsn,
    pg_pool as pg_pool,
    _pg_container as _pg_container,
)

# Project B.2: scenario fixtures (replied / silent / failed_pre_send turns).
# See tests/fixtures/scenarios.py for the seeding details.
from tests.fixtures.scenarios import (  # noqa: E402  (re-export at end of conftest)
    replied_turn as replied_turn,
    silent_turn as silent_turn,
    failed_pre_send_turn as failed_pre_send_turn,
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "postgres: test requires a real Postgres instance "
        "(provisioned via Docker locally or the CI services container).",
    )
