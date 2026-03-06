import asyncio
import functools
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from requests.auth import HTTPBasicAuth

from app.config import (
    YOOKASSA_API_BASE,
    YOOKASSA_CURRENCY,
    YOOKASSA_PREMIUM_MONTHLY_AMOUNT,
    YOOKASSA_RETURN_URL,
    YOOKASSA_SECRET_KEY,
    YOOKASSA_SHOP_ID,
)


def is_yookassa_configured():
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_RETURN_URL)


def _to_minor_units(amount_value):
    try:
        value = Decimal(str(amount_value))
    except (InvalidOperation, ValueError):
        return None
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _monthly_amount_value_text():
    value = Decimal(int(YOOKASSA_PREMIUM_MONTHLY_AMOUNT))
    return str(value.quantize(Decimal("1.00"), rounding=ROUND_HALF_UP))


def _request(method, path, *, payload=None, idempotence_key=None):
    if not is_yookassa_configured():
        raise RuntimeError("YooKassa is not configured.")
    url = f"{YOOKASSA_API_BASE}/{str(path).lstrip('/')}"
    headers = {"Content-Type": "application/json"}
    if idempotence_key:
        headers["Idempotence-Key"] = str(idempotence_key)
    response = requests.request(
        method=method,
        url=url,
        json=payload,
        headers=headers,
        timeout=20,
        auth=HTTPBasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
    )
    body = response.text or ""
    if response.status_code >= 400:
        raise RuntimeError(f"YooKassa API {response.status_code}: {body[:400]}")
    try:
        return response.json()
    except Exception:
        raise RuntimeError("YooKassa API returned non-JSON response.")


def _normalize_payment(data):
    if not isinstance(data, dict):
        raise RuntimeError("Invalid YooKassa payload.")
    payment_id = str(data.get("id") or "").strip()
    if not payment_id:
        raise RuntimeError("YooKassa payload has no payment id.")
    status = str(data.get("status") or "").strip() or "unknown"
    amount = data.get("amount") if isinstance(data.get("amount"), dict) else {}
    amount_value = amount.get("value")
    currency = str(amount.get("currency") or YOOKASSA_CURRENCY).upper()
    confirmation = data.get("confirmation") if isinstance(data.get("confirmation"), dict) else {}
    confirmation_url = confirmation.get("confirmation_url")
    return {
        "id": payment_id,
        "status": status,
        "paid": bool(data.get("paid")),
        "amount_value": str(amount_value) if amount_value is not None else None,
        "amount_minor": _to_minor_units(amount_value) if amount_value is not None else None,
        "currency": currency,
        "confirmation_url": str(confirmation_url) if confirmation_url else None,
        "raw": data,
    }


def extract_payment_metadata(payment_payload):
    raw = payment_payload.get("raw") if isinstance(payment_payload, dict) else {}
    metadata = raw.get("metadata") if isinstance(raw, dict) and isinstance(raw.get("metadata"), dict) else {}
    return {
        "user_id": str(metadata.get("user_id") or "").strip(),
        "plan_type": str(metadata.get("plan_type") or "").strip().lower(),
    }


def create_monthly_payment_sync(user_id, *, idempotence_key=None):
    uid = int(user_id)
    payload = {
        "amount": {
            "value": _monthly_amount_value_text(),
            "currency": YOOKASSA_CURRENCY,
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL,
        },
        "description": f"Premium Monthly for Telegram user {uid}",
        "metadata": {
            "user_id": str(uid),
            "plan_type": "premium_monthly",
        },
    }
    data = _request("POST", "/payments", payload=payload, idempotence_key=idempotence_key or uuid.uuid4().hex)
    return _normalize_payment(data)


def get_payment_sync(payment_id):
    data = _request("GET", f"/payments/{payment_id}")
    return _normalize_payment(data)


async def create_monthly_payment(user_id, *, idempotence_key=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(create_monthly_payment_sync, user_id, idempotence_key=idempotence_key)
    return await loop.run_in_executor(None, fn)


async def get_payment(payment_id):
    loop = asyncio.get_running_loop()
    fn = functools.partial(get_payment_sync, payment_id)
    return await loop.run_in_executor(None, fn)
