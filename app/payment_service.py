import asyncio
import hashlib
import threading
import time

from app.access import PLAN_PREMIUM_MONTHLY, format_utc_iso_for_display
from app.config import (
    PAYMENT_BUTTON_THROTTLE_SECONDS,
    PAYMENT_SESSION_WINDOW_SECONDS,
    ROBOKASSA_CURRENCY,
    ROBOKASSA_PREMIUM_MONTHLY_AMOUNT,
)
from app.i18n import get_lang, t, tf
from app.logging_utils import log_event
from app.payments_store import (
    acquire_payment_session,
    attach_payment_session,
    expire_payment_session,
    finalize_verified_payment,
    get_payment,
    get_payment_session,
    mark_payment_invalid,
    payments_store_is_ready,
    register_pending_payment,
)
from app.robokassa import create_monthly_payment, extract_payment_metadata


PROVIDER_TELEGRAM_STARS = "telegram_stars"
PROVIDER_ROBOKASSA = "robokassa"

_BUTTON_THROTTLE = {}
_BUTTON_THROTTLE_LOCK = threading.Lock()


def payments_available():
    return payments_store_is_ready()


def _expected_robokassa_amount_minor():
    return int(ROBOKASSA_PREMIUM_MONTHLY_AMOUNT) * 100


def allow_payment_callback(user_id, action):
    now = time.time()
    key = (int(user_id), str(action))
    with _BUTTON_THROTTLE_LOCK:
        last = float(_BUTTON_THROTTLE.get(key, 0.0))
        if now - last < float(PAYMENT_BUTTON_THROTTLE_SECONDS):
            return False
        _BUTTON_THROTTLE[key] = now
        return True


