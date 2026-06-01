from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.services.embed_worker import EmbedJobWorker
from app.services.embeddings import DeterministicFakeEmbedder, content_hash

pytestmark = pytest.mark.anyio


class TinyEmbedder:
    model_name = "text-embedding-3-small"
    dimension = 3

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("provider unavailable")
        return [[1.0, 0.0, 0.0] for _ in texts]


class SettingsStub:
    embedding_worker_batch_size = 10
    embedding_worker_poll_interval_s = 0.01


class FakeEmbedWorkerPool:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.searchable: dict = {}
        self.raw_messages: dict = {}
        self.embeddings: dict = {}
        self.sql: list[str] = []

    async def fetch(self, sql: str, *args):
        self.sql.append(sql)
        compact = " ".join(sql.split())
        if "FOR UPDATE SKIP LOCKED" not in compact:
            raise AssertionError(f"unexpected fetch: {compact}")
        now, limit, worker_id = args
        due = [
            job
            for job in self.jobs
            if job["status"] == "pending" and job["next_attempt_at"] <= now
        ]
        due = sorted(due, key=lambda row: (row["next_attempt_at"], row["created_at"], str(row["id"])))[:limit]
        rows = []
        for job in due:
            job.update(
                status="processing",
                attempts=job["attempts"] + 1,
                locked_at=now,
                locked_by=worker_id,
                updated_at=now,
            )
            rows.append(
                {
                    "id": job["id"],
                    "source_type": job["source_type"],
                    "source_id": job["source_id"],
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

    async def fetchrow(self, sql: str, *args):
        self.sql.append(sql)
        compact = " ".join(sql.split())
        if compact.startswith("SELECT source_type, source_id, message_id, canonical_text FROM mediator.v_searchable_content"):
            source_type, source_id = args
            return self.searchable.get((source_type, source_id))
        if compact.startswith("SELECT id, deleted_at, search_suppressed_at FROM mediator.messages"):
            return self.raw_messages.get(args[0])
        if compact.startswith("SELECT id, source_type, source_id, message_id, job_kind"):
            source_type, source_id, job_kind, content_hash_value = args
            matches = [
                job
                for job in self.jobs
                if job["source_type"] == source_type
                and job["source_id"] == source_id
                and job["job_kind"] == job_kind
                and job["status"] in {"pending", "processing"}
                and job["content_hash"] == content_hash_value
            ]
            return sorted(matches, key=lambda row: (row["created_at"], str(row["id"])))[0] if matches else None
        if compact.startswith("INSERT INTO mediator.embed_jobs"):
            source_type, source_id, message_id, job_kind, model, dimension, content_hash_value, now = args
            row = _job(
                message_id=message_id,
                source_type=source_type,
                source_id=source_id,
                job_kind=job_kind,
                content_hash=content_hash_value,
                model=model,
                dimension=dimension,
                now=now,
            )
            self.jobs.append(row)
            return row
        raise AssertionError(f"unexpected fetchrow: {compact}")

    async def execute(self, sql: str, *args):
        self.sql.append(sql)
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO mediator.content_embeddings"):
            source_type, source_id, vector, model, dimension, content_hash_value, now = args
            self.embeddings[(source_type, source_id)] = {
                "embedding": vector,
                "model": model,
                "dimension": dimension,
                "content_hash": content_hash_value,
                "embedded_at": now,
            }
            return "INSERT 0 1"
        if compact.startswith("DELETE FROM mediator.content_embeddings"):
            self.embeddings.pop((args[0], args[1]), None)
            return "DELETE 1"
        if compact.startswith("UPDATE mediator.embed_jobs SET status = $1"):
            status, last_error, now, job_id, worker_id = args
            for job in self.jobs:
                if job["id"] == job_id and job["status"] == "processing" and job["locked_by"] == worker_id:
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
            for job in self.jobs:
                if job["id"] == job_id and job["status"] == "processing" and job["locked_by"] == worker_id:
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
        if "superseded by newer content hash" in compact:
            source_type, source_id, content_hash_value, now = args
            affected = 0
            for job in self.jobs:
                if (
                    job["source_type"] == source_type
                    and job["source_id"] == source_id
                    and job["job_kind"] in {"embed", "reembed"}
                    and job["status"] == "pending"
                    and job["content_hash"] != content_hash_value
                ):
                    job.update(status="superseded", updated_at=now, completed_at=now)
                    affected += 1
            return f"UPDATE {affected}"
        if "superseded by drop job" in compact:
            return "UPDATE 0"
        raise AssertionError(f"unexpected execute: {compact}")


def _job(
    *,
    message_id,
    source_type="message",
    source_id=None,
    job_kind="embed",
    content_hash: str | None,
    model="text-embedding-3-small",
    dimension=3,
    now: datetime,
    attempts=0,
):
    return {
        "id": uuid4(),
        "source_type": source_type,
        "source_id": source_id or message_id,
        "message_id": message_id,
        "job_kind": job_kind,
        "status": "pending",
        "model": model,
        "dimension": dimension,
        "content_hash": content_hash,
        "attempts": attempts,
        "last_error": None,
        "next_attempt_at": now,
        "locked_at": None,
        "locked_by": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }


def test_worker_source_uses_claim_view_and_documented_cleanup_exception():
    source = open("app/services/embed_worker.py").read()

    assert "FOR UPDATE SKIP LOCKED" in source
    assert "FROM mediator.v_searchable_content" in source
    assert "ON CONFLICT (source_type, source_id)" in source
    assert "DELETE FROM mediator.content_embeddings" in source
    assert "FROM mediator.messages" in source
    assert "cleanup exception" in source
    assert "messages.content_hash" not in source


async def test_worker_embeds_searchable_text_and_upserts_embedding():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    message_id = uuid4()
    text = "hello\n\n\n"
    pool = FakeEmbedWorkerPool()
    pool.searchable[("message", message_id)] = {"message_id": message_id, "canonical_text": text}
    pool.jobs.append(_job(message_id=message_id, content_hash=content_hash(text), now=now))
    embedder = TinyEmbedder()

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=embedder,
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.claimed == 1
    assert result.embedded == 1
    assert embedder.calls == [[text]]
    assert pool.embeddings[("message", message_id)]["content_hash"] == content_hash(text)
    assert pool.jobs[0]["source_type"] == "message"
    assert pool.jobs[0]["source_id"] == message_id
    assert pool.jobs[0]["message_id"] == message_id
    assert pool.jobs[0]["status"] == "succeeded"
    assert pool.jobs[0]["locked_by"] is None


async def test_worker_embeds_non_message_source_by_composite_key():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    memory_id = uuid4()
    text = "private memory"
    pool = FakeEmbedWorkerPool()
    pool.searchable[("memory", memory_id)] = {
        "source_type": "memory",
        "source_id": memory_id,
        "message_id": None,
        "canonical_text": text,
    }
    pool.jobs.append(
        _job(
            source_type="memory",
            source_id=memory_id,
            message_id=None,
            content_hash=content_hash(text),
            now=now,
        )
    )

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.embedded == 1
    assert pool.embeddings[("memory", memory_id)]["content_hash"] == content_hash(text)
    assert all("FROM mediator.messages" not in sql for sql in pool.sql)


async def test_worker_reembeds_non_message_source_by_composite_key():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    observation_id = uuid4()
    text = "significant observation"
    pool = FakeEmbedWorkerPool()
    pool.searchable[("observation", observation_id)] = {
        "source_type": "observation",
        "source_id": observation_id,
        "message_id": None,
        "canonical_text": text,
    }
    pool.embeddings[("observation", observation_id)] = {"content_hash": "old"}
    pool.jobs.append(
        _job(
            source_type="observation",
            source_id=observation_id,
            message_id=None,
            job_kind="reembed",
            content_hash=content_hash(text),
            now=now,
        )
    )

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.embedded == 1
    assert pool.embeddings[("observation", observation_id)]["content_hash"] == content_hash(text)
    assert pool.jobs[0]["status"] == "succeeded"
    assert all("FROM mediator.messages" not in sql for sql in pool.sql)


async def test_worker_isolates_same_uuid_across_source_types():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    shared_id = uuid4()
    message_text = "message text"
    memory_text = "memory text"
    pool = FakeEmbedWorkerPool()
    pool.searchable[("message", shared_id)] = {
        "source_type": "message",
        "source_id": shared_id,
        "message_id": shared_id,
        "canonical_text": message_text,
    }
    pool.searchable[("memory", shared_id)] = {
        "source_type": "memory",
        "source_id": shared_id,
        "message_id": None,
        "canonical_text": memory_text,
    }
    pool.jobs.append(_job(message_id=shared_id, content_hash=content_hash(message_text), now=now))
    pool.jobs.append(
        _job(
            source_type="memory",
            source_id=shared_id,
            message_id=None,
            content_hash=content_hash(memory_text),
            now=now,
        )
    )

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.embedded == 2
    assert pool.embeddings[("message", shared_id)]["content_hash"] == content_hash(message_text)
    assert pool.embeddings[("memory", shared_id)]["content_hash"] == content_hash(memory_text)


async def test_worker_reembeds_edited_searchable_text_with_fake_embedder():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    message_id = uuid4()
    text = "edited text\nwith media summary\n\n"
    pool = FakeEmbedWorkerPool()
    pool.searchable[("message", message_id)] = {"message_id": message_id, "canonical_text": text}
    pool.embeddings[("message", message_id)] = {"content_hash": "old"}
    pool.jobs.append(
        _job(
            message_id=message_id,
            job_kind="reembed",
            content_hash=content_hash(text),
            model="deterministic-fake",
            dimension=4,
            now=now,
        )
    )

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=DeterministicFakeEmbedder(dimension=4),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.claimed == 1
    assert result.embedded == 1
    assert pool.jobs[0]["status"] == "succeeded"
    assert pool.embeddings[("message", message_id)]["model"] == "deterministic-fake"
    assert pool.embeddings[("message", message_id)]["dimension"] == 4
    assert pool.embeddings[("message", message_id)]["content_hash"] == content_hash(text)
    assert pool.embeddings[("message", message_id)]["embedding"].startswith("[")


async def test_worker_deletes_embedding_for_drop_without_view_read():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    message_id = uuid4()
    pool = FakeEmbedWorkerPool()
    pool.embeddings[("message", message_id)] = {"content_hash": "old"}
    pool.jobs.append(_job(message_id=message_id, job_kind="drop", content_hash=None, model=None, dimension=None, now=now))

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.dropped == 1
    assert ("message", message_id) not in pool.embeddings
    assert all("v_searchable_content" not in sql for sql in pool.sql)


async def test_worker_cancelled_job_is_not_claimed_or_embedded():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    message_id = uuid4()
    text = "cancelled text\n\n\n"
    pool = FakeEmbedWorkerPool()
    pool.searchable[("message", message_id)] = {"message_id": message_id, "canonical_text": text}
    pool.jobs.append(_job(message_id=message_id, content_hash=content_hash(text), now=now))
    pool.jobs[0]["status"] = "cancelled"
    pool.jobs[0]["completed_at"] = now
    embedder = TinyEmbedder()

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=embedder,
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.claimed == 0
    assert result.embedded == 0
    assert embedder.calls == []
    assert ("message", message_id) not in pool.embeddings


async def test_worker_suppressed_missing_view_row_deletes_embedding_and_skips():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    message_id = uuid4()
    pool = FakeEmbedWorkerPool()
    pool.raw_messages[message_id] = {
        "id": message_id,
        "deleted_at": None,
        "search_suppressed_at": now,
    }
    pool.embeddings[("message", message_id)] = {"content_hash": "old"}
    pool.jobs.append(_job(message_id=message_id, content_hash="a" * 64, now=now))

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.skipped == 1
    assert ("message", message_id) not in pool.embeddings
    assert pool.jobs[0]["status"] == "skipped"
    assert "no longer searchable" in pool.jobs[0]["last_error"]


async def test_worker_deleted_missing_view_row_deletes_embedding_and_skips():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    message_id = uuid4()
    pool = FakeEmbedWorkerPool()
    pool.raw_messages[message_id] = {
        "id": message_id,
        "deleted_at": now,
        "search_suppressed_at": None,
    }
    pool.embeddings[("message", message_id)] = {"content_hash": "old"}
    pool.jobs.append(_job(message_id=message_id, content_hash="b" * 64, now=now))

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.skipped == 1
    assert ("message", message_id) not in pool.embeddings
    assert pool.jobs[0]["status"] == "skipped"
    assert "no longer searchable" in pool.jobs[0]["last_error"]


async def test_worker_missing_visible_row_without_cleanup_state_does_not_delete_embedding():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    message_id = uuid4()
    pool = FakeEmbedWorkerPool()
    pool.embeddings[("message", message_id)] = {"content_hash": "old"}
    pool.jobs.append(_job(message_id=message_id, content_hash="c" * 64, now=now))

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.skipped == 1
    assert pool.embeddings[("message", message_id)] == {"content_hash": "old"}
    assert pool.jobs[0]["status"] == "skipped"
    assert pool.jobs[0]["last_error"] == "message not found"


async def test_worker_non_message_missing_view_row_deletes_stale_embedding():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    artifact_id = uuid4()
    pool = FakeEmbedWorkerPool()
    pool.embeddings[("artifact", artifact_id)] = {"content_hash": "old"}
    pool.jobs.append(
        _job(
            source_type="artifact",
            source_id=artifact_id,
            message_id=None,
            content_hash="e" * 64,
            now=now,
        )
    )

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.skipped == 1
    assert ("artifact", artifact_id) not in pool.embeddings
    assert pool.jobs[0]["last_error"] == "source no longer searchable; embedding deleted"


async def test_worker_stale_hash_supersedes_and_enqueues_current_hash():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    message_id = uuid4()
    text = "new content\n\n\n"
    pool = FakeEmbedWorkerPool()
    pool.searchable[("message", message_id)] = {"message_id": message_id, "canonical_text": text}
    pool.jobs.append(_job(message_id=message_id, content_hash="a" * 64, now=now))

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.superseded == 1
    assert pool.jobs[0]["status"] == "superseded"
    assert pool.jobs[1]["status"] == "pending"
    assert pool.jobs[1]["content_hash"] == content_hash(text)
    assert pool.jobs[1]["source_type"] == "message"
    assert pool.jobs[1]["source_id"] == message_id
    assert pool.jobs[1]["message_id"] == message_id


async def test_worker_stale_reembed_hash_enqueues_current_reembed_job():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    message_id = uuid4()
    text = "edited again\n\n\n"
    pool = FakeEmbedWorkerPool()
    pool.searchable[("message", message_id)] = {"message_id": message_id, "canonical_text": text}
    pool.jobs.append(_job(message_id=message_id, job_kind="reembed", content_hash="d" * 64, now=now))

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.superseded == 1
    assert pool.jobs[0]["status"] == "superseded"
    assert pool.jobs[1]["job_kind"] == "reembed"
    assert pool.jobs[1]["status"] == "pending"
    assert pool.jobs[1]["content_hash"] == content_hash(text)


async def test_worker_retries_provider_failures_and_releases_claim():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    message_id = uuid4()
    text = "hello\n\n\n"
    pool = FakeEmbedWorkerPool()
    pool.searchable[("message", message_id)] = {"message_id": message_id, "canonical_text": text}
    pool.jobs.append(_job(message_id=message_id, content_hash=content_hash(text), now=now))

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(fail=True),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.retried == 1
    assert pool.jobs[0]["status"] == "pending"
    assert pool.jobs[0]["locked_by"] is None
    assert pool.jobs[0]["next_attempt_at"] == now + timedelta(seconds=5)


async def test_worker_marks_provider_failure_failed_after_max_attempts():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    message_id = uuid4()
    text = "hello\n\n\n"
    pool = FakeEmbedWorkerPool()
    pool.searchable[("message", message_id)] = {"message_id": message_id, "canonical_text": text}
    pool.jobs.append(_job(message_id=message_id, content_hash=content_hash(text), now=now, attempts=4))

    result = await EmbedJobWorker(
        pool,
        settings=SettingsStub(),
        embedder=TinyEmbedder(fail=True),
        worker_id="worker-a",
    ).run_once(now=now)

    assert result.failed == 1
    assert pool.jobs[0]["status"] == "failed"
    assert pool.jobs[0]["locked_by"] is None
    assert pool.jobs[0]["completed_at"] == now
    assert pool.jobs[0]["last_error"] == "provider unavailable"
