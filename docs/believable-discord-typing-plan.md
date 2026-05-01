# Believable Discord Typing Plan

## Goal

Make the Véas Discord bot feel like it is composing in real time without turning typing into a fake animation layer. The implementation should use Discord's typing endpoint only through the existing pacer path, start visible typing early while the agent is genuinely preparing a reply, pause when the user is typing, and avoid repeated full pre-answer waits around incremental `send_message_part` chains.

The plan is intentionally lightweight. It should fit the current `app/services/pacer.py`, `app/main.py`, `app/services/agentic.py`, `app/services/turn_context.py`, `app/services/tools/read_tools.py`, and `app/services/messaging.py` boundaries rather than adding a separate typing service or transport abstraction.

## Scope

In scope:

- Live Discord inbound turns handled through `BurstCoalescer`, `DiscordPacer`, and the paced agentic turn path.
- Discord typing pulses emitted by `discord.send_typing` through `DiscordPacer._send_bot_typing_pulse`.
- Final answer sends from `app/services/agentic.py`.
- Incremental Discord message parts sent through the `send_message_part` read tool and `send_outbound_part`.
- Runtime user typing state reported by Discord gateway `TYPING_START` events and stored in `DiscordPacer`.

Out of scope:

- WhatsApp typing behavior.
- Catch-up, recovery, media-derived non-live work, reactions, silence decisions, scheduled jobs, and other offline or operational sends.
- A fake explicit "stop typing" call. Discord typing indicators are pulse-based and expire automatically.
- Replacing the coalescer, outbound delivery, or tool system.

## Settled Decisions

- **SD-001** — Keep typing control inside `DiscordPacer`. _load_bearing: true_
  Rationale: `DiscordPacer` already owns user typing state, bot pulse throttling, pacing preferences, sleeps, and `pacing_events` observability.

- **SD-002** — Send bot typing only through `_send_bot_typing_pulse`. _load_bearing: true_
  Rationale: That helper is the existing anti-spam gate for per-user pulse spacing and the place where typing starts are recorded.

- **SD-003** — Treat Discord typing as expiring pulses, not a start/stop session API. _load_bearing: true_
  Rationale: Discord exposes a typing pulse endpoint; clients drop the indicator after a short visible period. The app can wait to create off-gaps, but it cannot force a remote stop.

- **SD-004** — Limit believable typing to live Discord inbound work. _load_bearing: true_
  Rationale: Catch-up, recovery, media-derived non-live work, reactions, silence, and WhatsApp sends should not create Discord typing pulses.

- **SD-005** — Treat a multi-part Discord reply as one composition session. _load_bearing: true_
  Rationale: The first visible bubble can use normal answer typing, but later `send_message_part` calls are continuations and must never each pay a full answer-typing delay.

- **SD-006** — Avoid answer-sized typing delays for tiny or follow-up text. _load_bearing: false_
  Rationale: Very short acknowledgements, emoji-like replies, and one-line connective bubbles feel robotic when they wait like full paragraphs. Follow-up bubbles can still use a very short pulse when Discord allows it.

- **SD-007** — Add behavior as helper methods on `DiscordPacer`, not as a new service. _load_bearing: true_
  Rationale: The existing pacer already has the clock, sleep injection, settings, preference loading, user typing state, bot pulse history, and persistence hooks needed for deterministic tests.

- **SD-008** — Use a contextual paced-send callback instead of a transport abstraction. _load_bearing: true_
  Rationale: The current call graph only needs to distinguish `final`, `incremental_first`, and `incremental_next`; widening that hook is smaller and clearer than introducing a new delivery layer.

- **SD-009** — Preserve `send_outbound_part(...)` safety ordering. _load_bearing: true_
  Rationale: Pause checks and OOB checks must still happen before any paced typing wait, so withheld or blocked text does not create a misleading typing indicator.

- **SD-010** — Use gateway `TYPING_START` as the only user-typing signal. _load_bearing: true_
  Rationale: The existing `mark_user_typing(...)` and `typing_state(...)` runtime state is sufficient; polling or a second Discord transport state would add race conditions without improving the UX.

## Current Lifecycle

The live Discord path starts in the Discord gateway and inbound persistence code, then enters `BurstCoalescer` with `source="live"`. When Discord pacing is enabled, `BurstCoalescer` asks `DiscordPacer.decide(...)` whether to wait, react, silence, or answer.

