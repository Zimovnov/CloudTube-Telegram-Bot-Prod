import time

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.access import (
    PLAN_PREMIUM_LIFETIME,
    PLAN_PREMIUM_MONTHLY,
    activate_or_extend_monthly,
    format_utc_iso_for_display,
    get_user_profile,
)
from app.config import (
    PREMIUM_MONTHLY_STARS,
    PREMIUM_PERIOD_SECONDS,
    TELEGRAM_STARS_PROVIDER_TOKEN,
    YOOKASSA_CURRENCY,
    YOOKASSA_PREMIUM_MONTHLY_AMOUNT,
)
from app.errors import ERR_PAYMENT_DUPLICATE, ERR_PAYMENT_INVALID
from app.i18n import get_lang, t, tf
from app.logging_utils import log_event
from app.payments_store import complete_payment_once, get_payment, register_pending_payment, update_payment_status
from app.yookassa import create_monthly_payment, get_payment as get_yookassa_payment, is_yookassa_configured

PROVIDER_TELEGRAM_STARS = "telegram_stars"
PROVIDER_YOOKASSA = "yookassa"


def _stars_enabled():
    return int(PREMIUM_MONTHLY_STARS) > 0


def _resolve_payment_methods():
    return _stars_enabled(), is_yookassa_configured()


def _expected_yookassa_amount_minor():
    return int(YOOKASSA_PREMIUM_MONTHLY_AMOUNT) * 100


def _is_valid_stars_invoice_payload(payload, user_id):
    text = str(payload or "").strip()
    if not text:
        return False
    try:
        uid = int(user_id)
    except Exception:
        return False
    return text.startswith(f"premium_monthly:{uid}:")


def _validate_stars_payment(payment, user_id):
    if str(getattr(payment, "currency", "")).upper() != "XTR":
        return False, "invalid_currency"
    try:
        amount_minor = int(getattr(payment, "total_amount"))
    except Exception:
        return False, "invalid_amount"
    if amount_minor != int(PREMIUM_MONTHLY_STARS):
        return False, "unexpected_amount"
    if not _is_valid_stars_invoice_payload(getattr(payment, "invoice_payload", None), user_id):
        return False, "invalid_invoice_payload"
    return True, None


def _validate_yookassa_status_payload(status_payload, user_id):
    if not isinstance(status_payload, dict):
        return False, "invalid_payload"
    try:
        amount_minor = int(status_payload.get("amount_minor"))
    except Exception:
        return False, "invalid_amount"
    if amount_minor != _expected_yookassa_amount_minor():
        return False, "unexpected_amount"
    if str(status_payload.get("currency") or "").upper() != str(YOOKASSA_CURRENCY).upper():
        return False, "invalid_currency"
    raw = status_payload.get("raw") if isinstance(status_payload.get("raw"), dict) else {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    metadata_user_id = metadata.get("user_id")
    if metadata_user_id is not None and str(metadata_user_id).strip():
        if str(metadata_user_id).strip() != str(int(user_id)):
            return False, "metadata_user_mismatch"
    metadata_plan = str(metadata.get("plan_type") or "").strip().lower()
    if metadata_plan and metadata_plan != PLAN_PREMIUM_MONTHLY:
        return False, "metadata_plan_mismatch"
    return True, None


def build_premium_markup(lang):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("buy_premium_button", lang), callback_data="sub:buy_monthly")]]
    )


def _build_payment_methods_markup(lang, has_stars, has_yookassa):
    rows = []
    if has_stars:
        rows.append([InlineKeyboardButton(t("buy_premium_stars_button", lang), callback_data="sub:buy_stars")])
    if has_yookassa:
        rows.append([InlineKeyboardButton(t("buy_premium_yookassa_button", lang), callback_data="sub:buy_yookassa")])
    return InlineKeyboardMarkup(rows)


def _build_stars_invoice_markup(lang, invoice_url):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("buy_premium_stars_button", lang), url=invoice_url)]]
    )


