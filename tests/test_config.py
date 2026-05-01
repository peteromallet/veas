from pydantic import SecretStr

from app.config import Settings, get_settings


def test_config_loads(monkeypatch) -> None:
    env = {
        "ENV_NAME": "test",
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "dummy-service-role",
        "ANTHROPIC_API_KEY": "dummy-anthropic",
        "OPENAI_API_KEY": "dummy-openai",
        "GROQ_API_KEY": "dummy-groq",
        "WHATSAPP_TOKEN": "dummy-whatsapp",
        "WHATSAPP_PHONE_NUMBER_ID": "12345",
        "WHATSAPP_VERIFY_TOKEN": "dummy-verify",
        "WHATSAPP_APP_SECRET": "dummy-secret",
        "ADMIN_PASSWORD": "dummy-admin",
        "PARTNER_PHONE_A": "15555550100",
        "PARTNER_PHONE_B": "15555550101",
        "TEXT_LLM_DAILY_CAP_USD": "10.5",
        "VISION_DAILY_CAP_USD": "2.5",
        "TRANSCRIPTION_DAILY_CAP_USD": "1.5",
        "CONVERSATIONAL_MODEL": "claude-sonnet-test",
        "OOB_CHECKER_MODEL": "claude-sonnet-oob-test",
        "SCORING_MODEL": "claude-haiku-test",
        "SCHEDULER_ENABLED": "true",
        "SCHEDULER_POLL_INTERVAL_S": "5.5",
        "SCHEDULER_BATCH_SIZE": "7",
        "WEEKLY_SUMMARY_DEFAULT_DAY": "2",
        "WEEKLY_SUMMARY_DEFAULT_TIME": "09:00",
        "HEARTBEAT_INTERVAL_HOURS": "12",
        "ANTHROPIC_INPUT_USD_PER_MTOK": "3.5",
        "ANTHROPIC_OUTPUT_USD_PER_MTOK": "16.5",
        "ANTHROPIC_HAIKU_INPUT_USD_PER_MTOK": "1.25",
        "ANTHROPIC_HAIKU_OUTPUT_USD_PER_MTOK": "5.25",
        "SENTRY_DSN": "https://example.invalid/sentry",
        "LOG_DESTINATION": "stdout",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    settings = Settings()

    assert settings.env_name == "test"
    assert settings.database_url == env["DATABASE_URL"]
    assert settings.supabase_url == env["SUPABASE_URL"]
    assert isinstance(settings.supabase_service_role_key, SecretStr)
    assert settings.supabase_service_role_key.get_secret_value() == env["SUPABASE_SERVICE_ROLE_KEY"]
    assert settings.anthropic_api_key.get_secret_value() == env["ANTHROPIC_API_KEY"]
    assert settings.openai_api_key.get_secret_value() == env["OPENAI_API_KEY"]
    assert settings.groq_api_key.get_secret_value() == env["GROQ_API_KEY"]
    assert settings.whatsapp_token.get_secret_value() == env["WHATSAPP_TOKEN"]
    assert settings.whatsapp_phone_number_id == env["WHATSAPP_PHONE_NUMBER_ID"]
    assert settings.whatsapp_verify_token.get_secret_value() == env["WHATSAPP_VERIFY_TOKEN"]
    assert settings.whatsapp_app_secret.get_secret_value() == env["WHATSAPP_APP_SECRET"]
    assert settings.admin_password.get_secret_value() == env["ADMIN_PASSWORD"]
    assert settings.partner_phone_a == env["PARTNER_PHONE_A"]
    assert settings.partner_phone_b == env["PARTNER_PHONE_B"]
    assert settings.text_llm_daily_cap_usd == 10.5
    assert settings.vision_daily_cap_usd == 2.5
    assert settings.transcription_daily_cap_usd == 1.5
    assert settings.conversational_model == env["CONVERSATIONAL_MODEL"]
    assert settings.oob_checker_model == env["OOB_CHECKER_MODEL"]
    assert settings.scoring_model == env["SCORING_MODEL"]
    assert settings.scheduler_enabled is True
    assert settings.scheduler_poll_interval_s == 5.5
    assert settings.scheduler_batch_size == 7
    assert settings.weekly_summary_default_day == 2
    assert settings.weekly_summary_default_time == "09:00"
    assert settings.heartbeat_interval_hours == 12
    assert settings.anthropic_input_usd_per_mtok == 3.5
    assert settings.anthropic_output_usd_per_mtok == 16.5
    assert settings.anthropic_haiku_input_usd_per_mtok == 1.25
    assert settings.anthropic_haiku_output_usd_per_mtok == 5.25
    assert settings.sentry_dsn == env["SENTRY_DSN"]
    assert settings.log_destination == env["LOG_DESTINATION"]

    get_settings.cache_clear()
