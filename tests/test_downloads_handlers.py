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
        reply_audio=AsyncMock(),
        reply_video=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=user,
        effective_message=message,
        message=message,
    )


class DownloadsHandlersTests(unittest.IsolatedAsyncioTestCase):
    async def _run_download_with_metadata_offer_failure(self, logs_enabled):
        update = _build_update()
        context = _DummyContext()
        status_message = SimpleNamespace(delete=AsyncMock())
        update.message.reply_text = AsyncMock(return_value=status_message)

        class DummyYoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                return {"duration": 1}

        def fake_worker(url, tmpdir, *args):
            file_path = os.path.join(tmpdir, "track.mp3")
            with open(file_path, "wb") as fh:
                fh.write(b"fake mp3")
            return {
                "status": "ok",
                "mode": "file",
                "file_path": file_path,
                "ext": "mp3",
                "title": "Title",
                "uploader": "Artist",
            }

        async def noop(*args, **kwargs):
            return None

        with (
            mock.patch.object(downloads, "get_lang", new=AsyncMock(return_value="ru")),
            mock.patch.object(downloads, "get_user_logs_enabled", new=AsyncMock(return_value=logs_enabled)),
            mock.patch.object(downloads, "get_user_settings", new=AsyncMock(return_value={"metadata_prompt_enabled": True, "logs": logs_enabled})),
            mock.patch.object(downloads, "resolve_ffmpeg_path", return_value="ffmpeg"),
            mock.patch.object(downloads, "YoutubeDL", DummyYoutubeDL),
            mock.patch.object(downloads, "_sync_worker", side_effect=fake_worker),
            mock.patch.object(downloads, "_progress_watcher", side_effect=noop),
            mock.patch.object(downloads, "_progress_consumer", side_effect=noop),
            mock.patch.object(downloads, "_stall_watchdog", side_effect=noop),
            mock.patch.object(downloads, "register_active_download_task"),
            mock.patch.object(downloads, "unregister_active_download_task"),
            mock.patch.object(downloads, "register_active_worker_future"),
            mock.patch.object(downloads, "unregister_active_worker_future"),
            mock.patch.object(downloads, "maybe_offer_metadata_edit", new=AsyncMock(side_effect=RuntimeError("metadata boom"))),
            mock.patch.object(downloads, "log_event") as log_mock,
        ):
            result = await downloads.download_content(
                update,
                context,
                "https://example.com/track",
                platform="soundcloud",
                message=update.message,
            )

        self.assertFalse(result["metadata_prompt_offered"])
        update.message.reply_audio.assert_awaited_once()
        status_message.delete.assert_awaited()
        return log_mock

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

    async def test_download_logs_do_not_include_user_id_when_user_logs_disabled(self):
        log_mock = await self._run_download_with_metadata_offer_failure(logs_enabled=False)

        for call in log_mock.call_args_list:
            self.assertNotIn("user_id", call.kwargs)

        metadata_calls = [call for call in log_mock.call_args_list if call.args and call.args[0] == "metadata.offer.failed"]
        self.assertEqual(len(metadata_calls), 1)
        self.assertNotIn("error", metadata_calls[0].kwargs)

    async def test_download_logs_include_user_id_when_user_logs_enabled(self):
        log_mock = await self._run_download_with_metadata_offer_failure(logs_enabled=True)

        metadata_calls = [call for call in log_mock.call_args_list if call.args and call.args[0] == "metadata.offer.failed"]
        self.assertEqual(len(metadata_calls), 1)
        self.assertEqual(metadata_calls[0].kwargs.get("user_id"), 1)
        self.assertEqual(metadata_calls[0].kwargs.get("error"), "metadata boom")


if __name__ == "__main__":
    unittest.main()
