# Prompt Audit Inventory — T1

## Mediator section enumeration (app/services/prompts.py)

The mediator template (`SYSTEM_PROMPT_V1` / `SYSTEM_PROMPT_V3`) has TEN sections between
`# Surfacing The Partner's Perspective` and `# Output Style`:

| Order | Line (~v1) | Section |
|---|---|---|
| 1 | 131 | `# Surfacing The Partner's Perspective` (`{partner_perspective_section}` placeholder) |
| 2 | 134 | `# Bridge Candidates` (or `# Partner Bridges` in v2/v3) |
| 3 | 142 | `# Tool Usage Philosophy` |
| 4 | 151 | `# Scheduling Judgment` |
| 5 | 159 | `# Multi-Message Handling` |
| 6 | 167 | `# Voice Notes And Transcription Artifacts` |
| 7 | 173 | `# In-Person Redirection` |
| 8 | 202 | `# Conversation Closure` |
| 9 | 222 | `# Crisis Handling` |
| 10 | 231 | `# Output Style` |

Confirmed: TEN sections, not five, not seven.

## Tante Rosi primitives — stay in-profile

Tante Rosi's knowledge primitives (lines 194–221 in `tante_rosi.py`) define:
- Pregnancy state (EDD, dating basis, scan correction, birth, loss, termination)
- Memories (appointment logistics, support setup, broad preferences, milestones, constraints)
- Observations (recurring worries, what helps, what overwhelms, repeated needs)
- Open asks (missing pregnancy setup facts)
- Follow-ups / scheduled tasks

She does NOT have hector/habits-style commitments or events. Her primitives stay in her
`BotProfile.knowledge_primitives` field; she is EXCLUDED from the
`knowledge_primitives_rules` shared slot (audiences = `{hector, habits}` only).

## Four extractable shared paragraphs — Hector vs Habits diff

### 1. `body_image_eating_safety` (hector.py:122–135 / habits.py:105–118)

Both bots have a 4-bullet section: avoid body-image escalation, no weigh-ins/photos default,
no calorie-counting pressure, eating-disorder redirection.

| Diff point | Hector | Habits | Resolution |
|---|---|---|---|
| First bullet | "Do not compliment weight loss in a way that ties worth to appearance." | "Do not compliment weight or appearance changes in a way that ties worth to looks." | Pick Habits: "weight or appearance changes" is broader. |
| Second bullet | "Do not make progress photos or weigh-ins default." | "Do not make weigh-ins or measurements default." | Pick Habits: "weigh-ins or measurements" is broader. |
| Third bullet | "Avoid calorie-counting pressure unless the user asks for it. Nutrition commitments should be positively framed (eat at home, cook dinner) rather than negatively framed (don't eat this, restrict that)." | "Avoid calorie-counting pressure unless the user explicitly asks for it. Food-related habits should be positively framed (eat at home, cook dinner, eat enough) rather than negatively framed (restrict, cut)." | Merge: "explicitly asks" is the more careful phrasing; "Food-related" is more general than "Nutrition"; "(restrict, cut)" vs "(don't eat this, restrict that)" — pick Habits which is more general. |
| Fourth bullet | Identical | Identical | No diff. |

**Resolution:** Use Habits phrasing (more general/neutral), with `explicitly` for the calorie-counting gate.

### 2. `adherence_board_rules` (hector.py:142–160 / habits.py:121–147)

Both have the same core bullets: distinguish unknown from missed, unknown creates subtle
pressure, missed acknowledged plainly, excused different from missed, keep pressure
low-key, prefer one concrete next action, respect constraints.

| Diff point | Hector | Habits | Resolution |
|---|---|---|---|
| "Keep pressure real but low-key" elaboration | "You are the friend who notices when someone stops showing up and asks why." | "You are the second pair of eyes the user asked for." | Both are per-bot voice. Strip from shared slot; keep in each bot's profile `operating_principles` or `voice`. |
| "Calibrate pressure to the practice" (lines 138-141 in habits) | Absent from Hector | Present in Habits: "Some practices (meditation, sleep hygiene) are intrinsically about letting go rather than pushing through... firm pressure on a meditation slot should never read as a contradiction of the practice itself." | Habits-only nuance. Keep in Habits profile's `operating_principles`; do NOT extract to shared slot. |
| "Prefer one concrete next action" example | "Wednesday morning, same time?" | "Tomorrow morning, same time?" | Per-bot example. Strip from shared slot. |
| "Respect constraints" examples | "If the user can only train in the mornings..." "If their knee gets cranky after running..." | "If the user can only do their practice in the mornings..." "If a particular ritual reliably backfires..." | Per-bot examples. Strip from shared slot. |

