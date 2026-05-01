"""Persistence helpers for OOB-withheld outbound review rows."""

from typing import Any, Literal
from uuid import UUID


ReviewVerdict = Literal["rewrite", "block"]


async def record_withheld_outbound_review(
    pool: Any,
    *,
    recipient_id: UUID,
    original_content: str,
    reason: str,
    verdict: ReviewVerdict,
    sender_id: UUID | None = None,
    outbound_id: UUID | None = None,
    suggested_rewrite: str | None = None,
    checker_failed: bool = False,
    status: str = "pending",
) -> UUID:
    """Record a pending admin/retry review without scheduling or UI side effects."""
    row = await pool.fetchrow(
        """
        INSERT INTO withheld_outbound_reviews (
            recipient_id, sender_id, outbound_id, original_content, suggested_rewrite,
            reason, verdict, checker_failed, status, created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now(), now())
        RETURNING id
        """,
        recipient_id,
        sender_id,
        outbound_id,
        original_content,
        suggested_rewrite,
        reason,
        verdict,
        checker_failed,
        status,
    )
    return row["id"]
