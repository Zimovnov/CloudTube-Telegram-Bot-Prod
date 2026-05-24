import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "test-token")

# downloads.py imports worker.py, which imports moviepy at module import time.
# Provide a tiny stub so handler tests do not depend on heavy media libs.
if "moviepy.editor" not in sys.modules:
    moviepy_module = types.ModuleType("moviepy")
    moviepy_editor_module = types.ModuleType("moviepy.editor")
    moviepy_editor_module.AudioFileClip = object
    moviepy_editor_module.VideoFileClip = object
    sys.modules.setdefault("moviepy", moviepy_module)
    sys.modules["moviepy.editor"] = moviepy_editor_module

from app.handlers import downloads  # noqa: E402
from app.i18n import t  # noqa: E402


class _DummyContext:
    def __init__(self):
        self.user_data = {}
        self.chat_data = {}
        self.bot = SimpleNamespace(delete_message=AsyncMock())


def _build_update(user_id=1, chat_id=100, language_code="ru"):
    user = SimpleNamespace(id=user_id, language_code=language_code, first_name="User")
    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=user,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=user,
        effective_message=message,
        message=message,
    )


class DownloadsHandlersTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_command_closes_metadata_and_uses_metadata_message(self):
        update = _build_update()
        context = _DummyContext()
        context.user_data["trim_prompt_msg_id"] = 555

        with (
            mock.patch.object(downloads, "get_lang", new=AsyncMock(return_value="ru")),
            mock.patch.object(downloads, "abort_user_job", return_value=False) as abort_job_mock,
            mock.patch.object(downloads, "cancel_active_metadata_edit", new=AsyncMock(return_value=True)) as cancel_meta_mock,
            mock.patch.object(downloads, "clear_conversation_state") as clear_state_mock,
        ):
            state = await downloads.cancel_command(update, context)

        self.assertEqual(state, downloads.ASK_TRIM)
        context.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=555)
        update.message.reply_text.assert_awaited_once_with(t("metadata_cancelled", "ru"))
        abort_job_mock.assert_called_once_with(context, 1)
        cancel_meta_mock.assert_awaited_once_with(1, reason="user_cancelled_command")
        clear_state_mock.assert_called_once_with(context, 1)

    async def test_cancel_command_uses_regular_cancel_message_without_metadata(self):
        update = _build_update()
        context = _DummyContext()

        with (
            mock.patch.object(downloads, "get_lang", new=AsyncMock(return_value="ru")),
            mock.patch.object(downloads, "abort_user_job", return_value=False) as abort_job_mock,
            mock.patch.object(downloads, "cancel_active_metadata_edit", new=AsyncMock(return_value=False)) as cancel_meta_mock,
            mock.patch.object(downloads, "clear_conversation_state") as clear_state_mock,
        ):
            state = await downloads.cancel_command(update, context)

        self.assertEqual(state, downloads.ASK_TRIM)
        update.message.reply_text.assert_awaited_once_with(t("cancelled_send_link", "ru"))
        abort_job_mock.assert_called_once_with(context, 1)
        cancel_meta_mock.assert_awaited_once_with(1, reason="user_cancelled_command")
        clear_state_mock.assert_called_once_with(context, 1)

    async def test_schedule_download_background_skips_send_another_when_metadata_prompt_offered(self):
        update = _build_update()
        context = _DummyContext()
        target_message = update.message

        with (
            mock.patch.object(downloads, "download_content", new=AsyncMock(return_value={"metadata_prompt_offered": True})),
            mock.patch.object(downloads, "register_active_download_task"),
            mock.patch.object(downloads, "unregister_active_download_task"),
            mock.patch.object(downloads, "register_scheduled_download_task"),
            mock.patch.object(downloads, "finish_job"),
            mock.patch.object(downloads, "_track_background_job_task"),
            mock.patch.object(downloads, "get_user_profile", new=AsyncMock()) as profile_mock,
            mock.patch.object(downloads, "resolve_user_download_policy", new=AsyncMock()) as policy_mock,
        ):
            task = downloads.schedule_download_background(
                update,
                context,
                url="https://youtube.com/watch?v=test",
                platform="youtube",
                user_id=1,
                user_name="User",
                lang="ru",
                yt_type="audio",
                message=target_message,
            )
            self.assertIsNotNone(task)
            await task

        target_message.reply_text.assert_not_awaited()
        profile_mock.assert_not_awaited()
        policy_mock.assert_not_awaited()

    async def test_schedule_download_background_sends_send_another_without_metadata_prompt(self):
        update = _build_update()
        context = _DummyContext()
        target_message = update.message

        with (
            mock.patch.object(downloads, "download_content", new=AsyncMock(return_value={"metadata_prompt_offered": False})),
            mock.patch.object(downloads, "register_active_download_task"),
            mock.patch.object(downloads, "unregister_active_download_task"),
            mock.patch.object(downloads, "register_scheduled_download_task"),
            mock.patch.object(downloads, "finish_job"),
            mock.patch.object(downloads, "_track_background_job_task"),
            mock.patch.object(downloads, "get_user_profile", new=AsyncMock(return_value={"user_id": 1})),
            mock.patch.object(
                downloads,
                "resolve_user_download_policy",
                new=AsyncMock(return_value={"blocked_by_limit": False, "usage_count": 0, "free_limit": 42}),
            ),
        ):
            task = downloads.schedule_download_background(
                update,
                context,
                url="https://youtube.com/watch?v=test",
                platform="youtube",
                user_id=1,
                user_name="User",
                lang="ru",
                yt_type="audio",
                message=target_message,
            )
            self.assertIsNotNone(task)
            await task

        target_message.reply_text.assert_awaited_once_with(t("send_another", "ru"))


if __name__ == "__main__":
    unittest.main()
