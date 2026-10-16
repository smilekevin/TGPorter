"""Downloads media from telegram."""
import asyncio
import logging
import os
import shutil
import time
from typing import List, Optional, Tuple, Union

from module.telegram import pyrogram
from loguru import logger
from pyrogram.types import Audio, Document, Photo, Video, VideoNote, Voice
from rich.logging import RichHandler

from module.app import Application, ChatDownloadConfig, DownloadStatus, TaskNode
from module.retry_policy import network_backoff
from module.bot import start_download_backend, start_download_bot, stop_download_bot
from module.download_stat import load_downloads, save_downloads, set_chat_title, update_download_status
from module.download_stat import add_failed_download as _add_failed_download
from module.download_stat import track_download_stream
from module.task_store import update_task_progress, update_download_state
from module.get_chat_history_v2 import get_chat_history_v2
from module.parallel_download import (download_file, has_parts, discard_partial_download,
                                      DownloadWorkersExhausted)
from module.language import _t
from module.pyrogram_extension import (
    HookClient,
    fetch_message,
    get_extension,
    record_download_status,
    report_bot_download_status,
    set_max_concurrent_transmissions,
    set_meta_data,
    update_cloud_upload_stat,
    upload_telegram_chat,
)
from module.web import init_web
from utils.format import truncate_filename, validate_title
from utils.log import LogFilter
from utils.meta import print_meta
from utils.meta_data import MetaData

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler()],
)

CONFIG_NAME = "config.yaml"
DATA_FILE_NAME = "data.yaml"
APPLICATION_NAME = "media_downloader"
app = Application(CONFIG_NAME, DATA_FILE_NAME, APPLICATION_NAME)

queue: asyncio.Queue = None  # Created in main() after event loop starts
RETRY_TIME_OUT = 3

# ── Client connection error tracking & auto-reconnect ──
# When download_media hits connection-level errors (TimeoutError, OSError,
# FILE_REFERENCE_EXPIRED) consecutively, the Pyrogram session's underlying
# TCP connection is likely in a "half-dead" state — socket is alive but
# no data flows. Pyrogram's internal retry (3x) uses the same dead socket,
# so all retries fail. Auto-reconnect forces client.stop()+start() to build
# a fresh TCP connection + MTProto session, mirroring the manual fix of
# toggling the v2rayA node.
_client_conn_errors = {"count": 0}
_CLIENT_RECONNECT_THRESHOLD = 10
_client_reconnecting = {"active": False}
_client_last_reconnect = {"time": 0.0}
_CLIENT_RECONNECT_COOLDOWN = 300  # 5 min between reconnect attempts
_MAX_WATCHDOG_RETRIES = 2  # Maximum retries after watchdog cancellation.
_watchdog_retry_count = {}  # Retry counts by task_id.
_main_client_ref = {"client": None}  # set in start_server()

# Reconnect once when silent throttling exceeds the threshold, similar to toggling a proxy node.
# A shared timer avoids competing reconnects because throttling affects the account or node.
_THROTTLE_RESTART_AFTER = 24 * 3600  # No download progress for 24 hours.
_THROTTLE_RESTART_MAX = 3            # Warn only after N ineffective reconnects.
_throttle_state = {
    "last_progress": 0.0,   # Latest global progress heartbeat; zero means uninitialized.
    "restarts": 0,          # Consecutive restarts reset when progress returns.
}

# Resize the worker pool immediately when WebUI changes max_download_task.
import threading as _threading
_worker_pool_lock = _threading.Lock()
_live_workers = 0          # Live workers: increment at startup and decrement on exit.
_worker_exit_signal = False  # Surplus workers exit before taking another task when shrinking the pool.


async def _spawn_one_worker(client):
    """Create one worker on the main event loop via run_coroutine_threadsafe."""
    import asyncio as _aio
    _aio.get_running_loop().create_task(worker(client))


def _worker_pool_set_target(n: int):
    """Set the target worker count and resize safely, including from Flask threads."""
    global _worker_exit_signal, _live_workers
    with _worker_pool_lock:
        cur = _live_workers
    if n > cur:
        # This may run in a Flask thread without an event loop. Schedule worker creation on the
        # main Pyrogram loop.
        import asyncio as _aio
        added = 0
        client = _main_client_ref.get("client")
        loop = client.loop if client and getattr(client, "loop", None) else None
        for _ in range(n - cur):
            try:
                if loop and loop.is_running():
                    # Submit through run_coroutine_threadsafe to cross the thread boundary.
                    fut = _aio.run_coroutine_threadsafe(_spawn_one_worker(client), loop)
                    fut.result(timeout=10)
                else:
                    _aio.get_running_loop().create_task(worker(client))
                added += 1
            except Exception as e:
                logger.error(
                    f"Worker pool: failed to spawn worker: {e} "
                    f"(loop={loop}, is_running={loop.is_running() if loop else None}, "
                    f"cur={cur}, n={n})"
                )
                break
        logger.info(f"Worker pool: scaled up {cur} -> {cur + added} workers")
    elif n < cur:
        # Wake queue.get() waiters with sentinels. Surplus workers exit; retained workers ignore
        # extra sentinels.
        global queue
        _worker_exit_signal = True
        excess = cur - n
        for _ in range(cur):  # Wake all waiting workers.
            try:
                queue.put_nowait(None)
            except Exception:
                pass
        logger.info(f"Worker pool: scaling down {cur} -> {n} workers ({excess} excess)")
    return cur

logging.getLogger("pyrogram.session.session").addFilter(LogFilter())
logging.getLogger("pyrogram.client").addFilter(LogFilter())

logging.getLogger("pyrogram").setLevel(logging.WARNING)


def _check_download_finish(media_size: int, download_path: str, ui_file_name: str):
    """Check whether a download is complete.

    Preserve size-mismatched partial files as .temp so the next attempt can resume
    through _resume_download_from_temp."""
    download_size = os.path.getsize(download_path)
    if media_size == download_size:
        logger.success(f"{_t('Successfully downloaded')} - {ui_file_name}")
    else:
        logger.warning(
            f"{_t('Media downloaded with wrong size')}: "
            f"{download_size}, {_t('actual')}: "
            f"{media_size}, {_t('file name')}: {ui_file_name}"
        )
        # Preserve partial data as .temp so the next attempt can resume it.
        temp_path = download_path + ".temp"
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            os.rename(download_path, temp_path)
            logger.info(
                f"Partial file preserved for resume: {temp_path} "
                f"({download_size} bytes)"
            )
        except OSError as rename_err:
            logger.warning(f"Could not preserve partial file: {rename_err}")
            try:
                os.remove(download_path)
            except OSError:
                pass
        raise pyrogram.errors.exceptions.bad_request_400.BadRequest()


def _move_to_download_path(temp_download_path: str, download_path: str):
    """Move file to download path"""
    directory, _ = os.path.split(download_path)
    os.makedirs(directory, exist_ok=True)
    shutil.move(temp_download_path, download_path)


def _check_timeout(retry: int, _: int):
    """Check if message download timeout"""
    if retry == 2:
        return True
    return False


def _can_download(_type: str, file_formats: dict, file_format: Optional[str]) -> bool:
    """Check if the given file format can be downloaded."""
    if _type in ["audio", "document", "video"]:
        allowed_formats: list = file_formats[_type]
        if not file_format in allowed_formats and allowed_formats[0] != "all":
            return False
    return True


def _is_exist(file_path: str) -> bool:
    """Check if a file exists and it is not a directory."""
    return not os.path.isdir(file_path) and os.path.exists(file_path)


def _cleanup_temp_file(temp_file_name: str):
    """Remove a temporary file only when it is empty.

    Unconditional deletion could destroy resumable data restored to temp_file_name,
    including files without a .temp suffix. Keep nonempty data, matching the
    Pyrogram checkpoint-preservation patch; remove empty shells to avoid clutter."""
    if temp_file_name and os.path.exists(temp_file_name):
        try:
            if os.path.getsize(temp_file_name) == 0:
                os.remove(temp_file_name)
        except OSError:
            pass


_MB = 1024 * 1024


async def _download_parallel_media(client, message, temp_file_name, media_size,
                                   node, ui_file_name, task_start_time, parallelism, part_size):
    from pyrogram.file_id import FileId
    from module.download_stat import wait_download_ready, is_download_discarded

    media = next((getattr(message, kind, None) for kind in (
        "video", "document", "audio", "photo", "voice", "video_note", "animation"
    ) if getattr(message, kind, None) is not None), None)
    if media is None:
        raise ValueError("This message doesn't contain any downloadable media")
    file_id = FileId.decode(media.file_id)
    identity = getattr(media, "file_unique_id", None) or f"{file_id.dc_id}:{file_id.media_id}"

    first_progress = True

    async def progress(current, total):
        nonlocal first_progress
        await update_download_status(current, total, message.id, ui_file_name,
                                     task_start_time, node, client, initial=first_progress)
        first_progress = False

    async def wait_ready():
        await wait_download_ready(message.id, node, client)

    async def refresh_file_id():
        nonlocal message
        refreshed = await fetch_message(client, message)
        if refreshed is None:
            raise ValueError("Message missing or inaccessible to this account")
        fresh_media = next((getattr(refreshed, kind, None) for kind in (
            "video", "document", "audio", "photo", "voice", "video_note", "animation"
        ) if getattr(refreshed, kind, None) is not None), None)
        if fresh_media is None:
            raise ValueError("Refreshed message has no downloadable media")
        fresh_id = FileId.decode(fresh_media.file_id)
        fresh_identity = getattr(fresh_media, "file_unique_id", None) or f"{fresh_id.dc_id}:{fresh_id.media_id}"
        if fresh_identity != identity or fresh_media.file_size != media_size:
            raise ValueError("Refreshed message contains a different file")
        message = refreshed
        return fresh_id

    from module.pyrogram_extension import get_flood_wait_remaining

    return await download_file(client, file_id, media_size, temp_file_name,
                               identity, parallelism,
                               progress, wait_ready, part_size,
                               discard_requested=lambda: is_download_discarded(message.id, node),
                               refresh_file_id=refresh_file_id,
                               flood_wait=_publish_download_flood,
                               flood_remaining=get_flood_wait_remaining,
                               stream_context=lambda: track_download_stream(
                                   node.chat_id, message.id, node.task_id))


