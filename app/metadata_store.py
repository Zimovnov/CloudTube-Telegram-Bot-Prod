import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import state
from app.config import (
    METADATA_ARTIST_MAX_LEN,
    METADATA_SESSION_TTL_SECONDS,
    METADATA_STORAGE_DIR,
    METADATA_TITLE_MAX_LEN,
)
from app.errors import ERR_METADATA_INVALID_INPUT, ERR_METADATA_SESSION_EXPIRED
from app.jobs import RedisError, _get_redis_client, _log_redis_issue, _redis_key, resolve_ffmpeg_path
from app.logging_utils import log_event

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1F\x7F]")


def _utc_iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _storage_dir():
    root = Path(METADATA_STORAGE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _session_key(session_id):
    return _redis_key("metadata", "session", session_id)


def _input_key(user_id):
    return _redis_key("metadata", "input", int(user_id))


def _user_active_key(user_id):
    return _redis_key("metadata", "active", int(user_id))


def _expiry_zset_key():
    return _redis_key("metadata", "expires")


def _make_session_payload(user_id, file_path, title, artist, source_job_id=None):
    now = time.time()
    return {
        "session_id": uuid.uuid4().hex[:16],
        "user_id": int(user_id),
        "file_path": str(file_path),
        "title_original": str(title or ""),
        "artist_original": str(artist or ""),
        "title_new": None,
        "artist_new": None,
        "source_job_id": source_job_id,
        "created_at_utc": _utc_iso_now(),
        "updated_at_utc": _utc_iso_now(),
        "expires_at_ts": int(now + METADATA_SESSION_TTL_SECONDS),
    }


def _copy_working_file(src_path, session_id):
    root = _storage_dir()
    dst = root / f"{session_id}.mp3"
    shutil.copy2(src_path, dst)
    try:
        os.chmod(dst, 0o600)
    except Exception:
        pass
    return str(dst)


def _is_expired(session):
    return int(session.get("expires_at_ts") or 0) <= int(time.time())


def _touch_session_payload(session):
    session["updated_at_utc"] = _utc_iso_now()
    session["expires_at_ts"] = int(time.time() + METADATA_SESSION_TTL_SECONDS)
    return session


def validate_metadata_value(field, value):
    if field not in ("title", "artist"):
        return False, None, "metadata_invalid_field"
    raw = (value or "").strip()
    if not raw:
        return False, None, "metadata_invalid_empty"
    if _CONTROL_CHARS_RE.search(raw):
        return False, None, "metadata_invalid_control_chars"
    max_len = METADATA_TITLE_MAX_LEN if field == "title" else METADATA_ARTIST_MAX_LEN
    if len(raw) > max_len:
        return False, None, "metadata_invalid_too_long"
    return True, raw, None


def _cleanup_local_expired_maps():
    now = time.time()
    for key, expires in list(state.LOCAL_METADATA_INPUT.items()):
        if isinstance(expires, dict):
            exp_ts = float(expires.get("expires_at_ts") or 0)
            if exp_ts and exp_ts < now:
                state.LOCAL_METADATA_INPUT.pop(key, None)


def create_session_sync(user_id, src_file_path, title, artist, source_job_id=None):
    uid = int(user_id)
    client = _get_redis_client()
    prev = None
    with state.METADATA_LOCK:
        if client is not None:
            try:
                prev = client.get(_user_active_key(uid))
                if isinstance(prev, bytes):
                    prev = prev.decode("utf-8")
            except RedisError as e:
                _log_redis_issue(f"Redis metadata active read failed: {type(e).__name__}: {e}")
        else:
            prev = state.LOCAL_METADATA_USER_ACTIVE.get(uid)
    if prev:
        close_session_sync(prev, reason="replaced")

    with state.METADATA_LOCK:
        session = _make_session_payload(uid, src_file_path, title, artist, source_job_id=source_job_id)
        session["file_path"] = _copy_working_file(src_file_path, session["session_id"])
        _touch_session_payload(session)

        if client is not None:
            try:
                pipe = client.pipeline()
                payload = json.dumps(session, ensure_ascii=False)
                pipe.set(_session_key(session["session_id"]), payload, ex=int(METADATA_SESSION_TTL_SECONDS))
                pipe.set(_user_active_key(uid), session["session_id"], ex=int(METADATA_SESSION_TTL_SECONDS))
                pipe.zadd(_expiry_zset_key(), {session["session_id"]: float(session["expires_at_ts"])})
                pipe.execute()
            except RedisError as e:
                _log_redis_issue(f"Redis metadata create failed: {type(e).__name__}: {e}")
                state.LOCAL_METADATA_SESSIONS[session["session_id"]] = dict(session)
                state.LOCAL_METADATA_USER_ACTIVE[uid] = session["session_id"]
        else:
            state.LOCAL_METADATA_SESSIONS[session["session_id"]] = dict(session)
            state.LOCAL_METADATA_USER_ACTIVE[uid] = session["session_id"]

    log_event(
        "metadata.edit.started",
        level="INFO",
        user_id=uid,
        session_id=session["session_id"],
        source_job_id=source_job_id,
    )
    return dict(session)


async def create_session(user_id, src_file_path, title, artist, source_job_id=None):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, create_session_sync, user_id, src_file_path, title, artist, source_job_id)


