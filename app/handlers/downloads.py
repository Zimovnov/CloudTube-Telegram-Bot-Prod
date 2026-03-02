import asyncio
import os
import re
import tempfile
import threading
import time
import uuid

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TimedOut
from telegram.ext import ContextTypes
from yt_dlp import YoutubeDL

from app.config import (
    ASK_RANGE,
    ASK_RANGE_YT,
    ASK_TRIM,
    ASK_TRIM_YT,
    ASK_TYPE,
    MAX_DURATION,
    YTDLP_COOKIES_FILE,
    YTDLP_JS_RUNTIMES_MAP,
    YTDLP_META_SOCKET_TIMEOUT,
    YTDLP_REMOTE_COMPONENTS,
)
from app.access import PLAN_FREE, get_user_profile, is_premium_plan
from app.errors import (
    ERR_COOLDOWN_ACTIVE,
    ERR_DOWNLOAD_FAILED,
    ERR_FILE_NOT_FOUND,
    ERR_FFMPEG_MISSING,
    ERR_FREE_LIMIT_REACHED,
    ERR_HTTP_NOT_FOUND,
    ERR_INVALID_LINK,
    ERR_INVALID_RANGE_FORMAT,
    ERR_INVALID_RANGE_ORDER,
    ERR_JOB_ALREADY_RUNNING,
    ERR_NETWORK,
    ERR_STALE_BUTTON,
    ERR_TELEGRAM_TIMEOUT,
    ERR_TIMEOUT,
    ERR_WORKER_CANCELLED,
    ERR_WORKER_FAILED,
    ERR_WORKER_FILE_NOT_FOUND,
    ERR_WORKER_RUNTIME,
    ERR_WORKER_STALLED,
    ERR_WORKER_TRIM_FAILED,
    ERR_WORKER_TRIM_RANGE_INVALID,
    ERR_WORKER_UNKNOWN_MODE,
    ERR_WORKER_UPLOAD_BAD_RESPONSE,
    ERR_WORKER_UPLOAD_FAILED,
    ERR_WORKER_UPLOAD_HTTP,
)
from app.handlers.metadata import maybe_offer_metadata_edit
from app.handlers.payments import build_premium_markup
from app.i18n import get_lang, t, tf
from app.jobs import (
    abort_user_job,
    clear_conversation_state,
    acquire_download_cooldown,
    finish_job,
    register_active_download_task,
    register_active_worker_future,
    register_scheduled_download_task,
    request_active_download_cancel,
    resolve_ffmpeg_path,
    start_job,
    unregister_active_download_task,
    unregister_active_worker_future,
)
from app.logging_utils import classify_exception_error_code, log_event, worker_error
from app.policy import resolve_user_download_policy
from app.settings_store import (
    get_user_logs_enabled,
    get_user_logs_enabled_sync,
    get_user_settings,
    log_user_event_if_enabled,
)
from app.usage import increment_usage_success_once
from app.services.worker import _progress_consumer, _progress_watcher, _stall_watchdog, _sync_worker
from app.state import (
    BACKGROUND_JOB_TASKS,
    BACKGROUND_JOB_TASKS_LOCK as _BACKGROUND_JOB_TASKS_LOCK,
    JOB_PROGRESS,
)
# ===== Обработчик ошибок =====
def handle_error(e, user_name: str, lang: str = "ru", user_id=None, user_logs_enabled=None):
    err_text = str(e)
    error_code = classify_exception_error_code(e, err_text)
    if user_logs_enabled is None:
        if user_id is None:
            user_logs_enabled = True
        else:
            user_logs_enabled = get_user_logs_enabled_sync(user_id, default=False)
    if user_logs_enabled:
        log_event(
            "user.error.handled",
            level="ERROR",
            error_code=error_code,
            user_id=user_id,
            error_class=type(e).__name__,
            error=err_text,
        )

    if error_code in (ERR_TIMEOUT, ERR_TELEGRAM_TIMEOUT):
        return t("err_timeout", lang)
    elif error_code == ERR_FILE_NOT_FOUND:
        return t("err_file_not_found", lang)
    elif error_code == ERR_HTTP_NOT_FOUND:
        return t("err_not_available", lang)
    elif error_code == ERR_DOWNLOAD_FAILED:
        return t("err_download", lang)
    elif error_code == ERR_NETWORK:
        return t("err_network", lang)
    elif error_code == ERR_WORKER_STALLED:
        return t("err_timeout", lang)
    elif error_code == ERR_WORKER_CANCELLED:
        return t("cancelled_send_link", lang)
    elif error_code == ERR_STALE_BUTTON:
        return t("err_stale_button", lang)
    else:
        return tf("err_unknown", lang, name=type(e).__name__)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    error_code = classify_exception_error_code(err)
    user = getattr(update, "effective_user", None) if update else None
    user_logs_enabled = True if user is None else await get_user_logs_enabled(user.id, default=False)
    if user_logs_enabled:
        log_event(
            "bot.error_handler",
            level="ERROR",
            error_code=error_code,
            user_id=getattr(user, "id", None),
            error_class=type(err).__name__,
            error=str(err),
        )

    # Если Telegram API уже выдал timeout, не отправляем дополнительное сообщение.
    if isinstance(err, TimedOut):
        return

    msg = getattr(update, "effective_message", None)
    if not msg:
        return

    try:
        lang = await get_lang(user.id, getattr(user, "language_code", None)) if user else "ru"
        await msg.reply_text(t("generic_error", lang))
    except Exception:
        pass


