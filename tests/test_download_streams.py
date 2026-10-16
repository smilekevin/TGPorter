"""Live stream accounting uses request lifetimes rather than configured workers."""

import asyncio
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from module import download_stat as stat
from module.parallel_download import download_file, DownloadWorkersExhausted
from test_parallel_download import FakeClient, MB


class DownloadStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'file.bin')
        self.data = b'a' * (3 * MB)
        self.client = FakeClient(self.data)
        self.count = stat.get_active_download_streams
        for item in (
            patch.object(stat, '_download_streams', {}),
            patch.object(stat, '_paused_tasks', set()),
            patch.object(stat, '_cancelled_tasks', set()),
            patch.object(stat, '_download_state', stat.DownloadState.Downloading),
            patch('module.parallel_download.CONNECTION_START_INTERVAL', 0),
            patch('module.parallel_download._wait_network_retry', AsyncMock()),
        ):
            item.start()
            self.addCleanup(item.stop)

    async def download(self, workers=3, progress=None):
        return await download_file(
            self.client, 'file', len(self.data), self.path, 'identity', workers,
            progress or AsyncMock(), AsyncMock(), MB,
            stream_context=lambda: stat.track_download_stream(100, 61, 'owner'))

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0)
        await asyncio.wait_for(wait(), 2)

    async def stop(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_file_and_total_counts_follow_controls_and_unwind_exceptions(self):
        with ExitStack() as stack:
            stack.enter_context(stat.track_download_stream(100, 61, 'owner'))
            stack.enter_context(stat.track_download_stream(100, 61, 'owner'))
            stack.enter_context(stat.track_download_stream(200, 61, 'other'))
            self.assertEqual(self.count(), 3)
            self.assertEqual(self.count('100_61'), 2)
            stat.pause_task('100_61')
            self.assertEqual(self.count(), 1)
            self.assertEqual(self.count('100_61'), 0)
            stat.resume_task('100_61')
            stat.pause_task('owner')
            self.assertEqual(self.count(), 1)
            stat.resume_task('owner')
            stat.set_download_state(stat.DownloadState.StopDownload)
            self.assertEqual(self.count(), 0)
            stat.set_download_state(stat.DownloadState.Downloading)
            self.assertEqual(self.count(), 3)
            stat._cancelled_tasks.add('owner')
            self.assertEqual(self.count(), 1)
        self.assertEqual(self.count(), 0)
        with self.assertRaises(ConnectionError):
            with stat.track_download_stream(100, 61, 'other'):
                raise ConnectionError('offline')
        self.assertEqual(self.count(), 0)

    async def test_idle_connections_and_merge_do_not_count_as_streams(self):
        gates = [asyncio.Event() for _ in range(3)]
        async def before(index):
            await gates[index].wait()
        self.client.before_chunk = before
        counts = []
        async def progress(current, total):
            counts.append((current, self.count()))
        task = asyncio.create_task(self.download(progress=progress))
        try:
            await self.until(lambda: self.client.tracker['active'] == 3)
            self.assertEqual(self.count('100_61'), 3)
            gates[0].set()
            await self.until(lambda: self.count() == 2)
            gates[1].set()
            await self.until(lambda: self.count() == 1)
            gates[2].set()
            await task
            self.assertEqual(Path(self.path).read_bytes(), self.data)
            self.assertEqual(counts[0], (0, 0))
            self.assertEqual(counts[-1], (len(self.data), 0))
        finally:
            await self.stop(task)
        self.assertEqual(self.count(), 0)

    async def test_retired_connection_drops_count_and_cancel_clears_survivors(self):
        fail = asyncio.Event()
        hold = asyncio.Event()
        async def before(index):
            if index == 0:
                await fail.wait()
                raise ConnectionError('offline')
            await hold.wait()
        self.client.before_chunk = before
        task = asyncio.create_task(self.download())
        try:
            await self.until(lambda: self.count() == 3)
            fail.set()
            await self.until(lambda: self.count() == 2)
            self.assertFalse(task.done())
        finally:
            await self.stop(task)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.client.tracker['active'], 0)

    async def test_backoff_and_exhausted_attempts_leave_no_streams(self):
        async def fail(index):
            raise ConnectionError('offline')
        async def backoff(*args):
            self.assertEqual(self.count(), 0)
        self.client.before_chunk = fail
        with patch('module.parallel_download._wait_network_retry', side_effect=backoff):
            with self.assertRaises(DownloadWorkersExhausted):
                await self.download(workers=1)
        self.assertEqual(self.count(), 0)

    async def test_resume_uses_saved_worker_count_and_never_restores_live_counts(self):
        hold = asyncio.Event()
        async def before(index):
            await hold.wait()
        self.client.before_chunk = before
        task = asyncio.create_task(self.download(workers=2))
        try:
            await self.until(lambda: self.count() == 2)
        finally:
            await self.stop(task)
        self.assertEqual(self.count(), 0)
        task = asyncio.create_task(self.download(workers=6))
        try:
            await self.until(lambda: self.count() == 2)
            self.assertEqual(self.client.tracker['active'], 2)
            hold.set()
            await task
            self.assertEqual(Path(self.path).read_bytes(), self.data)
        finally:
            await self.stop(task)
        self.assertEqual(self.count(), 0)
