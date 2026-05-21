# Live conversation mode

Status: draft / design briefing. Not yet implemented.
Last updated: 2026-05-13.

## Why this exists

The existing bots are turn-based and text-only over Discord. Some interactions — coaching a pregnancy conversation between partners, walking a user through a hard topic, rehearsing a real conversation they're about to have — work much better as a real-time voice exchange with a prepared structure.

A "live conversation mode" gives a user:

1. A **prep step**: Opus reads their steering (or just a topic) plus their longitudinal state and existing themes, and produces a structured agenda — a checklist of items, optionally clustered under existing themes, with the evidence we'll look for to consider each item handled.
2. A **live step**: a web interface streams audio in both directions. Deepgram transcribes with speaker diarization. Haiku takes one turn at a time and emits a single atomic decision (utterance + routing + coverage + new items) per turn. TTS speaks the response.
3. A **post-session synthesis**: Opus folds the transcript and what got covered into existing `distillations` / `observations` / `themes` / pregnancy fields — surfaced to the user as a review screen they edit before anything writes to memory.

The agenda is not a static document — it's a **living, persisted checklist that Haiku mutates as the conversation unfolds**.

## Non-goals

- Not a general-purpose voice assistant. It's session-scoped: one user (or a user + partner) sits down for a bounded conversation with a known goal.
- Not a transcription product. Deepgram is a means; the transcript is a byproduct.
- Not a replacement for the Discord text bots. This is a parallel surface for a different interaction shape.
- Not OpenAI Realtime end-to-end. We want our own model (Haiku) doing the routing and writing, not a black-box voice agent. We use a pipeline.
- Not a tree / graph. Real conversations jump, but modeling that as a mutable graph gives Haiku structural authority it doesn't need. The agenda is an ordered checklist with optional theme clustering.

## Overall flow

```
┌─────────────────────────────────────────────────────────────────┐
│  PHASE 1 — PREP (Opus, structured output)                       │
│                                                                  │
│   user steering OR topic ──┐                                    │
│                            │                                    │
│   longitudinal state ──────┼─►  Claude Opus 4.7  ──►  Agenda   │
│   + distillations          │   (function-calling                │
│   + existing themes ───────┘    schema-validated)               │
│                                                                  │
│   UI: streamed phase descriptors                                │
│   ("Catching up on where you are…" → …)                         │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│  PHASE 2 — LIVE (per-session loop)                              │
│                                                                  │
│   ┌─ Client (browser, minimal mode default) ─┐                  │
│   │  mic in, audio out, transcript, controls │                  │
│   └──────────┬───────────────────▲────────────┘                 │
│              │ PCM frames        │ TTS audio                     │
│              ▼                   │                               │
│   ┌─ Session Orchestrator ─────────────────────┐                │
│   │  validates one emit_live_turn per turn,    │                │
│   │  applies atomically, then streams TTS      │                │
│   └─┬──────────────┬────────────────┬──────────┘                │
│     │              │                │                            │
│     ▼              ▼                ▼                            │
│  Deepgram      conversation_    Claude Haiku 4.5                │
│  Nova-3 ◄───── items (rows)    ─► emit_live_turn JSON           │
│  (streaming    + conversations    (one atomic output)            │
│   ASR +         .session_fields    │                             │
│   diarization)                     ▼                             │
│      │                          ElevenLabs Flash TTS            │
│      ▼                          ──► back to client               │
│  transcript_turns                                                │
│  (append-only, speaker-tagged)                                   │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│  PHASE 3 — POST-SESSION (Opus → review screen → memory)         │
│                                                                  │
│   transcript + coverage + notes ──►  Opus synthesis ──►         │
│   review screen (user edits) ──►  distillations / observations  │
│                                    / themes / pregnancy fields   │
└─────────────────────────────────────────────────────────────────┘
```

## Phase 1 — Prep

Triggered when a user starts a live session. Input is either:

- **Steered**: free-text steering (e.g. "we want to talk about whether to move to Berlin before the baby is born").
- **Open-ended**: just a coach/topic selection (e.g. "general pregnancy coaching session").

Opus receives:

- The steering text or topic identifier
- The user's longitudinal state row(s) for the relevant topic
- Recent distillations for the user (capped)
- The user's existing `themes` rows (active + tender)
- Optionally: the partner's state if dyadic

Opus produces an **agenda** as structured output (Anthropic function-calling, schema-validated — not prose-then-parse). The schema enforces enums, bounded arrays, required IDs, and that every `next_item_ids[]` resolves.