def _load_session_locked(client, session_id):
    sid = str(session_id)
    if client is not None:
        try:
            raw = client.get(_session_key(sid))
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except RedisError as e:
            _log_redis_issue(f"Redis metadata read failed: {type(e).__name__}: {e}")
        except Exception as e:
            _log_redis_issue(f"Metadata payload decode failed: {type(e).__name__}: {e}")
            return None
    return dict(state.LOCAL_METADATA_SESSIONS.get(sid) or {}) or None


def _save_session_locked(client, session):
    sid = str(session["session_id"])
    uid = int(session["user_id"])
    _touch_session_payload(session)
    if client is not None:
        try:
            payload = json.dumps(session, ensure_ascii=False)
            pipe = client.pipeline()
            pipe.set(_session_key(sid), payload, ex=int(METADATA_SESSION_TTL_SECONDS))
            pipe.set(_user_active_key(uid), sid, ex=int(METADATA_SESSION_TTL_SECONDS))
            pipe.zadd(_expiry_zset_key(), {sid: float(session["expires_at_ts"])})
            pipe.execute()
            return
        except RedisError as e:
            _log_redis_issue(f"Redis metadata write failed: {type(e).__name__}: {e}")
    state.LOCAL_METADATA_SESSIONS[sid] = dict(session)
    state.LOCAL_METADATA_USER_ACTIVE[uid] = sid


def _close_session_locked(client, sid, session, reason):
    if not session:
        return False
    uid = int(session.get("user_id") or 0)
    file_path = session.get("file_path")
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.delete(_session_key(sid))
            pipe.delete(_user_active_key(uid))
            pipe.delete(_input_key(uid))
            pipe.zrem(_expiry_zset_key(), sid)
            pipe.execute()
        except RedisError as e:
            _log_redis_issue(f"Redis metadata close failed: {type(e).__name__}: {e}")
    state.LOCAL_METADATA_SESSIONS.pop(sid, None)
    if state.LOCAL_METADATA_USER_ACTIVE.get(uid) == sid:
        state.LOCAL_METADATA_USER_ACTIVE.pop(uid, None)
    state.LOCAL_METADATA_INPUT.pop(uid, None)

    if file_path:
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass
        except Exception:
            pass
    log_event("metadata.edit.closed", level="INFO", user_id=uid, session_id=sid, reason=reason)
    return True


def close_session_sync(session_id, reason="closed"):
    sid = str(session_id)
    client = _get_redis_client()
    with state.METADATA_LOCK:
        session = _load_session_locked(client, sid)
        return _close_session_locked(client, sid, session, reason)


async def close_session(session_id, reason="closed"):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, close_session_sync, session_id, reason)


def get_session_sync(session_id, touch=False):
    sid = str(session_id)
    client = _get_redis_client()
    with state.METADATA_LOCK:
        session = _load_session_locked(client, sid)
        if not session:
            return None
        if _is_expired(session):
            _close_session_locked(client, sid, session, reason="expired")
            return {"error": ERR_METADATA_SESSION_EXPIRED}
        if touch:
            _save_session_locked(client, session)
        return dict(session)