async def _resume_download_from_temp(
    client: pyrogram.client.Client,
    message: pyrogram.types.Message,
    temp_file_name: str,
    media_size: int,
    node,
    ui_file_name: str,
    task_start_time: float,
) -> bool:
    """Resume an existing .temp file from a 1 MB aligned boundary.

    Pyrogram handle_download opens with "wb" at offset zero on each call. Bypass it
    by reading the checkpoint size, truncating to the MTProto 1 MB alignment,
    extracting FileId from a freshly fetched message, and appending chunks from
    client.get_file(file_id, size, 0, offset_mb).
    Return True on completion so the caller can validate the file; propagate
    exceptions through the caller's existing retry flow."""
    from pyrogram.file_id import FileId
    from pyrogram import utils as pyrogram_utils
    import functools

    if not temp_file_name:
        return False
    # Pyrogram writes to <temp_file_name>.temp; temp_file_name itself has no suffix. Check the
    # actual checkpoint path.
    real_temp = temp_file_name if temp_file_name.endswith(".temp") else temp_file_name + ".temp"
    if not os.path.exists(real_temp):
        return False

    existing = os.path.getsize(real_temp)
    if existing <= 0:
        return False
    if media_size > 0 and existing >= media_size:
        # The temporary file is complete; let the caller validate it.
        return False

    aligned = (existing // _MB) * _MB
    offset_mb = aligned // _MB
    if aligned != existing:
        # Truncate to a 1 MB boundary, discarding the trailing partial block.
        try:
            os.truncate(real_temp, aligned)
        except OSError as e:
            logger.warning(f"Resume: truncate failed: {e}, fallback to full re-download")
            return False

    if aligned <= 0:
        return False

    logger.info(
        f"Resume download: {ui_file_name} from {aligned/1024/1024:.0f}MB "
        f"(had {existing} bytes, aligned to {aligned})"
    )

    # Extract FileId from the message after fetch_message refreshes its file_reference.
    media = getattr(message, "video", None) or getattr(message, "document", None) \
        or getattr(message, "audio", None) or getattr(message, "photo", None) \
        or getattr(message, "voice", None) or getattr(message, "video_note", None) \
        or getattr(message, "animation", None)
    if media is None:
        logger.warning("Resume: no downloadable media in refreshed message")
        return False
    file_id_str = media if isinstance(media, str) else media.file_id
    file_id_obj = FileId.decode(file_id_str)
    real_size = getattr(media, "file_size", 0) or media_size

    from module.download_stat import update_download_status

    # The outer worker pool limits simultaneous files.
    with track_download_stream(node.chat_id, message.id, node.task_id), open(real_temp, "r+b") as f:
        f.seek(aligned)
        async for chunk in client.get_file(
            file_id_obj, real_size, 0, offset_mb,
            progress=update_download_status,
            progress_args=(message.id, ui_file_name, task_start_time, node, client),
        ):
            f.write(chunk)

    final_size = os.path.getsize(real_temp)
    # A truncated get_file stream can end normally without raising. Explicitly reject it so
    # callers do not rename it as complete and later truncate its checkpoint with open("wb").
    if media_size > 0 and final_size < media_size:
        raise ConnectionError(
            f"resume stream ended early: {final_size}/{media_size} bytes"
        )
    logger.info(
        f"Resume download finished: {ui_file_name} now {final_size} bytes "
        f"(expected {media_size})"
    )
    # Restore <name>.temp to temp_file_name after completion, matching Pyrogram's success path
    # expected by finish validation and file moves.
    if real_temp != temp_file_name and os.path.exists(temp_file_name):
        os.remove(temp_file_name)
    os.rename(real_temp, temp_file_name)
    return True


def _cleanup_stale_temp_files():
    """Remove stale temp files on startup.

    Rules:
    - 0-byte .temp files: always delete (empty shells from failed downloads)
    - Non-zero .temp files: delete if corresponding target file exists in downloads/
    - Empty directories in temp/: delete
    """
    temp_dir = app.temp_save_path
    if not os.path.isdir(temp_dir):
        return

    removed = 0
    for root, dirs, files in os.walk(temp_dir, topdown=False):
        for f in files:
            if not f.endswith('.temp'):
                continue
            temp_path = os.path.join(root, f)
            try:
                file_size = os.path.getsize(temp_path)
            except OSError:
                continue

            if file_size == 0:
                # Empty temp file — always remove
                try:
                    os.remove(temp_path)
                    removed += 1
                except OSError:
                    pass
            else:
                # Non-zero: check if target already exists in downloads/
                # temp path: temp/chat_id/filename.ext.temp
                # target path: downloads/chat_id/filename.ext
                rel_path = os.path.relpath(temp_path, temp_dir)
                # Strip .temp suffix to get the target filename
                target_name = f[:-5] if f.endswith('.temp') else f
                target_path = os.path.join(
                    os.path.abspath("."), "downloads",
                    os.path.dirname(rel_path), target_name
                )
                if os.path.exists(target_path):
                    try:
                        target_size = os.path.getsize(target_path)
                        if target_size >= file_size:
                            os.remove(temp_path)
                            removed += 1
                    except OSError:
                        pass

        # Remove empty directories
        for d in dirs:
            dir_path = os.path.join(root, d)
            try:
                if not os.listdir(dir_path):
                    os.rmdir(dir_path)
            except OSError:
                pass

    if removed:
        logger.info(f"Startup cleanup: removed {removed} stale temp files")


async def _get_media_meta(
    chat_id: Union[int, str],
    message: pyrogram.types.Message,
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice],
    _type: str,
) -> Tuple[str, str, Optional[str]]:
    """Extract file name and file id from media object."""
    if _type in ["audio", "document", "video"]:
        file_format: Optional[str] = media_obj.mime_type.split("/")[-1]
    else:
        file_format = None

    file_name = None
    temp_file_name = None
    dirname = validate_title(f"{chat_id}")
    if message.chat and message.chat.title:
        dirname = validate_title(f"{message.chat.title}")

    if message.date:
        datetime_dir_name = message.date.strftime(app.date_format)
    else:
        datetime_dir_name = "0"

    if _type in ["voice", "video_note"]:
        file_format = media_obj.mime_type.split("/")[-1]
        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)
        file_name = "{} - {}_{}.{}".format(
            message.id, _type, media_obj.date.isoformat(), file_format,
        )
        file_name = validate_title(file_name)
        temp_file_name = os.path.join(app.temp_save_path, dirname, file_name)
        file_name = os.path.join(file_save_path, file_name)
    else:
        file_name = getattr(media_obj, "file_name", None)
        caption = getattr(message, "caption", None)

        file_name_suffix = ".unknown"
        if not file_name:
            file_name_suffix = get_extension(
                media_obj.file_id, getattr(media_obj, "mime_type", "")
            )
        else:
            _, file_name_without_suffix = os.path.split(os.path.normpath(file_name))
            file_name, file_name_suffix = os.path.splitext(file_name_without_suffix)
            if not file_name_suffix:
                file_name_suffix = get_extension(
                    media_obj.file_id, getattr(media_obj, "mime_type", "")
                )

        if caption:
            caption = validate_title(caption)
            app.set_caption_name(chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(chat_id, message.media_group_id)

        if not file_name and message.photo:
            file_name = f"{message.photo.file_unique_id}"

        gen_file_name = (
            app.get_file_name(message.id, file_name, caption) + file_name_suffix
        )
        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)
        temp_file_name = os.path.join(app.temp_save_path, dirname, gen_file_name)
        file_name = os.path.join(file_save_path, gen_file_name)
    return truncate_filename(file_name), truncate_filename(temp_file_name), file_format


def _move_task_to_failed(node, message, error_message):
    """Move a task to failures, clear its placeholder and remove its stored task."""
    try:
        from module.download_stat import add_failed_download, delete_download_result_entry as _ddre
        from module.task_store import complete_task as _ct
        if node and node.task_id:
            msg_id = message.id if message else 0
            source_link = ""
            if getattr(node, 'source_chat_id', 0) and getattr(node, 'source_message_id', 0):
                sid = node.source_chat_id
                link_id = str(sid)[4:] if str(sid).startswith("-100") else str(sid)
                source_link = f"https://t.me/c/{link_id}/{node.source_message_id}"
            add_failed_download(
                chat_id=node.chat_id,
                msg_id=msg_id,
                task_id=getattr(node, "task_id_display", str(node.task_id)),
                file_name="",
                error_message=error_message,
                total_size=0,
                source_link=source_link,
                from_user_id=str(getattr(node, "from_user_id", "")) or "",
            )
            _ddre(node.chat_id, msg_id)
            _ct(node.task_id)
            _watchdog_retry_count.pop(node.task_id, None)
    except Exception:
        pass


async def _reset_task_for_retry(node, message):
    """Reset node state before requeueing a download."""
    from module.pyrogram_extension import remove_download_cache
    from module.download_stat import delete_download_result_entry as _ddre
    import time as _time
    # Clear download_cache so record_download_status cannot short-circuit as Downloading. Cache
    # has no pop(); use remove_download_cache to update its internal store.
    remove_download_cache(node.chat_id, message.id)
    # Clear the _download_result placeholder.
    _ddre(node.chat_id, message.id)
    # Reset the node state.
    node.total_task = 0
    node.total_download_task = 0
    node.success_download_task = 0
    node.failed_download_task = 0
    node.skip_download_task = 0
    node.download_status = {}
    node.last_reply_time = _time.time()
    node.last_edit_msg = ""
    node.last_bot_flood_notice = _time.time()
    node.initial_progress_reported = False


