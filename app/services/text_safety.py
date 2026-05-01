"""User-facing text cleanup helpers."""

from __future__ import annotations

import re

_INTERNAL_OUTPUT_PATTERNS = (
    "stored memory",
    "memory yet",
    "not in the stored",
    "responding now",
    "phase a",
    "phase b",
    "read phase",
    "write phase",
    "write calls",
    "write tools",
    "phase errors",
    "phase gate",
    "tool call",
    "tool ",
    "tools needed",
    "new tools",
    "hot context",
    "enough context",
    "database row",
    "database",
    "do not need any more reads",
    "don't need any more reads",
    "no more reads",
    "let me read it properly",
    "trigger message",
    "trigger is",
    "current context",
    "watch item",
    "system is still flagging",
    "user-facing reply has already been delivered",
    "key updates to record",
    "need to be retried",
    "needs to be retried",
    "safety escalation",
    "should be addressed",
    "should be updated",
)

_PROCESS_OPENERS = (
    "the person's message",
    "partner a's message",
    "partner b's message",
    "the message is",
    "this message is",
    "the user is",
    "the user has",
    "he's naming",
    "she's naming",
)


def _looks_internal(line: str) -> bool:
    lowered = line.lower()
    return any(pattern in lowered for pattern in _INTERNAL_OUTPUT_PATTERNS) or any(
        lowered.startswith(pattern) for pattern in _PROCESS_OPENERS
    )


def looks_like_internal_process_text(text: str) -> bool:
    """Return true when text appears to be only private process narration."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(_looks_internal(line) or line in {"---", "***", "___"} for line in lines)


def clean_user_facing_text(text: str) -> str:
    """Strip model process leakage from text before it reaches a user or prompt history."""
    parts = re.split(r"(?m)^\s*(?:---|\*\*\*|___)\s*$", text, maxsplit=1)
    if len(parts) == 2 and any(_looks_internal(line.strip()) for line in parts[0].splitlines() if line.strip()):
        text = parts[1]

    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line in {"---", "***", "___"}:
            continue
        if _looks_internal(line):
            continue
        cleaned_lines.append(raw_line.rstrip())
    cleaned = "\n".join(cleaned_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned
