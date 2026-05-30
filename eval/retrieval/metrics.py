"""Retrieval evaluation metrics.

All functions are deterministic: given the same inputs they always produce
the same outputs. No randomness, no external dependencies.
"""

from __future__ import annotations


def recall_at_k(ranked_ids: list[str], expected: list[str], k: int) -> float:
    """Fraction of expected message ids appearing in the first k ranked results.

    Args:
        ranked_ids: Ranked list of retrieved message ids (rank 1 = index 0).
        expected: List of message ids that should have been retrieved.
        k: Cutoff rank (1-indexed).

    Returns:
        Float in [0.0, 1.0]. Returns 0.0 if expected is empty.
    """
    if not expected:
        return 0.0
    top_k = set(ranked_ids[:k])
    hits = sum(1 for eid in expected if eid in top_k)
    return hits / len(expected)


def reciprocal_rank(ranked_ids: list[str], expected: list[str]) -> float:
    """Reciprocal rank of the first expected hit.

    Args:
        ranked_ids: Ranked list of retrieved message ids (rank 1 = index 0).
        expected: List of message ids that should have been retrieved.

    Returns:
        1/rank of the first expected id found (1-indexed), or 0.0 if none found.
    """
    if not expected:
        return 0.0
    expected_set = set(expected)
    for i, rid in enumerate(ranked_ids):
        if rid in expected_set:
            return 1.0 / (i + 1)
    return 0.0


def aggregate(per_case_results: list[dict]) -> dict:
    """Aggregate per-case metric dicts into summary statistics.

    Each per-case dict should contain:
        recall_at_1, recall_at_5, recall_at_10, reciprocal_rank

    Returns:
        Dict with keys: recall@1, recall@5, recall@10, mrr, n
        All means are macro-averaged (mean of per-case values).
    """
    n = len(per_case_results)
    if n == 0:
        return {"recall@1": 0.0, "recall@5": 0.0, "recall@10": 0.0, "mrr": 0.0, "n": 0}

    sum_r1 = sum(r["recall_at_1"] for r in per_case_results)
    sum_r5 = sum(r["recall_at_5"] for r in per_case_results)
    sum_r10 = sum(r["recall_at_10"] for r in per_case_results)
    sum_rr = sum(r["reciprocal_rank"] for r in per_case_results)

    return {
        "recall@1": sum_r1 / n,
        "recall@5": sum_r5 / n,
        "recall@10": sum_r10 / n,
        "mrr": sum_rr / n,
        "n": n,
    }


def aggregate_by_query_type(
    per_case_results: list[dict],
) -> dict[str, dict]:
    """Aggregate per-case results grouped by query_type.

    Each per-case dict should contain:
        recall_at_1, recall_at_5, recall_at_10, reciprocal_rank, query_type

    Returns:
        Dict mapping query_type string to aggregate stats dict
        (same shape as aggregate() return, plus 'n').
    """
    return aggregate_by_group(per_case_results, "query_type")


def exact_ordered_match(returned: list[str], expected: list[str]) -> bool:
    """Strict list equality between returned and expected.

    Args:
        returned: The list of message ids returned by a retriever.
        expected: The expected list of message ids.

    Returns:
        True iff both lists have identical elements in identical order.
    """
    return returned == expected


def contiguous_boundary_ok(
    returned: list[str], expected: list[str], corpus_order: list[str]
) -> bool:
    """Returned is a contiguous subsequence of corpus_order with correct boundaries.

    Checks that:
    - ``returned`` is a contiguous subsequence of ``corpus_order``.
    - ``returned[0] == expected[0]``.
    - ``returned[-1] == expected[-1]``.

    Interior messages are NOT checked against expected — only boundary position
    matters. This is useful when the exact interior retrieval order is flexible
    but the start and end must be pinned.

    Args:
        returned: The list of message ids returned by a retriever.
        expected: The expected list of message ids.
        corpus_order: The canonical ordering of all corpus message ids.

    Returns:
        True if all conditions are satisfied.
    """
    if not returned or not expected:
        return False
    if returned[0] != expected[0]:
        return False
    if returned[-1] != expected[-1]:
        return False

    # Check contiguity: returned must appear as a contiguous slice of corpus_order.
    n = len(corpus_order)
    m = len(returned)
    for i in range(n - m + 1):
        if corpus_order[i : i + m] == returned:
            return True
    return False


