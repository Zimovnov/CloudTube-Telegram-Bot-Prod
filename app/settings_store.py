import asyncio
import json

from app import state
from app.jobs import RedisError, _get_redis_client, _log_redis_issue, _redis_key
from app.logging_utils import log_event


def normalize_settings(s):
    default = {
        "format": {"soundcloud": "audio", "youtube": "ask"},
        "quality": {"soundcloud": "best", "youtube": "best"},
        "trim": {"soundcloud": "ask", "youtube": "ask"},
        "logs": True,
        "language": "ru",
        "metadata_prompt_enabled": True,
    }
    if not isinstance(s, dict):
        return default
    out = {}

    fmt = s.get("format", default["format"])
    if isinstance(fmt, str):
        out["format"] = {"soundcloud": "audio", "youtube": fmt}
    elif isinstance(fmt, dict):
        out["format"] = {
            "soundcloud": "audio",
            "youtube": fmt.get("youtube", fmt.get("yt", default["format"]["youtube"])),
        }
    else:
        out["format"] = default["format"]

    q = s.get("quality", default["quality"])
    if isinstance(q, str):
        out["quality"] = {"soundcloud": q, "youtube": q}
    elif isinstance(q, dict):
        out["quality"] = {
            "soundcloud": q.get("soundcloud", q.get("sc", default["quality"]["soundcloud"])),
            "youtube": q.get("youtube", q.get("yt", default["quality"]["youtube"])),
        }
    else:
        out["quality"] = default["quality"]

    tr = s.get("trim", default["trim"])
    if isinstance(tr, str):
        out["trim"] = {"soundcloud": tr, "youtube": tr}
    elif isinstance(tr, dict):
        out["trim"] = {
            "soundcloud": tr.get("soundcloud", tr.get("sc", default["trim"]["soundcloud"])),
            "youtube": tr.get("youtube", tr.get("yt", default["trim"]["youtube"])),
        }
    else:
        out["trim"] = default["trim"]

    out["logs"] = bool(s.get("logs", default["logs"]))
    lang = s.get("language", default["language"])
    out["language"] = lang if lang in ("ru", "en") else default["language"]
    out["metadata_prompt_enabled"] = bool(s.get("metadata_prompt_enabled", default["metadata_prompt_enabled"]))

    return out


def _redis_settings_key(user_id):
    return _redis_key("user", str(user_id), "settings")


def _require_redis_client():
    client = _get_redis_client()
    if client is None:
        raise RuntimeError("Redis client is not initialized.")
    return client


def _redis_read_user_settings(user_id):
    client = _require_redis_client()
    try:
        raw = client.get(_redis_settings_key(user_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        return None
    except RedisError as e:
        raise RuntimeError(f"Redis settings read failed: {e}") from e
    except (ValueError, TypeError, UnicodeDecodeError) as e:
        _log_redis_issue(f"Redis settings read error: {type(e).__name__}: {e}")
        return None


def _redis_write_user_settings(user_id, settings):
    client = _require_redis_client()
    try:
        payload = json.dumps(settings, ensure_ascii=False)
        client.set(_redis_settings_key(user_id), payload)
    except RedisError as e:
        raise RuntimeError(f"Redis settings write failed: {e}") from e


def _read_local_user_settings(uid):
    raw = state.LOCAL_USER_SETTINGS.get(uid)
    if raw is None:
        return None
    try:
        return json.loads(json.dumps(raw, ensure_ascii=False))
    except Exception:
        return dict(raw) if isinstance(raw, dict) else None


def _write_local_user_settings(uid, settings):
    try:
        state.LOCAL_USER_SETTINGS[uid] = json.loads(json.dumps(settings, ensure_ascii=False))
    except Exception:
        state.LOCAL_USER_SETTINGS[uid] = dict(settings)


def get_user_settings_sync(user_id):
    uid = str(user_id)
    with state.USER_SETTINGS_LOCK:
        client = _get_redis_client()
        if client is None:
            local_raw = _read_local_user_settings(uid)
            if local_raw is None:
                defaults = normalize_settings({})
                _write_local_user_settings(uid, defaults)
                return defaults
            normalized = normalize_settings(local_raw)
            if normalized != local_raw:
                _write_local_user_settings(uid, normalized)
            return normalized

        try:
            redis_raw = _redis_read_user_settings(uid)
            if redis_raw is None:
                defaults = normalize_settings({})
                _redis_write_user_settings(uid, defaults)
                return defaults
            normalized = normalize_settings(redis_raw)
            if normalized != redis_raw:
                _redis_write_user_settings(uid, normalized)
            return normalized
        except RuntimeError as e:
            _log_redis_issue(f"Redis settings fallback to local state: {e}")
            local_raw = _read_local_user_settings(uid)
            if local_raw is None:
                defaults = normalize_settings({})
                _write_local_user_settings(uid, defaults)
                return defaults
            normalized = normalize_settings(local_raw)
            if normalized != local_raw:
                _write_local_user_settings(uid, normalized)
            return normalized


def set_user_settings_sync(user_id, new_settings):
    uid = str(user_id)
    normalized = normalize_settings(new_settings)
    with state.USER_SETTINGS_LOCK:
        client = _get_redis_client()
        if client is None:
            _write_local_user_settings(uid, normalized)
            return
        try:
            _redis_write_user_settings(uid, normalized)
        except RuntimeError as e:
            _log_redis_issue(f"Redis settings write fallback to local state: {e}")
            _write_local_user_settings(uid, normalized)


async def get_user_settings(user_id):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_user_settings_sync, user_id)


async def set_user_settings(user_id, new_settings):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, set_user_settings_sync, user_id, new_settings)


def get_user_logs_enabled_sync(user_id, default=False):
    if user_id is None:
        return default
    try:
        settings = get_user_settings_sync(user_id)
        return bool(settings.get("logs", default))
    except Exception:
        return default


async def get_user_logs_enabled(user_id, default=False):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_user_logs_enabled_sync, user_id, default)


async def log_user_event_if_enabled(user_id, event, level="INFO", error_code=None, user_logs_enabled=None, **fields):
    enabled = user_logs_enabled
    if enabled is None:
        enabled = await get_user_logs_enabled(user_id, default=False)
    if not enabled:
        return
    payload = dict(fields)
    payload["user_id"] = user_id
    if error_code is not None:
        payload["error_code"] = error_code
    log_event(event, level=level, **payload)
