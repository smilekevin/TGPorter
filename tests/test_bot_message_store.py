"""Durable Bot receipts survive replay, failures and out-of-order deliveries."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from module.bot import DownloadBot
from module.bot_message_store import BotMessageStore


class BotMessageStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "bot_processed_messages.json"

    def test_ring_evicts_oldest_and_restores_exact_identities(self):
        store = BotMessageStore(self.path, capacity=3)
        keys = [(999, 123, 8), (999, 456, 8), (888, 123, 8), (999, 123, 2)]
        for key in keys:
            store.mark(key)
        store.mark(keys[-1])
        restored = BotMessageStore(self.path, capacity=3)
        self.assertFalse(restored.contains(keys[0]))
        for key in keys[1:]:
            self.assertTrue(restored.contains(key))
        self.assertEqual(len(json.loads(self.path.read_text())["messages"]), 3)
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_failed_atomic_save_preserves_previous_file_and_memory(self):
        store = BotMessageStore(self.path, capacity=1)
        first, second = (999, 123, 1), (999, 123, 2)
        store.mark(first)
        previous = self.path.read_bytes()
        for target in ("os.fsync", "os.replace"):
            with self.subTest(target=target), patch(
                "module.bot_message_store." + target, side_effect=OSError("disk error")
            ):
                with self.assertRaises(OSError):
                    store.mark(second)
                self.assertTrue(store.contains(first))
                self.assertFalse(store.contains(second))
                self.assertEqual(self.path.read_bytes(), previous)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        store.mark(second)
        self.assertTrue(BotMessageStore(self.path).contains(second))

    def test_corrupt_receipts_are_preserved_and_reported(self):
        for content in ("{", '{"version":2,"messages":[]}',
                        '{"version":1,"messages":[[999,123,"1"]]}'):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(ValueError):
                    BotMessageStore(self.path)
                self.assertEqual(self.path.read_text(), content)

    def test_default_capacity_is_one_thousand(self):
        self.path.write_text(json.dumps({"version": 1, "messages": [
            [999, 123, message_id] for message_id in range(1, 1002)
        ]}))
        store = BotMessageStore(self.path)
        self.assertFalse(store.contains((999, 123, 1)))
        self.assertTrue(store.contains((999, 123, 2)))
        self.assertTrue(store.contains((999, 123, 1001)))


class BotMessageHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "bot_processed_messages.json"
        self.backend = DownloadBot()
        self.backend._message_store = BotMessageStore(self.path)
        self.client = SimpleNamespace(me=SimpleNamespace(id=999))
        self.message = SimpleNamespace(id=8, chat=SimpleNamespace(id=123))

    def wrap(self, callback, *, deduplicate=True):
        callback.__name__ = "test_handler"
        return self.backend._logged_bot_handler(callback, deduplicate=deduplicate)

    async def test_replay_is_skipped_but_new_messages_chats_and_accounts_run(self):
        callback = AsyncMock()
        wrapped = self.wrap(callback)
        await wrapped(self.client, self.message)
        self.backend._message_store = BotMessageStore(self.path)
        await wrapped(self.client, self.message)
        self.message.id = 2  # Late delivery must not be compared to a high-water mark.
        await wrapped(self.client, self.message)
        self.message.chat.id = 456
        await wrapped(self.client, self.message)
        self.client.me.id = 888
        await wrapped(self.client, self.message)
        self.assertEqual(callback.await_count, 4)

    async def test_inflight_duplicate_waits_for_first_success(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def process(*args):
            entered.set()
            await release.wait()

        callback = AsyncMock(side_effect=process)
        wrapped = self.wrap(callback)
        first = asyncio.create_task(wrapped(self.client, self.message))
        await entered.wait()
        second = asyncio.create_task(wrapped(self.client, self.message))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        callback.assert_awaited_once()
        self.assertEqual(self.backend._message_inflight, {})

    async def test_failed_cancelled_and_rejected_handlers_can_retry(self):
        for failure in (RuntimeError("failed"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                callback = AsyncMock(side_effect=[failure, False, None])
                wrapped = self.wrap(callback)
                with self.assertRaises(type(failure)):
                    await wrapped(self.client, self.message)
                await wrapped(self.client, self.message)
                self.assertFalse(self.backend._message_store.contains((999, 123, 8)))
                await wrapped(self.client, self.message)
                callback.assert_awaited()
                self.assertEqual(callback.await_count, 3)
                self.assertEqual(self.backend._message_inflight, {})
                self.path.unlink()
                self.backend._message_store = BotMessageStore(self.path)

    async def test_cancelled_waiter_does_not_cancel_owner(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def process(*args):
            entered.set()
            await release.wait()

        callback = AsyncMock(side_effect=process)
        wrapped = self.wrap(callback)
        first = asyncio.create_task(wrapped(self.client, self.message))
        await entered.wait()
        second = asyncio.create_task(wrapped(self.client, self.message))
        await asyncio.sleep(0)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        release.set()
        await first
        self.assertTrue(self.backend._message_store.contains((999, 123, 8)))
        callback.assert_awaited_once()

    async def test_waiting_duplicate_retries_after_owner_failure(self):
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = 0

        async def process(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("first attempt failed")

        wrapped = self.wrap(process)
        first = asyncio.create_task(wrapped(self.client, self.message))
        await entered.wait()
        second = asyncio.create_task(wrapped(self.client, self.message))
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(RuntimeError):
            await first
        await second
        self.assertEqual(attempts, 2)
        self.assertTrue(self.backend._message_store.contains((999, 123, 8)))

    async def test_callback_query_clicks_are_not_deduplicated_by_message_id(self):
        callback = AsyncMock()
        wrapped = self.wrap(callback, deduplicate=False)
        query = SimpleNamespace(message=self.message)
        await wrapped(self.client, query)
        await wrapped(self.client, query)
        self.assertEqual(callback.await_count, 2)
        self.assertFalse(self.path.exists())