def set_precision(returned: list[str], expected: list[str]) -> float:
    """Precision: fraction of returned ids that are in expected.

    Args:
        returned: The list of message ids returned by a retriever.
        expected: The expected list of message ids.

    Returns:
        |returned ∩ expected| / |returned|. Returns 1.0 if returned is empty
        (by convention — the retriever made no false-positive claims).
    """
    if not returned:
        return 1.0
    returned_set = set(returned)
    expected_set = set(expected)
    hits = len(returned_set & expected_set)
    return hits / len(returned)


def set_recall(returned: list[str], expected: list[str]) -> float:
    """Recall: fraction of expected ids that appear in returned.

    Args:
        returned: The list of message ids returned by a retriever.
        expected: The expected list of message ids.

    Returns:
        |returned ∩ expected| / |expected|. Returns 0.0 if expected is empty
        (by convention — there is nothing to recall).
    """
    if not expected:
        return 0.0
    returned_set = set(returned)
    expected_set = set(expected)
    hits = len(returned_set & expected_set)
    return hits / len(expected)


def aggregate_set_metrics(per_case: list[dict]) -> dict:
    """Aggregate per-case set-precision and set-recall into macro-averaged stats.

    Each per-case dict should contain:
        set_precision, set_recall

    Returns:
        Dict with keys: set_precision, set_recall, f1, n.
        f1 is the harmonic mean of the macro-averaged set_precision and
        set_recall. All values are zero if per_case is empty.
    """
    n = len(per_case)
    if n == 0:
        return {"set_precision": 0.0, "set_recall": 0.0, "f1": 0.0, "n": 0}

    sum_p = sum(r["set_precision"] for r in per_case)
    sum_r = sum(r["set_recall"] for r in per_case)
    avg_p = sum_p / n
    avg_r = sum_r / n
    if avg_p + avg_r == 0.0:
        f1 = 0.0
    else:
        f1 = 2.0 * avg_p * avg_r / (avg_p + avg_r)
    return {
        "set_precision": avg_p,
        "set_recall": avg_r,
        "f1": f1,
        "n": n,
    }


def aggregate_by_group(
    per_case_results: list[dict], group_key: str
) -> dict[str, dict]:
    """Aggregate per-case results grouped by an arbitrary key.

    Each per-case dict should contain the ``group_key``. Results are partitioned
    by the value of that key and each group is aggregated via ``aggregate()``.

    Args:
        per_case_results: List of per-case metric dicts. Each dict must contain
            the standard aggregate keys (recall_at_1, recall_at_5,
            recall_at_10, reciprocal_rank) plus the field named by
            ``group_key``.
        group_key: The key to group by (e.g. ``"query_type"``,
            ``"difficulty"``, ``"fairness"``).

    Returns:
        Dict mapping group value to aggregate stats dict (same shape as
        ``aggregate()`` return, plus ``'n'``).
    """
    groups: dict[str, list[dict]] = {}
    for r in per_case_results:
        gv = r[group_key]
        groups.setdefault(gv, []).append(r)

    return {gv: aggregate(group) for gv, group in groups.items()}


def aggregate_by_fairness(per_case_results: list[dict]) -> dict[str, dict]:
    """Aggregate per-case results grouped by fairness label.

    Thin wrapper around ``aggregate_by_group(..., 'fairness')``.

    Args:
        per_case_results: List of per-case metric dicts. Each must contain
            a ``'fairness'`` key.

    Returns:
        Dict mapping fairness label to aggregate stats dict.
    """
    return aggregate_by_group(per_case_results, "fairness")


def aggregate_by_difficulty(per_case_results: list[dict]) -> dict[str, dict]:
    """Aggregate per-case results grouped by difficulty label.

    Thin wrapper around ``aggregate_by_group(..., 'difficulty')``.

    Args:
        per_case_results: List of per-case metric dicts. Each must contain
            a ``'difficulty'`` key.

    Returns:
        Dict mapping difficulty label to aggregate stats dict.
    """
    return aggregate_by_group(per_case_results, "difficulty")