def _track_background_job_task(task, user_id=None, platform=None, yt_type=None):
    with _BACKGROUND_JOB_TASKS_LOCK:
        BACKGROUND_JOB_TASKS.add(task)

    def _done_callback(done_task):
        with _BACKGROUND_JOB_TASKS_LOCK:
            BACKGROUND_JOB_TASKS.discard(done_task)
        try:
            exc = done_task.exception()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log_event(
                "job.background_runner.done_error",
                level="WARNING",
                user_id=user_id,
                platform=platform,
                yt_type=yt_type,
                error_class=type(e).__name__,
                error=str(e),
            )
            return
        if exc:
            log_event(
                "job.background_runner.unhandled_exception",
                level="ERROR",
                error_code=classify_exception_error_code(exc),
                user_id=user_id,
                platform=platform,
                yt_type=yt_type,
                error_class=type(exc).__name__,
                error=str(exc),
            )

    task.add_done_callback(_done_callback)


def schedule_download_background(
    update,
    context,
    url,
    platform,
    user_id,
    user_name,
    lang,
    yt_type="audio",
    start=None,
    end=None,
    message=None,
    prompt_chat_id=None,
    prompt_message_id=None,
    plan_snapshot=PLAN_FREE,
    max_duration_seconds=MAX_DURATION,
):
    target_message = message or getattr(update, "effective_message", None)
    if target_message is None:
        finish_job(context, user_id)
        log_event(
            "job.background_runner.target_message_missing",
            level="ERROR",
            error_code=ERR_WORKER_RUNTIME,
            user_id=user_id,
            platform=platform,
            yt_type=yt_type,
        )
        return None

    async def _runner():
        register_active_download_task(user_id)
        try:
            await download_content(
                update,
                context,
                url,
                platform=platform,
                start=start,
                end=end,
                yt_type=yt_type,
                message=target_message,
                plan_snapshot=plan_snapshot,
                max_duration_seconds=max_duration_seconds,
            )
            try:
                profile = await get_user_profile(user_id)
                policy = await resolve_user_download_policy(profile)
                if policy["blocked_by_limit"]:
                    await target_message.reply_text(
                        tf(
                            "free_limit_reached",
                            lang,
                            count=policy["usage_count"],
                            limit=policy["free_limit"],
                        ),
                        reply_markup=build_premium_markup(lang),
                    )
                else:
                    await target_message.reply_text(t("send_another", lang))
            except Exception:
                pass
        except asyncio.CancelledError:
            return
        except Exception as e:
            friendly = handle_error(e, user_name, lang, user_id=user_id)
            try:
                await target_message.reply_text(friendly)
            except Exception:
                pass
        finally:
            if prompt_chat_id and prompt_message_id:
                try:
                    await context.bot.delete_message(chat_id=prompt_chat_id, message_id=prompt_message_id)
                except Exception:
                    pass
            finish_job(context, user_id)
            unregister_active_download_task(user_id)

    try:
        task = asyncio.create_task(_runner())
    except Exception as e:
        finish_job(context, user_id)
        log_event(
            "job.background_runner.create_task_failed",
            level="ERROR",
            error_code=ERR_WORKER_RUNTIME,
            user_id=user_id,
            platform=platform,
            yt_type=yt_type,
            error_class=type(e).__name__,
            error=str(e),
        )
        return None
    register_scheduled_download_task(user_id, task)
    _track_background_job_task(task, user_id=user_id, platform=platform, yt_type=yt_type)
    return task


