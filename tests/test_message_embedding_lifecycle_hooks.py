from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.user import User
from app.services.embeddings import (
    canonical_content_hash,
    canonical_conversation_note_embedding_text,
    content_hash,
)
from app.services import hooks
from app.services import message_embedding_lifecycle as lifecycle
from app.services.live import artifacts as live_artifacts
from app.services.live import provenance as live_provenance
from app.services.inbound import process_inbound
from app.services.messaging import send_outbound
from app.services.scope import InboundScope
from app.services.tools import read_tools
from app.services.tools import write_tools
from app.services.turn_context import TurnContext
from tool_schemas import (
    AddDistillationInput,
    AddMemoryInput,
    DeleteOutboundMessageInput,
    EditOutboundMessageInput,
    LogObservationInput,
    ExplainMediaItemInput,
    SearchMessagesInput,
    SupersedeMemoryInput,
    ReviseDistillationInput,
    UpdateDistillationInput,
    UpdateObservationInput,
    UpdateMemoryInput,
)


pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def reset_hooks():
    hooks.check_oob = None


def _forbid_provider_calls(monkeypatch) -> None:
    async def fail_embedder_use(*args, **kwargs):
        raise AssertionError("write paths must not call embedding providers")

    monkeypatch.setattr("app.services.embeddings.embedder_from_settings", fail_embedder_use)
    monkeypatch.setattr("app.services.embeddings.OpenAIEmbedder.embed_texts", fail_embedder_use)
    monkeypatch.setattr("app.services.embeddings.LocalBgeSmallEmbedder.embed_texts", fail_embedder_use)
    monkeypatch.setattr("app.services.embeddings.DeterministicFakeEmbedder.embed_texts", fail_embedder_use)


def _user(fake_pool) -> User:
    row = {"id": uuid4(), "name": "Maya", "phone": "15555550100", "timezone": "UTC"}
    fake_pool.users[row["id"]] = row
    return User(**row)


def _scope(user: User) -> InboundScope:
    return InboundScope(
        bot_id="mediator",
        transport="discord",
        user_id=user.id,
        topic_id=uuid4(),
        channel_id=None,
        binding_id=uuid4(),
        dyad_id=uuid4(),
    )


def _turn_ctx(fake_pool, user: User, partner: User) -> TurnContext:
    return TurnContext(
        turn_id=uuid4(),
        pool=fake_pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        turn_started_at=datetime.now(UTC),
        current_step="respond",
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=uuid4(),
        dyad_id=uuid4(),
    )


def _partner(fake_pool) -> User:
    row = {"id": uuid4(), "name": "Noor", "phone": "15555550101", "timezone": "UTC"}
    fake_pool.users[row["id"]] = row
    return User(**row)


def _outbound_row(fake_pool, *, user: User, content: str = "original text") -> object:
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": user.id,
        "content": content,
        "content_encrypted": None,
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "whatsapp_message_id": "discord-message-1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
        "bot_id": "mediator",
        "topic_id": None,
    }
    return message_id


def _payload(sender: str, wa_id: str, content: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": sender, "profile": {"name": "Maya"}}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": wa_id,
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "type": "text",
                                    "text": {"body": content},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


class _ArtifactConn:
    def __init__(self) -> None:
        self.artifact_id = uuid4()
        self.commands: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *args):
        self.commands.append((sql, args))
        compact = " ".join(sql.split())
        if compact.startswith("UPDATE mediator.conversation_artifacts"):
            return "UPDATE 1"
        if compact.startswith("UPDATE mediator.artifact_links"):
            return "UPDATE 1"
        return "OK"

    async def fetchrow(self, sql: str, *args):
        self.commands.append((sql, args))
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO mediator.conversation_artifacts"):
            return {
                "id": self.artifact_id,
                "conversation_id": args[0],
                "bot_id": args[1],
                "user_id": args[2],
                "artifact_type": args[3],
                "payload": args[4],
                "payload_version": args[5],
                "revision_number": 1,
                "created_by_turn_id": args[6],
                "deleted_at": None,
                "expires_at": args[7],
                "created_at": datetime.now(UTC),
            }
        if compact.startswith("UPDATE mediator.conversation_artifacts"):
            return {
                "id": args[0],
                "conversation_id": str(uuid4()),
                "bot_id": "mediator",
                "user_id": str(uuid4()),
                "artifact_type": "live_debrief",
                "payload": args[1],
                "payload_version": 2,
                "revision_number": 1,
                "created_by_turn_id": "turn-1",
                "deleted_at": None,
                "expires_at": None,
                "created_at": datetime.now(UTC),
            }
        raise AssertionError(f"unexpected fetchrow: {compact}")

    async def fetch(self, sql: str, *args):
        self.commands.append((sql, args))
        return [{"id": self.artifact_id}]


def _edit_payload(sender: str, target_wa_id: str, content: str) -> dict:
    payload = _payload(sender, f"edit.{target_wa_id}", content)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["context"] = {"message_id": target_wa_id}
    return payload


