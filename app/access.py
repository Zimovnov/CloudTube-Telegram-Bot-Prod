import asyncio
import copy
import json
import secrets
import time
from datetime import datetime, timedelta, timezone

from app import state
from app.config import (
    ADMIN_MASS_CHANGES_ALERT_THRESHOLD,
    ADMIN_MASS_CHANGES_WINDOW_SECONDS,
    ADMIN_NONCE_TTL_SECONDS,
    ALLOWED_USERS,
    AUDIT_LOG_MAX_EVENTS,
    PREMIUM_PERIOD_SECONDS,
)
from app.errors import ERR_LAST_SUPERADMIN, ERR_RBAC_DENIED
from app.jobs import RedisError, _get_redis_client, _log_redis_issue, _redis_key
from app.logging_utils import log_event
from app.usage import reset_free_usage_sync

PLAN_FREE = "free"
PLAN_PREMIUM_MONTHLY = "premium_monthly"
PLAN_PREMIUM_LIFETIME = "premium_lifetime"
PLANS = {PLAN_FREE, PLAN_PREMIUM_MONTHLY, PLAN_PREMIUM_LIFETIME}

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_SUPERADMIN = "superadmin"
ROLES = {ROLE_USER, ROLE_ADMIN, ROLE_SUPERADMIN}

PERM_ADMIN_ACCESS = "admin.access"
PERM_PLAN_MANAGE = "plan.manage"
PERM_ROLE_MANAGE = "role.manage"

ROLE_ORDER = {
    ROLE_USER: 0,
    ROLE_ADMIN: 1,
    ROLE_SUPERADMIN: 2,
}

ROLE_PERMISSIONS = {
    ROLE_USER: set(),
    ROLE_ADMIN: {PERM_ADMIN_ACCESS, PERM_PLAN_MANAGE},
    ROLE_SUPERADMIN: {PERM_ADMIN_ACCESS, PERM_PLAN_MANAGE, PERM_ROLE_MANAGE},
}


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return to_utc_iso(utc_now())


