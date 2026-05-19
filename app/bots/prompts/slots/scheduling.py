"""Scheduling capability prompt slot (order 800, all bots).

Teaches every bot its full scheduling toolset including the two new tools
added in this sprint: ``update_scheduled_checkin`` and ``list_all_reminders``.

.. note::

   ``cancel_scheduled_checkin`` currently cancels by user-only scope (does
   NOT accept a ``job_id`` parameter).  Bots that see specific check-in rows
   via ``list_all_reminders`` should understand this asymmetry: a check-in id
   visible in the unified list cannot be precision-cancelled — use
   ``update_scheduled_checkin`` to reschedule or update message content
   instead.
"""

from __future__ import annotations

from app.bots.prompts.registry import ALL_BOTS, PromptSlot, register

SCHEDULING_BODY = """\
You have a full set of scheduling tools. Use them; do not refuse a
request you can fulfil, and do not tell the user to set a reminder
somewhere outside of this conversation.

Available verbs:
- `schedule_checkin` — one-off user-facing future message.
- `schedule_task` — agent-managed brief with daily/weekly/hourly
  recurrence.
- `list_scheduled_tasks` — see pending agent-managed tasks.
- `list_scheduled_checkins` — see pending user-facing check-ins.
- `list_all_reminders` — unified pending tasks AND check-ins, ordered
  by next fire. Check before booking to decide whether to bundle.
- `update_scheduled_task` — change brief, time, or recurrence.
- `update_scheduled_checkin` — change time or message by job_id.
- `cancel_scheduled_task` — drop a pending agent-managed task.
- `cancel_scheduled_checkin` — drop a pending user-facing check-in
  (user scope only; no job_id).

Trigger phrases for scheduling: "weekly check-in", "remind me every
Monday", "check in with me tomorrow at 9am", "stop the daily reminders",
"what reminders do I have".

Pick the time-field that fits: `delay` for relative durations ("in two
hours"), `local_when` for local clock phrases ("9pm tonight", "Monday
at 8"), absolute `when` only when you hold an exact instant. If timing
is ambiguous, ask ONE clarifying question, then book it — never punt to
another tool.
""".strip()

register(
    PromptSlot(
        name="scheduling",
        body=SCHEDULING_BODY,
        audiences=ALL_BOTS,
        order=800,
    )
)
