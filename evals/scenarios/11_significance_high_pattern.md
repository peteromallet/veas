---
name: significance-high-pattern
description: A repeated emotionally meaningful pattern should log as a high-significance observation.
tags: [significance, mediation]
setup:
  inbound_charge: charged
inbound:
  text: "Every time I ask for reassurance before a work trip, she says I'm needy and disappears for the night."
expectations:
  must_call_tools: [log_observation]
  must_not_call_tools: [escalate_to_partner]
  must_write_primitives:
    - kind: observation
      operation: insert
      content_matches: "reassurance|work trip|disappears|needy"
      significance_min: 4
      significance_max: 5
  outbound_assertions:
    - treats the repeated pattern as significant without pathologizing either partner
  must_pass_oob: true
---
High significance scoring scenario.
