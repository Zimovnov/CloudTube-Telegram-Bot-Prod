import asyncio

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.access import (
    PERM_PLAN_MANAGE,
    PERM_ROLE_MANAGE,
    PLAN_FREE,
    PLAN_PREMIUM_LIFETIME,
    PLAN_PREMIUM_MONTHLY,
    ROLE_ADMIN,
    ROLE_SUPERADMIN,
    ROLE_USER,
    apply_admin_payload_sync,
    create_admin_nonce,
    consume_admin_nonce,
    get_user_profile,
    rbac_check,
)
from app.errors import (
    ERR_ADMIN_NONCE_EXPIRED,
    ERR_ADMIN_NONCE_INVALID,
    ERR_ADMIN_SELF_ESCALATION,
    ERR_LAST_SUPERADMIN,
    ERR_RBAC_DENIED,
)
from app.i18n import get_lang, t, tf
from app.logging_utils import log_event
from app.usage import normalize_usage_month_label

_VALID_PLANS = {PLAN_FREE, PLAN_PREMIUM_MONTHLY, PLAN_PREMIUM_LIFETIME}
_VALID_ROLES = {ROLE_USER, ROLE_ADMIN, ROLE_SUPERADMIN}


def _is_private_chat(update):
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


def _parse_target_and_value(args):
    if len(args or []) < 2:
        return None, None, None
    try:
        target_user_id = int(args[0])
    except Exception:
        return None, None, None
    value = str(args[1]).strip().lower()
    reason = " ".join(args[2:]).strip()
    return target_user_id, value, reason


def _parse_target_and_reason(args):
    if len(args or []) < 1:
        return None, None
    try:
        target_user_id = int(args[0])
    except Exception:
        return None, None
    reason = " ".join(args[1:]).strip()
    return target_user_id, reason


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = await get_lang(user.id if user else None, getattr(user, "language_code", None))
    if not _is_private_chat(update):
        await update.effective_message.reply_text(t("admin_private_only", lang))
        return
    if not await rbac_check(user.id, PERM_PLAN_MANAGE, source="admin.help"):
        await update.effective_message.reply_text(t("rbac_denied", lang))
        return
    await update.effective_message.reply_text(t("admin_help", lang))


async def admin_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = await get_lang(user.id if user else None, getattr(user, "language_code", None))
    if not _is_private_chat(update):
        await update.effective_message.reply_text(t("admin_private_only", lang))
        return
    if not await rbac_check(user.id, PERM_PLAN_MANAGE, source="admin.profile"):
        await update.effective_message.reply_text(t("rbac_denied", lang))
        return
    target_id = user.id
    if context.args:
        try:
            target_id = int(context.args[0])
        except Exception:
            await update.effective_message.reply_text(t("admin_bad_user_id", lang))
            return
    profile = await get_user_profile(target_id)
    await update.effective_message.reply_text(
        tf(
            "admin_profile_text",
            lang,
            user_id=target_id,
            plan_type=profile.get("plan_type"),
            plan_expires_at_utc=profile.get("plan_expires_at_utc") or "-",
            role=profile.get("role"),
        )
    )


async def admin_set_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = await get_lang(user.id if user else None, getattr(user, "language_code", None))
    if not _is_private_chat(update):
        await update.effective_message.reply_text(t("admin_private_only", lang))
        return
    if not await rbac_check(user.id, PERM_PLAN_MANAGE, source="admin.set_plan"):
        await update.effective_message.reply_text(t("rbac_denied", lang))
        return

    target_id, plan_type, reason = _parse_target_and_value(context.args or [])
    if target_id is None:
        await update.effective_message.reply_text(t("admin_set_plan_usage", lang))
        return
    if plan_type not in _VALID_PLANS:
        await update.effective_message.reply_text(tf("admin_invalid_plan", lang, value=plan_type))
        return
    if not reason:
        reason = "manual admin action"
    payload = {
        "op": "set_plan",
        "target_user_id": target_id,
        "plan_type": plan_type,
        "reason": reason,
    }
    nonce_data = await create_admin_nonce(user.id, payload)
    nonce = nonce_data["nonce"]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("confirm", lang), callback_data=f"adminop:confirm:{nonce}"),
                InlineKeyboardButton(t("cancel", lang), callback_data=f"adminop:cancel:{nonce}"),
            ]
        ]
    )
    await update.effective_message.reply_text(
        tf(
            "admin_confirm_set_plan",
            lang,
            target_user_id=target_id,
            plan_type=plan_type,
            reason=reason,
            nonce=nonce,
        ),
        reply_markup=keyboard,
    )


