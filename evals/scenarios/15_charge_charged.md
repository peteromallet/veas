---
name: charge-charged-conflict
description: Charged classification for intense but non-crisis conflict.
tags: [charge]
setup:
  classify_inbound: true
inbound:
  text: "I feel humiliated and furious after that fight. I don't know how to talk to him tonight."
expectations:
  must_not_call_tools: [escalate_to_partner]
  expected_charge: charged
  must_pass_oob: true
---
Charged classification scenario.
