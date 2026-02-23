import time

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.access import PLAN_PREMIUM_LIFETIME, activate_or_extend_monthly, get_user_profile
from app.config import PREMIUM_MONTHLY_STARS, PREMIUM_PERIOD_SECONDS, TELEGRAM_STARS_PROVIDER_TOKEN
from app.errors import ERR_PAYMENT_DUPLICATE, ERR_PAYMENT_INVALID
from app.i18n import get_lang, t, tf
from app.logging_utils import log_event
from app.usage import register_payment_once


def build_premium_markup(lang):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("buy_premium_button", lang), callback_data="sub:buy_monthly")]]
    )


def build_premium_invoice_link_markup(lang, invoice_url):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("buy_premium_button", lang), url=invoice_url)]]
    )


async def _send_monthly_invoice(bot, chat_id, user_id, lang):
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
        reply_markup=build_premium_invoice_link_markup(lang, invoice_link),
    )


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    lang = await get_lang(user.id if user else None, getattr(user, "language_code", None))
    profile = await get_user_profile(user.id)
    if profile.get("plan_type") == PLAN_PREMIUM_LIFETIME:
        await msg.reply_text(t("premium_lifetime_already", lang))
        return
    try:
        await _send_monthly_invoice(context.bot, msg.chat_id, user.id, lang)
    except Exception as e:
        log_event(
            "payment.invoice.failed",
            level="ERROR",
            user_id=user.id if user else None,
            error_class=type(e).__name__,
            error=str(e),
        )
        await msg.reply_text(tf("payment_invoice_failed", lang, error=str(e)))


async def subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    user = query.from_user
    lang = await get_lang(user.id, user.language_code)
    data = query.data or ""
    if data != "sub:buy_monthly":
        return
    profile = await get_user_profile(user.id)
    if profile.get("plan_type") == PLAN_PREMIUM_LIFETIME:
        try:
            await query.answer(t("premium_lifetime_already", lang), show_alert=True)
        except BadRequest:
            pass
        return
    try:
        await _send_monthly_invoice(context.bot, query.message.chat_id, user.id, lang)
    except Exception as e:
        log_event(
            "payment.invoice.failed",
            level="ERROR",
            user_id=user.id,
            error_class=type(e).__name__,
            error=str(e),
        )
        try:
            await query.message.reply_text(tf("payment_invoice_failed", lang, error=str(e)))
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
    if not await register_payment_once(charge_id):
        log_event(
            "payment.duplicate_ignored",
            level="WARNING",
            error_code=ERR_PAYMENT_DUPLICATE,
            user_id=user.id,
            charge_id=charge_id,
        )
        return
    if payment.currency != "XTR":
        log_event(
            "payment.invalid",
            level="WARNING",
            error_code=ERR_PAYMENT_INVALID,
            user_id=user.id,
            charge_id=charge_id,
            currency=payment.currency,
        )
        await msg.reply_text(t("payment_invalid", lang))
        return
    profile = await activate_or_extend_monthly(user.id, charge_id=charge_id, source="telegram_stars")
    if profile.get("plan_type") == PLAN_PREMIUM_LIFETIME:
        await msg.reply_text(t("premium_lifetime_already", lang))
        return
    await msg.reply_text(
        tf(
            "subscription_active_until",
            lang,
            expires_at_utc=profile.get("plan_expires_at_utc") or "-",
        )
    )
