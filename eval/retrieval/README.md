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
| **Paraphrase pairs** | Target wording restates the query's intent with synonyms / different phrasing — e.g. a message *"the OAuth2 login integration is mostly done"* queried as `login integration` or `caching layer` for *"I've been thinking about the caching layer"*. | ILIKE matches the whole `text_contains` argument as one substring. A restated query rarely matches verbatim, so ILIKE either misses or ranks by recency rather than meaning. |
| **Cross-thread continuations** | The same `topic_id` appears across two different `thread_id` values. A topic-scoped query must return messages from **all** threads sharing that topic. | ILIKE can find a lexical hit in *one* thread, but the topic answer spans **both** threads; the second thread usually restates the topic in different words, so substring matching leaves recall low. |
| **Near-duplicate incidents** | Two messages describe the same event with wording differing by one detail, plus **decoy** incidents that share words but are the wrong answer (e.g. Nexus payment-transaction duplication vs. a notification-email duplication vs. Orion billing double-charge). | ILIKE finds all lexical matches indiscriminately; a semantic system should rank the right incident and reject the same-word decoy. |
| **Same-word-different-meaning distractors** | Words reused with a different sense: API `rate` limiting vs. customer-satisfaction `rate`; software `crash` vs. a roller-coaster scare; dinner `reservation` vs. having `reservations` (doubts); a chess `move` vs. an apartment `move`. | ILIKE has no way to tell the senses apart — these are pure lexical traps that hurt its precision. |
| **Media-analysis-only signal** | Messages whose `content` is generic (`"Check this out"`, `"See attached"`) with all relevant information in `media_analysis.{explanation,description,summary}`. | ILIKE baseline **does** search these fields, but only if the query substring appears there — it cannot *understand* the media description. |

### Fairness: the baseline gets a real lexical shot (this rebuild)

> **History.** The first version of this harness deliberately built every
> paraphrase / cross-thread query to share **zero** substrings with its target,
> pinning the ILIKE baseline at exactly 0% on those types. That floored the
> baseline artificially and inflated the semantic lift (recall@10 0.26 → 0.87,
> ~3x+). **This rebuild fixes that bias** so the comparison is trustworthy.

The `IlikeBaselineRetriever` is a pure-Python reimplementation of production
`search_messages` ILIKE semantics: it matches the **whole query string** as a
case-insensitive substring (`content ILIKE '%query%'`) across `content` and the
three `media_analysis` fields. To give it a *fair* shot:

- **Paraphrase / cross-thread / topic-recall queries are short, keyword-style
  search phrases** — the way a user or agent actually drives `search_messages` —
  and most of them contain a contiguous substring that genuinely appears in at
  least one expected target. The baseline therefore scores **nonzero** on these
  types. The semantic win has to come from *meaning*: pulling restated/synonym
  targets the baseline misses, spanning the second thread of a topic, and
  out-ranking same-word decoys — not from an artificial zero-overlap floor.

- **A labeled minority of paraphrase cases are genuinely zero-overlap** (synonym
  only, marked `[HARD zero-overlap]` in the golden-set notes, e.g. `UV protection`
  → *"sunscreen"*, `NPE fix` → *"null pointer ... fixed it"*). These preserve a
  pure-semantic ceiling measurement but do **not** dominate.

The generator `_generate_fixtures.py` prints a fairness audit showing, per query
type, how many cases the whole-query substring can match — confirming the
baseline is no longer 0% on paraphrase/cross-thread. See
`reports/comparison_report.md` for the fair three-way numbers (baseline vs
semantic vs hybrid) that **supersede** the first run's inflated figures.

### Fairness and difficulty tags

Every golden case carries two optional tags that enable stratified analysis:

