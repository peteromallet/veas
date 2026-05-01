# Relationship Reflection and Mediation Assistant — Development Spec

A private-chat reflection and mediation assistant for two partners. The assistant listens to each partner privately, accumulates structured understanding over time, surfaces patterns, and helps them understand each other better through asynchronous mediated communication. It is **not a therapist** — it borrows techniques from established relational frameworks but is explicit about its scope.

This spec describes the **fundamental maximally viable product**: the simplest version that captures what makes the idea valuable, with no scaffolding for capabilities the core doesn't need.

---

## Concept

Each partner has a private one-on-one chat with the assistant. They share frictions, reflections, frustrations, and questions. The assistant blends honest observation with gentle mediation — leaning reflective when emotional support is needed, mediating when cross-partner translation is the goal, and actively redirecting to in-person conversation when that's what's actually called for.

The assistant defaults to transparency: by default, anything one partner shares can inform what the other hears, distilled appropriately. Exceptions are explicit out-of-bounds (OOB) entries owned by each partner.

The assistant grounds responses in actual stored data via tools — minimal assumption, maximal retrieval. It builds structured knowledge about each partner and the relationship over time, so each conversation builds on the last rather than starting fresh.

---

## Definitions

Concrete definitions for terms used throughout the spec. The bot's prompts include these so behavior is consistent.

**Crisis** — used to determine when the bot drops the mediator role:
- Signs of self-harm ideation or intent
- Signs of imminent danger to self or others
- Signs of abuse (emotional, physical, sexual)
- Severe acute distress (panic, dissociation, breakdown)

Anything else, including intense relationship friction, is not crisis.

**Message charge levels:**
- `routine` — everyday content, low emotional weight
- `notable` — emotionally meaningful but not heavy
- `charged` — significant emotional weight, conflict, vulnerability, or intensity
- `crisis` — meets crisis criteria above

**Observation confidence:**
- `high` — multiple reinforcing instances over time, or directly stated by the partner
- `medium` — clear pattern with some evidence, but limited reinforcement
- `low` — initial impression, single instance, or speculative

**Significance scoring (1-5)** — anchor examples in the Significance Scoring section.

**Watch item "addressed"** — the bot has surfaced the item with the user, or the user has resolved it themselves, or the underlying situation has changed enough that the item no longer applies. The bot logs which case it was via the `addressing_note` parameter on `address_watch_item`.

**Theme abstraction level** — themes are **life domains**, not specific topics or recurring arguments. See Themes below.

---

## Frameworks the Assistant Borrows From

The assistant is not a therapist and does not deliver therapy. It borrows lenses and techniques from established frameworks, applied with judgment:

- **Nonviolent Communication (NVC)** — "when X, I feel Y, because I need Z" structure for translating charged content into hearable form
- **Gottman-style pattern recognition** — noticing bids, repair attempts, and the four horsemen (criticism, contempt, defensiveness, stonewalling) as observations, not diagnoses
- **Internal Family Systems "parts" language** — surfacing ambivalence without flattening it
- **Reflective listening** — paraphrasing before responding to confirm understanding
- **Curiosity over interpretation** — questions before diagnoses
- **Repair attempt surfacing** — naming de-escalation moves the recipient may have missed
- **Externalizing the problem** — framing recurring tensions as something the couple faces together

These are tools, not modes. The assistant blends them based on what the moment calls for.

---

## Stance on Assessments

The assistant makes honest observations without inferring pathology. It does **not** use diagnostic or clinical language ("anxious attachment," "ADHD traits," "avoidant"). It **does** describe behavior and patterns clearly when they're grounded in data. Observations are held as testable, not authoritative — the assistant invites confirmation or pushback, treats both partners as capable adults, and avoids both flattering vagueness and pathologizing labels.

---

## Architecture

**Hosting:** Railway
**Database:** Supabase (Postgres)
**Backend:** Python (FastAPI), async
**Messaging:** WhatsApp Cloud API (Meta) or Discord DM provider
**Voice transcription:** Groq Whisper API
**Image analysis:** OpenAI vision API (latest)
**Conversational LLM:** Claude (Anthropic API)

**Model selection:**
- Conversational loop: Claude Sonnet (latest at build time). Good balance of cost and capability for multi-tool reasoning.
- Significance scoring: Claude Haiku (latest). Scoring is a focused single-shot judgment; runs frequently enough that cost matters.
- If conversational quality feels insufficient, selectively upgrade the loop to Opus for charged messages or complex reasoning. Tradeoff worth measuring after launch.

**Security:**
- Phone number whitelist (the two partners only)
- Webhook signature validation against Meta
- Secrets in Railway env vars
- Daily LLM spend caps with alerts (separate caps for text, vision, transcription)
- Supabase service role key only on backend; RLS policies as defense in depth
- 2FA on Supabase and Railway accounts
- Short log retention (7–14 days)

**Starting cost caps** (tunable after first month of real usage):
- Text LLM (Anthropic): $5/day per partner ($10/day total)
- Vision (OpenAI): $2/day total
- Transcription (Groq): $1/day total
- Alerts at 80% of cap; hard stop at 100% with graceful degradation

