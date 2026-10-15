"""Queued fixed-size parts with independent worker connections and aggregate progress."""

import asyncio
import json
import os
import shutil
from pathlib import Path
from weakref import WeakValueDictionary

from loguru import logger

from module.download_connection import DownloadConnection
from module.retry_policy import network_backoff


CHUNK_SIZE = 1024 * 1024
_file_locks = WeakValueDictionary()


def parts_path(file_name):
    return Path(file_name + ".parts")


def has_parts(file_name):
    return parts_path(file_name).exists()


def discard_partial_download(file_name):
    """Delete only this download's staging files after its writers have stopped."""
    directory = parts_path(file_name)
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
    os.replace(temporary, directory / "manifest.json")


def part_count(size, part_size):
    """Count fixed-size parts, including a shorter final part."""
    return max(1, (size + part_size - 1) // part_size)


def _make_plan(size, workers, identity, import_pending, part_size):
    ranges = [[start, min(start + part_size, size)] for start in range(0, size, part_size)]
    return {"version": 2, "size": size, "identity": identity,
            "ranges": ranges, "workers": min(workers, len(ranges)),
            "part_size": part_size, "import_pending": import_pending}


def _read_plan(directory, size, identity):
    with (directory / "manifest.json").open(encoding="utf-8") as file:
        plan = json.load(file)
    if (plan.get("version") not in (1, 2) or plan.get("size") != size
            or plan.get("identity") != identity):
        raise ValueError("Partial download belongs to a different file")
    ranges = plan.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("Invalid partial download ranges")
    if plan["version"] == 2:
        if type(plan.get("workers")) is not int or not 1 <= plan["workers"] <= len(ranges):
            raise ValueError("Invalid partial download worker count")
        if type(plan.get("part_size")) is not int or plan["part_size"] < CHUNK_SIZE or plan["part_size"] % CHUNK_SIZE:
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
    return plan


async def download_file(client, file_id, size, file_name, identity, parallelism,
                        progress, wait_ready, part_size=64 * CHUNK_SIZE, request_started=None,
                        discard_requested=None):
    """Keep ranges across retries/restarts and validate every received block.

    Workers take the next unfinished part when their current part completes.
    Part lengths are its checkpoints; incomplete blocks are fetched again.
    """
    async def run():
        key = os.path.abspath(file_name)
        lock = _file_locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                return await _download_file(client, file_id, size, file_name, identity,
                                            parallelism, progress, wait_ready, part_size,
                                            request_started)
            finally:
                if discard_requested is not None and discard_requested():
                    discard_partial_download(file_name)

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
                         progress, wait_ready, part_size, request_started):
    if size <= 0 or parallelism < 1 or part_size < CHUNK_SIZE or part_size % CHUNK_SIZE:
        raise ValueError("File size and worker count must be positive; part size must be whole MiB")
    directory = parts_path(file_name)
    manifest = directory / "manifest.json"
    plan = _read_plan(directory, size, identity) if manifest.exists() else None
    await wait_ready()
    legacy = Path(file_name + ".temp")
    # A previous attempt may have finished before its final callback returned.
    output = Path(file_name)
    if plan is not None and output.exists() and output.stat().st_size == size:
        await progress(size, size)
        shutil.rmtree(directory)
        return file_name
    if plan is None:
        directory.mkdir(parents=True, exist_ok=True)
        plan = _make_plan(size, parallelism, identity, legacy.exists(), part_size)
        _save_plan(directory, plan)
    ranges = plan["ranges"]
    paths = [directory / f"{index}.part" for index in range(len(ranges))]

    # Import an old sequential checkpoint without deleting it until copied.
    if plan["import_pending"]:
        if legacy.exists():
            existing = legacy.stat().st_size
            if existing > size:
                raise ValueError("Sequential checkpoint exceeds file size")
            prefix = existing if existing == size else existing // CHUNK_SIZE * CHUNK_SIZE
            with legacy.open("rb") as source:
                for path, (start, end) in zip(paths, ranges):
                    length = max(0, min(end, prefix) - start)
                    with path.open("wb") as target:
                        source.seek(start)
                        while length:
                            await wait_ready()
                            chunk = source.read(min(CHUNK_SIZE, length))
                            if not chunk:
                                raise ConnectionError("Sequential checkpoint ended early")
                            target.write(chunk)
                            length -= len(chunk)
                            await asyncio.sleep(0)
        plan["import_pending"] = False
        _save_plan(directory, plan)
        legacy.unlink(missing_ok=True)

    downloaded = []
    for path, (start, end) in zip(paths, ranges):
        length = path.stat().st_size if path.exists() else 0
        if length > end - start:
            raise ValueError("Partial range exceeds its expected size")
        if length < end - start:
            aligned = length // CHUNK_SIZE * CHUNK_SIZE
            if aligned != length:
                with path.open("r+b") as file:
                    file.truncate(aligned)
            length = aligned
        downloaded.append(length)

    report_lock = asyncio.Lock()

    async def report():
        async with report_lock:
            # Do not persist a completed download until the output exists.
            await progress(min(sum(downloaded), size - 1), size)

    async def stream_progress(*_):
        await wait_ready()

    async def download_range(index, connection):
        start, end = ranges[index]
        if downloaded[index] == end - start:
            return
        await wait_ready()
        offset = (start + downloaded[index]) // CHUNK_SIZE
        remaining = (end - start - downloaded[index] + CHUNK_SIZE - 1) // CHUNK_SIZE
        if request_started:
            request_started(file_id, size, remaining, offset)
        stream = connection.get_file(file_id, size, remaining, offset, stream_progress)
        logger.info("Downloading {} part {}/{} at offset {}", file_name,
                    index + 1, len(ranges), start + downloaded[index])
        try:
            with paths[index].open("ab") as file:
                async for chunk in stream:
                    await wait_ready()
                    expected = min(CHUNK_SIZE, end - start - downloaded[index])
                    if not expected or len(chunk) != expected:
                        raise ConnectionError("Downloaded block has an unexpected length")
                    file.write(chunk)
                    file.flush()
                    downloaded[index] += len(chunk)
                    await report()
            if downloaded[index] != end - start:
                # Pyrogram can swallow network/reference errors in get_file.
                raise ConnectionError("Part download stream ended early")
        finally:
            await stream.aclose()

    await report()
    queue = asyncio.Queue()
    for index, (start, end) in enumerate(ranges):
        if downloaded[index] < end - start:
            queue.put_nowait(index)
    workers = min(plan.get("workers", len(ranges)), queue.qsize())
    logger.info("Downloading {} in {} part(s) with {} worker(s)", file_name, len(ranges), workers)

    async def worker(index):
        part = None
        failures = 0
        while True:
            try:
                async with DownloadConnection(client, f"{file_name} worker {index + 1}/{workers}") as connection:
                    while True:
                        await wait_ready()
                        if part is None:
                            try:
                                part = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                return
                        await download_range(part, connection)
                        part = None
                        failures = 0
                        await asyncio.sleep(0)
            except (ConnectionError, TimeoutError):
                failures += 1
                if part is None or failures >= 3 or getattr(client, "_download_connections_closing", False):
                    raise
                delay = network_backoff(10 * 2 ** (failures - 1))
                logger.warning("{} part {}/{}: network attempt {}/3 failed; "
                               "retrying with a fresh worker connection after {}s",
                               file_name, part + 1, len(ranges), failures, delay)
                # The failed context has closed its session before waiting. Other
                # workers keep their connections and continue draining the queue.
                await _wait_network_retry(delay, wait_ready)

    tasks = [asyncio.create_task(worker(index)) for index in range(workers)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if len(paths) == 1:
        # Single-range downloads need no second full-size temporary copy.
        await wait_ready()
        os.replace(paths[0], file_name)
    else:
        # Parts remain intact if merging is interrupted or the disk fills up.
        with legacy.open("wb") as target:
            for path in paths:
                with path.open("rb") as source:
                    while True:
                        await wait_ready()
                        chunk = source.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        target.write(chunk)
                        await asyncio.sleep(0)
        if legacy.stat().st_size != size:
            raise ConnectionError("Merged download has an unexpected size")
        await wait_ready()
        os.replace(legacy, file_name)
    await progress(size, size)
    shutil.rmtree(directory)
    return file_name
