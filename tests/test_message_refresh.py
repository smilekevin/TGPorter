"""Exercise real download retries with Bot-owned and empty Pyrogram messages."""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from module.telegram import pyrogram
from pyrogram import enums, types

import media_downloader as downloader
from module import bot as bot_module, pyrogram_extension as extension, download_stat
from module.app import DownloadStatus, TaskNode


class MessageRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.user = SimpleNamespace(get_messages=AsyncMock(), download_media=AsyncMock())
        self.bot = SimpleNamespace(get_messages=AsyncMock())
        self.message = self.media_message(self.bot)
        self.refreshed = self.media_message(self.bot)
        self.empty = types.Message(id=61, empty=True)
        self.bot.get_messages.return_value = self.refreshed
        self.user.get_messages.return_value = self.empty
        self.app = SimpleNamespace(
            media_types=["video"], file_formats={"video": ["all"]},
            enable_download_txt=False, hide_file_name=False,
            set_download_id=Mock(), upload_file=AsyncMock(return_value=False),
        )
        self.node = TaskNode(chat_id=100, from_user_id=100, task_id=1,
                             task_id_display="1014-1", limit=1)
        self.node.total_task = 1
        real_sleep = asyncio.sleep

        async def short_sleep(delay):
            await real_sleep(0)

        patches = [
            patch.object(download_stat, "_HISTORY_FILE", str(self.path / "history.json")),
            patch.object(download_stat, "_skipped_downloads", {}),
            patch.object(downloader, "app", self.app),
            patch.object(downloader, "_get_media_meta", AsyncMock(return_value=(
                str(self.path / "saved.mp4"), str(self.path / "partial.mp4"), "mp4"))),
            patch.object(downloader, "_maybe_reconnect_client", AsyncMock(return_value=False)),
            patch.object(downloader, "_add_failed_download", Mock()),
            patch.object(extension, "_download_cache", {}),
            patch.object(extension, "report_bot_status", AsyncMock()),
            patch.object(extension, "_unified_flood_wait", {"until": 0, "reason": ""}),
            patch.object(downloader.asyncio, "sleep", short_sleep),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def media_message(owner):
        return types.Message(
            client=owner, id=61, empty=False,
            chat=types.Chat(id=100, type=enums.ChatType.PRIVATE),
            media=enums.MessageMediaType.VIDEO,
            video=SimpleNamespace(file_size=32, date=datetime.now()),
        )

    async def test_refresh_uses_message_owner_instead_of_download_client(self):
        refreshed = await extension.fetch_message(self.user, self.message)
        self.assertIs(refreshed, self.refreshed)
        self.bot.get_messages.assert_awaited_once_with(chat_id=100, message_ids=61)
        self.user.get_messages.assert_not_awaited()

    async def test_text_download_passes_task_requester_to_path_builder(self):
        self.app.enable_download_txt = True
        self.message.text = "message text"
        self.message.media = None
        self.message.from_user = SimpleNamespace(id=999)
        self.node.from_user_id = 123
        with patch.object(downloader, "download_media", AsyncMock(
                return_value=(DownloadStatus.SkipDownload, None, ""))), \
                patch.object(downloader, "save_msg_to_file", AsyncMock(
                    return_value=(DownloadStatus.SkipDownload, None))) as save_text:
            await downloader.download_task(self.user, self.message, self.node)
        save_text.assert_awaited_once_with(self.app, self.node.chat_id, self.message, 123)

    async def test_filter_rejection_skips_network_text_saving_and_failure_history(self):
        self.node.download_filter = "media_file_size >= 100MB"
        self.app.get_caption_name = Mock(return_value="")
        self.app.exec_filter = Mock(return_value=False)
        self.app.enable_download_txt = True
        self.message.text = "message text"
        self.message.media = None
        with patch.object(downloader, "set_meta_data") as metadata, \
                patch.object(downloader, "download_media", AsyncMock()) as transfer, \
                patch.object(downloader, "save_msg_to_file", AsyncMock()) as save_text, \
                patch.object(downloader, "_add_failed_download") as failure:
            await downloader.download_task(self.user, self.message, self.node)
        metadata.assert_called_once()
        transfer.assert_not_awaited()
        save_text.assert_not_awaited()
        failure.assert_not_called()
        self.assertIs(self.node.download_status[self.message.id], DownloadStatus.SkipDownload)

    async def test_worker_applies_real_or_rules_before_media_transfer(self):
        from module.app import Application
        from module.filter import Filter
        self.app.download_filter = Filter()
        self.app.exec_filter = lambda config, metadata: Application.exec_filter(self.app, config, metadata)
        self.app.get_caption_name = Mock(return_value="")
        self.app.download_filters = ["media_file_size >= 100MB", "media_duration <= 600"]
        self.node.download_filter = Application.resolve_download_filter(self.app)
        self.message.video.file_id = "offline-file"
        for size, duration, accepted in ((150, 300, True), (50, 300, True), (150, 700, True), (50, 700, False)):
            with self.subTest(size=size, duration=duration), \
                    patch.object(extension, "get_extension", return_value="mp4"), \
                    patch.object(downloader, "download_media", AsyncMock(
                        return_value=(DownloadStatus.SkipDownload, None, ""))) as transfer:
                self.message.video.file_size = size * 1024**2
                self.message.video.duration = duration
                await downloader.download_task(self.user, self.message, self.node)
                self.assertEqual(transfer.await_count, int(accepted))

    async def test_empty_inputs_never_access_chat_id_or_call_telegram(self):
        for message in (None, self.empty, types.Message(id=61)):
            with self.subTest(message=message):
                self.assertIsNone(await extension.fetch_message(self.user, message))
        self.user.get_messages.assert_not_awaited()

    async def test_empty_response_is_normalized(self):
        self.bot.get_messages.return_value = self.empty
        self.assertIsNone(await extension.fetch_message(self.user, self.message))

    async def test_unbound_message_uses_supplied_client(self):
        self.message._client = None
        self.user.get_messages.return_value = self.refreshed
        self.assertIs(await extension.fetch_message(self.user, self.message), self.refreshed)
        self.user.get_messages.assert_awaited_once_with(chat_id=100, message_ids=61)

    async def test_real_download_recovers_bot_reference_after_partial_file(self):
        attempts = 0

        async def download(message, file_name, **kwargs):
            nonlocal attempts
            attempts += 1
            if message.empty:
                raise ValueError("This message doesn't contain any downloadable media")
            Path(file_name).write_bytes(b"" if attempts == 1 else b"x" * 32)
            return file_name

        self.user.download_media.side_effect = download
        # A zero-byte first result reproduces FILE_REFERENCE_EXPIRED being
        # swallowed by Pyrogram, followed by the app's size-check BadRequest.
        await downloader.download_task(self.user, self.message, self.node)
        self.assertEqual((self.path / "saved.mp4").read_bytes(), b"x" * 32)
        self.assertEqual(self.node.success_download_task, 1)
        self.assertEqual(self.node.failed_download_task, 0)
        self.bot.get_messages.assert_awaited_once()
        self.user.get_messages.assert_not_awaited()

    async def test_missing_refreshed_message_records_failure_without_worker_exception(self):
        self.bot.get_messages.return_value = self.empty
        self.user.download_media.return_value = None
        await downloader.download_task(self.user, self.message, self.node)
        self.assertEqual(self.node.download_status[61], DownloadStatus.FailedDownload)
        self.assertEqual(self.node.failed_download_task, 1)
        self.assertEqual(self.user.download_media.await_count, 1)
        self.assertNotIn("NoneType", downloader._add_failed_download.call_args.kwargs["error_message"])

    async def test_timeout_refreshing_missing_message_stops_cleanly(self):
        self.bot.get_messages.return_value = self.empty
        self.user.download_media.side_effect = TimeoutError("download timed out")
        await downloader.download_task(self.user, self.message, self.node)
        self.assertEqual(self.node.download_status[61], DownloadStatus.FailedDownload)
        self.assertEqual(self.user.download_media.await_count, 1)

    async def test_real_worker_uses_bot_client_for_bot_owned_download(self):
        self.app.is_running = True
        self.app.max_download_task = 1
        self.node.client = self.bot
        self.bot.download_media = AsyncMock()

        async def download(message, file_name, **kwargs):
            Path(file_name).write_bytes(b"x" * 32)
            return file_name

        self.bot.download_media.side_effect = download
        self.user.download_media.side_effect = AssertionError("Wrong download account")
        owner = SimpleNamespace(_active_downloads=0, _in_queue={1}, bot=self.bot)
        queue = asyncio.Queue()
        await queue.put((self.message, self.node))
        with patch.object(bot_module, "_bot", owner), \
                patch.object(downloader, "queue", queue), \
                patch.object(downloader, "_live_workers", 0), \
                patch.object(downloader, "update_download_state"):
            worker = asyncio.create_task(downloader.worker(self.user))
            try:
                async def finished():
                    while self.node.success_download_task == 0 or owner._active_downloads:
                        await asyncio.sleep(0)
                await asyncio.wait_for(finished(), timeout=1)
                self.bot.download_media.assert_awaited_once()
                self.user.download_media.assert_not_awaited()
                self.assertEqual(owner._active_downloads, 0)
            finally:
                self.app.is_running = False
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
