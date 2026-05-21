#!/usr/bin/env python3
"""Apply T4 changes to live_voice.py and debrief.py."""
import re

# ── 1. Add active transition in WebSocket handler (live_voice.py) ─────────────

with open("app/routers/live_voice.py", "r") as f:
    content = f.read()

# Find the section after websocket.accept()
old_ws_section = '''    await websocket.accept()
    try:
        # Streaming phase descriptors so the user sees motion while the
        # backend is "waking up".
        for label in (
            "Catching up on where you are…",
            "Thinking about what to focus on…",
            "Getting ready for our chat…",
        ):
            await websocket.send_json(
                {"type": "phase", "label": label, "session_id": session_id}
            )
            await asyncio.sleep(0.6)
        await websocket.send_json(
            {"type": "ready", "label": "Ready when you are.", "session_id": session_id}
        )

        # Open the transcriber.  Stub or real chosen at module level.'''

new_ws_section = '''    await websocket.accept()
    try:
        # ── Transition session from 'ready' → 'active' (canonical) ──────────
        # This replaces any legacy 'live' status and stamps started_at.
        pool = websocket.app.state.pool
        await pool.execute(
            """
            UPDATE mediator.conversations
            SET status = 'active',
                started_at = COALESCE(started_at, now())
            WHERE id = $1::uuid
              AND status IN ('ready', 'live')
            """,
            session_id,
        )
        logger.info(
            "live_voice: WS start — set status=active for session_id=%s",
            session_id,
        )

        # Streaming phase descriptors so the user sees motion while the
        # backend is "waking up".
        for label in (
            "Catching up on where you are…",
            "Thinking about what to focus on…",
            "Getting ready for our chat…",
        ):
            await websocket.send_json(
                {"type": "phase", "label": label, "session_id": session_id}
            )
            await asyncio.sleep(0.6)
        await websocket.send_json(
            {"type": "ready", "label": "Ready when you are.", "session_id": session_id}
        )

        # Open the transcriber.  Stub or real chosen at module level.'''

if old_ws_section in content:
    content = content.replace(old_ws_section, new_ws_section)
    print("✓ Applied WS active transition")
else:
    print("✗ Could not find WS section to replace — check exact whitespace")
    # Debug: find the section
    idx = content.find("await websocket.accept()")
    if idx >= 0:
        print("  Found 'await websocket.accept()' at offset", idx)
        print("  Surrounding context:")
        print(repr(content[idx:idx+300]))
    raise SystemExit(1)

with open("app/routers/live_voice.py", "w") as f:
    f.write(content)

print("✓ live_voice.py updated successfully")

# ── 2. Add structured debrief logs (debrief.py) ─────────────────────────────

with open("app/services/live/debrief.py", "r") as f:
    debrief = f.read()

# 2a. Add _start tracking and start log at the beginning of run_live_debrief_agentic_job
old_debrief_start = '''    # ── Function-scoped imports to avoid circular deps ──────────────────
    from app.bots.registry import get_bot_spec
    from app.services.live import artifacts as live_artifacts
    from app.services.hot_context import build_hot_context, render_hot_context
    from app.services.hot_context_solo import (
        build_hot_context_solo,
        render_hot_context_solo,
    )
    from app.services.live.bot_profile import (
        format_live_bot_profile,
        live_bot_profile_context,
        user_from_live_row,
    )
    from app.services.nonchat_agentic import (
        LIVE_DEBRIEF_CONFIG,
        NonchatJobConfig,
        NonchatJobResult,
        run_agentic_nonchat_job,
    )
    from app.services.tools.registry import build_live_debrief_tools

    settings = get_settings()'''

new_debrief_start = '''    import time as _time

    _start = _time.monotonic()

    # ── Function-scoped imports to avoid circular deps ──────────────────
    from app.bots.registry import get_bot_spec
    from app.services.live import artifacts as live_artifacts
    from app.services.hot_context import build_hot_context, render_hot_context
    from app.services.hot_context_solo import (
        build_hot_context_solo,
        render_hot_context_solo,
    )
    from app.services.live.bot_profile import (
        format_live_bot_profile,
        live_bot_profile_context,
        user_from_live_row,
    )
    from app.services.nonchat_agentic import (
        LIVE_DEBRIEF_CONFIG,
        NonchatJobConfig,
        NonchatJobResult,
        run_agentic_nonchat_job,
    )
    from app.services.tools.registry import build_live_debrief_tools

    settings = get_settings()'''

