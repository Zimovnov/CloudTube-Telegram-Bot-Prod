from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.access import PLAN_FREE, PLAN_PREMIUM_LIFETIME, get_user_profile, is_premium_plan
from app.config import FREE_MAX_DURATION_SECONDS, FREE_MONTHLY_LIMIT, PREMIUM_MAX_DURATION_SECONDS, PREMIUM_MONTHLY_STARS
from app.i18n import get_lang, pack_mark, t, tf
from app.jobs import allow_settings_change
from app.logging_utils import log_event
from app.settings_store import get_user_settings, set_user_settings
from app.usage import get_free_usage_count


def mk(button_rows):
    return InlineKeyboardMarkup(button_rows)


def _get_fmt_display(s):
    fmt = s.get("format", {})
    if isinstance(fmt, dict):
        sc = fmt.get("soundcloud", "audio")
        yt = fmt.get("youtube", "ask")
    else:
        sc = "audio"
        yt = fmt if isinstance(fmt, str) else "ask"
    return sc, yt


def build_main_settings_markup(s, lang):
    sc_val, yt_val = _get_fmt_display(s)
    sc_label = t(sc_val, lang) if sc_val in ("ask", "audio", "video") else sc_val
    yt_label = t(yt_val, lang) if yt_val in ("ask", "audio", "video") else yt_val
    rows = [
        [
            InlineKeyboardButton(
                f"{t('format_quality', lang)} — SC:{sc_label} | YT:{yt_label}",
                callback_data="settings:format",
            )
        ],
        [InlineKeyboardButton(t("trimming", lang), callback_data="settings:trimming")],
        [InlineKeyboardButton(t("limits", lang), callback_data="settings:limits")],
        [
            InlineKeyboardButton(
                f"{t('privacy_logs', lang)} {pack_mark(s.get('logs', False))}",
                callback_data="settings:logs",
            )
        ],
        [InlineKeyboardButton(f"{t('language', lang)}: {s.get('language')}", callback_data="settings:language")],
        [InlineKeyboardButton(t("support", lang), callback_data="settings:support")],
        [InlineKeyboardButton(t("reset", lang), callback_data="settings:reset")],
        [InlineKeyboardButton(t("close", lang), callback_data="settings:close")],
    ]
    return mk(rows)


async def _safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest:
        pass


async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        source = query.message
        user = query.from_user
    else:
        source = update.message
        user = update.message.from_user

    user_id = user.id
    s = await get_user_settings(user_id)
    lang = await get_lang(user_id, user.language_code)
    await source.reply_text(t("menu_title", lang), reply_markup=build_main_settings_markup(s, lang))


async def _settings_action_reset_confirm(query, user_id, lang):
    s = {
        "format": {"soundcloud": "audio", "youtube": "ask"},
        "quality": {"soundcloud": "best", "youtube": "best"},
        "trim": {"soundcloud": "ask", "youtube": "ask"},
        "logs": False,
        "language": "ru",
        "metadata_prompt_enabled": True,
    }
    await set_user_settings(user_id, s)
    await _safe_edit(query, t("reset_done", lang))


async def _settings_action_faq(query, lang):
    await _safe_edit(query, t("faq_text", lang))


async def _settings_action_contacts(query, lang):
    await _safe_edit(query, t("contacts_text", lang))


async def _settings_action_version(query, lang):
    await _safe_edit(query, t("version_text", lang))


async def _settings_action_close(query, lang):
    try:
        await query.edit_message_text(t("menu_closed", lang))
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass


async def _settings_action_back(query, user_id):
    s = await get_user_settings(user_id)
    lang = s.get("language", "ru")
    await _safe_edit(query, t("menu_title", lang), reply_markup=build_main_settings_markup(s, lang))


async def _show_format_root(query, s, lang, is_premium):
    kb = [
        [
            InlineKeyboardButton(t("soundcloud", lang), callback_data="settings:format_platform:soundcloud"),
            InlineKeyboardButton(t("youtube", lang), callback_data="settings:format_platform:youtube"),
        ],
    ]
    if is_premium:
        val = t("yes", lang) if bool(s.get("metadata_prompt_enabled", True)) else t("no", lang)
        kb.append(
            [
                InlineKeyboardButton(
                    tf("metadata_prompt_toggle", lang, value=val),
                    callback_data="settings:toggle_metadata_prompt",
                )
            ]
        )
    kb.append([InlineKeyboardButton(t("back", lang), callback_data="settings:back")])
    await _safe_edit(query, f"{t('format_quality', lang)} — {t('back', lang)}", reply_markup=InlineKeyboardMarkup(kb))


