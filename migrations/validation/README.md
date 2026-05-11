# Sprint 1 — Validation Queries

## Run order

1. Apply migrations 0020 → 0021 → 0022 → 0023 → 0024 (in order).
2. Run `scripts/seed_channels.py` — seeds channels for Discord (required) and WhatsApp (optional).
3. Run `scripts/backfill_artifact_topics.py` — populates `artifact_topics` for all existing artifact rows.
4. Run `0024_artifact_topics_counts.sql` — validates the backfill completed correctly.
5. Run `s1_checkpoint.sql` — mid-sprint checkpoint gates.

## Gate queries

### OOB zero-result gate

```sql
SELECT count(*) FROM out_of_bounds WHERE NOT EXISTS (
    SELECT 1 FROM artifact_topics
    WHERE artifact_table = 'out_of_bounds'
      AND artifact_id = out_of_bounds.id
);
```

Must return **0**. Every existing `out_of_bounds` row is classified as relationship-topic-scoped per the locked §16.8 decision. "No `artifact_topics` rows = global OOB" is a future-only mode; no current rows use it.

### Identity gate

```sql
SELECT count(*) FROM user_identities WHERE transport = 'legacy';
-- must equal:
SELECT count(*) FROM users WHERE phone IS NOT NULL;
```

### Per-table count parity

Each artifact table must have exactly one corresponding row in `artifact_topics`:

```sql
SELECT count(*) FROM artifact_topics WHERE artifact_table = '<table>';
-- must equal:
SELECT count(*) FROM <table>;
```

## messages.bot_id IS NULL divergence

S1 adds the `messages.bot_id` column as nullable in migration 0023. This column is **NOT** backfilled in S1 — all existing rows correctly remain NULL.

- **S1**: nonzero `messages.bot_id IS NULL` count is **expected and informational-only**.
- **S2a**: insert sites begin writing `bot_id` on new messages. The gate becomes meaningful then.

Do NOT attempt to backfill `messages.bot_id` in S1.

## Stop-if-mediator-turn-fails rule

After all migrations are applied to staging:

1. Run a manual mediator turn against the migrated staging DB with **NO code changes deployed**.
2. If the turn fails, the migration isn't truly additive — **stop and find the FK or trigger that broke**.

Common failure modes:
- A new NOT NULL constraint was accidentally introduced (audited by CP5).
- An FK constraint blocks writes to a table the mediator touches.
- A trigger on a new table fires during mediator's normal write path.

If the turn succeeds, proceed to frozen-fixture rendering equality: freeze `now()`, render hot context pre-migration, apply migrations, re-render, and assert byte-equal output.