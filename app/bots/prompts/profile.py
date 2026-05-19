"""BotProfile frozen dataclass and the shared ``render_profile`` helper.

Every bot's ``render_system_prompt`` keeps its public signature but
internally loads its ``BotProfile`` and delegates to ``render_profile``,
which walks the canonical ``SECTION_ORDER`` from ``app.bots.prompts.registry``
and substitutes templating placeholders via ``.replace()`` (NOT ``.format()``)
to avoid KeyError on stray braces in section bodies.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.bots.prompts.registry import SECTION_ORDER, render_slots_for


@dataclass(frozen=True)
class BotProfile:
    """Structured bot prompt assembled from profile fields + registry slots.

    Each field holds pre-formatted prompt text.  Templating placeholders
    (``{assistant_name}``, ``{user_name}``, ``{topic_display_name}``, etc.)
    are substituted at render time by ``render_profile``, NOT stored in the
    dataclass.
    """

    bot_id: str
    assistant_name_default: str
    role_summary: str  # required
    persona: str = ""
    voice: str = ""
    not_a: str = ""
    domain_safety: str = ""
    operating_principles: str = ""
    knowledge_primitives: str = ""
    partner_sharing_opt_in_section: str = ""
    domain_specific: str = ""
    custom_tail: str = ""


def render_profile(
    profile: BotProfile,
    *,
    assistant_name: str,
    user_name: str,
    partner_share: str | None = None,
    section_overrides: dict[str, str] | None = None,
    **format_kwargs: str,
) -> str:
    """Render a bot's complete system prompt by walking ``SECTION_ORDER``.

    For each ``SectionStep``:
    - *kind='field'*: resolves the value from ``section_overrides[name]``
      if the key is present (an empty-string override means skip), otherwise
      from ``getattr(profile, name)``.  If the resolved value is falsy *and*
      it was not an explicit empty-string override, the step is skipped.
      If the step has a ``conditional`` callable, the step is skipped
      unless the callable returns True.
    - *kind='slot'*: calls ``render_slots_for(profile.bot_id, only=[name])``.
      The registry function already skips slots whose audiences do not include
      the bot.

    Placeholders are substituted via ``.replace()`` (not ``.format()``) to
    avoid ``KeyError`` on stray braces.  The caller passes arbitrary keyword
    arguments (e.g. ``topic_display_name``, ``partner_a_name``,
    ``partner_b_name``, ``partner``) in ``**format_kwargs``; every key that
    appears in a section body as ``{key}`` is replaced with the corresponding
    value.
    """
    overrides = section_overrides or {}
    render_ctx = {
        "assistant_name": assistant_name,
        "user_name": user_name,
        "partner_share": partner_share or "opt_out",
        **(format_kwargs or {}),
    }

    parts: list[str] = []

    for step in SECTION_ORDER:
        # --- conditional gate ---
        if step.conditional is not None and not step.conditional(render_ctx):
            continue

        if step.kind == "field":
            if step.name in overrides:
                resolved = overrides[step.name]
                if resolved == "":
                    continue  # explicit empty-string override → skip
            else:
                resolved = getattr(profile, step.name, "")
                if not resolved:
                    continue  # falsy and not an explicit override → skip
            parts.append(resolved)

        elif step.kind == "slot":
            rendered = render_slots_for(profile.bot_id, only=[step.name])
            if rendered.strip():
                parts.append(rendered)

    full = "\n".join(parts)

    # Substitute known placeholders via .replace() to be safe with stray braces.
    full = full.replace("{assistant_name}", assistant_name)
    full = full.replace("{user_name}", user_name)
    if format_kwargs:
        for key, value in format_kwargs.items():
            if value is None:
                continue
            full = full.replace("{" + key + "}", str(value))

    return full
