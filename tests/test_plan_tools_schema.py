"""Unit tests for plan-tool Pydantic schemas in tool_schemas.py.

Covers:
- Valid payloads accepted
- Malformed payloads rejected (UUID validation, missing required fields)
- CreateConversationPlanInput has NO title field
- ListConversationPlansInput limit validation (ge=1, le=25)
"""

from datetime import datetime, UTC
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from tool_schemas import (
    CreateConversationPlanInput,
    CreateConversationPlanOutput,
    ListConversationPlansInput,
    ListConversationPlansOutput,
    ListConversationPlansRow,
    PlanItem,
    ReadConversationPlanInput,
    ReadConversationPlanOutput,
    UpdateConversationPlanInput,
    UpdateConversationPlanOutput,
)


# ---------------------------------------------------------------------------
# PlanItem
# ---------------------------------------------------------------------------


def test_plan_item_valid():
    item = PlanItem(
        id=uuid4(),
        title="Discuss budget",
        priority="must",
        order_hint=1,
    )
    assert item.priority == "must"


def test_plan_item_invalid_priority():
    with pytest.raises(ValidationError):
        PlanItem(id=uuid4(), title="x", priority="critical", order_hint=0)


def test_plan_item_invalid_uuid():
    with pytest.raises(ValidationError):
        PlanItem(id="not-a-uuid", title="x", priority="should", order_hint=0)


# ---------------------------------------------------------------------------
# ReadConversationPlanInput / Output
# ---------------------------------------------------------------------------


def test_read_input_valid():
    cid = uuid4()
    inp = ReadConversationPlanInput(conversation_id=cid)
    assert inp.conversation_id == cid


def test_read_input_invalid_uuid():
    with pytest.raises(ValidationError):
        ReadConversationPlanInput(conversation_id="not-a-uuid")


def test_read_input_missing_conversation_id():
    with pytest.raises(ValidationError):
        ReadConversationPlanInput()


def test_read_output_valid_empty():
    cid = uuid4()
    out = ReadConversationPlanOutput(conversation_id=cid, status="ready")
    assert out.items == []
    assert out.display_text == ""


def test_read_output_with_items():
    cid = uuid4()
    item = PlanItem(id=uuid4(), title="Topic", priority="should", order_hint=2)
    out = ReadConversationPlanOutput(
        conversation_id=cid,
        status="preparing",
        items=[item],
        display_text="1. Topic",
    )
    assert len(out.items) == 1
    assert out.display_text == "1. Topic"


# ---------------------------------------------------------------------------
# ListConversationPlansInput — limit validation
# ---------------------------------------------------------------------------


def test_list_input_default_limit():
    inp = ListConversationPlansInput()
    assert inp.limit == 5


def test_list_input_valid_limit():
    inp = ListConversationPlansInput(limit=10)
    assert inp.limit == 10


def test_list_input_limit_min_valid():
    inp = ListConversationPlansInput(limit=1)
    assert inp.limit == 1


def test_list_input_limit_max_valid():
    inp = ListConversationPlansInput(limit=25)
    assert inp.limit == 25


def test_list_input_limit_too_low():
    with pytest.raises(ValidationError):
        ListConversationPlansInput(limit=0)


def test_list_input_limit_too_high():
    with pytest.raises(ValidationError):
        ListConversationPlansInput(limit=26)


def test_list_input_negative_limit():
    with pytest.raises(ValidationError):
        ListConversationPlansInput(limit=-1)


# ---------------------------------------------------------------------------
# ListConversationPlansOutput
# ---------------------------------------------------------------------------


def test_list_output_empty():
    out = ListConversationPlansOutput()
    assert out.is_error is False
    assert out.error is None
    assert out.plans == []


def test_list_output_with_plans():
    row = ListConversationPlansRow(
        conversation_id=uuid4(),
        status="ready",
        title="My Plan",
        item_count=3,
        created_at=datetime.now(UTC),
    )
    out = ListConversationPlansOutput(plans=[row])
    assert len(out.plans) == 1
    assert out.plans[0].title == "My Plan"


def test_list_output_error():
    out = ListConversationPlansOutput(is_error=True, error="something failed")
    assert out.is_error is True
    assert out.error == "something failed"


# ---------------------------------------------------------------------------
# CreateConversationPlanInput — no title field
# ---------------------------------------------------------------------------


def test_create_input_has_no_title_field():
    assert not hasattr(CreateConversationPlanInput, "__fields__") or \
        "title" not in CreateConversationPlanInput.model_fields


def test_create_input_title_kwarg_rejected():
    """Pydantic should raise if 'title' is passed (extra fields forbidden or ignored)."""
    # This tests that 'title' is NOT a legitimate field.
    # If the model uses extra='forbid', passing title raises ValidationError.
    # If it uses 'ignore', the attribute won't be set.
    inp = CreateConversationPlanInput(plan_markdown="1. Topic", title="Oops")
    assert not hasattr(inp, "title") or getattr(inp, "title", None) is None


def test_create_input_valid_no_prep():
    inp = CreateConversationPlanInput(plan_markdown="1. Topic A\n2. Topic B")
    assert inp.plan_markdown.startswith("1.")
    assert inp.prep_summary is None


def test_create_input_valid_with_prep():
    inp = CreateConversationPlanInput(
        plan_markdown="1. Status update",
        prep_summary="Focus on Q2 blockers",
    )
    assert inp.prep_summary == "Focus on Q2 blockers"


def test_create_input_missing_plan_markdown():
    with pytest.raises(ValidationError):
        CreateConversationPlanInput()


def test_create_input_empty_plan_markdown():
    with pytest.raises(ValidationError):
        CreateConversationPlanInput(plan_markdown="")


def test_create_output_valid():
    cid = uuid4()
    out = CreateConversationPlanOutput(conversation_id=cid, status="ready")
    assert out.conversation_id == cid
    assert out.items == []
    assert out.display_text == ""


# ---------------------------------------------------------------------------
# UpdateConversationPlanInput / Output
# ---------------------------------------------------------------------------


def test_update_input_valid():
    inp = UpdateConversationPlanInput(
        conversation_id=uuid4(),
        plan_markdown="1. Revised topic",
    )
    assert inp.prep_summary is None


def test_update_input_with_prep_summary():
    cid = uuid4()
    inp = UpdateConversationPlanInput(
        conversation_id=cid,
        plan_markdown="1. Topic",
        prep_summary="Updated context",
    )
    assert inp.conversation_id == cid
    assert inp.prep_summary == "Updated context"


def test_update_input_missing_conversation_id():
    with pytest.raises(ValidationError):
        UpdateConversationPlanInput(plan_markdown="1. Topic")


def test_update_input_invalid_conversation_id():
    with pytest.raises(ValidationError):
        UpdateConversationPlanInput(conversation_id="bad-uuid", plan_markdown="1. Topic")


def test_update_input_missing_plan_markdown():
    with pytest.raises(ValidationError):
        UpdateConversationPlanInput(conversation_id=uuid4())


def test_update_input_empty_plan_markdown():
    with pytest.raises(ValidationError):
        UpdateConversationPlanInput(conversation_id=uuid4(), plan_markdown="")


def test_update_output_valid():
    cid = uuid4()
    out = UpdateConversationPlanOutput(conversation_id=cid, status="preparing")
    assert out.conversation_id == cid
    assert out.items == []
    assert out.display_text == ""
