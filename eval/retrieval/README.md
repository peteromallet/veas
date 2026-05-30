# Retrieval Evaluation Harness

## 1. Purpose

This is an **offline retrieval evaluation harness** for measuring and comparing
message-retrieval implementations over a **deterministic synthetic fixture
corpus**. It exists to answer the question: *"Is a given retriever better than
today's keyword (ILIKE) search?"* — before any embedding, vector index, or
production database is involved.

It produces standard IR metrics (recall@k, MRR) per golden test case and
aggregated by query type, with JSON and markdown reports.

## 2. Methodology

### Why a synthetic corpus?

The production corpus is small and privacy-sensitive; labeling it manually is
not feasible for this sprint. A synthetic fixture lets us design specific
adversarial conditions that stress-test retrieval in ways a real (but
privately-scrubbed) corpus might not surface.

### Hard-case categories

The corpus deliberately exercises six failure modes for naive string matching:

| Category | What it tests | Why ILIKE struggles |
|---|---|---|
| **Terse replies** | One-word / low-signal messages (`"fine."`, `"sure"`, `"I told you so."`) | ILIKE matches only if the query contains the exact word; semantic meaning is lost. |
| **Paraphrase pairs** | Target message wording diverges completely from natural query phrasing — e.g. a message about *"the idempotency key check failing under concurrent requests"* queried as *"transaction duplicate bug"* | ILIKE requires character-level substring overlap. Paraphrase cases in this harness share **zero** common substrings with their queries, so ILIKE returns `[]`. |
| **Cross-thread continuations** | The same `topic_id` appears across two different `thread_id` values. A topic-scoped query must return messages from **all** threads sharing that topic. | ILIKE can find the lexical hits, but the **scope filter** is the key differentiator — thread-scoped queries must exclude the other thread's hits, while topic-scoped queries must include both. |
| **Near-duplicate incidents** | Two messages describe the same event with wording differing by one detail (e.g. a payment processor bug). | Both contain overlapping keywords, so ILIKE finds both — but a semantic system should surface the more complete / authoritative one first. |
| **Media-analysis-only signal** | Messages whose `content` is generic (`"Check this out"`, `"See attached"`) with all semantically relevant information in `media_analysis.{explanation,description,summary}`. | ILIKE baseline **does** search these fields (see adapter docs), but only if the query substring appears there — it cannot *understand* the media description. |

### Why the ILIKE baseline loses on paraphrase and cross-thread cases

This is the **point** of the harness. The `IlikeBaselineRetriever` is a
pure-Python reimplementation of case-insensitive substring matching across
`content`, `media_analysis.explanation`, `media_analysis.description`, and
`media_analysis.summary`. It has zero semantic understanding:

- **Paraphrase cases**: Every golden case tagged `query_type: paraphrase` has
  been verified to share **no character-level substring** between the query and
  the target message content (and no `media_analysis` bridges the gap). The
  baseline will return an empty list — recall = 0 — for all of them. This is the
  **baseline floor** that any semantic retriever must beat.

- **Cross-thread cases**: The baseline applies scope filtering correctly
  (`thread_id` / `topic_id`), so it finds the right messages via substring
  matching. But a retriever that cannot resolve `topic_id` associations across
  threads will fail on the **scope correctness** dimension even if it has good
  lexical recall. The contrast is deliberate: cross-thread cases isolate the
  **scoping** failure mode from the **lexical** failure mode.

The contrast between high verbatim recall (GC07–GC14) and zero paraphrase
recall (GC15–GC24) is the harness's reason to exist.

## 3. Scope-model divergence: harness vs. production `search_messages`

**This section is required reading** before comparing harness results to
production behavior.

### Harness baseline (`IlikeBaselineRetriever`)

The harness `IlikeBaselineRetriever` implements a simplified scope model using
only the concepts present in the synthetic corpus:

| Scope  | Filter applied                  | Used for                                           |
|--------|---------------------------------|-----------------------------------------------------|
| thread | `message.thread_id == thread_id` | Isolate messages within a single conversation thread |
| topic  | `message.topic_id == topic_id`   | Cross-thread retrieval within a shared topic          |
| all    | No filter                        | Entire corpus                                        |

It orders results by `(sent_at DESC, id DESC)` for deterministic ranking per
[SD3](/Users/peteromalley/Documents/Veas/megaplans/xen-eval-harness-brief.md).

