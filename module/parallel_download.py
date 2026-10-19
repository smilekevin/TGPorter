"""Queued fixed-size parts with independent worker connections and aggregate progress."""

import asyncio
import json
import os
import shutil
import time
from contextlib import nullcontext
from pathlib import Path
from weakref import WeakValueDictionary

from loguru import logger

from module.download_connection import DownloadConnection
from module.partial_file import (PartialFile, DownloadStorageError, disk_operation,
                                 sync_directory)
from module.retry_policy import network_backoff
from module.telegram import pyrogram


CHUNK_SIZE = 1024 * 1024
CONNECTION_START_INTERVAL = 1.0
_file_locks = WeakValueDictionary()


class DownloadWorkersExhausted(ConnectionError):
    """The last available worker failed three consecutive download attempts."""

    def __init__(self, error):
        self.error = error
        super().__init__(f"Last download connection failed 3 times: {type(error).__name__}: {error}")


class DownloadCheckpointError(DownloadStorageError, ValueError):
    """Invalid or unsupported checkpoints are local failures, never network errors."""


class _PartConnectionError(Exception):
    """Keep transport/SDK failures separate from local writes and UI controls."""

    def __init__(self, error, file_id):
        self.error = error
        self.file_id = file_id


class _ConnectionGate:
    """Confirm each new connection before expansion, with spacing and cooldowns."""

    def __init__(self, wait_ready, flood_wait, flood_remaining):
        self.wait_ready = wait_ready
        self.flood_wait = flood_wait
        self.flood_remaining = flood_remaining
        self.cooldown_until = 0
        self.next_start = 0
        self.lock = asyncio.Lock()
        self.start_confirmed = asyncio.Event()
        self.start_confirmed.set()
        self.starting_task = None

    def throttled(self, error):
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + error.value)
        if self.flood_wait:
            self.flood_wait(error.value)

    async def wait(self, *, creating=False):
        while True:
            await self.wait_ready()
            now = time.monotonic()
            delay = max(0, self.cooldown_until - now,
                        self.flood_remaining() if self.flood_remaining else 0,
                        self.next_start - now if creating else 0)
            if delay <= 0:
                return
            await asyncio.sleep(min(0.25, delay))

    def finish_start(self):
        """Only the starting worker can confirm data or release a failed attempt."""
        if self.starting_task is asyncio.current_task():
            self.starting_task = None
            self.start_confirmed.set()
            return True
        return False

    async def start(self, queue, available):
        while True:
            await available.wait()
            async with self.lock:
                await self.start_confirmed.wait()
                await self.wait(creating=True)
                try:
                    part = queue.get_nowait()
                except asyncio.QueueEmpty:
                    available.clear()
                    continue
                # Reserve a creation slot only when there is actually work.
                # Otherwise an idle worker could open its first socket later,
                # after another worker has already published a FloodWait.
                self.next_start = time.monotonic() + CONNECTION_START_INTERVAL
                self.starting_task = asyncio.current_task()
                self.start_confirmed.clear()
                return part


def parts_path(file_name):
    return Path(file_name + ".parts")


def has_parts(file_name):
    return parts_path(file_name).exists()


def discard_partial_download(file_name, destination=None):
    """Delete only this download's staging files after its writers have stopped."""
    directory = parts_path(file_name)
    if destination is not None:
        # A cancelled thread may have completed the final rename. Only remove
        # the exact inode recorded by this download, never an unrelated output.
        try:
            with (directory / "manifest.json").open(encoding="utf-8") as file:
                plan = json.load(file)
            target = Path(destination)
            if (plan.get("version") == 3 and plan.get("phase") == "publishing"
                    and plan.get("destination") == os.path.abspath(target)
                    and not Path(file_name).exists() and target.exists()
                    and _file_stamp(target) == plan.get("file_stamp")):
                target.unlink()
        except (OSError, ValueError, AttributeError):
            pass
    for path in (Path(file_name), Path(file_name + ".temp"), directory):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path == directory and path.is_dir():
                shutil.rmtree(path)
            else:
                continue
            logger.info("Removed cancelled download data: {}", path)
        except OSError as error:
            logger.warning("Could not remove cancelled download data {}: {}", path, error)


def _save_plan(directory, plan):
    temporary = directory / "manifest.json.tmp"
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(plan, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, directory / "manifest.json")
    sync_directory(directory)


