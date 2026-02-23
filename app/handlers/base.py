from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from app.config import ALLOWED_USERS, ASK_TRIM
from app.errors import ERR_AUTH_DENIED
from app.i18n import get_lang, t
from app.jobs import abort_user_job, clear_conversation_state
from app.logging_utils import log_event


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text(t("not_allowed", "ru"))
        log_event(
            "auth.denied.start",
            level="WARNING",
            error_code=ERR_AUTH_DENIED,
            user_id=user_id,
        )
        return ConversationHandler.END

    lang = await get_lang(user_id, user.language_code)
    await update.message.reply_text(
        f"{t('start_prompt', lang)}\n\n{t('start_hint', lang)}"
    )
    return ASK_TRIM


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    abort_user_job(context, user_id)
    clear_conversation_state(context, user_id)
    return await start(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = await get_lang(uid, getattr(update.effective_user, "language_code", None))
    await update.message.reply_text(
        (
            "Я бот для скачивания медиафайлов из SoundCloud/YouTube с обрезкой.\n\n"
            "Команды:\n"
            "/start — начать\n"
            "/cancel — отменить текущий шаг\n"
            "/help — эта справка\n"
            "/settings — открыть меню настроек\n\n"
            "Просто пришли ссылку на трек или видео."
        )
        if lang == "ru"
        else (
            "I'm a bot to download mediafiles from SoundCloud/YouTube with trimming.\n\n"
            "Commands:\n"
            "/start — start\n"
            "/cancel — cancel current step\n"
            "/help — this help\n"
            "/settings — open settings\n\n"
            "Just send a track or video link."
        )
    )
