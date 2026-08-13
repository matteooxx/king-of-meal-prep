"""Read-only config loaded from app.env at process start.

Mutable runtime knobs go through `settings.get(key)` instead — that reads
from the in-process overlay which is fed by both app.env (for secrets like
GEMINI_API_KEY) and the settings_kv DB table (for the long-tail of
user-tunable knobs from the design doc).
"""
import os


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _bool(key: str, default: bool = False) -> bool:
    value = os.environ.get(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Auth
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
SESSION_INACTIVITY_HOURS = _int("SESSION_INACTIVITY_HOURS", 24)
FORCE_HTTPS = _bool("FORCE_HTTPS", False)
TRUSTED_HOSTS = tuple(
    item.strip()
    for item in os.environ.get(
        "TRUSTED_HOSTS",
        "localhost,127.0.0.1",
    ).split(",")
    if item.strip()
)

# Email (digest + reset)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", SMTP_FROM)

# LLM
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Storage
APP_ENV_PATH = os.environ.get(
    "APP_ENV_PATH", "./runtime/app.env"
)
DB_PATH = os.environ.get(
    "DB_PATH", "./runtime/data.db"
)
NUTRITION_DB = os.environ.get(
    "NUTRITION_DB", "./datasets/nutrition.db"
)
ENV_MARKER_PATH = os.environ.get(
    "ENV_MARKER_PATH",
    os.path.join(os.path.dirname(APP_ENV_PATH), ".env-changed"),
)
