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

    def test_faq_does_not_expose_metadata_session_ttl(self):
        self.assertIn("1)", t("faq_text", "ru"))
        self.assertIn("3)", t("faq_text", "en"))
        self.assertNotIn("4)", t("faq_text", "ru"))
        self.assertNotIn("4)", t("faq_text", "en"))
        self.assertNotIn("сессия редактирования", t("faq_text", "ru").lower())
        self.assertNotIn("metadata edit session ttl", t("faq_text", "en").lower())


if __name__ == "__main__":
    unittest.main()
