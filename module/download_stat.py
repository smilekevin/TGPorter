"""Download Stat"""
import asyncio
import json
import os
import time
from enum import Enum

from loguru import logger
from module.telegram import pyrogram
from pyrogram import Client

from lang import _t
from module.app import TaskNode
from utils.format import format_byte


class DownloadState(Enum):
    """Download state"""

    Downloading = 1
    StopDownload = 2


_download_result: dict = {}
_total_download_speed: int = 0
_total_download_size: int = 0
_last_download_time: float = time.time()
_download_state: DownloadState = DownloadState.Downloading
_paused_tasks: set = set()
_cancelled_tasks: set = set()
_discarded_tasks: set = set()
_failed_downloads: list = []
_chat_titles: dict = {}  # chat_id -> chat_title mapping
_download_lock: asyncio.Lock = asyncio.Lock()  # Protect concurrent access to _download_result, _total_download_speed and _failed_downloads.

# Pyrogram callbacks update progress heartbeats. The worker watchdog distinguishes dead
# connections without callbacks from slow transfers that still make progress.
_task_heartbeat: dict = {}  # composite_key → last progress timestamp
# This is a warning threshold, not a cancellation deadline. media_downloader.py owns the
# separate 30-minute zombie fallback.
_TASK_HEARTBEAT_TIMEOUT = 300

# Silent-throttle state machine: low speed enters SLOW_PENDING, sustained low speed notifies
# THROTTLED, and recovery returns to IDLE. Completed downloads preserve the baseline for the
# next task. Do not repeat notifications while notified; send a clear notification once normal
# speed returns.
_throttle_state: dict = {
    "since": 0.0,             # Low-speed start time.
    "notified": False,        # Whether a throttle notification was sent.
    "last_active_time": 0.0,  # Latest progress callback time.
    "recovery_since": 0.0,    # Start of sustained speed recovery.
}
_SLOW_THRESHOLD_BPS = 200 * 1024   # 200 KB/s
_SLOW_SUSTAIN_SEC = 120            # Trigger after 120 seconds.
_SLOW_CLEAR_SUSTAIN_SEC = 15       # Clear after 15 seconds of sustained recovery.

# Helper: asyncio.Lock can't be used from sync contexts; provide a try-lock wrapper
# for sync functions that are also called from async context.
# Strategy: sync functions that read/write shared state use a reentrant-compatible
# pattern via _sync_lock.
_sync_lock = __import__('threading').Lock()  # Protect writes in synchronous callers.


def get_download_result() -> dict:
    """get global download result"""
    return _download_result


def get_total_download_speed() -> int:
    """get total download speed — sum of all active (non-paused, non-completed, non-stale) task speeds.

    This replaces the old independent global accumulator approach which used a separate
    time window from individual task speeds, causing the total to never equal the sum of
    individual speeds. Now we simply sum the per-task speeds, with a staleness check:
    if a task's speed hasn't been updated in 3 seconds (no Pyrogram callback), treat it as 0.
    """
    import time as _time
    now = _time.time()
    total = 0
    for chat_id, messages in _download_result.items():
        for msg_id, value in messages.items():
            # Skip completed tasks
            if value.get("down_byte", 0) >= value.get("total_size", 0) and value.get("total_size", 0) > 0:
                continue
            composite_key = f"{chat_id}_{msg_id}"
            # Skip paused tasks
            if is_task_paused(composite_key) or is_task_paused(value.get("task_id", "")):
                continue
            speed = value.get("download_speed", 0)
            # Staleness check: if no callback update in 3 seconds, speed is stale → 0
            last_update = value.get("end_time", 0)
            if speed > 0 and (now - last_update) > 3.0:
                speed = 0
            total += int(speed)
    return total


def get_download_state() -> DownloadState:
    """get download state"""
    return _download_state


# pylint: disable = W0603
def set_download_state(state: DownloadState):
    """set download state"""
    global _download_state
    _download_state = state


