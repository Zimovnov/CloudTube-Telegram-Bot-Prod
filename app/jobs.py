import asyncio
import os
import re
import shutil
import time
from pathlib import Path

from app import state
from app.config import (
    MAX_PARALLEL_PER_USER,
    MIN_SECONDS_BETWEEN_DOWNLOADS,
    REDIS_CONNECT_TIMEOUT,
    REDIS_ERROR_LOG_COOLDOWN_SECONDS,
    REDIS_HEALTH_CHECK_INTERVAL,
    REDIS_KEY_PREFIX,
    REDIS_MAX_CONNECTIONS,
    REDIS_REQUIRED,
    REDIS_SOCKET_TIMEOUT,
    REDIS_URL,
    RUNNING_JOB_TTL_SECONDS,
    SETTINGS_THROTTLE_MS,
)
from app.errors import (
    ERR_REDIS_CLIENT_MISSING,
    ERR_REDIS_DISABLED,
    ERR_REDIS_ISSUE,
    ERR_REDIS_UNAVAILABLE,
)
from app.logging_utils import log_event

try:
    import redis
    from redis.exceptions import RedisError
except Exception:
    redis = None

    class RedisError(Exception):
        pass


def safe_filename(s):
    if not s:
        return "file"
    return re.sub(r"[^A-Za-z0-9_\-\.]", "_", s)[:100]


def resolve_ffmpeg_path():
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

    for key in ("FFMPEG_PATH", "FFMPEG_BIN", "FFMPEG_DIR"):
        raw = os.getenv(key)
        if not raw:
            continue
        candidate = Path(raw.strip().strip('"'))
        if candidate.is_dir():
            candidate = candidate / exe_name
        if candidate.is_file():
            return str(candidate)

    which = shutil.which("ffmpeg")
    if which:
        return which

    return None


def _redis_key(*parts):
    return f"{REDIS_KEY_PREFIX}:{':'.join(str(p) for p in parts)}"


def _get_redis_client():
    return state.REDIS_CLIENT


def _log_redis_issue(message):
    now = time.time()
    with state.REDIS_ERROR_LOCK:
        if now - state.LAST_REDIS_ERROR_TS >= REDIS_ERROR_LOG_COOLDOWN_SECONDS:
            log_event("redis.issue", level="WARNING", error_code=ERR_REDIS_ISSUE, message=message)
            state.LAST_REDIS_ERROR_TS = now


def init_redis_client():
    if not REDIS_URL:
        if REDIS_REQUIRED:
            raise RuntimeError("REDIS_REQUIRED=true, но REDIS_URL пустой.")
        log_event(
            "redis.disabled",
            level="WARNING",
            error_code=ERR_REDIS_DISABLED,
            reason="REDIS_URL is empty",
            mode="local_state",
        )
        state.REDIS_CLIENT = None
        return
    if redis is None:
        msg = "Redis client library is not installed."
        if REDIS_REQUIRED:
            raise RuntimeError(msg)
        log_event(
            "redis.client_missing",
            level="WARNING",
            error_code=ERR_REDIS_CLIENT_MISSING,
            reason=msg,
            mode="local_state",
        )
        state.REDIS_CLIENT = None
        return

    try:
        state.REDIS_CLIENT = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
            health_check_interval=REDIS_HEALTH_CHECK_INTERVAL,
            retry_on_timeout=True,
            max_connections=REDIS_MAX_CONNECTIONS,
        )
        state.REDIS_CLIENT.ping()
        log_event("redis.connected", level="INFO")
    except Exception as e:
        state.REDIS_CLIENT = None
        err_text = f"Redis is unavailable ({type(e).__name__}: {e})."
        if REDIS_REQUIRED:
            raise RuntimeError(err_text)
        log_event(
            "redis.unavailable",
            level="WARNING",
            error_code=ERR_REDIS_UNAVAILABLE,
            reason=err_text,
            mode="local_state",
        )


def acquire_download_cooldown(user_id):
    client = _get_redis_client()
    if client is not None:
        key = _redis_key("cooldown", "download", user_id)
        try:
            ok = client.set(key, str(int(time.time())), nx=True, ex=max(1, int(MIN_SECONDS_BETWEEN_DOWNLOADS)))
            if ok:
                return 0
            ttl = client.ttl(key)
            if ttl is None or ttl < 0:
                return int(MIN_SECONDS_BETWEEN_DOWNLOADS)
            return int(ttl)
        except RedisError as e:
            _log_redis_issue(f"Redis cooldown error: {type(e).__name__}: {e}")

    last = state.LAST_DOWNLOAD_TIME.get(user_id, 0)
    now = time.time()
    if now - last < MIN_SECONDS_BETWEEN_DOWNLOADS:
        return int(max(1, MIN_SECONDS_BETWEEN_DOWNLOADS - (now - last)))
    state.LAST_DOWNLOAD_TIME[user_id] = now
    return 0


