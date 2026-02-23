from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.config import (
    ASK_RANGE,
    ASK_RANGE_YT,
    ASK_TRIM,
    ASK_TRIM_YT,
    ASK_TYPE,
    FFMPEG_REQUIRED_ON_STARTUP,
    LOG_HASH_SALT_STRICT,
    LOG_USER_HASH_SALT,
    TOKEN,
)
from app.errors import ERR_FFMPEG_MISSING, ERR_SECURITY_WEAK_HASH_SALT
from app.handlers.base import help_cmd, restart, start
from app.handlers.downloads import (
    cancel_callback,
    cancel_command,
    error_handler,
    get_link,
    trim_callback,
    trim_range,
    yt_choice_callback,
)
from app.handlers.settings import settings_callback, settings_menu
from app.i18n import setup_bot_commands
from app.jobs import init_redis_client, resolve_ffmpeg_path
from app.logging_utils import log_event


def main():
    ffmpeg_path = resolve_ffmpeg_path()
    if not ffmpeg_path:
        msg = "FFmpeg не найден в PATH и не задан через FFMPEG_PATH/FFMPEG_BIN/FFMPEG_DIR."
        if FFMPEG_REQUIRED_ON_STARTUP:
            raise RuntimeError(msg)
        log_event(
            "ffmpeg.missing.startup",
            level="WARNING",
            error_code=ERR_FFMPEG_MISSING,
            message=msg,
        )
    else:
        log_event("ffmpeg.detected", level="INFO", ffmpeg_path=ffmpeg_path)

    if LOG_USER_HASH_SALT == "cloudtube_bot_default_salt":
        msg = "LOG_USER_HASH_SALT is not set, using weak default hash salt."
        if LOG_HASH_SALT_STRICT:
            raise RuntimeError(msg)
        log_event(
            "security.log_hash_salt_default",
            level="WARNING",
            error_code=ERR_SECURITY_WEAK_HASH_SALT,
            message=msg,
        )

    init_redis_client()
    app = ApplicationBuilder().token(TOKEN).post_init(setup_bot_commands).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_TRIM: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_link)],
            ASK_RANGE: [
                CallbackQueryHandler(trim_callback, pattern="^trim_"),
                CallbackQueryHandler(cancel_callback, pattern="^cancel"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, trim_range),
            ],
            ASK_TYPE: [
                CallbackQueryHandler(yt_choice_callback, pattern="^yt_"),
                CallbackQueryHandler(cancel_callback, pattern="^cancel"),
            ],
            ASK_TRIM_YT: [
                CallbackQueryHandler(trim_callback, pattern="^trim_"),
                CallbackQueryHandler(cancel_callback, pattern="^cancel"),
            ],
            ASK_RANGE_YT: [MessageHandler(filters.TEXT & ~filters.COMMAND, trim_range)],
        },
        fallbacks=[
            CommandHandler("start", restart),
            CommandHandler("cancel", cancel_command),
            CommandHandler("help", help_cmd),
            CommandHandler("settings", settings_menu),
        ],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("settings", settings_menu))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^settings:"))
    app.add_handler(CallbackQueryHandler(trim_callback, pattern="^trim_"))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern="^cancel"))
    app.add_handler(CallbackQueryHandler(yt_choice_callback, pattern="^yt_"))
    app.add_error_handler(error_handler)

    log_event("bot.started", level="INFO", message="Bot started. Waiting for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()