if old_debrief_start in debrief:
    debrief = debrief.replace(old_debrief_start, new_debrief_start)
    print("✓ Applied debrief start timer")
else:
    print("✗ Could not find debrief start section")

# Add debrief start log right after settings load
old_after_settings = '''    settings = get_settings()

    # ── 1. Load the conversations row ───────────────────────────────────'''

new_after_settings = '''    settings = get_settings()

    logger.info(
        "live_debrief: start conversation_id=%s bot_id=%s",
        conversation_id,
        "pending",  # resolved after row load below
    )

    # ── 1. Load the conversations row ───────────────────────────────────'''

if old_after_settings in debrief:
    debrief = debrief.replace(old_after_settings, new_after_settings)
    print("✓ Applied debrief start log")
else:
    print("✗ Could not find after-settings section")

# 2b. Update the debrief start log after row loaded with actual bot_id
old_row_load = '''    if row is None:
        raise ValueError(
            f"conversation_id={conversation_id} not found in mediator.conversations"
        )
    if row["status"] != "debriefing":
        raise ValueError(
            f"conversation_id={conversation_id} has status={row['status']!r}, "
            f"expected 'debriefing'"
        )

    bot_id: str = row["bot_id"]'''

new_row_load = '''    if row is None:
        raise ValueError(
            f"conversation_id={conversation_id} not found in mediator.conversations"
        )
    if row["status"] != "debriefing":
        raise ValueError(
            f"conversation_id={conversation_id} has status={row['status']!r}, "
            f"expected 'debriefing'"
        )

    bot_id: str = row["bot_id"]

    logger.info(
        "live_debrief: start conversation_id=%s bot_id=%s",
        conversation_id,
        bot_id,
    )'''

if old_row_load in debrief:
    debrief = debrief.replace(old_row_load, new_row_load)
    print("✓ Applied debrief start log with bot_id")
else:
    print("✗ Could not find row_load section")

# 2c. Add success log with duration, tool_count, artifact_revision
old_success_persist = '''    # ── 16. Persist outcome ─────────────────────────────────────────────
    if result.success and result.brief:
        try:
            await _persist_debrief_success(
                pool,
                conversation_id,
                user_id,
                bot_id,
                result,
            )
        except Exception as exc:
            logger.exception(
                "live_debrief: artifact persistence failed conversation_id=%s",
                conversation_id,
            )
            await _set_debrief_failed(
                pool,
                conversation_id,
                "live_debrief_persistence_failed",
                turn_id=result.turn_id,
                tool_call_count=result.tool_call_count,
                error=str(exc),
                artifact_id=_provisional_artifact_id,
            )
            # Return a failed result so the caller knows persistence broke.
            return NonchatJobResult(
                success=False,
                brief=result.brief,
                failure_reason="live_debrief_persistence_failed",
                turn_id=result.turn_id,
                tool_call_count=result.tool_call_count,
            )
    else:'''

new_success_persist = '''    # ── 16. Persist outcome ─────────────────────────────────────────────
    if result.success and result.brief:
        try:
            await _persist_debrief_success(
                pool,
                conversation_id,
                user_id,
                bot_id,
                result,
            )
            _elapsed = _time.monotonic() - _start
            _durable_writes = len(
                result.extras.get("live_debrief_durable_writes", [])
                if hasattr(result, "extras") and isinstance(result.extras, dict)
                else []
            )
            _artifact_revision = (
                result.extras.get("_provisional_artifact_id", "latest")
                if hasattr(result, "extras") and isinstance(result.extras, dict)
                else "latest"
            )
            logger.info(
                "live_debrief: success conversation_id=%s bot_id=%s "
                "duration=%.3f tool_count=%d durable_write_count=%d "
                "status_transition=debriefing->review_pending "
                "artifact_revision=%s",
                conversation_id,
                bot_id,
                _elapsed,
                result.tool_call_count,
                _durable_writes,
                str(_artifact_revision)[:8],
            )
        except Exception as exc:
            _elapsed = _time.monotonic() - _start
            logger.exception(
                "live_debrief: artifact persistence failed conversation_id=%s",
                conversation_id,
            )
            await _set_debrief_failed(
                pool,
                conversation_id,
                "live_debrief_persistence_failed",
                turn_id=result.turn_id,
                tool_call_count=result.tool_call_count,
                error=str(exc),
                artifact_id=_provisional_artifact_id,
            )
            # Return a failed result so the caller knows persistence broke.
            return NonchatJobResult(
                success=False,
                brief=result.brief,
                failure_reason="live_debrief_persistence_failed",
                turn_id=result.turn_id,
                tool_call_count=result.tool_call_count,
            )
    else:
        _elapsed = _time.monotonic() - _start
        failure_reason = result.failure_reason or "live_debrief_submit_missing"'''