def _delete_payload(sender: str, target_wa_id: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": sender, "profile": {"name": "Maya"}}],
                            "errors": [{"code": 131051, "message_id": target_wa_id}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": f"delete.{target_wa_id}",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "type": "unsupported",
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


async def test_content_lifecycle_wrappers_enqueue_by_source_identity(monkeypatch, app_env) -> None:
    source_id = uuid4()
    calls: list[tuple[str, str, object, str | None, str]] = []

    async def record_embed_job(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        calls.append(("embed", source_type, source_id, message_id, content_hash))

    async def record_reembed_job(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        calls.append(("reembed", source_type, source_id, message_id, content_hash))

    async def record_drop_job(pool, *, source_type, source_id, message_id=None):
        calls.append(("drop", source_type, source_id, message_id, ""))

    monkeypatch.setattr(lifecycle, "enqueue_embed_job", record_embed_job)
    monkeypatch.setattr(lifecycle, "enqueue_reembed_job", record_reembed_job)
    monkeypatch.setattr(lifecycle, "enqueue_drop_embedding_job", record_drop_job)

    await lifecycle.enqueue_content_embed(
        object(),
        source_type="memory",
        source_id=source_id,
        content_hash=canonical_content_hash("memory text"),
    )
    await lifecycle.enqueue_content_reembed(
        object(),
        source_type="memory",
        source_id=source_id,
        content_hash=canonical_content_hash("changed memory text"),
    )
    await lifecycle.enqueue_content_embedding_drop(object(), source_type="memory", source_id=source_id)

    assert calls == [
        ("embed", "memory", source_id, None, canonical_content_hash("memory text")),
        ("reembed", "memory", source_id, None, canonical_content_hash("changed memory text")),
        ("drop", "memory", source_id, None, ""),
    ]


async def test_content_lifecycle_wrappers_accept_deferred_source_types(monkeypatch, app_env) -> None:
    source_id = uuid4()
    calls: list[tuple[str, str, object, str | None, str]] = []

    async def record_embed_job(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        calls.append(("embed", source_type, source_id, message_id, content_hash))

    async def record_reembed_job(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        calls.append(("reembed", source_type, source_id, message_id, content_hash))

    async def record_drop_job(pool, *, source_type, source_id, message_id=None):
        calls.append(("drop", source_type, source_id, message_id, ""))

    monkeypatch.setattr(lifecycle, "enqueue_embed_job", record_embed_job)
    monkeypatch.setattr(lifecycle, "enqueue_reembed_job", record_reembed_job)
    monkeypatch.setattr(lifecycle, "enqueue_drop_embedding_job", record_drop_job)

    await lifecycle.enqueue_content_embed(
        object(),
        source_type="conversation_note",
        source_id=source_id,
        content_hash=content_hash("note text"),
    )
    await lifecycle.enqueue_content_reembed(
        object(),
        source_type="theme",
        source_id=source_id,
        content_hash=content_hash("theme text"),
    )
    await lifecycle.enqueue_content_embedding_drop(
        object(),
        source_type="theme",
        source_id=source_id,
    )

    assert calls == [
        ("embed", "conversation_note", source_id, None, content_hash("note text")),
        ("reembed", "theme", source_id, None, content_hash("theme text")),
        ("drop", "theme", source_id, None, ""),
    ]


async def test_content_lifecycle_wrappers_log_and_swallow_enqueue_failures(
    monkeypatch,
    caplog,
    app_env,
) -> None:
    async def fail_enqueue(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(lifecycle, "enqueue_embed_job", fail_enqueue)
    with caplog.at_level("ERROR", logger="app.services.message_embedding_lifecycle"):
        await lifecycle.enqueue_content_embed(
            object(),
            source_type="observation",
            source_id=uuid4(),
            content_hash=canonical_content_hash("observation text"),
        )

    assert "failed to enqueue embed job for source_type=observation" in caplog.text


async def test_artifact_create_enqueue_embed_for_non_empty_canonical_text(monkeypatch, app_env) -> None:
    conn = _ArtifactConn()
    calls: list[tuple[str, object, str]] = []

    async def record_embed(pool, *, source_type, source_id, content_hash, message_id=None):
        calls.append((source_type, source_id, content_hash))

    monkeypatch.setattr(live_artifacts, "enqueue_content_embed", record_embed)
    _forbid_provider_calls(monkeypatch)

    artifact = await live_artifacts.create_artifact(
        conn,
        conversation_id=str(uuid4()),
        bot_id="mediator",
        user_id=str(uuid4()),
        artifact_type="review_summary",
        payload={"review_summary": "Clear summary."},
        created_by_turn_id=str(uuid4()),
    )

    assert calls == [("artifact", artifact.id, content_hash("Clear summary."))]


async def test_artifact_create_skips_empty_canonical_text(monkeypatch, app_env) -> None:
    conn = _ArtifactConn()

    async def fail_embed(*args, **kwargs):
        raise AssertionError("empty artifact canonical text must not enqueue embed")

    monkeypatch.setattr(live_artifacts, "enqueue_content_embed", fail_embed)
    _forbid_provider_calls(monkeypatch)

    await live_artifacts.create_artifact(
        conn,
        conversation_id=str(uuid4()),
        bot_id="mediator",
        user_id=str(uuid4()),
        artifact_type="live_debrief",
        payload={"status": "provisional"},
        created_by_turn_id=str(uuid4()),
    )


async def test_artifact_finalize_and_failure_enqueue_reembed_and_drop(monkeypatch, app_env) -> None:
    conn = _ArtifactConn()
    artifact_id = uuid4()
    calls: list[tuple[str, object, str | None]] = []

    async def record_reembed(pool, *, source_type, source_id, content_hash, message_id=None):
        calls.append(("reembed", source_id, content_hash))

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        calls.append(("drop", source_id, None))

    monkeypatch.setattr(live_provenance, "enqueue_content_reembed", record_reembed)
    monkeypatch.setattr(live_provenance, "enqueue_content_embedding_drop", record_drop)
    _forbid_provider_calls(monkeypatch)

    await live_provenance.finalize_live_debrief_artifact(
        conn,
        artifact_id=str(artifact_id),
        content={"review_summary": "Debrief summary."},
        created_by_turn_id=str(uuid4()),
    )
    await live_provenance.mark_live_debrief_artifact_failed(
        conn,
        artifact_id=str(artifact_id),
        reason="failed",
    )
    await live_provenance._tombstone_stale_provisionals(conn, str(uuid4()))

    assert calls == [
        ("reembed", str(artifact_id), content_hash("Debrief summary.")),
        ("drop", str(artifact_id), None),
        ("drop", conn.artifact_id, None),
    ]


async def test_inbound_insert_edit_and_delete_enqueue_lifecycle_jobs(fake_pool, monkeypatch, app_env) -> None:
    calls: list[tuple[str, object, str | None, str | None]] = []

    async def record_embed_job(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        assert source_type == "message"
        assert message_id == source_id
        calls.append(("embed", message_id, content_hash, None))

    async def record_reembed_job(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        assert source_type == "message"
        assert message_id == source_id
        calls.append(("reembed", message_id, content_hash, None))

    async def record_drop_job(pool, *, source_type, source_id, message_id=None):
        assert source_type == "message"
        assert message_id == source_id
        calls.append(("drop", message_id, None, None))

    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_embed_job", record_embed_job)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_reembed_job", record_reembed_job)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_drop_embedding_job", record_drop_job)
    _forbid_provider_calls(monkeypatch)

    async def classify_charge(pool, content):
        return type("Charge", (), {"charge": "routine"})()

    monkeypatch.setattr("app.services.inbound.classify_charge", classify_charge)

    first = await process_inbound(
        fake_pool,
        _payload("15555550100", "wamid.lifecycle", "first text"),
        transport="whatsapp",
        bot_id="mediator",
    )
    duplicate = await process_inbound(
        fake_pool,
        _payload("15555550100", "wamid.lifecycle", "first text"),
        transport="whatsapp",
        bot_id="mediator",
    )
    await process_inbound(
        fake_pool,
        _edit_payload("15555550100", "wamid.lifecycle", "first text"),
        transport="whatsapp",
        bot_id="mediator",
    )
    await process_inbound(
        fake_pool,
        _edit_payload("15555550100", "wamid.lifecycle", "changed text"),
        transport="whatsapp",
        bot_id="mediator",
    )
    await process_inbound(
        fake_pool,
        _delete_payload("15555550100", "wamid.lifecycle"),
        transport="whatsapp",
        bot_id="mediator",
    )

    message_id = next(iter(fake_pool.messages))
    first_hash = canonical_content_hash("first text")
    changed_hash = canonical_content_hash("changed text")
    assert first.inserted == 1
    assert duplicate.skipped_existing == 1
    assert calls == [
        ("embed", message_id, first_hash, None),
        ("reembed", message_id, changed_hash, None),
        ("drop", message_id, None, None),
    ]


async def test_send_outbound_preserves_oob_return_shape_and_enqueues_after_row_creation(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    user = _user(fake_pool)
    inbound_id = uuid4()
    fake_pool.messages[inbound_id] = {
        "id": inbound_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "hi",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC) - timedelta(minutes=5),
        "charge": None,
        "whatsapp_message_id": "inbound",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
    }
    calls: list[tuple[object, str | None, str | None]] = []

    async def record_embed_job(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        assert source_type == "message"
        assert message_id == source_id
        calls.append((message_id, content_hash, None))

    async def block_oob(*args, **kwargs):
        return {
            "verdict": "block",
            "reason": "private",
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_embed_job", record_embed_job)
    _forbid_provider_calls(monkeypatch)
    hooks.check_oob = block_oob

    result = await send_outbound(fake_pool, user, "blocked text", scope=_scope(user))

    assert result == {
        "status": "blocked",
        "message_id": calls[0][0],
        "visible_to_user": False,
        "provider_message_id": None,
    }
    assert calls == [(result["message_id"], canonical_content_hash("blocked text"), None)]
    assert fake_pool.messages[result["message_id"]]["processing_state"] == "withheld"


async def test_memory_create_update_supersede_enqueue_from_post_write_state(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    calls: list[tuple[str, object, str | None]] = []

    async def record_embed(pool, *, source_type, source_id, content_hash, message_id=None):
        assert source_type == "memory"
        calls.append(("embed", source_id, content_hash))

    async def record_reembed(pool, *, source_type, source_id, content_hash, message_id=None):
        assert source_type == "memory"
        calls.append(("reembed", source_id, content_hash))

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        assert source_type == "memory"
        calls.append(("drop", source_id, None))

    monkeypatch.setattr(write_tools, "enqueue_content_embed", record_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", record_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", record_drop)

    created = await write_tools.add_memory(
        ctx,
        AddMemoryInput(about_user_id=user.id, content="private memory"),
    )

    await write_tools.update_memory(
        ctx,
        UpdateMemoryInput(memory_id=created.id, content="changed memory"),
    )

    superseded = await write_tools.supersede_memory(
        ctx,
        SupersedeMemoryInput(old_memory_id=created.id, new_content="replacement memory"),
    )

    assert calls == [
        ("embed", created.id, content_hash("private memory")),
        ("reembed", created.id, content_hash("changed memory")),
        ("drop", created.id, None),
        ("embed", superseded.new_id, content_hash("replacement memory")),
    ]


async def test_memory_update_drops_when_post_write_state_leaves_searchable_content(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    memory_id = uuid4()
    states = [
        {"id": memory_id, "content": "inactive", "status": "inactive", "visibility": "private"},
        {"id": memory_id, "content": "shared", "status": "active", "visibility": "dyad_shareable"},
    ]
    calls: list[tuple[str, object]] = []

    async def fetch_state(pool, memory_id):
        return states.pop(0)

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        assert source_type == "memory"
        calls.append(("drop", source_id))

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("ineligible memory rows must enqueue drops, not reembeds")

    monkeypatch.setattr(write_tools, "_fetch_memory_embedding_state", fetch_state)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", record_drop)

    await write_tools._sync_memory_embedding_after_update(ctx, memory_id)
    await write_tools._sync_memory_embedding_after_update(ctx, memory_id)

    assert calls == [("drop", memory_id), ("drop", memory_id)]


async def test_observation_create_and_threshold_crossings_enqueue_from_post_write_state(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    calls: list[tuple[str, object, str | None]] = []

    async def record_embed(pool, *, source_type, source_id, content_hash, message_id=None):
        assert source_type == "observation"
        calls.append(("embed", source_id, content_hash))

    async def record_reembed(pool, *, source_type, source_id, content_hash, message_id=None):
        assert source_type == "observation"
        calls.append(("reembed", source_id, content_hash))

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        assert source_type == "observation"
        calls.append(("drop", source_id, None))

    monkeypatch.setattr(write_tools, "enqueue_content_embed", record_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", record_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", record_drop)

    created = await write_tools.log_observation(
        ctx,
        LogObservationInput(
            about_user_id=user.id,
            content="important observation",
            confidence="medium",
            significance=4,
        ),
    )

    await write_tools.update_observation(
        ctx,
        UpdateObservationInput(observation_id=created.id, significance=2),
    )

    await write_tools.update_observation(
        ctx,
        UpdateObservationInput(
            observation_id=created.id,
            content="important observation revised",
            significance=5,
        ),
    )

    await write_tools.update_observation(
        ctx,
        UpdateObservationInput(observation_id=created.id, status="contradicted"),
    )

    assert calls == [
        ("embed", created.id, content_hash("important observation")),
        ("drop", created.id, None),
        ("reembed", created.id, content_hash("important observation revised")),
        ("drop", created.id, None),
    ]


async def test_observation_create_and_update_skip_or_drop_when_not_searchable(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    observation_id = uuid4()
    fake_pool.observations[observation_id] = {
        "id": observation_id,
        "content": "low-signal observation",
        "about_user_id": user.id,
        "confidence": "medium",
        "significance": 1,
        "status": "active",
    }
    calls: list[tuple[str, object]] = []

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        assert source_type == "observation"
        calls.append(("drop", source_id))

    async def fail_enqueue(*args, **kwargs):
        raise AssertionError("non-searchable observations must not enqueue embeds/reembeds")

    monkeypatch.setattr(write_tools, "enqueue_content_embed", fail_enqueue)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", fail_enqueue)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", record_drop)

    await write_tools._sync_observation_embedding_after_create(ctx, observation_id)

    fake_pool.observations[observation_id]["status"] = "stale"
    await write_tools.update_observation(
        ctx,
        UpdateObservationInput(observation_id=observation_id, status="stale"),
    )

    assert calls == [("drop", observation_id)]


async def test_distillation_create_update_and_revise_enqueue_from_post_write_state(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    ctx.triggering_message_ids = [uuid4()]
    fake_pool.messages[ctx.triggering_message_ids[0]] = {
        "id": ctx.triggering_message_ids[0],
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": partner.id,
        "content": "supporting message",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "whatsapp_message_id": "distillation-support-1",
        "deleted_at": None,
        "bot_id": ctx.bot_id,
        "topic_id": ctx.primary_topic_id,
    }
    calls: list[tuple[str, object, str | None]] = []

    async def record_embed(pool, *, source_type, source_id, content_hash, message_id=None):
        assert source_type == "distillation"
        calls.append(("embed", source_id, content_hash))

    async def record_reembed(pool, *, source_type, source_id, content_hash, message_id=None):
        assert source_type == "distillation"
        calls.append(("reembed", source_id, content_hash))

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        assert source_type == "distillation"
        calls.append(("drop", source_id, None))

    monkeypatch.setattr(write_tools, "enqueue_content_embed", record_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", record_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", record_drop)

    created = await write_tools.add_distillation(
        ctx,
        AddDistillationInput(
            content="private distillation",
            source_user_ids=[user.id],
            supporting_message_ids=[ctx.triggering_message_ids[0]],
        ),
    )

    await write_tools.update_distillation(
        ctx,
        UpdateDistillationInput(
            distillation_id=created.id,
            content="private distillation revised in place",
        ),
    )

    revised = await write_tools.revise_distillation(
        ctx,
        ReviseDistillationInput(
            old_distillation_id=created.id,
            new_content="replacement distillation",
            source_user_ids=[user.id],
            supporting_message_ids=[ctx.triggering_message_ids[0]],
            revision_note="newer evidence",
        ),
    )

    assert calls == [
        ("embed", created.id, content_hash("private distillation")),
        ("reembed", created.id, content_hash("private distillation revised in place")),
        ("drop", created.id, None),
        ("embed", revised.new_id, content_hash("replacement distillation")),
    ]


async def test_distillation_update_drops_when_post_write_state_leaves_searchable_content(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    supporting_message_id = uuid4()
    fake_pool.messages[supporting_message_id] = {
        "id": supporting_message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": partner.id,
        "content": "supporting message",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "whatsapp_message_id": "distillation-support-2",
        "deleted_at": None,
        "bot_id": ctx.bot_id,
        "topic_id": ctx.primary_topic_id,
    }
    calls: list[tuple[str, object]] = []

    async def record_embed(pool, *, source_type, source_id, content_hash, message_id=None):
        assert source_type == "distillation"
        calls.append(("embed", source_id))

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        assert source_type == "distillation"
        calls.append(("drop", source_id))

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("non-searchable distillations must enqueue drops, not reembeds")

    monkeypatch.setattr(write_tools, "enqueue_content_embed", record_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", record_drop)

    created = await write_tools.add_distillation(
        ctx,
        AddDistillationInput(
            content="private now, shared later",
            source_user_ids=[user.id],
            supporting_message_ids=[supporting_message_id],
        ),
    )

    await write_tools.update_distillation(
        ctx,
        UpdateDistillationInput(
            distillation_id=created.id,
            visibility="dyad_shareable",
            shareable_summary="partner-safe wording",
        ),
    )

    await write_tools.update_distillation(
        ctx,
        UpdateDistillationInput(distillation_id=created.id, status="retired"),
    )

    assert calls == [
        ("embed", created.id),
        ("drop", created.id),
        ("drop", created.id),
    ]


async def test_tool_outbound_edit_delete_and_media_explanation_enqueue_lifecycle_jobs(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    ctx.primary_topic_id = uuid4()
    message_id = _outbound_row(fake_pool, user=user)
    fake_pool.messages[message_id]["topic_id"] = ctx.primary_topic_id

    calls: list[tuple[str, object, str | None]] = []

    async def record_reembed_job(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        assert source_type == "message"
        assert message_id == source_id
        calls.append(("reembed", message_id, content_hash))

    async def record_drop_job(pool, *, source_type, source_id, message_id=None):
        assert source_type == "message"
        assert message_id == source_id
        calls.append(("drop", message_id, None))

    async def fake_edit_text(*args, **kwargs):
        return None

    async def fake_delete_text(*args, **kwargs):
        return None

    async def fake_explain_stored_image(pool, message_id):
        return {"explanation": "A diagram showing the changed plan."}

    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_reembed_job", record_reembed_job)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_drop_embedding_job", record_drop_job)
    monkeypatch.setattr("app.services.tools.write_tools.discord.edit_text", fake_edit_text)
    monkeypatch.setattr("app.services.tools.write_tools.discord.delete_text", fake_delete_text)
    monkeypatch.setattr("app.services.tools.write_tools.explain_stored_image", fake_explain_stored_image)
    _forbid_provider_calls(monkeypatch)

    edited = await write_tools.edit_outbound_message(
        ctx,
        EditOutboundMessageInput(
            message_id=str(message_id),
            content="edited text",
            reason="fix typo",
        ),
    )
    fake_pool.messages[message_id]["media_type"] = "image"
    fake_pool.messages[message_id]["media_url"] = "s3://bucket/image.png"
    explained = await write_tools.explain_media_item(
        ctx,
        ExplainMediaItemInput(message_id=str(message_id), reason="needs durable explanation"),
    )
    deleted = await write_tools.delete_outbound_message(
        ctx,
        DeleteOutboundMessageInput(message_id=str(message_id), reason="cleanup"),
    )

    assert edited.action == "edited"
    assert explained.action == "explained"
    assert deleted.action == "deleted"
    assert calls == [
        ("reembed", message_id, canonical_content_hash("edited text")),
        (
            "reembed",
            message_id,
            canonical_content_hash(
                "edited text",
                {"explanation": "A diagram showing the changed plan."},
            ),
        ),
        ("drop", message_id, None),
    ]


async def test_cross_path_enqueue_and_search_suppression_contract(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    topic_id = uuid4()
    ctx = _turn_ctx(fake_pool, user, partner)
    ctx.primary_topic_id = topic_id
    calls: list[tuple[str, object, str | None]] = []

    async def record_embed_job(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        assert source_type == "message"
        assert message_id == source_id
        calls.append(("embed", message_id, content_hash))

    async def record_reembed_job(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        assert source_type == "message"
        assert message_id == source_id
        calls.append(("reembed", message_id, content_hash))

    async def record_drop_job(pool, *, source_type, source_id, message_id=None):
        assert source_type == "message"
        assert message_id == source_id
        calls.append(("drop", message_id, None))

    async def classify_charge(pool, content):
        return type("Charge", (), {"charge": "routine"})()

    async def block_oob(*args, **kwargs):
        return {
            "verdict": "block",
            "reason": "private",
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    async def fake_edit_text(*args, **kwargs):
        return None

    async def fake_delete_text(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.inbound.classify_charge", classify_charge)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_embed_job", record_embed_job)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_reembed_job", record_reembed_job)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_drop_embedding_job", record_drop_job)
    monkeypatch.setattr("app.services.tools.write_tools.discord.edit_text", fake_edit_text)
    monkeypatch.setattr("app.services.tools.write_tools.discord.delete_text", fake_delete_text)
    _forbid_provider_calls(monkeypatch)

    inbound = await process_inbound(
        fake_pool,
        _payload("15555550100", "wamid.cross-path", "inbound stable needle"),
        transport="whatsapp",
        bot_id="mediator",
    )
    inbound_message_id = next(
        message_id
        for message_id, row in fake_pool.messages.items()
        if row.get("whatsapp_message_id") == "wamid.cross-path"
    )
    await process_inbound(
        fake_pool,
        _edit_payload("15555550100", "wamid.cross-path", "inbound edited needle"),
        transport="whatsapp",
        bot_id="mediator",
    )
    await process_inbound(
        fake_pool,
        _delete_payload("15555550100", "wamid.cross-path"),
        transport="whatsapp",
        bot_id="mediator",
    )

    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    hooks.check_oob = block_oob
    outbound = await send_outbound(fake_pool, user, "blocked outbound needle", scope=_scope(user))
    hooks.check_oob = None

    visible_tool_message_id = _outbound_row(fake_pool, user=user, content="visible tool needle")
    suppressed_tool_message_id = _outbound_row(fake_pool, user=user, content="suppressed tool needle")
    for message_id in (visible_tool_message_id, suppressed_tool_message_id):
        fake_pool.messages[message_id]["topic_id"] = topic_id
    await write_tools.edit_outbound_message(
        ctx,
        EditOutboundMessageInput(
            message_id=str(visible_tool_message_id),
            content="visible tool edited needle",
            reason="integration edit",
        ),
    )
    fake_pool.messages[suppressed_tool_message_id]["search_suppressed_at"] = datetime.now(UTC)

    first_search = await read_tools.search_messages(
        ctx, SearchMessagesInput(text_contains="tool", limit=10)
    )
    second_search = await read_tools.search_messages(
        ctx, SearchMessagesInput(text_contains="tool", limit=10)
    )
    await write_tools.delete_outbound_message(
        ctx,
        DeleteOutboundMessageInput(
            message_id=str(visible_tool_message_id),
            reason="integration delete",
        ),
    )

    assert inbound.inserted == 1
    assert outbound["status"] == "blocked"
    assert [hit.id for hit in first_search.hits] == [visible_tool_message_id]
    assert [hit.id for hit in second_search.hits] == [visible_tool_message_id]
    assert suppressed_tool_message_id not in [hit.id for hit in second_search.hits]
    assert calls == [
        ("embed", inbound_message_id, canonical_content_hash("inbound stable needle")),
        ("reembed", inbound_message_id, canonical_content_hash("inbound edited needle")),
        ("drop", inbound_message_id, None),
        ("embed", outbound["message_id"], canonical_content_hash("blocked outbound needle")),
        ("reembed", visible_tool_message_id, canonical_content_hash("visible tool edited needle")),
        ("drop", visible_tool_message_id, None),
    ]


# ---------------------------------------------------------------------------
# Theme lifecycle tests (T8)
# ---------------------------------------------------------------------------


async def test_theme_create_enqueues_embed_when_searchable(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    """Theme create enqueues embed when status=active, has active topic, and non-empty canonical text."""
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    theme_id = uuid4()
    topic_id = ctx.primary_topic_id

    # Build a searchable theme row: active status, has topic, non-empty title+description.
    theme_row = {
        "id": theme_id,
        "title": "Communication gap",
        "description": "Partner avoids difficult conversations.",
        "status": "active",
        "recorded_by_bot_id": "mediator",
        "_has_active_topic": True,
    }

    async def fetch_state(pool, tid):
        assert tid == theme_id
        return theme_row

    calls: list[tuple[str, object, str | None]] = []

    async def record_embed(pool, *, source_type, source_id, content_hash, message_id=None):
        assert source_type == "theme"
        calls.append(("embed", source_id, content_hash))

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("searchable theme create must not enqueue reembed")

    async def fail_drop(*args, **kwargs):
        raise AssertionError("searchable theme create must not enqueue drop")

    monkeypatch.setattr(write_tools, "_fetch_theme_embedding_state", fetch_state)
    monkeypatch.setattr(write_tools, "enqueue_content_embed", record_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", fail_drop)

    await write_tools._sync_theme_embedding_after_create(ctx, theme_id)

    expected_hash = write_tools._theme_content_hash(theme_row)
    assert calls == [("embed", theme_id, expected_hash)]


async def test_theme_update_reembeds_when_still_searchable(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    """Theme update reembeds when post-update state is still searchable."""
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    theme_id = uuid4()

    theme_row = {
        "id": theme_id,
        "title": "Updated theme",
        "description": "Revised description.",
        "status": "active",
        "recorded_by_bot_id": "mediator",
        "_has_active_topic": True,
    }

    async def fetch_state(pool, tid):
        assert tid == theme_id
        return theme_row

    calls: list[tuple[str, object, str | None]] = []

    async def record_reembed(pool, *, source_type, source_id, content_hash, message_id=None):
        assert source_type == "theme"
        calls.append(("reembed", source_id, content_hash))

    async def fail_embed(*args, **kwargs):
        raise AssertionError("searchable theme update must not enqueue embed")

    async def fail_drop(*args, **kwargs):
        raise AssertionError("searchable theme update must not enqueue drop")

    monkeypatch.setattr(write_tools, "_fetch_theme_embedding_state", fetch_state)
    monkeypatch.setattr(write_tools, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", record_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", fail_drop)

    await write_tools._sync_theme_embedding_after_update(ctx, theme_id)

    expected_hash = write_tools._theme_content_hash(theme_row)
    assert calls == [("reembed", theme_id, expected_hash)]


async def test_theme_update_drops_when_inactive(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    """Theme update enqueues drop when status is not 'active'."""
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    theme_id = uuid4()

    # Theme became dormant — not searchable.
    theme_row = {
        "id": theme_id,
        "title": "Resolved theme",
        "description": "No longer active.",
        "status": "dormant",
        "recorded_by_bot_id": "mediator",
        "_has_active_topic": True,
    }

    async def fetch_state(pool, tid):
        assert tid == theme_id
        return theme_row

    calls: list[tuple[str, object]] = []

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        assert source_type == "theme"
        calls.append(("drop", source_id))

    async def fail_embed(*args, **kwargs):
        raise AssertionError("inactive theme must not enqueue embed")

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("inactive theme must not enqueue reembed")

    monkeypatch.setattr(write_tools, "_fetch_theme_embedding_state", fetch_state)
    monkeypatch.setattr(write_tools, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", record_drop)

    await write_tools._sync_theme_embedding_after_update(ctx, theme_id)

    assert calls == [("drop", theme_id)]


async def test_theme_update_drops_when_hidden_topic(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    """Theme update enqueues drop when no active artifact_topic link exists."""
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    theme_id = uuid4()

    # Theme has active status but no active topic link.
    theme_row = {
        "id": theme_id,
        "title": "Orphan theme",
        "description": "No topic assigned.",
        "status": "active",
        "recorded_by_bot_id": "mediator",
        "_has_active_topic": False,
    }

    async def fetch_state(pool, tid):
        assert tid == theme_id
        return theme_row

    calls: list[tuple[str, object]] = []

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        assert source_type == "theme"
        calls.append(("drop", source_id))

    async def fail_embed(*args, **kwargs):
        raise AssertionError("hidden-topic theme must not enqueue embed")

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("hidden-topic theme must not enqueue reembed")

    monkeypatch.setattr(write_tools, "_fetch_theme_embedding_state", fetch_state)
    monkeypatch.setattr(write_tools, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", record_drop)

    await write_tools._sync_theme_embedding_after_update(ctx, theme_id)

    assert calls == [("drop", theme_id)]


async def test_theme_create_skips_when_empty_canonical_text(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    """Theme create skips enqueue when canonical text (title + description) is empty."""
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    theme_id = uuid4()

    # Both title and description are empty/None — _theme_is_searchable returns False.
    theme_row = {
        "id": theme_id,
        "title": "",
        "description": None,
        "status": "active",
        "recorded_by_bot_id": "mediator",
        "_has_active_topic": True,
    }

    async def fetch_state(pool, tid):
        assert tid == theme_id
        return theme_row

    async def fail_embed(*args, **kwargs):
        raise AssertionError("empty-canonical-text theme create must not enqueue embed")

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("empty-canonical-text theme create must not enqueue reembed")

    async def fail_drop(*args, **kwargs):
        raise AssertionError("empty-canonical-text theme create must not enqueue drop")

    monkeypatch.setattr(write_tools, "_fetch_theme_embedding_state", fetch_state)
    monkeypatch.setattr(write_tools, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", fail_drop)

    # Should silently skip — no calls.
    await write_tools._sync_theme_embedding_after_create(ctx, theme_id)


async def test_theme_update_drops_when_empty_canonical_text(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    """Theme update enqueues drop when canonical text becomes empty."""
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    theme_id = uuid4()

    # Both title and description are empty/None.
    theme_row = {
        "id": theme_id,
        "title": "",
        "description": None,
        "status": "active",
        "recorded_by_bot_id": "mediator",
        "_has_active_topic": True,
    }

    async def fetch_state(pool, tid):
        assert tid == theme_id
        return theme_row

    calls: list[tuple[str, object]] = []

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        assert source_type == "theme"
        calls.append(("drop", source_id))

    async def fail_embed(*args, **kwargs):
        raise AssertionError("empty-canonical-text theme update must not enqueue embed")

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("empty-canonical-text theme update must not enqueue reembed")

    monkeypatch.setattr(write_tools, "_fetch_theme_embedding_state", fetch_state)
    monkeypatch.setattr(write_tools, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", record_drop)

    await write_tools._sync_theme_embedding_after_update(ctx, theme_id)

    assert calls == [("drop", theme_id)]


async def test_theme_create_skips_when_null_fetch_state(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    """Theme create skips enqueue when theme row cannot be fetched (returns None)."""
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    theme_id = uuid4()

    async def fetch_state(pool, tid):
        return None  # row not found

    async def fail_embed(*args, **kwargs):
        raise AssertionError("null-fetch theme create must not enqueue embed")

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("null-fetch theme create must not enqueue reembed")

    async def fail_drop(*args, **kwargs):
        raise AssertionError("null-fetch theme create must not enqueue drop")

    monkeypatch.setattr(write_tools, "_fetch_theme_embedding_state", fetch_state)
    monkeypatch.setattr(write_tools, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", fail_drop)

    await write_tools._sync_theme_embedding_after_create(ctx, theme_id)


async def test_theme_composed_status_and_topic_transitions(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    """Theme update transitions through searchable→inactive→searchable→hidden states."""
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    theme_id = uuid4()

    states = [
        # Start: active, has topic, non-empty — searchable → reembed
        {
            "id": theme_id,
            "title": "Active theme",
            "description": "Has content.",
            "status": "active",
            "recorded_by_bot_id": "mediator",
            "_has_active_topic": True,
        },
        # Become dormant — not searchable → drop
        {
            "id": theme_id,
            "title": "Dormant theme",
            "description": "Still has content but inactive.",
            "status": "dormant",
            "recorded_by_bot_id": "mediator",
            "_has_active_topic": True,
        },
        # Reactivate — searchable again → reembed
        {
            "id": theme_id,
            "title": "Reactivated theme",
            "description": "Back to active with updated content.",
            "status": "active",
            "recorded_by_bot_id": "mediator",
            "_has_active_topic": True,
        },
        # Lose topic — hidden → drop
        {
            "id": theme_id,
            "title": "Active but hidden",
            "description": "No topic.",
            "status": "active",
            "recorded_by_bot_id": "mediator",
            "_has_active_topic": False,
        },
    ]

    async def fetch_state(pool, tid):
        return states.pop(0)

    calls: list[tuple[str, object, str | None]] = []

    async def record_reembed(pool, *, source_type, source_id, content_hash, message_id=None):
        assert source_type == "theme"
        calls.append(("reembed", source_id, content_hash))

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        assert source_type == "theme"
        calls.append(("drop", source_id, None))

    async def fail_embed(*args, **kwargs):
        raise AssertionError("update path must only enqueue reembeds/drops")

    monkeypatch.setattr(write_tools, "_fetch_theme_embedding_state", fetch_state)
    monkeypatch.setattr(write_tools, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(write_tools, "enqueue_content_reembed", record_reembed)
    monkeypatch.setattr(write_tools, "enqueue_content_embedding_drop", record_drop)

    await write_tools._sync_theme_embedding_after_update(ctx, theme_id)
    await write_tools._sync_theme_embedding_after_update(ctx, theme_id)
    await write_tools._sync_theme_embedding_after_update(ctx, theme_id)
    await write_tools._sync_theme_embedding_after_update(ctx, theme_id)

    assert len(calls) == 4
    assert calls[0][0] == "reembed"  # active+topic+content
    assert calls[1][0] == "drop"     # dormant
    assert calls[2][0] == "reembed"  # reactivated
    assert calls[3][0] == "drop"     # no topic


# ---------------------------------------------------------------------------
# Conversation-note lifecycle tests (T11)
# ---------------------------------------------------------------------------


async def test_conversation_note_embed_nonempty_text(
    monkeypatch,
    app_env,
) -> None:
    """enqueue_conversation_note_embed with non-empty text calls enqueue_content_embed.

    Covers crisis insert (live_voice.py) and live-turn insert (turn_loop.py).
    """
    note_id = uuid4()
    note_text = "[decision] Use httpOnly cookies for auth tokens."

    calls: list[tuple[str, object, str]] = []

    async def record_embed(pool, *, source_type, source_id, content_hash, message_id=None):
        calls.append(("embed", source_id, content_hash))

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("non-empty note embed must not enqueue reembed")

    async def fail_drop(*args, **kwargs):
        raise AssertionError("non-empty note embed must not enqueue drop")

    monkeypatch.setattr(lifecycle, "enqueue_content_embed", record_embed)
    monkeypatch.setattr(lifecycle, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(lifecycle, "enqueue_content_embedding_drop", fail_drop)

    await lifecycle.enqueue_conversation_note_embed(
        object(), note_id=note_id, text=note_text,
    )

    expected_hash = content_hash(canonical_conversation_note_embedding_text(note_text))
    assert calls == [("embed", note_id, expected_hash)]


async def test_conversation_note_embed_empty_text_skips(
    monkeypatch,
    app_env,
) -> None:
    """enqueue_conversation_note_embed with empty/None text skips without any call."""
    note_id = uuid4()

    async def fail_embed(*args, **kwargs):
        raise AssertionError("empty note embed must not enqueue embed")

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("empty note embed must not enqueue reembed")

    async def fail_drop(*args, **kwargs):
        raise AssertionError("empty note embed must not enqueue drop")

    monkeypatch.setattr(lifecycle, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(lifecycle, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(lifecycle, "enqueue_content_embedding_drop", fail_drop)

    # None text
    await lifecycle.enqueue_conversation_note_embed(
        object(), note_id=note_id, text=None,
    )

    # Empty-string text
    await lifecycle.enqueue_conversation_note_embed(
        object(), note_id=note_id, text="",
    )

    # Whitespace-only text
    await lifecycle.enqueue_conversation_note_embed(
        object(), note_id=note_id, text="   \t\n",
    )


async def test_conversation_note_reembed_nonempty_text(
    monkeypatch,
    app_env,
) -> None:
    """enqueue_conversation_note_reembed with non-empty text calls enqueue_content_reembed.

    Covers synthesis update/rewrite.
    """
    note_id = uuid4()
    note_text = "[decision] Revised: monthly budget set at $4000."

    calls: list[tuple[str, object, str]] = []

    async def record_reembed(pool, *, source_type, source_id, content_hash, message_id=None):
        calls.append(("reembed", source_id, content_hash))

    async def fail_embed(*args, **kwargs):
        raise AssertionError("non-empty note reembed must not enqueue embed")

    async def fail_drop(*args, **kwargs):
        raise AssertionError("non-empty note reembed must not enqueue drop")

    monkeypatch.setattr(lifecycle, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(lifecycle, "enqueue_content_reembed", record_reembed)
    monkeypatch.setattr(lifecycle, "enqueue_content_embedding_drop", fail_drop)

    await lifecycle.enqueue_conversation_note_reembed(
        object(), note_id=note_id, text=note_text,
    )

    expected_hash = content_hash(canonical_conversation_note_embedding_text(note_text))
    assert calls == [("reembed", note_id, expected_hash)]


async def test_conversation_note_reembed_empty_text_drops(
    monkeypatch,
    app_env,
) -> None:
    """enqueue_conversation_note_reembed with empty text enqueues drop."""
    note_id = uuid4()

    calls: list[tuple[str, object]] = []

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        calls.append(("drop", source_id))

    async def fail_embed(*args, **kwargs):
        raise AssertionError("empty note reembed must not enqueue embed")

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("empty note reembed must not enqueue reembed")

    monkeypatch.setattr(lifecycle, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(lifecycle, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(lifecycle, "enqueue_content_embedding_drop", record_drop)

    await lifecycle.enqueue_conversation_note_reembed(
        object(), note_id=note_id, text="",
    )

    assert calls == [("drop", note_id)]


async def test_conversation_note_reembed_whitespace_only_drops(
    monkeypatch,
    app_env,
) -> None:
    """enqueue_conversation_note_reembed with whitespace-only text enqueues drop."""
    note_id = uuid4()

    calls: list[tuple[str, object]] = []

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        calls.append(("drop", source_id))

    async def fail_embed(*args, **kwargs):
        raise AssertionError("whitespace-only note reembed must not enqueue embed")

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("whitespace-only note reembed must not enqueue reembed")

    monkeypatch.setattr(lifecycle, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(lifecycle, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(lifecycle, "enqueue_content_embedding_drop", record_drop)

    await lifecycle.enqueue_conversation_note_reembed(
        object(), note_id=note_id, text="  \t\n ",
    )

    assert calls == [("drop", note_id)]


async def test_conversation_note_drop(
    monkeypatch,
    app_env,
) -> None:
    """enqueue_conversation_note_drop calls enqueue_content_embedding_drop.

    Covers synthesis delete/drop.
    """
    note_id = uuid4()

    calls: list[tuple[str, object]] = []

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        calls.append(("drop", source_id))

    async def fail_embed(*args, **kwargs):
        raise AssertionError("note drop must not enqueue embed")

    async def fail_reembed(*args, **kwargs):
        raise AssertionError("note drop must not enqueue reembed")

    monkeypatch.setattr(lifecycle, "enqueue_content_embed", fail_embed)
    monkeypatch.setattr(lifecycle, "enqueue_content_reembed", fail_reembed)
    monkeypatch.setattr(lifecycle, "enqueue_content_embedding_drop", record_drop)

    await lifecycle.enqueue_conversation_note_drop(
        object(), note_id=note_id,
    )

    assert calls == [("drop", note_id)]


async def test_conversation_note_embed_has_correct_source_type(
    monkeypatch,
    app_env,
) -> None:
    """enqueue_conversation_note_embed passes source_type='conversation_note'."""
    note_id = uuid4()
    note_text = "[fact] The CI pipeline is green again."

    calls: list[tuple[str, object, str, str]] = []

    async def record_embed(pool, *, source_type, source_id, content_hash, message_id=None):
        calls.append(("embed", source_type, source_id, content_hash))

    monkeypatch.setattr(lifecycle, "enqueue_content_embed", record_embed)

    await lifecycle.enqueue_conversation_note_embed(
        object(), note_id=note_id, text=note_text,
    )

    assert len(calls) == 1
    assert calls[0][1] == "conversation_note"
    assert calls[0][2] == note_id


async def test_conversation_note_lifecycle_full_cycle(
    monkeypatch,
    app_env,
) -> None:
    """Full lifecycle: create (embed) → update (reembed) → empty (drop)."""
    note_id = uuid4()

    calls: list[tuple[str, object, str | None]] = []

    async def record_embed(pool, *, source_type, source_id, content_hash, message_id=None):
        calls.append(("embed", source_id, content_hash))

    async def record_reembed(pool, *, source_type, source_id, content_hash, message_id=None):
        calls.append(("reembed", source_id, content_hash))

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        calls.append(("drop", source_id, None))

    monkeypatch.setattr(lifecycle, "enqueue_content_embed", record_embed)
    monkeypatch.setattr(lifecycle, "enqueue_content_reembed", record_reembed)
    monkeypatch.setattr(lifecycle, "enqueue_content_embedding_drop", record_drop)

    # Crisis insert: create with text.
    text_v1 = "[decision] Initial note text."
    await lifecycle.enqueue_conversation_note_embed(
        object(), note_id=note_id, text=text_v1,
    )

    # Synthesis update: revise text.
    text_v2 = "[decision] Updated note text after review."
    await lifecycle.enqueue_conversation_note_reembed(
        object(), note_id=note_id, text=text_v2,
    )

    # Synthesis delete: empty note.
    await lifecycle.enqueue_conversation_note_reembed(
        object(), note_id=note_id, text="",
    )

    assert len(calls) == 3
    assert calls[0][0] == "embed"
    assert calls[0][1] == note_id
    assert calls[0][2] == content_hash(canonical_conversation_note_embedding_text(text_v1))

    assert calls[1][0] == "reembed"
    assert calls[1][1] == note_id
    assert calls[1][2] == content_hash(canonical_conversation_note_embedding_text(text_v2))

    assert calls[2][0] == "drop"
    assert calls[2][1] == note_id
