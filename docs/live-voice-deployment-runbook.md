# Live Voice Agent — Deployment Runbook

This branch ships every Sprint 0–5 chunk from `live-voice-agent-briefing.md`.
Below is the smallest sequence to get it running in production.

## 1. Merge

PR: https://github.com/peteromallet/Veas/pull/1 (`live-voice-agent` → `main`).

Railway is bound to `main` for auto-deploy; merging the PR triggers the build.

## 2. Apply migrations (in order)

Use the idempotent runner:

```bash
DATABASE_URL=postgres://… uv run python scripts/apply_live_voice_migrations.py
# Output:
#   ✓ 0042_live_conversations.sql: …
#   ✓ 0043_auth_magic_links.sql: …
#   ✓ 0044_live_session_latency.sql: …
#   ✓ 0045_live_session_spend.sql: …
#   Done.
```

The script creates `mediator.applied_migrations` on first run and
records every successful apply. Re-runs are no-ops. If 0042 was
manually applied before the tracker existed (the common case for
in-flight deploys), the script detects "already exists" errors and
records them in the tracker without surprise.

For a dry-run (just list what would land), add `--dry-run`.

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

## 7. Operator debug endpoint

`GET /api/live/ops/sessions/{session_id}/debug` returns full session
introspection for operators.  No auth changes — internal endpoint
under the `/api/live/ops/` prefix.

### Response shape

```jsonc
{
  "session_id": "uuid",
  "conversation": {      // metadata with canonicalized status
    "id", "status", "bot_id", "user_id", "partner_user_id",
    "mode", "steering_text", "prep_summary", "current_item_id",
    "started_at", "ended_at", "created_at",
    "session_fields", "topic_id", "spend_usd_cents"
  },
  "bot_turns": [         // agentic turn rows by conversation_id
    { "id", "kind", "turn_id", "model", "provider",
      "failure_reason", "completed", "completed_at",
      "started_at", "tool_call_count", "duration_ms" }
  ],
  "transcript_turns": [  // live call user/bot utterances (separate key)
    { "id", "speaker_label", "speaker_role", "text",
      "ts", "asr_confidence", "active_item_id", "was_routing_input" }
  ],
  "artifacts": {         // grouped by artifact_type, ordered by revision_number DESC
    "live_prep_brief": [
      { "id", "artifact_type", "revision_number", "payload",
        "payload_version", "created_by_turn_id",
        "deleted_at", "created_at", "expires_at",
        "current": bool,  // highest non-deleted revision
        "deleted": bool,
        "links": [...]    // per-artifact provenance links
      }
    ]
  },
  "provenance": {
    "links": [           // all artifact_links for this session
      { "id", "artifact_id", "target_table", "target_id",
        "relation", "evidence", "deleted_at", "created_at" }
    ],
    "durable_write_counts": {
      "observations": 3,
      "distillations": 2,
      "themes": 1
      // counts links to durable tables, excluding conversation-scoped tables
      // (conversations, conversation_items, transcript_turns, conversation_notes)
    }
  },
  "failure_classes": {
    "session": {         // from conversation.session_fields
      "prep_error": "...",
      "debrief_error": "..."
    },
    "bot_turns": [       // turns with failure_reason + _classify_failure()
      { "turn_id", "kind", "failure_reason", "failure_class" }
    ],
    "non_chat": [        // live_prep / live_debrief turns specifically
      { "turn_id", "kind", "failure_reason", "failure_class" }
                      // or { "turn_id", "kind", "outcome": "success" }
    ]
  }
}
```

### Key questions it answers

| Operator question | Look at |
|------------------|---------|
| "What _actually_ happened in this session?" | `conversation.status` (canonicalized), `transcript_turns` (the real conversation), `session_fields` |
| "Why did prep fail?" | `failure_classes.session.prep_error`, then `bot_turns` filtered to `kind=live_prep` for the specific `failure_reason` |
| "Why did debrief fail or take too long?" | `failure_classes.session.debrief_error`, `bot_turns` filtered to `kind=live_debrief`, `provenance.durable_write_counts` (did any durable writes land?) |
| "What did the debrief actually write?" | `provenance.durable_write_counts` per target table, `provenance.links` for each target_id |
| "Which model version / provider was used?" | `bot_turns[].model` and `bot_turns[].provider` (extracted from `model_version` prefix) |
| "How many retry attempts?" | Count `bot_turns` of each `kind`; also `session_fields.retry_count` |
| "Is the review data from artifact or fallback?" | Check `artifacts.live_debrief` — if absent or all deleted, the review used deterministic synthesis |
| "What is the spend on this session?" | `conversation.spend_usd_cents` + per-turn `duration_ms` |

## 8. Operator triage

### 8.1 Prep failure (`prep_failed`)

**Symptoms:** User lands on prep loading spinner indefinitely, then
sees a failure reason + Retry button.

**Triage steps:**

1. Hit the debug endpoint: `GET /api/live/ops/sessions/{session_id}/debug`
2. Check `failure_classes.session.prep_error` for the high-level reason.
3. Check `failure_classes.bot_turns` for the `live_prep` turn's
   `failure_reason`.  Common reasons:
   - `live_prep_text_no_submit` — model produced text but never called
     `submit_live_brief`.  LLM deviation; retry usually resolves it.
   - `live_prep_submit_missing` — model exhausted tool iterations
     without submitting.  May indicate the steering text is too vague.
   - `iteration_cap_skipped` — model hit `max_tool_iterations` without
     completing.  Increase `LIVE_VOICE_PREP_MAX_TOOL_ITERATIONS` (default 12).
   - `provider_timeout` / `llm_timeout` — the LLM call timed out.
     Check provider health.
