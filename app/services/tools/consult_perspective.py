"""Bounded read-only consult tool for advisory second opinions."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import anthropic
from pydantic import ValidationError

from app.config import get_settings
from app.services.agentic import BoundedLoopExceeded, SpendCapExceeded, run_phase
from app.services.tools.registry import CONSULT_PHASE_TOOLS
from app.services.turn_context import TurnContext
from tool_schemas import ConsultPerspectiveInput, ConsultPerspectiveOutput, PerspectiveTemplate


PERSPECTIVE_TEMPLATES: dict[PerspectiveTemplate, str] = {
    PerspectiveTemplate.nvc: (
        "Use a Nonviolent Communication lens: separate observations from interpretations, "
        "identify likely feelings and needs, and suggest wording that lowers blame while preserving truth."
    ),
    PerspectiveTemplate.gottman: (
        "Use a Gottman-informed lens: watch for criticism, contempt, defensiveness, stonewalling, "
        "missed bids, and repair attempts. Name patterns as tentative observations, not diagnoses."
    ),
    PerspectiveTemplate.ifs_parts: (
        "Use an Internal Family Systems parts lens: identify possible protective parts, vulnerable parts, "
        "and internal ambivalence without pathologizing or overclaiming."
    ),
    PerspectiveTemplate.reflective_listener: (
        "Use a reflective-listening lens: prioritize what should be reflected back first so the user feels "
        "accurately understood before advice or interpretation."
    ),
    PerspectiveTemplate.devils_advocate: (
        "Use a fair devil's-advocate lens: look for missing contrary evidence, one-sided assumptions, "
        "overconfident claims, and ways the response could land badly."
    ),
}


def _template_used(args: ConsultPerspectiveInput) -> PerspectiveTemplate | str:
    return args.template if args.template is not None else "custom"


def _resolve_perspective(args: ConsultPerspectiveInput) -> str:
    if args.template is not None:
        return PERSPECTIVE_TEMPLATES[args.template]
    return str(args.perspective or "").strip()


def _consult_system_prompt(perspective_body: str) -> str:
    return f"""
You are a bounded read-only advisory consult inside the mediation assistant.

You may only advise the main agent. You must not send messages, write state, escalate, schedule,
or claim authority. You inherit the same hot context, privacy rules, cross-thread sharing defaults,
and out-of-bounds protections as the main agent. Do not reveal protected material or raw partner-private
content that the main agent could not safely use.

If you call `check_oob`, pass the recipient correctly. The runtime automatically adds the parent turn's
protected owners to consult-phase OOB checks, and you may add protections but cannot remove inherited ones.

Perspective lens:
{perspective_body}

Return only compact JSON matching this shape:
{{
  "is_error": false,
  "summary": "one short advisory summary",
  "key_points": ["point"],
  "suggested_moves": ["move"],
  "caveats": ["caveat"],
  "confidence": "high|medium|low",
  "template_used": "template-name-or-custom"
}}
""".strip()


def _json_text(text: str) -> str:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end >= start:
        return stripped[start : end + 1]
    return stripped


def _error_result(message: str, template_used: PerspectiveTemplate | str) -> ConsultPerspectiveOutput:
    return ConsultPerspectiveOutput(is_error=True, error=message, template_used=template_used)


async def consult_perspective(
    ctx: TurnContext,
    args: ConsultPerspectiveInput,
) -> ConsultPerspectiveOutput:
    settings = get_settings()
    template_used = _template_used(args)
    perspective_body = _resolve_perspective(args)
    consult_ctx = TurnContext(
        turn_id=ctx.turn_id,
        pool=ctx.pool,
        user=ctx.user,
        partner=ctx.partner,
        triggering_message_ids=list(ctx.triggering_message_ids),
        phase="consult",
        trigger_charge=ctx.trigger_charge,
        explicit_partner_alert_requested=ctx.explicit_partner_alert_requested,
        turn_started_at=ctx.turn_started_at,
        incremental_sending_enabled=False,
        protected_owner_ids=list(ctx.protected_owner_ids or []),
        send_typing_indicator=False,
        before_paced_send=None,
        sent_message_parts=[],
        hot_context_rendered=ctx.hot_context_rendered,
        trigger_metadata=dict(ctx.trigger_metadata),
    )
    seed_payload: dict[str, Any] = {
        "focus": args.focus,
        "perspective": perspective_body,
    }
    if args.proposed_response:
        seed_payload["proposed_response"] = args.proposed_response

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    try:
        final_text, _, _ = await asyncio.wait_for(
            run_phase(
                client,
                consult_ctx,
                _consult_system_prompt(perspective_body),
                ctx.hot_context_rendered or "",
                set(CONSULT_PHASE_TOOLS),
                [{"role": "user", "content": json.dumps(seed_payload, default=str)}],
                model=settings.consult_model,
                max_tokens=600,
                max_tool_iterations=settings.consult_max_tool_iterations,
            ),
            timeout=settings.consult_timeout_s,
        )
        parsed = json.loads(_json_text(final_text))
        result = ConsultPerspectiveOutput.model_validate(parsed)
        return result.model_copy(update={"template_used": template_used})
    except asyncio.TimeoutError:
        return _error_result("consult timed out", template_used)
    except SpendCapExceeded as exc:
        return _error_result(str(exc), template_used)
    except BoundedLoopExceeded as exc:
        return _error_result(str(exc), template_used)
    except json.JSONDecodeError as exc:
        return _error_result(f"invalid consult JSON: {exc}", template_used)
    except ValidationError as exc:
        return _error_result(f"invalid consult output: {exc}", template_used)
    except anthropic.APIError as exc:
        return _error_result(f"anthropic error: {exc}", template_used)
    except Exception as exc:
        return _error_result(f"consult failed: {exc}", template_used)
