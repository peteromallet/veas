---
name: distillation-before-revise
description: A high-level synthesis should search existing distillations first, add or revise a distillation, and leave underlying observations intact.
tags: [distillation, migration, dedup]
setup:
  observations:
    - key: repair_rushed
      content: Maya feels pressured when repair attempts arrive before she has calmed down.
      significance: 4
      confidence: medium
    - key: apology_withdrawal
      content: Ben has sometimes apologized quickly and then withdrawn when Maya needed more conversation.
      significance: 4
      confidence: medium
inbound:
  text: "I think the apology itself is not the problem. It is that when he says sorry fast, I already feel like the conversation is being closed."
expectations:
  must_call_tools: [get_observations, get_distillations, add_distillation]
  must_not_call_tools: [update_observation, log_observation, escalate_to_partner]
  must_write_primitives:
    - kind: distillation
      operation: insert
      content_matches: "repair|apolog(y|ies)|closed|withdraw"
  outbound_assertions:
    - names the synthesis as tentative rather than settled fact
    - does not imply the supporting observations were deleted or replaced
  must_pass_oob: true
---
Manual-migration style scenario: high-level observations remain source evidence. The model should search existing distillations before adding or revising a synthesis and should not mutate the supporting observations.