```jsonc
{
  "title": "Berlin move conversation",
  "summary": "User and partner weighing a move to Berlin before week 32. Tension around partner's job, family proximity.",
  "items": [
    {
      "id": "i_open",
      "theme_id": null,                         // optional FK to existing themes row
      "kind": "planned",                        // 'planned' | 'dynamic' | 'thread'
      "title": "Where the move actually stands",
      "intent": "ground the conversation in the concrete timeline before going to feelings",
      "ask": "Where are you both at on the timeline right now — when would the move actually need to happen?",
      "done_when": "user has named a target window OR explicitly said no timeline yet",
      "next_item_ids": ["i_partner_job"],
      "priority": "must",                       // 'must' | 'should' | 'optional'
      "speaker_scope": "both",                  // 'primary' | 'partner' | 'both'
      "coverage_evidence_required": "explicit_answer"
        // 'explicit_answer' | 'emotional_shift' | 'concrete_decision' | 'blocker_named'
    },
    {
      "id": "i_partner_job",
      "theme_id": "th_partner_career",         // links to an existing theme row
      "kind": "planned",
      "title": "Partner's job situation",
      "intent": "surface whether the job is fixed or negotiable",
      "ask": "What's the picture on your side of the job thing?",
      "done_when": "partner has stated whether the job blocks the move",
      "next_item_ids": ["i_family"],
      "priority": "must",
      "speaker_scope": "partner",
      "coverage_evidence_required": "blocker_named"
    }
    // ...
  ],
  "session_fields_to_track": [
    "decision_direction",       // 'leaning_move' | 'leaning_stay' | 'undecided'
    "blockers_named",           // string[]
    "agreements_reached"        // string[]
  ]
}
```

Key choices:

- **One ordered list of items**, not a tree. `next_item_ids[]` gives Haiku adjacency for routing without modeling a full graph.
- **`theme_id` is the only clustering primitive.** Opus attaches items to existing themes during prep when there's a clear match; it never creates new themes here. Post-session synthesis decides if a new theme is warranted. Items with `theme_id = null` fall under a "this session" bucket in the UI.
- **`done_when` and `coverage_evidence_required` are explicit.** Haiku decides coverage against a stated criterion, not vibes. This is the load-bearing change for routing reliability.
- **`speaker_scope`** tells Haiku whose answer counts for coverage in a dyadic session.
- **`session_fields_to_track`** is per-session; lives in `conversations.session_fields jsonb` as Haiku writes to it.

### Prep UX

Opus prep is slow (30-60s). The browser shows a streamed loading state with phase descriptors emitted from the backend as the pipeline progresses — same WebSocket the live mode uses. Labels are in Rosi's voice, not technical:

1. *Catching up on where you are…* (reading longitudinal state + distillations)
2. *Thinking about what to focus on…* (drafting items + theme attachments)
3. *Getting ready for our chat…* (validating + persisting)

When prep is done the page transitions to a **session card** (see UI section) — not the raw agenda.

## Phase 2 — Live

### Cadence

Not a fixed timer. The primary trigger is **client-side voice activity detection (VAD)**: when the user stops speaking for ~600ms, the client sends a "turn end" event to the orchestrator. A 10s silence fallback triggers a bot turn if neither speaker is talking. Deepgram streams partial transcripts continuously so we're not waiting for the turn-end event to start thinking.

A bot turn is:

1. Orchestrator receives `turn_end` event (or 10s silence fallback).
2. Build Haiku prompt: agenda (prompt-cached), current item, last 6-10 transcript turns (~90s, whichever is smaller), `session_fields` slice, compact progress table (`must_pending` / `current` / `covered`), last bot utterance, open-thread items.
3. Haiku call. Haiku must emit **one structured output** `emit_live_turn` (see below). No procedural tool sequencing.
4. Orchestrator validates the output against the schema, applies it atomically to the DB (route + coverage + new items + notes + session_fields_patch), then streams the `utterance` to TTS.
5. TTS audio frames back to client.

This collapses routing-utterance coherence into one decision: there's no way to transition without saying something, or to say something without committing the routing.

### `emit_live_turn` shape

One required structured output per turn:

```jsonc
{
  "utterance": "Okay, so the timing is week 32 — what feels hardest about hitting that?",
  "route": {
    "current_item_id": "i_open",
    "reason": "user just named the week-32 deadline; staying on this item to dig into what makes it hard"
  },
  "coverage": [
    // zero or more — usually 0 or 1
    {
      "item_id": "i_intro",
      "status": "covered",                    // 'covered' | 'skipped'
      "summary": "Grounded that both partners are present and aware of the topic",
      "evidence_quote": "yes we both know what we're here to talk about"
        // verbatim or near-verbatim from transcript — required for status='covered'
    }
  ],
  "new_items": [
    // zero or more — dynamic items or threads Haiku wants to add
    {
      "parent_item_id": "i_open",
      "kind": "thread",                       // 'dynamic' | 'thread'
      "title": "Mother-in-law expectation",
      "ask": "The mom thing you mentioned — want to come back to that?",
      "theme_id": "th_in_laws"                // optional, only if clear match
    }
  ],
  "notes": [
    // zero or more — facts Haiku wants synthesis to weight later
    {
      "text": "Partner has a non-negotiable client commitment Oct 15-22",
      "attributed_to_speaker": "partner",
      "evidence_turn_id": "tt_18"
    }
  ],
  "session_fields_patch": {
    "blockers_named": ["partner_oct_commitment"]
  }
}
```

Coverage rule, baked into the prompt:

> Mark covered when the user has substantively answered the item's `done_when`, even if imperfect, unresolved, or emotionally messy. Do not wait for closure. If a loose end matters, add a thread item via `new_items`. Coverage requires `evidence_quote` from the actual transcript.

The `evidence_quote` requirement is the anti-eagerness check: no quote, no coverage. The "mark covered when substantively answered" line is the anti-cautiousness bias.

### Diarization handling

Deepgram returns speaker labels (`speaker_0`, `speaker_1`, ...) consistent within a session. The orchestrator stores a `speaker_map` on `conversations`:

```jsonc
{ "speaker_0": "primary", "speaker_1": "partner", "speaker_2": "other" }
```

- First detected speaker = `primary`. The UI lets the user re-tag a label ("That was my partner, not me") which updates the map.
- Routing decisions are scoped by each item's `speaker_scope`: a `partner`-scoped item considers coverage only against the partner's utterances.
- All speakers' utterances go into `transcript_turns` with their label intact. Haiku sees who said what.
- On new participant voice mid-session: pause and re-consent (see Consent below). No passive "other speaker" capture.
- Voice fingerprinting for cross-session re-identification is out of scope for v1.

### Barge-in

When the client VAD detects the primary speaker starting to talk while TTS is playing:

1. Client immediately stops audio playback.
2. Client sends `barge_in` event to orchestrator.
3. Orchestrator cancels any in-flight Haiku call and pending TTS.
4. Treats the incoming speech as a fresh turn.

Without this, the bot will talk over the user. With it, the interaction feels conversational.

### Latency budget

Target: <1.5s from user stopping speaking to TTS first audio.

- Deepgram finalization: ~200-400ms
- Orchestrator + DB read: ~50ms
- Haiku call (prompt-cached agenda, single structured output): ~500-900ms
- TTS first byte (ElevenLabs Flash, streaming): ~200-400ms

Prompt caching the agenda is load-bearing. Without it, Haiku resends the full checklist every turn and latency doubles.

## Phase 3 — Post-session synthesis

When the session ends (user clicks end, or extended inactivity):

1. Mark `conversations.status = 'synthesizing'`.
2. **Compression step (if session > ~20 min):** Opus reads the transcript and produces per-item slices + a list of notable quotes. For shorter sessions, raw transcript is fine. This prevents synthesis from overweighting the late transcript and missing earlier commitments.
3. **Synthesis call:** Opus reads (compressed) transcript + final coverage + notes + `session_fields` final values + existing user themes/distillations. Output is a structured proposal:
   - Items to add to `distillations` (linked to `theme_id` when set on the originating `conversation_items` row).
   - Items to add to `observations`.
   - Theme updates: `themes.last_active_at`, sentiment/health adjustments, new theme creation if warranted.
   - Pregnancy field updates if relevant (migrations 0032/0033).
   - "Open threads" surfaced as `watch_items` if they should survive the session.
4. **Review screen — non-skippable.** The user sees four sections before anything writes to memory:
   - *What Rosi heard* — neutral summary
   - *What you decided* — agreements / decisions
   - *Still open* — unresolved threads
   - *What Rosi should remember for next time* — the proposed memory writes
   Each item is editable + deletable. Buttons: **Save these notes** / **Discard session notes** (preserves operational logs but no memory writes). "No, we didn't actually decide that" is central, not edge.