def is_task_paused(task_id) -> bool:
    """Check if a specific task is paused"""
    return str(task_id) in _paused_tasks


def clear_task_heartbeat(composite_key: str):
    """Clear a task heartbeat when the task completes, fails or is cancelled."""
    _task_heartbeat.pop(composite_key, None)


def touch_task_heartbeat(composite_key: str):
    """Touch a task heartbeat without changing progress.

    Previously used to protect resume backoff from watchdog cancellation. Current
    callers can sleep during backoff because ordinary inactivity no longer causes
    cancellation. Retain this helper for compatibility; it is currently unused."""
    if composite_key:
        _task_heartbeat[composite_key] = time.time()


def get_task_heartbeat_age(composite_key: str) -> float:
    """Return seconds since the last progress callback, or -1 if none exists."""
    ts = _task_heartbeat.get(composite_key)
    if ts is None:
        return -1
    return time.time() - ts


def pause_task(task_id) -> bool:
    """Pause a specific download task by task_id"""
    _paused_tasks.add(str(task_id))
    return True


def resume_task(task_id) -> bool:
    """Resume a specific download task by task_id"""
    task_id_str = str(task_id)
    if task_id_str in _paused_tasks:
        _paused_tasks.discard(task_id_str)
        return True
    return False


def delete_task(task_id, *, discard_partial=True) -> bool:
    """Delete a specific task from download results by task_id.
    Also cancels the download so it doesn't reappear."""
    task_id_str = str(task_id)
    found = False
    with _sync_lock:
        for chat_id, messages in list(_download_result.items()):
            for msg_id, value in list(messages.items()):
                composite_key = f"{chat_id}_{msg_id}"
                if composite_key == task_id_str or str(value.get("task_id", "")) == task_id_str:
                    # File deletion is scoped to this task generation, so a new
                    # task can download the same Telegram message later.
                    key = (composite_key, str(value.get("task_id", "")))
                    _cancelled_tasks.add(key)
                    if discard_partial:
                        _discarded_tasks.add(key)
                    if task_id_str != composite_key:
                        _cancelled_tasks.add(task_id_str)
                        if discard_partial:
                            _discarded_tasks.add(task_id_str)
                    del _download_result[chat_id][msg_id]
                    if not _download_result[chat_id]:
                        del _download_result[chat_id]
                    found = True
        if found:
            save_downloads()
    return found


def delete_download_result_entry(chat_id, msg_id) -> bool:
    """Delete a specific entry from _download_result by (chat_id, msg_id).
    Unlike delete_task which matches by task_id, this deletes by exact
    chat_id + message_id pair. Used when a file download fails, so it
    doesn't stay in the WebUI active download list forever.
    Returns True if entry was found and deleted."""
    with _sync_lock:
        if chat_id in _download_result and msg_id in _download_result[chat_id]:
            del _download_result[chat_id][msg_id]
            if not _download_result[chat_id]:
                del _download_result[chat_id]
            save_downloads()
            return True
    return False


def add_failed_download(chat_id, msg_id, task_id, file_name, error_message, total_size=0, source_link="", from_user_id="", temp_file_name=""):
    """Track a failed download"""
    # Remove existing entry with same (chat_id, msg_id) to deduplicate
    global _failed_downloads
    composite_key = f"{chat_id}_{msg_id}"
    with _sync_lock:
        _failed_downloads = [
            f for f in _failed_downloads
            if f"{f.get('chat_id', '')}_{f.get('msg_id', '')}" != composite_key
        ]
        _failed_downloads.append({
            "chat_id": chat_id,
            "msg_id": msg_id,
            "task_id": str(task_id),
            "file_name": file_name,
            "temp_file_name": temp_file_name,
            "error_message": error_message,
            "total_size": total_size,
            "source_link": source_link,
            "from_user_id": from_user_id,
            "timestamp": time.time(),
        })
    save_downloads()  # Persist failures immediately.


def get_failed_downloads() -> list:
    """Get list of failed downloads"""
    return _failed_downloads