def to_utc_iso(value):
    if value is None:
        return None
    dt = value.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc_iso(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc_iso_for_display(value):
    if not value:
        return "-"
    dt = parse_utc_iso(value)
    if not dt:
        return str(value).strip() or "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def utc_month_label(now=None):
    dt = now or utc_now()
    return dt.strftime("%Y%m")


def is_premium_plan(plan_type):
    return plan_type in (PLAN_PREMIUM_MONTHLY, PLAN_PREMIUM_LIFETIME)


def _redis_profile_key(user_id):
    return _redis_key("user", user_id, "profile")


def _redis_audit_key():
    return _redis_key("audit", "events")


def _redis_role_key(role):
    return _redis_key("roles", role)


def _redis_nonce_key(nonce):
    return _redis_key("admin", "nonce", nonce)


def _redis_mass_ops_key(actor_user_id):
    window = int(time.time() // ADMIN_MASS_CHANGES_WINDOW_SECONDS)
    return _redis_key("admin", "ops", actor_user_id, window)


def _default_profile(user_id):
    return {
        "user_id": int(user_id),
        "plan_type": PLAN_FREE,
        "plan_expires_at_utc": None,
        "role": ROLE_USER,
        "updated_at_utc": utc_now_iso(),
    }


def normalize_profile(data, user_id=None):
    base = _default_profile(user_id or (data or {}).get("user_id") or 0)
    if not isinstance(data, dict):
        return base
    out = dict(base)
    try:
        out["user_id"] = int(data.get("user_id", base["user_id"]))
    except Exception:
        out["user_id"] = base["user_id"]

    plan_type = data.get("plan_type", base["plan_type"])
    out["plan_type"] = plan_type if plan_type in PLANS else PLAN_FREE
    expires = parse_utc_iso(data.get("plan_expires_at_utc"))
    out["plan_expires_at_utc"] = to_utc_iso(expires) if expires else None
    if out["plan_type"] == PLAN_PREMIUM_LIFETIME:
        out["plan_expires_at_utc"] = None
    if out["plan_type"] == PLAN_FREE:
        out["plan_expires_at_utc"] = None

    role = data.get("role", base["role"])
    out["role"] = role if role in ROLES else ROLE_USER
    updated_at = parse_utc_iso(data.get("updated_at_utc"))
    out["updated_at_utc"] = to_utc_iso(updated_at or utc_now())
    return out


def _deepcopy(data):
    return copy.deepcopy(data)


def _read_profile_redis(client, user_id):
    try:
        raw = client.get(_redis_profile_key(user_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except RedisError as e:
        raise RuntimeError(f"Redis profile read failed: {e}") from e
    except Exception as e:
        _log_redis_issue(f"Redis profile decode failed: {type(e).__name__}: {e}")
        return None


def _write_profile_redis(client, profile):
    try:
        client.set(_redis_profile_key(profile["user_id"]), json.dumps(profile, ensure_ascii=False))
    except RedisError as e:
        raise RuntimeError(f"Redis profile write failed: {e}") from e


def _read_profile_local(user_id):
    payload = state.LOCAL_USER_PROFILES.get(int(user_id))
    return _deepcopy(payload) if isinstance(payload, dict) else None


def _write_profile_local(profile):
    state.LOCAL_USER_PROFILES[int(profile["user_id"])] = _deepcopy(profile)


def _sync_role_index_local(user_id, old_role, new_role):
    uid = int(user_id)
    for role_name in ("admin", "superadmin"):
        state.LOCAL_ROLE_INDEX.setdefault(role_name, set()).discard(uid)
    if new_role in ("admin", "superadmin"):
        state.LOCAL_ROLE_INDEX.setdefault(new_role, set()).add(uid)


def _sync_role_index_redis(client, user_id, old_role, new_role):
    uid = str(int(user_id))
    pipe = client.pipeline()
    if old_role in ("admin", "superadmin") and old_role != new_role:
        pipe.srem(_redis_role_key(old_role), uid)
    if new_role in ("admin", "superadmin"):
        pipe.sadd(_redis_role_key(new_role), uid)
    pipe.execute()


def _count_superadmins_locked(client):
    if client is not None:
        try:
            return int(client.scard(_redis_role_key("superadmin")) or 0)
        except RedisError as e:
            _log_redis_issue(f"Redis superadmin count failed: {type(e).__name__}: {e}")
    return len(state.LOCAL_ROLE_INDEX.get("superadmin", set()))


def _write_profile_locked(client, profile, old_role=None):
    uid = profile["user_id"]
    if client is not None:
        _write_profile_redis(client, profile)
        try:
            _sync_role_index_redis(client, uid, old_role, profile["role"])
            return
        except RuntimeError as e:
            _log_redis_issue(str(e))
    _write_profile_local(profile)
    _sync_role_index_local(uid, old_role, profile["role"])


def _read_profile_locked(client, user_id):
    uid = int(user_id)
    if client is not None:
        try:
            payload = _read_profile_redis(client, uid)
            if payload is not None:
                return payload
        except RuntimeError as e:
            _log_redis_issue(str(e))
    return _read_profile_local(uid)


def append_audit_event_sync(
    event_name,
    *,
    target_user_id,
    reason="",
    source="system",
    granted_by=None,
    revoked_by=None,
    **extra,
):
    payload = {
        "event": str(event_name),
        "target_user_id": int(target_user_id),
        "granted_by": int(granted_by) if granted_by is not None else None,
        "revoked_by": int(revoked_by) if revoked_by is not None else None,
        "reason": str(reason or ""),
        "created_at_utc": utc_now_iso(),
        "source": str(source or "system"),
    }
    payload.update(extra or {})
    client = _get_redis_client()
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.lpush(_redis_audit_key(), json.dumps(payload, ensure_ascii=False))
            pipe.ltrim(_redis_audit_key(), 0, max(0, AUDIT_LOG_MAX_EVENTS - 1))
            pipe.execute()
        except RedisError as e:
            _log_redis_issue(f"Redis audit write failed: {type(e).__name__}: {e}")
    with state.USER_PROFILE_LOCK:
        state.LOCAL_AUDIT_EVENTS.insert(0, payload)
        if len(state.LOCAL_AUDIT_EVENTS) > AUDIT_LOG_MAX_EVENTS:
            del state.LOCAL_AUDIT_EVENTS[AUDIT_LOG_MAX_EVENTS:]
    log_event(
        "audit.event",
        level="INFO",
        audit_event=payload["event"],
        target_user_id=payload["target_user_id"],
        granted_by=payload["granted_by"],
        revoked_by=payload["revoked_by"],
        reason=payload["reason"],
        source=payload["source"],
    )
    return payload


def _expire_plan_if_needed_locked(client, profile):
    changed = False
    if profile.get("plan_type") == PLAN_PREMIUM_MONTHLY:
        expires_at = parse_utc_iso(profile.get("plan_expires_at_utc"))
        if expires_at is None or expires_at <= utc_now():
            profile["plan_type"] = PLAN_FREE
            profile["plan_expires_at_utc"] = None
            profile["updated_at_utc"] = utc_now_iso()
            changed = True
            log_event(
                "subscription.expired",
                level="INFO",
                user_id=profile["user_id"],
                plan_type=PLAN_PREMIUM_MONTHLY,
            )
    if changed:
        _write_profile_locked(client, profile, old_role=profile.get("role"))
    return changed


def get_user_profile_sync(user_id):
    uid = int(user_id)
    client = _get_redis_client()
    with state.USER_PROFILE_LOCK:
        payload = _read_profile_locked(client, uid)
        if payload is None:
            payload = _default_profile(uid)
            _write_profile_locked(client, payload, old_role=None)
        normalized = normalize_profile(payload, user_id=uid)
        changed = normalized != payload
        if _expire_plan_if_needed_locked(client, normalized):
            changed = False
        try:
            if client is not None:
                _sync_role_index_redis(client, uid, None, normalized.get("role"))
            else:
                _sync_role_index_local(uid, None, normalized.get("role"))
        except Exception:
            pass
        if changed:
            _write_profile_locked(client, normalized, old_role=payload.get("role"))
        return _deepcopy(normalized)


async def get_user_profile(user_id):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_user_profile_sync, user_id)


def _write_normalized_profile_sync(profile):
    client = _get_redis_client()
    uid = int(profile["user_id"])
    with state.USER_PROFILE_LOCK:
        prev = _read_profile_locked(client, uid)
        old_role = prev.get("role") if isinstance(prev, dict) else None
        normalized = normalize_profile(profile, user_id=uid)
        normalized["updated_at_utc"] = utc_now_iso()
        _write_profile_locked(client, normalized, old_role=old_role)
        return _deepcopy(normalized), (prev or _default_profile(uid))


def set_user_profile_sync(profile):
    normalized, _ = _write_normalized_profile_sync(profile)
    return normalized


async def set_user_profile(profile):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, set_user_profile_sync, profile)


def has_permission(role, permission):
    return permission in ROLE_PERMISSIONS.get(role, set())


def rbac_check_sync(user_id, permission, source="unknown"):
    profile = get_user_profile_sync(user_id)
    if has_permission(profile["role"], permission):
        return True
    log_event(
        "rbac.denied",
        level="WARNING",
        error_code=ERR_RBAC_DENIED,
        user_id=user_id,
        role=profile["role"],
        permission=permission,
        source=source,
    )
    return False


async def rbac_check(user_id, permission, source="unknown"):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, rbac_check_sync, user_id, permission, source)


def activate_or_extend_monthly_sync(user_id, charge_id=None, source="payment"):
    uid = int(user_id)
    client = _get_redis_client()
    with state.USER_PROFILE_LOCK:
        profile = normalize_profile(_read_profile_locked(client, uid) or _default_profile(uid), user_id=uid)
        old_profile = _deepcopy(profile)
        if profile.get("plan_type") == PLAN_PREMIUM_LIFETIME:
            log_event(
                "subscription.renew_ignored_lifetime",
                level="WARNING",
                user_id=uid,
                charge_id=charge_id,
                source=source,
            )
            return _deepcopy(profile)
        _expire_plan_if_needed_locked(client, profile)
        now = utc_now()
        current_expire = parse_utc_iso(profile.get("plan_expires_at_utc"))
        base = now
        if profile.get("plan_type") == PLAN_PREMIUM_MONTHLY and current_expire and current_expire > now:
            base = current_expire
        new_expire = base + timedelta(seconds=PREMIUM_PERIOD_SECONDS)
        profile["plan_type"] = PLAN_PREMIUM_MONTHLY
        profile["plan_expires_at_utc"] = to_utc_iso(new_expire)
        profile["updated_at_utc"] = utc_now_iso()
        _write_profile_locked(client, profile, old_role=old_profile.get("role"))

    old_expires = parse_utc_iso(old_profile.get("plan_expires_at_utc"))
    event_name = (
        "subscription.renewed"
        if old_profile.get("plan_type") == PLAN_PREMIUM_MONTHLY and old_expires and old_expires > utc_now()
        else "subscription.activated"
    )
    log_event(
        event_name,
        level="INFO",
        user_id=uid,
        plan_type=PLAN_PREMIUM_MONTHLY,
        expires_at_utc=profile["plan_expires_at_utc"],
        charge_id=charge_id,
        source=source,
    )
    append_audit_event_sync(
        "plan.changed",
        target_user_id=uid,
        granted_by=uid if source == "payment" else None,
        reason=f"{source}:{charge_id or 'n/a'}",
        source=source,
        plan_type=profile["plan_type"],
        plan_expires_at_utc=profile["plan_expires_at_utc"],
    )
    return _deepcopy(profile)


async def activate_or_extend_monthly(user_id, charge_id=None, source="payment"):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, activate_or_extend_monthly_sync, user_id, charge_id, source)