def build_payment_session_key(user_id, plan_type):
    window = int(time.time() // int(PAYMENT_SESSION_WINDOW_SECONDS))
    raw = f"{int(user_id)}:{str(plan_type).strip().lower()}:{window}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"rk:{digest[:32]}"


def validate_robokassa_verified_payload(status_payload, expected_user_id=None):
    if not isinstance(status_payload, dict):
        return False, "invalid_payload", None, None
    if not bool(status_payload.get("signature_valid")):
        return False, "invalid_signature", None, None
    try:
        amount_minor = int(status_payload.get("amount_minor"))
    except Exception:
        return False, "invalid_amount", None, None
    if amount_minor != _expected_robokassa_amount_minor():
        return False, "unexpected_amount", None, None
    if str(status_payload.get("currency") or "").upper() != str(ROBOKASSA_CURRENCY).upper():
        return False, "invalid_currency", None, None
    metadata = extract_payment_metadata(status_payload)
    if not metadata["user_id"]:
        return False, "metadata_user_missing", None, None
    if not metadata["plan_type"]:
        return False, "metadata_plan_missing", None, None
    if metadata["plan_type"] != PLAN_PREMIUM_MONTHLY:
        return False, "metadata_plan_mismatch", None, None
    try:
        metadata_user_id = int(metadata["user_id"])
    except Exception:
        return False, "metadata_user_invalid", None, None
    if expected_user_id is not None and metadata_user_id != int(expected_user_id):
        return False, "metadata_user_mismatch", metadata_user_id, metadata["plan_type"]
    return True, None, metadata_user_id, metadata["plan_type"]


async def notify_successful_entitlement(application, *, user_id, entitlement, payment_id, source):
    try:
        lang = await get_lang(user_id, None)
        if entitlement.get("plan_type") == "premium_lifetime":
            text = t("premium_lifetime_already", lang)
        else:
            text = tf(
                "subscription_active_until",
                lang,
                expires_at_utc=format_utc_iso_for_display(entitlement.get("plan_expires_at_utc")),
            )
        await application.bot.send_message(chat_id=int(user_id), text=text)
    except Exception as e:
        log_event(
            "payment.notify.failed",
            level="WARNING",
            user_id=user_id,
            payment_id=payment_id,
            source=source,
            error_class=type(e).__name__,
            error=str(e),
        )


async def create_or_reuse_robokassa_payment(user_id):
    session_key = build_payment_session_key(user_id, PLAN_PREMIUM_MONTHLY)
    session = await acquire_payment_session(
        session_key,
        provider=PROVIDER_ROBOKASSA,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        ttl_seconds=PAYMENT_SESSION_WINDOW_SECONDS,
    )
    action = session.get("action")
    if action in ("reuse", "wait"):
        for _ in range(10):
            current = await get_payment_session(session_key)
            if current and current.get("payment_id") and current.get("payment_url"):
                return current
            if action != "wait":
                break
            await asyncio.sleep(0.2)
    if action != "create":
        return session
    try:
        payment = await create_monthly_payment(user_id)
        payment_id = payment["id"]
        payment_url = payment.get("payment_url")
        if not payment_url:
            raise RuntimeError("Robokassa payment session is incomplete.")
        await register_pending_payment(
            PROVIDER_ROBOKASSA,
            payment_id,
            user_id=user_id,
            plan_type=PLAN_PREMIUM_MONTHLY,
            amount_minor=payment.get("amount_minor"),
            currency=payment.get("currency") or ROBOKASSA_CURRENCY,
            status=payment.get("status") or "pending",
            metadata=payment.get("raw"),
        )
        return await attach_payment_session(
            session_key,
            payment_id=payment_id,
            payment_url=payment_url,
            status=payment.get("status") or "pending",
            ttl_seconds=PAYMENT_SESSION_WINDOW_SECONDS,
        )
    except Exception:
        await expire_payment_session(session_key)
        raise


async def verify_and_finalize_robokassa_payment(application, status_payload, *, expected_user_id=None, trigger="manual"):
    payment_id = str(status_payload.get("id") or "").strip()
    if not payment_id:
        return {"result": "invalid", "reason": "missing_invoice_id", "status": "unknown", "payload": status_payload}
    valid_payload, invalid_reason, verified_user_id, verified_plan_type = validate_robokassa_verified_payload(
        status_payload,
        expected_user_id=expected_user_id,
    )
    if not valid_payload:
        existing = await get_payment(PROVIDER_ROBOKASSA, payment_id)
        if existing:
            await mark_payment_invalid(
                PROVIDER_ROBOKASSA,
                payment_id,
                metadata=status_payload.get("raw"),
                invalid_reason=invalid_reason,
            )
        log_event(
            "payment.invalid",
            level="WARNING",
            user_id=verified_user_id or expected_user_id,
            payment_id=payment_id,
            provider=PROVIDER_ROBOKASSA,
            reason=invalid_reason,
            amount_minor=status_payload.get("amount_minor"),
            currency=status_payload.get("currency"),
        )
        return {"result": "invalid", "reason": invalid_reason, "status": "invalid", "payload": status_payload}
    try:
        processed_now, payment_record, entitlement = await finalize_verified_payment(
            PROVIDER_ROBOKASSA,
            payment_id,
            user_id=verified_user_id,
            plan_type=verified_plan_type,
            amount_minor=status_payload.get("amount_minor"),
            currency=status_payload.get("currency") or ROBOKASSA_CURRENCY,
            status="succeeded",
            metadata=status_payload.get("raw"),
        )
    except RuntimeError as e:
        if "payment_binding_mismatch" in str(e):
            await mark_payment_invalid(
                PROVIDER_ROBOKASSA,
                payment_id,
                metadata=status_payload.get("raw"),
                invalid_reason="payment_binding_mismatch",
            )
            return {"result": "invalid", "reason": "payment_binding_mismatch", "status": "invalid", "payload": status_payload}
        raise
    if processed_now:
        await notify_successful_entitlement(
            application,
            user_id=verified_user_id,
            entitlement=entitlement,
            payment_id=payment_id,
            source=trigger,
        )
    return {
        "result": "processed" if processed_now else "duplicate",
        "status": "succeeded",
        "payment": payment_record,
        "entitlement": entitlement,
        "payload": status_payload,
    }


async def finalize_stars_payment(application, *, user_id, charge_id, amount_minor, currency, metadata):
    processed_now, payment_record, entitlement = await finalize_verified_payment(
        PROVIDER_TELEGRAM_STARS,
        charge_id,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        amount_minor=amount_minor,
        currency=currency,
        status="succeeded",
        metadata=metadata,
    )
    if processed_now:
        await notify_successful_entitlement(
            application,
            user_id=user_id,
            entitlement=entitlement,
            payment_id=charge_id,
            source=PROVIDER_TELEGRAM_STARS,
        )
    return processed_now, payment_record, entitlement