async def _start_download_flow(
    update,
    context,
    *,
    owner_id,
    user_name,
    lang,
    platform,
    url,
    yt_type,
    message,
    user_logs_enabled=None,
    start=None,
    end=None,
    prompt_chat_id=None,
    prompt_message_id=None,
    announce_coro=None,
):
    profile = await get_user_profile(owner_id)
    policy = await resolve_user_download_policy(profile)
    plan_snapshot = policy["plan_type"]
    max_duration_seconds = int(policy["max_duration_seconds"])
    if policy["blocked_by_limit"]:
        await log_user_event_if_enabled(
            owner_id,
            "limit.free.blocked",
            level="WARNING",
            error_code=ERR_FREE_LIMIT_REACHED,
            user_logs_enabled=user_logs_enabled,
            usage_count=policy["usage_count"],
            usage_limit=policy["free_limit"],
        )
        log_event(
            "limit.free.blocked",
            level="WARNING",
            error_code=ERR_FREE_LIMIT_REACHED,
            user_id=owner_id,
            usage_count=policy["usage_count"],
            usage_limit=policy["free_limit"],
        )
        try:
            await message.reply_text(
                tf(
                    "free_limit_reached",
                    lang,
                    count=policy["usage_count"],
                    limit=policy["free_limit"],
                ),
                reply_markup=build_premium_markup(lang),
            )
        except Exception:
            pass
        clear_conversation_state(context, owner_id)
        return ASK_TRIM

    if not start_job(context, owner_id):
        await log_user_event_if_enabled(
            owner_id,
            "job.rejected.parallel_limit",
            level="WARNING",
            error_code=ERR_JOB_ALREADY_RUNNING,
            user_logs_enabled=user_logs_enabled,
            platform=platform,
        )
        try:
            await message.reply_text(t("already_running", lang))
        except Exception:
            pass
        return ASK_TRIM

    if announce_coro is not None:
        try:
            await announce_coro()
        except Exception as e:
            finish_job(context, owner_id)
            friendly = handle_error(e, user_name, lang, user_id=owner_id)
            try:
                await message.reply_text(friendly)
            except Exception:
                pass
            clear_conversation_state(context, owner_id)
            return ASK_TRIM

    task = schedule_download_background(
        update,
        context,
        url=url,
        platform=platform,
        user_id=owner_id,
        user_name=user_name,
        lang=lang,
        yt_type=yt_type,
        start=start,
        end=end,
        message=message,
        prompt_chat_id=prompt_chat_id,
        prompt_message_id=prompt_message_id,
        plan_snapshot=plan_snapshot,
        max_duration_seconds=max_duration_seconds,
    )
    if task is None:
        try:
            await message.reply_text(t("generic_error", lang))
        except Exception:
            pass
        clear_conversation_state(context, owner_id)
        return ASK_TRIM
    clear_conversation_state(context, owner_id)
    return ASK_TRIM

# ===== Получение ссылки =====
async def get_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full_text = (update.message.text or "").strip()
    user_id = update.message.from_user.id
    lang = await get_lang(user_id, update.message.from_user.language_code)
    user_logs_enabled = await get_user_logs_enabled(user_id, default=False)

    if full_text.lower() in ("отмена", "cancel", "аннулировать", "отм", "отмени"):
        abort_user_job(context, user_id)
        await update.message.reply_text(t("cancelled_send_link", lang))
        clear_conversation_state(context, user_id)
        return ASK_TRIM

    if not full_text:
        await update.message.reply_text(t("need_link", lang))
        return ASK_TRIM

    # ищем первую ссылку в тексте (если склеено с текстом)
    m = re.search(r'(https?://[^\s]+)', full_text)
    if m:
        url = m.group(1).rstrip('.,)];\'"')
    else:
        url = full_text  # допустим, пользователь прислал только URL

    # Антиспам: минимальная пауза между запусками скачивания
    wait_s = acquire_download_cooldown(user_id)
    if wait_s > 0:
        await log_user_event_if_enabled(
            user_id,
            "job.rejected.cooldown",
            level="WARNING",
            error_code=ERR_COOLDOWN_ACTIVE,
            user_logs_enabled=user_logs_enabled,
            wait_seconds=wait_s,
        )
        await update.message.reply_text(tf("cooldown", lang, seconds=wait_s))
        return ASK_TRIM

    # определяем платформу
    if "soundcloud.com" in url.lower():
        context.user_data["platform"] = "soundcloud"
        context.user_data["url"] = url

        s = await get_user_settings(user_id)
        # Для SoundCloud всегда скачиваем audio
        context.user_data["yt_type"] = "audio"

        # Проверяем настройку обрезки для SoundCloud
        trim_pref = s.get("trim", {}).get("soundcloud", "ask")
        uid = update.message.from_user.id

        if trim_pref == "no":
            async def _announce_sc_no_trim():
                await update.message.reply_text(t("downloading_no_trim", lang))

            return await _start_download_flow(
                update,
                context,
                owner_id=uid,
                user_name=update.message.from_user.first_name,
                lang=lang,
                platform="soundcloud",
                url=url,
                yt_type="audio",
                message=update.message,
                user_logs_enabled=user_logs_enabled,
                announce_coro=_announce_sc_no_trim,
            )
        else:
            # Спрашиваем, нужна ли обрезка
            keyboard = [
                [InlineKeyboardButton(t("yes", lang), callback_data=f"trim_yes:{uid}"),
                 InlineKeyboardButton(t("no", lang), callback_data=f"trim_no:{uid}")],
                [InlineKeyboardButton(t("cancel", lang), callback_data=f"cancel:{uid}")]
            ]
            reply = await update.message.reply_text(t("ask_trim_track", lang), reply_markup=InlineKeyboardMarkup(keyboard))
            # Сохраняем ID сообщения с вопросом, чтобы удалить его позже
            context.user_data["trim_prompt_msg_id"] = reply.message_id
            return ASK_RANGE

    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        context.user_data["platform"] = "youtube"
        context.user_data["url"] = url

        s = await get_user_settings(user_id)
        yt_pref = s.get("format", {}).get("youtube", "ask")
        trim_pref = s.get("trim", {}).get("youtube", "ask")

        uid = update.message.from_user.id
        # Определяем yt_type (из настроек или через вопрос пользователю)
        if yt_pref in ("audio", "video"):
            context.user_data["yt_type"] = yt_pref
            # Проверяем настройку обрезки
            if trim_pref == "no":
                async def _announce_yt_no_trim():
                    await update.message.reply_text(t("downloading_no_trim", lang))

                return await _start_download_flow(
                    update,
                    context,
                    owner_id=uid,
                    user_name=update.message.from_user.first_name,
                    lang=lang,
                    platform="youtube",
                    url=url,
                    yt_type=yt_pref,
                    message=update.message,
                    user_logs_enabled=user_logs_enabled,
                    announce_coro=_announce_yt_no_trim,
                )
            else:
                # Спрашиваем про обрезку
                keyboard = [
                    [InlineKeyboardButton(t("yes", lang), callback_data=f"trim_yes:{uid}"),
                     InlineKeyboardButton(t("no", lang), callback_data=f"trim_no:{uid}")],
                    [InlineKeyboardButton(t("cancel", lang), callback_data=f"cancel:{uid}")]
                ]
                reply = await update.message.reply_text(t("ask_trim_before_send", lang), reply_markup=InlineKeyboardMarkup(keyboard))
                context.user_data["trim_prompt_msg_id"] = reply.message_id
                return ASK_TRIM_YT
        else:
            # Спрашиваем, что скачать: audio или video
            keyboard = [
                [InlineKeyboardButton(t("audio_choice", lang), callback_data=f"yt_audio:{uid}"),
                 InlineKeyboardButton(t("video_choice", lang), callback_data=f"yt_video:{uid}")],
                [InlineKeyboardButton(t("cancel", lang), callback_data=f"cancel:{uid}")]
            ]
            reply = await update.message.reply_text(t("choose_download_type", lang), reply_markup=InlineKeyboardMarkup(keyboard))
            # Это сообщение-выбор; сохраняем ID, чтобы при необходимости удалить в конце сценария
            context.user_data["trim_prompt_msg_id"] = reply.message_id
            return ASK_TYPE

    await log_user_event_if_enabled(
        user_id,
        "input.invalid_link",
        level="WARNING",
        error_code=ERR_INVALID_LINK,
        user_logs_enabled=user_logs_enabled,
        url=url,
    )
    await update.message.reply_text(t("invalid_link", lang))
    return ASK_TRIM

