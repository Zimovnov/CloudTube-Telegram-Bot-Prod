import os
import unittest

os.environ.setdefault("BOT_TOKEN", "test-token")

from app.i18n import t, tf  # noqa: E402


class I18nTests(unittest.TestCase):
    def test_translation_keys_exist_for_core_messages(self):
        self.assertEqual(t("menu_title", "ru"), "Меню настроек:")
        self.assertEqual(t("menu_title", "en"), "Settings menu:")

    def test_tf_formats_placeholders(self):
        msg = tf("cooldown", "en", seconds=5)
        self.assertIn("5", msg)

    def test_no_subscription_command_in_user_text(self):
        self.assertNotIn("premium", t("start_hint", "en").lower())
        self.assertNotIn("подпис", t("start_hint", "ru").lower())
        self.assertNotIn("/legal", t("start_hint", "en").lower())
        self.assertNotIn("/legal", t("start_hint", "ru").lower())
        self.assertIn("unlimited", t("rules_text", "en").lower())
        self.assertIn("без ограничений", t("rules_text", "ru").lower())


if __name__ == "__main__":
    unittest.main()