5. On Save: writes go to the existing primitives. On Discard: only the transcript and the conversation row are kept.
6. Mark `conversations.status = 'synthesized'`.

This is one synthesis call (post-review), not many — keeps the proposal coherent rather than incrementally drifting.

## User interface

Single web page. WebSocket connection to backend.

### First 10 seconds (scripted)

1. **Calm start screen**: title, the editable **session card** (focus statement, 3-5 plain-language focus areas, "Anything to add or avoid?" text box, consent status, mic check), **Start with Rosi** button.
2. User presses Start.
3. UI shows *"Rosi is getting ready…"* for 2-5 seconds. Honest preparation pause, not latency theater.
4. **Rosi speaks first.** Never silent open. First line is grounding, not agenda dump:
   > *"Hi love, I'm here. Take a breath. We'll go one step at a time. Is it just you today, or is your partner here too?"*
5. Consent flow runs immediately (see below) if a partner is present.

The raw agenda (items, IDs, evidence criteria) is **never** shown to the user. That's Rosi's homework. The session card is what they see.

### Live mode layout (minimal by default)

```
┌──────────────────────────────────────────────────────────────────┐
│  ┌───── header ────────────────────────────────────────────────┐ │
│  │  Berlin move conversation         [End session]  ⏺ Live    │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌── Transcript (centered, scrolling) ──────────────────────────┐ │
│  │                                                                │ │
│  │  🟢 You: "I keep thinking…"                                   │ │
│  │  🔵 Partner: "Yeah but…"                                      │ │
│  │  🟣 Rosi: "It sounds like the timing is the heavy part…"      │ │
│  │                                                                │ │
│  │  Soft focus line:  We're talking about timing right now.      │ │
│  │  [Show structure]                                              │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌── Controls (footer) ─────────────────────────────────────────┐ │
│  │  🎙️ Mic on   🔊 Rosi speaking                                │ │
│  │  [Pause]  [Repeat]  [Back up]  [Slow down]  [Skip this]      │ │
│  │  [End & save]   [End without saving notes]                    │ │
│  └────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

- **Transcript pane** (center). Scrolling, append-only. Each row has a colored dot for the speaker (`primary` / `partner` / `other` / `bot`). Final transcripts in normal weight, in-flight partials in lighter italic. Click a speaker dot to retag that speaker label.
- **Soft focus line.** One human-language sentence — *"We're talking about timing right now"* — rendered from the current item's `title`. No checklist by default.
- **`[Show structure]`** reveals a side pane: items grouped under their `theme.title` (or "This session" for null), `✓` covered, `◐` current, `•` pending, `⊘` skipped. Open threads pinned at the bottom. Power-user / partner-session affordance, not the default.
- **Controls footer** (buttons, **not voice-only**; in emotionally charged moments people freeze, whisper, cry):
  - **Pause** — stops mic processing and TTS, session stays alive.
  - **Repeat** — replays or regenerates the last bot turn, shorter.
  - **Back up** — "That's not what I meant." Rewinds coverage/state by one item.
  - **Slow down** — reduces pace, shorter responses.
  - **Skip this** — human language for skip-current-item.
  - **End & save** / **End without saving notes** — explicit fork.

### Consent (first-class, first-run blocking)

Not future work. Runs before mic ever opens.

1. Pre-mic question on the start screen: **Who is here?** — *Just me / Me and my partner / Someone else nearby.*
2. If a partner is selected: both voices consent on mic, **or** the partner taps a shared-screen confirmation. Rosi says: *"I'll be listening to both of you, transcribing, and saving session notes only after you review them. Is that okay with both of you?"* Each person answers yes. Store a consent event with `(session_id, speaker_label, ts)`.
3. If one says no: solo mode (no partner transcription) or end.
4. During session: **Both consented** indicator + a **Stop recording for everyone** control visible at all times.
5. On a new voice detected mid-session: orchestrator pauses, Rosi re-consents that speaker. No passive capture.

### What runs where

| Client (browser) | Backend |
|------------------|---------|
| Mic capture (Web Audio API) | Deepgram WebSocket relay |
| Client-side VAD (Silero or simple energy threshold) | Session orchestrator |
| WebSocket up: PCM frames + control events (`turn_end`, `barge_in`, `consent_*`) | Haiku turn loop (single `emit_live_turn`) |
| WebSocket down: transcript updates, soft focus line, prep-phase descriptors, TTS audio | ElevenLabs Flash TTS streaming |
| Audio playback | DB writes (atomic per turn) |
| Session card + controls | Post-session synthesis + review screen feed |

## Database schema

Match existing conventions (snake_case plural, uuid PKs, `mediator` schema, RLS + FORCE RLS + deny-anon policies).

```sql
-- The session envelope
CREATE TABLE conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id),
  partner_user_id uuid REFERENCES users(id),         -- nullable, dyadic sessions
  topic_id uuid REFERENCES topics(id),                -- nullable, links to long-running global topic
  bot_id uuid REFERENCES bots(id),                    -- which coach/persona
  mode text NOT NULL CHECK (mode IN ('steered', 'open')),
  steering_text text,                                  -- null when mode='open'
  prep_summary text,                                   -- Opus's one-line summary, denormalized for listing
  status text NOT NULL DEFAULT 'prepping'
    CHECK (status IN ('prepping', 'ready', 'live', 'ended', 'synthesizing', 'review_pending', 'synthesized', 'discarded', 'failed')),
  current_item_id uuid,                               -- pointer into conversation_items, FK added after that table
  speaker_map jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- {speaker_0: 'primary', speaker_1: 'partner', ...}
  session_fields jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- per-session values for prep.session_fields_to_track
  consent_events jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- [{speaker_label, role, consented: bool, ts}]
  started_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- The agenda: one row per item. Replaces conversation_topics + conversation_nodes + conversation_state.