| Tag | Values | Derived from | Meaning |
|-----|--------|-------------|---------|
| **`difficulty`** | `easy`, `medium`, `hard` | Query type + `hard_zero` flag | `easy` = verbatim quotes (exact substring match expected). `medium` = paraphrase / cross-thread / topic-recall with lexical overlap (baseline can score nonzero). `hard` = genuinely zero-overlap synonym-only cases (`hard_zero=True`) or cross-thread cases where no expected target contains the query substring. |
| **`fairness`** | `keyword_favored`, `semantic_favored`, `either`, `adversarial` | Query type + `overlap_hint` presence | `keyword_favored` = verbatim quotes (ILIKE always wins). `semantic_favored` = zero-overlap cross-thread cases (baseline expected at 0%). `either` = paraphrase/cross-thread/topic-recall with overlapping substrings (both systems can score). `adversarial` = `hard_zero=True` synonym-only cases (baseline cannot match by design). |

Both fields default to `None` (omitted) for backward compatibility, and the
runner maps missing values to `"unlabeled"` in reports. The comparison report
(`_make_comparison.py`) emits per-tag recall@10 and MRR breakdown tables so
you can see exactly where the semantic lift is concentrated — it should be
largest on `semantic_favored` and `adversarial` cases and smallest (or zero)
on `keyword_favored`.

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
  difficulty: medium          # "easy" | "medium" | "hard" | null
  fairness: either            # "keyword_favored" | "semantic_favored" | "either" | "adversarial" | null
  thread_id: thread_nexus_kickoff   # required only when scope == "thread"
  topic_id: topic_project_nexus     # required only when scope == "topic"
  extra_scope: {}                   # optional dict[str, Any]; production-scope fields for DB adapter
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

### Editing the fixtures

The corpus and golden set are **generated** by `_generate_fixtures.py`; do not
hand-edit `corpus.yaml` / `golden_set.yaml` (they carry a \"DO NOT hand-edit\"
header). Edit the generator's `THREADS` / `CASES` data and re-run it.

Each entry in the `CASES` list is a dict with these keys:

```python
dict(
    id="GC01",                 # Unique case id
    query="I told you so",     # Search query string
    expected=["m013"],         # List of expected message ids
    scope="all",               # "thread" | "topic" | "all"
    qt="verbatim_quote",       # query_type: verbatim_quote | paraphrase | cross_thread | topic_recall
    difficulty="easy",         # "easy" | "medium" | "hard"
    fairness="keyword_favored", # "keyword_favored" | "semantic_favored" | "either" | "adversarial"
    note="Exact terse reply.", # Human-readable notes
    # Optional fields:
    thread="thread_id",        # Required when scope=="thread"
    topic="topic_id",          # Required when scope=="topic"
    hard_zero=True,            # Genuinely zero-overlap (→ difficulty="hard", fairness="adversarial")
    overlap_hint="substring",  # Documents shared substring (informational)
)
```

The `difficulty` and `fairness` tags drive the stratified breakdown tables in
the comparison report. Map them per the table in §2:

| Query type | `hard_zero`? | `overlap_hint`? | `difficulty` | `fairness` |
|---|---|---|---|---|
| `verbatim_quote` | — | — | `easy` | `keyword_favored` |
| `paraphrase` | `True` | — | `hard` | `adversarial` |
| `paraphrase` | — | yes | `medium` | `either` |
| `cross_thread` | — | yes (strong) | `medium` | `either` |
| `cross_thread` | — | weak/none | `hard` | `semantic_favored` |
| `topic_recall` | — | yes | `medium` | `either` |

#### Paraphrase case design notes (fair)

A *fair* paraphrase case restates the query's intent and shares **some** real
lexical overlap with at least one expected target, so the keyword baseline can
score nonzero:

1. Write a short, keyword-style `query` (what a user would type into search),
   containing a contiguous phrase that appears in an expected target.
2. Make the *right* answer require meaning — e.g. several messages share the
   phrase but only one matches the intent, or the second half of the expected
   set restates the topic with synonyms the baseline can't reach.
3. Set `qt="paraphrase"`, `difficulty="medium"`, `fairness="either"`.
4. Document the shared substring with `overlap_hint="..."` (the generator
   emits `(overlap: ...)` in notes).
5. For a genuinely hard synonym-only case, set `hard_zero=True`,
   `difficulty="hard"`, `fairness="adversarial"`; it is labeled
   `[HARD zero-overlap]` and is expected to be a baseline miss. Keep these a
   minority.

