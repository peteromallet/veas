"""Read-only operator admin pages."""

from __future__ import annotations

import html
import json
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import get_settings
from app.db import get_pool
from evals.results import list_eval_results, list_eval_runs


router = APIRouter()
security = HTTPBasic(auto_error=False)


def authenticate_admin(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    settings = get_settings()
    expected_password = settings.admin_password.get_secret_value()
    username_ok = bool(credentials) and secrets.compare_digest(credentials.username, "admin")
    password_ok = bool(credentials) and secrets.compare_digest(
        credentials.password,
        expected_password,
    )
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="admin"'},
        )


def _esc(value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, default=str, indent=2)
    return html.escape("" if value is None else str(value))


def _page(title: str, body: str) -> str:
    nav = " ".join(
        f'<a href="/admin/{path}">{label}</a>'
        for path, label in [
            ("turns", "Turns"),
            ("messages", "Messages"),
            ("themes", "Themes"),
            ("memories", "Memories"),
            ("watch-items", "Watch items"),
            ("observations", "Observations"),
            ("oob", "OOB"),
            ("scheduled-jobs", "Jobs"),
            ("spend", "Spend"),
            ("escalations", "Escalations"),
            ("evals", "Evals"),
            ("feedback", "Feedback"),
            ("audit", "Audit"),
        ]
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{_esc(title)}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
<style>body{{max-width:1200px;margin:2rem auto}} pre{{white-space:pre-wrap}} td,th{{vertical-align:top}}</style>
</head><body><h1>{_esc(title)}</h1><nav>{nav}</nav>{body}</body></html>"""


def _table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "<p>No rows.</p>"
    head = "".join(f"<th>{_esc(column)}</th>" for column in columns)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(f"<td>{_esc(row.get(column))}</td>" for column in columns) + "</tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


async def _fetch(pool: Any, sql: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in await pool.fetch(sql, *args)]


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(_: None = Depends(authenticate_admin)) -> str:
    links = "<ul>" + "".join(
        f'<li><a href="/admin/{path}">{_esc(label)}</a></li>'
        for path, label in [
            ("turns", "Recent turns"),
            ("messages", "Recent messages"),
            ("spend", "Spend"),
            ("evals", "Evals"),
            ("audit", "Audit"),
        ]
    ) + "</ul>"
    return _page("Admin", links)


@router.get("/admin/turns", response_class=HTMLResponse)
async def turns(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin), limit: int = 50) -> str:
    rows = await _fetch(
        pool,
        """
        SELECT id, started_at, completed_at, failure_reason, user_in_context, model_version,
               triggered_by_message_id, final_output_message_id, tool_call_count
        FROM bot_turns
        ORDER BY started_at DESC
        LIMIT $1
        """,
        limit,
    )
    for row in rows:
        row["id"] = f'<a href="/admin/turns/{row["id"]}">{row["id"]}</a>'
    return _page("Recent Turns", _table(rows, list(rows[0].keys()) if rows else []))


@router.get("/admin/turns/{turn_id}", response_class=HTMLResponse)
async def turn_detail(turn_id: str, pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin)) -> str:
    rows = await _fetch(
        pool,
        """
        SELECT bt.id AS turn_id, bt.started_at, bt.completed_at, bt.failure_reason, bt.user_in_context,
               bt.model_version, bt.system_prompt_version, bt.prompt_snapshot, COALESCE(bt.reasoning, '') AS reasoning,
               bt.triggered_by_message_id, tm.content AS triggering_content,
               bt.final_output_message_id, om.content AS final_outbound_content,
               COALESCE(jsonb_agg(to_jsonb(tc) ORDER BY tc.called_at) FILTER (WHERE tc.id IS NOT NULL), '[]'::jsonb) AS tool_calls
        FROM bot_turns bt
        LEFT JOIN messages tm ON tm.id = bt.triggered_by_message_id
        LEFT JOIN messages om ON om.id = bt.final_output_message_id
        LEFT JOIN tool_calls tc ON tc.turn_id = bt.id
        WHERE bt.id = $1::uuid
        GROUP BY bt.id, tm.content, om.content
        """,
        turn_id,
    )
    if not rows:
        raise HTTPException(status_code=404)
    row = rows[0]
    body = "".join(f"<h2>{_esc(key)}</h2><pre>{_esc(value)}</pre>" for key, value in row.items())
    return _page("Turn Detail", body)


async def _simple_page(title: str, pool: Any, sql: str, columns: list[str], *args: Any) -> str:
    rows = await _fetch(pool, sql, *args)
    return _page(title, _table(rows, columns))


@router.get("/admin/messages", response_class=HTMLResponse)
async def messages(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin)) -> str:
    return await _simple_page(
        "Recent Messages",
        pool,
        "SELECT id, direction, sender_id, recipient_id, content, charge, processing_state, edit_history, sent_at FROM messages ORDER BY sent_at DESC LIMIT 100",
        ["id", "direction", "sender_id", "recipient_id", "content", "charge", "processing_state", "edit_history", "sent_at"],
    )


@router.get("/admin/themes", response_class=HTMLResponse)
async def themes(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin)) -> str:
    return await _simple_page("Themes", pool, "SELECT id, title, description, status, sentiment, health, last_active_at, last_reinforced_at FROM themes ORDER BY last_active_at DESC LIMIT 100", ["id", "title", "description", "status", "sentiment", "health", "last_active_at", "last_reinforced_at"])


@router.get("/admin/memories", response_class=HTMLResponse)
async def memories(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin), user_id: str | None = None, status_filter: str | None = Query(default=None, alias="status")) -> str:
    rows = await _fetch(pool, "SELECT id, about_user_id, content, status, supersedes_memory_id, created_at, last_referenced_at FROM memories ORDER BY created_at DESC LIMIT 100")
    if user_id:
        rows = [row for row in rows if str(row.get("about_user_id")) == user_id]
    if status_filter:
        rows = [row for row in rows if row.get("status") == status_filter]
    return _page("Memories", _table(rows, ["id", "about_user_id", "content", "status", "supersedes_memory_id", "created_at", "last_referenced_at"]))


@router.get("/admin/watch-items", response_class=HTMLResponse)
async def watch_items(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin), owner: str | None = None, status_filter: str | None = Query(default=None, alias="status")) -> str:
    rows = await _fetch(pool, "SELECT id, owner_user_id, content, due_at, status, addressing_note, addressed_at, created_at FROM watch_items ORDER BY COALESCE(due_at, created_at) DESC LIMIT 100")
    if owner:
        rows = [row for row in rows if str(row.get("owner_user_id")) == owner]
    if status_filter:
        rows = [row for row in rows if row.get("status") == status_filter]
    return _page("Watch Items", _table(rows, ["id", "owner_user_id", "content", "due_at", "status", "addressing_note", "addressed_at", "created_at"]))


@router.get("/admin/observations", response_class=HTMLResponse)
async def observations(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin), min_significance: int = 0) -> str:
    rows = await _fetch(pool, "SELECT id, about_user_id, content, confidence, significance, status, supporting_message_ids, created_at, last_reinforced_at FROM observations ORDER BY created_at DESC LIMIT 100")
    rows = [row for row in rows if (row.get("significance") or 0) >= min_significance]
    return _page("Observations", _table(rows, ["id", "about_user_id", "content", "confidence", "significance", "status", "supporting_message_ids", "created_at", "last_reinforced_at"]))


@router.get("/admin/oob", response_class=HTMLResponse)
async def oob(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin)) -> str:
    rows = await _fetch(
        pool,
        """
        SELECT id, owner_id, shareable_context, severity, status, review_at, created_at
        FROM out_of_bounds
        ORDER BY created_at DESC
        LIMIT 100
        """,
    )
    for row in rows:
        row["protected_summary"] = row.get("shareable_context") or "[protected]"
    return _page(
        "OOB Entries",
        _table(rows, ["id", "owner_id", "protected_summary", "severity", "status", "review_at", "created_at"]),
    )


@router.get("/admin/scheduled-jobs", response_class=HTMLResponse)
async def scheduled_jobs(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin)) -> str:
    return await _simple_page("Scheduled Jobs", pool, "SELECT id, user_id, job_type, scheduled_for, fired_at, status, context FROM scheduled_jobs ORDER BY scheduled_for DESC LIMIT 100", ["id", "user_id", "job_type", "scheduled_for", "fired_at", "status", "context"])


@router.get("/admin/spend", response_class=HTMLResponse)
async def spend(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin)) -> str:
    settings = get_settings()
    caps = {"text": settings.text_llm_daily_cap_usd, "vision": settings.vision_daily_cap_usd, "transcription": settings.transcription_daily_cap_usd}
    rows = await _fetch(pool, "SELECT provider, day, total_usd, warned_80_at, updated_at FROM llm_spend_log ORDER BY day DESC, provider ASC LIMIT 30")
    for row in rows:
        cap = Decimal(str(caps.get(row["provider"], 0)))
        total = Decimal(str(row.get("total_usd") or 0))
        row["cap_usd"] = cap
        row["percent"] = round(float((total / cap) * 100), 1) if cap else None
    return _page("Spend", _table(rows, ["provider", "day", "total_usd", "cap_usd", "percent", "warned_80_at", "updated_at"]))


@router.get("/admin/escalations", response_class=HTMLResponse)
async def escalations(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin)) -> str:
    return await _simple_page("Escalations", pool, "SELECT bt.id, bt.started_at, bt.reasoning, bt.final_output_message_id FROM bot_turns bt WHERE EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.turn_id = bt.id AND tc.tool_name='escalate_to_partner') ORDER BY bt.started_at DESC LIMIT 100", ["id", "started_at", "reasoning", "final_output_message_id"])


@router.get("/admin/evals", response_class=HTMLResponse)
async def evals(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin), limit: int = 25) -> str:
    runs = await list_eval_runs(pool, limit=limit)
    if not runs:
        return _page("Evals", "<p>No rows.</p>")
    rows = []
    for run in runs:
        rows.append(
            "<tr>"
            f'<td><a href="/admin/evals/{_esc(run.id)}">{_esc(run.id)}</a></td>'
            f"<td>{_esc(run.run_at)}</td>"
            f"<td>{_esc(run.prompt_version)}</td>"
            f"<td>{_esc(run.scenarios_passed)}</td>"
            f"<td>{_esc(run.scenarios_failed)}</td>"
            f"<td>{_esc(run.total_cost_usd)}</td>"
            f"<td>{_esc(run.git_sha)}</td>"
            f"<td>{_esc(run.notes)}</td>"
            "</tr>"
        )
    head = "".join(
        f"<th>{_esc(column)}</th>"
        for column in ["id", "run_at", "prompt_version", "passed", "failed", "cost", "git_sha", "notes"]
    )
    return _page("Evals", f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>")


@router.get("/admin/evals/{run_id}", response_class=HTMLResponse)
async def eval_detail(run_id: str, pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin)) -> str:
    try:
        parsed_run_id = UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    results = await list_eval_results(pool, parsed_run_id)
    if not results:
        raise HTTPException(status_code=404)
    sections = []
    for result in results:
        sections.append(
            "<article>"
            f"<h2>{_esc(result.scenario_name)}: {_esc(result.status)}</h2>"
            f"<p><strong>Failure:</strong> {_esc(result.failure_reason)}</p>"
            "<h3>Judge Verdicts</h3>"
            f"<pre>{_esc(result.judge_verdicts)}</pre>"
            "<h3>Tool Calls</h3>"
            f"<pre>{_esc(result.tool_calls)}</pre>"
            "</article>"
        )
    return _page("Eval Detail", "".join(sections))


@router.get("/admin/feedback", response_class=HTMLResponse)
async def feedback(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin)) -> str:
    return await _simple_page("Feedback", pool, "SELECT id, from_user_id, target_type, target_id, sentiment, content, source, created_at FROM feedback ORDER BY created_at DESC LIMIT 100", ["id", "from_user_id", "target_type", "target_id", "sentiment", "content", "source", "created_at"])


@router.get("/admin/audit", response_class=HTMLResponse)
async def audit(pool: Any = Depends(get_pool), _: None = Depends(authenticate_admin), target_type: str | None = None) -> str:
    rows = await _fetch(
        pool,
        """
        SELECT bt.id AS turn_id, bt.started_at, bt.user_in_context, bt.triggered_by_message_id,
               tm.content AS triggering_content, bt.final_output_message_id, om.content AS final_outbound_content,
               COALESCE(bt.reasoning, '') AS reasoning,
               COALESCE(jsonb_agg(to_jsonb(tc) ORDER BY tc.called_at) FILTER (WHERE tc.id IS NOT NULL), '[]'::jsonb) AS tool_calls
        FROM bot_turns bt
        LEFT JOIN messages tm ON tm.id = bt.triggered_by_message_id
        LEFT JOIN messages om ON om.id = bt.final_output_message_id
        LEFT JOIN tool_calls tc ON tc.turn_id = bt.id
        GROUP BY bt.id, tm.content, om.content
        ORDER BY bt.started_at DESC
        LIMIT 100
        """,
    )
    if target_type == "escalation":
        rows = [row for row in rows if any(call.get("tool_name") == "escalate_to_partner" for call in row.get("tool_calls", []))]
    return _page("Audit", _table(rows, ["turn_id", "started_at", "triggering_content", "final_outbound_content", "reasoning", "tool_calls"]))
