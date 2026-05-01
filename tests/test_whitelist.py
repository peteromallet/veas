import pytest

from app.config import get_settings
from app.services.whitelist import is_allowed_phone


@pytest.fixture(autouse=True)
def settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "dummy-service-role",
        "ANTHROPIC_API_KEY": "dummy-anthropic",
        "OPENAI_API_KEY": "dummy-openai",
        "GROQ_API_KEY": "dummy-groq",
        "MESSAGING_PROVIDER": "meta",
        "WHATSAPP_TOKEN": "dummy-whatsapp",
        "WHATSAPP_PHONE_NUMBER_ID": "12345",
        "WHATSAPP_VERIFY_TOKEN": "dummy-verify",
        "WHATSAPP_APP_SECRET": "dummy-secret",
        "ADMIN_PASSWORD": "dummy-admin",
        "PARTNER_PHONE_A": "+15555550100",
        "PARTNER_PHONE_B": "15555550101",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_partner_numbers_are_allowed() -> None:
    assert is_allowed_phone("+15555550100")
    assert is_allowed_phone("15555550101")


def test_normalization_variants_are_allowed() -> None:
    assert is_allowed_phone("  +15555550100  ")
    assert is_allowed_phone(" 15555550101 ")
    assert is_allowed_phone("15555550100")
    assert is_allowed_phone("+15555550101")


def test_third_number_is_denied() -> None:
    assert not is_allowed_phone("+15555550999")


def test_empty_or_none_is_denied() -> None:
    assert not is_allowed_phone("")
    assert not is_allowed_phone("   ")
    assert not is_allowed_phone(None)


def test_discord_mode_uses_discord_partner_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "123456")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "discord:789012")
    monkeypatch.setenv("PARTNER_PHONE_A", "+15555550100")
    monkeypatch.setenv("PARTNER_PHONE_B", "15555550101")
    get_settings.cache_clear()

    assert is_allowed_phone("123456")
    assert is_allowed_phone("discord:789012")
    assert not is_allowed_phone("15555550100")
    assert not is_allowed_phone("999999")