def allow_settings_change(user_id):
    client = _get_redis_client()
    if client is not None:
        key = _redis_key("cooldown", "settings", user_id)
        try:
            ok = client.set(key, "1", nx=True, px=max(1, int(SETTINGS_THROTTLE_MS)))
            return bool(ok)
        except RedisError as e:
            _log_redis_issue(f"Redis settings throttle error: {type(e).__name__}: {e}")

    now = time.time()
    last = state.LAST_SETTINGS_CHANGE.get(user_id, 0)
    if now - last < (SETTINGS_THROTTLE_MS / 1000.0):
        return False
    state.LAST_SETTINGS_CHANGE[user_id] = now
    return True


def clear_conversation_state(context, user_id=None):
    for key in ("platform", "url", "yt_type", "trim_prompt_msg_id", "job_id"):
        context.user_data.pop(key, None)

    if user_id is None:
        return

    cur = context.chat_data.get("running_jobs")
    if isinstance(cur, dict):
        if cur.get(user_id, 0) <= 0:
            cur.pop(user_id, None)
        if not cur:
            context.chat_data.pop("running_jobs", None)


def _acquire_job_slot_redis(user_id, max_parallel=MAX_PARALLEL_PER_USER):
    client = _get_redis_client()
    if client is None:
        return None
    key = _redis_key("jobs", "running", user_id)
    script = """
    local key = KEYS[1]
    local max_jobs = tonumber(ARGV[1])
    local ttl = tonumber(ARGV[2])
    local current = tonumber(redis.call('GET', key) or '0')
    if current >= max_jobs then
        return -1
    end
    current = redis.call('INCR', key)
    if ttl > 0 then
        redis.call('EXPIRE', key, ttl)
    end
    return current
    """
    try:
        result = client.eval(script, 1, key, int(max_parallel), int(RUNNING_JOB_TTL_SECONDS))
        return int(result) >= 1
    except RedisError as e:
        _log_redis_issue(f"Redis acquire job slot error: {type(e).__name__}: {e}")
        return None


def _release_job_slot_redis(user_id):
    client = _get_redis_client()
    if client is None:
        return None
    key = _redis_key("jobs", "running", user_id)
    script = """
    local key = KEYS[1]
    local ttl = tonumber(ARGV[1])
    local current = tonumber(redis.call('GET', key) or '0')
    if current <= 1 then
        redis.call('DEL', key)
        return 0
    end
    current = redis.call('DECR', key)
    if ttl > 0 then
        redis.call('EXPIRE', key, ttl)
    end
    return current
    """
    try:
        client.eval(script, 1, key, int(RUNNING_JOB_TTL_SECONDS))
        return True
    except RedisError as e:
        _log_redis_issue(f"Redis release job slot error: {type(e).__name__}: {e}")
        return None


def _clear_job_slot_redis(user_id):
    client = _get_redis_client()
    if client is None:
        return None
    key = _redis_key("jobs", "running", user_id)
    try:
        client.delete(key)
        return True
    except RedisError as e:
        _log_redis_issue(f"Redis clear job slot error: {type(e).__name__}: {e}")
        return None


def allowed_start_job(context, user_id, max_parallel=MAX_PARALLEL_PER_USER):
    client = _get_redis_client()
    if client is not None:
        key = _redis_key("jobs", "running", user_id)
        try:
            current = int(client.get(key) or 0)
            return current < max_parallel
        except RedisError as e:
            _log_redis_issue(f"Redis running job read error: {type(e).__name__}: {e}")

    cur = context.chat_data.get("running_jobs", {})
    return cur.get(user_id, 0) < max_parallel


def start_job(context, user_id, max_parallel=MAX_PARALLEL_PER_USER):
    acquired = _acquire_job_slot_redis(user_id, max_parallel=max_parallel)
    if acquired is not None:
        return bool(acquired)

    cur = context.chat_data.setdefault("running_jobs", {})
    if cur.get(user_id, 0) >= max_parallel:
        return False
    cur[user_id] = cur.get(user_id, 0) + 1
    return True


def finish_job(context, user_id):
    released = _release_job_slot_redis(user_id)
    if released:
        return

    cur = context.chat_data.get("running_jobs", {})
    if user_id in cur:
        cur[user_id] = max(0, cur[user_id] - 1)
        if cur[user_id] == 0:
            cur.pop(user_id, None)
    if not cur and "running_jobs" in context.chat_data:
        context.chat_data.pop("running_jobs", None)


def _clear_job_slot_local(context, user_id):
    cur = context.chat_data.get("running_jobs")
    if isinstance(cur, dict):
        cur.pop(user_id, None)
        if not cur:
            context.chat_data.pop("running_jobs", None)


