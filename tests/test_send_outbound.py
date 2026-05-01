from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.user import User
from app.services import hooks, system_state
from app.services.messaging import send_outbound
from app.services.templates import TemplateCall, render_template
from app.services import whatsapp


pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def reset_hooks():
    hooks.check_oob = None
    yield
    hooks.check_oob = None


def _user(fake_pool) -> User:
    row = {"id": uuid4(), "name": "Maya", "phone": "15555550100", "timezone": "UTC"}
    fake_pool.users[row["id"]] = row
    return User(**row)


def _inbound(fake_pool, user: User, sent_at: datetime) -> None:
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "hi",
        "processing_state": "raw",
        "sent_at": sent_at,
        "charge": None,
        "whatsapp_message_id": f"wa-{message_id}",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
    }


async def test_free_form_path_sends_text_and_updates_row(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(minutes=5))
    sent = []

    async def send_text(to, body):
        sent.append((to, body))
        return {"messages": [{"id": "wamid.out"}]}

    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)

    row_id = await send_outbound(fake_pool, user, "hello")

    assert sent == [(user.phone, "hello")]
    assert fake_pool.messages[row_id]["whatsapp_message_id"] == "wamid.out"
    assert fake_pool.messages[row_id]["processing_state"] == "processed"


async def test_template_path_and_param_validation(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(hours=25))
    sent = []

    async def send_template(to, payload):
        sent.append((to, payload))
        return {"messages": [{"id": "wamid.template"}]}

    monkeypatch.setattr("app.services.whatsapp.send_template", send_template)
    await send_outbound(fake_pool, user, "nudge", template_fallback=TemplateCall("checkin_nudge", [user.name]))

    assert sent == [(user.phone, render_template(TemplateCall("checkin_nudge", [user.name])))]
    with pytest.raises(ValueError):
        await send_outbound(fake_pool, user, "bad", template_fallback=TemplateCall("checkin_nudge", []))
    assert len([row for row in fake_pool.messages.values() if row["direction"] == "outbound"]) == 1
    assert sent == [(user.phone, render_template(TemplateCall("checkin_nudge", [user.name])))]


async def test_twilio_send_text_and_template(app_env, monkeypatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-token")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "+14155238886")
    from app.config import get_settings

    get_settings.cache_clear()
    whatsapp._client = None
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"sid": "SMtwilio"}

    class Client:
        async def post(self, path, auth=None, data=None, json=None, headers=None):
            calls.append((path, auth, data))
            return Response()

    async def get_client():
        return Client()

    monkeypatch.setattr(whatsapp, "_get_client", get_client)

    result = await whatsapp.send_text("+15555550100", "hello")
    template_result = await whatsapp.send_template(
        "+15555550100",
        render_template(TemplateCall("checkin_nudge", ["Maya"])),
    )

    assert result == {"messages": [{"id": "SMtwilio"}]}
    assert template_result == {"messages": [{"id": "SMtwilio"}]}
    assert calls[0][0] == "/2010-04-01/Accounts/AC123/Messages.json"
    assert calls[0][1] == ("AC123", "twilio-token")
    assert calls[0][2] == {"From": "whatsapp:+14155238886", "To": "whatsapp:+15555550100", "Body": "hello"}
    assert "been a bit" in calls[1][2]["Body"]
    get_settings.cache_clear()
    whatsapp._client = None


async def test_twilio_api_key_auth_uses_account_sid_for_url(app_env, monkeypatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "account-token")
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SK123")
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "api-secret")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "+14155238886")
    from app.config import get_settings

    get_settings.cache_clear()
    whatsapp._client = None
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"sid": "SMtwilio"}

    class Client:
        async def post(self, path, auth=None, data=None, json=None, headers=None):
            calls.append((path, auth, data))
            return Response()

    async def get_client():
        return Client()

    monkeypatch.setattr(whatsapp, "_get_client", get_client)

    await whatsapp.send_text("+15555550100", "hello")

    assert calls[0][0] == "/2010-04-01/Accounts/AC123/Messages.json"
    assert calls[0][1] == ("SK123", "api-secret")
    get_settings.cache_clear()
    whatsapp._client = None


async def test_discord_provider_sends_without_whatsapp_window(fake_pool, monkeypatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    user = _user(fake_pool)
    sent = []

    async def send_text(to, body):
        sent.append((to, body))
        return {"messages": [{"id": "discord-message"}]}

    monkeypatch.setattr("app.services.discord.send_text", send_text)

    row_id = await send_outbound(fake_pool, user, "hello discord")

    assert sent == [(user.phone, "hello discord")]
    assert fake_pool.messages[row_id]["whatsapp_message_id"] == "discord-message"
    assert fake_pool.messages[row_id]["processing_state"] == "processed"
    get_settings.cache_clear()


async def test_null_window_uses_template_no_none_arithmetic(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    sent = []

    async def send_template(to, payload):
        sent.append((to, payload))
        return {"messages": [{"id": "wamid.template"}]}

    monkeypatch.setattr("app.services.whatsapp.send_template", send_template)
    await send_outbound(
        fake_pool,
        user,
        "pause",
        template_fallback=TemplateCall("pause_confirmation", [user.name, "Sam"]),
    )

    assert sent


async def test_defer_without_template_appends_reasoning(fake_pool) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(hours=25))
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
        "triggering_message_ids": [],
    }

    row_id = await send_outbound(fake_pool, user, "too specific", bot_turn_id=turn_id)

    assert fake_pool.messages[row_id]["processing_state"] == "withheld"
    assert "outside WhatsApp 24h window" in fake_pool.bot_turns[turn_id]["reasoning"]


