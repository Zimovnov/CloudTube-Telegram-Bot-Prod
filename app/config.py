import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv


def _env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_float(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _env_int_list(name, default=None):
    raw = os.getenv(name)
    if raw is None:
        return list(default or [])
    out = []
    for part in str(raw).split(","):
        item = part.strip()
        if not item:
            continue
        try:
            out.append(int(item))
        except Exception:
            continue
    return out


def _env_str_list(name, default=None):
    raw = os.getenv(name)
    if raw is None:
        return list(default or [])
    out = []
    for part in str(raw).split(","):
        item = part.strip()
        if not item:
            continue
        out.append(item)
    return out


def _parse_js_runtimes_map(specs):
    out = {}
    for item in specs or []:
        text = str(item).strip()
        if not text:
            continue
        if ":" in text:
            runtime, path = text.split(":", 1)
            runtime = runtime.strip()
            path = path.strip()
            if not runtime:
                continue
            out[runtime] = {"path": path} if path else {}
            continue
        out[text] = {}
    return out


load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is required. Create .env with BOT_TOKEN=...")

# Bootstrap only: used to seed initial superadmin if none exists.
ALLOWED_USERS = _env_int_list("ALLOWED_USERS", default=[])

RUNNING_IN_DOCKER = Path("/.dockerenv").exists()
APP_ENV = (os.getenv("APP_ENV") or os.getenv("ENV") or "prod").strip().lower()
LOG_FILE = (os.getenv("LOG_FILE") or "bot.log").strip()
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
LOG_ENV = APP_ENV
LOG_SERVICE = (os.getenv("LOG_SERVICE_NAME") or "cloudtube_bot").strip()
LOG_USER_HASH_SALT = (os.getenv("LOG_USER_HASH_SALT") or "cloudtube_bot_default_salt").strip()
LOG_MAX_TEXT_LENGTH = max(120, _env_int("LOG_MAX_TEXT_LENGTH", 800))
LOG_HASH_SALT_STRICT = _env_bool("LOG_HASH_SALT_STRICT", default=False)
LOG_TO_STDOUT = _env_bool("LOG_TO_STDOUT", default=True)
LOG_TO_FILE = _env_bool("LOG_TO_FILE", default=not RUNNING_IN_DOCKER)
LOG_COLOR_STDOUT = _env_bool("LOG_COLOR_STDOUT", default=not RUNNING_IN_DOCKER)
LOG_FILE_MAX_BYTES = max(1024 * 1024, _env_int("LOG_FILE_MAX_BYTES", 5 * 1024 * 1024))
LOG_FILE_BACKUP_COUNT = max(1, _env_int("LOG_FILE_BACKUP_COUNT", 3))

MAX_PARALLEL_PER_USER = 1
REDIS_URL = (os.getenv("REDIS_URL") or "").strip()
REDIS_KEY_PREFIX = (os.getenv("REDIS_KEY_PREFIX") or "soundbot").strip()
REDIS_REQUIRED = _env_bool("REDIS_REQUIRED", default=True)
REDIS_SOCKET_TIMEOUT = _env_float("REDIS_SOCKET_TIMEOUT", 2.0)
REDIS_CONNECT_TIMEOUT = _env_float("REDIS_CONNECT_TIMEOUT", 2.0)
REDIS_HEALTH_CHECK_INTERVAL = _env_int("REDIS_HEALTH_CHECK_INTERVAL", 30)
REDIS_MAX_CONNECTIONS = _env_int("REDIS_MAX_CONNECTIONS", 50)
SETTINGS_THROTTLE_MS = max(100, _env_int("SETTINGS_THROTTLE_MS", 500))
REDIS_ERROR_LOG_COOLDOWN_SECONDS = max(5, _env_int("REDIS_ERROR_LOG_COOLDOWN_SECONDS", 30))
YTDLP_SOCKET_TIMEOUT = max(15, _env_int("YTDLP_SOCKET_TIMEOUT", 90))
YTDLP_RETRIES = max(1, _env_int("YTDLP_RETRIES", 6))
YTDLP_FRAGMENT_RETRIES = max(1, _env_int("YTDLP_FRAGMENT_RETRIES", 10))
YTDLP_META_SOCKET_TIMEOUT = max(10, _env_int("YTDLP_META_SOCKET_TIMEOUT", 30))
YTDLP_COOKIES_FILE = (os.getenv("YTDLP_COOKIES_FILE") or "/app/cookies.txt").strip()
YTDLP_JS_RUNTIMES = _env_str_list("YTDLP_JS_RUNTIMES", default=["node"])
YTDLP_JS_RUNTIMES_MAP = _parse_js_runtimes_map(YTDLP_JS_RUNTIMES)
YTDLP_REMOTE_COMPONENTS = _env_str_list("YTDLP_REMOTE_COMPONENTS", default=["ejs:github"])
DOWNLOAD_STALL_TIMEOUT_SECONDS = max(30, _env_int("DOWNLOAD_STALL_TIMEOUT_SECONDS", 120))
DOWNLOAD_STALL_CHECK_INTERVAL_SECONDS = max(3, _env_int("DOWNLOAD_STALL_CHECK_INTERVAL_SECONDS", 6))
EXTERNAL_UPLOAD_TIMEOUT_SECONDS = max(30, _env_int("EXTERNAL_UPLOAD_TIMEOUT_SECONDS", 600))
FFMPEG_REQUIRED_ON_STARTUP = _env_bool("FFMPEG_REQUIRED_ON_STARTUP", default=True)
MIN_SECONDS_BETWEEN_DOWNLOADS = max(1, _env_int("MIN_SECONDS_BETWEEN_DOWNLOADS", 15))

FREE_MONTHLY_LIMIT = max(1, _env_int("FREE_MONTHLY_LIMIT", 42))
FREE_MAX_DURATION_SECONDS = max(60, _env_int("FREE_MAX_DURATION_SECONDS", 3 * 60 * 60))
PREMIUM_MAX_DURATION_SECONDS = max(
    FREE_MAX_DURATION_SECONDS,
    _env_int("PREMIUM_MAX_DURATION_SECONDS", 10 * 60 * 60),
)
PREMIUM_MONTHLY_STARS = max(1, _env_int("PREMIUM_MONTHLY_STARS", 75))
PREMIUM_PERIOD_SECONDS = max(60, _env_int("PREMIUM_PERIOD_SECONDS", 30 * 24 * 60 * 60))
TELEGRAM_STARS_PROVIDER_TOKEN = (os.getenv("TELEGRAM_STARS_PROVIDER_TOKEN") or "").strip()
PAYMENTS_DATABASE_URL = (os.getenv("PAYMENTS_DATABASE_URL") or "").strip()
MIGRATIONS_DATABASE_URL = (os.getenv("MIGRATIONS_DATABASE_URL") or PAYMENTS_DATABASE_URL).strip()
PAYMENTS_DB_REQUIRED = _env_bool("PAYMENTS_DB_REQUIRED", default=True)
PAYMENTS_DB_CONNECT_TIMEOUT = max(1, _env_int("PAYMENTS_DB_CONNECT_TIMEOUT", 5))
PAYMENTS_ALERT_WINDOW_SECONDS = max(60, _env_int("PAYMENTS_ALERT_WINDOW_SECONDS", 900))
PAYMENTS_ALERT_THRESHOLD = max(1, _env_int("PAYMENTS_ALERT_THRESHOLD", 10))
PAYMENTS_STRICT_PROD = _env_bool("PAYMENTS_STRICT_PROD", default=APP_ENV == "prod")
PAYMENTS_ALLOW_INMEMORY_FALLBACK = _env_bool("PAYMENTS_ALLOW_INMEMORY_FALLBACK", default=not PAYMENTS_STRICT_PROD)
PAYMENT_SESSION_WINDOW_SECONDS = max(30, _env_int("PAYMENT_SESSION_WINDOW_SECONDS", 300))
PAYMENT_BUTTON_THROTTLE_SECONDS = max(1, _env_int("PAYMENT_BUTTON_THROTTLE_SECONDS", 2))

YOOKASSA_SHOP_ID = (os.getenv("YOOKASSA_SHOP_ID") or "").strip()
YOOKASSA_SECRET_KEY = (os.getenv("YOOKASSA_SECRET_KEY") or "").strip()
YOOKASSA_RETURN_URL = (os.getenv("YOOKASSA_RETURN_URL") or "").strip()
YOOKASSA_API_BASE = (os.getenv("YOOKASSA_API_BASE") or "https://api.yookassa.ru/v3").strip().rstrip("/")
YOOKASSA_CURRENCY = (os.getenv("YOOKASSA_CURRENCY") or "RUB").strip().upper()
YOOKASSA_PREMIUM_MONTHLY_AMOUNT = max(1, _env_int("YOOKASSA_PREMIUM_MONTHLY_AMOUNT", 299))
YOOKASSA_WEBHOOK_ENABLED = _env_bool("YOOKASSA_WEBHOOK_ENABLED", default=False)
YOOKASSA_WEBHOOK_BIND_HOST = (os.getenv("YOOKASSA_WEBHOOK_BIND_HOST") or "0.0.0.0").strip()
YOOKASSA_WEBHOOK_BIND_PORT = max(1, _env_int("YOOKASSA_WEBHOOK_BIND_PORT", 8080))
YOOKASSA_WEBHOOK_PATH = (os.getenv("YOOKASSA_WEBHOOK_PATH") or "/webhooks/yookassa").strip() or "/webhooks/yookassa"
YOOKASSA_RECONCILE_ENABLED = _env_bool("YOOKASSA_RECONCILE_ENABLED", default=False)
YOOKASSA_RECONCILE_INTERVAL_SEC = max(10, _env_int("YOOKASSA_RECONCILE_INTERVAL_SEC", 60))
YOOKASSA_RECONCILE_BATCH_SIZE = max(1, _env_int("YOOKASSA_RECONCILE_BATCH_SIZE", 50))

RUNNING_JOB_TTL_SECONDS = max(60, _env_int("RUNNING_JOB_TTL_SECONDS", PREMIUM_MAX_DURATION_SECONDS + 3600))
PAYMENT_DEDUP_TTL_SECONDS = max(3600, _env_int("PAYMENT_DEDUP_TTL_SECONDS", 400 * 24 * 60 * 60))
UPDATE_DEDUP_TTL_SECONDS = max(300, _env_int("UPDATE_DEDUP_TTL_SECONDS", 24 * 60 * 60))
USAGE_COUNTER_TTL_SECONDS = max(24 * 60 * 60, _env_int("USAGE_COUNTER_TTL_SECONDS", 400 * 24 * 60 * 60))
JOB_COUNTED_TTL_SECONDS = max(24 * 60 * 60, _env_int("JOB_COUNTED_TTL_SECONDS", 400 * 24 * 60 * 60))

ADMIN_NONCE_TTL_SECONDS = max(30, _env_int("ADMIN_NONCE_TTL_SECONDS", 300))
AUDIT_LOG_MAX_EVENTS = max(100, _env_int("AUDIT_LOG_MAX_EVENTS", 10000))
ADMIN_MASS_CHANGES_ALERT_THRESHOLD = max(3, _env_int("ADMIN_MASS_CHANGES_ALERT_THRESHOLD", 10))
ADMIN_MASS_CHANGES_WINDOW_SECONDS = max(60, _env_int("ADMIN_MASS_CHANGES_WINDOW_SECONDS", 3600))

METADATA_SESSION_TTL_SECONDS = max(300, _env_int("METADATA_SESSION_TTL_SECONDS", 3600))
METADATA_TITLE_MAX_LEN = max(1, _env_int("METADATA_TITLE_MAX_LEN", 128))
METADATA_ARTIST_MAX_LEN = max(1, _env_int("METADATA_ARTIST_MAX_LEN", 128))
METADATA_STORAGE_DIR = (
    os.getenv("METADATA_STORAGE_DIR")
    or os.path.join(os.getenv("TEMP") or "/tmp", "soundbot_metadata")
).strip()

PREMIUM_PROMPT_ON_LIMIT = _env_bool("PREMIUM_PROMPT_ON_LIMIT", default=True)

# Backward compatibility with legacy code path.
MAX_DURATION = FREE_MAX_DURATION_SECONDS

ASK_TRIM, ASK_RANGE, ASK_TYPE, ASK_TRIM_YT, ASK_RANGE_YT = range(5)


def _is_local_host(hostname):
    host = str(hostname or "").strip().lower()
    return host in ("", "localhost", "127.0.0.1", "::1")


def _parse_url_or_none(raw):
    text = str(raw or "").strip()
    if not text or "://" not in text:
        return None
    return urlparse(text)


def _validate_postgres_tls(errors):
    parsed = _parse_url_or_none(PAYMENTS_DATABASE_URL)
    if parsed is None:
        errors.append("PAYMENTS_DATABASE_URL must be a URL in strict mode.")
        return
    if _is_local_host(parsed.hostname):
        return
    sslmode = (parse_qs(parsed.query).get("sslmode") or [""])[0].strip().lower()
    if sslmode not in ("require", "verify-ca", "verify-full"):
        errors.append("PAYMENTS_DATABASE_URL must enforce PostgreSQL SSL via sslmode=require or stronger in strict mode.")


def _validate_redis_tls(errors):
    parsed = _parse_url_or_none(REDIS_URL)
    if parsed is None:
        errors.append("REDIS_URL must be a URL in strict mode.")
        return
    if _is_local_host(parsed.hostname):
        return
    if parsed.scheme.lower() != "rediss":
        errors.append("REDIS_URL must use rediss:// for non-local Redis in strict mode.")


def validate_runtime_configuration():
    errors = []
    if PAYMENTS_STRICT_PROD:
        if not PAYMENTS_DB_REQUIRED:
            errors.append("PAYMENTS_DB_REQUIRED must be enabled in strict mode.")
        if PAYMENTS_ALLOW_INMEMORY_FALLBACK:
            errors.append("PAYMENTS_ALLOW_INMEMORY_FALLBACK must be disabled in strict mode.")
        if not PAYMENTS_DATABASE_URL:
            errors.append("PAYMENTS_DATABASE_URL is required in strict mode.")
        if not MIGRATIONS_DATABASE_URL:
            errors.append("MIGRATIONS_DATABASE_URL is required in strict mode.")
        if not REDIS_REQUIRED:
            errors.append("REDIS_REQUIRED must be enabled in strict mode.")
        if not REDIS_URL:
            errors.append("REDIS_URL is required in strict mode.")
        if PAYMENTS_DATABASE_URL:
            _validate_postgres_tls(errors)
        if REDIS_URL:
            _validate_redis_tls(errors)
        if YOOKASSA_WEBHOOK_ENABLED:
            if not YOOKASSA_WEBHOOK_BIND_HOST:
                errors.append("YOOKASSA_WEBHOOK_BIND_HOST is required when webhook is enabled.")
            if not YOOKASSA_WEBHOOK_PATH.startswith("/"):
                errors.append("YOOKASSA_WEBHOOK_PATH must start with '/'.")
            if YOOKASSA_WEBHOOK_BIND_PORT <= 0:
                errors.append("YOOKASSA_WEBHOOK_BIND_PORT must be positive.")
    if errors:
        raise RuntimeError("Invalid runtime configuration:\n- " + "\n- ".join(errors))
