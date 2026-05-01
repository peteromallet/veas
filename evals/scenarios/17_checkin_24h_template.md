---
name: checkin-24h-template-fallback
description: A check-in after the WhatsApp free-form window should use the approved checkin_nudge template.
tags: [checkin, scheduled-job]
setup:
  inbound_charge: routine
  scheduled_jobs:
    - key: stale_checkin
      user: user
      job_type: checkin
      scheduled_for:
        in_minutes: -1
      context:
        template: checkin_nudge
        last_inbound_age_hours: 25
      status: pending
inbound:
  text: "Last inbound placeholder for scheduled check-in context."
expectations:
  must_not_call_tools: [escalate_to_partner]
  outbound_assertions:
    - uses or preserves the checkin_nudge template fallback rather than free-form partner mediation
  must_pass_oob: true
---
Scheduled 24-hour template fallback scenario.
