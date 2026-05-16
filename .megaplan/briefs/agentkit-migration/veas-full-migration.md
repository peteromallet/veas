# Veas agentkit migration: full mediator-bot loop on the kernel

Profile intent: `thoughtful//high @codex +feedback`.

This milestone migrates Veas's mediator bot off its hand-rolled `_run_agentic` loop and onto `agentkit.loop` + `agentkit.plan.StepPlan`. Every operational primitive Veas currently re-implements (claim, coalesce, defer, audit, dual-LLM fallback, hot context, OOB gate) is replaced with the agentkit equivalent. The mediator's spec, prompt, charge logic, and escalation gates stay app-local.

## Prerequisites

- `agentkit v0.2.0` published and installable.
- `agentkit-bootstrap-chain.yaml` sprints 1 and 2 merged.

## Source plan

- `agentkit`: `docs/agentkit-design.md`, `docs/operational.md`, `docs/storage.md`.
- This repo: `app/services/agentic.py` (especially `_run_agentic` 1001+ and `run_step` at 400), `tool_schemas.py`, `app/services/deepseek.py`, `app/services/tools/registry.py`, `app/services/hot_context.py`, `app/main.py` (`BurstCoalescer` 189-234, `ScheduledJobWorker` 415), `app/services/inbound_queue.py` (`claim_messages_for_turn`), `mediator-bot-spec.md`.

## Goal

All Veas bots run on `agentkit` in prod. The mediator's behaviour, OOB semantics, crisis-escalation gates, spend-cap defer, and atomic message claim are preserved exactly. `app/services/agentic.py` shrinks to a thin app-layer config defining the `quick_reply` and `extended` StepPlans, the HotContext subclass, and the mediator-specific gates.

## Required scope

- Pin `agentkit>=0.2.0,<0.3.0` in `pyproject.toml`.
- **Tools**: `tool_schemas.py` already uses Pydantic v2 — convert each tool to `agentkit.tools.ToolRegistration` (mostly rebadge). Add `operation_kind` (`read` / `write` / `meta`) to each. Move dispatch from `app/services/tools/registry.py:call_tool` into the agentkit `Toolkit.dispatch`.
- **StepPlans**: define `quick_reply_plan` and `extended_plan` matching the existing step skeleton (`read → consult → respond → record → schedule`). Each step's `allowed_tools` and iteration cap come from the existing `STEP_ITERATION_CAPS` (line 456).
- **HotContext**: subclass `agentkit.state.HotContext` as `MediatorHotContext`. Lift the build-from-Postgres logic from `agentic.py:1145+` into `MediatorHotContext.build_for(user_id, bot_id)`. Markdown rendering stays the same.
- **Atomic claim**: replace `claim_messages_for_turn` body with `await agentkit.control.claim.claim_rows(conn, 'messages', where=..., limit=N)`.
- **Burst coalescer**: replace `app/main.py:189-234` with `agentkit.control.coalesce.BurstCoalescer`. Per-bot closures stay (glue captures `bot_id`).
- **Spend cap → defer**: Veas's `is_under_cap()` check at `agentic.py:316` becomes `Budget(daily_cap_usd=..., mode='defer')`. The `BudgetDeferred` exception is caught by Veas's existing scheduled-job code path at `agentic.py:877-916` and persists a `scheduled_jobs` row exactly as before.
- **Audit events**: dual-write during shadow mode. Map Veas's current `turn_audit_events` writes to `agentkit.obs.audit.emit(event)`. The Postgres backend writes to the existing `turn_audit_events` table — agentkit v0.2.0's schema must be a strict superset, so no migration needed.
- **LLM router**: configure `agentkit.llm.router.ProviderRouter` with `["deepseek", "anthropic"]` (or just `["anthropic"]` based on `deepseek_enabled_user_names`). DeepSeek failure semantics (2-strike promote) preserved by the router.
- **Mediator gates**: OOB outbound check (`_check_outbound_oob` at `agentic.py:643`) becomes a `Gate.pre_send` callback returning `Withhold` when verdict is "not ok". Crisis-escalation rule (only `escalate_to_partner` when `charge == 'crisis'` OR explicit ask) stays inside the tool handler (a tool-level gate, not a kernel concern).
- **Validation cap**: replace local "2 consecutive validation errors" logic with the agentkit `ToolRegistration.recoverable_error_cap=2`. The kernel emits a `RecoverableCapHit` audit event and aborts the step.
- **Newer-inbound check**: keep this app-local. It's a Veas-specific concern wired as a `Gate.pre_send` callback that returns `Withhold` if a newer message has arrived.

