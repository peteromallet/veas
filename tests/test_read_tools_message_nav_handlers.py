from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services.turn_context import TurnContext, replace_ctx
from app.services.tools import read_tools
from app.services.tools.write_tools import ToolCallRejected
from tool_schemas import (
    MessagesAfterInput,
    MessagesBeforeInput,
    OpenThreadInput,
    ScrollInput,
    TopicRecentInput,
)


class NavPool:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.sql_calls: list[str] = []

    async def fetch(self, sql: str, *args):
        self.sql_calls.append(" ".join(sql.split()))
        assert "mediator.v_searchable_messages" in sql
        assert "FROM messages" not in sql
        return self._run(sql, *args)

    async def fetchrow(self, sql: str, *args):
        self.sql_calls.append(" ".join(sql.split()))
        assert "mediator.v_searchable_messages" in sql
        assert "FROM messages" not in sql
        rows = self._run(sql, *args)
        return rows[0] if rows else None

    def _run(self, sql: str, *args):
        compact = " ".join(sql.split())
        bot_id = args[0]
        viewer_id = args[1]
        participant_ids = set(args[2])
        idx = 3
        topic_id = None
        thread_owner_user_id = None
        if "m.topic_id =" in compact and idx < len(args) and isinstance(args[idx], UUID):
            topic_id = args[idx]
            idx += 1
        if "AND m.thread_owner_user_id = $" in compact and idx < len(args) and isinstance(
            args[idx], UUID
        ):
            thread_owner_user_id = args[idx]
            idx += 1
        start = end = None
        if "m.sent_at >=" in compact:
            start = args[idx]
            end = args[idx + 1]
            idx += 2
        anchor_sent_at = anchor_id = None
        if "(m.sent_at, m.message_id) <" in compact or "(m.sent_at, m.message_id) >" in compact or "(m.sent_at, m.message_id) <=" in compact:
            anchor_sent_at = args[idx]
            anchor_id = args[idx + 1]
            idx += 2
        elif "m.message_id =" in compact and idx < len(args) and isinstance(args[idx], UUID):
            idx += 1
        limit = args[idx] if idx < len(args) else len(self.rows)

        rows = []
        for row in self.rows:
            if row["bot_id"] != bot_id:
                continue
            if row["thread_owner_user_id"] not in participant_ids:
                continue
            if row["thread_owner_user_id"] != viewer_id and row["thread_owner_partner_share"] != "opt_in":
                continue
            if topic_id is not None and row["topic_id"] != topic_id:
                continue
            if thread_owner_user_id is not None and row["thread_owner_user_id"] != thread_owner_user_id:
                continue
            if start is not None and not (start <= row["sent_at"] < end):
                continue
            if "m.message_id =" in compact and anchor_id is None:
                message_id = args[idx - 1]
                if row["message_id"] != message_id:
                    continue
            rows.append(dict(row))

        if anchor_sent_at is not None and anchor_id is not None:
            op = "<=" if "<=" in compact else "<" if "<" in compact else ">"
            filtered = []
            anchor_key = (anchor_sent_at, anchor_id)
            for row in rows:
                row_key = (row["sent_at"], row["message_id"])
                if op == "<" and row_key < anchor_key:
                    filtered.append(row)
                elif op == ">" and row_key > anchor_key:
                    filtered.append(row)
                elif op == "<=" and row_key <= anchor_key:
                    filtered.append(row)
            rows = filtered

        reverse = "DESC" in compact.split("ORDER BY", 1)[1]
        rows.sort(key=lambda row: (row["sent_at"], row["message_id"]), reverse=reverse)
        return rows[:limit]


