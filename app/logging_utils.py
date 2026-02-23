import hashlib
import json
import logging
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yt_dlp
from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout
from telegram.error import BadRequest, TimedOut

from app.config import (
    LOG_ENV,
    LOG_COLOR_STDOUT,
    LOG_FILE,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_MAX_BYTES,
    LOG_LEVEL,
    LOG_MAX_TEXT_LENGTH,
    LOG_SERVICE,
    LOG_TO_FILE,
    LOG_TO_STDOUT,
    LOG_USER_HASH_SALT,
)
from app.errors import (
    ERR_DOWNLOAD_FAILED,
    ERR_FILE_NOT_FOUND,
    ERR_HTTP_NOT_FOUND,
    ERR_NETWORK,
    ERR_STALE_BUTTON,
    ERR_TELEGRAM_BAD_REQUEST,
    ERR_TELEGRAM_TIMEOUT,
    ERR_TIMEOUT,
    ERR_UNKNOWN,
    ERR_WORKER_CANCELLED,
    ERR_WORKER_STALLED,
    WorkerCancelledError,
)

_URL_FIELD_KEYS = {"url", "source_url", "request_url", "link"}
_USER_ID_FIELD_KEYS = {"user_id", "owner_id", "uid"}
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)\b(token|access[_-]?token|api[_-]?key|signature|sig|auth|password|pass|cookie|session|sid|bot_token|redis_password)\b=([^&\\s]+)"
)
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b")
_REDIS_AUTH_RE = re.compile(r"(redis://:)([^@/]+)(@)")
_HTTP_BASIC_AUTH_RE = re.compile(r"(https?://)([^/@\s:]+):([^@/\s]+)@")
_ANSI_RESET = "\033[0m"
_ANSI_LEVEL_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[34m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[35m",
}


def _log_level(level_name):
    return getattr(logging, str(level_name).upper(), logging.INFO)


class JsonLogFormatter(logging.Formatter):
    def format(self, record):
        payload = record.msg if isinstance(record.msg, dict) else {"message": record.getMessage()}
        if not isinstance(payload, dict):
            payload = {"message": str(payload)}

        base = {
            "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
        }
        base.update(payload)
        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)


class ColoredJsonLogFormatter(JsonLogFormatter):
    def format(self, record):
        rendered = super().format(record)
        color = _ANSI_LEVEL_COLORS.get(record.levelname)
        if not color:
            return rendered
        # Colorize only the "level" value, keep the rest of JSON uncolored.
        target = f"\"level\": \"{record.levelname}\""
        replacement = f"\"level\": \"{color}{record.levelname}{_ANSI_RESET}\""
        return rendered.replace(target, replacement, 1)


def _build_logger():
    app_logger = logging.getLogger("cloudtube_bot")
    app_logger.setLevel(_log_level(LOG_LEVEL))
    app_logger.propagate = False
    app_logger.handlers.clear()

    formatter = JsonLogFormatter()
    color_formatter = ColoredJsonLogFormatter()

    if LOG_TO_STDOUT:
        stdout_handler = logging.StreamHandler(sys.stdout)
        # Keep colors visible in Docker logs when LOG_COLOR_STDOUT=1.
        use_color = bool(LOG_COLOR_STDOUT)
        stdout_handler.setFormatter(color_formatter if use_color else formatter)
        app_logger.addHandler(stdout_handler)

    if LOG_TO_FILE:
        try:
            log_path = Path(LOG_FILE)
            if str(log_path.parent) not in ("", "."):
                log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                str(log_path),
                maxBytes=LOG_FILE_MAX_BYTES,
                backupCount=LOG_FILE_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            app_logger.addHandler(file_handler)
        except Exception as e:
            sys.stderr.write(f"[logging] failed to initialize file logger '{LOG_FILE}': {e}\\n")

    if not app_logger.handlers:
        fallback_handler = logging.StreamHandler(sys.stdout)
        fallback_handler.setFormatter(formatter)
        app_logger.addHandler(fallback_handler)
        sys.stderr.write("[logging] no handlers configured, fallback to stdout\\n")

    return app_logger


logger = _build_logger()


def _truncate_text(text, max_len=LOG_MAX_TEXT_LENGTH):
    if text is None:
        return None
    if len(text) <= max_len:
        return text
    return f"{text[:max_len]}...(truncated)"


def sanitize_text(value):
    if value is None:
        return None
    text = str(value)
    text = _TELEGRAM_TOKEN_RE.sub("[REDACTED_BOT_TOKEN]", text)
    text = _REDIS_AUTH_RE.sub(r"\1[REDACTED]\3", text)
    text = _HTTP_BASIC_AUTH_RE.sub(r"\1[REDACTED]@", text)
    text = _SENSITIVE_QUERY_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    return _truncate_text(text)


def sanitize_url(url):
    if not url:
        return None
    raw = str(url).strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return sanitize_text(raw)
        netloc = parts.hostname or parts.netloc.split("@")[-1]
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        path = parts.path or ""
        if len(path) > 180:
            path = f"{path[:180]}..."
        return urlunsplit((parts.scheme, netloc, path, "", ""))
    except Exception:
        return sanitize_text(raw)


def anonymize_user_id(user_id):
    if user_id is None:
        return None
    digest = hashlib.sha256(f"{LOG_USER_HASH_SALT}:{user_id}".encode("utf-8")).hexdigest()
    return digest[:12]


def _sanitize_log_value(key, value):
    if value is None:
        return None
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            sv = _sanitize_log_value(str(k), v)
            if sv is not None:
                out[str(k)] = sv
        return out
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_log_value(key, item) for item in value]
    if key in _USER_ID_FIELD_KEYS:
        return anonymize_user_id(value)
    if key in _URL_FIELD_KEYS:
        return sanitize_url(value)
    if isinstance(value, (int, float, bool)):
        return value
    return sanitize_text(value)


def log_event(event, level="INFO", **fields):
    payload = {
        "event": sanitize_text(event),
        "service": LOG_SERVICE,
        "env": LOG_ENV,
    }
    for key, value in fields.items():
        sanitized = _sanitize_log_value(key, value)
        if sanitized is not None:
            payload[key] = sanitized
    logger.log(_log_level(level), payload)


def classify_exception_error_code(e, err_text=None):
    text = str(e) if err_text is None else str(err_text)
    low = text.lower()
    if isinstance(e, WorkerCancelledError):
        if e.reason == "stall_watchdog":
            return ERR_WORKER_STALLED
        return ERR_WORKER_CANCELLED
    if isinstance(e, TimedOut):
        return ERR_TELEGRAM_TIMEOUT
    if isinstance(e, (TimeoutError, Timeout)) or "timed out" in low:
        return ERR_TIMEOUT
    if isinstance(e, FileNotFoundError):
        return ERR_FILE_NOT_FOUND
    if isinstance(e, HTTPError) or "404" in low:
        return ERR_HTTP_NOT_FOUND
    if isinstance(e, yt_dlp.utils.DownloadError):
        return ERR_DOWNLOAD_FAILED
    if isinstance(e, (RequestException, ConnectionError)):
        return ERR_NETWORK
    if isinstance(e, BadRequest):
        return ERR_TELEGRAM_BAD_REQUEST
    if "expected string or bytes-like object" in low:
        return ERR_STALE_BUTTON
    return ERR_UNKNOWN


def worker_error(code, message):
    return {"status": "error", "error_code": code, "error": message}