async def get_session(session_id, touch=False):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_session_sync, session_id, touch)


def set_input_mode_sync(user_id, session_id, field):
    uid = int(user_id)
    sid = str(session_id)
    if field not in ("title", "artist"):
        return False
    client = _get_redis_client()
    payload = {
        "session_id": sid,
        "field": field,
        "expires_at_ts": int(time.time() + METADATA_SESSION_TTL_SECONDS),
    }
    with state.METADATA_LOCK:
        if client is not None:
            try:
                client.set(_input_key(uid), json.dumps(payload, ensure_ascii=False), ex=int(METADATA_SESSION_TTL_SECONDS))
            except RedisError as e:
                _log_redis_issue(f"Redis metadata input set failed: {type(e).__name__}: {e}")
                state.LOCAL_METADATA_INPUT[uid] = dict(payload)
        else:
            state.LOCAL_METADATA_INPUT[uid] = dict(payload)
    return True


def clear_input_mode_sync(user_id):
    uid = int(user_id)
    client = _get_redis_client()
    with state.METADATA_LOCK:
        if client is not None:
            try:
                client.delete(_input_key(uid))
            except RedisError as e:
                _log_redis_issue(f"Redis metadata input clear failed: {type(e).__name__}: {e}")
        state.LOCAL_METADATA_INPUT.pop(uid, None)


def get_input_mode_sync(user_id):
    uid = int(user_id)
    client = _get_redis_client()
    with state.METADATA_LOCK:
        if client is not None:
            try:
                raw = client.get(_input_key(uid))
                if raw:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        return data
            except RedisError as e:
                _log_redis_issue(f"Redis metadata input read failed: {type(e).__name__}: {e}")
            except Exception:
                return None
        _cleanup_local_expired_maps()
        payload = state.LOCAL_METADATA_INPUT.get(uid)
        return dict(payload) if isinstance(payload, dict) else None


def update_field_sync(session_id, field, value):
    sid = str(session_id)
    ok, normalized, err_key = validate_metadata_value(field, value)
    if not ok:
        return {"ok": False, "error_code": ERR_METADATA_INVALID_INPUT, "error_key": err_key}
    client = _get_redis_client()
    with state.METADATA_LOCK:
        session = _load_session_locked(client, sid)
        if not session:
            return {"ok": False, "error_code": ERR_METADATA_SESSION_EXPIRED}
        if _is_expired(session):
            _close_session_locked(client, sid, session, reason="expired")
            return {"ok": False, "error_code": ERR_METADATA_SESSION_EXPIRED}
        session[f"{field}_new"] = normalized
        _save_session_locked(client, session)
    return {"ok": True, "session": dict(session)}


def _effective_title(session):
    title = session.get("title_new")
    if title is None:
        title = session.get("title_original")
    return str(title or "")


def _effective_artist(session):
    artist = session.get("artist_new")
    if artist is None:
        artist = session.get("artist_original")
    return str(artist or "")


def has_changes(session):
    if not isinstance(session, dict):
        return False
    return (session.get("title_new") is not None) or (session.get("artist_new") is not None)


