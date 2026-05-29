"""Sprint 1 — agenda schema + prep persistence tests (stub LLM, sync dev path only).

These tests cover the legacy synchronous prep path via ``produce_agenda``
with ``StubAgendaProducer``.  They do **not** exercise the agentic async
prep path (Sprint 2) — for agentic coverage, see
``tests/test_live_prep_agentic.py``.

Requires ``LIVE_VOICE_PREP_PROVIDER=stub`` to ensure no accidental routing
to the agentic path in environments where the env var may be set.

Two layers:
1. Pure-Python schema tests for ``Agenda`` / ``AgendaItem`` — internal-ref
   resolution, uniqueness, 'must' anchor, enum boundaries.
2. Persistence tests using a FakePool that records SQL statements so we can
   assert the transaction shape without a live DB.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.bots.base import BotSpec
from app.bots.registry import BOT_SPECS
from app.config import get_settings
from app.services.live.prep import (
    DeepseekAgendaProducer,
    StubAgendaProducer,
    produce_agenda,
    select_agenda_producer,
)
from app.services.live.schemas import Agenda, AgendaItem, PrepRequest


# ── Module-level fixture: ensure these tests always run in stub mode ────────
# These tests cover the legacy synchronous prep path (Sprint 1).  The agentic
# async path (Sprint 2) is exercised by tests/test_live_prep_agentic.py.


@pytest.fixture(autouse=True)
def _force_stub_prep_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure LIVE_VOICE_PREP_PROVIDER=stub so these sync-path tests never
    accidentally route to the agentic async path."""
    monkeypatch.setenv("LIVE_VOICE_PREP_PROVIDER", "stub")


# --------------------------------------------------------------------------- #
# 1. Pure-Python schema tests.
# --------------------------------------------------------------------------- #


def _valid_items() -> list[AgendaItem]:
    return [
        AgendaItem(
            id="anchor",
            title="Anchor",
            priority="must",
            next_item_ids=["follow"],
        ),
        AgendaItem(id="follow", title="Follow-up", priority="should"),
    ]


class TestAgendaSchema:
    def test_accepts_well_formed_agenda(self) -> None:
        agenda = Agenda(prep_summary="ok", items=_valid_items(), first_item_id="anchor")
        assert agenda.first_item_id == "anchor"

    def test_rejects_dangling_next_item_id(self) -> None:
        items = _valid_items()
        items[0] = items[0].model_copy(update={"next_item_ids": ["does_not_exist"]})
        with pytest.raises(ValidationError, match="unknown id"):
            Agenda(prep_summary="x", items=items, first_item_id="anchor")

    def test_rejects_unknown_first_item_id(self) -> None:
        with pytest.raises(ValidationError, match="does not resolve"):
            Agenda(prep_summary="x", items=_valid_items(), first_item_id="ghost")

    def test_rejects_duplicate_item_ids(self) -> None:
        items = [
            AgendaItem(id="dup", title="A", priority="must"),
            AgendaItem(id="dup", title="B", priority="should"),
        ]
        with pytest.raises(ValidationError, match="unique"):
            Agenda(prep_summary="x", items=items, first_item_id="dup")

    def test_rejects_agenda_without_must(self) -> None:
        items = [
            AgendaItem(id="a", title="A", priority="should"),
            AgendaItem(id="b", title="B", priority="optional"),
        ]
        with pytest.raises(ValidationError, match="'must'"):
            Agenda(prep_summary="x", items=items, first_item_id="a")

    def test_rejects_bad_enum_values(self) -> None:
        with pytest.raises(ValidationError):
            AgendaItem(id="x", title="t", priority="critical")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            AgendaItem(id="x", title="t", kind="freeform")  # type: ignore[arg-type]


class TestAgendaProducerSelection:
    def test_agentic_provider_selects_async_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIVE_VOICE_PREP_PROVIDER", "agentic")

        assert select_agenda_producer() is None


