"""Non-chat agentic job runner for live prep, live debrief, and other async agentic workflows.

This module provides a reusable runner that opens a non-chat bot_turn, executes
a single run_step with the bot's configured provider chain, and gates on a
required submit tool.  It is intentionally separate from the chat-oriented
_run_agentic and does NOT touch inbound queue lifecycle, outbound sends, or
chat-specific claiming/finalizing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.services.agentic import (
    BoundedLoopExceeded,
    MaxToolCallsExceeded,
    _finalize_turn_atomically,
    _open_nonchat_turn,
    _provider_model,
    run_step,
)
from app.services.crypto import encrypt_value
from app.services.scope import InboundScope
from app.services.tools.registry import _step_allowed
from app.services.turn_audit import record_turn_event
from app.services.turn_context import TurnContext, obs_fields

logger = logging.getLogger(__name__)


# ── Default failure reason prefixes ──────────────────────────────────────
_LIVE_PREP_FAILURE_PREFIX = "live_prep"
_LIVE_DEBRIEF_FAILURE_PREFIX = "live_debrief"


@dataclass
class NonchatJobConfig:
    """Configuration for a single run_agentic_nonchat_job invocation.

    All fields have defaults that preserve the existing live-prep behaviour
    exactly — callers that do not pass a config get the same result as
    before.
    """

    # The TurnStep name.  Must match a key in TurnStep literal.
    current_step: str = "live_prep"

    # The extras key where the submit tool handler stores its payload.
    submit_extras_key: str = "submitted_live_brief"

    # The name of the required finalisation-gate tool.
    submit_tool_name: str = "submit_live_brief"

    # Flat tool-allow set (None → use step-based policy via _step_allowed).
    # When set, it becomes TurnContext.flat_allowed_tools.
    allowed_tools: set[str] | None = None

    # Prefix for failure_reason strings (e.g. "live_prep", "live_debrief").
    failure_reason_prefix: str = _LIVE_PREP_FAILURE_PREFIX

    # Cap on tool *iterations* (assistant rounds that request tool calls).
    max_tool_iterations: int | None = None

    # Cap on total tool *calls* (non-update_turn_plan dispatches).
    max_tool_calls: int | None = None

    # Extra server-side metadata to seed onto TurnContext before any tool call.
    initial_extras: dict[str, Any] = field(default_factory=dict)


# ── Pre-built configs ────────────────────────────────────────────────────
LIVE_PREP_CONFIG = NonchatJobConfig(
    current_step="live_prep",
    submit_extras_key="submitted_live_brief",
    submit_tool_name="submit_live_brief",
    failure_reason_prefix=_LIVE_PREP_FAILURE_PREFIX,
)

LIVE_DEBRIEF_CONFIG = NonchatJobConfig(
    current_step="live_debrief",
    submit_extras_key="submitted_live_debrief",
    submit_tool_name="submit_live_debrief",
    failure_reason_prefix=_LIVE_DEBRIEF_FAILURE_PREFIX,
)


@dataclass
class NonchatJobResult:
    """Outcome of a single run_agentic_nonchat_job invocation.

    ``brief`` is the canonical field for backward compatibility — all
    existing live-prep callers construct with ``brief=...`` and access
    ``result.brief``.  ``submitted_payload`` is a property alias that
    reads/writes the same field, so new debrief callers can use the
    more descriptive name.
    """

    success: bool
    brief: dict[str, Any] | None
    failure_reason: str | None
    turn_id: UUID | None
    tool_call_count: int
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def submitted_payload(self) -> dict[str, Any] | None:
        """Alias for ``brief`` — the submitted tool payload."""
        return self.brief

    @submitted_payload.setter
    def submitted_payload(self, value: dict[str, Any] | None) -> None:
        self.brief = value


async def run_agentic_nonchat_job(
    *,
    kind: str,
    user: Any,
    conversation_id: UUID,
    system_task: str,
    pool: Any,
    bot_spec: Any,
    bot_id: str,
    topic_id: UUID | None,
    partner: Any | None,
    hot_context: str,
    max_tool_iterations: int | None = None,
    trigger_metadata: dict[str, Any] | None = None,
    config: NonchatJobConfig | None = None,
    # ── Deprecated kwargs kept for backward compatibility ──────────────
    # max_tool_iterations as a positional-style keyword is still accepted
    # but the authoritative cap comes from config when present.
) -> NonchatJobResult:
    """Run a bounded, non-chat agentic job with the selected bot's identity.

    The job opens a private ``bot_turn`` (kind=*kind*), runs a single
    ``run_step`` against the bot's *provider_chain*, and requires the model
    to call the *submit_tool_name* gate before caps are exhausted.  Plain
    text without a submit, an empty output, or hitting the cap all produce
    a failure result.

    When *config* is None the runner behaves exactly as it did for live_prep
    (current_step='live_prep', submit_live_brief gate, live_prep_* failure
    reasons).
    """
    cfg = config or LIVE_PREP_CONFIG
    settings = get_settings()
    trigger_metadata = trigger_metadata or {}

    # Resolve caps: config takes precedence, then the explicit kwarg,
    # then the settings default.
    _max_tool_iterations = cfg.max_tool_iterations
    if _max_tool_iterations is None:
        _max_tool_iterations = max_tool_iterations
    if _max_tool_iterations is None:
        _max_tool_iterations = getattr(settings, "live_prep_tool_cap", 50)

    _max_tool_calls = cfg.max_tool_calls

    fp = cfg.failure_reason_prefix  # short alias

    # ── 1. Build prompt / version snapshot ──────────────────────────────
    first_hop_provider = bot_spec.provider_chain[0]
    model_version = _provider_model(first_hop_provider, None)
    system_prompt_version = getattr(bot_spec, "system_prompt_version", "1.0.0")
    prompt_snapshot = system_task

    # ── 2. Open the non-chat turn ───────────────────────────────────────
    turn_id: UUID | None = None
    started_at: datetime | None = None
    try:
        turn_id, started_at = await _open_nonchat_turn(
            pool,
            user.id,
            prompt_snapshot,
            model_version,
            system_prompt_version,
            bot_id=bot_id,
            topic_id=topic_id,
            kind=kind,
            conversation_id=conversation_id,
        )
    except Exception:
        logger.exception(
            "nonchat_job: failed to open turn kind=%s conversation_id=%s bot_id=%s",
            kind,
            conversation_id,
            bot_id,
        )
        return NonchatJobResult(
            success=False,
            brief=None,
            failure_reason=f"{fp}_submit_missing",
            turn_id=None,
            tool_call_count=0,
            extras={},
        )

    # ── 3. Build TurnContext ────────────────────────────────────────────
    # flat_allowed_tools is set from config when present; when None
    # _step_allowed falls back to STEP_ALLOWED_TOOLS (live_prep path).
    ctx = TurnContext(
        turn_id=turn_id,
        pool=pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        bot_id=bot_id,
        transport=None,
        user_id=user.id,
        bot_spec=bot_spec,
        binding_id=None,
        participants_shape=getattr(bot_spec, "participants_shape", None),
        primary_topic_id=topic_id,
        primary_topic_slug=getattr(bot_spec, "primary_topic_slug", None),
        channel_id=None,
        read_scopes=getattr(bot_spec, "read_scopes", None),
        write_scopes=getattr(bot_spec, "write_scopes", None),
        cross_topic_policy=getattr(bot_spec, "cross_topic_policy", None),
        dyad_id=None,
        flat_allowed_tools=cfg.allowed_tools,
        current_step=cfg.current_step,  # type: ignore[arg-type]
        turn_started_at=started_at,
        trigger_metadata=trigger_metadata,
    )
    ctx.extras.update(cfg.initial_extras)

    # ── 4. Build allowed_tools ──────────────────────────────────────────
    # When flat_allowed_tools is set, _step_allowed uses it as the
    # authoritative set (still intersected with bot_allowlist / exclusives).
    allowed_tools = _step_allowed(ctx)

    # ── 5. Synthesize a minimal InboundScope for turn finalization ─────
    scope = InboundScope(
        bot_id=bot_id,
        transport=None,
        user_id=user.id,
        topic_id=topic_id or UUID("00000000-0000-0000-0000-000000000000"),
        channel_id=None,
        binding_id=None,
        dyad_id=None,
    )

    tool_call_count = 0
    try:
        # ── 6. Execute the single run_step with provider chain ──────────
        final_text, _messages, tool_call_count = await run_step(
            None,  # client — let run_step build from provider_chain
            ctx,
            system_prompt=prompt_snapshot,
            hot_context_rendered=hot_context or "",
            allowed_tools=allowed_tools,
            seed_messages=[],
            provider_chain=bot_spec.provider_chain,
            max_tool_iterations=_max_tool_iterations,
            max_tool_calls=_max_tool_calls,
        )

        # ── 7. Evaluate outcome ────────────────────────────────────────
        submitted = ctx.extras.get(cfg.submit_extras_key)
        if submitted:
            # Success path — model called the submit gate tool
            reasoning = f"{cfg.current_step} completed"
            await _finalize_turn_atomically(
                pool,
                turn_id,
                started_at,
                None,  # final_output_message_id
                tool_call_count,
                reasoning,
                outcome="responded",
                scope=scope,
                primary_topic_id=topic_id,
            )
            logger.info(
                "nonchat_job: %s submitted successfully turn_id=%s",
                cfg.current_step,
                turn_id,
                extra=obs_fields(ctx),
            )
            return NonchatJobResult(
                success=True,
                brief=submitted,
                failure_reason=None,
                turn_id=turn_id,
                tool_call_count=tool_call_count,
                extras=dict(ctx.extras),
            )

        if final_text and final_text.strip():
            # Plain text without submit — model responded but didn't call the gate
            failure_reason = f"{fp}_text_no_submit"
            await _finalize_turn_atomically(
                pool,
                turn_id,
                started_at,
                None,
                tool_call_count,
                f"plain text without submit: {final_text[:200]}",
                outcome="failed",
                scope=scope,
                primary_topic_id=topic_id,
                failure_reason=failure_reason,
                failure_class="infra_bug",
                processing_error=failure_reason,
            )
            logger.warning(
                "nonchat_job: %s produced text without %s turn_id=%s text_len=%d",
                cfg.current_step,
                cfg.submit_tool_name,
                turn_id,
                len(final_text),
                extra=obs_fields(ctx),
            )
            return NonchatJobResult(
                success=False,
                brief=None,
                failure_reason=failure_reason,
                turn_id=turn_id,
                tool_call_count=tool_call_count,
                extras=dict(ctx.extras),
            )

        # Neither text nor submit — model stopped with no output
        failure_reason = f"{fp}_submit_missing"
        await _finalize_turn_atomically(
            pool,
            turn_id,
            started_at,
            None,
            tool_call_count,
            f"no text output and no {cfg.submit_tool_name}",
            outcome="failed",
            scope=scope,
            primary_topic_id=topic_id,
            failure_reason=failure_reason,
            failure_class="infra_bug",
            processing_error=failure_reason,
        )
        logger.warning(
            "nonchat_job: %s produced no output and no submit turn_id=%s",
            cfg.current_step,
            turn_id,
            extra=obs_fields(ctx),
        )
        return NonchatJobResult(
            success=False,
            brief=None,
            failure_reason=failure_reason,
            turn_id=turn_id,
            tool_call_count=tool_call_count,
            extras=dict(ctx.extras),
        )

    except MaxToolCallsExceeded as exc:
        # Tool-call cap reached without the submit gate.  The messages
        # list already has stub tool_result blocks paired by run_step.
        # This is a distinct retryable failure reason for debrief jobs.
        failure_reason = f"{fp}_submit_missing_at_tool_cap"
        await _finalize_turn_atomically(
            pool,
            turn_id,
            started_at,
            None,
            exc.tool_call_count,
            f"tool call cap ({exc.max_calls}) exceeded without {cfg.submit_tool_name}",
            outcome="failed",
            scope=scope,
            primary_topic_id=topic_id,
            failure_reason=failure_reason,
            failure_class="infra_bug",
            processing_error=failure_reason,
        )
        logger.warning(
            "nonchat_job: %s tool-call cap exhausted without submit "
            "turn_id=%s tool_calls=%d cap=%d",
            cfg.current_step,
            turn_id,
            exc.tool_call_count,
            exc.max_calls,
            extra=obs_fields(ctx),
        )
        return NonchatJobResult(
            success=False,
            brief=None,
            failure_reason=failure_reason,
            turn_id=turn_id,
            tool_call_count=exc.tool_call_count,
            extras=dict(ctx.extras),
        )

    except BoundedLoopExceeded:
        # Tool iteration cap exhausted without submit gate.
        failure_reason = (
            f"{fp}_submit_missing_at_tool_cap"
            if cfg.current_step == "live_debrief"
            else f"{fp}_submit_missing"
        )
        await _finalize_turn_atomically(
            pool,
            turn_id,
            started_at,
            None,
            tool_call_count,
            f"tool iteration cap exceeded without {cfg.submit_tool_name}",
            outcome="failed",
            scope=scope,
            primary_topic_id=topic_id,
            failure_reason=failure_reason,
            failure_class="infra_bug",
            processing_error=failure_reason,
        )
        logger.warning(
            "nonchat_job: %s tool iteration cap exhausted without submit "
            "turn_id=%s cap=%d",
            cfg.current_step,
            turn_id,
            _max_tool_iterations,
            extra=obs_fields(ctx),
        )
        return NonchatJobResult(
            success=False,
            brief=None,
            failure_reason=failure_reason,
            turn_id=turn_id,
            tool_call_count=tool_call_count,
            extras=dict(ctx.extras),
        )

    except Exception:
        # Broad catch — any unhandled exception during the job.
        logger.exception(
            "nonchat_job: %s crashed turn_id=%s",
            cfg.current_step,
            turn_id,
            extra=obs_fields(ctx),
        )
        # Best-effort finalization if we have a turn_id
        if turn_id is not None:
            try:
                await _finalize_turn_atomically(
                    pool,
                    turn_id,
                    started_at,
                    None,
                    tool_call_count,
                    f"{cfg.current_step} crashed",
                    outcome="failed",
                    scope=scope,
                    primary_topic_id=topic_id,
                    failure_reason=f"{fp}_submit_missing",
                    failure_class="infra_bug",
                    processing_error="crashed",
                )
            except Exception:
                logger.exception(
                    "nonchat_job: failed to finalize turn after crash turn_id=%s",
                    turn_id,
                )
        return NonchatJobResult(
            success=False,
            brief=None,
            failure_reason=f"{fp}_submit_missing",
            turn_id=turn_id,
            tool_call_count=tool_call_count,
            extras=dict(ctx.extras),
        )