def apply_changes_sync(session_id):
    sid = str(session_id)
    client = _get_redis_client()
    with state.METADATA_LOCK:
        session = _load_session_locked(client, sid)
        if not session:
            return {"ok": False, "error_code": ERR_METADATA_SESSION_EXPIRED}
        if _is_expired(session):
            _close_session_locked(client, sid, session, reason="expired")
            return {"ok": False, "error_code": ERR_METADATA_SESSION_EXPIRED}
        if not has_changes(session):
            return {"ok": False, "error_code": ERR_METADATA_INVALID_INPUT, "error_key": "metadata_no_changes"}
        source_path = session.get("file_path")
        if not source_path or not os.path.exists(source_path):
            _close_session_locked(client, sid, session, reason="file_missing")
            return {"ok": False, "error_code": ERR_METADATA_SESSION_EXPIRED}
        ffmpeg_bin = resolve_ffmpeg_path()
        if not ffmpeg_bin:
            return {"ok": False, "error_code": "E_FFMPEG_MISSING"}
        out_path = str(Path(source_path).with_suffix(".edited.mp3"))
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            source_path,
            "-map",
            "0",
            "-c",
            "copy",
            "-metadata",
            f"title={_effective_title(session)}",
            "-metadata",
            f"artist={_effective_artist(session)}",
            out_path,
        ]
    try:
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=90)
        if completed.returncode != 0:
            return {"ok": False, "error_code": "E_METADATA_APPLY_FAILED", "error": completed.stderr[-1000:]}
        os.replace(out_path, source_path)
    except Exception as e:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return {"ok": False, "error_code": "E_METADATA_APPLY_FAILED", "error": str(e)}

    with state.METADATA_LOCK:
        session = _load_session_locked(client, sid)
        if not session:
            return {"ok": False, "error_code": ERR_METADATA_SESSION_EXPIRED}
        _save_session_locked(client, session)
    log_event("metadata.edit.applied", level="INFO", user_id=session["user_id"], session_id=sid)
    return {
        "ok": True,
        "session": dict(session),
        "file_path": source_path,
        "title": _effective_title(session),
        "artist": _effective_artist(session),
    }


async def apply_changes(session_id):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, apply_changes_sync, session_id)


def get_active_session_id_sync(user_id):
    uid = int(user_id)
    client = _get_redis_client()
    with state.METADATA_LOCK:
        if client is not None:
            try:
                raw = client.get(_user_active_key(uid))
                if raw is None:
                    return None
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                return str(raw)
            except RedisError as e:
                _log_redis_issue(f"Redis metadata active read failed: {type(e).__name__}: {e}")
        sid = state.LOCAL_METADATA_USER_ACTIVE.get(uid)
        return str(sid) if sid else None


def get_changed_summary(session):
    if not isinstance(session, dict):
        return {}
    return {
        "title": _effective_title(session),
        "artist": _effective_artist(session),
        "changed": has_changes(session),
    }


def expire_due_sessions_sync():
    expired = []
    now = time.time()
    client = _get_redis_client()
    with state.METADATA_LOCK:
        if client is not None:
            try:
                sids = client.zrangebyscore(_expiry_zset_key(), "-inf", now, start=0, num=100)
                for sid in sids:
                    sid_str = sid.decode("utf-8") if isinstance(sid, bytes) else str(sid)
                    raw = client.get(_session_key(sid_str))
                    session = None
                    if raw:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        try:
                            session = json.loads(raw)
                        except Exception:
                            session = None
                    uid = int((session or {}).get("user_id") or 0)
                    path = (session or {}).get("file_path")
                    pipe = client.pipeline()
                    pipe.delete(_session_key(sid_str))
                    if uid:
                        pipe.delete(_user_active_key(uid))
                        pipe.delete(_input_key(uid))
                    pipe.zrem(_expiry_zset_key(), sid_str)
                    pipe.execute()
                    if path:
                        try:
                            os.remove(path)
                        except Exception:
                            pass
                    if uid:
                        expired.append({"user_id": uid, "session_id": sid_str})
            except RedisError as e:
                _log_redis_issue(f"Redis metadata expiry sweep failed: {type(e).__name__}: {e}")

        for sid, session in list(state.LOCAL_METADATA_SESSIONS.items()):
            if int(session.get("expires_at_ts") or 0) > now:
                continue
            uid = int(session.get("user_id") or 0)
            path = session.get("file_path")
            state.LOCAL_METADATA_SESSIONS.pop(sid, None)
            if state.LOCAL_METADATA_USER_ACTIVE.get(uid) == sid:
                state.LOCAL_METADATA_USER_ACTIVE.pop(uid, None)
            state.LOCAL_METADATA_INPUT.pop(uid, None)
            if path:
                try:
                    os.remove(path)
                except Exception:
                    pass
            if uid:
                expired.append({"user_id": uid, "session_id": sid})
    for item in expired:
        log_event(
            "metadata.edit.expired",
            level="INFO",
            user_id=item["user_id"],
            session_id=item["session_id"],
        )
    return expired


async def expire_due_sessions():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, expire_due_sessions_sync)