For live answer decisions, `app/main.py` routes through `_run_paced_agentic_turn(...)`. That wrapper can start `perform_thinking_typing_until_stopped(...)` while the agentic turn is preparing. It also passes a `before_paced_send(answer_text)` callback into `run_agentic_turn_with_metadata(...)`.

Inside `app/services/agentic.py`, pacing metadata causes `send_typing_indicator=False`, suppressing the low-level `discord.send_text(..., send_typing_indicator=True)` behavior. Final sends then call `before_paced_send(sendable_text)` immediately before provider delivery, so `DiscordPacer.perform_answer_typing(...)` can pause for user typing and emit human-feeling pre-answer pulses.

Incremental sends follow the same broad callback shape. During Phase A, the `send_message_part` tool in `app/services/tools/read_tools.py` builds a `before_provider_send` lambda from `ctx.before_paced_send(content)` when paced Discord typing is active, then calls `send_outbound_part(...)`. `app/services/messaging.py` runs OOB checks first, calls `before_provider_send()` immediately before inserting and delivering the outbound part, and finally calls Discord with `send_typing_indicator=False`.

## Existing Guardrails

The current implementation already has important guardrails that should be preserved:

- `DiscordPacer.decide(...)` answers immediately for crisis, charged, media, catch-up, and recovery work before optional model judgement can react or silence.
- User typing is runtime state from gateway `TYPING_START` events via `mark_user_typing(...)`; stale typing state expires through `typing_grace_s`.
- `perform_answer_typing(...)` suppresses bot typing while the user appears to be composing, waits in bounded `typing_extend_s` chunks, and stops waiting at `max_typing_wait_s`.
- `_send_bot_typing_pulse(...)` refuses to send when no Discord typing function is configured or when `_typing_gap_remaining_s(...)` says the per-user minimum pulse gap has not elapsed.
- Pacer decisions and typing starts/stops/waits are recorded in `pacing_events`, which is the audit stream for rollout review.
- Agentic paced sends disable Discord's default provider-level typing indicator to avoid duplicate, uncoordinated typing calls.
- `send_message_part` checks for newer inbound messages before the first incremental send and again after the configured inter-part delay, returning `interrupted` when a live turn has been overtaken.

## Discord Pulse Semantics

Discord's typing API is a pulse. Calling `send_typing(channel_id)` asks the client to show a typing indicator for a short period; there is no companion endpoint that explicitly stops it.

The app currently models that by assuming a visible duration (`discord_pacing_typing_visible_s`), enforcing a minimum per-user gap (`discord_pacing_typing_pulse_min_gap_s`), and adding an off-gap (`discord_pacing_typing_off_gap_s`) before a later pulse when a long wait needs more than one visible indicator. This is the right mental model for the implementation plan: send sparse pulses, let them expire, and avoid a pattern where every outbound message attempts to refresh typing.

The minimum pulse gap is especially important. It prevents API spam and lets the Discord client visibly drop the indicator before a later pulse. Any new typing behavior must preserve this as the hard gate instead of calling `discord.send_typing` directly.

## Key Flaw: Coarse Send Hook Around Incremental Parts

The current `before_paced_send(text)` callback is too coarse for `send_message_part` chains. It receives only text, so `DiscordPacer.perform_answer_typing(...)` treats every call as a standalone final answer.

That creates the FLAG-001 failure mode:

- The first `send_message_part` may correctly stop the thinking-typing task, wait while the user is typing, and perform normal answer typing before sending the first visible bubble.
- A later `send_message_part` calls the same hook again with no context that it is part two or part three of the same composition session.
- `perform_answer_typing(...)` recalculates a full answer delay for the later part. If the previous pulse is still inside `discord_pacing_typing_pulse_min_gap_s`, `_send_bot_typing_pulse(...)` cannot emit a visible pulse, but `perform_answer_typing(...)` can still sleep through the remaining delay.
- The user can therefore experience either repeated robotic "typing before every bubble" or worse, a silent wait before a later bubble where no new typing indicator is visible because the anti-spam gap correctly blocked the pulse.

