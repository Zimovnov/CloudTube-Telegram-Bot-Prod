import importlib
import os
import shutil
import unittest
from unittest import mock

os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("BOT_TOKEN", "test-token")

from app import config, logging_utils  # noqa: E402
from app.ytdlp_cookies import prepare_ytdlp_cookiefile  # noqa: E402


class RuntimeSecurityTests(unittest.TestCase):
    def _reload_config(self):
        importlib.reload(config)
        return config

    def test_runtime_config_no_longer_requires_payments_database(self):
        with mock.patch.dict(
            os.environ,
            {
                "BOT_TOKEN": "test-token",
                "APP_ENV": "dev",
                "PAYMENTS_DATABASE_URL": "",
                "MIGRATIONS_DATABASE_URL": "",
            },
            clear=False,
        ):
            cfg = self._reload_config()
            cfg.validate_runtime_configuration()

    def test_prod_still_blocks_insecure_redis_url(self):
        with mock.patch.dict(
            os.environ,
            {
                "BOT_TOKEN": "test-token",
                "APP_ENV": "prod",
                "REDIS_REQUIRED": "1",
                "REDIS_URL": "redis://:secret@redis.example.com:6379/0",
            },
            clear=False,
        ):
            cfg = self._reload_config()
            with self.assertRaises(RuntimeError):
                cfg.validate_runtime_configuration()

        with mock.patch.dict(os.environ, {"APP_ENV": "dev", "BOT_TOKEN": "test-token"}, clear=False):
            self._reload_config()

    def test_prod_allows_local_compose_redis_url(self):
        with mock.patch.dict(
            os.environ,
            {
                "BOT_TOKEN": "test-token",
                "APP_ENV": "prod",
                "REDIS_REQUIRED": "1",
                "REDIS_URL": "redis://:secret@redis:6379/0",
            },
            clear=False,
        ):
            cfg = self._reload_config()
            cfg.validate_runtime_configuration()

        with mock.patch.dict(os.environ, {"APP_ENV": "dev", "BOT_TOKEN": "test-token"}, clear=False):
            self._reload_config()

    def test_log_sanitizer_drops_pii_and_secrets(self):
        self.assertIsNone(logging_utils._sanitize_log_value("invoice_payload", "legacy-payload"))
        self.assertIsNone(logging_utils._sanitize_log_value("raw_payload", {"a": 1}))
        self.assertIsNone(logging_utils._sanitize_log_value("api_token", "secret"))
        sanitized = logging_utils.sanitize_text("postgresql://user:pass@example.com/app?token=abc")
        self.assertIn("[REDACTED]", sanitized)

    def test_cookiefile_is_copied_to_runtime_writable_path(self):
        root = os.path.join(os.getcwd(), "tests_tmp_cookiefile")
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(root, exist_ok=True)
        try:
            source = os.path.join(root, "cookies.txt")
            runtime_dir = os.path.join(root, "runtime")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("cookie-data")
            copied = prepare_ytdlp_cookiefile(runtime_dir, source_path=source)
            self.assertNotEqual(os.path.abspath(copied), os.path.abspath(source))
            self.assertTrue(os.path.isfile(copied))
            with open(copied, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "cookie-data")
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
