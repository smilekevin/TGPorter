"""Probe the Bot session and trace dispatch without contacting Telegram."""

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from loguru import logger

from module import bot, pyrogram_extension as extension
from module.telegram import pyrogram
from pyrogram.session import Session


class BotHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = bot.DownloadBot()
        self.backend.app = SimpleNamespace(loop=asyncio.get_running_loop())
        self.clock = 1000.0
        self.logs = []
        sink = logger.add(lambda message: self.logs.append(str(message)))
        self.addCleanup(logger.remove, sink)
        self.backend.bot = SimpleNamespace(
            is_connected=True, invoke=AsyncMock(side_effect=self.pong),
            stop=AsyncMock(), start=AsyncMock(), disconnect=AsyncMock(),
            dispatcher=SimpleNamespace(handler_worker_tasks=[], updates_queue=asyncio.Queue()),
            add_handler=Mock(),
        )
        self.reconnect = AsyncMock(return_value=True)
        self.report_errors = {"count": 0}
        for item in (
            patch.object(self.backend, "_reconnect_bot", self.reconnect),
            patch.object(bot, "time", SimpleNamespace(
                time=lambda: self.clock, monotonic=lambda: self.clock,
                monotonic_ns=lambda: int(self.clock * 1e9))),
            patch.object(extension, "_bot_conn_errors", self.report_errors),
            patch.object(extension, "_bot_flood_wait", {"until": 0, "seconds": 0}),
            patch.object(extension, "_unified_flood_wait", {"until": 0, "reason": ""}),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.addAsyncCleanup(self.stop_monitor)

    async def stop_monitor(self):
        if self.backend.heartbeat_task is not None:
            self.backend.heartbeat_task.cancel()
            await asyncio.gather(self.backend.heartbeat_task, return_exceptions=True)

    @staticmethod
    async def pong(query, **kwargs):
        return pyrogram.raw.types.Pong(msg_id=1, ping_id=query.ping_id)

    async def fail(self, count=1):
        self.backend.bot.invoke.side_effect = ConnectionError("private error contents")
        for _ in range(count):
            await self.backend._check_bot_health()

    async def test_probe_requires_pong_and_disables_hidden_retries_and_flood_sleep(self):
        self.backend._heartbeat_failures = 2
        await self.backend._check_bot_health()
        call = self.backend.bot.invoke.call_args
        self.assertIsInstance(call.args[0], pyrogram.raw.functions.Ping)
        self.assertEqual(call.kwargs, dict(retries=1, timeout=10, sleep_threshold=0, retry_delay=0))
        self.assertEqual(self.backend._heartbeat_failures, 0)
        self.assertTrue(any("Bot heartbeat OK" in line and "rtt_ms=" in line for line in self.logs))
        self.reconnect.assert_not_awaited()

    async def test_health_snapshot_tracks_probes_without_extra_rpc_and_expires(self):
        self.assertEqual(self.backend.get_connection_status()["status"], "connecting")
        self.backend.bot.invoke.assert_not_awaited()
        await self.backend._check_bot_health()
        result = self.backend.get_connection_status()
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["rtt_ms"], 0)
        self.assertEqual(result["last_checked_at"], self.clock)
        self.assertEqual(result["last_success_at"], self.clock)
        self.assertNotIn("checked_monotonic", result)
        result["status"] = "modified"
        self.assertEqual(self.backend.get_connection_status()["status"], "connected")
        self.clock += 71
        self.assertEqual(self.backend.get_connection_status()["status"], "unknown")
        self.backend.bot.invoke.assert_awaited_once()
        await self.fail()
        result = self.backend.get_connection_status()
        self.assertEqual(result["status"], "unresponsive")
        self.assertEqual(result["failures"], 1)
        self.assertIsNone(result["rtt_ms"])
        self.assertEqual(result["last_success_at"], 1000)
        self.backend.bot.invoke.side_effect = self.pong
        await self.backend._check_bot_health()
        self.assertEqual(self.backend.get_connection_status()["status"], "connected")

    async def test_snapshot_distinguishes_transport_loss_flood_wait_and_shutdown(self):
        await self.backend._check_bot_health()
        extension._bot_flood_wait["until"] = self.clock + 120.5
        result = self.backend.get_connection_status()
        self.assertEqual(result["status"], "flood_wait")
        self.assertEqual(result["flood_wait_remaining"], 121)
        self.backend.bot.is_connected = False
        self.assertEqual(self.backend.get_connection_status()["status"], "disconnected")
        self.backend.is_running = False
        self.assertEqual(self.backend.get_connection_status()["status"], "stopped")

    async def test_reconnect_snapshot_stays_unconfirmed_until_next_pong(self):
        async def restart():
            self.assertEqual(self.backend.get_connection_status()["status"], "reconnecting")
            return True

        self.reconnect.side_effect = restart
        await self.fail(3)
        self.assertEqual(self.backend.get_connection_status()["status"], "connecting")
        self.backend.bot.invoke.side_effect = self.pong
        await self.backend._check_bot_health()
        self.assertEqual(self.backend.get_connection_status()["status"], "connected")

    async def test_probe_uses_the_pinned_sdk_invoke_path(self):
        client = self.use_sdk_session(self.pong)
        self.backend._heartbeat_failures = 2
        for _ in range(3):
            await self.backend._check_bot_health()
            self.assertEqual(self.backend._heartbeat_failures, 0)
        self.assertEqual(client.session.send.await_count, 3)
        self.assertIsInstance(client.session.send.call_args.args[0],
                              pyrogram.raw.functions.Ping)
        self.assertEqual(client.session.send.call_args.kwargs["timeout"], 10)
        self.reconnect.assert_not_awaited()
        self.assertTrue(any("Bot heartbeat OK" in line for line in self.logs))

    def use_sdk_session(self, send):
        """Keep both SDK invoke layers real; replace only the network send boundary."""
        client = pyrogram.Client("offline-heartbeat", api_id=1, api_hash="offline", in_memory=True)
        self.addCleanup(client.executor.shutdown)
        client.is_connected = True
        client.session = Session(client, 2, "149.154.167.51", 443, b"k" * 256, False)
        client.session.is_started.set()
        client.session.send = AsyncMock(side_effect=send)
        client.fetch_peers = AsyncMock()
        self.backend.bot = client
        return client

    async def test_sdk_send_failure_makes_one_attempt_per_probe(self):
        client = self.use_sdk_session(ConnectionError("Offline test transport"))
        for _ in range(3):
            await self.backend._check_bot_health()
        self.assertEqual(client.session.send.await_count, 3)
        self.reconnect.assert_awaited_once()

    async def test_sdk_hung_send_is_cancelled_at_probe_deadline(self):
        cancelled = asyncio.Event()

        async def hang(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        client = self.use_sdk_session(hang)
        with patch.object(bot, "_BOT_HEARTBEAT_TIMEOUT", 0.01):
            await asyncio.wait_for(self.backend._check_bot_health(), 1)
        client.session.send.assert_awaited_once()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.backend._heartbeat_failures, 1)

    async def test_sdk_flood_wait_does_not_retry_or_count_a_connection_failure(self):
        client = self.use_sdk_session(pyrogram.errors.FloodWait(120))
        with patch.object(extension, "time", SimpleNamespace(time=lambda: self.clock)):
            await self.backend._check_bot_health()
        client.session.send.assert_awaited_once()
        self.assertEqual(self.backend._heartbeat_failures, 0)
        self.assertEqual(extension._bot_flood_wait["until"], self.clock + 120)
        self.reconnect.assert_not_awaited()

    async def test_monitor_publishes_startup_health_before_waiting_for_next_interval(self):
        async def wait_interval(seconds):
            self.assertEqual(seconds, 60)
            self.backend.bot.invoke.assert_awaited_once()
            self.assertEqual(self.backend.get_connection_status()["status"], "connected")
            self.backend.is_running = False

        with patch.object(bot.asyncio, "sleep", wait_interval):
            await self.backend._heartbeat_loop()
        self.backend.bot.invoke.assert_awaited_once()

    async def test_immediate_startup_probe_respects_existing_flood_wait(self):
        extension._bot_flood_wait["until"] = self.clock + 120

        async def wait_interval(seconds):
            self.assertEqual(self.backend.get_connection_status()["status"], "flood_wait")
            self.backend.is_running = False

        with patch.object(bot.asyncio, "sleep", wait_interval):
            await self.backend._heartbeat_loop()
        self.backend.bot.invoke.assert_not_awaited()
        self.reconnect.assert_not_awaited()
        self.assertEqual(self.backend._heartbeat_failures, 0)

    async def test_monitor_reports_event_loop_stalls(self):
        async def delayed_sleep(seconds):
            self.clock += seconds + 30

        probes = 0

        async def finish_probe():
            nonlocal probes
            probes += 1
            if probes == 2:
                self.backend.is_running = False

        with patch.object(bot.asyncio, "sleep", delayed_sleep), \
                patch.object(self.backend, "_check_bot_health", finish_probe):
            await self.backend._heartbeat_loop()
        self.assertTrue(any("event_loop_delay_s=30.0" in line for line in self.logs))

    async def test_connected_flag_does_not_hide_a_hung_probe(self):
        cancelled = asyncio.Event()

        async def hang(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.backend.bot.invoke.side_effect = hang
        with patch.object(bot, "_BOT_HEARTBEAT_TIMEOUT", 0.01):
            await asyncio.wait_for(self.backend._check_bot_health(), 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.backend._heartbeat_failures, 1)
        self.assertTrue(any("error=TimeoutError" in line for line in self.logs))

    async def test_wrong_pong_is_a_failed_probe(self):
        self.backend.bot.invoke.side_effect = None
        self.backend.bot.invoke.return_value = pyrogram.raw.types.Pong(msg_id=1, ping_id=-1)
        await self.backend._check_bot_health()
        self.assertEqual(self.backend._heartbeat_failures, 1)

    async def test_idle_bot_reconnects_after_three_failures_and_obeys_cooldown(self):
        self.assertFalse(self.backend.task_node)
        await self.fail(2)
        self.reconnect.assert_not_awaited()
        await self.fail()
        self.reconnect.assert_awaited_once()
        await self.fail(3)
        self.assertEqual(self.reconnect.await_count, 1)
        self.clock += 300
        await self.fail()
        self.assertEqual(self.reconnect.await_count, 2)
        self.assertNotIn("private error contents", "".join(self.logs))

    async def test_failed_reconnect_can_retry_after_one_interval(self):
        self.reconnect.return_value = False
        await self.fail(3)
        self.clock += 59
        await self.fail()
        self.reconnect.assert_awaited_once()
        self.clock += 1
        await self.fail()
        self.assertEqual(self.reconnect.await_count, 2)

    async def test_cooldown_starts_after_slow_reconnect_finishes(self):
        async def restart():
            self.clock += 70
            return True

        self.reconnect.side_effect = restart
        await self.fail(3)
        self.assertEqual(self.backend._bot_reconnect_after, self.clock + 300)

    async def test_flood_wait_is_shared_and_does_not_trigger_reconnect(self):
        self.backend.bot.invoke.side_effect = pyrogram.errors.FloodWait(120)
        self.backend._heartbeat_failures = 2
        with patch.object(extension, "time", SimpleNamespace(time=lambda: self.clock)):
            await self.backend._check_bot_health()
        self.assertEqual(extension._bot_flood_wait["until"], self.clock + 120)
        snapshot = self.backend.get_connection_status()
        self.assertEqual(snapshot["status"], "flood_wait")
        self.assertEqual(snapshot["last_checked_at"], self.clock)
        self.assertIsNone(snapshot["last_success_at"])
        await self.backend._check_bot_health()
        self.backend.bot.invoke.assert_awaited_once()
        self.assertEqual(self.backend._heartbeat_failures, 2)
        self.reconnect.assert_not_awaited()
        self.clock += 121
        self.assertEqual(self.backend.get_connection_status()["status"], "unknown")
        self.backend.bot.invoke.side_effect = self.pong
        await self.backend._check_bot_health()
        self.assertEqual(self.backend._heartbeat_failures, 0)

    async def test_existing_report_error_threshold_uses_independent_monitor(self):
        self.report_errors["count"] = extension._BOT_RECONNECT_THRESHOLD
        await self.backend._check_bot_health()
        self.reconnect.assert_awaited_once()
        self.assertEqual(self.report_errors["count"], 0)

    async def test_shutdown_cancels_in_flight_probe_and_awaits_monitor(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def hang(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.backend.bot.invoke.side_effect = hang
        task = self.backend._ensure_heartbeat()
        self.assertIs(task, self.backend._ensure_heartbeat())
        # The initial probe must start within one second with the real 60s interval.
        await asyncio.wait_for(entered.wait(), 1)
        with patch.object(bot, "_bot", self.backend), \
                patch.object(self.backend, "update_config"), \
                patch.object(self.backend, "stop_task"):
            await bot.stop_download_bot()
        self.assertTrue(task.done())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.backend._heartbeat_failures, 0)
        self.assertIsNone(self.backend.heartbeat_task)
        self.backend.bot.stop.assert_awaited_once()

    async def test_monitor_can_restart_and_is_disabled_without_bot(self):
        first = self.backend._ensure_heartbeat()
        await self.stop_monitor()
        self.assertIsNot(first, self.backend._ensure_heartbeat())
        await self.stop_monitor()
        self.backend.bot = None
        self.assertIsNone(self.backend._ensure_heartbeat())
        await self.backend._check_bot_health()
        self.reconnect.assert_not_awaited()

    async def test_received_message_logs_authorization_without_contents(self):
        message = SimpleNamespace(id=5, text="/help private contents", media=None,
                                  date=datetime.fromtimestamp(self.clock - 900, timezone.utc),
                                  from_user=SimpleNamespace(id=123))
        await self.backend._observe_bot_message(self.backend.bot, message)
        self.backend.allowed_user_ids = [123]
        await self.backend._observe_bot_message(self.backend.bot, message)
        self.assertIn("authorized=False", self.logs[-2])
        self.assertIn("authorized=True", self.logs[-1])
        self.assertIn("message_age_s=900", self.logs[-1])
        self.assertNotIn("private contents", "".join(self.logs))

    async def test_handler_logs_stalls_completion_errors_and_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def command(client, update):
            entered.set()
            await release.wait()
            return "done"

        logged = self.backend._logged_bot_handler(command)
        task = asyncio.create_task(logged(self.backend.bot, None))
        await entered.wait()
        self.clock += 70
        await self.backend._check_bot_health()
        self.assertTrue(any("active_handlers=1 oldest_handler_s=70" in line for line in self.logs))
        release.set()
        self.assertEqual(await task, "done")
        self.assertFalse(self.backend._active_handlers)
        self.assertIn("outcome=completed", self.logs[-1])

        async def failed(client, update):
            raise ValueError("private error contents")

        with self.assertRaises(ValueError):
            await self.backend._logged_bot_handler(failed)(None, None)
        self.assertIn("outcome=failed", self.logs[-1])
        self.assertNotIn("private error contents", "".join(self.logs))

        release.clear()
        task = asyncio.create_task(logged(None, None))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIn("outcome=cancelled", self.logs[-1])
        self.assertFalse(self.backend._active_handlers)

    async def test_reconnect_registers_one_observer_and_each_handler_once(self):
        # Exercise the real stop/start path, not the monitor's mock restart.
        self.backend._reconnect_bot = bot.DownloadBot._reconnect_bot.__get__(self.backend)
        self.assertTrue(await self.backend._reconnect_bot())
        calls = self.backend.bot.add_handler.call_args_list
        observers = [call for call in calls if call.kwargs.get("group") == -1]
        self.assertEqual(len(observers), 1)
        names = [call.args[0].callback.__name__ for call in calls]
        self.assertEqual(names.count("forward_to_comments"), 1)
        self.assertEqual(names.count("help_command"), 2)  # /help and /start
        self.backend.bot.stop.assert_awaited_once()
        self.backend.bot.start.assert_awaited_once()