The flaw is not in `send_outbound_part(...)` ordering. It is correct that OOB checks happen before paced waiting and that `before_provider_send()` runs immediately before delivery. The flaw is that the callback contract has no `send_kind` or `part_index`, so the pacer cannot distinguish a final answer, the first incremental bubble, and later incremental bubbles in one live Discord turn.

## Target State

Believable typing should be a small extension of the current paced Discord answer path:

- Live Discord inbound turns can show typing while the agent is preparing and immediately before text is delivered.
- Non-live work does not gain typing. The same exclusions from the opening scope remain hard boundaries: catch-up, recovery, media-derived non-live sends, reactions, silence, scheduled jobs, operational sends, and WhatsApp.
- Provider-level Discord typing stays disabled for paced agentic sends through `send_typing_indicator=False`; the pacer remains the only coordinated typing source.
- `DiscordPacer` decides whether a specific outbound text is worth answer-sized paced typing. Tiny messages should not pay a paragraph-like delay, but later incremental bubbles may still show a short pulse unless a user-typing pause or pulse gap blocks it.
- Multi-part replies are treated as one composition session. The first visible send may use the same answer-typing behavior as a final answer. Later parts use short, bounded inter-part rhythm and may attempt a pulse only when `_send_bot_typing_pulse(...)` says the per-user gap allows it.
- The user typing pause remains authoritative. If the user is actively composing, the bot should wait up to the configured cap before typing or sending, then continue only after re-checking interruption where the caller already has that authority.

This target state should feel like: "the bot starts thinking quickly, does not talk over the user, sends the first bubble after a believable composition beat, and then continues with natural chat cadence." It should not feel like: "every bubble triggers a new fake typing animation."

## Pacer Design

Keep the typing controller in `DiscordPacer`. The implementation should add focused helper methods there rather than spreading typing math through `main.py`, `agentic.py`, or `read_tools.py`.

The pacer should own these decisions:

- Whether an answer text is large enough to deserve pre-send typing.
- Whether the current source is eligible for Discord typing at all.
- How long to pause while the user appears to be typing.
- How to size a normal first-send or final-send answer delay.
- How to pace later incremental parts without repeating the normal answer delay.
- Whether a typing pulse can be sent now under `_typing_gap_remaining_s(...)`.

The existing `_send_bot_typing_pulse(...)` remains the only function that can call `discord.send_typing`. New helper methods may choose not to pulse, may wait, and may record `pacing_events`, but they must not bypass `_send_bot_typing_pulse(...)` or reset `_last_bot_typing_at` themselves.

Tiny-message suppression should be deterministic and conservative if implemented. It means "do not perform answer-sized pre-send typing for this text." It does not mean "ignore active user typing," and it does not prevent later incremental bubbles from using a short pulse when that makes the send feel visible and natural.

For multi-part replies, the pacer should see one logical composition session:

- `final`: a single final answer when no incremental part has already handled user-visible text.
- `incremental_first`: the first `send_message_part` in a live Discord turn, eligible for normal answer typing.
- `incremental_next`: later `send_message_part` calls in the same turn, eligible only for bounded inter-part pacing and optional pulse attempts.

The core invariant is that `incremental_next` never calls the same full-delay path as `final` or `incremental_first`. If the minimum pulse gap blocks a visible Discord typing pulse, the pacer should not convert that blocked pulse into a long silent wait. Later parts may still use the existing `discord_multi_message_delay_s` rhythm or a similarly bounded pacer-owned wait, but the wait must be short, testable, and independent of answer length.

## Pacer API

Add small, testable helpers to `DiscordPacer` and keep `perform_answer_typing(...)` as a compatibility wrapper only if that reduces churn.

Recommended public surface:

```python
SendKind = Literal["final", "incremental_first", "incremental_next"]

def should_type_for_answer(
    self,
    answer_text: str,
    preferences: Mapping[str, Any],
    *,
    send_kind: SendKind,
) -> bool: ...

async def wait_until_user_stops_typing(
    self,
    user: User,
    channel_id: str,
    preferences: Mapping[str, Any],
    *,
    reason: str,
) -> float: ...

async def perform_send_typing(
    self,
    user: User,
    channel_id: str,
    answer_text: str,
    *,
    send_kind: SendKind,
    part_index: int | None = None,
) -> float: ...
```

