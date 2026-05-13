# Sprint 5 Feedback / Deferred Items

## T12 / U2: Prod Promotion Migration (DEFERRED)

**Status:** Blocked on operator-held facts.

**Blockers:**
1. **S4 staging seed verification** — migration 0031_coach_staging_seed.sql must be
   confirmed to have run cleanly in the staging database before we can promote.
2. **Consenting prod user UUID** — the production user who has consented to receive
   coach bot access must be identified. This UUID is PII and operator-held.

**What the migration would do (once unblocked):**
Write `migrations/0032_coach_prod_seed.sql` (or 0033 if T3 had introduced a
precursor) that:
- Inserts `bots` row: `('coach', 'Coach')`
- Inserts `topics` row: `('career', 'Career', 'Solo career/work coaching topic', 'solo')`
- Inserts `bot_binding` row for the consenting prod user (bot_id='coach',
  user_id=<consenting UUID>, participants_shape='solo')
- All wrapped in the same `IF current_database() NOT LIKE '%prod%' THEN RAISE NOTICE ...; RETURN; END IF;` in-SQL guard pattern as 0031.

**Do NOT write this migration in S5.** The executor must not execute any prod
write. This TODO is a placeholder for the future operator unblock (U2).

## T16: Preflight (Staging Seed Verification + Consenting User UUID)

**Status:** DEFERRED per user note. Operator-held facts.

See T12 above. Code-only deliverables (work items 1–8) proceed without T16.
TODO comments in `app/bots/coach.py` reference this deferral.