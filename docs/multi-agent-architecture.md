# Multi-Agent Architecture

Plan for evolving from a single mediator bot into a platform hosting many agents — each with its own identity, prompt, tool surface, participants shape, and data partition — while letting agents intentionally reach across partitions when it matters.

Design briefing. Two verification passes against the codebase are preserved as §19 (Codex read-only) and §20 (Codex gpt-5.5 high adversarial). The body integrates their findings as the actual design. Next step will be refactoring into phase sprints.

---

## 1. Core mental model

```
Channel (any addressable inbound surface)
  └─ Bot           (identity, prompt, tools, participants shape, persona)
        └─ Binding (which user or dyad this bot serves)
              └─ Topic   (the data partition; primary topic per bot)
                    └─ Knowledge artifacts (memories, themes,
                                            watch items, observations,
                                            distillations, OOB, status, …)
```

First-class concepts:

- **Channel** — transport surface a bot listens on (Discord bot account, phone number, future). Resolves inbound to a bot. No 1:1 assumption.
- **Bot** — agent identity with code-defined behavior. Has a *participants shape* (solo or dyad) and a primary topic.
- **Binding** — which user(s) a bot serves. Solo bot binds to one user; dyadic bot binds to a dyad.
- **Topic** — the data partition for accumulated knowledge.

Two principles drive everything:

1. **Partition = topic, attribution = bot.** Knowledge artifacts filter by topic; who recorded what is a separate column.
2. **Scope must be enforced, not requested.** Every tool call carries scope on `TurnContext`; every read and write is validated against the bot's authorization at the tool boundary, not by trusting the model.

---

## 2. The two scoping axes

The codebase already has a non-trivial scoping axis ("about whom"); multi-agent introduces a second, orthogonal axis ("topic"). They compose.

### Axis A: about-whom (existing)

| Table | About/owner column | Default mode |
|---|---|---|
| `memories` | `about_user_id` nullable | NULL = shared/pair |
| `observations` | `about_user_id` nullable | NULL = shared/pair |
| `watch_items` | `owner_user_id` NOT NULL | per-user |
| `out_of_bounds` | `owner_id` NOT NULL | per-user |
| `themes` | (no user FK) | pair-only-by-definition |
| `distillations` | `source_user_ids[]` | provenance (different axis) |

Four modes:

1. **Per-person** — owned by/about exactly one user.
2. **Shared** — about both users, represented by NULL on a nullable FK.
3. **Pair-only-by-definition** — themes have no user concept; always shared.
4. **Provenance** — distillations have `source_user_ids uuid[] NOT NULL`. This is *which users' material this conclusion was derived from*, not who it's about. A distinct axis. See §19.1.

For a **solo bot** axis A collapses (always the one bound user); for a **dyadic bot** it operates as today.

### Axis B: topic (new)

Every multi-topic artifact has a row in a **join table** `artifact_topics` rather than an array column:

```sql
CREATE TABLE artifact_topics (
  artifact_table   text NOT NULL,                 -- 'memories' | 'observations' | …
  artifact_id      uuid NOT NULL,
  topic_id         uuid NOT NULL REFERENCES topics(id),
  tagged_by_bot_id text NOT NULL REFERENCES bots(id),
  reason           text,
  status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
  created_at       timestamptz NOT NULL DEFAULT now(),
  retired_at       timestamptz,
  PRIMARY KEY (artifact_table, artifact_id, topic_id)
);
CREATE INDEX ON artifact_topics (topic_id, artifact_table) WHERE status='active';
CREATE INDEX ON artifact_topics (artifact_table, artifact_id) WHERE status='active';
```

Why join table, not array (reversed from earlier draft — see §20.10):

- **Audit fidelity.** Who tagged this topic, when, why, can be answered for every row.
- **Per-topic lifecycle.** Retiring "career" relevance while keeping "relationship" relevance is a single row update with a retirement timestamp — not an opaque array mutation.
- **Authorization at write time.** Every multi-tag has an associated bot; cross-bot tagging is structurally identifiable.

Single-topic tables (`messages`, `bot_turns`, `scheduled_jobs`, `feedback`, `bridge_candidates`, `topic_status`) keep an inline `topic_id` column — there's one answer and no need for a join.

### Composition

For multi-topic artifacts: scope = (about-whom column) × (join table membership).

For a dyadic bot reading `own` topic memories about Maya:

```sql
SELECT m.*
FROM memories m
JOIN artifact_topics at
  ON at.artifact_table = 'memories' AND at.artifact_id = m.id
WHERE at.topic_id = $bot_primary_topic
  AND at.status = 'active'
  AND m.about_user_id = $maya_id;
```

---

## 3. What gets a topic

| Table | Topic scope | About-whom | Bot attribution | Notes |
|---|---|---|---|---|
| `users` | — | self | — | Person is global. |
| `users.style_notes` | — | self | — | Global trait. |
| `memories` | join `artifact_topics` | `about_user_id` nullable | `recorded_by_bot_id` | Multi-topic. NULL = shared. |
| `themes` | join `artifact_topics` | (none) | `recorded_by_bot_id` | Multi-topic. Pair-only. |
| `observations` | join `artifact_topics` | `about_user_id` nullable | `recorded_by_bot_id` | Multi-topic. NULL = shared. |
| `watch_items` | join `artifact_topics` | `owner_user_id` NOT NULL | `recorded_by_bot_id` | Multi-topic. Per-user. |
| `distillations` | join `artifact_topics` | `source_user_ids[]` (provenance) | `recorded_by_bot_id` | Multi-topic; fourth scoping shape. |
| `out_of_bounds` | join `artifact_topics` nullable | `owner_id` NOT NULL | — | If no rows in join table = global OOB. Otherwise topic-scoped. |
| `bridge_candidates` | `topic_id` + `bot_id` + `dyad_id` (single) | `source_user_id`/`target_user_id` | `proposed_by_bot_id` | Dyadic-only. Needs all three scoping columns — see §3.1. |
| `messages` | `topic_id` single | sender/recipient | `bot_id` | One row, one bot, one topic. |
| `bot_turns` | `topic_id` single | `user_in_context` | `bot_id` + version pins | See §11.4 for audit columns. |
| `tool_calls` | inherited via `turn_id` | inherited | inherited | No new columns. |
| `scheduled_jobs` | `topic_id` single | `user_id` nullable | `bot_id` | `scheduled_task` is a `job_type` enum, not a separate table. |
| `feedback` | `topic_id` single | `from_user_id` | `bot_id` | Reaction targets one bot. |
| `topic_status` (new) | `topic_id` + `dyad_id` nullable | `user_id` nullable | `last_updated_by_bot_id` | NULL `user_id` + `dyad_id` set = pair-level status. See §7. |

**Rule of thumb:** if the row records *what happened* (turn, message, job, feedback, bridge proposal), single-topic. If the row records *something known about a user or pair* (memory, theme, observation, watch item, distillation, OOB), multi-topic via the join table.

### 3.1 Bridge candidates: bot_id + topic_id + dyad_id, all three

Verified structurally dyadic (§19.2). With multi-bot:

- `dyad_id` — prevents cross-dyad leakage.
- `topic_id` — a coach surfacing a bridge in the career topic must not be picked up by the mediator scanning the relationship topic.
- `bot_id` — a bot's bridge queue is its own. Two bots serving the same dyad in the same topic (unusual but possible) maintain separate queues.

All three columns are added in phase 0.

### 3.2 Themes — leave user-FK-less

Themes today have no user FK. For multi-bot, leave as-is: a solo bot's themes are implicitly about that single user; a dyadic bot's themes are pair-level. Add `about_user_id` later only if a real use case appears.

---

## 4. Why topic-scoped, not agent-scoped

- Topic is the **natural unit of meaning**.
- **Topic outlives bot.** Version/replace/rename a bot, data doesn't move.
- **Future flexibility.** Two bots can share one topic without migration.
- **Mirrors user mental model.**

Bot-scoping is preserved as `recorded_by_bot_id` for attribution. The doc treats partition (topic) and attribution (bot) as different concerns.

---

## 5. Bot participants shape and bindings

### Participants shape

```python
participants_shape: Literal["solo", "dyad"]   # future: "group"
```

- **Solo**: serves one user. `about_user_id` always that user. No partner concept. No pair-level status.
- **Dyad**: serves two users. Each turn picks `user_in_context` (whoever just messaged). Writes can specify `about_user_id = A`, `B`, or `NULL` (shared). Pair-level status supported.

This single config decides whether the bot needs partner handling, shared-mode artifacts, dyadic crisis escalation, bridge candidates.

### Enforcement, not aspiration (§19.3, §19.14)

"Solo bot never picks shared" is not an invariant unless tools reject illegal scopes. Tool entry points validate against `TurnContext.participants_shape` and `TurnContext.binding`:

- Solo bot calling a write with `about_user_id != bound_user.id` → rejected.
- Solo bot calling a write with `about_user_id = NULL` (shared) → rejected.
- Dyadic bot calling a write with `about_user_id` outside the bound dyad → rejected.

Tool registry has *participant-shape-specific contracts*. Bridge tools, in-person redirect, partner-perspective tools are only registered for dyadic bots — not "registered everywhere and gated by prompt language." Solo bots literally cannot call them.

### Bindings (data)

```sql
CREATE TABLE dyads (
  id          uuid PRIMARY KEY,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE dyad_members (
  dyad_id   uuid NOT NULL REFERENCES dyads(id),
  user_id   uuid NOT NULL REFERENCES users(id),
  PRIMARY KEY (dyad_id, user_id)
);

CREATE TABLE bot_bindings (
  id         uuid PRIMARY KEY,
  bot_id     text NOT NULL REFERENCES bots(id),
  user_id    uuid REFERENCES users(id),   -- set for solo
  dyad_id    uuid REFERENCES dyads(id),   -- set for dyad
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((user_id IS NOT NULL) <> (dyad_id IS NOT NULL))
);
```

The current implicit "dyad in settings" becomes an explicit `dyads` row in migration. Adding a second bot is then a new `bot_bindings` row.

### Defaults on writes

- **Solo bot**: `about_user_id` defaults to the bound user, validation enforces it.
- **Dyadic bot**: `about_user_id` defaults to `user_in_context`. Agent can explicitly set partner or NULL when warranted, subject to authorization (§6).

---

## 6. Scope on `TurnContext` and the authorization model

The most under-specified surface in the prior drafts. Codex flagged both (§19.4, §19.15). This section is the spine.

### 6.1 What `TurnContext` carries

Today (`app/services/turn_context.py:16`): `user`, mandatory `partner`, message ids, hot context text, OOB owners. Adds:

```python
@dataclass(frozen=True)
class TurnContext:
    # existing
    user: User
    partner: User | None           # None for solo bots
    triggering_message_ids: list[UUID]
    hot_context: str
    protected_owner_ids: list[UUID]
    # added
    bot_id: str
    bot_spec: BotSpec
    binding_id: UUID
    participants_shape: Literal["solo", "dyad"]
    primary_topic_id: UUID
    primary_topic_slug: str
    channel_id: UUID
    cross_topic_policy: Literal["peek", "explicit", "forbidden"]
    read_scopes: ReadScopes        # see §6.3
    write_scopes: WriteScopes      # see §6.3
```

Every tool call sees this. Every authorization check uses fields on this object, never `settings` and never the model's word.

### 6.2 Resolution at turn start

