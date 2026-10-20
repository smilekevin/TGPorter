"""Exercise real client construction at the production startup boundaries."""

import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import media_downloader as downloader
from module.app import Application
from module.bot import DownloadBot
from module.telegram import pyrogram
from pyrogram.connection.proxy import normalize_proxy
from pyrogram.connection.transport.tcp import TCP


PROXY_CASES = (
    {},
    {"proxy": {}},
    {"proxy": None},
    {"proxy": {"scheme": "http", "hostname": "127.0.0.1", "port": 1080}},
    {"proxy": {"scheme": "socks5", "hostname": "127.0.0.1", "port": 1080,
               "username": "offline", "password": "offline"}},
)


class StartupReached(BaseException):
    """Stop at the first network boundary after constructing the actual SDK client."""


class MainStartupProxyTests(unittest.TestCase):
    def test_web_login_constructs_real_clients_with_disabled_and_configured_proxies(self):
        from module.telegram_login import TelegramLogin, WebLoginClient
        for settings in PROXY_CASES:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                app = Application("unused-config.yaml", str(Path(directory) / "data.json"))
                app.assign_config({"api_id": 123, "api_hash": "offline", "media_types": [],
                                   "file_formats": {}, **settings})
                app.session_file_path = directory
                reached = []

                async def connect(client):
                    reached.append(client)
                    self.assertEqual(client.proxy, normalize_proxy(settings.get("proxy") or None))
                    raise StartupReached

                manager = TelegramLogin(app, AsyncMock(), AsyncMock())
                try:
                    with patch.object(WebLoginClient, "connect", new=connect):
                        with self.assertRaises(StartupReached):
                            app.loop.run_until_complete(manager._connect())
                    self.assertEqual(len(reached), 1)
                    self.assertEqual(list(Path(directory).iterdir()), [])
                finally:
                    if manager.client is not None:
                        manager.client.executor.shutdown()
                    app.loop.close()
                    app.executor.shutdown()
                    asyncio.set_event_loop(None)


class BotStartupProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_reaches_telegram_start_with_disabled_and_configured_proxies(self):
        for settings in PROXY_CASES:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                instance = DownloadBot()
                instance.config_path = str(Path(directory) / "bot.yaml")
                instance.recover_tasks = AsyncMock()
                app = SimpleNamespace(application_name="offline", api_id=123, api_hash="offline",
                                      bot_token="123:offline", session_file_path=directory,
                                      proxy=settings.get("proxy") or {}, allowed_user_ids=[])
                owner = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=123, is_bot=True)))
                reached = []

                async def start(client):
                    reached.append(client)
                    self.assertEqual(client.proxy, normalize_proxy(settings.get("proxy") or None))
                    raise StartupReached

                try:
                    with patch.object(pyrogram.Client, "start", new=start):
                        with self.assertRaises(StartupReached):
                            await instance.start(app, owner, AsyncMock(), AsyncMock())
                    self.assertEqual(len(reached), 1)
                    self.assertEqual(list(Path(directory).iterdir()), [])
                finally:
                    if instance.bot is not None:
                        instance.bot.executor.shutdown()