def set_plan_sync(target_user_id, plan_type, actor_user_id=None, reason="", source="admin"):
    if plan_type not in PLANS:
        raise ValueError("invalid_plan_type")

    uid = int(target_user_id)
    client = _get_redis_client()
    with state.USER_PROFILE_LOCK:
        profile = normalize_profile(_read_profile_locked(client, uid) or _default_profile(uid), user_id=uid)
        old = _deepcopy(profile)
        now = utc_now()
        if plan_type == PLAN_FREE:
            profile["plan_type"] = PLAN_FREE
            profile["plan_expires_at_utc"] = None
        elif plan_type == PLAN_PREMIUM_LIFETIME:
            profile["plan_type"] = PLAN_PREMIUM_LIFETIME
            profile["plan_expires_at_utc"] = None
        else:
            current_expire = parse_utc_iso(profile.get("plan_expires_at_utc"))
            base = now
            if profile.get("plan_type") == PLAN_PREMIUM_MONTHLY and current_expire and current_expire > now:
                base = current_expire
            profile["plan_type"] = PLAN_PREMIUM_MONTHLY
            profile["plan_expires_at_utc"] = to_utc_iso(base + timedelta(seconds=PREMIUM_PERIOD_SECONDS))
        profile["updated_at_utc"] = utc_now_iso()
        _write_profile_locked(client, profile, old_role=old.get("role"))

    granted_by = actor_user_id if profile["plan_type"] != PLAN_FREE else None
    revoked_by = actor_user_id if profile["plan_type"] == PLAN_FREE and old["plan_type"] != PLAN_FREE else None
    append_audit_event_sync(
        "plan.changed",
        target_user_id=uid,
        granted_by=granted_by,
        revoked_by=revoked_by,
        reason=reason,
        source=source,
        old_plan_type=old.get("plan_type"),
        plan_type=profile.get("plan_type"),
        plan_expires_at_utc=profile.get("plan_expires_at_utc"),
    )
    return _deepcopy(profile)


