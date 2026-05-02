# Mediator Bot

Implementation for the mediator bot described in
[`mediator-bot-spec.md`](mediator-bot-spec.md): WhatsApp ingestion, the
agentic mediation loop, read/write tools, scheduler, admin debugging views,
spend tracking, staging replay, and operating scripts.

`tool_schemas.py` lives at the repository root by design and must not be moved,
copied, renamed, or deleted. Packaging exposes it as the top-level
`tool_schemas` module.

## Local Setup

Use Python 3.11 or newer.

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

The editable install needs access to PyPI, or an equivalent pre-seeded package
index/wheel cache, for build, runtime, and dev dependencies. In a
network-restricted sandbox, treat dependency resolution failures as an
environment prerequisite and run source tests only after dependencies are
already present.

Populate `.env` with real values before running the app against a real
database. Do not commit `.env`.

## Environment Checklist

Mirror these variables in local `.env` and Railway environment variables:

- `ENV_NAME`
- `DATABASE_URL`
- `DATABASE_SCHEMA`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `GROQ_API_KEY`
- `WHATSAPP_VERIFY_TOKEN`
- `MESSAGING_PROVIDER`
- `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` when using `MESSAGING_PROVIDER=meta`
- `WHATSAPP_APP_SECRET` when using the Meta webhook
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_WHATSAPP_FROM` when using `MESSAGING_PROVIDER=twilio`
- `TWILIO_WEBHOOK_URL` when Twilio signature validation must use an externally visible URL
- `DISCORD_BOT_TOKEN` when using `MESSAGING_PROVIDER=discord`
- `DISCORD_PARTNER_USER_ID_A` and `DISCORD_PARTNER_USER_ID_B` when using `MESSAGING_PROVIDER=discord`
- `DISCORD_PARTNER_NAME_A` and `DISCORD_PARTNER_NAME_B` when using `MESSAGING_PROVIDER=discord`
- `DISCORD_PACING_*` variables when using Discord conversation pacing
- `ADMIN_PASSWORD`
- `PARTNER_PHONE_A`
- `PARTNER_PHONE_B`
- `TEXT_LLM_DAILY_CAP_USD`
- `VISION_DAILY_CAP_USD`
- `TRANSCRIPTION_DAILY_CAP_USD`
- `SENTRY_DSN`
- `LOG_DESTINATION`
- `SCHEDULER_ENABLED`
- `SCHEDULER_POLL_INTERVAL_S`
- `SCHEDULER_BATCH_SIZE`
- `WEEKLY_SUMMARY_DEFAULT_DAY`
- `WEEKLY_SUMMARY_DEFAULT_TIME`
- `HEARTBEAT_INTERVAL_HOURS`
- `SUPABASE_STORAGE_BUCKET`
- `MEDIA_FETCH_TIMEOUT_S`
- `DATA_ENCRYPTION_KEY`

Optional values may be left blank when unused. Secret values must only live in
local `.env`, Railway variables, or the relevant provider control plane.
Set `DATA_ENCRYPTION_KEY` in production so sensitive columns are written with
AES-GCM ciphertext.

## Apply Migrations

Apply the forward migrations with `psql`:

```sh
for file in migrations/0001_init.sql migrations/0002_plan2.sql migrations/0003_plan4_oob_reviews.sql migrations/0004_plan5_scheduled_jobs.sql migrations/0005_plan6_ops.sql migrations/0006_plan7_eval_results.sql migrations/0007_security_hardening.sql migrations/0008_discord_pacing.sql migrations/0009_incremental_agent_sending.sql migrations/0010_turn_prompt_encryption.sql migrations/0011_lock_public_schema.sql migrations/0012_cross_thread_sharing.sql migrations/0013_bridge_candidates.sql; do
  psql "$DATABASE_URL" -f "$file"