def _build_yookassa_markup(lang, payment_url, payment_id):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("payment_pay_now_button", lang), url=payment_url)],
            [InlineKeyboardButton(t("payment_check_button", lang), callback_data=f"sub:check_yk:{payment_id}")],
        ]
    )


async def _send_monthly_stars_invoice(bot, chat_id, user_id, lang):
    payload = f"premium_monthly:{user_id}:{int(time.time())}"
    prices = [LabeledPrice(label=t("premium_monthly_label", lang), amount=int(PREMIUM_MONTHLY_STARS))]
    invoice_link = await bot.create_invoice_link(
        title=t("premium_invoice_title", lang),
        description=tf("premium_invoice_desc", lang, stars=PREMIUM_MONTHLY_STARS),
        payload=payload,
        currency="XTR",
        prices=prices,
        provider_token=TELEGRAM_STARS_PROVIDER_TOKEN or None,
        subscription_period=int(PREMIUM_PERIOD_SECONDS),
    )
    await bot.send_message(
        chat_id=chat_id,
        text=tf("premium_invoice_desc", lang, stars=PREMIUM_MONTHLY_STARS),
        reply_markup=_build_stars_invoice_markup(lang, invoice_link),
    )


async def _send_monthly_yookassa_invoice(bot, chat_id, user_id, lang):
    payment = await create_monthly_payment(user_id)
    payment_id = payment["id"]
    payment_url = payment.get("confirmation_url")
    if not payment_url:
        raise RuntimeError("YooKassa payment has no confirmation_url.")

    await register_pending_payment(
        PROVIDER_YOOKASSA,
        payment_id,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        amount_minor=payment.get("amount_minor"),
        currency=payment.get("currency") or YOOKASSA_CURRENCY,
        status=payment.get("status") or "pending",
        metadata=payment.get("raw"),
    )
    log_event(
        "payment.yookassa.created",
        level="INFO",
        user_id=user_id,
        payment_id=payment_id,
        status=payment.get("status"),
    )
    await bot.send_message(
        chat_id=chat_id,
        text=tf(
            "premium_yookassa_desc",
            lang,
            amount=YOOKASSA_PREMIUM_MONTHLY_AMOUNT,
            currency=(payment.get("currency") or YOOKASSA_CURRENCY),
        ),
        reply_markup=_build_yookassa_markup(lang, payment_url, payment_id),
    )


async def _start_purchase_flow(bot, chat_id, user_id, lang):
    has_stars, has_yookassa = _resolve_payment_methods()
    if has_stars and has_yookassa:
        await bot.send_message(
            chat_id=chat_id,
            text=t("premium_choose_method", lang),
            reply_markup=_build_payment_methods_markup(lang, has_stars, has_yookassa),
        )
        return
    if has_stars:
        await _send_monthly_stars_invoice(bot, chat_id, user_id, lang)
        return
    if has_yookassa:
        await _send_monthly_yookassa_invoice(bot, chat_id, user_id, lang)
        return
    await bot.send_message(chat_id=chat_id, text=t("payment_method_unavailable", lang))


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    lang = await get_lang(user.id, getattr(user, "language_code", None))
    profile = await get_user_profile(user.id)
    if profile.get("plan_type") == PLAN_PREMIUM_LIFETIME:
        await msg.reply_text(t("premium_lifetime_already", lang))
        return
    try:
        await _start_purchase_flow(context.bot, msg.chat_id, user.id, lang)
    except Exception as e:
        log_event(
            "payment.invoice.failed",
            level="ERROR",
            user_id=user.id,
            error_class=type(e).__name__,
            error=str(e),
        )
        await msg.reply_text(t("payment_invoice_failed_generic", lang))