**Message flow:**
1. Transport ingress (WhatsApp webhook or Discord gateway) → FastAPI/service handler → quick ack/dispatch
2. Inbound message persisted immediately, queued for async processing
3. Smart multi-message handling (debouncing)
4. Voice notes routed through Groq Whisper first
5. Images routed through OpenAI vision first
6. Agentic loop runs (system prompt + hot context + tools)
7. Outbound messages pass through OOB guardrail before sending
8. State updates written to DB

---

## WhatsApp 24-Hour Window

Meta's Cloud API only allows free-form outbound within 24 hours of the recipient's last inbound message. Outside that window, only pre-approved templates can be sent. All outbound goes through a single `send_outbound(user, content, template_fallback)` helper that decides at send-time which path to use based on `now - last_inbound_at`.

**Approved templates needed (submit on day one — Meta approval has lead time):**
- `weekly_summary` — existing template described under Prompts
- `escalation` — existing template described under Prompts
- `checkin_nudge` — variable check-in when window has closed ("Hi {{1}}, been a bit — anything on your mind? Just message me back when you're ready.")
- `pause_confirmation` — pause notification to the non-pausing partner
- `media_failure` — retry prompt when transcription/vision fails outside the window

If a scheduled outbound can't be carried by any template (rare — e.g. crisis follow-up content too specific for the templates), the bot defers and waits for the user's next inbound, logging the deferral on `bot_turns.reasoning`. Most outbound is reactive (within seconds of an inbound), so the 24h rule mostly bites on scheduled jobs.

---

## The Five Knowledge Primitives

The assistant accumulates structured understanding through five distinct primitives. Each has a clear role; the bot writes to whichever fits. When something fits more than one, the bot writes to all that apply — primitives are designed to coexist, not partition.

### 1. Style notes — durable traits about how a person communicates and processes

*Examples:*
- "Tends to understate when upset. Processes by talking it out, gets clearer through speech."
- "Direct in conflict, takes time to soften. Defaults to humor when uncomfortable."

*Lives on:* `users` table. One living text field per user, refreshed periodically.

### 2. Memories — specific facts about the people and their life

*Examples:*
- "Her dad has Parkinson's, diagnosed 2023."
- "They've been trying for a kid since January 2024."
- "He's allergic to shellfish."

*Discriminator:* Is it a fact? → memory. Memories can optionally link to themes when they sit within a life domain.

### 3. Themes — high-level life domains

Themes operate at the **life domain** level — the durable shape of what the relationship is navigating. Not specific arguments or recurring topics. A relationship probably has 5–15 themes at any time, not 50. Themes emerge slowly and persist for years.

*Examples:*
- "Caring for aging parents"
- "Navigating their different communication styles"
- "Balancing work demands and the relationship"
- "Becoming parents / fertility journey"
- "Money and financial security"
- "Extended family dynamics"
- "Physical intimacy and connection"

*Not themes:* "weekend planning friction," "the dishwasher argument," "in-laws visiting last March." Those live as observations, watch items, or memories.

*Discriminator:* Is it a durable life domain organizing a category of experience? → theme.

*Creation:* No hard threshold — the bot uses judgment. Prompt guidance leans conservative ("themes are life domains, not topics — only create when you're seeing a durable pattern across multiple conversations"). Over-creation in early weeks is reviewable in the admin view and easy to clean up.

### 4. Watch items — specific things to follow up on

*Examples:*
- "He said he'd think about therapy — revisit in a week."
- "She mentioned a hard conversation with her sister coming up Sunday."
- "Doctor's appointment for her dad on the 14th — check in afterward."

*Discriminator:* Is there a specific moment to circle back on? → watch item.

### 5. Observations — learned patterns held with confidence

*Examples:*
- "He brings up work frustration before getting sharp with her."
- "She gets quieter the week after visiting her parents."
- "Their best reconnection happens on long walks."

*Discriminator:* Is it a pattern the bot inferred from accumulated evidence? → observation. Observations can link to themes.

---

## Data Model

All tables in English regardless of conversation language. All timestamps are `timestamptz`.

### users
```
id, name, phone (unique), timezone, style_notes (text, living),
created_at
```

### messages (unified inbound + outbound)
```
id, direction ('inbound'|'outbound'), sender_id (nullable: null=assistant),
recipient_id (nullable), content, sent_at, in_reply_to (self-ref nullable),
processing_state ('raw'|'processed'|'withheld'|'expired'),
charge ('routine'|'notable'|'charged'|'crisis'),
media_url (nullable), media_type (nullable: 'voice'|'image'|'document'),
media_duration_seconds (nullable, for voice),
media_analysis (jsonb, nullable, vision output for images),
whatsapp_message_id (unique, for dedup and edit/delete tracking),
edited_at (nullable), edit_history (jsonb, prior content versions),
deleted_at (nullable)
```

### memories
```
id, about_user_id (nullable: null=about-the-couple),
content, status ('active'|'superseded'|'invalidated'),
supersedes_memory_id (self-ref, nullable),
related_theme_ids (array, nullable),
created_at, last_referenced_at
```

### themes
```
id, title (life-domain level), description (living 2-3 sentence summary),
status ('active'|'dormant'|'resolved'|'resolved_by_time'),
sentiment ('improving'|'stable'|'worsening'|'mixed'),
health ('healthy'|'tender'|'strained'|'inflamed'),
first_seen_at, last_active_at, last_reinforced_at, updated_at
```

