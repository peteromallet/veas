# Agent Reliability Cleanup — Revised Plan

Date: 2026-05-16
Supersedes: `docs/agent-reliability-cleanup-sprints.md` (kept as the original analysis)

## Settled Decisions

- **SD-001** — Do NOT create an `inbound_handling_attempts` table or any new attempt-ledger table. Add `next_retry_at` and `failure_class` columns to the `messages` table instead. _load_bearing: true_
  Rationale: Architect and pragmatic critiques agree the ledger duplicates state already in `bot_turns` + `messages`; migration risk is a sprint by itself.
- **SD-002** — Failure classes are exactly three: `retryable_pre_send`, `terminal_post_send`, `infra_bug`. Do not expand the taxonomy in Project A or B. _load_bearing: true_
  Rationale: 7-class taxonomy from the original plan is YAGNI until dashboards or policy actually consume more classes.
- **SD-003** — Do NOT build a canonical `InternalMessage` IR or provider-neutral message type. Keep sanitization at the fallback boundary. _load_bearing: true_
  Rationale: Commit `bf5c22d` is the reference pattern; the abstraction is over-scoped without a third provider.
- **SD-004** — `scripts/why_no_reply.py` is the schema acceptance test for Project A. If the new schema cannot answer it trivially, fix the schema, not the script. _load_bearing: true_
  Rationale: Forces the lifecycle design to be adequate to the one query that proves it works.
- **SD-005** — The new recovery path must ship behind a feature flag with a verified kill switch (flip-off without redeploy). _load_bearing: true_
  Rationale: Persistence-touching changes need a runtime safety valve; SRE critique flagged this as missing.
- **SD-006** — Fallback policy must handle `Retry-After` on 429/529 and include a fallback-rate circuit breaker. A fallback chain whose primary equals the fallback target is a no-op that fails retryable. _load_bearing: true_
  Rationale: Otherwise Anthropic 429 mid-fallback storm is a textbook cascade; cost runaway from constant fallback is a real risk.
- **SD-007** — Manual repair CLI is out of scope. `scripts/why_no_reply.py` plus a runbook of canned SQL snippets is the operator's tool. _load_bearing: false_
  Rationale: Solo operator; CLI tooling is gold-plating.
- **SD-008** — Real-Postgres CI (Project B) is timeboxed to 2 days. If it bleeds past, fall back to a pre-commit script against a local dev DB. _load_bearing: false_
  Rationale: Spinning up Postgres-in-CI for the first time inside a sprint is a known yak-shave; bound the risk.
- **SD-009** — Project C is conditional. Only execute if a second incident proves the simplified schema inadequate, a third provider is added, or attempt-history is required for compliance. Otherwise it is cancelled, not deferred. _load_bearing: false_
  Rationale: All three critiques flagged Project C as gold-plating absent a concrete trigger. _Status: user override — execute regardless._
- **SD-010** — All megaplans use the `all-claude` profile at `standard` robustness with auto-approve on. Project A is sliced into three sub-megaplans (A1, A2, A3) to fit the megaplan parser output cap. _load_bearing: true_
  Rationale: User-mandated profile; the original single Project A plan exceeded the 20k-char parser cap when emitted by the Claude planner.

## Why This Revision Exists

The original 3-sprint plan was critiqued from operational, architectural, and pragmatic perspectives. Convergent findings: the proposed `inbound_handling_attempts` ledger duplicates state already in `bot_turns` + `messages`; the "canonical internal message format" is over-scoped without a concrete schema; observability and migration safety are dangerously thin; the "why no reply?" diagnostic is the acceptance test for the lifecycle work, not a follow-up.

This revision keeps the high-value, incident-preventing work and defers the cleanup-for-cleanliness work until a second incident proves it necessary.

## Shape

Three sequential 2-week projects. Project C is **conditional** — only execute if A and B reveal a need.

1. **Project A: Reliability Core** (always)
2. **Project B: Test Infrastructure + Audit Hardening** (always)
3. **Project C: Canonical Message IR + Attempt Ledger** (only if needed)

---

## Project A: Reliability Core (2 weeks)

### Goal