In the inbound flow (§13.1), the runtime resolves: channel → bot → binding → primary topic → bot_spec → scopes. The fully-populated `TurnContext` is constructed once and passed to the agentic loop. No tool re-resolves any of this; no tool consults `settings`.

### 6.3 Authorization model

Each `BotSpec` declares scopes explicitly:

```python
@dataclass(frozen=True)
class ReadScopes:
    topics: frozenset[str] | Literal["own", "all"]  # own = primary only
    allow_cross_topic_peek: bool                    # status counts of other topics
    allow_cross_topic_status_injection: bool        # see §7.4

@dataclass(frozen=True)
class WriteScopes:
    topics: frozenset[str]                         # always contains primary
    require_reason_for_cross_topic: bool           # logged on write
```

The mediator launches with `read_scopes = {"own", peek=True, status=True}` and `write_scopes = {"relationship"}`. A coach launches with `{"career"}`/`{"career"}` and `peek=True`. Cross-topic writes (a coach writing to the relationship topic) require an explicit `write_scopes` widening and a `reason` field on the write — which is recorded in `artifact_topics.reason`.

This replaces the previous "agent can fully override `topic_ids`" rule, which was unconstrained write authority masquerading as flexibility (§19.11).

### 6.4 Enforcement points

- **Read tools** consult `ctx.read_scopes` before filtering.
- **Write tools** consult `ctx.write_scopes` before INSERT. Cross-topic writes require non-empty `reason`.
- **Status injection** in hot context consults `ctx.read_scopes.allow_cross_topic_status_injection`. A `forbidden` bot does not see other topics' statuses in its prompt at all (§19.13).
- **`consult_perspective`** clones `ctx` *preserving scopes*. Consult inherits the same authorization (§19.7).

---

## 7. Topic status

Short headline + paragraph the agent maintains. Visible in every bot's system prompt (when authorized).

```sql
CREATE TABLE topic_status (
  id                       uuid PRIMARY KEY,
  topic_id                 uuid NOT NULL REFERENCES topics(id),
  dyad_id                  uuid REFERENCES dyads(id),    -- set when pair-level
  user_id                  uuid REFERENCES users(id),    -- set when per-user
  headline                 text NOT NULL,
  body                     text NOT NULL,
  last_updated_at          timestamptz NOT NULL DEFAULT now(),
  last_updated_by_bot_id   text NOT NULL REFERENCES bots(id),
  CHECK ((user_id IS NOT NULL) <> (dyad_id IS NOT NULL))
);
CREATE UNIQUE INDEX topic_status_user_key
  ON topic_status (topic_id, user_id) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX topic_status_dyad_key
  ON topic_status (topic_id, dyad_id) WHERE dyad_id IS NOT NULL;
```

### 7.1 Pair-level needs `dyad_id` (§19.12)

Per `(topic, user)` with NULL=pair only worked if there were exactly one dyad. With multiple dyads the pair-level rows collide. Pair-level rows are now keyed by `(topic_id, dyad_id)`.

### 7.2 Solo vs dyadic write rules

- **Solo bot**: writes per-user statuses only, for its bound user.
- **Dyadic bot**: writes per-user statuses for either partner *and* pair-level status (`dyad_id` set, `user_id` NULL).

### 7.3 The tool

```
set_topic_status(scope, headline, body)
  scope = "user:<id>" | "pair"
```

