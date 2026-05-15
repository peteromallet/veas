"""Sprint 2 DoD test: raw audio frames never survive request scope.

We can't easily run uvicorn in-process here, so the assertion is
structural: the live_voice router source does NOT emit any INSERT /
UPDATE that writes a `bytes` (BYTEA) column. Audio is only forwarded
to the transcriber and acked; nothing about it lands in the DB.
"""

from __future__ import annotations

from pathlib import Path

ROUTER = (
    Path(__file__).resolve().parent.parent / "app" / "routers" / "live_voice.py"
).read_text()


def test_no_bytea_audio_persistence_in_router() -> None:
    # The audio path: receive() -> transcriber.push(data_bytes) -> ack.
    # Anything writing `data_bytes` (or any binary chunk) to the DB
    # would have to mention BYTEA or pass a `bytes` value to an INSERT.
    # We assert the router never has either pattern.
    lower = ROUTER.lower()
    assert "bytea" not in lower, "router shouldn't reference BYTEA — no audio persistence"
    # The only INSERT/UPDATE strings allowed touch text columns.
    for forbidden in ("INSERT INTO mediator.audio_chunks", "INSERT INTO audio_"):
        assert forbidden not in ROUTER, f"router should not insert raw audio ({forbidden!r})"


def test_audio_bytes_only_flow_to_transcriber() -> None:
    # The bytes path is exactly: total_bytes counter + transcriber.push(data_bytes).
    # Make sure no other consumer is hiding in the binary branch.
    binary_branch_start = ROUTER.find("if data_bytes is not None")
    assert binary_branch_start >= 0
    # Look at the next 800 chars (the branch body).
    branch_body = ROUTER[binary_branch_start : binary_branch_start + 800]
    # Permitted: transcriber.push, counters, websocket.send_json(frame_ack).
    forbidden_calls = ("pool.execute", "INSERT", "write(", "open(", "f.write", "shelve")
    for token in forbidden_calls:
        assert token not in branch_body, (
            f"binary branch should NOT call {token!r}; raw audio must stay in memory"
        )
