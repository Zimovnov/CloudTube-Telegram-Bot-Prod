import os
from pathlib import Path
from urllib.parse import urlparse

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
YTDLP_SOCKET_TIMEOUT = max(15, _env_int("YTDLP_SOCKET_TIMEOUT", 30))
YTDLP_RETRIES = max(1, _env_int("YTDLP_RETRIES", 2))
YTDLP_FRAGMENT_RETRIES = max(1, _env_int("YTDLP_FRAGMENT_RETRIES", 3))
YTDLP_META_SOCKET_TIMEOUT = max(10, _env_int("YTDLP_META_SOCKET_TIMEOUT", 15))
YTDLP_FORCE_IPV4 = _env_bool("YTDLP_FORCE_IPV4", default=True)
YTDLP_HTTP_CHUNK_SIZE = max(0, _env_int("YTDLP_HTTP_CHUNK_SIZE", 10 * 1024 * 1024))
YTDLP_COOKIES_FILE = (os.getenv("YTDLP_COOKIES_FILE") or "/app/cookies.txt").strip()
YTDLP_JS_RUNTIMES = _env_str_list("YTDLP_JS_RUNTIMES", default=["node"])
YTDLP_JS_RUNTIMES_MAP = _parse_js_runtimes_map(YTDLP_JS_RUNTIMES)
YTDLP_REMOTE_COMPONENTS = _env_str_list("YTDLP_REMOTE_COMPONENTS", default=["ejs:github"])
DOWNLOAD_STALL_TIMEOUT_SECONDS = max(30, _env_int("DOWNLOAD_STALL_TIMEOUT_SECONDS", 120))
DOWNLOAD_STALL_CHECK_INTERVAL_SECONDS = max(3, _env_int("DOWNLOAD_STALL_CHECK_INTERVAL_SECONDS", 6))
EXTERNAL_UPLOAD_TIMEOUT_SECONDS = max(30, _env_int("EXTERNAL_UPLOAD_TIMEOUT_SECONDS", 180))
FFMPEG_REQUIRED_ON_STARTUP = _env_bool("FFMPEG_REQUIRED_ON_STARTUP", default=True)
MIN_SECONDS_BETWEEN_DOWNLOADS = max(1, _env_int("MIN_SECONDS_BETWEEN_DOWNLOADS", 15))
TELEGRAM_CONNECT_TIMEOUT = max(5.0, _env_float("TELEGRAM_CONNECT_TIMEOUT", 20.0))
TELEGRAM_READ_TIMEOUT = max(5.0, _env_float("TELEGRAM_READ_TIMEOUT", 60.0))
TELEGRAM_WRITE_TIMEOUT = max(5.0, _env_float("TELEGRAM_WRITE_TIMEOUT", 60.0))
TELEGRAM_POOL_TIMEOUT = max(1.0, _env_float("TELEGRAM_POOL_TIMEOUT", 30.0))
TELEGRAM_MEDIA_WRITE_TIMEOUT = max(10.0, _env_float("TELEGRAM_MEDIA_WRITE_TIMEOUT", 120.0))
TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT = max(
    5.0, _env_float("TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT", TELEGRAM_CONNECT_TIMEOUT)
)
TELEGRAM_GET_UPDATES_READ_TIMEOUT = max(
    5.0, _env_float("TELEGRAM_GET_UPDATES_READ_TIMEOUT", TELEGRAM_READ_TIMEOUT)
)
TELEGRAM_GET_UPDATES_WRITE_TIMEOUT = max(
    5.0, _env_float("TELEGRAM_GET_UPDATES_WRITE_TIMEOUT", TELEGRAM_WRITE_TIMEOUT)
)
TELEGRAM_GET_UPDATES_POOL_TIMEOUT = max(
    1.0, _env_float("TELEGRAM_GET_UPDATES_POOL_TIMEOUT", TELEGRAM_POOL_TIMEOUT)
)

MAX_MEDIA_DURATION_SECONDS = max(60, _env_int("MAX_MEDIA_DURATION_SECONDS", 3 * 60 * 60))

