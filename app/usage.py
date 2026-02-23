import asyncio
import re
import time

from app import state
from app.config import (
    FREE_MONTHLY_LIMIT,
    JOB_COUNTED_TTL_SECONDS,
    PAYMENT_DEDUP_TTL_SECONDS,
    UPDATE_DEDUP_TTL_SECONDS,
    USAGE_COUNTER_TTL_SECONDS,
)
from app.jobs import RedisError, _get_redis_client, _log_redis_issue, _redis_key
from app.logging_utils import log_event


def utc_month_label(now_ts=None):
    ts = float(now_ts or time.time())
    return time.strftime("%Y%m", time.gmtime(ts))


def usage_key(user_id, month_label=None):
    month = month_label or utc_month_label()
    return _redis_key("usage", int(user_id), month)


def job_counted_key(job_id):
    return _redis_key("job_counted", str(job_id))


def payment_done_key(charge_id):
    return _redis_key("payment_done", str(charge_id))


def update_done_key(update_id):
    return _redis_key("update_done", str(update_id))


def get_free_usage_count_sync(user_id, month_label=None):
    uid = int(user_id)
    month = month_label or utc_month_label()
    client = _get_redis_client()
    if client is not None:
        try:
            return int(client.get(usage_key(uid, month)) or 0)
        except RedisError as e:
            _log_redis_issue(f"Redis usage read failed: {type(e).__name__}: {e}")
    with state.USAGE_LOCK:
        return int(state.LOCAL_USAGE_COUNTERS.get((uid, month), 0))


async def get_free_usage_count(user_id, month_label=None):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_free_usage_count_sync, user_id, month_label)


def normalize_usage_month_label(month_label=None):
    if month_label is None:
        return utc_month_label()
    text = str(month_label).strip()
    if not re.fullmatch(r"\d{6}", text):
        raise ValueError("invalid_month_label")
    month_num = int(text[4:6])
    if month_num < 1 or month_num > 12:
        raise ValueError("invalid_month_label")
    return text


def is_free_limit_reached_sync(user_id, limit=FREE_MONTHLY_LIMIT):
    count = get_free_usage_count_sync(user_id)
    return count >= int(limit), count


async def is_free_limit_reached(user_id, limit=FREE_MONTHLY_LIMIT):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, is_free_limit_reached_sync, user_id, limit)


def increment_usage_success_once_sync(user_id, job_id, month_label=None):
    uid = int(user_id)
    month = month_label or utc_month_label()
    usage_redis_key = usage_key(uid, month)
    counted_redis_key = job_counted_key(job_id)
    client = _get_redis_client()
    if client is not None:
        script = """
        local usage_key = KEYS[1]
        local counted_key = KEYS[2]
        local usage_ttl = tonumber(ARGV[1])
        local counted_ttl = tonumber(ARGV[2])

        local marked = redis.call('SET', counted_key, '1', 'NX', 'EX', counted_ttl)
        if not marked then
            return -1
        end
        local v = redis.call('INCR', usage_key)
        if usage_ttl > 0 then
            local ttl = redis.call('TTL', usage_key)
            if ttl == -1 then
                redis.call('EXPIRE', usage_key, usage_ttl)
            end
        end
        return v
        """
        try:
            result = int(
                client.eval(
                    script,
                    2,
                    usage_redis_key,
                    counted_redis_key,
                    int(USAGE_COUNTER_TTL_SECONDS),
                    int(JOB_COUNTED_TTL_SECONDS),
                )
            )
            if result >= 0:
                log_event(
                    "usage.incremented",
                    level="INFO",
                    user_id=uid,
                    usage_month=month,
                    usage_count=result,
                    job_id=str(job_id),
                )
                return True, result
            return False, get_free_usage_count_sync(uid, month)
        except RedisError as e:
            _log_redis_issue(f"Redis usage increment failed: {type(e).__name__}: {e}")

    with state.USAGE_LOCK:
        if str(job_id) in state.LOCAL_JOB_COUNTED:
            return False, int(state.LOCAL_USAGE_COUNTERS.get((uid, month), 0))
        state.LOCAL_JOB_COUNTED[str(job_id)] = time.time() + JOB_COUNTED_TTL_SECONDS
        current = int(state.LOCAL_USAGE_COUNTERS.get((uid, month), 0)) + 1
        state.LOCAL_USAGE_COUNTERS[(uid, month)] = current
    log_event(
        "usage.incremented",
        level="INFO",
        user_id=uid,
        usage_month=month,
        usage_count=current,
        job_id=str(job_id),
    )
    return True, current


async def increment_usage_success_once(user_id, job_id, month_label=None):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, increment_usage_success_once_sync, user_id, job_id, month_label)


def reset_free_usage_sync(user_id, month_label=None):
    uid = int(user_id)
    month = normalize_usage_month_label(month_label)
    client = _get_redis_client()
    if client is not None:
        script = """
        local key = KEYS[1]
        local current = redis.call('GET', key)
        redis.call('DEL', key)
        if not current then
            return 0
        end
        local v = tonumber(current)
        if not v then
            return 0
        end
        return v
        """
        try:
            previous = int(client.eval(script, 1, usage_key(uid, month)) or 0)
            log_event(
                "usage.reset",
                level="WARNING",
                user_id=uid,
                usage_month=month,
                previous_count=previous,
            )
            return {"user_id": uid, "month_label": month, "previous_count": previous}
        except RedisError as e:
            _log_redis_issue(f"Redis usage reset failed: {type(e).__name__}: {e}")

    with state.USAGE_LOCK:
        previous = int(state.LOCAL_USAGE_COUNTERS.get((uid, month), 0))
        state.LOCAL_USAGE_COUNTERS.pop((uid, month), None)
    log_event(
        "usage.reset",
        level="WARNING",
        user_id=uid,
        usage_month=month,
        previous_count=previous,
    )
    return {"user_id": uid, "month_label": month, "previous_count": previous}


async def reset_free_usage(user_id, month_label=None):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, reset_free_usage_sync, user_id, month_label)


def register_payment_once_sync(charge_id):
    if not charge_id:
        return False
    key = str(charge_id)
    client = _get_redis_client()
    if client is not None:
        try:
            ok = client.set(payment_done_key(key), "1", nx=True, ex=int(PAYMENT_DEDUP_TTL_SECONDS))
            return bool(ok)
        except RedisError as e:
            _log_redis_issue(f"Redis payment dedup failed: {type(e).__name__}: {e}")
    with state.USAGE_LOCK:
        expires_at = state.LOCAL_PAYMENT_DONE.get(key)
        now = time.time()
        if expires_at and expires_at > now:
            return False
        state.LOCAL_PAYMENT_DONE[key] = now + PAYMENT_DEDUP_TTL_SECONDS
        return True


async def register_payment_once(charge_id):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, register_payment_once_sync, charge_id)


def register_update_once_sync(update_id):
    if update_id is None:
        return True
    key = str(update_id)
    client = _get_redis_client()
    if client is not None:
        try:
            ok = client.set(update_done_key(key), "1", nx=True, ex=int(UPDATE_DEDUP_TTL_SECONDS))
            return bool(ok)
        except RedisError as e:
            _log_redis_issue(f"Redis update dedup failed: {type(e).__name__}: {e}")
    with state.USAGE_LOCK:
        expires_at = state.LOCAL_UPDATES_DONE.get(key)
        now = time.time()
        if expires_at and expires_at > now:
            return False
        state.LOCAL_UPDATES_DONE[key] = now + UPDATE_DEDUP_TTL_SECONDS
        return True


async def register_update_once(update_id):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, register_update_once_sync, update_id)
