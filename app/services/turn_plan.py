"""Adaptive per-turn plan helpers for the agentic runner."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

TurnStep = Literal["read", "consult", "respond", "record", "schedule", "done"]
SkeletonName = Literal["quick_reply", "standard", "charged", "crisis", "silence_or_react"]

SKELETONS: dict[SkeletonName, list[TurnStep]] = {
    "quick_reply": ["respond", "done"],
    "standard": ["read", "respond", "record", "schedule", "done"],
    "charged": ["read", "respond", "record", "schedule", "done"],
    "crisis": ["respond", "record", "schedule", "done"],
    "silence_or_react": ["respond", "done"],
}

STEP_LABELS: dict[TurnStep, str] = {
    "read": "read context",
    "consult": "consult if useful",
    "respond": "reply or stay silent",
    "record": "record durable state",
    "schedule": "schedule follow-up if warranted",
    "done": "done",
}

STEP_ORDER: dict[TurnStep, int] = {
    "read": 0,
    "consult": 1,
    "respond": 2,
    "record": 3,
    "schedule": 4,
    "done": 5,
}

ACK_TEXTS = {
    "ok",
    "okay",
    "k",
    "yes",
    "yeah",
    "yep",
    "sure",
    "thanks",
    "thank you",
    "ty",
    "got it",
    "sounds good",
    "makes sense",
}

MEMORY_REQUEST_RE = re.compile(
    r"\b(remember|save|note|update|change|correct|forget|my (?:new|old)|i moved|i work|my job|my address)\b",
    re.IGNORECASE,
)

FITNESS_RECORD_RE = re.compile(
    r"\b("
    r"work\s*out|workout|training|train|lift|lifting|gym|exercise|session|"
    r"walk|dog walk|run|running|bench|laptop|morning|evening|weekday|weekdays|"
    r"routine|habit|plan|regroup|next week|schedule|slot|missed|done|completed|"
    r"before work|after work|computer|fitness"
    r")\b",
    re.IGNORECASE,
)

PREGNANCY_RECORD_RE = re.compile(
    r"\b("
    r"pregnan(?:t|cy)|schwanger|entbindungstermin|due date|edd|lmp|"
    r"last period|scan|ultrasound|ultraschall|appointment|termin|hebamme|"
    r"ärztin|arzt|birth|born|geboren|miscarriage|stillbirth|loss|lost|"
    r"termination|ended|ended the pregnancy|bleeding|blutung|contractions|"
    r"wehen|movement|kindsbewegung|nausea|übelkeit|scan moved|moved the due"
    r")\b",
    re.IGNORECASE,
)

FITNESS_CONFIRM_RE = re.compile(
    r"\b("
    r"let'?s\s+do\s+it|"
    r"let\s+us\s+do\s+it|"
    r"make\s+that\s+the\s+plan|"
    r"log\s+that\s+for\s+me|"
    r"let'?s\s+start\s+(next\s+)?(mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"let\s+us\s+start\s+(next\s+)?(mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"log\s+it|"
    r"record\s+that|"
    r"go ahead and schedule|"
    r"set\s+that\s+up|"
    r"add\s+that\s+to\s+(my\s+)?(plan|routine|schedule)"
    r")\b",
    re.IGNORECASE,
)

# B3: explicit scheduling-intent phrases that need a ``schedule`` step.
# ``quick_reply`` skeleton is ``["respond","done"]`` which has no schedule
# step; the tool registry rejects ``schedule_checkin`` during respond.
# Hector's prompt tells him to call it when users request check-ins, so
# these phrases must route to ``"standard"`` to unblock that tool.
CHECKIN_CONFIRM_RE = re.compile(
    r"\b("
    r"(?:please\s+)?(?:can\s+you\s+)?check\s+in\s+with\s+me"
    r"|remind\s+me"
    r"|schedule\s+(?:a\s+)?(?:check[\s-]?in|reminder|follow[\s-]?up)"
    r"|set\s+(?:a\s+)?reminder"
    r"|send\s+me\s+a\s+(?:reminder|nudge|ping)"
    r"|give\s+me\s+a\s+(?:nudge|ping)"
    r")\b",
    re.IGNORECASE,
)

AUDIT_REQUEST_RE = re.compile(
    r"\b(why did you|what did you|what have you|action log|tool call|tools? did you|did you tell|did you send)\b",
    re.IGNORECASE,
)


@dataclass
class TurnPlan:
    steps: list[TurnStep]
    skeleton_name: str = "custom"
    current_index: int = 0
    completed: list[TurnStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def current(self) -> TurnStep:
        if not self.steps:
            return "done"
        index = min(max(self.current_index, 0), len(self.steps) - 1)
        return self.steps[index]

    def mark_done(self, step: TurnStep | None = None) -> None:
        value = step or self.current
        if value not in self.completed:
            self.completed.append(value)

    def advance(self) -> TurnStep:
        self.mark_done(self.current)
        while self.current_index < len(self.steps) - 1:
            self.current_index += 1
            if self.current not in self.completed:
                break
        return self.current

    def add_steps(self, steps: list[TurnStep]) -> None:
        for step in steps:
            if step == "done" or step in self.steps:
                continue
            insert_at = next(
                (
                    index
                    for index, existing in enumerate(self.steps)
                    if index > self.current_index and STEP_ORDER[existing] > STEP_ORDER[step]
                ),
                self.steps.index("done") if "done" in self.steps else len(self.steps),
            )
            self.steps.insert(insert_at, step)

    def remove_steps(self, steps: list[TurnStep]) -> None:
        removable = set(steps) - {"done", self.current}
        current_step = self.current
        self.steps = [step for step in self.steps if step not in removable]
        if not self.steps or self.steps[-1] != "done":
            self.steps.append("done")
        if current_step in self.steps:
            self.current_index = self.steps.index(current_step)
        else:
            self.current_index = min(self.current_index, len(self.steps) - 1)

    def render_checklist(self) -> str:
        lines = []
        for index, step in enumerate(self.steps):
            if step in self.completed:
                marker = "x"
            elif index == self.current_index:
                marker = ">"
            else:
                marker = " "
            lines.append(f"- [{marker}] {step}: {STEP_LABELS[step]}")
        if self.notes:
            lines.append("Notes:")
            lines.extend(f"- {note}" for note in self.notes[-3:])
        return "\n".join(lines)

    def trace(self) -> str:
        return " -> ".join(self.steps)


def make_turn_plan(name: SkeletonName) -> TurnPlan:
    return TurnPlan(steps=list(SKELETONS[name]), skeleton_name=name)


def _trigger_text(trigger_metadata: dict[str, Any] | None) -> str:
    messages = (trigger_metadata or {}).get("messages") or []
    return "\n".join(str(message.get("content") or "") for message in messages if isinstance(message, dict))


def _is_short_ack(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s']", "", text.lower()).strip()
    return cleaned in ACK_TEXTS or (len(cleaned.split()) <= 2 and cleaned in ACK_TEXTS)


def pick_default_skeleton(
    *,
    trigger_metadata: dict[str, Any] | None,
    charge: str | None,
    hot_context_signals: dict[str, Any] | None = None,
) -> SkeletonName:
    if charge == "crisis":
        return "crisis"
    if charge == "charged":
        return "charged"

    if (trigger_metadata or {}).get("kind") == "scheduled_task":
        return "standard"

    text = _trigger_text(trigger_metadata)
    pacing = (trigger_metadata or {}).get("pacing") or {}

    # --- plan-confirmation routing (before short-ack to prevent
    #     accepted-plan confirmations from being downgraded) ---------
    if (
        (hot_context_signals or {}).get("bot_id") == "hector"
        and FITNESS_CONFIRM_RE.search(text)
    ):
        return "standard"

    # B3: scheduling-intent inbounds need a ``schedule`` step.
    # ``quick_reply`` has none; the tool registry rejects
    # ``schedule_checkin`` during ``respond``.  Route to ``standard``
    # so Hector can fulfil the prompt's explicit scheduling mandate.
    if (
        (hot_context_signals or {}).get("bot_id") == "hector"
        and CHECKIN_CONFIRM_RE.search(text)
    ):
        return "standard"

    if pacing.get("action") in {"react", "silence"} or _is_short_ack(text):
        return "silence_or_react"
    if AUDIT_REQUEST_RE.search(text):
        return "standard"
    if MEMORY_REQUEST_RE.search(text):
        return "standard"
    if (hot_context_signals or {}).get("explicit_memory_update"):
        return "standard"
    if (
        (hot_context_signals or {}).get("bot_id") == "hector"
        and FITNESS_RECORD_RE.search(text)
    ):
        return "standard"
    if (
        (hot_context_signals or {}).get("bot_id") == "tante_rosi"
        and PREGNANCY_RECORD_RE.search(text)
    ):
        return "standard"
    return "quick_reply"


def orient_summary(
    *,
    trigger_metadata: dict[str, Any] | None,
    charge: str | None,
    hot_context_signals: dict[str, Any] | None = None,
) -> str:
    metadata = trigger_metadata or {}
    compact = {
        "kind": metadata.get("kind", "inbound"),
        "charge": charge or "routine",
        "context": metadata.get("context") or {},
    }
    if metadata.get("pacing") is not None:
        compact["pacing"] = metadata["pacing"]
    if hot_context_signals:
        compact["signals"] = hot_context_signals
    return "Orient: " + json.dumps(compact, default=str, sort_keys=True)
