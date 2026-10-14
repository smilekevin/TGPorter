"""Offline regression tests using real persistence, consumer and worker code."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import media_downloader as downloader
from module import bot, download_stat, task_store as store
from module.app import TaskNode


class PendingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = bot.DownloadBot()
        self.bot.config_path = str(Path(self.temp.name) / "bot.yaml")
        self.app = SimpleNamespace(
            loop=asyncio.get_running_loop(), max_download_task=2,
            is_running=True, application_name="test", api_id=1, api_hash="test",
            bot_token="test", session_file_path=self.temp.name, proxy=None,
            allowed_user_ids=[], chat_download_config={},
        )
        self.bot.app = self.app
        self.client = SimpleNamespace(
            get_messages=AsyncMock(side_effect=lambda cid, mid: self.message(mid, cid)),
            get_me=AsyncMock(return_value=SimpleNamespace(id=100)),
            get_chat=AsyncMock(return_value=SimpleNamespace(has_protected_content=False)),
        )
        self.bot.client = self.client
        self.queue = asyncio.Queue()
        self.bot.add_download_task = downloader.add_download_task
        self.bot.download_chat_task = AsyncMock()
        self.workers = []
        real_sleep = asyncio.sleep

        async def short_sleep(delay, *args, **kwargs):
            await real_sleep(min(delay, 0.001), *args, **kwargs)

        patches = [
            patch.object(bot, "_bot", self.bot),
            patch.object(store, "_TASKS_FILE", str(Path(self.temp.name) / "tasks.json")),
            patch.object(store, "_COUNTER_FILE", str(Path(self.temp.name) / "counter.json")),
            patch.object(download_stat, "_download_result", {}),
            patch.object(download_stat, "add_failed_download", Mock()),
            patch.object(bot, "report_bot_status", AsyncMock()),
            patch.object(bot, "is_flood_wait_active", return_value=False),
            patch.object(downloader, "queue", self.queue),
            patch.object(downloader, "app", self.app),
            patch.object(downloader, "_live_workers", 0),
            patch.object(downloader, "_worker_exit_signal", False),
            patch.object(bot.asyncio, "sleep", short_sleep),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    async def asyncTearDown(self):
        self.bot.is_running = False
        self.app.is_running = False
        tasks = [self.bot.reply_task, getattr(self.bot, "_pending_consumer_task", None),
                 *getattr(self.bot, "_range_tasks", {}).values(), *self.workers]
        tasks = [task for task in tasks if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def message(mid=101, cid=-10001):
        return SimpleNamespace(id=mid, empty=False, media=True,
                               chat=SimpleNamespace(id=cid, title="test"))

    def save(self, tid=1, mid=101, state="pending", kind="direct", extra=None):
        data = {"task_id_display": f"1014-{tid}"}
        if mid is not None:
            data["message_id"] = mid
        data.update(extra or {})
        store.save_task(tid, -10001, "", 10, 100, 90, "media_type == video", "",
                        task_type=kind, extra_data=data)
        store.update_download_state(tid, state)
        return store.get_running_tasks()[-1]

    async def until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait(), timeout=1)

    async def start(self, gateway=None):
        gateway = gateway or SimpleNamespace(
            start=AsyncMock(), stop=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(id=200)),
            set_bot_commands=AsyncMock(), add_handler=Mock(), send_message=AsyncMock(),
        )
        with patch.object(bot.pyrogram, "Client", return_value=gateway), \
                patch.object(self.bot, "_register_bot_handlers"), \
                patch.object(bot, "send_help_str", AsyncMock()):
            await self.bot.start(self.app, self.client, self.bot.add_download_task,
                                 self.bot.download_chat_task)
        return gateway

    async def test_empty_start_consumes_a_later_task(self):
        inspected = asyncio.Event()

        def inspect_store():
            tasks = store.get_running_tasks()
            self.assertFalse(tasks)
            inspected.set()
            return tasks

        with patch.object(bot, "get_running_tasks", side_effect=inspect_store):
            await self.start()
            await asyncio.wait_for(inspected.wait(), timeout=1)
        self.save()
        await self.until(lambda: not self.queue.empty())
        _, node = self.queue.get_nowait()
        self.assertEqual(node.task_id, 1)
        self.assertEqual(store.get_running_tasks()[0]["download_state"], "downloading")

    async def test_restart_normalizes_tasks_and_reserves_ids_before_client_start(self):
        self.save(tid=41, state="downloading")

        async def on_start():
            self.assertEqual(self.bot.gen_task_id(), 42)
            self.assertEqual(store.get_running_tasks()[0]["download_state"], "pending")

        gateway = SimpleNamespace(
            start=AsyncMock(side_effect=on_start), stop=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(id=200)),
            set_bot_commands=AsyncMock(), add_handler=Mock(), send_message=AsyncMock(),
        )
        await self.start(gateway)
        await self.until(lambda: not self.queue.empty())
        _, node = self.queue.get_nowait()
        self.assertEqual(node.task_id, 41)
        self.assertEqual(node.task_id_display, "1014-41")

    async def test_consumer_is_singleton_and_can_restart_after_cancellation(self):
        first = self.bot._ensure_pending_consumer()
        self.assertIs(self.bot._ensure_pending_consumer(), first)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        second = self.bot._ensure_pending_consumer()
        self.assertIsNot(second, first)
        self.assertIs(self.bot._ensure_pending_consumer(), second)

    async def test_shutdown_awaits_consumer_and_reply_task(self):
        await self.start()
        consumer = self.bot._pending_consumer_task
        with patch.object(self.bot, "update_config"):
            await bot.stop_download_bot()
        self.assertTrue(consumer.done())
        self.assertTrue(self.bot.reply_task.done())
        self.assertIsNone(self.bot._pending_consumer_task)

    async def test_overlap_does_not_fetch_or_queue_the_same_task_twice(self):
        self.save()
        entered, release = asyncio.Event(), asyncio.Event()

        async def get_message(*args):
            entered.set()
            await release.wait()
            return self.message()

        self.client.get_messages.side_effect = get_message
        first = asyncio.create_task(bot._consume_one_pending())
        await entered.wait()
        await bot._consume_one_pending()
        release.set()
        await first
        self.assertEqual(self.client.get_messages.await_count, 1)
        self.assertEqual(self.queue.qsize(), 1)
        self.assertFalse(self.bot._consuming)

    async def test_overlap_during_notification_reserves_one_slot(self):
        self.save()
        tasks = store.get_running_tasks()
        tasks[0]["from_user_id"] = 100
        store._save_all(tasks)
        entered, release = asyncio.Event(), asyncio.Event()

        async def notify(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(id=5)

        self.bot.bot = SimpleNamespace(send_message=AsyncMock(side_effect=notify))
        first = asyncio.create_task(bot._consume_one_pending())
        await entered.wait()
        await bot._consume_one_pending()
        release.set()
        await first
        self.assertEqual(self.queue.qsize(), 1)
        self.assertFalse(self.bot._consuming)

    async def test_failed_queue_handoff_keeps_task_retryable(self):
        for result in (False, RuntimeError("queue unavailable")):
            with self.subTest(result=result):
                self.save()
                rejected = AsyncMock(return_value=result)
                if isinstance(result, Exception):
                    rejected.side_effect = result
                self.bot.add_download_task = rejected
                await bot._consume_one_pending()
                self.assertEqual(store.get_running_tasks()[0]["download_state"], "pending")
                self.assertFalse(self.bot._in_queue)
                self.assertFalse(self.bot._consuming)
                self.bot.add_download_task = downloader.add_download_task
                await bot._consume_one_pending()
                self.assertEqual(self.queue.qsize(), 1)
                self.queue.get_nowait()
                self.bot._in_queue.clear()
                store.complete_task(1)

    async def test_cancellation_during_handoff_releases_reservations(self):
        self.save()
        entered = asyncio.Event()

        async def enqueue(*args):
            entered.set()
            await asyncio.Event().wait()

        self.bot.add_download_task = enqueue
        task = asyncio.create_task(bot._consume_one_pending())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.bot._in_queue)
        self.assertFalse(self.bot._consuming)
        self.assertEqual(store.get_running_tasks()[0]["download_state"], "pending")

    async def test_imported_worker_active_count_blocks_consumer(self):
        self.save()
        self.bot._active_downloads = self.app.max_download_task
        await bot._consume_one_pending()
        self.client.get_messages.assert_not_awaited()
        self.assertTrue(self.queue.empty())
        self.bot._active_downloads = 0
        await bot._consume_one_pending()
        self.assertEqual(self.queue.qsize(), 1)

    async def test_simultaneous_consumers_respect_limit_for_distinct_tasks(self):
        for tid in (1, 2, 3):
            self.save(tid=tid, mid=100 + tid)
        release = asyncio.Event()

        async def fetch(cid, mid):
            await release.wait()
            return self.message(mid, cid)

        self.client.get_messages.side_effect = fetch
        consumers = [asyncio.create_task(bot._consume_one_pending()) for _ in range(3)]
        self.workers.extend(consumers)
        await self.until(lambda: self.client.get_messages.await_count == 2)
        release.set()
        await asyncio.gather(*consumers)
        self.assertEqual(self.queue.qsize(), 2)
        self.assertEqual([t["task_id"] for t in store.get_pending_tasks()], [3])

    async def test_legacy_states_recover_into_the_same_queue(self):
        self.save(tid=7, state="queued")
        self.save(tid=8, mid=102)
        tasks = store.get_running_tasks()
        tasks[1].pop("download_state")
        store._save_all(tasks)
        await self.bot.recover_tasks()
        self.assertEqual(self.bot.task_id, 8)
        self.assertTrue(all(t["download_state"] == "pending" for t in store.get_running_tasks()))
        await bot._consume_one_pending()
        await bot._consume_one_pending()
        self.assertEqual(self.queue.qsize(), 2)

    async def test_batch_recovery_rescans_lost_queue_and_allows_next_pending(self):
        self.save(mid=None, kind="download")
        store.update_task_progress(1, 60)
        self.save(tid=2, mid=102)
        entered, release = asyncio.Event(), asyncio.Event()

        async def scan(client, config, node):
            self.assertEqual(config.last_read_message_id, 10)
            self.assertEqual(config.download_filter, "media_type == video")
            self.assertEqual(node.end_offset_id, 100)
            self.assertEqual(node.task_id_display, "1014-1")
            entered.set()
            await release.wait()

        self.bot.download_chat_task.side_effect = scan
        await bot._consume_one_pending()
        recovery = self.bot._range_tasks[1]
        await entered.wait()
        await bot._consume_one_pending()
        self.assertEqual(self.queue.qsize(), 1)
        release.set()
        await recovery
        self.assertEqual([t["task_id"] for t in store.get_running_tasks()], [2])
        self.assertFalse(self.bot._consuming)

    async def test_failed_batch_scan_stays_pending(self):
        self.save(mid=None, kind="download")
        self.bot.download_chat_task.side_effect = ConnectionError("offline")
        await bot._consume_one_pending()
        await self.bot._range_tasks[1]
        self.assertEqual(store.get_running_tasks()[0]["download_state"], "pending")
        self.assertFalse(self.bot._consuming)

    async def test_failed_batch_drains_queued_files_before_retry(self):
        self.save(mid=None, kind="download")

        async def scan(client, config, node):
            await downloader.add_download_task(self.message(), node)
            # A worker retry can set this while the scan is still running.
            node.is_running = True
            raise ConnectionError("scan interrupted after queueing a file")

        self.bot.download_chat_task.side_effect = scan
        await bot._consume_one_pending()
        recovery = self.bot._range_tasks[1]
        await self.until(lambda: not self.queue.empty())
        _, node = self.queue.get_nowait()
        self.assertEqual(store.get_running_tasks()[0]["download_state"], "downloading")
        node.total_download_task = 1
        self.assertFalse(node.is_finish())
        await recovery
        self.assertEqual(store.get_running_tasks()[0]["download_state"], "pending")
        self.assertFalse(self.bot._consuming)

    async def test_forward_recovery_preserves_destination_topic_and_checkpoint(self):
        self.save(mid=None, kind="forward", extra={"dst_chat_id": -10002, "topic_id": 9})
        store.update_task_progress(1, 60)

        async def history(*args, **kwargs):
            self.assertEqual(kwargs["offset_id"], 60)
            yield self.message(61)

        with patch.object(bot, "get_chat_history_v2", history), \
                patch.object(bot, "forward_normal_content", AsyncMock()) as forward:
            await bot._consume_one_pending()
            await self.bot._range_tasks[1]
        node = forward.await_args.args[1]
        self.assertEqual(node.upload_telegram_chat_id, -10002)
        self.assertEqual(node.topic_id, 9)
        self.assertIs(node.upload_user, self.client)
        self.assertFalse(store.get_running_tasks())

    async def test_protected_forward_recovery_uses_download_queue(self):
        self.save(mid=None, kind="forward", extra={"dst_chat_id": -10002})
        self.client.get_chat.return_value.has_protected_content = True
        await bot._consume_one_pending()
        await self.bot._range_tasks[1]
        self.bot.download_chat_task.assert_awaited_once()
        node = self.bot.download_chat_task.await_args.args[2]
        self.assertTrue(node.has_protected_content)
        self.assertEqual(node.upload_telegram_chat_id, -10002)
        self.assertFalse(store.get_running_tasks())

    async def test_worker_finishes_and_releases_shared_slot(self):
        self.save()
        await bot._consume_one_pending()
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(client, message, node):
            entered.set()
            await release.wait()
            node.total_download_task += 1

        with patch.object(downloader, "download_task", download):
            task = asyncio.create_task(downloader.worker(self.client))
            self.workers.append(task)
            await entered.wait()
            self.assertEqual(self.bot._active_downloads, 1)
            self.assertFalse(self.bot._in_queue)
            release.set()
            await self.until(lambda: self.bot._active_downloads == 0)
            self.assertEqual(downloader._live_workers, 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.bot._active_downloads, 0)
        self.assertEqual(downloader._live_workers, 0)

    async def test_idle_worker_cancellation_and_sentinel_do_not_decrement_active(self):
        task = asyncio.create_task(downloader.worker(self.client))
        self.workers.append(task)
        await self.until(lambda: downloader._live_workers == 1)
        await self.queue.put(None)
        await self.until(self.queue.empty)
        self.assertEqual(self.bot._active_downloads, 0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.bot._active_downloads, 0)
        self.assertEqual(downloader._live_workers, 0)

    async def test_worker_network_retry_reserves_queue_without_leaking_active(self):
        self.save()
        await bot._consume_one_pending()
        original_put = self.queue.put
        attempts = 0

        async def tracked_put(item):
            self.assertIn(item[1].task_id, self.bot._in_queue)
            await original_put(item)

        async def download(client, message, node):
            nonlocal attempts
            attempts += 1
            self.assertEqual(self.bot._active_downloads, 1)
            if attempts == 1:
                raise ConnectionError("connection lost")
            node.total_download_task = node.total_task

        with patch.object(self.queue, "put", tracked_put), \
                patch.object(downloader, "download_task", download), \
                patch.object(downloader, "_maybe_reconnect_client", AsyncMock(return_value=True)), \
                patch.object(downloader, "_reset_task_for_retry", AsyncMock()):
            task = asyncio.create_task(downloader.worker(self.client))
            self.workers.append(task)
            await self.until(lambda: attempts == 2 and self.bot._active_downloads == 0)
            self.assertFalse(self.bot._in_queue)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.bot._active_downloads, 0)

    async def test_scale_down_counts_each_worker_exit_once(self):
        self.app.max_download_task = 1
        for _ in range(2):
            self.workers.append(asyncio.create_task(downloader.worker(self.client)))
        await self.until(lambda: downloader._live_workers == 2)
        with patch.object(downloader, "_worker_exit_signal", True):
            await self.queue.put(None)
            await self.until(lambda: downloader._live_workers == 1)
        self.assertEqual(self.bot._active_downloads, 0)


if __name__ == "__main__":
    unittest.main()
