"""Offline range, checkpoint and concurrency tests; no Telegram login needed."""

import asyncio
import errno
import os
import threading
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
from module.parallel_download import DownloadWorkersExhausted
from module.telegram import pyrogram


def checkpoint_lengths(path):
    return json.loads((parts_path(str(path)) / "manifest.json").read_text())["committed_bytes"]


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
        ramp = patch("module.parallel_download.CONNECTION_START_INTERVAL", 0)
        ramp.start()
        self.addCleanup(ramp.stop)
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

    async def download(self, count=3, part_size=3 * MB, **kwargs):
        return await download_file(self.client, "file-id", len(self.data), str(self.path),
                                   "file-identity", count,
                                   self.report, self.wait_ready, part_size, **kwargs)

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
        # Completing a part resets the last worker's counter, so errors across
        # separate successful parts do not exhaust the file attempt.
        await self.download(count=1, part_size=MB)
        self.assert_file()
        self.assertGreater(self.retry_wait.await_count, 3)
        self.assertEqual(self.client.tracker["active"], 0)

    async def test_failed_connections_retire_while_survivor_finishes_their_parts(self):
        healthy = None
        failed = set()
        failed_both = asyncio.Event()

        async def before(index):
            nonlocal healthy
            task = asyncio.current_task()
            if index == 0 and healthy is None:
                healthy = task
            if task is healthy and index == 1:
                await failed_both.wait()
            if task is not healthy:
                failed.add(task)
                if len(failed) == 2:
                    failed_both.set()
                raise pyrogram.errors.AuthBytesInvalid()

        self.client.before_chunk = before
        await asyncio.wait_for(self.download(count=3, part_size=MB), 2)
        self.assert_file()
        self.assertEqual(len(failed), 2)
        self.retry_wait.assert_not_awaited()
        self.assertEqual(self.client.tracker["active"], 0)

    async def test_persistent_part_failure_has_bounded_attempts(self):
        async def before(_):
            raise ConnectionError("Persistent offline failure")
        self.client.before_chunk = before
        with self.assertRaises(DownloadWorkersExhausted):
            await self.download(count=1, part_size=MB)
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(self.retry_wait.await_count, 2)
        self.assertTrue(parts_path(str(self.path)).exists())

    async def test_first_valid_block_is_required_before_next_connection(self):
        from module.partial_file import PartialFile
        writes = []
        real_write = PartialFile.write_at
        def write(file, offset, data):
            real_write(file, offset, data)
            writes.append(offset)
        writer = patch.object(PartialFile, "write_at", write)
        writer.start()
        self.addCleanup(writer.stop)
        started = []
        grants = [asyncio.Event() for _ in range(3)]
        release = asyncio.Event()
        healthy_progress = asyncio.Event()
        async def before(index):
            task = asyncio.current_task()
            if task not in started:
                position = len(started)
                started.append(task)
                await grants[position].wait()
            elif task is started[0] and index == 1:
                await healthy_progress.wait()
            else:
                await release.wait()
        self.client.before_chunk = before
        task = asyncio.create_task(self.download(count=3))
        try:
            await self.until(lambda: len(started) >= 1)
            await asyncio.sleep(0.02)
            self.assertEqual(len(self.client.calls), 1)
            grants[0].set()
            await self.until(lambda: len(started) >= 2)
            self.assertIn(0, writes)
            healthy_progress.set()
            await self.until(lambda: MB in writes)
            await asyncio.sleep(0.02)
            self.assertEqual(len(self.client.calls), 2)
            grants[1].set()
            await self.until(lambda: len(started) >= 3)
            self.assertEqual(self.client.tracker["active"], 3)
        finally:
            release.set()
            healthy_progress.set()
            for grant in grants:
                grant.set()
            await task
        self.assert_file()

    async def test_connections_start_gradually(self):
        starts = []
        blocked = asyncio.Event()

        seen = set()
        async def before(_):
            task = asyncio.current_task()
            if task not in seen:
                seen.add(task)
                starts.append(asyncio.get_running_loop().time())
            else:
                await blocked.wait()

        self.client.before_chunk = before
        with patch("module.parallel_download.CONNECTION_START_INTERVAL", 0.04):
            task = asyncio.create_task(self.download(count=3))
            try:
                await self.until(lambda: len(starts) == 1)
                await asyncio.sleep(0.015)
                self.assertEqual(len(starts), 1)
                await self.until(lambda: len(starts) == 3)
                self.assertTrue(all(b - a >= 0.035 for a, b in zip(starts, starts[1:])))
            finally:
                blocked.set()
                await task
        self.assert_file()

    async def test_only_last_worker_gets_three_attempts(self):
        async def before(_):
            raise pyrogram.errors.AuthBytesInvalid()
        self.client.before_chunk = before
        with self.assertRaises(DownloadWorkersExhausted) as result:
            await self.download(count=3)
        self.assertIsInstance(result.exception.error, pyrogram.errors.AuthBytesInvalid)
        self.assertEqual(len(self.client.calls), 5)
        self.assertEqual(self.retry_wait.await_count, 2)
        self.assertEqual(self.client.tracker["active"], 0)

    async def test_flood_stops_expansion_but_existing_connection_finishes(self):
        throttled = asyncio.Event()
        release = asyncio.Event()
        healthy = None

        async def before(index):
            nonlocal healthy
            if index == 0 and healthy is None:
                healthy = asyncio.current_task()
            if asyncio.current_task() is healthy and index == 1:
                await release.wait()
            if asyncio.current_task() is not healthy:
                raise pyrogram.errors.FloodWait(1)

        self.client.before_chunk = before
        with patch("module.parallel_download.CONNECTION_START_INTERVAL", 0.03):
            task = asyncio.create_task(download_file(
                self.client, "file-id", len(self.data), str(self.path), "file-identity", 3,
                self.report, self.wait_ready, 3 * MB,
                flood_wait=lambda _: throttled.set()))
            try:
                await asyncio.wait_for(throttled.wait(), 2)
                await asyncio.sleep(0.07)
                self.assertEqual(len(self.client.calls), 2)
                self.assertFalse(task.done())
                release.set()
                await asyncio.wait_for(task, 0.5)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assert_file()
        self.retry_wait.assert_not_awaited()

    async def test_all_flooded_connections_resume_after_extended_cooldown(self):
        first = asyncio.Event()
        second = asyncio.Event()
        starts = []
        failed = set()
        deadlines = []

        async def before(index):
            task = asyncio.current_task()
            if index in (1, 4) and task not in failed:
                failed.add(task)
                if len(failed) == 1:
                    await second.wait()
                else:
                    second.set()
                    await first.wait()
                    await asyncio.sleep(0.04)
                starts.append(asyncio.get_running_loop().time())
                raise pyrogram.errors.FloodWait(0.08 if len(starts) == 1 else 0.16)

        def flood(seconds):
            deadlines.append(asyncio.get_running_loop().time() + seconds)
            first.set()

        self.client.before_chunk = before
        task = asyncio.create_task(download_file(
            self.client, "file-id", len(self.data), str(self.path), "file-identity", 2,
            self.report, self.wait_ready, 3 * MB, flood_wait=flood))
        await asyncio.wait_for(task, 2)
        self.assertEqual(len(deadlines), 2)
        self.assertGreaterEqual(asyncio.get_running_loop().time(), max(deadlines))
        self.assert_file()
        self.retry_wait.assert_not_awaited()

    async def test_cancel_during_flood_cooldown_preserves_checkpoint(self):
        throttled = asyncio.Event()

        async def before(index):
            if index == 1:
                raise pyrogram.errors.FloodPremiumWait(900)

        self.client.before_chunk = before
        task = asyncio.create_task(download_file(
            self.client, "file-id", len(self.data), str(self.path), "file-identity", 1,
            self.report, self.wait_ready, 3 * MB, flood_wait=lambda _: throttled.set()))
        await asyncio.wait_for(throttled.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.client.tracker["active"], 0)
        self.assertEqual(checkpoint_lengths(self.path)[0], MB)

    async def test_shared_flood_cooldown_delays_other_file_connections(self):
        deadline = asyncio.get_running_loop().time() + 0.08
        other = FakeClient(self.data)
        task = asyncio.create_task(download_file(
            other, "other-id", len(self.data), str(self.path), "other-identity", 2,
            self.report, self.wait_ready, 3 * MB,
            flood_remaining=lambda: max(0, deadline - asyncio.get_running_loop().time())))
        try:
            await asyncio.sleep(0.03)
            self.assertEqual(other.calls, [])
            deadline += 0.08
            await asyncio.sleep(0.07)
            self.assertEqual(other.calls, [])
            await asyncio.wait_for(task, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assert_file()

    async def test_unused_worker_cannot_keep_creation_slot_across_flood_wait(self):
        self.data = self.data[:2 * MB]
        self.client.data = self.data
        entered = asyncio.Event()
        release = asyncio.Event()
        throttled = asyncio.Event()
        failed = False

        async def before(index):
            nonlocal failed
            if index == 1 and not failed:
                entered.set()
                await release.wait()
                failed = True
                raise pyrogram.errors.FloodWait(0.15)

        self.client.before_chunk = before
        with patch("module.parallel_download.CONNECTION_START_INTERVAL", 0.03):
            task = asyncio.create_task(download_file(
                self.client, "file-id", len(self.data), str(self.path), "file-identity", 2,
                self.report, self.wait_ready, MB, flood_wait=lambda _: throttled.set()))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.sleep(0.08)
                release.set()
                await asyncio.wait_for(throttled.wait(), 2)
                await asyncio.sleep(0.04)
                self.assertEqual(len(self.client.calls), 2)
                self.assertFalse(task.done())
                await asyncio.wait_for(task, 2)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assert_file()

    async def test_repeated_premium_flood_waits_resume_without_failure_budget(self):
        waits = 0

        async def before(index):
            nonlocal waits
            if index == 1 and waits < 4:
                waits += 1
                raise pyrogram.errors.FloodPremiumWait(0.01)

        self.client.before_chunk = before
        await asyncio.wait_for(self.download(count=1), 2)
        self.assertEqual(waits, 4)
        self.retry_wait.assert_not_awaited()
        self.assert_file()

    async def test_out_of_order_ranges_write_identical_file_and_aggregate_progress(self):
        release_first = asyncio.Event()

        async def before(index):
            if index == 1:
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
            if index == 1:
                await release.wait()
        self.client.before_chunk = before
        task = asyncio.create_task(self.download(count=2, part_size=2 * MB))
        try:
            await self.until(lambda: len(self.client.calls) == 5
                             and self.client.tracker["active"] == 1)
            self.assertEqual([offset for offset, _ in self.client.calls], list(range(0, 9, 2)))
            self.assertEqual(self.client.tracker["peak"], 2)
            plan = json.loads((parts_path(str(self.path)) / "manifest.json").read_text())
            self.assertEqual(plan["workers"], 2)
            self.assertEqual(plan["ranges"], [[i * 2 * MB, min((i + 1) * 2 * MB, len(self.data))]
                                             for i in range(5)])
        finally:
            release.set()
            await task
        self.assert_file()

    async def test_multiple_files_start_their_full_ranges_independently(self):
        tracker = {"active": 0, "peak": 0}
        clients = [FakeClient(self.data, tracker) for _ in range(2)]
        release = asyncio.Event()
        async def block(index):
            if index % 2:
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
        self.assertEqual(checkpoint_lengths(self.path)[0], MB)
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
        self.assertEqual(checkpoint_lengths(self.path)[0], MB)
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
        self.assertEqual(checkpoint_lengths(self.path)[0], MB)
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
    import json
    saved=json.loads(Path(sys.argv[1]+".parts/manifest.json").read_text())["committed_bytes"]
    if sum(saved)>=3*MB:
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
        saved = sum(checkpoint_lengths(self.path))
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
            self.assertEqual(len(self.client.calls), 1)
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

    async def test_old_sequential_checkpoint_is_preserved_and_rejected(self):
        from module.partial_file import DownloadStorageError
        path = Path(str(self.path) + ".temp")
        path.write_bytes(self.data[:MB + 100])
        with self.assertRaises(DownloadStorageError):
            await self.download()
        self.assertEqual(path.read_bytes(), self.data[:MB + 100])
        self.assertEqual(self.client.calls, [])

    async def test_old_manifest_is_preserved_and_rejected(self):
        directory = parts_path(str(self.path))
        directory.mkdir()
        manifest = json.dumps({"version": 2, "size": len(self.data),
                               "identity": "file-identity"})
        (directory / "manifest.json").write_text(manifest)
        (directory / "0.part").write_bytes(self.data[:MB])
        with self.assertRaises(ValueError):
            await self.download()
        self.assertEqual((directory / "manifest.json").read_text(), manifest)
        self.assertEqual((directory / "0.part").read_bytes(), self.data[:MB])
        self.assertEqual(self.client.calls, [])

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

    async def test_uncommitted_bytes_are_overwritten_on_resume(self):
        self.client.early_end = 1
        with self.assertRaises(ConnectionError):
            await self.download()
        partial = Path(str(self.path) + ".temp")
        with partial.open("r+b") as file:
            file.seek(MB)
            file.write(b"incomplete disk write")
        self.client.early_end = None
        await self.download()
        self.assert_file()

    async def test_interrupted_publication_resumes_without_redownloading(self):
        initial_wait = self.wait_ready
        async def wait():
            await initial_wait()
            manifest = parts_path(str(self.path)) / "manifest.json"
            if manifest.exists() and json.loads(manifest.read_text()).get("phase") == "ready":
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
        before = Path(str(self.path) + ".temp").read_bytes()
        with self.assertRaises(ValueError):
            await self.download()
        self.assertEqual(Path(str(self.path) + ".temp").read_bytes(), before)

    async def make_checkpoint(self):
        self.client.early_end = 1
        with self.assertRaises(ConnectionError):
            await self.download(count=1)
        self.client.early_end = None
        self.client.calls.clear()

    async def test_preallocated_full_length_does_not_complete_unfinished_file(self):
        await self.make_checkpoint()
        self.assertFalse(self.path.exists())
        self.assertEqual(Path(str(self.path) + ".temp").stat().st_size, len(self.data))
        self.assertEqual(checkpoint_lengths(self.path), [MB, 0, 0])
        self.assertEqual(list(parts_path(str(self.path)).glob("*.part")), [])
        await self.download()
        self.assert_file()
        self.assertNotIn((0, 3), self.client.calls)

    async def test_missing_or_truncated_data_never_recreates_committed_prefix(self):
        from module.partial_file import DownloadStorageError
        await self.make_checkpoint()
        path = Path(str(self.path) + ".temp")
        with path.open("r+b") as file:
            file.truncate(MB)
        with self.assertRaises(ValueError):
            await self.download()
        self.assertEqual(path.stat().st_size, MB)
        path.unlink()
        with self.assertRaises(DownloadStorageError):
            await self.download()
        self.assertFalse(path.exists())
        self.assertEqual(self.client.calls, [])

    async def test_invalid_checkpoint_vectors_are_rejected_before_writing(self):
        await self.make_checkpoint()
        manifest = parts_path(str(self.path)) / "manifest.json"
        plan = json.loads(manifest.read_text())
        for values in ([MB + 1, 0, 0], [4 * MB, 0, 0], [True, 0, 0], [], [-MB, 0, 0]):
            with self.subTest(values=values):
                manifest.write_text(json.dumps(dict(plan, committed_bytes=values)))
                with self.assertRaises(ValueError):
                    await self.download()
        manifest.write_text("broken json")
        with self.assertRaises(ValueError):
            await self.download()
        self.assertEqual(self.client.calls, [])

    async def test_final_callback_observes_published_output_and_can_resume_after_failure(self):
        final = self.path.parent / "final.bin"
        async def progress(current, total):
            if current == total:
                self.assertEqual(final.read_bytes(), self.data)
                self.assertFalse(self.path.exists())
                raise Stopped()
        self.report = progress
        with self.assertRaises(Stopped):
            await self.download(destination=str(final))
        self.client.calls.clear()
        self.report = lambda *_: asyncio.sleep(0)
        self.assertEqual(await self.download(destination=str(final)), str(final))
        self.assertEqual(self.client.calls, [])
        self.assertFalse(parts_path(str(self.path)).exists())

    async def test_cross_device_publish_preserves_ready_file_and_does_not_redownload(self):
        from module.partial_file import DownloadStorageError
        final = self.path.parent / "final.bin"
        real_replace = os.replace
        def replace(source, target):
            if Path(source) == self.path and Path(target) == final:
                raise OSError(errno.EXDEV, "different filesystem")
            return real_replace(source, target)
        with patch("module.parallel_download.os.replace", replace):
            with self.assertRaises(DownloadStorageError):
                await self.download(destination=str(final))
        self.assertFalse(final.exists())
        self.assert_file()
        self.assertNotIn(len(self.data), self.progress)
        self.client.calls.clear()
        await self.download(destination=str(final))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(final.read_bytes(), self.data)

    async def test_publish_conflict_never_overwrites_another_file(self):
        from module.partial_file import DownloadStorageError
        final = self.path.parent / "existing.bin"
        final.write_bytes(b"existing output")
        with self.assertRaises(DownloadStorageError):
            await self.download(destination=str(final))
        self.assertEqual(final.read_bytes(), b"existing output")
        self.assert_file()
        self.assertNotIn(len(self.data), self.progress)

    async def test_interruption_after_staging_rename_resumes_without_network(self):
        from module.partial_file import DownloadStorageError
        real_replace = os.replace
        def interrupted(source, target):
            result = real_replace(source, target)
            if Path(target) == self.path:
                raise OSError(errno.EIO, "failure after rename")
            return result
        with patch("module.parallel_download.os.replace", interrupted):
            with self.assertRaises(DownloadStorageError):
                await self.download()
        self.assert_file()
        self.client.calls.clear()
        await self.download()
        self.assertEqual(self.client.calls, [])
        self.assert_file()

    async def test_cancelled_disk_write_finishes_before_download_returns(self):
        from module.partial_file import PartialFile
        entered, release = threading.Event(), threading.Event()
        original = PartialFile.write_at
        def write(file, offset, data):
            entered.set()
            release.wait(5)
            original(file, offset, data)
        with patch.object(PartialFile, "write_at", write):
            task = asyncio.create_task(self.download())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.client.tracker["active"], 0)
        await self.download()
        self.assert_file()

    async def test_cancelled_publication_discards_only_its_own_renamed_file(self):
        final = self.path.parent / "final.bin"
        entered, release = threading.Event(), threading.Event()
        original = os.replace
        def replace(source, target):
            result = original(source, target)
            if Path(source) == self.path and Path(target) == final:
                entered.set()
                release.wait(5)
            return result
        with patch("module.parallel_download.os.replace", replace):
            task = asyncio.create_task(self.download(destination=str(final), discard_requested=lambda: True))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(final.exists())
        self.assertFalse(self.path.exists())
        self.assertFalse(parts_path(str(self.path)).exists())

    async def test_checkpoint_failure_keeps_old_commit_and_overwrites_uncommitted_bytes(self):
        from module.partial_file import DownloadStorageError
        from module.parallel_download import _save_plan
        def save(directory, plan):
            if sum(plan["committed_bytes"]) > 0:
                raise OSError(errno.ENOSPC, "manifest disk full")
            return _save_plan(directory, plan)
        with patch("module.parallel_download._save_plan", save):
            with self.assertRaises(DownloadStorageError):
                await self.download()
        self.assertEqual(sum(checkpoint_lengths(self.path)), 0)
        self.assertFalse(self.path.exists())
        self.assertNotIn(len(self.data), self.progress)
        await self.download()
        self.assert_file()

    async def test_data_sync_precedes_every_nonempty_checkpoint_commit(self):
        from module.partial_file import PartialFile
        from module.parallel_download import _save_plan
        original_write, original_sync = PartialFile.write_at, PartialFile.sync
        written = synced = 0
        commits = []
        def write(file, offset, data):
            nonlocal written
            original_write(file, offset, data)
            written += len(data)
        def sync(file):
            nonlocal synced
            original_sync(file)
            synced = written
        def save(directory, plan):
            count = sum(plan["committed_bytes"])
            self.assertLessEqual(count, synced)
            commits.append(count)
            _save_plan(directory, plan)
        with patch.object(PartialFile, "write_at", write), patch.object(PartialFile, "sync", sync), patch(
                "module.parallel_download._save_plan", save):
            await self.download()
        self.assertEqual(commits[-1], len(self.data))
        self.assert_file()

    async def test_failed_post_rename_directory_sync_is_retried_before_completion(self):
        from module.partial_file import DownloadStorageError
        from module.parallel_download import sync_directory
        final = self.path.parent / "final.bin"
        def sync(path):
            if final.exists():
                raise OSError(errno.EIO, "directory sync failed")
            sync_directory(path)
        with patch("module.parallel_download.sync_directory", sync):
            with self.assertRaises(DownloadStorageError):
                await self.download(destination=str(final))
        self.assertEqual(final.read_bytes(), self.data)
        self.assertNotIn(len(self.data), self.progress)
        self.client.calls.clear()
        with patch("module.parallel_download.sync_directory", wraps=sync_directory) as synchronized:
            await self.download(destination=str(final))
        self.assertIn(((final.parent,), {}), synchronized.call_args_list)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.progress[-1], len(self.data))

    async def test_cleanup_failure_after_completion_does_not_fail_published_download(self):
        with patch("module.parallel_download.shutil.rmtree", side_effect=OSError(errno.EIO, "cleanup failed")):
            result = await self.download()
        self.assertEqual(result, str(self.path))
        self.assert_file()
        self.assertEqual(self.progress[-1], len(self.data))
        self.client.calls.clear()
        await self.download()
        self.assertEqual(self.client.calls, [])

    async def test_all_committed_bytes_resume_without_fictitious_transfer_progress(self):
        from module.partial_file import DownloadStorageError
        from module.parallel_download import _save_plan
        def save(directory, plan):
            if plan["phase"] == "ready":
                raise OSError(errno.EIO, "ready checkpoint failed")
            _save_plan(directory, plan)
        with patch("module.parallel_download._save_plan", save):
            with self.assertRaises(DownloadStorageError):
                await self.download()
        self.assertEqual(sum(checkpoint_lengths(self.path)), len(self.data))
        self.client.calls.clear()
        self.progress.clear()
        await self.download()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.progress, [len(self.data)])
        self.assert_file()


class PartCountTests(unittest.TestCase):
    def test_fixed_size_part_count_includes_short_tail(self):
        for size, expected in ((10, 1), (64, 1), (65, 2), (128, 2), (129, 3), (256, 4)):
            with self.subTest(size=size):
                self.assertEqual(part_count(size * MB, 64 * MB), expected)
        self.assertEqual(part_count(64 * MB + 1, 64 * MB), 2)


if __name__ == "__main__":
    unittest.main()
