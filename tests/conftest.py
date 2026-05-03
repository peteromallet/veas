import sys
import types
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import app


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
        return await self.pool.execute(sql, *args)

    async def fetchrow(self, sql: str, *args):
        return await self.pool.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args):
        return await self.pool.fetchval(sql, *args)

    async def fetch(self, sql: str, *args):
        return await self.pool.fetch(sql, *args)

    def transaction(self) -> "FakeTransactionContext":
        return FakeTransactionContext()


class FakeTransactionContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeAcquireContext:
    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool

    async def __aenter__(self) -> FakeConnection:
        return FakeConnection(self.pool)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakePool:
    def __init__(self) -> None:
        self.closed = False
        self.users = {}
        self.messages = {}
        self.bot_turns = {}
        self.llm_spend_log = {}
        self.tool_calls = []
        self.memories = {}
        self.themes = {}
        self.watch_items = {}
        self.observations = {}
        self.out_of_bounds = {}
        self.withheld_outbound_reviews = {}
        self.bridge_candidates = {}
        self.pacing_events = {}
        self.scheduled_jobs = {}
        self.eval_runs = {}
        self.eval_results = {}
        self.system_state = {"global_pause": {"key": "global_pause", "paused_at": None, "value": {}}}
        self.feedback = {}

    def acquire(self) -> FakeAcquireContext:
        return FakeAcquireContext(self)

    async def close(self) -> None:
        self.closed = True

    async def fetchrow(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO users"):
            name, phone, timezone = args
            existing = next((u for u in self.users.values() if u["phone"] == phone), None)
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
                "cross_thread_sharing_default": None,
            }
            self.users[row["id"]] = row
            return row
        if (
            compact.startswith("SELECT id, name, phone, timezone FROM users WHERE id")
            or compact.startswith("SELECT id, name, phone, timezone, onboarding_state FROM users WHERE id")
            or compact.startswith("SELECT id, name, phone, timezone, onboarding_state, pacing_preferences FROM users WHERE id")
            or compact.startswith("SELECT id, name, phone, timezone, onboarding_state, pacing_preferences, cross_thread_sharing_default FROM users WHERE id")
        ):
            return self.users[args[0]]
        if compact.startswith("SELECT pacing_preferences FROM users WHERE id"):
            user = self.users.get(args[0])
            if user is None:
                return None
            return {"pacing_preferences": user.get("pacing_preferences", {})}
        if compact.startswith("SELECT cross_thread_sharing_default FROM users WHERE id"):
            user = self.users.get(args[0])
            if user is None:
                return None
            return {"cross_thread_sharing_default": user.get("cross_thread_sharing_default")}
        if compact.startswith("UPDATE users SET pacing_preferences"):
            user_id, preferences_json = args
            preferences = json.loads(preferences_json)
            self.users.setdefault(
                user_id,
                {
                    "id": user_id,
                    "name": "User",
                    "phone": "1",
                    "timezone": "UTC",
                    "onboarding_state": "pending",
                    "pacing_preferences": {},
                    "cross_thread_sharing_default": None,
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
        if compact.startswith("SELECT id, name, phone, timezone, weekly_summary_enabled"):
            row = dict(self.users[args[0]])
            row.setdefault("weekly_summary_enabled", True)
            row.setdefault("weekly_summary_day", 1)
            row.setdefault("weekly_summary_time", "09:00")
            return row
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
                and (row.get("sender_id") == user_id or row.get("recipient_id") == user_id)
                and period_start <= row["sent_at"] < period_end
            ]
            return {
                "period_start": period_start,
                "period_end": period_end,
                "inbound_count": sum(1 for row in messages if row.get("direction") == "inbound"),
                "outbound_count": sum(1 for row in messages if row.get("direction") == "outbound"),
                "total_count": len(messages),
            }
        if compact.startswith("SELECT whatsapp_message_id FROM messages WHERE id=$1 AND direction='inbound'"):
            message_id, sender_id = args
            row = self.messages.get(message_id)
            if row is None or row.get("direction") != "inbound" or row.get("sender_id") != sender_id:
                return None
            return {"whatsapp_message_id": row.get("whatsapp_message_id")}
        if compact.startswith("SELECT id, processing_state, whatsapp_message_id, content FROM messages WHERE outbound_part_key"):
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
        if compact.startswith("SELECT processing_state, whatsapp_message_id FROM messages WHERE id=$1 AND direction='outbound'"):
            row = self.messages.get(args[0])
            if row is None or row.get("direction") != "outbound":
                return None
            return {
                "processing_state": row.get("processing_state"),
                "whatsapp_message_id": row.get("whatsapp_message_id"),
            }
        if compact.startswith("SELECT id, media_type, media_url FROM messages WHERE id=$1"):
            row = self.messages.get(args[0])
            if row is None or row.get("deleted_at") is not None:
                return None
            return {
                "id": row["id"],
                "media_type": row.get("media_type"),
                "media_url": row.get("media_url"),
            }
        if compact.startswith("SELECT id, direction, sender_id, recipient_id, media_type, media_url, deleted_at FROM messages"):
            message_id, user_ids = args
            row = self.messages.get(message_id)
            if row is None:
                return None
            if row.get("sender_id") not in user_ids and row.get("recipient_id") not in user_ids:
                return None
            return row
        if compact.startswith("INSERT INTO messages"):
            if "direction, recipient_id" in compact:
                # Outbound: basic form is (recipient_id, content, content_encrypted, state).
                # Incremental-send form appends (bot_turn_id, outbound_part_key, outbound_part_index).
                recipient_id, content, _content_encrypted, state, *part_args = args
                bot_turn_id = part_args[0] if len(part_args) >= 1 else None
                outbound_part_key = part_args[1] if len(part_args) >= 2 else None
                outbound_part_index = part_args[2] if len(part_args) >= 3 else None
                if outbound_part_key is not None:
                    existing = next(
                        (row for row in self.messages.values() if row.get("outbound_part_key") == outbound_part_key),
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
                }
                self.messages[row["id"]] = row
                return {"id": row["id"]}
            # Inbound. Accept both the legacy 10-arg form (no content_encrypted)
            # and the 11-arg form that includes the AES-GCM ciphertext column.
            if "content_encrypted" in compact:
                user_id, content, _content_encrypted, wa_id, sent_at, media_type, media_url, duration, media_analysis, *rest = args
            else:
                user_id, content, wa_id, sent_at, media_type, media_url, duration, media_analysis, *rest = args
            charge = rest[0] if rest else None
            if any(m["whatsapp_message_id"] == wa_id for m in self.messages.values()):
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
                "edit_history": None,
                "edited_at": None,
                "deleted_at": None,
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
                *encrypted,
            ) = args
            prompt_snapshot_encrypted = encrypted[0] if encrypted else None
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
            }
            self.bot_turns[row["id"]] = row
            return {"id": row["id"], "started_at": row["started_at"]}
        if compact.startswith("INSERT INTO public.eval_runs"):
            prompt_version, scenarios_passed, scenarios_failed, total_cost_usd, git_sha, notes = args
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
            run_id, scenario_name, status, judge_verdicts, tool_calls, failure_reason = args
            row = {
                "id": uuid4(),
                "run_id": run_id,
                "scenario_name": scenario_name,
                "status": status,
                "judge_verdicts": json.loads(judge_verdicts),
                "tool_calls": json.loads(tool_calls),
                "failure_reason": failure_reason,
                "created_at": datetime.now(UTC),
            }
            self.eval_results[row["id"]] = row
            return {"id": row["id"]}
        if compact.startswith("UPDATE users SET style_notes"):
            notes, user_id = args
            self.users.setdefault(user_id, {"id": user_id, "name": "User", "phone": "1", "timezone": "UTC"})
            self.users[user_id]["style_notes"] = notes
            return {"user_id": user_id, "updated_at": datetime.now(UTC)}
        if compact.startswith("UPDATE users SET cross_thread_sharing_default"):
            user_id, sharing_default = args
            self.users.setdefault(user_id, {"id": user_id, "name": "User", "phone": "1", "timezone": "UTC"})
            self.users[user_id]["cross_thread_sharing_default"] = sharing_default
            return {"user_id": user_id, "cross_thread_sharing_default": sharing_default, "updated_at": datetime.now(UTC)}
        if compact.startswith("INSERT INTO bridge_candidates"):
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
            ) = args
            now = datetime.now(UTC)
            row = {
                "id": uuid4(),
                "source_user_id": source_user_id,
                "target_user_id": target_user_id,
                "kind": kind,
                "status": status,
                "sensitivity": sensitivity,
                "source_message_ids": list(source_message_ids or []),
                "related_memory_ids": list(related_memory_ids or []),
                "related_observation_ids": list(related_observation_ids or []),
                "internal_note": internal_note,
                "shareable_summary": shareable_summary,
                "sent_message_id": None,
                "created_at": now,
                "updated_at": now,
                "resolved_at": now if status in {"sent", "declined", "blocked", "addressed", "expired"} else None,
            }
            self.bridge_candidates[row["id"]] = row
            return dict(row)
        if compact.startswith("SELECT id, source_user_id, target_user_id") and "FROM bridge_candidates" in compact:
            candidate_id, user_id, partner_id = args
            row = self.bridge_candidates.get(candidate_id)
            if row is None:
                return None
            if {row["source_user_id"], row["target_user_id"]} != {user_id, partner_id}:
                return None
            return dict(row)
        if compact.startswith("UPDATE bridge_candidates SET kind=COALESCE"):
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
            row = self.bridge_candidates[candidate_id]
            if kind is not None:
                row["kind"] = kind
            if status is not None:
                row["status"] = status
                if status in {"sent", "declined", "blocked", "addressed", "expired"} and row.get("resolved_at") is None:
                    row["resolved_at"] = datetime.now(UTC)
            if sensitivity is not None:
                row["sensitivity"] = sensitivity
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
            return dict(row)
        if compact.startswith("UPDATE bridge_candidates SET status=$2"):
            candidate_id, status, sent_message_id, internal_note = args
            row = self.bridge_candidates[candidate_id]
            row["status"] = status
            if sent_message_id is not None:
                row["sent_message_id"] = sent_message_id
            if internal_note is not None:
                row["internal_note"] = internal_note
            if status in {"sent", "declined", "blocked", "addressed", "expired"} and row.get("resolved_at") is None:
                row["resolved_at"] = datetime.now(UTC)
            row["updated_at"] = datetime.now(UTC)
            return dict(row)
        if compact.startswith("INSERT INTO memories (about_user_id"):
            # (about_user_id, content, content_encrypted, related_theme_ids)
            about_user_id, content, _content_encrypted, related_theme_ids = args
            row = {
                "id": uuid4(),
                "about_user_id": about_user_id,
                "content": content,
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
            self.memories.setdefault(memory_id, {"id": memory_id, "status": "active"})
            return {"id": memory_id}
        if compact.startswith("WITH old AS ( UPDATE memories SET status='superseded'"):
            # (old_id, new_content, content_encrypted, related_theme_ids)
            old_id, new_content, _content_encrypted, related_theme_ids = args
            old = self.memories[old_id]
            old["status"] = "superseded"
            new = {
                "id": uuid4(),
                "about_user_id": old["about_user_id"],
                "content": new_content,
                "related_theme_ids": list(related_theme_ids or []),
                "status": "active",
                "supersedes_memory_id": old_id,
                "created_at": datetime.now(UTC),
                "last_referenced_at": None,
            }
            self.memories[new["id"]] = new
            return {"new_id": new["id"], "old_id": old_id}
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
            self.watch_items[watch_item_id].update(status="addressed", addressing_note=note, addressed_at=datetime.now(UTC))
            return {"id": watch_item_id, "addressed_at": self.watch_items[watch_item_id]["addressed_at"]}
        if compact.startswith("UPDATE watch_items SET"):
            watch_item_id = args[-1]
            self.watch_items.setdefault(watch_item_id, {"id": watch_item_id})
            return {"id": watch_item_id}
        if compact.startswith("INSERT INTO observations"):
            # (content, content_encrypted, about_user_id, confidence, significance, scoring_prompt_version, related_theme_ids, supporting_message_ids)
            content, _content_encrypted, about_user_id, confidence, significance, scoring_prompt_version, related_theme_ids, supporting_message_ids = args
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
            if "significance = $1" in compact and "scoring_prompt_version = $2" in compact:
                significance, scoring_prompt_version, observation_id = args
                self.observations.setdefault(observation_id, {"id": observation_id, "status": "active"})
                self.observations[observation_id]["significance"] = significance
                self.observations[observation_id]["scoring_prompt_version"] = scoring_prompt_version
                self.observations[observation_id]["last_reinforced_at"] = self.observations[observation_id].get("last_reinforced_at") or datetime.now(UTC)
                return {"id": observation_id}
            observation_id = args[-1]
            self.observations.setdefault(observation_id, {"id": observation_id, "status": "active"})
            return {"id": observation_id}
        if compact.startswith("INSERT INTO out_of_bounds"):
            # (owner_id, sensitive_core, sensitive_core_encrypted, shareable_context, severity, review_at)
            owner_id, sensitive_core, sensitive_core_encrypted, shareable_context, severity, review_at = args
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
                if job["user_id"] == user_id and job["job_type"] == "checkin" and job["status"] == "pending":
                    job["status"] = "superseded"
                    return {"id": job["id"]}
            return None
        if compact.startswith("INSERT INTO scheduled_jobs") and "SELECT NULL, 'heartbeat'" in compact:
            scheduled_for = args[0]
            if any(job["job_type"] == "heartbeat" and job["status"] == "pending" for job in self.scheduled_jobs.values()):
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
        if compact.startswith("INSERT INTO scheduled_jobs") and "'weekly_summary'" in compact:
            user_id, scheduled_for, context_json = args[:3]
            source_job_id = args[3] if len(args) > 3 else None
            if any(
                job["user_id"] == user_id
                and job["job_type"] == "weekly_summary"
                and job["status"] == "pending"
                and job["id"] != source_job_id
                for job in self.scheduled_jobs.values()
            ):
                return None
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "job_type": "weekly_summary",
                "scheduled_for": scheduled_for,
                "context": json.loads(context_json),
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 2,
                "delayed": False,
                "claimed_at": None,
                "claimed_by": None,
            }
            self.scheduled_jobs[row["id"]] = row
            return {"id": row["id"], "scheduled_for": scheduled_for}
        if compact.startswith("INSERT INTO scheduled_jobs") and "'deferred_turn'" in compact:
            user_id, scheduled_for, context_json = args
            if any(
                job["user_id"] == user_id and job["job_type"] == "deferred_turn" and job["status"] == "pending"
                for job in self.scheduled_jobs.values()
            ):
                return None
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "job_type": "deferred_turn",
                "scheduled_for": scheduled_for,
                "context": json.loads(context_json),
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 2,
                "delayed": False,
                "claimed_at": None,
                "claimed_by": None,
            }
            self.scheduled_jobs[row["id"]] = row
            return {"id": row["id"], "scheduled_for": scheduled_for}
        if compact.startswith("INSERT INTO scheduled_jobs") and ("'watch_item_due'" in compact or "VALUES ($1, $2, $3, $4::jsonb, 'pending')" in compact and args[1] == "watch_item_due"):
            if len(args) == 4:
                user_id, job_type, scheduled_for, context_json = args
            else:
                user_id, scheduled_for, context_json = args
                job_type = "watch_item_due"
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "job_type": job_type,
                "scheduled_for": scheduled_for,
                "context": json.loads(context_json),
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 2,
                "delayed": False,
                "claimed_at": None,
                "claimed_by": None,
            }
            self.scheduled_jobs[row["id"]] = row
            return {"id": row["id"], "scheduled_for": scheduled_for}
        if compact.startswith("INSERT INTO scheduled_jobs") and ("'oob_review'" in compact or "VALUES ($1, $2, $3, $4::jsonb, 'pending')" in compact and args[1] == "oob_review"):
            if len(args) == 4:
                user_id, job_type, scheduled_for, context_json = args
            else:
                user_id, scheduled_for, context_json = args
                job_type = "oob_review"
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "job_type": job_type,
                "scheduled_for": scheduled_for,
                "context": json.loads(context_json),
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
            user_id, scheduled_for, context_json = args
            row = {
                "id": uuid4(),
                "user_id": user_id,
                "job_type": "checkin",
                "scheduled_for": scheduled_for,
                "context": json.loads(context_json),
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 2,
                "delayed": False,
                "claimed_at": None,
                "claimed_by": None,
            }
            self.scheduled_jobs[row["id"]] = row
            return {"job_id": row["id"], "scheduled_for": scheduled_for}
        if compact.startswith("UPDATE scheduled_jobs SET status='cancelled'"):
            user_id = args[0]
            for job in self.scheduled_jobs.values():
                if job["user_id"] == user_id and job["job_type"] == "checkin" and job["status"] == "pending":
                    job["status"] = "cancelled"
                    return {"id": job["id"]}
            return None
        if compact.startswith("SELECT ( SELECT COUNT(*) FROM messages"):
            user_id = args[0]
            conversation_count = sum(
                1
                for message in self.messages.values()
                if message.get("deleted_at") is None
                and (message.get("sender_id") == user_id or message.get("recipient_id") == user_id)
            )
            ongoing_count = sum(1 for theme in self.themes.values() if theme.get("status", "active") == "active")
            ongoing_count += sum(
                1
                for item in self.watch_items.values()
                if item.get("owner_user_id") == user_id and item.get("status", "open") == "open"
            )
            return {"conversation_count": conversation_count, "ongoing_count": ongoing_count}
        if compact.startswith("SELECT id, owner_user_id, content, due_at, status FROM watch_items WHERE id"):
            return self.watch_items.get(args[0])
        if compact.startswith("INSERT INTO feedback"):
            if len(args) == 6:
                from_user_id, target_type, target_id, sentiment, content, source = args
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
                "resolution": "open",
                "resolved_at": None,
                "resolution_note": None,
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
                "signal_snapshot": json.loads(signal_snapshot),
                "preference_snapshot": json.loads(preference_snapshot),
                "wait_ms": wait_ms,
                "reaction": reaction,
                "llm_judgement": json.loads(llm_judgement) if llm_judgement is not None else None,
                "created_at": datetime.now(UTC),
            }
            self.pacing_events[row["id"]] = row
            return {"id": row["id"]}
        raise AssertionError(f"unhandled fetchrow SQL: {compact}")

    async def fetchval(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact == "SELECT 1":
            return 1
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
        if compact.startswith("SELECT EXISTS ( SELECT 1 FROM messages WHERE direction='inbound'"):
            user_id, since, triggering_message_ids = args
            triggering = set(triggering_message_ids or [])
            return any(
                row.get("direction") == "inbound"
                and row.get("sender_id") == user_id
                and row.get("sent_at") > since
                and row["id"] not in triggering
                for row in self.messages.values()
            )
        if compact.startswith("SELECT m.whatsapp_message_id FROM messages m JOIN users u ON u.id = m.sender_id"):
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
        if compact.startswith("SELECT owner_user_id FROM watch_items WHERE id"):
            return self.watch_items[args[0]]["owner_user_id"]
        if compact.startswith("SELECT owner_id FROM out_of_bounds WHERE id"):
            return self.out_of_bounds[args[0]]["owner_id"]
        if compact.startswith("SELECT id FROM messages WHERE whatsapp_message_id"):
            wa_id = args[0]
            for row in self.messages.values():
                if row.get("whatsapp_message_id") == wa_id and row.get("direction") == "outbound":
                    return row["id"]
            return None
        if compact.startswith("SELECT paused_at FROM system_state WHERE key = 'global_pause'"):
            return self.system_state["global_pause"].get("paused_at")
        if compact.startswith("SELECT COALESCE(reasoning, '') FROM bot_turns WHERE id"):
            return self.bot_turns[args[0]].get("reasoning") or ""
        raise AssertionError(f"unhandled fetchval SQL: {compact}")

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT id FROM messages WHERE id = ANY") and "sender_id=$2 OR recipient_id=$2" in compact:
            wanted = set(args[0])
            source_user_id = args[1]
            return [
                {"id": row["id"]}
                for row in self.messages.values()
                if row["id"] in wanted
                and row.get("deleted_at") is None
                and (row.get("sender_id") == source_user_id or row.get("recipient_id") == source_user_id)
            ]
        if compact.startswith("SELECT id FROM messages WHERE id = ANY"):
            wanted = set(args[0])
            return [{"id": row["id"]} for row in self.messages.values() if row["id"] in wanted]
        if compact.startswith("SELECT id FROM themes WHERE id = ANY"):
            wanted = set(args[0])
            return [{"id": row["id"]} for row in self.themes.values() if row["id"] in wanted]
        if compact.startswith("SELECT id FROM observations WHERE id = ANY"):
            wanted = set(args[0])
            return [{"id": row["id"]} for row in self.observations.values() if row["id"] in wanted]
        if compact.startswith("SELECT id FROM memories WHERE id = ANY"):
            wanted = set(args[0])
            return [{"id": row["id"]} for row in self.memories.values() if row["id"] in wanted]
        if compact.startswith("SELECT id, name, phone, timezone FROM users WHERE id <>"):
            return [row for user_id, row in self.users.items() if user_id != args[0]]
        if compact.startswith("SELECT id, name, phone, timezone, weekly_summary_enabled") and "WHERE weekly_summary_enabled = true" in compact:
            rows = []
            for row in self.users.values():
                out = dict(row)
                out.setdefault("weekly_summary_enabled", True)
                out.setdefault("weekly_summary_day", 1)
                out.setdefault("weekly_summary_time", "09:00")
                if out["weekly_summary_enabled"]:
                    rows.append(out)
            return rows
        if compact.startswith("SELECT u.id AS user_id"):
            start, end, *rest = args
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
                    and (message.get("sender_id") == user["id"] or message.get("recipient_id") == user["id"])
                ]
                latest = max(messages, key=lambda message: message["sent_at"]) if messages else None
                rows.append(
                    {
                        "user_id": user["id"],
                        "user_name": user["name"],
                        "cross_thread_sharing_default": user.get("cross_thread_sharing_default"),
                        "message_count": len(messages),
                        "last_message_at": latest["sent_at"] if latest else None,
                        "latest_content": latest["content"] if latest else None,
                    }
                )
            rows.sort(key=lambda row: (row["last_message_at"] is None, row["last_message_at"] or datetime.min.replace(tzinfo=UTC), row["user_name"]))
            return rows
        if "FROM bridge_candidates" in compact and "WHERE target_user_id=$1 AND source_user_id=$2" in compact:
            target_user_id, source_user_id = args
            rows = [
                dict(row)
                for row in self.bridge_candidates.values()
                if row["target_user_id"] == target_user_id
                and row["source_user_id"] == source_user_id
                and row["status"] in {"ready", "sent", "addressed"}
            ]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return rows[:3]
        if "FROM bridge_candidates" in compact:
            user_id, partner_id, source_filter, target_filter, status_filter, limit = args
            rows = [
                dict(row)
                for row in self.bridge_candidates.values()
                if {row["source_user_id"], row["target_user_id"]} == {user_id, partner_id}
                and (source_filter is None or row["source_user_id"] == source_filter)
                and (target_filter is None or row["target_user_id"] == target_filter)
                and (status_filter is None or row["status"] == status_filter)
            ]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return rows[:limit]
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
        if "FROM memories" in compact:
            return [
                {
                    "id": row["id"],
                    "about_user_id": row.get("about_user_id"),
                    "content": row.get("content", ""),
                    "status": row.get("status", "active"),
                    "related_theme_ids": row.get("related_theme_ids", []),
                    "created_at": row.get("created_at", datetime.now(UTC)),
                    "last_referenced_at": row.get("last_referenced_at"),
                }
                for row in self.memories.values()
                if row.get("status", "active") == "active"
            ]
        if "FROM themes" in compact:
            return list(self.themes.values())[:10]
        if "FROM watch_items" in compact:
            return [
                {
                    "id": row["id"],
                    "owner_user_id": row["owner_user_id"],
                    "content": row["content"],
                    "due_at": row.get("due_at"),
                    "related_theme_ids": row.get("related_theme_ids", []),
                }
                for row in self.watch_items.values()
                if row.get("status", "open") == "open" and (not args or row.get("owner_user_id") == args[0])
            ]
        if "FROM observations" in compact:
            if "SELECT id, content" in compact and "scoring_prompt_version" in compact:
                threshold = args[0]
                return [
                    {"id": row["id"], "content": row.get("content", "")}
                    for row in self.observations.values()
                    if row.get("scoring_prompt_version") is None
                    or row.get("scoring_prompt_version") < threshold
                    or str(row.get("scoring_prompt_version", "")).endswith("failed")
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
                if row.get("status", "active") == "active" and row.get("significance", 0) >= 3
            ]
        if "FROM messages" in compact and "WHERE id = ANY" in compact:
            message_ids = set(args[0])
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
                }
                for row in self.messages.values()
                if row["id"] in message_ids
            ]
        if "FROM messages" in compact and "SELECT id, sender_id" in compact and "sent_at, content" in compact:
            params = list(args)
            limit = params[-1]
            text_filter = next((arg.strip("%").lower() for arg in params if isinstance(arg, str) and arg.startswith("%")), None)
            id_filters = [arg for arg in params if not isinstance(arg, (str, int))]
            rows = []
            for row in self.messages.values():
                if row.get("deleted_at") is not None:
                    continue
                analysis = row.get("media_analysis") or {}
                analysis_text = " ".join(
                    str(analysis.get(key) or "")
                    for key in ("explanation", "description", "summary")
                    if isinstance(analysis, dict)
                )
                if text_filter and text_filter not in f"{row.get('content') or ''} {analysis_text}".lower():
                    continue
                if id_filters:
                    allowed = id_filters[0]
                    if isinstance(allowed, list):
                        if row.get("sender_id") not in allowed and row.get("recipient_id") not in allowed:
                            continue
                    elif row.get("sender_id") != allowed and row.get("recipient_id") != allowed:
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
            rows.sort(key=lambda row: (row.get("outbound_part_index") or 0, row.get("sent_at")))
            return [{"content": row.get("content")} for row in rows]
        if "FROM messages" in compact and "direction='inbound'" in compact:
            user_id, since = args
            rows = [
                row
                for row in self.messages.values()
                if row.get("direction") == "inbound" and row.get("sender_id") == user_id and str(row.get("sent_at")) >= str(since)
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
                    rows.append({"triggering_message_ids": turn["triggering_message_ids"]})
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
                    }
                )
            return rows
        if compact.startswith("SELECT m.id, m.sender_id FROM messages m"):
            referenced = {
                message_id
                for turn in self.bot_turns.values()
                for message_id in turn.get("triggering_message_ids", [])
            }
            return [
                {"id": m["id"], "sender_id": m["sender_id"]}
                for m in self.messages.values()
                if m["processing_state"] == "raw" and m["direction"] == "inbound" and m["id"] not in referenced
            ]
        if "FROM messages" in compact and not args:
            rows = list(self.messages.values())
            rows.sort(key=lambda row: row["sent_at"], reverse=True)
            return rows[:100]
        if "FROM messages" in compact and "SELECT id, direction" in compact:
            user_filter = args[0]
            user_ids = set(user_filter) if isinstance(user_filter, list) else {user_filter}
            rows = [
                row
                for row in self.messages.values()
                if row.get("deleted_at") is None
                and (row.get("sender_id") in user_ids or row.get("recipient_id") in user_ids)
            ]
            rows.sort(key=lambda row: row["sent_at"], reverse=True)
            return rows[:20]
        if "FROM users" in compact and "onboarding_state" in compact:
            return list(self.users.values())
        if "FROM bot_turns" in compact:
            rows = []
            for turn in self.bot_turns.values():
                trigger = self.messages.get(turn.get("triggered_by_message_id"))
                outbound = self.messages.get(turn.get("final_output_message_id"))
                rows.append(
                    {
                        **turn,
                        "turn_id": turn["id"],
                        "triggering_content": trigger.get("content") if trigger else None,
                        "final_outbound_content": outbound.get("content") if outbound else None,
                        "tool_calls": [tc for tc in self.tool_calls if tc["turn_id"] == turn["id"]],
                    }
                )
            rows.sort(key=lambda row: row["started_at"], reverse=True)
            limit = args[-1] if args and isinstance(args[-1], int) else 50
            return rows[:limit]
        if "FROM feedback" in compact:
            rows = list(self.feedback.values())
            for row in rows:
                row.setdefault("resolution", "open")
                row.setdefault("resolved_at", None)
                row.setdefault("resolution_note", None)
            if "WHERE resolution =" in compact and args:
                wanted = args[0]
                rows = [row for row in rows if (row.get("resolution") or "open") == wanted]
            rows.sort(key=lambda row: row.get("created_at", datetime.now(UTC)), reverse=True)
            return rows[:100]
        if "FROM public.eval_runs" in compact:
            rows = list(self.eval_runs.values())
            rows.sort(key=lambda row: row["run_at"], reverse=True)
            limit = args[0] if args else 25
            return rows[:limit]
        if "FROM public.eval_results" in compact:
            run_id = args[0]
            rows = [row for row in self.eval_results.values() if row["run_id"] == run_id]
            rows.sort(key=lambda row: row["scenario_name"])
            return rows
        if "FROM scheduled_jobs" in compact:
            return sorted(self.scheduled_jobs.values(), key=lambda row: row["scheduled_for"], reverse=True)[:50]
        if "FROM llm_spend_log" in compact:
            rows = []
            for provider, value in self.llm_spend_log.items():
                if isinstance(value, dict):
                    rows.append({"provider": provider, **value})
                else:
                    rows.append({"provider": provider, "day": datetime.now(UTC).date(), "total_usd": value, "warned_80_at": None})
            return rows
        raise AssertionError(f"unhandled fetch SQL: {compact}")

    async def execute(self, sql: str, *args) -> str:
        compact = " ".join(sql.split())
        if compact.startswith("SET search_path TO"):
            return "SET"
        if compact == "SELECT 1":
            return "SELECT 1"
        if compact.startswith("INSERT INTO system_state") and "paused_at = EXCLUDED.paused_at" in compact:
            paused_at, paused_by_user_id = args
            self.system_state["global_pause"].update(
                paused_at=paused_at,
                paused_by_user_id=paused_by_user_id,
                updated_at=paused_at,
            )
            return "INSERT 0 1"
        if compact.startswith("INSERT INTO system_state") and "paused_at = NULL" in compact:
            now = args[0]
            self.system_state["global_pause"].update(
                paused_at=None,
                paused_by_user_id=None,
                updated_at=now,
            )
            return "INSERT 0 1"
        if compact.startswith("INSERT INTO llm_spend_log"):
            provider, dollars = args
            current = self.llm_spend_log.get(provider, Decimal("0"))
            if isinstance(current, dict):
                current["total_usd"] = current.get("total_usd", Decimal("0")) + Decimal(dollars)
            else:
                self.llm_spend_log[provider] = current + Decimal(dollars)
            return "INSERT 0 1"
        if compact.startswith("UPDATE observations SET related_theme_ids ="):
            theme_id, observation_ids = args
            for observation_id in observation_ids:
                row = self.observations[observation_id]
                row["related_theme_ids"] = list({*row.get("related_theme_ids", []), theme_id})
            return f"UPDATE {len(observation_ids)}"
        if compact.startswith("UPDATE memories SET related_theme_ids ="):
            theme_id, memory_ids = args
            for memory_id in memory_ids:
                row = self.memories[memory_id]
                row["related_theme_ids"] = list({*row.get("related_theme_ids", []), theme_id})
            return f"UPDATE {len(memory_ids)}"
        if compact.startswith("UPDATE llm_spend_log SET warned_80_at"):
            provider = args[0]
            current = self.llm_spend_log.get(provider, Decimal("0"))
            if isinstance(current, dict):
                current["warned_80_at"] = current.get("warned_80_at") or datetime.now(UTC)
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
        if compact.startswith("UPDATE messages SET content=$1, content_encrypted=$2 WHERE id"):
            content, content_encrypted, message_id = args
            self.messages[message_id]["content"] = content
            self.messages[message_id]["content_encrypted"] = content_encrypted
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET content=$1 WHERE id"):
            content, message_id = args
            self.messages[message_id]["content"] = content
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET content=$1, content_encrypted=$2, media_analysis=$3"):
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
        if compact.startswith("UPDATE messages SET processing_state='processed' WHERE id = ANY"):
            message_ids = set(args[0])
            for message_id in message_ids:
                if message_id in self.messages and self.messages[message_id]["processing_state"] == "raw":
                    self.messages[message_id]["processing_state"] = "processed"
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET processing_state='processed' WHERE id=$1"):
            self.messages[args[0]]["processing_state"] = "processed"
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET processing_state='deferred' WHERE id = ANY"):
            message_ids = set(args[0])
            for message_id in message_ids:
                if message_id in self.messages:
                    self.messages[message_id]["processing_state"] = "deferred"
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET processing_state='raw' WHERE id = ANY"):
            message_ids = set(args[0])
            for message_id in message_ids:
                if message_id in self.messages:
                    self.messages[message_id]["processing_state"] = "raw"
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET edit_history"):
            new_content = args[0]
            content_encrypted = args[1] if len(args) == 3 else None
            wa_id = args[-1]
            for message in self.messages.values():
                if message["whatsapp_message_id"] == wa_id:
                    message["edit_history"] = [{"content": message["content"], "at": datetime.now(UTC).isoformat()}]
                    message["content"] = new_content
                    if content_encrypted is not None:
                        message["content_encrypted"] = content_encrypted
                    message["edited_at"] = datetime.now(UTC)
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET deleted_at"):
            wa_id = args[0]
            for message in self.messages.values():
                if message["whatsapp_message_id"] == wa_id:
                    message["deleted_at"] = datetime.now(UTC)
            return "UPDATE 1"
        if compact.startswith("UPDATE scheduled_jobs SET status = 'superseded'"):
            if "job_type = ANY" in compact:
                job_types = set(args[1])
                for job in self.scheduled_jobs.values():
                    if job.get("status") == "pending" and job.get("job_type") in job_types:
                        job.update(
                            status="superseded",
                            cancellation_reason=job.get("cancellation_reason") or "global pause",
                            claimed_at=None,
                            claimed_by=None,
                        )
                return "UPDATE 1"
            user_id = args[0]
            for job in self.scheduled_jobs.values():
                if job.get("user_id") == user_id and job.get("job_type") == "checkin" and job.get("status") == "pending":
                    job["status"] = "superseded"
            return "UPDATE 1"
        if compact.startswith("UPDATE scheduled_jobs SET status='superseded'") and "context->>" in compact:
            job_type, context_key, context_id = args
            for job in self.scheduled_jobs.values():
                if (
                    job.get("job_type") == job_type
                    and job.get("status") == "pending"
                    and str(job.get("context", {}).get(context_key)) == str(context_id)
                ):
                    job["status"] = "superseded"
            return "UPDATE 1"
        if compact.startswith("UPDATE scheduled_jobs SET status = 'cancelled'") and "interval '24 hours'" in compact:
            now = args[0]
            for job in self.scheduled_jobs.values():
                if job["status"] == "pending" and job["scheduled_for"] < now - timedelta(hours=24):
                    job["status"] = "cancelled"
                    job["cancellation_reason"] = "too stale"
                    job["claimed_at"] = None
                    job["claimed_by"] = None
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
        if compact.startswith("UPDATE scheduled_jobs SET claimed_at = NULL") and "interval '1 hour'" in compact:
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
            self.scheduled_jobs[job_id].update(status="fired", fired_at=now, claimed_at=None, claimed_by=None)
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
        if compact.startswith("UPDATE scheduled_jobs SET status = 'cancelled'") and "attempt_count = $1" in compact:
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
            for theme in self.themes.values():
                last = theme.get("last_reinforced_at") or theme.get("first_seen_at")
                if theme.get("status") == "active" and last is not None and last <= now - timedelta(weeks=6):
                    theme["status"] = "dormant"
                    theme["updated_at"] = now
            return "UPDATE 1"
        if compact.startswith("UPDATE themes SET status = 'resolved_by_time'"):
            now = args[0]
            for theme in self.themes.values():
                if theme.get("status") == "dormant" and theme.get("updated_at") is not None and theme["updated_at"] <= now - timedelta(days=120):
                    theme["status"] = "resolved_by_time"
                    theme["updated_at"] = now
            return "UPDATE 1"
        if compact.startswith("UPDATE observations SET status = 'stale'"):
            now = args[0]
            for observation in self.observations.values():
                last = observation.get("last_reinforced_at") or observation.get("created_at")
                if observation.get("status") == "active" and last is not None and last <= now - timedelta(days=183):
                    observation["status"] = "stale"
            return "UPDATE 1"
        if compact.startswith("UPDATE observations SET confidence = CASE confidence"):
            now = args[0]
            for observation in self.observations.values():
                last = observation.get("last_reinforced_at") or observation.get("created_at")
                if (
                    observation.get("status") == "active"
                    and last is not None
                    and last <= now - timedelta(days=91)
                    and last > now - timedelta(days=183)
                    and observation.get("confidence") in {"high", "medium"}
                ):
                    observation["confidence"] = "medium" if observation["confidence"] == "high" else "low"
            return "UPDATE 1"
        if compact.startswith("UPDATE watch_items SET status = 'expired'"):
            now = args[0]
            for item in self.watch_items.values():
                if (
                    item.get("status") == "open"
                    and item.get("due_at") is not None
                    and item.get("addressed_at") is None
                    and item["due_at"] <= now - timedelta(days=30)
                ):
                    item["status"] = "expired"
            return "UPDATE 1"
        if compact.startswith("UPDATE messages SET content='[deleted]'"):
            content_encrypted = args[0] if args else None
            for message in self.messages.values():
                if message["deleted_at"] is not None and message["content"] != "[deleted]":
                    message["content"] = "[deleted]"
                    if content_encrypted is not None:
                        message["content_encrypted"] = content_encrypted
            return "UPDATE 1"
        if compact.startswith("UPDATE bot_turns SET failure_reason='crashed_after_send'"):
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
        if compact.startswith("UPDATE bot_turns SET final_output_message_id=$1 WHERE id=$2"):
            final_output_message_id, turn_id = args
            self.bot_turns[turn_id]["final_output_message_id"] = final_output_message_id
            return "UPDATE 1"
        if compact.startswith("UPDATE bot_turns SET final_output_message_id"):
            final_output_message_id, reasoning, reasoning_encrypted, duration_ms, tool_call_count, turn_id = args
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
        if compact.startswith("UPDATE feedback SET resolution = 'open'"):
            try:
                feedback_id = UUID(args[0]) if isinstance(args[0], str) else args[0]
            except (ValueError, TypeError):
                feedback_id = args[0]
            row = self.feedback.get(feedback_id)
            if row is None:
                return "UPDATE 0"
            row["resolution"] = "open"
            row["resolved_at"] = None
            row["resolution_note"] = None
            return "UPDATE 1"
        if compact.startswith("UPDATE feedback SET resolution = $1"):
            new_resolution, note, raw_id = args
            try:
                feedback_id = UUID(raw_id) if isinstance(raw_id, str) else raw_id
            except (ValueError, TypeError):
                feedback_id = raw_id
            row = self.feedback.get(feedback_id)
            if row is None:
                return "UPDATE 0"
            row["resolution"] = new_resolution
            row["resolved_at"] = datetime.now(UTC)
            row["resolution_note"] = note
            return "UPDATE 1"
        if compact.startswith("INSERT INTO tool_calls"):
            turn_id, tool_name, arguments, result, called_at, duration_ms = args
            self.tool_calls.append(
                {
                    "turn_id": turn_id,
                    "tool_name": tool_name,
                    "arguments": json.loads(arguments),
                    "result": json.loads(result),
                    "called_at": called_at,
                    "duration_ms": duration_ms,
                }
            )
            return "INSERT 0 1"
        if compact.startswith("DELETE FROM llm_spend_log"):
            self.llm_spend_log.clear()
            return "DELETE 0"
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
async def async_client(app_env: None, fake_asyncpg: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