CREATE TABLE conversation_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  theme_id uuid REFERENCES themes(id),                -- nullable; the only clustering primitive
  parent_item_id uuid REFERENCES conversation_items(id),
  kind text NOT NULL CHECK (kind IN ('planned', 'dynamic', 'thread')),
  title text NOT NULL,
  intent text,
  ask text,                                            -- the opening prompt for this item
  done_when text,                                      -- coverage criterion in natural language
  next_item_ids uuid[] NOT NULL DEFAULT '{}',         -- adjacency hint for routing
  priority text NOT NULL DEFAULT 'should'
    CHECK (priority IN ('must', 'should', 'optional')),
  speaker_scope text NOT NULL DEFAULT 'primary'
    CHECK (speaker_scope IN ('primary', 'partner', 'both')),
  coverage_evidence_required text NOT NULL DEFAULT 'explicit_answer'
    CHECK (coverage_evidence_required IN ('explicit_answer', 'emotional_shift', 'concrete_decision', 'blocker_named')),
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'active', 'covered', 'skipped')),
  coverage_summary text,
  coverage_evidence_quote text,                       -- the quote Haiku attached when marking covered
  order_hint int NOT NULL DEFAULT 0,
  covered_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_conversation_items_conv ON conversation_items(conversation_id, order_hint);
CREATE INDEX idx_conversation_items_theme ON conversation_items(theme_id) WHERE theme_id IS NOT NULL;
CREATE INDEX idx_conversation_items_open_threads ON conversation_items(conversation_id) WHERE kind = 'thread' AND status IN ('pending', 'active');

-- Now wire the back-pointer from conversations.current_item_id
ALTER TABLE conversations
  ADD CONSTRAINT conversations_current_item_fk
  FOREIGN KEY (current_item_id) REFERENCES conversation_items(id);

-- Append-only transcript history, one row per finalized utterance
CREATE TABLE transcript_turns (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  ts timestamptz NOT NULL DEFAULT now(),
  speaker_label text NOT NULL,                       -- 'speaker_0', 'speaker_1', 'bot'
  speaker_role text NOT NULL
    CHECK (speaker_role IN ('primary', 'partner', 'other', 'bot')),
  text text NOT NULL,
  asr_confidence real,                               -- null for bot turns
  active_item_id uuid REFERENCES conversation_items(id),  -- where we were when this was said
  was_routing_input bool NOT NULL DEFAULT false      -- did this turn trigger a Haiku call
);

CREATE INDEX idx_transcript_turns_conv_ts ON transcript_turns(conversation_id, ts);

-- Lightweight per-turn facts Haiku flagged. Synthesis weights these.
-- Not the same as observations/distillations; these are session-local.
CREATE TABLE conversation_notes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  text text NOT NULL,
  attributed_to_speaker text,                        -- 'primary' | 'partner' | 'other'
  evidence_turn_id uuid REFERENCES transcript_turns(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Audit log of item traversal (replay / evals / "back up" undo target)
CREATE TABLE item_visits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  item_id uuid NOT NULL REFERENCES conversation_items(id),
  entered_at timestamptz NOT NULL DEFAULT now(),
  exited_at timestamptz,
  transition_reason text                              -- from emit_live_turn.route.reason
);