def register_active_download_task(user_id, cancel_event=None, cancel_reason_ref=None):
    if user_id is None:
        return
    task = asyncio.current_task()
    if task is None:
        return
    with state.ACTIVE_DOWNLOAD_TASKS_LOCK:
        state.ACTIVE_DOWNLOAD_TASKS[user_id] = task
        if cancel_event is not None:
            state.ACTIVE_DOWNLOAD_CANCEL_EVENTS[user_id] = cancel_event
        if cancel_reason_ref is not None:
            state.ACTIVE_DOWNLOAD_CANCEL_REASONS[user_id] = cancel_reason_ref


def register_scheduled_download_task(user_id, task):
    if user_id is None or task is None:
        return
    with state.ACTIVE_DOWNLOAD_TASKS_LOCK:
        state.ACTIVE_DOWNLOAD_TASKS[user_id] = task


def unregister_active_download_task(user_id):
    if user_id is None:
        return
    current = asyncio.current_task()
    with state.ACTIVE_DOWNLOAD_TASKS_LOCK:
        task = state.ACTIVE_DOWNLOAD_TASKS.get(user_id)
        if task is None:
            state.ACTIVE_DOWNLOAD_CANCEL_EVENTS.pop(user_id, None)
            state.ACTIVE_DOWNLOAD_CANCEL_REASONS.pop(user_id, None)
            state.ACTIVE_DOWNLOAD_WORKER_FUTURES.pop(user_id, None)
            return
        if task is current or task.done():
            state.ACTIVE_DOWNLOAD_TASKS.pop(user_id, None)
            state.ACTIVE_DOWNLOAD_CANCEL_EVENTS.pop(user_id, None)
            state.ACTIVE_DOWNLOAD_CANCEL_REASONS.pop(user_id, None)
            state.ACTIVE_DOWNLOAD_WORKER_FUTURES.pop(user_id, None)


def register_active_worker_future(user_id, worker_future):
    if user_id is None or worker_future is None:
        return
    with state.ACTIVE_DOWNLOAD_TASKS_LOCK:
        if user_id in state.ACTIVE_DOWNLOAD_TASKS:
            state.ACTIVE_DOWNLOAD_WORKER_FUTURES[user_id] = worker_future


def unregister_active_worker_future(user_id, worker_future=None):
    if user_id is None:
        return
    with state.ACTIVE_DOWNLOAD_TASKS_LOCK:
        current = state.ACTIVE_DOWNLOAD_WORKER_FUTURES.get(user_id)
        if current is None:
            return
        if worker_future is None or current is worker_future:
            state.ACTIVE_DOWNLOAD_WORKER_FUTURES.pop(user_id, None)


def request_active_download_cancel(user_id, reason="user_cancel"):
    if user_id is None:
        return False
    with state.ACTIVE_DOWNLOAD_TASKS_LOCK:
        cancel_event = state.ACTIVE_DOWNLOAD_CANCEL_EVENTS.get(user_id)
        reason_ref = state.ACTIVE_DOWNLOAD_CANCEL_REASONS.get(user_id)
        if reason_ref is not None and isinstance(reason_ref, list):
            if not reason_ref:
                reason_ref.append(reason)
            elif reason_ref[0] is None:
                reason_ref[0] = reason
    if cancel_event is None:
        return False
    cancel_event.set()
    return True


def cancel_active_download_task(user_id, reason="user_cancel"):
    if user_id is None:
        return False
    request_active_download_cancel(user_id, reason=reason)
    with state.ACTIVE_DOWNLOAD_TASKS_LOCK:
        task = state.ACTIVE_DOWNLOAD_TASKS.get(user_id)
        worker_future = state.ACTIVE_DOWNLOAD_WORKER_FUTURES.get(user_id)
        if task is None:
            state.ACTIVE_DOWNLOAD_CANCEL_EVENTS.pop(user_id, None)
            state.ACTIVE_DOWNLOAD_CANCEL_REASONS.pop(user_id, None)
            state.ACTIVE_DOWNLOAD_WORKER_FUTURES.pop(user_id, None)
        if task is not None and task.done():
            state.ACTIVE_DOWNLOAD_TASKS.pop(user_id, None)
            state.ACTIVE_DOWNLOAD_CANCEL_EVENTS.pop(user_id, None)
            state.ACTIVE_DOWNLOAD_CANCEL_REASONS.pop(user_id, None)
            state.ACTIVE_DOWNLOAD_WORKER_FUTURES.pop(user_id, None)
            task = None
            worker_future = None
    if worker_future is not None:
        try:
            worker_future.cancel()
        except Exception:
            pass
    if task is None:
        return worker_future is not None
    entry = state.JOB_PROGRESS.get(user_id)
    if isinstance(entry, dict):
        entry["cancel_requested"] = True
        entry["last_info"] = {"status": "cancel_requested"}
    task.cancel()
    return True


def abort_user_job(context, user_id):
    cancelled = cancel_active_download_task(user_id, reason="user_cancel")
    cleared = _clear_job_slot_redis(user_id)
    if cleared is None:
        finish_job(context, user_id)
    _clear_job_slot_local(context, user_id)
    return cancelled
