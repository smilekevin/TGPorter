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

from module.download_connection import close_download_connections
from module.parallel_download import CHUNK_SIZE as MB, download_file, parts_path, discard_partial_download
from module.pyrogram_extension import HookClient


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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
                if isinstance(query, raw.functions.auth.ImportAuthorization):
                    return True
                if owner.request_gate:
                    await owner.request_gate.wait()
                if isinstance(query, raw.functions.upload.GetFile) and owner.cdn:
                    return raw.types.upload.FileCdnRedirect(
                        dc_id=4, file_token=b"file", encryption_key=b"k" * 32,
                        encryption_iv=b"i" * 16, file_hashes=[])
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
        with self.assertRaises(pyrogram.errors.CDNFileHashMismatch):
            await self.download()
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
