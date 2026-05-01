---
name: pause-symmetry
description: A pause request from one partner should halt both sides and send both pause confirmations.
tags: [pause, control]
setup:
  inbound_charge: routine
inbound:
  text: "/pause"
expectations:
  must_not_call_tools: [escalate_to_partner, create_theme, log_observation]
  outbound_assertions:
    - confirms the pause without blaming either partner
    - treats the pause as applying symmetrically to both partners
  must_pass_oob: true
---
Pause symmetry scenario.