# ===== Callback для выбора типа YouTube: аудио / видео =====
async def yt_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    lang = await get_lang(query.from_user.id, query.from_user.language_code)

    data = query.data  # формат: 'yt_audio:uid' или 'yt_video:uid'
    try:
        action, owner_s = data.split(":", 1)
        owner_id = int(owner_s)
    except Exception:
        try:
            await query.edit_message_text(t("bad_callback", lang))
        except BadRequest:
            pass
        return ASK_TRIM

    if query.from_user.id != owner_id:
        try:
            await query.answer(t("button_not_for_you", lang), show_alert=True)
        except BadRequest:
            pass
        return

    lang = await get_lang(owner_id, query.from_user.language_code)

    url = context.user_data.get("url")
    platform = context.user_data.get("platform")
    if not url or not platform:
        try:
            await query.answer(t("stale_button_alert", lang), show_alert=True)
        except BadRequest:
            pass
        try:
            await query.message.reply_text(t("stale_button_reply", lang))
        except Exception:
            pass
        clear_conversation_state(context, owner_id)
        return ASK_TRIM

    if action == "yt_audio":
        context.user_data["yt_type"] = "audio"
    else:
        context.user_data["yt_type"] = "video"

    # Проверяем настройку обрезки для YouTube
    try:
        s = await get_user_settings(owner_id)
        trim_pref = s.get("trim", {}).get("youtube", "ask")
    except Exception:
        trim_pref = "ask"

    if trim_pref == "no":
        pid = context.user_data.pop("trim_prompt_msg_id", None)

        async def _announce_choice_no_trim():
            try:
                await query.edit_message_text(t("downloading_no_trim", lang))
            except BadRequest:
                pass

        return await _start_download_flow(
            update,
            context,
            owner_id=owner_id,
            user_name=query.from_user.first_name,
            lang=lang,
            platform="youtube",
            url=url,
            yt_type=context.user_data.get("yt_type", "audio"),
            message=query.message,
            user_logs_enabled=None,
            prompt_chat_id=(query.message.chat.id if pid else None),
            prompt_message_id=pid,
            announce_coro=_announce_choice_no_trim,
        )

    # Показываем вопрос про обрезку
    try:
        await query.edit_message_text(t("ask_trim_before_send", lang), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(t("yes", lang), callback_data=f"trim_yes:{owner_id}"),
             InlineKeyboardButton(t("no", lang), callback_data=f"trim_no:{owner_id}")],
            [InlineKeyboardButton(t("cancel", lang), callback_data=f"cancel:{owner_id}")]
        ]))
    except BadRequest:
        pass

    # Гарантируем, что ID prompt-сообщения сохранён (это отредактированное сообщение)
    try:
        context.user_data["trim_prompt_msg_id"] = query.message.message_id
    except Exception:
        # Если объекта сообщения нет — просто игнорируем
        context.user_data.pop("trim_prompt_msg_id", None)
    return ASK_TRIM_YT

