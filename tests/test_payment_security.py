import importlib
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("BOT_TOKEN", "test-token")

from app import config, logging_utils  # noqa: E402
from app.payment_service import build_payment_session_key  # noqa: E402
from app.ytdlp_cookies import prepare_ytdlp_cookiefile  # noqa: E402


class PaymentSecurityTests(unittest.TestCase):
    def _reload_config(self):
        importlib.reload(config)
        return config

    def test_strict_prod_blocks_insecure_urls(self):
        with mock.patch.dict(
            os.environ,
            {
                "BOT_TOKEN": "test-token",
                "APP_ENV": "prod",
                "PAYMENTS_STRICT_PROD": "1",
                "PAYMENTS_DB_REQUIRED": "1",
                "PAYMENTS_ALLOW_INMEMORY_FALLBACK": "0",
                "MIGRATIONS_DATABASE_URL": "postgresql://user:pass@db.example.com/app",
                "PAYMENTS_DATABASE_URL": "postgresql://user:pass@db.example.com/app",
                "REDIS_REQUIRED": "1",
                "REDIS_URL": "redis://:secret@redis.example.com:6379/0",
                "YOOKASSA_WEBHOOK_ENABLED": "1",
                "YOOKASSA_WEBHOOK_PATH": "bad-path",
            },
            clear=False,
        ):
            cfg = self._reload_config()
            with self.assertRaises(RuntimeError):
                cfg.validate_runtime_configuration()

        with mock.patch.dict(os.environ, {"APP_ENV": "dev", "BOT_TOKEN": "test-token"}, clear=False):
            self._reload_config()

    def test_log_sanitizer_drops_payment_pii_and_secrets(self):
        self.assertIsNone(logging_utils._sanitize_log_value("invoice_payload", "premium_monthly:1:1"))
        self.assertIsNone(logging_utils._sanitize_log_value("raw_payload", {"a": 1}))
        self.assertIsNone(logging_utils._sanitize_log_value("api_token", "secret"))
        sanitized = logging_utils.sanitize_text("postgresql://user:pass@example.com/app?token=abc")
        self.assertIn("[REDACTED]", sanitized)

    def test_payment_session_key_is_stable_inside_same_window(self):
        with mock.patch("app.payment_service.time.time", return_value=1_700_000_000):
            first = build_payment_session_key(42, "premium_monthly")
            second = build_payment_session_key(42, "premium_monthly")
        self.assertEqual(first, second)

    def test_cookiefile_is_copied_to_runtime_writable_path(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "cookies.txt")
            runtime_dir = os.path.join(root, "runtime")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("cookie-data")
            copied = prepare_ytdlp_cookiefile(runtime_dir, source_path=source)
            self.assertNotEqual(os.path.abspath(copied), os.path.abspath(source))
            self.assertTrue(os.path.isfile(copied))
            with open(copied, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "cookie-data")


if __name__ == "__main__":
    unittest.main()