@pytest.fixture
def nav_ctx():
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    current_topic = uuid4()
    other_topic = uuid4()
    other_bot = "coach"
    current_bot = "mediator"
    partner_thread = partner.id
    user_thread = user.id

    def row(
        minute: int,
        *,
        topic_id: UUID,
        thread_owner_user_id: UUID,
        sender_id: UUID,
        recipient_id: UUID | None,
        message_id: UUID | None = None,
        bot_id: str = current_bot,
        partner_share: str = "opt_in",
        content: str | None = None,
    ) -> dict[str, object]:
        return {
            "message_id": message_id or uuid4(),
            "sender_id": sender_id,
            "recipient_id": recipient_id,
            "thread_owner_user_id": thread_owner_user_id,
            "thread_owner_partner_share": partner_share,
            "bot_id": bot_id,
            "topic_id": topic_id,
            "dyad_id": uuid4(),
            "direction": "inbound" if sender_id is not None else "outbound",
            "sent_at": datetime(2026, 6, 1, 10, minute, tzinfo=UTC),
            "content": content or f"message-{minute}",
            "media_analysis": None,
            "charge": "routine",
            "edited_at": None,
            "edit_history": None,
        }

    m1 = row(0, topic_id=current_topic, thread_owner_user_id=partner_thread, sender_id=partner.id, recipient_id=None, content="m1")
    m2 = row(1, topic_id=current_topic, thread_owner_user_id=partner_thread, sender_id=partner.id, recipient_id=None, content="m2")
    m3 = row(2, topic_id=current_topic, thread_owner_user_id=partner_thread, sender_id=partner.id, recipient_id=None, content="m3")
    m4 = row(3, topic_id=other_topic, thread_owner_user_id=partner_thread, sender_id=partner.id, recipient_id=None, content="m4")
    m5 = row(4, topic_id=current_topic, thread_owner_user_id=partner_thread, sender_id=partner.id, recipient_id=None, content="m5")
    m6 = row(5, topic_id=current_topic, thread_owner_user_id=user_thread, sender_id=user.id, recipient_id=None, content="m6")
    hidden = row(6, topic_id=current_topic, thread_owner_user_id=partner_thread, sender_id=partner.id, recipient_id=None, partner_share="opt_out", content="hidden")
    wrong_bot = row(7, topic_id=current_topic, thread_owner_user_id=partner_thread, sender_id=partner.id, recipient_id=None, bot_id=other_bot, content="wrong-bot")

    pool = NavPool([m1, m2, m3, m4, m5, m6, hidden, wrong_bot])
    ctx = TurnContext(
        uuid4(),
        pool,
        user,
        partner,
        [uuid4()],
        current_step="read",
        bot_id=current_bot,
        user_id=user.id,
        primary_topic_id=current_topic,
        dyad_id=uuid4(),
        bot_spec=SimpleNamespace(display_name="Veas"),
        extras={
            "hot_context_edge": {
                "message_id": str(m3["message_id"]),
                "sent_at": m3["sent_at"].isoformat(),
            }
        },
    )
    return ctx, {"m1": m1, "m2": m2, "m3": m3, "m4": m4, "m5": m5, "m6": m6}


@pytest.mark.asyncio
async def test_messages_before_after_and_missing_current_anchor(nav_ctx) -> None:
    ctx, rows = nav_ctx

    before = await read_tools.messages_before(ctx, MessagesBeforeInput(anchor="current", n=2))
    assert [hit.content for hit in before.messages] == ["m1", "m2"]
    decoded_before = read_tools._decode_nav_cursor(before.cursor)
    assert decoded_before["scope"] == "topic"
    assert decoded_before["anchor_id"] == str(rows["m1"]["message_id"])

    after = await read_tools.messages_after(
        ctx,
        MessagesAfterInput(anchor=rows["m2"]["message_id"], n=2),
    )
    assert [hit.content for hit in after.messages] == ["m3", "m5"]
    decoded_after = read_tools._decode_nav_cursor(after.cursor)
    assert decoded_after["anchor_id"] == str(rows["m5"]["message_id"])

    missing_ctx = replace_ctx(ctx, extras={})
    with pytest.raises(ToolCallRejected) as exc_info:
        await read_tools.messages_before(
            missing_ctx,
            MessagesBeforeInput(anchor="current", n=2),
        )
    assert exc_info.value.result["error_code"] == "missing_current_anchor"


