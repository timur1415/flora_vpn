import hashlib
import hmac
from urllib.parse import parse_qsl


def validate_telegram_webapp_data(init_data: str, bot_token: str) -> dict | None:
    validated, _ = validate_telegram_webapp_data_verbose(init_data, bot_token)
    return validated


def validate_telegram_webapp_data_verbose(
    init_data: str, bot_token: str
) -> tuple[dict | None, str | None]:
    if not init_data or not bot_token:
        return None, "Missing init_data or BOT_TOKEN"

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None, "Malformed init_data"

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None, "Missing hash in init_data"

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None, "Invalid init_data signature"

    user_payload = parsed.get("user")
    if not user_payload:
        return None, "Missing user payload in init_data"

    # We parse user JSON lazily in route to keep this helper focused on validation.
    return parsed, None
