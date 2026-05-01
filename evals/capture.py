from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterator


@dataclass(frozen=True)
class ToolCallRecord:
    tool_name: str
    args: dict[str, Any]
    result: dict[str, Any]
    phase: str
    duration_ms: int
    called_at: datetime


@dataclass
class ToolTranscript:
    calls: list[ToolCallRecord] = field(default_factory=list)

    def record(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
        phase: str,
        duration_ms: int,
        called_at: datetime,
    ) -> None:
        self.calls.append(
            ToolCallRecord(
                tool_name=tool_name,
                args=args,
                result=result,
                phase=phase,
                duration_ms=duration_ms,
                called_at=called_at,
            )
        )

    def as_json(self) -> list[dict[str, Any]]:
        return [
            {
                "tool_name": call.tool_name,
                "args": call.args,
                "result": call.result,
                "phase": call.phase,
                "duration_ms": call.duration_ms,
                "called_at": call.called_at.isoformat(),
            }
            for call in self.calls
        ]


_current_transcript: ContextVar[ToolTranscript | None] = ContextVar("eval_tool_transcript", default=None)


@contextmanager
def capture_tool_calls(transcript: ToolTranscript | None = None) -> Iterator[ToolTranscript]:
    active = transcript or ToolTranscript()
    token = _current_transcript.set(active)
    try:
        yield active
    finally:
        _current_transcript.reset(token)


def current_transcript() -> ToolTranscript | None:
    return _current_transcript.get()


def record_tool_call(
    *,
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
    phase: str,
    started_at: datetime,
) -> None:
    transcript = current_transcript()
    if transcript is None:
        return
    duration_ms = max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000))
    transcript.record(
        tool_name=tool_name,
        args=args,
        result=result,
        phase=phase,
        duration_ms=duration_ms,
        called_at=started_at,
    )
