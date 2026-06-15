"""Migration tests for the SuperPOM schema (0061).

Verifies table existence, seed rows, idempotency posture, and FK-order
down migrations via text-based checks against migration SQL files.
No DB connection required.
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _read_migration(filename: str) -> str:
    """Read a migration file by exact name."""
    return (MIGRATIONS_DIR / filename).read_text()


# ═══════════════════════════════════════════════════════════════════
# 0061: superpom topic + SuperPOM bot row
# ═══════════════════════════════════════════════════════════════════


class TestMigration0061:
    """Migration 0061 seeds the superpom topic and SuperPOM bot row."""

    def test_0061_exists(self):
        assert (MIGRATIONS_DIR / "0061_superpom_topic.sql").exists()
        assert (MIGRATIONS_DIR / "0061_superpom_topic.down.sql").exists()

    def test_0061_inserts_superpom_topic(self):
        sql = _read_migration("0061_superpom_topic.sql")
        assert "INSERT INTO mediator.topics" in sql
        assert "'superpom'" in sql
        assert "ON CONFLICT (slug) DO NOTHING" in sql
        assert "gen_random_uuid()" in sql

    def test_0061_inserts_superpom_bot_row(self):
        sql = _read_migration("0061_superpom_topic.sql")
        assert "INSERT INTO mediator.bots" in sql
        assert "'superpom'" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql

    def test_0061_down_deletes_in_fk_order(self):
        sql = _read_migration("0061_superpom_topic.down.sql")
        assert "DELETE FROM mediator.bots WHERE id = 'superpom'" in sql
        assert "DELETE FROM mediator.topics WHERE slug = 'superpom'" in sql
        # Bot row must be deleted before topic row (FK order)
        bot_pos = sql.index("DELETE FROM mediator.bots")
        topic_pos = sql.index("DELETE FROM mediator.topics")
        assert bot_pos < topic_pos, (
            "Bot row must be deleted before topic (FK order)"
        )

    def test_0061_is_idempotent(self):
        sql = _read_migration("0061_superpom_topic.sql")
        assert "ON CONFLICT (slug) DO NOTHING" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql

    def test_0061_backfills_participants_shape_solo(self):
        sql = _read_migration("0061_superpom_topic.sql")
        assert "participants_shape = 'solo'" in sql
        assert "UPDATE mediator.topics" in sql
        assert "AND participants_shape <> 'solo'" in sql
