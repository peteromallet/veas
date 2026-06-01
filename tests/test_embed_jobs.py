from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.services.embed_jobs import (
    enqueue_drop_embedding_job,
    enqueue_message_drop_embedding_job,
    enqueue_message_embed_job,
    enqueue_message_reembed_job,
    enqueue_embed_job,
    enqueue_reembed_job,
)

pytestmark = pytest.mark.anyio


class FakeEmbedJobsConn:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.executed_sql: list[str] = []

    async def execute(self, sql: str, *args):
        self.executed_sql.append(sql)
        compact = " ".join(sql.split())
        affected = 0
        if "superseded by drop job" in compact:
            source_type, source_id, now = args
            for job in self.jobs:
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
        elif "superseded by newer content hash" in compact:
            source_type, source_id, content_hash, now = args
            for job in self.jobs:
                if (
                    job.get("source_type", "message") == source_type
                    and job.get("source_id", job["message_id"]) == source_id
                    and job["job_kind"] in {"embed", "reembed"}
                    and job["status"] == "pending"
                    and job["content_hash"] != content_hash
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
        else:  # pragma: no cover - keeps fake strict as the helper evolves.
            raise AssertionError(f"unexpected execute: {compact}")
        return f"UPDATE {affected}"

    async def fetchrow(self, sql: str, *args):
        self.executed_sql.append(sql)
        compact = " ".join(sql.split())
        if compact.startswith("SELECT id, source_type, source_id, message_id, job_kind"):
            source_type, source_id, job_kind, content_hash = args
            matches = [
                job
                for job in self.jobs
                if job.get("source_type", "message") == source_type
                and job.get("source_id", job["message_id"]) == source_id
                and job["job_kind"] == job_kind
                and job["status"] in {"pending", "processing"}
                and job["content_hash"] == content_hash
            ]
            if not matches:
                return None
            row = sorted(matches, key=lambda row: (row["created_at"], str(row["id"])))[0]
            row.setdefault("source_type", "message")
            row.setdefault("source_id", row["message_id"])
            return row
        if compact.startswith("INSERT INTO mediator.embed_jobs"):
            source_type, source_id, message_id, job_kind, model, dimension, content_hash, now = args
            row = {
                "id": uuid4(),
                "source_type": source_type,
                "source_id": source_id,
                "message_id": message_id,
                "job_kind": job_kind,
                "status": "pending",
                "model": model,
                "dimension": dimension,
                "content_hash": content_hash,
                "attempts": 0,
                "last_error": None,
                "next_attempt_at": now,
                "locked_at": None,
                "locked_by": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            self.jobs.append(row)
            return row
        raise AssertionError(f"unexpected fetchrow: {compact}")


class ProviderUnavailableConn(FakeEmbedJobsConn):
    def __getattr__(self, name: str):
        if "embed" in name or "provider" in name or "openai" in name:
            raise RuntimeError(f"unexpected provider access via {name}")
        raise AttributeError(name)


def _hash(prefix: str = "a") -> str:
    return prefix * 64


def test_embed_job_helper_source_does_not_call_embedding_providers():
    source = open("app/services/embed_jobs.py").read()

    assert "embedder_from_settings" not in source
    assert ".embed_texts(" not in source
    assert "OpenAIEmbedder" not in source
    assert "LocalBgeSmallEmbedder" not in source


def test_embed_job_helper_source_does_not_depend_on_messages_content_hash():
    source = open("app/services/embed_jobs.py").read()

    assert "messages.content_hash" not in source
    assert "ADD COLUMN content_hash" not in source


async def test_enqueue_embed_job_is_idempotent_for_same_hash():
    conn = FakeEmbedJobsConn()
    message_id = uuid4()
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)

    first = await enqueue_message_embed_job(
        conn,
        message_id=message_id,
        content_hash=_hash(),
        model="text-embedding-3-small",
        dimension=1536,
        now=now,
    )
    second = await enqueue_message_embed_job(
        conn,
        message_id=message_id,
        content_hash=_hash(),
        model="text-embedding-3-small",
        dimension=1536,
        now=now + timedelta(minutes=5),
    )

    assert first.action == "created"
    assert second.action == "existing"
    assert second.job.id == first.job.id
    assert len(conn.jobs) == 1
    assert conn.jobs[0]["attempts"] == 0
    assert conn.jobs[0]["last_error"] is None
    assert conn.jobs[0]["locked_at"] is None
    assert conn.jobs[0]["next_attempt_at"] == now


async def test_same_source_id_across_source_types_does_not_collide():
    conn = FakeEmbedJobsConn()
    source_id = uuid4()
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)

    message = await enqueue_embed_job(
        conn,
        source_type="message",
        source_id=source_id,
        message_id=source_id,
        content_hash=_hash(),
        model="text-embedding-3-small",
        dimension=1536,
        now=now,
    )
    memory = await enqueue_embed_job(
        conn,
        source_type="memory",
        source_id=source_id,
        content_hash=_hash(),
        model="text-embedding-3-small",
        dimension=1536,
        now=now + timedelta(minutes=1),
    )

    assert message.action == "created"
    assert memory.action == "created"
    assert message.job.id != memory.job.id
    assert message.job.message_id == source_id
    assert memory.job.message_id is None
    assert {job["source_type"] for job in conn.jobs} == {"message", "memory"}


