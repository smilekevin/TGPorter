"""Exercise timed Bot reports and rate-limit notices without Telegram access."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import media_downloader as downloader
from module import bot, download_stat, pyrogram_extension as extension
from module.app import TaskNode
from module.language import Language, set_language
from module.telegram import pyrogram


class BotReportingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1000.0
        self.client = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
        self.node = TaskNode(chat_id=100, from_user_id=200, reply_message_id=300,
                             bot=self.client, task_id=1, task_id_display="1016-1", limit=1)
        self.node.last_reply_time = 0
        self.node.last_bot_flood_notice = 0
        self.record = dict(task_id=1, file_name="large.mp4", total_size=10000,
                           down_byte=100, download_speed=100)
        self.wait = {"until": 0.0, "reason": ""}
        self.bot_wait = {"until": 0.0, "seconds": 0}
        self.patches = [
            patch.object(extension, "time", SimpleNamespace(time=lambda: self.now)),
            patch("module.app.time.time", side_effect=lambda: self.now),
            patch.object(extension, "_unified_flood_wait", self.wait),
            patch.object(extension, "_bot_flood_wait", self.bot_wait),
            patch.object(extension, "_report_lock", asyncio.Lock()),
            patch.object(extension, "_bot_conn_errors", {"count": 0}),
            patch.object(download_stat, "_download_result", {100: {61: self.record}}),
            patch.object(download_stat, "_paused_tasks", set()),
            patch.object(download_stat, "_cancelled_tasks", set()),
            patch.object(download_stat, "_discarded_tasks", set()),
            patch.object(download_stat, "_download_streams", {}),
            patch.object(download_stat, "_download_state", download_stat.DownloadState.Downloading),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        set_language(Language.EN)
        self.addCleanup(set_language, Language.EN)

    async def report(self, immediate=False):
        return await extension.report_bot_status(self.client, self.node, immediate)

    async def test_report_connection_errors_include_type_and_reach_reconnect_counter(self):
        for error in (TimeoutError(), ConnectionError("private error contents")):
            with self.subTest(error=type(error).__name__):
                extension._bot_conn_errors["count"] = 0
                self.client.edit_message_text.side_effect = error
                with patch.object(extension.logger, "warning") as warning:
                    self.assertFalse(await self.report(immediate=True))
                self.assertEqual(extension._bot_conn_errors["count"], 1)
                self.assertEqual(warning.call_args.args[1], type(error).__name__)
                self.assertNotIn("private error contents", str(warning.call_args))

    async def test_progress_reports_live_streams_and_updates_when_paused(self):
        with download_stat.track_download_stream(100, 61, 1), \
                download_stat.track_download_stream(100, 61, 1):
            self.assertTrue(await self.report())
            self.assertIn('Active streams: 2', self.client.edit_message_text.call_args.args[2])
            download_stat.pause_task('100_61')
            self.now += 10
            self.assertTrue(await self.report())
            self.assertIn('Active streams: 0', self.client.edit_message_text.call_args.args[2])
            download_stat.resume_task('100_61')
            set_language(Language.ZH_CN)
            self.now += 10
            self.assertTrue(await self.report())
            self.assertIn('当前下载流数：2', self.client.edit_message_text.call_args.args[2])
        self.now += 10
        self.assertTrue(await self.report())
        self.assertIn('当前下载流数：0', self.client.edit_message_text.call_args.args[2])

    async def test_progress_updates_within_same_twenty_percent_bucket(self):
        self.assertTrue(await self.report())
        self.record["down_byte"] = 200
        self.now += 10
        self.assertTrue(await self.report())
        self.assertEqual(self.client.edit_message_text.await_count, 2)
        self.assertIn("(2%)", self.client.edit_message_text.call_args.args[2])
        self.client.send_message.assert_not_awaited()

    async def test_interval_and_unchanged_message_do_not_spam(self):
        await self.report()
        self.record["down_byte"] = 200
        self.now += 9
        self.assertFalse(await self.report())
        self.now += 1
        await self.report()
        self.now += 10
        await self.report()
        self.assertEqual(self.client.edit_message_text.await_count, 2)

    async def test_downloaded_bytes_refresh_even_with_unchanged_integer_percentage(self):
        await self.report()
        self.record["down_byte"] = 150
        self.now += 10
        await self.report()
        self.assertEqual(self.client.edit_message_text.await_count, 2)
        self.assertIn("150B /", self.client.edit_message_text.call_args.args[2])
        self.assertIn("(1%)", self.client.edit_message_text.call_args.args[2])

    async def test_countdown_and_manual_pause_update_without_new_bytes(self):
        self.wait.update(until=self.now + 900, reason="FLOOD_WAIT")
        download_stat.pause_task("100_61")
        await self.report()
        first = self.client.edit_message_text.call_args.args[2]
        self.assertIn("FLOOD_WAIT", first)
        self.assertIn("Task paused", first)
        self.now += 10
        await self.report()
        self.assertNotEqual(first, self.client.edit_message_text.call_args.args[2])
        self.assertEqual(self.client.edit_message_text.await_count, 2)

    async def test_shorter_bot_flood_preserves_longer_download_cooldown(self):
        self.wait.update(until=self.now + 900, reason="FLOOD_WAIT")
        for error in (pyrogram.errors.FloodWait, pyrogram.errors.FloodPremiumWait):
            with self.subTest(error=error):
                self.bot_wait["until"] = 0
                self.node.flood_wait_until = 0
                self.client.edit_message_text.side_effect = error(30)
                self.assertFalse(await self.report(immediate=True))
                self.assertEqual(self.wait["until"], self.now + 900)
                self.assertEqual(self.node.last_edit_msg, "")
                before = self.client.edit_message_text.await_count
                self.assertFalse(await self.report(immediate=True))
                self.assertEqual(self.client.edit_message_text.await_count, before)

    async def test_bot_cooldown_applies_to_other_nodes_and_resumes_with_notice(self):
        self.client.edit_message_text.side_effect = pyrogram.errors.FloodWait(30)
        await self.report()
        other = TaskNode(chat_id=100, from_user_id=201, reply_message_id=301,
                         bot=self.client, task_id=2, task_id_display="1016-2")
        self.assertFalse(await extension.report_bot_status(self.client, other, True))
        self.client.edit_message_text.assert_awaited_once()
        self.now += 31
        self.client.edit_message_text.side_effect = None
        self.assertTrue(await self.report())
        self.assertIn("notification FLOOD_WAIT ended after 30 seconds",
                      self.client.edit_message_text.call_args.args[2])

    async def test_final_report_obeys_node_cooldown_and_retries_after_expiry(self):
        self.node.flood_wait_until = self.now + 30
        self.node.total_download_task = self.node.success_download_task = 1
        self.record["down_byte"] = self.record["total_size"]
        self.assertFalse(await self.report(immediate=True))
        self.client.edit_message_text.assert_not_awaited()
        self.now += 31
        self.assertTrue(await self.report(immediate=True))
        self.assertIn("Success", self.client.edit_message_text.call_args.args[2])

    async def test_failed_completion_remains_registered_until_report_delivered(self):
        backend = bot.DownloadBot()
        backend.bot = self.client
        backend.is_running = self.node.is_running = True
        backend.task_node = {1: self.node}
        self.node.total_task = self.node.total_download_task = 1

        async def stop_loop(_):
            backend.is_running = False

        with patch.object(bot, "report_bot_status", AsyncMock(return_value=False)), patch.object(
                bot.asyncio, "sleep", stop_loop), patch("module.task_store.complete_task") as complete:
            await backend.update_reply_message()
        self.assertIn(1, backend.task_node)
        complete.assert_not_called()
        backend.is_running = True
        with patch.object(bot, "report_bot_status", AsyncMock(return_value=True)), patch.object(
                bot.asyncio, "sleep", stop_loop), patch("module.task_store.complete_task") as complete, patch(
                "module.task_store.get_pending_tasks", return_value=[]):
            await backend.update_reply_message()
        self.assertNotIn(1, backend.task_node)
        complete.assert_called_once_with(1)

    async def test_notification_failure_and_timeout_are_bounded(self):
        self.client.send_message.side_effect = pyrogram.errors.FloodWait(30)
        self.assertFalse(await extension.send_bot_notification(self.client, 200, "pause"))
        self.assertFalse(await extension.send_bot_notification(self.client, 200, "pause"))
        self.client.send_message.assert_awaited_once()
        self.now += 31

        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()

        self.client.send_message.side_effect = stalled
        self.client.edit_message_text.side_effect = stalled
        with patch.object(extension, "_BOT_REPORT_TIMEOUT", 0.01):
            self.assertFalse(await asyncio.wait_for(
                extension.send_bot_notification(self.client, 200, "pause"), 1))
            self.assertFalse(await asyncio.wait_for(self.report(immediate=True), 1))

    async def test_lock_wait_is_bounded(self):
        await extension._report_lock.acquire()
        try:
            with patch.object(extension, "_BOT_REPORT_TIMEOUT", 0.01):
                self.assertFalse(await asyncio.wait_for(self.report(), 1))
        finally:
            extension._report_lock.release()

    async def test_long_download_flood_announces_automatic_resume_and_recovery(self):
        clock = SimpleNamespace(time=lambda: self.now, monotonic=Mock(side_effect=[0, 901]))

        with patch.object(downloader, "time", clock), patch.object(
                extension, "get_flood_wait_remaining", return_value=0):
            self.assertTrue(await downloader._wait_download_flood(900, 61, self.node, self.client))
        texts = [call.args[1] for call in self.client.send_message.await_args_list]
        self.assertIn("Retry automatically", texts[0])
        self.assertIn("900", texts[0])
        self.assertIn("resuming download", texts[1])
        self.assertFalse(download_stat.is_task_paused("100_61"))

    async def test_cancel_during_flood_does_not_announce_recovery(self):
        with patch.object(download_stat, "wait_download_ready", AsyncMock(
                side_effect=pyrogram.StopTransmission)):
            self.assertFalse(await downloader._wait_download_flood(30, 61, self.node, self.client))
        self.client.send_message.assert_awaited_once()

    async def test_no_recipient_does_not_infer_bot_notification(self):
        self.assertFalse(await extension.send_bot_notification(self.client, "", "pause"))
        self.client.send_message.assert_not_awaited()
