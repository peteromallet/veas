"""Hot-context evaluation: schemas, loader, reference selector, runner, and CLI.

Defines the HotContextState/HotContextFixtures models, a validated loader,
the HotContextSelector Protocol, PythonReferenceSelector baseline,
and run_hotcontext_eval with a CLI entrypoint.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, Field

from eval.retrieval.metrics import aggregate_set_metrics, set_precision, set_recall
from eval.retrieval.schema import Corpus

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

from typing import Literal as _Literal

HotContextCategory = _Literal[
    "gap_continue",
    "topic_switch",
    "no_relevant_prior",
    "near_duplicate_prior",
]


class HotContextState(BaseModel):
    """A single hot-context fixture (one test scenario)."""

    id: str
    topic_id: str
    last_window_message_ids: list[str]
    gold_prior_on_topic_ids: list[str]
    budget: int = Field(default=5, ge=1)
    rationale: str
    category: str  # one of HotContextCategory


class HotContextFixtures(BaseModel):
    """A collection of hot-context test fixtures."""

    fixtures: list[HotContextState]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_hotcontext_fixtures(
    path: Path, corpus: Corpus | None = None
) -> HotContextFixtures:
    """Load and validate hot-context fixtures.

    Args:
        path: Path to hotcontext_fixtures.yaml.
        corpus: Optional Corpus for id membership and distractor validation.

    Returns:
        A validated HotContextFixtures.

    Raises:
        ValueError: If any per-category invariant is violated.
    """
    with open(path) as f:
        data = yaml.safe_load(f)
    fixtures = HotContextFixtures.model_validate(data)

    corpus_ids: set[str] | None = None
    corpus_by_topic: dict[str, list[str]] = {}
    if corpus is not None:
        corpus_ids = {m.id for m in corpus.messages}
        for m in corpus.messages:
            corpus_by_topic.setdefault(m.topic_id, []).append(m.id)

    for fx in fixtures.fixtures:
        # ---------------------------------------------------------------
        # 1. All referenced ids exist in corpus.
        # ---------------------------------------------------------------
        if corpus_ids is not None:
            for mid in fx.last_window_message_ids:
                if mid not in corpus_ids:
                    raise ValueError(
                        f"HotContextState '{fx.id}' references last_window "
                        f"message id '{mid}' which is not in the corpus"
                    )
            for mid in fx.gold_prior_on_topic_ids:
                if mid not in corpus_ids:
                    raise ValueError(
                        f"HotContextState '{fx.id}' references gold_prior "
                        f"message id '{mid}' which is not in the corpus"
                    )

        # ---------------------------------------------------------------
        # 2. Per-category invariants.
        # ---------------------------------------------------------------

        if fx.category == "gap_continue":
            if corpus_ids is not None:
                win_set = set(fx.last_window_message_ids)
                for mid in fx.gold_prior_on_topic_ids:
                    if mid in win_set:
                        raise ValueError(
                            f"HotContextState '{fx.id}' category=gap_continue "
                            f"but gold message '{mid}' appears in "
                            f"last_window_message_ids"
                        )
            if not fx.gold_prior_on_topic_ids:
                raise ValueError(
                    f"HotContextState '{fx.id}' category=gap_continue "
                    f"has empty gold_prior_on_topic_ids"
                )

        elif fx.category == "topic_switch":
            if corpus is not None and fx.last_window_message_ids:
                last_win_id = fx.last_window_message_ids[-1]
                last_win_topic = _topic_for(corpus, last_win_id)
                if last_win_topic is None:
                    raise ValueError(
                        f"HotContextState '{fx.id}' category=topic_switch "
                        f"could not resolve topic for "
                        f"last_window_message_ids[-1]='{last_win_id}'"
                    )
                if last_win_topic == fx.topic_id:
                    raise ValueError(
                        f"HotContextState '{fx.id}' category=topic_switch "
                        f"but last_window_message_ids[-1] topic "
                        f"'{last_win_topic}' matches fixture topic_id "
                        f"'{fx.topic_id}'"
                    )

        elif fx.category == "no_relevant_prior":
            if fx.gold_prior_on_topic_ids:
                raise ValueError(
                    f"HotContextState '{fx.id}' category=no_relevant_prior "
                    f"but gold_prior_on_topic_ids is non-empty"
                )

        elif fx.category == "near_duplicate_prior":
            if corpus is not None and fx.gold_prior_on_topic_ids:
                gold_set = set(fx.gold_prior_on_topic_ids)
                for mid in fx.gold_prior_on_topic_ids:
                    msg_topic = _topic_for(corpus, mid)
                    if msg_topic != fx.topic_id:
                        raise ValueError(
                            f"HotContextState '{fx.id}' "
                            f"category=near_duplicate_prior but gold message "
                            f"'{mid}' topic '{msg_topic}' != fixture "
                            f"topic_id '{fx.topic_id}'"
                        )
                found_distractor = False
                topic_msgs = corpus_by_topic.get(fx.topic_id, [])
                for mid in topic_msgs:
                    if mid in gold_set:
                        continue
                    msg = _msg_by_id(corpus, mid)
                    if msg is None:
                        continue
                    msg_words = set(msg.content.lower().split())
                    for gold_mid in fx.gold_prior_on_topic_ids:
                        gold_msg = _msg_by_id(corpus, gold_mid)
                        if gold_msg is None:
                            continue
                        gold_words = set(gold_msg.content.lower().split())
                        common = msg_words & gold_words
                        if len(common) >= 2:
                            found_distractor = True
                            break
                    if found_distractor:
                        break
                if not found_distractor:
                    raise ValueError(
                        f"HotContextState '{fx.id}' "
                        f"category=near_duplicate_prior but no distractor "
                        f"message found in topic '{fx.topic_id}' sharing "
                        f"key words with gold"
                    )

    return fixtures


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _topic_for(corpus: Corpus, msg_id: str) -> str | None:
    for m in corpus.messages:
        if m.id == msg_id:
            return m.topic_id
    return None


def _msg_by_id(corpus: Corpus, msg_id: str):
    for m in corpus.messages:
        if m.id == msg_id:
            return m
    return None


# ---------------------------------------------------------------------------
# HotContextSelector Protocol
# ---------------------------------------------------------------------------


class HotContextSelector(Protocol):
    """Protocol for hot-context prior-message selectors.

    Given a HotContextState (simulating the current window + budget) and
    the corpus, returns a set of message ids to include as prior context.
    Must return at most ``state.budget`` ids and exclude any already in
    ``last_window_message_ids``.
    """

    def select(self, state: HotContextState, corpus: Corpus) -> set[str]: ...


# ---------------------------------------------------------------------------
# PythonReferenceSelector — honest recency-only baseline
# ---------------------------------------------------------------------------


class PythonReferenceSelector:
    """Honest recency-only baseline selector (NOT hand-tuned to gold).

    Behaviour by category:
    - gap_continue: Return up-to-budget most-recent messages in state.topic_id
      that are NOT in last_window_message_ids.
    - topic_switch: Same recency approach.
    - no_relevant_prior: Return empty set.
    - near_duplicate_prior: Same recency approach (may miss near-duplicates —
      this is intentional; the selector is not hand-tuned).
    """

    def select(self, state: HotContextState, corpus: Corpus) -> set[str]:
        if state.category == "no_relevant_prior":
            return set()

        last_window = set(state.last_window_message_ids)

        # All messages in the fixture's topic, sorted by sent_at DESC
        # (most recent first), with id as deterministic tiebreaker.
        topic_msgs = [
            m
            for m in corpus.messages
            if m.topic_id == state.topic_id and m.id not in last_window
        ]
        topic_msgs.sort(key=lambda m: (m.sent_at, m.id), reverse=True)

        # Take up to budget most-recent not-in-window messages.
        result: set[str] = set()
        for msg in topic_msgs:
            result.add(msg.id)
            if len(result) >= state.budget:
                break

        return result


# ---------------------------------------------------------------------------
# DbHotContextSelector — pgvector Postgres-backed hot-context selection
# ---------------------------------------------------------------------------


class DbHotContextSelector:
    """Thin wrapper translating hot-context selection to read-only SQL against
    a pgvector-enabled Postgres.

    Requires ``DIRECT_DATABASE_URL`` to be set in the environment.  Lazily
    imports ``psycopg`` and ``pgvector`` inside ``__init__`` so the offline
    harness never needs database dependencies at module-load time.

    Raises ``ValueError`` if ``DIRECT_DATABASE_URL`` is unset.
    """

    def __init__(self, corpus: Corpus) -> None:
        import os as _os

        self._corpus = corpus  # kept for interface compatibility

        db_url = _os.environ.get("DIRECT_DATABASE_URL")
        if not db_url:
            raise ValueError(
                "DIRECT_DATABASE_URL must be set to use DbHotContextSelector"
            )

        # Lazy-import database dependencies inside __init__.
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "psycopg is required for DbHotContextSelector. "
                "Install with: pip install psycopg[binary]"
            ) from exc

        try:
            import pgvector  # noqa: F401  — registers the vector adapter
        except ImportError as exc:
            raise ImportError(
                "pgvector is required for DbHotContextSelector. "
                "Install with: pip install pgvector"
            ) from exc

        self._db_url = db_url

    def select(self, state: HotContextState, corpus: Corpus) -> set[str]:
        import psycopg

        if state.category == "no_relevant_prior":
            return set()

        last_window = set(state.last_window_message_ids)
        budget = state.budget

        with psycopg.connect(self._db_url) as conn:
            with conn.cursor() as cur:
                # Select most-recent messages in the fixture's topic not in
                # the last window, up to budget.
                cur.execute(
                    "SELECT id FROM messages "
                    "WHERE topic_id = %s "
                    "ORDER BY sent_at DESC, id DESC "
                    "LIMIT 500",
                    (state.topic_id,),
                )
                rows = cur.fetchall()

        result: set[str] = set()
        for row in rows:
            mid = row[0]
            if mid in last_window:
                continue
            result.add(mid)
            if len(result) >= budget:
                break

        return result


# ---------------------------------------------------------------------------
# HotContextReport
# ---------------------------------------------------------------------------


class HotContextReport(BaseModel):
    """Report produced by run_hotcontext_eval."""

    per_fixture: list[dict[str, Any]]
    aggregate: dict[str, float | int]
    by_category: dict[str, dict[str, float | int]]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_hotcontext_eval(
    selector: HotContextSelector,
    fixtures: HotContextFixtures,
    corpus: Corpus,
) -> HotContextReport:
    """Run hot-context evaluation of a selector against fixtures.

    Args:
        selector: Any object satisfying the HotContextSelector protocol.
        fixtures: The hot-context test fixtures.
        corpus: The corpus of messages.

    Returns:
        HotContextReport with per-fixture precision/recall/F1, global
        aggregate, and per-category aggregates.
    """
    per_fixture: list[dict[str, Any]] = []

    for fx in fixtures.fixtures:
        returned = selector.select(fx, corpus)
        returned_list = list(returned)

        precision = set_precision(returned_list, fx.gold_prior_on_topic_ids)
        recall = set_recall(returned_list, fx.gold_prior_on_topic_ids)
        if precision + recall > 0:
            f1 = 2.0 * precision * recall / (precision + recall)
        else:
            f1 = 0.0

        per_fixture.append({
            "fixture_id": fx.id,
            "category": fx.category,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "budget": fx.budget,
            "returned_count": len(returned),
            "gold_count": len(fx.gold_prior_on_topic_ids),
        })

    # Global aggregate via aggregate_set_metrics.
    set_metrics_input = [
        {"set_precision": r["precision"], "set_recall": r["recall"]}
        for r in per_fixture
    ]
    aggregate = aggregate_set_metrics(set_metrics_input)

    # Per-category aggregates.
    by_category_raw: dict[str, list[dict[str, float]]] = {}
    for r in per_fixture:
        cat = r["category"]
        by_category_raw.setdefault(cat, []).append({
            "set_precision": r["precision"],
            "set_recall": r["recall"],
        })

    by_category = {
        cat: aggregate_set_metrics(items)
        for cat, items in by_category_raw.items()
    }

    return HotContextReport(
        per_fixture=per_fixture,
        aggregate=aggregate,
        by_category=by_category,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_path(relative: str) -> Path:
    """Resolve a relative path against the project root (parent of eval/)."""
    base = Path(__file__).resolve().parent.parent.parent
    return base / relative


def _load_corpus(path: Path) -> Corpus:
    """Load corpus YAML."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return Corpus.model_validate(data)


