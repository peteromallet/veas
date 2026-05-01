import asyncio
import base64
import copy
import hmac
import json
from datetime import UTC, datetime
from hashlib import sha1
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from app.main import app


pytestmark = pytest.mark.anyio

FIXTURE = Path(__file__).parent / "fixtures" / "whatsapp" / "inbound_text.json"


@pytest.fixture(autouse=True)
def fake_whatsapp_send(monkeypatch):
    counter = {"value": 0}

    async def send_text(to, body):
        counter["value"] += 1
        return {"messages": [{"id": f"wamid.welcome.{counter['value']}"}]}

    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)


def _body(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def _signature(body: bytes) -> str:
    return "sha256=" + hmac.new(b"dummy-secret", body, sha256).hexdigest()


def _twilio_signature(url: str, form: dict[str, str]) -> str:
    signed = url + "".join(f"{key}{form[key]}" for key in sorted(form))
    digest = hmac.new(b"dummy-secret", signed.encode(), sha1).digest()
    return base64.b64encode(digest).decode()


async def _wait_for_messages(count: int) -> None:
    for _ in range(20):
        if len(app.state.pool.messages) >= count:
            return
        await asyncio.sleep(0)


async def _wait_for_feedback(count: int) -> None:
    for _ in range(20):
        if len(app.state.pool.feedback) >= count:
            return
        await asyncio.sleep(0)


async def _wait_for_outbound_whatsapp_id() -> None:
    for _ in range(20):
        if any(m["direction"] == "outbound" and m.get("whatsapp_message_id") for m in app.state.pool.messages.values()):
            return
        await asyncio.sleep(0)


async def test_get_verification(async_client) -> None:
    response = await async_client.get(
        "/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "dummy-verify",
            "hub.challenge": "123",
        },
    )
    assert response.status_code == 200
    assert response.text == "123"
    assert response.headers["content-type"].startswith("text/plain")

    response = await async_client.get(
        "/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "bad",
            "hub.challenge": "123",
        },
    )
    assert response.status_code == 403


async def test_post_signature_accepts_signed_and_rejects_tampered(async_client, caplog) -> None:
    payload = json.loads(FIXTURE.read_text())
    body = _body(payload)
    response = await async_client.post(
        "/whatsapp/webhook",
        content=body,
        headers={"x-hub-signature-256": _signature(body)},
    )
    assert response.status_code == 200

    caplog.clear()
    response = await async_client.post(
        "/whatsapp/webhook",
        content=body + b" ",
        headers={"x-hub-signature-256": _signature(body)},
    )
    assert response.status_code == 401
    assert "webhook signature mismatch" in caplog.text


async def test_non_whitelisted_sender_drops_without_row(async_client, caplog) -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = "15555550999"
    body = _body(payload)

    response = await async_client.post(
        "/whatsapp/webhook",
        content=body,
        headers={"x-hub-signature-256": _signature(body)},
    )
    await _wait_for_feedback(1)

    assert response.status_code == 200
    assert app.state.pool.messages == {}
    assert "dropping non-whitelisted sender" in caplog.text


async def test_idempotent_redelivery_writes_one_message(async_client) -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "Hello"
    body = _body(payload)
    headers = {"x-hub-signature-256": _signature(body)}

    assert (await async_client.post("/whatsapp/webhook", content=body, headers=headers)).status_code == 200
    assert (await async_client.post("/whatsapp/webhook", content=body, headers=headers)).status_code == 200
    await _wait_for_messages(1)

    inbound = [m for m in app.state.pool.messages.values() if m["direction"] == "inbound"]
    outbound = [m for m in app.state.pool.messages.values() if m["direction"] == "outbound"]
    assert len(inbound) == 1
    assert outbound == []
    assert next(iter(app.state.pool.users.values()))["onboarding_state"] == "pending"


async def test_signed_text_post_triggers_agentic_turn_with_user(async_client, monkeypatch) -> None:
    calls = []

    async def callback(message_ids, user):
        calls.append((message_ids, user))

    app.state.coalescer.on_burst_complete = callback
    payload = json.loads(FIXTURE.read_text())
    body = _body(payload)

    response = await async_client.post(
        "/whatsapp/webhook",
        content=body,
        headers={"x-hub-signature-256": _signature(body)},
    )
    await _wait_for_messages(1)
    message_id = next(iter(app.state.pool.messages))
    user_id = next(iter(app.state.pool.users))
    await app.state.coalescer._fire(user_id)

    assert response.status_code == 200
    assert calls[0][0] == [message_id]
    assert calls[0][1].id == user_id