CREATE INDEX idx_item_visits_conv ON item_visits(conversation_id, entered_at);
```

Notes on the schema choices:

- **`conversation_items` is the single agenda primitive.** It replaces what the earlier draft split across `conversation_topics`, `conversation_nodes`, and `conversation_state`. Threads and dynamic items are just rows with a different `kind`. There is no separate "state" row; current position is `conversations.current_item_id` + `conversation_items.status`.
- **`theme_id` is the only clustering.** UI groups items by `themes.title` when set, "This session" when null. Opus attaches existing themes during prep; post-session synthesis may create new ones.
- **`coverage_evidence_quote` is on the item row**, not in a side table. This is what enforces "no quote, no coverage" at the schema level.
- **`speaker_map`, `session_fields`, `consent_events`** live as JSONB on `conversations`. Acceptable JSONB use — read-only-ish, low-cardinality, never queried across rows.
- **`conversation_notes` is the live-fact channel.** Tiny, append-only, session-scoped. Synthesis reads these; if they matter long-term they get promoted to `observations` / `distillations` post-review.
- **`tool_calls`** (already exists) continues to capture every Haiku emission for audit. Each `emit_live_turn` is one row.
- **Live turns create `bot_turns`** rows the same way other bot surfaces do — gives `tool_calls.turn_id` a parent.
- **`prep_summary` denormalized** onto `conversations` so the user can scroll past sessions without joining items.
- **Open threads** are just `conversation_items WHERE kind='thread' AND status IN ('pending','active')`. Same primitive, no parallel table.

## Models, costs, latency

| Stage              | Model                       | Why                                              |
|--------------------|-----------------------------|--------------------------------------------------|
| Prep               | Claude Opus 4.7 (structured output)  | One-shot, quality matters, latency hidden behind loading UX. |
| Live routing+gen   | Claude Haiku 4.5 (one `emit_live_turn` per turn) | Sub-second turns, prompt-cached agenda. |
| ASR                | Deepgram Nova-3 streaming   | Streaming + diarization in one.                  |
| TTS                | ElevenLabs Flash             | Persona consistency for Tante Rosi. Warm/slow/grounded, no caricature. |
| Synthesis compress | Claude Opus 4.7 (if >20 min) | Avoids late-transcript bias.                     |
| Post-session synth | Claude Opus 4.7              | Proposes writes to existing primitives; user edits before commit. |

## Open questions / things to decide

1. **Dynamic item cap.** How many `new_items` entries (kind='dynamic' or 'thread') per session before the orchestrator refuses to apply more? Suggest 12 (8 dynamic + 4 thread); revisit after observing real sessions.
2. **Re-prep mid-session.** If the conversation goes wildly off the agenda, do we trigger a fresh Opus run, or rely on Haiku's `new_items` to extend? Default: rely on `new_items`. Re-prep is a manual button (v2).
3. **"Back up" semantics.** Does the button rewind by one `item_visit` (last transition) or one Haiku turn (last `emit_live_turn`)? Suggest: rewind one `item_visit`, surfacing the prior item with its status restored. Coverage made in the rewound turn is reverted.
4. **Persistence of audio.** Store raw audio or transcript only? Privacy-favored default: transcript only, audio discarded after Deepgram finalization. Confirm.
5. **Auth surface.** The web UI needs auth — presumably the same identity as the Discord user. Mechanism (magic link? Discord OAuth?) is unspecified.
6. **ElevenLabs voice provisioning.** Use a pre-built ElevenLabs voice for Tante Rosi, or clone one? Cloning is more identity-distinct but operationally heavier. Default: start with a pre-built warm/older voice tuned with style instructions; revisit if it feels generic.
7. **Theme creation during synthesis.** What threshold should Opus apply before creating a new theme post-session? Suggest: only if the same not-yet-themed cluster appears across ≥2 sessions, otherwise just write `distillations` and let the existing distillation→theme pipeline handle it.

## Staged rollout

Don't build it all at once. Each stage is independently shippable and de-risks the next.

1. **Prep + session card only.** Opus generates and persists the agenda. Render the session card (focus areas only, never raw items). No live audio. Useful by itself — the user reads the card before a real human conversation.
2. **Transcript-only live mode.** Browser captures mic, Deepgram streams to a backend that persists `transcript_turns` and `conversation_items` via a manual "advance" button. Consent flow live. UI shows minimal-mode transcript + soft focus line. No bot speech. Validates ASR, diarization, consent, schema.
3. **Manual bot turns.** A "speak next" trigger fires one Haiku `emit_live_turn`; bot responds via TTS. Validates Haiku structured output + atomic apply + review-screen synthesis.
4. **Autonomous turn-taking.** Client VAD + silence fallback + barge-in. The full feature.

Each stage adds one new dependency category, so failures are isolated to that stage's surface.

## Canonical session status lifecycle (Sprint 5)

Every live conversation row has a `status` column that moves through a
well-defined lifecycle.  The **canonical** status values (the public API
contract) are:

```
persona_pick  →  preparing  →  ready  →  consent  →  active
                                                        │
                                                        ▼
                                                 debriefing
                                                        │
                                                        ▼
                                                review_pending
                                                        │
                                                        ▼
                                                   completed
