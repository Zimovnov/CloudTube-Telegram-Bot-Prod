from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app.errors import ERR_UPDATE_DUPLICATE
from app.logging_utils import log_event
from app.usage import register_update_once


async def update_dedup_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_id = getattr(update, "update_id", None)
    if update_id is None:
        return
    ok = await register_update_once(update_id)
    if ok:
        return
    user = getattr(update, "effective_user", None)
    log_event(
        "update.duplicate_ignored",
        level="WARNING",
        error_code=ERR_UPDATE_DUPLICATE,
        update_id=update_id,
        user_id=getattr(user, "id", None),
    )
    raise ApplicationHandlerStop