Close the operational risk surfaced by the Hector incident without introducing migration risk from new schema. By the end: no inbound message gets stranded; provider failures degrade gracefully; an operator can answer "why didn't the bot reply?" in one command.

### Work Items

1. **Bot-aware recovery by construction.**
   - Replace any single-coalescer parameter with a `CoalescerRegistry` (or `dict[str, BurstCoalescer]`) threaded through recovery.
   - Missing-coalescer is a structured warning + leave-retryable, never silent-recover.
   - Recovery readiness gate at process startup: don't claim rows until the registry reports all expected bots ready.

2. **Minimal lifecycle column additions on `messages`.**
   - Add `next_retry_at timestamptz` and `failure_class text` columns. No new tables.
   - Failure classes (only 3 to start): `retryable_pre_send`, `terminal_post_send`, `infra_bug`.
   - `claim_messages_for_turn`, `complete_messages`, `fail_messages` are the **only** code paths that mutate these. Document this as an invariant; add an assertion helper.
   - Stage the change: add columns nullable → write-only path first → read-path uses them → drop the old inference logic.

3. **Phase-aware tool-loop caps.**
   - `read` cap exhaustion → stop reading, advance to respond.
   - `respond` cap exhaustion before any output → retry/fallback; if still failing → mark `retryable_pre_send`.
   - `record`/`schedule` cap exhaustion → record non-user-facing failure; do not change the inbound row from `replied`.

4. **Fallback policy with cost + rate-limit guards.**
   - Per-bot fallback chain (e.g. `[deepseek, anthropic]`); "fallback to same provider as primary" is a no-op that goes straight to retryable failure.
   - Respect `Retry-After` on 429/529; do not eat rate limit signals.
   - Fallback budget / circuit breaker: if fallback rate > X% over 5 min, stop falling back, fail retryable, emit a high-priority log line.
   - Track fallback invocations as a first-class counter: `provider_fallback_invoked{from,to,phase,bot}`.

5. **`scripts/why_no_reply.py` as the schema acceptance test.**
   - Takes a message id (Discord or DB) and returns: inbound row state, current/last attempt, bot turn ids, tool calls, final outbound id if any, retry eligibility, next retry time, recommended action.
   - **Constraint:** if the new schema (item 2) can't answer this trivially, the schema is wrong — fix the schema, not the script.

6. **Observability.**
   - Counters: `inbound_attempts_started/completed/failed{bot,failure_class}`, `recovery_requeued{bot,reason}`, `recovery_skipped_missing_coalescer{bot}`, `provider_fallback_invoked{from,to,phase,bot}`, `terminal_rows_without_outbound{bot}`, attempt-age p95/p99.
   - Three alerts to start: `recovery_skipped_missing_coalescer > 0`, `failed-attempt rate > N/min`, `fallback rate > X% over 10m`.
   - One synthetic prober: drop a message per bot every 5 min and assert it reaches `replied` within SLO.

7. **Feature flag + kill switch.**
   - The new recovery path is behind a flag that defaults on but can be flipped off without redeploy, reverting to the prior sweeper.

### Acceptance Criteria

- A failed pre-send Hector turn is retried by Hector, not mediator.
- A failed pre-send Tante Rosi turn is retried by Tante Rosi, not mediator.
- A failed post-send turn never duplicates a user-visible reply.
- A DeepSeek failure cannot strand a message if Anthropic is available *and* has budget.
- A read-loop cap exhaustion does not suppress a response.
- `python scripts/why_no_reply.py <message_id>` answers the Hector-class incident with no hand-written SQL.
- The recovery kill switch is verified working in a staging-like environment.

### Non-Goals (deferred to B or C)

- New `inbound_handling_attempts` table.
- Canonical internal message format / `InternalMessage` type.
- 7-class failure taxonomy.
- Manual repair CLI tool (the script is the tool).
- Materialized audit views.

---

## Project B: Test Infrastructure + Audit Hardening (2 weeks)

### Goal

Make SQL regressions impossible to ship unnoticed, and make audit outputs reliable enough for bots to reason about their own actions.

### Work Items

