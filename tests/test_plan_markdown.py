"""Unit tests for app.services.live.plan_markdown.

Covers:
- Round-trip: markdown_to_agenda → agenda_to_display
- Priority assignment (first='must', rest='should')
- Default speaker_scope and coverage_evidence_required values
- ID uniqueness and sequential naming
- first_item_id and next_item_ids chain
- ValueError on empty input
- prep_summary None/empty coercion
"""

import pytest

from app.services.live.plan_markdown import agenda_to_display, markdown_to_agenda


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_PLAN = """\
1. Intro and check-in
2. Review last week
3. Plan for next week
"""

BULLETED_PLAN = """\
- First topic
- Second topic
- Third topic
"""

MIXED_PLAN = """\
1. Priority item
- Bulleted item
2. Another numbered
"""


# ---------------------------------------------------------------------------
# Parsing / round-trip
# ---------------------------------------------------------------------------


def test_round_trip_numbered():
    agenda = markdown_to_agenda(SIMPLE_PLAN, prep_summary=None)
    display = agenda_to_display(agenda.items)
    lines = display.strip().splitlines()
    assert len(lines) == 3
    assert lines[0] == "1. Intro and check-in"
    assert lines[1] == "2. Review last week"
    assert lines[2] == "3. Plan for next week"


def test_round_trip_bulleted():
    agenda = markdown_to_agenda(BULLETED_PLAN, prep_summary=None)
    display = agenda_to_display(agenda.items)
    lines = display.strip().splitlines()
    assert len(lines) == 3
    assert lines[0] == "1. First topic"
    assert lines[1] == "2. Second topic"
    assert lines[2] == "3. Third topic"


def test_round_trip_mixed():
    agenda = markdown_to_agenda(MIXED_PLAN, prep_summary=None)
    assert len(agenda.items) == 3
    display = agenda_to_display(agenda.items)
    lines = display.strip().splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("1.")
    assert lines[1].startswith("2.")
    assert lines[2].startswith("3.")


# ---------------------------------------------------------------------------
# Priority assignment
# ---------------------------------------------------------------------------


def test_first_item_is_must():
    agenda = markdown_to_agenda(SIMPLE_PLAN, prep_summary=None)
    assert agenda.items[0].priority == "must"


def test_rest_are_should():
    agenda = markdown_to_agenda(SIMPLE_PLAN, prep_summary=None)
    for item in agenda.items[1:]:
        assert item.priority == "should"


def test_single_item_is_must():
    agenda = markdown_to_agenda("1. Solo topic", prep_summary=None)
    assert len(agenda.items) == 1
    assert agenda.items[0].priority == "must"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_speaker_scope_default_both():
    agenda = markdown_to_agenda(SIMPLE_PLAN, prep_summary=None)
    for item in agenda.items:
        assert item.speaker_scope == "both"


def test_coverage_evidence_required_default():
    agenda = markdown_to_agenda(SIMPLE_PLAN, prep_summary=None)
    for item in agenda.items:
        assert item.coverage_evidence_required == "explicit_answer"


# ---------------------------------------------------------------------------
# IDs and chaining
# ---------------------------------------------------------------------------


def test_ids_are_sequential():
    agenda = markdown_to_agenda(SIMPLE_PLAN, prep_summary=None)
    for i, item in enumerate(agenda.items, start=1):
        assert item.id == f"item-{i}"


def test_ids_are_unique():
    agenda = markdown_to_agenda(SIMPLE_PLAN, prep_summary=None)
    ids = [item.id for item in agenda.items]
    assert len(ids) == len(set(ids))


def test_first_item_id():
    agenda = markdown_to_agenda(SIMPLE_PLAN, prep_summary=None)
    assert agenda.first_item_id == "item-1"


def test_next_item_ids_chain_forward():
    agenda = markdown_to_agenda(SIMPLE_PLAN, prep_summary=None)
    items = agenda.items
    # Each item except the last should point to the next
    for i in range(len(items) - 1):
        assert items[i].next_item_ids == [f"item-{i + 2}"]
    # Last item has no successors
    assert items[-1].next_item_ids == []


def test_single_item_no_next():
    agenda = markdown_to_agenda("1. Only topic", prep_summary=None)
    assert agenda.items[0].next_item_ids == []


# ---------------------------------------------------------------------------
# order_hint
# ---------------------------------------------------------------------------


def test_order_hint_sequential():
    agenda = markdown_to_agenda(SIMPLE_PLAN, prep_summary=None)
    for i, item in enumerate(agenda.items, start=1):
        assert item.order_hint == i


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_empty_string_raises():
    with pytest.raises(ValueError, match="at least one"):
        markdown_to_agenda("", prep_summary=None)


def test_whitespace_only_raises():
    with pytest.raises(ValueError, match="at least one"):
        markdown_to_agenda("   \n\n  ", prep_summary=None)


def test_no_recognised_lines_raises():
    with pytest.raises(ValueError, match="at least one"):
        markdown_to_agenda("Just a sentence without a list marker.", prep_summary=None)


# ---------------------------------------------------------------------------
# prep_summary coercion
# ---------------------------------------------------------------------------


def test_none_prep_summary_coerced():
    """None prep_summary must produce a valid Agenda (min_length=1 satisfied)."""
    agenda = markdown_to_agenda("1. Topic", prep_summary=None)
    assert len(agenda.prep_summary) >= 1


def test_empty_prep_summary_coerced():
    """Empty string prep_summary is coerced to a single space."""
    agenda = markdown_to_agenda("1. Topic", prep_summary="")
    assert len(agenda.prep_summary) >= 1


def test_non_empty_prep_summary_preserved():
    summary = "Talk about the project status."
    agenda = markdown_to_agenda("1. Topic", prep_summary=summary)
    assert agenda.prep_summary == summary


def test_whitespace_only_prep_summary_coerced():
    """Whitespace-only prep_summary is treated as falsy and coerced."""
    agenda = markdown_to_agenda("1. Topic", prep_summary="   ")
    # Coerced to " " which is non-empty
    assert len(agenda.prep_summary) >= 1