async def set_plan(target_user_id, plan_type, actor_user_id=None, reason="", source="admin"):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        set_plan_sync,
        target_user_id,
        plan_type,
        actor_user_id,
        reason,
        source,
    )


def set_role_sync(target_user_id, new_role, actor_user_id=None, reason="", source="admin"):
    if new_role not in ROLES:
        raise ValueError("invalid_role")

    uid = int(target_user_id)
    actor_id = int(actor_user_id) if actor_user_id is not None else None
    client = _get_redis_client()
    with state.USER_PROFILE_LOCK:
        target = normalize_profile(_read_profile_locked(client, uid) or _default_profile(uid), user_id=uid)
        old = _deepcopy(target)
        actor = normalize_profile(
            _read_profile_locked(client, actor_id) or _default_profile(actor_id),
            user_id=actor_id,
        ) if actor_id is not None else {"role": ROLE_SUPERADMIN}

        if actor_id is not None and uid == actor_id and ROLE_ORDER.get(new_role, 0) > ROLE_ORDER.get(actor.get("role"), 0):
            raise PermissionError("self_escalation")
        if actor_id is not None and new_role in (ROLE_ADMIN, ROLE_SUPERADMIN) and actor.get("role") != ROLE_SUPERADMIN:
            raise PermissionError("admin_role_change_denied")
        if old.get("role") == ROLE_SUPERADMIN and new_role != ROLE_SUPERADMIN:
            if _count_superadmins_locked(client) <= 1:
                raise RuntimeError(ERR_LAST_SUPERADMIN)

        target["role"] = new_role
        target["updated_at_utc"] = utc_now_iso()
        _write_profile_locked(client, target, old_role=old.get("role"))

    granted_by = actor_id if ROLE_ORDER.get(new_role, 0) > ROLE_ORDER.get(old.get("role"), 0) else None
    revoked_by = actor_id if ROLE_ORDER.get(new_role, 0) < ROLE_ORDER.get(old.get("role"), 0) else None
    append_audit_event_sync(
        "role.changed",
        target_user_id=uid,
        granted_by=granted_by,
        revoked_by=revoked_by,
        reason=reason,
        source=source,
        old_role=old.get("role"),
        role=new_role,
    )
    return _deepcopy(target)


