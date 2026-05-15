"""Pydantic models for the live-voice agenda surface (Sprint 1).

These mirror the ``mediator.conversation_items`` schema in migration 0042 so
that round-tripping a schema-validated Opus output into the DB is a thin
mapping.  Enums match the CHECK constraints character-for-character; any
divergence is a bug.

Notes:

* ``id`` is a string here so Opus can produce stable item ids that internal
  references (``next_item_ids``) resolve to *before* DB insert.  The
  orchestrator maps these stable string ids to real ``uuid`` PKs at persist
  time.
* ``theme_id`` is optional and refers to an existing ``mediator.themes`` row.
  When set it must resolve at persist time; the prep validator checks this.
* The ``Agenda.validate_internal_refs`` model-level check enforces that
  every ``next_item_ids`` entry points at a real item id within the same
  agenda.  This is the "every next_item_ids[] resolves" gate from the
  briefing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ItemKind = Literal["planned", "dynamic", "thread"]
ItemPriority = Literal["must", "should", "optional"]
SpeakerScope = Literal["primary", "partner", "both"]
CoverageEvidence = Literal[
    "explicit_answer",
    "emotional_shift",
    "concrete_decision",
    "blocker_named",
]


class AgendaItem(BaseModel):
    """One row in the prep-time agenda checklist.

    Maps 1:1 onto ``mediator.conversation_items`` columns (minus DB-only
    fields like ``id`` -> ``uuid``, ``status``, ``covered_at``).
    """

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(..., min_length=1, max_length=200)
    intent: str | None = Field(default=None, max_length=400)
    ask: str | None = Field(default=None, max_length=400)
    done_when: str | None = Field(default=None, max_length=400)
    kind: ItemKind = "planned"
    priority: ItemPriority = "should"
    speaker_scope: SpeakerScope = "primary"
    coverage_evidence_required: CoverageEvidence = "explicit_answer"
    next_item_ids: list[str] = Field(default_factory=list)
    theme_slug: str | None = Field(default=None, max_length=80)
    order_hint: int = 0


class Agenda(BaseModel):
    """An Opus-produced prep agenda.

    ``items`` is non-empty; ``first_item_id`` must reference an item in
    ``items`` and is used to seed ``conversations.current_item_id`` at
    persist time.
    """

    prep_summary: str = Field(..., min_length=1, max_length=2000)
    items: list[AgendaItem] = Field(..., min_length=1, max_length=24)
    first_item_id: str

    @model_validator(mode="after")
    def validate_internal_refs(self) -> "Agenda":
        ids = {item.id for item in self.items}
        if len(ids) != len(self.items):
            raise ValueError("agenda item ids must be unique")
        if self.first_item_id not in ids:
            raise ValueError(
                f"first_item_id={self.first_item_id!r} does not resolve to any agenda item"
            )
        for item in self.items:
            for ref in item.next_item_ids:
                if ref not in ids:
                    raise ValueError(
                        f"item {item.id!r}.next_item_ids contains unknown id {ref!r}"
                    )
        # At least one 'must' item exists (otherwise Haiku has no anchor to
        # advance from).
        if not any(item.priority == "must" for item in self.items):
            raise ValueError("agenda must contain at least one 'must' item")
        return self


class PrepRequest(BaseModel):
    """Inputs to :func:`prep.produce_agenda`."""

    user_id: str
    bot_id: str
    steering_text: str | None = None
    topic_slug: str | None = None


class PrepResult(BaseModel):
    """Return value of :func:`prep.produce_agenda`."""

    session_id: str
    agenda: Agenda
    items_persisted: int
    current_item_id: str  # DB UUID of the row matching first_item_id