Run `python -m eval.retrieval._generate_fixtures` — it validates all
`expected_message_ids` exist and prints the per-type fairness audit.

### Source-aware golden cases (M4, GC71–GC80)

The golden set includes 10 **source-aware** cases (GC71–GC80) that exercise
the `expected_source_keys` contract added in the M4 milestone. These cases
reference non-message corpus entries — memories, observations, distillations,
artifacts, conversation notes, and themes — alongside traditional message ids.

Key design properties:

| Property | Detail |
|---|---|
| **Intent labels** | `know_about` (semantic knowledge recall) or `exact_said` (exact wording lookup). The runner reports intent-stratified metrics. |
| **Non-message source types** | All six non-message types appear across the 10 cases: memory, observation, distillation, artifact, conversation_note, theme. |
| **Deliberate low-weight theme cases** | GC75, GC76, and GC80 expect **only** a theme source key with **zero message fallback**. The ILIKE baseline *must* score 0.0 on these by design — they are a pure-semantic measurement. |
| **Message fallback** | Cases that do include `expected_message_ids` use message ids the baseline CAN find (lexical overlap exists). The semantic win must come from the non-message sources the baseline cannot reach. |
| **`query_type`** | `knowledge_recall` for `know_about`; `exact_source_quote` for `exact_said`. Both types are recognized by the runner and appear in `by_query_type` aggregate tables. |
| **`fairness`** | `adversarial` for zero-overlap theme/note-only cases; `either` for mixed source-type cases with lexical overlap; `semantic_favored` for cases where the baseline can only reach a minority of expected sources. |
| **Backward compatibility** | The loader's `_normalize_expected_keys` validator merges `expected_source_keys` and `expected_message_ids` into a single canonical list. Legacy message-only cases continue to work unchanged. |

To add a new source-aware case, include both `expected_source_keys` (for
non-message targets) and `expected_message_ids` (for the message-only subset):

```yaml
- id: GC99
  query: "your query"
  expected_message_ids:
  - m001
  expected_source_keys:
  - source_type: memory
    source_id: mem001
  - source_type: theme
    source_id: thm001
  scope: all
  query_type: knowledge_recall
  intent: know_about
  difficulty: medium
  fairness: either
  notes: "Optional notes."
```

Source keys that are not messages are accepted by the loader without requiring
corpus-row validation (the synthetic corpus has non-message entries but the
current loader only cross-checks `source_type='message'` ids).

## 5. How to run

### Prerequisites

- Python 3.11+
- Baseline / stub: `pip install pydantic pyyaml` — no database, no API keys, no
  network.
- Semantic / hybrid: additionally `pip install sentence-transformers numpy` and
  the `all-MiniLM-L6-v2` model present in the local Hugging Face cache (the
  embedder forces `HF_HUB_OFFLINE=1`, so it never hits the network). Corpus
  embeddings are cached to `reports/.emb_cache/` keyed by a content hash, so
  re-runs are deterministic and fast.

### Run an adapter

```bash
python -m eval.retrieval.runner --adapter baseline   # today's keyword/ILIKE
python -m eval.retrieval.runner --adapter semantic   # dense MiniLM cosine
python -m eval.retrieval.runner --adapter hybrid     # RRF(baseline, semantic)
python -m eval.retrieval.runner --adapter stub       # empty (proves the seam)
python -m eval.retrieval.runner --adapter db         # pgvector Postgres (requires DIRECT_DATABASE_URL)
```

### Run the navigation evaluation

```bash
python -m eval.retrieval.nav_eval --adapter reference       # pure-Python reference
python -m eval.retrieval.nav_eval --adapter reference --assert-nav-gate  # gate mode
python -m eval.retrieval.nav_eval --adapter db              # pgvector Postgres

# Custom paths:
python -m eval.retrieval.nav_eval --adapter reference \
    --nav-golden eval/retrieval/nav_golden.yaml \
    --corpus eval/retrieval/corpus.yaml
```

### Run the hot-context inclusion evaluation

