import asyncio
import functools
import json
import re
import time
from datetime import datetime, timezone

from app import state
from app.config import (
    PAYMENTS_ALERT_THRESHOLD,
    PAYMENTS_ALERT_WINDOW_SECONDS,
    PAYMENTS_DATABASE_URL,
    PAYMENTS_DB_CONNECT_TIMEOUT,
    PAYMENTS_DB_REQUIRED,
)
from app.logging_utils import log_event

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:
    psycopg2 = None
    RealDictCursor = None


LEGACY_TABLE = "payment_transactions"
TABLE_USERS = "users"
TABLE_PRODUCTS = "products"
TABLE_ORDERS = "orders"
TABLE_PAYMENTS = "payments"
TABLE_REFUNDS = "refunds"
TABLE_AUDIT = "audit_log"

PAYMENT_STATUSES = {"pending", "waiting_for_capture", "succeeded", "failed", "blocked", "canceled", "refunded", "unknown"}
ORDER_STATUSES = {"pending", "succeeded", "failed", "blocked", "canceled", "refunded", "unknown"}
REFUND_STATUSES = {"pending", "succeeded", "failed", "canceled", "unknown"}
ALERT_STATUSES = {"failed", "blocked", "canceled"}
PROVIDER_RE = re.compile(r"^[a-z0-9_][a-z0-9_:.-]{0,63}$")
MAX_METADATA_LEN = 64_000

_PAYMENTS_DB_ACTIVE = False


def _utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _clone(value):
    return json.loads(_json_dumps(value))


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


def _product_code(plan_type, amount_minor, currency):
    amount_part = "na" if amount_minor is None else str(int(amount_minor))
    curr_part = (currency or "NA").upper()
    return f"{str(plan_type).strip().lower()}:{curr_part}:{amount_part}"


def _order_status_for_payment(payment_status):
    if payment_status == "succeeded":
        return "succeeded"
    if payment_status in {"failed", "blocked", "canceled"}:
        return payment_status
    if payment_status == "refunded":
        return "refunded"
    return "pending"


def _idempotency_key(provider, external_payment_id):
    return f"{provider}:{external_payment_id}"


def _db_ready():
    return bool(_PAYMENTS_DB_ACTIVE and PAYMENTS_DATABASE_URL and psycopg2 is not None)


def _connect():
    return psycopg2.connect(PAYMENTS_DATABASE_URL, connect_timeout=int(PAYMENTS_DB_CONNECT_TIMEOUT))