async def test_greeting_onboarding_uses_agentic_turn(async_client, monkeypatch) -> None:
    calls = []

    async def callback(message_ids, user):
        calls.append((message_ids, user))

    app.state.coalescer.on_burst_complete = callback
    payload = json.loads(FIXTURE.read_text())
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "Hello"
    body = _body(payload)

    response = await async_client.post(
        "/whatsapp/webhook",
        content=body,
        headers={"x-hub-signature-256": _signature(body)},
    )
    await _wait_for_messages(1)
    user_id = next(iter(app.state.pool.users))
    await app.state.coalescer._fire(user_id)

    assert response.status_code == 200
    inbound = [m for m in app.state.pool.messages.values() if m["direction"] == "inbound"]
    outbound = [m for m in app.state.pool.messages.values() if m["direction"] == "outbound"]
    assert len(inbound) == 1
    assert inbound[0]["processing_state"] == "raw"
    assert outbound == []
    assert calls[0][0] == [inbound[0]["id"]]
    assert calls[0][1].id == user_id


async def test_twilio_webhook_accepts_signed_form(async_client, monkeypatch) -> None:
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "dummy-secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_URL", "http://test/whatsapp/twilio/webhook")
    from app.config import get_settings

    get_settings.cache_clear()
    form = {
        "From": "whatsapp:+15555550100",
        "ProfileName": "Maya",
        "MessageSid": "SMtwilio-inbound",
        "Body": "hello from sandbox",
        "NumMedia": "0",
    }
    response = await async_client.post(
        "/whatsapp/twilio/webhook",
        data=form,
        headers={"x-twilio-signature": _twilio_signature("http://test/whatsapp/twilio/webhook", form)},
    )
    await _wait_for_messages(1)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    inbound = next(m for m in app.state.pool.messages.values() if m["direction"] == "inbound")
    assert inbound["content"] == "hello from sandbox"
    assert inbound["whatsapp_message_id"] == "SMtwilio-inbound"
    get_settings.cache_clear()


async def test_new_whitelisted_user_timezone_and_charge_routine_fallback(async_client) -> None:
    payload = json.loads(FIXTURE.read_text())
    payload = copy.deepcopy(payload)
    body = _body(payload)

    response = await async_client.post(
        "/whatsapp/webhook",
        content=body,
        headers={"x-hub-signature-256": _signature(body)},
    )
    await _wait_for_messages(1)

    user = next(iter(app.state.pool.users.values()))
    message = next(iter(app.state.pool.messages.values()))
    assert response.status_code == 200
    assert user["timezone"] == "UTC"
    assert message["charge"] == "routine"


async def test_reaction_webhook_logs_feedback_without_coalescing(async_client) -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "Hello"
    body = _body(payload)
    response = await async_client.post(
        "/whatsapp/webhook",
        content=body,
        headers={"x-hub-signature-256": _signature(body)},
    )
    await _wait_for_messages(1)
    assert response.status_code == 200
    user = next(iter(app.state.pool.users.values()))
    outbound_id = uuid4()
    app.state.pool.messages[outbound_id] = {
        "id": outbound_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": user["id"],
        "content": "prior outbound",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": None,
        "deleted_at": None,
        "whatsapp_message_id": "wamid.out.manual",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
    }
    app.state.coalescer._bursts.clear()

    reaction_payload = copy.deepcopy(payload)
    reaction_payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "from": user["phone"],
        "id": "wamid.reaction",
        "timestamp": "1700000001",
        "type": "reaction",
        "reaction": {"message_id": "wamid.out.manual", "emoji": "👍"},
    }
    reaction_body = _body(reaction_payload)
    before_tasks = set(app.state.background_tasks)
    response = await async_client.post(
        "/whatsapp/webhook",
        content=reaction_body,
        headers={"x-hub-signature-256": _signature(reaction_body)},
    )
    new_tasks = set(app.state.background_tasks) - before_tasks
    if new_tasks:
        await asyncio.gather(*new_tasks)
    await _wait_for_feedback(1)

    assert response.status_code == 200
    feedback = next(iter(app.state.pool.feedback.values()))
    assert feedback["source"] == "reaction"
    assert feedback["target_type"] == "message"
    assert feedback["target_id"] == outbound_id
    assert feedback["sentiment"] == "positive"
    assert app.state.coalescer._bursts == {}