async def add_download_task(message: pyrogram.types.Message, node: TaskNode):
    """Add Download task"""
    if message.empty:
        return False
    if queue is None:
        logger.error(f"add_download_task: queue is None! msg {message.id} cannot be queued")
        return False
    node.download_status[message.id] = DownloadStatus.Downloading
    await queue.put((message, node))
    node.total_task += 1
    logger.info(f"add_download_task: put msg {message.id} into queue (size now {queue.qsize()}), task {getattr(node, 'task_id_display', node.task_id)}")
    return True


async def save_msg_to_file(app, chat_id: Union[int, str], message: pyrogram.types.Message):
    """Write message text into file"""
    dirname = validate_title(
        message.chat.title if message.chat and message.chat.title else str(chat_id)
    )
    datetime_dir_name = message.date.strftime(app.date_format) if message.date else "0"
    file_save_path = app.get_file_save_path("msg", dirname, datetime_dir_name)
    file_name = os.path.join(
        app.temp_save_path, file_save_path,
        f"{app.get_file_name(message.id, None, None)}.txt",
    )
    os.makedirs(os.path.dirname(file_name), exist_ok=True)
    if _is_exist(file_name):
        return DownloadStatus.SkipDownload, None
    with open(file_name, "w", encoding="utf-8") as f:
        f.write(message.text or "")
    return DownloadStatus.SuccessDownload, file_name


async def download_task(client: pyrogram.Client, message: pyrogram.types.Message, node: TaskNode):
    """Download and Forward media"""
    download_status, file_name, error_message = await download_media(
        client, message, app.media_types, app.file_formats, node
    )
    temp_file_name = node.download_temp_paths.pop(message.id, "")
    # Backfill source_chat_title from cache (populated during download_media)
    if not node.source_chat_title and getattr(node, 'source_chat_id', 0):
        from module.download_stat import get_chat_title as _gct
        cached = _gct(node.source_chat_id)
        if cached:
            node.source_chat_title = cached
    if app.enable_download_txt and message.text and not message.media:
        download_status, file_name = await save_msg_to_file(app, node.chat_id, message)
    if not node.bot:
        app.set_download_id(node, message.id, download_status)
    node.download_status[message.id] = download_status
    file_size = os.path.getsize(file_name) if file_name else 0
    # Record failed downloads to the failed list for webui display
    if download_status is DownloadStatus.FailedDownload:
        # Get task_id_display (format: MMDD-N)
        task_id_display = getattr(node, "task_id_display", "") or str(node.task_id)
        # Build source link from node or message
        source_link = ""
        if getattr(node, 'source_chat_id', 0) and getattr(node, 'source_message_id', 0):
            # For forwarded messages, use source channel link
            source_id = node.source_chat_id
            if str(source_id).startswith("-100"):
                link_id = str(source_id)[4:]
            else:
                link_id = str(source_id)
            source_link = f"https://t.me/c/{link_id}/{node.source_message_id}"
        elif message and message.chat:
            # For direct messages, use current message link
            chat_id_for_link = message.chat.id
            if hasattr(message.chat, 'username') and message.chat.username:
                source_link = f"https://t.me/{message.chat.username}/{message.id}"
            else:
                if str(chat_id_for_link).startswith("-100"):
                    link_id = str(chat_id_for_link)[4:]
                else:
                    link_id = str(chat_id_for_link)
                source_link = f"https://t.me/c/{link_id}/{message.id}"
        _add_failed_download(
            chat_id=node.chat_id,
            msg_id=message.id if message else message_id,
            task_id=task_id_display,
            file_name=file_name or "",
            temp_file_name=temp_file_name,
            error_message=error_message or 'Download failed',
            total_size=file_size,
            source_link=source_link,
            from_user_id=getattr(node, "from_user_id", "") or "",
        )
        # Remove from active download list so it doesn't stay in WebUI forever
        from module.download_stat import delete_download_result_entry as _ddre
        _ddre(node.chat_id, message.id if message else message_id)
    elif download_status is DownloadStatus.SkipDownload:
        # Remove placeholder from active download list
        from module.download_stat import delete_download_result_entry as _ddre
        _ddre(node.chat_id, message.id if message else message_id)
    await upload_telegram_chat(
        client, node.upload_user if node.upload_user else client,
        app, node, message, download_status, file_name,
    )
    if not node.upload_telegram_chat_id and download_status is DownloadStatus.SuccessDownload:
        ui_file_name = file_name
        if app.hide_file_name:
            ui_file_name = f"****{os.path.splitext(file_name)[-1]}"
        if await app.upload_file(file_name, update_cloud_upload_stat, (node, message.id, ui_file_name)):
            node.upload_success_count += 1
    await report_bot_download_status(node.bot, node, download_status, file_size)
    # Send final status with full stats immediately for single downloads
    if node.bot and node.is_finish() and not node.is_stop_transmission:
        from module.pyrogram_extension import report_bot_status
        try:
            await report_bot_status(node.bot, node, immediate_reply=True)
        except Exception as e:
            logger.warning(f"Failed to send final bot status for task {node.task_id}: {e}")
        try:
            from module.task_store import complete_task as _ct
            _ct(node.task_id)
        except Exception as e:
            logger.warning(f"Failed to complete task {node.task_id}: {e}")


def _publish_download_flood(seconds):
    """Share download cooldowns without manually pausing healthy connections."""
    from module.pyrogram_extension import _unified_flood_wait

    _unified_flood_wait["until"] = max(
        _unified_flood_wait["until"], time.time() + seconds + 5)
    _unified_flood_wait["reason"] = "FLOOD_WAIT"


async def _wait_download_flood(seconds, message_id, node, client):
    """Automatically resume rate-limit waits while honoring manual controls."""
    from module.download_stat import wait_download_ready
    from module.pyrogram_extension import get_flood_wait_remaining, send_bot_notification

    _publish_download_flood(seconds)
    deadline = time.monotonic() + seconds
    notice = (
        f"⏳ {_t('Telegram rate limit pause')}\n"
        f"{_t('Task')}: {node.task_id_display}\n"
        f"{_t('Message ID')}: {message_id}\n"
        + _t("FLOOD_WAIT: {seconds} seconds remaining", seconds=seconds)
    )
    notice += "\n" + _t("Retry automatically after {seconds} seconds", seconds=seconds)
    notified = await send_bot_notification(node.bot, node.from_user_id, notice)
    while True:
        try:
            await wait_download_ready(message_id, node, client)
        except pyrogram.StopTransmission:
            return False
        remaining = max(deadline - time.monotonic(), get_flood_wait_remaining())
        if remaining <= 0:
            if notified:
                await send_bot_notification(
                    node.bot, node.from_user_id,
                    f"✅ {_t('Telegram rate limit cleared; resuming download')}\n"
                    f"{_t('Task')}: {node.task_id_display}\n"
                    f"{_t('Message ID')}: {message_id}",
                )
            return True
        await asyncio.sleep(min(1, remaining))


