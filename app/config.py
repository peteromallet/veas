"""Environment-backed application settings."""

import os
import re
from functools import cached_property, lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_SEED_BOT = "mediator"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env_name: str = "local"
    database_url: str
    database_schema: str = "public"
    supabase_url: str
    supabase_service_role_key: SecretStr
    anthropic_api_key: SecretStr
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
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
    deepseek_conversational_model: str = "deepseek-v4-pro"
    deepseek_thinking_enabled: bool = False
    deepseek_reasoning_effort: str | None = None
    live_voice_stt_provider: str = ""
    live_voice_turn_provider: str = "deepseek"
    live_voice_prep_provider: str = "agentic"
    live_voice_test_user_id: str = "00000000-0000-0000-0000-000000000001"
    live_voice_whisper_model: str = ""
    live_voice_whisper_language: str = "en"
    live_voice_auth_enabled: bool = False
    consult_model: str = ""  # Bounded read-only consult loop model; defaults to conversational_model.
    consult_max_tool_iterations: int = Field(default=3, ge=0, le=10)
    nonchat_default_max_tool_iterations: int = Field(default=100, ge=0, le=2000)
    live_debrief_max_tool_iterations: int = Field(default=500, ge=0, le=5000)
    # ── Live debrief agentic settings ────────────────────────────────────
    live_debrief_agentic_enabled: bool = False
    live_debrief_tool_call_cap: int = Field(default=500, ge=1, le=5000)
    # ── Live prep agentic settings ──────────────────────────────────────
    live_prep_tool_cap: int = Field(default=100, ge=1, le=500)
    live_prep_timeout_s: float = Field(default=90.0, ge=5.0, le=600.0)
    live_prep_allow_consult: bool = Field(default=False)
    live_prep_orphan_timeout_minutes: int = Field(default=10, ge=1, le=60)
    consult_timeout_s: float = Field(default=20.0, ge=1.0, le=120.0)
    oob_checker_model: str = "claude-sonnet-4-6"  # Delivery/read-tool OOB checker model.
    scoring_model: str = "claude-haiku-4-5-20251001"  # Observation scoring and OOB topic clustering model.
    hot_context_token_budget: int = 6000  # Approximate prompt budget for hot context.
    default_seed_bot_id: str = _DEFAULT_SEED_BOT  # Seed-only bot id for mediator-owned scheduled jobs.
    system_prompt_version: str = "v3"  # Version tag stored with each bot turn.
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
    # Inbound queue: max age for unprocessed inbound messages before expiry.
    # Messages older than this window are marked expired instead of retried.
    inbound_queue_retention_days: int = Field(default=7, ge=1, le=365)
    # Inbound queue: maximum retry attempts for failed messages before they
    # are marked terminal (no further automatic retry).
    inbound_queue_max_retry_attempts: int = Field(default=3, ge=0, le=50)
    # Recovery-v2 retry backoff base (seconds): scheduled retry delay for a
    # ``retryable_pre_send`` failure on its first attempt; subsequent attempts
    # double via SET-time CASE in inbound_queue.fail_messages, capped below.
    recovery_v2_retry_base_seconds: int = 15
    # Recovery-v2 retry backoff cap (seconds): maximum scheduled retry delay
    # regardless of attempt count.
    recovery_v2_retry_cap_seconds: int = 600
    # ── Project A2 provider robustness ─────────────────────────────────
    # Maximum Retry-After value (seconds) the provider chain will honour
    # in-band before advancing to the next provider hop.  Values above this
    # cap are treated as "skip wait, advance to fallback".
    provider_retry_after_cap_seconds: int = 30
    # Sliding window (seconds) for the per-bot fallback-rate circuit breaker.
    provider_fallback_breaker_window_seconds: int = 300
    # Fallback rate (fell_back / samples) that trips the breaker open.
    provider_fallback_breaker_threshold: float = 0.5
    # Minimum sample count before the breaker is allowed to trip open.
    provider_fallback_breaker_min_samples: int = 10
    # Per-call provider timeout (seconds).  Applied to the DeepSeek HTTPX
    # client and used as a soft assumption for Anthropic.  Kept at the same
    # default as the previous hard-coded DeepSeek timeout (120s) so existing
    # latency profiles are unchanged.
    provider_call_timeout_seconds: int = 120
    # ── Project C feature flags (off by default) ───────────────────────
    # When True, routes provider message conversions through the canonical
    # IR in app/llm/internal_message.py instead of A2's sanitize-on-boundary
    # path (``_anthropic_safe_messages``).  Kept OFF until a third provider
    # makes the abstraction load-bearing.  See Project C, C1.
    provider_use_canonical_ir: bool = False
    # When True, claim/complete/fail mutator helpers in
    # app/services/inbound_queue.py also write rows to the
    # ``mediator.inbound_handling_attempts`` ledger (Project C, C2).  Read
    # paths (recovery / retry sweepers) are unchanged and continue to
    # consult ``messages.next_retry_at`` + ``messages.failure_class``.
    # Flip OFF to revert to messages-only writes without a redeploy.
    ledger_dual_write_enabled: bool = False
    heartbeat_interval_hours: int = 24
    anthropic_input_usd_per_mtok: float = 3.0  # Cache creation is 1.25x input.
    anthropic_output_usd_per_mtok: float = 15.0  # Cache reads are 0.10x input.
    anthropic_haiku_input_usd_per_mtok: float = 1.0  # Cache creation is 1.25x input.
    anthropic_haiku_output_usd_per_mtok: float = 5.0  # Cache reads are 0.10x input.
    deepseek_input_usd_per_mtok: float = 0.27
    deepseek_output_usd_per_mtok: float = 1.10
    sentry_dsn: str | None = None
    log_destination: str | None = None
    # Base64-encoded 32-byte symmetric key for column-level encryption of
    # sensitive content (out_of_bounds.sensitive_core, messages.content,
    # memories.content, observations.content, bot_turns.reasoning).
    # When unset, the app falls back to plaintext storage and logs a warning.
    data_encryption_key: SecretStr | None = None

    # ── Per-bot Discord token discovery ──────────────────────────────────────
    # Pattern: DISCORD_BOT_TOKEN_<BOT_ID_UPPER> → lowercased bot_id → SecretStr
    #   e.g. DISCORD_BOT_TOKEN_MEDIATOR → "mediator", DISCORD_BOT_TOKEN_TANTE_ROSI → "tante_rosi"
    _DISCORD_PER_BOT_TOKEN_RE: re.Pattern = re.compile(
        r"^DISCORD_BOT_TOKEN_([A-Z0-9_]+)$"
    )
    _DISCORD_PER_BOT_USER_ID_RE: re.Pattern = re.compile(
        r"^DISCORD_BOT_USER_ID_([A-Z0-9_]+)$"
    )

    @cached_property
    def discord_bot_tokens(self) -> dict[str, SecretStr]:
        """Per-bot Discord tokens keyed by lowercased bot_id.

        Scans environ for DISCORD_BOT_TOKEN_<BOT_ID_UPPER>.  The bot_id is the
        lowercased suffix.  Non-alphanumeric characters in the env-var suffix
        are preserved (the convention is uppercase letters, digits, and _ only).

        Callers must still handle the legacy DISCORD_BOT_TOKEN field for
        backward compatibility (see §6 of the multi-gateway brief).
        """
        result: dict[str, SecretStr] = {}
        for key, value in os.environ.items():
            m = self._DISCORD_PER_BOT_TOKEN_RE.match(key)
            if m and value:
                bot_id = m.group(1).lower()
                result[bot_id] = SecretStr(value)
        return result

    @cached_property
    def discord_bot_user_id_overrides(self) -> dict[str, str]:
        """Per-bot Discord bot user-id overrides keyed by lowercased bot_id.

        Scans environ for DISCORD_BOT_USER_ID_<BOT_ID_UPPER>.  Values must be
        digit-only strings (the canonical Discord user id format).

        When set, these take precedence over token-decoded user ids in
        discord_bot_user_id(bot_id).
        """
        _DIGIT_ONLY: re.Pattern = re.compile(r"^\d+$")
        result: dict[str, str] = {}
        for key, value in os.environ.items():
            m = self._DISCORD_PER_BOT_USER_ID_RE.match(key)
            if m and value and _DIGIT_ONLY.match(value):
                bot_id = m.group(1).lower()
                result[bot_id] = value
        return result

    @model_validator(mode="after")
    def default_consult_model(self) -> "Settings":
        if not self.consult_model:
            self.consult_model = self.conversational_model
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
