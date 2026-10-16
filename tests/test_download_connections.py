"""Exercise real Pyrogram get_file routing with offline media connections."""

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from module.telegram import pyrogram
from pyrogram import Client, raw
from pyrogram.file_id import FileId, FileType

from loguru import logger
from module.download_connection import DownloadConnection, close_download_connections
from module.parallel_download import CHUNK_SIZE as MB, download_file, parts_path, discard_partial_download
from module.parallel_download import DownloadWorkersExhausted
from module.pyrogram_extension import HookClient


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ramp = patch("module.parallel_download.CONNECTION_START_INTERVAL", 0)
        ramp.start()
        self.addCleanup(ramp.stop)
        retry_wait = patch("module.parallel_download._wait_network_retry", AsyncMock())
        self.retry_wait = retry_wait.start()
        self.addCleanup(retry_wait.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "download.bin"
        self.data = b"".join(bytes([i]) * MB for i in range(8)) + b"tail"
        self.client = HookClient("offline", in_memory=True, max_concurrent_transmissions=8)
        self.addCleanup(self.client.executor.shutdown)
        self.client.storage = SimpleNamespace(
            dc_id=AsyncMock(return_value=2), auth_key=AsyncMock(return_value=b"k" * 256),
            test_mode=AsyncMock(return_value=False))
        self.client.get_dc_option = AsyncMock(return_value=SimpleNamespace(ip_address="149.154.167.51", port=443))
        self.client.proxy = {"scheme": "socks5", "hostname": "offline", "port": 1080}
        self.client.invoke = AsyncMock(return_value=SimpleNamespace(id=1, bytes=b"authorization"))
        self.existing = SimpleNamespace(stop=AsyncMock())
        self.client.media_sessions[2] = self.existing
        self.file_id = FileId(file_type=FileType.DOCUMENT, dc_id=2,
                              media_id=1, access_hash=2, file_reference=b"reference")
        self.sessions = []
        self.active = set()
        self.start_gate = None
        self.request_gate = None
        self.stop_gate = None
        self.ready_gate = asyncio.Event()
        self.ready_gate.set()
        self.cdn = False
        self.corrupt_hash = False
        self.rpc_options = []
        owner = self

        class Transport:
            def __init__(self, **kwargs):
                self.session = None

            async def close(self):
                if self.session is not None:
                    self.session.closed = True
                    owner.active.discard(self.session)

        self.client.connection_factory = Transport

        class MediaSession:
            def __init__(self, client, dc_id, server_address, port, auth_key, test_mode,
                         is_media=False, is_cdn=False):
                self.client = client
                self.dc_id = dc_id
                self.auth_key = auth_key
                self.is_cdn = is_cdn
                self.connection = client.connection_factory(dc_id=dc_id, media=True)
                self.connection.session = self
                self.stop_started = False
                self.closed = False
                owner.sessions.append(self)

            async def start(self):
                owner.active.add(self)
                if owner.start_gate:
                    await owner.start_gate.wait()

            async def stop(self):
                self.stop_started = True
                if owner.stop_gate:
                    await owner.stop_gate.wait()
                await self.connection.close()

            async def invoke(self, query, **kwargs):
                owner.rpc_options.append(kwargs)
                if isinstance(query, raw.functions.auth.ImportAuthorization):
                    return True
                if owner.request_gate:
                    await owner.request_gate.wait()
                if isinstance(query, raw.functions.upload.GetFile) and owner.cdn:
                    return raw.types.upload.FileCdnRedirect(
                        dc_id=4, file_token=b"file", encryption_key=b"k" * 32,
                        encryption_iv=b"i" * 16, file_hashes=[])
                if getattr(owner, "network_failure", False):
                    if getattr(owner, "fail_once", False):
                        owner.network_failure = False
                    raise ConnectionError("Offline interrupted transfer")
                chunk = owner.data[query.offset:query.offset + MB]
                if isinstance(query, raw.functions.upload.GetCdnFile):
                    return raw.types.upload.CdnFile(bytes=chunk)
                if isinstance(query, raw.functions.upload.GetCdnFileHashes):
                    return [raw.types.FileHash(offset=query.offset, limit=len(chunk),
                                              hash=bytes(32) if owner.corrupt_hash else hashlib.sha256(chunk).digest())]
                return raw.types.upload.File(type=raw.types.storage.FileUnknown(),
                                             mtime=0, bytes=chunk)

        self.auth = AsyncMock(return_value=b"a" * 256)
        for item in (
            patch("module.download_connection.Session", MediaSession),
            patch("module.download_connection.Auth", return_value=SimpleNamespace(create=self.auth)),
            patch("pyrogram.client.aes.ctr256_decrypt", side_effect=lambda chunk, *_: chunk),
        ):
            item.start()
            self.addCleanup(item.stop)

    async def ready(self):
        await self.ready_gate.wait()

    async def progress(self, current, total):
        pass

    async def download(self, path=None):
        return await download_file(self.client, self.file_id, len(self.data),
                                   str(path or self.path), "identity", 4,
                                   self.progress, self.ready, 2 * MB)

    async def until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait(), 2)

    def assert_closed(self):
        self.assertFalse(self.active)
        self.assertTrue(all(session.closed for session in self.sessions))
        self.assertEqual(self.client._independent_download_connections, set())
        self.assertIs(self.client.media_sessions[2], self.existing)
        self.existing.stop.assert_not_awaited()

    async def assert_startup_wait(self, stage):
        records = []
        sink = logger.add(lambda message: records.append(message.record), level="DEBUG")
        self.addCleanup(lambda: logger.remove(sink))
        self.file_id.dc_id = 4
        gate = asyncio.Event()
        if stage == "starting media session":
            self.start_gate = gate
        elif stage == "GetFile":
            self.request_gate = gate
        target, method = {
            "resolving DC endpoint": (self.client, "get_dc_option"),
            "creating auth key": (self.auth, "side_effect"),
            "exporting authorization": (self.client, "invoke"),
        }.get(stage, (self, "unused_gate"))

        async def blocked(*args, **kwargs):
            await gate.wait()

        replacement = blocked if stage == "creating auth key" else AsyncMock(side_effect=blocked)
        with patch.object(target, method, replacement, create=True), patch(
                "module.download_connection.STARTUP_WARNING_INTERVAL", 0.01):
            task = asyncio.create_task(self.download())
            try:
                await self.until(lambda: any(
                    record["level"].name == "WARNING" and f"stage={stage}," in record["message"]
                    for record in records))
                self.assertFalse(task.done())
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
        self.assert_closed()
        self.assertTrue(any("before first data" in record["message"] for record in records))
        self.assertFalse(any("Opened independent" in record["message"] for record in records))
        self.assertFalse(any(task.get_coro().__qualname__.endswith("warn_startup_wait")
                             for task in asyncio.all_tasks() if not task.done()))
        self.start_gate = self.request_gate = None

    async def test_endpoint_wait_reports_stage_and_cancellation_stops_monitor(self):
        await self.assert_startup_wait("resolving DC endpoint")

    async def test_auth_key_wait_reports_stage_and_cancellation_stops_monitor(self):
        await self.assert_startup_wait("creating auth key")

    async def test_session_wait_reports_stage_and_cancellation_stops_monitor(self):
        await self.assert_startup_wait("starting media session")

    async def test_export_wait_reports_stage_and_cancellation_stops_monitor(self):
        await self.assert_startup_wait("exporting authorization")

    async def test_request_wait_reports_stage_and_cancellation_stops_monitor(self):
        await self.assert_startup_wait("GetFile")

    async def test_first_data_stops_warning_monitor_and_traces_authorization_without_payloads(self):
        records = []
        sink = logger.add(lambda message: records.append(message.record), level="DEBUG")
        self.addCleanup(lambda: logger.remove(sink))
        self.file_id.dc_id = 4
        with patch("module.download_connection.STARTUP_WARNING_INTERVAL", 0.01):
            async with DownloadConnection(self.client, "offline worker") as connection:
                stream = connection.get_file(self.file_id, len(self.data), 1, 0, self.progress)
                try:
                    self.assertEqual(await anext(stream), self.data[:MB])
                    self.assertFalse(connection.download_client.diagnosing)
                    await asyncio.sleep(0.03)
                    self.assertFalse(any(record["level"].name == "WARNING" for record in records))
                    self.assertFalse(any(task.get_coro().__qualname__.endswith("warn_startup_wait")
                                         for task in asyncio.all_tasks() if not task.done()))
                finally:
                    await stream.aclose()
        messages = "\n".join(record["message"] for record in records)
        for stage in ("creating auth key", "starting media session", "exporting authorization",
                      "importing authorization", "GetFile"):
            self.assertIn(stage, messages)
        self.assertNotIn(repr(b"authorization"), messages)
        self.assertNotIn(repr(b"a" * 256), messages)
        self.assertNotIn(repr(b"reference"), messages)
        self.assert_closed()

    async def test_unicode_directory_and_filename_complete_part_download(self):
        path = self.path.parent / "XXX全球优质集合地 ｜XXX🐝" / "飞，一.mov"
        await self.download(path)
        self.assertEqual(path.read_bytes(), self.data)
        self.assertFalse(parts_path(str(path)).exists())
        self.assert_closed()

    async def test_two_files_in_same_dc_use_eight_independent_connections(self):
        # A shared SDK semaphore must not silently turn ranges into one TCP stream.
        self.client.get_file_semaphore = asyncio.Semaphore(1)
        self.request_gate = asyncio.Event()
        second = self.path.with_name("second.bin")
        tasks = [asyncio.create_task(self.download(path)) for path in (self.path, second)]
        try:
            await self.until(lambda: len(self.active) == 8)
            self.assertEqual(len({id(session.connection) for session in self.sessions}), 8)
            self.assertEqual(len({id(session.client.media_sessions) for session in self.sessions}), 8)
            self.assertTrue(all(session.client.storage is self.client.storage for session in self.sessions))
            self.assertTrue(all(session.client.proxy is self.client.proxy for session in self.sessions))
        finally:
            self.request_gate.set()
            await asyncio.gather(*tasks)
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assertEqual(second.read_bytes(), self.data)
        self.assert_closed()

    async def test_cancel_hung_request_closes_all_connections_and_keeps_parts(self):
        self.request_gate = asyncio.Event()
        task = asyncio.create_task(self.download())
        await self.until(lambda: len(self.active) == 4)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assert_closed()
        self.assertTrue((parts_path(str(self.path)) / "manifest.json").exists())

    async def test_explicit_cancel_deletes_staging_only_after_connections_close(self):
        self.request_gate = asyncio.Event()
        discard = False
        task = asyncio.create_task(download_file(
            self.client, self.file_id, len(self.data), str(self.path), "identity", 4,
            self.progress, self.ready, 2 * MB, discard_requested=lambda: discard))
        await self.until(lambda: len(self.active) == 4)
        self.path.write_bytes(b"staged output")
        Path(str(self.path) + ".temp").write_bytes(b"merge scratch")
        completed = self.path.with_name("completed.bin")
        completed.write_bytes(b"keep")

        def clean(path):
            self.assert_closed()
            discard_partial_download(path)

        with patch("module.parallel_download.discard_partial_download", side_effect=clean):
            discard = True
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertFalse(self.path.exists())
        self.assertFalse(Path(str(self.path) + ".temp").exists())
        self.assertFalse(parts_path(str(self.path)).exists())
        self.assertEqual(completed.read_bytes(), b"keep")

    async def test_pause_keeps_the_same_four_connections(self):
        self.request_gate = asyncio.Event()
        paused = asyncio.Event()

        async def progress(current, total):
            if current and not paused.is_set():
                self.ready_gate.clear()
                paused.set()

        self.progress = progress
        task = asyncio.create_task(self.download())
        try:
            await self.until(lambda: len(self.active) == 4)
            original = {id(session.connection) for session in self.active}
            self.request_gate.set()
            await asyncio.wait_for(paused.wait(), 2)
            await asyncio.sleep(0.02)
            self.assertEqual({id(session.connection) for session in self.active}, original)
            self.assertEqual(len(self.sessions), 4)
        finally:
            self.ready_gate.set()
            self.request_gate.set()
            await task
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assert_closed()

    async def test_single_range_files_also_use_separate_connections(self):
        self.request_gate = asyncio.Event()
        paths = [self.path, self.path.with_name("second.bin")]
        tasks = [asyncio.create_task(download_file(
            self.client, self.file_id, len(self.data), str(path), "identity", 1,
            self.progress, self.ready, MB)) for path in paths]
        try:
            await self.until(lambda: len(self.active) == 2)
            self.assertEqual(len({id(session.connection) for session in self.sessions}), 2)
        finally:
            self.request_gate.set()
            await asyncio.gather(*tasks)
        self.assertTrue(all(path.read_bytes() == self.data for path in paths))
        self.assert_closed()

    async def test_cancel_during_session_start_closes_partially_started_connections(self):
        self.start_gate = asyncio.Event()
        task = asyncio.create_task(self.download())
        await self.until(lambda: len(self.active) == 4)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assert_closed()

    async def test_second_cancel_does_not_interrupt_socket_cleanup(self):
        self.request_gate = asyncio.Event()
        self.stop_gate = asyncio.Event()
        task = asyncio.create_task(self.download())
        await self.until(lambda: len(self.active) == 4)
        task.cancel()
        await self.until(lambda: all(session.stop_started for session in self.sessions))
        task.cancel()
        self.stop_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await self.until(lambda: not self.active)
        self.assert_closed()

    async def test_cdn_sockets_are_closed_even_if_second_cancel_interrupts_sdk_cleanup(self):
        self.cdn = True
        entered = asyncio.Event()
        release = asyncio.Event()

        async def progress(current, total):
            if current:
                entered.set()
                await release.wait()

        self.progress = progress
        self.stop_gate = asyncio.Event()
        task = asyncio.create_task(self.download())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await self.until(lambda: any(session.is_cdn and session.stop_started for session in self.sessions))
        task.cancel()
        self.stop_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await self.until(lambda: not self.active)
        self.assert_closed()

    async def test_client_restart_releases_connections_and_file_can_retry(self):
        self.request_gate = asyncio.Event()
        task = asyncio.create_task(self.download())
        await self.until(lambda: len(self.active) == 4)
        await close_download_connections(self.client)
        with self.assertRaises(ConnectionError):
            await task
        self.assert_closed()
        with self.assertRaises(ConnectionError):
            await self.download()
        self.client._download_connections_closing = False
        self.request_gate.set()
        await self.download()
        self.assertEqual(len(self.sessions), 8)
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assert_closed()

    async def test_transient_part_error_uses_fresh_session_without_file_retry(self):
        self.network_failure = True
        self.fail_once = True
        await self.download()
        self.retry_wait.assert_not_awaited()
        self.assertLessEqual(len(self.sessions), 4)
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assert_closed()

    async def test_failed_attempt_closes_sessions_before_checkpoint_retry(self):
        self.network_failure = True
        with self.assertRaises(ConnectionError):
            await self.download()
        self.assert_closed()
        previous = list(self.sessions)
        self.assertTrue(previous)
        self.assertTrue(parts_path(str(self.path)).exists())
        self.network_failure = False
        await self.download()
        self.assertGreater(len(self.sessions), len(previous))
        self.assertTrue(all(session not in previous for session in self.sessions[len(previous):]))
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assert_closed()

    async def test_hook_terminate_closes_downloads_before_account_shutdown(self):
        self.request_gate = asyncio.Event()
        task = asyncio.create_task(self.download())
        await self.until(lambda: len(self.active) == 4)

        async def terminate(**kwargs):
            self.assert_closed()

        with patch.object(Client, "terminate", AsyncMock(side_effect=terminate)) as base:
            await self.client.terminate()
            base.assert_awaited_once_with(clear_handlers=True)
        with self.assertRaises(ConnectionError):
            await task
        self.assertTrue(self.client._download_connections_closing)
        self.assert_closed()

    async def test_foreign_dc_authorizes_each_independent_session(self):
        self.file_id.dc_id = 4
        await self.download()
        self.assertEqual(len(self.sessions), 4)
        self.assertEqual(self.auth.await_count, 4)
        self.assertEqual(self.client.invoke.await_count, 4)
        self.assertTrue(all(call.kwargs["sleep_threshold"] == 0
                            for call in self.client.invoke.await_args_list))
        self.assertTrue(all(options.get("sleep_threshold") == 0 for options in self.rpc_options))
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assert_closed()

    async def test_cdn_download_keeps_hash_validation_and_closes_both_session_types(self):
        self.cdn = True
        await self.download()
        self.assertEqual(len(self.sessions), 9)
        self.assertEqual(sum(session.is_cdn for session in self.sessions), 5)
        self.assertTrue(all(session.dc_id == 4 for session in self.sessions if session.is_cdn))
        self.assertTrue(all(session.dc_id == 2 for session in self.sessions if not session.is_cdn))
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assert_closed()

    async def test_cdn_hash_mismatch_preserves_parts_and_closes_connections(self):
        self.cdn = True
        self.corrupt_hash = True
        with self.assertRaises(DownloadWorkersExhausted) as result:
            await self.download()
        self.assertIsInstance(result.exception.error, pyrogram.errors.CDNFileHashMismatch)
        self.assertFalse(self.path.exists())
        self.assertTrue((parts_path(str(self.path)) / "manifest.json").exists())
        self.assert_closed()

    async def test_foreign_dc_authorization_retries_are_bounded_and_cleaned_up(self):
        self.file_id.dc_id = 4
        self.request_gate = None
        self.client.invoke.return_value = SimpleNamespace(id=1, bytes=b"authorization")
        from module.download_connection import DownloadConnection
        # Reject the import on the actual worker-owned session, before any bytes.
        with patch("module.download_connection.Session.invoke",
                   AsyncMock(side_effect=pyrogram.errors.AuthBytesInvalid)):
            async with DownloadConnection(self.client, "offline-auth") as connection:
                with self.assertRaises(pyrogram.errors.AuthBytesInvalid):
                    async for _ in connection.get_file(self.file_id, len(self.data), 1, 0, self.progress):
                        pass
        self.assertEqual(self.client.invoke.await_count, 3)
        self.assert_closed()


if __name__ == "__main__":
    unittest.main()