`last_active_at` = recently mentioned. `last_reinforced_at` = recent evidence the underlying domain is still live in the relationship. The latter drives surfacing weight.

### watch_items
```
id, owner_user_id (whose thread this came from),
content, due_at (nullable, when to circle back),
status ('open'|'addressed'|'expired'|'cancelled'),
addressing_note (nullable, text — how it got addressed),
created_at, addressed_at (nullable),
related_theme_ids (array, nullable)
```

### observations
```
id, content, about_user_id (nullable: null=about-the-dynamic-or-pair),
confidence ('high'|'medium'|'low'),
significance (1-5), scoring_prompt_version,
status ('active'|'contradicted'|'stale'),
related_theme_ids (array, nullable),
supporting_message_ids (array, nullable),
created_at, last_reinforced_at, surfaced_count
```

### out_of_bounds
```
id, owner_id, sensitive_core, shareable_context (nullable),
severity ('soft'|'firm'|'hard'),
status ('active'|'expired'|'lifted'),
created_at, review_at (nullable)
```

### scheduled_jobs
```
id, user_id (nullable), job_type ('checkin'|'weekly_summary'|'oob_review'|'watch_item_due'),
scheduled_for, context (jsonb), status ('pending'|'fired'|'superseded'|'cancelled'),
created_at, fired_at (nullable)
```

Persistent, DB-backed — survives app restarts.

### bot_turns
```
id, triggered_by_message_id (nullable), user_in_context (nullable),
prompt_snapshot, system_prompt_version, reasoning,
final_output_message_id (nullable),
started_at, completed_at, model_version,
tool_call_count (int, default 0), duration_ms (nullable),
failure_reason (nullable, e.g. 'crashed', 'llm_timeout')
```

### tool_calls (write operations only — read calls live in app logs)
```
id, turn_id, tool_name, arguments (jsonb), result (jsonb),
called_at, duration_ms
```

### feedback
```
id, from_user_id, target_type ('message'|'turn'|'general'),
target_id (nullable), sentiment ('positive'|'negative'|'mixed'),
content (nullable, free text), source ('conversational'|'reaction'),
created_at
```

---

## Index Strategy

Starting indexes — revisit after observing real query patterns.

- `messages`: `(sender_id, sent_at DESC)` for thread retrieval; partial index on `(processing_state)` where state = 'raw' for restart recovery; `(whatsapp_message_id)` already unique
- `scheduled_jobs`: partial index on `(status, scheduled_for)` where status = 'pending' for the worker poll
- `themes`: `(status)`; `(last_reinforced_at DESC)` for recency-weighted retrieval
- `observations`: partial index on `(status, last_reinforced_at DESC)` where status = 'active'; `(about_user_id, status)` for per-user retrieval
- `memories`: partial index on `(about_user_id, status)` where status = 'active'
- `watch_items`: partial index on `(owner_user_id, status)` where status = 'open'; partial index on `(due_at)` where due_at is not null
- `bot_turns`: `(started_at DESC)` for admin view; `(triggered_by_message_id)`
- `out_of_bounds`: partial index on `(owner_id, status)` where status = 'active'

GIN indexes on array fields (`related_theme_ids`, `supporting_message_ids`) for queries like "observations linked to theme X."

---

## Significance Scoring

Stored on observations. (Themes use status/health; OOB uses severity; messages use charge; memories are binary present/superseded.)

Scored in the **application layer**, not in Postgres. When the bot writes an observation, the score is produced as part of the same agentic turn — either as a structured field in the write tool's arguments or via a focused LLM scoring call within the loop. Stored alongside `scoring_prompt_version` so re-scoring is possible when the prompt changes.

**1–5 scale with anchor examples** (these live in the scoring prompt so the LLM sees them every time):

- **1 — Trivial.** Marginal pattern, weak evidence, low relevance even if true. *Example: "He uses slightly more emojis on weekends."*
- **2 — Minor.** Real but small. Worth recording, not worth surfacing proactively. *Example: "She tends to send longer messages in the morning."*
- **3 — Notable.** Solid pattern with real relevance to how they relate. Worth surfacing when relevant context arises. *Example: "He brings up work frustration before getting sharp with her."*
- **4 — Significant.** Strong pattern materially affecting how the relationship functions. Should actively inform the bot's engagement. *Example: "Their conflicts cool faster when she initiates the repair; much slower when he does."*
- **5 — Core.** Defining pattern of the relationship dynamic. Always-on context. *Example: "Long walks have been their primary reconnection mechanism for years — it's how they consistently repair."*

Used to gate: surfacing decisions, weekly summary inclusion, "anything I should think about?" retrieval.

---

## Decay

Memory and confidence decay over time, baked into queries and periodic adjustment:

**Query-level:**
- Default sorts use `recency_weighted_score = significance / (1 + age_days / 60)` — ~60-day effective half-life, expressible directly in SQL, no exp(). Age is measured against `last_reinforced_at` (falling back to `created_at` if null).
- Older content remains searchable but is deprioritized in default surfacing
- Style notes and observations apply more weight to recent reinforcement