`send_kind` should be deliberately narrow. Do not accept arbitrary strings or transport names. The only valid values for this plan are:

- `final`: a normal final response from `agentic.py`.
- `incremental_first`: the first visible `send_message_part` for a turn.
- `incremental_next`: the second or later visible `send_message_part` for the same turn.

`wait_until_user_stops_typing(...)` should contain the existing bounded loop from `perform_answer_typing(...)`: use `typing_state(...)`, sleep in `discord_pacing_typing_extend_s` chunks, record `typing_wait`, and stop at `max_typing_wait_s`. It should return the wait duration so tests can assert bounded behavior.

If implemented, `should_type_for_answer(...)` should centralize tiny-message suppression. It should normally return `False` for `incremental_next` because later parts are rhythm-paced, not answer-paced. The exact threshold can start as a small internal constant or derive from existing settings; add a new config only if the tests prove a setting is needed.

`perform_send_typing(...)` should be the only high-level pre-send typing method used by the paced callback. Its behavior:

- Fetch user pacing preferences once.
- Return `0.0` immediately when preferences are disabled or no Discord typing sender is configured.
- Always run `wait_until_user_stops_typing(...)` before considering bot typing.
- For `final` and `incremental_first`, call `should_type_for_answer(...)`; when true, use the existing `answer_typing_delay_s(...)` and pulse loop.
- For `incremental_next`, use only bounded inter-part pacing. Do not use `answer_typing_delay_s(...)`; do not scale wait length by message length.
- For every visible bot typing pulse, call `_send_bot_typing_pulse(...)`; never call the Discord API directly.

For `incremental_next`, the bounded pacing should be intentionally small. Prefer the existing `discord_multi_message_delay_s` as the first source of truth if it remains in `read_tools.py`; otherwise place the bounded wait in `DiscordPacer` and keep it no larger than the current inter-part delay default. If the pulse gap blocks a new typing pulse, `perform_send_typing(...)` may still wait the short inter-part rhythm, but it must not wait the full answer typing duration.

## Hook Contract

Replace the text-only callback with a contextual callback:

```python
before_paced_send: Callable[
    [str],
    Awaitable[None],
]  # current

before_paced_send(
    text,
    *,
    send_kind: Literal["final", "incremental_first", "incremental_next"],
    part_index: int | None = None,
)
```

In code, define a small alias near `TurnContext` or `agentic.py` to avoid repeating the callable type. Keep the hook local to the agentic turn; do not introduce a cross-provider delivery interface.

Required caller changes:

- `app/services/turn_context.py`: update `TurnContext.before_paced_send` to accept keyword-only `send_kind` and optional `part_index`.
- `app/main.py`: update `_run_paced_agentic_turn(...)` so its nested `before_paced_send(...)` stops the thinking-typing task once, resolves the DM channel, and calls `pacer.perform_send_typing(user, channel_id, text, send_kind=send_kind, part_index=part_index)`.
- `app/services/agentic.py`: when sending final text, call `before_paced_send(sendable_text, send_kind="final", part_index=None)` through the existing `before_provider_send` path.
- `app/services/agentic.py`: for spend-cap fallback text, also pass `send_kind="final"` because it is a single final outbound send.
- `app/services/tools/read_tools.py`: compute `part_index = len(sent_parts) + 1` as it does today, then call the hook with `send_kind="incremental_first"` when `part_index == 1` and `send_kind="incremental_next"` otherwise.
- `app/services/messaging.py`: keep accepting a zero-argument `before_provider_send` callback. It should not learn about `send_kind`; the caller should close over the contextual `before_paced_send(...)` invocation.

The hook should only be installed for paced live Discord turns, matching today's `_run_paced_agentic_turn(...)` guard on `decision.signal_snapshot.get("source") == "live"`. Non-paced sends should continue to rely on provider defaults, and `send_typing_indicator=False` should remain paired with the paced hook to prevent duplicate low-level typing.

## Config And Events

Avoid new configuration unless it is needed to make tests or rollout tuning precise. The current settings already provide most of the necessary inputs:

