"""Pure Markdown-to-Agenda converter (Sprint 2 — Discord Agenda Authoring).

Provides two stateless functions that the read/write plan-tool handlers
consume:

* ``markdown_to_agenda`` — parses a user-written numbered list into an
  :class:`~app.services.live.schemas.Agenda`.
* ``agenda_to_display`` — renders agenda items back into a numbered list
  string suitable for bot spoken confirmation.

This module contains **no SQL, no I/O, and no network calls**.  It is ~200
lines of pure Python and relies on the :class:`Agenda` schema's own
validators for correctness enforcement.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.live.schemas import Agenda, AgendaItem

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(
    r"""
    ^
    \s*
    (?:
        (?P<num>\d+)\.\s+  # numbered: 1. Title
        |
        -\s+               # bulleted: - Title
    )
    (?P<title>.+?)
    \s*$
    """,
    re.VERBOSE,
)


def _parse_lines(plan_markdown: str) -> list[dict[str, Any]]:
    """Extract raw (num, title) pairs from every recognised line.

    Returns a list of dicts with keys ``num`` (int or None), ``title`` (str).
    """
    raw: list[dict[str, Any]] = []
    bullet_counter = 0
    for line in plan_markdown.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if m is None:
            # Skip unrecognised lines (e.g. blank lines, blockquotes).
            continue
        if m.group("num"):
            raw.append({"num": int(m.group("num")), "title": m.group("title").strip()})
        else:
            bullet_counter += 1
            raw.append({"num": None, "title": m.group("title").strip()})
    return raw


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def markdown_to_agenda(
    plan_markdown: str,
    prep_summary: str | None,
) -> Agenda:
    """Convert a user-authored plan in markdown to a validated ``Agenda``.

    Parameters
    ----------
    plan_markdown:
        Text containing numbered (``1. ...``) and/or bulleted (``- ...``)
        lines.  Each recognised line becomes one ``AgendaItem``.
    prep_summary:
        Optional steering/prep text.  When *falsy* (``None`` or empty
        string) it is coerced to a single space so that the ``Agenda``
        schema's ``min_length=1`` invariant is satisfied.  **Mode
        derivation** (steered vs open) does NOT use this coerced value; it
        is evaluated against the *caller-supplied* argument.

    Returns
    -------
    Agenda
        A schema-validated agenda with sequential item ids, chained
        ``next_item_ids``, and ``first_item_id`` set to ``"item-1"``.

    Raises
    ------
    ValueError
        If *plan_markdown* yields zero recognised lines.
    """
    raw = _parse_lines(plan_markdown)
    if not raw:
        raise ValueError(
            "plan_markdown must contain at least one numbered (1.) or "
            "bulleted (-) line; received empty or unparseable input"
        )

    items: list[AgendaItem] = []
    for i, entry in enumerate(raw):
        item_id = f"item-{i + 1}"

        # Promote the first item to 'must'; all others stay 'should'.
        priority = "must" if i == 0 else "should"

        # next_item_ids: sequential chain (item-n → [item-n+1]).
        next_ids: list[str] = []
        if i < len(raw) - 1:
            next_ids.append(f"item-{i + 2}")

        items.append(
            AgendaItem(
                id=item_id,
                title=entry["title"],
                kind="planned",
                priority=priority,
                speaker_scope="both",
                coverage_evidence_required="explicit_answer",
                order_hint=i + 1,
                next_item_ids=next_ids,
            )
        )

    # Coerce prep_summary to satisfy Agenda.min_length=1 when falsy.
    # IMPORTANT: mode derivation (steered vs open) is evaluated against the
    # *original, caller-supplied* prep_summary argument — not this coerced
    # value.
    coerced_summary = prep_summary if (prep_summary or "").strip() else " "

    return Agenda(
        prep_summary=coerced_summary,
        items=items,
        first_item_id="item-1",
    )


def agenda_to_display(items: list[AgendaItem]) -> str:
    """Render agenda items as a simple numbered list for display.

    Each line has the form ``1. Title``.
    """
    return "\n".join(
        f"{i}. {item.title}" for i, item in enumerate(items, start=1)
    )
