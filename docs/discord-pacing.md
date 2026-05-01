# Discord Pacing

Discord pacing makes Discord DMs feel less like a webhook and more like an
attentive conversation. It is only active when `MESSAGING_PROVIDER=discord` and
`DISCORD_PACING_ENABLED=true`; other providers keep the legacy coalescer and
outbound behavior.

## Runtime Flow

Inbound Discord messages still persist through the normal inbound path, then
enter `BurstCoalescer` with source metadata. When pacing is enabled, the
coalescer asks `DiscordPacer` for one of four pre-turn actions:

| Action | Behavior |
| --- | --- |
| `wait` | Delay and reschedule the burst so nearby lines and active typing can settle. |
| `react` | Add one sparse allowlisted reaction and mark the messages processed without a full agentic turn. |
| `silence` | Mark the messages processed without replying when the burst is clearly conversationally closed. |
| `answer` | Run the normal agentic turn with compact pacing metadata included in the trigger context. |

Deterministic gates always run before optional model judgement. Crisis,
charged, media, catch-up, and recovery work must answer; they cannot be
silenced or handled by reaction only.

## Source Modes

| Source | Entry point | Pacing behavior |
| --- | --- | --- |
| `live` | Normal Discord DM ingestion. | May wait for burst coalescing or user typing, and may use sparse reactions or silence for routine low-risk acknowledgements. |
| `catch_up` | Discord startup/history catch-up through `process_inbound(..., coalescer_source="catch_up")`. | Treated as stale offline work, so it bypasses live typing delays, reactions, and silence. |
| `media` | Voice transcription and image analysis success paths. | Must answer so media-derived context is not dropped. |
| `recovery` | Orphan raw-message recovery after an interrupted process. | Must answer so recovered work is reconciled explicitly; crashed turn recovery still uses direct `add_burst(...)`. |

## Typing UX

The Discord gateway reports raw `TYPING_START` events through
`GatewayCallbacks.on_event`. The pacer records that runtime state and extends a
live burst while the user appears to be composing. `_handle_message` does not
start an immediate bot typing task; typing is controlled by the pacing decision.

For paced answers, the pacer can show short human typing pulses before the
agentic turn, but it suppresses those pulses while the user is actively typing.
Agentic sends that include pacing metadata call Discord outbound delivery with
`send_typing_indicator=false`, avoiding a second low-level typing indicator.
Operational or non-paced Discord sends keep the default
`send_typing_indicator=true`.

## LLM Judgement

`DISCORD_PACING_LLM_JUDGEMENT_ENABLED` allows a small text model call only for
ambiguous live routine bursts above `DISCORD_PACING_LLM_MIN_AMBIGUITY`. Before
that call, the normal text spend cap is checked. Accepted calls are cost
recorded through `llm_spend_log`, decoded as strict JSON, and then validated
against deterministic safety and source gates. Spend-cap skips, invalid JSON,
model failures, and policy overrides are recorded in `pacing_events` as
fallbacks.

## Observability

Every pacer decision records a `pacing_events` row with:

- `user_id`
- `message_ids`
- `source`
- `decision`
- `reason`
- `signal_snapshot`
- `preference_snapshot`
- `wait_ms`
- `reaction`
- `llm_judgement`

Use these rows to answer why a burst waited, reacted, silenced, or answered.
`DISCORD_PACING_EVENT_RETENTION_DAYS` is the operator retention target for this
audit stream.

