"""Background worker for content embedding jobs.

Embed/reembed jobs read canonical text only from
``mediator.v_searchable_content``.  The only raw ``mediator.messages`` read in
this module is the cleanup exception used after a claimed message job disappears
from the view: deleted or search-suppressed rows are intentionally hidden from
the searchable surface, so the worker may inspect those lifecycle flags solely
to delete stale embeddings and finish the job without embedding hidden content.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.config import Settings, get_settings
from app.services.embed_jobs import enqueue_embed_job, enqueue_reembed_job
from app.services.embeddings import Embedder, content_hash, embedder_from_settings

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@asynccontextmanager
async def _connection(pool_or_conn: Any) -> AsyncIterator[Any]:
    acquire = getattr(pool_or_conn, "acquire", None)
    if acquire is None:
        yield pool_or_conn
        return
    async with acquire() as conn:
        yield conn


@dataclass(frozen=True)
class EmbedWorkerResult:
    claimed: int = 0
    embedded: int = 0
    dropped: int = 0
    superseded: int = 0
    skipped: int = 0
    retried: int = 0
    failed: int = 0


class EmbedJobWorker:
    """Claim and process async embedding jobs."""

    def __init__(
        self,
        pool: Any,
        *,
        settings: Settings | None = None,
        embedder: Embedder | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.pool = pool
        self.settings = settings or get_settings()
        self.embedder = embedder or embedder_from_settings(self.settings)
        self.worker_id = worker_id or f"embed-{uuid4()}"

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("embedding worker tick failed")
            await asyncio.sleep(self.settings.embedding_worker_poll_interval_s)

    async def run_once(self, *, now: datetime | None = None) -> EmbedWorkerResult:
        now = _aware_utc(now or _utc_now())
        jobs = await self._claim_due_jobs(now=now)
        counts = {
            "claimed": len(jobs),
            "embedded": 0,
            "dropped": 0,
            "superseded": 0,
            "skipped": 0,
            "retried": 0,
            "failed": 0,
        }
        for job in jobs:
            try:
                outcome = await self._process_job(job, now=now)
            except Exception as exc:
                logger.exception("embedding job %s failed", job["id"])
                retrying = await self._record_failure(job, exc, now=now)
                counts["retried" if retrying else "failed"] += 1
            else:
                counts[outcome] += 1
        return EmbedWorkerResult(**counts)

    async def _claim_due_jobs(self, *, now: datetime) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            WITH due AS (
                SELECT id
                FROM mediator.embed_jobs
                WHERE status = 'pending'
                  AND next_attempt_at <= $1
                ORDER BY next_attempt_at ASC, created_at ASC, id ASC
                LIMIT $2
                FOR UPDATE SKIP LOCKED
            )
            UPDATE mediator.embed_jobs ej
            SET status = 'processing',
                attempts = ej.attempts + 1,
                locked_at = $1,
                locked_by = $3,
                updated_at = $1
            FROM due
            WHERE ej.id = due.id
            RETURNING ej.id, ej.source_type, ej.source_id, ej.message_id,
                      ej.job_kind, ej.model, ej.dimension,
                      ej.content_hash, ej.attempts, ej.locked_by
            """,
            now,
            self.settings.embedding_worker_batch_size,
            self.worker_id,
        )
        return [dict(row) for row in rows]

    async def _process_job(self, job: dict[str, Any], *, now: datetime) -> str:
        if job["job_kind"] == "drop":
            await self._delete_embedding(job["source_type"], job["source_id"])
            await self._mark_completed(job, status="succeeded", now=now)
            return "dropped"

        searchable = await self._fetch_searchable_content(job["source_type"], job["source_id"])
        if searchable is None:
            if job["source_type"] == "message":
                cleanup_state = await self._fetch_raw_cleanup_state(job["message_id"] or job["source_id"])
                if cleanup_state is not None and (
                    cleanup_state.get("deleted_at") is not None
                    or cleanup_state.get("search_suppressed_at") is not None
                ):
                    await self._delete_embedding(job["source_type"], job["source_id"])
                    await self._mark_completed(
                        job,
                        status="skipped",
                        now=now,
                        last_error="message no longer searchable; embedding deleted",
                    )
                    return "skipped"
                await self._mark_completed(job, status="skipped", now=now, last_error="message not found")
                return "skipped"
            await self._delete_embedding(job["source_type"], job["source_id"])
            await self._mark_completed(
                job,
                status="skipped",
                now=now,
                last_error="source no longer searchable; embedding deleted",
            )
            return "skipped"

        canonical_text = searchable["canonical_text"] or ""
        current_hash = content_hash(canonical_text)
        if current_hash != job["content_hash"]:
            async with _connection(self.pool) as conn:
                if job["job_kind"] == "embed":
                    await enqueue_embed_job(
                        conn,
                        source_type=job["source_type"],
                        source_id=job["source_id"],
                        message_id=job["message_id"],
                        content_hash=current_hash,
                        model=job["model"],
                        dimension=job["dimension"],
                        now=now,
                    )
                else:
                    await enqueue_reembed_job(
                        conn,
                        source_type=job["source_type"],
                        source_id=job["source_id"],
                        message_id=job["message_id"],
                        content_hash=current_hash,
                        model=job["model"],
                        dimension=job["dimension"],
                        now=now,
                    )
            await self._mark_completed(
                job,
                status="superseded",
                now=now,
                last_error="superseded by newer canonical content hash",
            )
            return "superseded"

        vector = (await self.embedder.embed_texts([canonical_text]))[0]
        await self._upsert_embedding(
            source_type=job["source_type"],
            source_id=job["source_id"],
            vector=vector,
            model=job["model"],
            dimension=job["dimension"],
            content_hash=current_hash,
            now=now,
        )
        await self._mark_completed(job, status="succeeded", now=now)
        return "embedded"

    async def _fetch_searchable_content(self, source_type: str, source_id: Any) -> Any | None:
        return await self.pool.fetchrow(
            """
            SELECT source_type, source_id, message_id, canonical_text
            FROM mediator.v_searchable_content
            WHERE source_type = $1 AND source_id = $2
            """,
            source_type,
            source_id,
        )

    async def _fetch_raw_cleanup_state(self, message_id: Any) -> Any | None:
        return await self.pool.fetchrow(
            """
            SELECT id, deleted_at, search_suppressed_at
            FROM mediator.messages
            WHERE id = $1
            """,
            message_id,
        )

    async def _upsert_embedding(
        self,
        *,
        source_type: str,
        source_id: Any,
        vector: Sequence[float],
        model: str,
        dimension: int,
        content_hash: str,
        now: datetime,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO mediator.content_embeddings (
                source_type, source_id, embedding, model, dimension, content_hash, embedded_at
            )
            VALUES ($1, $2, $3::vector, $4, $5, $6, $7)
            ON CONFLICT (source_type, source_id) DO UPDATE
            SET embedding = EXCLUDED.embedding,
                model = EXCLUDED.model,
                dimension = EXCLUDED.dimension,
                content_hash = EXCLUDED.content_hash,
                embedded_at = EXCLUDED.embedded_at
            """,
            source_type,
            source_id,
            _vector_literal(vector),
            model,
            dimension,
            content_hash,
            now,
        )

    async def _delete_embedding(self, source_type: str, source_id: Any) -> None:
        await self.pool.execute(
            """
            DELETE FROM mediator.content_embeddings
            WHERE source_type = $1 AND source_id = $2
            """,
            source_type,
            source_id,
        )

    async def _mark_completed(
        self,
        job: dict[str, Any],
        *,
        status: str,
        now: datetime,
        last_error: str | None = None,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE mediator.embed_jobs
            SET status = $1,
                last_error = $2,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = $3,
                completed_at = $3
            WHERE id = $4
              AND status = 'processing'
              AND locked_by = $5
            """,
            status,
            last_error,
            now,
            job["id"],
            self.worker_id,
        )

    async def _record_failure(self, job: dict[str, Any], exc: Exception, *, now: datetime) -> bool:
        attempts = int(job["attempts"])
        if attempts >= MAX_ATTEMPTS:
            await self._mark_completed(job, status="failed", now=now, last_error=str(exc))
            return False
        next_attempt_at = now + _retry_delay(attempts)
        await self.pool.execute(
            """
            UPDATE mediator.embed_jobs
            SET status = 'pending',
                last_error = $1,
                next_attempt_at = $2,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = $3
            WHERE id = $4
              AND status = 'processing'
              AND locked_by = $5
            """,
            str(exc),
            next_attempt_at,
            now,
            job["id"],
            self.worker_id,
        )
        return True


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(300, 5 * (2 ** max(attempts - 1, 0))))


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"
