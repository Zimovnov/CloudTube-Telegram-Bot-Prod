import asyncio

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    TypeHandler,
    filters,
)

from app.access import bootstrap_superadmin_sync
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
from app.handlers.admin import (
    admin_grant_month,
    admin_help,
    admin_operation_callback,
    admin_profile,
    admin_reset_limit,
    admin_reset_premium,
    admin_set_plan,
    admin_set_role,
)
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
from app.handlers.metadata import metadata_callback, metadata_expiry_sweeper, metadata_text_input_handler
from app.handlers.payments import (
    precheckout_handler,
    premium_command,
    subscription_callback,
    successful_payment_handler,
)
from app.handlers.security import update_dedup_guard
from app.handlers.settings import settings_callback, settings_menu
from app.i18n import setup_bot_commands
from app.jobs import init_redis_client, resolve_ffmpeg_path
from app.logging_utils import log_event
from app.state import BACKGROUND_JOB_TASKS, BACKGROUND_JOB_TASKS_LOCK


def _track_background_task(task):
    with BACKGROUND_JOB_TASKS_LOCK:
        BACKGROUND_JOB_TASKS.add(task)

    def _done(done_task):
        with BACKGROUND_JOB_TASKS_LOCK:
            BACKGROUND_JOB_TASKS.discard(done_task)
        try:
            exc = done_task.exception()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log_event(
                "background.task.done_error",
                level="WARNING",
                error_class=type(e).__name__,
                error=str(e),
            )
            return
        if exc:
            log_event(
                "background.task.unhandled_exception",
                level="ERROR",
                error_class=type(exc).__name__,
                error=str(exc),
            )

    task.add_done_callback(_done)


async def _post_init(application):
    await setup_bot_commands(application)
    task = asyncio.create_task(metadata_expiry_sweeper(application))
    application.bot_data["metadata_expiry_sweeper_task"] = task
    _track_background_task(task)


async def _post_shutdown(application):
    task = application.bot_data.pop("metadata_expiry_sweeper_task", None)
    if task:
        task.cancel()
        try:
            await task
        except Exception:
            pass


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
    bootstrap_superadmin_sync()
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

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
            CommandHandler("premium", premium_command),
        ],
    )

    app.add_handler(TypeHandler(Update, update_dedup_guard), group=-10)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, metadata_text_input_handler), group=-1)
    app.add_handler(conv)

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("settings", settings_menu))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("admin", admin_help))
    app.add_handler(CommandHandler("admin_profile", admin_profile))
    app.add_handler(CommandHandler("admin_setplan", admin_set_plan))
    app.add_handler(CommandHandler("admin_setrole", admin_set_role))
    app.add_handler(CommandHandler("admin_resetlimit", admin_reset_limit))
    app.add_handler(CommandHandler("admin_resetpremium", admin_reset_premium))
    app.add_handler(CommandHandler("admin_grantmonth", admin_grant_month))

    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^settings:"))
    app.add_handler(CallbackQueryHandler(trim_callback, pattern="^trim_"))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern="^cancel"))
    app.add_handler(CallbackQueryHandler(yt_choice_callback, pattern="^yt_"))
    app.add_handler(CallbackQueryHandler(subscription_callback, pattern="^sub:"))
    app.add_handler(CallbackQueryHandler(admin_operation_callback, pattern="^adminop:"))
    app.add_handler(CallbackQueryHandler(metadata_callback, pattern="^meta:"))

    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    app.add_error_handler(error_handler)

    log_event("bot.started", level="INFO", message="Bot started. Waiting for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()
