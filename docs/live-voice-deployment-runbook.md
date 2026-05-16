# Live Voice Agent — Deployment Runbook

This branch ships every Sprint 0–5 chunk from `live-voice-agent-briefing.md`.
Below is the smallest sequence to get it running in production.

## 1. Merge

PR: https://github.com/peteromallet/Veas/pull/1 (`live-voice-agent` → `main`).

Railway is bound to `main` for auto-deploy; merging the PR triggers the build.

## 2. Apply migrations (in order)

Connect to the prod Supabase DB and run:

```bash
PGPASSWORD=… psql -h <prod-host> -U postgres -d <prod-db> \
  -f migrations/0042_live_conversations.sql \
  -f migrations/0043_auth_magic_links.sql \
  -f migrations/0044_live_session_latency.sql \
  -f migrations/0045_live_session_spend.sql
```

These are idempotent (CHECK + CREATE INDEX IF NOT EXISTS + DO blocks
that catch `duplicate_object`). Re-running is safe.

Each migration has a matching `.down.sql` next to it; revert in reverse
order if needed.

## 3. Set production env vars in Railway

Required:

| Var | Value |
|---|---|
| `LIVE_VOICE_JWT_SECRET` | strong random — `openssl rand -hex 32` |
| `OPENAI_API_KEY` | real `sk-…` |
| `ANTHROPIC_API_KEY` | real `sk-ant-…` |
| `LIVE_VOICE_WS_AUTH_REQUIRED` | `1` (require magic-link JWT for WS) |

Optional:

| Var | Default | Notes |
|---|---|---|
| `ELEVENLABS_API_KEY` | unset | Required for real Flash TTS; falls back to browser SpeechSynthesis if absent |
| `LIVE_VOICE_WHISPER_MODEL` | `whisper-1` | Try `gpt-4o-mini-transcribe` for lower hallucinations + cost |
| `LIVE_VOICE_WS_RATE_PER_MIN` | `10` | Per-IP WS-open rate cap |
| `LIVE_VOICE_CORS_ORIGINS` | localhost+veas-production | Comma-sep allowlist |
| `LIVE_VOICE_TEST_USER_ID` | unset | Falls back to a stub UUID; should be unset in prod |

## 4. Smoke

```bash
curl -sf https://veas-production.up.railway.app/api/live/healthz | jq .
# Expect: { "ok": true, "checks": { "db.ok": true, "conversations_table.ok": true, "openai_api_key.ok": true } }

curl -sf https://veas-production.up.railway.app/api/live/config | jq .
# Expect: { "auth_mode": "magic_link", "magic_link_enabled": true, "openai_voice_enabled": true, ... }

curl -sf https://veas-production.up.railway.app/api/live/ops/metrics | jq .
# Expect: real latency_ms / spend_usd_today / active_sessions
```

## 5. Browser verification

Open `https://veas-production.up.railway.app/live/` in Chrome/Safari.

Flow: PersonaPicker → SessionCard (steering) → AgendaCard → ConsentGate
("Just me" or partner-present) → LiveScreen (mic permission prompt) →
real Opus agenda items render → speak → real Whisper transcript +
real Haiku reply → "Stop for everyone" → ReviewScreen with 4 sections
+ Save → kept notes land in `mediator.observations`.

## 6. Alarm wiring

`/api/live/ops/metrics` returns the four briefing alarm signals. Wire
to Railway/Datadog with the thresholds it returns under `thresholds`:

- `latency_ms.ear_to_ear.p95 > 2000` for 5 min — alert on
  `>3500ms` with real Anthropic/Whisper baseline.
- `spend_usd_today > 0.8 * daily_cap` — operator-set cap.
- `error_rate_5m > 0.01` for 5 min.
- `ws_disconnect_rate_5m > 0.05` for 5 min.

## Provider selectors (env-driven, no code change)

| Provider | Var | Stub | Real |
|---|---|---|---|
| STT | `LIVE_VOICE_STT_PROVIDER` | `stub` | `whisper` (default if real `OPENAI_API_KEY`) or `openai_realtime` |
| Prep | `LIVE_VOICE_PREP_PROVIDER` | `stub` | `anthropic` (default if real `ANTHROPIC_API_KEY`) |
| Turn | `LIVE_VOICE_TURN_PROVIDER` | `stub` | `anthropic` (default if real `ANTHROPIC_API_KEY`) |
| TTS | `LIVE_VOICE_TTS_PROVIDER` | `stub` | `elevenlabs` (default if real `ELEVENLABS_API_KEY`) |
