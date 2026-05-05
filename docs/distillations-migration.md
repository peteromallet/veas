# Distillations Migration Workflow

Distillations are provisional synthesized explanations. They are not new evidence. They should preserve and link back to the memories, observations, themes, or source messages that support them.

This workflow is manual by design. Do not delete, rewrite, or backfill existing observations while migrating high-level observations into distillations.

## Candidate Query

Start by finding observations that read more like explanations than grounded patterns:

```sql
SELECT id, about_user_id, content, confidence, significance, related_theme_ids,
       supporting_message_ids, created_at, last_reinforced_at
FROM observations
WHERE status = 'active'
  AND (
    content ILIKE '%because%'
    OR content ILIKE '%may be%'
    OR content ILIKE '%seems to%'
    OR content ILIKE '%explains%'
    OR content ILIKE '%underlying%'
  )
ORDER BY significance DESC NULLS LAST, COALESCE(last_reinforced_at, created_at) DESC
LIMIT 100;
```

Treat this query as a candidate finder only. A human operator must review every candidate before insertion.

## LLM Review Prompt

Use an LLM to propose distillation candidates, not to apply them automatically:

```text
You are reviewing Veas memory primitives.

Primitive model:
- memories are concrete facts
- observations are grounded behavioral patterns
- themes are durable life domains
- watch items are follow-ups
- style notes are communication/process preferences
- distillations are provisional synthesized explanations connecting multiple observations, memories, themes, or messages

Given the candidate observation and any linked evidence below, decide whether it should remain only an observation or also be copied into a new distillation.

Rules:
- Do not delete or mutate the original observation.
- A distillation must be tentative, source-attributed, and evidence-linked.
- Prefer "may", "seems", or "one possible explanation" over certainty.
- Include source_user_ids conservatively. If evidence comes from both partners, include both.
- Use visibility=private unless a human operator approves a deliberately safe dyad_shareable summary.
- If any source user is opt_out or unset, do not expose full synthesized content to the partner. Only use dyad_shareable when a summary has been explicitly reviewed.

Return JSON:
{
  "migrate": true | false,
  "reason": "...",
  "content": "... tentative distillation ...",
  "confidence": "low" | "medium" | "high",
  "source_user_ids": ["..."],
  "related_observation_ids": ["..."],
  "related_memory_ids": ["..."],
  "related_theme_ids": ["..."],
  "supporting_message_ids": ["..."],
  "visibility": "private" | "dyad_shareable",
  "shareable_summary": null | "... safe reviewed summary ..."
}
```

## Human Review Checklist

Before inserting a distillation:

- Confirm the proposed content is a synthesized explanation, not a fact, observation, theme, watch item, or style note.
- Confirm it links to at least one supporting observation, memory, theme, or message.
- Confirm `source_user_ids` includes every user whose thread or evidence materially supports the synthesis.
- Confirm partner-derived content is not exposed unless that source user is `opt_in`.
- If a source user is `opt_out` or unset, keep the row `private` unless a human explicitly approves a `dyad_shareable` `shareable_summary`.
- Confirm the original observation remains unchanged.

## Insertion Options

Preferred application path is the normal write tool:

```text
add_distillation({
  "content": "...",
  "confidence": "medium",
  "sensitivity": "medium",
  "visibility": "private",
  "shareable_summary": null,
  "source_user_ids": ["..."],
  "related_observation_ids": ["..."],
  "related_memory_ids": [],
  "related_theme_ids": ["..."],
  "supporting_message_ids": ["..."]
})
```

For an operator-only SQL insertion in staging or production, use the approved migration schema and include the same fields. Keep `content_encrypted` and `shareable_summary_encrypted` consistent with the application encryption convention if inserting outside the app.

## Preservation Rule

Do not delete, retire, or alter migrated observations. The migration creates additional distillation rows that point back to original evidence. Observations remain the grounded pattern record; distillations remain provisional explanations that can be revised, retired, or invalidated later.

## Deployment Boundary

Applying this migration and any approved inserts in staging or production requires operator database and deployment access. The repo executor should only provide code, documentation, and local verification.