# ===== Callback-обработчик кнопок "Обрезать? Да/Нет" =====
async def trim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    lang = await get_lang(query.from_user.id, query.from_user.language_code)

    data = query.data  # формат: "trim_yes:uid" или "trim_no:uid"
    user_name = query.from_user.first_name

    try:
        action, owner_s = data.split(":", 1)
        owner_id = int(owner_s)
    except Exception:
        try:
            await query.edit_message_text(t("bad_callback", lang))
        except BadRequest:
            pass
        return ASK_TRIM

    if query.from_user.id != owner_id:
        try:
            await query.answer(t("button_not_for_you", lang), show_alert=True)
        except BadRequest:
            pass
        return

    lang = await get_lang(owner_id, query.from_user.language_code)

    # Сохраняем ID prompt-сообщения (текущего отредактированного), чтобы удалить его позже.
    try:
        context.user_data["trim_prompt_msg_id"] = query.message.message_id
    except Exception:
        context.user_data.pop("trim_prompt_msg_id", None)

    platform = context.user_data.get("platform")
    url = context.user_data.get("url")
    if not url or not platform:
        try:
            await query.answer(t("stale_button_alert", lang), show_alert=True)
        except BadRequest:
            pass
        try:
            await query.message.reply_text(t("stale_button_reply", lang))
        except Exception:
            pass
        clear_conversation_state(context, owner_id)
        return ASK_TRIM

    if action == "trim_yes":
        keyboard = [[InlineKeyboardButton(t("cancel", lang), callback_data=f"cancel:{owner_id}")]]
        try:
            if platform == "soundcloud":
                await query.edit_message_text(t("range_prompt_sc", lang), reply_markup=InlineKeyboardMarkup(keyboard))
                return ASK_RANGE
            else:
                await query.edit_message_text(t("range_prompt_yt", lang), reply_markup=InlineKeyboardMarkup(keyboard))
                return ASK_RANGE_YT
        except BadRequest:
            # Если редактировать нельзя (слишком старое), всё равно оставляем то же состояние.
            return ASK_RANGE if platform == "soundcloud" else ASK_RANGE_YT

    elif action == "trim_no":
        pid = context.user_data.pop("trim_prompt_msg_id", None)

        async def _announce_trim_no():
            try:
                await query.edit_message_text(t("downloading_no_trim", lang))
            except BadRequest:
                pass

        return await _start_download_flow(
            update,
            context,
            owner_id=owner_id,
            user_name=user_name,
            lang=lang,
            platform=platform,
            url=url,
            yt_type=("audio" if platform == "soundcloud" else context.user_data.get("yt_type", "audio")),
            message=query.message,
            user_logs_enabled=None,
            prompt_chat_id=(query.message.chat.id if pid else None),
            prompt_message_id=pid,
            announce_coro=_announce_trim_no,
        )

    else:
        try:
            await query.edit_message_text(t("unknown_action", lang))
        except BadRequest:
            pass
        return ASK_TRIM

# ===== Обработчик команды /cancel =====
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    lang = await get_lang(user.id, getattr(user, "language_code", None)) if user else "ru"

    pid = context.user_data.pop("trim_prompt_msg_id", None)
    if pid:
        try:
            await context.bot.delete_message(chat_id=update.message.chat.id, message_id=pid)
        except Exception:
            pass

    abort_user_job(context, user.id if user else None)
    await update.message.reply_text(t("cancelled_send_link", lang))
    clear_conversation_state(context, user.id if user else None)
    return ASK_TRIM

# ===== Callback-обработчик отмены =====
async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    lang = await get_lang(query.from_user.id, query.from_user.language_code)

    data = query.data
    try:
        action, owner_s = data.split(":", 1)
        owner_id = int(owner_s)
    except Exception:
        try:
            await query.edit_message_text(t("bad_callback", lang))
        except BadRequest:
            pass
        return ASK_TRIM

    if query.from_user.id != owner_id:
        try:
            await query.answer(t("button_not_for_you", lang), show_alert=True)
        except BadRequest:
            pass
        return

    lang = await get_lang(owner_id, query.from_user.language_code)

    # Пытаемся удалить prompt-сообщение обрезки, если его ID сохранён
    pid = context.user_data.pop("trim_prompt_msg_id", None)
    if pid:
        try:
            await context.bot.delete_message(chat_id=query.message.chat.id, message_id=pid)
        except Exception:
            pass

    try:
        await query.edit_message_text(t("cancelled_send_link", lang))
    except BadRequest:
        try:
            await query.message.reply_text(t("cancelled_send_link", lang))
        except Exception:
            pass

    abort_user_job(context, owner_id)
    clear_conversation_state(context, owner_id)
    return ASK_TRIM