```bash
python -m eval.retrieval.hotcontext_eval --selector reference       # pure-Python recency baseline
python -m eval.retrieval.hotcontext_eval --selector reference --assert-m3-gate  # gate mode
python -m eval.retrieval.hotcontext_eval --selector db              # pgvector Postgres

# Custom paths:
python -m eval.retrieval.hotcontext_eval --selector reference \
    --fixtures eval/retrieval/hotcontext_fixtures.yaml \
    --corpus eval/retrieval/corpus.yaml
```

### Build the comparison report with M1 gate

```bash
python -m eval.retrieval._make_comparison                  # standard report
python -m eval.retrieval._make_comparison --assert-m1-gate  # gate mode (exits non-zero on failure)
```

### Regenerate fixtures + the fair comparison report

```bash
# Rebuild corpus.yaml + golden_set.yaml deterministically (prints fairness audit)
python -m eval.retrieval._generate_fixtures
# Run all three adapters, then build reports/comparison_report.md
python -m eval.retrieval.runner --adapter baseline
python -m eval.retrieval.runner --adapter semantic
python -m eval.retrieval.runner --adapter hybrid
python -m eval.retrieval._make_comparison
```

### Run the semantic and hybrid adapters

```bash
python -m eval.retrieval.runner --adapter semantic   # cosine over embeddings
python -m eval.retrieval.runner --adapter hybrid      # RRF(keyword, semantic)
```

The `semantic` adapter (`SemanticRetriever`) embeds every corpus message and
the query and ranks scope-filtered candidates by cosine similarity. The
`hybrid` adapter (`HybridRetriever`) fuses the keyword (ILIKE) and semantic
rankings with Reciprocal Rank Fusion (k=60).

**Embedding backend selection** (`eval/retrieval/embeddings.py`,
`get_default_embedder`), in priority order:

1. OpenAI `text-embedding-3-small` — used only if `OPENAI_API_KEY` is already
   in the environment (the key is never read, logged, or hardcoded by this code;
   the openai SDK reads it). 
2. Local sentence-transformers `all-MiniLM-L6-v2` — used if importable; runs
   fully offline.
3. TF-IDF char-ngram **floor** — NOT a real embedding; a deterministic sanity
   floor used only when neither real backend is available. Reports must label it
   as such.

Corpus embeddings are cached to disk under `eval/retrieval/.embedding_cache/`
(gitignored) so reruns are cheap and need no network. A `--comparison`-style
side-by-side of all three retrievers lives in
`reports/comparison_report.md`.

Adapter tests use a tiny deterministic fake embedder so they need no network:

```bash
pytest tests/test_retrieval_eval_semantic.py -v
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
pytest tests/test_retrieval_eval_semantic.py -v
pytest tests/test_nav_eval.py -v
pytest tests/test_hotcontext_eval.py -v

# All retrieval eval tests
pytest tests/test_retrieval_eval_metrics.py \
        tests/test_retrieval_eval_adapters.py \
        tests/test_retrieval_eval_runner.py \
        tests/test_retrieval_eval_semantic.py \
        tests/test_nav_eval.py \
        tests/test_hotcontext_eval.py -v
```

## 6. How to implement a new adapter

### The `Retriever` Protocol

Every adapter must satisfy the `Retriever` protocol defined in
`eval/retrieval/adapters.py`:

```python
from typing import Any, Protocol
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
        **extra_scope: Any,
    ) -> list[str]:
        """Retrieve ranked message ids for a query.

        Args:
            query: The search query string.
            scope: Filter scope ('thread', 'topic', or 'all').
            thread_id: Required for scope=='thread', ignored otherwise.
            topic_id: Required for scope=='topic', ignored otherwise.
            limit: Maximum number of results to return.
            **extra_scope: Additional scope filters (bot_id, participant,
                partner_share, date, etc.) used by DbBackedRetriever.
                Ignored by offline adapters.

        Returns:
            Ordered list of message ids (rank 1 = index 0), truncated to limit.
        """
        ...
```

### Skeleton adapter

