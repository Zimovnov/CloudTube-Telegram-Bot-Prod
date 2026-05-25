import json
from pathlib import Path

from telegram import BotCommand

from app.config import MIN_SECONDS_BETWEEN_DOWNLOADS
from app.settings_store import get_user_settings


def _load_locale(language):
    base_dir = Path(__file__).resolve().parent.parent
    path = base_dir / "locales" / f"{language}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in list(data.items()):
        if isinstance(value, str):
            data[key] = value.replace("{min_seconds}", str(MIN_SECONDS_BETWEEN_DOWNLOADS))
    return data


_RU = _load_locale("ru")
_EN = _load_locale("en")

_TRANSLATIONS = {}
for _k in set(_RU) | set(_EN):
    _TRANSLATIONS[_k] = {"ru": _RU.get(_k, _k), "en": _EN.get(_k, _RU.get(_k, _k))}


def t(key, lang="ru"):
    return _TRANSLATIONS.get(key, {}).get(lang, _TRANSLATIONS.get(key, {}).get("ru", key))


def tf(key, lang="ru", **kwargs):
    return t(key, lang).format(**kwargs)


def _normalize_lang_code(code):
    if not code:
        return None
    code = code.lower()
    if code.startswith("ru"):
        return "ru"
    if code.startswith("en"):
        return "en"
    return None


async def get_lang(user_id=None, tg_lang=None):
    lang = None
    if user_id is not None:
        try:
            s = await get_user_settings(user_id)
            lang = s.get("language")
        except Exception:
            lang = None
    if lang in ("ru", "en"):
        return lang
    fallback = _normalize_lang_code(tg_lang)
    return fallback or "ru"


def pack_mark(enabled):
    return "🟢" if enabled else "🔴"


def _build_bot_commands(lang):
    return [
        BotCommand("start", t("cmd_start", lang)),
        BotCommand("cancel", t("cmd_cancel", lang)),
        BotCommand("help", t("cmd_help", lang)),
        BotCommand("settings", t("cmd_settings", lang)),
    ]


async def setup_bot_commands(application):
    try:
        for lang in ("ru", "en"):
            await application.bot.set_my_commands(_build_bot_commands(lang), language_code=lang)
        await application.bot.set_my_commands(_build_bot_commands("en"))
    except Exception:
        pass
