"""Refresh translated Telegram menus asynchronously without delaying saved settings."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from module import bot, pyrogram_extension as extension
from module.language import set_language


class BotCommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.addCleanup(set_language, 'en')
        set_language('en')
        self.backend = bot.DownloadBot()
        self.backend.app = SimpleNamespace(loop=asyncio.get_running_loop())
        self.backend.bot = SimpleNamespace(is_initialized=True, set_bot_commands=AsyncMock())
        flood_wait = patch.object(extension, '_bot_flood_wait', {'until': 0, 'seconds': 0})
        flood_wait.start()
        self.addCleanup(flood_wait.stop)
        self.addAsyncCleanup(self.stop_refresh)

    async def stop_refresh(self):
        task = self.backend._commands_refresh_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_requests_are_nonblocking_coalesced_and_use_current_language(self):
        self.backend.schedule_commands_refresh()
        first = self.backend._commands_refresh_task
        set_language('zh-cn')
        self.backend.schedule_commands_refresh()
        self.assertIs(self.backend._commands_refresh_task, first)
        self.backend.bot.set_bot_commands.assert_not_awaited()
        await first
        commands = self.backend.bot.set_bot_commands.await_args.args[0]
        self.assertEqual(commands[0].command, 'help')
        self.assertEqual(commands[0].description, '帮助')
        self.assertIn('query', [command.command for command in commands])
        self.backend.bot.set_bot_commands.assert_awaited_once()
        set_language('fr')
        self.backend.schedule_commands_refresh()
        await self.backend._commands_refresh_task
        self.assertEqual(self.backend.bot.set_bot_commands.await_args.args[0][0].description, 'Aide')

    async def test_save_during_refresh_publishes_latest_language_last(self):
        started, release = asyncio.Event(), asyncio.Event()
        published = []

        async def publish(commands):
            if not published:
                started.set()
                await release.wait()
            published.append(commands[0].description)

        self.backend.bot.set_bot_commands.side_effect = publish
        self.backend.schedule_commands_refresh()
        await started.wait()
        set_language('ja')
        self.backend.schedule_commands_refresh()
        set_language('fr')
        self.backend.schedule_commands_refresh()
        release.set()
        await self.backend._commands_refresh_task
        self.assertEqual(published, ['Help', 'Aide'])

    async def test_unstarted_and_stopped_bots_do_not_schedule_requests(self):
        for client, running in ((None, True), (SimpleNamespace(is_initialized=False), True),
                                (self.backend.bot, False)):
            self.backend.bot, self.backend.is_running = client, running
            self.backend.schedule_commands_refresh()
            self.assertIsNone(self.backend._commands_refresh_task)

    async def test_errors_and_timeout_finish_and_allow_later_refresh(self):
        async def blocked(*args):
            await asyncio.Future()

        for failure in (ConnectionError('private details'), blocked):
            self.backend.bot.set_bot_commands.side_effect = failure
            with patch.object(bot, '_BOT_COMMANDS_TIMEOUT', 0.01):
                self.backend.schedule_commands_refresh()
                await self.backend._commands_refresh_task
            self.assertIsNone(self.backend._commands_refresh_task.exception())
        self.backend.bot.set_bot_commands.side_effect = None
        self.backend.schedule_commands_refresh()
        await self.backend._commands_refresh_task
        self.assertEqual(self.backend.bot.set_bot_commands.await_count, 3)

    async def test_shared_flood_wait_is_respected_and_new_wait_is_recorded(self):
        extension._bot_flood_wait['until'] = bot.time.time() + 30
        self.backend.schedule_commands_refresh()
        await self.backend._commands_refresh_task
        self.backend.bot.set_bot_commands.assert_not_awaited()
        extension._bot_flood_wait['until'] = 0
        self.backend.bot.set_bot_commands.side_effect = bot.pyrogram.errors.FloodWait(30)
        with patch.object(extension, '_record_bot_flood_wait') as record:
            self.backend.schedule_commands_refresh()
            await self.backend._commands_refresh_task
        record.assert_called_once_with(30)

    async def test_shutdown_cancels_and_awaits_menu_refresh(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked(*args):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.backend.bot.set_bot_commands.side_effect = blocked
        self.backend.bot.stop = AsyncMock()
        self.backend.update_config = Mock()
        self.backend.stop_task = Mock()
        self.backend.schedule_commands_refresh()
        await started.wait()
        task = self.backend._commands_refresh_task
        self.backend.schedule_commands_refresh()
        self.assertEqual(self.backend._commands_queue.qsize(), 1)
        with patch.object(bot, '_bot', self.backend):
            await bot.stop_download_bot()
        self.assertTrue(task.cancelled())
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.backend._commands_refresh_task)
        self.assertTrue(self.backend._commands_queue.empty())
        await self.backend._commands_queue.join()