async def _show_format_platform(query, s, lang, platform):
    cur = s.get("format", {}).get(platform, "ask") if isinstance(s.get("format"), dict) else (s.get("format") or "ask")
    cur_quality = s.get("quality", {}).get(platform, "best") if isinstance(s.get("quality"), dict) else (s.get("quality") or "best")
    if platform == "soundcloud":
        kb = [
            [InlineKeyboardButton(t("sc_audio_fixed", lang), callback_data="noop")],
            [InlineKeyboardButton(f"{t('quality', lang)}: {cur_quality}", callback_data=f"settings:quality_platform:{platform}")],
            [InlineKeyboardButton(t("back", lang), callback_data="settings:format")],
        ]
    else:
        kb = [
            [
                InlineKeyboardButton(
                    f"{'🟢' if cur=='ask' else '🔴'} {t('ask', lang)}",
                    callback_data=f"settings:set:format:{platform}:ask",
                ),
                InlineKeyboardButton(
                    f"{'🟢' if cur=='audio' else '🔴'} {t('audio', lang)}",
                    callback_data=f"settings:set:format:{platform}:audio",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"{'🟢' if cur=='video' else '🔴'} {t('video', lang)}",
                    callback_data=f"settings:set:format:{platform}:video",
                )
            ],
            [InlineKeyboardButton(f"{t('quality', lang)}: {cur_quality}", callback_data=f"settings:quality_platform:{platform}")],
            [InlineKeyboardButton(t("back", lang), callback_data="settings:format")],
        ]
    await _safe_edit(query, f"{t('format_quality', lang)} — {t(platform, lang)}", reply_markup=InlineKeyboardMarkup(kb))


async def _show_quality_platform(query, s, lang, platform):
    cur = s.get("quality", {}).get(platform, "best") if isinstance(s.get("quality"), dict) else (s.get("quality") or "best")
    opts = [("best", "best"), ("320", "320kbps"), ("128", "128kbps")] if platform == "soundcloud" else [("best", "best"), ("720", "720p"), ("480", "480p")]
    kb = [[InlineKeyboardButton(f"{'🟢' if cur==val else '🔴'} {label}", callback_data=f"settings:set:quality:{platform}:{val}")] for val, label in opts]
    kb.append([InlineKeyboardButton(t("back", lang), callback_data=f"settings:format_platform:{platform}")])
    await _safe_edit(query, f"{t('quality', lang)} — {t(platform, lang)}", reply_markup=InlineKeyboardMarkup(kb))


async def _show_trimming_root(query, lang):
    kb = [
        [
            InlineKeyboardButton(t("soundcloud", lang), callback_data="settings:trimming_platform:soundcloud"),
            InlineKeyboardButton(t("youtube", lang), callback_data="settings:trimming_platform:youtube"),
        ],
        [InlineKeyboardButton(t("back", lang), callback_data="settings:back")],
    ]
    await _safe_edit(query, f"{t('trimming', lang)} — {t('back', lang)}", reply_markup=InlineKeyboardMarkup(kb))


async def _show_trimming_platform(query, s, lang, platform):
    cur = s.get("trim", {}).get(platform, "ask")
    kb = [
        [
            InlineKeyboardButton(f"{'🟢' if cur=='no' else '🔴'} {t('trim_no', lang)}", callback_data=f"settings:set:trim:{platform}:no"),
            InlineKeyboardButton(f"{'🟢' if cur=='ask' else '🔴'} {t('trim_ask', lang)}", callback_data=f"settings:set:trim:{platform}:ask"),
        ],
        [InlineKeyboardButton(t("back", lang), callback_data="settings:trimming")],
    ]
    await _safe_edit(query, f"{t('trimming', lang)} — {t(platform, lang)}", reply_markup=InlineKeyboardMarkup(kb))


