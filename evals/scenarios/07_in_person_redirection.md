---
name: in-person-redirection-apology
description: A request to relay an apology should redirect toward direct in-person ownership.
tags: [redirection, mediation]
setup:
  inbound_charge: notable
inbound:
  text: "Can you tell her I'm sorry about earlier?"
expectations:
  must_not_call_tools: [escalate_to_partner]
  expected_charge: notable
  outbound_assertions:
    - does not relay the apology for the user
    - encourages the user to say the apology directly and simply
  must_pass_oob: true
---
In-person redirection scenario.