```python
from typing import Any
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
        **extra_scope: Any,
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

- **Production scope-model fidelity**: The harness baseline's scope model
  (`thread_id`, `topic_id`, `all`) diverges from production `search_messages`
  (which uses `bot_id` + `topic_id` + participant scoping with date and partner
  filters). See §3 above for the full accounting. The DB-backed adapter (§10)
  partially closes this gap by exercising the production messages table, but
  the synthetic fixture still uses simplified IDs.

- **Embeddings, vector indexes, pgvector**: The harness provides the evaluation
  seam; selecting embedding granularity, building indexes, and implementing a
  semantic retriever is a separate project gated on this baseline.

- **No production code changes**: The harness must not import from `app.*`,
  modify production tables, or require a live database. It lives entirely under
  `eval/retrieval/`.

## 8. Navigation evaluation

### Purpose

The nav-eval module (`nav_eval.py`) measures whether a navigation adapter
correctly implements the seven nav operations used by the chat UI. It is
independent of the retrieval harness — it answers: *"When the UI asks for
messages-before or scrolls around an anchor, do we return the right set?"*

### Schema

```yaml
# eval/retrieval/nav_golden.yaml
cases:
  - id: NAV01
    op: open_thread              # one of seven NavOp values
    scope: thread
    thread_id: thread_nexus_kickoff
    topic_id: topic_project_nexus
    expected_ids_in_order:
      - m001
      - nav_thread_nexus_kickoff_start
      - m002
      # ...
    anchor: null                 # optional, used by anchor-based ops
    n: null                      # optional, n-message window size
    notes: "All messages in Nexus kickoff thread, chronological."
```

### Seven nav operations

| Op | `anchor` required? | `n` required? | Returns |
|---|---|---|---|
| `open_thread` | no | ignored | All messages in `thread_id`, chronologically ascending. |
| `messages_before` | yes | yes | `n` messages chronologically before `anchor` (exclusive), ascending. |
| `messages_after` | yes | yes | `n` messages chronologically after `anchor` (exclusive), ascending. |
| `scroll` | yes | yes | `n` messages centered on `anchor` (±n/2, anchor inclusive), chronologically ascending. |
| `topic_recent` | no | defaults to 20 | `n` most-recent messages in `topic_id`, chronologically ascending. |
| `recent_before_current` | yes | defaults to 20 | `n` most-recent messages chronologically before `anchor` (exclusive), ascending. |
| `before_message_id` | yes | yes | `n` messages chronologically before `anchor_id` (exclusive), ascending. |

The golden set ships with 14 cases covering all seven ops (≥1 per op, 2+ for
most-used). `recent_before_current` cases reference explicit nav anchor message
ids (e.g. `nav_thread_nexus_kickoff_end`) that were injected into the corpus
by `_generate_fixtures.py`.

### The NavReference Protocol

Adapters must satisfy:

```python
class NavReference(Protocol):
    def messages_before(self, anchor: str, n: int) -> list[str]: ...
    def messages_after(self, anchor: str, n: int) -> list[str]: ...
    def open_thread(self, anchor: str | None, thread_id: str) -> list[str]: ...
    def scroll(self, anchor: str, n: int) -> list[str]: ...
    def topic_recent(self, topic_id: str, n: int) -> list[str]: ...
    def before_message_id(self, anchor_id: str, n: int) -> list[str]: ...
    def recent_before_current(self, anchor: str, n: int) -> list[str]: ...