@record_download_status
async def download_media(
    client: pyrogram.client.Client,
    message: pyrogram.types.Message,
    media_types: List[str],
    file_formats: dict,
    node: TaskNode,
):
    """Download media from Telegram. Each file retried 3 times with 5s delay.
    Returns: (DownloadStatus, file_name, error_message)
    """
    file_name: str = ""
    ui_file_name: str = ""
    task_start_time: float = time.time()
    media_size = 0
    _media = None
    error_message = ""  # Track specific error reason
    # Skip the initial fetch_message — the pending consumer already called
    # get_messages seconds ago, so the file reference is fresh. The extra
    # get_messages call here was causing indefinite blocks when TG rate-limits
    # the same chat. If the file reference does expire during download, the
    # retry loop below will call fetch_message to refresh it.

    # Cache chat title from message object
    if message and message.chat:
        chat_title = getattr(message.chat, 'title', None) or getattr(message.chat, 'first_name', None)
        if chat_title:
            set_chat_title(message.chat.id, chat_title)
    try:
        for _type in media_types:
            _media = getattr(message, _type, None)
            if _media is None:
                continue
            file_name, temp_file_name, file_format = await _get_media_meta(
                node.chat_id, message, _media, _type
            )
            # Keep the exact staging path for active checks and failed-task cleanup.
            node.download_temp_paths[message.id] = temp_file_name
            media_size = getattr(_media, "file_size", 0)
            ui_file_name = file_name
            if app.hide_file_name:
                ui_file_name = f"****{os.path.splitext(file_name)[-1]}"

            if _can_download(_type, file_formats, file_format):
                if _is_exist(file_name):
                    file_size = os.path.getsize(file_name)
                    if media_size > 0 and file_size >= media_size:
                        logger.info(
                            f"id={message.id} {ui_file_name} "
                            f"{_t('already download,download skipped')}."
                        )
                        return DownloadStatus.SkipDownload, None, ""
                    elif file_size > 0:
                        # Back up the existing file as .bak; remove the backup only after
                        # success.
                        backup_path = file_name + ".bak"
                        os.replace(file_name, backup_path)
                        logger.info(
                            f"id={message.id} {ui_file_name} "
                            f"{_t('File exists but size mismatch')}: "
                            f"{file_size} != {media_size}, {_t('re-downloading')}."
                        )
            else:
                return DownloadStatus.SkipDownload, None, ""
            break
    except Exception as e:
        logger.error(
            f"Message[{message.id}]: "
            f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
            exc_info=True,
        )
        return DownloadStatus.SkipDownload, None, ""
    if _media is None:
        logger.warning(f"Message[{message.id}]: no media found in message, skipping download")
        return DownloadStatus.SkipDownload, None, ""
    # Snapshot once. A saved range plan takes precedence after a restart.
    file_parallelism = getattr(app, "file_download_concurrency", 1)
    part_size = getattr(app, "download_part_size_mb", 64) * _MB
    use_parallel = has_parts(temp_file_name) or (
        media_size > 0 and hasattr(app, "file_download_concurrency")
    )
    # Build source link from message for failed downloads
    source_link = ""
    if message and message.chat:
        chat_id_for_link = message.chat.id
        # For private chats (user bot), use username if available
        if hasattr(message.chat, 'username') and message.chat.username:
            source_link = f"https://t.me/{message.chat.username}/{message.id}"
        else:
            # For channels/supergroups, use c/ prefix
            # Remove -100 prefix for channels
            if str(chat_id_for_link).startswith("-100"):
                link_id = str(chat_id_for_link)[4:]
            else:
                link_id = str(chat_id_for_link)
            source_link = f"https://t.me/c/{link_id}/{message.id}"

    message_id = message.id
    total_wait = 0
    # Manage retry explicitly: early stream termination uses independent backoff without
    # consuming the three download attempts. Other exceptions still advance retry.
    retry = -1
    while retry < 2:
        retry += 1
        # Resume existing Pyrogram checkpoint data before each attempt. The on-disk path
        # includes the .temp suffix.
        _pyrogram_temp = (temp_file_name + ".temp") if temp_file_name else None
        # Never enter client.download_media after a failed resume while .temp holds data:
        # handle_download opens with "wb" and would erase all saved progress.
        _resume_attempted = False
        if not use_parallel and _pyrogram_temp and os.path.exists(_pyrogram_temp) \
                and os.path.getsize(_pyrogram_temp) > 0 \
                and not (media_size > 0 and os.path.getsize(_pyrogram_temp) >= media_size):
            _resume_attempted = True
            _pre_size = os.path.getsize(_pyrogram_temp) if os.path.exists(_pyrogram_temp) else 0
            if _pre_size > 0:
                try:
                    # Always refresh message references before resuming. Previous references may
                    # have expired; fetching is idempotent and inexpensive.
                    try:
                        message = await fetch_message(client, message)
                        if message is None:
                            logger.error(f"Message[{message_id}] {ui_file_name}: Message deleted; stopping resume")
                            error_message = 'Message missing or inaccessible to this account'
                            break
                    except Exception as fetch_err:
                        logger.warning(f"Message[{message_id}] {ui_file_name}: fetch_message failed: {fetch_err}")
                    resumed = await _resume_download_from_temp(
                        client, message, temp_file_name, media_size,
                        node, ui_file_name, task_start_time,
                    )
                    if resumed:
                        _check_download_finish(media_size, temp_file_name, ui_file_name)
                        await asyncio.sleep(0.5)
                        _move_to_download_path(temp_file_name, file_name)
                        _client_conn_errors["count"] = 0
                        logger.info(f"Message[{message.id}] {ui_file_name}: Checkpoint resume complete (pre-loop)")
                        return DownloadStatus.SuccessDownload, file_name, ""
                except pyrogram.StopTransmission:
                    from module.download_stat import is_download_discarded
                    if is_download_discarded(message_id, node):
                        discard_partial_download(temp_file_name)
                    return DownloadStatus.SkipDownload, None, ""
                except pyrogram.errors.exceptions.bad_request_400.BadRequest:
                    # Preserve the temporary file and retry if its resumed size is still
                    # incorrect.
                    logger.warning(f"Message[{message.id}]: resume pre-loop size mismatch, will retry")
                    error_message = 'Size mismatch after resume'
                except Exception as resume_err:
                    # Do not fall back to client.download_media: open("wb") would erase the
                    # checkpoint. Keep .temp and retry with a fresh reference.
                    logger.warning(
                        f"Message[{message.id}] {ui_file_name}: pre-loop resume failed: "
                        f"{type(resume_err).__name__}: {str(resume_err)[:100]}, "
                        f"retry with fresh reference (temp preserved)"
                    )
                    # Network failures use independent backoff rather than the three-attempt
                    # download budget, matching early stream termination. Brief network
                    # instability must not exhaust the task.
                    _err_str = str(resume_err)
                    _is_network_err = (
                        isinstance(resume_err, (ConnectionError, TimeoutError, OSError))
                        or "timed out" in _err_str.lower()
                        or "timeout" in _err_str.lower()
                        or "connection" in _err_str.lower()
                        or "flood" in _err_str.lower()
                    )
                    if _is_network_err:
                        # Retry network resume errors indefinitely; only missing-message errors
                        # fail the task. Cap backoff at 30 seconds to limit requests; zombie
                        # recovery can periodically requeue a stalled worker.
                        _net_retry = getattr(node, "_resume_net_retries", 0) + 1
                        node._resume_net_retries = _net_retry
                        # Honor the server's FloodWait/FloodPremiumWait duration without
                        # consuming retry counters.
                        import re as _re
                        _fw_m = _re.search(
                            r"FLOOD_(?:PREMIUM_)?WAIT_(\d+).*?wait of (\d+) seconds?",
                            _err_str, _re.IGNORECASE,
                        )
                        if _fw_m:
                            _fw = int(_fw_m.group(1) or _fw_m.group(2))
                            logger.warning(
                                f"Message[{message.id}] {ui_file_name}: "
                                f"resume FloodWait {_fw}s — sleeping, no retry counted"
                            )
                            if not await _wait_download_flood(_fw, message_id, node, client):
                                return DownloadStatus.SkipDownload, None, ""
                            retry -= 1
                            continue
                        _backoff = network_backoff(min(15 * (2 ** (_net_retry - 1)), 30))
                        logger.warning(
                            f"Message[{message.id}] {ui_file_name}: "
                            f"resume network error (retry {_net_retry}), "
                            f"backoff {_backoff}s (does NOT consume download retries)"
                        )
                        error_message = f'Resume network error; retry in {_backoff} seconds'
                        # Backoff no longer touches heartbeats: the watchdog does not cancel for
                        # ordinary inactivity, so heartbeats represent actual download progress
                        # only.
                        await asyncio.sleep(_backoff)
                        retry -= 1  # This while-loop iteration does not consume a download attempt.
                        continue
                    error_message = f'Resume failed: {str(resume_err)[:60]}'
                    await asyncio.sleep(RETRY_TIME_OUT)
                    continue
            elif _resume_attempted:
                # Even when resume returns False after passing prechecks, do not call
                # download_media and truncate .temp. Refresh the media reference on the next
                # attempt.
                logger.warning(
                    f"Message[{message_id}] {ui_file_name}: "
                    f"resume skipped (no media in refreshed message), "
                    f"temp preserved, retrying"
                )
                error_message = 'Resume skipped: refreshed message has no media'
                # Use a separate counter to prevent a loop without consuming the three download
                # attempts, matching network backoff.
                _skip_retry = getattr(node, "_resume_skip_retries", 0) + 1
                node._resume_skip_retries = _skip_retry
                if _skip_retry > 6:
                    logger.error(
                        f"Message[{message_id}] {ui_file_name}: "
                        f"resume skipped x{_skip_retry}, giving up (temp preserved)"
                    )
                    error_message = 'Resume skipped too often: refreshed message still has no media'
                    break
                await asyncio.sleep(RETRY_TIME_OUT)
                retry -= 1  # This iteration does not consume a download attempt.
                continue
        if _resume_attempted and _pyrogram_temp and os.path.exists(_pyrogram_temp) \
                and os.path.getsize(_pyrogram_temp) > 0:
            # Safety guard: while .temp contains data, never enter client.download_media because
            # it can truncate the checkpoint.
            logger.warning(
                f"Message[{message_id}] {ui_file_name}: "
                f"skip download_media to protect .temp "
                f"({os.path.getsize(_pyrogram_temp)} bytes), retrying"
            )
            error_message = 'Checkpoint preserved; waiting to resume'
            # Use a separate guard counter instead of consuming download attempts, matching
            # skipped resumes.
            _prot_retry = getattr(node, "_protect_retries", 0) + 1
            node._protect_retries = _prot_retry
            if _prot_retry > 6:
                logger.error(
                    f"Message[{message_id}] {ui_file_name}: "
                    f"protect-block x{_prot_retry}, giving up (temp preserved)"
                )
                error_message = 'Checkpoint guard exhausted: resume remains incomplete'
                break
            await asyncio.sleep(RETRY_TIME_OUT)
            retry -= 1  # This iteration does not consume a download attempt.
            continue
        temp_download_path = None
        try:
            if use_parallel:
                temp_download_path = await _download_parallel_media(
                    client, message, temp_file_name, media_size, node,
                    ui_file_name, task_start_time, file_parallelism, part_size,
                )
            else:
                with track_download_stream(node.chat_id, message_id, node.task_id):
                    temp_download_path = await client.download_media(
                        message, file_name=temp_file_name,
                        progress=update_download_status,
                        progress_args=(message_id, ui_file_name, task_start_time, node, client),
                    )
            from module.download_stat import wait_download_ready
            await wait_download_ready(message_id, node, client)
            if temp_download_path and isinstance(temp_download_path, str):
                _check_download_finish(media_size, temp_download_path, ui_file_name)
                await asyncio.sleep(0.5)
                await wait_download_ready(message_id, node, client)
                _move_to_download_path(temp_download_path, file_name)
                # Remove the .bak backup after a successful download.
                bak_path = file_name + ".bak"
                if os.path.exists(bak_path):
                    try:
                        os.remove(bak_path)
                    except OSError:
                        pass
                _client_conn_errors["count"] = 0  # Reset on success
                return DownloadStatus.SuccessDownload, file_name, ""
            else:
                # download_media returned None or non-str — Pyrogram couldn't fetch
                # without raising. Log details and set error_message for user.
                reason = 'Download returned no file' if temp_download_path is None else f'Download returned unexpected type: {type(temp_download_path).__name__}'
                logger.warning(
                    f"Message[{message.id}] {ui_file_name}: "
                    f"client.download_media returned {repr(temp_download_path)}, "
                    f"retry {retry + 1}/3"
                )
                error_message = reason
                await asyncio.sleep(RETRY_TIME_OUT)
                message = await fetch_message(client, message)
                if message is None:
                    logger.error(f"Message[{message_id}] {ui_file_name}: fetch_message returned None, message may be deleted")
                    error_message = 'Message not found or deleted'
                    break
                if _check_timeout(retry, message.id):
                    logger.error(
                        f"Message[{message.id}] {ui_file_name}: "
                        f"download_media returned None/empty after 3 retries."
                    )
                    if error_message:
                        error_message = f'{error_message} (failed after 3 retries)'
        except pyrogram.StopTransmission:
            from module.download_stat import is_download_discarded
            if is_download_discarded(message_id, node) and (not use_parallel or temp_download_path):
                discard_partial_download(temp_file_name)
            return DownloadStatus.SkipDownload, None, ""
        except DownloadWorkersExhausted as exhausted:
            # The pool already applied the last connection's three-attempt
            # budget. Retrying the file here would multiply that budget by three.
            error_message = str(exhausted)
            logger.error("Message[{}]: {}", message_id, error_message)
            break
        except pyrogram.errors.exceptions.bad_request_400.BadRequest:
            # Preserve .temp, refresh the message reference and resume the checkpoint.
            logger.warning(
                f"Message[{message.id}]: {_t('file reference expired, refetching')}..."
            )
            error_message = 'File reference expired'
            _client_conn_errors["count"] += 1
            if await _maybe_reconnect_client():
                error_message = 'File reference expired (reconnecting client)'
            await asyncio.sleep(RETRY_TIME_OUT)
            message = await fetch_message(client, message)
            if message is None:
                logger.error(f"Message[{message_id}] {ui_file_name}: fetch_message returned None (file ref expired), message may be deleted")
                error_message = 'Message missing or deleted (file reference expired)'
                break
            # Try resuming Pyrogram's checkpoint at temp_file_name + ".temp".
            if not use_parallel and temp_file_name and os.path.exists(temp_file_name + ".temp"):
                try:
                    resumed = await _resume_download_from_temp(
                        client, message, temp_file_name, media_size,
                        node, ui_file_name, task_start_time,
                    )
                    if resumed:
                        _check_download_finish(media_size, temp_file_name, ui_file_name)
                        await asyncio.sleep(0.5)
                        _move_to_download_path(temp_file_name, file_name)
                        _client_conn_errors["count"] = 0
                        logger.info(f"Message[{message.id}] {ui_file_name}: Checkpoint resume complete")
                        return DownloadStatus.SuccessDownload, file_name, ""
                except pyrogram.errors.exceptions.bad_request_400.BadRequest:
                    # Keep the checkpoint and retry if its resumed size still differs.
                    error_message = 'File reference expired (size still incorrect after resume)'
                    continue
                except Exception as resume_err:
                    logger.warning(
                        f"Message[{message.id}] {ui_file_name}: resume failed: "
                        f"{type(resume_err).__name__}: {str(resume_err)[:100]}, "
                        f"retry with fresh reference (temp preserved)"
                    )
                    error_message = f'Resume failed: {str(resume_err)[:60]}'
                    # Do not remove temp or fall back to download_media, which would truncate
                    # it.
                    await asyncio.sleep(RETRY_TIME_OUT)
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}]: {_t('file reference expired for 3 retries, download skipped')}"
                )
                error_message = 'File reference expired (failed after 3 retries)'
        except (pyrogram.errors.exceptions.flood_420.FloodWait,
                pyrogram.errors.FloodPremiumWait) as wait_err:
            # Rate limits pause the task indefinitely without spending download attempts.
            total_wait += wait_err.value
            if not await _wait_download_flood(wait_err.value, message_id, node, client):
                return DownloadStatus.SkipDownload, None, ""
            retry -= 1
            logger.info("Message[{}]: FlowWait {}s, waiting (total={}s)", message.id, wait_err.value, total_wait)
            error_message = f'Rate limited; waiting {wait_err.value} seconds'
        except TypeError:
            _cleanup_temp_file(temp_file_name)
            logger.warning(
                f"{_t('Timeout Error occurred when downloading Message')}[{message.id}], "
                f"{_t('retrying after')} {RETRY_TIME_OUT} {_t('seconds')}"
            )
            error_message = 'Download timed out'
            await asyncio.sleep(RETRY_TIME_OUT)
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}]: {_t('Timing out after 3 reties, download skipped')}"
                )
                error_message = 'Download timed out (failed after 3 retries)'
        except TimeoutError as e:
            # A connection timeout may indicate silent Telegram throttling without FloodWait.
            # Catch TimeoutError before OSError, its superclass.
            _cleanup_temp_file(temp_file_name)
            # Apply the same bounded network backoff to timeouts.
            backoff = network_backoff([60, 120, 300][min(retry, 2)])
            from module.pyrogram_extension import _unified_flood_wait
            _unified_flood_wait["until"] = max(_unified_flood_wait["until"], time.time() + backoff + 5)
            _unified_flood_wait["reason"] = f'Connection timeout, possibly rate limited (message {message.id})'
            _client_conn_errors["count"] += 1
            if await _maybe_reconnect_client():
                backoff = 5  # Short backoff after reconnect
            # Notify the user on the first timeout.
            if retry == 0 and node and node.bot and getattr(node, "from_user_id", ""):
                try:
                    notify_text = (
                        f"⏸️ {_t('Telegram connection timed out, possibly rate limited')}\n"
                        f"{_t('Task')}: {getattr(node, 'task_id_display', str(node.task_id))}\n"
                        f"{_t('File')}: {ui_file_name}\n"
                        + _t("Pause {seconds} seconds before retrying automatically", seconds=backoff) + "\n" +
                        f"{_t('Reason')}: {_t('Request timed out (not FloodWait)')}"
                    )
                    await node.bot.send_message(int(node.from_user_id), notify_text)
                except Exception:
                    pass
            await asyncio.sleep(backoff)
            # Refresh a possibly expired message reference; guard against another timeout.
            try:
                message = await fetch_message(client, message)
                if message is None:
                    error_message = 'Message missing or inaccessible after connection timeout'
                    break
            except Exception as fetch_err:
                logger.warning(f"Message[{message.id}]: fetch_message also timed out: {fetch_err}")
            error_message = f'Connection timed out, possibly rate limited; retry in {backoff} seconds'
        except OSError as e:
            # Handle non-timeout connection failures such as network or proxy disconnection.
            # Exceptions mentioning timeout also enter the cooldown path defensively.
            _cleanup_temp_file(temp_file_name)
            err_str = str(e).lower()
            if "timed out" in err_str or "timeout" in err_str:
                # Treat apparent timeouts with a longer backoff and shared cooldown.
                backoff = network_backoff([60, 120, 300][min(retry, 2)])
                _unified_flood_wait["until"] = max(_unified_flood_wait["until"], time.time() + backoff + 5)
                _unified_flood_wait["reason"] = f'Connection timeout, possibly rate limited (message {message.id})'
            else:
                # Jitter spreads initial retries; later delays stop at the cap.
                backoff = network_backoff([10, 60, 300][min(retry, 2)])
            _client_conn_errors["count"] += 1
            if await _maybe_reconnect_client():
                backoff = 5  # Short backoff after reconnect
            logger.warning(
                f"Message[{message.id}] {ui_file_name}: connection error ({type(e).__name__}), "
                f"retry {retry + 1}/3 after {backoff}s backoff"
            )
            error_message = f'Connection error: {str(e)[:80]}'
            await asyncio.sleep(backoff)
            message = await fetch_message(client, message)
            if message is None:
                logger.error(f"Message[{message_id}] {ui_file_name}: fetch_message returned None after connection error")
                error_message = 'Message unavailable after connection error'
                break
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}] {ui_file_name}: connection failed after 3 retries"
                )
                error_message = "Connection error (failed after 3 retries)"
        except Exception as e:
            _cleanup_temp_file(temp_file_name)
            error_str = str(e)
            # "This message doesn't contain any downloadable media" happens when
            # the file reference is stale. Refresh the message and retry instead
            # of breaking immediately.
            if "doesn't contain any downloadable media" in error_str:
                logger.warning(
                    f"Message[{message.id}] {ui_file_name}: stale file reference "
                    f"(no downloadable media), refreshing message (retry {retry + 1}/3)"
                )
                error_message = 'File reference expired; refreshing message'
                await asyncio.sleep(RETRY_TIME_OUT)
                message = await fetch_message(client, message)
                if message is None:
                    logger.error(f"Message[{message_id}] {ui_file_name}: fetch_message returned None (stale file ref)")
                    error_message = 'File reference expired and message unavailable'
                    break
                if _check_timeout(retry, message.id):
                    logger.error(
                        f"Message[{message.id}] {ui_file_name}: "
                        f"still no downloadable media after 3 retries with message refresh"
                    )
                    error_message = 'File reference expired (failed after 3 message refreshes)'
                continue
            logger.error(
                f"Message[{message.id}]: "
                f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
                exc_info=True,
            )
            # Retry an unknown transient error once before failing. The outer loop allows at
            # most three attempts and each iteration consumes one retry slot, bounding extra
            # attempts.
            _unknown_retries = getattr(node, "_unknown_err_retries", 0) + 1
            node._unknown_err_retries = _unknown_retries
            if _unknown_retries <= 1 and retry < 2:
                logger.warning(
                    f"Message[{message.id}] {ui_file_name}: "
                    f"unknown exception, retrying once before giving up "
                    f"(temp preserved, {_unknown_retries}/1)"
                )
                error_message = f'Download error (retry {_unknown_retries}/1): {error_str[:80]}'
                await asyncio.sleep(RETRY_TIME_OUT)
                try:
                    message = await fetch_message(client, message)
                    if message is None:
                        error_message = 'Message missing or inaccessible to this account'
                        break
                except Exception:
                    pass  # A failed refresh does not prevent the retry itself.
                continue
            error_message = f'Download error: {error_str[:100]}'
            break
    # Before reporting failure, check whether Pyrogram finished writing the temporary file
    # before raising.
    if temp_file_name and os.path.exists(temp_file_name):
        temp_size = os.path.getsize(temp_file_name)
        if media_size > 0 and temp_size >= media_size:
            try:
                _move_to_download_path(temp_file_name, file_name)
                logger.info(f"Message[{message_id}] {ui_file_name}: Download already complete (temp {temp_size} bytes)")
                return DownloadStatus.SkipDownload, file_name, ""
            except Exception as e:
                logger.warning(f"Message[{message_id}]: Failed to move completed file: {e}")
    _cleanup_temp_file(temp_file_name)
    # The destination may already exist from another task or an earlier successful attempt.
    if file_name and _is_exist(file_name):
        file_size = os.path.getsize(file_name)
        if media_size > 0 and file_size >= media_size:
            logger.info(f"Message[{message_id}] {ui_file_name}: File already exists ({file_size} bytes); marking skipped")
            return DownloadStatus.SkipDownload, None, ""
    # Log the specific failure reason before returning
    final_reason = error_message or 'Download failed (unknown reason)'
    logger.warning(f"Message[{message_id}] {ui_file_name}: download failed after retries, reason: {final_reason}")
    return DownloadStatus.FailedDownload, None, final_reason


