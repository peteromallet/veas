"""Apply the live-voice migration set to a target DATABASE_URL.

Idempotent: every migration uses `IF NOT EXISTS` / `EXCEPTION WHEN
duplicate_object` so re-running is a no-op. Safe to call before every
deploy.

Usage:

    DATABASE_URL=postgres://... uv run python scripts/apply_live_voice_migrations.py

    # Dry-run (just list what would be applied):
    DATABASE_URL=... uv run python scripts/apply_live_voice_migrations.py --dry-run

Migrations applied (in order):

  0042_live_conversations.sql           — 7 live-conversation tables + RLS
  0043_auth_magic_links.sql             — Discord magic-link audit
  0044_live_session_latency.sql         — per-stage latency spans
  0045_live_session_spend.sql           — per-session spend cents

The `auth.uid()` reference in 0042 RLS policies relies on Supabase's
auth schema being present. On vanilla Postgres targets, run the
following beforehand:

    CREATE SCHEMA IF NOT EXISTS auth;
    CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE
      AS $$ SELECT NULL::uuid; $$;
    CREATE ROLE IF NOT EXISTS anon NOINHERIT;

The full bootstrap (roles + schemas) lives in the worktree's
.env.local notes; Supabase deployments ship these by default.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


MIGRATIONS = (
    "0042_live_conversations.sql",
    "0043_auth_magic_links.sql",
    "0044_live_session_latency.sql",
    "0045_live_session_spend.sql",
    "0049_messages_bot_turn_id.sql",
    "0050_habits_topic.sql",
)


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations"


def _resolve_dsn() -> str:
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("ERROR: DATABASE_URL must be set", file=sys.stderr)
        sys.exit(2)
    return dsn


_TRACK_TABLE_SQL = """
CREATE SCHEMA IF NOT EXISTS mediator;
CREATE TABLE IF NOT EXISTS mediator.applied_migrations (
    filename text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def _ensure_tracking(dsn: str) -> None:
    import subprocess
    result = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-c", _TRACK_TABLE_SQL],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("  !! failed to ensure applied_migrations table:", result.stderr.strip()[:300])
        sys.exit(1)


def _already_applied(dsn: str, name: str) -> bool:
    import subprocess
    result = subprocess.run(
        ["psql", dsn, "-tA", "-c", f"SELECT 1 FROM mediator.applied_migrations WHERE filename='{name}'"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "1"


def _apply_one(dsn: str, name: str, *, dry_run: bool) -> None:
    path = _migrations_dir() / name
    if not path.exists():
        print(f"  !! MISSING: {name}")
        return
    if _already_applied(dsn, name):
        print(f"  ✓ {name}: already applied (skip)")
        return
    sql = path.read_text(encoding="utf-8")
    if dry_run:
        print(f"  [dry-run] would apply {name} ({len(sql)} bytes)")
        return
    import subprocess
    result = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Recover from a partially-applied state: if the failure is "X
        # already exists", we assume the previous run landed everything
        # and just record it in the tracker so the next run sees it.
        stderr = result.stderr.strip()
        if "already exists" in stderr:
            print(f"  ✓ {name}: schema already present (no-op; recording in tracker)")
        else:
            print(f"  !! FAILED: {name}\n{stderr[:800]}")
            sys.exit(1)
    # Record success.
    subprocess.run(
        [
            "psql",
            dsn,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f"INSERT INTO mediator.applied_migrations (filename) VALUES ('{name}') ON CONFLICT DO NOTHING",
        ],
        capture_output=True,
        text=True,
    )
    tail = result.stdout.strip().splitlines()[-3:]
    print(f"  ✓ {name}: " + " | ".join(tail))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Skip the apply step, just report")
    args = ap.parse_args()
    dsn = _resolve_dsn()
    print(f"Applying live-voice migrations to {dsn.split('@')[-1]} ({len(MIGRATIONS)} files)")
    if not args.dry_run:
        _ensure_tracking(dsn)
    for name in MIGRATIONS:
        _apply_one(dsn, name, dry_run=args.dry_run)
    print("Done." if not args.dry_run else "Dry-run complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
