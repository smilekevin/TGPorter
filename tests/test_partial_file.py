"""Exercise real random-access files and cancellation without Telegram."""

import asyncio
import errno
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from module.partial_file import (DownloadStorageError, PartialFile, disk_operation,
                                 write_all)


class PartialFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "file.temp"

    @unittest.skipIf(os.name == "nt", "POSIX file permissions required")
    async def test_new_file_permissions_respect_umask_and_survive_publication(self):
        for mask, expected in ((0o000, 0o644), (0o022, 0o644), (0o077, 0o600)):
            with self.subTest(umask=oct(mask)):
                previous = os.umask(mask)
                file = PartialFile(self.path, 4)
                try:
                    file.open(initialize=True)
                    file.write_at(0, b"data")
                    file.sync()
                finally:
                    file.close()
                    os.umask(previous)
                destination = self.path.with_suffix(".bin")
                os.replace(self.path, destination)
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), expected)
                self.assertEqual(destination.read_bytes(), b"data")

    @unittest.skipIf(os.name == "nt", "POSIX file permissions required")
    async def test_reopening_existing_file_preserves_permissions(self):
        self.path.write_bytes(b"data")
        self.path.chmod(0o600)
        file = PartialFile(self.path, 4)
        for initialize in (True, False):
            with self.subTest(initialize=initialize):
                try:
                    file.open(initialize=initialize)
                    self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
                    self.assertEqual(self.path.read_bytes(), b"data")
                finally:
                    file.close()

    async def test_short_positioned_writes_preserve_offsets_and_retry_tail(self):
        calls = []
        def short(fd, data, offset):
            calls.append((bytes(data), offset))
            return min(2, len(data))
        with patch("module.partial_file.os.pwrite", short, create=True):
            write_all(7, b"abcdefg", 11)
        self.assertEqual(calls, [(b"abcdefg", 11), (b"cdefg", 13), (b"efg", 15), (b"g", 17)])

    async def test_zero_write_fails_without_spinning(self):
        with patch("module.partial_file.os.pwrite", return_value=0, create=True):
            with self.assertRaises(OSError):
                write_all(7, b"abc", 0)

    async def test_preallocation_is_not_progress_and_random_writes_preserve_neighbors(self):
        file = PartialFile(self.path, 8192)
        await disk_operation(file.open, True)
        try:
            self.assertEqual(self.path.stat().st_size, 8192)
            await asyncio.gather(disk_operation(file.write_at, 4096, b"b" * 4096),
                                 disk_operation(file.write_at, 0, b"a" * 4096))
            await disk_operation(file.sync)
            self.assertEqual(self.path.read_bytes(), b"a" * 4096 + b"b" * 4096)
            for offset, data in ((-1, b"x"), (8192, b"x"), (0, b"")):
                with self.assertRaises(ValueError):
                    await disk_operation(file.write_at, offset, data)
        finally:
            await disk_operation(file.close)

    async def test_fallback_opens_independent_offsets_and_handles_short_writes(self):
        real_pwrite = getattr(os, "pwrite", None)
        if real_pwrite is not None:
            del os.pwrite
        try:
            file = PartialFile(self.path, 4096)
            await disk_operation(file.open, True)
            try:
                original_write = os.write
                def short(fd, data):
                    return original_write(fd, data[:19])
                with patch("module.partial_file.os.write", short):
                    await asyncio.gather(disk_operation(file.write_at, 0, b"a" * 2048),
                                         disk_operation(file.write_at, 2048, b"b" * 2048))
                self.assertEqual(self.path.read_bytes(), b"a" * 2048 + b"b" * 2048)
            finally:
                await disk_operation(file.close)
        finally:
            if real_pwrite is not None:
                os.pwrite = real_pwrite

    async def test_cancellation_drains_thread_before_returning_even_if_cancelled_twice(self):
        entered = threading.Event()
        release = threading.Event()
        ended = threading.Event()
        def blocked():
            entered.set()
            release.wait(5)
            ended.set()
        task = asyncio.create_task(disk_operation(blocked))
        try:
            await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(ended.is_set())

    async def test_allocation_failure_closes_fd_and_is_a_storage_error(self):
        file = PartialFile(self.path, 1024)
        with patch("module.partial_file.os.posix_fallocate",
                   side_effect=OSError(errno.ENOSPC, "disk full"), create=True):
            with self.assertRaises(DownloadStorageError):
                await disk_operation(file.open, True)
        self.assertIsNone(file.fd)
        self.assertEqual(self.path.stat().st_size, 0)

    async def test_unsupported_allocation_falls_back_but_permission_errors_do_not(self):
        for error in (errno.EOPNOTSUPP, errno.EACCES):
            with self.subTest(error=error):
                file = PartialFile(self.path, 1024)
                try:
                    with patch("module.partial_file.os.posix_fallocate",
                               side_effect=OSError(error, "allocation failed"), create=True):
                        if error == errno.EACCES:
                            with self.assertRaises(DownloadStorageError):
                                await disk_operation(file.open, True)
                        else:
                            await disk_operation(file.open, True)
                            self.assertEqual(self.path.stat().st_size, 1024)
                finally:
                    await disk_operation(file.close)

    async def test_large_offsets_use_wide_integers_in_fallback(self):
        with patch("module.partial_file.os.lseek") as seek, patch(
                "module.partial_file.os.write", return_value=3):
            write_all(7, b"abc", 5 * 1024 ** 3, positioned=False)
        seek.assert_called_once_with(7, 5 * 1024 ** 3, os.SEEK_SET)
