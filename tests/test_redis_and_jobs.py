import os
import unittest
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "test-token")

from app import jobs, settings_store, state  # noqa: E402


class DummyContext:
    def __init__(self):
        self.chat_data = {}
        self.user_data = {}


class RedisAndJobsTests(unittest.TestCase):
    def setUp(self):
        self._orig_redis_client = state.REDIS_CLIENT
        self._orig_redis_url = jobs.REDIS_URL
        self._orig_redis_required = jobs.REDIS_REQUIRED
        self._orig_local_settings = dict(state.LOCAL_USER_SETTINGS)
        self._orig_last_download_time = dict(state.LAST_DOWNLOAD_TIME)
        self._orig_last_settings_change = dict(state.LAST_SETTINGS_CHANGE)

        state.REDIS_CLIENT = None
        state.LOCAL_USER_SETTINGS.clear()
        state.LAST_DOWNLOAD_TIME.clear()
        state.LAST_SETTINGS_CHANGE.clear()

    def tearDown(self):
        state.REDIS_CLIENT = self._orig_redis_client
        jobs.REDIS_URL = self._orig_redis_url
        jobs.REDIS_REQUIRED = self._orig_redis_required

        state.LOCAL_USER_SETTINGS.clear()
        state.LOCAL_USER_SETTINGS.update(self._orig_local_settings)
        state.LAST_DOWNLOAD_TIME.clear()
        state.LAST_DOWNLOAD_TIME.update(self._orig_last_download_time)
        state.LAST_SETTINGS_CHANGE.clear()
        state.LAST_SETTINGS_CHANGE.update(self._orig_last_settings_change)

    def test_allowed_start_job_local_check(self):
        ctx = DummyContext()
        self.assertTrue(jobs.allowed_start_job(ctx, user_id=1))
        ctx.chat_data["running_jobs"] = {1: 1}
        self.assertFalse(jobs.allowed_start_job(ctx, user_id=1))

    def test_start_and_finish_job_local_state(self):
        ctx = DummyContext()
        self.assertTrue(jobs.start_job(ctx, user_id=42, max_parallel=1))
        self.assertFalse(jobs.start_job(ctx, user_id=42, max_parallel=1))

        jobs.finish_job(ctx, user_id=42)
        self.assertTrue(jobs.start_job(ctx, user_id=42, max_parallel=1))

    def test_init_redis_client_raises_when_required_and_url_missing(self):
        jobs.REDIS_REQUIRED = True
        jobs.REDIS_URL = ""
        with self.assertRaises(RuntimeError):
            jobs.init_redis_client()

    def test_init_redis_client_local_fallback_when_optional_and_url_missing(self):
        jobs.REDIS_REQUIRED = False
        jobs.REDIS_URL = ""
        state.REDIS_CLIENT = object()
        jobs.init_redis_client()
        self.assertIsNone(state.REDIS_CLIENT)

    def test_user_settings_local_fallback_without_redis(self):
        uid = "1001"
        defaults = settings_store.get_user_settings_sync(uid)
        self.assertEqual(defaults["language"], "ru")
        self.assertFalse(defaults["logs"])

        settings_store.set_user_settings_sync(uid, {"language": "en", "logs": True})
        saved = settings_store.get_user_settings_sync(uid)
        self.assertEqual(saved["language"], "en")
        self.assertTrue(saved["logs"])

    def test_user_settings_read_error_falls_back_to_local(self):
        state.REDIS_CLIENT = object()
        with mock.patch.object(settings_store, "_redis_read_user_settings", side_effect=RuntimeError("read failed")):
            settings = settings_store.get_user_settings_sync("2002")
        self.assertEqual(settings["language"], "ru")
        self.assertIn("2002", state.LOCAL_USER_SETTINGS)

    def test_user_settings_write_error_falls_back_to_local(self):
        state.REDIS_CLIENT = object()
        with mock.patch.object(settings_store, "_redis_write_user_settings", side_effect=RuntimeError("write failed")):
            settings_store.set_user_settings_sync("3003", {"language": "en", "logs": True})
        self.assertEqual(state.LOCAL_USER_SETTINGS["3003"]["language"], "en")
        self.assertTrue(state.LOCAL_USER_SETTINGS["3003"]["logs"])


if __name__ == "__main__":
    unittest.main()