# ===== Диапазон обрезки (общий обработчик) =====
async def trim_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.message.from_user.id
    lang = await get_lang(user_id, update.message.from_user.language_code)
    user_logs_enabled = await get_user_logs_enabled(user_id, default=False)

    # Защитная проверка: если мы не в активном сценарии обрезки (кнопка уже удалена),
    # трактуем сообщение как новую ссылку и передаём в get_link.
    if not context.user_data.get("trim_prompt_msg_id"):
        # Перенаправляем как обычную ссылку
        return await get_link(update, context)

    if text.lower() in ("отмена", "cancel", "аннулировать", "отм", "отмени"):
        # Удаляем prompt-сообщение, если оно есть
        pid = context.user_data.pop("trim_prompt_msg_id", None)
        if pid:
            try:
                await context.bot.delete_message(chat_id=update.message.chat.id, message_id=pid)
            except Exception:
                pass

        abort_user_job(context, user_id)
        await update.message.reply_text(t("cancelled_send_link", lang))
        clear_conversation_state(context, user_id)
        return ASK_TRIM

    platform = context.user_data.get("platform", "soundcloud")
    range_state = ASK_RANGE if platform == "soundcloud" else ASK_RANGE_YT

    match = re.match(r"(\d{1,2}:\d{1,2})\s*-\s*(\d{1,2}:\d{1,2})", text)
    if not match:
        await log_user_event_if_enabled(
            user_id,
            "input.invalid_trim_format",
            level="WARNING",
            error_code=ERR_INVALID_RANGE_FORMAT,
            user_logs_enabled=user_logs_enabled,
            value=text,
        )
        await update.message.reply_text(t("invalid_format", lang))
        return range_state

    start, end = match.groups()

    def to_seconds(t):
        parts = list(map(int, t.split(":")))
        return parts[0] * 60 + parts[1]

    start_s, end_s = to_seconds(start), to_seconds(end)

    # Проверяем базовый порядок: начало должно быть меньше конца
    if start_s >= end_s:
        await log_user_event_if_enabled(
            user_id,
            "input.invalid_trim_range",
            level="WARNING",
            error_code=ERR_INVALID_RANGE_ORDER,
            user_logs_enabled=user_logs_enabled,
            start_seconds=start_s,
            end_seconds=end_s,
        )
        await update.message.reply_text(t("invalid_range", lang))
        return range_state

    yt_type = context.user_data.get("yt_type", "audio")

    user_id = update.message.from_user.id
    pid = context.user_data.pop("trim_prompt_msg_id", None)
    return await _start_download_flow(
        update,
        context,
        owner_id=user_id,
        user_name=update.message.from_user.first_name,
        lang=lang,
        platform=platform,
        url=context.user_data.get("url"),
        yt_type=yt_type,
        user_logs_enabled=user_logs_enabled,
        start=start_s,
        end=end_s,
        message=update.message,
        prompt_chat_id=(update.message.chat.id if pid else None),
        prompt_message_id=pid,
    )

# ===== Синхронный воркер (с progress hook и учётом качества) =====

