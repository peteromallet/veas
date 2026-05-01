"""Environment-backed application settings."""

from functools import lru_cache

from pydantic import SecretStr
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
