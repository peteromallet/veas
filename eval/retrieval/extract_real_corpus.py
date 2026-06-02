"""Extract a REAL-data corpus from the production messages table and
optionally from mediator.v_searchable_content for non-message rows.

  ⚠️  PRIVACY WARNING  ⚠️
  This script reads REAL, intimate user data from the production database
  and writes it to disk in plaintext YAML. The output file
  (default: eval/retrieval/real_corpus.yaml) is GITIGNORED and must NEVER be
  committed. Treat the output as sensitive: keep it local, and delete it when
  you are finished labeling (see eval/retrieval/README.md §12).

This is part of the launch gate for the hosted retrieval eval: it lets a human
build a real-data golden set to validate the retriever against actual
production messages and (optionally) non-message searchable rows (memories,
observations, distillations, artifacts, conversation notes, themes).

The output conforms to the Corpus schema in eval/retrieval/schema.py and can
be loaded by eval/retrieval/loader.py.

Connection: reads DIRECT_DATABASE_URL the same way DbBackedRetriever does
(prefer app.config.get_settings().direct_database_url, fall back to the raw
env var), and lazily imports psycopg so the offline harness never needs DB
dependencies just to show --help.

Bounded by default for privacy: --limit defaults to 300 for messages,
--non-message-limit defaults to 100 for non-message rows (NEVER unbounded).

Usage:
    # Messages only (backward-compatible)
    python -m eval.retrieval.extract_real_corpus \\
        [--limit N] [--since YYYY-MM-DD] \\
        [--topic <uuid>] [--thread-root <uuid>] \\
        [--out eval/retrieval/real_corpus.yaml]

    # Include non-message searchable rows (M4 gate #2)
    python -m eval.retrieval.extract_real_corpus \\
        --include-non-message [--non-message-limit N] \\
        [--source-types memory,observation,theme,...] \\
        [--out eval/retrieval/real_corpus.yaml]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NO_TOPIC_SENTINEL = "no_topic"
DEFAULT_OUT = "eval/retrieval/real_corpus.yaml"
DEFAULT_LIMIT = 300
DEFAULT_NON_MESSAGE_LIMIT = 100
_ALL_SOURCE_TYPES = (
    "memory",
    "observation",
    "distillation",
    "artifact",
    "conversation_note",
    "theme",
)


def _get_db_url() -> str:
    """Resolve DIRECT_DATABASE_URL the same way DbBackedRetriever does."""
    db_url = None
    try:
        import importlib

        _cfg = importlib.import_module("app.config")
        db_url = _cfg.get_settings().direct_database_url
    except (ImportError, ModuleNotFoundError, AttributeError):
        db_url = None
    if not db_url:
        db_url = os.environ.get("DIRECT_DATABASE_URL")
    if not db_url:
        raise SystemExit(
            "DIRECT_DATABASE_URL must be set (or app.config must provide it) "
            "to extract a real corpus."
        )
    return db_url


def _build_query(
    *,
    limit: int,
    since: str | None,
    topic: str | None,
    thread_root: str | None,
) -> tuple[str, list[Any]]:
    """Build the SELECT against the messages table with privacy-safe filters."""
    where = ["deleted_at IS NULL", "search_suppressed_at IS NULL"]
    params: list[Any] = []

    if since is not None:
        where.append("sent_at >= %s")
        params.append(since)
    if topic is not None:
        where.append("topic_id = %s")
        params.append(topic)
    if thread_root is not None:
        # The chain rooted at thread_root: either the root itself or any
        # message that (transitively) replies into it. We can't express the
        # transitive walk in SQL cheaply, so fetch the candidate root plus its
        # direct/indirect replies via a recursive CTE.
        where.append(
            "id IN ("
            "WITH RECURSIVE chain AS ("
            "  SELECT id FROM messages WHERE id = %s "
            "  UNION ALL "
            "  SELECT m.id FROM messages m JOIN chain c ON m.in_reply_to = c.id"
            ") SELECT id FROM chain)"
        )
        params.append(thread_root)

    where_clause = " AND ".join(where)
    sql = (
        "SELECT id, content, sender_id, recipient_id, direction, topic_id, "
        "sent_at, in_reply_to, media_analysis, bot_id "
        f"FROM messages WHERE {where_clause} "
        "ORDER BY sent_at DESC LIMIT %s"
    )
    params.append(limit)
    return sql, params


def _build_non_message_query(
    *,
    limit: int,
    source_types: tuple[str, ...],
    topic: str | None,
) -> tuple[str, list[Any]]:
    """Build a SELECT against mediator.v_searchable_content for non-message rows."""
    where = ["sc.source_type <> 'message'"]
    params: list[Any] = []

    where.append("sc.source_type = ANY(%s)")
    params.append(list(source_types))

    if topic is not None:
        where.append("sc.topic_id = %s")
        params.append(topic)

    where_clause = " AND ".join(where)
    sql = (
        "SELECT sc.source_type, sc.source_id, sc.message_id, "
        "sc.sent_at, sc.source_created_at, sc.source_updated_at, "
        "sc.sort_at, sc.canonical_text AS content, "
        "sc.bot_id, sc.topic_id, sc.dyad_id "
        f"FROM mediator.v_searchable_content sc "
        f"WHERE {where_clause} "
        "ORDER BY sc.sort_at DESC NULLS LAST LIMIT %s"
    )
    params.append(limit)
    return sql, params


def _resolve_root(
    msg_id: str,
    parent_of: dict[str, str | None],
    memo: dict[str, str],
) -> str:
    """Walk in_reply_to to the root id, memoizing and guarding against cycles."""
    if msg_id in memo:
        return memo[msg_id]
    path: list[str] = []
    cur: str | None = msg_id
    seen: set[str] = set()
    while cur is not None and cur in parent_of and cur not in seen:
        seen.add(cur)
        path.append(cur)
        parent = parent_of[cur]
        if parent is None:
            break
        if parent not in parent_of:
            break
        cur = parent
    root = cur if cur is not None and cur in parent_of else msg_id
    for node in path:
        memo[node] = root
    memo[msg_id] = root
    return root


def _name_map(conn: Any, user_ids: set[str]) -> dict[str, str]:
    """Resolve user uuids to users.name. Missing ids simply absent from map."""
    names: dict[str, str] = {}
    ids = [u for u in user_ids if u]
    if not ids:
        return names
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM users WHERE id = ANY(%s)", (ids,))
        for uid, name in cur.fetchall():
            names[str(uid)] = name
    return names


def _label(
    user_id: str | None,
    direction: str | None,
    names: dict[str, str],
) -> str:
    """Resolve a participant to a stable, deterministic display label."""
    if user_id and str(user_id) in names:
        return names[str(user_id)]
    role = (direction or "unknown").strip() or "unknown"
    if user_id:
        return f"{role}:{str(user_id)[:8]}"
    return f"{role}:unknown"


def _extract_messages(
    conn: Any,
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, str | None], set[str], set[str], list[datetime]]:
    """Extract message rows and return (entries, parent_of, threads, topics, dates)."""
    sql, params = _build_query(
        limit=args.limit,
        since=args.since,
        topic=args.topic,
        thread_root=args.thread_root,
    )

    rows: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        for raw in cur.fetchall():
            rows.append(dict(zip(cols, raw)))

    # Resolve participant names.
    user_ids: set[str] = set()
    for r in rows:
        if r.get("sender_id"):
            user_ids.add(str(r["sender_id"]))
        if r.get("recipient_id"):
            user_ids.add(str(r["recipient_id"]))
    names = _name_map(conn, user_ids)

    # Build a parent map for thread-root synthesis.
    parent_of: dict[str, str | None] = {}
    for r in rows:
        mid = str(r["id"])
        irt = r.get("in_reply_to")
        parent_of[mid] = str(irt) if irt is not None else None

    memo: dict[str, str] = {}

    entries: list[dict[str, Any]] = []
    threads: set[str] = set()
    topics: set[str] = set()
    dates: list[datetime] = []

    for r in rows:
        mid = str(r["id"])
        thread_id = _resolve_root(mid, parent_of, memo)
        topic_raw = r.get("topic_id")
        topic_id = str(topic_raw) if topic_raw is not None else NO_TOPIC_SENTINEL
        sent_at = r["sent_at"]
        if isinstance(sent_at, datetime):
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            sent_at_iso = sent_at.isoformat()
            dates.append(sent_at)
        else:
            sent_at_iso = str(sent_at)

        entry: dict[str, Any] = {
            "id": mid,
            "thread_id": thread_id,
            "topic_id": topic_id,
            "sender": _label(r.get("sender_id"), r.get("direction"), names),
            "recipient": _label(r.get("recipient_id"), r.get("direction"), names),
            "sent_at": sent_at_iso,
            "content": r.get("content") or "",
        }
        ma = r.get("media_analysis")
        if ma is not None:
            entry["media_analysis"] = ma
        entries.append(entry)
        threads.add(thread_id)
        topics.add(topic_id)

    return entries, parent_of, threads, topics, dates


def _extract_non_message_rows(
    conn: Any,
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], int, int]:
    """Extract non-message searchable rows from v_searchable_content.

    Returns (entries, source_type_count, total_rows).
    """
    source_types: tuple[str, ...]
    if args.source_types:
        source_types = tuple(
            s.strip() for s in args.source_types.split(",") if s.strip() in _ALL_SOURCE_TYPES
        )
    else:
        source_types = _ALL_SOURCE_TYPES

    if not source_types:
        return [], 0, 0

    sql, params = _build_non_message_query(
        limit=args.non_message_limit,
        source_types=source_types,
        topic=args.topic,
    )

    rows: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        for raw in cur.fetchall():
            rows.append(dict(zip(cols, raw)))

    entries: list[dict[str, Any]] = []
    source_types_seen: set[str] = set()

    for row in rows:
        st = row["source_type"]
        sid = str(row["source_id"])
        topic_raw = row.get("topic_id")
        topic_id = str(topic_raw) if topic_raw is not None else NO_TOPIC_SENTINEL

        # Determine the best timestamp for ordering.
        ts = (
            row.get("sent_at")
            or row.get("source_updated_at")
            or row.get("source_created_at")
            or row.get("sort_at")
        )
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_iso = ts.isoformat()
        else:
            ts_iso = str(ts) if ts else None

        entry: dict[str, Any] = {
            "id": sid,
            "source_type": st,
            "topic_id": topic_id,
            "content": row.get("content") or "",
            "created_at": ts_iso,
        }
        # Attach extra_scope fields for DB-backed golden cases.
        bot_id = row.get("bot_id")
        if bot_id is not None:
            entry["bot_id"] = str(bot_id)
        dyad_id = row.get("dyad_id")
        if dyad_id is not None:
            entry["dyad_id"] = str(dyad_id)

        entries.append(entry)
        source_types_seen.add(st)

    return entries, len(source_types_seen), len(rows)


def extract(args: argparse.Namespace) -> int:
    db_url = _get_db_url()

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "psycopg is required to extract a real corpus. "
            "Install with: pip install psycopg[binary]"
        ) from exc

    import yaml

    with psycopg.connect(db_url) as conn:
        message_entries, _parent_of, threads, topics, dates = _extract_messages(conn, args)

        non_message_entries: list[dict[str, Any]] = []
        nm_source_type_count = 0
        nm_total = 0
        if args.include_non_message:
            non_message_entries, nm_source_type_count, nm_total = _extract_non_message_rows(
                conn, args
            )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    output: dict[str, Any] = {"messages": message_entries}
    if non_message_entries:
        output["non_message_sources"] = non_message_entries

    with open(out_path, "w") as f:
        yaml.safe_dump(
            output,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )

    # ── Sanitized-only summary (never prints message content) ────────────
    if dates:
        lo = min(dates).isoformat()
        hi = max(dates).isoformat()
        date_range = f"{lo} .. {hi}"
    else:
        date_range = "(none)"
    print(f"Wrote {len(message_entries)} messages to {out_path}")
    print(f"  threads: {len(threads)}")
    print(f"  topics:  {len(topics)}")
    print(f"  date range: {date_range}")
    if non_message_entries:
        print(
            f"  non-message rows: {nm_total} total ({nm_source_type_count} "
            f"source types) across {_ALL_SOURCE_TYPES}"
        )
    print("  NOTE: this file contains REAL user data and is gitignored.")
    print("  Delete it after labeling is complete.")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m eval.retrieval.extract_real_corpus",
        description=(
            "Extract a REAL-data corpus from the production messages table "
            "and optionally from mediator.v_searchable_content for non-message "
            "searchable rows. ⚠️ WRITES REAL INTIMATE USER DATA TO DISK IN "
            "PLAINTEXT — the output is gitignored and should be deleted after "
            "labeling. Bounded by default (never unbounded)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max messages to extract (default {DEFAULT_LIMIT}; never unbounded).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only messages with sent_at >= this date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--topic",
        default=None,
        help="Restrict to a single topic_id (uuid).",
    )
    parser.add_argument(
        "--thread-root",
        default=None,
        help="Restrict to the reply chain rooted at this message id (uuid).",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help=f"Output YAML path (default {DEFAULT_OUT}; gitignored).",
    )
    parser.add_argument(
        "--include-non-message",
        action="store_true",
        default=False,
        help=(
            "Also extract non-message searchable rows from "
            "mediator.v_searchable_content (memories, observations, "
            "distillations, artifacts, conversation notes, themes)."
        ),
    )
    parser.add_argument(
        "--non-message-limit",
        type=int,
        default=DEFAULT_NON_MESSAGE_LIMIT,
        help=(
            f"Max non-message rows per source type (default "
            f"{DEFAULT_NON_MESSAGE_LIMIT}; never unbounded)."
        ),
    )
    parser.add_argument(
        "--source-types",
        default=None,
        help=(
            "Comma-separated source types to extract from non-message sources "
            f"(default: all of {', '.join(_ALL_SOURCE_TYPES)})."
        ),
    )
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be a positive integer (extraction is never unbounded).")
    if args.non_message_limit <= 0:
        parser.error(
            "--non-message-limit must be a positive integer (extraction is never unbounded)."
        )
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return extract(args)


if __name__ == "__main__":
    sys.exit(main())
