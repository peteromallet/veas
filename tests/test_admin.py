from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers.admin import router
from tests.conftest import FakePool


def _client(monkeypatch, pool=None) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "dummy-service-role")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai")
    monkeypatch.setenv("GROQ_API_KEY", "dummy-groq")
    monkeypatch.setenv("WHATSAPP_TOKEN", "dummy-whatsapp")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "12345")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "dummy-verify")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "dummy-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-password")
    monkeypatch.setenv("PARTNER_PHONE_A", "15555550100")
    monkeypatch.setenv("PARTNER_PHONE_B", "15555550101")
    get_settings.cache_clear()
    app = FastAPI()
    app.state.pool = pool or FakePool()
    app.include_router(router)
    return TestClient(app)


def test_admin_requires_basic_auth(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get("/admin")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="admin"'


def test_admin_rejects_wrong_credentials(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get("/admin", auth=("admin", "wrong-password"))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="admin"'


def test_admin_pages_and_turn_detail_render_escaped_read_only(monkeypatch) -> None:
    pool = FakePool()
    user_id = uuid4()
    inbound_id = uuid4()
    outbound_id = uuid4()
    turn_id = uuid4()
    pool.users[user_id] = {"id": user_id, "name": "Maya", "phone": "15555550100", "timezone": "UTC", "onboarding_state": "welcomed"}
    pool.messages[inbound_id] = {
        "id": inbound_id,
        "direction": "inbound",
        "sender_id": user_id,
        "recipient_id": None,
        "content": "<script>bad()</script>",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "whatsapp_message_id": "wamid.in",
        "edit_history": [{"content": "old"}],
        "deleted_at": None,
    }
    pool.messages[outbound_id] = {
        "id": outbound_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": user_id,
        "content": "final answer",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": None,
        "whatsapp_message_id": "wamid.out",
        "edit_history": None,
        "deleted_at": None,
    }
    pool.bot_turns[turn_id] = {
        "id": turn_id,
        "triggered_by_message_id": inbound_id,
        "triggering_message_ids": [inbound_id],
        "user_in_context": user_id,
        "system_prompt_version": "v1",
        "model_version": "claude",
        "prompt_snapshot": "prompt",
        "started_at": datetime.now(UTC),
        "completed_at": datetime.now(UTC),
        "failure_reason": None,
        "reasoning": "because",
        "final_output_message_id": outbound_id,
        "tool_call_count": 1,
    }
    pool.tool_calls.append({"turn_id": turn_id, "tool_name": "log_feedback", "arguments": {}, "result": {}, "called_at": datetime.now(UTC), "duration_ms": 1})
    client = _client(monkeypatch, pool)

    for path in ["/admin", "/admin/turns", f"/admin/turns/{turn_id}", "/admin/messages", "/admin/spend", "/admin/audit"]:
        response = client.get(path, auth=("admin", "correct-password"))
        assert response.status_code == 200, path
        assert "<form" not in response.text.lower()

    feedback_response = client.get("/admin/feedback", auth=("admin", "correct-password"))
    assert feedback_response.status_code == 200

    detail = client.get(f"/admin/turns/{turn_id}", auth=("admin", "correct-password"))
    assert "prompt_snapshot" in detail.text
    assert "because" in detail.text
    assert "log_feedback" in detail.text
    assert "&lt;script&gt;bad()&lt;/script&gt;" in client.get("/admin/messages", auth=("admin", "correct-password")).text
    get_settings.cache_clear()


def test_admin_eval_pages_render_escaped_read_only_without_scratch_schema(monkeypatch) -> None:
    pool = FakePool()
    run_id = uuid4()
    result_id = uuid4()
    pool.eval_runs[run_id] = {
        "id": run_id,
        "run_at": datetime.now(UTC),
        "prompt_version": "v1<script>",
        "scenarios_passed": 14,
        "scenarios_failed": 1,
        "total_cost_usd": "1.23",
        "git_sha": "abc123",
        "notes": "<script>run()</script>",
    }
    pool.eval_results[result_id] = {
        "id": result_id,
        "run_id": run_id,
        "scenario_name": "stance <bad>",
        "status": "fail",
        "judge_verdicts": [{"criterion": "<b>no labels</b>", "passes": False, "reason": "<script>judge()</script>"}],
        "tool_calls": [{"tool_name": "log_observation", "arguments": {"content": "<script>tool()</script>"}}],
        "failure_reason": "<script>failed()</script>",
    }
    client = _client(monkeypatch, pool)

    list_response = client.get("/admin/evals", auth=("admin", "correct-password"))
    detail_response = client.get(f"/admin/evals/{run_id}", auth=("admin", "correct-password"))

    assert list_response.status_code == 200
    assert detail_response.status_code == 200
    assert "<form" not in list_response.text.lower()
    assert "<form" not in detail_response.text.lower()
    assert f"/admin/evals/{run_id}" in list_response.text
    assert "&lt;script&gt;run()&lt;/script&gt;" in list_response.text
    assert "stance &lt;bad&gt;" in detail_response.text
    assert "&lt;script&gt;failed()&lt;/script&gt;" in detail_response.text
    assert "&lt;script&gt;judge()&lt;/script&gt;" in detail_response.text
    assert "&lt;script&gt;tool()&lt;/script&gt;" in detail_response.text
    get_settings.cache_clear()


def test_admin_oob_page_redacts_raw_core_and_escapes_safe_metadata(monkeypatch) -> None:
    pool = FakePool()
    owner_id = uuid4()
    safe_oob_id = uuid4()
    protected_oob_id = uuid4()
    raw_core = "raw family secret must not render"
    pool.out_of_bounds[safe_oob_id] = {
        "id": safe_oob_id,
        "owner_id": owner_id,
        "sensitive_core": raw_core,
        "shareable_context": "<b>family boundary & repair</b>",
        "severity": "hard",
        "status": "active",
        "review_at": None,
        "created_at": datetime.now(UTC),
    }
    pool.out_of_bounds[protected_oob_id] = {
        "id": protected_oob_id,
        "owner_id": owner_id,
        "sensitive_core": "another raw core",
        "shareable_context": None,
        "severity": "firm",
        "status": "active",
        "review_at": None,
        "created_at": datetime.now(UTC),
    }
    client = _client(monkeypatch, pool)

    response = client.get("/admin/oob", auth=("admin", "correct-password"))

    assert response.status_code == 200
    assert "OOB Entries" in response.text
    assert "protected_summary" in response.text
    assert "sensitive_core" not in response.text
    assert raw_core not in response.text
    assert "another raw core" not in response.text
    assert "&lt;b&gt;family boundary &amp; repair&lt;/b&gt;" in response.text
    assert "[protected]" in response.text
    assert "hard" in response.text
    assert "firm" in response.text
    assert str(owner_id) in response.text
    get_settings.cache_clear()


def test_admin_feedback_resolve_marks_handled(monkeypatch) -> None:
    pool = FakePool()
    user_id = uuid4()
    target_id = uuid4()
    fb1_id = uuid4()
    fb2_id = uuid4()
    pool.feedback[fb1_id] = {
        "id": fb1_id,
        "from_user_id": user_id,
        "target_type": "message",
        "target_id": target_id,
        "sentiment": "negative",
        "content": "<script>x()</script>",
        "source": "reaction",
        "created_at": datetime.now(UTC),
        "resolution": "open",
        "resolved_at": None,
        "resolution_note": None,
    }
    pool.feedback[fb2_id] = {
        "id": fb2_id,
        "from_user_id": user_id,
        "target_type": "message",
        "target_id": target_id,
        "sentiment": "positive",
        "content": "good",
        "source": "reaction",
        "created_at": datetime.now(UTC),
        "resolution": "open",
        "resolved_at": None,
        "resolution_note": None,
    }
    client = _client(monkeypatch, pool)

    open_response = client.get("/admin/feedback", auth=("admin", "correct-password"))
    assert open_response.status_code == 200
    assert "&lt;script&gt;x()&lt;/script&gt;" in open_response.text
    assert "<form" in open_response.text.lower()
    assert "Mark resolved" in open_response.text

    resolved_empty = client.get("/admin/feedback?resolution=resolved", auth=("admin", "correct-password"))
    assert resolved_empty.status_code == 200
    assert str(fb1_id) not in resolved_empty.text

    redirect = client.post(
        f"/admin/feedback/{fb1_id}/resolve",
        data={"action": "resolve", "note": "fixed", "from_filter": "open"},
        auth=("admin", "correct-password"),
        follow_redirects=False,
    )
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/admin/feedback?resolution=open"

    resolved_response = client.get("/admin/feedback?resolution=resolved", auth=("admin", "correct-password"))
    assert resolved_response.status_code == 200
    assert str(fb1_id) in resolved_response.text
    assert "fixed" in resolved_response.text
    assert "Reopen" in resolved_response.text

    reopen_redirect = client.post(
        f"/admin/feedback/{fb1_id}/resolve",
        data={"action": "reopen", "from_filter": "resolved"},
        auth=("admin", "correct-password"),
        follow_redirects=False,
    )
    assert reopen_redirect.status_code == 303
    assert reopen_redirect.headers["location"] == "/admin/feedback?resolution=resolved"
    assert pool.feedback[fb1_id]["resolution"] == "open"
    assert pool.feedback[fb1_id]["resolved_at"] is None
    assert pool.feedback[fb1_id]["resolution_note"] is None

    bogus = client.post(
        f"/admin/feedback/{fb1_id}/resolve",
        data={"action": "bogus"},
        auth=("admin", "correct-password"),
        follow_redirects=False,
    )
    assert bogus.status_code == 400

    missing = client.post(
        f"/admin/feedback/{uuid4()}/resolve",
        data={"action": "resolve"},
        auth=("admin", "correct-password"),
        follow_redirects=False,
    )
    assert missing.status_code == 404
    get_settings.cache_clear()