1. **Real-Postgres CI — timeboxed 2 days.**
   - testcontainers or `docker compose` against the same Postgres major version as Railway.
   - One green test at end of day 2 is the gate; otherwise fall back to a pre-commit script that runs SQL tests against a local dev DB and add a CI step that runs the same script.
   - Migration runner wired into the test setup; schema is the real production schema.

2. **Three fixtures + expand-on-demand.**
   - `replied turn`, `silent turn`, `failed pre-send turn`. Add more only when a specific bug demands them.

3. **`get_bot_actions` as a SQL view (or generated query module), not application-layer joins.**
   - Define `v_bot_actions` once; the function becomes `SELECT * FROM v_bot_actions WHERE ...`.
   - Bot-scoping is enforced in the view's `WHERE` clause and cannot be opted out of without an explicit flag.
   - Test asserts that adding a new column to `messages`/`bot_turns` either appears in the view or fails CI.

4. **Round-trip tests for the two real fallback paths.**
   - DeepSeek → Anthropic with tool-call history.
   - Anthropic → DeepSeek with tool-result history.
   - These are not the full canonical IR — they are sanitization tests against the current code paths.

5. **Extend `why_no_reply.py` with whatever Project A's schema actually shipped.**

### Acceptance Criteria

- CI runs the SQL tests on every PR; a `GROUP BY`-class bug in `get_bot_actions` fails CI.
- The three fixtures cover the Hector incident pattern; the test suite would have caught it.
- Round-trip fallback tests pass; provider-native blocks never leak across boundaries in either direction.
- Audit view is bot-scoped by construction.

### Non-Goals

- 8-scenario fixture matrix (add lazily).
- Standardized failure-class taxonomy beyond the 3 already in A.
- Admin UI / route for audit tooling.

---

## Project C: Canonical Message IR + Attempt Ledger (2 weeks, **conditional**)

### Trigger Condition

Only execute Project C if at least one of these is true after B ships:

- A second incident occurs that the simplified A/B schema cannot diagnose or prevent.
- A third model provider is added (the abstraction debt becomes real).
- Attempt-history queries are needed for compliance or product reasons.

If none of these fire within 6 weeks of B shipping, this project is **cancelled**, not deferred.

### Work Items (sketch only — refine if triggered)

1. `internal_message.py` with a versioned schema and round-trip property tests; converters become trivial.
2. `inbound_handling_attempts` table with explicit migration plan (dual-write phase, backfill job, reconciliation pass for in-flight rows, kill switch).
3. Expanded failure-class taxonomy + `FAILURE_POLICY` decision table.
4. Migrate `failure_class` and `next_retry_at` from `messages` columns to the ledger.

---

## Sequencing

1. Project A first. Standard-robustness megaplan, all-Claude profile.
2. After A ships: 1-week observation window in production before starting B.
3. Project B. Standard-robustness megaplan, all-Claude profile.
4. Stop. Re-evaluate against the Project C trigger conditions.

## What Not To Do

(Carried forward from the original plan, still binding.)

- Do not add another loose sweeper that scans `messages` with different inference rules.
- Do not rely on `processing_state='raw'` alone as proof that a row will be retried.
- Do not make recovery single-bot or mediator-special.
- Do not pass provider-native message blocks across fallback boundaries.
- Do not treat all tool caps as fatal.
- Do not silently repair rows without an audit trail.

## Operator Runbook — Project A1 (recovery-v2 + lifecycle columns)

### Deployment handoff: migration `0046_message_lifecycle_columns.sql`

Apply against staging, verify, then promote to production.

```bash
# 1. Staging
psql "$DATABASE_URL_STAGING" -f migrations/0046_message_lifecycle_columns.sql

# 2. Verify the writer-marker trigger and assertion function are installed.
psql "$DATABASE_URL_STAGING" -c "\dft mediator.assert_lifecycle_columns_writer"

# 3. Promote to production once staging is clean.
psql "$DATABASE_URL_PROD" -f migrations/0046_message_lifecycle_columns.sql
psql "$DATABASE_URL_PROD" -c "\dft mediator.assert_lifecycle_columns_writer"
```