- `discord_pacing_typing_grace_s`, `discord_pacing_typing_extend_s`, and `discord_pacing_max_typing_wait_s` for user-typing waits.
- `discord_pacing_answer_typing_min_s`, `discord_pacing_answer_typing_max_s`, and `discord_pacing_answer_chars_per_s` for final and first-part answer typing.
- `discord_pacing_typing_pulse_min_gap_s`, `discord_pacing_typing_visible_s`, and `discord_pacing_typing_off_gap_s` for pulse safety.
- `discord_multi_message_delay_s` for later-part rhythm, if the existing delay remains outside the pacer.

If a new tiny-message threshold is necessary, add one narrowly named setting with a conservative default and bounds. Do not add a group of tuning knobs for every send kind.

Persisted `pacing_events` should stay useful for rollout review. Reuse the existing `typing_wait`, `typing_start`, and `typing_stop` decisions where they still fit, and add compact `signal_snapshot` metadata such as `send_kind`, `part_index`, `skipped_tiny`, or `pulse_blocked_by_gap` when it helps explain why no visible typing happened. Do not persist full outbound content in pacing metadata.

## Incremental Send Rhythm

Keep `send_message_part` as a Phase A tool that sends complete chat bubbles, not a streaming primitive. The typing behavior should support that shape:

- Before the first part, stop the thinking-typing task and run `perform_send_typing(..., send_kind="incremental_first", part_index=1)`.
- For the first part, allow normal answer-sized pacing when the text is not tiny and the user is not actively typing.
- Before later parts, use `send_kind="incremental_next"` with the actual `part_index`.
- For later parts, never call `answer_typing_delay_s(...)` and never sleep in proportion to the part text length.
- Later parts may keep the existing `discord_multi_message_delay_s` rhythm, but that delay should be the upper bound for ordinary inter-part pacing unless a user-typing wait is active.
- Later parts may attempt a Discord typing pulse only when `_send_bot_typing_pulse(...)` allows it. If the pulse gap blocks the attempt, send after the short rhythm rather than waiting silently for a full answer delay.

`read_tools.py` should remain responsible for computing `part_index`, `part_key`, and the `incremental_first` versus `incremental_next` distinction. That keeps the pacer free of tool-specific `sent_parts` state while still giving it enough context to choose the right rhythm.

The `send_outbound_part(...)` ordering in `messaging.py` should not move:

1. Return duplicates before doing any new typing work.
2. Reject non-Discord providers before typing.
3. Check system and user pause hooks before typing.
4. Run the OOB hook before typing.
5. Only then invoke `before_provider_send()`.
6. Insert and deliver the outbound message.

This ordering avoids "the bot typed but then nothing appeared" for known local blocks. It also keeps `messaging.py` transport-adjacent rather than pacing-aware: it receives a zero-argument `before_provider_send` callback, runs it at the existing safe point, and does not inspect `send_kind`.

## Interruption Checks

`_newer_inbound_exists(...)` in `read_tools.py` remains the authority for interrupting incremental sends. Do not duplicate that query inside `DiscordPacer`; the pacer should not know about turn-start SQL or tool interruption policy.

For `send_message_part`, the order should be:

1. Check `_newer_inbound_exists(...)` before any delay or typing for the part.
2. For later parts, run the bounded inter-part delay where it already exists or through the pacer.
3. Check `_newer_inbound_exists(...)` again after that delay and before `send_outbound_part(...)`.
4. Let `send_outbound_part(...)` run local delivery gates and call the contextual `before_provider_send()` only after those gates pass.

If the implementation moves inter-part delay into `DiscordPacer.perform_send_typing(...)`, add a re-check in `read_tools.py` after the pacer call and before `send_outbound_part(...)`. The important invariant is that a newer inbound message can stop a pending part before a visible outbound bubble is delivered.

Final sends from `agentic.py` do not currently have the same `_newer_inbound_exists(...)` tool helper. This batch does not add a new final-send interruption system; keep the change scoped to preserving existing incremental interruption behavior.

## User Typing Pause

Continue to derive user-typing state from Discord gateway `TYPING_START` events. `app/services/discord.py` should keep calling `pacer.mark_user_typing(...)`, and the pacer should keep using `typing_state(...)` with `typing_grace_s` to expire stale typing. Do not add polling, direct gateway reads from the agentic loop, or a second typing-state cache.

`wait_until_user_stops_typing(...)` should apply to all paced send kinds, including tiny messages and later incremental parts. Tiny-message suppression only skips bot typing animation; it must not make the bot talk over a user who is actively composing.