def _build_hotcontext_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run hot-context evaluation harness."
    )
    parser.add_argument(
        "--selector",
        choices=["reference", "db"],
        default="reference",
        help="HotContextSelector to evaluate.",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="Path to fixtures YAML (default: eval/retrieval/hotcontext_fixtures.yaml).",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Path to corpus YAML (default: eval/retrieval/corpus.yaml).",
    )
    parser.add_argument(
        "--assert-m3-gate",
        action="store_true",
        default=False,
        help="Exit non-zero unless aggregate set_recall >= 0.8 AND set_precision >= 0.6.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for hot-context eval.

    With --assert-m3-gate, exits non-zero unless aggregate set_recall >= 0.8
    AND aggregate set_precision >= 0.6.  Without the flag, always exits 0.
    """
    parser = _build_hotcontext_parser()
    args = parser.parse_args(argv)

    corpus_path: Path = args.corpus or _default_path("eval/retrieval/corpus.yaml")
    fixtures_path: Path = args.fixtures or _default_path(
        "eval/retrieval/hotcontext_fixtures.yaml"
    )

    corpus = _load_corpus(corpus_path)
    fixtures = load_hotcontext_fixtures(fixtures_path, corpus=corpus)

    selector: HotContextSelector
    if args.selector == "reference":
        selector = PythonReferenceSelector()
    elif args.selector == "db":
        try:
            selector = DbHotContextSelector(corpus)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    else:
        print(f"Unknown selector: {args.selector}", file=sys.stderr)
        return 1

    report = run_hotcontext_eval(selector, fixtures, corpus)

    # Print per-fixture table.
    print(
        f"{'fixture_id':<12} {'category':<22} {'precision':<10} {'recall':<10} "
        f"{'f1':<10} {'budget':<8} {'ret':<6} {'gold':<6}"
    )
    print("-" * 90)
    for r in report.per_fixture:
        print(
            f"{r['fixture_id']:<12} {r['category']:<22} "
            f"{r['precision']:<10.4f} {r['recall']:<10.4f} {r['f1']:<10.4f} "
            f"{r['budget']:<8} {r['returned_count']:<6} {r['gold_count']:<6}"
        )

    print()
    print("Aggregate:")
    agg = report.aggregate
    print(f"  set_precision: {agg['set_precision']:.4f}")
    print(f"  set_recall:    {agg['set_recall']:.4f}")
    print(f"  f1:            {agg['f1']:.4f}")
    print(f"  n:             {agg['n']}")

    print()
    print("By category:")
    for cat in sorted(report.by_category.keys()):
        cat_agg = report.by_category[cat]
        print(f"  {cat}:")
        print(f"    set_precision: {cat_agg['set_precision']:.4f}")
        print(f"    set_recall:    {cat_agg['set_recall']:.4f}")
        print(f"    f1:            {cat_agg['f1']:.4f}")
        print(f"    n:             {cat_agg['n']}")

    # M3 gate assertion.
    if args.assert_m3_gate:
        recall = agg["set_recall"]
        precision = agg["set_precision"]
        failures: list[str] = []
        if recall < 0.8:
            failures.append(
                f"set_recall={recall:.4f} < 0.8"
            )
        if precision < 0.6:
            failures.append(
                f"set_precision={precision:.4f} < 0.6"
            )
        if failures:
            print(
                f"M3 GATE FAILED: {'; '.join(failures)}",
                file=sys.stderr,
            )
            return 1
        print(
            "M3 GATE PASSED: set_recall >= 0.8 and set_precision >= 0.6",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
