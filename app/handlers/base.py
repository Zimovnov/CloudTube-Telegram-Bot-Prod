from telegram import Update
from telegram.ext import ContextTypes

from app.access import ROLE_ADMIN, ROLE_SUPERADMIN, get_user_profile
from app.config import ASK_TRIM
from app.i18n import get_lang, t
from app.jobs import abort_user_job, clear_conversation_state


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id if user else None
    lang = await get_lang(user_id, getattr(user, "language_code", None))
    await update.effective_message.reply_text(
        f"{t('start_prompt', lang)}\n\n{t('start_hint', lang)}"
    )
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
    base_help = (
        "Я бот для скачивания медиа из SoundCloud/YouTube.\n\n"
        "Команды:\n"
        "/start — начать\n"
        "/cancel — отменить текущий шаг\n"
        "/help — эта справка\n"
        "/settings — открыть меню настроек\n"
        "/premium — оформить Premium\n\n"
        "Просто пришли ссылку на трек или видео."
    )
    if lang == "en":
        base_help = (
            "I download media from SoundCloud/YouTube.\n\n"
            "Commands:\n"
            "/start — start\n"
            "/cancel — cancel current step\n"
            "/help — this help\n"
            "/settings — open settings\n"
            "/premium — buy Premium\n\n"
            "Just send a track or video link."
        )
    if can_admin:
        base_help += (
            "\n\nAdmin:\n"
            "/admin\n"
            "/admin_profile [user_id]\n"
            "/admin_setplan <user_id> <free|premium_monthly|premium_lifetime> [reason]\n"
            "/admin_setrole <user_id> <user|admin|superadmin> [reason]\n"
            "/admin_resetlimit <user_id> [YYYYMM] [reason]\n"
            "/admin_resetpremium <user_id> [reason]\n"
            "/admin_grantmonth <user_id> [reason]"
        )
    await update.effective_message.reply_text(base_help)