def _load_config():
    """Load config"""
    app.load_config()


def _check_config() -> bool:
    """Check config"""
    print_meta(logger)
    try:
        _load_config()

        logger.add(
            os.path.join(app.log_file_path, "tdl.log"),
            rotation="10 MB",
            retention="30 days",
            level=app.log_level,
        )

        logger.add(
            os.path.join(app.log_file_path, "download.log"),
            rotation="10 MB",
            retention="30 days",
            level=app.log_level,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} - {message}",
        )

        load_downloads()
    except Exception as e:
        logger.exception(f"load config error: {e}")
        return False
    return True


async def worker(client: pyrogram.client.Client):
    """Process queued downloads with a progress watchdog.

    Run download_task separately and check heartbeats every 30 seconds. Silent
    throttling and stream backoff only produce warnings, limited to once per five
    minutes. Cancel only after 30 minutes without progress, then reconnect and
    requeue for checkpoint recovery."""
    from module.download_stat import (
        _TASK_HEARTBEAT_TIMEOUT, get_task_heartbeat_age, clear_task_heartbeat,
    )
    from module.bot import _bot
    global _live_workers
    with _worker_pool_lock:
        _live_workers += 1
    try:
        while app.is_running:
            counted_active = False
            message = None
            node = None
            try:
                logger.info(f"Worker waiting for queue item...")
                item = await queue.get()
                # A None sentinel wakes workers during shrinking; surplus workers exit.
                if item is None:
                    with _worker_pool_lock:
                        target = max(1, getattr(app, "max_download_task", 2))
                        if _worker_exit_signal and _live_workers > target:
                            logger.info(f"Worker exiting (pool scale-down, live={_live_workers - 1})")
                            return
                        # Retained workers ignore the sentinel and continue.
                    continue
                message = item[0]
                node: TaskNode = item[1]
                _bot._active_downloads += 1
                counted_active = True
                logger.info(f"Worker picked up message {message.id} from chat {node.chat_id} for task {node.task_id_display} (active={_bot._active_downloads})")
                _requeued = False  # Track requeue branches without changing counter cleanup in finally.
                # Mark task as actively downloading (no longer pending/in-queue)
                if node.task_id:
                    _bot._in_queue.discard(node.task_id)
                    update_download_state(node.task_id, "downloading")
                if node.is_stop_transmission:
                    continue

                target_client = node.client if node.client else client
                composite_key = f"{node.chat_id}_{message.id}"

                # Run download_task separately so the heartbeat watchdog can observe it.
                dl_task = asyncio.create_task(
                    download_task(target_client, message, node)
                )
                dl_task_start = time.time()
                # Silent throttling and dead TCP both lack heartbeats. Warn after 300 seconds,
                # at most once per five minutes; cancel only after 30 minutes without progress
                # or backoff. Ordinary dead TCP should raise via Pyrogram's 900-second timeout
                # first.
                _ZOMBIE_TIMEOUT = 1800  # Zombie fallback after 30 minutes, well beyond TCP.TIMEOUT=900 seconds.
                _last_warn = 0.0
                watchdog_triggered = False
                try:
                    while not dl_task.done():
                        await asyncio.sleep(30)
                        if dl_task.done():
                            break
                        age = get_task_heartbeat_age(composite_key)
                        # No heartbeat (age=-1) can mean startup or early backoff, not a
                        # failure.
                        if age < 0:
                            continue
                        if age > _TASK_HEARTBEAT_TIMEOUT:
                            now = time.time()
                            # Keep silent throttling visible with at most one warning per five
                            # minutes.
                            if now - _last_warn >= 300:
                                logger.warning(
                                    f"Worker: task {node.task_id_display} (msg {message.id}) "
                                    f"no progress for {int(age)}s — likely silent throttle "
                                    f"(S1), keep waiting (no cancel)"
                                )
                                _last_warn = now
                            # Only treat 30 minutes without progress as a coroutine stuck
                            # without an exception.
                            if age > _ZOMBIE_TIMEOUT:
                                logger.error(
                                    f"Worker: task {node.task_id_display} (msg {message.id}) "
                                    f"no progress for {int(age)}s (>{_ZOMBIE_TIMEOUT}s zombie "
                                    f"threshold), cancelling — coroutine hung with no exception"
                                )
                                dl_task.cancel()
                                watchdog_triggered = True
                                break
                    # Await normal completion or cancellation of dl_task.
                    await dl_task
                    # Clear retry counts after normal completion.
                    _watchdog_retry_count.pop(node.task_id, None)
                except asyncio.CancelledError:
                    # Awaiting a cancelled dl_task raises CancelledError.
                    if watchdog_triggered:
                        logger.warning(
                            f"Worker: task {node.task_id_display} cancelled by zombie watchdog"
                        )
                        # A zombie coroutine likely has dead TCP; increment the connection error
                        # counter.
                        _client_conn_errors["count"] += 3
                        # Wait for the reconnect result. If another task is reconnecting, use a
                        # bounded wait rather than failing immediately while recovery is still
                        # in progress.
                        reconnect_ok = await _maybe_reconnect_client(force=True)
                        if not reconnect_ok:
                            reconnect_ok = await _wait_for_reconnect_result(timeout=45)
                        retry_count = _watchdog_retry_count.get(node.task_id, 0)
                        if reconnect_ok and retry_count < _MAX_WATCHDOG_RETRIES:
                            # Requeue after reconnect succeeds if retries remain.
                            _watchdog_retry_count[node.task_id] = retry_count + 1
                            logger.info(
                                f"Worker: requeue task {node.task_id_display} "
                                f"(retry {retry_count + 1}/{_MAX_WATCHDOG_RETRIES}) after zombie cancel + reconnect"
                            )
                            await _reset_task_for_retry(node, message)
                            clear_task_heartbeat(composite_key)
                            node.total_task = 1
                            node.is_running = True
                            _bot._in_queue.add(node.task_id)
                            await queue.put((message, node))
                            _requeued = True
                            continue
                        else:
                            # Move to the failed list when reconnect fails or retries are
                            # exhausted.
                            if not reconnect_ok:
                                err_msg = 'Watchdog cancelled stalled task (reconnect failed)'
                            else:
                                err_msg = f'Watchdog cancelled stalled task (failed after {_MAX_WATCHDOG_RETRIES} retries)'
                            logger.warning(
                                f"Worker: moving task {node.task_id_display} to failed: {err_msg}"
                            )
                            _move_task_to_failed(node, message, err_msg)
                    else:
                        dl_task.cancel()
                        await asyncio.gather(dl_task, return_exceptions=True)
                        raise
                finally:
                    clear_task_heartbeat(composite_key)
            except Exception as e:
                logger.exception(f"Worker exception for task {getattr(node, 'task_id_display', '?')}: {e}")
                # Recognize network errors escaping recovery, including OSError, TimeoutError,
                # cancellation from wait_for(get_messages), and an unstarted client. Matching
                # only two strings previously failed recoverable tasks.
                error_str = str(e)
                _worker_is_net = (
                    "Client has not been started" in error_str
                    or "ConnectionError" in error_str
                    or isinstance(e, (ConnectionError, TimeoutError, OSError))
                    or "Connection lost" in error_str
                    or "timed out" in error_str.lower()
                    or "timeout" in error_str.lower()
                    or isinstance(e, asyncio.CancelledError)
                )
                if _worker_is_net:
                    _client_conn_errors["count"] += 3
                    reconnect_ok = await _maybe_reconnect_client(force=True)
                    # Network worker errors have no retry budget. Requeue after successful
                    # reconnect; otherwise sleep and retry locally while preserving .temp
                    # checkpoints.
                    if reconnect_ok:
                        _watchdog_retry_count.pop(node.task_id, None)
                        logger.info(
                            f"Worker: requeue task {node.task_id_display} "
                            f"after network exception + reconnect"
                        )
                        await _reset_task_for_retry(node, message)
                        clear_task_heartbeat(f"{node.chat_id}_{message.id}" if message else "")
                        node.total_task = 1
                        node.is_running = True
                        if message:
                            _bot._in_queue.add(node.task_id)
                            await queue.put((message, node))
                        _requeued = True
                        continue
                    # After reconnect failure, back off and retry within the same worker without
                    # failing the task.
                    logger.warning(
                        f"Worker: reconnect failed for {node.task_id_display}, "
                        f"backoff 60s then requeue (temp preserved, NOT failing)"
                    )
                    await _reset_task_for_retry(node, message)
                    clear_task_heartbeat(f"{node.chat_id}_{message.id}" if message else "")
                    node.total_task = 1
                    node.is_running = True
                    await asyncio.sleep(60)
                    if message:
                        _bot._in_queue.add(node.task_id)
                        await queue.put((message, node))
                    _requeued = True
                    continue
                # Only non-network logic errors move the task to the failed list.
                _move_task_to_failed(node, message, f'Worker error: {error_str[:80]}')
            finally:
                # Always decrement active concurrency, including requeues. The same worker can
                # immediately reclaim the task and increment again; skipping cleanup leaks one
                # reservation per watchdog cycle. Queued occupancy is already included in the
                # guard's in_queue term.
                if counted_active:
                    _bot._active_downloads -= 1
    finally:
        with _worker_pool_lock:
            _live_workers -= 1


