"""Exercise on-demand file workers without network or persistent task data."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import media_downloader as downloader
from module import bot, pyrogram_extension as extension


class WorkerPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = SimpleNamespace(is_running=True, max_download_task=5,
                                   loop=asyncio.get_running_loop())
        self.owner = SimpleNamespace(_active_downloads=0, _in_queue=set())
        self.client = object()
        self.queue = downloader._DownloadQueue(self.client)
        self.started = []
        self.finished = []
        self.release = {}
        self.peak = 0
        self.sleep = asyncio.sleep

        async def short_sleep(delay):
            await self.sleep(min(delay, 0.001))

        async def download(client, message, node):
            self.started.append(message.id)
            self.peak = max(self.peak, self.owner._active_downloads)
            await self.release[message.id].wait()
            self.finished.append(message.id)

        for item in [
            patch.object(downloader, "app", self.app),
            patch.object(downloader, "queue", self.queue),
            patch.object(downloader, "_live_workers", 0),
            patch.object(downloader, "download_task", download),
            patch.object(downloader, "update_download_state", Mock()),
            patch.object(downloader, "_move_task_to_failed", Mock()),
            patch.object(downloader.asyncio, "sleep", short_sleep),
            patch.object(bot, "_bot", self.owner),
        ]:
            item.start()
            self.addCleanup(item.stop)

    async def asyncTearDown(self):
        self.app.is_running = False
        await self.queue.stop()
        self.assertEqual(downloader._live_workers, 0)
        self.assertEqual(self.owner._active_downloads, 0)
        downloader._move_task_to_failed.assert_not_called()

    async def until(self, condition):
        async def wait():
            while not condition():
                await self.sleep(0)
        await asyncio.wait_for(wait(), 2)

    async def submit(self, mid):
        self.release[mid] = asyncio.Event()
        self.owner._in_queue.add(mid)
        node = SimpleNamespace(chat_id=100, task_id=mid, task_id_display=str(mid),
                               is_stop_transmission=False, client=None)
        await self.queue.put((SimpleNamespace(id=mid), node))

    def release_all(self):
        for event in self.release.values():
            event.set()

    async def test_empty_start_and_limit_increase_create_no_workers(self):
        self.assertFalse(self.queue.workers)
        self.app.max_download_task = 6
        await asyncio.to_thread(downloader._worker_pool_set_target, 6)
        await self.sleep(0)
        self.assertFalse(self.queue.workers)
        self.assertEqual(downloader._live_workers, 0)

    async def test_one_file_starts_one_worker_and_idle_pool_exits(self):
        await self.submit(1)
        await self.until(lambda: self.started == [1])
        self.assertEqual(downloader._live_workers, 1)
        self.assertEqual(len(self.queue.workers), 1)
        self.release_all()
        await self.until(lambda: not self.queue.workers)
        self.assertEqual(self.finished, [1])
        self.assertEqual(self.owner._in_queue, set())

    async def test_burst_reserves_workers_before_they_start_and_bounds_concurrency(self):
        for mid in range(12):
            await self.submit(mid)
        await self.until(lambda: len(self.started) == 5)
        self.assertEqual(len(self.queue.workers), 5)
        self.assertEqual(self.queue.qsize(), 7)
        self.release_all()
        await self.until(lambda: not self.queue.workers)
        self.assertCountEqual(self.finished, range(12))
        self.assertEqual(self.peak, 5)

    async def test_two_files_do_not_precreate_the_other_three_workers(self):
        await self.submit(1)
        await self.submit(2)
        await self.until(lambda: len(self.started) == 2)
        self.assertEqual(len(self.queue.workers), 2)
        self.assertEqual(downloader._live_workers, 2)

    async def test_additional_submission_while_busy_uses_another_slot(self):
        await self.submit(1)
        await self.until(lambda: len(self.started) == 1)
        await self.submit(2)
        await self.until(lambda: len(self.started) == 2)
        self.assertEqual(len(self.queue.workers), 2)

    async def test_late_submission_restarts_an_empty_pool(self):
        await self.submit(1)
        self.release_all()
        await self.until(lambda: len(self.finished) == 1 and not self.queue.workers)
        await self.submit(2)
        await self.until(lambda: self.started == [1, 2])
        self.assertEqual(len(self.queue.workers), 1)

    async def test_submission_before_exit_callback_is_not_stranded(self):
        retired = []
        on_done = self.queue._worker_done
        # Keep the finished task registered until after the next wake-up.
        with patch.object(self.queue, "_worker_done", side_effect=retired.append):
            await self.submit(1)
            self.release_all()
            await self.until(lambda: len(retired) == 1)
        await self.submit(2)
        await self.sleep(0)
        self.assertEqual(self.started, [1])
        on_done(retired[0])
        await self.until(lambda: self.started == [1, 2])
        self.assertEqual(len(self.queue.workers), 1)

    async def test_increase_from_request_thread_starts_only_queued_work(self):
        self.app.max_download_task = 1
        for mid in range(3):
            await self.submit(mid)
        await self.until(lambda: len(self.started) == 1)
        self.app.max_download_task = 5
        await asyncio.to_thread(downloader._worker_pool_set_target, 5)
        await self.until(lambda: len(self.started) == 3)
        self.assertEqual(len(self.queue.workers), 3)

    async def test_decrease_finishes_active_files_before_claiming_more(self):
        self.app.max_download_task = 3
        for mid in range(6):
            await self.submit(mid)
        await self.until(lambda: len(self.started) == 3)
        self.app.max_download_task = 1
        await asyncio.to_thread(downloader._worker_pool_set_target, 1)
        self.assertEqual(self.started, [0, 1, 2])
        self.release[0].set()
        self.release[1].set()
        await self.until(lambda: downloader._live_workers == 1)
        self.assertEqual(self.started, [0, 1, 2])
        self.release[2].set()
        await self.until(lambda: len(self.started) == 4)
        self.assertEqual(self.owner._active_downloads, 1)
        self.release_all()
        await self.until(lambda: not self.queue.workers)
        self.assertCountEqual(self.finished, range(6))

    async def test_network_retry_reuses_slot_without_duplicate_processing(self):
        attempts = 0

        async def download(client, message, node):
            nonlocal attempts
            attempts += 1
            self.assertEqual(self.owner._active_downloads, 1)
            if attempts == 1:
                raise ConnectionError("Offline retry")
            self.finished.append(message.id)

        with patch.object(downloader, "download_task", download), \
                patch.object(downloader, "_maybe_reconnect_client", AsyncMock(return_value=True)), \
                patch.object(downloader, "_reset_task_for_retry", AsyncMock()):
            await self.submit(1)
            await self.until(lambda: attempts == 2 and not self.queue.workers)
        self.assertEqual(self.finished, [1])
        self.assertEqual(self.owner._active_downloads, 0)
        self.assertFalse(self.owner._in_queue)

    def start_bot_heartbeat(self):
        """Run the real monitor alongside the queue, replacing only Telegram I/O."""
        self.owner = bot.DownloadBot()
        self.owner.app = self.app

        async def pong(query, **kwargs):
            return bot.pyrogram.raw.types.Pong(msg_id=1, ping_id=query.ping_id)

        self.owner.bot = SimpleNamespace(is_connected=True, invoke=AsyncMock(side_effect=pong),
                                         stop=AsyncMock())
        for item in [
            patch.object(bot, "_bot", self.owner),
            patch.object(extension, "_bot_conn_errors", {"count": 0}),
            patch.object(extension, "_bot_flood_wait", {"until": 0, "seconds": 0}),
        ]:
            item.start()
            self.addCleanup(item.stop)
        monitor = self.owner._ensure_heartbeat()

        async def stop_monitor():
            self.owner.is_running = False
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

        self.addAsyncCleanup(stop_monitor)
        return monitor

    async def test_bot_heartbeat_continues_with_idle_active_and_drained_workers(self):
        monitor = self.start_bot_heartbeat()
        invoke = self.owner.bot.invoke
        await self.until(lambda: invoke.await_count > 0)
        self.assertFalse(self.queue.workers)
        self.assertIs(monitor, self.owner._ensure_heartbeat())
        await self.submit(1)
        await self.until(lambda: self.started == [1])
        probes = invoke.await_count
        await self.until(lambda: invoke.await_count > probes)
        self.assertEqual(self.owner._active_downloads, 1)
        self.assertFalse(monitor.done())
        self.release_all()
        await self.until(lambda: not self.queue.workers)
        probes = invoke.await_count
        await self.until(lambda: invoke.await_count > probes)
        self.assertFalse(monitor.done())
        self.assertEqual(self.owner._heartbeat_failures, 0)

    async def test_shutdown_awaits_bot_probe_and_file_worker_together(self):
        monitor = self.start_bot_heartbeat()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def hang(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.owner.bot.invoke.side_effect = hang
        await asyncio.wait_for(entered.wait(), 1)
        await self.submit(1)
        await self.until(lambda: self.started == [1])
        self.app.is_running = False
        with patch.object(self.owner, "update_config"), patch.object(self.owner, "stop_task"):
            await bot.stop_download_bot()
        await self.queue.stop()
        self.assertTrue(monitor.done())
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.owner.heartbeat_task)
        self.assertFalse(self.queue.workers)
        self.assertEqual(self.owner._active_downloads, 0)
        self.assertEqual(downloader._live_workers, 0)
        self.owner.bot.stop.assert_awaited_once()

    async def test_shutdown_cancels_and_awaits_active_downloads(self):
        await self.submit(1)
        await self.until(lambda: len(self.started) == 1)
        self.app.is_running = False
        await self.queue.stop()
        self.assertFalse(self.queue.workers)
        self.assertEqual(downloader._live_workers, 0)
        self.assertEqual(self.owner._active_downloads, 0)
        # A pending wake-up must not start a worker after shutdown.
        await self.submit(2)
        await self.sleep(0)
        self.assertFalse(self.queue.workers)


if __name__ == "__main__":
    unittest.main()