The wait must stay capped:

- Sleep in chunks no larger than `discord_pacing_typing_extend_s`.
- Stop once `typing_state(...)` is stale or absent.
- Stop once total typing wait reaches `max_typing_wait_s`.
- Record each wait chunk with enough metadata to explain `send_kind`, `part_index`, and the channel.
- If the cap is reached and the user still appears to be typing, return without sending a bot typing pulse; the caller can proceed to delivery subject to its interruption checks.

This preserves the existing "do not type over the user" behavior while preventing an unbounded stuck turn.

## Test Plan

Use focused tests with fake clocks and fake sleeps. Do not rely on wall-clock sleeps for pacing behavior.

Target files and assertions:

- `tests/test_pacer.py`
  - Add coverage for `should_type_for_answer(...)`: normal text types, tiny text skips answer typing, emoji-like text skips answer typing, and `incremental_next` never uses answer-sized typing.
  - Add coverage for `wait_until_user_stops_typing(...)`: user typing pauses are recorded as `typing_wait`, sleep happens in bounded chunks, and total wait stops at `max_typing_wait_s`.
  - Add coverage for `perform_send_typing(..., send_kind="final")`: preserves current answer typing pulse behavior and still uses `_send_bot_typing_pulse(...)`.
  - Add coverage for `perform_send_typing(..., send_kind="incremental_first")`: behaves like a first visible answer when text is not tiny.
  - Add coverage for `perform_send_typing(..., send_kind="incremental_next")`: uses bounded inter-part pacing, does not call `answer_typing_delay_s(...)`, and does not sleep through a full answer delay when the pulse gap blocks a visible pulse.
  - Assert `pacing_events` metadata includes compact `send_kind`, `part_index`, and skip/block reasons where relevant.

- `tests/test_main_startup_pacing.py`
  - Update the paced-turn test to assert `_run_paced_agentic_turn(...)` passes a contextual `before_paced_send`.
  - Assert the callback calls `perform_send_typing(...)` with `send_kind="final"` for final answer text.
  - Assert thinking typing is stopped before the first paced send and is not restarted between incremental parts.

- `tests/test_agentic_lifecycle.py`
  - Update callback fakes to accept `send_kind` and `part_index`.
  - Assert final outbound sends use `send_kind="final"` and still pass `send_typing_indicator=False` when pacing metadata is present.
  - Extend the existing `send_message_part` lifecycle coverage to verify Phase A incremental calls receive `incremental_first` for part one and `incremental_next` for later parts.
  - Add multi-part coverage where the first part can trigger normal pacing but the second part cannot trigger a full answer delay.

- `tests/test_send_outbound.py`
  - Preserve coverage that Discord provider sends can suppress the low-level typing indicator with `send_typing_indicator=False`.
  - Add or update coverage proving `send_outbound_part(...)` calls `before_provider_send()` only after duplicate/provider/pause/OOB gates pass.
  - Assert `messaging.py` remains unaware of `send_kind`; contextual metadata is closed over by the caller.

- `tests/test_discord.py`
  - Keep gateway `TYPING_START` coverage for `mark_user_typing(...)`.
  - Keep `send_text(..., send_typing_indicator=False)` coverage proving default Discord typing can be suppressed.

- `tests/test_debouncer.py`
  - Keep live typing lifecycle coverage for initial coalescing typing.
  - Add coverage only if the implementation changes when thinking typing starts or stops.

- `tests/test_config.py`
  - Update only if a new tiny-message threshold setting is added.
  - Assert any new setting has a conservative default and bounded validation.

- `tests/test_discord_pacing_docs.py`
  - Add this plan document to docs coverage only if the repo expects implementation plans to be checked.
  - Otherwise leave docs tests focused on operator docs.

## Validation Commands

Run targeted tests before the full suite:

```bash
pytest tests/test_pacer.py tests/test_main_startup_pacing.py tests/test_agentic_lifecycle.py tests/test_send_outbound.py tests/test_discord.py tests/test_debouncer.py
```

If config changes are introduced:

```bash
pytest tests/test_config.py
```

Then run the full suite:

```bash
pytest
```

No test should require a real Discord connection. Use fake `send_typing`, fake clocks, and fake sleeps to verify pulse timing, wait caps, and no-full-delay behavior deterministically.