async def test_retry_success_and_exhaustion(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(minutes=5))
    attempts = 0
    sleeps = []

    async def send_text(to, body):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary")
        return {"messages": [{"id": "wamid.retry"}]}

    async def no_sleep(seconds):
        sleeps.append(seconds)
        return None

    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)
    monkeypatch.setattr("app.services.messaging.asyncio.sleep", no_sleep)
    row_id = await send_outbound(fake_pool, user, "hello")
    assert fake_pool.messages[row_id]["processing_state"] == "processed"
    assert sleeps == [1, 2]

    attempts = 0
    sleeps.clear()

    async def always_fails(to, body):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("down")

    monkeypatch.setattr("app.services.whatsapp.send_text", always_fails)
    row_id = await send_outbound(fake_pool, user, "hello")
    assert attempts == 3
    assert sleeps == [1, 2]
    assert fake_pool.messages[row_id]["processing_state"] == "expired"


async def test_pause_and_oob_hooks(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(minutes=5))
    sent = []

    async def send_text(to, body):
        sent.append(body)
        return {"messages": [{"id": "wamid.oob"}]}

    async def paused(user_id):
        return True

    monkeypatch.setattr("app.services.hooks.paused_for_user", paused)
    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)
    row_id = await send_outbound(fake_pool, user, "hidden")
    assert fake_pool.messages[row_id]["processing_state"] == "withheld"
    assert sent == []

    row_id = await send_outbound(fake_pool, user, "control", ignore_pause=True)
    assert fake_pool.messages[row_id]["processing_state"] == "processed"
    assert sent == ["control"]
    sent.clear()

    async def not_paused(user_id):
        return False

    monkeypatch.setattr("app.services.hooks.paused_for_user", not_paused)

    async def rewrite(content, recipient):
        return {"verdict": "rewrite", "reason": "too specific", "suggested_rewrite": "rewritten"}

    hooks.check_oob = rewrite
    row_id = await send_outbound(fake_pool, user, "rough")
    assert sent == []
    assert fake_pool.messages[row_id]["content"] == "rough"
    assert fake_pool.messages[row_id]["processing_state"] == "withheld"
    review = next(iter(fake_pool.withheld_outbound_reviews.values()))
    assert review["original_content"] == "rough"
    assert review["suggested_rewrite"] == "rewritten"
    assert review["verdict"] == "rewrite"

    async def block(content, recipient):
        return {"verdict": "block", "reason": "blocked", "suggested_rewrite": None}

    hooks.check_oob = block
    row_id = await send_outbound(fake_pool, user, "blocked")
    assert fake_pool.messages[row_id]["processing_state"] == "withheld"


async def test_global_pause_default_withholds_and_ignore_pause_bypasses(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(minutes=5))
    sent = []

    async def send_text(to, body):
        sent.append(body)
        return {"messages": [{"id": f"wamid.{len(sent)}"}]}

    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)
    await system_state.pause(fake_pool, user.id)

    withheld_id = await send_outbound(fake_pool, user, "ordinary")
    sent_id = await send_outbound(fake_pool, user, "control", ignore_pause=True)

    assert fake_pool.messages[withheld_id]["processing_state"] == "withheld"
    assert fake_pool.messages[sent_id]["processing_state"] == "processed"
    assert sent == ["control"]


async def test_send_outbound_passes_protected_owner_ids_and_withholds_current_user_leak(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    current_user_id = uuid4()
    protected_owner_ids = [current_user_id, user.id]
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(minutes=5))
    sent = []
    oob_calls = []

    async def send_text(to, body):
        sent.append(body)
        return {"messages": [{"id": "wamid.should-not-send"}]}

    async def block_current_user_leak(pool, content, recipient_id, protected_owner_ids=None):
        oob_calls.append((pool, content, recipient_id, protected_owner_ids))
        return {
            "verdict": "block",
            "reason": "current-user hard OOB",
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)
    hooks.check_oob = block_current_user_leak

    row_id = await send_outbound(fake_pool, user, "current-user protected detail", protected_owner_ids=protected_owner_ids)

    assert sent == []
    assert oob_calls == [(fake_pool, "current-user protected detail", user.id, protected_owner_ids)]
    assert fake_pool.messages[row_id]["processing_state"] == "withheld"
