# Prompt registry, BotProfile dataclass, and reminder bundling — design brief

## Context

Veas runs five bots that each render their own system prompt: `mediator` (dyadic relationship coach, `app/services/prompts.py`), `solo_coach` (generic solo reflection coach, `app/services/prompts_solo.py`), `hector` (solo fitness, `app/bots/prompts/hector.py`), `habits` (solo habits, `app/bots/prompts/habits.py`), and `tante_rosi` (pregnancy companion, `app/bots/prompts/tante_rosi.py`).

Today two shared prompt chunks (`SCHEDULING_CAPABILITY_PROMPT_SLOT` in `app/bots/prompts/scheduling.py` and `PARTNER_NUDGE_PROMPT_SLOT` in `app/bots/prompts/partner_nudge.py`) are imported individually by each of the five renderers and concatenated via `"\n" + SLOT + "\n"` into template placeholders (`{scheduling_section}`, `{partner_nudge_section}`). Several other paragraphs — voice rules, operating principles, body-image safety, knowledge primitives, commitment flow, weekly review, reply discipline — are duplicated or near-duplicated across `hector.py` and `habits.py` (and partially in `tante_rosi.py`). There is no registry, no canonical ordering, no audience routing, and no formal structure to a "bot profile" — each bot file is a free-form string template.