def part_count(size, part_size):
    """Count fixed-size parts, including a shorter final part."""
    return max(1, (size + part_size - 1) // part_size)


def _make_plan(size, workers, identity, part_size):
    ranges = [[start, min(start + part_size, size)] for start in range(0, size, part_size)]
    return {"version": 3, "storage": "single-file", "phase": "initializing",
            "size": size, "identity": identity, "ranges": ranges,
            "workers": min(workers, len(ranges)), "part_size": part_size,
            "committed_bytes": [0] * len(ranges)}


def _read_plan(directory, size, identity):
    with (directory / "manifest.json").open(encoding="utf-8") as file:
        plan = json.load(file)
    if (not isinstance(plan, dict) or plan.get("version") != 3
            or plan.get("size") != size or plan.get("identity") != identity):
        raise ValueError("Unsupported checkpoint or partial download belongs to a different file; clear its cache to restart")
    ranges = plan.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("Invalid partial download ranges")
    if type(plan.get("workers")) is not int or not 1 <= plan["workers"] <= len(ranges):
        raise ValueError("Invalid partial download worker count")
    part_size = plan.get("part_size")
    if type(part_size) is not int or part_size < CHUNK_SIZE or part_size % CHUNK_SIZE:
        raise ValueError("Invalid partial download part size")
    previous = 0
    for bounds in ranges:
        if (not isinstance(bounds, list) or len(bounds) != 2
                or any(type(value) is not int for value in bounds)):
            raise ValueError("Invalid partial download range")
        start, end = bounds
        if (start != previous or start % CHUNK_SIZE or not start < end <= size
                or (end != size and end % CHUNK_SIZE)):
            raise ValueError("Invalid partial download range")
        previous = end
    if previous != size:
        raise ValueError("Partial download ranges do not cover the file")
    committed = plan.get("committed_bytes")
    if (plan.get("storage") != "single-file"
            or plan.get("phase") not in ("initializing", "downloading", "ready", "publishing")
            or not isinstance(committed, list) or len(committed) != len(ranges)):
        raise ValueError("Invalid single-file checkpoint")
    for length, (start, end) in zip(committed, ranges):
        if (type(length) is not int or not 0 <= length <= end - start
                or (length != end - start and length % CHUNK_SIZE)):
            raise ValueError("Invalid committed range length")
    if plan["phase"] == "initializing" and any(committed):
        raise ValueError("Uninitialized download has committed bytes")
    if plan["phase"] in ("ready", "publishing"):
        if any(length != end - start for length, (start, end) in zip(committed, ranges)):
            raise ValueError("Incomplete download marked ready")
        stamp = plan.get("file_stamp")
        if not isinstance(stamp, list) or len(stamp) != 4 or any(type(v) is not int for v in stamp):
            raise ValueError("Invalid ready file identity")
    return plan


def _file_stamp(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


async def download_file(client, file_id, size, file_name, identity, parallelism,
                        progress, wait_ready, part_size=64 * CHUNK_SIZE, request_started=None,
                        discard_requested=None, refresh_file_id=None,
                        flood_wait=None, flood_remaining=None, stream_context=None, destination=None):
    """Keep ranges across retries/restarts and validate every received block.

    Workers take the next unfinished part when their current part completes.
    Committed prefixes are its checkpoints; uncommitted blocks are fetched again.
    New connections start gradually. Failed workers return their ranges while
    healthy workers continue; only the final worker has a three-attempt budget.
    Flood callbacks publish and inspect the application's shared cooldown.
    The optional synchronous stream context tracks requests, excluding idle workers.
    """
    async def run():
        key = os.path.abspath(file_name)
        lock = _file_locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                return await _download_file(client, file_id, size, file_name, identity,
                                            parallelism, progress, wait_ready, part_size,
                                            request_started, refresh_file_id,
                                            flood_wait, flood_remaining, stream_context, destination)
            finally:
                if discard_requested is not None and discard_requested():
                    await disk_operation(discard_partial_download, file_name, destination)

    async def watch_control():
        # Cancellation must work even when a request or slot acquisition hangs.
        while True:
            await wait_ready()
            await asyncio.sleep(0.25)

    work = asyncio.create_task(run())
    control = asyncio.create_task(watch_control())
    try:
        done, _ = await asyncio.wait((work, control), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return await work
        await control
    finally:
        work.cancel()
        control.cancel()
        await asyncio.gather(work, control, return_exceptions=True)


async def _wait_network_retry(seconds, wait_ready):
    """Keep part backoff responsive to pause and cancellation controls."""
    remaining = seconds
    while remaining > 0:
        await wait_ready()
        interval = min(1, remaining)
        await asyncio.sleep(interval)
        remaining -= interval
    await wait_ready()


async def _download_file(client, file_id, size, file_name, identity, parallelism,
                         progress, wait_ready, part_size, request_started,
                         refresh_file_id, flood_wait, flood_remaining, stream_context, destination):
    if size <= 0 or parallelism < 1 or part_size < CHUNK_SIZE or part_size % CHUNK_SIZE:
        raise ValueError("File size and worker count must be positive; part size must be whole MiB")
    directory = parts_path(file_name)
    manifest = directory / "manifest.json"
    await wait_ready()
    output = Path(file_name)
    destination = Path(destination or file_name)
    partial = PartialFile(file_name + ".temp", size)
    try:
        plan = await disk_operation(_read_plan, directory, size, identity) if manifest.exists() else None
        return await _download_to_file(client, file_id, size, file_name, identity, parallelism,
                                       progress, wait_ready, part_size, request_started,
                                       refresh_file_id, flood_wait, flood_remaining, stream_context,
                                       directory, plan, output, destination, partial)
    except ValueError as error:
        raise DownloadCheckpointError(str(error)) from error
    except OSError as error:
        if isinstance(error, (ConnectionError, TimeoutError)):
            raise
        raise DownloadStorageError(f"Local download storage failed: {error}") from error
    finally:
        await disk_operation(partial.close)


async def _download_to_file(client, file_id, size, file_name, identity, parallelism,
                            progress, wait_ready, part_size, request_started,
                            refresh_file_id, flood_wait, flood_remaining, stream_context,
                            directory, plan, output, destination, partial):
    if plan is None:
        if partial.path.exists() or output.exists() or directory.exists():
            raise DownloadStorageError("Unrecognized download cache; remove it to restart this download")
        plan = _make_plan(size, parallelism, identity, part_size)
        await disk_operation(directory.mkdir, 0o700, True, True)
        await disk_operation(_save_plan, directory, plan)
    ranges = plan["ranges"]
    downloaded = list(plan["committed_bytes"])
    if plan["phase"] in ("ready", "publishing"):
        return await _publish_file(directory, plan, output, destination, partial.path,
                                   progress, wait_ready)
    await disk_operation(partial.open, plan["phase"] == "initializing")
    if plan["phase"] == "initializing":
        plan["phase"] = "downloading"
        await disk_operation(_save_plan, directory, plan)

    checkpoint_lock = asyncio.Lock()
    checkpoint_time = time.monotonic()

    async def checkpoint(force=False):
        nonlocal checkpoint_time
        async with checkpoint_lock:
            snapshot = list(downloaded)
            delta = sum(snapshot) - sum(plan["committed_bytes"])
            if not delta or (not force and delta < 8 * CHUNK_SIZE
                             and time.monotonic() - checkpoint_time < 1):
                return
            # Snapshot only complete writes, then order durable data before metadata.
            await disk_operation(partial.sync)
            updated = dict(plan, committed_bytes=snapshot)
            await disk_operation(_save_plan, directory, updated)
            plan.update(updated)
            checkpoint_time = time.monotonic()

    report_lock = asyncio.Lock()

    async def report():
        async with report_lock:
            # Do not persist a completed download until the output exists.
            await progress(min(sum(downloaded), size - 1), size)

    async def stream_progress(*_):
        await wait_ready()

    gate = _ConnectionGate(wait_ready, flood_wait, flood_remaining)

    async def connection_chunks(connection, requested_id, remaining, offset):
        stream = connection.get_file(requested_id, size, remaining, offset, stream_progress)
        with stream_context() if stream_context else nullcontext():
            try:
                try:
                    async for chunk in stream:
                        yield chunk
                except pyrogram.StopTransmission:
                    raise
                except Exception as error:
                    if isinstance(error, (pyrogram.errors.FloodWait, pyrogram.errors.FloodPremiumWait)):
                        # Publish the cooldown before connection cleanup can yield.
                        gate.throttled(error)
                    raise _PartConnectionError(error, requested_id) from error
            finally:
                await stream.aclose()

    async def download_range(index, connection):
        start, end = ranges[index]
        if downloaded[index] == end - start:
            return
        await wait_ready()
        offset = (start + downloaded[index]) // CHUNK_SIZE
        remaining = (end - start - downloaded[index] + CHUNK_SIZE - 1) // CHUNK_SIZE
        if request_started:
            request_started(file_id, size, remaining, offset)
        requested_id = file_id
        stream = connection_chunks(connection, requested_id, remaining, offset)
        logger.info("Downloading {} part {}/{} at offset {}", file_name,
                    index + 1, len(ranges), start + downloaded[index])
        try:
            async for chunk in stream:
                await wait_ready()
                expected = min(CHUNK_SIZE, end - start - downloaded[index])
                if not expected or len(chunk) != expected:
                    raise _PartConnectionError(ConnectionError(
                        "Downloaded block has an unexpected length"), requested_id)
                await disk_operation(partial.write_at, start + downloaded[index], chunk)
                downloaded[index] += len(chunk)
                if gate.finish_start():
                    logger.info("Confirmed independent download startup for {}", connection.label)
                await checkpoint(force=downloaded[index] == end - start)
                await report()
            if downloaded[index] != end - start:
                # Pyrogram can swallow network/reference errors in get_file.
                raise _PartConnectionError(ConnectionError(
                    "Part download stream ended early"), requested_id)
        finally:
            await stream.aclose()

    if sum(downloaded) < size:
        await report()
    # Retry earlier unfinished ranges before moving on to later parts.
    queue = asyncio.PriorityQueue()
    for index, (start, end) in enumerate(ranges):
        if downloaded[index] < end - start:
            queue.put_nowait(index)
    workers = min(plan.get("workers", len(ranges)), queue.qsize())
    logger.info("Downloading {} in {} part(s) with {} worker(s)", file_name, len(ranges), workers)

    live = set()
    available = asyncio.Event()
    if not queue.empty():
        available.set()
    refresh_lock = asyncio.Lock()
    last_failures = 0

    async def worker(index):
        nonlocal file_id, last_failures
        part = None
        task = asyncio.current_task()
        try:
            while True:
                part = await gate.start(queue, available)
                if downloaded[part] == ranges[part][1] - ranges[part][0]:
                    queue.task_done()
                    part = None
                    last_failures = 0
                    gate.finish_start()
                    continue
                try:
                    async with DownloadConnection(client, f"{file_name} worker {index + 1}/{workers}") as connection:
                        while True:
                            await wait_ready()
                            await download_range(part, connection)
                            queue.task_done()
                            part = None
                            last_failures = 0
                            await asyncio.sleep(0)
                            # Keep idle healthy connections available while other
                            # workers close/requeue failed in-flight ranges.
                            part = await queue.get()
                except _PartConnectionError as failure:
                    # No other worker can write this part until its old stream
                    # and private session have both finished closing.
                    queue.put_nowait(part)
                    available.set()
                    queue.task_done()
                    part = None
                    # __aexit__ has stopped the failed writer and closed its
                    # connections before another worker may start.
                    gate.finish_start()
                    error = failure.error
                    if getattr(client, "_download_connections_closing", False):
                        raise ConnectionError("Download client is restarting") from error
                    logger.warning("{} worker {}/{} failed: {}: {}",
                                   file_name, index + 1, workers, type(error).__name__, error)
                    if isinstance(error, (pyrogram.errors.FloodWait, pyrogram.errors.FloodPremiumWait)):
                        # Keep this worker available. Its next connection waits
                        # at the gate; healthy existing connections keep working.
                        continue
                    if refresh_file_id and isinstance(error, (
                            pyrogram.errors.FileReferenceExpired, pyrogram.errors.FileReferenceInvalid)):
                        try:
                            async with refresh_lock:
                                if file_id is failure.file_id:
                                    await gate.wait()
                                    file_id = await refresh_file_id()
                        except (pyrogram.errors.FloodWait, pyrogram.errors.FloodPremiumWait) as wait_error:
                            gate.throttled(wait_error)
                            continue
                        except pyrogram.StopTransmission:
                            raise
                        except Exception as refresh_error:
                            error = refresh_error
                            logger.warning("{} reference refresh failed: {}: {}",
                                           file_name, type(error).__name__, error)
                    if len(live) > 1:
                        # Retire only this connection. Its unfinished range is
                        # available to the surviving workers, without a file retry.
                        return
                    last_failures += 1
                    if last_failures >= 3:
                        raise DownloadWorkersExhausted(error) from error
                    delay = network_backoff(10 * 2 ** (last_failures - 1))
                    logger.warning("{}: last worker attempt {}/3 failed; retrying after {}s",
                                   file_name, last_failures, delay)
                    await _wait_network_retry(delay, wait_ready)
        finally:
            gate.finish_start()
            live.discard(task)

    async def periodic_checkpoint():
        while True:
            await asyncio.sleep(1)
            await checkpoint(force=True)

    async def run_workers():
        tasks = {asyncio.create_task(worker(index)) for index in range(workers)}
        live.update(tasks)
        timer = asyncio.create_task(periodic_checkpoint())
        completed = asyncio.create_task(queue.join())
        try:
            pending = set(tasks) | {timer}
            while pending:
                done, _ = await asyncio.wait(pending | {completed}, return_when=asyncio.FIRST_COMPLETED)
                for task in done - {completed}:
                    task.result()
                if completed in done:
                    break
                pending.difference_update(done)
        finally:
            completed.cancel()
            timer.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(completed, timer, *tasks, return_exceptions=True)

    try:
        await run_workers()
    finally:
        await checkpoint(force=True)
    await disk_operation(partial.sync)
    await disk_operation(partial.close)
    plan.update(phase="ready", file_stamp=await disk_operation(_file_stamp, partial.path))
    await disk_operation(_save_plan, directory, plan)
    return await _publish_file(directory, plan, output, destination, partial.path,
                               progress, wait_ready)


async def _publish_file(directory, plan, output, destination, partial_path, progress, wait_ready):
    """Resume either rename without redownloading or exposing incomplete output."""
    key = "publish:" + os.path.abspath(destination)
    lock = _file_locks.setdefault(key, asyncio.Lock())
    async with lock:
        await wait_ready()
        target = os.path.abspath(destination)
        if plan["phase"] == "publishing" and plan.get("destination") != target:
            raise ValueError("Partial download publication target changed")
        if partial_path.exists():
            if _file_stamp(partial_path) != plan["file_stamp"] or output.exists():
                raise ValueError("Unexpected ready download files")
            await disk_operation(os.replace, partial_path, output)
            await disk_operation(sync_directory, output.parent)
        if output != destination and output.exists():
            if _file_stamp(output) != plan["file_stamp"]:
                raise ValueError("Ready download identity changed")
            await disk_operation(destination.parent.mkdir, 0o755, True, True)
            if destination.exists():
                raise DownloadStorageError("Download destination already exists")
            plan.update(phase="publishing", destination=target)
            await disk_operation(_save_plan, directory, plan)
            await wait_ready()
            try:
                await disk_operation(os.replace, output, destination)
            except asyncio.CancelledError:
                # Cancellation can arrive after rename has completed in its thread.
                # Put our own file back so staging cleanup/preservation owns it.
                if (not output.exists() and destination.exists()
                        and _file_stamp(destination) == plan["file_stamp"]):
                    await disk_operation(os.replace, destination, output)
                    await disk_operation(sync_directory, destination.parent)
                    await disk_operation(sync_directory, output.parent)
                raise
        elif output != destination and plan["phase"] != "publishing":
            raise ValueError("Ready download file is missing")
        if not destination.is_file() or _file_stamp(destination) != plan["file_stamp"]:
            raise ValueError("Published download has an unexpected size")
        # Recovery after a successful rename must retry failed directory syncs.
        await disk_operation(sync_directory, destination.parent)
        if output.parent != destination.parent:
            await disk_operation(sync_directory, output.parent)
        await progress(plan["size"], plan["size"])
        try:
            await disk_operation(shutil.rmtree, directory)
            await disk_operation(sync_directory, directory.parent)
        except DownloadStorageError as error:
            # The published file and completion callback have already committed.
            logger.warning("Could not clean completed download checkpoint {}: {}", directory, error)
        return str(destination)