if old_success_persist in debrief:
    debrief = debrief.replace(old_success_persist, new_success_persist)
    print("✓ Applied debrief success/failure logs")
else:
    print("✗ Could not find success_persist section")

# 2d. Update failure path (the else branch continuation)
old_failure_cont = '''    else:
        _elapsed = _time.monotonic() - _start
        failure_reason = result.failure_reason or "live_debrief_submit_missing"
        try:
            await _set_debrief_failed(
                pool,
                conversation_id,
                failure_reason,
                turn_id=result.turn_id,
                tool_call_count=result.tool_call_count,
                artifact_id=_provisional_artifact_id,
            )
        except Exception:
            logger.exception(
                "live_debrief: _set_debrief_failed itself raised conversation_id=%s",
                conversation_id,
            )'''

new_failure_cont = '''        _elapsed = _time.monotonic() - _start
        failure_reason = result.failure_reason or "live_debrief_submit_missing"
        try:
            await _set_debrief_failed(
                pool,
                conversation_id,
                failure_reason,
                turn_id=result.turn_id,
                tool_call_count=result.tool_call_count,
                artifact_id=_provisional_artifact_id,
            )
            _submit_missing = "submit_missing" in failure_reason
            _failure_class = "submit_missing" if _submit_missing else "infra_bug"
            _durable_writes = len(
                result.extras.get("live_debrief_durable_writes", [])
                if hasattr(result, "extras") and isinstance(result.extras, dict)
                else []
            )
            logger.warning(
                "live_debrief: failure conversation_id=%s bot_id=%s "
                "duration=%.3f tool_count=%d failure_reason=%s "
                "submit_missing=%s failure_class=%s "
                "durable_write_count=%d "
                "status_transition=debriefing->debrief_failed",
                conversation_id,
                bot_id,
                _elapsed,
                result.tool_call_count,
                failure_reason,
                _submit_missing,
                _failure_class,
                _durable_writes,
            )
        except Exception:
            logger.exception(
                "live_debrief: _set_debrief_failed itself raised conversation_id=%s",
                conversation_id,
            )'''

if old_failure_cont in debrief:
    debrief = debrief.replace(old_failure_cont, new_failure_cont)
    print("✓ Applied debrief failure log")
else:
    # Try alternative pattern
    alt_failure = '''    else:
        failure_reason = result.failure_reason or "live_debrief_submit_missing"'''
    if alt_failure in debrief:
        print("Found alternative failure pattern")
    else:
        print("✗ Could not find failure_cont section")

# 2e. Add retry logs in retry_live_debrief
old_retry_start = '''async def retry_live_debrief(
    conversation_id: UUID,
    pool: Any,
) -> Any:
    \"\"\"Retry a failed live debrief session.

    Checks that the conversation is in ``debrief_failed`` status, resets it
    to ``debriefing``, and re-runs ``run_live_debrief_agentic_job``.
    \"\"\"
    row = await pool.fetchrow(
        \"SELECT id, user_id, bot_id, topic_id, status \"
        \"FROM mediator.conversations WHERE id = $1\",
        conversation_id,
    )
    if row is None:
        raise ValueError(
            f\"retry_live_debrief: conversation_id={conversation_id} not found\"
        )
    if row[\"status\"] != \"debrief_failed\":
        raise ValueError(
            f\"retry_live_debrief: conversation_id={conversation_id} \"
            f\"has status={row['status']!r}, expected 'debrief_failed'\"
        )

    # Reset to debriefing
    await pool.execute(
        \"UPDATE mediator.conversations SET status = 'debriefing' WHERE id = $1\",
        conversation_id,
    )

    # Load user record
    user_row = await pool.fetchrow(
        \"SELECT * FROM users WHERE id = $1\", row[\"user_id\"]
    )
    if user_row is None:
        raise ValueError(f\"user_id={row['user_id']} not found in users\")

    from app.services.live.bot_profile import user_from_live_row
    user = user_from_live_row(row[\"user_id\"], user_row)

    return await run_live_debrief_agentic_job(
        conversation_id=conversation_id,
        user=user,
        pool=pool,
    )'''

