from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tool_schemas import (
    MessageNavHit,
    MessageSpeaker,
    MessagesAfterInput,
    MessagesBeforeInput,
    OpenThreadInput,
    ScrollInput,
    SearchHit,
    SearchInput,
    SearchMessagesInput,
    SearchOutput,
    SearchMatchType,
    SourceMessagesInput,
    TOOL_REGISTRY,
    TopicRecentInput,
)


def test_message_nav_and_search_tools_are_registered() -> None:
    expected = {
        "messages_before": "MessagesBeforeInput",
        "messages_after": "MessagesAfterInput",
        "open_thread": "OpenThreadInput",
        "scroll": "ScrollInput",
        "topic_recent": "TopicRecentInput",
        "source_messages": "SourceMessagesInput",
        "search": "SearchInput",
    }

    for tool_name, input_model_name in expected.items():
        input_model, output_model = TOOL_REGISTRY[tool_name]
        assert input_model.__name__ == input_model_name
        assert output_model is not None


def test_message_nav_inputs_accept_expected_anchor_shapes() -> None:
    anchor_id = uuid4()

    assert MessagesBeforeInput(anchor="current").anchor == "current"
    assert MessagesAfterInput(anchor=anchor_id).anchor == anchor_id
    assert OpenThreadInput(around=anchor_id).around == anchor_id
    assert OpenThreadInput(around=date(2026, 6, 1)).around == date(2026, 6, 1)
    assert OpenThreadInput(around="latest").around == "latest"
    assert TopicRecentInput(topic_id=anchor_id).topic_id == anchor_id
    assert ScrollInput(cursor="opaque-nav-cursor", direction="older").direction == "older"


def test_search_input_and_output_cover_paging_and_match_metadata() -> None:
    search_input = SearchInput(query="repair attempt", mode="semantic", scope="thread")
    assert search_input.limit == 10
    assert "source_weight_map" not in SearchInput.model_fields

    message_id = uuid4()
    hit = SearchHit(
        message_id=message_id,
        cursor="nav-cursor",
        speaker=MessageSpeaker(label="You", user_id=uuid4(), direction="inbound"),
        sent_at=datetime.now(UTC),
        charge="charged",
        edited_at=datetime.now(UTC),
        edit_history_original="before edit",
        header="You, Tuesday 9:14pm:",
        snippet="snippet text",
        match_type=SearchMatchType.both,
        why_matched="matched exact words and embedding neighbors",
    )
    output = SearchOutput(hits=[hit], truncated=True, next_cursor="page-2")

    assert output.hits[0].match_type == SearchMatchType.both
    assert output.hits[0].message_id == message_id
    assert output.hits[0].source_type == "message"
    assert output.hits[0].source_id == message_id
    assert output.next_cursor == "page-2"


def test_search_hit_accepts_non_message_source_identity() -> None:
    source_id = uuid4()
    hit = SearchHit(
        message_id=None,
        source_type="memory",
        source_id=source_id,
        cursor="source-cursor",
        speaker=MessageSpeaker(label="Memory", user_id=None, direction="inbound"),
        sent_at=datetime.now(UTC),
        header="Memory:",
        snippet="remembered repair pattern",
        match_type=SearchMatchType.semantic,
    )

    assert hit.message_id is None
    assert hit.source_type == "memory"
    assert hit.source_id == source_id


def test_source_messages_input_accepts_unknown_source_type_for_structured_result() -> None:
    source_id = uuid4()
    args = SourceMessagesInput(source_type="unknown", source_id=source_id)

    assert args.source_type == "unknown"
    assert args.source_id == source_id


def test_message_nav_hit_exposes_render_and_edit_metadata() -> None:
    hit = MessageNavHit(
        message_id=uuid4(),
        cursor="opaque-nav-cursor",
        speaker=MessageSpeaker(label="Partner", user_id=uuid4(), direction="outbound"),
        sent_at=datetime.now(UTC),
        charge="routine",
        edited_at=datetime.now(UTC),
        edit_history_original="original text",
        header="Partner, Tuesday 9:14pm:",
        content="updated text",
    )

    assert hit.speaker.label == "Partner"
    assert hit.edit_history_original == "original text"
    assert hit.header.startswith("Partner")


def test_search_messages_input_remains_backward_compatible() -> None:
    legacy = SearchMessagesInput(text_contains="exact phrase", limit=25)
    assert legacy.text_contains == "exact phrase"
    assert legacy.limit == 25

    with pytest.raises(ValidationError, match="Use either local_day or date_range"):
        SearchMessagesInput(
            text_contains="conflict",
            local_day="today",
            date_range={
                "start": datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
                "end": datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            },
        )
