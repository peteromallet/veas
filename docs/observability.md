# Observability — Project A3 (work items 5 & 6)

This document describes the structured-log-based metrics layer, the three
alerts that page on reliability regressions, and the synthetic prober.

## Metrics layer

We do **not** ship a Prometheus/statsd client.  Every "metric event"
is emitted as a structured log line via `app.services.metrics`.  Each
record carries:

```json
{
  "metric": "<metric_name>",
  "metric_kind": "counter" | "gauge" | "histogram_obs",
  "value": <number>,
  "labels": { "<label>": "<value>", ... }
}
```

A log shipper (Vector, Loki, Datadog Agent, etc.) is expected to scrape
the `app.metrics` logger and turn these records into real series.

### Counters

| Metric | Labels | Where it's incremented |
| --- | --- | --- |
| `inbound_attempts_started` | `bot` | `inbound_queue.claim_messages_for_turn` — once per row actually claimed. |
| `inbound_attempts_completed` | `bot`, `failure_class` | `inbound_queue.complete_messages` (with `failure_class="success"`) and `inbound_queue.fail_messages` (with the SD-002 class). |
| `recovery_requeued` | `bot`, `reason` | `recovery._recover_v2_inbound` — `reason ∈ {crashed_turn, raw_message, stale_processing, retryable_failed}`. |
| `recovery_skipped_missing_coalescer` | `bot` | Every time recovery encounters a row whose `bot_id` has no installed coalescer. |
| `provider_fallback_invoked` | `from`, `to`, `phase`, `bot` | `agentic._create_message_with_retry`, right before the next provider call after the kill-switch/breaker gates pass. |

`failure_class` is one of the SD-002 three (`retryable_pre_send`,
`terminal_post_send`, `infra_bug`) on `fail_messages`, plus the
sentinel `success` on `complete_messages`.

### Gauges / histograms

Emitted every 5 minutes by `app.services.metrics_sweep`
(`run_metrics_sweep_forever`, wired in `app/main.py`):

| Metric | Kind | Labels | Notes |
| --- | --- | --- | --- |
| `terminal_rows_without_outbound` | gauge | `bot` | Count of inbound rows in the last hour that reached a terminal state with `handling_result='replied'` but whose linked `bot_turns` has `final_output_message_id IS NULL`. |
| `attempt_age_seconds` | histogram_obs | `bot`, `quantile ∈ {p50,p95,p99}` | Three observations per bot per sweep, computed via `percentile_cont` over `handled_at - processing_started_at`. |

## Alerts

Pasted here so they can be ingested by whatever alerting layer is in
front of the log shipper.  The shipper is expected to convert the metric
events into PromQL-shaped series; rule expressions below assume that
shape.

```yaml
# observability/alerts.yaml
groups:
- name: agent-reliability
  rules:
  - alert: RecoverySkippedMissingCoalescer
    expr: sum(increase(recovery_skipped_missing_coalescer[5m])) > 0
    for: 0m
    labels:
      severity: page
    annotations:
      summary: "Recovery skipped a row because no coalescer is installed for its bot"
      runbook: "docs/observability.md#recoveryskippedmissingcoalescer"

  - alert: HighFailedAttemptRate
    # Default threshold: 10/min across all bots & classes; tune via the
    # `RELIABILITY_FAILED_ATTEMPT_THRESHOLD_PER_MIN` env var on the
    # alerting side.
    expr: |
      sum(rate(inbound_attempts_completed{failure_class!="success"}[5m])) * 60 > 10
    for: 5m
    labels:
      severity: warn
    annotations:
      summary: "Failed-attempt rate >10/min over 5 minutes"

  - alert: HighProviderFallbackRate
    # Default threshold: 20% of inbound_attempts_started become fallbacks
    # over a 10-minute window; tune via
    # `RELIABILITY_FALLBACK_RATE_THRESHOLD` on the alerting side.
    expr: |
      sum(rate(provider_fallback_invoked[10m]))
        /
      clamp_min(sum(rate(inbound_attempts_started[10m])), 1e-9)
      > 0.20
    for: 10m
    labels:
      severity: warn
    annotations:
      summary: "Provider fallback rate >20% over 10 minutes"
```

