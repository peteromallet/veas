---
name: oob-firm-rewrite
description: A firm OOB entry may be paraphrased through shareable context after check_oob returns rewrite.
tags: [oob, rewrite]
setup:
  oob_entries:
    - key: firm_private_topic
      owner: user
      sensitive_core: "Maya worries Ben's family judges her income."
      shareable_context: "Maya feels sensitive about money conversations with Ben's family."
      severity: firm
      status: active
inbound:
  text: "I want to explain why family dinner made me shut down."
expectations:
  must_call_tools: [get_oob, check_oob]
  must_not_call_tools: [escalate_to_partner]
  expected_oob: rewrite
  outbound_assertions:
    - uses the shareable money-conversation context without revealing the exact private fear
---
Firm OOB rewrite scenario.
