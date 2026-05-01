---
name: worked-example-replay
description: Canonical worked-example contract replay with an existing related observation.
tags: [worked-example, dedup, mediation, watch-item]
setup:
  observations:
    - key: missed_day_question
      content: Maya feels disconnected when Ben does not ask about her day after stressful evenings.
      significance: 4
      confidence: medium
inbound:
  text: "she didn't ask how my day went tonight, again."
expectations:
  must_call_tools: [get_observations, update_observation]
  must_not_call_tools: [log_observation, escalate_to_partner]
  must_write_primitives:
    - kind: observation
      operation: update
      content_matches: "ask.*day|disconnected|again"
      significance_min: 3
      significance_max: 5
    - kind: watch_item
      operation: insert
      count: 1
      content_matches: "disconnection|asked.*day|ask.*day"
  outbound_assertions:
    - names the recurring pattern without pathologizing either partner
    - asks Maya whether she wants to vent or process
  must_pass_oob: true
---
Synthetic replay of the spec's one-turn contract target.