# Legacy payment/plan constants are kept so old modules can still be imported for
# historical data inspection, but active bot runtime no longer uses them.
FREE_MONTHLY_LIMIT = max(1, _env_int("FREE_MONTHLY_LIMIT", 42))
FREE_MAX_DURATION_SECONDS = MAX_MEDIA_DURATION_SECONDS
PREMIUM_MAX_DURATION_SECONDS = MAX_MEDIA_DURATION_SECONDS
PREMIUM_MONTHLY_STARS = max(0, _env_int("PREMIUM_MONTHLY_STARS", 0))
PREMIUM_PERIOD_SECONDS = max(60, _env_int("PREMIUM_PERIOD_SECONDS", 30 * 24 * 60 * 60))
TELEGRAM_STARS_PROVIDER_TOKEN = (os.getenv("TELEGRAM_STARS_PROVIDER_TOKEN") or "").strip()
PAYMENTS_DATABASE_URL = (os.getenv("PAYMENTS_DATABASE_URL") or "").strip()
MIGRATIONS_DATABASE_URL = (os.getenv("MIGRATIONS_DATABASE_URL") or PAYMENTS_DATABASE_URL).strip()
PAYMENTS_DB_REQUIRED = _env_bool("PAYMENTS_DB_REQUIRED", default=False)
PAYMENTS_DB_CONNECT_TIMEOUT = max(1, _env_int("PAYMENTS_DB_CONNECT_TIMEOUT", 5))
PAYMENTS_ALERT_WINDOW_SECONDS = max(60, _env_int("PAYMENTS_ALERT_WINDOW_SECONDS", 900))
PAYMENTS_ALERT_THRESHOLD = max(1, _env_int("PAYMENTS_ALERT_THRESHOLD", 10))
PAYMENTS_STRICT_PROD = _env_bool("PAYMENTS_STRICT_PROD", default=False)
PAYMENTS_ALLOW_INMEMORY_FALLBACK = _env_bool("PAYMENTS_ALLOW_INMEMORY_FALLBACK", default=not PAYMENTS_STRICT_PROD)
PAYMENT_SESSION_WINDOW_SECONDS = max(30, _env_int("PAYMENT_SESSION_WINDOW_SECONDS", 300))
PAYMENT_BUTTON_THROTTLE_SECONDS = max(1, _env_int("PAYMENT_BUTTON_THROTTLE_SECONDS", 2))

ROBOKASSA_MERCHANT_LOGIN = (os.getenv("ROBOKASSA_MERCHANT_LOGIN") or "").strip()
ROBOKASSA_PASSWORD1 = (os.getenv("ROBOKASSA_PASSWORD1") or "").strip()
ROBOKASSA_PASSWORD2 = (os.getenv("ROBOKASSA_PASSWORD2") or "").strip()
ROBOKASSA_PAYMENT_URL = (
    os.getenv("ROBOKASSA_PAYMENT_URL") or "https://auth.robokassa.ru/Merchant/Index.aspx"
).strip()
ROBOKASSA_HASH_ALGORITHM = (os.getenv("ROBOKASSA_HASH_ALGORITHM") or "MD5").strip().upper()
ROBOKASSA_IS_TEST = _env_bool("ROBOKASSA_IS_TEST", default=False)
ROBOKASSA_CURRENCY = (os.getenv("ROBOKASSA_CURRENCY") or "RUB").strip().upper()
ROBOKASSA_PREMIUM_MONTHLY_AMOUNT = max(1, _env_int("ROBOKASSA_PREMIUM_MONTHLY_AMOUNT", 299))
ROBOKASSA_WEBHOOK_ENABLED = _env_bool("ROBOKASSA_WEBHOOK_ENABLED", default=False)
ROBOKASSA_WEBHOOK_BIND_HOST = (os.getenv("ROBOKASSA_WEBHOOK_BIND_HOST") or "0.0.0.0").strip()
ROBOKASSA_WEBHOOK_BIND_PORT = max(1, _env_int("ROBOKASSA_WEBHOOK_BIND_PORT", 8080))
ROBOKASSA_WEBHOOK_PATH = (os.getenv("ROBOKASSA_WEBHOOK_PATH") or "/webhooks/robokassa").strip() or "/webhooks/robokassa"

RUNNING_JOB_TTL_SECONDS = max(60, _env_int("RUNNING_JOB_TTL_SECONDS", MAX_MEDIA_DURATION_SECONDS + 3600))
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

PUBLIC_PRIVACY_URL = (os.getenv("PUBLIC_PRIVACY_URL") or "").strip()
PUBLIC_OFFER_URL = (os.getenv("PUBLIC_OFFER_URL") or "").strip()
PUBLIC_PD_CONSENT_URL = (os.getenv("PUBLIC_PD_CONSENT_URL") or "").strip()
LEGAL_ACCEPTANCE_TTL_SECONDS = max(300, _env_int("LEGAL_ACCEPTANCE_TTL_SECONDS", 24 * 60 * 60))

# Backward compatibility with legacy code path.
MAX_DURATION = MAX_MEDIA_DURATION_SECONDS

ASK_TRIM, ASK_RANGE, ASK_TYPE, ASK_TRIM_YT, ASK_RANGE_YT = range(5)


def _is_local_host(hostname):
    host = str(hostname or "").strip().lower()
    return host in ("", "localhost", "127.0.0.1", "::1")


def _parse_url_or_none(raw):
    text = str(raw or "").strip()
    if not text or "://" not in text:
        return None
    return urlparse(text)


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
    if APP_ENV == "prod":
        if not REDIS_REQUIRED:
            errors.append("REDIS_REQUIRED must be enabled in strict mode.")
        if not REDIS_URL:
            errors.append("REDIS_URL is required in strict mode.")
        if REDIS_URL:
            _validate_redis_tls(errors)
    if errors:
        raise RuntimeError("Invalid runtime configuration:\n- " + "\n- ".join(errors))
