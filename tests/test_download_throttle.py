"""Task termination must reset the shared low-speed notification timer."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import media_downloader as downloader
from module import download_stat as stat
from module.app import TaskNode
from module.download_service import DownloadService


MB = 1024 * 1024


class DownloadThrottleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1000.0
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.service = DownloadService(SimpleNamespace())
        for item in (
            patch.object(stat, '_download_result', {}),
            patch.object(stat, '_download_lock', asyncio.Lock()),
            patch.object(stat, '_task_heartbeat', {}),
            patch.object(stat, '_paused_tasks', set()),
            patch.object(stat, '_cancelled_tasks', set()),
            patch.object(stat, '_failed_downloads', []),
            patch.object(stat, '_discarded_tasks', set()),
            patch.object(stat, '_download_state', stat.DownloadState.Downloading),
            patch.object(stat, '_total_download_size', 0),
            patch.object(stat, '_total_download_speed', 0),
            patch.object(stat, '_last_download_time', self.now),
            patch.object(stat, '_throttle_state', dict(since=0.0, notified=False,
                                                      last_active_time=0.0, recovery_since=0.0)),
            patch.object(stat, 'save_downloads'),
            patch.object(stat.time, 'time', side_effect=lambda: self.now),
            patch.object(downloader, '_throttle_state', {'last_progress': 0.0}),
            patch.object(downloader, '_client_conn_errors', {'count': 0}),
            patch.object(downloader, '_maybe_reconnect_client', AsyncMock()),
            patch('module.pyrogram_extension.report_bot_status', AsyncMock()),
        ):
            item.start()
            self.addCleanup(item.stop)

    def node(self, task_id, chat_id=100):
        return TaskNode(chat_id=chat_id, from_user_id=200, task_id=task_id,
                        task_id_display=f'1022-{task_id}', bot=self.bot)

    async def progress(self, node, when, size, *, initial=False):
        self.now = when
        await stat.update_download_status(size, 1000 * MB, 61, 'video.mp4',
                                          1000, node, None, initial=initial)

    async def start(self, node, when=1000, restored=0):
        self.now = when
        stat.prepare_download(61, node)
        await self.progress(node, when, restored, initial=True)

    async def mark_slow(self, node):
        await self.start(node)
        await self.progress(node, 1001, 1024)
        await self.progress(node, 1121, 121 * 1024)

    async def test_completed_file_and_idle_time_do_not_age_a_new_task(self):
        first, second = self.node(28), self.node(29, chat_id=101)
        await self.start(first)
        await self.progress(first, 1001, 1024)
        await self.progress(first, 1002, 1000 * MB)
        await self.start(second, 1300)
        await self.progress(second, 1301, 1024)
        self.bot.send_message.assert_not_awaited()

    async def test_concurrent_downloads_continue_to_share_one_low_speed_timer(self):
        first, second = self.node(28), self.node(29, chat_id=101)
        await self.start(first)
        await self.progress(first, 1001, 1024)
        await self.start(second, 1121)
        await self.progress(second, 1122, 1024)
        self.bot.send_message.assert_awaited_once()
        self.assertIn('1022-29', self.bot.send_message.call_args.args[1])
        await self.progress(first, 1123, 123 * 1024)
        self.bot.send_message.assert_awaited_once()

    async def test_resumed_bytes_do_not_trigger_or_start_low_speed_timer(self):
        resumed = self.node(29)
        await self.start(resumed, 1000, restored=MB)
        await self.progress(resumed, 1400, MB, initial=True)
        self.bot.send_message.assert_not_awaited()
        await self.progress(resumed, 1401, MB + 1024)
        await self.progress(resumed, 1520, MB + 120 * 1024)
        self.bot.send_message.assert_not_awaited()
        await self.progress(resumed, 1521, MB + 121 * 1024)
        self.bot.send_message.assert_awaited_once()
        self.assertIn('1022-29', self.bot.send_message.call_args.args[1])

    async def test_fast_parallel_file_resets_the_shared_unnotified_low_speed_interval(self):
        slow, fast = self.node(28), self.node(29, chat_id=101)
        await self.start(slow)
        await self.progress(slow, 1001, 1024)
        await self.start(fast, 1099)
        await self.progress(fast, 1100, MB)
        await self.progress(slow, 1121, 121 * 1024)
        self.bot.send_message.assert_not_awaited()
        await self.progress(slow, 1241, 241 * 1024)
        self.bot.send_message.assert_awaited_once()
        self.assertIn('1022-28', self.bot.send_message.call_args.args[1])

    async def test_notification_requires_120_seconds_and_15_seconds_to_recover(self):
        node = self.node(29)
        await self.start(node)
        await self.progress(node, 1001, 1024)
        await self.progress(node, 1120, 120 * 1024)
        self.bot.send_message.assert_not_awaited()
        await self.progress(node, 1121, 121 * 1024)
        self.bot.send_message.assert_awaited_once()
        self.assertTrue(self.service.get_flood_wait()['throttle']['active'])
        await self.progress(node, 1122, 122 * 1024)
        self.bot.send_message.assert_awaited_once()
        await self.progress(node, 1123, 122 * 1024 + MB)
        await self.progress(node, 1137, 122 * 1024 + 15 * MB)
        self.bot.send_message.assert_awaited_once()
        await self.progress(node, 1138, 122 * 1024 + 16 * MB)
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertIn('✅', self.bot.send_message.call_args.args[1])
        self.assertFalse(self.service.get_flood_wait()['throttle']['active'])

    async def test_completion_and_removal_clear_the_web_throttle_banner(self):
        for offset, cleanup in enumerate(('complete', 'delete', 'failed', 'heartbeat', 'failure-record')):
            with self.subTest(cleanup=cleanup):
                self.bot.send_message.reset_mock()
                node = self.node(29 + offset, chat_id=100 + offset)
                await self.mark_slow(node)
                self.assertTrue(self.service.get_flood_wait()['throttle']['active'])
                if cleanup == 'complete':
                    await self.progress(node, 1122, 1000 * MB)
                elif cleanup == 'delete':
                    stat.delete_task(node.task_id)
                elif cleanup == 'failed':
                    stat.delete_download_result_entry(node.chat_id, 61)
                elif cleanup == 'heartbeat':
                    stat.clear_task_heartbeat(f'{node.chat_id}_61')
                else:
                    stat.add_failed_download(node.chat_id, 61, node.task_id, 'video.mp4', 'offline')
                self.assertFalse(self.service.get_flood_wait()['throttle']['active'])

    async def test_finishing_before_a_warning_starts_the_next_interval_from_zero(self):
        for offset, cleanup in enumerate(('worker-end', 'cancel', 'failure')):
            with self.subTest(cleanup=cleanup):
                self.bot.send_message.reset_mock()
                first = self.node(28 + 2 * offset, chat_id=200 + 2 * offset)
                second = self.node(29 + 2 * offset, chat_id=201 + 2 * offset)
                await self.start(first)
                await self.progress(first, 1001, 1024)
                if cleanup == 'worker-end':
                    stat.clear_task_heartbeat(f'{first.chat_id}_61')
                elif cleanup == 'cancel':
                    stat.delete_task(first.task_id)
                else:
                    stat.delete_download_result_entry(first.chat_id, 61)
                await self.start(second, 1300)
                await self.progress(second, 1301, 1024)
                await self.progress(second, 1420, 120 * 1024)
                self.bot.send_message.assert_not_awaited()
                await self.progress(second, 1421, 121 * 1024)
                self.bot.send_message.assert_awaited_once()
                self.assertIn(second.task_id_display, self.bot.send_message.call_args.args[1])
                stat.clear_task_heartbeat(f'{second.chat_id}_61')

    async def test_deleting_a_missing_task_does_not_reset_a_live_warning(self):
        await self.mark_slow(self.node(29))
        self.assertFalse(stat.delete_task('not-a-task'))
        self.assertFalse(stat.delete_download_result_entry(999, 999))
        self.assertTrue(self.service.get_flood_wait()['throttle']['active'])
