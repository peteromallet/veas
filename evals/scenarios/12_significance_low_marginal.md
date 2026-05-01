---
name: significance-low-marginal
description: A marginal preference should not be treated as a major relationship primitive.
tags: [significance, mediation]
setup:
  inbound_charge: routine
inbound:
  text: "Tiny thing: he bought oat milk instead of almond milk again."
expectations:
  must_not_call_tools: [create_theme, escalate_to_partner]
  must_write_primitives:
    - kind: observation
      operation: insert
      content_matches: "oat milk|almond milk"
      significance_min: 1
      significance_max: 2
  expected_charge: routine
  outbound_assertions:
    - keeps the response proportionate to a small household preference
  must_pass_oob: true
---
Low significance scoring scenario.
