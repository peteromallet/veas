"""Deletion grace-period purge job."""

from typing import Any


async def purge_expired_deletions(pool: Any) -> str:
    return await pool.execute(
        """
        UPDATE messages
        SET content='[deleted]'
        WHERE deleted_at IS NOT NULL
          AND deleted_at < now() - interval '24 hours'
          AND content <> '[deleted]'
        """
    )
