"""Verify the SDK migration without Telegram credentials or network access."""

import importlib.metadata
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from module.telegram import pyrogram
from pyrogram import enums, raw, types
from pyrogram.file_id import FileId, FileType
from pyrogram.storage.sqlite_storage import SQLiteStorage

from module.pyrogram_extension import HookClient
from module.send_media_group_v2 import send_media_group_v2


class KurigramMigrationTests(unittest.IsolatedAsyncioTestCase):
    def test_distribution_owns_the_pyrogram_namespace(self):
        self.assertEqual(importlib.metadata.version("Kurigram"), "2.2.26")
        with self.assertRaises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.version("Pyrogram")

    def test_previous_fork_file_id_keeps_its_identity_and_reference(self):
        # Encoded by tangyoha/pyrogram 51a100c5 using synthetic identifiers.
        encoded = "BQACAgIAAxFvZmZsaW5lLXJlZmVyZW5jZQACOTAABjIJAQAFHgQ"
        decoded = FileId.decode(encoded)
        self.assertEqual(decoded.file_type, FileType.DOCUMENT)
        self.assertEqual((decoded.dc_id, decoded.media_id, decoded.access_hash), (2, 12345, 67890))
        self.assertEqual(decoded.file_reference, b"offline-reference")
        self.assertEqual(decoded.encode(), encoded)

    async def test_version_six_session_migrates_without_losing_account_or_peers(self):
        for is_bot in (False, True):
            with self.subTest(is_bot=is_bot), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "offline.session"
                # The prior fork's version-six columns, populated only with fake data.
                with sqlite3.connect(path) as connection:
                    connection.executescript('''
                        CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, api_id INTEGER,
                            test_mode INTEGER, auth_key BLOB, date INTEGER NOT NULL,
                            user_id INTEGER, is_bot INTEGER);
                        CREATE TABLE peers (id INTEGER PRIMARY KEY, access_hash INTEGER,
                            type TEXT NOT NULL, phone_number TEXT, last_update_on INTEGER);
                        CREATE TABLE usernames (id INTEGER, username TEXT);
                        CREATE TABLE update_state (id INTEGER PRIMARY KEY, pts INTEGER,
                            qts INTEGER, date INTEGER, seq INTEGER);
                        CREATE TABLE version (number INTEGER PRIMARY KEY);
                        INSERT INTO version VALUES (6);
                        INSERT INTO peers VALUES (100, 200, 'user', NULL, 0);
                        INSERT INTO usernames VALUES (100, 'offline');
                        INSERT INTO update_state VALUES (0, 10, 20, 30, 40);
                    ''')
                    connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                                       (2, 123, 0, b"k" * 256, 0, 456, is_bot))
                storage = SQLiteStorage("offline", workdir=Path(directory))
                await storage.open()
                try:
                    self.assertEqual(await storage.version(), 7)
                    self.assertEqual(await storage.auth_key(), b"k" * 256)
                    self.assertEqual(await storage.user_id(), 456)
                    self.assertEqual(await storage.is_bot(), is_bot)
                    self.assertEqual(await storage.server_address(), "149.154.167.51")
                    self.assertEqual(await storage.port(), 443)
                    self.assertEqual(storage.conn.execute("SELECT id, access_hash FROM peers").fetchone(), (100, 200))
                    self.assertEqual(storage.conn.execute("SELECT pts FROM update_state").fetchone(), (10,))
                finally:
                    await storage.close()

    async def test_connect_uses_stored_endpoint_and_configured_timeout(self):
        client = HookClient("offline", in_memory=True, start_timeout=75)
        self.addCleanup(client.executor.shutdown)
        client.load_session = AsyncMock()
        client.storage = SimpleNamespace(**{name: AsyncMock(return_value=value) for name, value in {
            "dc_id": 2, "server_address": "149.154.167.51", "port": 443,
            "auth_key": b"k" * 256, "test_mode": False, "user_id": 123,
        }.items()})
        session = SimpleNamespace(start=AsyncMock(), start_timeout=Mock())
        with patch("module.pyrogram_extension.HookSession", return_value=session) as factory:
            self.assertTrue(await client.connect())
        factory.assert_called_once_with(client, 2, "149.154.167.51", 443, b"k" * 256, False)
        session.start_timeout.assert_called_once_with(75)
        session.start.assert_awaited_once()
        self.assertTrue(client.is_connected)

    async def test_start_initializes_dispatcher_and_update_state_once(self):
        client = HookClient("offline", in_memory=True)
        self.addCleanup(client.executor.shutdown)
        client.connect = AsyncMock(return_value=True)
        client.is_connected = True
        client.invoke = AsyncMock(return_value=SimpleNamespace(pts=1, qts=2, date=3, seq=4))
        client.get_me = AsyncMock(return_value=types.User(id=123))
        client.storage = SimpleNamespace(get_update_states=AsyncMock(return_value=None),
                                         set_update_state=AsyncMock(), save=AsyncMock())
        client.dispatcher.start = AsyncMock()
        client._download_connections_closing = True
        self.assertIs(await client.start(), client)
        try:
            client.dispatcher.start.assert_awaited_once()
            client.storage.set_update_state.assert_awaited_once()
            client.storage.save.assert_awaited_once()
            self.assertFalse(client._download_connections_closing)
        finally:
            client.updates_watchdog_event.set()
            await client.updates_watchdog_task

    async def test_album_reply_and_topic_use_kurigram_raw_reply(self):
        client = SimpleNamespace(resolve_peer=AsyncMock(return_value=None),
                                 invoke=AsyncMock(return_value=SimpleNamespace(updates=[], users=[], chats=[])),
                                 parser=SimpleNamespace(parse=AsyncMock(return_value={"message": "", "entities": None})))
        await send_media_group_v2(
            client, 123, [], message_thread_id=50, reply_to_message_id=60,
            quote_text="quoted", quote_entities=[types.MessageEntity(
                type=enums.MessageEntityType.BOLD, offset=0, length=6)])
        request = client.invoke.call_args.args[0]
        self.assertIsInstance(request.reply_to, raw.types.InputReplyToMessage)
        self.assertEqual(request.reply_to.reply_to_msg_id, 60)
        self.assertEqual(request.reply_to.top_msg_id, 50)
        self.assertEqual(request.reply_to.quote_text, "quoted")
        self.assertIsInstance(request.reply_to.quote_entities[0], raw.types.MessageEntityBold)