**Periodic transitions** (handled in the weekly summary job, which does light housekeeping alongside producing the summary):
- Themes with no `last_active_at` update for 6+ weeks → `dormant`
- Themes dormant 4+ months → `resolved_by_time` (still surfaceable as "this used to come up")
- Observations not reinforced for 3+ months → confidence drops a level; not reinforced for 6+ months → `stale`
- Watch items past `due_at` with no addressing for 30+ days → `expired`

A theme being mentioned (`last_active_at`) is different from new evidence the domain is still live (`last_reinforced_at`). The latter drives decay.

---

## Out-of-Bounds Guardrail

OOB is **both** in-prompt context (LLM aware) **and** a separate outbound check (every outbound message runs through `check_oob(content, recipient)` before delivery). Belt and suspenders — the duplication is the safety, accuracy is the constraint.

`check_oob` runs as its own LLM call against the recipient's active OOB entries — same model as the conversational loop (Sonnet) so the second check is at least as smart as the first. Inputs: the draft outbound, the recipient's active OOB entries, and a short sender-intent summary. Outputs: `verdict ∈ {ok, rewrite, block}`, reason, and a `suggested_rewrite` when applicable. On `rewrite` the main loop receives the suggestion as a tool result and decides whether to accept or re-draft. On `block` the message doesn't send and the loop is told why.

