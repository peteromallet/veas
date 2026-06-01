"""Boot-time guard: production must not run with live-voice auth disabled.

Ownership / operator enforcement on /api/live hinges on
``live_voice_auth_enabled``.  A prod deploy that forgets the flag silently
reverts to no-auth, re-opening the holes this fix closed.  The lifespan must
refuse to start in that configuration.

Covers:
  * ``Settings.is_production`` inference from ``env_name``.
  * ``Settings.live_voice_ops_user_id_set`` parsing.
  * The lifespan raises when prod + flag-off, and boots otherwise.
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

import pytest

from app.config import Settings, get_settings

_BASE_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "dummy",
    "ANTHROPIC_API_KEY": "dummy",
    "OPENAI_API_KEY": "dummy",
    "GROQ_API_KEY": "dummy",
    "WHATSAPP_TOKEN": "dummy",
    "WHATSAPP_PHONE_NUMBER_ID": "12345",
    "WHATSAPP_VERIFY_TOKEN": "dummy",
    "WHATSAPP_APP_SECRET": "dummy",
    "ADMIN_PASSWORD": "dummy",
}


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Clear the cached Settings before and after each test so env changes here
    don't leak into (or inherit from) other tests under random ordering."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_RAILWAY_SIGNAL_VARS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
)


def _prime(monkeypatch, extra: dict[str, str]) -> None:
    # Start from a clean slate: clear any Railway deploy markers and ENV_NAME
    # that might leak in from the host environment so the deploy-signal logic is
    # exercised deterministically.  Cases re-add what they need via ``extra``.
    for var in (*_RAILWAY_SIGNAL_VARS, "ENV_NAME"):
        monkeypatch.delenv(var, raising=False)
    for k, v in {**_BASE_ENV, **extra}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


# ─────────────────────────────────────────────────────────────────────────────
# is_production inference
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "env_name,expected",
    [
        ("local", False),
        ("test", False),
        ("staging", True),  # staging holds real-ish data → requires auth
        ("dev", False),
        ("ci", False),
        ("production", True),
        ("prod", True),
        ("Production", True),
        ("", True),  # unrecognised → assume prod (fail-safe)
        ("railway", True),
    ],
)
def test_is_production_inference(monkeypatch, env_name, expected):
    """env_name-only inference (no Railway deploy signal present)."""
    _prime(monkeypatch, {"ENV_NAME": env_name})
    assert Settings().is_production is expected


# ─────────────────────────────────────────────────────────────────────────────
# Railway deploy-signal inference (fail-OPEN regression coverage)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("signal_var", _RAILWAY_SIGNAL_VARS)
def test_railway_signal_without_env_name_is_production(monkeypatch, signal_var):
    """A real Railway deploy that simply never set ENV_NAME must be treated as
    production.  ENV_NAME is unset ⇒ Settings.env_name defaults to "local", but
    the RAILWAY_* deploy marker overrides that default ⇒ is_production True.

    This is the fail-OPEN the review found: pre-hardening, this resolved to
    is_production=False and the boot guard stayed dormant.
    """
    _prime(monkeypatch, {signal_var: "production"})  # ENV_NAME intentionally unset
    assert "ENV_NAME" not in __import__("os").environ
    assert Settings().is_production is True


def test_railway_signal_with_explicit_local_opts_out(monkeypatch):
    """RAILWAY present but ENV_NAME explicitly "local" ⇒ NOT production.

    The explicit developer opt-out must still win even inside a deployed
    container (e.g. a Railway-hosted dev box).
    """
    _prime(
        monkeypatch,
        {"RAILWAY_ENVIRONMENT": "production", "ENV_NAME": "local"},
    )
    assert Settings().is_production is False


# ─────────────────────────────────────────────────────────────────────────────
# operator allow-list parsing
# ─────────────────────────────────────────────────────────────────────────────
def test_ops_user_id_set_parsing(monkeypatch):
    _prime(
        monkeypatch,
        {
            "ENV_NAME": "test",
            "LIVE_VOICE_OPS_USER_IDS": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA, "
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb,not-a-uuid, ",
        },
    )
    s = Settings()
    assert s.live_voice_ops_user_id_set == frozenset(
        {
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        }
    )


def test_ops_user_id_set_empty(monkeypatch):
    _prime(monkeypatch, {"ENV_NAME": "test", "LIVE_VOICE_OPS_USER_IDS": ""})
    assert Settings().live_voice_ops_user_id_set == frozenset()


# ─────────────────────────────────────────────────────────────────────────────
# lifespan guard
# ─────────────────────────────────────────────────────────────────────────────
@contextlib.asynccontextmanager
async def _noop_db_lifespan(app: Any) -> AsyncIterator[None]:
    yield


class _FakeAppState:
    pool = object()


class _FakeApp:
    def __init__(self) -> None:
        self.state = _FakeAppState()


@pytest.mark.asyncio
async def test_lifespan_refuses_prod_with_auth_off(monkeypatch):
    """env_name=production + LIVE_VOICE_AUTH_ENABLED=false ⇒ RuntimeError."""
    import app.main as main_mod

    _prime(
        monkeypatch,
        {"ENV_NAME": "production", "LIVE_VOICE_AUTH_ENABLED": "false"},
    )
    monkeypatch.setattr(main_mod, "db_lifespan", _noop_db_lifespan)

    with pytest.raises(RuntimeError, match="live_voice_auth_enabled must be True"):
        async with main_mod.lifespan(_FakeApp()):
            pass


@pytest.mark.asyncio
async def test_lifespan_refuses_railway_deploy_missing_env_name(monkeypatch):
    """Fail-OPEN regression: a Railway deploy that forgot ENV_NAME (so it
    defaults to "local") AND left auth off must still refuse to boot.

    Pre-hardening this booted wide-open because env_name="local" ⇒
    is_production=False ⇒ guard dormant.
    """
    import app.main as main_mod

    _prime(
        monkeypatch,
        {
            "RAILWAY_ENVIRONMENT": "production",  # ENV_NAME intentionally unset
            "LIVE_VOICE_AUTH_ENABLED": "false",
        },
    )
    monkeypatch.setattr(main_mod, "db_lifespan", _noop_db_lifespan)

    with pytest.raises(RuntimeError, match="live_voice_auth_enabled must be True"):
        async with main_mod.lifespan(_FakeApp()):
            pass


@pytest.mark.asyncio
async def test_lifespan_refuses_staging_with_auth_off(monkeypatch):
    """Staging holds real-ish data, so the guard must fire there too when auth
    is off."""
    import app.main as main_mod

    _prime(
        monkeypatch,
        {"ENV_NAME": "staging", "LIVE_VOICE_AUTH_ENABLED": "false"},
    )
    monkeypatch.setattr(main_mod, "db_lifespan", _noop_db_lifespan)

    with pytest.raises(RuntimeError, match="live_voice_auth_enabled must be True"):
        async with main_mod.lifespan(_FakeApp()):
            pass


@pytest.mark.asyncio
async def test_lifespan_allows_local_with_auth_off(monkeypatch):
    """Non-prod env with auth off must NOT raise the guard.

    We stub out db_lifespan and the heavy startup helpers so the lifespan body
    runs far enough to prove the guard did not fire (any later failure is not a
    RuntimeError about live_voice_auth_enabled).
    """
    import app.main as main_mod

    _prime(
        monkeypatch,
        {"ENV_NAME": "local", "LIVE_VOICE_AUTH_ENABLED": "false"},
    )
    monkeypatch.setattr(main_mod, "db_lifespan", _noop_db_lifespan)

    guard_msg = "live_voice_auth_enabled must be True"
    try:
        async with main_mod.lifespan(_FakeApp()):
            pass
    except RuntimeError as exc:  # pragma: no cover - defensive
        assert guard_msg not in str(exc), f"guard fired in non-prod: {exc}"
    except Exception:
        # Later startup steps need a real DB / clients; that's fine — the guard
        # (which runs first) did not block us.
        pass