Topic is implicit from `ctx.primary_topic_id` (a bot can only update its own topic's status). Rejected at tool boundary if scope doesn't match bot shape.

### 7.4 Status injection is policy-gated (§19.13)

Hot context normally injects all topic statuses for served user(s) and pair. But this respects `ctx.read_scopes.allow_cross_topic_status_injection`:

- Bot with `peek` or `all`: sees its own topic's statuses + cross-topic statuses.
- Bot with `forbidden`: sees only its own topic's statuses.

Status headlines will become tiny summaries of sensitive domains; verbatim cross-topic leakage is the exact failure mode the policy must guard.

### 7.5 Status updates folded into `record`

The record-step instruction prompts the agent to call `set_topic_status` when the topic's status no longer reflects current reality. No new turn phase. Promote to its own phase only if telemetry shows agents forgetting.

### 7.6 History deferred

`topic_status_history` (append-only) added in a later phase when there's a UI or audit need for it.

---

## 8. Tool scope and write authorization

### 8.1 Reads

Every read tool grows an optional `scope` argument:

```python
scope: Literal["own", "all"] | str = "own"   # "topic:<slug>"
```

Resolution against `ctx.read_scopes`:

- `"own"` (default): filter by `ctx.primary_topic_id` via `artifact_topics`.
- `"all"`: allowed only if `ctx.read_scopes.topics == "all"`.
- `"topic:<slug>"`: allowed only if slug is in `ctx.read_scopes.topics` or `topics == "all"`.

Cross-topic reads are **explicit**, never auto-expanded on empty results. The agent opts in and the reason appears in `tool_calls.arguments`.

### 8.2 Writes — gated by `write_scopes` (§19.11)

Writes accept `topic_slugs: list[str]` defaulting to `[ctx.primary_topic_slug]`. The set is intersected with `ctx.write_scopes.topics` at the tool boundary; if non-trivially intersected (the agent asked to write to a topic it's not authorized for), the tool rejects.

Cross-topic writes (any topic other than primary) require a non-empty `reason`. The reason is stored in every created `artifact_topics` row.

This is the durable rule: **default own topic, allow configured cross-topic writes, record why every cross-topic tag exists.** Not "agent freely overrides."

### 8.3 The cross-topic peek block (in hot context)

For each other topic the served user(s) have activity in, when `peek` is enabled:

```
[topic: career]
  Status (Maya, 3d ago by coach): "Looking for a new role — anxious about timeline."
  Counts for Maya: memories=12, watch_items=3, themes=2, recent_observations=5 (last 14d)
  Counts for Liam: memories=4,  watch_items=0, themes=1, recent_observations=1
```

Rules:

- Counts **per-user**, not pooled.
- Window fixed and stated (proposal: 14 days).
- Status headline verbatim only when `allow_cross_topic_status_injection` is true; otherwise summarized to "status present" or omitted.
- Contents not in the peek — agent must read explicitly.

---

## 9. Cross-topic awareness — why no "bridge" primitive

Status visible in every bot's system prompt + explicit cross-topic reads cover the use case. A coach that wants the mediator to know something updates its own status; the mediator sees it next turn. Push/ack workflows are premature.

The existing within-topic `bridge_candidates` (dyadic cross-partner) is a different concept that happens to share the word. Preserved unchanged, now with `bot_id` + `topic_id` + `dyad_id` columns (§3.1).

---

## 10. The Bot abstraction (code) and the full refactor surface

### 10.1 BotSpec fields

| Currently on `settings` | Moves to `BotSpec` |
|---|---|
| `bot_id` | self |
| `assistant_name` | `bot.assistant_name` |
| `system_prompt_version` | `bot.prompt_version` |
| `STEP_ALLOWED_TOOLS` (global) | `bot.allowed_tools_for_step(step)` |
| `partner_phone_a/b` | resolved via `bot_bindings` + `channels` |
| `messaging_provider` | resolved via channel + transport |

New fields on `BotSpec`:

- `primary_topic_slug`
- `participants_shape`
- `allowed_tools_per_step` — participant-shape specific (§5.2)
- `tool_registry` — solo bots literally don't register dyadic tools (§19.14)
- `read_scopes` / `write_scopes` (§6.3)
- `hot_context_builder` — separate solo and dyadic implementations
- `system_prompt_renderer` — separate solo and dyadic implementations
- `crisis_handler` — per-bot
- `onboarding_renderer` — per-bot first-contact behavior
- `oob_guardrail` — `strict | lenient | off`
- `cross_topic_policy` — `peek | explicit | forbidden`
- `tool_descriptions_override` — escape hatch (see §15)

After this refactor every per-bot decision flows from `bot_spec`. Nothing reads `settings.bot_id`.

### 10.2 Full refactor list (broader than earlier drafts)

Beyond the spec itself, multi-bot reaches into:

- **Burst coalescer** keyed by `(user_id, bot_id)`.
- **Newer-inbound suppression** (§19.5): keyed by `(sender_id, bot_id)` not just `sender_id`. Without this, a finance bot message can cancel the mediator's outbound. Affects `agentic.py:612`, `read_tools.py:166`.
- **Outbound delivery** uses the bot's transport via channel (no inline `messaging_provider` switch).
- **OOB guardrail** at delivery: filter to `bot_topic` membership in `artifact_topics`, or globally-scoped OOB (no rows in `artifact_topics`).
- **Scheduled job dispatcher** resolves `bot_id` from the row, fires that bot's loop.
- **Onboarding** (§19.9): per-bot welcome text, per-bot first-turn behavior. Hard-coded mediator welcome in `inbound.py:23` becomes `bot_spec.onboarding_renderer.welcome()`. `users.onboarding_state` migrates to `user_bot_state(user_id, bot_id, onboarding_state, paused, …)`.
- **Pause/resume** (§19.6): per-(user, bot) in `user_bot_state`. **Global pause is preserved as a separate kill switch** — `system_state.is_paused` stays as the platform-wide brake; the per-(user, bot) state is additive, not a replacement.
- **`consult_perspective`** (§19.7): clones `TurnContext` preserving scopes, primary topic, and policy. Cross-topic reads from consult respect the same authorization.
- **Prompt renderer**: split into solo and dyadic implementations rather than partner-optional one renderer with dead policy.
- **Hot context builder**: split likewise.
- **`TurnContext.partner`** becomes `Optional[User]`; the type change is small, the rewrites in hot context and prompts are not.
- **Telemetry / logs** include `bot_id`, `topic_id`, `binding_id` on every event.
- **Evals**: per-bot suites, no reuse across bots.
- **Tests / fixtures**: take a `bot_spec`; default mediator fixture stays for back-compat.

### 10.3 Audit versioning on `bot_turns` (§19.8)

`bot_turns` adds `bot_id` *and* pinned versions of every load-bearing piece:

- `bot_spec_version` — content hash or version tag of the code-defined spec
- `system_prompt_version` — already exists, kept
- `hot_context_builder_version` — version tag of the builder used
- `tool_schema_version` — global version of `tool_schemas.py`

This is what enables "why did the bot answer this way on 2026-04-12" six months from now.

---

## 11. Code vs data config

**Code** (versioned with source):

- System prompts, step instructions
- Allowed tool sets, tool implementations
- Crisis handlers, hot context builders, onboarding renderers
- `BotSpec` instances

**Data** (changeable without deploy):

- `topics`, `bots`, `channels`, `dyads`, `dyad_members`, `bot_bindings`
- `user_bot_state` (onboarding + pause/resume)
- `user_identities` (§12)

`bots` rows are thin pointers to code-defined `BotSpec`. Ops can register a bot or rebind a channel without code changes; behavior stays in version control.

---

## 12. Channels, routing, and user identity

The doc previously punted user identity to "phase 4+." That was wrong (§19.16, §19.18) — multi-transport breaks identity before the second bot is useful. Identity lands in phase 0/1.

### 12.1 Channels

A **channel** is any addressable inbound surface a bot listens on. Today: a Discord bot account or a phone number. Tomorrow: a Discord mention in a guild channel, a Slack app, etc.

```sql
CREATE TABLE channels (
  id         uuid PRIMARY KEY,
  bot_id     text NOT NULL REFERENCES bots(id),
  transport  text NOT NULL,                      -- 'discord' | 'whatsapp' | …
  address    text NOT NULL,                      -- bot account id, phone number, …
  guild_id   text,                               -- transport-specific
  channel_id text,
  config     jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (transport, address, guild_id, channel_id)
);
```

The routing primitive: `resolve_bot(inbound_event) -> bot_id`. Channels table is the lookup; keys are transport-specific. No code path assumes 1:1.

### 12.2 User identity across transports

Today `users.phone` is reused for Discord ids (`discord.py:176` vs `migrations/0001_init.sql:10`). For multi-bot, a single human may reach the coach via Discord and the mediator via WhatsApp; we need a way to know "same person."

```sql
CREATE TABLE user_identities (
  user_id    uuid NOT NULL REFERENCES users(id),
  transport  text NOT NULL,
  address    text NOT NULL,
  verified_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (transport, address)
);
```

Sender resolution at inbound becomes: `(transport, address)` lookup against `user_identities`, returning `user_id`. The legacy `users.phone` column is migrated to a `(transport='legacy', address=<phone>)` row in phase 0 and the field is no longer load-bearing.

Account-linking (a human verifying "this Discord id and that WhatsApp number are me") is a workflow we don't build yet but the schema is ready for. Until linking happens, two transport identities are two `user_id`s. The risk of writing memories/OOB/feedback under duplicate humans is real and the schema lets us merge later without rewriting encrypted blobs.

### 12.3 Inbound flow

1. Webhook receives event; extract transport identity.
2. `resolve_bot(event) -> bot_id`.
3. Sender resolution → `user_id` via `user_identities`.
4. Resolve `bot_spec`; resolve `binding` from `bot_bindings`.
5. For dyadic: confirm sender is a dyad member; set `user_in_context`.
6. For solo: confirm sender is the bound user.
7. Inbound `messages` row stamped with `bot_id`, `topic_id`, `user_in_context`, `channel_id`.
8. Burst coalesce by `(user_id, bot_id)`.
9. Construct `TurnContext` with all scope fields populated (§6.1).
10. `bot_turns` created with `bot_id`, `topic_id`, version pins.

---

## 13. Tool descriptions

**Decision: genericize.** Tool descriptions say what the tool does ("record a durable fact about a user"); per-bot framing belongs in the system prompt and step instructions. `BotSpec.tool_descriptions_override` exists as an escape hatch; should be unused at first.

This works in combination with §5.2 enforcement: a coach simply has fewer tools registered, rather than the same tool described differently.

---

## 14. Hot context

`build_hot_context` is a **per-bot** callable: `bot_spec.hot_context_builder(...)`. Solo and dyadic builders are separate implementations (§19.14), not one partner-optional builder.

Every builder must include:

- **Status block** for the bot's own topic, for the served user(s) and pair (if dyadic).
- **Cross-topic status block** *only if* `ctx.read_scopes.allow_cross_topic_status_injection` is true.
- **Cross-topic peek block** *only if* `ctx.cross_topic_policy == "peek"` and `ctx.read_scopes.allow_cross_topic_peek` is true.
- **OOB block** filtered to global OOB + own-topic OOB.

Bot-specific:

- **Dyadic builder** bins artifacts into three buckets (about A / about B / shared).
- **Solo builder** has one bucket (about the user).
- Selection, ordering, domain framing per bot.

Style notes are global to the user.

---

## 15. Crisis handling

Pluggable on `BotSpec`:

- `detect_crisis(message_text, context) -> CrisisLevel | None`
- `handle_crisis(level, ctx) -> CrisisAction`

The mediator's existing drop-role behavior is one implementation. A **solo bot** needs its own answer: it can't escalate to a partner. Default for solo bots: refer the user to their dyadic bot (if a binding exists for them), plus surface external resources. Per-bot can override.

---

## 16. Sprint plan

Seven 2-week sprints. One engineer. Every sprint ships to production. The live mediator never breaks. Rollback / deploy mechanics are handled by Megaplan and not separately documented here.

The sprint structure is the executable form of the migration plan; the abstract "phases" from earlier drafts are folded in where they map.

| # | Title | Goal | Key deliverable |
|---|---|---|---|
| 1 | Foundation schema + code shape | Additive tables and code carriers, zero behavior change | All new tables + nullable scope columns; `TurnContext`/`BotSpec` retyped with defaults |
| 2a | Stamp + rekey (nullable) | Insert sites write columns; coalescer/suppression rekeyed with dual-key transition; observability + eval baseline | Every new row scoped per-bot; mediator behavior unchanged; no NOT NULL yet |
| 2b | Constraints + soak gate | NOT NULL after `CHECK ... NOT VALID` + VALIDATE; drop legacy dual-key paths | Schema fully scoped; rollback target locked |
| 3 | `artifact_topics` join cutover | All artifact reads route through join table | Reads partition-safe with one topic — no-op behavior, foundation laid |
| 4 | Authorization + topic status + solo pre-flight | Scope enforcement on `TurnContext`; `topic_status` shipped; solo bot infra provisioned | Mediator governed by explicit scopes; S5 has no infrastructure work |
| 5 | First solo bot | Solo renderer/builder/onboarding/crisis written; new bot in prod | A second bot serving a real user |
| 6 | Multi-topic writes + hardening | Cross-topic write mechanism, lint, runbook | Architecture ready for bot #3 in days, not weeks |

**Three structural decisions baked into this sequence:**

- **Original "phase 1" splits into S1 + S2a + S2b.** Schema and code carriers land first (S1) with defaults that preserve behavior. Insert sites + rekey ship with nullable columns and dual-key transition (S2a). Constraints apply only after a soak with the `CHECK ... NOT VALID` → VALIDATE → NOT NULL pattern (S2b). Isolates each risk class.
- **`artifact_topics` join cutover gets its own sprint (S3).** Doing the join while the mediator is the only consumer is dramatically safer than doing it under a live second bot. This is the single most important do-it-while-it's-a-no-op sprint.
- **Solo bot pre-flight (infrastructure) lives in S4, not S5.** S5 is the riskiest sprint already; loading transport provisioning, secrets, webhook config, and consent into the same sprint as net-new solo prompt and hot context code is what causes the slip. Pre-flight runs in parallel with the authorization work in S4.

### 16.1 Sprint 1 — Foundation schema + code shape

**Goal.** Land every additive table, backfill, and the code skeletons (`TurnContext` fields, `BotSpec` carrier fields, channel/binding/identity resolution helpers) so subsequent sprints only flip switches.

**Pre-flight context.**

- Read `migrations/0001_init.sql`, `0013_bridge_candidates.sql`, `0015_distillations.sql`, `0017_scheduled_tasks.sql` to confirm column shapes against §3.
- Read `app/services/inbound.py:23, 136, 161` and `app/services/messaging.py:55, 84, 155, 338, 370`.
- Confirm `users.phone` backfill semantics for legacy Discord ids: `transport='legacy'` per §12.2.
- Snapshot prod schema (`pg_dump --schema-only`) for rollback comparison.

**Work items, ordered.**

1. Migration `0020_topics_bots_bindings.sql`: `topics`, `bots`, `dyads`, `dyad_members`, `bot_bindings`, `channels`, `user_identities`. Seed `relationship` topic, `mediator` bot, one `dyads` row, two `dyad_members`, one `bot_bindings`, channel rows for current Discord + WhatsApp. Backfill `user_identities` from `users.phone`.
2. Migration `0021_artifact_topics.sql`: create the join table + partial indexes from §2. No FK to each artifact table (polymorphic via `artifact_table` column) — orphan prevention is application discipline; lint catches it in S2a.
3. Migration `0022_topic_status_user_bot_state.sql`: `topic_status` (both partial indexes from §7), `user_bot_state` populated from `users.onboarding_state` for `mediator`.
4. Migration `0023_nullable_scope_columns.sql`: nullable `topic_id`, `bot_id` on `messages`, `bot_turns`, `scheduled_jobs`, `feedback`, `bridge_candidates`; `dyad_id` on `bridge_candidates`; `recorded_by_bot_id` on artifact tables; audit columns on `bot_turns` (`bot_spec_version`, `hot_context_builder_version`, `tool_schema_version`).
5. Migration `0024_backfill.sql`: **cursor-keyed `INSERT INTO artifact_topics SELECT ... WHERE id > last_id ORDER BY id LIMIT 10000` loops with resumability**, per artifact table. Not UPDATEs (the join table is new and empty). Track progress in a small `migration_progress(table, last_id)` table that can resume on restart. Validation queries committed to `migrations/validation/`.

   **OOB backfill rule (resolves §19/§20 ambiguity):** existing `out_of_bounds` rows are all classified as **`relationship` topic-scoped** — they get an `artifact_topics` row pointing at the relationship topic. "No `artifact_topics` rows = global OOB" is a *new mode introduced for the future*; no existing rows use it. Validation query: `SELECT count(*) FROM out_of_bounds WHERE NOT EXISTS (SELECT 1 FROM artifact_topics WHERE artifact_table='out_of_bounds' AND artifact_id=out_of_bounds.id)` must be zero post-backfill.
6. Code: extend `app/services/turn_context.py` with `bot_id`, `bot_spec`, `binding_id`, `participants_shape`, `primary_topic_id`, `primary_topic_slug`, `channel_id`, `read_scopes`, `write_scopes`, `cross_topic_policy` — **all optional with defaults**. Make `partner: User | None`. Existing dereference sites untouched; defaults keep `partner` non-None.
7. Code: extend `app/bots/base.py` BotSpec with the new fields as optional with mediator-shaped defaults. Add `ReadScopes` / `WriteScopes` dataclasses. No call-site changes.
8. Code: new module `app/services/routing.py` — `resolve_bot(event)`, `resolve_sender(transport, address)`, `resolve_binding(bot_id, user_id)`. **Not wired into inbound yet.** Unit tests cover them.
9. Code: `MEDIATOR_BOT` carries new fields populated from DB at startup.

Dependencies: (1) gates (2,3,4); (4) gates (5); (6–9) parallel to migrations as long as no call sites change.

**Mid-sprint checkpoint (day 5–6).**

- All migrations applied to staging clone of prod with full row counts.
- `SELECT count(*) FROM artifact_topics` equals sum of (memories + themes + observations + watch_items + distillations + out_of_bounds) row counts.
- `SELECT count(*) FROM messages WHERE bot_id IS NULL` is zero on the backfilled set.
- `SELECT count(*) FROM user_identities WHERE transport='legacy'` equals `count(*) FROM users WHERE phone IS NOT NULL`.
- Mediator runs against the migrated staging DB with **no code changes** and a clean turn completes end-to-end. **If this fails, the additive migration isn't actually additive — stop and find the FK or trigger that broke.**

**Definition of done.**

- All migrations live in prod. Backfill validated by the OOB query above and equivalent counts per artifact table.
- Mediator behavior verified via **frozen-fixture rendering equality**: deterministic snapshot of DB + frozen `now` → render hot context pre- and post-migration → byte-equal. Live `bot_turns.prompt_snapshot` hashes are not comparable (hot context embeds `now_utc` and relative time labels per `hot_context.py:65, 592` and `time_context.py:98`); production check is **semantic diff on section counts/order/ids**, not prompt-hash equality.
- `TurnContext`, `BotSpec`, `routing.py` shipped with new fields populated by default constructors. No call site references the new fields yet.
- Zero new NOT NULL constraints.

**Risks.**

- Backfilling `artifact_topics` on a hot DB is the largest write of the sprint. Batch + advisory-lock or accept lag. Schedule off-hours.
- `users.phone` → `user_identities` backfill: if any row contains both a phone and a Discord id, both must become identity rows. Audit before backfill.
- `bot_bindings` CHECK constraint mistake (`(user_id IS NOT NULL) <> (dyad_id IS NOT NULL)`) is one typo from blocking inserts. Test the CHECK in CI.

**Deliberately not in this sprint.**

Any insert site writes the new columns. Any NOT NULL. Any read of `artifact_topics`. Any scope check. Any second bot.

### 16.2 Sprint 2a — Stamp + rekey (nullable), observability, eval baseline

**Goal.** Insert sites write the new columns; coalescer and newer-inbound suppression are rekeyed with a dual-key transition; per-bot observability comes online; pre-S3 eval baseline captured. Mediator behavior unchanged. **No NOT NULL.**

**Pre-flight context.**

- Confirm S1 backfill held over a week.
- Exhaustive grep sweep of insert sites, broader than the obvious ones: `inbound.py:161`, `messaging.py:55, 84`, `agentic.py:483`, `agentic.py:577` (deferred jobs), `scheduled_jobs.py:216` (scheduler seeds), `write_tools.py:1367` (scheduled-task writes), every `INSERT INTO memories|themes|observations|watch_items|distillations|out_of_bounds` site. Checklist file local to the sprint.

**Work items, ordered.**

1. Wire `routing.py` into `inbound.py:23-180`. Populate `TurnContext` with real scope fields (still mediator-only defaults).
2. Stamp insert sites:
   - `inbound.py:161` — `bot_id`, `topic_id`, `channel_id`.
   - `messaging.py:55, 84` — same on outbound.
   - `agentic.py:483` — `bot_id`, `topic_id`, `bot_spec_version` (hash of `BotSpec` repr), `hot_context_builder_version`, `tool_schema_version` (hash of `tool_schemas.py`).
   - `agentic.py:577` deferred jobs and `scheduled_jobs.py:216` scheduler seeds — stamp `bot_id`, `topic_id`.
   - `write_tools.py:1367` scheduled-task writes — same.
   - Every artifact write in `write_tools.py`: stamp `recorded_by_bot_id` AND create matching `artifact_topics` row(s) for the bot's primary topic in the same transaction.
3. **Dual-key burst coalescer** (`app/services/debouncer.py`). In-memory dicts keyed by `user_id` today; new code services BOTH `(user_id)` and `(user_id, bot_id)` keys during this sprint. Bursts in-flight at deploy flush under old key; new bursts use the composite key. Drop the legacy reader in S2b.
4. **Dual-key newer-inbound suppression**:
   - `agentic.py:612` (final-send suppression).
   - `read_tools.py:166` (incremental-send suppression).
   - Filter: `(bot_id = ctx.bot_id OR bot_id IS NULL)` during transition — the `IS NULL` half catches legacy boundary rows written before the stamping deploy.
5. Scheduled-job dispatcher (`scheduled_jobs.py`) reads `bot_id` from row. No-op with one bot but the code path is live.
6. **Per-bot observability**: every log line and structured event in inbound, agentic, messaging, scheduler, OOB check carries `bot_id`, `topic_id`, `channel_id`, `binding_id`. Dashboards split by bot (even though only one bot exists yet — the panes are there for S5).
7. **Per-(user, bot) pause read path**: outbound and scheduler consult `user_bot_state.paused` in addition to `system_state.is_paused`. Write path for per-(user, bot) pause stays deferred. Global pause remains the kill switch.
8. **Eval baseline**: run the existing mediator eval suite against current prod-like state and snapshot results. This is the pre-S3 regression reference.
9. Tests/fixtures take a `bot_spec` argument (default mediator).
10. **Lint**: CI fails on any new artifact INSERT outside a transaction that also creates the `artifact_topics` row, and on any new `INSERT INTO messages|bot_turns|scheduled_jobs|feedback` missing `bot_id`/`topic_id`.

Dependencies: (1) gates (2-7); (8) runs against pre-stamp prod, so day 1.

**Mid-sprint checkpoint (day 5–6).**

- Production has been writing the new columns for ≥48h.
- `SELECT count(*) FROM messages WHERE created_at > now() - interval '24h' AND bot_id IS NULL` is zero.
- `SELECT count(*) FROM artifact_topics WHERE created_at > now() - interval '24h'` matches new artifact write count.
- Dual-key coalescer/suppression logs show old-key paths firing only for legacy rows (count trending to zero).
- Eval baseline captured and stored.

**Definition of done.**

- All insert sites stamp the new columns. Coalescer + suppression rekeyed with dual-key transition live. Per-bot observability live. Per-(user, bot) pause read-path live (write-path deferred). Eval baseline captured. **No NOT NULL constraints yet.**

**Risks.**

- A missed insert site in S2a means S2b fails. The grep checklist + lint must be exhaustive.
- Dual-key coalescer holds state in process memory; deploys still flush old-key bursts under old key. Stagger replicas if multi-instance.
- An `artifact_topics` write that fails after the artifact INSERT leaves an orphan. Mandatory transaction wrap.

**Deliberately not in this sprint.**

NOT NULL. Reading via `artifact_topics`. Scope enforcement. Solo prompt renderer. Second bot.

### 16.3 Sprint 2b — Constraints + soak gate

**Goal.** After a soak with the dual-key/null-allowing code from S2a, apply NOT NULL using a rollback-safe pattern and retire the legacy code paths.

**Pre-flight context.**

- Confirm S2a code has been in prod for ≥7 days with zero `bot_id IS NULL` rows on the active window.
- Confirm dual-key fallback paths have been firing only for pre-S2a legacy rows (queryable from observability logs).

**Work items, ordered.**

1. Migration `0025_check_not_valid.sql` — `ALTER TABLE messages ADD CONSTRAINT messages_bot_id_check CHECK (bot_id IS NOT NULL) NOT VALID;` and the same for `topic_id` on the same tables. `NOT VALID` does not block writes during deploy and does not scan the table.
2. Wait for any in-flight long transactions to drain.
3. Run `ALTER TABLE messages VALIDATE CONSTRAINT messages_bot_id_check;` (and equivalents). Locks weak; full table scan.
4. Migration `0026_apply_not_null.sql` — `ALTER TABLE messages ALTER COLUMN bot_id SET NOT NULL;` etc. Cheap because the validated CHECK already proves no NULLs.
5. Drop the validated CHECK constraints (NOT NULL subsumes them).
6. **Retire legacy dual-key paths**: coalescer reads only `(user_id, bot_id)`; newer-inbound suppression drops the `OR bot_id IS NULL` half.
7. **Retire legacy `users.phone` reads.** Any remaining code paths that read `users.phone` directly switch to `user_identities` lookup. Column stays for now (drop in a later sprint if anything still references it).
8. Tests/fixtures updated to require `bot_spec` (no default).

**Definition of done.**

- NOT NULL applied on `messages.bot_id`, `topic_id`, `bot_turns.bot_id`, `topic_id`, `scheduled_jobs.bot_id`, `topic_id`, `feedback.bot_id`, `topic_id`, `bridge_candidates.bot_id`, `topic_id`, `dyad_id`.
- Legacy dual-key paths removed.
- All reads route through `user_identities` for sender resolution.

**Risks.**

- A single straggling NULL row blocks the migration. Re-run the S2a validation queries one more time before `VALIDATE`.
- Retiring dual-key while there are still in-memory bursts under the legacy key drops a turn. Stagger: deploy the retire commit only after coalescer process restart.

**Deliberately not in this sprint.**

Read-path changes. Scope enforcement. Solo bot work.

### 16.4 Sprint 3 — `artifact_topics` join cutover for reads

**Goal.** Every read of memories/themes/observations/watch_items/distillations/OOB goes through the join. Behavior identical (mediator's primary topic is always `relationship`).

This is a **full data-access audit sprint**, not a query helper refactor. Direct reads in hot context + read tools + OOB check + cross-thread privacy + decay + background summaries all in scope.

**Pre-flight context.**

- Audit every `SELECT` and `UPDATE` against the six artifact tables. Grep beyond the obvious files:
  - `hot_context.py` — reads all six families (~6 query sites).
  - `read_tools.py` — direct artifact reads + theme-related lookups (`read_tools.py:546`).
  - `oob_check.py`, `cross_thread_privacy.py`.
  - `scheduled_job_handlers.py:243` — background summaries count themes/watch items.
  - `decay.py:41` — mutates global themes/observations/watch items without topic restriction; this is the most likely site to surface a multi-bot bug.
  - `withheld_reviews.py`, distillation tool validations.
- Expect **50+ query sites including writes' linked-ID validations and background jobs**, not 30. Treat as audit-shaped.
- Confirm S2a eval baseline is captured. Re-run after sprint to verify no semantic regression.

**Work items, ordered.**

1. Add `app/services/topic_filter.py` with `join_artifact_topics(table_alias, topic_id)` returning SQL fragment.
2. Rewrite each read query to JOIN `artifact_topics` filtering by `topic_id = ctx.primary_topic_id AND status='active'`. Per file order: `hot_context.py`, `read_tools.py`, `oob_check.py`, `cross_thread_privacy.py`, `scheduled_job_handlers.py`, `decay.py`, `withheld_reviews.py`.
3. **`decay.py` specifically**: today's decay walks themes/observations/watch items globally. Decide per-job whether decay is per-topic or global. Probable answer: per-topic (a coach's career observations shouldn't decay because a mediator hasn't reinforced them). Verify with explicit comment in `decay.py`.
4. EXPLAIN regression check: a script that runs representative queries on staging and asserts index usage on the partial index `(topic_id, artifact_table) WHERE status='active'`.
5. OOB delivery-time guardrail: filter to `artifact_topics.topic_id = ctx.primary_topic_id` (existing OOB) — global OOB stays as the no-row mode, unused at launch.
6. Property test: compare hot context output on a frozen-fixture snapshot before-vs-after — must match exactly.
7. Re-run eval baseline; compare to S2a snapshot.

Dependencies: (1) gates (2); (2) gates (3, 4, 6).

**Mid-sprint checkpoint (day 5–6).**

- Half the read sites converted. Hot context frozen-fixture diff against pre-cutover is empty.
- Eval re-run shows zero regressions.
- Query latency on staging within 10% of pre-cutover.

**Definition of done.**

- Every artifact read goes through `artifact_topics`. EXPLAIN check green.
- Lint added: CI fails if `FROM (memories|themes|observations|watch_items|distillations|out_of_bounds)\b` appears outside `topic_filter.py` and known exceptions.
- `decay.py` documents and tests per-topic vs global behavior.
- Mediator eval suite passes against post-cutover code with no semantic regression vs S2a baseline.

**Risks.**

- Forgetting one read site means a future bot sees unauthorized data. Lint is the safety net.
- `decay.py` is the easiest sleeper bug. Property test it explicitly.
- Index scan plan regressions on memories/distillations — they're already large.

**Deliberately not in this sprint.**

Multi-topic writes. Scope enforcement beyond hardcoded primary. Status injection. The `scope` argument on read tools.

### 16.5 Sprint 4 — Authorization + topic status + solo bot pre-flight

**Goal.** Authorization layer (§6, §8) and `topic_status` (§7) ship. `consult_perspective` rewritten to actually clone. **Solo bot infrastructure pre-flighted in parallel** so S5 is pure code work.

This is a heavier sprint than earlier drafts implied — 19 read-phase tools plus read surfaces hiding in `write_tools.py` (`list_scheduled_tasks`, etc.) all get `scope` parameter changes, and tool-schema changes are model-behavior risk.

**Pre-flight context.**

- Lock decision A (peek window): default 14d.
- Lock decision D (status cap N): default 5 most-recently-active topics per user.
- Lock decision B: status update folded into `record` step, per §7.5.
- Lock decision: which solo bot is #2 (identity, prompt outline, transport).
- Lock decision C (solo crisis default): refer to dyadic bot + external resources.
- Read `consult_perspective.py:105-139` — the current code reconstructs `TurnContext` field-by-field, it does not clone. Plan a `dataclasses.replace` helper.
- Audit the read-phase tool registry (`tools/registry.py:157`) — count is ~19. Catalogue which need a `scope` arg vs which are scope-irrelevant (`get_bot_actions`, `recent_activity`, etc.).

**Work items, ordered (authorization track).**

1. `ReadScopes`/`WriteScopes` enforcement: every read tool consults `ctx.read_scopes`; every write consults `ctx.write_scopes`. Add `scope` parameter to artifact-reading tools only (skip scope-irrelevant tools). With one topic, `"own"` and `"all"` resolve the same — verify plumbing without changing output.
2. **`consult_perspective` rewrite**: replace the field-by-field `TurnContext` reconstruction at `consult_perspective.py:105` with a `dataclasses.replace(ctx, …)` helper. Add a unit test that enumerates every `TurnContext` field and asserts none are dropped on clone. Add a lint that fails if any new `TurnContext(...)` constructor appears outside the clone helper.
3. `set_topic_status` tool. Register only for `mediator` initially. Folded into `record` step instruction.
4. Hot context: status block (own topic) for served user(s) + pair (`dyad_id` set, `user_id` NULL). Policy-gated cross-topic status injection — code path live but with one topic emits nothing.
5. Cross-topic peek block code path — same.
6. Mediator's BotSpec: `cross_topic_policy = "peek"`, `read_scopes = ReadScopes(topics={"own"}, allow_cross_topic_peek=True, allow_cross_topic_status_injection=True)`, `write_scopes = WriteScopes(topics={"relationship"}, require_reason_for_cross_topic=True)`.
7. Lint/test: every DB-touching function in `read_tools.py`/`write_tools.py` accepts and uses `ctx`.
8. **Tool-schema rollout strategy**: ship `scope` parameter undocumented in tool descriptions for one deploy, observe no behavior regression in evals, then surface in descriptions in a follow-up deploy.
9. Per-(user, bot) pause write path (read path landed in S2a): admin endpoint + tool, defaulting to off.

**Work items, in parallel (solo pre-flight track).**

10. Provision the solo bot's transport: new Discord bot account (or new phone number). Lead time can be days.
11. Create `channels`/`bots`/`topics`/`bot_bindings` rows for the solo bot in **staging only** — not prod yet.
12. Configure webhook endpoint, secrets, provider allowlists for the new transport.
13. Identify and obtain consent from the target user. Written expectations on what the bot does and doesn't do.
14. Transport-specific OOB guardrail test: send a known-bad message through the new transport in staging, verify OOB guardrail fires.
15. Observability dashboards: confirm the per-bot split panes built in S2a render correctly for a second (staging-only) bot.

**Mid-sprint checkpoint.**

- Mediator running with authorization wired. Eval suite passes (against S2a baseline + S3 baseline). `bot_turns` log unchanged in shape.
- A no-op second `BotSpec` in a test (`read_scopes.topics={"career"}`) fails to read `relationship` artifacts.
- Solo bot transport reachable in staging; webhook resolves to the right `bot_id`; OOB guardrail fires.

**Definition of done.**

- Every artifact read/write checks `ctx.read_scopes`/`ctx.write_scopes`. `consult_perspective` cloning verified by enumeration test.
- Mediator writes status; status block renders.
- Negative-permission test in CI passes.
- Solo bot transport, channel, secrets, consent are all green in staging. Provisioning is **not** a blocker for S5.

**Risks.**

- `scope` parameter on every artifact-reading tool changes tool schemas → may affect mediator. The undocumented-first rollout strategy mitigates.
- Status injection token budget. Enforce headline ≤ 80, body ≤ 300 in `set_topic_status`.
- `consult_perspective` is the easiest leak path — the enumeration test is mandatory.
- Solo pre-flight runs in parallel; if it blocks (e.g. Discord bot account approval delays), defer to S5 but don't expand S5 by more than ~3 days.

**Deliberately not in this sprint.**

Multi-topic writes via `topic_slugs`. Solo bot prompt or hot context code (lands in S5). Account-linking workflow.

### 16.6 Sprint 5 — First solo bot (code)

**Goal.** Ship the solo prompt renderer, hot context builder, onboarding renderer, crisis handler, and `BotSpec`. Wire to the pre-provisioned transport from S4 and deploy to the consenting target user.

Infrastructure already exists in staging from S4. This sprint is **pure code work + cutover to prod**.

**Pre-flight context.**

- Confirm S4 solo pre-flight is green: transport reachable, channel resolves, OOB fires, dashboards render, consent obtained.
- Decision: `cross_topic_policy` for the new bot. Recommend `"peek"` to match mediator pattern.
- Read `prompts.py:122-186, 217-222, 335-370` (dyad-only sections to skip in solo renderer).
- Read `agentic.py:686` — the runner calls `partner_of` unconditionally before building context. This is the single line that breaks for solo bots; confirm the work item below covers it.

**Work items, ordered.**

1. `app/services/prompts_solo.py` — solo system prompt renderer. No partner placeholder, no bridges, no in-person redirect, no partner crisis escalation.
2. `app/services/hot_context_solo.py` — solo hot context builder. Single about-user bucket. Status block for own topic for the bound user. Peek block (still no other topics for *this* user unless the dyadic mediator also serves them).
3. `app/services/onboarding_solo.py` — first-contact renderer per §10.2.
4. `app/services/crisis_solo.py` — default handler: refer to dyadic bot + external resources.
5. `app/bots/<name>.py` — `BotSpec` instance, `participants_shape="solo"`, primary topic, tool registry that **excludes** bridge tools, `create_bridge_candidate`, in-person redirect.
6. **`agentic.py:686` runner fix**: skip `partner_of` resolution when `participants_shape == "solo"`. `TurnContext.partner = None`. Hot context dispatch picks the solo builder.
7. Tool registry: filter dyadic-only tools at registration time per `participants_shape`. §5.2 enforcement — solo bot literally doesn't see the tools.
8. Solo participant-shape validation in write tools: reject `about_user_id != bound_user_id`, reject `about_user_id IS NULL`.
9. Promote the staging `bots`/`channels`/`bot_bindings` rows from S4 into prod (or insert equivalent prod rows pointing at prod transport).
10. End-to-end test: solo bot turn against the test user produces response, writes a memory, writes status, attempts a bridge tool call and fails at the boundary.
11. Deploy to the consenting user. Monitor closely for ≥3 days.

Dependencies: (1–4) parallel; (5) gates (6); (7–8) gate (10, 11).

**Mid-sprint checkpoint (day 5–6).**

- Solo bot completes turns end-to-end in staging against a test user.
- Bridge tool call attempt by solo bot raises at the tool boundary (verify registry filter AND `participants_shape` check are both live — belt and braces).
- Mediator unaffected: control mediator turn run side-by-side produces identical output to last week's snapshot.

**Definition of done.**

- Solo bot running in production for ≥3 days against ≥1 user.
- Mediator unaffected.
- Per-(user, bot) pause works.
- Onboarding flow runs on first contact.

**Risks.**

- Two bots sharing one human via `user_identities`: if the solo bot's user is also in the mediator's dyad, do they get duplicate replies? Routing must be channel-specific. Verify `resolve_bot` resolves from channel, not user.
- Crisis handler for solo bot is untested in prod until a real crisis. Make sure default is at least "refuse + external resources" — never silent.
- Onboarding `user_bot_state` write race: two simultaneous first messages must not create two onboarding states. Use upsert.
- Solo prompt renderer is new code with no production trail. Pair-review the prompt explicitly with the user.

**Deliberately not in this sprint.**

Cross-topic writes from any bot. Second dyadic bot. Account-linking UI. Status history. Admin filters.

### 16.7 Sprint 6 — Multi-topic writes + hardening

**Goal.** Enable multi-topic writes via `topic_slugs` + `reason` (§8.2), close out the open risk items in §17, make the system safe to add a third bot without another foundational sprint.

**Pre-flight context.**

- Decision F (when does any bot get cross-topic write scope?): recommend "no bot at launch; mechanism only."
- Audit prod: any orphan `artifact_topics` rows, messages with NULL columns, duplicate user identities.

**Work items, ordered.**

1. Write tools accept `topic_slugs: list[str]` defaulting to `[ctx.primary_topic_slug]`. Intersect with `ctx.write_scopes.topics`. Cross-topic writes require non-empty `reason`, stored on every created `artifact_topics` row.
2. Cross-topic peek block (real this time — solo bot's served user might appear in mediator's relationship topic, so peek can show non-empty counts).
3. Status injection cross-topic — code path wired in S4, exercise now: mediator sees coach's per-user status headline for shared users, gated by `allow_cross_topic_status_injection`.
4. Per-(user, bot) pause UI surface (slash command or admin endpoint). Global pause stays.
5. Telemetry: every log line includes `bot_id`, `topic_id`, `binding_id`. Dashboard split by bot.
6. Per-bot eval suite scaffolding (no evals filled out yet — just per-bot isolation).
7. Documentation pass: short runbook "add a new bot in N steps" — proves the architecture.

**Mid-sprint checkpoint.**

- Cross-topic write test: a coach configured with `write_scopes.topics={"career","relationship"}` writes to relationship with `reason="user asked mediator to remember promotion"`; `artifact_topics.reason` populated.
- Mediator → coach peek block: mediator's hot context shows coach's career status headline for the served user.

**Definition of done.**

- Multi-topic write mechanism in prod (not exercised by any active bot but available).
- Per-bot telemetry visible.
- Pause-per-(user, bot) functional.
- Runbook merged.

**Risks.**

- Status leakage via injection — log every cross-topic status injection event in S6 for a week to verify policy gates work.
- Adding `topic_slugs` to tool schemas changes model surface area again; consider hiding until a bot needs it.

**Deliberately not in this sprint.**

Account-linking workflow. Status history. RLS. GDPR per-topic delete. Second dyadic bot (mechanically trivial after S5).

### 16.8 Decisions to lock before each sprint

Most design decisions from §18 are already closed. The remaining ones are sprint-blocking only when each sprint approaches:

| Decision | Blocks sprint | Recommended default |
|---|---|---|
| `users.phone` → `user_identities` legacy-vs-discord backfill | S1 | `transport='legacy'` |
| OOB existing-rows classification | S1 | All → `relationship` topic-scoped; "no rows = global OOB" is a future-only mode |
| Soak duration before NOT NULL (S2a → S2b) | S2b | 7 days minimum |
| Per-(user, bot) pause write-path scope (read-path lands in S2a) | S4 | Admin endpoint + tool, default off |
| `decay.py` per-topic vs global behavior | S3 | Per-topic (a coach's career observations shouldn't decay because mediator hasn't reinforced them) |
| Peek window length (decision A) | S4 | 14 days |
| Status cap N (decision D) | S4 | 5 most-recently-active topics per user |
| Solo bot identity, prompt, transport | S4 (pre-flight); S5 (code) | Lock during S2a–S3 in parallel; transport provisioning starts as soon as decided |
| Solo crisis default (decision C) | S4 | Refer to dyadic bot + external resources |
| Cross-topic write ramp (decision F) | S6 | No bot at launch; mechanism only |

### 16.9 What can be cut under pressure

If compressed:

- **S6 can shrink to its safety subset** (lint + runbook); defer multi-topic writes. No bot at launch needs them. Saves ~one sprint.
- **`set_topic_status` (S4) can slip into S5** if calendar pressure hits.
- **S1 and S2a could merge** into one 3-week sprint if the engineer can tolerate it. Not recommended.

What is **not** cuttable:

- **S2b's `CHECK ... NOT VALID` → VALIDATE → NOT NULL pattern.** Tempting to skip the CHECK step and go straight to NOT NULL; this makes rollback impossible.
- **S3 (the join cutover) before any second bot.** Doing it under live multi-bot is dramatically worse than as a no-op.
- **S4 solo pre-flight track.** Moving it back into S5 reintroduces the original sizing problem.
- **`user_identities` in S1.** Duplicate-human risk (§20.18) is the highest-cost retroactive fix.
- **`consult_perspective` clone helper in S4.** Skipping leaves every new `TurnContext` field silently dropped on consult. Single highest correctness defect in the current S4.
- **Observability + eval baseline in S2a.** Without them, S3 and S4 cannot detect regressions.

### 16.10 Per-sprint megaplan setup

Each sprint is one megaplan job. The selection rubric is three independent dials:

1. **Intelligence tier** (`--profile`) — `basic` / `led` / `thoughtful` / `premium` / `super-premium`.
2. **Planning complexity** (`--robustness`) — `light` / `standard` / `robust`. `standard` is home base; `robust` should feel exceptional ("regression = production incident *during this sprint*").
3. **Depth** (`--depth`) — `low` / `medium` / `high` / `xhigh` / `max`. Default `low`; bump only when the planner specifically needs to deliberate (long brief) or do extensive repo-reading (unfamiliar codebase).

Vendor at tiers 2–4 is interchangeable by policy; set `[defaults].vendor = "claude"` once in `~/.config/megaplan/config.toml` and every sprint here inherits it without per-sprint flags. Tier 5 is vendor-locked. Shorthand for sprint notes: `profile/robustness/depth` with `//` skipping the middle slot.

**Per-sprint picks:**

| Sprint | Pick | One-line rationale |
|---|---|---|
| **S1** — Foundation schema + code shape | `led//medium` | Six additive migrations + dataclass plumbing — the schema design is locked in §3–§12; the planner sequences migrations and backfill cursors, but execution is mechanical. |
| **S2a** — Stamp + rekey + observability + eval baseline | `thoughtful//medium` | Cross-cutting insert-site rewrite across 8+ files with a dual-key transition — classic tier-3 "inbox/routing rewrite" shape. |
| **S2b** — Constraints + soak gate | `led` | Pure Postgres `CHECK NOT VALID → VALIDATE → NOT NULL` recipe — the rubric's tier-2 archetype "step ordering matters but code is mechanical." |
| **S3** — `artifact_topics` join cutover for reads | `thoughtful//high` | Full data-access audit across 7 files with the `decay.py` sleeper-bug judgment call; long brief plus partly-unfamiliar repo surface. |
| **S4** — Authorization + topic status + solo pre-flight | `premium/robust/high` | Scope enforcement IS the security model (§17); a regression here is a data-leak production incident — the rubric's specifically-named `robust` trigger. |
| **S5** — First solo bot (code) | `thoughtful//medium` | Novel implementation of solo renderer/hot-context/crisis in a now-known architecture, adapted from named dyadic sections. |
| **S6** — Multi-topic writes + hardening | `led` | Mechanism-only extensions of patterns from S1–S5; design is documented in §8.2, no active bot exercises it at launch. |

**Per-sprint rationale:**

- **S1 → `led//medium`.** The architectural decisions are *already made* in §3–§12; S1 *implements* them as additive migrations and optional dataclass fields. That matches tier 2's description ("complex schema migrations where step ordering matters … architecture demands deliberation but code follows patterns"). It is **not** tier 5 — the schemas everyone-builds-on are *designed* here, not *decided* here. The rubric's tier-1 warning about reaching for tier 3 the moment "code" appears applies one rung up too. Robustness `standard` because the work is additive + nullable; S2b's soak gate is the safety net. Depth `medium` because the brief is long (6 migrations, 4 modules, cursor-keyed resumable backfill, OOB classification rule, named CHECK typo).
- **S2a → `thoughtful//medium`.** The brief is an inbox/routing rewrite shape — the rubric's tier-3 archetype. Cross-cutting work spanning `inbound.py`, `messaging.py`, `agentic.py:483/577/612`, `scheduled_jobs.py:216`, `write_tools.py:1367`, every artifact write, plus the dual-key transition. Tier 2 isn't enough — the critic needs to catch missed insert sites. Robustness `standard` because no individual stamping miss is a production incident; the incident only happens at S2b NOT NULL apply, and the soak window catches it. Depth `medium`, not `high` — the touched files are well-known mediator code, no deep repo-reading needed.
- **S2b → `led`.** Tier-2 archetype: the five-step `CHECK NOT VALID → VALIDATE → NOT NULL → drop CHECK → retire legacy` recipe is well-established Postgres practice. Once the precondition (S2a soak with zero NULLs) and sequence are named, SQL is mechanical. The recipe itself is rollback safety. Default depth — the brief is short and the patterns are explicit.
- **S3 → `thoughtful//high`.** Full data-access audit with a sleeper bug specifically called out in `decay.py:41` (today's decay walks themes/observations/watch items globally — per-topic vs global is a real design call closing this sprint). 50+ query sites across `hot_context.py`, `read_tools.py`, `oob_check.py`, `cross_thread_privacy.py`, `scheduled_job_handlers.py`, `decay.py`, `withheld_reviews.py`. **Not `robust`**: the blast radius this sprint is mediator-only — a missed read site is an EXPLAIN regression or frozen-fixture diff now, only a *future* privacy leak when bots #2+ ship (which S4's authorization layer catches). The lint added in this sprint is the right belt-and-braces. Depth `high` because the codebase surface is partly unfamiliar (`decay.py`, `withheld_reviews.py`, `cross_thread_privacy.py` are off the mediator main path) — exactly the rubric's `high` trigger.
- **S4 → `premium/robust/high`.** This is the sprint that ships the security model. §17 lead bullet: "Scope enforcement is the security model. Every leak path runs through a tool that didn't consult `ctx.read_scopes`/`ctx.write_scopes`." The rubric's tier-4 trigger names this near-verbatim ("production-critical work … security-critical code paths"). Three concrete tier-4 markers: (a) authorization layer with negative-permission CI tests, (b) `consult_perspective` clone helper named in §16.9 as the *single highest correctness defect*, (c) tool-schema changes across 19 tools (model-behavior risk). **Not tier 5** because the decisions A, B, C, D, F are locked in §16.8 — tier 5 is reserved for *making* big architectural decisions; here we *enforce* them. `robust` because a tool that skips `ReadScopes` enforcement exposes one user's memories to the wrong bot once S5 ships — that's not "regression," that's a privacy incident. Exactly the `robust` trigger.
- **S5 → `thoughtful//medium`.** Novel code in a now-known architecture: solo prompt renderer, hot context builder, onboarding, crisis handler, BotSpec, `agentic.py:686` runner fix. Genuinely new files but they're parallels of mediator code with specifically-named sections stripped (`prompts.py:122-186, 217-222, 335-370`). Robustness `standard` because per-(user, bot) pause makes rollback per-user — a misbehaving solo bot can be paused without a production incident. Depth `medium`, not `high` — the doc's file map already does much of the structural work; the work is *adapting*, not *designing*.
- **S6 → `led`.** The `topic_slugs` + `reason` design is documented in §8.2. The work is mechanical extensions: write tools accept the parameter, peek block exercises the path, status injection cross-topic flips on, per-(user, bot) pause UI, telemetry, runbook. Decision F (no bot at launch with cross-topic write) means the mechanism ships dormant. Default robustness + depth — patterns are locked from S1–S5.

**Where escalation was considered but rejected:**

- **S4 → `super-premium`.** Tier 5's distinguishing value is *combining* Claude + Codex when one premium model's depth isn't enough. Here `premium/robust/high` already concentrates Claude on every phase, which is plenty for *implementing* a designed authorization model.
- **S3 → `robust`.** The "missed read site = future leak" framing is real but the leak is *future* (post-S4); this sprint's blast radius is mediator-only. The lint added in S3 is the right safety net.
- **S1 → `thoughtful`.** Six migrations *feels* tier-3-shaped, but the design is locked and the patterns (additive nullable columns, cursor-keyed backfill, polymorphic join table) are well-known. `led` puts the premium model on the plan, which is where the value is for migration ordering.

**Cross-cutting notes:**

- **Vendor config.** Set `[defaults].vendor = "claude"` once in `~/.config/megaplan/config.toml`. The empirical "Opus reads existing repo better" finding fits this codebase — the planner is repeatedly asked to grep wide swaths of established code (S2a's 8+ files, S3's 50+ query sites, S4's 19-tool registry, S5's `agentic.py:686`). No sprint here is a strong fit for `--vendor codex` override.
- **No `--with-prep` anywhere.** No sprint surveys an unfamiliar external API. S5's transport provisioning is human-shaped (Discord bot account approval, consent) and lives in S4's pre-flight track, not a megaplan prep phase.
- **No `--critic` overrides anywhere.** The rubric is explicit that critic overrides should be reserved for specific reasons; none of these sprints clears that bar.
- **Don't split sprints into multiple megaplans.** §16.2 and §16.4 each list ~10 work items but they're tightly coupled — the dual-key transition in S2a doesn't make sense without the stamping, and S4's solo-pre-flight is *deliberately* parallel to the authorization work. The rubric's "one profile per sprint; lower-stakes work inherits the tier" applies.

**Open judgment calls (revisit if the first runs surprise you):**

- **S1 between `led//medium` and `thoughtful`.** Strongest argument for tier 3 is the breadth — six migrations + four code modules + a backfill resumability scheme + the OOB classification rule that resolves a documented §19/§20 ambiguity. If `led` runs repeatedly miss the OOB classification rule or fumble the polymorphic-FK comment, promote to `thoughtful`. The rubric's tier-2 calibration caveat ("design point, not measured") cuts both ways — this is exactly the kind of sprint where tier 2 should be tested before defaulting up.
- **S3 between `//high` and `//medium` depth.** Pivot is `decay.py`. If the planner can be trusted to *recognize* that decay needs a per-topic vs global decision at all, `medium` is enough. If you want the planner to actively read `decay.py:41`, `withheld_reviews.py`, and `cross_thread_privacy.py` to find the sleeper-bug sites, `high` earns its cost.
- **S4 between `premium/robust/high` and `super-premium/robust/high`.** If during planning the megaplan starts re-debating the `ReadScopes`/`WriteScopes` shape or the cross-topic peek policy, that's a signal the design isn't as locked as §16.8 claims, and `super-premium` becomes right. With the design locked, `premium/robust/high` is cleaner — it doesn't pay tier-5 cost for tier-4 work.
- **S5 robustness `standard` vs `robust`.** Genuinely close. Argument for `robust`: first non-mediator bot in prod against a real consenting user, crisis handler untested in prod, two-bots-sharing-one-human routing risk. Argument for `standard` (picked): all those risks are bounded by per-(user, bot) pause + ≥3-day monitoring + control mediator side-by-side. If the consent target is a more visible user or the rollout window shrinks, escalate to `robust`.
- **Whether S2b deserves a megaplan at all.** Eight work items, all mechanical SQL + a coalescer code retire. The rubric's "skip megaplan for anything you can hold in your head" lower bound is close. `led` kept because the consequences of skipping a CHECK→VALIDATE→NOT NULL step are real and `led` is cheap. If the team has run this exact pattern before, dropping to `basic/light` is defensible.

---

## 17. Risks

- **Scope enforcement is the security model.** Every leak path runs through a tool that didn't consult `ctx.read_scopes`/`ctx.write_scopes`. Centralize the check; add a lint that rejects DB queries in tools that don't pass through the authorization layer.
- **`artifact_topics` migration is the largest single migration.** Backfilling memories/observations/themes/watch_items/distillations/OOB rows on a hot DB needs batching and validation queries.
- **OOB nullable membership semantics.** Global OOB is "no row in `artifact_topics`." Make this loud in code review.
- **Newer-inbound suppression cutover.** Re-keying mid-deploy will briefly let stale sends through. Drain or use a feature flag.
- **Channel routing is security-sensitive.** Startup integrity check: every channel resolves to a known bot; FK with `ON DELETE RESTRICT`.
- **Prompt token budget.** Status + peek compound with topic count. Cap peek to N most-recently-active topics; enforce headline ≤ 80 chars, body ≤ 300; cap injected statuses.
- **Identity duplication risk before linking lands.** A human reaching two bots on two transports may be two `user_id`s for a while. Memories/OOB/feedback under both. Linking workflow (phase 7) must support merging without rewriting encrypted blobs — the schema is built for this but the workflow isn't free.
- **Global pause + per-(user, bot) pause** interact non-trivially. Document precedence: global pause wins, per-(user, bot) is additive.
- **Solo bot crisis defaults.** Generic default exists; flag at each new solo bot creation that this needs review.
- **Out of scope but worth flagging**: per-bot spend caps, per-bot eval baselines, admin UI filters, RLS policies, GDPR delete/export semantics across topics (each user's memories live in many `artifact_topics` rows now). All deferred but named.

---

## 18. Decisions confirmed and remaining open

### Confirmed

1. 1:1 bot↔topic to start; schema-ready for many-to-1.
2. OOB topic membership via `artifact_topics`; no rows = global.
3. `topic_status` keyed by `(topic, user)` or `(topic, dyad)`; mutual exclusion via CHECK.
4. Mediator's `cross_topic_policy = "peek"`; status injection allowed for own topic always, cross-topic gated by `allow_cross_topic_status_injection`.
5. Tool descriptions: genericize universally; per-bot override only as escape hatch.
6. No "bridge" primitive across topics — dropped.
7. **Multi-topic via `artifact_topics` join table.** Reversed from the array decision (§20.10).
8. **Writes gated by `BotSpec.write_scopes`; cross-topic writes require `reason`.** Reversed from "agent fully overrides" (§20.11).
9. Status update folded into the `record` step.
10. `participants_shape` on `BotSpec`: `solo` or `dyad`. Tool registry and OOB policy are participant-shape specific.
11. Explicit `dyads`, `dyad_members`, `bot_bindings`, `user_bot_state`, `user_identities` tables.
12. Themes leave user-FK-less for now.
13. `bot_id` is FK to `bots` everywhere with `ON DELETE RESTRICT`.
14. `bot_turns` carries version pins for every load-bearing artifact (spec, prompt, hot context builder, tool schema).
15. Phase 0 is genuinely additive + no-op; phase 1 ships insert sites + NOT NULL.
16. User identity model lands in phase 0; account-linking workflow deferred.
17. Global pause stays as a kill switch alongside per-(user, bot) pause.
18. Bridge candidates carry `topic_id` + `bot_id` + `dyad_id`.

### Still open

A. **Peek window length.** 14d proposed. Confirm.

B. **Status update cadence.** Every turn the agent decides whether to call, vs only "when material"? Recommend: every turn, agent decides.

C. **Solo bot crisis escalation default.** Refer to dyadic bot (if bound) + external resources is the proposed default. Confirm.

D. **Status-block cap in prompt budget.** N most-recently-active topics. Pick N.

E. **Multi-transport bots.** A single bot listening on Discord + SMS. Probably out of scope until needed, but the channels table accommodates it; confirm no need to design further now.

F. **Cross-topic write capability ramp.** Phase 3 lands the mechanism, but no bot has cross-topic write scopes at launch. When do we first give one? Likely never for the mediator; possibly for cross-functional bots later. Defer to use-case.

G. **`themes.about_user_id` later?** Confirmed deferred (option (a) in §3.2); confirm we're OK adding it later if needed.

---

## 19. Verification addendum (Codex pass, read-only)

Findings from an independent read of the actual code, against the design. Every claim cites file:line.

### 19.1 `distillations` about-whom

`distillations` are neither `about_user_id` nullable like memories/observations nor pair-only like themes. They are evidence/provenance-scoped via `source_user_ids uuid[] NOT NULL CHECK (cardinality(source_user_ids) > 0)` (`migrations/0015_distillations.sql:16`). Core columns: `id`, `content`, encrypted content, `confidence`, `status`, `sensitivity`, `visibility`, `shareable_summary`, `source_user_ids`, related memory/observation/theme/message arrays, revision links/counts, and timestamps (`migrations/0015_distillations.sql:6-30`). Tool code enforces `source_user_ids` stay inside `ctx.user`/`ctx.partner`, not "about whom" (`app/services/tools/write_tools.py:568-576`), and inserts `source_user_ids` directly (`app/services/tools/write_tools.py:939-956`). Read filtering is by `source_user_id = ANY(source_user_ids)` (`app/services/tools/read_tools.py:654-656`). Plan needs a fourth about/provenance shape.

### 19.2 `bridge_candidates` end-to-end

Schema is explicitly dyadic: `source_user_id`, `target_user_id`, `kind`, lifecycle `status`, `sensitivity`, `source_message_ids`, summaries, sent message, timestamps (`migrations/0013_bridge_candidates.sql:3-18`). `partner_path` later classifies the bridge route (`migrations/0016_partner_bridge_paths.sql:3-17`). Proposed by `create_bridge_candidate`; requires `source_user_id == ctx.user.id` and both users in the current dyad (`app/services/tools/write_tools.py:273-286`, `:504-506`). Status auto-derives `ready` only for opt-in low/medium source material (`app/services/tools/write_tools.py:291-302`). Target surfacing in hot context only where `target_user_id = current user`, `source_user_id = partner`, `status='ready'`, `partner_path='message_partner'` (`app/services/hot_context.py:424-438`). Multi-bot breaks unless bridge rows gain dyad/bot/topic scope.

### 19.3 `scheduled_jobs` vs `scheduled_tasks`

There is no `scheduled_tasks` table. Active table is `scheduled_jobs` (`migrations/0001_init.sql:98-107`). Migration 0017 only adds `scheduled_task` as a `scheduled_jobs.job_type` (`migrations/0017_scheduled_tasks.sql:3-24`). Worker claims from `scheduled_jobs` only (`app/services/scheduled_jobs.py:104-138`). `scheduled_task` handled by `handle_scheduled_task` (`app/services/scheduled_job_handlers.py:160-169`). Add `bot_id` and `topic_id` to `scheduled_jobs`; do not model `scheduled_tasks` as a parallel table.

### 19.4 `hot_context` coupling

`build_hot_context(pool, user, partner: User, ...)` is not partner-optional (`app/services/hot_context.py:173-179`). Passing `None` breaks at `_user_profile(pool, partner)` (`:180-181`). Dyad assumptions are pervasive: OOB pulls both owners (`:227-235`), memories use `[user.id, partner.id]` plus shared NULL (`:248-258`), messages query either participant (`:328-339`), sharing defaults require both users (`:340-343`), distillations filter both source ids (`:344-360`), bridges require partner-as-source (`:424-438`), render emits "Your Partner" plus partner sharing defaults (`:581-608`). `TurnContext` types `partner: User` (`app/services/turn_context.py:16-22`). Solo builder must be separate.

### 19.5 Prompt renderer coupling

Prompt opens as relationship mediator between `{partner_a_name}` and `{partner_b_name}` (`app/services/prompts.py:7-12`). Renderer requires both partner strings (`:435-443`) and replaces both placeholders (`:464-471`). Dyad-only sections: cross-thread sharing, partner perspective, bridge candidates, in-person redirection, crisis escalation to partner (`:122-133`, `:166-186`, `:217-222`, `:335-370`). Split renderers; partner-optional leaves too much dead policy.

### 19.6 OOB delivery-time guardrail

Delivery-time OOB runs before `send_outbound` via `_call_oob_hook` (`app/services/messaging.py:338-366`, incremental at `:193-228`). Default hook calls `check_oob_with_policy` (`app/services/hooks.py:13-25`). Scope defaults to recipient when `protected_owner_ids` omitted (`app/services/oob_check.py:106-109`, `:175-180`); agentic turns currently pass both dyad ids (`app/services/agentic.py:725-741`). Check fetches active OOB by owner/status, no topic filter (`app/services/oob_check.py:112-124`). Adding topic filter is moderate; separate Anthropic call, not in-process logic (`:191-221`).

### 19.7 Outbound transport coupling

Transport selected from `settings.messaging_provider` (`app/config.py:26`). `send_outbound` branches inline on provider (`app/services/messaging.py:370-399`). Incremental sending Discord-only, returns `not_enabled` otherwise (`:155-168`). No sender interface. Per-bot outbound is moderate: introduce transport/channel object, move provider/window/template behavior behind it, stamp message rows with bot/channel.

### Findings that change the plan

- `distillations` need explicit provenance-vs-about modeling.
- `scheduled_tasks` is not a separate concept; it's a `scheduled_jobs` subtype.
- Solo support is not a signature tweak. Hot context, prompt rendering, turn context, and bridge tools are structurally dyadic.
- Bridge candidates need `dyad_id` + `bot_id` + `topic_id`; current source/target-only rows will leak.

---

## 20. Adversarial review addendum (Codex gpt-5.5 high)

Second-pass review at higher reasoning effort. Found issues missed by the first pass.

### 20.1 Phase 0 is not truly "no behavior change"

Original §15 said add `bot_id`/`topic_id`, backfill, then set NOT NULL. But current inserts don't write either column: inbound `messages` at `app/services/inbound.py:161`, outbound at `app/services/messaging.py:55,84`, `bot_turns` at `app/services/agentic.py:483`. Either keep nullable until phase 1, or phase 0 ships insert-site updates alongside schema. **Resolved in §16:** phase 0 is now genuinely additive + NULL-allowed; phase 1 updates insert sites and applies NOT NULL.

### 20.2 Bridge candidates need bot_id AND topic_id, not just dyad_id

`dyad_id` only prevents cross-dyad leakage. A coach and mediator sharing a dyad cannot share one bridge queue without bot/topic scope. **Resolved in §3.1:** all three columns added in phase 0.

### 20.3 "Axis A collapses for solo bots" is aspirational

Tool schemas at `tool_schemas.py:815, 884, 954, 1041` accept raw `about_user_id`, `owner_user_id`, `source_user_ids`, OOB `owner_id` with no binding-shape validation. "Solo bot never picks shared" is not an invariant unless tools reject illegal scopes. **Resolved in §5.2:** tool entry points validate against `ctx.participants_shape` and `ctx.binding`.

### 20.4 TurnContext is the dependency hub the doc under-specifies

`app/services/turn_context.py:16` has `user`, mandatory `partner`, message ids, hot context, OOB owners — but no `bot_id`, `topic_id`, `binding_id`, `channel_id`, `participants_shape`. Every scope decision must live on `TurnContext` or every tool rediscovers it. **Resolved in §6.1.**

### 20.5 Newer-inbound suppression is per-user, not per-(user, bot)

`_newer_inbound_exists` at `agentic.py:612` suppresses by `sender_id` only; same in `send_message_part` at `read_tools.py:166`. A message to the finance bot cancels the mediator's final send. **Resolved in §10.2:** keying changes to `(sender_id, bot_id)`.

### 20.6 Global pause is deeper than per-(user, bot)

`system_state.is_paused` at `system_state.py:23` reads a single `global_pause` key. Inbound pause supersedes scheduled jobs at `inbound.py:136`. Scheduler downshifts to heartbeat-only at `scheduled_jobs.py:75`. Moving pause into `user_bot_state` is not a local refactor. **Resolved in §10.2:** global pause stays as a kill switch; per-(user, bot) is additive.

### 20.7 `consult_perspective` clones ctx without topic-awareness

At `consult_perspective.py:105-139`, the consult clones the same ctx, partner, protected_owner_ids, hot context, and grants consult-phase reads. **Resolved in §6.4, §10.2:** consult inherits scopes structurally.

### 20.8 Audit needs more than `bot_id` on `bot_turns`

`bot_turns` stores `system_prompt_version` and `prompt_snapshot` but no bot identity. Six months from now, audits need spec version, prompt renderer version, tool schema version, hot-context builder version. **Resolved in §10.3.**

### 20.9 Onboarding is hard-coded mediator-shaped

Hard-coded mediator welcome at `inbound.py:23`. Global `users.onboarding_state` at `models/user.py:16`. First-contact prompt rendering keyed from that state at `prompts.py:446`. **Resolved in §10.2:** per-bot `onboarding_renderer`, `user_bot_state` table.

### 20.10 `topic_ids uuid[]` is the wrong durable shape

Array column makes "who tagged this topic, when, why, with what confidence" impossible. Per-topic lifecycle becomes opaque array edit with no audit trail. **Resolved in §2:** moved to `artifact_topics` join table.

### 20.11 "Agent can fully override topic on write" is ambient write authority

Free override across the system is not flexibility; it's unconstrained write authority. **Resolved in §6.3, §8.2:** writes gated by `BotSpec.write_scopes`; cross-topic writes require `reason`.

### 20.12 `topic_status` NULL = pair needs `dyad_id`

If two dyads share `relationship`, pair-level rows collide. **Resolved in §7.1:** pair-level keyed by `(topic_id, dyad_id)`.

### 20.13 Status injection undermines `cross_topic_policy`

A bot with `cross_topic_policy="forbidden"` still sees other topics' status headlines verbatim. **Resolved in §7.4, §14:** status injection is policy-gated.

### 20.14 Solo/dyad split is incomplete

Hot context and prompt renderer split is right but not enough. Tool registry, allowed tools, OOB policy, bridge tools, scheduler handlers, `TurnContext` type need participant-shape-specific contracts. **Resolved in §5.2, §10.1:** participant-shape specific tool registry and OOB policy on `BotSpec`.

### 20.15 Authorization model is missing

`scope="all"` is a parameter, not a permission. **Resolved in §6.3:** explicit `read_scopes` and `write_scopes` on `BotSpec`, enforced at the tool boundary.

### 20.16 Channel identity / account-linking is phase 0/1, not phase 4

Current `users.phone` is reused for Discord ids (`discord.py:176` vs `migrations/0001_init.sql:10`). Multi-transport breaks identity before the second bot is useful. **Resolved in §12:** `user_identities` table lands in phase 0; account-linking workflow deferred to phase 7 but schema is ready.

### 20.17 Other missing topics

Per-bot spend caps. Per-bot eval baselines. Admin UI filters by bot. RLS/policy changes. Deletion/export semantics across topics. Migration validation queries proving no unscoped rows. **Partially resolved in §16 (validation queries), §17 (named in risks); rest explicitly deferred.**

### 20.18 Most-dangerous open question: cross-bot user identity

If we write memories/OOB/feedback under duplicate humans, retrofitting a merge later is privacy-sensitive and expensive. **Resolved in §12.2:** `user_identities` schema lands in phase 0; account-linking is a workflow on top, deferrable, but the schema is built to support merging without rewriting encrypted blobs.

### 20.19 Bottom line

The doc previously treated multi-agent as "add columns and route through `BotSpec`." The hard part is enforcing scope at every site that currently assumes one dyad, one transport, one global pause, one prompt family, one conversation stream. **Resolved through §6 (scope on `TurnContext` + authorization), §10.2 (full refactor surface), §16 (phased correctly).**

---

## 21. Sprint plan sense-check (Codex gpt-5.5 high)

Third verification pass, focused on the sprint plan in §16. Findings folded into §16 in this same revision; this section is the record of the review for future reference.

### 21.1 Sprint boundary critique

**S2 (original) was too large.** Combined routing cutover, insert-site stamping, artifact write transactions, coalescer rekey, newer-inbound rekey, scheduled-job bot routing, fixture churn, and `NOT NULL`. Insert sites are broader than the doc named: also `agentic.py:577` (deferred jobs), `scheduled_jobs.py:216` (scheduler seeds), `write_tools.py:1367` (scheduled-task writes). 7-day soak in 10-workday sprint leaves no margin to find a missed insert and restart soak. **Resolved by splitting into S2a + S2b.**

**S3 underestimated.** 30–50 query sites was plausible only if counting indirect reads. Hot context reads all six artifact families at multiple sites (`hot_context.py:230, 252, 276, 295, 320, 353`). Read tools add theme-related lookups (`read_tools.py:546`). Background summaries count themes/watch items (`scheduled_job_handlers.py:243`). `decay.py:41` mutates global themes/observations/watch items without topic restriction — the single sleeper site. **Resolved in revised §16.4 as a full data-access audit sprint.**

**S4 (original) heavier than the doc said.** 19 read-phase tools registered (`registry.py:157`); plus read surfaces in `write_tools.py` (`list_scheduled_tasks` etc.). Tool-schema changes are model-behavior risk, not plumbing. **Acknowledged in revised §16.5; rollout strategy ships `scope` undocumented for one deploy before surfacing.**

**S5 (original) under-scoped non-code work.** Transport provisioning, webhook setup, secrets, channel registration, consent, transport-specific OOB testing, dashboards — all sat in the riskiest sprint. Plus the current `BotSpec.render_system_prompt` at `base.py:31` and `agentic.py:686` always call `partner_of` before building context. **Resolved by moving pre-flight to S4 in parallel with authorization; S5 becomes pure code work.**

### 21.2 Hidden dependencies

**Pause depth.** `system_state.is_paused` reads a single `global_pause` key (`system_state.py:23`); pause supersedes every pending user-facing job globally (`system_state.py:59`); outbound checks both global pause and user pause hooks (`messaging.py:170`). Per-(user, bot) pause cannot wait until S5 if S2a is already rekeying scheduled jobs and coalescing by bot. **Resolved: per-(user, bot) pause read-path lands in S2a (work item 7); write-path lands in S4.**

**Transitional dual-key rekey** is implementable only with care. Coalescer's in-memory dicts are keyed by `UUID user_id` (`debouncer.py:42, 47`). Cannot just change SQL filters; active in-memory bursts from before deploy still flush under the old key. Safe cutover needs either drain/restart semantics or a dual reader. Newer-inbound suppression has two copies (`agentic.py:612`, `read_tools.py:166`); during cutover old rows may have NULL `bot_id`, so dual-key must mean `(bot_id = ctx.bot_id OR bot_id IS NULL)`. **Resolved in §16.2 work items 3, 4.**

**`consult_perspective` is more work than originally implied.** It reconstructs a fresh `TurnContext` field-by-field (`consult_perspective.py:105`), not a clone. Every new field added in S1 would be silently dropped unless S4 changes the pattern. **Resolved in §16.5 work item 2: rewrite with `dataclasses.replace` helper + enumeration test + lint.**

### 21.3 Migration safety concerns

**OOB backfill ambiguity** (highest single-defect risk). Original §16.1 validation said `artifact_topics` count equals sum of artifact tables including OOB; §16.3 said global OOB = no `artifact_topics` rows. Inconsistent. **Resolved in §16.1: existing OOB rows all → `relationship` topic-scoped. Global OOB is a new mode for future, unused at launch.**

**Backfill strategy.** Batched `UPDATE ... LIMIT 5000` is wrong shape for a pure join-table insert. Should be cursor-keyed `INSERT INTO artifact_topics SELECT ... WHERE id > last_id ORDER BY id LIMIT n` with resumability and duplicate protection. Join table has no FK to each artifact table (polymorphic via `artifact_table` column) — orphan prevention is application discipline. **Resolved in §16.1.**

**NOT NULL rollback rule.** Once `messages.bot_id/topic_id` and `bot_turns.bot_id/topic_id` are NOT NULL, rolling back to old app code breaks inserts immediately. Safer: `CHECK (bot_id IS NOT NULL) NOT VALID` → `VALIDATE` → `SET NOT NULL` only after new code has survived a full rollback window. **Resolved in §16.3 (S2b) work items 1–4.**

**Byte-identical DoD unrealistic.** Hot context embeds `now_utc` (`hot_context.py:65`), relative temporal labels (`time_context.py:98`), rendered current-time blocks (`hot_context.py:592`). Prompt-hash equality between live turns is impossible. **Resolved in §16.1 DoD: frozen-fixture rendering equality + production semantic diff, not live hashes.**

### 21.4 What was missing

- **Per-bot observability** — was deferred to S6, too late. Moved to S2a.
- **Eval baseline** — was deferred to S6, too late. Moved to S2a (work item 8); re-run in S3.
- **Solo bot pre-flight** — was in S5. Moved to S4 in parallel.
- **Per-sprint rollback procedures** — handled by Megaplan integration; not separately documented per user direction.

### 21.5 Highest-risk single moment

Originally: S2 day 8–9 (routing + stamping + rekey + scheduled-job bot routing + multi-row artifact transactions + NOT NULL in one deploy window). **Defused by splitting S2 into S2a (nullable changes only) + S2b (constraints after soak with CHECK NOT VALID pattern).** New highest-risk moment is **S3 — the read-path cutover** — where missing one query site means a future bot can leak data. The lint and enumeration tests are the safety net.

### 21.6 Verdict

The original 6-sprint plan was "not shippable as written." The revised 7-sprint plan in §16 addresses every finding in this section. The verdict on the revised plan is that the sequencing is right, the risks are isolated, and the critical correctness defects (OOB backfill, `consult_perspective` clone, byte-identical DoD) are fixed.