async def test_new_content_hash_supersede_is_scoped_by_source_type_and_id():
    conn = FakeEmbedJobsConn()
    shared_id = uuid4()
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    conn.jobs.append(
        {
            "id": uuid4(),
            "source_type": "memory",
            "source_id": shared_id,
            "message_id": None,
            "job_kind": "embed",
            "status": "pending",
            "model": "text-embedding-3-small",
            "dimension": 1536,
            "content_hash": _hash("a"),
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": now,
            "locked_at": None,
            "locked_by": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
    )

    result = await enqueue_embed_job(
        conn,
        source_type="message",
        source_id=shared_id,
        message_id=shared_id,
        content_hash=_hash("b"),
        model="text-embedding-3-small",
        dimension=1536,
        now=now + timedelta(minutes=1),
    )

    assert result.action == "created"
    assert result.superseded_pending == 0
    assert conn.jobs[0]["status"] == "pending"


async def test_message_wrappers_populate_message_id_compatibility_column():
    conn = FakeEmbedJobsConn()
    message_id = uuid4()

    result = await enqueue_message_embed_job(
        conn,
        message_id=message_id,
        content_hash=_hash(),
        model="text-embedding-3-small",
        dimension=1536,
    )

    assert result.job.source_type == "message"
    assert result.job.source_id == message_id
    assert result.job.message_id == message_id
    assert conn.jobs[0]["message_id"] == message_id


async def test_new_content_hash_supersedes_pending_embed_and_reembed_jobs():
    conn = FakeEmbedJobsConn()
    message_id = uuid4()
    old_hash = _hash("a")
    new_hash = _hash("b")
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    conn.jobs.extend(
        [
            {
                "id": uuid4(),
                "message_id": message_id,
                "job_kind": "embed",
                "status": "pending",
                "model": "text-embedding-3-small",
                "dimension": 1536,
                "content_hash": old_hash,
                "attempts": 2,
                "last_error": "rate limit",
                "next_attempt_at": now + timedelta(hours=1),
                "locked_at": None,
                "locked_by": None,
                "created_at": now - timedelta(minutes=10),
                "updated_at": now - timedelta(minutes=10),
                "completed_at": None,
            },
            {
                "id": uuid4(),
                "message_id": message_id,
                "job_kind": "reembed",
                "status": "pending",
                "model": "text-embedding-3-small",
                "dimension": 1536,
                "content_hash": old_hash,
                "attempts": 1,
                "last_error": "timeout",
                "next_attempt_at": now + timedelta(hours=1),
                "locked_at": None,
                "locked_by": None,
                "created_at": now - timedelta(minutes=9),
                "updated_at": now - timedelta(minutes=9),
                "completed_at": None,
            },
            {
                "id": uuid4(),
                "message_id": message_id,
                "job_kind": "embed",
                "status": "processing",
                "model": "text-embedding-3-small",
                "dimension": 1536,
                "content_hash": old_hash,
                "attempts": 1,
                "last_error": None,
                "next_attempt_at": now,
                "locked_at": now,
                "locked_by": "worker",
                "created_at": now - timedelta(minutes=8),
                "updated_at": now - timedelta(minutes=8),
                "completed_at": None,
            },
        ]
    )

    result = await enqueue_message_reembed_job(
        conn,
        message_id=message_id,
        content_hash=new_hash,
        model="text-embedding-3-small",
        dimension=1536,
        now=now,
    )

    assert result.action == "created"
    assert result.superseded_pending == 2
    assert [job["status"] for job in conn.jobs[:3]] == ["superseded", "superseded", "processing"]
    assert conn.jobs[-1]["job_kind"] == "reembed"
    assert conn.jobs[-1]["content_hash"] == new_hash
    assert conn.jobs[-1]["attempts"] == 0
    assert conn.jobs[-1]["last_error"] is None
    assert conn.jobs[-1]["locked_at"] is None
    assert conn.jobs[-1]["next_attempt_at"] == now


async def test_matching_processing_job_is_reused_with_retry_metadata_unchanged():
    conn = FakeEmbedJobsConn()
    message_id = uuid4()
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    processing_job = {
        "id": uuid4(),
        "message_id": message_id,
        "job_kind": "embed",
        "status": "processing",
        "model": "text-embedding-3-small",
        "dimension": 1536,
        "content_hash": _hash(),
        "attempts": 3,
        "last_error": "temporary timeout",
        "next_attempt_at": now + timedelta(minutes=15),
        "locked_at": now - timedelta(minutes=1),
        "locked_by": "worker-a",
        "created_at": now - timedelta(minutes=5),
        "updated_at": now - timedelta(minutes=2),
        "completed_at": None,
    }
    conn.jobs.append(processing_job)

    result = await enqueue_message_embed_job(
        conn,
        message_id=message_id,
        content_hash=_hash(),
        model="text-embedding-3-small",
        dimension=1536,
        now=now,
    )

    assert result.action == "existing"
    assert result.superseded_pending == 0
    assert result.job.id == processing_job["id"]
    assert conn.jobs == [processing_job]
    assert result.job.attempts == 3
    assert result.job.next_attempt_at == processing_job["next_attempt_at"]


async def test_drop_job_cancels_pending_embed_work_and_is_idempotent():
    conn = FakeEmbedJobsConn()
    message_id = uuid4()
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    conn.jobs.append(
        {
            "id": uuid4(),
            "message_id": message_id,
            "job_kind": "embed",
            "status": "pending",
            "model": "text-embedding-3-small",
            "dimension": 1536,
            "content_hash": _hash(),
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": now,
            "locked_at": None,
            "locked_by": None,
            "created_at": now - timedelta(minutes=5),
            "updated_at": now - timedelta(minutes=5),
            "completed_at": None,
        }
    )

    first = await enqueue_message_drop_embedding_job(conn, message_id=message_id, now=now)
    second = await enqueue_message_drop_embedding_job(conn, message_id=message_id, now=now + timedelta(minutes=5))

    assert first.action == "created"
    assert second.action == "existing"
    assert first.superseded_pending == 1
    assert second.superseded_pending == 0
    assert conn.jobs[0]["status"] == "cancelled"
    assert conn.jobs[-1]["job_kind"] == "drop"
    assert conn.jobs[-1]["content_hash"] is None
    assert conn.jobs[-1]["model"] is None
    assert conn.jobs[-1]["dimension"] is None


async def test_drop_job_cancels_pending_embed_and_reembed_but_not_processing_jobs():
    conn = FakeEmbedJobsConn()
    message_id = uuid4()
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    conn.jobs.extend(
        [
            {
                "id": uuid4(),
                "message_id": message_id,
                "job_kind": "embed",
                "status": "pending",
                "model": "text-embedding-3-small",
                "dimension": 1536,
                "content_hash": _hash("a"),
                "attempts": 1,
                "last_error": "rate limit",
                "next_attempt_at": now + timedelta(minutes=20),
                "locked_at": None,
                "locked_by": None,
                "created_at": now - timedelta(minutes=5),
                "updated_at": now - timedelta(minutes=5),
                "completed_at": None,
            },
            {
                "id": uuid4(),
                "message_id": message_id,
                "job_kind": "reembed",
                "status": "pending",
                "model": "text-embedding-3-small",
                "dimension": 1536,
                "content_hash": _hash("b"),
                "attempts": 2,
                "last_error": "timeout",
                "next_attempt_at": now + timedelta(minutes=25),
                "locked_at": None,
                "locked_by": None,
                "created_at": now - timedelta(minutes=4),
                "updated_at": now - timedelta(minutes=4),
                "completed_at": None,
            },
            {
                "id": uuid4(),
                "message_id": message_id,
                "job_kind": "reembed",
                "status": "processing",
                "model": "text-embedding-3-small",
                "dimension": 1536,
                "content_hash": _hash("c"),
                "attempts": 1,
                "last_error": None,
                "next_attempt_at": now,
                "locked_at": now - timedelta(minutes=1),
                "locked_by": "worker-a",
                "created_at": now - timedelta(minutes=3),
                "updated_at": now - timedelta(minutes=3),
                "completed_at": None,
            },
        ]
    )

    result = await enqueue_message_drop_embedding_job(conn, message_id=message_id, now=now)

    assert result.action == "created"
    assert result.superseded_pending == 2
    assert [job["status"] for job in conn.jobs[:3]] == ["cancelled", "cancelled", "processing"]
    assert conn.jobs[0]["completed_at"] == now
    assert conn.jobs[1]["completed_at"] == now
    assert conn.jobs[2]["completed_at"] is None


async def test_enqueue_write_succeeds_even_when_provider_is_unavailable():
    conn = ProviderUnavailableConn()
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)

    result = await enqueue_message_embed_job(
        conn,
        message_id=uuid4(),
        content_hash=_hash(),
        model="text-embedding-3-small",
        dimension=1536,
        now=now,
    )

    assert result.action == "created"
    assert len(conn.jobs) == 1
    assert conn.jobs[0]["next_attempt_at"] == now


async def test_enqueue_validates_hash_model_and_dimension():
    conn = FakeEmbedJobsConn()

    with pytest.raises(ValueError, match="content_hash"):
        await enqueue_message_embed_job(
            conn,
            message_id=uuid4(),
            content_hash="not-a-hash",
            model="text-embedding-3-small",
            dimension=1536,
        )
    with pytest.raises(ValueError, match="model"):
        await enqueue_message_reembed_job(conn, message_id=uuid4(), content_hash=_hash(), model="", dimension=1536)
    with pytest.raises(ValueError, match="dimension"):
        await enqueue_message_embed_job(
            conn,
            message_id=uuid4(),
            content_hash=_hash(),
            model="text-embedding-3-small",
            dimension=0,
        )
