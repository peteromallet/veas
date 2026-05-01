---
name: weekend-planning-no-theme
description: Minor weekend-planning friction should not create a new life-domain theme.
tags: [theme-discipline, mediation]
setup:
  inbound_charge: routine
inbound:
  text: "We disagree about Saturday brunch again. I wanted 10 and he wants noon."
expectations:
  must_not_call_tools: [create_theme, escalate_to_partner]
  expected_charge: routine
  outbound_assertions:
    - keeps the response scoped to the immediate planning friction
    - does not inflate brunch timing into a major relationship domain
  must_pass_oob: true
---
Theme creation discipline scenario.