async def set_role(target_user_id, new_role, actor_user_id=None, reason="", source="admin"):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        set_role_sync,
        target_user_id,
        new_role,
        actor_user_id,
        reason,
        source,
    )


def _track_admin_mass_changes(actor_user_id):
    if actor_user_id is None:
        return
    uid = int(actor_user_id)
    client = _get_redis_client()
    if client is not None:
        key = _redis_mass_ops_key(uid)
        try:
            value = int(client.incr(key))
            if value == 1:
                client.expire(key, int(ADMIN_MASS_CHANGES_WINDOW_SECONDS))
            if value >= ADMIN_MASS_CHANGES_ALERT_THRESHOLD:
                log_event(
                    "security.alert.mass_admin_ops",
                    level="WARNING",
                    actor_user_id=uid,
                    operations=value,
                    window_seconds=ADMIN_MASS_CHANGES_WINDOW_SECONDS,
                )
            return
        except RedisError as e:
            _log_redis_issue(f"Redis admin op tracking failed: {type(e).__name__}: {e}")
    with state.USAGE_LOCK:
        key = (uid, int(time.time() // ADMIN_MASS_CHANGES_WINDOW_SECONDS))
        count = int(state.LOCAL_USAGE_COUNTERS.get(("admin_ops", key), 0)) + 1
        state.LOCAL_USAGE_COUNTERS[("admin_ops", key)] = count
    if count >= ADMIN_MASS_CHANGES_ALERT_THRESHOLD:
        log_event(
            "security.alert.mass_admin_ops",
            level="WARNING",
            actor_user_id=uid,
            operations=count,
            window_seconds=ADMIN_MASS_CHANGES_WINDOW_SECONDS,
        )


def create_admin_nonce_sync(initiator_user_id, payload, ttl_seconds=ADMIN_NONCE_TTL_SECONDS):
    nonce = secrets.token_urlsafe(16)
    body = {
        "nonce": nonce,
        "initiator_user_id": int(initiator_user_id),
        "payload": payload or {},
        "created_at_utc": utc_now_iso(),
        "expires_at_utc": to_utc_iso(utc_now() + timedelta(seconds=int(ttl_seconds))),
    }
    client = _get_redis_client()
    if client is not None:
        try:
            client.set(_redis_nonce_key(nonce), json.dumps(body, ensure_ascii=False), ex=int(ttl_seconds))
            return body
        except RedisError as e:
            _log_redis_issue(f"Redis nonce create failed: {type(e).__name__}: {e}")
    with state.USAGE_LOCK:
        state.LOCAL_PENDING_NONCES[nonce] = (body, time.time() + int(ttl_seconds))
    return body


async def create_admin_nonce(initiator_user_id, payload, ttl_seconds=ADMIN_NONCE_TTL_SECONDS):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, create_admin_nonce_sync, initiator_user_id, payload, ttl_seconds)


def consume_admin_nonce_sync(nonce):
    client = _get_redis_client()
    if client is not None:
        script = """
        local key = KEYS[1]
        local v = redis.call('GET', key)
        if not v then
            return nil
        end
        redis.call('DEL', key)
        return v
        """
        try:
            raw = client.eval(script, 1, _redis_nonce_key(nonce))
            if not raw:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except RedisError as e:
            _log_redis_issue(f"Redis nonce consume failed: {type(e).__name__}: {e}")
        except Exception as e:
            _log_redis_issue(f"Nonce decode failed: {type(e).__name__}: {e}")

    with state.USAGE_LOCK:
        item = state.LOCAL_PENDING_NONCES.pop(str(nonce), None)
    if item is None:
        return None
    payload, expires_ts = item
    if time.time() > float(expires_ts):
        return None
    return payload


async def consume_admin_nonce(nonce):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, consume_admin_nonce_sync, nonce)