@pytest.mark.asyncio
async def test_open_thread_and_scroll_chain_from_topic_recent(nav_ctx) -> None:
    ctx, rows = nav_ctx

    thread_window = await read_tools.open_thread(
        ctx,
        OpenThreadInput(around=rows["m5"]["message_id"], n=10),
    )
    assert [hit.content for hit in thread_window.messages] == ["m1", "m2", "m3", "m4", "m5"]
    decoded_thread = read_tools._decode_nav_cursor(thread_window.cursor)
    assert decoded_thread["scope"] == "thread"
    assert decoded_thread["thread_owner_user_id"] == str(ctx.partner.id)

    recent = await read_tools.topic_recent(ctx, TopicRecentInput(n=2))
    assert [hit.content for hit in recent.messages] == ["m6", "m5"]
    decoded_recent = read_tools._decode_nav_cursor(recent.cursor)
    assert decoded_recent["scope"] == "topic"
    assert decoded_recent["anchor_id"] == str(rows["m5"]["message_id"])

    older = await read_tools.scroll(
        ctx,
        ScrollInput(cursor=recent.cursor, direction="older", n=2),
    )
    assert [hit.content for hit in older.messages] == ["m2", "m3"]

    newer = await read_tools.scroll(
        ctx,
        ScrollInput(cursor=older.cursor, direction="newer", n=2),
    )
    assert [hit.content for hit in newer.messages] == ["m3", "m5"]
    assert all("mediator.v_searchable_messages" in sql for sql in ctx.pool.sql_calls)


@pytest.mark.asyncio
async def test_nav_boundaries_seeded_current_anchor_and_privacy_filters(nav_ctx) -> None:
    ctx, rows = nav_ctx

    seeded_ctx = replace_ctx(
        ctx,
        extras={
            "current_anchor": {
                "message_id": str(rows["m2"]["message_id"]),
                "sent_at": rows["m2"]["sent_at"].isoformat(),
            }
        },
    )
    seeded_before = await read_tools.messages_before(
        seeded_ctx,
        MessagesBeforeInput(anchor="current", n=5),
    )
    assert [hit.content for hit in seeded_before.messages] == ["m1"]

    before_start = await read_tools.messages_before(
        ctx,
        MessagesBeforeInput(anchor=rows["m1"]["message_id"], n=3),
    )
    assert before_start.messages == []
    assert before_start.cursor is None

    after_end = await read_tools.messages_after(
        ctx,
        MessagesAfterInput(anchor=rows["m6"]["message_id"], n=3),
    )
    assert after_end.messages == []
    assert after_end.cursor is None

    thread_window = await read_tools.open_thread(
        ctx,
        OpenThreadInput(around=rows["m5"]["message_id"], n=10),
    )
    assert [hit.content for hit in thread_window.messages] == ["m1", "m2", "m3", "m4", "m5"]
    assert "hidden" not in [hit.content for hit in thread_window.messages]
    assert "wrong-bot" not in [hit.content for hit in thread_window.messages]


@pytest.mark.asyncio
async def test_scroll_cursor_is_stable_across_mid_window_message_mutation(nav_ctx) -> None:
    ctx, rows = nav_ctx

    recent = await read_tools.topic_recent(ctx, TopicRecentInput(n=2))
    assert [hit.content for hit in recent.messages] == ["m6", "m5"]

    rows["m3"]["content"] = "m3 revised"
    rows["m3"]["edited_at"] = datetime(2026, 6, 1, 10, 9, tzinfo=UTC)
    rows["m3"]["edit_history"] = [{"content": "m3"}]

    older = await read_tools.scroll(
        ctx,
        ScrollInput(cursor=recent.cursor, direction="older", n=2),
    )
    assert [hit.message_id for hit in older.messages] == [
        rows["m2"]["message_id"],
        rows["m3"]["message_id"],
    ]
    assert older.messages[-1].content == "m3 revised"
    assert older.messages[-1].edit_history_original == "m3"

    newer = await read_tools.scroll(
        ctx,
        ScrollInput(cursor=older.cursor, direction="newer", n=2),
    )
    assert [hit.message_id for hit in newer.messages] == [
        rows["m3"]["message_id"],
        rows["m5"]["message_id"],
    ]