async def admin_set_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = await get_lang(user.id if user else None, getattr(user, "language_code", None))
    if not _is_private_chat(update):
        await update.effective_message.reply_text(t("admin_private_only", lang))
        return
    if not await rbac_check(user.id, PERM_ROLE_MANAGE, source="admin.set_role"):
        await update.effective_message.reply_text(t("rbac_denied", lang))
        return

    target_id, role, reason = _parse_target_and_value(context.args or [])
    if target_id is None:
        await update.effective_message.reply_text(t("admin_set_role_usage", lang))
        return
    if role not in _VALID_ROLES:
        await update.effective_message.reply_text(tf("admin_invalid_role", lang, value=role))
        return
    if not reason:
        reason = "manual admin action"
    payload = {
        "op": "set_role",
        "target_user_id": target_id,
        "role": role,
        "reason": reason,
    }
    nonce_data = await create_admin_nonce(user.id, payload)
    nonce = nonce_data["nonce"]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("confirm", lang), callback_data=f"adminop:confirm:{nonce}"),
                InlineKeyboardButton(t("cancel", lang), callback_data=f"adminop:cancel:{nonce}"),
            ]
        ]
    )
    await update.effective_message.reply_text(
        tf(
            "admin_confirm_set_role",
            lang,
            target_user_id=target_id,
            role=role,
            reason=reason,
            nonce=nonce,
        ),
        reply_markup=keyboard,
    )


async def admin_reset_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = await get_lang(user.id if user else None, getattr(user, "language_code", None))
    if not _is_private_chat(update):
        await update.effective_message.reply_text(t("admin_private_only", lang))
        return
    if not await rbac_check(user.id, PERM_PLAN_MANAGE, source="admin.reset_limit"):
        await update.effective_message.reply_text(t("rbac_denied", lang))
        return

    if not context.args:
        await update.effective_message.reply_text(t("admin_reset_limit_usage", lang))
        return
    target_id, reason = _parse_target_and_reason(context.args or [])
    if target_id is None:
        await update.effective_message.reply_text(t("admin_bad_user_id", lang))
        return
    month_label = None
    tail = context.args[1:] if len(context.args) > 1 else []
    if tail:
        candidate = str(tail[0]).strip()
        if candidate.isdigit() and len(candidate) == 6:
            try:
                month_label = normalize_usage_month_label(candidate)
            except ValueError:
                await update.effective_message.reply_text(tf("admin_reset_limit_bad_month", lang, value=candidate))
                return
            reason = " ".join(tail[1:]).strip()
        else:
            reason = " ".join(tail).strip()
    if not reason:
        reason = "manual admin action"

    payload = {
        "op": "reset_usage",
        "target_user_id": target_id,
        "month_label": month_label,
        "reason": reason,
    }
    nonce_data = await create_admin_nonce(user.id, payload)
    nonce = nonce_data["nonce"]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("confirm", lang), callback_data=f"adminop:confirm:{nonce}"),
                InlineKeyboardButton(t("cancel", lang), callback_data=f"adminop:cancel:{nonce}"),
            ]
        ]
    )
    await update.effective_message.reply_text(
        tf(
            "admin_confirm_reset_limit",
            lang,
            target_user_id=target_id,
            month_label=month_label or normalize_usage_month_label(),
            reason=reason,
            nonce=nonce,
        ),
        reply_markup=keyboard,
    )


async def admin_reset_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = await get_lang(user.id if user else None, getattr(user, "language_code", None))
    if not _is_private_chat(update):
        await update.effective_message.reply_text(t("admin_private_only", lang))
        return
    if not await rbac_check(user.id, PERM_PLAN_MANAGE, source="admin.reset_premium"):
        await update.effective_message.reply_text(t("rbac_denied", lang))
        return

    target_id, reason = _parse_target_and_reason(context.args or [])
    if target_id is None:
        await update.effective_message.reply_text(t("admin_reset_premium_usage", lang))
        return
    if not reason:
        reason = "manual admin action"
    payload = {
        "op": "set_plan",
        "target_user_id": target_id,
        "plan_type": PLAN_FREE,
        "reason": reason,
    }
    nonce_data = await create_admin_nonce(user.id, payload)
    nonce = nonce_data["nonce"]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("confirm", lang), callback_data=f"adminop:confirm:{nonce}"),
                InlineKeyboardButton(t("cancel", lang), callback_data=f"adminop:cancel:{nonce}"),
            ]
        ]
    )
    await update.effective_message.reply_text(
        tf(
            "admin_confirm_set_plan",
            lang,
            target_user_id=target_id,
            plan_type=PLAN_FREE,
            reason=reason,
            nonce=nonce,
        ),
        reply_markup=keyboard,
    )


async def admin_grant_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = await get_lang(user.id if user else None, getattr(user, "language_code", None))
    if not _is_private_chat(update):
        await update.effective_message.reply_text(t("admin_private_only", lang))
        return
    if not await rbac_check(user.id, PERM_PLAN_MANAGE, source="admin.grant_month"):
        await update.effective_message.reply_text(t("rbac_denied", lang))
        return

    target_id, reason = _parse_target_and_reason(context.args or [])
    if target_id is None:
        await update.effective_message.reply_text(t("admin_grant_month_usage", lang))
        return
    if not reason:
        reason = "manual admin action"
    payload = {
        "op": "set_plan",
        "target_user_id": target_id,
        "plan_type": PLAN_PREMIUM_MONTHLY,
        "reason": reason,
    }
    nonce_data = await create_admin_nonce(user.id, payload)
    nonce = nonce_data["nonce"]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("confirm", lang), callback_data=f"adminop:confirm:{nonce}"),
                InlineKeyboardButton(t("cancel", lang), callback_data=f"adminop:cancel:{nonce}"),
            ]
        ]
    )
    await update.effective_message.reply_text(
        tf(
            "admin_confirm_set_plan",
            lang,
            target_user_id=target_id,
            plan_type=PLAN_PREMIUM_MONTHLY,
            reason=reason,
            nonce=nonce,
        ),
        reply_markup=keyboard,
    )


