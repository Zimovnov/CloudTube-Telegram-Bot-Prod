import time

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.access import PLAN_PREMIUM_LIFETIME, get_user_profile
from app.config import (
    FREE_MONTHLY_LIMIT,
    PREMIUM_MAX_DURATION_SECONDS,
    PREMIUM_MONTHLY_STARS,
    PREMIUM_PERIOD_SECONDS,
    ROBOKASSA_CURRENCY,
    ROBOKASSA_PREMIUM_MONTHLY_AMOUNT,
    TELEGRAM_STARS_PROVIDER_TOKEN,
)
from app.errors import ERR_PAYMENT_DUPLICATE, ERR_PAYMENT_INVALID
from app.i18n import get_lang, t, tf
from app.logging_utils import log_event
from app.payment_service import (
    PROVIDER_ROBOKASSA,
    PROVIDER_TELEGRAM_STARS,
    allow_payment_callback,
    create_or_reuse_robokassa_payment,
    finalize_stars_payment,
    payments_available,
    validate_robokassa_verified_payload,
)
from app.payments_store import get_payment
from app.robokassa import is_robokassa_configured


def _stars_enabled():
    return int(PREMIUM_MONTHLY_STARS) > 0


def _resolve_payment_methods():
    if not payments_available():
        return False, False
    return _stars_enabled(), is_robokassa_configured()


def _premium_duration_days():
    return max(1, int(PREMIUM_PERIOD_SECONDS // (24 * 60 * 60)))


def _build_premium_overview_text(lang):
    has_stars, has_robokassa = _resolve_payment_methods()
    if has_robokassa:
        price_line = tf(
            "premium_price_rub_line",
            lang,
            amount=ROBOKASSA_PREMIUM_MONTHLY_AMOUNT,
            currency=ROBOKASSA_CURRENCY,
            days=_premium_duration_days(),
        )
    elif has_stars:
        price_line = tf(
            "premium_price_stars_line",
            lang,
            stars=PREMIUM_MONTHLY_STARS,
            days=_premium_duration_days(),
        )
    else:
        price_line = tf(
            "premium_price_rub_line",
            lang,
            amount=ROBOKASSA_PREMIUM_MONTHLY_AMOUNT,
            currency=ROBOKASSA_CURRENCY,
            days=_premium_duration_days(),
        )
    return tf(
        "premium_overview_text",
        lang,
        price_line=price_line,
        free_limit=FREE_MONTHLY_LIMIT,
        max_hours=int(PREMIUM_MAX_DURATION_SECONDS / 3600),
    )


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


def _validate_robokassa_status_payload(status_payload, user_id):
    valid, reason, _, _ = validate_robokassa_verified_payload(status_payload, expected_user_id=user_id)
    return valid, reason


def build_premium_markup(lang):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t("buy_premium_button", lang), callback_data="sub:buy_monthly")]])


def _build_payment_methods_markup(lang, has_stars, has_robokassa):
    rows = []
    if has_stars:
        rows.append([InlineKeyboardButton(t("buy_premium_stars_button", lang), callback_data="sub:buy_stars")])
    if has_robokassa:
        rows.append([InlineKeyboardButton(t("buy_premium_robokassa_button", lang), callback_data="sub:buy_robokassa")])
    return InlineKeyboardMarkup(rows)


def _build_stars_invoice_markup(lang, invoice_url):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t("buy_premium_stars_button", lang), url=invoice_url)]])


def _build_robokassa_markup(lang, payment_url, payment_id):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("payment_pay_now_button", lang), url=payment_url)],
            [InlineKeyboardButton(t("payment_check_button", lang), callback_data=f"sub:check_rk:{payment_id}")],
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


