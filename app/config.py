import os
from pathlib import Path

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
    raise RuntimeError("BOT_TOKEN не найден. Создай .env с BOT_TOKEN=...")

# Bootstrap only: used to seed initial superadmin if none exists.
ALLOWED_USERS = _env_int_list(
    "ALLOWED_USERS",
    default=[5059244843, 651046373, 171624253, 487722436],
)

RUNNING_IN_DOCKER = Path("/.dockerenv").exists()
LOG_FILE = (os.getenv("LOG_FILE") or "bot.log").strip()
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
LOG_ENV = (os.getenv("APP_ENV") or os.getenv("ENV") or "prod").strip()
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
