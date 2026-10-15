"""MCP submission uses real persistence and the existing consumer, with offline Telegram RPCs."""

import asyncio
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx2
import pyrogram
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import media_downloader as downloader
from module import bot, download_stat, task_store, web
from module.download_service import DownloadService
from module.mcp_server import create_server


class MCPSubmitDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.app = SimpleNamespace(
            loop=asyncio.get_running_loop(), max_download_task=2,
            web_login_secret="browser-secret", mcp_token="agent-secret", mcp_enabled=True,
            mcp_read_only=False, mcp_allowed_hosts=[],
            bot_token="", restart_program=False, chat_download_config={},
        )
        self.queue = asyncio.Queue()
        self.chat = SimpleNamespace(id=-100123456, title="Offline channel")

        async def get_chat(cid):
            self.assertIs(asyncio.get_running_loop(), self.app.loop)
            return self.chat

        self.telegram = SimpleNamespace(
            get_chat=AsyncMock(side_effect=get_chat),
            get_messages=AsyncMock(side_effect=lambda cid, mid: SimpleNamespace(
                id=mid, empty=False, media=True, chat=self.chat, _client=self.telegram)),
        )
        self.bots = []
        self.bot = self.new_bot()
        for item in (
            patch.object(bot, "_bot", self.bot),
            patch.object(web, "_app", self.app),
            patch.object(task_store, "_TASKS_FILE", str(self.path / "tasks.json")),
            patch.object(task_store, "_COUNTER_FILE", str(self.path / "counter.json")),
            patch.object(download_stat, "_HISTORY_FILE", str(self.path / "history.json")),
            patch.object(download_stat, "_download_result", {}),
            patch.object(downloader, "queue", self.queue),
            patch.object(bot, "is_flood_wait_active", return_value=False),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.service = DownloadService(self.app)
        self.server = create_server(self.service)

    def new_bot(self):
        instance = bot.DownloadBot()
        instance.app = self.app
        instance.client = self.telegram
        instance.add_download_task = downloader.add_download_task
        instance.bot = SimpleNamespace(send_message=AsyncMock())
        self.bots.append(instance)
        return instance

    async def asyncTearDown(self):
        tasks = []
        for instance in self.bots:
            instance.is_running = False
            for task in (instance._pending_consumer_task, instance.reply_task):
                if task is not None:
                    task.cancel()
                    tasks.append(task)
        await asyncio.gather(*tasks, return_exceptions=True)

    def success(self, result):
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    async def submit(self, client, url="https://t.me/channel_name/123"):
        return await client.call_tool("submit_download", {"url": url})

    async def test_submission_is_durable_before_fetch_and_visible_in_pending_list(self):
        async with Client(self.server) as client:
            result = self.success(await self.submit(client))
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["task_id"], "-100123456_123")
            self.assertFalse(result["duplicate"])
            stored = task_store.get_pending_tasks()[0]
            self.assertEqual(str(stored["task_id"]), result["pending_task_id"])
            self.assertEqual(stored["extra_data"]["task_id_display"], result["task_id_display"])
            self.assertEqual(stored["extra_data"]["message_client"], "user")
            self.assertEqual(stored["from_user_id"], "")
            self.assertEqual(stored["url"], "https://t.me/channel_name/123")
            self.telegram.get_messages.assert_not_awaited()
            self.assertTrue(self.queue.empty())
            self.assertFalse(self.bot.task_node)
            pending = self.success(await client.call_tool("list_downloads", {"status": "pending"}))
            self.assertEqual(pending["tasks"][0]["task_id"], result["pending_task_id"])
            self.assertEqual(pending["tasks"][0]["chat_title"], "Offline channel")

    async def test_public_private_and_topic_links_use_same_consumer(self):
        async with Client(self.server) as client:
            result = self.success(await self.submit(client, "https://t.me/c/123456/77/123"))
            self.telegram.get_chat.assert_awaited_once_with(-100123456)
            self.assertEqual(task_store.get_pending_tasks()[0]["extra_data"]["topic_id"], 77)
            await bot._consume_one_pending()
            message, node = self.queue.get_nowait()
            self.assertEqual(message.id, 123)
            self.assertEqual(node.task_id_display, result["task_id_display"])
            self.assertEqual(node.topic_id, 77)
            self.bot.bot.send_message.assert_not_awaited()
            active = self.success(await client.call_tool("list_downloads"))
            self.assertEqual(active["tasks"][0]["task_id"], result["task_id"])

    async def test_public_topic_and_preview_links_are_canonicalized(self):
        async with Client(self.server) as client:
            topic = self.success(await self.submit(client, "https://t.me/channel_name/77/123"))
            self.assertEqual(task_store.get_pending_tasks()[0]["extra_data"]["topic_id"], 77)
            preview = self.success(await self.submit(client, "https://t.me/s/channel_name/124/"))
            self.assertEqual(preview["url"], "https://t.me/channel_name/124")
            self.assertNotEqual(topic["pending_task_id"], preview["pending_task_id"])

    async def test_concurrent_duplicate_links_reuse_task_without_starting_extra_consumers(self):
        async with Client(self.server) as client:
            replies = await asyncio.gather(self.submit(client), self.submit(client),
                                           self.submit(client, "https://t.me/c/123456/123"))
            results = [self.success(reply) for reply in replies]
            self.assertEqual(len({r["pending_task_id"] for r in results}), 1)
            self.assertEqual(sum(not r["duplicate"] for r in results), 1)
            self.assertEqual(len(task_store.get_pending_tasks()), 1)
            consumer = self.bot._pending_consumer_task
            await bot._consume_one_pending()
            duplicate = self.success(await self.submit(client))
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["status"], "downloading")
            self.assertIs(consumer, self.bot._pending_consumer_task)
            self.assertEqual(self.queue.qsize(), 1)

    async def test_restart_keeps_ids_user_account_and_shared_concurrency_budget(self):
        self.app.max_download_task = 1
        async with Client(self.server) as client:
            first = self.success(await self.submit(client))
            second = self.success(await self.submit(client, "https://t.me/channel_name/124"))
        fresh = self.new_bot()
        with patch.object(bot, "_bot", fresh):
            await fresh.recover_tasks()
            await bot._consume_one_pending()
            await bot._consume_one_pending()
            self.assertEqual(self.queue.qsize(), 1)
            self.telegram.get_messages.assert_awaited_once_with(self.chat.id, 123)
            message, node = self.queue.get_nowait()
            self.assertEqual(str(node.task_id), first["pending_task_id"])
            self.assertEqual(node.task_id_display, first["task_id_display"])
            self.assertEqual(message.id, 123)
            self.assertIsNone(node.client)  # Existing user client is selected by the worker.
            self.assertEqual(str(task_store.get_pending_tasks()[0]["task_id"]), second["pending_task_id"])
            self.assertEqual(fresh.task_id, int(second["pending_task_id"]))
            fresh.bot.send_message.assert_not_awaited()

    async def test_pending_submission_can_be_cancelled_before_fetch(self):
        async with Client(self.server) as client:
            result = self.success(await self.submit(client))
            self.success(await client.call_tool("remove_pending_task", {"task_id": result["pending_task_id"]}))
            await bot._consume_one_pending()
            self.assertFalse(task_store.get_pending_tasks())
            self.telegram.get_messages.assert_not_awaited()
            self.assertTrue(self.queue.empty())

    async def test_invalid_links_and_unsafe_types_never_reach_telegram(self):
        urls = ["", 123, "http://t.me/channel/1", "https://example.com/channel/1",
                "https://t.me.evil/channel/1", "https://user:pass@t.me/channel/1",
                "https://t.me/channel", "https://t.me/+invite", "https://t.me/channel/0",
                "https://t.me/channel/-1", "https://t.me/channel/2147483648",
                "https://t.me/c/not-a-number/1", "https://t.me/channel/1?comment=2",
                "https://t.me/channel/1#fragment", "https://t.me/channel/1 other", "x" * 2049]
        async with Client(self.server) as client:
            for url in urls:
                with self.subTest(url=url):
                    self.assertTrue((await self.submit(client, url)).is_error)
        self.telegram.get_chat.assert_not_awaited()
        self.assertFalse(task_store.get_pending_tasks())

    async def test_not_ready_lookup_error_timeout_and_cooldown_are_tool_errors(self):
        async with Client(self.server) as client:
            self.bot.client = None
            self.assertTrue((await self.submit(client)).is_error)
            self.bot.client = self.telegram
            for error in (ValueError("private chat"), asyncio.TimeoutError()):
                self.telegram.get_chat.side_effect = error
                self.assertTrue((await self.submit(client)).is_error)
            with patch.object(bot, "is_flood_wait_active", return_value=True):
                self.telegram.get_chat.reset_mock()
                self.assertTrue((await self.submit(client)).is_error)
                self.telegram.get_chat.assert_not_awaited()
        self.assertFalse(task_store.get_pending_tasks())

    async def test_flood_wait_updates_shared_cooldown_without_persisting(self):
        from module.pyrogram_extension import _unified_flood_wait
        self.telegram.get_chat.side_effect = pyrogram.errors.FloodWait(12)
        with patch.dict(_unified_flood_wait, until=0, reason=""):
            async with Client(self.server) as client:
                result = await self.submit(client)
                self.assertTrue(result.is_error)
                self.assertGreaterEqual(_unified_flood_wait["until"], time.time() + 11)
                self.assertIn("submit_download", _unified_flood_wait["reason"])
        self.assertFalse(task_store.get_pending_tasks())

    async def test_persistence_failure_is_not_reported_as_acceptance(self):
        blocked = self.path / "blocked"
        blocked.mkdir()
        with patch.object(task_store, "_TASKS_FILE", str(blocked)):
            async with Client(self.server) as client:
                result = await self.submit(client)
                self.assertTrue(result.is_error)
                self.assertIn("not accepted", result.content[0].text)
        self.assertIsNone(self.bot._pending_consumer_task)
        self.assertFalse(self.bot.task_node)
        self.assertTrue(self.queue.empty())

    async def test_read_only_omits_submission_and_rejects_calls(self):
        async with Client(create_server(self.service, read_only=True)) as client:
            self.assertNotIn("submit_download", {t.name for t in (await client.list_tools()).tools})
            self.assertTrue((await self.submit(client)).is_error)
        self.telegram.get_chat.assert_not_awaited()
        self.assertFalse(task_store.get_pending_tasks())

    async def test_http_submission_authenticates_and_runs_on_downloader_loop(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        runtime = uvicorn.Server(uvicorn.Config(web.create_web_app(self.app), log_level="critical"))
        thread = threading.Thread(target=lambda: runtime.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(300):
                if runtime.started:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(runtime.started)
            async with httpx2.AsyncClient(trust_env=False) as http:
                self.assertEqual((await http.post(url, json={})).status_code, 401)
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + self.app.mcp_token},
                                          trust_env=False) as http:
                async with Client(streamable_http_client(url, http_client=http)) as client:
                    tools = {t.name: t for t in (await client.list_tools()).tools}
                    self.assertTrue(tools["submit_download"].annotations.open_world_hint)
                    result = self.success(await self.submit(client))
                    pending = self.success(await client.call_tool("list_downloads", {"status": "pending"}))
                    self.assertEqual(pending["tasks"][0]["task_id"], result["pending_task_id"])
            self.telegram.get_messages.assert_not_awaited()
        finally:
            runtime.should_exit = True
            await asyncio.to_thread(thread.join, 5)
            sock.close()

    async def test_mcp_backend_without_bot_session_recovers_consumes_and_stops_cleanly(self):
        self.bot.config_path = str(self.path / "bot.yaml")
        with patch.object(bot.pyrogram, "Client", side_effect=AssertionError("No extra Telegram session")):
            await bot.start_download_backend(self.app, self.telegram, downloader.add_download_task, AsyncMock())
            self.assertIsNone(self.bot.bot)
            async with Client(self.server) as client:
                result = self.success(await self.submit(client))
            await bot._consume_one_pending()
            self.assertEqual(self.queue.qsize(), 1)
            consumer, reply = self.bot._pending_consumer_task, self.bot.reply_task
            await bot.start_download_backend(self.app, self.telegram, downloader.add_download_task, AsyncMock())
            self.assertIs(consumer, self.bot._pending_consumer_task)
            self.assertIs(reply, self.bot.reply_task)
            self.assertEqual(str(task_store.get_downloading_tasks()[0]["task_id"]), result["pending_task_id"])
            await bot.stop_download_bot()
            self.assertTrue(consumer.done())
            self.assertTrue(reply.done())
            self.assertFalse((self.path / "bot.yaml").exists())
            self.assertTrue(task_store.get_downloading_tasks())  # Shutdown preserves recovery data.

    async def test_mcp_mode_stays_alive_after_configured_downloads_finish(self):
        with patch.object(downloader, "app", self.app):
            running = asyncio.create_task(downloader.run_until_all_task_finish())
            try:
                await asyncio.sleep(.01)
                self.assertFalse(running.done())
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            self.app.mcp_enabled = False
            await asyncio.wait_for(downloader.run_until_all_task_finish(), timeout=.1)


if __name__ == "__main__":
    unittest.main()
