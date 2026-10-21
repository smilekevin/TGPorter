"""Replay offline Bot messages through the pinned SDK without Telegram access."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from module import bot, task_store
from module.telegram import pyrogram
from pyrogram import raw, types
from pyrogram.storage import UpdateState


class BotOfflineUpdatesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clients = []
        self.client_type = pyrogram.Client
        self.messages = []
        self.queries = []
        self.links = []
        self.processed = asyncio.Event()
        self.requested_pts = []
        self.date = 1792454400
        self.account = types.User(id=999, is_bot=True, username="offline_bot")
        self.owner = SimpleNamespace(get_me=AsyncMock(return_value=self.account))
        self.app = SimpleNamespace(
            application_name="offline", api_id=1, api_hash="offline", bot_token="999:offline",
            session_file_path=self.temp.name, proxy=None, loop=asyncio.get_running_loop(),
            allowed_user_ids=[123], chat_download_config={}, resolve_download_filter=lambda: "",
        )

        async def query(client, message):
            self.queries.append(message.id)
            self.processed.set()

        async def link(client, message):
            self.links.append(message.id)

        for item in (
            patch.object(task_store, "_TASKS_FILE", str(Path(self.temp.name) / "tasks.json")),
            patch.object(task_store, "_COUNTER_FILE", str(Path(self.temp.name) / "counter.json")),
            patch.object(bot, "query_tasks", query),
            patch.object(bot, "download_from_link", link),
            patch.object(bot.pyrogram, "Client", side_effect=self.make_client),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.new_backend()
        self.addAsyncCleanup(self.close_clients)
        # Simulate an existing on-disk session from before the service stopped.
        saved = self.client_type("offline_bot", api_id=1, api_hash="offline", workdir=self.temp.name)
        self.addCleanup(saved.executor.shutdown)
        await saved.storage.open()
        await saved.storage.set_update_state(UpdateState(0, 10, 0, self.date, 1))
        await saved.storage.save()
        await saved.storage.close()

    def new_backend(self):
        self.backend = bot.DownloadBot()
        self.backend.config_path = str(Path(self.temp.name) / "bot.yaml")
        self.backend._ensure_pending_consumer = Mock()
        self.backend._ensure_heartbeat = Mock()
        self.backend.update_reply_message = AsyncMock()
        item = patch.object(bot, "_bot", self.backend)
        item.start()
        self.addCleanup(item.stop)

    def make_client(self, *args, **kwargs):
        # Keep start, recovery, SQLite storage, parsing and dispatch real.
        client = self.client_type(*args, **kwargs, workers=1)
        self.clients.append(client)
        self.addCleanup(client.executor.shutdown)

        async def connect():
            await client.storage.open()
            await client.storage.user_id(999)
            client.is_connected = True
            return True

        async def disconnect():
            await client.storage.save()
            await client.storage.close()
            client.is_connected = False

        async def invoke(request, **kwargs):
            if isinstance(request, raw.functions.updates.GetState):
                return self.state(100)
            if isinstance(request, raw.functions.updates.GetDifference):
                self.requested_pts.append(request.pts)
                messages, self.messages = self.messages, []
                return raw.types.updates.Difference(
                    new_messages=messages, new_encrypted_messages=[], other_updates=[], chats=[],
                    users=[raw.types.User(id=123, first_name="Operator"),
                           raw.types.User(id=456, first_name="Unauthorized"),
                           raw.types.User(id=999, first_name="Bot", bot=True)],
                    state=self.state(request.pts + 1),
                )
            raise AssertionError(f"Unexpected network request: {type(request).__name__}")

        client.connect = AsyncMock(side_effect=connect)
        client.disconnect = AsyncMock(side_effect=disconnect)
        client.invoke = AsyncMock(side_effect=invoke)
        client.get_me = AsyncMock(return_value=self.account)
        client.set_bot_commands = AsyncMock()
        client.send_message = AsyncMock(return_value=types.Message(id=200))
        client.edit_message_text = AsyncMock()
        return client

    def state(self, pts):
        return raw.types.updates.State(pts=pts, qts=0, date=self.date, seq=1, unread_count=0)

    def message(self, message_id, sender=123, text="", media=False):
        attachment = None
        if media:
            attachment = raw.types.MessageMediaDocument(document=raw.types.Document(
                id=message_id, access_hash=1, file_reference=b"offline", date=self.date,
                mime_type="application/octet-stream", size=1024, dc_id=2,
                attributes=[raw.types.DocumentAttributeFilename(file_name="offline.bin")],
            ))
        return raw.types.Message(
            id=message_id, peer_id=raw.types.PeerUser(user_id=999),
            from_id=raw.types.PeerUser(user_id=sender), date=self.date,
            message=text, media=attachment, restriction_reason=[],
        )

    async def start_backend(self):
        await self.backend.start(self.app, self.owner, AsyncMock(), AsyncMock())
        await self.backend.reply_task
        await asyncio.wait_for(self.processed.wait(), 2)

    async def close_clients(self):
        for client in reversed(self.clients):
            if client.is_initialized:
                await client.stop()
            elif client.is_connected:
                await client.disconnect()

    async def test_startup_replays_media_links_and_commands_with_authorization_ready(self):
        self.messages = [
            self.message(2, media=True), self.message(3, sender=456, media=True),
            self.message(4, text="https://t.me/offline_source/61"),
            self.message(5, sender=456, text="https://t.me/offline_source/62"),
            self.message(6, sender=456, text="/query"), self.message(7, text="/query"),
        ]
        await self.start_backend()
        self.assertFalse(self.backend.bot.skip_updates)
        self.assertEqual(self.requested_pts, [10])
        self.assertEqual(self.links, [4])
        self.assertEqual(self.queries, [7])
        tasks = task_store.get_pending_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["chat_id"], 123)
        self.assertEqual(tasks[0]["extra_data"]["message_id"], 2)
        self.assertEqual(tasks[0]["extra_data"]["message_client"], "bot")
        self.backend.add_download_task.assert_not_awaited()
        self.assertIn(tasks[0]["task_id"], self.backend._cached_messages)
        # Startup and the accepted replayed media both ensure the singleton.
        self.assertEqual(self.backend._ensure_pending_consumer.call_count, 2)

    async def test_process_restart_keeps_session_cursor_and_recovers_task_ids_before_replay(self):
        self.messages = [self.message(2, media=True), self.message(3, text="/query")]
        await self.start_backend()
        first_task = task_store.get_pending_tasks()[0]
        await self.backend.bot.stop()
        self.new_backend()
        self.processed.clear()
        self.messages = [self.message(4, media=True), self.message(5, text="/query")]
        await self.start_backend()
        self.assertEqual(self.requested_pts, [10, 11])
        tasks = task_store.get_pending_tasks()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["task_id"], first_task["task_id"])
        self.assertGreater(tasks[1]["task_id"], first_task["task_id"])
        self.assertEqual([t["extra_data"]["message_id"] for t in tasks], [2, 4])
        self.assertEqual(self.queries, [3, 5])

    async def test_reconnect_reinstalls_handlers_before_replay_without_duplicates(self):
        self.messages = [self.message(2, text="/query")]
        await self.start_backend()
        handler_count = sum(len(group) for group in self.backend.bot.dispatcher.groups.values())
        for message_id in (3, 5):
            self.processed.clear()
            self.messages = [self.message(message_id, media=True),
                             self.message(message_id + 1, text="/query")]
            self.assertTrue(await self.backend._reconnect_bot())
            await asyncio.wait_for(self.processed.wait(), 2)
            self.assertEqual(sum(len(group) for group in self.backend.bot.dispatcher.groups.values()),
                             handler_count)
        self.assertEqual(self.requested_pts, [10, 11, 12])
        self.assertEqual(self.queries, [2, 4, 6])
        self.assertEqual([t["extra_data"]["message_id"] for t in task_store.get_pending_tasks()], [3, 5])

    async def test_stale_session_replay_skips_handled_inbox_messages_after_restart(self):
        self.messages = [self.message(2, media=True), self.message(3, text="/query")]
        await self.start_backend()
        # A finished download no longer has an active task to deduplicate against.
        task_store.complete_task(task_store.get_pending_tasks()[0]["task_id"])
        # Simulate a session cursor older than the independently committed receipts.
        await self.backend.bot.storage.set_update_state(UpdateState(0, 10, 0, self.date, 1))
        await self.backend.bot.stop()
        self.new_backend()
        self.processed.clear()
        self.messages = [self.message(2, media=True), self.message(3, text="/query"),
                         self.message(4, media=True), self.message(5, text="/query")]
        await self.start_backend()
        self.assertEqual(self.requested_pts, [10, 10])
        self.assertEqual(self.queries, [3, 5])
        self.assertEqual([task["extra_data"]["message_id"]
                          for task in task_store.get_pending_tasks()], [4])

    async def test_rejected_download_is_not_recorded_as_a_handled_message(self):
        self.messages = [self.message(2, media=True), self.message(3, text="/query")]
        with patch.object(task_store, "_save_all", return_value=False):
            await self.start_backend()
        self.assertFalse(self.backend._message_store.contains((999, 123, 2)))
        self.assertFalse(task_store.get_pending_tasks())
        await self.backend.bot.stop()
        self.new_backend()
        self.processed.clear()
        self.messages = [self.message(2, media=True), self.message(4, text="/query")]
        await self.start_backend()
        self.assertEqual([task["extra_data"]["message_id"]
                          for task in task_store.get_pending_tasks()], [2])