async def _process_yookassa_check(query, user_id, lang, payment_id):
    record = await get_payment(PROVIDER_YOOKASSA, payment_id)
    if not record:
        try:
            await query.answer(t("payment_unknown", lang), show_alert=True)
        except BadRequest:
            pass
        return
    if int(record.get("user_id") or 0) != int(user_id):
        try:
            await query.answer(t("not_for_you", lang), show_alert=True)
        except BadRequest:
            pass
        return

    status_payload = await get_yookassa_payment(payment_id)
    status = status_payload.get("status") or "unknown"
    paid = bool(status_payload.get("paid"))
    await update_payment_status(PROVIDER_YOOKASSA, payment_id, status, metadata=status_payload.get("raw"))
    if status != "succeeded" or not paid:
        key = "payment_pending"
        if status == "canceled":
            key = "payment_cancelled"
        elif status == "waiting_for_capture":
            key = "payment_waiting_capture"
        try:
            await query.answer(t(key, lang), show_alert=True)
        except BadRequest:
            pass
        return
    valid_payload, invalid_reason = _validate_yookassa_status_payload(status_payload, user_id)
    if not valid_payload:
        log_event(
            "payment.invalid",
            level="WARNING",
            error_code=ERR_PAYMENT_INVALID,
            user_id=user_id,
            payment_id=payment_id,
            provider=PROVIDER_YOOKASSA,
            reason=invalid_reason,
            amount_minor=status_payload.get("amount_minor"),
            currency=status_payload.get("currency"),
        )
        try:
            await query.answer(t("payment_invalid", lang), show_alert=True)
        except BadRequest:
            pass
        return

    processed_now, _ = await complete_payment_once(
        PROVIDER_YOOKASSA,
        payment_id,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        amount_minor=status_payload.get("amount_minor"),
        currency=status_payload.get("currency") or YOOKASSA_CURRENCY,
        status="succeeded",
        metadata=status_payload.get("raw"),
    )
    if not processed_now:
        log_event(
            "payment.duplicate_ignored",
            level="WARNING",
            error_code=ERR_PAYMENT_DUPLICATE,
            user_id=user_id,
            payment_id=payment_id,
            provider=PROVIDER_YOOKASSA,
        )
        try:
            await query.answer(t("payment_already_processed", lang), show_alert=True)
        except BadRequest:
            pass
        return

    profile = await activate_or_extend_monthly(user_id, charge_id=payment_id, source="yookassa")
    if profile.get("plan_type") == PLAN_PREMIUM_LIFETIME:
        await query.message.reply_text(t("premium_lifetime_already", lang))
        return
    await query.message.reply_text(
        tf(
            "subscription_active_until",
            lang,
            expires_at_utc=format_utc_iso_for_display(profile.get("plan_expires_at_utc")),
        )
    )


