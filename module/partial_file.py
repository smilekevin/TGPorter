"""Bounded random-access disk operations with cancellation-safe ownership."""

import asyncio
import errno
import os
from pathlib import Path


class DownloadStorageError(Exception):
    """A local storage failure must never trigger Telegram reconnects."""


def sync_directory(path):
    """Persist directory entries where the platform supports directory fsync."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(fd)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


async def disk_operation(function, *args):
    """Drain submitted disk work before propagating cancellation or closing FDs."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except OSError as error:
            raise DownloadStorageError(f"Local download storage failed: {error}") from error
    if cancelled:
        raise asyncio.CancelledError
    return result


def write_all(fd, data, offset, *, positioned=True):
    """Write an entire block, including short writes, without crossing its bounds."""
    view = memoryview(data)
    if not positioned:
        os.lseek(fd, offset, os.SEEK_SET)
    while view:
        count = os.pwrite(fd, view, offset) if positioned else os.write(fd, view)
        if count <= 0 or count > len(view):
            raise OSError(errno.EIO, "Random-access write made no valid progress")
        view = view[count:]
        offset += count


class PartialFile:
    """Own one preallocated file; each fallback writer opens its own file offset."""

    def __init__(self, path, size):
        self.path = Path(path)
        self.size = size
        self.fd = None

    def open(self, initialize=False):
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        if initialize:
            flags |= os.O_CREAT
        # New downloads default to 0644, subject to umask; resumed files retain
        # their permissions, which atomic publication also preserves.
        fd = os.open(self.path, flags, 0o644)
        try:
            if initialize:
                if os.fstat(fd).st_size > self.size:
                    raise ValueError("Partial download exceeds file size")
                if hasattr(os, "posix_fallocate"):
                    try:
                        os.posix_fallocate(fd, 0, self.size)
                    except OSError as error:
                        if error.errno not in (errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL):
                            raise
                os.ftruncate(fd, self.size)
                os.fsync(fd)
                sync_directory(self.path.parent)
            elif os.fstat(fd).st_size != self.size:
                raise ValueError("Preallocated download has an unexpected size")
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def write_at(self, offset, data):
        if offset < 0 or not data or offset + len(data) > self.size:
            raise ValueError("Write exceeds preallocated download bounds")
        if hasattr(os, "pwrite"):
            write_all(self.fd, data, offset)
        else:
            # An independent open is essential: dup() shares the file position.
            fd = os.open(self.path, os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                write_all(fd, data, offset, positioned=False)
            finally:
                os.close(fd)

    def sync(self):
        os.fsync(self.fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