### RecoverySkippedMissingCoalescer (runbook)

This fires when the recovery-v2 loop found an inbound row whose
`bot_id` is unknown to the `CoalescerRegistry`.  Per A1 this is the
SRE-flagged case: the row is left in `failed` rather than crashing
recovery.  Action: check `app/main.py` startup logs for a missing
bot coalescer install, and `BOT_SPECS` registration for the offending
`bot_id`.

## Synthetic prober

`scripts/synthetic_prober.py` injects a synthetic inbound "ping" row for
each known bot and asserts that the row reaches a terminal-success
state (`processing_state IN ('processed','expired')` with
`handling_result='replied'`) within a configurable SLO (default 60s).

It emits one structured log line per bot probed:

```
synthetic_probe{bot,latency_seconds,reached_terminal}
```

Exit code:

* `0` — every probed bot reached terminal success within the SLO.
* non-zero — at least one bot failed.

### Running

```bash
python scripts/synthetic_prober.py
python scripts/synthetic_prober.py --slo-seconds 90 --bots mediator,hector
python scripts/synthetic_prober.py --database-url "$DATABASE_URL"
```

### Scheduling

Designed to be invoked by an external cron (Railway scheduled job
recommended).  Example Railway cron config (15-minute cadence):

```toml
[[services]]
name = "synthetic-prober"
schedule = "*/15 * * * *"
command = "python scripts/synthetic_prober.py"
```

The prober logs are picked up by the same log shipper as the rest of
the metrics layer; alert on a single failed probe within any 15-minute
window.


## Ledger dual-write (Project C, C2)

The `mediator.inbound_handling_attempts` ledger (migration `0044`) is
**dual-written** alongside the existing `mediator.messages.next_retry_at`
and `mediator.messages.failure_class` columns.  The read path is
unchanged — recovery and retry sweepers continue to consult the
`messages` columns; the ledger is observability + future migration
runway.

### Flag

`Settings.ledger_dual_write_enabled` (default **False**).

When **OFF**:

* `claim_messages_for_turn`, `complete_messages`, `fail_messages` do not
  touch `inbound_handling_attempts`.
* The startup reconciliation pass
  (`reconcile_ledger_active_attempts`) is a no-op.
* The backfill script still runs on demand if invoked manually, but
  nothing else writes to the table.

When **ON**:

* Each claim opens a new ledger row with `status='active'`,
  `created_by='live'`, and `attempt_number = messages.processing_attempts`.
* `complete_messages` updates the matching active row to
  `status='succeeded'`.
* `fail_messages` updates the matching active row to `status='failed'`
  with `failure_class`, `failure_reason`, and a copy of
  `messages.next_retry_at`.
* Startup runs `reconcile_ledger_active_attempts` which scans for
  messages stuck in `processing` with no active ledger entry and
  synthesises one (`created_by='catch_up'`).

### Kill switch

Setting `LEDGER_DUAL_WRITE_ENABLED=false` (or removing the env var) and
restarting the app reverts to messages-only writes.  No schema rollback
is required — the table can stay in place.  Migration `0044` is
reversible via `0048_inbound_handling_attempts.down.sql` if the table
is removed altogether.

### Backfill

`scripts/backfill_inbound_handling_attempts.py` walks `messages` rows
lacking ledger entries and inserts one attempt each (`created_by='backfill'`).
Idempotent: safe to re-run.  Emits one
`backfill_inbound_handling_attempts{status}` counter per ledger-status
bucket on completion.

### Counters added

* `backfill_inbound_handling_attempts{status}` — from the backfill script.
* `ledger_reconcile_catch_up_opened` — from
  `reconcile_ledger_active_attempts` at startup.
