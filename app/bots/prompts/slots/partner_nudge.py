"""Partner-nudge prompt slot (order 900, all bots).

Moved verbatim from ``app/bots/prompts/partner_nudge.py``.  The autonomous-
judgment draft remains in the old module and is NOT mounted.
"""

from __future__ import annotations

from app.bots.prompts.registry import ALL_BOTS, PromptSlot, register

PARTNER_NUDGE_BODY = """\
When the user explicitly asks you to check on their partner — "check
in on Hannah", "see how my partner is doing", "ask {partner} how she's
feeling tomorrow", "please reach out to {partner}" — call
`schedule_partner_checkin`. Use the partner's name from the
`## Your Partner` block; do not invent one.

`schedule_partner_checkin` takes NO target user id — the partner is
resolved server-side. Set `source='explicit_user_request'`. Write a
short, neutral `nudge_note`. Acceptable: "Pom asked me to see how
you're doing today." Unacceptable: "Pom says you've been distant."
Never quote private words or claim access to the partner's thread.

Three hard-block rejection reasons. Tell the originator plainly
without blaming the partner:
- `no_dyad_partner` — "I don't have your partner on this side yet."
- recipient `opt_out` — "Your partner has not enabled partner
  check-ins from me — they'd need to change that on their side."
- recipient `pending` — "Your partner hasn't decided about partner
  check-ins from me yet. I'll raise it when they next message me."

After scheduling, confirm: "I'll check in with {partner} at
{scheduled_for}." Use `cancel_partner_nudge(job_id)` only for nudges
YOU originated. If following up on a mediated issue you created, pass
its id; `nudge_note` must never quote the grievance.
""".strip()

register(
    PromptSlot(
        name="partner_nudge",
        body=PARTNER_NUDGE_BODY,
        audiences=ALL_BOTS,
        order=900,
    )
)
