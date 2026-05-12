# Coach Bot — 3-Day Production Soak Plan

**Status:** Do NOT auto-deploy. Manual deploy + offline monitoring per §16.6.

---

## Overview

The solo coach bot (`bot_id="coach"`, primary topic `"career"`) goes live
for exactly one consenting user. This document describes what to watch, what
to log, and how to roll back if anything goes wrong.

---

## Dashboards / Logs To Watch

1. **Turn events table** (`turn_events`):
   - `turn.opened` with `metadata->>'bot_id' = 'coach'` — confirm coach is
     receiving inbound turns.
   - `tool.rejected` — any tool rejection from coach. Expected only for
     bridge/escalate tools blocked at the registry boundary; unexpected
     rejections on valid tools are red flags.
   - `tool.requested` + `tool.completed` — verify write tools (add_memory,
     etc.) are landing in the `career` topic.

2. **Bot turns table** (`bot_turns`):
   - Confirm `topic_id` matches the career topic UUID, NOT the relationship
     topic UUID (scope-guard violation). This is the #1 stop condition.

3. **Messages**:
   - Outbound messages from `bot_id='coach'` — confirm content is career-
     scoped. No bridge mentions, no "your partner" language.

4. **Scope guard violations**:
   - The existing scope guard (S4) runs on every write. Any violation in
     the coach path trips a `tool.rejected` event and is a STOP condition.

5. **Error logs** (application logs):
   - `partner_of` ValueError warnings from `inbound.py:130` are benign if
     they appear during pause/resume commands — the try/except catch is the
     safety net.
   - Any unhandled exception in the coach turn path is a red flag.

---

## Relevant Turn Event Types

| Event Type | Severity | Meaning |
|---|---|---|
| `turn.opened` (bot_id=coach) | Info | Coach turn started |
| `tool.rejected` (reason=step_not_allowed) | Warning | Expected for bridge tools at boundary |
| `tool.rejected` (reason=unknown_tool) | Warning | Expected for bridge tools at boundary |
| `tool.rejected` (reason=scope_guard) | **Critical** | Write crossed topic boundary — STOP |
| `tool.rejected` (other) | Warning | Investigate |
| `turn.completed` | Info | Turn finished cleanly |

---

## Rollback Procedure

If any stop condition trips during the soak:

1. **Comment coach out of BOT_SPECS** in `app/bots/registry.py`:
   ```python
   # coach = build_coach_spec()
   # BOT_SPECS[coach.bot_id] = coach
   ```
   (Or set `STAGING=0` if the staging guard was the only registration path.)

2. **Deploy** the commented-out config. This prevents new coach turns from
   starting.

3. **If prod-seed migration landed** in a future sprint (T12/U2):
   - Write and run a revert migration that removes the coach `bots` row,
     `topics` row, and `bot_binding` row (if no other data depends on them).
   - The bot_binding row must be re-evaluated if the consenting user has
     already sent messages.

4. **Notify the consenting user** that coach is paused.

---

## Known Limitations (Soak Period)

- **resolve_bot routing for coach's transport** (DEBT-044/scope-2):
  Migration 0031 deferred channel seeding. Live verification that
  `resolve_bot` correctly routes inbound from the coach's transport channel
  is deferred to the soak. The e2e test proves the code path via fixtures;
  live routing depends on the transport being provisioned out-of-band.

- **Single-user dyad relationship**: In current prod, the mediator serves
  a 2-person dyad. The coach bot's "peek" policy renders as an explicit
  empty section because there are no other topics for this user. This is
  correct behavior.

- **No cross-topic writes**: Coach cannot write to the relationship topic
  (S6 scope). If the model attempts this, the scope guard rejects it.

---

## Success Criteria For Soak Completion

After 3 days of monitoring:
- Zero scope-guard violations
- Zero unexpected tool rejections
- All coach turns have correct `topic_id` (career, not relationship)
- Consenting user reports coach is usable and appropriate
- No mediator test regressions