class TestDeepseekAgendaProducer:
    @pytest.mark.anyio
    async def test_rejects_empty_content_with_clear_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-real-looking")
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.example")
        get_settings.cache_clear()

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {
                    "choices": [
                        {
                            "message": {"content": ""},
                            "finish_reason": "length",
                        }
                    ]
                }

        class _Client:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def post(self, *args: Any, **kwargs: Any) -> _Resp:
                return _Resp()

        monkeypatch.setattr("httpx.AsyncClient", _Client)

        producer = DeepseekAgendaProducer(model="deepseek-test")
        with pytest.raises(
            RuntimeError,
            match="Deepseek agenda producer returned empty content",
        ):
            await producer(
                PrepRequest(
                    user_id=str(uuid4()),
                    bot_id="hector",
                    steering_text="test",
                    topic_slug="habits",
                ),
                context={},
            )


# --------------------------------------------------------------------------- #
# 2. Persistence: FakePool that records SQL + values.
# --------------------------------------------------------------------------- #


class _FakeConn:
    def __init__(self, parent: "_FakePool") -> None:
        self._parent = parent

    def transaction(self) -> "_FakeTxn":
        return _FakeTxn()

    async def execute(self, sql: str, *args: Any) -> str:
        self._parent.executed.append((sql.strip(), args))
        return "OK"