async def admin_operation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    user = query.from_user
    lang = await get_lang(user.id, user.language_code)
    if not _is_private_chat(update):
        try:
            await query.answer(t("admin_private_only", lang), show_alert=True)
        except BadRequest:
            pass
        return
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        try:
            await query.answer(t("admin_nonce_invalid", lang), show_alert=True)
        except BadRequest:
            pass
        return
    _, action, nonce = parts
    if action not in ("confirm", "cancel"):
        try:
            await query.answer(t("admin_nonce_invalid", lang), show_alert=True)
        except BadRequest:
            pass
        return
    payload = await consume_admin_nonce(nonce)
    if not payload:
        log_event(
            "admin.nonce.invalid_or_expired",
            level="WARNING",
            error_code=ERR_ADMIN_NONCE_EXPIRED,
            user_id=user.id,
            nonce=nonce,
        )
        try:
            await query.edit_message_text(t("admin_nonce_expired", lang))
        except Exception:
            await query.message.reply_text(t("admin_nonce_expired", lang))
        return

    owner_id = int(payload.get("initiator_user_id") or 0)
    if owner_id != user.id:
        log_event(
            "security.suspicious.admin_nonce_owner_mismatch",
            level="WARNING",
            error_code=ERR_ADMIN_NONCE_INVALID,
            user_id=user.id,
            owner_id=owner_id,
            nonce=nonce,
        )
        try:
            await query.answer(t("admin_nonce_not_for_you", lang), show_alert=True)
        except BadRequest:
            pass
        return

    if action == "cancel":
        try:
            await query.edit_message_text(t("admin_operation_cancelled", lang))
        except Exception:
            await query.message.reply_text(t("admin_operation_cancelled", lang))
        return

    op_payload = payload.get("payload") or {}
    op = op_payload.get("op")
    required_perm = PERM_ROLE_MANAGE if op == "set_role" else PERM_PLAN_MANAGE
    if not await rbac_check(user.id, required_perm, source=f"admin.confirm.{op or 'unknown'}"):
        try:
            await query.edit_message_text(t("rbac_denied", lang))
        except Exception:
            await query.message.reply_text(t("rbac_denied", lang))
        return

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            apply_admin_payload_sync,
            op_payload,
            user.id,
        )
    except PermissionError as e:
        code = ERR_ADMIN_SELF_ESCALATION if "self_escalation" in str(e) else ERR_RBAC_DENIED
        log_event("admin.operation.denied", level="WARNING", error_code=code, user_id=user.id, error=str(e))
        text = t("admin_self_escalation_denied", lang) if code == ERR_ADMIN_SELF_ESCALATION else t("rbac_denied", lang)
        try:
            await query.edit_message_text(text)
        except Exception:
            await query.message.reply_text(text)
        return
    except RuntimeError as e:
        code = ERR_LAST_SUPERADMIN if ERR_LAST_SUPERADMIN in str(e) else None
        text = t("admin_last_superadmin_denied", lang) if code == ERR_LAST_SUPERADMIN else tf("admin_operation_failed", lang, error=str(e))
        try:
            await query.edit_message_text(text)
        except Exception:
            await query.message.reply_text(text)
        return
    except Exception as e:
        log_event("admin.operation.failed", level="ERROR", user_id=user.id, error=str(e))
        try:
            await query.edit_message_text(tf("admin_operation_failed", lang, error=str(e)))
        except Exception:
            await query.message.reply_text(tf("admin_operation_failed", lang, error=str(e)))
        return

    op = result.get("op")
    details = result.get("profile") or {}
    if op == "set_plan":
        text = tf(
            "admin_set_plan_done",
            lang,
            target_user_id=details.get("user_id"),
            plan_type=details.get("plan_type"),
            plan_expires_at_utc=details.get("plan_expires_at_utc") or "-",
        )
    elif op == "set_role":
        text = tf(
            "admin_set_role_done",
            lang,
            target_user_id=details.get("user_id"),
            role=details.get("role"),
        )
    else:
        usage_info = result.get("usage") or {}
        text = tf(
            "admin_reset_limit_done",
            lang,
            target_user_id=usage_info.get("user_id"),
            month_label=usage_info.get("month_label"),
            previous_count=usage_info.get("previous_count", 0),
        )
    try:
        await query.edit_message_text(text)
    except Exception:
        await query.message.reply_text(text)
