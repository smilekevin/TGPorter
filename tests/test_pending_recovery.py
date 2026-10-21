"""Offline regression tests using real persistence, consumer and worker code."""

import asyncio
from collections import Counter
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import media_downloader as downloader
from module import bot, download_stat, task_store as store
from module import web
from module.app import TaskNode
from module.language import set_language
from module.download_service import DownloadService


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
            download_filters=[],
        )
        from module.app import Application
        self.app.resolve_download_filter = lambda expression=None: Application.resolve_download_filter(self.app, expression)
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
            patch.object(download_stat, "_chat_titles", {}),
            patch.object(download_stat, "add_failed_download", Mock()),
            patch.object(bot, "report_bot_status", AsyncMock()),
            patch.object(bot, "is_flood_wait_active", return_value=False),
            patch.object(downloader, "queue", self.queue),
            patch.object(downloader, "app", self.app),
            patch.object(downloader, "_live_workers", 0),
            patch.object(bot.asyncio, "sleep", short_sleep),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        set_language("en")
        self.addCleanup(set_language, "en")

    async def asyncTearDown(self):
        self.bot.is_running = False
        self.app.is_running = False
        tasks = [self.bot.reply_task, self.bot.heartbeat_task,
                 getattr(self.bot, "_pending_consumer_task", None),
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
                patch.object(self.bot, "_ensure_heartbeat"), \
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

    async def test_preparation_handoff_is_visible_and_rejection_returns_to_queue(self):
        self.save()
        self.app.web_login_secret = ''
        with patch.object(web, '_app', self.app):
            client = web.get_flask_app().test_client()
            self.assertEqual(client.get('/get_pending_list').json['tasks'][0]['queue_label'], 'Queued')
            entered, release = asyncio.Event(), asyncio.Event()

            async def fetch(*args):
                entered.set()
                await release.wait()
                return self.message()

            self.client.get_messages.side_effect = fetch
            self.bot.add_download_task = AsyncMock(return_value=False)
            task = asyncio.create_task(bot._consume_one_pending())
            await entered.wait()
            try:
                self.assertEqual(client.get('/get_pending_list').json['total'], 0)
                row = client.get('/get_download_list?already_down=false').json[0]
                self.assertEqual((row['status'], row['phase'], row['total_size_bytes']),
                                 ('active', 'preparing', 0))
                self.assertEqual(row['wait_reason'], 'Fetching message')
                self.assertEqual(row['task_id_display'], '1014-1')
            finally:
                release.set()
                await task
            self.assertEqual(client.get('/get_download_list?already_down=false').json, [])
            self.assertEqual(client.get('/get_pending_list').json['total'], 1)
            self.bot.add_download_task = downloader.add_download_task
            await bot._consume_one_pending()
            self.assertEqual(client.get('/get_pending_list').json['total'], 0)
            self.assertEqual(client.get('/get_download_list?already_down=false').json[0]['phase'], 'preparing')

    async def test_deletion_during_preparation_does_not_enqueue_or_reappear(self):
        self.save()
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(*args):
            entered.set()
            await release.wait()
            return self.message()

        self.client.get_messages.side_effect = fetch
        with patch.object(download_stat, '_cancelled_tasks', set()), patch.object(
                download_stat, '_HISTORY_FILE', str(Path(self.temp.name) / 'history.json')):
            task = asyncio.create_task(bot._consume_one_pending())
            await entered.wait()
            try:
                self.assertEqual(DownloadService(self.app).web_delete_task('-10001_101')['code'], '1')
            finally:
                release.set()
                await task
            self.assertTrue(self.queue.empty())
            self.assertFalse(store.get_running_tasks())
            self.assertFalse(download_stat.get_download_result())
            await bot._consume_one_pending()
            self.assertEqual(self.client.get_messages.await_count, 1)

    async def test_stale_pending_cancellation_also_stops_inflight_preparation(self):
        self.save()
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(*args):
            entered.set()
            await release.wait()
            return self.message()

        self.client.get_messages.side_effect = fetch
        with patch.object(download_stat, '_cancelled_tasks', set()), patch.object(
                download_stat, '_HISTORY_FILE', str(Path(self.temp.name) / 'history.json')):
            task = asyncio.create_task(bot._consume_one_pending())
            await entered.wait()
            try:
                self.assertEqual(DownloadService(self.app).web_remove_pending('1')['code'], '1')
            finally:
                release.set()
                await task
            self.assertTrue(self.queue.empty())
            self.assertFalse(store.get_running_tasks())
            self.assertFalse(download_stat.get_download_result())

    async def test_fetch_cancellation_restores_pending_visibility(self):
        self.save()
        entered = asyncio.Event()

        async def fetch(*args):
            entered.set()
            await asyncio.Event().wait()

        self.client.get_messages.side_effect = fetch
        task = asyncio.create_task(bot._consume_one_pending())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(download_stat.get_download_result())
        self.assertEqual(DownloadService(self.app).web_get_pending_list()['total'], 1)
        self.assertFalse(self.bot._consuming)

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

    async def test_duplicate_bot_links_return_original_task_without_orphan_nodes(self):
        self.bot.bot = self.inbox_gateway()
        self.bot.bot.edit_message_text = AsyncMock()
        command = SimpleNamespace(id=2, from_user=SimpleNamespace(id=100))
        media = self.message()
        # Yield during the provisional reply to overlap all handlers.
        async def reply(*args, **kwargs):
            await asyncio.sleep(0)
            return SimpleNamespace(id=5)
        self.bot.bot.send_message.side_effect = reply
        with patch.object(self.bot, "_ensure_pending_consumer") as ensure:
            await asyncio.gather(*(bot.direct_download(self.bot, -10001, command, media)
                                   for _ in range(20)))
        stored = store.get_running_tasks()
        self.assertEqual(len(stored), 1)
        original_id = stored[0]["task_id"]
        original_display = stored[0]["extra_data"]["task_id_display"]
        self.assertEqual(set(self.bot.task_node), {original_id})
        self.assertEqual(set(self.bot._cached_messages), {original_id})
        self.assertEqual(self.bot.task_id, 1)
        ensure.assert_called_once()
        await bot._consume_one_pending()
        self.assertEqual(self.queue.qsize(), 1)
        # Submitting again after handoff must also reuse the downloading task.
        await bot.direct_download(self.bot, -10001, command, media)
        self.assertEqual(len(store.get_running_tasks()), 1)
        self.assertEqual(self.queue.qsize(), 1)
        self.assertEqual(set(self.bot.task_node), {original_id})
        self.assertEqual(self.bot.bot.edit_message_text.await_count, 20)
        for call in self.bot.bot.edit_message_text.await_args_list:
            self.assertEqual(call.args[2], f"Task already exists\nTask: {original_display}")

    async def test_failed_bot_persistence_never_registers_or_queues_a_task(self):
        self.bot.bot = self.inbox_gateway()
        self.bot.bot.edit_message_text = AsyncMock()
        command = SimpleNamespace(id=2, from_user=SimpleNamespace(id=100))
        with patch.object(store, "_save_all", return_value=False), \
                patch.object(self.bot, "_ensure_pending_consumer") as ensure:
            await bot.direct_download(self.bot, -10001, command, self.message())
        self.assertFalse(store.get_running_tasks())
        self.assertFalse(self.bot.task_node)
        self.assertFalse(self.bot._cached_messages)
        self.assertTrue(self.queue.empty())
        ensure.assert_not_called()
        self.assertEqual(self.bot.bot.edit_message_text.await_args.args[2],
                         "Task could not be persisted; submission was not accepted.")

    async def test_pending_lifecycle_reaches_application_log_with_display_id(self):
        self.save()
        path = Path(self.temp.name) / "tdl.log"
        sink = bot.logger.add(path, format="{message}", level="INFO")
        try:
            await bot._consume_one_pending()
        finally:
            bot.logger.remove(sink)
        log = path.read_text()
        self.assertIn("preparing task 1014-1 (id=1", log)
        self.assertIn("queued task 1014-1 (id=1", log)

    async def test_stress_bot_links_to_real_queue_with_duplicates_and_capacity_limits(self):
        # Only Telegram RPCs and media transfer are simulated. The link handler,
        # atomic task store, consumer, queue handoff and worker accounting are real.
        self.bot.bot = self.inbox_gateway()
        self.bot.bot.edit_message_text = AsyncMock()
        self.client.get_chat.return_value = SimpleNamespace(
            id=-10001, title="Stress channel", first_name="", has_protected_content=False)
        async def reply(*args, **kwargs):
            await asyncio.sleep(0)
            return SimpleNamespace(id=5)
        self.bot.bot.send_message.side_effect = reply
        unique_count, repetitions = 200, 5
        for limit in (1, 5, 16):
            with self.subTest(max_download_task=limit):
                self.app.max_download_task = limit
                release = asyncio.Event()
                received, finished = Counter(), set()
                peak = 0
                stop_pump = False

                async def transfer(client, message, node):
                    received[message.id] += 1
                    await release.wait()
                    await asyncio.sleep(0)
                    node.total_download_task = node.total_task
                    store.complete_task(node.task_id)
                    self.bot.task_node.pop(node.task_id, None)
                    finished.add(message.id)

                async def pump():
                    nonlocal peak
                    while not stop_pump:
                        # Overlapping polls exercise the shared reservation guard.
                        await asyncio.gather(*(bot._consume_one_pending() for _ in range(8)))
                        occupied = self.bot._active_downloads + len(self.bot._in_queue | self.bot._consuming)
                        peak = max(peak, occupied)
                        self.assertLessEqual(occupied, limit)
                        if not self.queue.empty():
                            running = sum(not task.done() and task is not asyncio.current_task()
                                          for task in self.workers)
                            for _ in range(max(0, limit - running)):
                                self.workers.append(asyncio.create_task(downloader.worker(self.client)))
                        await asyncio.sleep(0.001)

                async def submit(index):
                    command = SimpleNamespace(id=index + 1, from_user=SimpleNamespace(id=100),
                                              text=f"https://t.me/c/01/{index % unique_count + 1}")
                    await bot.download_from_link(self.bot.bot, command)

                with patch.object(downloader, "download_task", transfer), \
                        patch.object(self.bot, "_ensure_pending_consumer"):
                    pumping = asyncio.create_task(pump())
                    self.workers.append(pumping)
                    try:
                        await asyncio.wait_for(asyncio.gather(
                            *(submit(i) for i in range(unique_count * repetitions))), timeout=30)
                        await self.until(lambda: self.bot._active_downloads == limit)
                        self.assertEqual(len(store.get_running_tasks()), unique_count)
                        self.assertEqual(len(self.bot.task_node), unique_count)
                        self.assertEqual(self.bot.bot.edit_message_text.await_count,
                                         unique_count * (repetitions - 1))
                        release.set()
                        async def drained():
                            while len(finished) != unique_count or self.bot._active_downloads:
                                if pumping.done():
                                    await pumping
                                await asyncio.sleep(0.001)
                        await asyncio.wait_for(drained(), timeout=30)
                    finally:
                        stop_pump = True
                        release.set()
                        await pumping
                        await asyncio.gather(*self.workers)
                self.assertEqual(received, Counter({mid: 1 for mid in range(1, unique_count + 1)}))
                self.assertEqual(peak, limit)
                self.assertFalse(store.get_running_tasks())
                self.assertTrue(self.queue.empty())
                self.assertFalse(self.bot._cached_messages)
                self.assertFalse(self.bot._consuming)
                self.assertFalse(self.bot._in_queue)
                self.assertFalse(self.bot.task_node)
                self.assertEqual(self.bot._active_downloads, 0)
                self.assertEqual(downloader._live_workers, 0)
                self.bot.bot.edit_message_text.reset_mock()
                self.workers.clear()

    def inbox_gateway(self):
        return SimpleNamespace(
            get_messages=AsyncMock(return_value=self.message(61, 100)),
            send_message=AsyncMock(return_value=SimpleNamespace(id=5)),
        )

    async def test_legacy_bot_inbox_recovery_selects_bot_for_fetch_and_download(self):
        self.save(mid=61)
        tasks = store.get_running_tasks()
        tasks[0].update(chat_id=100, from_user_id="100")
        store._save_all(tasks)
        self.bot.bot = self.inbox_gateway()
        await bot._consume_one_pending()
        self.bot.bot.get_messages.assert_awaited_once_with(100, 61)
        self.client.get_messages.assert_not_awaited()
        _, node = self.queue.get_nowait()
        self.assertIs(node.client, self.bot.bot)

    async def test_explicit_user_owner_overrides_legacy_inbox_inference(self):
        self.save(mid=61, extra={"message_client": "user"})
        tasks = store.get_running_tasks()
        tasks[0].update(chat_id=100, from_user_id="100")
        store._save_all(tasks)
        self.bot.bot = self.inbox_gateway()
        await bot._consume_one_pending()
        self.client.get_messages.assert_awaited_once_with(100, 61)
        self.bot.bot.get_messages.assert_not_awaited()
        _, node = self.queue.get_nowait()
        self.assertIsNone(node.client)

    async def test_new_direct_task_persists_message_owner(self):
        self.bot.bot = self.inbox_gateway()
        command = SimpleNamespace(from_user=SimpleNamespace(id=100), id=2)
        media = self.message(61, 100)
        media._client = self.bot.bot
        await bot.direct_download(self.bot, 100, command, media)
        self.assertEqual(store.get_running_tasks()[0]["extra_data"]["message_client"], "bot")
        await bot._consume_one_pending()
        _, node = self.queue.get_nowait()
        self.assertIs(node.client, self.bot.bot)

    async def test_web_retry_restores_bot_inbox_identity_and_caches_message(self):
        self.bot.bot = self.inbox_gateway()
        await web._async_retry_download(100, 61, from_user_id="100")
        self.bot.bot.get_messages.assert_awaited_once_with(100, 61)
        self.client.get_messages.assert_not_awaited()
        task = store.get_running_tasks()[0]
        self.assertEqual(task["extra_data"]["message_client"], "bot")
        self.assertIn(task["task_id"], self.bot._cached_messages)

    async def test_web_retry_source_link_persists_resolved_chat_and_message(self):
        self.bot.bot = self.inbox_gateway()
        with patch("module.pyrogram_extension.parse_link", AsyncMock(return_value=(-10002, 901, 0))):
            await web._async_retry_download(100, 61, from_user_id="100",
                                          source_link="https://t.me/c/2/901")
        task = store.get_running_tasks()[0]
        self.assertEqual(task["chat_id"], -10002)
        self.assertEqual(task["extra_data"]["message_id"], 901)
        self.assertEqual(task["extra_data"]["message_client"], "user")
        self.assertIn(task["task_id"], self.bot._cached_messages)

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
            self.assertEqual(downloader._live_workers, 0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.bot._active_downloads, 0)
        self.assertEqual(downloader._live_workers, 0)

    async def test_idle_worker_exits_without_decrementing_active(self):
        await downloader.worker(self.client)
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
        with patch.object(downloader, "_live_workers", 1):
            await downloader.worker(self.client)
            self.assertEqual(downloader._live_workers, 1)
        self.assertEqual(self.bot._active_downloads, 0)


if __name__ == "__main__":
    unittest.main()