class _FakeTxn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeAcquire:
    def __init__(self, parent: "_FakePool") -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._parent)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    """Minimal asyncpg pool stand-in.

    Only the methods used by ``produce_agenda`` and ``gather_prep_context``
    are implemented.  ``fetchrow`` / ``fetch`` return canned values that the
    individual tests set; missing keys behave like an empty result.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._fetchrow: Any = None
        self._fetch_by_first_arg: dict[str, list[dict[str, Any]]] = {}

    # Public test helpers.
    def set_user_row(self, row: dict[str, Any] | None) -> None:
        self._fetchrow = row

    def set_fetch(self, sql_marker: str, rows: list[dict[str, Any]]) -> None:
        self._fetch_by_first_arg[sql_marker] = rows

    # asyncpg-shaped surface.
    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return self._fetchrow

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        for marker, rows in self._fetch_by_first_arg.items():
            if marker in sql:
                return rows
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql.strip(), args))
        return "OK"


@pytest.mark.anyio
async def test_produce_agenda_persists_session_items_and_current_item() -> None:
    pool = _FakePool()
    user_id = uuid4()
    request = PrepRequest(
        user_id=str(user_id),
        bot_id="tante_rosi",
        steering_text="Talking through the partner conversation tonight",
    )

    result = await produce_agenda(pool, request, producer=StubAgendaProducer())

    assert result.items_persisted == 3
    assert result.agenda.first_item_id == "must_anchor"

    # The INSERT into conversations must come first, then 3 conversation_items
    # INSERTs, then the UPDATE setting current_item_id.
    sql_compact = [(" ".join(s.split()), args) for s, args in pool.executed]
    insert_conv = [s for s, _ in sql_compact if s.startswith("INSERT INTO mediator.conversations")]
    insert_items = [s for s, _ in sql_compact if s.startswith("INSERT INTO mediator.conversation_items")]
    update_current = [s for s, _ in sql_compact if s.startswith("UPDATE mediator.conversations SET current_item_id")]
    assert len(insert_conv) == 1, sql_compact
    assert len(insert_items) == 3, sql_compact
    assert len(update_current) == 1, sql_compact

    # The UPDATE's first arg matches result.current_item_id.
    update_args = next(args for s, args in pool.executed if "current_item_id = $1" in " ".join(s.split()))
    assert str(update_args[0]) == result.current_item_id


@pytest.mark.anyio
async def test_produce_agenda_propagates_steering_mode() -> None:
    """steering_text present -> mode='steered'; absent -> mode='open'."""
    for steering, expected_mode in [
        ("guide me through the prep doc", "steered"),
        ("", "open"),
        (None, "open"),
    ]:
        pool = _FakePool()
        request = PrepRequest(user_id=str(uuid4()), bot_id="tante_rosi", steering_text=steering)
        await produce_agenda(pool, request, producer=StubAgendaProducer())

        conv_sql, conv_args = next(
            (s, args) for s, args in pool.executed if "INSERT INTO mediator.conversations" in s
        )
        # mode is the 4th positional parameter ($4).
        assert conv_args[3] == expected_mode, (steering, conv_args)


@pytest.mark.anyio
async def test_stub_producer_returns_validated_agenda() -> None:
    producer = StubAgendaProducer()
    agenda = await producer(
        PrepRequest(user_id=str(uuid4()), bot_id="tante_rosi", steering_text="x"),
        context={
            "bot_profile": {
                "bot_id": "tante_rosi",
                "display_name": "Tante Rosi",
                "primary_topic_slug": "pregnancy",
                "participants_shape": "solo",
            },
            "themes": [
                {"id": str(uuid4()), "slug": "pregnancy_timing", "label": "Timing"}
            ],
        },
    )
    # Pydantic round-trip — would raise on schema break.
    Agenda.model_validate(agenda.model_dump())
    assert agenda.items[0].theme_slug == "pregnancy_timing"
    assert "Tante Rosi" in agenda.prep_summary
    assert "pregnancy" in agenda.prep_summary
    assert "pregnancy" in (agenda.items[0].ask or "").lower()
    assert "partner" not in (agenda.items[0].ask or "").lower()


async def test_stub_producer_clips_long_steering_for_fallback() -> None:
    producer = StubAgendaProducer()
    agenda = await producer(
        PrepRequest(
            user_id=str(uuid4()),
            bot_id="mediator",
            steering_text="x" * 5000,
            topic_slug="relationship",
        ),
        context={},
    )

    assert len(agenda.prep_summary) <= 2000
    Agenda.model_validate(agenda.model_dump())


@pytest.mark.anyio
async def test_produce_agenda_passes_selected_bot_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: live prep must not silently use mediator-shaped prompts."""

    def prompt_renderer(
        assistant_name: str,
        user_name: str,
        partner_name: str | None = None,
        **_: Any,
    ) -> str:
        del partner_name
        return (
            f"{assistant_name} fitness prompt for {user_name}; "
            "ask about training load."
        )

    monkeypatch.setitem(
        BOT_SPECS,
        "hector_live_test",
        BotSpec(
            bot_id="hector_live_test",
            prompt_renderer=prompt_renderer,
            step_instructions={
                "read": "read",
                "consult": "consult",
                "respond": "respond",
                "record": "record",
                "schedule": "schedule",
                "done": "done",
            },
            display_name="Hector",
            primary_topic_slug="fitness",
            participants_shape="solo",
        ),
    )

    class CapturingProducer:
        def __init__(self) -> None:
            self.context: dict[str, Any] | None = None

        async def __call__(self, request: PrepRequest, context: dict[str, Any]) -> Agenda:
            del request
            self.context = context
            return Agenda(
                prep_summary="fitness prep",
                items=[AgendaItem(id="anchor", title="Anchor", priority="must")],
                first_item_id="anchor",
            )

    user_id = uuid4()
    pool = _FakePool()
    pool.set_user_row(
        {
            "id": user_id,
            "name": "Maya",
            "phone": "+15555550100",
            "timezone": "Europe/Berlin",
            "style_notes": None,
            "onboarding_state": "ready",
            "pacing_preferences": {},
        }
    )
    producer = CapturingProducer()

    await produce_agenda(
        pool,
        PrepRequest(user_id=str(user_id), bot_id="hector_live_test", steering_text=""),
        producer=producer,
    )

    profile = (producer.context or {})["bot_profile"]
    assert profile["bot_id"] == "hector_live_test"
    assert profile["display_name"] == "Hector"
    assert profile["primary_topic_slug"] == "fitness"
    assert profile["participants_shape"] == "solo"
    assert "fitness prompt for Maya" in profile["system_prompt"]