async def _set_format(query, s, user_id, lang, platform, value):
    if platform == "soundcloud" and value == "video":
        value = "audio"
    s.setdefault("format", {})[platform] = value
    await set_user_settings(user_id, s)
    if bool(s.get("logs")):
        log_event("settings.updated", level="INFO", user_id=user_id, setting="format", platform=platform, value=value)
    value_label = t(value, lang) if value in ("ask", "audio", "video") else value
    await _safe_edit(query, f"{t('format_quality', lang)} — {t(platform, lang)}: {value_label}")


async def _set_quality(query, s, user_id, lang, platform, val):
    s.setdefault("quality", {})[platform] = val
    await set_user_settings(user_id, s)
    if bool(s.get("logs")):
        log_event("settings.updated", level="INFO", user_id=user_id, setting="quality", platform=platform, value=val)
    await _safe_edit(query, tf("quality_saved", lang, platform=t(platform, lang), val=val))


async def _set_trim(query, s, user_id, lang, platform, val):
    s.setdefault("trim", {})[platform] = val
    await set_user_settings(user_id, s)
    if bool(s.get("logs")):
        log_event("settings.updated", level="INFO", user_id=user_id, setting="trim", platform=platform, value=val)
    val_label = t("trim_no", lang) if val == "no" else t("trim_ask", lang)
    await _safe_edit(query, tf("trim_saved", lang, platform=t(platform, lang), val=val_label))


async def _set_language(query, s, user_id, lang, val):
    s["language"] = val
    await set_user_settings(user_id, s)
    if bool(s.get("logs")):
        log_event("settings.updated", level="INFO", user_id=user_id, setting="language", value=val)
    await _safe_edit(query, tf("language_saved", val, val=val))


async def _toggle_logs(query, s, user_id, lang):
    prev = bool(s.get("logs"))
    new = not prev
    s["logs"] = new
    await set_user_settings(user_id, s)
    if prev or new:
        log_event("settings.updated", level="INFO", user_id=user_id, setting="logs", previous=prev, value=new)
    await _safe_edit(query, t("logs_on", lang) if new else t("logs_off", lang))


async def _toggle_metadata_prompt(query, s, user_id, lang):
    profile = await get_user_profile(user_id)
    if not is_premium_plan(profile.get("plan_type")):
        await _safe_edit(query, t("metadata_premium_only", lang))
        return
    prev = bool(s.get("metadata_prompt_enabled", True))
    s["metadata_prompt_enabled"] = not prev
    await set_user_settings(user_id, s)
    val = t("yes", lang) if s["metadata_prompt_enabled"] else t("no", lang)
    await _safe_edit(query, tf("metadata_prompt_saved", lang, value=val))


async def _show_limits(query, user_id, lang):
    profile = await get_user_profile(user_id)
    usage_count = await get_free_usage_count(user_id)
    plan = profile.get("plan_type")
    if plan == PLAN_FREE:
        text = tf(
            "limits_free_text",
            lang,
            count=usage_count,
            limit=FREE_MONTHLY_LIMIT,
            max_hours=int(FREE_MAX_DURATION_SECONDS / 3600),
            premium_stars=PREMIUM_MONTHLY_STARS,
        )
        kb = [
            [InlineKeyboardButton(t("buy_premium_button", lang), callback_data="sub:buy_monthly")],
            [InlineKeyboardButton(t("back", lang), callback_data="settings:back")],
        ]
    elif plan == PLAN_PREMIUM_LIFETIME:
        text = tf(
            "limits_premium_lifetime_text",
            lang,
            max_hours=int(PREMIUM_MAX_DURATION_SECONDS / 3600),
        )
        kb = [[InlineKeyboardButton(t("back", lang), callback_data="settings:back")]]
    else:
        text = tf(
            "limits_premium_monthly_text",
            lang,
            max_hours=int(PREMIUM_MAX_DURATION_SECONDS / 3600),
            expires_at_utc=profile.get("plan_expires_at_utc") or "-",
        )
        kb = [[InlineKeyboardButton(t("back", lang), callback_data="settings:back")]]
    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb))


