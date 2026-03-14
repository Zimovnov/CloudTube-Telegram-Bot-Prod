import asyncio

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app.access import is_premium_plan
from app.errors import ERR_METADATA_INVALID_INPUT, ERR_METADATA_NOT_ALLOWED, ERR_METADATA_SESSION_EXPIRED
from app.i18n import get_lang, t, tf
from app.logging_utils import log_event
from app.metadata_store import (
    apply_changes,
    clear_input_mode_sync,
    close_session,
    create_session,
    expire_due_sessions,
    get_active_session_id_sync,
    get_changed_summary,
    get_input_mode_sync,
    get_session,
    set_input_mode_sync,
    update_field_sync,
)


def _build_start_markup(lang, session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("metadata_edit_button", lang), callback_data=f"meta:open:{session_id}"),
                InlineKeyboardButton(t("metadata_keep_button", lang), callback_data=f"meta:keep:{session_id}"),
            ]
        ]
    )


def _build_menu_markup(lang, session):
    session_id = session["session_id"]
    rows = [
        [
            InlineKeyboardButton(t("metadata_change_title", lang), callback_data=f"meta:field_title:{session_id}"),
            InlineKeyboardButton(t("metadata_change_artist", lang), callback_data=f"meta:field_artist:{session_id}"),
        ],
        [InlineKeyboardButton(t("metadata_cancel", lang), callback_data=f"meta:cancel:{session_id}")],
    ]
    if get_changed_summary(session).get("changed"):
        rows.insert(1, [InlineKeyboardButton(t("metadata_get_file", lang), callback_data=f"meta:apply:{session_id}")])
    return InlineKeyboardMarkup(rows)


def _build_back_markup(lang, session_id):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("back", lang), callback_data=f"meta:back:{session_id}")]]
    )


async def maybe_offer_metadata_edit(
    *,
    context,
    message,
    user_id,
    lang,
    plan_type,
    settings,
    file_path,
    title,
    artist,
    source_job_id,
):
    if not is_premium_plan(plan_type):
        return None
    if not bool((settings or {}).get("metadata_prompt_enabled", True)):
        return None
    session = await create_session(
        user_id=user_id,
        src_file_path=file_path,
        title=title,
        artist=artist,
        source_job_id=source_job_id,
    )
    await message.reply_text(
        t("metadata_prompt_intro", lang),
        reply_markup=_build_start_markup(lang, session["session_id"]),
    )
    return session


async def cancel_active_metadata_edit(user_id, reason="user_cancelled"):
    if user_id is None:
        return False
    clear_input_mode_sync(user_id)
    session_id = get_active_session_id_sync(user_id)
    if not session_id:
        return False
    return bool(await close_session(session_id, reason=reason))


async def _render_edit_menu(query, lang, session):
    summary = get_changed_summary(session)
    text = tf(
        "metadata_menu_summary",
        lang,
        title=summary.get("title") or "-",
        artist=summary.get("artist") or "-",
    )
    try:
        await query.edit_message_text(text, reply_markup=_build_menu_markup(lang, session))
    except Exception:
        await query.message.reply_text(text, reply_markup=_build_menu_markup(lang, session))


