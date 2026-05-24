from telegram import Update
from telegram.ext import ContextTypes

from app.access import ROLE_ADMIN, ROLE_SUPERADMIN, get_user_profile
from app.config import ASK_TRIM
from app.i18n import get_lang, t
from app.jobs import abort_user_job, clear_conversation_state
from app.legal_utils import build_public_legal_markup, get_public_legal_url, has_public_legal_urls


async def _send_public_doc(message, *, lang, doc_kind, text_key):
    url = get_public_legal_url(doc_kind)
    if not url:
        await message.reply_text(t("legal_specific_unavailable", lang))
        return
    await message.reply_text(
        f"{t(text_key, lang)}\n{url}",
        reply_markup=build_public_legal_markup(lang, kinds=(doc_kind,), row_width=1),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id if user else None
    lang = await get_lang(user_id, getattr(user, "language_code", None))
    text = f"{t('start_prompt', lang)}\n\n{t('start_hint', lang)}"
    reply_markup = None
    if has_public_legal_urls():
        text = f"{text}\n\n{t('start_legal_notice', lang)}"
        reply_markup = build_public_legal_markup(lang)
    await update.effective_message.reply_text(text, reply_markup=reply_markup)
    return ASK_TRIM


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    abort_user_job(context, user_id)
    clear_conversation_state(context, user_id)
    return await start(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id if user else None
    lang = await get_lang(uid, getattr(user, "language_code", None))
    profile = await get_user_profile(uid) if uid else {"role": None}
    can_admin = profile.get("role") in (ROLE_ADMIN, ROLE_SUPERADMIN)

    if lang == "en":
        base_help = (
            "I download media from SoundCloud/YouTube.\n\n"
            "Commands:\n"
            "/start - start\n"
            "/cancel - cancel current step\n"
            "/help - this help\n"
            "/settings - open settings\n"
            "/legal - legal documents\n"
            "/privacy - privacy policy\n"
            "/offer - public offer\n\n"
            "Just send a track or video link."
        )
    else:
        base_help = (
            "Я бот для скачивания медиа из SoundCloud/YouTube.\n\n"
            "Команды:\n"
            "/start - начать\n"
            "/cancel - отменить текущий шаг\n"
            "/help - эта справка\n"
            "/settings - открыть меню настроек\n"
            "/legal - правовые документы\n"
            "/privacy - политика обработки данных\n"
            "/offer - публичная оферта\n\n"
            "Просто пришли ссылку на трек или видео."
        )

    if can_admin:
        base_help += (
            "\n\nAdmin:\n"
            "/admin\n"
            "/admin_profile [user_id]\n"
            "/admin_ads\n"
            "/admin_ad_add <button_text> | <url> | <advertiser> | <erid> | <text>\n"
            "/admin_ad_on <ad_id>\n"
            "/admin_ad_off <ad_id>\n"
            "/admin_ad_delete <ad_id>\n"
            "/admin_ad_send <ad_id>\n"
            "/admin_broadcast\n"
            "/admin_setrole <user_id> <user|admin|superadmin> [reason]"
        )

    reply_markup = build_public_legal_markup(lang) if has_public_legal_urls() else None
    await update.effective_message.reply_text(base_help, reply_markup=reply_markup)


async def legal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id if user else None
    lang = await get_lang(user_id, getattr(user, "language_code", None))
    if not has_public_legal_urls():
        await update.effective_message.reply_text(t("legal_docs_unavailable", lang))
        return
    await update.effective_message.reply_text(
        t("legal_docs_text", lang),
        reply_markup=build_public_legal_markup(lang),
    )


async def privacy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id if user else None
    lang = await get_lang(user_id, getattr(user, "language_code", None))
    await _send_public_doc(update.effective_message, lang=lang, doc_kind="privacy", text_key="legal_privacy_text")


async def offer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id if user else None
    lang = await get_lang(user_id, getattr(user, "language_code", None))
    await _send_public_doc(update.effective_message, lang=lang, doc_kind="offer", text_key="legal_offer_text")
