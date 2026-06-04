import os
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "test-token")

from app.handlers import settings  # noqa: E402
from app.i18n import t  # noqa: E402


def _settings_payload(logs=False, language="ru"):
    return {
        "format": {"soundcloud": "audio", "youtube": "ask"},
        "quality": {"soundcloud": "best", "youtube": "best"},
        "trim": {"soundcloud": "ask", "youtube": "ask"},
        "logs": logs,
        "language": language,
        "metadata_prompt_enabled": True,
    }


def _flatten_markup(markup):
    buttons = []
    for row in markup.inline_keyboard:
        buttons.extend(row)
    return buttons


class SettingsHandlersTests(unittest.IsolatedAsyncioTestCase):
    def test_main_settings_markup_has_no_rules_button(self):
        for lang in ("ru", "en"):
            markup = settings.build_main_settings_markup(_settings_payload(language=lang), lang)
            buttons = _flatten_markup(markup)
            labels = [button.text for button in buttons]
            callbacks = [button.callback_data for button in buttons]

            self.assertNotIn("settings:limits", callbacks)
            self.assertNotIn(t("limits", lang), labels)
            self.assertFalse(any("Правила" in label or "Rules" in label for label in labels))

    async def test_settings_limits_callback_is_unknown(self):
        query = SimpleNamespace(
            data="settings:limits",
            from_user=SimpleNamespace(id=123, language_code="ru"),
            answer=AsyncMock(),
            message=SimpleNamespace(),
        )
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace()

        with (
            mock.patch.object(settings, "get_user_settings", new=AsyncMock(return_value=_settings_payload())),
            mock.patch.object(settings, "get_lang", new=AsyncMock(return_value="ru")),
            mock.patch.object(settings, "allow_settings_change", return_value=True),
        ):
            await settings.settings_callback(update, context)

        self.assertEqual(query.answer.await_count, 2)
        query.answer.assert_any_await(t("unknown_command", "ru"), show_alert=True)


if __name__ == "__main__":
    unittest.main()