async def download_chat_task(client: pyrogram.Client, chat_download_config: ChatDownloadConfig, node: TaskNode):
    """Download all task"""
    messages_iter = get_chat_history_v2(
        client, node.chat_id, limit=node.limit,
        max_id=node.end_offset_id, offset_id=chat_download_config.last_read_message_id, reverse=True,
    )
    chat_download_config.node = node
    if chat_download_config.ids_to_retry:
        logger.info(f"{_t('Downloading files failed during last run')}...")
        try:
            skipped_messages: list = await asyncio.wait_for(
                client.get_messages(
                    chat_id=node.chat_id, message_ids=chat_download_config.ids_to_retry
                ),
                timeout=120,  # Bound batch fetching to 120 seconds so half-dead TCP cannot hang for 15 minutes.
            )
        except asyncio.TimeoutError:
            logger.error(
                f"download_chat_task: get_messages timeout (120s) for {len(chat_download_config.ids_to_retry)} "
                f"retry messages in chat {node.chat_id}, skipping retry this run"
            )
            skipped_messages = []
        for message in skipped_messages:
            await add_download_task(message, node)
    async for message in messages_iter:
        # Yield control to avoid blocking handlers.
        await asyncio.sleep(0)

        # Cache chat title from message
        if message and message.chat:
            chat_title = getattr(message.chat, 'title', None) or getattr(message.chat, 'first_name', None)
            if chat_title:
                set_chat_title(message.chat.id, chat_title)
        meta_data = MetaData()
        caption = message.caption
        if caption:
            caption = validate_title(caption)
            app.set_caption_name(node.chat_id, message.media_group_id, caption)
            app.set_caption_entities(node.chat_id, message.media_group_id, message.caption_entities)
        else:
            caption = app.get_caption_name(node.chat_id, message.media_group_id)
        set_meta_data(meta_data, message, caption)
        if app.need_skip_message(chat_download_config, message.id):
            continue
        if app.exec_filter(chat_download_config, meta_data):
            if message.media:  # Only add to download queue if message has media
                await add_download_task(message, node)
        else:
            node.download_status[message.id] = DownloadStatus.SkipDownload
            if message.media_group_id:
                await upload_telegram_chat(client, node.upload_user, app, node, message, DownloadStatus.SkipDownload)
        # Update task progress for crash recovery
        update_task_progress(node.task_id, message.id)
        # Persist last_read_message_id every 200 messages; a crash then rescans at most 200
        # messages instead of writing after every message.
        chat_download_config.last_read_message_id = max(
            chat_download_config.last_read_message_id, message.id
        )
        if message.id % 200 == 0:
            app.update_config(immediate=True)
    # Persist the final scan position for correct restart recovery.
    chat_download_config.need_check = True
    chat_download_config.total_task = node.total_task
    node.is_running = True
    app.update_config(immediate=True)


