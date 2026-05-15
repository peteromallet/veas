"""Auth surface for the Live Voice web app.

R5 (see ``docs/live-voice-agent-meta-checklist.md``) picked Discord
magic-link DM auth as the v1 path because no `DISCORD_CLIENT_ID/SECRET`
is available for code-grant OAuth.

Public API:

* :mod:`jwt` — compact HMAC-SHA256 token mint + verify.
* :mod:`magic_link` — request a code (DM it via the mediator bot) and
  verify it to mint a JWT.
* :mod:`discord_dm` — thin Discord REST wrapper used by ``magic_link``
  to deliver the code.
"""
