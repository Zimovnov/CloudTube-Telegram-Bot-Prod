import asyncio
import functools
import hashlib
import hmac
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode

from app.config import (
    ROBOKASSA_CURRENCY,
    ROBOKASSA_HASH_ALGORITHM,
    ROBOKASSA_IS_TEST,
    ROBOKASSA_MERCHANT_LOGIN,
    ROBOKASSA_PASSWORD1,
    ROBOKASSA_PASSWORD2,
    ROBOKASSA_PAYMENT_URL,
    ROBOKASSA_PREMIUM_MONTHLY_AMOUNT,
)


_HASH_ALGO_ALIASES = {
    "MD5": "md5",
    "SHA1": "sha1",
    "SHA-1": "sha1",
    "SHA256": "sha256",
    "SHA-256": "sha256",
    "SHA384": "sha384",
    "SHA-384": "sha384",
    "SHA512": "sha512",
    "SHA-512": "sha512",
    "RIPEMD160": "ripemd160",
}


def is_robokassa_configured():
    return bool(ROBOKASSA_MERCHANT_LOGIN and ROBOKASSA_PASSWORD1 and ROBOKASSA_PASSWORD2)


def _normalize_hash_algorithm():
    algo = str(ROBOKASSA_HASH_ALGORITHM or "MD5").strip().upper()
    return _HASH_ALGO_ALIASES.get(algo, algo.lower())


def _hash_signature(raw_value):
    name = _normalize_hash_algorithm()
    try:
        digest = hashlib.new(name)
    except ValueError as exc:
        raise RuntimeError(f"Unsupported ROBOKASSA_HASH_ALGORITHM: {ROBOKASSA_HASH_ALGORITHM}") from exc
    digest.update(str(raw_value).encode("utf-8"))
    return digest.hexdigest().lower()


def _to_minor_units(amount_value):
    try:
        value = Decimal(str(amount_value))
    except (InvalidOperation, ValueError):
        return None
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _monthly_amount_decimal():
    return Decimal(int(ROBOKASSA_PREMIUM_MONTHLY_AMOUNT))


def _monthly_amount_value_text():
    value = _monthly_amount_decimal()
    quantum = Decimal("1") if ROBOKASSA_IS_TEST else Decimal("1.000000")
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), "f")


def _sorted_user_params(user_params):
    items = {}
    for key, value in (user_params or {}).items():
        text_key = str(key or "").strip()
        if not text_key.startswith("Shp_"):
            continue
        items[text_key] = str(value or "").strip()
    return sorted(items.items(), key=lambda item: item[0])


def build_payment_signature(amount_text, invoice_id, *, user_params=None):
    parts = [
        ROBOKASSA_MERCHANT_LOGIN,
        str(amount_text),
        str(invoice_id),
        ROBOKASSA_PASSWORD1,
    ]
    for key, value in _sorted_user_params(user_params):
        parts.append(f"{key}={value}")
    return _hash_signature(":".join(parts))


def build_result_signature(out_sum, invoice_id, *, user_params=None):
    parts = [
        str(out_sum),
        str(invoice_id),
        ROBOKASSA_PASSWORD2,
    ]
    for key, value in _sorted_user_params(user_params):
        parts.append(f"{key}={value}")
    return _hash_signature(":".join(parts))


def verify_result_signature(out_sum, invoice_id, signature_value, *, user_params=None):
    provided = str(signature_value or "").strip().lower()
    if not provided:
        return False
    expected = build_result_signature(out_sum, invoice_id, user_params=user_params)
    return hmac.compare_digest(expected, provided)


def extract_payment_metadata(payment_payload):
    raw = payment_payload.get("raw") if isinstance(payment_payload, dict) else {}
    user_params = raw.get("user_params") if isinstance(raw, dict) and isinstance(raw.get("user_params"), dict) else {}
    return {
        "user_id": str(user_params.get("Shp_user_id") or "").strip(),
        "plan_type": str(user_params.get("Shp_plan_type") or "").strip().lower(),
    }


def normalize_result_payload(data):
    if not isinstance(data, dict):
        raise RuntimeError("Invalid Robokassa payload.")
    invoice_id = str(
        data.get("InvId")
        or data.get("InvoiceID")
        or data.get("invoice_id")
        or data.get("payment_id")
        or ""
    ).strip()
    out_sum = str(data.get("OutSum") or data.get("out_sum") or "").strip()
    signature_value = str(data.get("SignatureValue") or data.get("signature_value") or "").strip()
    user_params = {
        str(key): str(value).strip()
        for key, value in data.items()
        if str(key).startswith("Shp_")
    }
    return {
        "id": invoice_id or None,
        "status": "succeeded",
        "paid": True,
        "amount_value": out_sum or None,
        "amount_minor": _to_minor_units(out_sum) if out_sum else None,
        "currency": ROBOKASSA_CURRENCY,
        "signature_value": signature_value or None,
        "signature_valid": verify_result_signature(out_sum, invoice_id, signature_value, user_params=user_params)
        if invoice_id and out_sum and signature_value
        else False,
        "raw": {
            "invoice_id": invoice_id or None,
            "out_sum": out_sum or None,
            "signature_value": signature_value or None,
            "user_params": user_params,
            "is_test": str(data.get("IsTest") or "").strip(),
            "received": {str(key): str(value) for key, value in data.items()},
        },
    }


def _make_invoice_id(user_id):
    uid = abs(int(user_id))
    millis = int(time.time() * 1000)
    return str((millis * 1000) + (uid % 1000))


def create_monthly_payment_sync(user_id, *, invoice_id=None):
    if not is_robokassa_configured():
        raise RuntimeError("Robokassa is not configured.")
    payment_id = str(invoice_id or _make_invoice_id(user_id))
    amount_text = _monthly_amount_value_text()
    user_params = {
        "Shp_plan_type": "premium_monthly",
        "Shp_user_id": str(int(user_id)),
    }
    query_params = {
        "MerchantLogin": ROBOKASSA_MERCHANT_LOGIN,
        "OutSum": amount_text,
        "InvId": payment_id,
        "Description": "Premium subscription for 30 days",
        "SignatureValue": build_payment_signature(amount_text, payment_id, user_params=user_params),
        "Culture": "ru",
        **user_params,
    }
    if ROBOKASSA_IS_TEST:
        query_params["IsTest"] = "1"
    payment_url = f"{ROBOKASSA_PAYMENT_URL}?{urlencode(query_params)}"
    return {
        "id": payment_id,
        "status": "pending",
        "paid": False,
        "amount_value": amount_text,
        "amount_minor": _to_minor_units(amount_text),
        "currency": ROBOKASSA_CURRENCY,
        "payment_url": payment_url,
        "raw": {
            "invoice_id": payment_id,
            "amount_value": amount_text,
            "user_params": user_params,
            "is_test": ROBOKASSA_IS_TEST,
        },
    }


async def create_monthly_payment(user_id, *, invoice_id=None):
    loop = asyncio.get_running_loop()
    fn = functools.partial(create_monthly_payment_sync, user_id, invoice_id=invoice_id)
    return await loop.run_in_executor(None, fn)
