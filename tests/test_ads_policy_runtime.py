import os
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "test-token")

from app import ads_store, config, state  # noqa: E402
from app.handlers import metadata  # noqa: E402
from app.i18n import _build_bot_commands  # noqa: E402
from app.policy import resolve_user_download_policy  # noqa: E402


class AdsPolicyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._redis = state.REDIS_CLIENT
        self._campaigns = dict(state.LOCAL_AD_CAMPAIGNS)
        self._stats = dict(state.LOCAL_AD_STATS)
        state.REDIS_CLIENT = None
        state.LOCAL_AD_CAMPAIGNS.clear()
        state.LOCAL_AD_STATS.clear()

    def tearDown(self):
        state.REDIS_CLIENT = self._redis
        state.LOCAL_AD_CAMPAIGNS.clear()
        state.LOCAL_AD_CAMPAIGNS.update(self._campaigns)
        state.LOCAL_AD_STATS.clear()
        state.LOCAL_AD_STATS.update(self._stats)

    async def test_policy_is_unlimited_with_three_hour_cap(self):
        policy = await resolve_user_download_policy({"user_id": 1, "role": "user"})
        self.assertFalse(policy["blocked_by_limit"])
        self.assertTrue(policy["unlimited_requests"])
        self.assertEqual(policy["max_duration_seconds"], config.MAX_MEDIA_DURATION_SECONDS)
        self.assertEqual(policy["max_duration_seconds"], 3 * 60 * 60)

    def test_bot_commands_do_not_include_premium(self):
        commands = _build_bot_commands("en")
        names = [item.command for item in commands]
        self.assertNotIn("premium", names)

    async def test_metadata_prompt_is_available_without_plan_gate(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        with mock.patch.object(
            metadata,
            "create_session",
            new=AsyncMock(return_value={"session_id": "s1"}),
        ) as create_mock:
            session = await metadata.maybe_offer_metadata_edit(
                context=SimpleNamespace(),
                message=message,
                user_id=1,
                lang="en",
                plan_type="free",
                settings={"metadata_prompt_enabled": True},
                file_path="track.mp3",
                title="Title",
                artist="Artist",
                source_job_id="job",
            )
        self.assertEqual(session["session_id"], "s1")
        create_mock.assert_awaited_once()
        message.reply_text.assert_awaited_once()

    def test_ad_store_builds_manual_broadcast_campaign(self):
        ad = ads_store.create_ad_sync(
            text="Try this service",
            button_text="Open",
            url="https://example.com",
            advertiser="Example LLC",
            erid="test-erid",
            created_by=7,
        )

        fetched = ads_store.get_ad_sync(ad["ad_id"])
        self.assertEqual(fetched["ad_id"], ad["ad_id"])
        self.assertIn("Реклама", ads_store.build_ad_message(fetched))
        self.assertIn("Рекламодатель: Example LLC", ads_store.build_ad_message(fetched))

        ads_store.record_ad_impression_sync(ad["ad_id"])
        self.assertEqual(state.LOCAL_AD_STATS[ad["ad_id"]], 1)

        updated = ads_store.set_ad_enabled_sync(ad["ad_id"], False)
        self.assertFalse(updated["enabled"])

        self.assertTrue(ads_store.delete_ad_sync(ad["ad_id"]))
        self.assertIsNone(ads_store.get_ad_sync(ad["ad_id"]))


if __name__ == "__main__":
    unittest.main()
