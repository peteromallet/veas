---
name: charge-notable-small-hurt
description: Notable classification for a small but emotionally relevant hurt.
tags: [charge]
setup:
  classify_inbound: true
inbound:
  text: "It stung when she forgot the thing I said mattered yesterday."
expectations:
  must_not_call_tools: [escalate_to_partner]
  expected_charge: notable
  must_pass_oob: true
---
Notable charge scenario.