async def _show_language(query, s, lang):
    kb = [
        [
            InlineKeyboardButton(f"{'🟢' if s.get('language')=='ru' else '🔴'} RU", callback_data="settings:set:language:ru"),
            InlineKeyboardButton(f"{'🟢' if s.get('language')=='en' else '🔴'} EN", callback_data="settings:set:language:en"),
        ],
        [InlineKeyboardButton(t("back", lang), callback_data="settings:back")],
    ]
    await _safe_edit(query, t("choose_language", lang), reply_markup=InlineKeyboardMarkup(kb))


async def _show_support(query, lang):
    kb = [
        [InlineKeyboardButton(t("support_faq_btn", lang), callback_data="settings:faq")],
        [InlineKeyboardButton(t("support_contacts_btn", lang), callback_data="settings:contacts")],
        [InlineKeyboardButton(t("support_version_btn", lang), callback_data="settings:version")],
        [InlineKeyboardButton(t("back", lang), callback_data="settings:back")],
    ]
    await _safe_edit(query, t("support_text", lang), reply_markup=InlineKeyboardMarkup(kb))


async def _show_reset(query, lang):
    kb = [
        [InlineKeyboardButton(t("reset_confirm_btn", lang), callback_data="settings:reset_confirm")],
        [InlineKeyboardButton(t("back", lang), callback_data="settings:back")],
    ]
    await _safe_edit(query, t("reset_confirm_text", lang), reply_markup=InlineKeyboardMarkup(kb))


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass

    user = query.from_user
    user_id = user.id

    data = query.data
    if data == "noop":
        return
    parts = data.split(":")
    s = await get_user_settings(user_id)
    lang = await get_lang(user_id, user.language_code)
    profile = await get_user_profile(user_id)
    premium = is_premium_plan(profile.get("plan_type"))

    if not allow_settings_change(user_id):
        try:
            await query.answer(t("slow_down", lang), show_alert=True)
        except BadRequest:
            pass
        return

    static_routes = {
        "settings:reset_confirm": lambda: _settings_action_reset_confirm(query, user_id, lang),
        "settings:faq": lambda: _settings_action_faq(query, lang),
        "settings:contacts": lambda: _settings_action_contacts(query, lang),
        "settings:version": lambda: _settings_action_version(query, lang),
        "settings:close": lambda: _settings_action_close(query, lang),
        "settings:back": lambda: _settings_action_back(query, user_id),
        "settings:toggle_metadata_prompt": lambda: _toggle_metadata_prompt(query, s, user_id, lang),
    }
    route = static_routes.get(data)
    if route:
        await route()
        return

    if len(parts) == 2:
        action = parts[1]
        simple_routes = {
            "format": lambda: _show_format_root(query, s, lang, premium),
            "trimming": lambda: _show_trimming_root(query, lang),
            "logs": lambda: _toggle_logs(query, s, user_id, lang),
            "limits": lambda: _show_limits(query, user_id, lang),
            "language": lambda: _show_language(query, s, lang),
            "support": lambda: _show_support(query, lang),
            "reset": lambda: _show_reset(query, lang),
            "back": lambda: _settings_action_back(query, user_id),
        }
        route = simple_routes.get(action)
        if route:
            await route()
            return

    if len(parts) >= 3:
        section = parts[1]
        if section == "format_platform" and len(parts) >= 3:
            await _show_format_platform(query, s, lang, parts[2])
            return
        if section == "quality_platform" and len(parts) >= 3:
            await _show_quality_platform(query, s, lang, parts[2])
            return
        if section == "trimming_platform" and len(parts) >= 3:
            await _show_trimming_platform(query, s, lang, parts[2])
            return
        if section == "set":
            if len(parts) >= 5 and parts[2] == "format":
                await _set_format(query, s, user_id, lang, parts[3], parts[4])
                return
            if len(parts) >= 5 and parts[2] == "quality":
                await _set_quality(query, s, user_id, lang, parts[3], parts[4])
                return
            if len(parts) >= 5 and parts[2] == "trim":
                await _set_trim(query, s, user_id, lang, parts[3], parts[4])
                return
            if len(parts) >= 4 and parts[2] == "language":
                await _set_language(query, s, user_id, lang, parts[3])
                return

    try:
        await query.answer(t("unknown_command", lang), show_alert=True)
    except BadRequest:
        pass
