from uuid import uuid4

import pytest


def test_tool_schemas_registry_importable() -> None:
    from tool_schemas import TOOL_REGISTRY

    assert TOOL_REGISTRY


def test_check_oob_schema_accepts_optional_protected_owner_ids() -> None:
    from uuid import uuid4

    from tool_schemas import CheckOOBInput

    recipient_id = uuid4()
    sender_id = uuid4()

    payload = CheckOOBInput(content="draft", recipient_id=recipient_id, protected_owner_ids=[sender_id, recipient_id])
    schema = CheckOOBInput.model_json_schema()

    assert payload.protected_owner_ids == [sender_id, recipient_id]
    assert "protected_owner_ids" in schema["properties"]
    assert "recipient-only compatibility" in schema["properties"]["protected_owner_ids"]["description"]


def test_oob_row_schema_exposes_safe_summary_not_sensitive_core() -> None:
    from tool_schemas import OOBRow

    schema = OOBRow.model_json_schema()

    assert "protected_summary" in schema["properties"]
    assert "sensitive_core" not in schema["properties"]


def test_bridge_candidate_tools_are_registered_with_exact_enums() -> None:
    from pydantic import ValidationError

    from app.services.tools.registry import READ_PHASE_TOOLS, TOOL_DISPATCH, WRITE_PHASE_TOOLS
    from tool_schemas import (
        BridgeCandidateSensitivity,
        BridgeCandidateStatus,
        BridgeCandidateKind,
        CreateBridgeCandidateInput,
        ListBridgeCandidatesInput,
        SendBridgeCandidateInput,
        TOOL_REGISTRY,
        UpdateBridgeCandidateInput,
    )

    assert {"list_bridge_candidates"} <= READ_PHASE_TOOLS
    assert {
        "create_bridge_candidate",
        "update_bridge_candidate",
        "send_bridge_candidate",
    } <= WRITE_PHASE_TOOLS
    for name in (
        "list_bridge_candidates",
        "create_bridge_candidate",
        "update_bridge_candidate",
        "send_bridge_candidate",
    ):
        assert name in TOOL_REGISTRY
        assert name in TOOL_DISPATCH

    assert {item.value for item in BridgeCandidateStatus} == {
        "pending",
        "ready",
        "sent",
        "declined",
        "blocked",
        "addressed",
        "expired",
    }
    assert {item.value for item in BridgeCandidateSensitivity} == {"low", "medium", "high"}
    assert {item.value for item in BridgeCandidateKind} == {
        "context",
        "clarification",
        "contradiction",
        "repair",
        "vulnerability",
        "process",
    }

    source_id = uuid4()
    target_id = uuid4()
    message_id = uuid4()
    CreateBridgeCandidateInput(
        source_user_id=source_id,
        target_user_id=target_id,
        kind="repair",
        sensitivity="low",
        source_message_ids=[message_id],
        shareable_summary="A repair is available.",
    )
    ListBridgeCandidatesInput(status="ready")
    UpdateBridgeCandidateInput(candidate_id=uuid4(), status="addressed")
    SendBridgeCandidateInput(candidate_id=uuid4())

    with pytest.raises(ValidationError):
        CreateBridgeCandidateInput(
            source_user_id=source_id,
            target_user_id=target_id,
            kind="repair",
            sensitivity="low",
            source_message_ids=[message_id],
            shareable_summary="Invalid lifecycle.",
            status="offered",
        )