## Rollout Checks

After tests pass, validate in staging with Discord pacing enabled:

- Inspect `pacing_events` for a live single-bubble answer: expect thinking typing and a final or first-send typing path with no provider-level duplicate typing.
- Inspect `pacing_events` for a tiny answer: expect no answer-sized pre-send typing and clear skip metadata if implemented.
- Inspect `pacing_events` for a multi-part answer: expect one composition session, `incremental_first` for part one, `incremental_next` for later parts, and no repeated full answer delay.
- Inspect logs for Discord typing API calls: pulse frequency should respect `discord_pacing_typing_pulse_min_gap_s`.
- Confirm catch-up, recovery, reactions, silence, and WhatsApp paths do not produce new Discord typing pulses.

Manual smoke test:

1. In a staging Discord DM, send a prompt that naturally asks for three separate bubbles.
2. Watch for early typing while the agent is composing.
3. Confirm the first bubble has a believable pre-send beat.
4. Confirm the second and third bubbles arrive with short natural rhythm, not full answer delays.
5. Start typing as the user before one of the bubbles and confirm the bot pauses up to the cap rather than typing over the user indefinitely.
6. Confirm there is no silent stuck-typing feel when a later pulse is blocked by the minimum gap.

## Execution Order

1. Add the `SendKind` type and pacer helpers in `app/services/pacer.py`.
2. Update the `before_paced_send` callable type in `app/services/turn_context.py`.
3. Update `_run_paced_agentic_turn(...)` in `app/main.py` to call `perform_send_typing(...)`.
4. Update final and fallback sends in `app/services/agentic.py` to pass `send_kind="final"`.
5. Update `send_message_part(...)` in `app/services/tools/read_tools.py` to pass `incremental_first` or `incremental_next` with `part_index`.
6. Preserve `send_outbound_part(...)` hook ordering in `app/services/messaging.py`; change only tests or types there unless implementation requires a tiny adapter.
7. Add targeted tests with fake clocks/sleeps.
8. Run targeted pytest, then full pytest.
9. Deploy to staging and review `pacing_events`, logs, and the manual three-bubble Discord smoke test.

## Success Criteria

- Live Discord inbound turns can show typing while the agent is composing and before substantial sends.
- `send_typing_indicator=False` remains active for paced agentic Discord sends, so provider-level default typing is not duplicated.
- `_send_bot_typing_pulse(...)` remains the only path to Discord `send_typing`.
- Tiny final or first-part messages do not get robotic answer-sized pre-send typing.
- User typing pauses use gateway `TYPING_START` state, are recorded, and are capped.
- `send_message_part` part one can use normal answer pacing; part two and later use bounded inter-part rhythm.
- Later message parts never incur a full answer delay and never wait silently for that full delay when pulse min-gap blocks a visible pulse.
- Incremental interruption checks still happen before typing/delay and again before delivery.
- `send_outbound_part(...)` still runs local delivery gates before invoking `before_provider_send()`.
- Targeted tests and full `pytest` pass.
- Staging `pacing_events` and the manual three-bubble smoke test show believable pulse frequency with no API spam and no stuck silent waits.

## Final Review Checklist

- The only plan artifact for this work is `docs/believable-discord-typing-plan.md`.
- Primary implementation touch points are named: `app/services/pacer.py`, `app/main.py`, `app/services/agentic.py`, `app/services/turn_context.py`, `app/services/tools/read_tools.py`, and `app/services/messaging.py`.
- Discord typing is described as pulse-based with no fake stop endpoint or explicit remote stop API.
- The architecture remains lightweight: focused helpers in `DiscordPacer`, contextual hook metadata, minimal config, no new service, and no broad transport abstraction.
- FLAG-001 is directly addressed: later `send_message_part` calls use `incremental_next`, never repeat full answer typing, and do not create long silent waits when pulse min-gap blocks a visible pulse.
- Live Discord inbound work is the only typing scope; catch-up, recovery, media-derived non-live work, reactions, silence, scheduled jobs, operational sends, and WhatsApp remain outside the typing plan.
- Validation order is preserved: targeted pytest first, config tests only if needed, full `pytest`, then staging `pacing_events` review and the manual three-bubble Discord smoke test.
