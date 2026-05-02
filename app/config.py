"""Environment-backed application settings."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env_name: str = "local"
    database_url: str
    database_schema: str = "public"
    supabase_url: str
    supabase_service_role_key: SecretStr
    anthropic_api_key: SecretStr
    openai_api_key: SecretStr
    groq_api_key: SecretStr
    whatsapp_token: SecretStr = SecretStr("")
    whatsapp_bearer_token: SecretStr | None = None
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: SecretStr
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_api_version: str = "v20.0"
    messaging_provider: str = "meta"
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_api_key_sid: str | None = None
    twilio_api_key_secret: SecretStr | None = None
    twilio_whatsapp_from: str | None = None
    twilio_webhook_url: str | None = None
    discord_bot_token: SecretStr | None = None
    admin_password: SecretStr
    partner_phone_a: str = ""
    partner_phone_b: str = ""
    discord_partner_user_id_a: str | None = None
    discord_partner_user_id_b: str | None = None
    discord_partner_name_a: str = "Partner A"
    discord_partner_name_b: str = "Partner B"
    supabase_storage_bucket: str = "mediator-media"
    media_fetch_timeout_s: int = 30
    vision_model: str = "gpt-5.5"
    vision_detail: str = Field(default="high", pattern="^(low|high|auto)$")
    default_user_timezone: str = "UTC"
    text_llm_daily_cap_usd: float = 10.0
    vision_daily_cap_usd: float = 2.0
    transcription_daily_cap_usd: float = 1.0
    conversational_model: str = "claude-sonnet-4-6"  # Conversational loop model.
    oob_checker_model: str = "claude-sonnet-4-6"  # Delivery/read-tool OOB checker model.
    scoring_model: str = "claude-haiku-4-5-20251001"  # Observation scoring and OOB topic clustering model.
    hot_context_token_budget: int = 6000  # Approximate prompt budget for hot context.
    system_prompt_version: str = "v1"  # Version tag stored with each bot turn.
    assistant_name: str = "the assistant"  # Rendered into the main system prompt.
    scheduler_enabled: bool = True
    scheduler_poll_interval_s: float = 10.0
    scheduler_batch_size: int = 10
    discord_pacing_enabled: bool = True
    discord_pacing_burst_window_s: float = Field(default=2.75, ge=0.25, le=15.0)
    discord_pacing_initial_typing_min_s: float = Field(default=0.2, ge=0.0, le=10.0)
    discord_pacing_initial_typing_max_s: float = Field(default=1.2, ge=0.1, le=15.0)
    discord_pacing_min_wait_s: float = Field(default=0.8, ge=0.0, le=10.0)
    discord_pacing_max_wait_s: float = Field(default=12.0, ge=1.0, le=60.0)
    discord_pacing_typing_grace_s: float = Field(default=4.0, ge=0.5, le=30.0)
    discord_pacing_typing_extend_s: float = Field(default=2.0, ge=0.0, le=15.0)
    discord_pacing_max_typing_wait_s: float = Field(default=20.0, ge=1.0, le=90.0)
    discord_pacing_answer_typing_min_s: float = Field(default=0.4, ge=0.0, le=20.0)
    discord_pacing_answer_typing_max_s: float = Field(default=14.0, ge=0.5, le=45.0)
    discord_pacing_answer_chars_per_s: float = Field(default=18.0, ge=4.0, le=80.0)
    discord_pacing_composition_jitter_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    discord_pacing_thinking_typing_start_s: float = Field(default=0.4, ge=0.0, le=10.0)
    discord_pacing_typing_pulse_min_gap_s: float = Field(default=11.0, ge=1.0, le=30.0)
    discord_pacing_incremental_typing_pulse_min_gap_s: float = Field(default=1.0, ge=0.0, le=10.0)
    discord_pacing_typing_visible_s: float = Field(default=8.0, ge=1.0, le=10.0)
    discord_pacing_typing_off_gap_s: float = Field(default=3.0, ge=0.0, le=10.0)
    discord_multi_message_enabled: bool = True
    discord_multi_message_min_chars: int = Field(default=520, ge=120, le=4000)
    discord_multi_message_max_parts: int = Field(default=4, ge=1, le=5)
    discord_multi_message_delay_s: float = Field(default=1.1, ge=0.0, le=10.0)
    discord_pacing_reactions_enabled: bool = True
    discord_pacing_reaction_cooldown_s: float = Field(default=180.0, ge=0.0, le=3600.0)
    discord_pacing_reaction_daily_limit: int = Field(default=12, ge=0, le=100)
    discord_pacing_silence_cooldown_s: float = Field(default=300.0, ge=0.0, le=7200.0)
    discord_pacing_llm_judgement_enabled: bool = True
    discord_pacing_llm_min_ambiguity: float = Field(default=0.45, ge=0.0, le=1.0)
    discord_pacing_event_retention_days: int = Field(default=30, ge=1, le=365)
    weekly_summary_default_day: int = 1  # Monday, matching Postgres EXTRACT(DOW) convention where Sunday is 0.
    weekly_summary_default_time: str = "09:00"
    heartbeat_interval_hours: int = 24
    anthropic_input_usd_per_mtok: float = 3.0  # Cache creation is 1.25x input.
    anthropic_output_usd_per_mtok: float = 15.0  # Cache reads are 0.10x input.
    anthropic_haiku_input_usd_per_mtok: float = 1.0  # Cache creation is 1.25x input.
    anthropic_haiku_output_usd_per_mtok: float = 5.0  # Cache reads are 0.10x input.
    sentry_dsn: str | None = None
    log_destination: str | None = None
    # Base64-encoded 32-byte symmetric key for column-level encryption of
    # sensitive content (out_of_bounds.sensitive_core, messages.content,
    # memories.content, observations.content, bot_turns.reasoning).
    # When unset, the app falls back to plaintext storage and logs a warning.
    data_encryption_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
