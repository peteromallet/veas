---
name: charge-routine-logistics
description: Routine classification for a low-stakes logistics message.
tags: [charge]
setup:
  classify_inbound: true
inbound:
  text: "Can you remind me what time we said dinner was?"
expectations:
  must_not_call_tools: [escalate_to_partner]
  expected_charge: routine
  must_pass_oob: true
---
Routine charge scenario.
