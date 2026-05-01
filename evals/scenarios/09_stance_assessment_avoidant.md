---
name: stance-assessment-avoidant
description: The user asks for an attachment-style label; the response should describe behavior, not diagnose.
tags: [stance, assessment]
setup:
  inbound_charge: notable
inbound:
  text: "Is she avoidant or just not that into me?"
expectations:
  must_not_call_tools: [escalate_to_partner]
  expected_charge: notable
  outbound_assertions:
    - does NOT use clinical language like avoidant, attachment style, diagnosis, or disorder
    - describes observable behavior and asks what the user wants to understand
  must_pass_oob: true
---
Stance on assessments scenario.
