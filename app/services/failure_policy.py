"""Expanded failure-class taxonomy + retry policy table (Project C, C3).

Background
----------
Project A (SD-002) collapsed the failure surface to three durable classes:
``retryable_pre_send``, ``terminal_post_send``, ``infra_bug``.  These are
the only values currently allowed by the CHECK constraint on
``mediator.messages.failure_class`` and remain the source of truth for the
recovery sweep's retry decisions.

Per SD-009 (user override of SD-003), Project C ships an **expanded**
taxonomy alongside the legacy three.  The new classes are additive — they
do NOT replace the legacy strings, are not enforced by the database CHECK
constraint, and are not yet read by the recovery sweep.  They exist so
that:

1. The ledger (``inbound_handling_attempts``, Project C, C2) can record a
   finer-grained class on a per-attempt basis.
2. A future retry-policy refactor can switch from the messages CHECK to
   a ledger-driven policy without another taxonomy change.

Compatibility
-------------
* The legacy three strings (``retryable_pre_send`` / ``terminal_post_send``
  / ``infra_bug``) remain valid.
* ``classify(reason)`` maps every key in
  :data:`app.services.inbound_queue.FAILURE_REASON_TO_CLASS` to one of the
  seven enum values.  Unknown reasons fall back to ``INFRA_BUG`` (preserving
  the existing inbound_queue.py guard semantics).
* :data:`FAILURE_POLICY` is the decision table: each class has a
  :class:`RetryPolicy` (retryable?  default delay?  max attempts?).

This module is pure logic and has no DB or pool dependencies, which keeps
unit tests fast and lets it be imported from the inbound queue, recovery,
and the why_no_reply diagnostic without circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class FailureClass(str, Enum):
    """The seven-class taxonomy.

    Members inherit from ``str`` so equality works against the legacy
    column values without coercion (``FailureClass.INFRA_BUG == "infra_bug"``).
    """

    RETRYABLE_PRE_SEND = "retryable_pre_send"
    TERMINAL_POST_SEND = "terminal_post_send"
    INFRA_BUG = "infra_bug"
    MODEL_PROVIDER_BAD_REQUEST = "model_provider_bad_request"
    MODEL_PROVIDER_TIMEOUT = "model_provider_timeout"
    TOOL_VALIDATION_RECOVERABLE = "tool_validation_recoverable"
    DELIVERY_PROVIDER_FAILURE = "delivery_provider_failure"


@dataclass(frozen=True)
class RetryPolicy:
    """Per-class retry policy.

    ``retryable`` is the headline boolean: True means automatic recovery
    should pick the row up again subject to ``max_attempts`` and the
    backoff schedule.  ``default_retry_delay_seconds`` is the *first*
    retry delay; the existing backoff in ``inbound_queue.fail_messages``
    handles exponential growth from there.  ``max_attempts=None`` means
    "no policy-level cap" (the existing ``inbound_queue_max_retry_attempts``
    setting still applies as a global ceiling).
    """

    retryable: bool
    default_retry_delay_seconds: int
    max_attempts: int | None


FAILURE_POLICY: Final[dict[FailureClass, RetryPolicy]] = {
    # Legacy three — values mirror current behaviour in inbound_queue.
    FailureClass.RETRYABLE_PRE_SEND: RetryPolicy(
        retryable=True,
        default_retry_delay_seconds=30,
        max_attempts=3,
    ),
    FailureClass.TERMINAL_POST_SEND: RetryPolicy(
        retryable=False,
        default_retry_delay_seconds=0,
        max_attempts=None,
    ),
    FailureClass.INFRA_BUG: RetryPolicy(
        retryable=False,
        default_retry_delay_seconds=0,
        max_attempts=None,
    ),
    # ── C3 additions ───────────────────────────────────────────────────
    # Provider returned a 4xx other than rate-limit/overload (malformed
    # payload, schema mismatch, etc.).  Retryable after a short delay to
    # let a follow-up code change ride out; max bounded.
    FailureClass.MODEL_PROVIDER_BAD_REQUEST: RetryPolicy(
        retryable=True,
        default_retry_delay_seconds=60,
        max_attempts=2,
    ),
    # Provider call exceeded the per-call timeout.  Retryable with the
    # same base delay as RETRYABLE_PRE_SEND; max bumped slightly since
    # transient network blips are common.
    FailureClass.MODEL_PROVIDER_TIMEOUT: RetryPolicy(
        retryable=True,
        default_retry_delay_seconds=30,
        max_attempts=4,
    ),
    # Tool argument validation failed in a way the model can correct on
    # the next iteration (the existing
    # ``tool_validation_recoverable_exhausted`` reason maps here).  Short
    # delay, bounded attempts.
    FailureClass.TOOL_VALIDATION_RECOVERABLE: RetryPolicy(
        retryable=True,
        default_retry_delay_seconds=15,
        max_attempts=3,
    ),
    # Outbound delivery (Discord / WhatsApp) failed AFTER the LLM
    # produced a reply.  Retryable because the reply text is in hand;
    # the next attempt only needs to re-send.  Generous max.
    FailureClass.DELIVERY_PROVIDER_FAILURE: RetryPolicy(
        retryable=True,
        default_retry_delay_seconds=45,
        max_attempts=5,
    ),
}


# ── reason → class mapping ──────────────────────────────────────────────────


# The "live" reasons that exist in app.services.inbound_queue.FAILURE_REASON_TO_CLASS
# are mapped here to specific C3 classes where the new taxonomy adds information,
# and to the legacy class otherwise.  Unknown reasons fall back to INFRA_BUG.
_REASON_TO_CLASS: Final[dict[str, FailureClass]] = {
    # ── live reasons (legacy three preserved) ─────────────────────────
    "provider_send_failed": FailureClass.RETRYABLE_PRE_SEND,
    "llm_timeout": FailureClass.MODEL_PROVIDER_TIMEOUT,
    # Catch-all when the provider chain exhausts without a more specific
    # reason on the exception instance. Distinct from `llm_timeout` (which
    # is reserved for actual clock timeouts on the provider call) — same
    # downstream retry semantics as other transient pre-send failures.
    "llm_phase_failed": FailureClass.RETRYABLE_PRE_SEND,
    "tool_validation_recoverable_exhausted": FailureClass.TOOL_VALIDATION_RECOVERABLE,
    "crashed": FailureClass.RETRYABLE_PRE_SEND,
    "transcription_failed": FailureClass.RETRYABLE_PRE_SEND,
    "vision_failed": FailureClass.RETRYABLE_PRE_SEND,
    # ── A2 provider-chain reasons ─────────────────────────────────────
    "provider_fallback_killed": FailureClass.RETRYABLE_PRE_SEND,
    "same_provider_fallback_noop": FailureClass.RETRYABLE_PRE_SEND,
    "fallback_breaker_open": FailureClass.RETRYABLE_PRE_SEND,
    "respond_cap_no_output": FailureClass.RETRYABLE_PRE_SEND,
    # Genuine configuration bug; matches the legacy INFRA_BUG mapping.
    "unsupported_chain_anthropic_to_deepseek": FailureClass.INFRA_BUG,
    # ── forward-compat (dead today; mirrors the legacy table) ────────
    "spend_cap": FailureClass.RETRYABLE_PRE_SEND,
    "newer_inbound_before_final_send": FailureClass.RETRYABLE_PRE_SEND,
    "crashed_after_send": FailureClass.TERMINAL_POST_SEND,
}


def classify(reason: str) -> FailureClass:
    """Map a failure reason string to a :class:`FailureClass`.

    Unknown reasons return :attr:`FailureClass.INFRA_BUG` — same fallback
    that ``inbound_queue.FAILURE_REASON_TO_CLASS`` uses today via
    ``dict.get(reason, "infra_bug")``.  This keeps the C3 taxonomy a
    strict superset of the live behaviour.
    """
    if reason is None:
        return FailureClass.INFRA_BUG
    return _REASON_TO_CLASS.get(str(reason), FailureClass.INFRA_BUG)


def is_retryable(failure_class: FailureClass | str) -> bool:
    """Convenience accessor: pulled out for call sites that don't want to
    look up the policy themselves."""
    if isinstance(failure_class, str):
        try:
            failure_class = FailureClass(failure_class)
        except ValueError:
            return False
    policy = FAILURE_POLICY.get(failure_class)
    return policy.retryable if policy else False


__all__ = [
    "FailureClass",
    "RetryPolicy",
    "FAILURE_POLICY",
    "classify",
    "is_retryable",
]