### Production `search_messages` (`app/services/tools/read_tools.py`)

The production `search_messages` function applies a **much richer** scope model
that the harness deliberately does not exercise. It includes:

- **`bot_id` scoping**: Messages are always scoped to the current bot.
- **`topic_id` scoping**: Always present; ties messages to a topic.
- **Participant scoping**: Filters by `sender_id` / `recipient_id` based on the
  current user and partner.
- **Partner sharing visibility**: Respects `partner_share` flags for cross-user
  visibility.
- **`_message_in_current_scope` filtering**: Additional session-level scope
  rules.
- **Local-day / date-range filtering**: Temporal window constraints.

### Why this divergence exists (deliberate, per SD2)

The harness is a **pure-Python re-implementation** of ILIKE semantics only,
designed to run completely offline with zero database, zero API keys, and zero
imports from `app.*`. The synthetic corpus uses `thread_id`/`topic_id` as its
scoping primitive because that matches the fixture design — not because those
columns exist in production tables (the messages table has no `thread_id`
column).

**Consequence**: Harness recall numbers reflect the lexical substring-matching
ability of the baseline within a simplified scoping model. They do **not**
reflect production recall because production applies additional filters
(bot/participant/partner/date). A retriever that scores well in the harness
still needs end-to-end evaluation against real production queries with full
scoping to confirm production behavior.

This is accepted scope-model divergence per SD2; a future follow-up slice may
add a DB-backed adapter that exercises the full production scope model.

## 4. How to add a golden case

### Required fields

Add a new entry under `cases:` in `golden_set.yaml`:

```yaml
- id: GC29
  query: "your search query here"
  expected_message_ids:
    - m001
    - m015
  scope: topic            # "thread" | "topic" | "all"
  query_type: verbatim_quote  # "topic_recall" | "verbatim_quote" | "paraphrase" | "cross_thread"
  thread_id: thread_nexus_kickoff   # required only when scope == "thread"
  topic_id: topic_project_nexus     # required only when scope == "topic"
  notes: "Optional notes about this case."
```

### Validation rules (enforced by loader)

The loader (`eval/retrieval/loader.py`) enforces these at load time:

1. **`expected_message_ids` must be non-empty** (SD6 / correctness-4). A case
   with an empty expected list will raise `ValueError`.
2. **Every message id in `expected_message_ids` must exist in the corpus**.
   A dangling reference raises `ValueError`.
3. **Scope/id consistency**:
   - `scope == 'thread'` requires a **non-None `thread_id`**.
   - `scope == 'topic'` requires a **non-None `topic_id`**.
   - `scope == 'all'` must have `thread_id` and `topic_id` omitted or `None`.
   Violations raise `ValueError`.

### Paraphrase case design notes

To create a valid paraphrase case (where ILIKE is expected to miss):

1. Write a `query` whose words share **no** character-level substrings with the
   target message's `content` or any `media_analysis` field.
2. Verify: `python -c "q='your query'; m='target message content'; print(q.lower() in m.lower())"`
   should print `False` for the message content and every media_analysis
   field value.
3. Set `query_type: paraphrase`.
4. Add a note explaining why ILIKE will miss (which "danger" substrings were
   avoided, e.g. "Note: avoided 'sun' because it matches inside 'sunscreen'").

## 5. How to run

### Prerequisites

- Python 3.11+
- `pip install pydantic pyyaml`
- No database, no API keys, no network required.

### Run the baseline adapter

```bash
python -m eval.retrieval.runner --adapter baseline
```

### Run the stub adapter (proves the pluggable seam)

```bash
python -m eval.retrieval.runner --adapter stub
```

### Custom paths

```bash
python -m eval.retrieval.runner --adapter baseline \
    --corpus eval/retrieval/corpus.yaml \
    --golden eval/retrieval/golden_set.yaml \
    --out-dir eval/retrieval/reports/
```

### Default paths (used when flags are omitted)

| Flag        | Default                               |
|-------------|---------------------------------------|
| `--corpus`  | `eval/retrieval/corpus.yaml`          |
| `--golden`  | `eval/retrieval/golden_set.yaml`      |
| `--out-dir` | `eval/retrieval/reports/`             |

### Output

Reports are written to the output directory (created automatically if missing):

