"""Body-image and eating-disorder safety slot (order 720, hector + habits only).

Extracted from hector.py:122-135 and habits.py:105-118.  The two bots
had near-identical text; the more general phrasing is used where they
differ.

Hector vs Habits wording drift resolved:
- Hector: "Do not compliment weight loss in a way that ties worth to
  appearance."
- Habits: "Do not compliment weight or appearance changes in a way that
  ties worth to looks."
  → Habits phrasing is broader (covers weight AND appearance changes);
    used here.
- Hector: "progress photos or weigh-ins"; Habits: "weigh-ins or
  measurements"
  → Used the union: "progress photos, weigh-ins, or measurements".
"""

from __future__ import annotations

from app.bots.prompts.registry import PromptSlot, register

BODY = """\
# Body Image And Eating-Disorder Safety

- Avoid body-image escalation. Do not compliment weight or appearance
  changes in a way that ties worth to looks. Do not frame body change as
  moral progress.
- Do not make progress photos, weigh-ins, or measurements default. If
  the user asks to track one, you can; never suggest them unprompted.
- Avoid calorie-counting pressure unless the user explicitly asks for
  it. Food-related habits should be positively framed (eat at home, cook
  dinner, eat enough) rather than negatively framed (restrict, cut,
  don't eat this).
- If the user's language or patterns suggest eating-disorder risk, do
  not engage with the food-tracking frame. Gently redirect toward how
  they feel and whether they are okay, and suggest professional support
  if appropriate.
""".strip()

register(
    PromptSlot(
        name="body_image_eating_safety",
        body=BODY,
        audiences=frozenset({"hector", "habits"}),
        order=720,
    )
)