async def download_content(
    update,
    context,
    url,
    platform="soundcloud",
    start=None,
    end=None,
    yt_type="audio",
    message=None,
    plan_snapshot=PLAN_FREE,
    max_duration_seconds=MAX_DURATION,
):
    msg = message or update.message
    effective_user = update.effective_user or (msg.from_user if msg else None)
    user_id = effective_user.id if effective_user else (msg.from_user.id if msg and msg.from_user else None)
    user_name = effective_user.first_name if effective_user else (msg.from_user.first_name if msg and msg.from_user else "User")
    lang = await get_lang(user_id, getattr(update.effective_user, "language_code", None)) if user_id is not None else "ru"
    user_logs_enabled = True if user_id is None else await get_user_logs_enabled(user_id, default=False)
    job_id = uuid.uuid4().hex[:12]

    status_text = tf("download_status", lang, user=user_name)
    status_msg = await msg.reply_text(status_text)
    if user_logs_enabled:
        log_event(
            "job.requested",
            level="INFO",
            job_id=job_id,
            user_id=user_id,
            platform=platform,
            yt_type=yt_type,
            url=url,
        )

    ffmpeg_path = resolve_ffmpeg_path()
    if not ffmpeg_path:
        await msg.reply_text(t("ffmpeg_not_found", lang))
        log_event(
            "ffmpeg.missing",
            level="ERROR",
            error_code=ERR_FFMPEG_MISSING,
            job_id=job_id,
            platform=platform,
            yt_type=yt_type,
        )
        try:
            await status_msg.delete()
        except Exception:
            pass
        return

    cancel_event = threading.Event()
    cancel_reason_ref = [None]
    register_active_download_task(user_id, cancel_event=cancel_event, cancel_reason_ref=cancel_reason_ref)
    loop = asyncio.get_running_loop()
    progress_q = asyncio.Queue()
    watcher_task = None
    consumer_task = None
    watchdog_task = None
    worker_future = None

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            try:
                os.chmod(tmpdir, 0o700)
            except Exception:
                pass

            now_ts = time.time()
            JOB_PROGRESS[user_id] = {
                'percent': 0,
                'downloaded_bytes': 0,
                'last_info': {},
                'done': False,
                'phase': 'downloading',
                'started_ts': now_ts,
                'last_progress_ts': now_ts,
                'last_advance_ts': now_ts,
            }

            watcher_task = asyncio.create_task(_progress_watcher(user_id, status_msg, status_text, lang))
            consumer_task = asyncio.create_task(_progress_consumer(user_id, progress_q))
            watchdog_task = asyncio.create_task(
                _stall_watchdog(
                    user_id,
                    cancel_event,
                    cancel_reason_ref,
                    user_logs_enabled=user_logs_enabled,
                    job_id=job_id,
                )
            )

            # Сначала запрашиваем метаданные (длительность, теги и т.д.)
            def meta_worker():
                try:
                    if cancel_event.is_set():
                        return {}
                    ydl_opts_meta = {
                        'format': 'bestaudio/best' if yt_type == "audio" else 'best',
                        'quiet': True,
                        'no_warnings': True,
                        'socket_timeout': YTDLP_META_SOCKET_TIMEOUT,
                    }
                    if platform == "youtube" and YTDLP_COOKIES_FILE and os.path.isfile(YTDLP_COOKIES_FILE):
                        ydl_opts_meta["cookiefile"] = YTDLP_COOKIES_FILE
                    if platform == "youtube" and YTDLP_JS_RUNTIMES_MAP:
                        ydl_opts_meta["js_runtimes"] = dict(YTDLP_JS_RUNTIMES_MAP)
                    if platform == "youtube" and YTDLP_REMOTE_COMPONENTS:
                        ydl_opts_meta["remote_components"] = list(YTDLP_REMOTE_COMPONENTS)
                    with YoutubeDL(ydl_opts_meta) as ydl:
                        if cancel_event.is_set():
                            return {}
                        return ydl.extract_info(url, download=False) or {}
                except Exception:
                    return {}

            worker_future = loop.run_in_executor(None, meta_worker)
            register_active_worker_future(user_id, worker_future)
            info_meta = await worker_future
            unregister_active_worker_future(user_id, worker_future)
            worker_future = None
            duration = info_meta.get("duration") or 0
            if duration and duration > max_duration_seconds:
                await msg.reply_text(
                    tf(
                        "too_long",
                        lang,
                        hours=int(duration / 3600),
                        max_hours=int(max_duration_seconds / 3600),
                    )
                )
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                return

            worker_future = loop.run_in_executor(
                None,
                _sync_worker,
                url,
                tmpdir,
                platform,
                yt_type,
                start,
                end,
                ffmpeg_path,
                user_id,
                loop,
                progress_q,
                cancel_event,
                cancel_reason_ref,
            )
            register_active_worker_future(user_id, worker_future)
            result = await worker_future

            entry = JOB_PROGRESS.get(user_id)
            if entry:
                entry['done'] = True
                entry['percent'] = 100
                entry['phase'] = 'done'

            await asyncio.sleep(0.1)
            try:
                await asyncio.wait_for(watcher_task, timeout=3.0)
            except Exception:
                watcher_task.cancel()

            if result.get('status') != 'ok':
                worker_error_code = result.get("error_code") or ERR_WORKER_FAILED
                if worker_error_code in (ERR_WORKER_STALLED, ERR_TIMEOUT):
                    friendly = t("err_timeout", lang)
                elif worker_error_code == ERR_WORKER_CANCELLED:
                    friendly = t("cancelled_send_link", lang)
                else:
                    friendly = result.get('error', t("generic_error", lang))
                await msg.reply_text(friendly)
                if user_logs_enabled:
                    log_event(
                        "job.failed.worker",
                        level="WARNING",
                        error_code=worker_error_code,
                        job_id=job_id,
                        user_id=user_id,
                        platform=platform,
                        yt_type=yt_type,
                        error=friendly,
                    )
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                return

            mode = result.get('mode')
            title = result.get('title', 'Media')
            uploader = result.get('uploader', 'Unknown')
            caption = f"🎵 {title}\n👤 {uploader}"
            delivery_success = False
            delivered_ext = result.get("ext")
            delivered_file_path = None

            if mode == 'file':
                file_path = result.get('file_path')
                try:
                    with open(file_path, 'rb') as f:
                        if result.get('ext') == 'mp3':
                            await msg.reply_audio(f, caption=caption, title=title, performer=uploader)
                        else:
                            await msg.reply_video(f, caption=caption)
                    delivery_success = True
                    delivered_file_path = file_path
                    if user_logs_enabled:
                        log_event(
                            "job.completed.file",
                            level="INFO",
                            job_id=job_id,
                            user_id=user_id,
                            platform=platform,
                            yt_type=yt_type,
                            ext=result.get("ext"),
                            title=title,
                        )
                except Exception as e:
                    send_error_code = classify_exception_error_code(e)
                    if user_logs_enabled:
                        log_event(
                            "telegram.file_send_failed",
                            level="ERROR",
                            error_code=send_error_code,
                            job_id=job_id,
                            user_id=user_id,
                            platform=platform,
                            yt_type=yt_type,
                            title=title,
                            error_class=type(e).__name__,
                            error=str(e),
                        )
                    else:
                        log_event(
                            "telegram.file_send_failed",
                            level="ERROR",
                            error_code=send_error_code,
                            job_id=job_id,
                            platform=platform,
                            yt_type=yt_type,
                            error_class=type(e).__name__,
                        )
                    await msg.reply_text(t("file_send_fail", lang))
            elif mode == 'link':
                link = result.get('link')
                await msg.reply_text(tf("file_too_big", lang, link=link))
                delivery_success = True
                if user_logs_enabled:
                    log_event(
                        "job.completed.link",
                        level="INFO",
                        job_id=job_id,
                        user_id=user_id,
                        platform=platform,
                        yt_type=yt_type,
                        title=title,
                        link=link,
                    )
            else:
                await msg.reply_text(t("unknown_worker_result", lang))
                if user_logs_enabled:
                    log_event(
                        "job.failed.unknown_mode",
                        level="ERROR",
                        error_code=ERR_WORKER_UNKNOWN_MODE,
                        job_id=job_id,
                        user_id=user_id,
                        platform=platform,
                        yt_type=yt_type,
                        result=result,
                    )
                else:
                    log_event(
                        "job.failed.unknown_mode",
                        level="ERROR",
                        error_code=ERR_WORKER_UNKNOWN_MODE,
                        job_id=job_id,
                        platform=platform,
                        yt_type=yt_type,
                    )

            if delivery_success and user_id is not None and plan_snapshot == PLAN_FREE:
                await increment_usage_success_once(user_id, job_id)

            if (
                delivery_success
                and user_id is not None
                and delivered_ext == "mp3"
                and delivered_file_path
                and is_premium_plan(plan_snapshot)
            ):
                try:
                    user_settings = await get_user_settings(user_id)
                    await maybe_offer_metadata_edit(
                        context=context,
                        message=msg,
                        user_id=user_id,
                        lang=lang,
                        plan_type=plan_snapshot,
                        settings=user_settings,
                        file_path=delivered_file_path,
                        title=title,
                        artist=uploader,
                        source_job_id=job_id,
                    )
                except Exception as e:
                    log_event(
                        "metadata.offer.failed",
                        level="ERROR",
                        user_id=user_id,
                        job_id=job_id,
                        error_class=type(e).__name__,
                        error=str(e),
                    )

        except asyncio.CancelledError:
            request_active_download_cancel(user_id, reason="task_cancelled")
            if user_logs_enabled:
                log_event(
                    "job.cancelled",
                    level="INFO",
                    job_id=job_id,
                    user_id=user_id,
                    platform=platform,
                    yt_type=yt_type,
                )
            raise
        except Exception as e:
            exception_error_code = classify_exception_error_code(e)
            friendly_error = handle_error(e, user_name, lang, user_id=user_id, user_logs_enabled=user_logs_enabled)
            await msg.reply_text(friendly_error)
            if user_logs_enabled:
                log_event(
                    "job.failed.exception",
                    level="ERROR",
                    error_code=exception_error_code,
                    job_id=job_id,
                    user_id=user_id,
                    platform=platform,
                    yt_type=yt_type,
                    error_class=type(e).__name__,
                    error=str(e),
                )

        finally:
            if worker_future is not None and not worker_future.done():
                request_active_download_cancel(user_id, reason="cleanup")
            try:
                await status_msg.delete()
            except Exception:
                pass
            try:
                progress_q.put_nowait(None)
            except Exception:
                pass
            if consumer_task:
                try:
                    await asyncio.wait_for(consumer_task, timeout=2.0)
                except Exception:
                    consumer_task.cancel()
            if watchdog_task:
                try:
                    watchdog_task.cancel()
                except Exception:
                    pass
            if watcher_task:
                try:
                    watcher_task.cancel()
                except Exception:
                    pass
            unregister_active_worker_future(user_id, worker_future)
            JOB_PROGRESS.pop(user_id, None)
            unregister_active_download_task(user_id)

    # tmpdir удаляется автоматически здесь
    # В context.user_data могут быть и другие данные; очистку/prompt удаляет вызывающий код
    return

# ===== Инициализация =====

