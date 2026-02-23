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


load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Создай .env с BOT_TOKEN=...")

ALLOWED_USERS = [5059244843, 651046373, 171624253, 487722436]

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

MAX_DURATION = 3 * 60 * 60
MAX_PARALLEL_PER_USER = 1
REDIS_URL = (os.getenv("REDIS_URL") or "").strip()
REDIS_KEY_PREFIX = (os.getenv("REDIS_KEY_PREFIX") or "soundbot").strip()
REDIS_REQUIRED = _env_bool("REDIS_REQUIRED", default=True)
REDIS_SOCKET_TIMEOUT = _env_float("REDIS_SOCKET_TIMEOUT", 2.0)
REDIS_CONNECT_TIMEOUT = _env_float("REDIS_CONNECT_TIMEOUT", 2.0)
REDIS_HEALTH_CHECK_INTERVAL = _env_int("REDIS_HEALTH_CHECK_INTERVAL", 30)
REDIS_MAX_CONNECTIONS = _env_int("REDIS_MAX_CONNECTIONS", 50)
RUNNING_JOB_TTL_SECONDS = max(60, _env_int("RUNNING_JOB_TTL_SECONDS", MAX_DURATION + 3600))
SETTINGS_THROTTLE_MS = max(100, _env_int("SETTINGS_THROTTLE_MS", 500))
REDIS_ERROR_LOG_COOLDOWN_SECONDS = max(5, _env_int("REDIS_ERROR_LOG_COOLDOWN_SECONDS", 30))
YTDLP_SOCKET_TIMEOUT = max(15, _env_int("YTDLP_SOCKET_TIMEOUT", 90))
YTDLP_RETRIES = max(1, _env_int("YTDLP_RETRIES", 6))
YTDLP_FRAGMENT_RETRIES = max(1, _env_int("YTDLP_FRAGMENT_RETRIES", 10))
YTDLP_META_SOCKET_TIMEOUT = max(10, _env_int("YTDLP_META_SOCKET_TIMEOUT", 30))
DOWNLOAD_STALL_TIMEOUT_SECONDS = max(30, _env_int("DOWNLOAD_STALL_TIMEOUT_SECONDS", 120))
DOWNLOAD_STALL_CHECK_INTERVAL_SECONDS = max(3, _env_int("DOWNLOAD_STALL_CHECK_INTERVAL_SECONDS", 6))
EXTERNAL_UPLOAD_TIMEOUT_SECONDS = max(30, _env_int("EXTERNAL_UPLOAD_TIMEOUT_SECONDS", 600))
FFMPEG_REQUIRED_ON_STARTUP = _env_bool("FFMPEG_REQUIRED_ON_STARTUP", default=True)
MIN_SECONDS_BETWEEN_DOWNLOADS = 15

ASK_TRIM, ASK_RANGE, ASK_TYPE, ASK_TRIM_YT, ASK_RANGE_YT = range(5)