async def _send_monthly_robokassa_invoice(bot, chat_id, user_id, lang):
    session = await create_or_reuse_robokassa_payment(user_id)
    payment_id = session.get("payment_id")
    payment_url = session.get("payment_url")
    if not payment_id or not payment_url:
        raise RuntimeError("Robokassa payment session is incomplete.")
    log_event(
        "payment.robokassa.created",
        level="INFO",
        user_id=user_id,
        payment_id=payment_id,
        status=session.get("status"),
    )
    await bot.send_message(
        chat_id=chat_id,
        text=tf(
            "premium_robokassa_desc",
            lang,
            amount=ROBOKASSA_PREMIUM_MONTHLY_AMOUNT,
            currency=ROBOKASSA_CURRENCY,
        ),
        reply_markup=_build_robokassa_markup(lang, payment_url, payment_id),
    )


async def _start_purchase_flow(bot, chat_id, user_id, lang):
    has_stars, has_robokassa = _resolve_payment_methods()
    if has_stars and has_robokassa:
        await bot.send_message(
            chat_id=chat_id,
            text=t("premium_choose_method", lang),
            reply_markup=_build_payment_methods_markup(lang, has_stars, has_robokassa),
        )
        return
    if has_stars:
        await _send_monthly_stars_invoice(bot, chat_id, user_id, lang)
        return
    if has_robokassa:
        await _send_monthly_robokassa_invoice(bot, chat_id, user_id, lang)
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
    text = _build_premium_overview_text(lang)
    has_stars, has_robokassa = _resolve_payment_methods()
    if has_stars or has_robokassa:
        await msg.reply_text(text, reply_markup=build_premium_markup(lang))
        return
    await msg.reply_text(f"{text}\n\n{t('payment_method_unavailable', lang)}")


async def _process_robokassa_check(query, user_id, lang, payment_id):
    record = await get_payment(PROVIDER_ROBOKASSA, payment_id)
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
    status = str(record.get("status") or "").strip().lower()
    if bool(record.get("is_processed")) or status == "succeeded":
        try:
            await query.answer(t("payment_already_processed", lang), show_alert=True)
        except BadRequest:
            pass
        return
    if status in ("invalid", "failed"):
        try:
            await query.answer(t("payment_invalid", lang), show_alert=True)
        except BadRequest:
            pass
        return
    if status in ("cancelled", "canceled"):
        try:
            await query.answer(t("payment_cancelled", lang), show_alert=True)
        except BadRequest:
            pass
        return
    try:
        await query.answer(t("payment_pending", lang), show_alert=True)
    except BadRequest:
        pass


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

    if data in ("sub:buy_monthly", "sub:buy_stars", "sub:buy_robokassa"):
        profile = await get_user_profile(user.id)
        if profile.get("plan_type") == PLAN_PREMIUM_LIFETIME:
            try:
                await query.answer(t("premium_lifetime_already", lang), show_alert=True)
            except BadRequest:
                pass
            return

    try:
        if data == "sub:buy_monthly":
            if not allow_payment_callback(user.id, "buy_monthly"):
                return
            await _start_purchase_flow(context.bot, query.message.chat_id, user.id, lang)
            return
        if data == "sub:buy_stars":
            if not allow_payment_callback(user.id, "buy_stars"):
                return
            await _send_monthly_stars_invoice(context.bot, query.message.chat_id, user.id, lang)
            return
        if data == "sub:buy_robokassa":
            if not allow_payment_callback(user.id, "buy_robokassa"):
                return
            await _send_monthly_robokassa_invoice(context.bot, query.message.chat_id, user.id, lang)
            return
        if data.startswith("sub:check_rk:"):
            payment_id = data.split(":", 2)[-1].strip()
            if not payment_id:
                try:
                    await query.answer(t("payment_unknown", lang), show_alert=True)
                except BadRequest:
                    pass
                return
            await _process_robokassa_check(query, user.id, lang, payment_id)
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
    processed_now, _, _ = await finalize_stars_payment(
        context.application,
        user_id=user.id,
        charge_id=charge_id,
        amount_minor=int(payment.total_amount),
        currency=payment.currency,
        metadata={
            "telegram_payment_charge_id": payment.telegram_payment_charge_id,
            "provider_payment_charge_id": payment.provider_payment_charge_id,
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