## Cutover protocol

1. Deploy with `VEAS_USE_AGENTKIT=false` and `VEAS_SHADOW_AGENTKIT=true` for 24h. Shadow path dual-writes `bot_turns_shadow` and `turn_audit_events_shadow` tables for diffing without affecting prod queries.
2. Nightly diff job compares per-turn outcomes: same outbound text (modulo whitespace), same tool sequence, same `failure_reason`, cost delta < 5%.
3. Eval suite under `evals/` runs against both paths. Require ≥95% parity (existing baseline) before flag-flip.
4. Roll out one prod bot at a time at 1h intervals via `VEAS_USE_AGENTKIT_BOTS=<comma-list>`.
5. Monitor: spend, latency, escalations, withhold rate, recoverable-cap rate.
6. After all bots stable for 7 days, delete legacy paths.

## Explicit non-goals

- Do not change the mediator-bot prompt, charge taxonomy, or escalation rules. (`mediator-bot-spec.md` is the canonical source.)
- Do not migrate transcription / vision pre-processing. They run pre-agentic and stay there.
- Do not migrate or change Discord pacing (`DiscordPacer`). It stays per-bot.
- Do not change the existing `turn_audit_events` schema, `bot_turns` schema, or any other Veas-prod table beyond the additive `_shadow` tables for cutover.
- Do not couple to `agentkit v0.3.0` features (no Workflow, no Subagent).

## Acceptance criteria

- All 95+ tools register cleanly via `agentkit.tools.Toolkit.merge`. Schema validation passes against ≥10 recorded real tool-call payloads per tool category (read / write / meta).
- Shadow mode: ≥95% turn-level parity (outbound text equivalence + tool sequence) for ≥24h.
- Eval suite: ≥ existing baseline pass rate.
- Atomic claim: load test of 100 concurrent message inserts shows zero double-processing.
- Spend cap test: synthetically exhaust daily cap, verify deferral creates a `scheduled_jobs` row and the turn re-runs after cap reset.
- All bots in prod on `agentkit` for ≥7 days. `_shadow` tables paused. Legacy `_run_agentic` deleted. `agentic.py` shrinks by ≥60%.
- Mediator escalations behave identically: crisis + explicit-ask logic unchanged, audit trail intact.

## Testing notes

- Per-user prompt-cache breakpoints differ between Veas's hand-rolled blocks and agentkit's renderer. Budget 1 day to tune block boundaries to match — measurable cache hit-rate regression is expected and must be closed before cutover.
- DeepSeek adapter shape-shifter is the riskiest port; bring `app/services/deepseek.py` test fixtures with you and assert output parity in agentkit unit tests.
- Encryption (`DATA_ENCRYPTION_KEY` + AES-GCM for sensitive metadata) — verify agentkit's encryption hook produces ciphertext that Veas's existing decryption code can read. Round-trip test required.
- Concurrency: agentkit's `BurstCoalescer` is async-safe; verify under simulated 50 concurrent inbound bursts that per-bot isolation holds (no cross-bot flushes).

## Risks and mitigations

- **Prompt cache regression.** Block boundaries WILL differ. Measure cache hit-rate during shadow mode; if delta > 10%, tune ephemeral mark placement before cutover.
- **DeepSeek behavioural drift.** Veas's shape-shifter has quirks (e.g. tool-use block ordering); preserve them in the port.
- **Mediator behaviour regression.** Crisis logic must not change. Have someone re-read `mediator-bot-spec.md` lines 23–28 and audit the migration against it before cutover.
- **Audit event volume.** agentkit may emit *more* events than Veas's current code (kernel-level events vs app-level only). Ensure `turn_audit_events` retention policies still hold; if needed, throttle low-value events.