**Failure mode:** On checker timeout or error, fail closed for `firm` and `hard` OOB (don't send, queue for retry); fail open for `soft` (send with a logged warning). The `checker_failed` flag travels on the tool result.

Severity levels:
- `soft` — prefer not to share, use judgment
- `firm` — don't share unless directly relevant and important
- `hard` — never share

Both partners can see counts and topic-level summaries of each other's OOB entries (not contents). Owners manage their OOB through natural language ("don't bring up X again," "you can mention Y now").

**OOB countersummary generation:** Computed on demand, not stored. When asked ("what topics has she asked you not to share?"), the bot reads the partner's active OOB entries and generates a topic-level summary that strips identifying detail — categories of sensitive content, never quotes or paraphrases. Counts plus topic clusters ("three entries related to family history, two related to past relationships"). If the OOB owner has only one entry on a niche topic, the summary stays vague enough that the topic itself isn't revealed ("one entry related to a personal matter"). Computing on demand means there's no separate countersummary record to drift out of sync. The bot's prompt has explicit guidance on this language.

---

## Multi-Message Handling

**Idle state, message arrives:** Coalescing window. Start 10s timer. If new messages arrive, reset timer (cap total wait at 30s from first message). On timer expiry, mark all messages in the burst as a single processing unit and trigger the agentic loop. A message arriving after the cap during processing falls into the next-turn bucket.

**Assistant is mid-processing, new message arrives:** Let the current turn finish; the new message starts the next turn with both the just-sent outbound and the new message in hot context, so continuity flows naturally. Mid-reasoning interruption is not implemented.

**Conversation gaps:** When the prior assistant message is recent, continuity is natural. When the gap is longer (days+), the assistant acknowledges it if meaningful ("good to hear from you again — last we talked you were navigating X, has anything shifted?"). Judgment-based, not rigid.

**App restart recovery:** On startup, scan for:
- Messages with `processing_state = 'raw'` older than 30s with no associated `bot_turn` → reprocess
- `bot_turns` with `started_at` more than 5 minutes old and `completed_at = null` → mark `failure_reason = 'crashed'`, reprocess the triggering message
- `scheduled_jobs` with `status = 'pending'` and `scheduled_for` in the past:
  - Less than 1 hour late → fire normally
  - 1–24 hours late → fire with `context.delayed = true` so the bot adjusts ("I meant to send this earlier...")
  - More than 24 hours late → mark `cancelled` with reason "too stale"

Partial bot_turn writes (tool_calls during a crashed turn) are not rolled back — they're additive (new memory, new observation, etc.) and the next turn reasons forward from current state. Acceptable trade-off vs. transactional complexity.

**Scheduled job worker:** Uses `SELECT ... FOR UPDATE SKIP LOCKED` so two workers can't fire the same job. Each job has a unique ID for idempotency.

---

## Pause / Resume

**On `/pause`:**
- Cancel all pending scheduled jobs for both users (mark `superseded`)
- Stop responding to inbound (still persist messages to DB so nothing is lost)
- Send a confirmation outbound to each partner: "Pausing for now. Message me again when you're ready to resume."

**On `/resume` (from either partner):**
- Re-enable inbound processing
- Do not auto-process backlog — start fresh from this point. The messages are stored if the user wants to reference them.
- Restore weekly summary scheduling

**Symmetry note:** When one partner pauses, the bot stops responding to both. The other partner is informed via the pause confirmation outbound. Either partner can resume.

---

## Edits and Deletions

**Edits:** Update the row, append prior content to `edit_history`, set `edited_at`. If the edit changes meaning materially, the bot's next turn in that thread acknowledges the change naturally rather than carrying the old understanding forward.

**Deletions:** Set `deleted_at`; retain content for 24 hours (accidental-deletion grace period). After 24 hours, replace content with `[deleted]` marker but preserve row for audit integrity. The assistant doesn't proactively notify the other partner of the deletion but reflects the change if the topic comes up.

**Drift on derivative writes:** If an edited or deleted message already produced memories/observations/watch items, those derivative rows are NOT retracted automatically. The bot reasons forward from current state on the next turn and corrects via `update_*` / `supersede_*` if the change matters. Accepting this drift is intentional — transactional rollback across primitives is more complex than it's worth, and the agentic-search dedup pattern (search before writing) self-corrects over time.

---

## In-Person Redirection

The assistant actively recognizes moments where direct conversation between the partners is the right tool, and redirects rather than mediating. Triggers:

- Charged content where face-to-face matters (apologies, big news, emotional repair)
- Recurring tension that hasn't moved despite multiple mediated touches — assistant becoming substitute, not scaffold
- Logistical decisions that don't need mediation
- "Tell her X" requests for things the user could just say directly
- Genuine connection moments — "this sounds like something to share with her tonight"

Active behavior in the system prompt. The assistant should want to make itself less necessary over time.

---

## Tools

All write operations log to `tool_calls`. Read operations log to app logs. Full Pydantic input/output schemas live in `tool_schemas.py` — single source of truth for the orchestrator validation layer and the Anthropic tool-list payload.

**Dedup via agentic search, not embeddings.** Before writing a memory or observation, the agent reads existing rows via the relevant retrieval tool (`get_memories`, `get_observations`) and decides whether to add a new row, update an existing one (`update_memory` / `update_observation`), or supersede (`supersede_memory`). Write tools themselves don't dedup — they trust the agent has already searched. The system prompt makes this explicit: "search before writing; reinforcing an existing observation is `update_observation`, not a new `log_observation`."

No vector store, no embedding model, no similarity threshold to tune. The cost is one extra read round per write turn; the agent was going to do that anyway under the spec's "ground in data" principle.

**Read:**
- `search_messages(partner?, date_range?, text_contains?)`
- `recent_activity(days)` — recent messages summary across both threads
- `list_themes(active_only?, sort_by?)` / `get_theme(theme_id)`
- `get_memories(about_user_id?, status?, theme_id?)`
- `list_watch_items(owner_user_id?, status?)`
- `get_observations(theme_id?, status?, about_user_id?, min_significance?)`
- `get_oob(owner_id?)`
- `check_oob(content, recipient_id)` — auto-run on every outbound
- `get_self_model(user_id)` — combined: style notes, memories about them, active themes they're in, observations about them
- `get_bot_actions(date_range?, target_type?)` — for "why did you tell her that?" queries

**Write:**
- `update_user_style_notes(user_id, notes)`
- `add_memory(...)` / `update_memory(...)` / `supersede_memory(old_id, new_content)`
- `create_theme(...)` / `update_theme(theme_id, ...)`
- `add_watch_item(...)` / `update_watch_item(...)` / `address_watch_item(id, addressing_note)`
- `log_observation(...)` / `update_observation(...)`
- `add_oob(...)` / `update_oob(...)` / `lift_oob(oob_id)`
- `schedule_checkin(when, about_what, reason)` — supersedes any existing pending check-in for that user
- `cancel_scheduled_checkin(user_id)`
- `escalate_to_partner(content, reason)` — high bar, for crisis or genuinely time-sensitive concerns
- `log_feedback(from_user_id, target_type, target_id, sentiment, content)`

---

## Triggers

**Inbound:**
- Standard message (debounced)
- Voice note (transcribe via Groq first)
- Image (analyze via OpenAI vision first)
- Other media (assistant decides whether to engage or ask)
- Edit and delete webhooks

**Scheduled (DB-backed):**
- **Weekly summary per partner** — minimal: conversation count, themes touched, invitation to ask. Also performs decay housekeeping.
- **Variable check-in** (4–24h, assistant-decided), one pending per user max. Supersedes existing pending check-in for that user.
- **Watch item due** — when a watch item's `due_at` arrives, surfaces it next time the owner is in conversation, or schedules a check-in.
- **OOB review** — when `review_at` hits, asks the owner if it's still off-limits.

**Cross-partner:**
- Sharing knowledge across threads is opportunistic — when one partner asks about the other or about a shared theme, the bot's hot context includes relevant memories/observations and OOB-checks all outbound. No proactive cross-partner messaging beyond the weekly summary and escalation tool.
- Escalation (`escalate_to_partner`) — high bar; for crisis or genuinely time-sensitive concerns.

**Manual / command (natural language, no slash commands except pause):**
- `/pause` and `/resume` — symmetric across both users
- OOB management ("don't bring up X")
- Direct queries ("anything I should think about?", "what do you know about me?", "why did you tell her that?", "why has she been quiet?")
- Corrections ("that's not what she meant") — bot re-examines source data
- Manual state actions ("this is resolved", "remind me about X next week", "open a new theme around Y")
- Deliberate relay ("tell her I'm sorry about earlier") — bot recognizes as relay; passes faithfully, clarifies framing, or redirects IRL

---

## Onboarding

Light, not a questionnaire.

1. **Welcome message.** Warm, explanatory. Sets expectations: I'm a reflection and mediation assistant, not a therapist; I'll get things wrong at first; please correct me; here's how the basics work.
2. **Natural early seeding.** In the first few conversations, the assistant gently asks one or two contextual questions when natural ("anything you'd want me to be careful about from the start?", "how do you tend to process things?"). Seeds style notes, memories, OOB through ordinary conversation rather than intake.
3. **Acknowledge thin context.** During the first weeks, the assistant is upfront about limited context and asks accordingly rather than fabricating depth.

---

## Agentic Loop

Per inbound message:

1. **Orient** — read trigger context, hot context (both users' style notes, active themes, recent memories, open watch items, active high-significance observations, OOB, recent ~20 messages, prior assistant messages, conversation gap if any).
2. **Read** — call retrieval tools as needed. Multiple rounds expected. Search before assuming. For questions about own past actions, query `get_bot_actions` rather than relying on memory.
3. **Decide** — respond (which may include redirecting to in-person) or stay silent. Silence must be justified in `bot_turns.reasoning` for charged messages.
4. **Respond** — produce message; passes through OOB guardrail.
5. **Write** — using the five-primitive decision framework: update style notes, add/update memories, create/update themes, add/address watch items, log observations, log feedback if expressed conversationally. Multiple primitives can apply to one piece of information.
6. **Schedule** — decide whether to schedule one follow-up check-in (4–24h window) or none. Supersedes any existing pending check-in.
7. **End turn explicitly.**

No hard cap on tool calls or wallclock per turn — daily LLM spend caps are the budgetary backstop. `bot_turns` records tool count and duration per turn for admin visibility into pathological loops, but nothing enforces termination beyond the spend cap.

---

## Hot Context (per LLM invocation)

- System prompt (static, includes definitions and significance anchors)
- Both users' profiles + style notes
- All active OOB entries with severity
- Active memories about both users (concise list)
- Active themes (titles + 1-line status, max ~10), weighted by recency
- Open watch items relevant to current user
- Active high-significance observations (significance ≥3)
- Last ~20 messages in current user's thread (including prior assistant messages)
- Time gap since last message in this thread
- Trigger metadata (what caused this run)

Cold context retrieved via tools.

---

## Prompts

**Main system prompt (one mega-prompt for the conversational loop):**
- Role: relationship reflection and mediation assistant; not a therapist; borrows from NVC, Gottman observations, IFS parts language, reflective listening, curiosity-first stance, repair-attempt surfacing, externalizing the problem
- Identity: assistant name [TBD], operating between two named partners
- Operating principles: ground in data, distill but quote when wording carries information, attribution clarity, default transparency with OOB exceptions, symmetry between partners, epistemic humility
- Definitions section (crisis, charge levels, confidence levels, theme abstraction level, what "addressing" a watch item means)
- Stance on assessments: honest observations without pathology language; describe behavior, not personalities or conditions; hold observations as testable
- The five-primitive decision framework with discriminators and examples
- OOB rules and severity handling, including countersummary language guidance
- Tool usage philosophy: read → reason → respond → write → optionally schedule → end. Search before guessing. For "what did you do" questions, query audit trail.
- Each tool: when to use, when not to, examples
- Multi-message handling: weave continuity dynamically, acknowledge gaps naturally
- In-person redirection: actively recognize when direct conversation is the right tool, redirect gently
- Crisis handling: drop mediator role, respond with care, surface resources. May escalate via `escalate_to_partner`.
- Refusal patterns: not a weapon, not a substitute for direct talk when direct talk is appropriate
- Output style: private-chat conversational, warm, brief by default
- Silence acceptable; must justify on charged messages
- Honesty when nothing significant: willing to say "honestly, things seem fine"
- Deliberate relay handling: recognize "tell her X" as relay; pass faithfully, clarify framing, or redirect IRL

**Significance scoring prompt (application-layer call, Haiku):**
- Focused: given content + context, return 1–5 with brief reason
- Includes the five anchor examples inline
- Versioned via `scoring_prompt_version` field

**Weekly summary template (Meta-approved):**
- Parameters: name, conversation count, themes touched count
- "Hi {{1}}, this week we had {{2}} conversations and touched on {{3}} ongoing things. Want to talk through anything? Just ask."

**Escalation template (Meta-approved):**
- Parameters: recipient name, partner name, brief context line
- "Hi {{1}}, this is your assistant. {{2}} has shared something I think is worth your attention soon. They haven't asked me to share specifics — when you're ready, please reach out to them directly. {{3}}"
- The `{{3}}` parameter is a vague contextual line the bot fills in (e.g., "They're going through a hard moment" or "Something significant happened today"). Intentionally vague to respect privacy and avoid alarming over text — the actual conversation should be in person.
- Used for both "important to know soon" and concerning-but-not-acute cases. For genuine acute crisis, the bot also surfaces emergency resources to the affected partner directly.

---

## Voice Notes

- Audio fetched from WhatsApp media endpoint
- Sent to Groq Whisper for transcription
- Both audio URL and transcript stored on the message row
- LLM receives transcript + metadata ("voice note, 2:47 runtime") to infer tone from word choice and pacing markers
- **Retention:** indefinite. Audio binary stays in Supabase Storage until the user explicitly deletes the underlying message.

**Failure mode:** Transcription failure → audio is stored, transcript field stays null, message has `processing_state = 'expired'`. Bot acknowledges with "I couldn't transcribe that — can you send it as text or try again?" If transcription fails twice on the same audio, give up and tell the user.

---

## Images

- Image fetched from WhatsApp media endpoint
- Sent to OpenAI vision API for analysis
- Image binary stored in Supabase Storage (non-public); analysis text stored on message row in `media_analysis`
- Conversational LLM receives the analysis text as context (not the image directly)
- Assistant uses judgment: trivial images (memes) acked without deep analysis; meaningful images (screenshots, photos with context) analyzed and woven in
- **Retention:** indefinite. Image binary stays in Supabase Storage until the user explicitly deletes the underlying message.

**Failure modes:**
- Vision API failure → image stored, `media_analysis` null, bot says "I'm having trouble seeing that — can you describe it?"
- Vision daily cap hit → image stored, `media_analysis` null, bot is told in context "image analysis unavailable today (cap)" and falls back to "I see you sent an image — can you describe it?" Conversational handling, not a hard error.

---

## LLM API Failures

- Conversational loop timeout or error → retry once with exponential backoff. If still failing:
  - For charged or crisis messages → send "I'm having trouble responding right now, give me a moment" and queue for retry
  - For routine messages → queue silently, retry within 5 minutes
- Outbound transport send failure → retry with exponential backoff up to 3 attempts. After that, log to `bot_turns.reasoning` and surface in admin view. Don't silently swallow failures.
- Significance scoring failure → write the observation without a score; flag for re-scoring during weekly housekeeping

---

## Crisis Handling

When the bot detects crisis content (per definitions above):

- Drops the mediator role entirely
- Responds as a caring presence; supportive, present, non-clinical
- Surfaces resources (LLM uses common sense for region/language-appropriate)
- May call `escalate_to_partner` if genuinely warranted — concrete gate below
- Escalation produces a templated outbound to the other partner if outside the 24-hour WhatsApp window

**`escalate_to_partner` gate (concrete):** the tool fires only when one of the following is true, and the calling reason names which:
1. The triggering message meets the spec's `crisis` charge definition (self-harm ideation, imminent danger, abuse, severe acute distress).
2. The user explicitly asks the bot to alert their partner ("tell her something's wrong," "let him know I'm in a bad place").

The tool input requires an explicit `is_crisis: bool` flag and a `reason` string identifying which trigger fired. Anything else — intense friction, recurring tension, the user being upset — is **not** a valid escalation trigger and the bot stays in mediator role. Every escalation is logged distinctly on `bot_turns.reasoning` and surfaced in the admin view for retro audit.

---

## Feedback Loop

Conversational feedback ("that was helpful," "no, that's wrong") recognized and logged via `log_feedback` tool. Reactions on assistant messages are logged as feedback when supported by the active transport.

Feedback informs prompt iteration by the spec author.

---

## Operations

- **Monitoring:** uptime check + error rate alert (cron pinging health endpoint)
- **Heartbeat:** daily "I'm alive" log to detect silent failures
- **Backups:** Supabase automated; tested restore path before launch
- **Admin view:** simple password-protected page showing recent turns, recent messages, themes, memories, watch items, observations, scheduled jobs — for debugging without writing SQL
- **Staging path:** ability to test prompt changes against past message logs without sending output
- **Idempotency:** transport message IDs as dedup keys
- **Restart safety:** messages persisted on webhook receipt; recovery routine resumes unprocessed messages on restart; scheduled jobs DB-backed
- **Audit:** all turns and write tool calls logged; both partners can ask "why did you tell her that?" via `get_bot_actions`
- **Pause kill switch:** fully halts processing for both users symmetrically (see Pause / Resume)
- **Daily LLM spend caps with alerts:** separate caps for text (Anthropic), vision (OpenAI), transcription (Groq); see Architecture for starting numbers

---

## Scheduled-Job Timezones

All scheduled jobs that target a specific user fire in that user's local timezone (`users.timezone`). The recurring weekly summary fires at 09:00 local on the user's configured day. Variable check-ins (`schedule_checkin`) are stored as absolute UTC instants — the loop is responsible for converting from "in 8 hours" or "tomorrow morning" to UTC at write time using the owner's timezone. Jobs that don't belong to a single user (e.g. heartbeat) use UTC.

---

## Worked Example: One Turn

This is the canonical end-to-end shape of a single inbound message becoming an outbound, with concrete tool calls and DB rows. Use it as a contract test target.

**Setup:** Maya and Ben are the two partners. Maya messages the bot at 21:14 local: *"she didn't ask how my day went tonight, again."*

**1. Webhook → persistence.** Inbound row written to `messages` with `direction='inbound'`, `sender_id=maya.id`, `processing_state='raw'`, `whatsapp_message_id` for dedup. Burst debounce timer starts (10s).

**2. Debounce expires.** No follow-up messages. Single-message burst handed to `run_agentic_turn([msg_id], maya)`.

**3. Loop opens.** A `bot_turns` row is created: `triggered_by_message_id=msg_id`, `user_in_context=maya.id`, `system_prompt_version='v1'`, `model_version='claude-sonnet-4-6'`, `started_at=now()`, `prompt_snapshot=<full prompt>`. `processing_state` on the message flips to `processed`.

**4. Hot context built.** Maya's style notes, Ben's style notes, both users' active memories, ~10 active themes (titles + status), open watch items for Maya, observations with significance ≥ 3, last 20 messages in Maya's thread, and active OOB entries for both partners are loaded into the prompt.

**5. Read rounds.** The agent makes 3 tool calls:
- `get_observations(about_user_id=ben.id, status='active', min_significance=2)` → finds an existing observation: *"When Ben is mid-deadline, he goes quieter at home — Maya often reads it as disengagement."* (id `obs_4d2`, significance 3, last reinforced 11 days ago).
- `search_messages(partner_user_id=maya.id, text_contains='asked how', limit=20)` → 2 prior hits in the last 6 weeks where Maya raised the same pattern.
- `list_themes(active_only=True)` → confirms an active theme `"Navigating their different communication styles"` (id `thm_a01`).

**6. Decide.** The agent chooses to respond reflectively, name the existing pattern lightly without pathologizing, and ask whether Maya wants to talk through it or just vent.

**7. Draft outbound.** *"That landing again stings — especially given how often it shows up the same way. Last time we talked about this you mentioned Ben's been heads-down on the launch. Want to think out loud about it, or is this more of a 'just need to say it' moment?"*

**8. `check_oob` runs.** Separate Sonnet call against Ben's active OOB entries (Ben is the topic of the message). Returns `{verdict: 'ok', reason: '...', triggering_oob_ids: []}`. Pass.

**9. Outbound sent.** `send_outbound(maya, draft, template_fallback=None)` — Maya messaged in 0 seconds ago, well within the 24h window, so free-form. New `messages` row written with `direction='outbound'`, `recipient_id=maya.id`, `in_reply_to=msg_id`. WhatsApp send succeeds.

**10. Writes.**
- `update_observation(obs_4d2)` — bumps `last_reinforced_at` because Maya raised the same pattern again. The agent did NOT call `log_observation` because step 5 found the existing row. (This is the agentic-search dedup pattern.)
- `add_watch_item(owner_user_id=maya.id, content="Maya raised the 'didn't ask about my day' pattern again — check in if it surfaces a third time within a month", due_at=now+14d, related_theme_ids=[thm_a01])`.
- No memory write (no new fact).
- No theme update (existing theme is fine).
- One `tool_calls` row per write tool, with `turn_id`, `arguments`, `result`, `duration_ms`.

**11. Schedule.** Agent calls `schedule_checkin(user_id=maya.id, when=now+18h, about_what="follow-up on the disconnection she raised tonight", reason="charged content + recurring pattern, want to see if mood shifted by morning")`. Any prior pending check-in for Maya is marked `superseded`. New `scheduled_jobs` row inserted.

**12. End turn.** `bot_turns` row updated: `final_output_message_id`, `reasoning="Reflected the recurring pattern without pathologizing; asked Maya which mode she wanted; reinforced existing observation rather than creating new; scheduled morning check-in."`, `tool_call_count=5`, `duration_ms=~4200`, `completed_at=now()`.

**Total LLM calls this turn:** 1 conversational Sonnet (multiple turns inside the SDK's tool-use loop counted as one billable session per tool round) + 1 `check_oob` Sonnet. No Haiku scoring this turn (no new observation written).

---

## Open Decisions Before Build

- Assistant's name and personality calibration
- Welcome message exact copy
- Weekly summary timing per partner
- **Whether your wife is a co-designer or recipient of the system** — the most important non-technical decision. Recommendation: open with a conversation, not a doc. Frame as exploration, listen to her concerns, design together.

## Resolved Decisions

- **WhatsApp 24h window:** handled via `send_outbound` helper + five approved templates (see WhatsApp 24-Hour Window section).
- **Tool I/O contracts:** Pydantic schemas in `tool_schemas.py`, single source of truth for orchestrator validation.
- **Write-tool dedup:** agentic search (agent reads before writing); no embeddings, no vector store. See Tools section.
- **Theme creation:** agent judgment, no hard threshold. See Themes section.
- **`check_oob` model & failure mode:** separate Sonnet call; fail closed for firm/hard, fail open for soft. See Out-of-Bounds Guardrail.
- **Loop budget:** no per-turn cap; daily spend caps are the backstop; per-turn tool count and duration logged on `bot_turns`.
- **Edits/deletions:** derivative writes are not retracted; bot reasons forward and corrects via `update_*` / `supersede_*` if needed. See Edits and Deletions.
- **`escalate_to_partner` gate:** crisis charge OR explicit user request, with `is_crisis` flag and named reason. See Crisis Handling.
- **Voice/image retention:** indefinite, until message is deleted. See Voice Notes / Images.
- **Scheduled-job timezones:** owner-bound jobs in `users.timezone`; `schedule_checkin` stores absolute UTC. See Scheduled-Job Timezones.
- **`recency_weighted_score`:** `significance / (1 + age_days / 60)`. See Decay.
- **`system_prompt_version` on `bot_turns`:** added as a column. See Data Model.
- **Worked example:** canonical one-turn walkthrough as contract target. See Worked Example: One Turn.
- **Eval harness:** new Plan 7 (light) — 15-25 scripted scenarios with expected primitives + outbound assertions, runnable in CI on prompt changes.
