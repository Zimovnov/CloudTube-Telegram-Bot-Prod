import asyncio
import functools
import json
import re
import time
from datetime import datetime, timedelta, timezone

from app import state
from app.config import (
    PAYMENTS_ALERT_THRESHOLD,
    PAYMENTS_ALERT_WINDOW_SECONDS,
    PAYMENTS_ALLOW_INMEMORY_FALLBACK,
    PAYMENTS_DATABASE_URL,
    PAYMENTS_DB_CONNECT_TIMEOUT,
    PAYMENTS_DB_REQUIRED,
    PREMIUM_PERIOD_SECONDS,
)
from app.logging_utils import log_event

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:
    psycopg2 = None
    RealDictCursor = None


TABLE_USERS = "users"
TABLE_PRODUCTS = "products"
TABLE_ORDERS = "orders"
TABLE_PAYMENTS = "payments"
TABLE_REFUNDS = "refunds"
TABLE_AUDIT = "audit_log"
TABLE_ENTITLEMENTS = "subscription_entitlements"
TABLE_PAYMENT_SESSIONS = "payment_sessions"
TABLE_SCHEMA_MIGRATIONS = "schema_migrations"

PAYMENT_STATUSES = {"pending", "creating", "waiting_for_capture", "succeeded", "failed", "blocked", "canceled", "refunded", "invalid", "unknown"}
ORDER_STATUSES = {"pending", "succeeded", "failed", "blocked", "canceled", "refunded", "invalid", "unknown"}
REFUND_STATUSES = {"pending", "succeeded", "failed", "canceled", "unknown"}
ENTITLEMENT_PLANS = {"free", "premium_monthly", "premium_lifetime"}
ALERT_STATUSES = {"failed", "blocked", "canceled", "invalid"}
ACTIVE_SESSION_STATUSES = {"creating", "pending", "waiting_for_capture"}
PROVIDER_RE = re.compile(r"^[a-z0-9_][a-z0-9_:.-]{0,63}$")
MAX_METADATA_LEN = 64_000

_PAYMENTS_DB_ACTIVE = False


def _utc_now():
    return datetime.now(timezone.utc)


def _utc_now_iso():
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _metadata_to_text(metadata):
    if metadata is None:
        return None
    try:
        text = _json_dumps(metadata)
    except Exception:
        text = _json_dumps({"raw": str(metadata)})
    if len(text) > MAX_METADATA_LEN:
        text = text[:MAX_METADATA_LEN] + "...(truncated)"
    return text


def _text_to_metadata(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return {"raw": str(value)}


def _dt_to_iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value)


def _norm_provider(provider):
    text = str(provider or "").strip().lower()
    if not text or not PROVIDER_RE.fullmatch(text):
        raise ValueError("invalid_provider")
    return text


def _norm_currency(currency):
    if currency is None:
        return None
    text = str(currency).strip().upper()
    return text[:8] if text else None


def _norm_amount(amount_minor):
    if amount_minor is None:
        return None
    return int(amount_minor)


def _norm_status(value, allowed, fallback):
    text = str(value or "").strip().lower()
    return text if text in allowed else fallback


def _norm_plan(plan_type):
    text = str(plan_type or "").strip().lower()
    if text not in ENTITLEMENT_PLANS:
        raise ValueError("invalid_plan_type")
    return text


def _product_code(plan_type, amount_minor, currency):
    amount_part = "na" if amount_minor is None else str(int(amount_minor))
    curr_part = (currency or "NA").upper()
    return f"{str(plan_type).strip().lower()}:{curr_part}:{amount_part}"


def _order_status_for_payment(payment_status):
    if payment_status == "succeeded":
        return "succeeded"
    if payment_status in {"failed", "blocked", "canceled", "invalid"}:
        return payment_status
    if payment_status == "refunded":
        return "refunded"
    return "pending"


def _idempotency_key(provider, external_payment_id):
    return f"{provider}:{external_payment_id}"


def _db_ready():
    return bool(_PAYMENTS_DB_ACTIVE and PAYMENTS_DATABASE_URL and psycopg2 is not None)


def payments_store_is_ready():
    return _db_ready()


def _connect():
    return psycopg2.connect(PAYMENTS_DATABASE_URL, connect_timeout=int(PAYMENTS_DB_CONNECT_TIMEOUT))


def _require_db():
    if not _db_ready():
        raise RuntimeError("Payments PostgreSQL is not ready.")


def _assert_schema_ready(cur):
    required_tables = (
        TABLE_SCHEMA_MIGRATIONS,
        TABLE_USERS,
        TABLE_PRODUCTS,
        TABLE_ORDERS,
        TABLE_PAYMENTS,
        TABLE_REFUNDS,
        TABLE_AUDIT,
        TABLE_ENTITLEMENTS,
        TABLE_PAYMENT_SESSIONS,
    )
    missing = []
    for table_name in required_tables:
        cur.execute("SELECT to_regclass(%s) AS table_name", (f"public.{table_name}",))
        row = cur.fetchone()
        current = row["table_name"] if isinstance(row, dict) else row[0]
        if not current:
            missing.append(table_name)
    if missing:
        raise RuntimeError(
            "Payments schema is not ready. Run migrations first. Missing tables: "
            + ", ".join(missing)
        )


