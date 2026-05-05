"""Shared cross-thread privacy decisions.

This module deliberately handles raw message visibility by explicit thread owner
only. Memories and observations need a future provenance field before they can
use the same raw cross-thread filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Mapping
from uuid import UUID

SharingDefault = Literal["unset", "opt_in", "opt_out"]
RawMessageVisibilityReason = Literal[
    "current_user_thread",
    "source_user_opted_in",
    "source_user_not_opted_in",
]

BRIDGE_TARGET_VISIBLE_STATUSES = frozenset({"ready", "sent", "addressed"})
RAW_PARTNER_CONTENT_REDACTION = "[raw partner content withheld by sharing_default]"
RAW_PARTNER_CONTENT_OMISSION_REASON = "raw_partner_content_hidden_by_sharing_default"


@dataclass(frozen=True)
class RawMessageVisibility:
    visible: bool
    sharing_default: SharingDefault
    reason: RawMessageVisibilityReason
    redaction: str | None = None
    omission_reason: str | None = None


def normalize_sharing_default(value: Any) -> SharingDefault:
    """Normalize storage/API values for display and privacy checks."""
    if isinstance(value, Enum):
        value = value.value
    if value in (None, "", "unset"):
        return "unset"
    if value == "opt_in":
        return "opt_in"
    if value == "opt_out":
        return "opt_out"
    return "unset"


def raw_message_visibility(
    *,
    viewer_user_id: UUID,
    thread_owner_user_id: UUID,
    thread_owner_sharing_default: Any,
) -> RawMessageVisibility:
    """Return whether a viewer can see raw message content from a thread owner."""
    sharing_default = normalize_sharing_default(thread_owner_sharing_default)
    if viewer_user_id == thread_owner_user_id:
        return RawMessageVisibility(
            visible=True,
            sharing_default=sharing_default,
            reason="current_user_thread",
        )
    if sharing_default == "opt_in":
        return RawMessageVisibility(
            visible=True,
            sharing_default=sharing_default,
            reason="source_user_opted_in",
        )
    return RawMessageVisibility(
        visible=False,
        sharing_default=sharing_default,
        reason="source_user_not_opted_in",
        redaction=RAW_PARTNER_CONTENT_REDACTION,
        omission_reason=RAW_PARTNER_CONTENT_OMISSION_REASON,
    )


def can_view_raw_message(
    *,
    viewer_user_id: UUID,
    thread_owner_user_id: UUID,
    thread_owner_sharing_default: Any,
) -> bool:
    return raw_message_visibility(
        viewer_user_id=viewer_user_id,
        thread_owner_user_id=thread_owner_user_id,
        thread_owner_sharing_default=thread_owner_sharing_default,
    ).visible


def redact_raw_message_content(
    content: Any,
    *,
    viewer_user_id: UUID,
    thread_owner_user_id: UUID,
    thread_owner_sharing_default: Any,
) -> str:
    visibility = raw_message_visibility(
        viewer_user_id=viewer_user_id,
        thread_owner_user_id=thread_owner_user_id,
        thread_owner_sharing_default=thread_owner_sharing_default,
    )
    if visibility.visible:
        return "" if content is None else str(content)
    return visibility.redaction or RAW_PARTNER_CONTENT_REDACTION


def should_omit_raw_message(
    *,
    viewer_user_id: UUID,
    thread_owner_user_id: UUID,
    thread_owner_sharing_default: Any,
) -> bool:
    return not can_view_raw_message(
        viewer_user_id=viewer_user_id,
        thread_owner_user_id=thread_owner_user_id,
        thread_owner_sharing_default=thread_owner_sharing_default,
    )


def is_bridge_status_target_visible(status: Any) -> bool:
    if isinstance(status, Enum):
        status = status.value
    return str(status) in BRIDGE_TARGET_VISIBLE_STATUSES


def bridge_candidate_visible_to_target(
    candidate: Mapping[str, Any],
    *,
    target_user_id: UUID | None = None,
) -> bool:
    status = candidate.get("status")
    if isinstance(status, Enum):
        status = status.value
    status = str(status)
    if not is_bridge_status_target_visible(status):
        return False
    if status == "ready":
        partner_path = candidate.get("partner_path", "message_partner")
        if isinstance(partner_path, Enum):
            partner_path = partner_path.value
        # Gate ready rows by path so source-only bookkeeping rows such as
        # hold_for_context or coach_in_person cannot leak through target lists.
        if partner_path != "message_partner":
            return False
    if target_user_id is None:
        return True
    return candidate.get("target_user_id") == target_user_id