async def metadata_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    user = query.from_user
    lang = await get_lang(user.id, user.language_code)
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        return
    _, action, session_id = parts
    session = await get_session(session_id, touch=True)
    if not session:
        try:
            await query.answer(t("metadata_session_expired", lang), show_alert=True)
        except BadRequest:
            pass
        return
    if isinstance(session, dict) and session.get("error") == ERR_METADATA_SESSION_EXPIRED:
        await query.message.reply_text(t("metadata_session_expired", lang))
        return
    owner_id = int(session.get("user_id") or 0)
    if owner_id != user.id:
        log_event(
            "security.suspicious.metadata_foreign_access",
            level="WARNING",
            error_code=ERR_METADATA_NOT_ALLOWED,
            user_id=user.id,
            owner_id=owner_id,
            session_id=session_id,
        )
        try:
            await query.answer(t("button_not_for_you", lang), show_alert=True)
        except BadRequest:
            pass
        return

    if action == "keep":
        clear_input_mode_sync(user.id)
        await close_session(session_id, reason="user_kept_original")
        try:
            await query.edit_message_text(t("metadata_kept_original", lang))
        except Exception:
            await query.message.reply_text(t("metadata_kept_original", lang))
        return

    if action == "open":
        clear_input_mode_sync(user.id)
        await _render_edit_menu(query, lang, session)
        return

    if action == "field_title":
        set_input_mode_sync(user.id, session_id, "title")
        await query.message.reply_text(
            t("metadata_enter_title", lang),
            reply_markup=_build_back_markup(lang, session_id),
        )
        return

    if action == "field_artist":
        set_input_mode_sync(user.id, session_id, "artist")
        await query.message.reply_text(
            t("metadata_enter_artist", lang),
            reply_markup=_build_back_markup(lang, session_id),
        )
        return

    if action == "back":
        clear_input_mode_sync(user.id)
        session = await get_session(session_id, touch=True)
        if not session or session.get("error") == ERR_METADATA_SESSION_EXPIRED:
            await query.message.reply_text(t("metadata_session_expired", lang))
            return
        await _render_edit_menu(query, lang, session)
        return

    if action == "cancel":
        clear_input_mode_sync(user.id)
        await close_session(session_id, reason="user_cancelled")
        try:
            await query.edit_message_text(t("metadata_cancelled", lang))
        except Exception:
            await query.message.reply_text(t("metadata_cancelled", lang))
        return

    if action == "apply":
        clear_input_mode_sync(user.id)
        result = await apply_changes(session_id)
        if not result.get("ok"):
            code = result.get("error_code")
            if code == ERR_METADATA_SESSION_EXPIRED:
                await query.message.reply_text(t("metadata_session_expired", lang))
            elif code == ERR_METADATA_INVALID_INPUT:
                await query.message.reply_text(t("metadata_need_changes", lang))
            else:
                await query.message.reply_text(tf("metadata_apply_failed", lang, error=result.get("error") or code))
            return
        file_path = result.get("file_path")
        title = result.get("title")
        artist = result.get("artist")
        try:
            with open(file_path, "rb") as f:
                await query.message.reply_audio(f, title=title, performer=artist, caption=tf("metadata_applied_caption", lang, title=title, artist=artist))
            await close_session(session_id, reason="applied_sent")
            try:
                await query.edit_message_text(t("metadata_done_once", lang))
            except Exception:
                await query.message.reply_text(t("metadata_done_once", lang))
        except Exception as e:
            log_event(
                "metadata.apply.send_failed",
                level="ERROR",
                user_id=user.id,
                session_id=session_id,
                error_class=type(e).__name__,
                error=str(e),
            )
            await query.message.reply_text(t("file_send_fail", lang))
        return


async def metadata_text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    input_state = get_input_mode_sync(user.id)
    if not input_state:
        return

    lang = await get_lang(user.id, getattr(user, "language_code", None))
    session_id = input_state.get("session_id")
    field = input_state.get("field")
    if not session_id or field not in ("title", "artist"):
        clear_input_mode_sync(user.id)
        raise ApplicationHandlerStop

    session = await get_session(session_id, touch=True)
    if not session or session.get("error") == ERR_METADATA_SESSION_EXPIRED:
        clear_input_mode_sync(user.id)
        await message.reply_text(t("metadata_session_expired", lang))
        raise ApplicationHandlerStop
    if int(session.get("user_id") or 0) != user.id:
        clear_input_mode_sync(user.id)
        raise ApplicationHandlerStop

    text = (message.text or "").strip()
    if text.lower() in ("назад", "back"):
        clear_input_mode_sync(user.id)
        fake_query = type("_Q", (), {"edit_message_text": message.reply_text, "message": message})
        await _render_edit_menu(fake_query, lang, session)
        raise ApplicationHandlerStop

    updated = update_field_sync(session_id, field, text)
    if not updated.get("ok"):
        error_key = updated.get("error_key") or "metadata_invalid_input"
        await message.reply_text(t(error_key, lang))
        raise ApplicationHandlerStop

    clear_input_mode_sync(user.id)
    await message.reply_text(t("metadata_value_saved", lang))
    session = await get_session(session_id, touch=True)
    if session and not session.get("error"):
        await message.reply_text(
            tf(
                "metadata_menu_summary",
                lang,
                title=get_changed_summary(session).get("title") or "-",
                artist=get_changed_summary(session).get("artist") or "-",
            ),
            reply_markup=_build_menu_markup(lang, session),
        )
    raise ApplicationHandlerStop


async def metadata_expiry_sweeper(application):
    while True:
        try:
            expired = await expire_due_sessions()
            for item in expired:
                uid = item.get("user_id")
                if not uid:
                    continue
                lang = await get_lang(uid, None)
                try:
                    await application.bot.send_message(chat_id=uid, text=t("metadata_session_expired", lang))
                except Exception:
                    pass
        except asyncio.CancelledError:
            return
        except Exception as e:
            log_event(
                "metadata.expiry_sweeper.failed",
                level="ERROR",
                error_class=type(e).__name__,
                error=str(e),
            )
        await asyncio.sleep(30)