async def subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except BadRequest:
        pass
    user = query.from_user
    if not user:
        return
    lang = await get_lang(user.id, user.language_code)
    data = query.data or ""

    if data in ("sub:buy_monthly", "sub:buy_stars", "sub:buy_yookassa"):
        profile = await get_user_profile(user.id)
        if profile.get("plan_type") == PLAN_PREMIUM_LIFETIME:
            try:
                await query.answer(t("premium_lifetime_already", lang), show_alert=True)
            except BadRequest:
                pass
            return

    try:
        if data == "sub:buy_monthly":
            await _start_purchase_flow(context.bot, query.message.chat_id, user.id, lang)
            return
        if data == "sub:buy_stars":
            await _send_monthly_stars_invoice(context.bot, query.message.chat_id, user.id, lang)
            return
        if data == "sub:buy_yookassa":
            await _send_monthly_yookassa_invoice(context.bot, query.message.chat_id, user.id, lang)
            return
        if data.startswith("sub:check_yk:"):
            payment_id = data.split(":", 2)[-1].strip()
            if not payment_id:
                try:
                    await query.answer(t("payment_unknown", lang), show_alert=True)
                except BadRequest:
                    pass
                return
            await _process_yookassa_check(query, user.id, lang, payment_id)
            return
    except Exception as e:
        log_event(
            "payment.flow.failed",
            level="ERROR",
            user_id=user.id,
            callback_data=data,
            error_class=type(e).__name__,
            error=str(e),
        )
        try:
            await query.message.reply_text(t("payment_invoice_failed_generic", lang))
        except Exception:
            pass


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if not query:
        return
    user_id = query.from_user.id if query.from_user else None
    lang = await get_lang(user_id, getattr(query.from_user, "language_code", None))
    if query.currency != "XTR":
        log_event(
            "payment.precheckout.invalid_currency",
            level="WARNING",
            error_code=ERR_PAYMENT_INVALID,
            user_id=user_id,
            currency=query.currency,
        )
        await query.answer(ok=False, error_message=t("payment_invalid_currency", lang))
        return
    try:
        total_amount = int(getattr(query, "total_amount", 0))
    except Exception:
        total_amount = None
    if total_amount != int(PREMIUM_MONTHLY_STARS):
        log_event(
            "payment.precheckout.invalid_amount",
            level="WARNING",
            error_code=ERR_PAYMENT_INVALID,
            user_id=user_id,
            amount_minor=total_amount,
            expected_amount_minor=int(PREMIUM_MONTHLY_STARS),
        )
        await query.answer(ok=False, error_message=t("payment_invalid", lang))
        return
    if not _is_valid_stars_invoice_payload(getattr(query, "invoice_payload", None), user_id):
        log_event(
            "payment.precheckout.invalid_payload",
            level="WARNING",
            error_code=ERR_PAYMENT_INVALID,
            user_id=user_id,
            invoice_payload=getattr(query, "invoice_payload", None),
        )
        await query.answer(ok=False, error_message=t("payment_invalid", lang))
        return
    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not getattr(msg, "successful_payment", None) or not user:
        return
    lang = await get_lang(user.id, getattr(user, "language_code", None))
    payment = msg.successful_payment
    charge_id = payment.telegram_payment_charge_id
    if not charge_id:
        log_event(
            "payment.invalid",
            level="WARNING",
            error_code=ERR_PAYMENT_INVALID,
            user_id=user.id,
            reason="missing_charge_id",
        )
        await msg.reply_text(t("payment_invalid", lang))
        return
    valid_payment, invalid_reason = _validate_stars_payment(payment, user.id)
    if not valid_payment:
        log_event(
            "payment.invalid",
            level="WARNING",
            error_code=ERR_PAYMENT_INVALID,
            user_id=user.id,
            charge_id=charge_id,
            reason=invalid_reason,
            currency=getattr(payment, "currency", None),
            amount_minor=getattr(payment, "total_amount", None),
        )
        await msg.reply_text(t("payment_invalid", lang))
        return
    processed_now, _ = await complete_payment_once(
        PROVIDER_TELEGRAM_STARS,
        charge_id,
        user_id=user.id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        amount_minor=int(payment.total_amount),
        currency=payment.currency,
        status="succeeded",
        metadata={
            "telegram_payment_charge_id": payment.telegram_payment_charge_id,
            "provider_payment_charge_id": payment.provider_payment_charge_id,
            "invoice_payload": payment.invoice_payload,
        },
    )
    if not processed_now:
        log_event(
            "payment.duplicate_ignored",
            level="WARNING",
            error_code=ERR_PAYMENT_DUPLICATE,
            user_id=user.id,
            charge_id=charge_id,
            provider=PROVIDER_TELEGRAM_STARS,
        )
        return
    profile = await activate_or_extend_monthly(user.id, charge_id=charge_id, source=PROVIDER_TELEGRAM_STARS)
    if profile.get("plan_type") == PLAN_PREMIUM_LIFETIME:
        await msg.reply_text(t("premium_lifetime_already", lang))
        return
    await msg.reply_text(
        tf(
            "subscription_active_until",
            lang,
            expires_at_utc=format_utc_iso_for_display(profile.get("plan_expires_at_utc")),
        )
    )