def set_chat_title(chat_id, title: str):
    """Cache chat title for a chat_id"""
    _chat_titles[str(chat_id)] = title


def get_chat_title(chat_id) -> str:
    """Get cached chat title, fallback to chat_id string"""
    return _chat_titles.get(str(chat_id), str(chat_id))


def remove_failed_download(task_id) -> bool:
    """Remove a failed download entry by task_id"""
    global _failed_downloads
    with _sync_lock:
        before = len(_failed_downloads)
        _failed_downloads = [
            f for f in _failed_downloads if str(f.get("task_id", "")) != str(task_id)
        ]
        if len(_failed_downloads) < before:
            save_downloads()
            return True
    return False


def batch_delete_tasks(task_ids: list) -> int:
    """Delete multiple tasks from download results. Returns count deleted."""
    deleted = 0
    for task_id in task_ids:
        if delete_task(task_id):
            deleted += 1
    return deleted


def batch_delete_failed(task_ids: list) -> int:
    """Delete multiple failed downloads. Returns count deleted."""
    global _failed_downloads
    with _sync_lock:
        before = len(_failed_downloads)
        task_id_set = {str(tid) for tid in task_ids}
        _failed_downloads = [
            f for f in _failed_downloads if str(f.get("task_id", "")) not in task_id_set
        ]
        deleted = before - len(_failed_downloads)
    if deleted > 0:
        save_downloads()
    return deleted


def _reset_task_speed(task_id):
    """Reset download speed for a specific task to 0.
    Supports both composite key (chat_id_msg_id) and numeric task_id."""
    global _total_download_speed
    with _sync_lock:
        for chat_id, messages in _download_result.items():
            for msg_id, value in messages.items():
                composite_key = f"{chat_id}_{msg_id}"
                if composite_key == str(task_id) or str(value.get("task_id", "")) == str(task_id):
                    value["download_speed"] = 0
        # Recalculate total speed from remaining active tasks
        total = 0
        for chat_id, messages in _download_result.items():
            for msg_id, value in messages.items():
                composite_key = f"{chat_id}_{msg_id}"
                if not (is_task_paused(composite_key) or is_task_paused(value.get("task_id", ""))):
                    total += value.get("download_speed", 0)
        _total_download_speed = total


def _check_and_reset_global_speed():
    """Reset global speed if no active (non-paused) tasks are downloading"""
    global _total_download_speed
    with _sync_lock:
        for chat_id, messages in _download_result.items():
            for msg_id, value in messages.items():
                composite_key = f"{chat_id}_{msg_id}"
                if not (is_task_paused(composite_key) or is_task_paused(value.get("task_id", ""))):
                    return  # There are active tasks, don't reset
        _total_download_speed = 0


def _download_control_keys(message_id: int, node: TaskNode):
    composite_key = f"{node.chat_id}_{message_id}"
    return composite_key, str(node.task_id), (composite_key, str(node.task_id))


def is_download_discarded(message_id: int, node: TaskNode):
    """Only explicit user cancellation discards resumable data."""
    return getattr(node, "discard_partial", False) or any(
        key in _discarded_tasks for key in _download_control_keys(message_id, node))


async def wait_download_ready(message_id: int, node: TaskNode, client: Client):
    """Share pause/cancel controls between serial and fixed-range downloads."""
    chat_id = node.chat_id
    composite_key = f"{chat_id}_{message_id}"
    while True:
        if (node.is_stop_transmission or any(
                key in _cancelled_tasks for key in _download_control_keys(message_id, node))):
            client.stop_transmission()
        if not (is_task_paused(composite_key) or is_task_paused(node.task_id)
                or get_download_state() == DownloadState.StopDownload):
            return
        # Reset this task's speed to 0 while paused
        _reset_task_speed(composite_key)
        _check_and_reset_global_speed()
        await asyncio.sleep(1)