```

### Metrics

Nav evaluation uses two metrics defined in `eval/retrieval/metrics.py`:

- **`exact_ordered_match(returned, expected) -> bool`** — strict list equality
  (same ids in the same order).
- **`contiguous_boundary_ok(returned, expected, corpus_order) -> bool`** —
  `returned` is a contiguous subsequence of the corpus's chronological ordering,
  AND `returned[0] == expected[0]` AND `returned[-1] == expected[-1]` (interior
  NOT checked). Useful when exact order is hard but the window boundaries are
  critical.

The CLI prints a per-case table and aggregate pass rates for both metrics.

### CLI

```bash
python -m eval.retrieval.nav_eval --adapter reference
python -m eval.retrieval.nav_eval --adapter reference --assert-nav-gate
python -m eval.retrieval.nav_eval --adapter db
```

With `--assert-nav-gate`, exits non-zero unless aggregate exact pass rate is
exactly 1.0. Without the flag, exits 0 (report-only mode).

### Example output

```
case_id    op                        exact    boundary   returned  expected
NAV01      open_thread               PASS     PASS       m001,nav_...  m001,nav_...
NAV02      open_thread               PASS     PASS       m118,m119...  m118,m119...
...
Pass rate (exact):    64.29% (9/14)
Pass rate (boundary): 92.86% (13/14)
```

## 9. Hot-context inclusion evaluation

### Purpose

The hotcontext-eval module (`hotcontext_eval.py`) measures whether a
hot-context selector correctly identifies prior messages that should be
included when the conversation window changes. It answers: *"When the UI
advances to a new window, do we pull the right prior context?"*

### Schema

```yaml
# eval/retrieval/hotcontext_fixtures.yaml
fixtures:
  - id: HC01
    topic_id: topic_project_nexus
    last_window_message_ids:      # messages currently in the UI window
      - m006
      - m007
      - m008
      - m009
      - m010
    gold_prior_on_topic_ids:      # messages that SHOULD be included as prior context
      - m001
      - m002
      - m003
      - m004
      - m005
    budget: 5                     # max messages the selector can return
    rationale: "Window covers Nexus kickoff msgs after midpoint..."
    category: gap_continue
```

### Four fixture categories

| Category | Invariant | Semantic |
|---|---|---|
| `gap_continue` | Gold NOT in `last_window_message_ids`; gold non-empty. | Same topic, current window skips earlier messages — selector must close the gap. |
| `topic_switch` | `last_window[-1]` topic ≠ fixture `topic_id`. | User switched to a new topic — selector must find relevant priors in the new topic. |
| `no_relevant_prior` | Gold list empty. | The topic has no relevant prior messages — selector should return empty. |
| `near_duplicate_prior` | All gold share topic; at least one distractor in same topic shares ≥2 content words with gold but is NOT in gold. | Tests whether the selector can distinguish the right prior from a near-duplicate. |

The fixtures file ships with 12 cases across all four categories.

### The HotContextSelector Protocol

```python
class HotContextSelector(Protocol):
    def select(self, state: HotContextState, corpus: Corpus) -> set[str]: ...
```

Selectors must return at most `state.budget` ids and exclude any already in
`state.last_window_message_ids`.

### Reference baseline

The `PythonReferenceSelector` is an **honest recency-only baseline** (NOT
hand-tuned to gold). For `gap_continue` / `topic_switch` / `near_duplicate_prior`,
it returns the up-to-`budget` most-recent messages in the fixture's topic that
are not already in the window. For `no_relevant_prior`, it returns an empty set.

Because it uses only recency (no semantic disambiguation), it is expected to
miss near-duplicate cases and topic-switch cases where recency alone doesn't
capture relevance. This gives a realistic lower bound for a future semantic
selector to beat.

### Metrics

Uses the set-metrics functions from `eval/retrieval/metrics.py`:

- **`set_precision(returned, expected)`** — |∩| / |returned| (1.0 if returned empty).
- **`set_recall(returned, expected)`** — |∩| / |expected| (0.0 if expected empty).
- **F1** — harmonic mean of macro-averaged precision and recall.

The report includes per-fixture precision/recall/F1, global aggregate, and
per-category breakdowns.

### CLI

```bash
python -m eval.retrieval.hotcontext_eval --selector reference
python -m eval.retrieval.hotcontext_eval --selector reference --assert-m3-gate
python -m eval.retrieval.hotcontext_eval --selector db
```

With `--assert-m3-gate`, exits non-zero unless aggregate `set_recall >= 0.8`
AND `set_precision >= 0.6`. Without the flag, exits 0 (report-only mode).

### Example output

```
fixture_id   category               precision  recall     f1         budget   ret    gold
HC01         gap_continue           1.0000     1.0000     1.0000     5        5      5
HC02         gap_continue           1.0000     1.0000     1.0000     5        5      5
HC03         no_relevant_prior      1.0000     0.0000     0.0000     5        0      0
...

Aggregate:
  set_precision: 0.7500
  set_recall:    0.7500
  f1:            0.7500
  n:             12

