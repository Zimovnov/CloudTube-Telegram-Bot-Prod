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


if __name__ == "__main__":
    unittest.main()