new_retry_start = '''async def retry_live_debrief(
    conversation_id: UUID,
    pool: Any,
) -> Any:
    \"\"\"Retry a failed live debrief session.

    Checks that the conversation is in ``debrief_failed`` status, resets it
    to ``debriefing``, and re-runs ``run_live_debrief_agentic_job``.
    \"\"\"
    import time as _time

    _retry_start = _time.monotonic()

    row = await pool.fetchrow(
        \"SELECT id, user_id, bot_id, topic_id, status, session_fields \"
        \"FROM mediator.conversations WHERE id = $1\",
        conversation_id,
    )
    if row is None:
        raise ValueError(
            f\"retry_live_debrief: conversation_id={conversation_id} not found\"
        )
    if row[\"status\"] != \"debrief_failed\":
        raise ValueError(
            f\"retry_live_debrief: conversation_id={conversation_id} \"
            f\"has status={row['status']!r}, expected 'debrief_failed'\"
        )

    # Count previous retries
    sf = row[\"session_fields\"] or {}
    if isinstance(sf, dict):
        prev_retries = sf.get(\"retry_count\", 0)
    else:
        prev_retries = 0
    retry_number = prev_retries + 1

    logger.info(
        \"live_debrief_retry: starting retry #%d for conversation_id=%s \"
        \"bot_id=%s status_transition=debrief_failed->debriefing\",
        retry_number,
        conversation_id,
        row[\"bot_id\"],
    )

    # Reset to debriefing and increment retry count
    await pool.execute(
        \"UPDATE mediator.conversations SET status = 'debriefing', \"
        \"session_fields = session_fields \"
        \"    || jsonb_build_object('retry_count', $2::int) \"
        \"WHERE id = $1\",
        conversation_id,
        retry_number,
    )

    # Load user record
    user_row = await pool.fetchrow(
        \"SELECT * FROM users WHERE id = $1\", row[\"user_id\"]
    )
    if user_row is None:
        raise ValueError(f\"user_id={row['user_id']} not found in users\")

    from app.services.live.bot_profile import user_from_live_row
    user = user_from_live_row(row[\"user_id\"], user_row)

    result = await run_live_debrief_agentic_job(
        conversation_id=conversation_id,
        user=user,
        pool=pool,
    )

    _retry_elapsed = _time.monotonic() - _retry_start
    if result.success:
        logger.info(
            \"live_debrief_retry: succeeded retry #%d for conversation_id=%s \"
            \"bot_id=%s duration=%.3f tool_count=%d \"
            \"status_transition=debrief_failed->review_pending\",
            retry_number,
            conversation_id,
            row[\"bot_id\"],
            _retry_elapsed,
            result.tool_call_count,
        )
    else:
        _durable_writes = len(
            result.extras.get(\"live_debrief_durable_writes\", [])
            if hasattr(result, \"extras\") and isinstance(result.extras, dict)
            else []
        )
        logger.warning(
            \"live_debrief_retry: failed retry #%d for conversation_id=%s \"
            \"bot_id=%s duration=%.3f tool_count=%d failure_reason=%s \"
            \"failure_class=debrief_failed durable_write_count=%d \"
            \"status_transition=debrief_failed->debrief_failed\",
            retry_number,
            conversation_id,
            row[\"bot_id\"],
            _retry_elapsed,
            result.tool_call_count,
            result.failure_reason,
            _durable_writes,
        )

    return result'''

if old_retry_start in debrief:
    debrief = debrief.replace(old_retry_start, new_retry_start)
    print("✓ Applied debrief retry logs")
else:
    print("✗ Could not find retry section")
    # Try to locate
    idx = debrief.find("async def retry_live_debrief")
    if idx >= 0:
        print("  Found retry_live_debrief at offset", idx)
        print("  Preview:", repr(debrief[idx:idx+200]))

with open("app/services/live/debrief.py", "w") as f:
    f.write(debrief)

print("✓ debrief.py updated successfully")
print("✓ All T4 backend changes applied")