By category:
  gap_continue:
    set_precision: 1.0000
    set_recall:    1.0000
    f1:            1.0000
    n:             4
  ...
```

## 10. DB-backed adapter

### Overview

The `DbBackedRetriever` (in `adapters.py`), `DbNavAdapter` (in `nav_eval.py`),
and `DbHotContextSelector` (in `hotcontext_eval.py`) are thin wrappers that
translate the same evaluation protocols to read-only SQL queries against a
pgvector-enabled Postgres database. They exercise the **production messages
table** with the same query/scope semantics as the offline reference adapters.

### Environment requirement

All three adapters require the `DIRECT_DATABASE_URL` environment variable to
be set to a valid Postgres connection string (e.g.
`postgresql://user:pass@host:5432/dbname`). If the variable is unset,
construction raises:

```
ValueError: DIRECT_DATABASE_URL must be set to use DbBackedRetriever
```

### Lazy-import discipline

Database dependencies (`psycopg` and `pgvector`) are imported **inside
`__init__`**, not at module-load time. This preserves the offline guarantee:
the harness can still be run with only `pydantic` + `pyyaml` when no database
is configured, and the DB imports are only triggered when a DB adapter is
actually constructed.

### Scope model

When `retrieve()` receives `**extra_scope` kwargs (from `GoldenCase.extra_scope`),
the DB adapter applies production-scope filters (`bot_id`, `participant`,
`partner_share`, `date`). When those fields are absent, it falls back to
`thread_id` / `topic_id` / `all` scope — matching the offline adapter behavior.

The `DbNavAdapter` and `DbHotContextSelector` follow the same env-gating and
lazy-import pattern. They query the production `messages` table using
`ORDER BY sent_at` clauses to replicate the chronological semantics of
`PythonNavReference` and `PythonReferenceSelector`.

### Limitations

- The DB adapters use ILIKE text search, not pgvector cosine similarity. A
  future version will embed the query and use `<=>` operator for semantic
  ranking.
- The adapters assume a pgvector-enabled Postgres with the production
  `messages` table schema (`id`, `content`, `thread_id`, `topic_id`, `bot_id`,
  `sent_at`, `sender_id`, `recipient_id`, `partner_share`, etc.).
- DB adapter tests are marked `@pytest.mark.skipif(not os.environ.get(
  'DIRECT_DATABASE_URL'), ...)` and are skipped in offline CI.

### Running

```bash
# Requires DIRECT_DATABASE_URL + psycopg + pgvector installed.
python -m eval.retrieval.runner --adapter db
python -m eval.retrieval.nav_eval --adapter db
python -m eval.retrieval.hotcontext_eval --selector db
```

If `DIRECT_DATABASE_URL` is unset, the CLI catches the `ValueError`, prints
`Error: DIRECT_DATABASE_URL must be set to use DbBackedRetriever` to stderr,
and exits code 1 (no traceback).

## 11. Gate flags

Three gate flags assert minimum quality bars. Each exits non-zero on failure
with a human-readable message naming the failing condition.

### `--assert-m1-gate` (retrieval quality)

```
python -m eval.retrieval._make_comparison --assert-m1-gate
```

Loads the baseline and semantic-or-hybrid JSON reports and asserts:

1. `(semantic_or_hybrid).by_query_type['paraphrase']['recall@10'] >= 0.7`
2. `(semantic_or_hybrid).by_query_type['verbatim_quote']['recall@1'] >=
   baseline.by_query_type['verbatim_quote']['recall@1']`

Prefers the semantic report (when paraphrase n > 0), falls back to hybrid.
Fails with a message like:

```
M1 gate FAILED for semantic:
  - paraphrase recall@10 (0.6500) < 0.7
```

### `--assert-nav-gate` (navigation correctness)

```
python -m eval.retrieval.nav_eval --adapter reference --assert-nav-gate
```

Asserts that the aggregate exact pass rate is exactly **1.0** (every nav case
returns the exact expected ids in the exact expected order). Fails with:

```
NAV GATE FAILED: exact pass rate 92.86% < 1.0
```

Use this to catch regressions when nav-adapter logic changes.

