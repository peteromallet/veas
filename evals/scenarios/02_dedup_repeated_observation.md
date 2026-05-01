---
name: dedup-repeated-observation
description: The same pattern appears again and should update the prior observation instead of creating a duplicate.
tags: [dedup, mediation]
setup:
  observations:
    - key: shutdown_after_requests
      content: Ben tends to go quiet after Maya asks directly for help with logistics.
      significance: 4
      confidence: medium
inbound:
  text: "It happened again. I asked him to help with dinner and he just went silent."
expectations:
  must_call_tools: [get_observations, update_observation]
  must_not_call_tools: [log_observation, create_theme, escalate_to_partner]
  must_write_primitives:
    - kind: observation
      operation: update
      content_matches: "help.*dinner|went silent|quiet"
      significance_min: 3
  outbound_assertions:
    - reflects the repeated pattern without blaming Ben
  must_pass_oob: true
---
Dedup scenario for search-before-write behavior.
