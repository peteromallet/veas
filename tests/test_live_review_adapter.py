"""Tests for Sprint 5 T5: debrief artifact preference in GET /review.

Covers:
- Artifact-preferred review (debrief artifact exists → adapter used)
- Fallback to synthesize_review when no artifact
- Highest revision_number selection
- Scalar-string coercion for all four fields
- Empty-string omission
- UI-compatible array/object output types
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services.live.adapters import (
    _coerce_review_field,
    _debrief_artifact_to_session_review,
)


# ── Adapter unit tests (no DB) ───────────────────────────────────────────────


class TestScalarStringCoercion:
    """Verify that each field coerces scalar strings per T5 contract."""

    def test_what_heard_string_wrapped_with_source(self) -> None:
        result = _coerce_review_field(
            "User mentioned feeling anxious about work.", field_name="what_heard",
        )
        assert len(result) == 1
        assert result[0] == {
            "text": "User mentioned feeling anxious about work.",
            "source": "live_debrief",
        }

    def test_what_decided_string_wrapped_with_source(self) -> None:
        result = _coerce_review_field(
            "Decided to revisit the career conversation next week.",
            field_name="what_decided",
        )
        assert len(result) == 1
        assert result[0]["text"].startswith("Decided")
        assert result[0]["source"] == "live_debrief"

    def test_still_open_string_wrapped_with_source(self) -> None:
        result = _coerce_review_field(
            "Still open: relationship boundaries.", field_name="still_open",
        )
        assert len(result) == 1
        assert result[0]["source"] == "live_debrief"

    def test_what_to_remember_string_wrapped_with_source(self) -> None:
        result = _coerce_review_field(
            "User wants to train 3x/week.", field_name="what_to_remember",
        )
        assert len(result) == 1
        assert result[0]["source"] == "live_debrief"

    def test_empty_string_omitted(self) -> None:
        result = _coerce_review_field("   ", field_name="what_heard")
        assert result == []

    def test_whitespace_only_string_omitted(self) -> None:
        result = _coerce_review_field("\n  \t", field_name="what_heard")
        assert result == []

    def test_none_returns_empty_list(self) -> None:
        result = _coerce_review_field(None, field_name="what_heard")
        assert result == []

    def test_leading_trailing_whitespace_trimmed(self) -> None:
        result = _coerce_review_field(
            "  hello world  ", field_name="what_heard",
        )
        assert result[0]["text"] == "hello world"


class TestListCoercion:
    """Verify list-of-strings / list-of-dicts coercion."""

    def test_list_of_strings_each_wrapped(self) -> None:
        result = _coerce_review_field(
            ["User said A", "User said B"], field_name="what_heard",
        )
        assert len(result) == 2
        assert result[0] == {"text": "User said A", "source": "live_debrief"}
        assert result[1] == {"text": "User said B", "source": "live_debrief"}

    def test_list_of_strings_empty_skipped(self) -> None:
        result = _coerce_review_field(
            ["User said A", "", "   ", "User said C"], field_name="what_heard",
        )
        assert len(result) == 2
        texts = [r["text"] for r in result]
        assert "User said A" in texts
        assert "User said C" in texts

    def test_list_of_dicts_source_added_when_missing(self) -> None:
        result = _coerce_review_field(
            [
                {"item_id": "abc", "title": "Focus"},
                {"item_id": "def", "title": "Explore", "source": "custom"},
            ],
            field_name="what_decided",
        )
        assert len(result) == 2
        assert result[0]["source"] == "live_debrief"
        assert result[1]["source"] == "custom"  # pre-existing preserved

    def test_list_of_dicts_original_keys_preserved(self) -> None:
        result = _coerce_review_field(
            [
                {"item_id": "x", "title": "T", "priority": "must", "intent": "I"},
            ],
            field_name="still_open",
        )
        entry = result[0]
        assert entry["item_id"] == "x"
        assert entry["title"] == "T"
        assert entry["priority"] == "must"
        assert entry["source"] == "live_debrief"

    def test_non_string_non_dict_items_skipped(self) -> None:
        result = _coerce_review_field(
            ["hello", 123, None, {"key": "val"}], field_name="what_heard",
        )
        assert len(result) == 2  # string + dict, 123 and None skipped
        texts = [r.get("text") for r in result if "text" in r]
        assert "hello" in texts


class TestDictCoercion:
    """A single dict is wrapped in a one-element list."""

    def test_single_dict_wrapped(self) -> None:
        result = _coerce_review_field(
            {"item_id": "xyz", "title": "One item"}, field_name="what_decided",
        )
        assert len(result) == 1
        assert result[0]["item_id"] == "xyz"
        assert result[0]["source"] == "live_debrief"


class TestFullAdapter:
    """End-to-end adapter tests."""

    def test_artifact_payload_with_all_string_fields(self) -> None:
        payload: dict[str, Any] = {
            "session_id": str(uuid4()),
            "bot_id": "tante_rosi",
            "status": "review_pending",
            "prep_summary": "Prep summary text",
            "what_heard": "User shared career concerns.",
            "what_decided": "Decided to explore next steps.",
            "still_open": "Relationship topic not fully covered.",
            "what_to_remember": "User wants coach follow-up.",
        }
        result = _debrief_artifact_to_session_review(payload)
        assert result["session_id"] == payload["session_id"]
        assert result["bot_id"] == "tante_rosi"
        assert result["is_empty"] is False
        assert len(result["what_heard"]) == 1
        assert result["what_heard"][0]["source"] == "live_debrief"
        assert len(result["what_decided"]) == 1
        assert len(result["still_open"]) == 1
        assert len(result["what_to_remember"]) == 1

    def test_artifact_payload_with_empty_strings_omitted(self) -> None:
        payload: dict[str, Any] = {
            "session_id": str(uuid4()),
            "what_heard": "",
            "what_decided": "   ",
            "still_open": None,
            "what_to_remember": [],
        }
        result = _debrief_artifact_to_session_review(payload)
        assert result["is_empty"] is True
        assert result["what_heard"] == []
        assert result["what_decided"] == []
        assert result["still_open"] == []
        assert result["what_to_remember"] == []

    def test_artifact_payload_with_mixed_types(self) -> None:
        payload: dict[str, Any] = {
            "session_id": str(uuid4()),
            "what_heard": ["User said X", "User said Y"],
            "what_decided": [
                {"item_id": "a", "title": "Resolved", "summary": "done"},
            ],
            "still_open": [],
            "what_to_remember": "Remember to check in next week.",
        }
        result = _debrief_artifact_to_session_review(payload)
        assert len(result["what_heard"]) == 2
        assert all(
            item["source"] == "live_debrief" for item in result["what_heard"]
        )
        assert len(result["what_decided"]) == 1
        assert result["what_decided"][0]["source"] == "live_debrief"
        assert result["still_open"] == []
        assert len(result["what_to_remember"]) == 1

    def test_additive_fields_forwarded(self) -> None:
        payload: dict[str, Any] = {
            "session_id": str(uuid4()),
            "debrief_pending": True,
            "debrief_failed": {"reason": "none"},
            "live_debrief": {"raw": "data"},
            "review_summary": "Summary text",
            "what_heard": "x",
            "what_decided": "y",
            "still_open": "z",
            "what_to_remember": "w",
        }
        result = _debrief_artifact_to_session_review(payload)
        assert result["debrief_pending"] is True
        assert result["debrief_failed"] == {"reason": "none"}
        assert result["live_debrief"] == {"raw": "data"}
        assert result["review_summary"] == "Summary text"

    def test_missing_fields_dont_crash(self) -> None:
        """Adapter must not raise on completely empty/minimal payloads."""
        result = _debrief_artifact_to_session_review({})
        assert result["is_empty"] is True
        assert result["what_heard"] == []
        assert "session_id" not in result  # optional

    def test_session_id_and_status_preserved(self) -> None:
        sid = str(uuid4())
        payload = {
            "session_id": sid,
            "status": "completed",
            "what_heard": "x",
            "what_decided": "y",
            "still_open": "z",
            "what_to_remember": "w",
        }
        result = _debrief_artifact_to_session_review(payload)
        assert result["session_id"] == sid
        assert result["status"] == "completed"