async def download_all_chat(client: pyrogram.Client):
    """Download All chat"""
    from module.task_store import save_task as _save_task
    for key, value in app.chat_download_config.items():
        value.node = TaskNode(chat_id=key)
        _save_task(
            task_id=value.node.task_id,
            chat_id=key,
            url="",
            start_offset_id=value.last_read_message_id,
            end_offset_id=0,
            limit=0,
            download_filter=value.download_filter,
            from_user_id=0,
            task_type="config",
        )
        try:
            await download_chat_task(client, value, value.node)
        except Exception as e:
            logger.warning(f"Download {key} error: {e}")
        finally:
                    value.need_check = True
                    from module.task_store import complete_task
                    complete_task(value.node.task_id)


async def run_until_all_task_finish():
    """Keep accepting WebUI tasks until a restart is requested."""
    while True:
        if app.restart_program:
            break
        await asyncio.sleep(1)


def _exec_loop():
    """Exec loop"""
    app.loop.run_until_complete(run_until_all_task_finish())


async def start_server(client: pyrogram.Client):
    """Start the server"""
    _main_client_ref["client"] = client
    await client.start()


async def _reconnect_client():
    """Reconnect the main Pyrogram client after a half-dead TCP session.

    Stopping and starting reuses the existing session file without authentication.
    Bound all operations so a 900-second TCP timeout cannot permanently hold the
    reconnect guard. If stop times out while SQLite retains an exclusive lock,
    clean up journal files and internal session references before restarting."""
    client = _main_client_ref["client"]
    if client is None:
        logger.error("Cannot reconnect: no client reference")
        return False

    _client_reconnecting["active"] = True
    try:
        # Use a 30-second hard timeout with create_task plus Event: synchronous Pyrogram stop()
        # work can block the loop and prevent wait_for callbacks. If stop does not finish,
        # return False and keep workers alive. Never start before stop finishes: a late stop
        # could close the new connection.
        logger.warning("Client auto-reconnect: stop()...")
        stop_done = asyncio.Event()
        stop_task = asyncio.create_task(client.stop())

        def _stop_finished(_t=None):
            stop_done.set()

        stop_task.add_done_callback(_stop_finished)
        try:
            await asyncio.wait_for(stop_done.wait(), timeout=30)
            # stop() finished within 30 seconds; the task captures any exception.
            try:
                stop_task.result()
            except Exception as e:
                logger.warning(f"client.stop() during reconnect failed (continuing): {e}")
                client.is_connected = False
                _force_release_session_lock(client)
        except asyncio.TimeoutError:
            logger.error(
                "client.stop() not finished within 30s (hard timeout) — "
                "abandoning reconnect, returning False (workers stay alive)"
            )
            # Leave stop_task running in the background because Pyrogram may swallow
            # cancellation and add noisy logs. Force is_connected to False.
            client.is_connected = False
            _force_release_session_lock(client)
            return False  # Do not call start(): a late stop could close the new connection.
        except Exception as e:
            # If Event.wait is cancelled or otherwise fails, leave start() untouched.
            logger.error(f"reconnect stop-phase interrupted: {e}, abandoning")
            client.is_connected = False
            _force_release_session_lock(client)
            return False

        # Use the same 30-second Event-based hard timeout for start(), which can hang on an
        # unclean session.
        logger.warning("Client auto-reconnect: start()...")
        start_done = asyncio.Event()
        start_task = asyncio.create_task(client.start())

        def _start_finished(_t=None):
            start_done.set()

        start_task.add_done_callback(_start_finished)
        try:
            await asyncio.wait_for(start_done.wait(), timeout=30)
            try:
                start_task.result()
            except asyncio.TimeoutError:
                raise
            except Exception as e:
                error_str = str(e)
                if "database is locked" in error_str:
                    # Retry once after a second lock cleanup.
                    logger.warning("client.start() failed with database is locked, forcing release and retrying...")
                    _force_release_session_lock(client)
                    retry_done = asyncio.Event()
                    retry_task = asyncio.create_task(client.start())
                    retry_task.add_done_callback(lambda _t: retry_done.set())
                    try:
                        await asyncio.wait_for(retry_done.wait(), timeout=30)
                        retry_task.result()
                        logger.success("Client reconnected successfully on retry — fresh TCP session established")
                        _client_conn_errors["count"] = 0
                        return True
                    except Exception as e2:
                        logger.error(f"client.start() retry also failed: {e2}")
                        return False
                logger.error(f"client.start() during reconnect failed: {e}")
                return False
            logger.success("Client reconnected successfully — fresh TCP session established")
            _client_conn_errors["count"] = 0
            return True
        except asyncio.TimeoutError:
            logger.error(
                "client.start() not finished within 30s (hard timeout) — "
                "abandoning reconnect, returning False"
            )
            return False
    finally:
        _client_reconnecting["active"] = False


def _force_release_session_lock(client):
    """Release locks held by the Pyrogram SQLite session.

    An interrupted stop may leave a transaction and exclusive lock behind. Close
    the storage SQLite connection, disconnect the MTProto session, remove journal
    files and reset client state so start() opens the database again."""
    import os
    try:
        # 1. Close the storage SQLite connection that owns the lock.
        storage = getattr(client, "storage", None)
        if storage and getattr(storage, "conn", None):
            try:
                storage.conn.close()
                logger.info("Closed storage SQLite connection")
            except Exception:
                pass
            storage.conn = None
        # 2. Close the MTProto session's TCP connection.
        session = getattr(client, "session", None)
        if session:
            try:
                # Session.stop() may hang while closing the connection; use disconnect instead.
                conn = getattr(session, "connection", None)
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
            except Exception:
                pass
            client.session = None
        # 3. Remove journal files.
        session_path = os.path.join(app.session_file_path, "media_downloader.session")
        for suffix in ["-journal", "-wal", "-shm"]:
            f = session_path + suffix
            if os.path.exists(f):
                os.remove(f)
                logger.info(f"Removed stale SQLite file: {f}")
        # 4. Reset internal client state.
        client.is_initialized = False
        client.is_connected = False
        logger.info("Reset client session/storage/flags for clean restart")
    except Exception as e:
        logger.warning(f"Failed to release session lock: {e}")


