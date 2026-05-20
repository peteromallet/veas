from pydantic import SecretStr

from app.config import Settings, get_settings


def test_config_loads(monkeypatch) -> None:
    env = {
        "ENV_NAME": "test",
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "dummy-service-role",
        "ANTHROPIC_API_KEY": "dummy-anthropic",
        "DEEPSEEK_API_KEY": "dummy-deepseek",
        "DEEPSEEK_BASE_URL": "https://deepseek.example",
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
        "DEEPSEEK_CONVERSATIONAL_MODEL": "deepseek-test",
        "DEEPSEEK_THINKING_ENABLED": "false",
        "DEEPSEEK_REASONING_EFFORT": "medium",
        "CONSULT_MODEL": "claude-consult-test",
        "CONSULT_MAX_TOOL_ITERATIONS": "4",
        "CONSULT_TIMEOUT_S": "12.5",
        "OOB_CHECKER_MODEL": "claude-sonnet-oob-test",
        "SCORING_MODEL": "claude-haiku-test",
        "SCHEDULER_ENABLED": "true",
        "SCHEDULER_POLL_INTERVAL_S": "5.5",
        "SCHEDULER_BATCH_SIZE": "7",
        "DISCORD_PACING_ENABLED": "true",
        "DISCORD_PACING_BURST_WINDOW_S": "3.25",
        "DISCORD_PACING_MIN_WAIT_S": "1.25",
        "DISCORD_PACING_MAX_WAIT_S": "14",
        "DISCORD_PACING_TYPING_GRACE_S": "5",
        "DISCORD_PACING_TYPING_EXTEND_S": "2.5",
        "DISCORD_PACING_MAX_TYPING_WAIT_S": "24",
        "DISCORD_PACING_ANSWER_TYPING_MIN_S": "1.5",
        "DISCORD_PACING_ANSWER_TYPING_MAX_S": "9",
        "DISCORD_PACING_ANSWER_CHARS_PER_S": "20",
        "DISCORD_PACING_COMPOSITION_JITTER_RATIO": "0.25",
        "DISCORD_PACING_TYPING_PULSE_MIN_GAP_S": "12",
        "DISCORD_PACING_INCREMENTAL_TYPING_PULSE_MIN_GAP_S": "1.5",
        "DISCORD_PACING_TYPING_VISIBLE_S": "7",
        "DISCORD_PACING_TYPING_OFF_GAP_S": "4",
        "DISCORD_PACING_REACTIONS_ENABLED": "true",
        "DISCORD_PACING_REACTION_COOLDOWN_S": "240",
        "DISCORD_PACING_REACTION_DAILY_LIMIT": "8",
        "DISCORD_PACING_SILENCE_COOLDOWN_S": "420",
        "DISCORD_PACING_LLM_JUDGEMENT_ENABLED": "true",
        "DISCORD_PACING_LLM_MIN_AMBIGUITY": "0.5",
        "DISCORD_PACING_EVENT_RETENTION_DAYS": "45",
        "HEARTBEAT_INTERVAL_HOURS": "12",
        "ANTHROPIC_INPUT_USD_PER_MTOK": "3.5",
        "ANTHROPIC_OUTPUT_USD_PER_MTOK": "16.5",
        "ANTHROPIC_HAIKU_INPUT_USD_PER_MTOK": "1.25",
        "ANTHROPIC_HAIKU_OUTPUT_USD_PER_MTOK": "5.25",
        "DEEPSEEK_INPUT_USD_PER_MTOK": "0.33",
        "DEEPSEEK_OUTPUT_USD_PER_MTOK": "1.25",
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
    assert settings.deepseek_api_key is not None
    assert settings.deepseek_api_key.get_secret_value() == env["DEEPSEEK_API_KEY"]
    assert settings.deepseek_base_url == env["DEEPSEEK_BASE_URL"]
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
    assert settings.deepseek_conversational_model == env["DEEPSEEK_CONVERSATIONAL_MODEL"]
    assert settings.deepseek_thinking_enabled is False
    assert settings.deepseek_reasoning_effort == env["DEEPSEEK_REASONING_EFFORT"]
    assert settings.consult_model == env["CONSULT_MODEL"]
    assert settings.consult_max_tool_iterations == 4
    assert settings.consult_timeout_s == 12.5
    assert settings.oob_checker_model == env["OOB_CHECKER_MODEL"]
    assert settings.scoring_model == env["SCORING_MODEL"]
    assert settings.scheduler_enabled is True
    assert settings.scheduler_poll_interval_s == 5.5
    assert settings.scheduler_batch_size == 7
    assert settings.discord_pacing_enabled is True
    assert settings.discord_pacing_burst_window_s == 3.25
    assert settings.discord_pacing_min_wait_s == 1.25
    assert settings.discord_pacing_max_wait_s == 14
    assert settings.discord_pacing_typing_grace_s == 5
    assert settings.discord_pacing_typing_extend_s == 2.5
    assert settings.discord_pacing_max_typing_wait_s == 24
    assert settings.discord_pacing_answer_typing_min_s == 1.5
    assert settings.discord_pacing_answer_typing_max_s == 9
    assert settings.discord_pacing_answer_chars_per_s == 20
    assert settings.discord_pacing_composition_jitter_ratio == 0.25
    assert settings.discord_pacing_typing_pulse_min_gap_s == 12
    assert settings.discord_pacing_incremental_typing_pulse_min_gap_s == 1.5
    assert settings.discord_pacing_typing_visible_s == 7
    assert settings.discord_pacing_typing_off_gap_s == 4
    assert settings.discord_pacing_reactions_enabled is True
    assert settings.discord_pacing_reaction_cooldown_s == 240
    assert settings.discord_pacing_reaction_daily_limit == 8
    assert settings.discord_pacing_silence_cooldown_s == 420
    assert settings.discord_pacing_llm_judgement_enabled is True
    assert settings.discord_pacing_llm_min_ambiguity == 0.5
    assert settings.discord_pacing_event_retention_days == 45
    assert settings.heartbeat_interval_hours == 12
    assert settings.anthropic_input_usd_per_mtok == 3.5
    assert settings.anthropic_output_usd_per_mtok == 16.5
    assert settings.anthropic_haiku_input_usd_per_mtok == 1.25
    assert settings.anthropic_haiku_output_usd_per_mtok == 5.25
    assert settings.deepseek_input_usd_per_mtok == 0.33
    assert settings.deepseek_output_usd_per_mtok == 1.25
    assert settings.sentry_dsn == env["SENTRY_DSN"]
    assert settings.log_destination == env["LOG_DESTINATION"]

    get_settings.cache_clear()


def test_consult_model_defaults_to_conversational_model(app_env, monkeypatch) -> None:
    monkeypatch.setenv("CONVERSATIONAL_MODEL", "claude-main-test")
    monkeypatch.delenv("CONSULT_MODEL", raising=False)
    get_settings.cache_clear()

    settings = Settings()

    assert settings.consult_model == "claude-main-test"
