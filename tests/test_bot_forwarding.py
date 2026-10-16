"""Keep forwarded-media identity and replies compatible with the pinned SDK."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from module import bot, pyrogram_extension, task_store
from module.telegram import pyrogram
from pyrogram import enums, raw, types


class BotForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = bot.DownloadBot()
        self.sender = types.User(id=100)
        self.inbox = types.Chat(id=100, type=enums.ChatType.PRIVATE)
        self.source = types.Chat(id=-100123, type=enums.ChatType.CHANNEL,
                                 title="Offline source", username="offline_source")
        self.date = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.telegram = pyrogram.Client("offline-forward", api_id=1, api_hash="offline", in_memory=True)
        self.addCleanup(self.telegram.executor.shutdown)
        self.telegram.resolve_peer = AsyncMock(side_effect=lambda peer: (
            None if peer is None else raw.types.InputPeerUser(user_id=100, access_hash=0)))
        self.telegram.invoke = AsyncMock(return_value=raw.types.UpdateShortSentMessage(
            id=200, pts=1, pts_count=1, date=int(self.date.timestamp())))
        self.backend.bot = self.telegram
        self.user_client = SimpleNamespace(get_messages=AsyncMock())
        self.backend.client = self.user_client
        self.source_message = self.message(61, chat=self.source, client=self.user_client)
        self.user_client.get_messages.return_value = self.source_message
        for item in (
            patch.object(bot, "_bot", self.backend),
            patch.object(task_store, "_TASKS_FILE", str(Path(self.temp.name) / "tasks.json")),
            patch.object(task_store, "_COUNTER_FILE", str(Path(self.temp.name) / "counter.json")),
        ):
            item.start()
            self.addCleanup(item.stop)

    def message(self, message_id=2, origin=None, chat=None, client=None):
        return types.Message(
            id=message_id, date=self.date, from_user=self.sender, chat=chat or self.inbox,
            forward_origin=origin, media=enums.MessageMediaType.DOCUMENT,
            document=types.Document(file_id="offline-file", file_unique_id="offline-unique"),
            client=client or self.telegram)

    async def download(self, origin=None):
        with self.assertNoLogs("pyrogram", level="WARNING"):
            await bot.download_forward_media(self.telegram, self.message(origin=origin))
        request = self.telegram.invoke.call_args.args[0]
        self.assertIsInstance(request, raw.functions.messages.SendMessage)
        self.assertIsInstance(request.reply_to, raw.types.InputReplyToMessage)
        self.assertEqual(request.reply_to.reply_to_msg_id, 2)
        self.assertIsNone(request.reply_to.reply_to_peer_id)
        tasks = task_store.get_running_tasks()
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    async def test_channel_origin_preserves_source_and_account_without_deprecations(self):
        task = await self.download(types.MessageOriginChannel(
            date=self.date, chat=self.source, message_id=61))
        self.user_client.get_messages.assert_awaited_once_with(-100123, 61)
        self.assertEqual(task["chat_id"], -100123)
        self.assertEqual(task["extra_data"]["message_id"], 61)
        self.assertEqual(task["extra_data"]["source_chat_id"], -100123)
        self.assertEqual(task["extra_data"]["source_message_id"], 61)
        self.assertEqual(task["extra_data"]["source_chat_title"], "Offline source")
        self.assertEqual(task["extra_data"]["message_client"], "user")

    async def test_missing_source_media_uses_public_link_fallback(self):
        self.user_client.get_messages.side_effect = [types.Message(id=61, empty=True), self.source_message]
        with patch.object(pyrogram_extension, "parse_link", AsyncMock(return_value=(-100123, 61, 0))) as parse:
            task = await self.download(types.MessageOriginChannel(
                date=self.date, chat=self.source, message_id=61))
        parse.assert_awaited_once_with(self.user_client, "https://t.me/offline_source/61")
        self.assertEqual(task["extra_data"]["source_message_id"], 61)
        self.assertEqual(task["extra_data"]["message_client"], "user")

    async def test_private_channel_fallback_keeps_source_link_identity(self):
        self.source.username = None
        self.user_client.get_messages.side_effect = [types.Message(id=61, empty=True), self.source_message]
        with patch.object(pyrogram_extension, "parse_link", AsyncMock(return_value=(-100123, 61, 0))) as parse:
            task = await self.download(types.MessageOriginChannel(
                date=self.date, chat=self.source, message_id=61))
        parse.assert_awaited_once_with(self.user_client, "https://t.me/c/123/61")
        self.assertEqual(task["extra_data"]["source_chat_id"], -100123)

    async def test_unavailable_source_falls_back_to_bot_inbox(self):
        self.user_client.get_messages.return_value = types.Message(id=61, empty=True)
        with patch.object(pyrogram_extension, "parse_link", AsyncMock(return_value=(None, None, None))):
            task = await self.download(types.MessageOriginChannel(
                date=self.date, chat=self.source, message_id=61))
        self.assertEqual(task["chat_id"], 100)
        self.assertEqual(task["extra_data"]["message_id"], 2)
        self.assertEqual(task["extra_data"]["message_client"], "bot")

    async def test_group_sender_origin_without_post_id_uses_bot_inbox(self):
        task = await self.download(types.MessageOriginChat(date=self.date, sender_chat=self.source))
        self.user_client.get_messages.assert_not_awaited()
        self.assertEqual(task["chat_id"], 100)
        self.assertEqual(task["extra_data"]["message_client"], "bot")

    async def test_user_origin_uses_bot_inbox(self):
        task = await self.download(types.MessageOriginUser(date=self.date, sender_user=self.sender))
        self.user_client.get_messages.assert_not_awaited()
        self.assertEqual(task["extra_data"]["message_client"], "bot")

    async def test_hidden_user_origin_uses_bot_inbox(self):
        task = await self.download(types.MessageOriginHiddenUser(date=self.date, sender_user_name="Hidden"))
        self.user_client.get_messages.assert_not_awaited()
        self.assertEqual(task["extra_data"]["message_client"], "bot")

    async def test_direct_upload_uses_bot_inbox(self):
        task = await self.download()
        self.user_client.get_messages.assert_not_awaited()
        self.assertEqual(task["extra_data"]["message_client"], "bot")