```

| Step | Canonical status | Meaning |
|------|------------------|---------|
| User lands on persona picker | *(no session yet)* | `createSession` has not been called. |
| Prep running | `preparing` | The agentic prep job is building the agenda (30–60s). |
| Agenda ready | `ready` | Prep succeeded; agenda items persisted. Waiting for user consent / mic start. |
| Consent / WS start | `active` | Conversation is live. Transcript turns streaming. Haiku emitting turns. |
| Session ended | `debriefing` | The agentic debrief job is running. Review data is being produced. |
| Review available | `review_pending` | Debrief succeeded; the user can inspect / edit / save the review. |
| Review saved | `completed` | The user saved (or discarded) the review. Terminal state. |

Two failure branches exist:

- `prep_failed` — Prep could not produce an agenda (e.g. model exhausted
  without calling `submit_live_brief`).  The user can retry via the
  same session ID.
- `debrief_failed` — Debrief could not finish (e.g. model crashed,
  `submit_live_debrief` missing).  The user's transcript is still
  accessible.  Retry is available.

### Single source of truth

All status canonicalization routes through the helpers in
`app/services/live/status.py`.  Every endpoint that returns a status
— `/card`, `/end`, `/review`, `/review/save`, raw `GET /api/live/sessions/{id}`,
and `/api/live/ops/metrics` — must call `canonicalize_status()` before
returning to clients.  Writers use only canonical statuses.

### Compatibility with legacy statuses and old conversations

The additive migration `0055_live_product_statuses` extends the
`CHECK` constraint on `mediator.conversations.status` to accept both
canonical and legacy values.  The `DEFAULT` was changed to `'preparing'`.
Old rows are **never rewritten** — backward compatibility is handled
entirely at read time via the canonicalization layer.

Legacy → canonical mapping (read-side only):

| Legacy value | Canonical equivalent |
|-------------|---------------------|
| `prepping` | `preparing` |
| `live` | `active` |
| `synthesizing` | `debriefing` |
| `synthesized` | `completed` |
| `ended` | `completed` |

All read paths (`/card`, `/end`, `/review`, `/review/save`, raw GET,
`/api/live/ops/metrics`) accept both canonical and legacy statuses.
Ops metrics aggregation folds legacy counts into canonical buckets
via `grouped_status_metric()` so operators see a single view
regardless of which migration epoch each row came from.

Partial indexes (`idx_conversations_status_active`,
`idx_conversations_spend_active`) were extended with `OR` conditions
to include canonical statuses while retaining legacy values, so
existing query plans do not regress.

## Retry semantics (Sprint 5)

Both prep and debrief support retry on failure.  Key invariants:

### Prep retry (`retry_live_prep`)

- **Called on the same session ID.**  No replacement row is created.
- **Prep retry owns the status transition.**  Callers MUST NOT pre-set
  the status before invoking `retry_live_prep`.  The function checks
  that the conversation is in `prep_failed` (canonicalized), resets it
  to `preparing`, then re-runs `run_live_prep_agentic_job`.
- **New bot turn.**  Every retry attempt creates a fresh
  `mediator.bot_turns` row (kind=`live_prep`).  Old bot turns are preserved
  for audit.
- **New artifact revision.**  Retry creates a new revision of the
  `live_prep_brief` artifact (higher `revision_number`).  The old
  artifact revision is NOT deleted — it remains auditable with
  `deleted_at` left null.
- **Structured logs** are emitted for every retry attempt
  (start → success/failure) with `duration_s`, `tool_count`,
  `failure_reason`, `failure_class`, `retry_count`, and
  `status_transition`.

### Debrief retry (`retry_live_debrief`)

- **Called on the same session ID.**  No replacement row.
- **Status transition:** `debrief_failed` → `debriefing` → (on success)
  `review_pending` or (on failure) `debrief_failed`.
- **New bot turn** (kind=`live_debrief`) and **new artifact revision**
  (type=`live_debrief`) on every attempt.
- **Structured logs** include `submit_missing` (boolean, whether
  `submit_live_debrief` was never called), `durable_write_count`
  (how many durable writes were attempted before the failure), and all
  standard fields.

## Single-row prep creation invariant (Sprint 5)

Users create a session by calling `POST /api/live/sessions`.  The
router **pre-inserts exactly one** `mediator.conversations` row in
`preparing` status.  That row's UUID is returned to the client and
passed into `produce_agenda(session_id=…)` so the producer updates it
in-place rather than inserting a second row.

Two prep paths exist:

1. **Agentic path** (default): The router pre-inserts the row and
   schedules `run_live_prep_agentic_job` via `asyncio.create_task`.
   The job accepts the `conversation_id` as a parameter and updates
   the existing row.
2. **Legacy synchronous path** (stub / explicit provider override):
   The router passes the pre-inserted `session_id` to
   `produce_agenda(session_id=…)`, which updates the row in-place via
   `UPDATE … SET status='ready', prep_summary=$2, mode=$3 WHERE id=$1`.

**In both paths exactly one conversation row is created.**  The
`session_id` plumbing eliminates the dual-creation hazard where the
router and producer each inserted their own row, potentially leaving
an orphan.

An internal sweep (`sweep_orphaned_prepping`) covers any pre-existing
orphan rows that may exist from before this reconciliation, but new
sessions guarantee the single-row invariant at creation time.

## Debrief artifact adapter contract (Sprint 5)

`GET /api/live/sessions/{id}/review` prefers the highest-revision
non-deleted `live_debrief` artifact when one exists.  If no such
artifact exists (old conversations, missing artifacts), it falls back
to deterministic synthesis via `synthesize_review()`.

The adapter `_debrief_artifact_to_session_review()` in
`app/services/live/adapters.py` converts the raw `live_debrief` artifact
payload (produced by the LLM via `submit_live_debrief`) into the
`SessionReview` shape the frontend expects.

**Scalar coercion rules for the four content fields:**

| Field | LLM output | Adapter output |
|-------|-----------|---------------|
| `what_heard` | scalar string `"user talked about Berlin"` | `[{text: "user talked about Berlin", source: "live_debrief"}]` |
| `what_decided` | null | `[]` |
| `still_open` | whitespace-only string `"  "` | `[]` (empty strings omitted after trim) |
| `what_to_remember` | list of strings `["fact A", "fact B"]` | `[{text: "fact A", source: "live_debrief"}, {text: "fact B", source: "live_debrief"}]` |
| (any field) | list of dicts `[{text: "x", item_id: "y"}]` | Same list with `source: "live_debrief"` added to each entry if missing |
| (any field) | single dict `{key: val}` | `[{key: val, source: "live_debrief"}]` |
| (any field) | integer / bool / unknown type | `[]` (defensive empty list) |

**Key contract rules:**

1. Every scalar string is trimmed.  Whitespace-only strings are omitted
   (empty list returned).
2. Every coerced item carries `source: "live_debrief"` metadata so the
   frontend can distinguish debrief-artifact data from deterministic
   synthesis data.
3. The adapter never passes raw strings or objects directly to the UI —
   every content field is coerced through `_coerce_review_field()`.
4. Additive fields (`debrief_pending`, `debrief_failed`,
   `live_debrief`, `review_summary`) are forwarded through unchanged
   when present in the payload.
5. `is_empty` is derived from content — true when all four fields are
   empty lists.

The frontend (`ReviewScreen.tsx`) handles both shapes gracefully:
plain strings (deterministic synthesis) render directly; objects carry
a `✦` source indicator when from `live_debrief`.  The `itemKey()`
helper falls back to array index when `item_id` is missing (as is the
case for scalar-coerced debrief items).

## Related docs

- [Multi-agent architecture](multi-agent-architecture.md) — bot/topic/binding model. Live conversation mode is a new channel surface in this model.
- [Longitudinal state](longitudinal-state.md) — `user_journeys` schema that post-session synthesis writes back into.
- [Distillations migration](distillations-migration.md) — distillation pipeline that synthesis hooks into.
- [Live voice deployment runbook](live-voice-deployment-runbook.md) — operator triage and debug endpoint documentation.
