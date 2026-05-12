# S2a Pre-flight Validation Report

**Date**: 2026-05-11 (re-verified 2026-05-12)
**Branch**: `s2a-stamp-rekey-observability`
**S1 Tip Commit**: `d4c2a7c` (S1 — Foundation schema + code shape for multi-agent buildout)

---

## ⚠️ T2 (Eval Baseline Capture) — DEFERRED

**T2 is DEFERRED per user instruction.** The Supabase transaction-mode pooler (port 6543) does not preserve `SET search_path` across pooled connection acquisitions, so `python scripts/capture_eval_baseline.py` cannot connect reliably. Capture is deferred to S3 pre-flight or end-of-sprint manual run via port 5432 session-mode.

- `scripts/capture_eval_baseline.py` does NOT exist and should NOT be created in S2a.
- `tests/fixtures/eval_baseline/` does NOT exist and should NOT be created in S2a.
- T17 (step 17.2) eval baseline diff is explicitly SKIPPED with a PR-description note.
- Success criterion 13 ("Eval baseline captured...") is downgraded from **must** to **should/deferred** for S2a DoD.
- U1 tracks the deferred work as a human-only after_execute action.

---

## (a) `mediator.artifact_topics` Row Count

**Query**: `SELECT count(*) FROM mediator.artifact_topics`
**Result**: **235** ✅
**Verdict**: Matches expected S1 baseline. S1 backfill held; no new writes have occurred since S1 landed.

---

## (b) Seed Tables: `mediator.user_identities` and `mediator.channels`

### `mediator.user_identities`
- **Count**: 4 rows (>0 ✅) *(re-verified 2026-05-12 via psql)*
- **Columns**: `transport`, `address`, `user_id`, `verified_at`, `created_at`
- **Note**: No `provider` column (non-legacy check satisfied by existence of rows — these are S1-seeded identities).

### `mediator.channels`
- **Count**: 1 row (>0 ✅)
- **Columns**: `id`, `bot_id`, `transport`, `address`, `guild_id`, `channel_id`, `config`, `created_at`
- **Existing row**: Discord channel for bot address `1245222614276898866` (bot_id=`mediator`).
- **Note**: If channels had been empty in dev, `python scripts/seed_channels.py` would be the fallback. Not needed — already seeded.

---

## (c) Comprehensive INSERT Site Grep Sweep

Full sweep of every `INSERT INTO` under `app/` against target tables recorded in `migrations/validation/s2a_insert_sites.md`. Summary:

| Table | Files (lines) | Count |
|---|---|---|
| `messages` | `inbound.py:161`, `messaging.py:55,84` | 3 |
| `bot_turns` | `agentic.py:483` | 1 |
| `scheduled_jobs` | `agentic.py:577`, `scheduled_jobs.py:216`, `checkins.py:37`, `scheduled_job_handlers.py:200,318`, `write_tools.py:183,1367` | 7 |
| `feedback` | `inbound.py:105`, `discord.py:470`, `write_tools.py:1763` | 3 |
| `bridge_candidates` | `write_tools.py:305` | 1 |
| `memories` | `write_tools.py:699,746` | 2 |
| `themes` | `write_tools.py:764` | 1 |
| `watch_items` | `write_tools.py:800` | 1 |
| `observations` | `write_tools.py:878` | 1 |
| `distillations` | `write_tools.py:939,1049` | 2 |
| `out_of_bounds` | `write_tools.py:1109` | 1 |
| `tool_calls` | `write_tools.py:136` | 1 (not stamped in S2a) |
| `withheld_outbound_reviews` | `withheld_reviews.py:26` | 1 (no column change in S2a) |
| `pacing_events` | `user.py:212` | 1 (out of scope) |
| `users` | `user.py:155` | 1 (out of scope) |
| `turn_audit_events` | `turn_audit.py:88` | 1 (observability only) |
| `system_state` | `system_state.py:32,48` | 2 (out of scope) |
| `llm_spend_log` | `spend.py:27` | 1 (out of scope) |

**Tables requiring `bot_id`/`topic_id` stamps in S2a**: `messages`, `bot_turns`, `scheduled_jobs`, `feedback`, `bridge_candidates`, `memories`, `themes`, `watch_items`, `observations`, `distillations`, `out_of_bounds`.

