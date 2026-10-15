"""Offline range, checkpoint and concurrency tests; no Telegram login needed."""

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from module.parallel_download import CHUNK_SIZE as MB
from module.parallel_download import download_file, parts_path, part_count, _wait_network_retry


class Stopped(Exception):
    pass


class FakeClient:
    def __init__(self, data, tracker=None):
        self.data = data
        self.calls = []
        self.before_chunk = None
        self.early_end = None
        self.short_chunk = None
        self.tracker = tracker if tracker is not None else {"active": 0, "peak": 0}

    async def get_file(self, file_id, size, limit, offset, progress=None):
        self.calls.append((offset, limit))
        self.tracker["active"] += 1
        self.tracker["peak"] = max(self.tracker["peak"], self.tracker["active"])
        try:
            for index in range(offset, offset + limit):
                if self.before_chunk:
                    await self.before_chunk(index)
                await asyncio.sleep(0)
                if index == self.early_end:
                    return  # Pyrogram can swallow an exception and end the stream.
                chunk = self.data[index * MB:(index + 1) * MB]
                if index == self.short_chunk:
                    chunk = chunk[:-1]
                yield chunk
                if progress:
                    await progress(min((index + 1) * MB, size), size)
        finally:
            self.tracker["active"] -= 1


class ParallelDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        retry_wait = patch("module.parallel_download._wait_network_retry", AsyncMock())
        self.retry_wait = retry_wait.start()
        self.addCleanup(retry_wait.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "download.bin"
        self.data = b"".join(bytes([index]) * MB for index in range(8)) + b"tail" * 37
        self.client = FakeClient(self.data)
        self.progress = []
        self.ready = asyncio.Event()
        self.ready.set()
        self.stopped = False

    async def wait_ready(self):
        if self.stopped:
            raise Stopped()
        await self.ready.wait()
        if self.stopped:
            raise Stopped()

    async def report(self, current, total):
        self.assertEqual(total, len(self.data))
        self.progress.append(current)

    async def download(self, count=3, part_size=3 * MB):
        return await download_file(self.client, "file-id", len(self.data), str(self.path),
                                   "file-identity", count,
                                   self.report, self.wait_ready, part_size)

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait(), 2)

    def assert_file(self):
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(),
                         hashlib.sha256(self.data).digest())

    async def test_part_backoff_checks_controls_each_second(self):
        ready = AsyncMock(side_effect=[None, Stopped()])
        with patch("module.parallel_download.asyncio.sleep", AsyncMock()) as sleep:
            with self.assertRaises(Stopped):
                await _wait_network_retry(30, ready)
        sleep.assert_awaited_once_with(1)

    async def test_each_part_retries_without_exhausting_file_attempt(self):
        failed = set()
        async def before(index):
            if index not in failed:
                failed.add(index)
                raise ConnectionError("Transient connection failure")
        self.client.before_chunk = before
        # Each part has its own budget, so more than three errors across the
        # file do not exhaust the file attempt. Parts resume completed chunks.
        await self.download(count=1, part_size=MB)
        self.assert_file()
        self.assertGreater(self.retry_wait.await_count, 3)
        self.assertEqual(self.client.tracker["active"], 0)

    async def test_part_retry_does_not_cancel_healthy_worker(self):
        blocked = asyncio.Event()
        release = asyncio.Event()
        failed = False
        async def before(index):
            nonlocal failed
            if index == 0 and not failed:
                failed = True
                raise ConnectionError("Transient connection failure")
        async def wait(*args):
            blocked.set()
            await release.wait()
        self.client.before_chunk = before
        self.retry_wait.side_effect = wait
        task = asyncio.create_task(self.download(count=2, part_size=MB))
        try:
            await asyncio.wait_for(blocked.wait(), 2)
            await self.until(lambda: len(self.client.calls) >= 9)
            self.assertFalse(task.done())
            release.set()
            await task
            self.assert_file()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_persistent_part_failure_has_bounded_attempts(self):
        self.client.early_end = 0
        with self.assertRaises(ConnectionError):
            await self.download(count=1, part_size=MB)
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(self.retry_wait.await_count, 2)
        self.assertTrue(parts_path(str(self.path)).exists())

    async def test_out_of_order_ranges_merge_to_identical_file_and_aggregate_progress(self):
        release_first = asyncio.Event()

        async def before(index):
            if index == 0:
                await release_first.wait()
            if index == 3:
                release_first.set()
        self.client.before_chunk = before
        await self.download()
        self.assert_file()
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(self.progress, sorted(self.progress))
        self.assertEqual(self.progress[-1], len(self.data))
        self.assertGreater(self.client.tracker["peak"], 1)
        self.assertFalse(parts_path(str(self.path)).exists())

    async def test_fast_worker_takes_next_parts_while_slow_worker_keeps_its_part(self):
        release = asyncio.Event()

        async def before(index):
            if index == 0:
                await release.wait()
        self.client.before_chunk = before
        task = asyncio.create_task(self.download(count=2, part_size=MB))
        try:
            await self.until(lambda: len(self.client.calls) == 9
                             and self.client.tracker["active"] == 1)
            self.assertEqual([offset for offset, _ in self.client.calls], list(range(9)))
            self.assertEqual(self.client.tracker["peak"], 2)
            plan = json.loads((parts_path(str(self.path)) / "manifest.json").read_text())
            self.assertEqual(plan["workers"], 2)
            self.assertEqual(plan["ranges"], [[i * MB, min((i + 1) * MB, len(self.data))]
                                             for i in range(9)])
        finally:
            release.set()
            await task
        self.assert_file()

    async def test_multiple_files_start_their_full_ranges_independently(self):
        tracker = {"active": 0, "peak": 0}
        clients = [FakeClient(self.data, tracker) for _ in range(2)]
        release = asyncio.Event()
        async def block(_):
            await release.wait()
        for client in clients:
            client.before_chunk = block
        tasks = []
        try:
            for index, client in enumerate(clients):
                tasks.append(asyncio.create_task(download_file(
                    client, "id", len(self.data), str(self.path) + str(index),
                    "identity", 4, self.report, self.wait_ready, 2 * MB)))
            await self.until(lambda: tracker["active"] == 8)
            self.assertEqual([len(client.calls) for client in clients], [4, 4])
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(tracker["peak"], 8)
        for index in range(2):
            self.assertEqual(Path(str(self.path) + str(index)).read_bytes(), self.data)

    async def test_pause_resumes_same_ranges(self):
        paused = asyncio.Event()
        paused_once = False
        original = self.report
        async def report(current, total):
            nonlocal paused_once
            await original(current, total)
            if current and not paused_once:
                paused_once = True
                self.ready.clear()
                paused.set()
        self.report = report
        task = asyncio.create_task(self.download())
        try:
            await asyncio.wait_for(paused.wait(), 2)
            await asyncio.sleep(0.01)
            calls = list(self.client.calls)
            progress = list(self.progress)
            await asyncio.sleep(0.02)
            self.assertEqual(self.client.calls, calls)
            self.assertEqual(self.progress, progress)
        finally:
            self.ready.set()
            await task
        self.assertEqual(len(self.client.calls), 3)
        self.assert_file()

    async def test_early_stream_end_keeps_ranges_and_resume_ignores_new_file_settings(self):
        self.client.early_end = 1
        with self.assertRaises(ConnectionError):
            await self.download()
        directory = parts_path(str(self.path))
        plan = (directory / "manifest.json").read_text()
        self.assertEqual((directory / "0.part").stat().st_size, MB)
        self.assertFalse(self.path.exists())
        self.client.early_end = None
        self.client.calls.clear()
        # Restart with a new count and a part size larger than the entire file.
        await self.download(count=1, part_size=100 * MB)
        self.assert_file()
        self.assertNotIn((0, 3), self.client.calls)
        self.assertEqual(len(json.loads(plan)["ranges"]), 3)
        self.assertTrue(any(offset == 1 for offset, _ in self.client.calls))

    async def test_short_block_is_not_written_or_reported_as_complete(self):
        self.client.short_chunk = 1
        with self.assertRaises(ConnectionError):
            await self.download()
        self.assertEqual((parts_path(str(self.path)) / "0.part").stat().st_size, MB)
        self.assertNotIn(len(self.data), self.progress)
        self.client.short_chunk = None
        await self.download()
        self.assert_file()

    async def test_task_cancellation_closes_streams_and_preserves_parts(self):
        blocked = asyncio.Event()
        async def before(index):
            if index == 1:
                blocked.set()
                await asyncio.Event().wait()
        self.client.before_chunk = before
        task = asyncio.create_task(self.download())
        await asyncio.wait_for(blocked.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client.tracker["active"], 0)
        self.assertEqual((parts_path(str(self.path)) / "0.part").stat().st_size, MB)
        self.client.before_chunk = None
        await self.download()
        self.assert_file()

    async def test_hard_process_exit_can_resume_from_flushed_parts(self):
        script = '''
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/"tests"))
from test_parallel_download import FakeClient, MB
from module.parallel_download import download_file
data=b"".join(bytes([i])*MB for i in range(8))+b"tail"*37
async def progress(current,total):
    if current>=3*MB:
        os._exit(86)
async def ready():
    return
asyncio.run(download_file(FakeClient(data),"file-id",len(data),sys.argv[1],
                          "file-identity",3,progress,ready,MB))
'''
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.path)],
                                cwd=Path(__file__).resolve().parents[1], timeout=10,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 86, result.stderr)
        saved = sum(p.stat().st_size for p in parts_path(str(self.path)).glob("*.part"))
        self.assertGreaterEqual(saved, 3 * MB)
        await self.download(count=1, part_size=100 * MB)
        self.assertEqual(self.progress[0], saved)
        self.assert_file()

    async def test_stop_interrupts_hung_network_request(self):
        entered = asyncio.Event()
        async def before(_):
            entered.set()
            await asyncio.Event().wait()
        self.client.before_chunk = before
        task = asyncio.create_task(self.download())
        await asyncio.wait_for(entered.wait(), 2)
        self.stopped = True
        with self.assertRaises(Stopped):
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.client.tracker["active"], 0)

    async def test_cancel_while_waiting_for_same_file_lock(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def block(_):
            entered.set()
            await release.wait()
        self.client.before_chunk = block
        first = asyncio.create_task(self.download())
        second_stopped = False
        async def second_ready():
            if second_stopped:
                raise Stopped()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(download_file(
                self.client, "file-id", len(self.data), str(self.path),
                "file-identity", 4, self.report, second_ready))
            await asyncio.sleep(0.01)
            second_stopped = True
            with self.assertRaises(Stopped):
                await asyncio.wait_for(second, 2)
            self.assertFalse(first.done())
            self.assertEqual(len(self.client.calls), 3)
        finally:
            release.set()
            await first

    async def test_discard_waiting_for_file_lock_does_not_delete_other_active_download(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def block(_):
            entered.set()
            await release.wait()

        self.client.before_chunk = block
        first = asyncio.create_task(self.download())
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(download_file(
            self.client, "file-id", len(self.data), str(self.path), "file-identity", 4,
            self.report, self.wait_ready, MB, discard_requested=lambda: True))
        try:
            await asyncio.sleep(0.01)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            self.assertTrue(parts_path(str(self.path)).exists())
            self.assertFalse(first.done())
        finally:
            release.set()
            await first
        self.assert_file()

    async def test_import_old_sequential_checkpoint_and_refetch_incomplete_block(self):
        Path(str(self.path) + ".temp").write_bytes(self.data[:MB + 100])
        await self.download()
        self.assert_file()
        self.assertEqual(self.progress[0], MB)
        self.assertTrue(all(offset != 0 for offset, _ in self.client.calls))

    async def test_legacy_fixed_range_manifest_resumes_without_repartitioning(self):
        directory = parts_path(str(self.path))
        directory.mkdir()
        ranges = [[0, 3 * MB], [3 * MB, 6 * MB], [6 * MB, len(self.data)]]
        (directory / "manifest.json").write_text(json.dumps({
            "version": 1, "size": len(self.data), "identity": "file-identity",
            "ranges": ranges, "import_pending": False}))
        (directory / "0.part").write_bytes(self.data[:MB])
        await self.download(count=1, part_size=64 * MB)
        self.assertEqual(self.progress[0], MB)
        self.assertEqual(sorted(self.client.calls), [(1, 2), (3, 3), (6, 3)])
        self.assert_file()

    async def test_fixed_size_parts_include_short_final_part(self):
        await self.download(count=4, part_size=3 * MB)
        self.assertEqual(len(self.client.calls), 3)
        self.assert_file()

    async def test_single_range_checkpoint_survives_callback_interruption(self):
        original = self.report
        async def interrupted(current, total):
            if current == total:
                raise Stopped()
            await original(current, total)
        self.report = interrupted
        with self.assertRaises(Stopped):
            await self.download(count=1, part_size=100 * MB)
        self.assertTrue(self.path.exists())
        self.client.calls.clear()
        self.report = original
        await self.download(count=4)
        self.assertEqual(self.client.calls, [])
        self.assert_file()

    async def test_incomplete_part_tail_is_truncated_before_resume(self):
        self.client.early_end = 1
        with self.assertRaises(ConnectionError):
            await self.download()
        part = parts_path(str(self.path)) / "0.part"
        with part.open("ab") as file:
            file.write(b"incomplete disk write")
        self.client.early_end = None
        await self.download()
        self.assert_file()

    async def test_interrupted_merge_can_resume_without_redownloading_completed_parts(self):
        initial_wait = self.wait_ready
        async def wait():
            await initial_wait()
            legacy = Path(str(self.path) + ".temp")
            if legacy.exists() and legacy.stat().st_size > 0:
                raise Stopped()
        self.wait_ready = wait
        with self.assertRaises(Stopped):
            await self.download()
        self.assertNotIn(len(self.data), self.progress)
        self.assertTrue(parts_path(str(self.path)).exists())
        self.wait_ready = initial_wait
        self.client.calls.clear()
        await self.download()
        self.assertEqual(self.client.calls, [])
        self.assert_file()

    async def test_checkpoint_for_different_file_is_rejected_without_overwriting(self):
        self.client.early_end = 1
        with self.assertRaises(ConnectionError):
            await self.download()
        directory = parts_path(str(self.path))
        manifest = directory / "manifest.json"
        plan = json.loads(manifest.read_text())
        plan["identity"] = "another-file"
        manifest.write_text(json.dumps(plan))
        before = (directory / "0.part").read_bytes()
        with self.assertRaises(ValueError):
            await self.download()
        self.assertEqual((directory / "0.part").read_bytes(), before)


class PartCountTests(unittest.TestCase):
    def test_fixed_size_part_count_includes_short_tail(self):
        for size, expected in ((10, 1), (64, 1), (65, 2), (128, 2), (129, 3), (256, 4)):
            with self.subTest(size=size):
                self.assertEqual(part_count(size * MB, 64 * MB), expected)
        self.assertEqual(part_count(64 * MB + 1, 64 * MB), 2)


if __name__ == "__main__":
    unittest.main()
