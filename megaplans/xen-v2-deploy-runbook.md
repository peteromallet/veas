# Xen v2 — production deploy runbook

> **Trigger:** all of m1–m4 are completed + merged to `main`. User has explicitly
> authorized the full prod deploy ("DEPLOY IT ALL — PUSH EVERYTHING, RUN EVERY
> MIGRATION, GET IT RUNNING"). This is the human gate for the previously-gated
> prod schema migration + multi-type backfill. Execute it; do not re-ask. But the
> two SAFETY PRE-FLIGHTS below are non-negotiable — a wrong migration destroys the
> 1826 live vectors, and a wrong backfill leaks partner-private content in a
> relationship-mediation app. Verify, then proceed without pausing.

## 0. Confirm build complete
- `megaplan chain status --spec megaplans/xen-v2-epic.chain.yaml` → all 4 in `completed`.
- All four PRs merged (m1=#19 ✓; record m2/m3/m4 PR numbers).
- `git checkout main && git pull` → main contains all four milestones.

## 1. SAFETY PRE-FLIGHT A — vector preservation (before ANY prod write)
- Snapshot current prod vector count: `SELECT count(*) FROM mediator.message_embeddings;`
  (v1 baseline = **1826**). Record it.
- Read `migrations/0058_content_embeddings_unified_index.sql`: confirm it GENERALIZES
  `message_embeddings → content_embeddings(source_type,source_id,…)` by **migrating/
  copying** the existing rows (default `source_type='message'`), NOT drop+recreate.
  The 1826 vectors MUST survive. Confirm `0058...down.sql` exists for rollback.

## 2. SAFETY PRE-FLIGHT B — visibility correctness spine (privacy gate)
Inspect the merged m1/m2 code before backfilling these types:
- **OOB**: `out_of_bounds.sensitive_core` is NEVER embedded — OOB stays a hard
  exclusion filter only (`retrieval.py` `_visibility_filters`). Confirm no UNION arm
  embeds it.
- **dyad_shareable memories**: partner may see only `shareable_summary`, never raw
  `content`. Confirm the index either excludes dyad_shareable rows OR post-filters +
  substitutes `shareable_summary` before returning to the partner.
- Each `v_searchable_content` UNION arm replicates its type's hot-context visibility
  predicate (memories / observations / distillations / artifacts). A leak here = a
  partner seeing hidden content. STOP and fix if any arm is permissive.

## 3. Apply migrations to prod (SESSION pooler, port 5432, DIRECT_DATABASE_URL)
- Use session mode (NOT the 6543 transaction pooler) — CREATE INDEX CONCURRENTLY +
  data migration need it. `DIRECT_DATABASE_URL` = session-pooler URL.
- Apply in order: `0057_searchable_messages_render_metadata.sql`, then
  `0058_content_embeddings_unified_index.sql`.
- POST-CHECK: `SELECT count(*) FROM mediator.content_embeddings WHERE source_type='message';`
  must equal the pre-migration 1826. If not → STOP, roll back via `.down.sql`.

## 4. Multi-type backfill (privacy-enforced, rate-limited)
- Run the v2 backfill over the new types (distillations, memories, observations,
  conversation_artifacts) via `DIRECT_DATABASE_URL`, batch 8–64, rate-limited
  (reuse `scripts/backfill_embeddings.py` shape; v2 multi-type variant from m1/m4).
- ENFORCE the §2 filters at embed time. After: verify per-type counts > 0, and
  spot-check that NO `out_of_bounds.sensitive_core` and NO dyad_shareable raw
  `content` got embedded.

## 5. Deploy the app (Railway) and get it running
- `railway up --detach` (deploys local dir). `railway run` injects prod env.
- Required env: `OPENAI_API_KEY`, `DATABASE_URL`, `LIVE_VOICE_AUTH_ENABLED=true`,
  `LIVE_VOICE_JWT_SECRET` (boot guard at `app/main.py` requires auth in prod).
- Confirm boot succeeds (no RuntimeError from the auth guard).

## 6. Verify live
- Health endpoint green.
- Live hybrid search returns **type-labeled** results across messages + the new
  distilled types, ranked per the source weights.
- Visibility holds: a partner query never returns OOB or dyad_shareable raw content.
- Hot context renders the new types (recency-ordered, name-resolved, type labels).

## Rollback
- Migrations: `0058...down.sql`, `0057...down.sql` (reverse order).
- App: redeploy prior Railway image.
- Backfill rows are additive (new source_types) — deleting them is safe and reversible.
