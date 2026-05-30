"""Navigation evaluation: schemas, loader, reference implementation, runner, and CLI.

Defines the seven nav operations, the NavCase/NavGoldenSet models,
a validated loader, the PythonNavReference reference implementation,
and run_nav_eval with a CLI entrypoint.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel

from eval.retrieval.metrics import contiguous_boundary_ok, exact_ordered_match
from eval.retrieval.schema import Corpus, CorpusMessage, Scope

# ---------------------------------------------------------------------------
# Seven nav operations
# ---------------------------------------------------------------------------

from typing import Literal as _Literal

NavOp = _Literal[
    "open_thread",
    "messages_before",
    "messages_after",
    "scroll",
    "topic_recent",
    "recent_before_current",
    "before_message_id",
]

# Ops that REQUIRE a non-None `n`.
_N_REQUIRED: set[str] = {
    "messages_before",
    "messages_after",
    "scroll",
    "before_message_id",
}

# Ops where `n` defaults to 20 (the runner can still override).
_N_DEFAULTS_TO_20: set[str] = {"topic_recent", "recent_before_current"}

# Ops where `n` is IGNORED (any value is fine, but typically None).
_N_IGNORED: set[str] = {"open_thread"}


# ---------------------------------------------------------------------------
# Schema models
# ---------------------------------------------------------------------------


class NavCase(BaseModel):
    """A single navigation golden case."""

    id: str
    op: str  # one of NavOp
    anchor: str | None = None
    n: int | None = None
    scope: Scope
    thread_id: str | None = None
    topic_id: str | None = None
    expected_ids_in_order: list[str]
    notes: str | None = None


class NavGoldenSet(BaseModel):
    """A collection of navigation golden cases."""

    cases: list[NavCase]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_nav_golden(path: Path, corpus: Corpus | None = None) -> NavGoldenSet:
    """Load and validate a nav-golden YAML file.

    Args:
        path: Path to nav_golden.yaml.
        corpus: Optional Corpus for id membership validation.

    Returns:
        A validated NavGoldenSet.

    Raises:
        ValueError: If any validation invariant is violated.
    """
    with open(path) as f:
        data = yaml.safe_load(f)
    nav_set = NavGoldenSet.model_validate(data)

    corpus_ids: set[str] | None = None
    if corpus is not None:
        corpus_ids = {m.id for m in corpus.messages}

    for case in nav_set.cases:
        # ---------------------------------------------------------------
        # 1. Every expected id must exist in the corpus.
        # ---------------------------------------------------------------
        if corpus_ids is not None:
            for mid in case.expected_ids_in_order:
                if mid not in corpus_ids:
                    raise ValueError(
                        f"NavCase '{case.id}' references message id "
                        f"'{mid}' which is not in the corpus"
                    )

        # ---------------------------------------------------------------
        # 2. Non-None anchor must exist in the corpus.
        # ---------------------------------------------------------------
        if case.anchor is not None and corpus_ids is not None:
            if case.anchor not in corpus_ids:
                raise ValueError(
                    f"NavCase '{case.id}' has anchor '{case.anchor}' "
                    f"which is not in the corpus"
                )

        # ---------------------------------------------------------------
        # 3. Per-op `n` matrix.
        # ---------------------------------------------------------------
        if case.op in _N_REQUIRED:
            if case.n is None:
                raise ValueError(
                    f"NavCase '{case.id}' op='{case.op}' requires "
                    f"a non-None `n`"
                )

        if case.op in _N_DEFAULTS_TO_20 and case.n is None:
            # Supply the default in-place so callers can rely on it.
            case.n = 20

        # open_thread: n is ignored – nothing to enforce.

        # ---------------------------------------------------------------
        # 4. Scope / id consistency (mirrors load_golden_set).
        # ---------------------------------------------------------------
        if case.scope == "thread" and case.thread_id is None:
            raise ValueError(
                f"NavCase '{case.id}' has scope='thread' but thread_id is None"
            )
        if case.scope == "topic" and case.topic_id is None:
            raise ValueError(
                f"NavCase '{case.id}' has scope='topic' but topic_id is None"
            )

        # ---------------------------------------------------------------
        # 5. expected_ids_in_order must be non-empty.
        # ---------------------------------------------------------------
        if not case.expected_ids_in_order:
            raise ValueError(
                f"NavCase '{case.id}' has empty expected_ids_in_order"
            )

    return nav_set


# ---------------------------------------------------------------------------
# NavReference Protocol
# ---------------------------------------------------------------------------


class NavReference(Protocol):
    """Protocol for navigation reference implementations.

    Each method corresponds to one of the seven NavOp values and returns
    message ids in the order specified by the op's contract.
    """

    def messages_before(self, anchor: str, n: int) -> list[str]: ...
    def messages_after(self, anchor: str, n: int) -> list[str]: ...
    def open_thread(self, anchor: str | None, thread_id: str) -> list[str]: ...
    def scroll(self, anchor: str, n: int) -> list[str]: ...
    def topic_recent(self, topic_id: str, n: int) -> list[str]: ...
    def before_message_id(self, anchor_id: str, n: int) -> list[str]: ...
    def recent_before_current(self, anchor: str, n: int) -> list[str]: ...


# ---------------------------------------------------------------------------
# PythonNavReference — reference implementation
# ---------------------------------------------------------------------------


class PythonNavReference:
    """Reference implementation of all seven nav ops.

    Maintains a sorted-by-sent_at view of the corpus for deterministic
    chronological operations. All methods return message ids in the order
    specified by the golden contract.
    """

    def __init__(self, corpus: Corpus) -> None:
        self._corpus = corpus
        # Chronological order (sent_at ascending, id ascending as tiebreaker).
        self._sorted: list[CorpusMessage] = sorted(
            corpus.messages, key=lambda m: (m.sent_at, m.id)
        )
        self._by_id: dict[str, CorpusMessage] = {m.id: m for m in corpus.messages}
        # Index of each message in the sorted list.
        self._idx: dict[str, int] = {m.id: i for i, m in enumerate(self._sorted)}

    # ------------------------------------------------------------------
    # messages_before — n messages chronologically before anchor (exclusive).
    # ------------------------------------------------------------------
    def messages_before(self, anchor: str, n: int) -> list[str]:
        anchor_idx = self._idx[anchor]
        start = max(0, anchor_idx - n)
        return [m.id for m in self._sorted[start:anchor_idx]]

    # ------------------------------------------------------------------
    # messages_after — n messages chronologically after anchor (exclusive).
    # ------------------------------------------------------------------
    def messages_after(self, anchor: str, n: int) -> list[str]:
        anchor_idx = self._idx[anchor]
        return [m.id for m in self._sorted[anchor_idx + 1 : anchor_idx + 1 + n]]

    # ------------------------------------------------------------------
    # open_thread — all messages in a thread, chronological order.
    # ------------------------------------------------------------------
    def open_thread(self, anchor: str | None, thread_id: str) -> list[str]:
        return [
            m.id
            for m in self._sorted
            if m.thread_id == thread_id
        ]

    # ------------------------------------------------------------------
    # scroll — n messages centered on anchor (±n/2, chronological,
    # anchor included).
    # ------------------------------------------------------------------
    def scroll(self, anchor: str, n: int) -> list[str]:
        anchor_idx = self._idx[anchor]
        half = n // 2
        start = max(0, anchor_idx - half)
        end = min(len(self._sorted), anchor_idx + half + 1)
        # If end doesn't give enough after, adjust start backward.
        after_count = end - anchor_idx - 1
        if after_count < half:
            start = max(0, start - (half - after_count))
        # Ensure we have n total (or as many as possible).
        end = min(len(self._sorted), start + n)
        return [m.id for m in self._sorted[start:end]]

    # ------------------------------------------------------------------
    # topic_recent — n most-recent messages in a topic, chronological.
    # ------------------------------------------------------------------
    def topic_recent(self, topic_id: str, n: int) -> list[str]:
        # Collect all messages in the topic, sort by sent_at DESC.
        topic_msgs = [m for m in self._corpus.messages if m.topic_id == topic_id]
        topic_msgs.sort(key=lambda m: (m.sent_at, m.id), reverse=True)
        recent = topic_msgs[:n]
        # Return in chronological (ascending) order.
        recent.sort(key=lambda m: (m.sent_at, m.id))
        return [m.id for m in recent]

    # ------------------------------------------------------------------
    # before_message_id — n messages chronologically before a specific
    # message id (exclusive).
    # ------------------------------------------------------------------
    def before_message_id(self, anchor_id: str, n: int) -> list[str]:
        anchor_idx = self._idx[anchor_id]
        start = max(0, anchor_idx - n)
        return [m.id for m in self._sorted[start:anchor_idx]]

    # ------------------------------------------------------------------
    # recent_before_current — n most-recent messages chronologically
    # before anchor (exclusive), sorted chronologically ascending.
    # ------------------------------------------------------------------
    def recent_before_current(self, anchor: str, n: int) -> list[str]:
        anchor_idx = self._idx[anchor]
        start = max(0, anchor_idx - n)
        return [m.id for m in self._sorted[start:anchor_idx]]


# ---------------------------------------------------------------------------
# DbNavAdapter — pgvector Postgres-backed nav ops (read-only)
# ---------------------------------------------------------------------------


class DbNavAdapter:
    """Thin wrapper translating each nav op to read-only SQL against a
    pgvector-enabled Postgres.

    Requires ``DIRECT_DATABASE_URL`` to be set in the environment.  Lazily
    imports ``psycopg`` and ``pgvector`` inside ``__init__`` so the offline
    harness never needs database dependencies at module-load time.

    Raises ``ValueError`` if ``DIRECT_DATABASE_URL`` is unset.
    """

    def __init__(self, corpus: Corpus) -> None:
        import os as _os

        self._corpus = corpus  # kept for interface compatibility
        self._corpus_order = sorted(corpus.messages, key=lambda m: (m.sent_at, m.id))

        db_url = _os.environ.get("DIRECT_DATABASE_URL")
        if not db_url:
            raise ValueError(
                "DIRECT_DATABASE_URL must be set to use DbNavAdapter"
            )

        # Lazy-import database dependencies inside __init__.
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "psycopg is required for DbNavAdapter. "
                "Install with: pip install psycopg[binary]"
            ) from exc

        try:
            import pgvector  # noqa: F401  — registers the vector adapter
        except ImportError as exc:
            raise ImportError(
                "pgvector is required for DbNavAdapter. "
                "Install with: pip install pgvector"
            ) from exc

        self._db_url = db_url

    # ------------------------------------------------------------------
    # messages_before — n messages chronologically before anchor (exclusive).
    # ------------------------------------------------------------------
    def messages_before(self, anchor: str, n: int) -> list[str]:
        import psycopg

        with psycopg.connect(self._db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sent_at FROM messages WHERE id = %s",
                    (anchor,),
                )
                anchor_row = cur.fetchone()
                if anchor_row is None:
                    return []
                cur.execute(
                    "SELECT id FROM messages "
                    "WHERE sent_at < %s "
                    "ORDER BY sent_at DESC, id DESC "
                    "LIMIT %s",
                    (anchor_row[0], n),
                )
                rows = cur.fetchall()
        result = [r[0] for r in rows]
        result.reverse()  # chronological ascending
        return result

    # ------------------------------------------------------------------
    # messages_after — n messages chronologically after anchor (exclusive).
    # ------------------------------------------------------------------
    def messages_after(self, anchor: str, n: int) -> list[str]:
        import psycopg

        with psycopg.connect(self._db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sent_at FROM messages WHERE id = %s",
                    (anchor,),
                )
                anchor_row = cur.fetchone()
                if anchor_row is None:
                    return []
                cur.execute(
                    "SELECT id FROM messages "
                    "WHERE sent_at > %s "
                    "ORDER BY sent_at ASC, id ASC "
                    "LIMIT %s",
                    (anchor_row[0], n),
                )
                rows = cur.fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # open_thread — all messages in a thread, chronological order.
    # ------------------------------------------------------------------
    def open_thread(self, anchor: str | None, thread_id: str) -> list[str]:
        import psycopg

        with psycopg.connect(self._db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM messages WHERE thread_id = %s "
                    "ORDER BY sent_at ASC, id ASC",
                    (thread_id,),
                )
                rows = cur.fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # scroll — n messages centered on anchor (±n/2, chronological,
    # anchor included).
    # ------------------------------------------------------------------
    def scroll(self, anchor: str, n: int) -> list[str]:
        import psycopg

        with psycopg.connect(self._db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sent_at FROM messages WHERE id = %s",
                    (anchor,),
                )
                anchor_row = cur.fetchone()
                if anchor_row is None:
                    return []
                sent = anchor_row[0]
                half = n // 2
                # Count messages before and after.
                cur.execute(
                    "SELECT COUNT(*) FROM messages WHERE sent_at < %s",
                    (sent,),
                )
                before_count = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM messages WHERE sent_at > %s",
                    (sent,),
                )
                after_count = cur.fetchone()[0]
                # Adjust window.
                if after_count < half:
                    before_take = half + (half - after_count)
                else:
                    before_take = half
                before_take = min(before_take, before_count)
                after_take = min(half, after_count)
                # If still short on total, extend after.
                total = before_take + 1 + after_take
                if total < n:
                    after_take = min(after_count, n - before_take - 1)
                # Fetch before.
                cur.execute(
                    "SELECT id FROM messages WHERE sent_at < %s "
                    "ORDER BY sent_at DESC, id DESC LIMIT %s",
                    (sent, before_take),
                )
                before_rows = cur.fetchall()
                before_ids = [r[0] for r in reversed(before_rows)]
                # Fetch anchor + after.
                cur.execute(
                    "SELECT id FROM messages WHERE sent_at >= %s "
                    "ORDER BY sent_at ASC, id ASC LIMIT %s",
                    (sent, after_take + 1),
                )
                after_rows = cur.fetchall()
                after_ids = [r[0] for r in after_rows]
        return before_ids + after_ids

    # ------------------------------------------------------------------
    # topic_recent — n most-recent messages in a topic, chronological.
    # ------------------------------------------------------------------
    def topic_recent(self, topic_id: str, n: int) -> list[str]:
        import psycopg

        with psycopg.connect(self._db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM messages WHERE topic_id = %s "
                    "ORDER BY sent_at DESC, id DESC "
                    "LIMIT %s",
                    (topic_id, n),
                )
                rows = cur.fetchall()
        result = [r[0] for r in rows]
        result.reverse()  # chronological ascending
        return result

    # ------------------------------------------------------------------
    # before_message_id — n messages chronologically before a specific
    # message id (exclusive).
    # ------------------------------------------------------------------
    def before_message_id(self, anchor_id: str, n: int) -> list[str]:
        import psycopg

        with psycopg.connect(self._db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sent_at FROM messages WHERE id = %s",
                    (anchor_id,),
                )
                anchor_row = cur.fetchone()
                if anchor_row is None:
                    return []
                cur.execute(
                    "SELECT id FROM messages "
                    "WHERE sent_at < %s "
                    "ORDER BY sent_at DESC, id DESC "
                    "LIMIT %s",
                    (anchor_row[0], n),
                )
                rows = cur.fetchall()
        result = [r[0] for r in rows]
        result.reverse()  # chronological ascending
        return result

    # ------------------------------------------------------------------
    # recent_before_current — n most-recent messages chronologically
    # before anchor (exclusive), sorted chronologically ascending.
    # ------------------------------------------------------------------
    def recent_before_current(self, anchor: str, n: int) -> list[str]:
        return self.messages_before(anchor, n)


# ---------------------------------------------------------------------------
# NavReport
# ---------------------------------------------------------------------------


class NavReport(BaseModel):
    """Report produced by run_nav_eval."""

    per_case: list[dict[str, Any]]
    pass_rate_exact: float
    pass_rate_boundary: float
    n: int


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_nav_eval(
    adapter: NavReference,
    golden: NavGoldenSet,
    corpus: Corpus,
) -> NavReport:
    """Run navigation evaluation of an adapter against a golden set.

    Args:
        adapter: Any object satisfying the NavReference protocol.
        golden: The navigation golden cases.
        corpus: The corpus of messages.

    Returns:
        NavReport with per-case results and pass rates.
    """
    # Canonical chronological ordering for boundary checks.
    corpus_order = [m.id for m in sorted(corpus.messages, key=lambda m: (m.sent_at, m.id))]

    per_case: list[dict[str, Any]] = []
    exact_passes = 0
    boundary_passes = 0

    for case in golden.cases:
        # Dispatch to the correct method based on op.
        op = case.op
        if op == "open_thread":
            returned = adapter.open_thread(case.anchor, case.thread_id or "")
        elif op == "messages_before":
            returned = adapter.messages_before(case.anchor or "", case.n or 0)
        elif op == "messages_after":
            returned = adapter.messages_after(case.anchor or "", case.n or 0)
        elif op == "scroll":
            returned = adapter.scroll(case.anchor or "", case.n or 0)
        elif op == "topic_recent":
            returned = adapter.topic_recent(case.topic_id or "", case.n or 0)
        elif op == "recent_before_current":
            returned = adapter.recent_before_current(case.anchor or "", case.n or 0)
        elif op == "before_message_id":
            returned = adapter.before_message_id(case.anchor or "", case.n or 0)
        else:
            raise ValueError(f"Unknown nav op: {op}")

        exact = exact_ordered_match(returned, case.expected_ids_in_order)
        boundary = contiguous_boundary_ok(
            returned, case.expected_ids_in_order, corpus_order
        )

        per_case.append({
            "case_id": case.id,
            "op": case.op,
            "pass_exact": exact,
            "pass_boundary": boundary,
            "returned": returned,
            "expected": case.expected_ids_in_order,
            "notes": case.notes,
        })

        if exact:
            exact_passes += 1
        if boundary:
            boundary_passes += 1

    n = len(golden.cases)
    return NavReport(
        per_case=per_case,
        pass_rate_exact=exact_passes / n if n else 0.0,
        pass_rate_boundary=boundary_passes / n if n else 0.0,
        n=n,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_path(relative: str) -> Path:
    """Resolve a relative path against the project root (parent of eval/)."""
    base = Path(__file__).resolve().parent.parent.parent
    return base / relative


def _build_nav_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run navigation evaluation harness."
    )
    parser.add_argument(
        "--adapter",
        choices=["reference", "db"],
        default="reference",
        help="NavReference adapter to evaluate.",
    )
    parser.add_argument(
        "--nav-golden",
        type=Path,
        default=None,
        help="Path to nav golden YAML (default: eval/retrieval/nav_golden.yaml).",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Path to corpus YAML (default: eval/retrieval/corpus.yaml).",
    )
    parser.add_argument(
        "--assert-nav-gate",
        action="store_true",
        default=False,
        help="Exit non-zero unless aggregate exact pass rate is exactly 1.0.",
    )
    return parser


def _load_corpus(path: Path) -> Corpus:
    """Load corpus YAML."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return Corpus.model_validate(data)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for nav eval.

    With --assert-nav-gate, exits non-zero unless aggregate exact pass rate
    is exactly 1.0.  Without the flag, always exits 0 (report-only mode).
    """
    parser = _build_nav_parser()
    args = parser.parse_args(argv)

    corpus_path: Path = args.corpus or _default_path("eval/retrieval/corpus.yaml")
    nav_golden_path: Path = args.nav_golden or _default_path(
        "eval/retrieval/nav_golden.yaml"
    )

    corpus = _load_corpus(corpus_path)
    golden = load_nav_golden(nav_golden_path, corpus=corpus)

    adapter: NavReference
    if args.adapter == "reference":
        adapter = PythonNavReference(corpus)
    elif args.adapter == "db":
        try:
            adapter = DbNavAdapter(corpus)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    else:
        print(f"Unknown adapter: {args.adapter}", file=sys.stderr)
        return 1

    report = run_nav_eval(adapter, golden, corpus)

    # Print per-case table.
    print(f"{'case_id':<10} {'op':<25} {'exact':<8} {'boundary':<10} {'returned':<50} {'expected':<50} {'notes'}")
    print("-" * 160)
    for r in report.per_case:
        returned_str = ",".join(r["returned"])
        expected_str = ",".join(r["expected"])
        print(
            f"{r['case_id']:<10} {r['op']:<25} "
            f"{'PASS' if r['pass_exact'] else 'FAIL':<8} "
            f"{'PASS' if r['pass_boundary'] else 'FAIL':<10} "
            f"{returned_str[:48]:<50} {expected_str[:48]:<50} "
            f"{r.get('notes') or ''}"
        )

    print()
    print(f"Pass rate (exact):    {report.pass_rate_exact:.2%} ({int(report.pass_rate_exact * report.n)}/{report.n})")
    print(f"Pass rate (boundary): {report.pass_rate_boundary:.2%} ({int(report.pass_rate_boundary * report.n)}/{report.n})")

    # Gate assertion.
    if args.assert_nav_gate:
        if report.pass_rate_exact < 1.0:
            print(
                f"NAV GATE FAILED: exact pass rate {report.pass_rate_exact:.2%} < 1.0",
                file=sys.stderr,
            )
            return 1
        print("NAV GATE PASSED: exact pass rate 100%", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