The trigger raises if anything other than the inbound-queue mutators
(`claim_messages_for_turn` / `complete_messages` / `fail_messages`) touches
`next_retry_at` or `failure_class` — those mutators set the txn-local
`app.lifecycle_writer` GUC to `inbound_queue` to pass the check.  No backfill
is required; both columns are nullable.

#### Manual trigger verification

`migrations/validation/test_0046_trigger.py` exercises the trigger against a
live Postgres but is skipped unless `RECOVERY_V2_TRIGGER_TEST_DB_URL` is set
(per SD-008 — no CI infra changes).  To reproduce the assertion by hand
against a scratch database that already has migration `0042` applied:

```bash
# Should RAISE: lifecycle write without the writer marker.
psql "$DATABASE_URL_SCRATCH" -c "
  BEGIN;
  UPDATE mediator.messages
     SET failure_class = 'retryable_pre_send'
   WHERE id = '<scratch-row-id>';
  ROLLBACK;
"

# Should SUCCEED: same write with the txn-local writer marker set.
psql "$DATABASE_URL_SCRATCH" -c "
  BEGIN;
  SELECT set_config('app.lifecycle_writer', 'inbound_queue', true);
  UPDATE mediator.messages
     SET failure_class = 'retryable_pre_send'
   WHERE id = '<scratch-row-id>';
  ROLLBACK;
"
```

Alternatively, set `RECOVERY_V2_TRIGGER_TEST_DB_URL` and run
`pytest migrations/validation/test_0046_trigger.py` to execute both halves
in one shot.

### Kill switch: `recovery_v2_kill`

The recovery-v2 inbound paths (raw / stale-processing / retryable-failed
recovery) are gated by a row in `mediator.system_state` with
`key='recovery_v2_kill'`.  The legacy invariants — scheduled_jobs
reconciliation, bot_turn crash-marking, and retention-expiry sweeps — are
NOT gated and continue running.

Engage (stop recovery-v2 without redeploy):

```sql
INSERT INTO mediator.system_state(key, value, updated_at)
VALUES ('recovery_v2_kill', '{"on": true}'::jsonb, now())
ON CONFLICT (key) DO UPDATE
SET value = EXCLUDED.value,
    updated_at = EXCLUDED.updated_at;
```

Disengage:

```sql
INSERT INTO mediator.system_state(key, value, updated_at)
VALUES ('recovery_v2_kill', '{"on": false}'::jsonb, now())
ON CONFLICT (key) DO UPDATE
SET value = EXCLUDED.value,
    updated_at = EXCLUDED.updated_at;
```

The reader (`app.services.system_state.is_recovery_v2_killed`) is consulted
inside `_recover_v2_inbound` and again at the top of each
`run_recovery_forever` tick before the v2 helper is invoked.

### Observable shift: failure-class taxonomy (4 legacy strings → 3 classes)

Project A1 collapses the legacy four-string failure surface to the SD-002
three-class taxonomy.  Two places observable in logs / DB shift:

- `turn_event` metadata `failure_class` (emitted at
  `app/services/agentic.py:878`, `:1566`, `:1748`).
- The `[failure_class=...]` substring embedded in `error_detail` strings
  (emitted at `app/services/agentic.py:1571` and `:1766`), which lands in
  `mediator.messages.processing_error`.

Both now record one of `retryable_pre_send`, `terminal_post_send`,
`infra_bug` (with unknown failure reasons falling through to `infra_bug`).
Dashboards, eval log greps, and any ad-hoc SQL that filters on the old
4-string surface must be updated.

## Definition Of Done For The Whole Program

After A + B (assuming C does not trigger):

- For any inbound Discord message id, `python scripts/why_no_reply.py <id>` answers "why did or didn't the bot reply?" in one command.
- A failed pre-send Hector turn retries through Hector and either sends or reaches an explicit retry cap.
- A failed post-send turn never duplicates a user-visible reply.
- A DeepSeek failure cannot strand a message if Anthropic is available and within fallback budget.
- A read-phase loop cannot suppress a response.
- Audit tools do not crash under production Postgres; CI catches `GROUP BY`-class regressions.
- Every background path is bot-scoped by construction (registry pattern), not by ad hoc parameters.
- Production has counters + 3 alerts + a synthetic prober per bot.
