# Scripts

Post-migration operational scripts for the Veas codebase.

## seed_channels.py

Seeds the `channels` table with rows for each configured transport.

- **Idempotent:** safe to run multiple times (`ON CONFLICT ... DO NOTHING`).
- **Credentials-optional:** each transport block independently checks its env var;
  if absent, the transport is skipped with an INFO log — no error.
- **Required env vars:**
  - `DISCORD_BOT_TOKEN` — Discord bot token (user ID derived from token prefix).
  - `DISCORD_BOT_USER_ID` — optional; if unset, derived from the token.
- **Optional env vars:**
  - `WHATSAPP_PHONE_NUMBER_ID` — WhatsApp business phone number ID. Skipped if absent.
- **Database connection:** reads `DATABASE_URL` or falls back to `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE`.

### Usage

```bash
python scripts/seed_channels.py
```

### Expected output

- `INFO` — seeded or already-exists for each transport with credentials.
- `INFO` — skipped for each transport without credentials.
- `WARNING` — if no channels were seeded at all.