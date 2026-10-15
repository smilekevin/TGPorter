"""Keep explicit Bot command authorization independent of Telegram peer caches."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from module import bot
from module.telegram import pyrogram
from pyrogram import enums, types


class BotAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_ids_authorize_start_without_resolving_unknown_peers(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = bot.DownloadBot()
            instance.config_path = str(Path(directory) / "bot.yaml")
            instance.recover_tasks = AsyncMock()
            instance._ensure_pending_consumer = Mock()
            instance.update_reply_message = AsyncMock()
            account = types.User(id=999, is_bot=True, username="offline_bot")
            telegram = SimpleNamespace(
                start=AsyncMock(), get_me=AsyncMock(return_value=account), me=account,
                set_bot_commands=AsyncMock(), add_handler=Mock(),
                get_chat=AsyncMock(side_effect=pyrogram.errors.PeerIdInvalid),
            )
            app = SimpleNamespace(
                application_name="offline", api_id=123, api_hash="offline", bot_token="offline",
                session_file_path=directory, proxy={}, loop=asyncio.get_running_loop(),
                allowed_user_ids=[123, "456"], chat_download_config={},
            )
            with patch.object(bot.pyrogram, "Client", return_value=telegram), \
                    patch.object(bot, "send_help_str", AsyncMock()) as send_help:
                await instance.start(app, telegram, AsyncMock(), AsyncMock())
                await instance.reply_task
                telegram.get_chat.assert_not_awaited()
                send_help.assert_not_awaited()
            self.assertEqual(set(instance.allowed_user_ids), {123, 456})
            handlers = [call.args[0] for call in telegram.add_handler.call_args_list
                        if call.args[0].callback is bot.help_command]
            for user_id, expected in [(123, True), (456, True), (789, False)]:
                message = types.Message(
                    id=1, text="/start", from_user=types.User(id=user_id),
                    chat=types.Chat(id=user_id, type=enums.ChatType.PRIVATE),
                )
                self.assertEqual(any([await handler.check(telegram, message) for handler in handlers]), expected)

    async def test_symbolic_users_still_resolve_and_human_admin_keeps_access(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = bot.DownloadBot()
            instance.config_path = str(Path(directory) / "bot.yaml")
            instance.recover_tasks = AsyncMock()
            instance._ensure_pending_consumer = Mock()
            instance.update_reply_message = AsyncMock()
            human = types.User(id=10, is_bot=False)
            owner = SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(id=20)),
                                    get_me=AsyncMock(return_value=human))
            telegram = SimpleNamespace(start=AsyncMock(), get_me=AsyncMock(),
                                       set_bot_commands=AsyncMock(), add_handler=Mock())
            app = SimpleNamespace(
                application_name="offline", api_id=123, api_hash="offline", bot_token="offline",
                session_file_path=directory, proxy=None, loop=asyncio.get_running_loop(),
                allowed_user_ids=["tester"], chat_download_config={},
            )
            with patch.object(bot.pyrogram, "Client", return_value=telegram), \
                    patch.object(bot, "send_help_str", AsyncMock()) as send_help:
                await instance.start(app, owner, AsyncMock(), AsyncMock())
                await instance.reply_task
                send_help.assert_awaited_once_with(telegram, 10)
            owner.get_chat.assert_awaited_once_with("tester")
            self.assertEqual(set(instance.allowed_user_ids), {10, 20})
