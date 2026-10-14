"""Fixed-range downloads with one aggregate progress callback per file."""

import asyncio
import json
import os
import shutil
from pathlib import Path
from weakref import WeakValueDictionary

from loguru import logger


CHUNK_SIZE = 1024 * 1024
_file_locks = WeakValueDictionary()


def parts_path(file_name):
    return Path(file_name + ".parts")


def has_parts(file_name):
    return parts_path(file_name).exists()


def _save_plan(directory, plan):
    temporary = directory / "manifest.json.tmp"
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(plan, file)
    os.replace(temporary, directory / "manifest.json")


def _ranges(size, count):
    chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    count = min(count, chunks)
    ranges = []
    for index in range(count):
        start = (chunks * index // count) * CHUNK_SIZE
        end = min((chunks * (index + 1) // count) * CHUNK_SIZE, size)
        ranges.append([start, end])
    return ranges


def split_count(size, requested, minimum):
    """Reduce the initial range count for small files, with at least one."""
    if size <= 0:
        return 1
    return max(1, min(requested, size // minimum))


def _make_plan(size, count, identity, import_pending):
    ranges = _ranges(size, count)
    return {"version": 1, "size": size, "identity": identity,
            "ranges": ranges, "import_pending": import_pending}


def _read_plan(directory, size, identity):
    with (directory / "manifest.json").open(encoding="utf-8") as file:
        plan = json.load(file)
    if (plan.get("version") != 1 or plan.get("size") != size
            or plan.get("identity") != identity):
        raise ValueError("Partial download belongs to a different file")
    ranges = plan.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("Invalid partial download ranges")
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
                        progress, wait_ready, minimum=CHUNK_SIZE):
    """Keep ranges across retries/restarts and validate every received block.

    Each file starts its fixed ranges independently of other files.
    Part lengths are its checkpoints; incomplete blocks are fetched again.
    """
    async def run():
        key = os.path.abspath(file_name)
        lock = _file_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await _download_file(client, file_id, size, file_name, identity,
                                        parallelism, progress, wait_ready, minimum)

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


async def _download_file(client, file_id, size, file_name, identity, parallelism,
                         progress, wait_ready, minimum):
    directory = parts_path(file_name)
    manifest = directory / "manifest.json"
    plan = _read_plan(directory, size, identity) if manifest.exists() else None
    desired = len(plan["ranges"]) if plan else split_count(size, parallelism, minimum)
    # Every file owns its initial ranges; no global slot reservation.
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
        plan = _make_plan(size, desired, identity, legacy.exists())
        _save_plan(directory, plan)
    ranges = plan["ranges"]
    logger.info("Downloading {} in {} fixed range(s)", file_name, len(ranges))
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

    async def download_range(index):
        start, end = ranges[index]
        if downloaded[index] == end - start:
            return
        # Completed ranges are not replaced or redistributed.
        await wait_ready()
        offset = (start + downloaded[index]) // CHUNK_SIZE
        remaining = (end - start - downloaded[index] + CHUNK_SIZE - 1) // CHUNK_SIZE
        stream = client.get_file(file_id, size, remaining, offset,
                                 progress=stream_progress)
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
                raise ConnectionError("Parallel download stream ended early")
        finally:
            await stream.aclose()

    await report()
    tasks = [asyncio.create_task(download_range(index)) for index in range(len(ranges))]
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
