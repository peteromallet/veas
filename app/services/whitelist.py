"""Phone-number whitelist checks."""

from app.config import get_settings


def normalize_phone(num: str | None) -> str:
    if num is None:
        return ""
    normalized = num.strip()
    if normalized.startswith("whatsapp:"):
        normalized = normalized.removeprefix("whatsapp:")
    if normalized.startswith("discord:"):
        normalized = normalized.removeprefix("discord:")
    if normalized.startswith("+"):
        normalized = normalized[1:]
    return normalized


def is_allowed_phone(num: str | None) -> bool:
    normalized = normalize_phone(num)
    if not normalized:
        return False

    settings = get_settings()
    if settings.messaging_provider.strip().lower() == "discord":
        allowed = {
            normalize_phone(settings.discord_partner_user_id_a),
            normalize_phone(settings.discord_partner_user_id_b),
        }
        return normalized in allowed

    allowed = {
        normalize_phone(settings.partner_phone_a),
        normalize_phone(settings.partner_phone_b),
    }
    return normalized in allowed
