"""Live-voice service package.

Sprint 1+ surface lives under this package:

* :mod:`schemas` — Pydantic models for the agenda and (later) emit_live_turn.
* :mod:`prep` — Opus-driven agenda producer; entry point :func:`prep.produce_agenda`.
* :mod:`orchestrator` — turn-loop driver (Sprint 3+).
* :mod:`turn_loop` — Haiku per-turn caller (Sprint 3+).
* :mod:`synthesis` — Opus end-of-session synthesizer (Sprint 3+).

The skeleton modules in this package were created during Sprint 0 to lock
import paths.  Behavior is added per-sprint as listed in
``docs/live-voice-agent-meta-checklist.md``.
"""

from app.services.live import schemas  # re-export for convenience

__all__ = ["schemas"]