### `--assert-m3-gate` (hot-context inclusion quality)

```
python -m eval.retrieval.hotcontext_eval --selector reference --assert-m3-gate
```

Asserts that:

- `aggregate['set_recall'] >= 0.8`
- `aggregate['set_precision'] >= 0.6`

Both conditions must hold; if either fails, the message names the failing
metric(s):

```
M3 GATE FAILED: set_recall=0.7500 < 0.8; set_precision=0.5500 < 0.6
```

### How to run all gates

```bash
python -m eval.retrieval._make_comparison --assert-m1-gate
python -m eval.retrieval.nav_eval --adapter reference --assert-nav-gate
python -m eval.retrieval.hotcontext_eval --selector reference --assert-m3-gate
```

All three exit 0 when quality bars are met — suitable for CI gating.

## 12. Real-data gate #2 (M4: non-message extraction)

### Purpose

Gate #2 validates that the retrieval eval harness can measure **non-message**
recall (memories, observations, distillations, artifacts, conversation notes,
themes) against a real production database — not just the synthetic corpus.

### ⚠️ Privacy: Sanitized-only reporting

The `extract_real_corpus.py` script reads **REAL, intimate user data** and
writes it to disk.  All outputs are **gitignored** (see `.gitignore` lines
covering `eval/retrieval/real_*.yaml`).  The script prints only sanitized
aggregates (message count, thread count, topic count, date range, non-message
row count) — **never raw content**.  When labeling is complete, delete the
extracted files:

```bash
rm eval/retrieval/real_corpus.yaml
rm eval/retrieval/real_golden_set.yaml
```

### Extracting a real corpus with non-message rows

```bash
# Messages only (backward-compatible, gate #1)
python -m eval.retrieval.extract_real_corpus \
    --limit 300 --since 2025-01-01 \
    --out eval/retrieval/real_corpus.yaml

# Include non-message searchable rows (gate #2)
python -m eval.retrieval.extract_real_corpus \
    --limit 300 --since 2025-01-01 \
    --include-non-message --non-message-limit 100 \
    --source-types memory,observation,theme,conversation_note,artifact \
    --out eval/retrieval/real_corpus.yaml
```

The output YAML includes:
- `messages`: list of CorpusMessage-compatible entries (as before)
- `non_message_sources`: list of entries with `source_type`, `id`, `topic_id`,
  `content`, `created_at`, `bot_id`, and `dyad_id` — the `extra_scope` fields
  needed for DbBackedRetriever golden cases

### Building a source-aware real golden set

1. Extract the corpus with `--include-non-message`.
2. Browse it with `python -m eval.retrieval.browse_corpus`.
3. Copy the template: `cp eval/retrieval/real_golden_set.template.yaml eval/retrieval/real_golden_set.yaml`
4. Fill in real ids for RC01–RC10 (see template comments).
5. Cases RC05–RC10 use `expected_source_keys` for non-message targets and
   require `extra_scope` with `viewer_user_id`, `bot_id`, and `topic_id`.
6. Run the eval: `python -m eval.retrieval.runner --adapter db --golden eval/retrieval/real_golden_set.yaml`
7. Delete the files when done.

### Source-type gating

The `--source-types` flag accepts a comma-separated list.  The default
(`memory,observation,distillation,artifact,conversation_note,theme`) extracts
all six non-message families.  Narrow with e.g.
`--source-types theme,conversation_note` for targeted extraction.

### Bounded extraction

- `--limit` (default 300): max messages extracted.  **Never unbounded.**
- `--non-message-limit` (default 100): max non-message rows extracted.
  **Never unbounded.**
- Both limits are enforced at parse time; values ≤ 0 are rejected.

### Gitignore coverage

The following patterns in `.gitignore` protect real-data outputs:

```
eval/retrieval/real_corpus.yaml
eval/retrieval/real_golden_set.yaml
eval/retrieval/real_*.yaml
!eval/retrieval/real_golden_set.template.yaml
```

The template (`real_golden_set.template.yaml`) is the **only** committed file
with `real_` prefix — it contains **zero real data**, only placeholder strings.
All other `real_*.yaml` files are ignored.