Separately, the scheduling tool surface is almost complete: `schedule_checkin`, `schedule_task`, `list_scheduled_tasks`, `list_scheduled_checkins`, `update_scheduled_task`, `cancel_scheduled_task`, `cancel_scheduled_checkin` all exist as real tools in `app/services/tools/registry.py`. The one verb missing is `update_scheduled_checkin` (no way to edit a pending one-off check-in's time or message — only cancel). The `Upcoming reminders` section already renders in hot context (`app/services/hot_context.py:1301-1316` and `app/services/hot_context_solo.py:1201-1218`) but the renderer does not include the row id even though `_fetch_upcoming_items` already returns it (`hot_context_solo.py:338`). Without the id, a bot can see "you have something at 14:05" but cannot reliably target it for update or cancel without a round-trip through `list_scheduled_tasks` / `list_scheduled_checkins`.

There is also no "bundling" guidance: bots that get four reminders in one hour will happily create four separate reminders instead of folding them into one richer morning check-in. The judgment call should belong to the bot, not to a timing rule — but the bot must be told to make it.

## Goal (one sentence)

Introduce a shared prompt slot registry with audience routing and canonical ordering, refactor every bot to a structured `BotProfile` dataclass that composes registry slots with bot-specific sections, surface task ids in the hot-context `Upcoming reminders` section, add the missing `update_scheduled_checkin` tool and a new `list_all_reminders` tool that returns a unified list of both tasks and check-ins with human-readable recurrence labels, and add a `reminders_bundling` shared slot teaching bots to prefer folding new reminders into existing ones based on judgment rather than time windows.

## Decisions already made (do not re-litigate)

1. **`BotProfile` is a Python dataclass** in `app/bots/prompts/profiles/<bot_id>.py`. One module per bot, exporting a single module-level `PROFILE: BotProfile` instance. Not TOML, not a DB row, not a registry of dicts. Python because bot-specific section bodies are multi-line strings that benefit from triple-quoted literals next to the data.

2. **The registry lives at `app/bots/prompts/registry.py`** and exposes:
   - `PromptSlot` frozen dataclass with fields `name: str`, `body: str`, `audiences: frozenset[str]`, `order: int`.
   - `register(slot: PromptSlot) -> PromptSlot` — adds to a module-level list, raises `ValueError` on duplicate `name`.
   - `slots_for(bot_id: str) -> list[PromptSlot]` — slots routed to `bot_id`, sorted by `(order, name)` for stable output.
   - `render_slots_for(bot_id: str, *, only: Iterable[str] | None = None) -> str` — concatenates `"\n" + slot.body + "\n"` per slot so the output is byte-identical to today's mount style.
   - Module-level constant `ALL_BOTS: frozenset[str] = frozenset({"mediator", "solo_coach", "hector", "habits", "tante_rosi"})` for ergonomics.
   - At the bottom of the file, a side-effect import of `app.bots.prompts.slots` to populate the registry.

3. **Slot modules live in `app/bots/prompts/slots/<slot_name>.py`** and each calls `register(PromptSlot(...))` at import time. The package `__init__.py` imports every submodule so the registry is populated by side effect. Slot files are import-safe (no I/O, no DB).

4. **Bot ids are fixed strings:** `"mediator"`, `"solo_coach"`, `"hector"`, `"habits"`, `"tante_rosi"`. Define once in `registry.py` as `ALL_BOTS`; bot profiles reference their own id explicitly.

5. **Canonical section ordering is one ordered sequence applied to every bot.** Define a `SECTION_ORDER` list in `registry.py` where each entry is either a profile field name (renders `getattr(profile, field)` if non-empty) or a registry slot name (renders the slot if `bot_id` is in its audiences). The renderer walks `SECTION_ORDER` and emits each step. Profile fields that are empty/None are skipped. The order:

   | order | kind | name |
   |---|---|---|
   | 100 | profile field | `role_summary` |
   | 200 | profile field | `persona` |
   | 300 | profile field | `voice` |
   | 400 | profile field | `not_a` |
   | 500 | profile field | `domain_safety` |
   | 600 | profile field | `operating_principles` |
   | 700 | profile field | `knowledge_primitives` |
   | 720 | registry slot | `body_image_eating_safety` |
   | 740 | registry slot | `adherence_board_rules` |
   | 760 | registry slot | `knowledge_primitives_rules` |
   | 780 | registry slot | `commitment_flow_rules` |
   | 800 | registry slot | `scheduling` |
   | 850 | registry slot | `reminders_bundling` |
   | 900 | registry slot | `partner_nudge` |
   | 950 | profile field | `partner_sharing_opt_in_section` (conditional on partner_share state) |
   | 1000 | registry slot | `reply_discipline` |
   | 1100 | profile field | `custom_tail` |

6. **`BotProfile` shape** (dataclass in `app/bots/prompts/profile.py`, frozen):

   ```python
   @dataclass(frozen=True)
   class BotProfile:
       bot_id: str                                       # one of ALL_BOTS
       assistant_name_default: str
       role_summary: str                                 # required
       persona: str = ""                                 # optional biography
       voice: str = ""                                   # bot-specific tone rules
       not_a: str = ""                                   # "what you are not" block
       domain_safety: str = ""                           # bot-specific safety (Rosi red flags, Hector medical defer, etc.)
       operating_principles: str = ""                    # bot-specific operating rules (mediator transparency, etc.)
       knowledge_primitives: str = ""                    # bot-specific examples/quotes for primitives
       partner_sharing_opt_in_section: str = ""          # mounted only when partner_share == 'opt_in'
       custom_tail: str = ""                             # free-form tail for anything that doesn't fit
   ```

   Section bodies are pre-formatted prompt text. Templating (`{assistant_name}`, `{user_name}`, etc.) happens at render time, not in the dataclass.

7. **Each bot's existing renderer (`render_system_prompt`) keeps its public signature.** Internally it loads its `BotProfile`, then delegates to a single shared `render_profile(profile, *, assistant_name, user_name, partner_share, **kwargs) -> str` helper in `app/bots/prompts/profile.py` that walks `SECTION_ORDER` and substitutes templating placeholders. The five existing `render_system_prompt` functions become thin wrappers that pick the profile, evaluate optional branches (e.g. partner_share opt-in), and call the shared renderer.

8. **Slot inventory** (with audiences and order numbers):

   | Slot name | Audiences | Order | Source content |
   |---|---|---|---|
   | `body_image_eating_safety` | `hector`, `habits` | 720 | Combined neutral version of the body-image / eating-disorder safety paragraphs from `hector.py:122-135` and `habits.py:105-118` (near-identical today; pick the more general phrasing where they differ). |
   | `adherence_board_rules` | `hector`, `habits` | 740 | "Distinguish unknown from missed" + excused-vs-missed + low-key pressure bullets from `hector.py:142-160` and `habits.py:121-147` (the *neutral* rules, not the per-bot example quotes — those stay in `BotProfile.operating_principles`). |
   | `knowledge_primitives_rules` | `hector`, `habits` | 760 | The neutral type-definition list (memories / observations / commitments / events / follow-ups) from `hector.py:163-190` and `habits.py:149-180`. Per-bot example phrasing stays in `BotProfile.knowledge_primitives`. Tante Rosi has a different primitives set (pregnancy_state + open_asks) — she does NOT get this slot, her primitives stay in her profile's `knowledge_primitives`. |
   | `commitment_flow_rules` | `hector`, `habits` | 780 | "When the user states a plan / accepts a plan / reports adherence / weekly review" — the neutral rules (call create_commitment on concrete plans; ask clarifying Q on vague; use list_commitments before create on accept; never invent commitment_id; log_event on adherence). Per-bot example quotes ("I am going to work out Monday to Friday" vs "I am going to meditate every morning before coffee") stay in `BotProfile.knowledge_primitives` or `custom_tail`. |
   | `scheduling` | `ALL_BOTS` | 800 | Move existing body from `app/bots/prompts/scheduling.py` into `app/bots/prompts/slots/scheduling.py`. Extend body to reference `update_scheduled_checkin` and `list_all_reminders` (the two new tools added in this sprint). Keep the trigger-phrases and time-field guidance verbatim. |
   | `reminders_bundling` | `ALL_BOTS` | 850 | NEW slot. See "reminders_bundling slot text" section below for the canonical body. |
   | `partner_nudge` | `ALL_BOTS` | 900 | Move existing body from `app/bots/prompts/partner_nudge.py` into `app/bots/prompts/slots/partner_nudge.py` unchanged. |
   | `reply_discipline` | `ALL_BOTS` | 1000 | "One question per reply, maximum. Do not interview. Keep replies short by default. Longer only when there is substance to say." — pulled from the closing bullets of `hector.py:248-249`, `habits.py:234-235`, `tante_rosi.py:228-230`. Apply to mediator and solo_coach too (they have richer reply-style sections in their `BotProfile.custom_tail` / `operating_principles`; this slot is the universal closing reminder, NOT a replacement for their existing Output Style sections). |

9. **Back-compat shims.** The existing module paths `app/bots/prompts/scheduling.py` and `app/bots/prompts/partner_nudge.py` MUST keep exporting `SCHEDULING_CAPABILITY_PROMPT_SLOT` and `PARTNER_NUDGE_PROMPT_SLOT` as thin re-exports from their slot files. **Both constants will resolve to the new (possibly extended) slot bodies, NOT the historical body strings.** Existing tests in `tests/test_scheduling_capability_prompt.py` and `tests/test_partner_nudge_prompt.py` will be updated to match the new bodies (the test that asserts every tool verb appears in the slot must be extended to include `update_scheduled_checkin` and `list_all_reminders`; other assertions about forbidden-phrase absence stay as-is). The contract is "the import path resolves to a non-empty string body equivalent to what the registry would render for that slot." The `_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT` constant in `partner_nudge.py` stays as-is (it is intentionally not mounted; leave the draft text in the old file or move it to a clearly-named module — either is fine, but it MUST remain unmounted). Before declaring back-compat done, grep for `SCHEDULING_CAPABILITY_PROMPT_SLOT ==` and `PARTNER_NUDGE_PROMPT_SLOT ==` to verify no caller does an exact-equality check that would break against an extended body.

10. **New tool: `update_scheduled_checkin`.** Mirrors `update_scheduled_task` for user-facing one-off check-ins. Lives in `app/services/tools/write_tools.py` next to `update_scheduled_task`. Args: `task_id` (the check-in's stable id), optional `when` / `local_when` / `delay` (one of the three; same time-field semantics as `schedule_checkin`), optional `message` (replacement user-facing message — stored in `scheduled_jobs.context['message']` per the existing `schedule_checkin` write path in `app/services/checkins.py`). Updates the row in `scheduled_jobs` where `job_type = 'checkin'` (note: the literal DB value is `'checkin'`, not `'task'`). Registered in `app/services/tools/registry.py` with a description matching the style of `update_scheduled_task`'s description. Added to the appropriate per-bot tool allowlists (every bot that has `schedule_checkin` gets `update_scheduled_checkin`). **Single-pending-checkin invariant:** today `_schedule_once` in `app/services/checkins.py:34-48` supersedes any existing pending check-in row when a new one is scheduled, so there is at most one pending check-in per `(user_id, bot_id, topic_id)`. The new tool performs an in-place UPDATE on that row by id and does NOT add a new row — the invariant is preserved. Tests cover: time update, message update, both, attempting to update a row where `job_type != 'checkin'` → reject, attempting to update a cancelled/fired row → reject, attempting to update an id that does not belong to the calling scope → reject.

11. **New tool: `list_all_reminders`.** Returns a single unified list of both pending agent-managed tasks AND pending user-facing check-ins for the current `(user_id, bot_id, topic_id)`. Lives in `app/services/tools/read_tools.py` next to `list_scheduled_checkins`. Registered in `app/services/tools/registry.py`. Query filters `status = 'pending' AND job_type IN ('scheduled_task', 'checkin')` — note the literal DB values: `'scheduled_task'` (NOT `'task'`) for agent-managed tasks, and `'checkin'` for user-facing check-ins. Other `job_type` values (`'heartbeat'`, `'watch_item_due'`, `'oob_review'`) are NOT returned. Return shape — one row per pending item, ordered by `next_fire_utc` ascending:

    ```
    {
      "id": str,                       # scheduled_jobs.id
      "kind": "task" | "checkin",      # external API: 'scheduled_task' DB value maps to 'task' here; 'checkin' stays 'checkin'
      "next_fire_local": str,          # human-readable, e.g. "Mon 14 May 09:00"
      "next_fire_utc": str,            # ISO8601 with tz
      "recurrence_label": str,         # see decision 12
      "recurrence_rule": dict | None,  # structured form for the bot to pass back; matches normalize_recurrence() output (always None for checkins, since they are one-off)
      "brief": str | None,             # what the bot wrote (for tasks; from context['brief'])
      "message": str | None,           # what the user sees (for checkins; from context['message'])
    }
    ```

    No `limit` parameter; this is the "give me the full picture" call. Default ordering ascending by next fire. Existing `list_scheduled_tasks` and `list_scheduled_checkins` STAY — they are referenced from elsewhere in the codebase (see `app/services/tools/audit.py`, `app/services/tools/registry.py:1080`). The new tool is the bot's bundling-decision surface.

12. **`recurrence_label` format — intuitive read AND structured input.** Two parallel fields:
    - `recurrence_label: str` — human-readable, the bot reads it. **Derived from BOTH `recurrence_rule` AND the row's `scheduled_for` (cast to the user's local timezone) — HH:MM is NOT in the rule; it comes from `scheduled_for`.** The rule provides cadence (`type`, `interval`, `weekdays`); `scheduled_for` provides the clock time. Canonical strings:
      - One-off (`recurrence_rule is None`): `"one-off"`.
      - Daily at a fixed local time (`type='daily', interval=1`): `"daily at HH:MM local"`.
      - Weekly on a single named day (`type='weekly', interval=1, weekdays=[X]`): `"weekly Mon HH:MM local"`.
      - Weekly on multiple named days (`type='weekly', interval=1, weekdays=[X,Y,Z]`): `"weekly Mon+Wed+Fri HH:MM local"`.
      - Hourly (`type='hourly', interval=1`): `"hourly"`.
      - Interval-based (`type='daily', interval>1`): `"every N days at HH:MM local"`. Similar for `hourly` (`"every N hours"`) and `weekly` (`"every N weeks Mon HH:MM local"`).
      - Fallback if a rule doesn't match any pattern above: pretty-print the structured rule alongside the local time.
    - `recurrence_rule: dict | None` — exactly the canonical v1 dict returned by `normalize_recurrence()` in `app/services/scheduled_task_recurrence.py` (fields: `version=1`, `type`, `interval`, optional `until`, `remaining_occurrences`, `weekdays`, `cancelled`). For check-ins this is always `None`. The bot passes the dict back VERBATIM to `update_scheduled_task` when changing recurrence — never re-derives it from the label. Document the exact dict shape (and reference `normalize_recurrence` as the source of truth) in the docstring of `list_all_reminders`.

13. **Hot-context `Upcoming reminders` renders the task id.** Each line in `app/services/hot_context.py:1301-1316` and `app/services/hot_context_solo.py:1201-1218` includes the id as a short bracketed suffix in BOTH renderers. **Format:** ``- {when} [{job_type}] [id={id}] — {brief_or_message}``. Keep `[{job_type}]` (not `[{kind}]`) — that's what the renderer uses today and there is no reason to rename it cross-cuttingly. The id rendered is the `scheduled_jobs.id` value already returned by `_fetch_upcoming_items`. **Additionally, fix `_fetch_upcoming_items` (`hot_context_solo.py:332-336`) to also read `context['message']` for check-in rows** — today it only falls through to `brief`/`reason`/`kind`, so check-in rows render with the id but an empty/garbage trailing text. Updated extraction: prefer `context['brief']`, else `context['message']`, else `context['reason']`, else `context['kind']`. Existing fixtures / golden tests for hot-context output must be updated; the planner should grep for "Upcoming reminders" in `tests/` and enumerate every affected test before declaring done.

14. **`reminders_bundling` slot text** (canonical body — extend only if you must, do not soften):

    > Before booking a new reminder, look at the `Upcoming reminders` section in the hot context and consider running `list_all_reminders` for the full picture. Ask: could the new intent ride on an existing reminder — by broadening its brief, by folding it into a morning or evening check-in that already covers several things, or by updating its time? Prefer one richer reminder that does several jobs over many narrow ones. This is judgment, not a fixed time window: two reminders ten minutes apart may legitimately be separate, while two reminders six hours apart may legitimately belong together. When you bundle, use `update_scheduled_task` for agent-managed tasks or `update_scheduled_checkin` for user-facing check-ins, and tell the user concisely what you folded in.

15. **Per-bot tool allowlists.** `update_scheduled_checkin` is added to every bot that already has `schedule_checkin`. `list_all_reminders` is added to every bot that already has `schedule_checkin` OR `schedule_task`. Surveying which bots have which: planner reads `app/services/tools/registry.py` allowlist sections and the per-bot configurations.

16. **Slot ordering is enforced by `(order, name)`.** Two slots at the same `order` value sort by name. Don't introduce duplicate orders in the canonical list above; reserve gaps of at least 20 between adjacent items so future slots can wedge in.

17. **`app/bots/prompts/partner_sharing.py` is left untouched.** Inspect it — today it is a near-empty module shell (a docstring and `from __future__ import annotations`, no exports, no consumers). It is NOT imported anywhere in the codebase. Do not delete it; do not extend it; do not route it through the registry. The `partner_sharing_opt_in_section` profile field's content comes from the existing bot-specific constants (`_PARTNER_SHARE_OPT_IN_V1` in `hector.py:252-267`, in `habits.py:238-252`, and in `tante_rosi.py:234-249`). Those bodies move into each bot's `BotProfile.partner_sharing_opt_in_section`. The skeleton module `partner_sharing.py` stays on disk as-is, unmodified.

## Files known to be relevant (planner should read at least these)

The planner should still survey the repo, but these are the focal points:

**Prompt registry / profiles (new + existing):**
- `app/bots/prompts/scheduling.py` — current location of `SCHEDULING_CAPABILITY_PROMPT_SLOT`; becomes a thin re-export.
- `app/bots/prompts/partner_nudge.py` — current location of `PARTNER_NUDGE_PROMPT_SLOT`; becomes a thin re-export. Note the `_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT` constant must remain unmounted.
- `app/bots/prompts/hector.py`, `app/bots/prompts/habits.py`, `app/bots/prompts/tante_rosi.py` — current bot prompt files; their `render_system_prompt` keeps its public signature, internals delegate to the shared `render_profile`.
- `app/services/prompts.py` (mediator) — internally restructure to use the same `BotProfile` + `render_profile` flow. Keep `render_system_prompt`'s public signature.
- `app/services/prompts_solo.py` (solo_coach) — same.

**Tools:**
- `app/services/tools/registry.py` — register new tools, update descriptions; check existing `update_scheduled_task` description as the template for `update_scheduled_checkin`.
- `app/services/tools/write_tools.py` — `update_scheduled_task` is the implementation template for `update_scheduled_checkin`.
- `app/services/tools/read_tools.py` — `list_scheduled_checkins` is the implementation template for `list_all_reminders`. Note the TODO comment about `list_scheduled_tasks` location at line 1205-1210; leave that TODO unaddressed in this sprint (out of scope).
- `app/services/tools/audit.py` — has `_summary_list_scheduled_tasks` / `_summary_list_scheduled_checkins`. Add a parallel `_summary_list_all_reminders` and wire it in the dispatch dict at line 178-179.
- `app/services/scheduled_task_recurrence.py` — current recurrence shape; planner must document it in the `list_all_reminders` docstring so the bot knows what to pass back via `recurrence_rule`.
- `app/services/scheduled_jobs.py`, `app/services/scheduled_job_handlers.py` — context on how `scheduled_jobs` rows are written / fired; needed for `update_scheduled_checkin`'s validation rules.

**Hot context:**
- `app/services/hot_context.py:1301-1316` — `Upcoming reminders` renderer (mediator/dyad path).
- `app/services/hot_context_solo.py:279-352` — `_fetch_upcoming_items` (returns `id`).
- `app/services/hot_context_solo.py:1201-1218` — `Upcoming reminders` renderer (solo path).

**Tests:**
- `tests/test_scheduling_capability_prompt.py` — guards the existing scheduling slot. Must still pass without modification (back-compat re-export).
- `tests/test_partner_nudge_prompt.py` — guards the existing partner-nudge slot. Must still pass without modification.
- Existing prompt tests for each bot (`tests/test_hector_prompt.py`, `tests/test_habits_prompt.py`, etc. — verify these exist and update or extend as needed).
- Existing hot-context render tests — must be updated for the id-in-`Upcoming reminders` change.

## Invariants to enforce (must hold)

1. **No prompt regression for existing bots without explicit reason.** After the refactor, each bot's rendered system prompt MUST contain every paragraph that was present before — voice rules, persona, operating principles, "what you are not," domain safety, knowledge primitives, examples, partner-sharing branch, reply discipline. Acceptable changes are: ordering normalised to the canonical `SECTION_ORDER`, exact whitespace between sections may differ (one blank line between sections is fine). Unacceptable changes: dropped paragraphs, dropped bullets, dropped example quotes. The audit step must verify this. For each bot, the test suite should include a "no-content-lost" assertion that the rendered prompt contains key phrases from every original section.

2. **Back-compat constants stay exported.** `from app.bots.prompts.scheduling import SCHEDULING_CAPABILITY_PROMPT_SLOT` and `from app.bots.prompts.partner_nudge import PARTNER_NUDGE_PROMPT_SLOT` MUST resolve to non-empty string bodies equivalent to what the registry would render for those slots. The scheduling slot's body is extended in this sprint to reference `update_scheduled_checkin` and `list_all_reminders`; the constant resolves to the extended body. Tests in `tests/test_scheduling_capability_prompt.py` (and `tests/test_partner_nudge_prompt.py` if it has any equivalent verb-set assertion) are updated to assert the new verbs appear in the slot. Other test assertions about forbidden-phrase absence stay as-is.

3. **Audience routing is correct.** Every slot's `audiences` set MUST match the inventory in decision 8. A slot must NOT render for a bot not in its audiences. Test: for every (bot_id, slot_name) pair, assert the rendered prompt either contains or excludes the slot body based on audiences.

4. **Slot registration is duplicate-safe.** `register()` raises `ValueError` if two slots share a `name`. The slots package's `__init__.py` imports each slot module exactly once.

5. **`update_scheduled_checkin` only touches check-ins.** Attempting to call it with a `task_id` that points to a `scheduled_jobs` row where `job_type != 'checkin'` must reject with a clear error. Symmetric to `update_scheduled_task`'s task-only enforcement.

6. **`list_all_reminders` is read-only and scoped.** Returns only rows where `user_id`, `bot_id`, and `topic_id` match the calling context, and `status = 'pending'`. Does not return cancelled or fired rows.

7. **`recurrence_label` is computed, never stored.** The structured `recurrence_rule` is the source of truth (it's what `schedule_task` writes today). The label is derived at read time. If a label and a rule disagree, the rule wins — and the label-generator should be fixed.

8. **The `reminders_bundling` slot text is unchanged from decision 14.** Don't soften it to "you may consider" — the slot text as written is the contract. If the planner thinks it should be softer or harder, raise it as a question rather than changing the text unilaterally.

9. **`update_scheduled_checkin` is wired into the `scheduling` slot's verb list.** The slot's body in `app/bots/prompts/slots/scheduling.py` must explicitly list `update_scheduled_checkin` alongside `update_scheduled_task` so bots know it exists. Same for `list_all_reminders`.

10. **No new dependency on bot id strings outside `registry.py`.** Bot files reference `ALL_BOTS` or a specific subset by importing from `registry.py`, not by hardcoding the set elsewhere. Profile files declare their own `bot_id` once.

## Edge cases and ordering concerns

The planner should treat these as real, not paranoid:

- **Slot file import order.** `app/bots/prompts/slots/__init__.py` imports each submodule. Alphabetical is fine. Slot files MUST be import-safe (no DB calls, no environment-dependent state at import time). The registry's slot-imports happen at the bottom of `registry.py` so that any caller of `register` / `render_slots_for` triggers slot registration as a side effect.

- **Bot profiles import the registry.** This means `app/bots/prompts/profiles/<bot>.py` → `app/bots/prompts/registry.py` → `app/bots/prompts/slots/__init__.py` → each slot module. Watch for circular imports: slot modules MUST NOT import from profiles, and profiles MUST NOT import from slot modules directly (use the registry instead).

- **Tante Rosi has open_asks and pregnancy_state primitives.** She does NOT share the `knowledge_primitives_rules` slot (it's `hector` + `habits` only). Her `knowledge_primitives` profile field carries her pregnancy-specific primitives section verbatim from today's prompt.

- **Mediator and solo_coach have richer reply-style sections.** Their existing "Output Style" sections (mediator: `prompts.py:231-241`, solo: `prompts_solo.py:350-373`) stay in their `BotProfile.custom_tail` (or `operating_principles` — pick one and be consistent). The `reply_discipline` slot is the universal closing reminder added at order 1000; it does not replace their richer style guidance.

- **Hector has a `partner_sharing` opt-in section** (`hector.py:252-267`). Habits has a similar one (`habits.py:238-252`). Tante Rosi has one too (`tante_rosi.py:234-249`). These stay bot-specific in `BotProfile.partner_sharing_opt_in_section`. They are mounted conditionally based on `normalize_partner_share_for_privacy(partner_share) == "opt_in"` — the conditional logic stays in each bot's `render_system_prompt` wrapper (which decides what to pass to `render_profile`), not in the shared renderer.

- **The `{topic_display_name}` placeholder is solo_coach-specific.** Today `prompts_solo.py:15-17` interpolates `{topic_display_name}`. Keep that capability — `render_profile` should accept arbitrary `**format_kwargs` and pass them through to `.format()` (or `.replace()`) substitutions across all section bodies. Section bodies that don't use a placeholder are unaffected.

- **The mediator has `{partner_a_name}`, `{partner_b_name}`, and dyadic-specific sections** (`partner_perspective_section`, `cross_thread_section`). These are mediator-specific; they go into `BotProfile.domain_specific` for mediator or into `custom_tail`. Pick one consistently. The mediator's `render_system_prompt` continues to evaluate these branches and passes results into `render_profile` via section overrides or format kwargs.

- **Idempotency of `register()`.** If a slot module is imported twice somehow (e.g. a test reload), `register` should raise. This makes import-order bugs surface loudly instead of producing a duplicated slot in the registry.

- **Test isolation.** Tests that import the registry get the full slot set populated by side effect. There's no "clean registry" fixture — slots are static module-level state. Tests should assert on `slots_for(bot_id)` results rather than poking the internal list.

- **Existing `scheduled_jobs.context` shape.** The `update_scheduled_checkin` implementation must understand how `schedule_checkin` writes the message into the row today. If the message lives in `context['message']` (or similar), update that field; if it lives in a column, update the column. Survey `app/services/checkins.py` (referenced from `scheduled_job_handlers.py:14`) for the exact write path.

- **`list_all_reminders` and the existing `max_total=5` in `_fetch_upcoming_items`.** That cap is for hot-context rendering only. `list_all_reminders` has no such cap — it returns every pending row for the scope.

- **Audit fidelity.** When extracting `body_image_eating_safety` / `adherence_board_rules` / `knowledge_primitives_rules` / `commitment_flow_rules`, pick the more general phrasing where Hector and Habits diverge. Log any non-trivial drift in the PR description so it can be reviewed. If the divergence is large enough that one shared body would distort either bot's intent, leave the section in each bot's profile and don't extract.

## Explicitly out of scope

- Moving `list_scheduled_tasks` from `read_tools.py` to `write_tools.py` (the existing TODO at `read_tools.py:1210` stays untouched).
- The autonomous-judgment partner-nudge variant (`_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT` in `partner_nudge.py`). Leave it unmounted.
- Adding new bots. The pattern must accommodate them but no new bot is added in this sprint.
- UI surfaces. There is no UI for reminders today and none is added here.
- Changing the on-disk schema of `scheduled_jobs` (status flags, retention, etc.).
- Per-topic granularity (slots are by `bot_id`, not by `(bot_id, topic_id)`).
- Bake-off / comparing the new prompt rendering against the old one in production. Test-level byte/content checks are the gate, not a live A/B.
- New tooling for measuring how often bots actually bundle vs. stack reminders post-rollout. (Worth doing later; out of scope here.)
- Refactoring mediator's `phase A / phase B / record` structure or solo_coach's equivalents — only the *prompt assembly* is refactored, not the conversational loop.
- **Cross-partner reminder visibility on the mediator path.** `hot_context.py:1051-1061` calls `_fetch_upcoming_items` scoped to `user_id=user.id` only — the partner's pending check-ins/tasks for the same topic are NOT surfaced. This means on the mediator side the bundling judgment is partial (the bot sees the current user's reminders but not the partner's). Acknowledged limitation; bundling decisions are still better than today's zero-awareness baseline. Fixing this would require extending the fetch to also pull the partner's pending items with provenance prefixing — explicitly deferred to a future sprint.

## Success criteria

Reviewer should check, in priority order:

**must (block merge if any fail):**
- `tests/test_scheduling_capability_prompt.py` is updated to assert the slot mentions `update_scheduled_checkin` and `list_all_reminders` in addition to the existing verb set, and passes. `tests/test_partner_nudge_prompt.py` passes unchanged (the partner-nudge body is NOT extended in this sprint).
- `app.bots.prompts.scheduling.SCHEDULING_CAPABILITY_PROMPT_SLOT` and `app.bots.prompts.partner_nudge.PARTNER_NUDGE_PROMPT_SLOT` resolve to non-empty string bodies equivalent to what the registry renders for those slots.
- For each of the five bots, the rendered system prompt contains every key phrase from the pre-refactor prompt (no content lost). New tests in `tests/test_bot_profile_render.py` (or per-bot extensions) assert this.
- `register()` raises `ValueError` on duplicate slot name.
- `slots_for(bot_id)` returns slots filtered by audience and sorted stably by `(order, name)`.
- The `Upcoming reminders` section in hot context renders the row id in a consistent format across `hot_context.py` and `hot_context_solo.py`. Existing hot-context tests updated to match.
- `update_scheduled_checkin` updates time and/or message on a pending check-in by id, rejects task rows, rejects cancelled / fired rows.
- `list_all_reminders` returns the unified shape described in decision 11, ordered ascending by `next_fire_utc`, both `recurrence_label` and `recurrence_rule` populated.
- The `scheduling` slot body lists `update_scheduled_checkin` and `list_all_reminders` alongside the existing verbs.
- The `reminders_bundling` slot body matches the canonical text in decision 14 (verbatim, modulo whitespace).
- The `reply_discipline` slot is mounted into all five bots and contains both "one question per reply" and "keep replies short" guidance.

**should:**
- Per-bot profile files live at `app/bots/prompts/profiles/<bot_id>.py` and export `PROFILE: BotProfile` plus a `render_system_prompt` thin wrapper that preserves the existing public signature for each bot.
- The audit notes in the PR description list every paragraph that was extracted into a shared slot, and any paragraph where Hector and Habits had non-trivial wording drift that was resolved by picking one phrasing.
- `list_all_reminders` has a docstring documenting the exact `recurrence_rule` dict shape so future bots can pass it back to `update_scheduled_task`.
- Per-bot tool allowlists are updated to include `update_scheduled_checkin` (where `schedule_checkin` exists) and `list_all_reminders` (where any scheduling tool exists).

**nice:**
- A short developer-facing comment at the top of `app/bots/prompts/registry.py` explaining the canonical section ordering and how to add a new slot.
- Slot files use frozenset literals for `audiences` rather than `frozenset({...})` calls where possible, for readability.
- The `recurrence_label` generator handles at least the five forms enumerated in decision 12 (one-off, daily, weekly single-day, weekly multi-day, hourly, every-N-units); unknown shapes pretty-print the rule with no crash.

## Notes for the planner

- The work is mostly mechanical refactor + a careful audit + two well-shaped new tools + a hot-context tweak. The hardest single piece is the audit (which paragraphs are "the same rule" vs "the same voice in different words"). Decision 8 enumerates the exact slot inventory so the audit at run-time is constrained, not open-ended.
- Five renderers change shape. The shared `render_profile` helper is small (walk `SECTION_ORDER`, emit each step). Each bot's `render_system_prompt` becomes ~10-15 lines wrapping it.
- Existing prompt tests for hector / habits / tante_rosi (if they exist) probably assert specific phrases — survey them before refactoring so the no-content-lost assertion is informed by what's already covered.
- The mediator's `render_system_prompt` is the most complex caller (multiple dyadic substitutions, partner perspective block, cross-thread state). Migrate it last, after the simpler bots have proven the shape.
- Suggested migration order: registry + back-compat shims → slot moves (scheduling, partner_nudge into new locations with re-exports) → BotProfile dataclass + render_profile helper → hector profile + render_system_prompt refactor (pilot) → habits, then tante_rosi, then solo_coach, then mediator → slot extractions (body_image_eating_safety, adherence_board_rules, knowledge_primitives_rules, commitment_flow_rules, reply_discipline) → update_scheduled_checkin tool → list_all_reminders tool → hot-context id surfacing → reminders_bundling slot → final test pass.