**Resolution:** Extract only the neutral rule bullets (distinguish unknown from missed, unknown creates subtle pressure, missed acknowledged plainly, excused different from missed, keep pressure real but low-key, prefer one concrete next action, respect constraints) without the per-bot elaborations or examples.

### 3. `knowledge_primitives_rules` (hector.py:163–190 / habits.py:149–180)

Both have a 5-item type-definition list (memories, observations, commitments, events,
follow-ups), plus "a single message can justify more than one durable update" paragraph,
plus "before adding or updating durable state..." paragraph, plus a privacy-safety closing.

| Diff point | Hector | Habits | Resolution |
|---|---|---|---|
| Memories examples | "the bench is near the computer, or the user protects a dog walk with their wife" | "meditation happens on the cushion in the corner before coffee" | Per-bot examples. Strip from shared slot; keep in profile `knowledge_primitives`. |
| Observations examples | "once work starts, later workouts often get crowded out" | "once the phone is in hand, the practice rarely happens" | Per-bot examples. Strip. |
| "A single message can justify more than one durable update" examples | "weekday workout before opening the laptop, minimum twenty minutes" may create/update a commitment while "laptop pulls me into work too fast" may become an observation | "ten minutes every morning before coffee, minimum five" may create/update a commitment while "the cushion in the corner is the spot that actually works" may become a memory | Per-bot examples. Strip. |
| Privacy closing | "Keep medical, injury, body-image, and eating-disorder-sensitive details private and conservative." | "Keep medical, mental-health, and body-image-sensitive details private and conservative." | Habits is slightly broader ("mental-health"). Pick Habits phrasing. |
| "Do not save diagnoses or clinical conclusions" | Present | Present | Identical. |

**Resolution:** Extract the neutral type-definition list (memories, observations, commitments, events, follow-ups — the generic descriptions without per-bot examples), the "single message" paragraph without examples, the "before adding" paragraph, and the privacy closing. Per-bot example quotes stay in each profile's `knowledge_primitives`.

Tante Rosi is EXCLUDED — her primitives are pregnancy-specific (pregnancy_state, memories, observations, open_asks, follow-ups) and live in her profile.

### 4. `commitment_flow_rules` (hector.py:196–247 / habits.py:181–233)

Both have four subsections: "When The User States A Plan", "When The User Accepts A Proposed Plan", "When The User Reports Adherence", "Weekly Review".

| Diff point | Hector | Habits | Resolution |
|---|---|---|---|
| "States a plan" concrete example | "I am going to work out Monday to Friday." | "I am going to meditate every morning before coffee." | Per-bot. Strip. |
| "States a plan" vague example | "I need to get healthier." | "I want to be more present." | Per-bot. Strip. |
| "States a plan" clarifying question | "What are we actually putting on the board this week: workouts, food, or both?" | "What are we actually putting on the board this week: a daily sit, no phone after dinner, something else?" | Per-bot. Strip. |
| "Reports adherence" example | "Got the lift in this morning." | "Got the sit in this morning." | Per-bot. Strip. |
| "Reports adherence" confirmation | "Logged. Monday handled." | "Logged. Monday handled." | Identical. |
| "Weekly review" example | "Week was 3/5 workouts and 4/5 food. That is not perfect, but it is a real week. Same target next week, or do we make the workout plan three days and stop pretending Friday is available?" | "Week was 5/7 sits. That's not perfect, but it's a real week. Same target next week, or do we dial it back to weekdays only?" | Per-bot. Strip. |

The neutral rules (call `create_commitment` on concrete plans; ask clarifying Q on vague; use `list_commitments` before `create` on accept; never invent `commitment_id`; call `log_event` on adherence; weekly review using adherence data) are identical in substance.

**Resolution:** Extract the neutral rules without per-bot example quotes. Per-bot example quotes stay in each profile's `knowledge_primitives` or `custom_tail`.

## Slot inventory summary

| Slot name | Audiences | Order | Source lines (hector/habits) |
|---|---|---|---|
| `body_image_eating_safety` | `{hector, habits}` | 720 | hector:122-135, habits:105-118 |
| `adherence_board_rules` | `{hector, habits}` | 740 | hector:142-160, habits:121-147 |
| `knowledge_primitives_rules` | `{hector, habits}` | 760 | hector:163-190, habits:149-180 |
| `commitment_flow_rules` | `{hector, habits}` | 780 | hector:196-247, habits:181-233 |
| `scheduling` | `ALL_BOTS` | 800 | scheduling.py:17-43 |
| `reminders_bundling` | `ALL_BOTS` | 850 | NEW |
| `partner_nudge` | `ALL_BOTS` | 900 | partner_nudge.py:16-42 |
| `reply_discipline` | `ALL_BOTS` | 1000 | hector:248-249, habits:234-235, tante_rosi:228-230 |