async def _wait_for_reconnect_result(timeout: float = 45.0) -> bool:
    """Wait within a bounded interval for an in-flight reconnect result.

    Two watchdog cancellations can arrive together. Returning False immediately
    to the second task would discard it even if the first reconnect succeeds a
    moment later. Poll reconnect state and client connectivity until timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _client_reconnecting["active"]:
            # In-flight reconnect finished — report actual connection state
            client = _main_client_ref.get("client")
            if client and getattr(client, "is_connected", False):
                return True
            # Reconnect finished but client still down — one more attempt
            return await _maybe_reconnect_client(force=True)
        await asyncio.sleep(2)
    # Timeout waiting — final state check
    client = _main_client_ref.get("client")
    return bool(client and getattr(client, "is_connected", False))


async def _maybe_reconnect_client(force=False):
    """Check if consecutive connection errors warrant a client reconnect.

    Returns True if client is currently connected (either was already connected,
    or reconnect succeeded). Returns False if reconnect failed or was skipped.

    Args:
        force: If True, skip cooldown check (used by watchdog cancel which
               has already determined the connection is dead).
    """
    if _client_conn_errors["count"] < _CLIENT_RECONNECT_THRESHOLD and not force:
        # Below threshold and not forced — assume client is fine
        return True

    if _client_reconnecting["active"]:
        logger.debug("Reconnect already in progress, skipping")
        # Another reconnect is in progress — wait for it to finish, then check result
        # Can't wait here (would block), so return False to be safe
        # Caller will move task to failed, which is correct — task can be retried later
        return False
    now = time.time()
    if not force and now - _client_last_reconnect["time"] < _CLIENT_RECONNECT_COOLDOWN:
        logger.debug(
            f"Reconnect cooldown active ({int(_CLIENT_RECONNECT_COOLDOWN - (now - _client_last_reconnect['time']))}s remaining), skipping"
        )
        # In cooldown — don't reconnect, but check if client is still connected
        client = _main_client_ref.get("client")
        if client and getattr(client, "is_connected", False):
            return True
        return False

    logger.warning(
        f"Client connection errors reached {_client_conn_errors['count']} "
        f"(threshold {_CLIENT_RECONNECT_THRESHOLD}) — triggering auto-reconnect"
    )
    _client_last_reconnect["time"] = now
    success = await _reconnect_client()
    if not success:
        # Reconnect failed — retry sooner (60s instead of full cooldown)
        _client_last_reconnect["time"] = now - _CLIENT_RECONNECT_COOLDOWN + 60
        return False
    return True


async def _throttle_restart_loop(client):
    """Reconnect after prolonged silent throttling.

    Every ten minutes, check whether active downloads have made no global progress
    for 24 hours. Reconnect once, reset the counter when progress returns, and stop
    automatic reconnects after three ineffective attempts while retaining warnings."""
    from module.download_stat import get_download_result

    # Initialize the baseline at startup to avoid falsely detecting 24 hours of inactivity after
    # restart.
    while _throttle_state["last_progress"] == 0.0:
        await asyncio.sleep(60)
    while True:
        try:
            await asyncio.sleep(600)  # Check once every ten minutes.
            # Count inactivity only while downloads are active; an empty queue is not
            # throttling.
            dr = get_download_result()
            active = 0
            for _chat, msgs in dr.items():
                for _mid, v in msgs.items():
                    if 0 < v.get("down_byte", 0) < v.get("total_size", 0):
                        active += 1
                        break
                if active:
                    break
            if not active:
                # Reset the baseline when idle so completed downloads cannot trigger a reconnect
                # a day later.
                _throttle_state["last_progress"] = time.time()
                continue
            silent = time.time() - _throttle_state["last_progress"]
            if silent < _THROTTLE_RESTART_AFTER:
                continue
            if _throttle_state["restarts"] >= _THROTTLE_RESTART_MAX:
                logger.warning(
                    f"Throttle-restart: silent {int(silent/3600)}h, "
                    f"{_throttle_state['restarts']} restarts already tried without "
                    f"progress — manual node change suggested (no more auto-restarts)"
                )
                continue
            logger.warning(
                f"Throttle-restart: no download progress for {int(silent/3600)}h "
                f"({active} active tasks) — restarting node connection "
                f"(attempt {_throttle_state['restarts'] + 1}/{_THROTTLE_RESTART_MAX})"
            )
            _throttle_state["restarts"] += 1
            # Advance the baseline before reconnecting to avoid immediately retriggering after
            # failure.
            _throttle_state["last_progress"] = time.time()
            ok = await _reconnect_client()
            if ok:
                logger.success(
                    "Throttle-restart: node connection restarted — "
                    "watching for progress to confirm throttle reset"
                )
            else:
                logger.error("Throttle-restart: reconnect failed, will retry next cycle")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"throttle_restart_loop error: {e}")
            await asyncio.sleep(60)


async def stop_server(client: pyrogram.Client):
    """Stop the server"""
    await client.stop()


def main():
    """Main function"""
    # ── Patch A: Increase TCP timeout to 900s (15 min) ──
    # Pyrogram default TCP.TIMEOUT=10s causes reconnect storms when TG throttles
    # downloads — each chunk request waits >10s for a response, triggering
    # connection teardown + MTProto re-handshake, producing massive upload traffic.
    # Xray proxy has no idle timeout, so 900s is safe.
    from pyrogram.connection.transport.tcp import TCP as _TCP
    _TCP.TIMEOUT = 900
    logger.info(f"Patched TCP.TIMEOUT: 10s -> {_TCP.TIMEOUT}s")

    # ── Patch B: Swallow ChannelInvalid in Message._parse ──
    # When a monitored channel has messages that reply to messages in another
    # channel the user account can't access, Pyrogram's Message._parse tries
    # to fetch reply_to_message via client.get_messages → resolve_peer →
    # channels.GetChannels, which throws ChannelInvalid. This kills the entire
    # message_parser call in the dispatcher, so the update is silently dropped
    # and our NewMessage handler never fires — the download never starts.
    # Fix: wrap Message._parse so that on ChannelInvalid, we retry with a
    # client wrapper that returns None for inaccessible reply targets.
    from pyrogram.types import Message as _PMsg
    try:
        from pyrogram.errors import ChannelInvalid as _ChannelInvalidErr
    except ImportError:
        # Fallback for forks where ChannelInvalid is under subpackage
        from pyrogram.errors.exceptions.bad_request_400 import ChannelInvalid as _ChannelInvalidErr

    _orig_msg_parse = _PMsg._parse

    class _SafeReplyClient:
        """Pass-through client that swallows ChannelInvalid in reply fetches."""
        def __init__(self, client):
            self._client = client

        async def get_messages(self, *a, **kw):
            try:
                return await self._client.get_messages(*a, **kw)
            except _ChannelInvalidErr:
                return None

        async def get_stories(self, *a, **kw):
            try:
                return await self._client.get_stories(*a, **kw)
            except _ChannelInvalidErr:
                return None

        async def get_forum_topics_by_id(self, *a, **kw):
            try:
                return await self._client.get_forum_topics_by_id(*a, **kw)
            except _ChannelInvalidErr:
                return None

        def __getattr__(self, name):
            return getattr(self._client, name)

    async def _safe_msg_parse(client, *args, **kwargs):
        try:
            return await _orig_msg_parse(client, *args, **kwargs)
        except _ChannelInvalidErr:
            logger.warning(
                "ChannelInvalid in Message._parse — "
                "retrying without reply_to_message"
            )
            return await _orig_msg_parse(
                _SafeReplyClient(client), *args, **kwargs
            )

    _PMsg._parse = _safe_msg_parse
    logger.info("Patched Message._parse: ChannelInvalid no longer drops updates")

    tasks = []
    client = HookClient(
        "media_downloader", api_id=app.api_id, api_hash=app.api_hash,
        proxy=app.proxy or None, workdir=app.session_file_path,
        start_timeout=app.start_timeout, no_updates=False,
    )
    try:
        app.pre_run()
        _cleanup_stale_temp_files()
        init_web(app)
        set_max_concurrent_transmissions(client, app.max_concurrent_transmissions)
        app.loop.run_until_complete(start_server(client))
        # Create queue AFTER event loop is running to ensure it binds to the
        # correct loop. Creating asyncio.Queue at module import time (before
        # loop starts) can cause put/get to use different loop references,
        # resulting in workers never receiving items.
        global queue
        queue = asyncio.Queue()
        logger.success(_t("Successfully started (Press Ctrl+C to stop)"))
        app.loop.create_task(download_all_chat(client))
        # Create max_download_task workers to enforce physical concurrency. The previous fixed
        # pool of six could exceed the limit during scheduling races.
        _MAX_WORKERS = max(1, getattr(app, 'max_download_task', 2))
        logger.info(f"Starting {_MAX_WORKERS} workers (max_download_task={_MAX_WORKERS})")
        with _worker_pool_lock:
            global _live_workers
            _live_workers = 0  # Reset the count; main runs once at startup.
        for _ in range(_MAX_WORKERS):
            tasks.append(app.loop.create_task(worker(client)))
        # Start the shared silent-throttling recovery monitor.
        tasks.append(app.loop.create_task(_throttle_restart_loop(client)))
        if app.bot_token:
            app.loop.run_until_complete(start_download_bot(app, client, add_download_task, download_chat_task))
        else:
            app.loop.run_until_complete(start_download_backend(app, client, add_download_task, download_chat_task))
        _exec_loop()
    except KeyboardInterrupt:
        logger.info(_t("KeyboardInterrupt"))
    except Exception as e:
        logger.exception("{}", e)
    finally:
        app.is_running = False
        save_downloads()
        app.loop.run_until_complete(stop_download_bot())
        app.loop.run_until_complete(stop_server(client))
        for task in tasks:
            task.cancel()
        logger.info(_t("Stopped"))
        logger.info(f"{_t('update config')}......")
        app.update_config()
        logger.success(
            f"{_t('Updated last read message_id to config file')},"
            f"{_t('total download')} {app.total_download_task}, "
            f"{_t('total upload file')} {app.cloud_drive_config.total_upload_success_file_count}"
        )


if __name__ == "__main__":
    if _check_config():
        main()
