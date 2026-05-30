# Retrieval Evaluation Report

- **Adapter:** IlikeBaselineRetriever
- **Corpus:** /Users/peteromalley/Documents/Veas/eval/retrieval/corpus.yaml
- **Golden Set:** /Users/peteromalley/Documents/Veas/eval/retrieval/golden_set.yaml
- **Generated:** 2026-05-30T00:35:11.084551+00:00
- **Cases:** 28

## Overall Metrics

| Metric    | Value |
|-----------|-------|
| mrr | 0.2500 |
| recall@1 | 0.1429 |
| recall@10 | 0.2619 |
| recall@5 | 0.2560 |
| n         | 28 |

## Per Query-Type Metrics

### cross_thread

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 4 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC25 | What are the weekend plans for food? | topic | 8 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC26 | What deployment issues have come up? | topic | 9 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC27 | performance and scaling discussions | topic | 6 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC28 | Saturday morning plans | topic | 8 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

### paraphrase

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 10 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC15 | login system status | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC16 | migration scripts delayed | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC17 | UV protection reminder | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC18 | in-memory cache architecture | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC19 | meal beverage planning | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC20 | schedule coordination | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC21 | NPE resolution | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC22 | test environment throttle | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC23 | sunburn story | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC24 | weekend logistics | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

### topic_recall

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 6 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC01 | What's the status of the Nexus project authentication module? | topic | 3 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC02 | What bugs have been reported in the Nexus project? | topic | 7 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC03 | What's the plan for the Saturday hike? | thread | 9 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC04 | Where are we eating dinner Saturday night? | thread | 6 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC05 | What are our weekend plans? | topic | 8 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC06 | Has anyone mentioned performance problems or scaling concerns? | all | 7 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

### verbatim_quote

| Metric    | Value |
|-----------|-------|
| mrr | 0.8750 |
| recall@1 | 0.5000 |
| recall@10 | 0.9167 |
| recall@5 | 0.8958 |
| n         | 8 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC07 | I told you so | all | 1 | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC08 | fine. | all | 1 | 2 | 0.0000 | 1.0000 | 1.0000 | 0.5000 |
| GC09 | sure | all | 1 | 2 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC10 | osso buco | all | 3 | 4 | 0.0000 | 1.0000 | 1.0000 | 0.5000 |
| GC11 | Blue Ridge | all | 6 | 6 | 0.1667 | 0.8333 | 1.0000 | 1.0000 |
| GC12 | CI pipeline is green | all | 1 | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| GC13 | idempotency key | all | 2 | 2 | 0.5000 | 1.0000 | 1.0000 | 1.0000 |
| GC14 | rate limiting | thread | 3 | 1 | 0.3333 | 0.3333 | 0.3333 | 1.0000 |
