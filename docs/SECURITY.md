# Security & Privacy

This system holds the most personal conversations of two people, including
explicit out-of-bounds (OOB) entries marked sensitive. The threats we take
seriously and the operational hardening that addresses them are documented
here.

## Threat model (one page)

**What this system protects against**

- *Backup or DB-snapshot leakage.* The highest-sensitivity content
  fields (`out_of_bounds.sensitive_core`, `messages.content`,
  `memories.content`, `observations.content`, `bot_turns.prompt_snapshot`,
  `bot_turns.reasoning`) are
  written with AES-GCM ciphertext into `*_encrypted` columns when
  `DATA_ENCRYPTION_KEY` is configured. A snapshot leaked without the key is
  unreadable for those columns.
- *Postgres role escalation.* Every RLS-protected table has
  `FORCE ROW LEVEL SECURITY` set, so even the table-owner role goes through
  the policy layer. Service-role connections still work because Supabase's
  `service_role` has `BYPASSRLS`.
- *Anon REST exposure.* Every privacy-relevant table has an explicit
  `deny_anon_*` policy. If the anon key were leaked, no rows are reachable.
- *Legacy public-schema exposure.* The app data lives in the `mediator`
  schema, not the Supabase `public` schema. Migration
  `0011_lock_public_schema.sql` revokes `anon`/`authenticated` grants on
  legacy `public` tables, enables + forces RLS, and adds deny-all policies so
  old tables such as `public.conversations` are not reachable through
  PostgREST.
- *Single-key compromise.* The encryption key is held only in the
  application environment (Railway), not the database. A snapshot of the
  database alone cannot decrypt the protected columns.

**What this system does NOT mitigate**

- A compromised production host can decrypt: the key is in env so a process
  with shell access to the running container can read it.
- A malicious operator with both DB and Railway access can decrypt
  everything. There is no per-user key escrow.
- Plaintext columns still exist alongside the encrypted columns until the
  operator runs the follow-up migration to drop them. Until that is done,
  a snapshot still contains plaintext for legacy rows.
- The two existing partner transport identities are trusted; takeover of a
  partner's WhatsApp account, Discord account, or configured transport identity
  compromises that partner's data.
- LLM provider logs (Anthropic, OpenAI, Groq) are out of scope; we rely on
  their data-handling policies and short-retention settings.

## Operator checklist (deploy-time)

- [ ] Enable 2FA on the Supabase organization and on the Railway account.
- [ ] Configure an IP allowlist on the Supabase Postgres connection where
      possible; restrict to the Railway egress IPs and the operator's
      maintenance IP.
- [ ] Rotate `SUPABASE_SERVICE_ROLE_KEY` quarterly and after any suspected
      exposure. Keep the previous value for a 24-hour overlap.
- [ ] Verify backups are encrypted at rest. Supabase backups are AES-256
      encrypted by default; document this in the runbook and confirm by
      checking the bucket policy at least monthly.
- [ ] Set log retention to 14 days or less in Railway and in any third-party
      log destinations. Sentry: 30 days at most, scrub PII via the
      `before_send` hook. Do not log message content, OOB text, observations,
      or bot reasoning.
- [ ] Set `DATA_ENCRYPTION_KEY` in Railway env (and in any backup-restore
      environment) to a base64-encoded 32-byte secret. Generate with
      `python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"`.
- [ ] Apply migration `0007_security_hardening.sql` (RLS + FORCE RLS +
      `*_encrypted` columns + pgcrypto + idempotent backfill notice).
- [ ] Apply migrations through `0013_bridge_candidates.sql` so legacy
      `public` tables are not anonymously accessible.
- [ ] Run `python -m scripts.backfill_encryption` once with the key set.
      Re-run is safe; the script only encrypts rows where the encrypted
      column is `NULL`.
- [ ] Verify ciphertext is present:
      ```sql
      SELECT
        (SELECT count(*) FROM out_of_bounds WHERE sensitive_core IS NOT NULL AND sensitive_core_encrypted IS NULL) AS oob_pending,
        (SELECT count(*) FROM messages       WHERE content IS NOT NULL        AND content_encrypted        IS NULL) AS msg_pending,
        (SELECT count(*) FROM memories       WHERE content IS NOT NULL        AND content_encrypted        IS NULL) AS mem_pending,
        (SELECT count(*) FROM observations   WHERE content IS NOT NULL        AND content_encrypted        IS NULL) AS obs_pending,
        (SELECT count(*) FROM bot_turns      WHERE prompt_snapshot IS NOT NULL AND prompt_snapshot_encrypted IS NULL) AS prompt_pending,
        (SELECT count(*) FROM bot_turns      WHERE reasoning IS NOT NULL      AND reasoning_encrypted      IS NULL) AS turn_pending;
      ```
      Every column should be 0.
- [ ] Schedule a follow-up migration to `DROP COLUMN sensitive_core` (etc.)
      once you are confident the encrypted columns are populated and the
      application is reading them. Do this only after one full successful
      backup containing only ciphertext.
- [ ] Confirm the Supabase Storage bucket `mediator-media` is **private**
      (public = `false`) and that signed-URL generation is the only way to
      retrieve media. The bucket is operator-created in the Supabase
      dashboard; the app does not auto-provision it.

## Key rotation (manual, follow-up work)

Rotation is intentionally not automated in this pass. To rotate
`DATA_ENCRYPTION_KEY`:

1. Provision the new key alongside the old one (`DATA_ENCRYPTION_KEY_NEXT`).
2. Stop writes briefly or accept eventual consistency.
3. Run a re-encryption job: `SELECT id, *_encrypted FROM <table>`, decrypt
   with the old key in Python, encrypt with the new key, `UPDATE` the row.
4. Promote the new key (set `DATA_ENCRYPTION_KEY` to its value) and remove
   the old key from the environment.

Until the helper supports a dual-key read path, rotations require a
brief write window or a per-row migration job. This is documented as a
follow-up — do not attempt rotation without first adding read-time
fallback to the previous key.

## Storage bucket policy

The `mediator-media` bucket holds raw voice and image binaries referenced by
`messages.media_url`. Per the spec it must be private (signed URL only). The
application code (`app/services/storage.py`) only uploads to whatever bucket
the operator provisions; it does not create the bucket. Confirm in the
Supabase dashboard:

- Bucket > `mediator-media` > Settings > Public bucket = **off**
- RLS policies on `storage.objects` for this bucket = anon DENY, service-role
  ALLOW
- Generate URLs only via `createSignedUrl` with a short TTL (≤ 5 minutes for
  inline display, ≤ 1 hour for transcription/vision pipelines).

## What's logged

- Webhook signature failures (no body content).
- Charge classifications (label only, no message text).
- Tool-call invocations (arguments are recorded in `tool_calls.arguments` —
  these include user-authored content and are themselves under RLS; this is
  acceptable because tool_calls is a service-role-only table and the rows are
  reachable only with the service-role key).
- LLM spend totals.

The runtime does **not** log message content, OOB text, observations, or
bot reasoning to stdout / Sentry. Auditing should be done through the
admin UI, which is service-role-gated.
