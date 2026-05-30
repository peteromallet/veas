# Xen Retrieval Eval Harness — Brief

> Prerequisite #1 for the Xen agent-search project (see `xen-retrieval-brief.md`
> §v3). Round-2 critique verdict: **you cannot tune, choose embedding granularity,
> or prove semantic search beats today's `ILIKE` without an evaluation harness** —
> and the corpus (terse, context-dependent dyadic messages) is hostile to naive
> embeddings, so measuring is non-optional. This sprint builds that harness FIRST,
> before any retrieval/embedding code.

## Outcome

A self-contained **retrieval evaluation harness + seed golden set** that scores any
message-retrieval implementation on standard IR metrics over labeled
query→expected-message cases — giving a baseline number for today's keyword search
and a drop-in seam for a future semantic retriever to be measured against.

## Scope

IN:
- A **golden-set format** (YAML): cases of `{id, query, expected_message_ids[],
  scope (thread|topic|all), query_type (topic_recall|verbatim_quote|paraphrase|
  cross_thread), notes}`.
- A **seed golden set** (≥25 cases) over a **synthetic-but-representative fixture
  corpus** of messages — deliberately including the hard cases that break naive
  retrieval: terse messages ("fine.", "I told you so"), paraphrase (query wording ≠
  message wording), cross-thread references, near-duplicate incidents.
- A **pluggable eval runner**: takes a retriever function
  `(query, scope) -> ranked message_ids`, runs every golden case, computes
  **recall@k (k=1,5,10), MRR**, and a **per-query-type breakdown**; emits a
  markdown + JSON report.
- A **baseline adapter** wrapping the current `search_messages` ILIKE behaviour
  (`app/services/tools/read_tools.py`) so we get a real baseline number now.
- A **stub semantic retriever** adapter (returns nothing / random) to prove the
  pluggable seam — NOT a real implementation.
- Harness unit tests + a short README (methodology, how to add cases, how to run).

OUT (anti-scope):
- **No embeddings, no pgvector, no real semantic retriever** — that's the next
  slice, gated on this harness's baseline.
- No changes to any production retrieval/tool/code path; no DB schema changes.
- **No labeling of the real production corpus** (privacy; tiny dataset). Synthetic
  fixture now; real-data labeling is an explicit follow-up.
- No agent/tool wiring; this is offline eval tooling only.

## Locked decisions

- Metrics: **recall@{1,5,10} + MRR**, broken down by `query_type`.
- Corpus: **synthetic fixture** (representative, hard cases included), not prod data.
- **Pluggable retriever interface**; baseline = current ILIKE search wrapped as an
  adapter.
- Lives under a new `eval/retrieval/` dir; runs fully **offline** (no prod DB, no
  API keys) for the baseline.

## Open questions (planner resolves; do not invent silently)

1. Exact golden-set schema + directory layout.
2. **The methodology crux:** how to make the synthetic corpus genuinely
   representative of terse, context-dependent dyadic messaging so the baseline and
   future comparisons are meaningful — not a strawman that any retriever aces.
3. Whether to also expose a tiny adapter that can (optionally, off by default) run
   against a real DB via `DATABASE_URL` for later real-data eval — interface only,
   not wired now.

## Constraints

- Deterministic, fast, offline; no network/API/DB needed to run the baseline + tests.
- Pure addition — must not touch or import-break production code paths; the baseline
  adapter may *call* `search_messages`-equivalent logic but must not require a live DB
  (use the fixture corpus + an in-memory/SQLite or pure-python ILIKE shim).
- Clear seam so the future semantic retriever and embedding-granularity experiments
  drop in without changing the runner or golden set.

## Done criteria

- A single command runs the seed golden set against the baseline adapter and prints
  recall@{1,5,10} + MRR + per-query-type breakdown, and writes a JSON+markdown report.
- The stub semantic adapter runs through the same path (proving the seam).
- Seed golden set has ≥25 cases including paraphrase, verbatim-quote, and
  cross-thread hard cases.
- Harness unit tests pass; README documents methodology + add-a-case + run steps.

## Touchpoints

- New: `eval/retrieval/` — `golden_set.yaml`, `corpus.yaml` (fixture), `runner.py`,
  `metrics.py`, `adapters.py`, `README.md`
- New: `tests/test_retrieval_eval.py`
- Reference only (do not modify): `app/services/tools/read_tools.py` (`search_messages`
  ILIKE behaviour, for the baseline adapter's semantics)

## Why this first

It's the cheapest thing that de-risks the whole Xen v1: it sets the baseline,
makes the embedding-granularity decision empirical instead of a guess, and gives a
go/no-go recall threshold before a single embedding is computed.