def init_payments_store_sync():
    global _PAYMENTS_DB_ACTIVE
    if not PAYMENTS_DATABASE_URL:
        _PAYMENTS_DB_ACTIVE = False
        if PAYMENTS_DB_REQUIRED or not PAYMENTS_ALLOW_INMEMORY_FALLBACK:
            raise RuntimeError("PAYMENTS_DATABASE_URL is empty.")
        log_event("payments.db.disabled", level="WARNING", mode="payments_off", reason="PAYMENTS_DATABASE_URL is empty")
        return False
    if psycopg2 is None:
        _PAYMENTS_DB_ACTIVE = False
        if PAYMENTS_DB_REQUIRED or not PAYMENTS_ALLOW_INMEMORY_FALLBACK:
            raise RuntimeError("PostgreSQL client library is not installed.")
        log_event("payments.db.client_missing", level="WARNING", mode="payments_off", reason="client library missing")
        return False
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _assert_schema_ready(cur)
        _PAYMENTS_DB_ACTIVE = True
        log_event("payments.db.ready", level="INFO")
        return True
    except Exception as e:
        _PAYMENTS_DB_ACTIVE = False
        message = f"Payments PostgreSQL is unavailable ({type(e).__name__}: {e})."
        if PAYMENTS_DB_REQUIRED or not PAYMENTS_ALLOW_INMEMORY_FALLBACK:
            raise RuntimeError(message)
        log_event("payments.db.unavailable", level="WARNING", mode="payments_off", reason=message)
        return False


def _insert_audit(
    cur,
    *,
    event_type,
    provider=None,
    user_id=None,
    order_id=None,
    payment_id=None,
    refund_id=None,
    severity="INFO",
    message=None,
    details=None,
):
    cur.execute(
        f"""
        INSERT INTO {TABLE_AUDIT} (event_type, severity, provider, user_id, order_id, payment_id, refund_id, message, details_json, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """,
        (
            str(event_type),
            str(severity or "INFO"),
            str(provider) if provider else None,
            int(user_id) if user_id is not None else None,
            int(order_id) if order_id is not None else None,
            int(payment_id) if payment_id is not None else None,
            int(refund_id) if refund_id is not None else None,
            str(message) if message else None,
            _metadata_to_text(details),
        ),
    )