## Global Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_PACING_ENABLED` | `true` | Enables Discord pacing when the provider is Discord. |
| `DISCORD_PACING_BURST_WINDOW_S` | `2.75` | Base window for coalescing nearby messages. |
| `DISCORD_PACING_INITIAL_TYPING_MIN_S` | `0.5` | Earliest live Discord typing cue after the first message in a burst. |
| `DISCORD_PACING_INITIAL_TYPING_MAX_S` | `2.5` | Latest live Discord typing cue after the first message in a burst. |
| `DISCORD_PACING_MIN_WAIT_S` | `0.8` | Lower bound for wait decisions. |
| `DISCORD_PACING_MAX_WAIT_S` | `12` | Upper bound for ordinary wait decisions. |
| `DISCORD_PACING_TYPING_GRACE_S` | `4` | How recently a user typing event counts as active composition. |
| `DISCORD_PACING_TYPING_EXTEND_S` | `2` | Extra wait added while the user appears to be typing. |
| `DISCORD_PACING_MAX_TYPING_WAIT_S` | `20` | Maximum typing-driven wait budget. |
| `DISCORD_PACING_ANSWER_TYPING_MIN_S` | `1` | Minimum pre-answer typing pulse duration. |
| `DISCORD_PACING_ANSWER_TYPING_MAX_S` | `10` | Maximum pre-answer typing pulse duration. |
| `DISCORD_PACING_ANSWER_CHARS_PER_S` | `18` | Approximate answer typing speed used to size pulses. |
| `DISCORD_PACING_REACTIONS_ENABLED` | `true` | Enables sparse reaction-only decisions. |
| `DISCORD_PACING_REACTION_COOLDOWN_S` | `180` | Minimum gap between reaction-only decisions for a user. |
| `DISCORD_PACING_REACTION_DAILY_LIMIT` | `12` | Per-user daily cap for reaction-only decisions. |
| `DISCORD_PACING_SILENCE_COOLDOWN_S` | `300` | Minimum gap before another silence decision for a user. |
| `DISCORD_PACING_LLM_JUDGEMENT_ENABLED` | `true` | Enables optional model judgement for ambiguous live routine bursts. |
| `DISCORD_PACING_LLM_MIN_AMBIGUITY` | `0.45` | Ambiguity threshold before the model can be consulted. |
| `DISCORD_PACING_EVENT_RETENTION_DAYS` | `30` | Target retention window for decision audit rows. |

## Per-User Preferences

Per-user overrides live in `users.pacing_preferences`. They are always clamped
before storage or use, so malformed JSON cannot create unbounded sleeps or turn
off deterministic safety gates.

| Key | Bounds | Notes |
| --- | --- | --- |
| `enabled` | Boolean | Turns pacing on or off for the user, defaulting to the global setting. |
| `burst_window_s` | `0.25` to `min(max_wait_s, 15.0)` | How long to coalesce nearby lines. |
| `min_wait_s` | `0.0` to `min(global max wait, 10.0)` | Clamped not to exceed `max_wait_s`. |
| `max_wait_s` | `max(global min wait, 1.0)` to `60.0` | Caps ordinary waits. |
| `typing_grace_s` | `0.5` to `30.0` | How long a typing signal remains fresh. |
| `max_typing_wait_s` | `1.0` to `90.0` | Caps typing-driven extension. |
| `answer_typing_min_s` | `0.0` to `20.0` | Clamped not to exceed `answer_typing_max_s`. |
| `answer_typing_max_s` | `0.5` to `45.0` | Caps pre-answer typing pulses. |
| `answer_chars_per_s` | `4.0` to `80.0` | Slower values make longer answer typing pulses. |
| `reactions_enabled` | Boolean | Allows reaction-only decisions for that user. |
| `reaction_daily_limit` | `0` to `100` | Per-user daily reaction-only cap. |

Example override:

```json
{
  "burst_window_s": 4,
  "typing_grace_s": 6,
  "reactions_enabled": false,
  "answer_chars_per_s": 14
}
```

## Operator Tuning

Tune globals first, then use per-user preferences only for stable individual
needs. Keep reaction limits low enough that reactions feel intentional. Prefer
raising `DISCORD_PACING_BURST_WINDOW_S` before raising maximum waits if users
often send multi-line thoughts. Validate final timing in a real Discord DM after
deploy, because client typing indicators and subjective pacing cannot be fully
proven by unit tests.