def bootstrap_superadmin_sync():
    if not ALLOWED_USERS:
        return None
    client = _get_redis_client()
    with state.USER_PROFILE_LOCK:
        if _count_superadmins_locked(client) > 0:
            return None
        target_id = int(ALLOWED_USERS[0])
        profile = normalize_profile(_read_profile_locked(client, target_id) or _default_profile(target_id), user_id=target_id)
        old_role = profile.get("role")
        profile["role"] = ROLE_SUPERADMIN
        profile["updated_at_utc"] = utc_now_iso()
        _write_profile_locked(client, profile, old_role=old_role)
    append_audit_event_sync(
        "role.changed",
        target_user_id=target_id,
        granted_by=None,
        reason="bootstrap superadmin from ALLOWED_USERS",
        source="bootstrap",
        role=ROLE_SUPERADMIN,
    )
    log_event("rbac.bootstrap.superadmin", level="WARNING", target_user_id=target_id)
    return profile


def apply_admin_payload_sync(payload, actor_user_id):
    op = (payload or {}).get("op")
    target_user_id = (payload or {}).get("target_user_id")
    reason = (payload or {}).get("reason") or ""
    if op == "set_plan":
        profile = set_plan_sync(target_user_id, payload.get("plan_type"), actor_user_id=actor_user_id, reason=reason, source="admin")
        _track_admin_mass_changes(actor_user_id)
        return {"op": op, "profile": profile}
    if op == "set_role":
        profile = set_role_sync(target_user_id, payload.get("role"), actor_user_id=actor_user_id, reason=reason, source="admin")
        _track_admin_mass_changes(actor_user_id)
        return {"op": op, "profile": profile}
    if op == "reset_usage":
        usage_result = reset_free_usage_sync(target_user_id, payload.get("month_label"))
        append_audit_event_sync(
            "usage.reset",
            target_user_id=int(target_user_id),
            granted_by=int(actor_user_id) if actor_user_id is not None else None,
            reason=reason,
            source="admin",
            usage_month=usage_result.get("month_label"),
            previous_count=usage_result.get("previous_count"),
        )
        _track_admin_mass_changes(actor_user_id)
        return {"op": op, "usage": usage_result}
    raise ValueError("unknown_admin_op")