done
```

The migrations use guarded DDL where practical so accidental re-runs are safer,
but they are forward migrations rather than a general migration manager.

It creates 11 spec tables plus the operational `llm_spend_log` and
`pacing_events` tables. It enables only `pgcrypto`; it must not create pgvector,
embedding columns, or vector storage.

After applying the migration to Supabase or a scratch Postgres database, verify
the foundation schema before deploying:

```sh
psql "$DATABASE_URL"
\dt
\d+ messages
\d+ observations
\di
\d users
```

The table list should show 13 tables: the 11 spec tables plus `llm_spend_log`
and `pacing_events`. Confirm the spec partial indexes and GIN array indexes are
present, and that `users` reports row security enabled. Then verify anon access
is denied for application tables and the service role can still read through
Supabase's RLS bypass.

## Teardown

For local development only:

```sh
psql "$DATABASE_URL" -f migrations/teardown.sql
```

Do not run `migrations/teardown.sql` against shared, staging, production, or
Supabase project databases.

## Run Server

```sh
uvicorn app.main:app --reload
```

For Railway, the production command is:

```sh
uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
```

## Discord Transport

For local Discord testing, create a Discord application, add a bot, copy the bot
token, and enable the Message Content intent for that bot. Invite the bot to a
small shared server with both partners so the bot can receive/send DMs.

Set:

```env
MESSAGING_PROVIDER=discord
DISCORD_BOT_TOKEN=<bot-token>
DISCORD_PARTNER_USER_ID_A=<first-discord-user-id>
DISCORD_PARTNER_USER_ID_B=<second-discord-user-id>
DISCORD_PARTNER_NAME_A=Partner A
DISCORD_PARTNER_NAME_B=Partner B
```

In Discord mode, inbound messages are ignored unless the author ID matches one
of those two Discord user IDs.

Discord conversation pacing is documented in
[`docs/discord-pacing.md`](docs/discord-pacing.md), including typing behavior,
pre-turn wait/react/silence/answer decisions, source handling, observability,
global tuning variables, and per-user preference keys.

## Run Tests

```sh
pytest -q
```

DB-backed tests skip when `TEST_DATABASE_URL` is unset. Set it to a scratch
database URL if you want spend and live health checks to run against Postgres.

## Admin

Admin pages are read-only and protected with HTTP Basic Auth. Use username
`admin` and `ADMIN_PASSWORD`.

- `/admin`: index of operator views.
- `/admin/turns`: recent bot turns; click a turn for prompt snapshot,
  reasoning, triggering inbound content, final outbound content, and tool calls.
- `/admin/messages`: recent messages, charge, processing state, and edit history.
- `/admin/themes`, `/admin/memories`, `/admin/watch-items`,
  `/admin/observations`, `/admin/oob`, `/admin/scheduled-jobs`: current state
  tables for debugging without writing SQL.
- `/admin/spend`: daily provider spend, configured caps, percent used, and
  whether the 80 percent warning has fired.
- `/admin/escalations`: turns that called `escalate_to_partner`.
- `/admin/feedback`: conversational and reaction feedback.
- `/admin/audit`: human-readable audit surface for "why did you tell her that?"

Rotate the admin password by changing `ADMIN_PASSWORD` in the deployment
environment and redeploying/restarting the service.

## Health And Monitoring

`/health` is the cheap liveness check: it verifies the DB responds to `SELECT 1`.
`/health/deep` also checks Anthropic API reachability with a short timeout and a
60 second in-process cache. Use `/health` for high-frequency uptime checks and
`/health/deep` for lower-frequency smoke checks after deploys.

External alerting is intentionally outside the repo. Configure:

- An uptime monitor for `/health` every 1-5 minutes.
- A deploy smoke check for `/health/deep`.
- A cron-ping alert tied to the scheduled heartbeat log/job cadence.
- Log alerting for `LLM spend ... crossed 80% of daily warning threshold`.

## Staging Replay

Replay historical inbound rows without sending WhatsApp messages or writing app
tables:

```sh
python -m app.staging replay --prompt-version candidate-v1 --since 2026-04-01 --user "$USER_UUID"
```

The command prints JSON lines with prompt previews, candidate outbound text, and
`would_write` records. In v1, `--prompt-version` tags the run while using the
current prompt renderer.

## Backups

Supabase automated backups remain the primary backup. Quarterly, run a fresh
operator dump and record its checksum:

```sh
python scripts/backup_dump.py --out-dir backups
```

Practice restore quarterly into a disposable database:

```sh
pg_restore --dbname "$DISPOSABLE_DATABASE_URL" backups/<dump-file>.dump
sha256sum -c backups/<dump-file>.dump.sha256
```

Never run restore practice against staging or production.

## Launch Notes

The engineering-authored `WELCOME_MESSAGE` in `app/services/inbound.py` is
intentionally short and conversational, but it must be approved or edited before
launch. The assistant name remains configurable through `ASSISTANT_NAME`; the
co-designer-vs-recipient product decision is out of technical scope.

To send a one-off operator-specified welcome to an allowlisted user:

```sh
uv run python scripts/send_welcome.py --recipient 15555550100 --name Maya --message "Hi Maya..."
```

The command exits non-zero unless the outbound row reaches `processed` with a
provider message id. Only a successful provider send marks onboarding as
`welcomed`.

Weekly-summary timing is configurable with `WEEKLY_SUMMARY_DEFAULT_DAY`,
`WEEKLY_SUMMARY_DEFAULT_TIME`, and per-user schedule fields.

## Deploy To Railway

1. Create a Railway service from this repository.
2. Keep the Nixpacks builder from `railway.json`.
3. Confirm the start command is
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
4. Add every variable from the environment checklist above.
5. Apply all forward migrations through `migrations/0013_bridge_candidates.sql` to
   staging/production before deploying this version.
6. Deploy and confirm Railway reports `/health` as healthy.
7. Smoke test `/admin`, `/admin/turns`, `/admin/spend`, and `/health/deep` with
   real credentials.
8. Configure the external monitors described above.

See [`docs/ops.md`](docs/ops.md) for uptime monitoring, spend-counter location,
rotation expectations, and [`docs/scheduler.md`](docs/scheduler.md) for
scheduler runtime ownership, heartbeat, pause/resume, and weekly-summary
operations.