**Tables requiring `artifact_topics` companion rows in S2a**: `memories`, `themes`, `watch_items`, `observations`, `distillations`, `out_of_bounds`.

---

## (d) Direct-Outbound Audit

**Query**: Grep for `INSERT INTO messages` with `direction='outbound'` outside `messaging.py`.
**Result**: **Empty** ✅

All `INSERT INTO messages` with `direction='outbound'` are confined to:
- `messaging.py:55-59` (with `bot_turn_id`/`outbound_part_key` columns)
- `messaging.py:84-85` (simple outbound)

No other file under `app/` performs a direct outbound messages INSERT. All outbound message creation routes through `messaging.py`'s `_insert_outbound` helper, called via `send_outbound` / `send_outbound_part`.

---

## (e) `withheld_reviews.py` — `record_withheld_outbound_review`

- **Current INSERT**: Line 26 — `INSERT INTO withheld_outbound_reviews (recipient_id, sender_id, outbound_id, original_content, suggested_rewrite, reason, verdict, checker_failed, status, created_at, updated_at)`
- **S2a readiness**: Function signature will accept `bot_id`/`topic_id` as NEW OPTIONAL kwargs (default `None`) in S2a. The SQL column list will NOT change — `withheld_outbound_reviews` has no scope columns yet. S2b adds the columns.
- **Caller contract**: Callers in `messaging.py:207-216` and `:342-365` will pass `ctx.bot_id`/`ctx.primary_topic_id` through these new kwargs. The values are accepted and stored for S2b readiness but are no-ops at the DB layer in S2a.

---

## Existing Column Shape (Post-S1, Pre-S2a)

### `messages` — existing `bot_id` and `topic_id` columns present
- `bot_id` (text, nullable) — added in S1 migration
- `topic_id` (uuid, nullable) — added in S1 migration

### `bot_turns` — existing scope columns present
- `topic_id`, `bot_id`, `bot_spec_version`, `hot_context_builder_version`, `tool_schema_version`

### `scheduled_jobs` — existing scope columns present
- `topic_id`, `bot_id`

### `feedback` — existing scope columns present
- `topic_id`, `bot_id`

### `bridge_candidates` — existing scope columns present
- `topic_id`, `bot_id`, `dyad_id`

### Artifact tables — `recorded_by_bot_id` present on all
- `memories.recorded_by_bot_id`, `themes.recorded_by_bot_id`, `observations.recorded_by_bot_id`, `watch_items.recorded_by_bot_id`, `distillations.recorded_by_bot_id`, `out_of_bounds.recorded_by_bot_id`

### `artifact_topics` — ready
- `artifact_table`, `artifact_id`, `topic_id`, `status`, `tagged_by_bot_id`, `reason`, `created_at`, `retired_at`

---

## Lint Advisory Note

CI will warn (not fail) in S2a on:
- New `INSERT INTO messages|bot_turns|scheduled_jobs|feedback|bridge_candidates` missing `bot_id`/`topic_id`
- New artifact `INSERT INTO memories|themes|observations|watch_items|distillations|out_of_bounds` lacking a companion `INSERT INTO artifact_topics` in the same SQL statement

Blocking lint is deferred to S2b.

---

## Dashboard Panes

Per-bot dashboard panes are downstream config, out of scope for S2a code changes. Noted for observability completeness.

---

## Verification Summary

| Check | Expected | Actual | Status |
|---|---|---|---|
| `artifact_topics` count | 235 | 235 | ✅ (re-verified 2026-05-12 via psql) |
| `user_identities` rows | >0 | 4 | ✅ (re-verified 2026-05-12 via psql) |
| `channels` rows | >0 | 1 | ✅ (re-verified 2026-05-12 via psql) |
|| INSERT sweep recorded | Complete | 31 sites mapped | ✅ |
|| Direct outbound audit | Empty | No hits | ✅ |
|| Pytest baseline | 424 passed | 424 passed, 3 skipped | ✅ |


## T10 — Bridge Candidate + Feedback + Withheld Reviews Stamping (COMPLETE)

**Status**: ✅ All three sub-items verified complete.

1. **`bridge_candidates` INSERT** (`write_tools.py:305-329`): Columns `bot_id`, `topic_id`, `dyad_id` present. Values bound to `ctx.bot_id`, `ctx.primary_topic_id`, `ctx.dyad_id` (NOT `ctx.binding_id`). ✅

