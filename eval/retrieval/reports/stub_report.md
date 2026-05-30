# Retrieval Evaluation Report

- **Adapter:** StubSemanticRetriever
- **Corpus:** /Users/peteromalley/Documents/Veas/eval/retrieval/corpus.yaml
- **Golden Set:** /Users/peteromalley/Documents/Veas/eval/retrieval/golden_set.yaml
- **Generated:** 2026-05-30T03:51:52.849776+00:00
- **Cases:** 70

## Overall Metrics

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 70 |

## Per Query-Type Metrics

### cross_thread

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 14 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC37 | bring food | topic | 6 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC38 | deploy | topic | 6 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC39 | overkill for our scale | topic | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC40 | login flow | all | 6 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC41 | Lisbon | topic | 5 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC42 | rate limiting | all | 5 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC43 | broken | topic | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC44 | training plan | topic | 5 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC45 | budget | topic | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC46 | duplicate charge | all | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC47 | 6 AM Saturday | topic | 5 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC48 | audit findings | topic | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC49 | Atlas is down | topic | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC50 | other apartments | topic | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

### paraphrase

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 22 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC15 | login integration | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC16 | migration scripts blocked | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC17 | don't forget sunscreen | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC18 | caching layer | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC19 | duplicating transaction | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC20 | apartment search | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC21 | dishes in the sink | all | 3 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC22 | car repair | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC23 | query latency | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC24 | window seat | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC25 | rollback plan | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC26 | drafty window | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC27 | rooftop reservation | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC28 | token theft risk | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC29 | sunburn anecdote | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC30 | NPE fix | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC31 | demoralized after the launch | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC32 | UV protection | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC33 | food and drinks to pack | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC34 | hiding money stress | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC35 | brute force | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC36 | partitioning the events table | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

### topic_recall

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 12 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC51 | authentication module | topic | 3 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC52 | payment processor | topic | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC53 | Blue Ridge hike | thread | 5 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC54 | dinner | thread | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC55 | latency | all | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC56 | feels equal | topic | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC57 | Belem tower | thread | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC58 | gym membership | thread | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC59 | Atlas launch | topic | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC60 | beta rollout | thread | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC61 | call the landlord | thread | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC62 | half marathon training | thread | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

### verbatim_quote

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 22 |

#### Cases

| Case ID | Query | Scope | Expected | Retrieved | Recall@1 | Recall@5 | Recall@10 | MRR |
|---------|-------|-------|----------|-----------|----------|----------|-----------|-----|
| GC01 | I told you so | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC02 | fine. | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC03 | osso buco | all | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC04 | Blue Ridge | all | 6 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC05 | idempotency key | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC06 | rate limiting | thread | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC07 | httpOnly cookies | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC08 | worker pool | all | 4 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC09 | osso buco | thread | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC10 | kitchen faucet | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC11 | half marathon | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC12 | Lisbon flights | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC13 | running shoes | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC14 | sure | thread | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC63 | sprint review | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC64 | first aid kit | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC65 | design doc | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC66 | moving quotes | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC67 | monthly budget | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC68 | audit trail | all | 1 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC69 | dishwasher | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| GC70 | feature flags | all | 2 | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

## Per Fairness Metrics

### adversarial

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 5 |

### either

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 42 |

### keyword_favored

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 22 |

### semantic_favored

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 1 |

## Per Difficulty Metrics

### easy

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 22 |

### hard

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 6 |

### medium

| Metric    | Value |
|-----------|-------|
| mrr | 0.0000 |
| recall@1 | 0.0000 |
| recall@10 | 0.0000 |
| recall@5 | 0.0000 |
| n         | 42 |
