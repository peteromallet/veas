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
    groups: dict[str, list[dict]] = {}
    for r in per_case_results:
        qt = r["query_type"]
        groups.setdefault(qt, []).append(r)

    return {qt: aggregate(group) for qt, group in groups.items()}