4. Check `bot_turns` for the model/provider used — confirm it matches
   the configured `LIVE_VOICE_PREP_PROVIDER`.
5. Look at `session_fields.retry_count` — if the user has retried ≥3
   times, the steering text may need adjustment or the tool policy may
   be too restrictive.

**Actions:**
- If `live_prep_text_no_submit` or `live_prep_submit_missing`: retry is
  safe (the Retry button calls the same session).  Usually resolves on
  first retry.
- If persistent: widen `max_tool_iterations`, check that the bot spec's
  `tool_allowlist` includes the read tools needed to ground the agenda.
- If provider errors: check API key validity, rate limits, provider
  status pages.

### 8.2 Debrief failure (`debrief_failed`)

**Symptoms:** After `/end`, the user sees a debrief waiting spinner,
then a failure reason with Retry Debrief button.  The transcript
remains accessible.

**Triage steps:**

1. Hit the debug endpoint.
2. Check `failure_classes.session.debrief_error` for the high-level reason.
3. Check `failure_classes.bot_turns` for the `live_debrief` turn's
   `failure_reason`.  Common reasons:
   - `live_debrief_submit_missing` — model ran tools but never called
     `submit_live_debrief`.  LLM deviation; retry usually resolves.
   - `live_debrief_text_no_submit` — model produced text without
     submitting structured output.
   - `provider_timeout` / `llm_timeout` — LLM timeout.
4. Check `provenance.durable_write_counts` — were any durable writes
   attempted before the failure?  If `durable_write_counts` is non-zero,
   partial writes may have landed; the user should review them even if
   the debrief "failed."
5. Check `artifacts.live_debrief` — is there a provisional artifact?
   If the debrief crashed after creating the artifact but before
   finalizing, a stale provisional may exist.

**Actions:**
- If `live_debrief_submit_missing`: retry is safe (Retry Debrief button
  calls the same session).  Usually resolves on first retry.
- If partial writes landed (`durable_write_counts > 0`): the review
  screen may show partial data — the user should verify before saving.
- If provider errors: check API key validity, rate limits, provider
  status pages.
- If persistent failures: check that the debrief tool policy includes
  the read tools needed to inspect the transcript before writing.

### 8.3 Long debrief latency

**Symptoms:** Debrief spinner visible for >60s after `/end`.  The user
is not blocked (transcript stays visible, UI is responsive), but the
review screen is delayed.

**Triage steps:**

1. Hit the debug endpoint and check `bot_turns` — is the `live_debrief`
   turn `completed` or still in-flight?  If `completed_at` is null and
   `started_at` is recent, the job is still running.
2. Check `session_fields` for any error indicators.
3. Check provider latency: is the LLM provider experiencing high
   latency?  Check the provider's status page.
4. Check `tool_call_count` on the in-flight turn — a high count
   (>20 tools) suggests the model is iterating excessively through
   read tools before calling `submit_live_debrief`.

**Actions:**
- If still running: wait for it to finish (the system will time out after
  `LIVE_VOICE_DEBRIEF_TIMEOUT_S`, default 120s).
- If the turn is stuck: the process may have crashed.  The operator can
  manually set the conversation to `debrief_failed` and the user can
  retry.
- No immediate data loss: the transcript and artifacts are already
  persisted.

### 8.4 Missing submit (no `submit_live_debrief`)

**Symptoms:** The debrief ran tools but never produced a final review
payload.  The debug endpoint's `artifacts.live_debrief` is empty or
shows only a provisional artifact that was never finalized.

**Triage steps:**

1. Check `failure_classes.bot_turns` for the `live_debrief` turn.
2. Verify `failure_reason` contains `submit_missing`.
3. Check `provenance.durable_write_counts` — if >0, the model did
   attempt some writes before failing to submit.

**Actions:**
- Retry is the primary remediation.  The retry creates a new bot turn
  and new artifact revision, so no stale state is carried forward.
- If writes landed but submit was missing, the review screen falls
  back to deterministic synthesis — the user can still review and save.

## 9. Structured log reference

All live lifecycle events emit structured logs via helpers in
`app/services/live/metrics.py`.  Operators can search for these
patterns:

| Log pattern | Fields | When |
|------------|--------|------|
| `live_prep: start` | `conversation_id`, `bot_id`, `user_id`, `status_transition`, `retry_count` | Prep job begins |
| `live_prep: success` | `conversation_id`, `bot_id`, `duration`, `tool_count`, `status_transition`, `artifact_revision` | Prep completes successfully |
| `live_prep: failure` | `conversation_id`, `bot_id`, `duration`, `tool_count`, `failure_reason`, `failure_class`, `status_transition` | Prep fails |
| `live_prep_retry: succeeded` | `conversation_id`, `bot_id`, `retry_number`, `duration`, `tool_count` | Retry prep succeeds |
| `live_prep_retry: failed` | `conversation_id`, `bot_id`, `retry_number`, `duration`, `tool_count`, `failure_reason` | Retry prep fails again |
| `live_debrief: start` | `conversation_id`, `bot_id` | Debrief job begins |
| `live_debrief: success` | `conversation_id`, `bot_id`, `duration`, `tool_count`, `durable_write_count`, `status_transition`, `artifact_revision` | Debrief completes |
| `live_debrief: failure` | `conversation_id`, `bot_id`, `duration`, `tool_count`, `failure_reason`, `submit_missing`, `failure_class`, `durable_write_count`, `status_transition` | Debrief fails |
| `live_debrief_retry: succeeded` | `conversation_id`, `bot_id`, `retry_number`, `duration`, `tool_count` | Retry debrief succeeds |
| `live_debrief_retry: failed` | `conversation_id`, `bot_id`, `retry_number`, `duration`, `tool_count`, `failure_reason`, `durable_write_count` | Retry debrief fails again |