def _ensure_schema(cur):
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_USERS} (
            id BIGSERIAL PRIMARY KEY,
            telegram_user_id BIGINT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS {TABLE_PRODUCTS} (
            id BIGSERIAL PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            plan_type TEXT NOT NULL,
            amount_minor BIGINT NULL,
            currency TEXT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            metadata_json TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS {TABLE_ORDERS} (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES {TABLE_USERS}(id) ON DELETE RESTRICT,
            product_id BIGINT NOT NULL REFERENCES {TABLE_PRODUCTS}(id) ON DELETE RESTRICT,
            provider TEXT NOT NULL,
            external_order_id TEXT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            amount_minor BIGINT NULL,
            currency TEXT NULL,
            status TEXT NOT NULL,
            metadata_json TEXT NULL,
            completed_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_provider_external ON {TABLE_ORDERS}(provider, external_order_id) WHERE external_order_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS {TABLE_PAYMENTS} (
            id BIGSERIAL PRIMARY KEY,
            order_id BIGINT NOT NULL REFERENCES {TABLE_ORDERS}(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            provider_payment_id TEXT NOT NULL,
            external_id TEXT NULL,
            telegram_charge_id TEXT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            amount_minor BIGINT NULL,
            currency TEXT NULL,
            status TEXT NOT NULL,
            is_processed BOOLEAN NOT NULL DEFAULT FALSE,
            processed_at TIMESTAMPTZ NULL,
            metadata_json TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_provider_payment ON {TABLE_PAYMENTS}(provider, provider_payment_id);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_provider_external ON {TABLE_PAYMENTS}(provider, external_id) WHERE external_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_telegram_charge ON {TABLE_PAYMENTS}(telegram_charge_id) WHERE telegram_charge_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_payments_status_created ON {TABLE_PAYMENTS}(status, created_at DESC);
        CREATE TABLE IF NOT EXISTS {TABLE_REFUNDS} (
            id BIGSERIAL PRIMARY KEY,
            payment_id BIGINT NOT NULL REFERENCES {TABLE_PAYMENTS}(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            provider_refund_id TEXT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            amount_minor BIGINT NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NULL,
            metadata_json TEXT NULL,
            processed_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_refunds_provider_id ON {TABLE_REFUNDS}(provider, provider_refund_id) WHERE provider_refund_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS {TABLE_AUDIT} (
            id BIGSERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'INFO',
            provider TEXT NULL,
            user_id BIGINT NULL,
            order_id BIGINT NULL,
            payment_id BIGINT NULL,
            refund_id BIGINT NULL,
            message TEXT NULL,
            details_json TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_audit_event_created ON {TABLE_AUDIT}(event_type, created_at DESC);
        """
    )


def _drop_empty_legacy_table(cur):
    cur.execute("SELECT to_regclass(%s) AS table_name", (f"public.{LEGACY_TABLE}",))
    row = cur.fetchone()
    table_name = row["table_name"] if isinstance(row, dict) else row[0]
    if not table_name:
        return
    cur.execute(f"SELECT COUNT(*) AS cnt FROM {LEGACY_TABLE}")
    count_row = cur.fetchone()
    count = int(count_row["cnt"] if isinstance(count_row, dict) else count_row[0])
    if count > 0:
        log_event("payments.db.legacy_not_empty", level="WARNING", table=LEGACY_TABLE, rows=count)
        return
    cur.execute(f"DROP TABLE IF EXISTS {LEGACY_TABLE}")
    log_event("payments.db.legacy_dropped", level="WARNING", table=LEGACY_TABLE)


def _insert_audit(cur, *, event_type, provider=None, user_id=None, order_id=None, payment_id=None, refund_id=None, severity="INFO", message=None, details=None):
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


def _upsert_payment_bundle(cur, *, provider, external_payment_id, user_id, plan_type, amount_minor, currency, payment_status, order_status, metadata):
    cur.execute(
        f"""
        INSERT INTO {TABLE_USERS} (telegram_user_id, created_at, updated_at)
        VALUES (%s, NOW(), NOW())
        ON CONFLICT (telegram_user_id) DO UPDATE SET updated_at = NOW()
        RETURNING id
        """,
        (int(user_id),),
    )
    user_db_id = int(cur.fetchone()["id"])

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
        (code, f"{plan_type} ({currency or 'N/A'})", plan_type, amount_minor, currency, _metadata_to_text({"source": "payments_store"})),
    )
    product_db_id = int(cur.fetchone()["id"])

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
            status = CASE WHEN {TABLE_ORDERS}.status = 'succeeded' THEN {TABLE_ORDERS}.status ELSE EXCLUDED.status END,
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
            datetime.now(timezone.utc) if order_status == "succeeded" else None,
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
            status = CASE WHEN {TABLE_PAYMENTS}.is_processed THEN {TABLE_PAYMENTS}.status ELSE EXCLUDED.status END,
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


def init_payments_store_sync():
    global _PAYMENTS_DB_ACTIVE
    if not PAYMENTS_DATABASE_URL:
        _PAYMENTS_DB_ACTIVE = False
        if PAYMENTS_DB_REQUIRED:
            raise RuntimeError("PAYMENTS_DB_REQUIRED=true but PAYMENTS_DATABASE_URL is empty.")
        log_event("payments.db.disabled", level="WARNING", mode="local_state", reason="PAYMENTS_DATABASE_URL is empty")
        return False
    if psycopg2 is None:
        _PAYMENTS_DB_ACTIVE = False
        if PAYMENTS_DB_REQUIRED:
            raise RuntimeError("PostgreSQL client library is not installed.")
        log_event("payments.db.client_missing", level="WARNING", mode="local_state", reason="client library missing")
        return False
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_schema(cur)
                _drop_empty_legacy_table(cur)
        _PAYMENTS_DB_ACTIVE = True
        log_event("payments.db.ready", level="INFO")
        return True
    except Exception as e:
        _PAYMENTS_DB_ACTIVE = False
        message = f"Payments PostgreSQL is unavailable ({type(e).__name__}: {e})."
        if PAYMENTS_DB_REQUIRED:
            raise RuntimeError(message)
        log_event("payments.db.unavailable", level="WARNING", mode="local_state", reason=message)
        return False


def register_pending_payment_sync(provider, external_payment_id, *, user_id, plan_type, amount_minor=None, currency=None, status="pending", metadata=None):
    provider = _norm_provider(provider)
    payment_id = str(external_payment_id).strip()
    if not payment_id:
        raise ValueError("external_payment_id_required")
    payment_status = _norm_status(status, PAYMENT_STATUSES, "pending")
    order_status = _order_status_for_payment(payment_status)
    amount = _norm_amount(amount_minor)
    curr = _norm_currency(currency)
    plan = str(plan_type or "premium_monthly").strip().lower()
    if _db_ready():
        try:
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
        except Exception as e:
            if PAYMENTS_DB_REQUIRED:
                raise
            log_event("payments.db.runtime_unavailable", level="WARNING", operation="register_pending_payment", error_class=type(e).__name__, error=str(e))

    with state.PAYMENTS_LOCK:
        now_iso = _utc_now_iso()
        idem = _idempotency_key(provider, payment_id)
        order = state.LOCAL_PAYMENT_ORDERS.get(idem) or {}
        if not order:
            order = {
                "id": int(state.LOCAL_PAYMENT_SEQUENCES.get("orders", 0)) + 1,
                "user_id": int(user_id),
                "plan_type": plan,
                "idempotency_key": idem,
                "status": order_status,
                "completed_at": now_iso if order_status == "succeeded" else None,
            }
            state.LOCAL_PAYMENT_SEQUENCES["orders"] = order["id"]
        order["updated_at"] = now_iso
        state.LOCAL_PAYMENT_ORDERS[idem] = order
        key = (provider, payment_id)
        record = state.LOCAL_PAYMENT_RECORDS.get(key) or {
            "payment_id": int(state.LOCAL_PAYMENT_SEQUENCES.get("payments", 0)) + 1,
            "order_id": order["id"],
            "provider": provider,
            "external_payment_id": payment_id,
            "idempotency_key": idem,
            "is_processed": False,
            "created_at": now_iso,
        }
        state.LOCAL_PAYMENT_SEQUENCES["payments"] = int(record["payment_id"])
        if not record.get("is_processed"):
            record["status"] = payment_status
        record["user_id"] = int(user_id)
        record["plan_type"] = plan
        record["amount_minor"] = amount
        record["currency"] = curr
        record["metadata_json"] = metadata
        record["updated_at"] = now_iso
        state.LOCAL_PAYMENT_RECORDS[key] = record
        out = _clone(record)
        out["order_status"] = order.get("status")
        out["metadata_json"] = _text_to_metadata(out.get("metadata_json"))
        return out


def update_payment_status_sync(provider, external_payment_id, status, metadata=None):
    provider = _norm_provider(provider)
    payment_id = str(external_payment_id).strip()
    if not payment_id:
        return None
    payment_status = _norm_status(status, PAYMENT_STATUSES, "unknown")
    order_status = _order_status_for_payment(payment_status)
    if _db_ready():
        try:
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
                        f"UPDATE {TABLE_PAYMENTS} SET status = %s, metadata_json = COALESCE(%s, metadata_json), updated_at = NOW() WHERE id = %s",
                        (payment_status, _metadata_to_text(metadata), payment_db_id),
                    )
                    cur.execute(
                        f"UPDATE {TABLE_ORDERS} SET status = %s, metadata_json = COALESCE(%s, metadata_json), updated_at = NOW(), completed_at = CASE WHEN %s = 'succeeded' AND completed_at IS NULL THEN NOW() ELSE completed_at END WHERE id = %s",
                        (order_status, _metadata_to_text(metadata), order_status, order_db_id),
                    )
                    _insert_audit(cur, event_type="payment.status_updated", provider=provider, order_id=order_db_id, payment_id=payment_db_id, message="Payment status updated", details={"status": payment_status})
                    _track_failed_status(provider, payment_status)
                    return _fetch_payment_public(cur, provider, payment_id)
        except Exception as e:
            if PAYMENTS_DB_REQUIRED:
                raise
            log_event("payments.db.runtime_unavailable", level="WARNING", operation="update_payment_status", error_class=type(e).__name__, error=str(e))

    with state.PAYMENTS_LOCK:
        key = (provider, payment_id)
        record = state.LOCAL_PAYMENT_RECORDS.get(key)
        if not record:
            return None
        now_iso = _utc_now_iso()
        record["status"] = payment_status
        record["updated_at"] = now_iso
        if metadata is not None:
            record["metadata_json"] = metadata
        state.LOCAL_PAYMENT_RECORDS[key] = record
        order = state.LOCAL_PAYMENT_ORDERS.get(record.get("idempotency_key"))
        if isinstance(order, dict):
            order["status"] = order_status
            order["updated_at"] = now_iso
            if order_status == "succeeded" and not order.get("completed_at"):
                order["completed_at"] = now_iso
        _track_failed_status(provider, payment_status)
        out = _clone(record)
        out["order_status"] = order.get("status") if isinstance(order, dict) else order_status
        out["metadata_json"] = _text_to_metadata(out.get("metadata_json"))
        return out


def get_payment_sync(provider, external_payment_id):
    provider = _norm_provider(provider)
    payment_id = str(external_payment_id).strip()
    if not payment_id:
        return None
    if _db_ready():
        try:
            with _connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    return _fetch_payment_public(cur, provider, payment_id)
        except Exception as e:
            if PAYMENTS_DB_REQUIRED:
                raise
            log_event("payments.db.runtime_unavailable", level="WARNING", operation="get_payment", error_class=type(e).__name__, error=str(e))
    with state.PAYMENTS_LOCK:
        record = state.LOCAL_PAYMENT_RECORDS.get((provider, payment_id))
        if not record:
            return None
        out = _clone(record)
        out["metadata_json"] = _text_to_metadata(out.get("metadata_json"))
        return out


def complete_payment_once_sync(provider, external_payment_id, *, user_id, plan_type, amount_minor=None, currency=None, status="succeeded", metadata=None):
    provider = _norm_provider(provider)
    payment_id = str(external_payment_id).strip()
    if not payment_id:
        raise ValueError("external_payment_id_required")
    payment_status = _norm_status(status, PAYMENT_STATUSES, "succeeded")
    order_status = _order_status_for_payment(payment_status)
    amount = _norm_amount(amount_minor)
    curr = _norm_currency(currency)
    plan = str(plan_type or "premium_monthly").strip().lower()
    if _db_ready():
        try:
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
                    cur.execute(f"SELECT is_processed FROM {TABLE_PAYMENTS} WHERE provider = %s AND provider_payment_id = %s FOR UPDATE", (provider, payment_id))
                    row = cur.fetchone()
                    if not row:
                        return False, None
                    if bool(row["is_processed"]):
                        _insert_audit(cur, event_type="payment.duplicate_ignored", severity="WARNING", provider=provider, user_id=user_id, order_id=order_db_id, payment_id=payment_db_id, message="Duplicate payment completion ignored")
                        return False, _fetch_payment_public(cur, provider, payment_id)
                    cur.execute(
                        f"UPDATE {TABLE_PAYMENTS} SET status = %s, is_processed = TRUE, processed_at = COALESCE(processed_at, NOW()), amount_minor = COALESCE(%s, amount_minor), currency = COALESCE(%s, currency), metadata_json = COALESCE(%s, metadata_json), updated_at = NOW() WHERE provider = %s AND provider_payment_id = %s",
                        (payment_status, amount, curr, _metadata_to_text(metadata), provider, payment_id),
                    )
                    cur.execute(
                        f"UPDATE {TABLE_ORDERS} SET status = %s, completed_at = CASE WHEN %s = 'succeeded' THEN COALESCE(completed_at, NOW()) ELSE completed_at END, metadata_json = COALESCE(%s, metadata_json), updated_at = NOW() WHERE id = %s",
                        (order_status, order_status, _metadata_to_text(metadata), order_db_id),
                    )
                    _insert_audit(cur, event_type="payment.completed", provider=provider, user_id=user_id, order_id=order_db_id, payment_id=payment_db_id, message="Payment marked as processed", details={"status": payment_status})
                    _track_failed_status(provider, payment_status)
                    return True, _fetch_payment_public(cur, provider, payment_id)
        except Exception as e:
            if PAYMENTS_DB_REQUIRED:
                raise
            log_event("payments.db.runtime_unavailable", level="WARNING", operation="complete_payment_once", error_class=type(e).__name__, error=str(e))

    with state.PAYMENTS_LOCK:
        key = (provider, payment_id)
        now_iso = _utc_now_iso()
        record = state.LOCAL_PAYMENT_RECORDS.get(key)
        if record and bool(record.get("is_processed")):
            return False, _clone(record)
        idem = _idempotency_key(provider, payment_id)
        order = state.LOCAL_PAYMENT_ORDERS.get(idem) or {"id": int(state.LOCAL_PAYMENT_SEQUENCES.get("orders", 0)) + 1, "idempotency_key": idem}
        state.LOCAL_PAYMENT_SEQUENCES["orders"] = int(order["id"])
        order["user_id"] = int(user_id)
        order["plan_type"] = plan
        order["status"] = order_status
        order["completed_at"] = now_iso if order_status == "succeeded" else order.get("completed_at")
        order["updated_at"] = now_iso
        state.LOCAL_PAYMENT_ORDERS[idem] = order
        if not record:
            record = {"payment_id": int(state.LOCAL_PAYMENT_SEQUENCES.get("payments", 0)) + 1, "created_at": now_iso}
            state.LOCAL_PAYMENT_SEQUENCES["payments"] = int(record["payment_id"])
        record.update(
            {
                "order_id": order["id"],
                "provider": provider,
                "external_payment_id": payment_id,
                "idempotency_key": idem,
                "user_id": int(user_id),
                "plan_type": plan,
                "amount_minor": amount,
                "currency": curr,
                "status": payment_status,
                "is_processed": True,
                "processed_at": record.get("processed_at") or now_iso,
                "metadata_json": metadata if metadata is not None else record.get("metadata_json"),
                "updated_at": now_iso,
            }
        )
        state.LOCAL_PAYMENT_RECORDS[key] = record
        _track_failed_status(provider, payment_status)
        out = _clone(record)
        out["metadata_json"] = _text_to_metadata(out.get("metadata_json"))
        return True, out


def register_refund_pending_sync(provider, provider_refund_id, *, payment_provider, payment_external_id, amount_minor, currency, status="pending", reason="", metadata=None, idempotency_key=None):
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
    if _db_ready():
        try:
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
                            processed_at = CASE WHEN EXCLUDED.status = 'succeeded' THEN COALESCE({TABLE_REFUNDS}.processed_at, NOW()) ELSE {TABLE_REFUNDS}.processed_at END,
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
                            datetime.now(timezone.utc) if refund_status == "succeeded" else None,
                        ),
                    )
                    row = dict(cur.fetchone())
                    row["metadata_json"] = _text_to_metadata(row.get("metadata_json"))
                    for field in ("created_at", "updated_at", "processed_at"):
                        row[field] = _dt_to_iso(row.get(field))
                    _insert_audit(cur, event_type="refund.pending_registered", provider=provider, user_id=payment.get("user_id"), payment_id=payment.get("payment_id"), refund_id=row.get("id"), message="Refund registered", details={"status": refund_status})
                    return row
        except Exception as e:
            if PAYMENTS_DB_REQUIRED:
                raise
            log_event("payments.db.runtime_unavailable", level="WARNING", operation="register_refund_pending", error_class=type(e).__name__, error=str(e))
    with state.PAYMENTS_LOCK:
        now_iso = _utc_now_iso()
        payload = state.LOCAL_PAYMENT_REFUNDS.get(idem) or {
            "id": int(state.LOCAL_PAYMENT_SEQUENCES.get("refunds", 0)) + 1,
            "payment_id": payment.get("payment_id"),
            "provider": provider,
            "provider_refund_id": str(provider_refund_id) if provider_refund_id else None,
            "idempotency_key": idem,
            "created_at": now_iso,
        }
        state.LOCAL_PAYMENT_SEQUENCES["refunds"] = int(payload["id"])
        payload.update(
            {
                "amount_minor": amount,
                "currency": curr,
                "status": refund_status,
                "reason": str(reason or payload.get("reason") or ""),
                "metadata_json": metadata if metadata is not None else payload.get("metadata_json"),
                "processed_at": now_iso if refund_status == "succeeded" and not payload.get("processed_at") else payload.get("processed_at"),
                "updated_at": now_iso,
            }
        )
        state.LOCAL_PAYMENT_REFUNDS[idem] = payload
        return _clone(payload)


def update_refund_status_sync(idempotency_key, status, metadata=None, reason=None):
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    refund_status = _norm_status(status, REFUND_STATUSES, "unknown")
    if _db_ready():
        try:
            with _connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        UPDATE {TABLE_REFUNDS}
                        SET
                            status = %s,
                            reason = COALESCE(%s, reason),
                            metadata_json = COALESCE(%s, metadata_json),
                            processed_at = CASE WHEN %s = 'succeeded' THEN COALESCE(processed_at, NOW()) ELSE processed_at END,
                            updated_at = NOW()
                        WHERE idempotency_key = %s
                        RETURNING *
                        """,
                        (refund_status, str(reason) if reason is not None else None, _metadata_to_text(metadata), refund_status, key),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    payload = dict(row)
                    payload["metadata_json"] = _text_to_metadata(payload.get("metadata_json"))
                    for field in ("created_at", "updated_at", "processed_at"):
                        payload[field] = _dt_to_iso(payload.get(field))
                    return payload
        except Exception as e:
            if PAYMENTS_DB_REQUIRED:
                raise
            log_event("payments.db.runtime_unavailable", level="WARNING", operation="update_refund_status", error_class=type(e).__name__, error=str(e))
    with state.PAYMENTS_LOCK:
        payload = state.LOCAL_PAYMENT_REFUNDS.get(key)
        if not payload:
            return None
        now_iso = _utc_now_iso()
        payload["status"] = refund_status
        payload["updated_at"] = now_iso
        if reason is not None:
            payload["reason"] = str(reason)
        if metadata is not None:
            payload["metadata_json"] = metadata
        if refund_status == "succeeded" and not payload.get("processed_at"):
            payload["processed_at"] = now_iso
        state.LOCAL_PAYMENT_REFUNDS[key] = payload
        return _clone(payload)


def get_refund_sync(idempotency_key):
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    if _db_ready():
        try:
            with _connect() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(f"SELECT * FROM {TABLE_REFUNDS} WHERE idempotency_key = %s LIMIT 1", (key,))
                    row = cur.fetchone()
                    if not row:
                        return None
                    payload = dict(row)
                    payload["metadata_json"] = _text_to_metadata(payload.get("metadata_json"))
                    for field in ("created_at", "updated_at", "processed_at"):
                        payload[field] = _dt_to_iso(payload.get(field))
                    return payload
        except Exception as e:
            if PAYMENTS_DB_REQUIRED:
                raise
            log_event("payments.db.runtime_unavailable", level="WARNING", operation="get_refund", error_class=type(e).__name__, error=str(e))
    with state.PAYMENTS_LOCK:
        payload = state.LOCAL_PAYMENT_REFUNDS.get(key)
        return _clone(payload) if payload else None


async def register_pending_payment(provider, external_payment_id, *, user_id, plan_type, amount_minor=None, currency=None, status="pending", metadata=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(register_pending_payment_sync, provider, external_payment_id, user_id=user_id, plan_type=plan_type, amount_minor=amount_minor, currency=currency, status=status, metadata=metadata)
    return await loop.run_in_executor(None, fn)


async def update_payment_status(provider, external_payment_id, status, metadata=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(update_payment_status_sync, provider, external_payment_id, status, metadata)
    return await loop.run_in_executor(None, fn)


async def get_payment(provider, external_payment_id):
    loop = asyncio.get_running_loop()
    fn = functools.partial(get_payment_sync, provider, external_payment_id)
    return await loop.run_in_executor(None, fn)


async def complete_payment_once(provider, external_payment_id, *, user_id, plan_type, amount_minor=None, currency=None, status="succeeded", metadata=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(complete_payment_once_sync, provider, external_payment_id, user_id=user_id, plan_type=plan_type, amount_minor=amount_minor, currency=currency, status=status, metadata=metadata)
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


async def update_refund_status(idempotency_key, status, metadata=None, reason=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(update_refund_status_sync, idempotency_key, status, metadata, reason)
    return await loop.run_in_executor(None, fn)


async def get_refund(idempotency_key):
    loop = asyncio.get_running_loop()
    fn = functools.partial(get_refund_sync, idempotency_key)
    return await loop.run_in_executor(None, fn)
