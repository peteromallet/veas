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