async def update_download_status(
    down_byte: int,
    total_size: int,
    message_id: int,
    file_name: str,
    start_time: float,
    node: TaskNode,
    client: Client,
    *,
    initial: bool = False,
):
    """update_download_status"""
    await wait_download_ready(message_id, node, client)
    cur_time = time.time()
    # pylint: disable = W0603
    global _total_download_speed
    global _total_download_size
    global _last_download_time

    chat_id = node.chat_id
    composite_key = f"{chat_id}_{message_id}"

    # Update the progress heartbeat used by the worker watchdog.
    _task_heartbeat[composite_key] = cur_time
    # The silent-throttling monitor consumes the latest global progress time. Heartbeats
    # represent actual progress and are no longer touched during backoff.
    try:
        import media_downloader as _md
        _md._throttle_state["last_progress"] = cur_time
    except Exception:
        pass

    async with _download_lock:
        if not _download_result.get(chat_id):
            _download_result[chat_id] = {}

        if _download_result[chat_id].get(message_id):
            last_download_byte = _download_result[chat_id][message_id]["down_byte"]
            last_time = _download_result[chat_id][message_id]["end_time"]
            download_speed = _download_result[chat_id][message_id]["download_speed"]
            each_second_total_download = _download_result[chat_id][message_id][
                "each_second_total_download"
            ]
            end_time = _download_result[chat_id][message_id]["end_time"]

            # Detect when a placeholder resolves to real data so Bot status refreshes
            # immediately.
            _prev_total = _download_result[chat_id][message_id].get("total_size", 0)
            _placeholder_resolved = _prev_total <= 1 and total_size > 1

            if initial:
                download_speed = 0
                each_second_total_download = 0
                end_time = cur_time
            else:
                _total_download_size += down_byte - last_download_byte
                each_second_total_download += down_byte - last_download_byte

            if not initial and cur_time - last_time >= 1.0:
                download_speed = int(each_second_total_download / (cur_time - last_time))
                end_time = cur_time
                each_second_total_download = 0

            download_speed = max(download_speed, 0)

            # Compute progress here (under lock) so Flask thread only reads
            # a single atomic value instead of dividing down_byte/total_size
            # which can tear across the two dict writes below.
            _progress = round(down_byte / total_size * 100, 1) if total_size > 0 else 0

            _download_result[chat_id][message_id]["down_byte"] = down_byte
            _download_result[chat_id][message_id]["total_size"] = total_size
            _download_result[chat_id][message_id]["progress"] = _progress
            _download_result[chat_id][message_id]["file_name"] = file_name
            _download_result[chat_id][message_id]["temp_file_name"] = getattr(node, "download_temp_paths", {}).get(message_id, "")
            _download_result[chat_id][message_id]["end_time"] = end_time
            _download_result[chat_id][message_id]["download_speed"] = download_speed
            _download_result[chat_id][message_id][
                "each_second_total_download"
            ] = each_second_total_download

            # Mark completion time when download finishes
            if down_byte >= total_size and total_size > 0:
                _download_result[chat_id][message_id]["end_time"] = cur_time
                _download_result[chat_id][message_id]["download_speed"] = 0
                # Recalculate total speed from remaining active tasks
                _total = 0
                for _cid, _msgs in list(_download_result.items()):
                    for _mid, _val in list(_msgs.items()):
                        _ckey = f"{_cid}_{_mid}"
                        if not (is_task_paused(_ckey) or is_task_paused(_val.get("task_id", ""))):
                            _total += _val.get("download_speed", 0)
                _total_download_speed = _total
                # Persist immediately when a download completes.
                save_downloads()
        else:
            each_second_total_download = 0 if initial else down_byte
            _progress = round(down_byte / total_size * 100, 1) if total_size > 0 else 0
            _download_result[chat_id][message_id] = {
                "down_byte": down_byte,
                "total_size": total_size,
                "progress": _progress,
                "file_name": file_name,
                "temp_file_name": getattr(node, "download_temp_paths", {}).get(message_id, ""),
                "start_time": start_time,
                "end_time": cur_time,
                "download_speed": 0 if initial else down_byte / max(cur_time - start_time, 0.001),
                "each_second_total_download": each_second_total_download,
                "task_id": node.task_id,
                "task_id_display": getattr(node, "task_id_display", ""),
                "source_chat_title": getattr(node, "source_chat_title", ""),
                "source_chat_id": getattr(node, "source_chat_id", 0),
                "source_message_id": getattr(node, "source_message_id", 0),
            }
            _total_download_size += down_byte
            _placeholder_resolved = False

        # Detect silent throttling under the lock; send notifications after releasing it.
        _throttle_action = None  # None / "notify" / "clear"
        _throttle_state["last_active_time"] = cur_time
        dl_entry = _download_result.get(chat_id, {}).get(message_id)
        if dl_entry:
            cur_speed = dl_entry.get("download_speed", 0)
            dl_total = dl_entry.get("total_size", 0)
            dl_down = dl_entry.get("down_byte", 0)
            # Check only incomplete downloads with actual progress.
            if dl_total > 0 and 0 < dl_down < dl_total:
                if cur_speed < _SLOW_THRESHOLD_BPS:
                    # Reset the recovery timer while speed remains low.
                    _throttle_state["recovery_since"] = 0.0
                    if _throttle_state["since"] == 0:
                        _throttle_state["since"] = cur_time
                    elif (cur_time - _throttle_state["since"] >= _SLOW_SUSTAIN_SEC
                          and not _throttle_state["notified"]):
                        _throttle_state["notified"] = True
                        _throttle_action = "notify"
                else:
                    # Require 15 seconds of sustained speed recovery before clearing throttling.
                    if _throttle_state["notified"]:
                        if _throttle_state["recovery_since"] == 0:
                            _throttle_state["recovery_since"] = cur_time
                        elif cur_time - _throttle_state["recovery_since"] >= _SLOW_CLEAR_SUSTAIN_SEC:
                            _throttle_action = "clear"
                            _throttle_state["since"] = 0
                            _throttle_state["notified"] = False
                            _throttle_state["recovery_since"] = 0.0
                    else:
                        # If no notification was sent and speed is normal, reset silently.
                        _throttle_state["since"] = 0
                        _throttle_state["notified"] = False
                        _throttle_state["recovery_since"] = 0.0
            # Keep the baseline for the next task while idle or complete.

    # Send silent-throttle notifications outside the lock to avoid awaiting while locked.
    if _throttle_action == "notify" and node.bot and getattr(node, "from_user_id", ""):
        # Increase connection error counts and schedule a reconnect without interrupting
        # Pyrogram. A dead connection will subsequently reach download_media's TimeoutError
        # retry handler.
        try:
            from media_downloader import _client_conn_errors, _maybe_reconnect_client
            _client_conn_errors["count"] += 3  # Add three toward the reconnect threshold of ten to accelerate recovery.
            asyncio.create_task(_maybe_reconnect_client())
        except Exception:
            pass
        try:
            await node.bot.send_message(
                int(node.from_user_id),
                f"🐌 {_t('Telegram download may be speed limited')}\n"
                f"{_t('Task')}: {getattr(node, 'task_id_display', str(node.task_id))}\n"
                f"{_t('File')}: {os.path.basename(file_name)}\n"
                + _t("Speed below {speed}/s for {seconds} seconds",
                     speed=format_byte(_SLOW_THRESHOLD_BPS), seconds=_SLOW_SUSTAIN_SEC)
            )
        except Exception:
            pass
    elif _throttle_action == "clear" and node.bot and getattr(node, "from_user_id", ""):
        try:
            await node.bot.send_message(
                int(node.from_user_id),
                f"✅ {_t('Telegram rate limit cleared')}\n"
                f"{_t('Task')}: {getattr(node, 'task_id_display', str(node.task_id))}\n" +
                _t("Download speed is back to normal")
            )
        except Exception:
            pass

    # Refresh Bot status as soon as a placeholder resolves to real data.
    if _placeholder_resolved and node.bot:
        node.last_progress_pct = -1  # Reset progress buckets so the initial 0-20% range also triggers an update.
        from module.pyrogram_extension import report_bot_status
        await report_bot_status(node.bot, node, immediate_reply=True)

    # Send initial progress report when download first starts
    if node.bot and not node.initial_progress_reported and down_byte > 0:
        node.initial_progress_reported = True
        from module.pyrogram_extension import report_bot_status
        await report_bot_status(node.bot, node)

    # Report progress at every 20% milestone during active download
        dl_result = _download_result.get(chat_id, {})
        total = 0
        weighted = 0
        for _v in dl_result.values():
            if str(_v.get("task_id")) == str(node.task_id):
                ts = _v.get("total_size", 0)
                if ts > 0:
                    total += ts
                    weighted += _v.get("down_byte", 0)
        if total > 0:
            pct = int(weighted / total * 100)
            bucket = (pct // 20) * 20
            prev = (node.last_progress_pct // 20) * 20 if node.last_progress_pct >= 0 else -1
            if bucket != prev:
                node.last_progress_pct = pct
                await report_bot_status(node.bot, node)

    if cur_time - _last_download_time >= 1.0:
        # update speed
        _total_download_speed = int(
            _total_download_size / (cur_time - _last_download_time)
        )
        _total_download_speed = max(_total_download_speed, 0)
        _total_download_size = 0
        _last_download_time = cur_time


_HISTORY_FILE = os.path.join(os.path.abspath("."), "log", "download_history.json")


def save_downloads():
    """Save completed and failed downloads to file for persistence."""
    try:
        # Build completed list from _download_result
        completed = []
        for chat_id, messages in _download_result.items():
            for msg_id, value in messages.items():
                if value["down_byte"] == value["total_size"] and value["total_size"] > 0:
                    completed.append({
                        "task_id": str(value.get("task_id", "")),
                        "chat_id": str(chat_id),
                        "msg_id": str(msg_id),
                        "file_name": value.get("file_name", ""),
                        "total_size": value.get("total_size", 0),
                        "chat_title": value.get("source_chat_title", "") or get_chat_title(chat_id),
                        "start_time": value.get("start_time", 0),
                        "end_time": value.get("end_time", 0),
                        "task_id_display": value.get("task_id_display", ""),
                    })

        data = {
            "completed": completed,
            "failed": _failed_downloads,
            "chat_titles": _chat_titles,
        }

        os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
        # Write atomically through a temporary file and rename.
        tmp_file = _HISTORY_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, _HISTORY_FILE)
        logger.info(f"Saved {len(completed)} completed, {len(_failed_downloads)} failed downloads")
    except Exception as e:
        logger.warning(f"Failed to save download history: {e}")


def load_downloads():
    """Load completed and failed downloads from file."""
    global _download_result, _failed_downloads, _chat_titles
    try:
        if not os.path.exists(_HISTORY_FILE):
            return

        with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Restore chat titles
        _chat_titles = data.get("chat_titles", {})

        # Restore completed downloads into _download_result
        for item in data.get("completed", []):
            chat_id = item.get("chat_id", "")
            msg_id = item.get("msg_id", "")
            if chat_id and msg_id:
                if chat_id not in _download_result:
                    _download_result[chat_id] = {}
                _download_result[chat_id][msg_id] = {
                    "down_byte": item.get("total_size", 0),
                    "total_size": item.get("total_size", 0),
                    "file_name": item.get("file_name", ""),
                    "start_time": item.get("start_time", 0),
                    "end_time": item.get("end_time", 0),
                    "download_speed": 0,
                    "each_second_total_download": 0,
                    "task_id": item.get("task_id", ""),
                    "task_id_display": item.get("task_id_display", ""),
                    "source_chat_title": item.get("chat_title", ""),
                }

        # Restore failed downloads
        _failed_downloads = data.get("failed", [])

        logger.info(f"Loaded {len(data.get('completed', []))} completed, {len(_failed_downloads)} failed downloads")
    except Exception as e:
        logger.warning(f"Failed to load download history: {e}")
