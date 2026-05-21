#!/usr/bin/env python3
"""Re-apply T1 changes to live_voice.py lost by git checkout."""
import sys

with open("app/routers/live_voice.py", "r") as f:
    content = f.read()

changes = 0

# 1. Add status import
old = "from app.services.live.schemas import PrepRequest, TurnRequest"
new = "from app.services.live.schemas import PrepRequest, TurnRequest\nfrom app.services.live.status import canonicalize_status, normalize_row_status"
if old in content and "from app.services.live.status import" not in content:
    content = content.replace(old, new)
    changes += 1
    print("1. Added status import")

# 2. Fix create_session status
if 'status="prepping",' in content:
    content = content.replace('status="prepping",', 'status="preparing",')
    changes += 1
    print("2. Fixed create_session status")

# 3. Add canonicalize_status to /card
old_card = '\n    status = conv["status"]\n\n    # Preparing'
new_card = '\n    status = canonicalize_status(conv["status"])\n\n    # Preparing'
if old_card in content:
    content = content.replace(old_card, new_card)
    changes += 1
    print("3. Added canonicalize_status to /card")

# 4. Add canonicalize_status to /end
old_end = '    new_status = await finalize_session(pool, session_id)\n    review = await synthesize_review(pool, session_id)'
new_end = '    new_status = await finalize_session(pool, session_id)\n    review = await synthesize_review(pool, session_id)\n    # Normalize the status in the review response to canonical form.\n    review["status"] = canonicalize_status(review.get("status", ""))'
if old_end in content:
    # Only add if not already present
    if 'review["status"] = canonicalize_status' not in content:
        content = content.replace(old_end, new_end)
        changes += 1
        print("4. Added canonicalize_status to /end")

# 5. Add canonicalize_status to get_review review synthesis
# Find the get_review function's synthesize_review call
idx = content.find("async def get_review")
if idx >= 0:
    chunk = content[idx:]
    synth_marker = "    review = await synthesize_review(pool, session_id)"
    marker_idx = chunk.find(synth_marker)
    if marker_idx >= 0:
        actual = idx + marker_idx
        suspect = content[actual:actual+len(synth_marker)+20]
        # Check if normalize already present
        if 'review["status"] = canonicalize_status' not in suspect and 'review["status"] = canonicalize_status' not in content[actual-50:actual+100]:
            replacement = '    review = await synthesize_review(pool, session_id)\n    # Normalize the status in the review response to canonical form.\n    review["status"] = canonicalize_status(review.get("status", ""))'
            content = content[:actual] + replacement + content[actual+len(synth_marker):]
            changes += 1
            print("5. Added canonicalize_status to get_review")

# 6. Add canonicalize_status for conv status in get_review
old_review_conv = '        return review\n\n    status = conv["status"]'
new_review_conv = '        return review\n\n    status = canonicalize_status(conv["status"])'
if old_review_conv in content:
    content = content.replace(old_review_conv, new_review_conv)
    changes += 1
    print("6. Added canonicalize_status for conv in /review")

# 7. Fix /review/save status
if '"status": "synthesized"' in content:
    content = content.replace('"status": "synthesized"', '"status": "completed"')
    changes += 1
    print("7. Fixed /review/save status")

# 8. Raw GET normalization
old_raw = '    return dict(row)\n\n\n# \xe2\x94\x80\xe2\x94\x80 WebSocket stub'
new_raw = '    return normalize_row_status(dict(row))\n\n\n# \xe2\x94\x80\xe2\x94\x80 WebSocket stub'
if old_raw in content:
    content = content.replace(old_raw, new_raw)
    changes += 1
    print("8. Added normalize_row_status to raw GET")

# 9. Update ops_metrics active count
old_ops = '''    active_count = await pool.fetchval(
        """SELECT count(*) FROM mediator.conversations
        WHERE status IN ('prepping', 'ready', 'live')"""
    )'''
new_ops = '''    # Active sessions: count both canonical and legacy statuses together.
    active_count = await pool.fetchval(
        """SELECT count(*) FROM mediator.conversations
        WHERE status IN ('preparing', 'ready', 'active', 'review_pending',
                         'prepping', 'live')"""
    )'''
if old_ops in content:
    content = content.replace(old_ops, new_ops)
    changes += 1
    print("9. Updated ops_metrics active count")

# 10. Add grouped_status_metric
if "grouped_status_metric" not in content:
    old_ret = '''    return {
        "latency_ms": latency,'''
    new_ret = '''    # Grouped status counts normalized to canonical values.
    status_rows = await pool.fetch(
        "SELECT status, count(*) AS cnt FROM mediator.conversations GROUP BY status"
    )
    from app.services.live.status import grouped_status_metric
    normalized_statuses = grouped_status_metric(
        [{"status": r["status"]} for r in status_rows]
    )

    return {
        "latency_ms": latency,'''
    if old_ret in content:
        content = content.replace(old_ret, new_ret)
        # Add status_counts to return
        content = content.replace(
            '"error_rate_5m": _event_rate_5m("ws_5xx", "ws_open"),',
            '"status_counts": normalized_statuses,\n        "error_rate_5m": _event_rate_5m("ws_5xx", "ws_open"),'
        )
        changes += 1
        print("10. Added grouped_status_metric + status_counts")

with open("app/routers/live_voice.py", "w") as f:
    f.write(content)

print(f"\nTotal changes applied: {changes}")