```
eval/retrieval/reports/
├── baseline_report.json   # Full structured report (per-case + aggregates)
├── baseline_report.md     # Human-readable markdown (tables, per-query-type breakdown)
├── stub_report.json
└── stub_report.md
```

### Running tests

```bash
# Unit tests for each component
pytest tests/test_retrieval_eval_metrics.py -v
pytest tests/test_retrieval_eval_adapters.py -v
pytest tests/test_retrieval_eval_runner.py -v

# All retrieval eval tests
pytest tests/test_retrieval_eval_metrics.py \
        tests/test_retrieval_eval_adapters.py \
        tests/test_retrieval_eval_runner.py -v
```

## 6. How to implement a new adapter

### The `Retriever` Protocol

Every adapter must satisfy the `Retriever` protocol defined in
`eval/retrieval/adapters.py`:

```python
from typing import Protocol
from eval.retrieval.schema import Corpus, Scope


class Retriever(Protocol):
    """Protocol for retrieval adapters."""

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None,
        topic_id: str | None,
        limit: int,
    ) -> list[str]:
        """Retrieve ranked message ids for a query.

        Args:
            query: The search query string.
            scope: Filter scope ('thread', 'topic', or 'all').
            thread_id: Required for scope=='thread', ignored otherwise.
            topic_id: Required for scope=='topic', ignored otherwise.
            limit: Maximum number of results to return.

        Returns:
            Ordered list of message ids (rank 1 = index 0), truncated to limit.
        """
        ...
```

### Skeleton adapter

```python
from eval.retrieval.schema import Corpus, Scope


class MySemanticRetriever:
    """A semantic retriever using <your method here>."""

    def __init__(self, corpus: Corpus) -> None:
        self._corpus = corpus
        # Build your index here (embeddings, BM25, etc.)

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
    ) -> list[str]:
        # 1. Apply scope filter (same pattern as IlikeBaselineRetriever)
        candidates = self._corpus.messages
        if scope == "thread":
            candidates = [m for m in candidates if m.thread_id == thread_id]
        elif scope == "topic":
            candidates = [m for m in candidates if m.topic_id == topic_id]
        # scope == 'all': no filter

        # 2. Score candidates against the query
        scored = []
        for msg in candidates:
            score = self._score(query, msg)  # Your similarity function
            if score > 0:
                scored.append((score, msg))

        # 3. Sort by score descending, then by deterministic tiebreaker
        scored.sort(key=lambda x: (x[0], x[1].sent_at, x[1].id), reverse=True)

        # 4. Slice to limit and return ids
        return [msg.id for _, msg in scored[:limit]]

    def _score(self, query: str, message) -> float:
        """Return a relevance score for query vs. message."""
        # TODO: implement your scoring (cosine similarity, BM25, etc.)
        return 0.0
```

### Registering the adapter in the CLI

In `eval/retrieval/runner.py`, add your adapter to the `main()` function's
adapter dispatch:

```python
from eval.retrieval.adapters import MySemanticRetriever

# In main():
if adapter_name == "my_semantic":
    retriever = MySemanticRetriever(corpus)
```

Or, if your adapter requires external dependencies (e.g. a model download), add
it as a lazy import so the baseline remains dependency-free.

## 7. Out-of-scope (deferred)

The following are **explicitly deferred** to a follow-up slice:

- **Real-corpus labeling and golden set**: The synthetic fixture is a stand-in.
  Producing a manually-labeled golden set from the production corpus requires
  privacy review and is not part of this sprint.

- **DB-backed adapter**: An adapter that connects to a real database (e.g. via
  `DATABASE_URL`), applies the full `search_messages` scope model (bot_id,
  participant visibility, partner sharing, date filters), and runs the harness
  against real production data. This is the natural next step once the harness
  is proven.

- **Production scope-model fidelity**: The harness baseline's scope model
  (`thread_id`, `topic_id`, `all`) diverges from production `search_messages`
  (which uses `bot_id` + `topic_id` + participant scoping with date and partner
  filters). See §3 above for the full accounting.

- **Embeddings, vector indexes, pgvector**: The harness provides the evaluation
  seam; selecting embedding granularity, building indexes, and implementing a
  semantic retriever is a separate project gated on this baseline.

- **No production code changes**: The harness must not import from `app.*`,
  modify production tables, or require a live database. It lives entirely under
  `eval/retrieval/`.