def _track_failed_status(provider, status):
    if status not in ALERT_STATUSES:
        return
    window = int(time.time() // int(PAYMENTS_ALERT_WINDOW_SECONDS))
    key = (str(provider), str(status), window)
    with state.PAYMENTS_LOCK:
        value = int(state.LOCAL_PAYMENT_ALERT_COUNTERS.get(key, 0)) + 1
        state.LOCAL_PAYMENT_ALERT_COUNTERS[key] = value
    if value >= int(PAYMENTS_ALERT_THRESHOLD):
        log_event(
            "payments.alert.status_spike",
            level="WARNING",
            provider=str(provider),
            status=str(status),
            count=value,
            threshold=int(PAYMENTS_ALERT_THRESHOLD),
            window_seconds=int(PAYMENTS_ALERT_WINDOW_SECONDS),
        )


def _upsert_user(cur, user_id):
    cur.execute(
        f"""
        INSERT INTO {TABLE_USERS} (telegram_user_id, created_at, updated_at)
        VALUES (%s, NOW(), NOW())
        ON CONFLICT (telegram_user_id) DO UPDATE SET updated_at = NOW()
        RETURNING id
        """,
        (int(user_id),),
    )
    return int(cur.fetchone()["id"])


def _upsert_product(cur, plan_type, amount_minor, currency):
    code = _product_code(plan_type, amount_minor, currency)
    cur.execute(
        f"""
        INSERT INTO {TABLE_PRODUCTS} (code, title, plan_type, amount_minor, currency, metadata_json, is_active, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, TRUE, NOW(), NOW())
        ON CONFLICT (code) DO UPDATE SET
            title = EXCLUDED.title,
            amount_minor = COALESCE(EXCLUDED.amount_minor, {TABLE_PRODUCTS}.amount_minor),
            currency = COALESCE(EXCLUDED.currency, {TABLE_PRODUCTS}.currency),
            updated_at = NOW(),
            is_active = TRUE
        RETURNING id
        """,
        (
            code,
            f"{plan_type} ({currency or 'N/A'})",
            plan_type,
            amount_minor,
            currency,
            _metadata_to_text({"source": "payments_store"}),
        ),
    )
    return int(cur.fetchone()["id"])


def _upsert_payment_bundle(
    cur,
    *,
    provider,
    external_payment_id,
    user_id,
    plan_type,
    amount_minor,
    currency,
    payment_status,
    order_status,
    metadata,
):
    user_db_id = _upsert_user(cur, user_id)
    product_db_id = _upsert_product(cur, plan_type, amount_minor, currency)
    idem = _idempotency_key(provider, external_payment_id)
    cur.execute(
        f"""
        INSERT INTO {TABLE_ORDERS} (
            user_id, product_id, provider, external_order_id, idempotency_key,
            amount_minor, currency, status, metadata_json, completed_at, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (idempotency_key) DO UPDATE SET
            user_id = EXCLUDED.user_id,
            product_id = EXCLUDED.product_id,
            amount_minor = COALESCE(EXCLUDED.amount_minor, {TABLE_ORDERS}.amount_minor),
            currency = COALESCE(EXCLUDED.currency, {TABLE_ORDERS}.currency),
            status = CASE
                WHEN {TABLE_ORDERS}.status = 'succeeded' THEN {TABLE_ORDERS}.status
                ELSE EXCLUDED.status
            END,
            metadata_json = COALESCE(EXCLUDED.metadata_json, {TABLE_ORDERS}.metadata_json),
            completed_at = CASE
                WHEN EXCLUDED.status = 'succeeded' AND {TABLE_ORDERS}.completed_at IS NULL THEN NOW()
                ELSE {TABLE_ORDERS}.completed_at
            END,
            updated_at = NOW()
        RETURNING id
        """,
        (
            user_db_id,
            product_db_id,
            provider,
            external_payment_id,
            idem,
            amount_minor,
            currency,
            order_status,
            _metadata_to_text(metadata),
            _utc_now() if order_status == "succeeded" else None,
        ),
    )
    order_db_id = int(cur.fetchone()["id"])
    telegram_charge = external_payment_id if provider == "telegram_stars" else None
    cur.execute(
        f"""
        INSERT INTO {TABLE_PAYMENTS} (
            order_id, provider, provider_payment_id, external_id, telegram_charge_id, idempotency_key,
            amount_minor, currency, status, is_processed, metadata_json, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, NOW(), NOW())
        ON CONFLICT (provider, provider_payment_id) DO UPDATE SET
            order_id = EXCLUDED.order_id,
            external_id = COALESCE(EXCLUDED.external_id, {TABLE_PAYMENTS}.external_id),
            telegram_charge_id = COALESCE(EXCLUDED.telegram_charge_id, {TABLE_PAYMENTS}.telegram_charge_id),
            amount_minor = COALESCE(EXCLUDED.amount_minor, {TABLE_PAYMENTS}.amount_minor),
            currency = COALESCE(EXCLUDED.currency, {TABLE_PAYMENTS}.currency),
            status = CASE
                WHEN {TABLE_PAYMENTS}.is_processed THEN {TABLE_PAYMENTS}.status
                ELSE EXCLUDED.status
            END,
            metadata_json = COALESCE(EXCLUDED.metadata_json, {TABLE_PAYMENTS}.metadata_json),
            updated_at = NOW()
        RETURNING id, is_processed
        """,
        (
            order_db_id,
            provider,
            external_payment_id,
            external_payment_id,
            telegram_charge,
            idem,
            amount_minor,
            currency,
            payment_status,
            _metadata_to_text(metadata),
        ),
    )
    row = cur.fetchone()
    return order_db_id, int(row["id"]), bool(row["is_processed"])


def _fetch_payment_public(cur, provider, external_payment_id):
    cur.execute(
        f"""
        SELECT
            p.id AS payment_id,
            p.order_id AS order_id,
            p.provider AS provider,
            p.provider_payment_id AS external_payment_id,
            p.external_id AS external_id,
            p.telegram_charge_id AS telegram_charge_id,
            p.idempotency_key AS idempotency_key,
            u.telegram_user_id AS user_id,
            pr.plan_type AS plan_type,
            p.amount_minor AS amount_minor,
            p.currency AS currency,
            p.status AS status,
            o.status AS order_status,
            p.is_processed AS is_processed,
            p.processed_at AS processed_at,
            p.invalid_reason AS invalid_reason,
            p.metadata_json AS metadata_json,
            p.created_at AS created_at,
            p.updated_at AS updated_at,
            o.completed_at AS completed_at
        FROM {TABLE_PAYMENTS} p
        JOIN {TABLE_ORDERS} o ON o.id = p.order_id
        JOIN {TABLE_USERS} u ON u.id = o.user_id
        JOIN {TABLE_PRODUCTS} pr ON pr.id = o.product_id
        WHERE p.provider = %s AND p.provider_payment_id = %s
        LIMIT 1
        """,
        (provider, external_payment_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    payload = dict(row)
    payload["status"] = _norm_status(payload.get("status"), PAYMENT_STATUSES, "unknown")
    payload["order_status"] = _norm_status(payload.get("order_status"), ORDER_STATUSES, "unknown")
    payload["currency"] = _norm_currency(payload.get("currency"))
    payload["is_processed"] = bool(payload.get("is_processed"))
    payload["metadata_json"] = _text_to_metadata(payload.get("metadata_json"))
    for key in ("created_at", "updated_at", "processed_at", "completed_at"):
        payload[key] = _dt_to_iso(payload.get(key))
    return payload


def register_pending_payment_sync(
    provider,
    external_payment_id,
    *,
    user_id,
    plan_type,
    amount_minor=None,
    currency=None,
    status="pending",
    metadata=None,
):
    _require_db()
    provider = _norm_provider(provider)
    payment_id = str(external_payment_id).strip()
    if not payment_id:
        raise ValueError("external_payment_id_required")
    payment_status = _norm_status(status, PAYMENT_STATUSES, "pending")
    order_status = _order_status_for_payment(payment_status)
    amount = _norm_amount(amount_minor)
    curr = _norm_currency(currency)
    plan = _norm_plan(plan_type)
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            order_db_id, payment_db_id, processed = _upsert_payment_bundle(
                cur,
                provider=provider,
                external_payment_id=payment_id,
                user_id=int(user_id),
                plan_type=plan,
                amount_minor=amount,
                currency=curr,
                payment_status=payment_status,
                order_status=order_status,
                metadata=metadata,
            )
            _insert_audit(
                cur,
                event_type="payment.pending_registered",
                provider=provider,
                user_id=user_id,
                order_id=order_db_id,
                payment_id=payment_db_id,
                message="Pending payment registered",
                details={"status": payment_status, "is_processed": processed},
            )
            return _fetch_payment_public(cur, provider, payment_id)


def update_payment_status_sync(provider, external_payment_id, status, metadata=None, invalid_reason=None):
    _require_db()
    provider = _norm_provider(provider)
    payment_id = str(external_payment_id).strip()
    if not payment_id:
        return None
    payment_status = _norm_status(status, PAYMENT_STATUSES, "unknown")
    order_status = _order_status_for_payment(payment_status)
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT id, order_id FROM {TABLE_PAYMENTS} WHERE provider = %s AND provider_payment_id = %s FOR UPDATE",
                (provider, payment_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            payment_db_id = int(row["id"])
            order_db_id = int(row["order_id"])
            cur.execute(
                f"""
                UPDATE {TABLE_PAYMENTS}
                SET status = %s,
                    invalid_reason = COALESCE(%s, invalid_reason),
                    metadata_json = COALESCE(%s, metadata_json),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (payment_status, invalid_reason, _metadata_to_text(metadata), payment_db_id),
            )
            cur.execute(
                f"""
                UPDATE {TABLE_ORDERS}
                SET status = %s,
                    metadata_json = COALESCE(%s, metadata_json),
                    updated_at = NOW(),
                    completed_at = CASE WHEN %s = 'succeeded' AND completed_at IS NULL THEN NOW() ELSE completed_at END
                WHERE id = %s
                """,
                (order_status, _metadata_to_text(metadata), order_status, order_db_id),
            )
            _insert_audit(
                cur,
                event_type="payment.status_updated",
                provider=provider,
                order_id=order_db_id,
                payment_id=payment_db_id,
                message="Payment status updated",
                details={"status": payment_status, "invalid_reason": invalid_reason},
            )
            _track_failed_status(provider, payment_status)
            return _fetch_payment_public(cur, provider, payment_id)


def mark_payment_invalid_sync(provider, external_payment_id, *, metadata=None, invalid_reason=None):
    return update_payment_status_sync(provider, external_payment_id, "invalid", metadata=metadata, invalid_reason=invalid_reason)


def get_payment_sync(provider, external_payment_id):
    _require_db()
    provider = _norm_provider(provider)
    payment_id = str(external_payment_id).strip()
    if not payment_id:
        return None
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            return _fetch_payment_public(cur, provider, payment_id)


def _fetch_entitlement_row(cur, user_id):
    cur.execute(
        f"""
        SELECT user_id, plan_type, expires_at_utc, updated_at, source_provider, source_payment_id, version
        FROM {TABLE_ENTITLEMENTS}
        WHERE user_id = %s
        LIMIT 1
        """,
        (int(user_id),),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _effective_entitlement_from_row(row, user_id):
    if not row:
        return {
            "user_id": int(user_id),
            "plan_type": "free",
            "plan_expires_at_utc": None,
            "source_provider": None,
            "source_payment_id": None,
            "version": 0,
            "updated_at": None,
        }
    plan_type = _norm_plan(row.get("plan_type"))
    expires_at = row.get("expires_at_utc")
    expires_dt = expires_at.astimezone(timezone.utc) if isinstance(expires_at, datetime) else None
    if plan_type == "premium_monthly" and (expires_dt is None or expires_dt <= _utc_now()):
        return {
            "user_id": int(user_id),
            "plan_type": "free",
            "plan_expires_at_utc": None,
            "source_provider": row.get("source_provider"),
            "source_payment_id": row.get("source_payment_id"),
            "version": int(row.get("version") or 0),
            "updated_at": _dt_to_iso(row.get("updated_at")),
        }
    return {
        "user_id": int(user_id),
        "plan_type": plan_type,
        "plan_expires_at_utc": _dt_to_iso(expires_dt),
        "source_provider": row.get("source_provider"),
        "source_payment_id": row.get("source_payment_id"),
        "version": int(row.get("version") or 0),
        "updated_at": _dt_to_iso(row.get("updated_at")),
    }


def get_effective_entitlement_sync(user_id):
    _require_db()
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = _fetch_entitlement_row(cur, user_id)
            return _effective_entitlement_from_row(row, user_id)


def _apply_entitlement_update(cur, *, user_id, plan_type, source_provider, source_payment_id):
    user_id = int(user_id)
    plan = _norm_plan(plan_type)
    row = _fetch_entitlement_row(cur, user_id)
    current = _effective_entitlement_from_row(row, user_id)
    now = _utc_now()
    if current.get("plan_type") == "premium_lifetime" and plan == "premium_monthly":
        return current
    if plan == "free":
        cur.execute(f"DELETE FROM {TABLE_ENTITLEMENTS} WHERE user_id = %s", (user_id,))
        return _effective_entitlement_from_row(None, user_id)
    if plan == "premium_lifetime":
        cur.execute(
            f"""
            INSERT INTO {TABLE_ENTITLEMENTS} (user_id, plan_type, expires_at_utc, updated_at, source_provider, source_payment_id, version)
            VALUES (%s, %s, NULL, NOW(), %s, %s, 1)
            ON CONFLICT (user_id) DO UPDATE SET
                plan_type = EXCLUDED.plan_type,
                expires_at_utc = NULL,
                updated_at = NOW(),
                source_provider = EXCLUDED.source_provider,
                source_payment_id = EXCLUDED.source_payment_id,
                version = {TABLE_ENTITLEMENTS}.version + 1
            """,
            (user_id, plan, source_provider, source_payment_id),
        )
        return _effective_entitlement_from_row(_fetch_entitlement_row(cur, user_id), user_id)
    current_expires = row.get("expires_at_utc") if row else None
    current_expires = current_expires.astimezone(timezone.utc) if isinstance(current_expires, datetime) else None
    base = current_expires if current.get("plan_type") == "premium_monthly" and current_expires and current_expires > now else now
    new_expires = base + timedelta(seconds=int(PREMIUM_PERIOD_SECONDS))
    cur.execute(
        f"""
        INSERT INTO {TABLE_ENTITLEMENTS} (user_id, plan_type, expires_at_utc, updated_at, source_provider, source_payment_id, version)
        VALUES (%s, %s, %s, NOW(), %s, %s, 1)
        ON CONFLICT (user_id) DO UPDATE SET
            plan_type = EXCLUDED.plan_type,
            expires_at_utc = EXCLUDED.expires_at_utc,
            updated_at = NOW(),
            source_provider = EXCLUDED.source_provider,
            source_payment_id = EXCLUDED.source_payment_id,
            version = {TABLE_ENTITLEMENTS}.version + 1
        """,
        (user_id, plan, new_expires, source_provider, source_payment_id),
    )
    return _effective_entitlement_from_row(_fetch_entitlement_row(cur, user_id), user_id)


def set_plan_entitlement_sync(user_id, plan_type, *, source_provider="admin", source_payment_id=None):
    _require_db()
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _upsert_user(cur, user_id)
            result = _apply_entitlement_update(
                cur,
                user_id=user_id,
                plan_type=plan_type,
                source_provider=source_provider,
                source_payment_id=source_payment_id,
            )
            _insert_audit(
                cur,
                event_type="entitlement.updated",
                provider=source_provider,
                user_id=user_id,
                message="Entitlement updated",
                details={"plan_type": result.get("plan_type"), "expires_at_utc": result.get("plan_expires_at_utc")},
            )
            return result


def finalize_verified_payment_sync(
    provider,
    external_payment_id,
    *,
    user_id,
    plan_type,
    amount_minor=None,
    currency=None,
    status="succeeded",
    metadata=None,
):
    _require_db()
    provider = _norm_provider(provider)
    payment_id = str(external_payment_id).strip()
    if not payment_id:
        raise ValueError("external_payment_id_required")
    plan = _norm_plan(plan_type)
    payment_status = _norm_status(status, PAYMENT_STATUSES, "succeeded")
    order_status = _order_status_for_payment(payment_status)
    amount = _norm_amount(amount_minor)
    curr = _norm_currency(currency)
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            order_db_id, payment_db_id, _ = _upsert_payment_bundle(
                cur,
                provider=provider,
                external_payment_id=payment_id,
                user_id=int(user_id),
                plan_type=plan,
                amount_minor=amount,
                currency=curr,
                payment_status=payment_status,
                order_status=order_status,
                metadata=metadata,
            )
            cur.execute(
                f"""
                SELECT
                    p.is_processed,
                    u.telegram_user_id AS stored_user_id,
                    pr.plan_type AS stored_plan_type
                FROM {TABLE_PAYMENTS} p
                JOIN {TABLE_ORDERS} o ON o.id = p.order_id
                JOIN {TABLE_USERS} u ON u.id = o.user_id
                JOIN {TABLE_PRODUCTS} pr ON pr.id = o.product_id
                WHERE p.provider = %s AND p.provider_payment_id = %s
                FOR UPDATE
                """,
                (provider, payment_id),
            )
            row = cur.fetchone()
            if not row:
                return False, None, _effective_entitlement_from_row(None, user_id)
            if int(row["stored_user_id"]) != int(user_id) or str(row["stored_plan_type"]) != plan:
                raise RuntimeError("payment_binding_mismatch")
            if bool(row["is_processed"]):
                _insert_audit(
                    cur,
                    event_type="payment.duplicate_ignored",
                    severity="WARNING",
                    provider=provider,
                    user_id=user_id,
                    order_id=order_db_id,
                    payment_id=payment_db_id,
                    message="Duplicate payment completion ignored",
                )
                return False, _fetch_payment_public(cur, provider, payment_id), _effective_entitlement_from_row(_fetch_entitlement_row(cur, user_id), user_id)
            cur.execute(
                f"""
                UPDATE {TABLE_PAYMENTS}
                SET status = %s,
                    is_processed = TRUE,
                    processed_at = COALESCE(processed_at, NOW()),
                    amount_minor = COALESCE(%s, amount_minor),
                    currency = COALESCE(%s, currency),
                    invalid_reason = NULL,
                    metadata_json = COALESCE(%s, metadata_json),
                    updated_at = NOW()
                WHERE provider = %s AND provider_payment_id = %s
                """,
                (payment_status, amount, curr, _metadata_to_text(metadata), provider, payment_id),
            )
            cur.execute(
                f"""
                UPDATE {TABLE_ORDERS}
                SET status = %s,
                    completed_at = CASE WHEN %s = 'succeeded' THEN COALESCE(completed_at, NOW()) ELSE completed_at END,
                    metadata_json = COALESCE(%s, metadata_json),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (order_status, order_status, _metadata_to_text(metadata), order_db_id),
            )
            entitlement = _apply_entitlement_update(
                cur,
                user_id=user_id,
                plan_type=plan,
                source_provider=provider,
                source_payment_id=payment_id,
            )
            _insert_audit(
                cur,
                event_type="payment.completed",
                provider=provider,
                user_id=user_id,
                order_id=order_db_id,
                payment_id=payment_db_id,
                message="Payment processed and entitlement finalized",
                details={"status": payment_status, "plan_type": entitlement.get("plan_type"), "expires_at_utc": entitlement.get("plan_expires_at_utc")},
            )
            _track_failed_status(provider, payment_status)
            return True, _fetch_payment_public(cur, provider, payment_id), entitlement


def complete_payment_once_sync(
    provider,
    external_payment_id,
    *,
    user_id,
    plan_type,
    amount_minor=None,
    currency=None,
    status="succeeded",
    metadata=None,
):
    processed, payment, _ = finalize_verified_payment_sync(
        provider,
        external_payment_id,
        user_id=user_id,
        plan_type=plan_type,
        amount_minor=amount_minor,
        currency=currency,
        status=status,
        metadata=metadata,
    )
    return processed, payment


def _fetch_payment_session_public(cur, session_key):
    cur.execute(
        f"""
        SELECT session_key, provider, user_id, plan_type, payment_id, payment_url, status, expires_at_utc, created_at, updated_at
        FROM {TABLE_PAYMENT_SESSIONS}
        WHERE session_key = %s
        LIMIT 1
        """,
        (str(session_key),),
    )
    row = cur.fetchone()
    if not row:
        return None
    payload = dict(row)
    for key in ("expires_at_utc", "created_at", "updated_at"):
        payload[key] = _dt_to_iso(payload.get(key))
    return payload


def acquire_payment_session_sync(session_key, *, provider, user_id, plan_type, ttl_seconds):
    _require_db()
    provider = _norm_provider(provider)
    plan = _norm_plan(plan_type)
    ttl = max(30, int(ttl_seconds))
    expires_at = _utc_now() + timedelta(seconds=ttl)
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {TABLE_PAYMENT_SESSIONS} WHERE session_key = %s FOR UPDATE",
                (str(session_key),),
            )
            row = cur.fetchone()
            if row:
                row = dict(row)
                row_expires = row.get("expires_at_utc")
                row_expires = row_expires.astimezone(timezone.utc) if isinstance(row_expires, datetime) else None
                if row_expires and row_expires > _utc_now() and str(row.get("status")) in ACTIVE_SESSION_STATUSES:
                    payload = _fetch_payment_session_public(cur, session_key)
                    payload["action"] = "reuse" if row.get("payment_id") else "wait"
                    return payload
                cur.execute(
                    f"""
                    UPDATE {TABLE_PAYMENT_SESSIONS}
                    SET provider = %s,
                        user_id = %s,
                        plan_type = %s,
                        payment_id = NULL,
                        payment_url = NULL,
                        status = 'creating',
                        expires_at_utc = %s,
                        updated_at = NOW()
                    WHERE session_key = %s
                    """,
                    (provider, int(user_id), plan, expires_at, str(session_key)),
                )
            else:
                cur.execute(
                    f"""
                    INSERT INTO {TABLE_PAYMENT_SESSIONS} (
                        session_key, provider, user_id, plan_type, payment_id, payment_url, status, expires_at_utc, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, NULL, NULL, 'creating', %s, NOW(), NOW())
                    """,
                    (str(session_key), provider, int(user_id), plan, expires_at),
                )
            payload = _fetch_payment_session_public(cur, session_key)
            payload["action"] = "create"
            return payload


def attach_payment_session_sync(session_key, *, payment_id, payment_url, status, ttl_seconds):
    _require_db()
    ttl = max(30, int(ttl_seconds))
    expires_at = _utc_now() + timedelta(seconds=ttl)
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE {TABLE_PAYMENT_SESSIONS}
                SET payment_id = %s,
                    payment_url = %s,
                    status = %s,
                    expires_at_utc = %s,
                    updated_at = NOW()
                WHERE session_key = %s
                """,
                (str(payment_id), str(payment_url) if payment_url else None, _norm_status(status, PAYMENT_STATUSES, "pending"), expires_at, str(session_key)),
            )
            return _fetch_payment_session_public(cur, session_key)


def expire_payment_session_sync(session_key):
    _require_db()
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE {TABLE_PAYMENT_SESSIONS}
                SET status = 'canceled',
                    expires_at_utc = NOW(),
                    updated_at = NOW()
                WHERE session_key = %s
                """,
                (str(session_key),),
            )
            return _fetch_payment_session_public(cur, session_key)


def get_payment_session_sync(session_key):
    _require_db()
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            return _fetch_payment_session_public(cur, session_key)


def list_reconcilable_payments_sync(provider="robokassa", limit=50):
    _require_db()
    provider = _norm_provider(provider)
    limit = max(1, int(limit))
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    p.provider_payment_id AS payment_id,
                    p.status AS status,
                    p.created_at AS created_at,
                    u.telegram_user_id AS user_id,
                    pr.plan_type AS plan_type
                FROM {TABLE_PAYMENTS} p
                JOIN {TABLE_ORDERS} o ON o.id = p.order_id
                JOIN {TABLE_USERS} u ON u.id = o.user_id
                JOIN {TABLE_PRODUCTS} pr ON pr.id = o.product_id
                WHERE p.provider = %s
                  AND p.is_processed = FALSE
                  AND p.status IN ('pending', 'waiting_for_capture')
                ORDER BY p.created_at ASC
                LIMIT %s
                """,
                (provider, limit),
            )
            items = []
            for row in cur.fetchall():
                payload = dict(row)
                payload["created_at"] = _dt_to_iso(payload.get("created_at"))
                items.append(payload)
            return items


def _fetch_refund_public(cur, idempotency_key):
    cur.execute(
        f"""
        SELECT id, payment_id, provider, provider_refund_id, idempotency_key, amount_minor, currency, status, reason, metadata_json, processed_at, created_at, updated_at
        FROM {TABLE_REFUNDS}
        WHERE idempotency_key = %s
        LIMIT 1
        """,
        (str(idempotency_key),),
    )
    row = cur.fetchone()
    if not row:
        return None
    payload = dict(row)
    payload["metadata_json"] = _text_to_metadata(payload.get("metadata_json"))
    for field in ("created_at", "updated_at", "processed_at"):
        payload[field] = _dt_to_iso(payload.get(field))
    return payload


def register_refund_pending_sync(
    provider,
    provider_refund_id,
    *,
    payment_provider,
    payment_external_id,
    amount_minor,
    currency,
    status="pending",
    reason="",
    metadata=None,
    idempotency_key=None,
):
    _require_db()
    provider = _norm_provider(provider)
    refund_status = _norm_status(status, REFUND_STATUSES, "pending")
    payment = get_payment_sync(payment_provider, payment_external_id)
    if not payment:
        raise ValueError("payment_not_found")
    idem = str(idempotency_key or f"{provider}:{provider_refund_id or time.time_ns()}").strip()
    if not idem:
        raise ValueError("refund_idempotency_key_required")
    amount = int(amount_minor)
    if amount <= 0:
        raise ValueError("invalid_refund_amount")
    curr = _norm_currency(currency) or "RUB"
    payment_amount = payment.get("amount_minor")
    if payment_amount is not None and amount > int(payment_amount):
        raise ValueError("refund_amount_exceeds_payment")
    payment_currency = _norm_currency(payment.get("currency"))
    if payment_currency and curr != payment_currency:
        raise ValueError("refund_currency_mismatch")
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                INSERT INTO {TABLE_REFUNDS} (payment_id, provider, provider_refund_id, idempotency_key, amount_minor, currency, status, reason, metadata_json, processed_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (idempotency_key) DO UPDATE SET
                    status = EXCLUDED.status,
                    reason = COALESCE(EXCLUDED.reason, {TABLE_REFUNDS}.reason),
                    metadata_json = COALESCE(EXCLUDED.metadata_json, {TABLE_REFUNDS}.metadata_json),
                    processed_at = CASE
                        WHEN EXCLUDED.status = 'succeeded' THEN COALESCE({TABLE_REFUNDS}.processed_at, NOW())
                        ELSE {TABLE_REFUNDS}.processed_at
                    END,
                    updated_at = NOW()
                RETURNING *
                """,
                (
                    int(payment["payment_id"]),
                    provider,
                    str(provider_refund_id) if provider_refund_id else None,
                    idem,
                    amount,
                    curr,
                    refund_status,
                    str(reason or "") or None,
                    _metadata_to_text(metadata),
                    _utc_now() if refund_status == "succeeded" else None,
                ),
            )
            row = dict(cur.fetchone())
            _insert_audit(
                cur,
                event_type="refund.pending_registered",
                provider=provider,
                user_id=payment.get("user_id"),
                payment_id=payment.get("payment_id"),
                refund_id=row.get("id"),
                message="Refund registered",
                details={"status": refund_status},
            )
            return _fetch_refund_public(cur, idem)


def update_refund_status_sync(idempotency_key, status, metadata=None):
    _require_db()
    idem = str(idempotency_key or "").strip()
    if not idem:
        return None
    refund_status = _norm_status(status, REFUND_STATUSES, "unknown")
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT id, payment_id FROM {TABLE_REFUNDS} WHERE idempotency_key = %s FOR UPDATE",
                (idem,),
            )
            row = cur.fetchone()
            if not row:
                return None
            refund_id = int(row["id"])
            payment_id = int(row["payment_id"])
            cur.execute(
                f"""
                UPDATE {TABLE_REFUNDS}
                SET status = %s,
                    metadata_json = COALESCE(%s, metadata_json),
                    processed_at = CASE WHEN %s = 'succeeded' THEN COALESCE(processed_at, NOW()) ELSE processed_at END,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (refund_status, _metadata_to_text(metadata), refund_status, refund_id),
            )
            _insert_audit(
                cur,
                event_type="refund.status_updated",
                payment_id=payment_id,
                refund_id=refund_id,
                message="Refund status updated",
                details={"status": refund_status},
            )
            return _fetch_refund_public(cur, idem)


def get_refund_sync(idempotency_key):
    _require_db()
    idem = str(idempotency_key or "").strip()
    if not idem:
        return None
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            return _fetch_refund_public(cur, idem)


async def register_pending_payment(provider, external_payment_id, *, user_id, plan_type, amount_minor=None, currency=None, status="pending", metadata=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(
        register_pending_payment_sync,
        provider,
        external_payment_id,
        user_id=user_id,
        plan_type=plan_type,
        amount_minor=amount_minor,
        currency=currency,
        status=status,
        metadata=metadata,
    )
    return await loop.run_in_executor(None, fn)


async def update_payment_status(provider, external_payment_id, status, metadata=None, invalid_reason=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(update_payment_status_sync, provider, external_payment_id, status, metadata, invalid_reason)
    return await loop.run_in_executor(None, fn)


async def mark_payment_invalid(provider, external_payment_id, *, metadata=None, invalid_reason=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(mark_payment_invalid_sync, provider, external_payment_id, metadata=metadata, invalid_reason=invalid_reason)
    return await loop.run_in_executor(None, fn)


async def get_payment(provider, external_payment_id):
    loop = asyncio.get_running_loop()
    fn = functools.partial(get_payment_sync, provider, external_payment_id)
    return await loop.run_in_executor(None, fn)


async def finalize_verified_payment(provider, external_payment_id, *, user_id, plan_type, amount_minor=None, currency=None, status="succeeded", metadata=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(
        finalize_verified_payment_sync,
        provider,
        external_payment_id,
        user_id=user_id,
        plan_type=plan_type,
        amount_minor=amount_minor,
        currency=currency,
        status=status,
        metadata=metadata,
    )
    return await loop.run_in_executor(None, fn)


async def complete_payment_once(provider, external_payment_id, *, user_id, plan_type, amount_minor=None, currency=None, status="succeeded", metadata=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(
        complete_payment_once_sync,
        provider,
        external_payment_id,
        user_id=user_id,
        plan_type=plan_type,
        amount_minor=amount_minor,
        currency=currency,
        status=status,
        metadata=metadata,
    )
    return await loop.run_in_executor(None, fn)


async def get_effective_entitlement(user_id):
    loop = asyncio.get_running_loop()
    fn = functools.partial(get_effective_entitlement_sync, user_id)
    return await loop.run_in_executor(None, fn)


async def set_plan_entitlement(user_id, plan_type, *, source_provider="admin", source_payment_id=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(
        set_plan_entitlement_sync,
        user_id,
        plan_type,
        source_provider=source_provider,
        source_payment_id=source_payment_id,
    )
    return await loop.run_in_executor(None, fn)


async def acquire_payment_session(session_key, *, provider, user_id, plan_type, ttl_seconds):
    loop = asyncio.get_running_loop()
    fn = functools.partial(
        acquire_payment_session_sync,
        session_key,
        provider=provider,
        user_id=user_id,
        plan_type=plan_type,
        ttl_seconds=ttl_seconds,
    )
    return await loop.run_in_executor(None, fn)


async def attach_payment_session(session_key, *, payment_id, payment_url, status, ttl_seconds):
    loop = asyncio.get_running_loop()
    fn = functools.partial(
        attach_payment_session_sync,
        session_key,
        payment_id=payment_id,
        payment_url=payment_url,
        status=status,
        ttl_seconds=ttl_seconds,
    )
    return await loop.run_in_executor(None, fn)


async def expire_payment_session(session_key):
    loop = asyncio.get_running_loop()
    fn = functools.partial(expire_payment_session_sync, session_key)
    return await loop.run_in_executor(None, fn)


async def get_payment_session(session_key):
    loop = asyncio.get_running_loop()
    fn = functools.partial(get_payment_session_sync, session_key)
    return await loop.run_in_executor(None, fn)


async def list_reconcilable_payments(provider="robokassa", limit=50):
    loop = asyncio.get_running_loop()
    fn = functools.partial(list_reconcilable_payments_sync, provider=provider, limit=limit)
    return await loop.run_in_executor(None, fn)


async def register_refund_pending(provider, provider_refund_id, *, payment_provider, payment_external_id, amount_minor, currency, status="pending", reason="", metadata=None, idempotency_key=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(
        register_refund_pending_sync,
        provider,
        provider_refund_id,
        payment_provider=payment_provider,
        payment_external_id=payment_external_id,
        amount_minor=amount_minor,
        currency=currency,
        status=status,
        reason=reason,
        metadata=metadata,
        idempotency_key=idempotency_key,
    )
    return await loop.run_in_executor(None, fn)


async def update_refund_status(idempotency_key, status, metadata=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(update_refund_status_sync, idempotency_key, status, metadata)
    return await loop.run_in_executor(None, fn)


async def get_refund(idempotency_key):
    loop = asyncio.get_running_loop()
    fn = functools.partial(get_refund_sync, idempotency_key)
    return await loop.run_in_executor(None, fn)
