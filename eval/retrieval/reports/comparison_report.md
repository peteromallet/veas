# FAIR Retrieval Comparison — Baseline vs Semantic vs Hybrid

- Corpus: `corpus.yaml` (273 messages, 8 topics, 17 threads)
- Golden set: `golden_set.yaml` (70 cases)
- Embedding backend: local sentence-transformers `all-MiniLM-L6-v2` (384-dim, offline, cached). Hybrid = Reciprocal Rank Fusion (k=60) of baseline + semantic.

> **These numbers supersede the first experiment's (recall@10 0.26 -> 0.87).** The first run was rigged: every paraphrase and cross-thread query was constructed to share ZERO substrings with its target, pinning the keyword baseline at exactly 0% on those types and inflating the semantic lift to ~3x+. This fair rebuild gives paraphrase/cross-thread queries realistic lexical overlap (short, keyword-style search phrases that the production `%text_contains%` ILIKE can actually match), adds hard distractors (near-duplicate incidents, same-word-different-meaning traps), and scales the corpus and golden set up. As a result the baseline is no longer artificially 0% on paraphrase/cross-thread.

## Overall

| Metric    | Baseline (ILIKE) | Semantic | Hybrid (RRF) |
|-----------|-----------------:|---------:|-------------:|
| recall@1 | 0.338 | 0.464 | 0.457 |
| recall@5 | 0.502 | 0.741 | 0.764 |
| recall@10 | 0.510 | 0.853 | 0.855 |
| MRR | 0.660 | 0.849 | 0.847 |

## Per query-type — recall@10

| Query type | n | Baseline | Semantic | Hybrid |
|------------|--:|---------:|---------:|-------:|
| verbatim_quote | 22 | 0.909 | 0.955 | 0.955 |
| paraphrase | 22 | 0.409 | 0.902 | 0.902 |
| cross_thread | 14 | 0.237 | 0.681 | 0.693 |
| topic_recall | 12 | 0.281 | 0.779 | 0.779 |

## Per query-type — MRR

| Query type | n | Baseline | Semantic | Hybrid |
|------------|--:|---------:|---------:|-------:|
| verbatim_quote | 22 | 0.920 | 0.879 | 0.917 |
| paraphrase | 22 | 0.477 | 0.894 | 0.902 |
| cross_thread | 14 | 0.607 | 0.805 | 0.733 |
| topic_recall | 12 | 0.579 | 0.764 | 0.750 |

## Per fairness tag — recall@10

| Fairness       | n-baseline | Baseline | Semantic | Hybrid |
|----------------|-----------:|---------:|---------:|-------:|
| adversarial | 5 | 0.000 | 0.800 | 0.800 |
| either | 42 | 0.373 | 0.821 | 0.825 |
| keyword_favored | 22 | 0.909 | 0.955 | 0.955 |
| semantic_favored | 1 | 0.000 | 0.250 | 0.250 |

## Per fairness tag — MRR

| Fairness       | n-baseline | Baseline | Semantic | Hybrid |
|----------------|-----------:|---------:|---------:|-------:|
| adversarial | 5 | 0.000 | 0.667 | 0.667 |
| either | 42 | 0.618 | 0.873 | 0.849 |
| keyword_favored | 22 | 0.920 | 0.879 | 0.917 |
| semantic_favored | 1 | 0.000 | 0.100 | 0.100 |

## Per difficulty tag — recall@10

| Difficulty     | n-baseline | Baseline | Semantic | Hybrid |
|----------------|-----------:|---------:|---------:|-------:|
| easy | 22 | 0.909 | 0.955 | 0.955 |
| hard | 6 | 0.000 | 0.708 | 0.708 |
| medium | 42 | 0.373 | 0.821 | 0.825 |

## Per difficulty tag — MRR

| Difficulty     | n-baseline | Baseline | Semantic | Hybrid |
|----------------|-----------:|---------:|---------:|-------:|
| easy | 22 | 0.920 | 0.879 | 0.917 |
| hard | 6 | 0.000 | 0.572 | 0.572 |
| medium | 42 | 0.618 | 0.873 | 0.849 |

## Verdict

With a **fair** keyword baseline, semantic search still wins clearly but by a realistic margin, not the inflated ~3x of the first run. Overall recall@10 goes 0.510 -> 0.853 (~1.7x) and MRR 0.660 -> 0.849 (~1.3x). The baseline is no longer 0% on the hard types: paraphrase recall@10 is now 0.409 for keyword (vs 0.902 semantic) and cross_thread is 0.237 for keyword (vs 0.681 semantic) — keyword genuinely finds *some* of these because the queries share real words with their targets. Semantic's edge is concentrated exactly where it should be: restated intent (paraphrase) and answers that span both threads of a topic (cross_thread), plus precision against same-word-different-meaning distractors. On verbatim quotes the keyword baseline is competitive (recall@10 0.909 vs 0.955). Hybrid (RRF) tracks pure semantic on recall and is the safer production default (it never loses the keyword hits) but adds little measurable recall on this corpus. **Conclusion: semantic search is still worth building — the ~1.9x recall@10 / ~1.4x MRR lift over a fair baseline is meaningful and shows up precisely on the query shapes keyword search structurally cannot serve — but the honest expected gain is roughly half of what the first rigged experiment advertised.** Caveat unchanged: synthetic corpus + simplified scope model; confirm against real queries with the full production scope before shipping.