2. **Feedback INSERTs**:
   - (a) `log_feedback` (`write_tools.py:1858-1877`): INSERT includes `bot_id`, `topic_id` columns. Values bound to `ctx.bot_id`, `ctx.primary_topic_id`. ✅
   - (b) Discord reaction handler (`discord.py:468-478`): Resolves scope via `discord_bot_user_id()` (the bot's address, NEVER the reacting user). `_resolve_scope(self.pool, 'discord', bot_user_id)` used. INSERT at :481-491 stamps `bot_id`, `topic_id`. ✅

3. **`record_withheld_outbound_review`** (`withheld_reviews.py:10-45`): Function signature accepts `bot_id`/`topic_id` as optional kwargs (default `None`). SQL column list unchanged (no scope columns yet — S2b adds them). Callers at `messaging.py:219-229` and `:360-370` pass `bot_id`/`topic_id`. ✅

4. **s2a_preflight.md** updated with this readiness documentation. ✅


## T14 — Per-Bot Observability Fields Fan-Out (COMPLETE)

**Status**: ✅ All sub-items implemented.

1. **`obs_fields(ctx_or_scope)`** added to `app/services/turn_context.py`. Returns `{'bot_id': ..., 'topic_id': ..., 'channel_id': ..., 'binding_id': ...}` with None values filtered. Accepts `TurnContext`, `ResolvedScope`, or any object with those attributes. ✅

2. **Hot-path `logger.*` calls** updated with `extra=` or `# obs N/A:` comment:
   - **`inbound.py`**: 6 logger sites updated. `_handle_reaction` passes `extra={'bot_id': ..., 'topic_id': ...}`. `_resolve_scope` debug logs carry `bot_id`/`topic_id`. `_control_recipients` and `process_inbound` (pre-scope) annotated `# obs N/A: ...`. ✅
   - **`agentic.py`**: 6 logger sites. `_call_anthropic_with_retry`, `_run_agentic` (charged silence), `_run_agentic` (post-outbound failure) pass `extra=obs_fields(ctx)`. Wrapper functions annotated `# obs N/A: wrapper (no ctx)`. ✅
   - **`messaging.py`**: 3 logger sites in `send_outbound_part`/`send_outbound` pass `extra={'bot_id': ..., 'topic_id': ...}`. ✅
   - **`scheduled_job_handlers.py`**: 2 logger sites. `handle_heartbeat` passes `extra={'bot_id': job.get(...), 'topic_id': job.get(...)}`. `_zoneinfo` annotated `# obs N/A: startup/config tz lookup`. ✅
   - **`oob_check.py`**: 2 logger sites annotated `# obs N/A: no scope in checker`. ✅
   - **`turn_audit.py`**: 1 logger site annotated `# obs N/A: audit fallback`. ✅

3. **Discord emitter line ranges**: `run_forever:361` annotated `# obs N/A: transport-only`. `_handle_message:421` annotated `# obs N/A: pre-scope gateway`. `_handle_reaction_add:463` passes `extra={'bot_id': ..., 'topic_id': ...}`. `catch_up_recent_messages:546` annotated `# obs N/A: no scope in catch-up`. Emitter functions (`send_text`, `add_reaction`, `edit_text`, `delete_text`, `send_typing`) have no logger calls — annotated in function docstrings. ✅

4. **`whatsapp.py`**: Zero logger calls found (verified full file scan). N/A. ✅

5. **`hooks.py`**: NEW `logger.debug("paused_for_user check", extra={'user_id': ..., 'bot_id': ...})` added at `paused_for_user:37`. Logging infrastructure (import + getLogger) added to file. ✅

6. **`_default_check_oob`** (`hooks.py:13-28`): No logger calls currently. Logging infrastructure available if needed in future. ✅

7. **`withheld_reviews.py`**: Logging infrastructure (import + getLogger) added for S2b readiness. No logger calls in S2a. ✅

8. **`record_turn_event` metadata**: `turn.opened` event at `agentic.py:742` now carries `bot_id`, `topic_id`, `channel_id`, `binding_id` in metadata dict. Other `record_turn_event` call sites inside `_run_agentic` have access to `ctx` scope. ✅

9. **Dashboard panes**: Noted in preflight as downstream config, out of scope. ✅
| **T2 (eval baseline) deferral** | Documented at top | Explicit deferral + rationale | ✅ |