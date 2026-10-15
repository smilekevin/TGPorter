"""Shared download management operations for Web API and MCP, without HTTP state."""

import asyncio
import datetime
import logging
import os
import re
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import urlsplit

from lang import _t

import utils
from module.task_store import get_pending_tasks, remove_task
from module.download_stat import (
    DownloadState,
    delete_task,
    get_chat_title,
    get_download_result,
    get_download_state,
    get_failed_downloads,
    get_total_download_speed,
    is_task_paused,
    pause_task,
    remove_failed_download,
    resume_task,
    set_download_state,
)
from utils.format import format_byte


logger = logging.getLogger("download.service")


def parse_download_url(url):
    """Accept a single Telegram post, including private-channel and topic links."""
    error = "Use an HTTPS t.me message link, such as https://t.me/channel/123 or https://t.me/c/123456/789."
    if not isinstance(url, str) or not 1 <= len(url) <= 2048 or any(c.isspace() for c in url):
        raise ValueError(error)
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc.lower() != "t.me"
                or parsed.query or parsed.fragment):
            raise ValueError(error)
        parts = parsed.path.strip("/").split("/")
        if parts[0] == "s" and len(parts) == 3:
            parts = parts[1:]
        private = parts[0] == "c"
        if private:
            if len(parts) not in (3, 4) or not re.fullmatch(r"[1-9][0-9]*", parts[1]):
                raise ValueError(error)
            chat_id = int("-100" + parts[1])
            ids = parts[2:]
        else:
            if len(parts) not in (2, 3) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", parts[0]):
                raise ValueError(error)
            chat_id = parts[0]
            ids = parts[1:]
        if not all(re.fullmatch(r"[1-9][0-9]*", value) and int(value) <= 2147483647 for value in ids):
            raise ValueError(error)
        return chat_id, int(ids[-1]), int(ids[0]) if len(ids) == 2 else 0, "https://t.me/" + "/".join(parts)
    except (ValueError, IndexError):
        raise ValueError(error) from None


async def _async_retry_download(chat_id, msg_id, from_user_id="", placeholder_task_id="", source_link="", original_task_id=""):
    """Async helper: fetch the message and re-add to download queue

    Args:
        source_link: If provided, use parse_link → get_messages to get a fresh
                     message copy. This is more reliable than direct get_messages
                     because it resolves the link the same way download_from_link does.
        original_task_id: The original failed task_id, used to restore the failed
                          entry if retry fails. Without this, the task "disappears"
                          from the failed list.
    """
    logger = logging.getLogger("web.retry")

    def _restore_failed(reason):
        """Re-add to failed list if retry can't even start downloading."""
        try:
            from module.download_stat import add_failed_download
            add_failed_download(
                chat_id=chat_id,
                msg_id=msg_id,
                task_id=original_task_id or placeholder_task_id or "",
                file_name="",
                error_message=reason,
                total_size=0,
                source_link=source_link or "",
                from_user_id=str(from_user_id) if from_user_id else "",
            )
        except Exception:
            pass
        # Remove placeholders and task nodes so bot_tasks.json does not retain stale entries.
        try:
            if placeholder_task_id:
                from module.task_store import remove_task
                remove_task(placeholder_task_id)
        except Exception:
            pass

    try:
        try:
            cid = int(chat_id)
        except (ValueError, TypeError):
            cid = chat_id

        from module.bot import _bot

        client = _bot.client
        if not client:
            logger.error("Retry failed: _bot.client is not available")
            _restore_failed('Retry failed: client unavailable')
            return

        msg = None

        # Strategy 1: If we have a source_link, use parse_link to get a fresh
        # message. This mirrors the download_from_link path and is more reliable
        # for forwarded messages with stale file references.
        if source_link:
            try:
                from module.pyrogram_extension import parse_link
                link_chat_id, link_msg_id, _ = await parse_link(client, source_link)
                if link_chat_id and link_msg_id:
                    link_msg = await client.get_messages(link_chat_id, link_msg_id)
                    if link_msg and not link_msg.empty:
                        msg = link_msg
                        logger.info(
                            f"Retry: source_link parse succeeded, "
                            f"got msg {msg.id} from chat {link_chat_id}"
                        )
                    else:
                        logger.warning(
                            f"Retry: source_link got empty message for "
                            f"{link_chat_id}/{link_msg_id}, falling back to direct get_messages"
                        )
            except Exception as e:
                logger.warning(
                    f"Retry: source_link parse failed ({source_link}): {e}, "
                    f"falling back to direct get_messages"
                )

        # Strategy 2: Fallback to direct get_messages with original chat_id/msg_id
        if not msg:
            message_client = _bot.get_message_client(cid, from_user_id)
            if message_client is None:
                _restore_failed('Retry failed: message client unavailable')
                return
            msg = await message_client.get_messages(cid, int(msg_id))

        if not msg or msg.empty:
            logger.error(f"Retry failed: message {msg_id} not found in chat {cid}")
            _restore_failed('Retry failed: message not found or deleted')
            return

        from module.app import TaskNode, TaskType

        # A source-link retry can resolve a different chat/message pair. Persist
        # the actual fetched pair and account so a later restart can refetch it.
        cid, msg_id = msg.chat.id, msg.id
        owner = getattr(msg, "_client", None) or _bot.get_message_client(cid, from_user_id)

        node = TaskNode(
            chat_id=cid,
            from_user_id=from_user_id or cid,
            reply_message_id=0,
            replay_message="WebUI retry",
            limit=1,
            bot=_bot.bot,
            task_id=_bot.gen_task_id(),
        )
        _bot.add_task_node(node)

        # Persist to bot_tasks.json so the task survives container restarts
        from module.task_store import save_task as _save_task
        saved_ok = _save_task(
            task_id=node.task_id,
            chat_id=cid,
            url="",
            start_offset_id=0,
            end_offset_id=0,
            limit=1,
            download_filter=None,
            from_user_id=from_user_id or cid,
            task_type="download",
            extra_data={"task_id_display": node.task_id_display, "message_id": msg_id,
                        "message_client": "bot" if owner is _bot.bot else "user"},
        )
        if saved_ok is False:
            logger.warning(f"Retry skipped: save_task refused (duplicate) chat={cid} msg={msg_id}")
            return

        # Clean up the placeholder entry created by batch_retry
        if placeholder_task_id:
            try:
                from module.task_store import remove_task as _remove_placeholder
                _remove_placeholder(placeholder_task_id)
            except Exception:
                pass

        # Cache message and trigger consumer — same flow as direct_download
        _bot._cached_messages[node.task_id] = msg
        node.is_running = True

        # Send retry notification to user via bot and record message ID for later editing
        if from_user_id and _bot and _bot.bot:
            try:
                chat_name = getattr(msg.chat, "title", str(cid))
                msg_text = f"🔄 {_t('Retry task queued for download')}\n"
                msg_text += f"{_t('Message')}: {msg_id}\n"
                msg_text += f"{_t('Task')}: {node.task_id_display}\n"
                msg_text += f"{_t('Group')}: {chat_name}"
                sent_msg = await _bot.bot.send_message(int(from_user_id), msg_text)
                # Store the message ID so report_bot_status can edit this notification with
                # progress.
                node.reply_message_id = sent_msg.id
            except Exception as e:
                logger.warning(f"Retry notification failed for user {from_user_id}: {e}")

        logger.info(f"Retry: queued message {msg_id} from chat {cid} as task {node.task_id_display}")

        # Do NOT trigger _consume_one_pending directly — let the periodic
        # _pending_consumer_loop (every 2s) pick it up. This ensures the
        # concurrency guard is always respected, preventing over-fill when
        # many retries are submitted simultaneously.
    except Exception as e:
        logger.error(f"Retry failed for chat={chat_id} msg={msg_id}: {e}", exc_info=True)
        _restore_failed(f'Retry failed: {e}')


class DownloadService:
    """Operate on the running downloader; never create another worker pool or event loop."""

    def __init__(self, app, retry_download=None):
        self.app = app
        self.retry_download = retry_download or _async_retry_download

    def submit_download(self, url):
        """Resolve and persist on the downloader loop; never fetch media in a request thread."""
        try:
            chat_id, message_id, topic_id, canonical_url = parse_download_url(url)
        except ValueError as error:
            return {"code": "0", "message": str(error)}
        from module.bot import _bot
        loop = getattr(self.app, "loop", None)
        if (not loop or not loop.is_running() or loop.is_closed()
                or _bot.app is not self.app or not _bot.client or not _bot.is_running
                or not callable(getattr(_bot, "add_download_task", None))):
            return {"code": "0", "message": "Downloader is not ready; try again after Telegram startup."}
        operation = _bot.submit_download_link(chat_id, message_id, canonical_url, topic_id)
        try:
            future = asyncio.run_coroutine_threadsafe(operation, loop)
        except RuntimeError:
            operation.close()
            return {"code": "0", "message": "Downloader is shutting down."}
        try:
            return future.result(timeout=15)
        except FutureTimeout:
            future.cancel()
            return {"code": "0", "message": "Submission timed out; check pending tasks before retrying."}

    def get_completed_count(self):
        """Lightweight endpoint: return only the total count of completed downloads.
        Used for polling to detect new completions without fetching full data."""
        count = 0
        for messages in get_download_result().values():
            for value in messages.values():
                if value["down_byte"] == value["total_size"] and value["total_size"] > 0:
                    count += 1
        return {'total': count}

    def get_download_speed(self):
        """Get download speed"""
        return {'download_speed': format_byte(get_total_download_speed()) + "/s", 'upload_speed': "0.00 B/s"}

    def get_flood_wait(self):
        """Get unified FloodWait cooldown status for WebUI display."""
        from module.pyrogram_extension import is_flood_wait_active, get_flood_wait_remaining, _unified_flood_wait
        from module.download_stat import _throttle_state
        import time as _now
        now = _now.time()
        # Keep throttling visible while notified is set. Container restart resets
        # _throttle_state automatically.
        throttle_active = _throttle_state["notified"]
        throttle_elapsed = int(now - _throttle_state["since"]) if _throttle_state["since"] > 0 else 0
        return {'active': is_flood_wait_active(), 'remaining': int(get_flood_wait_remaining()), 'reason': _unified_flood_wait.get("reason", ""), 'throttle': {
                "active": throttle_active,
                "elapsed": throttle_elapsed,
            }}

    def web_get_download_state(self):
        """Get current download state"""
        state = get_download_state()
        if state is DownloadState.Downloading:
            return "downloading"
        return "paused"

    def web_set_download_state(self, state=None):
        """Set download state"""

        if state == "continue" and get_download_state() is DownloadState.StopDownload:
            set_download_state(DownloadState.Downloading)

        if state == "pause" and get_download_state() is DownloadState.Downloading:
            set_download_state(DownloadState.StopDownload)

        # Always return action based on actual current state
        return "pause" if get_download_state() is DownloadState.Downloading else "continue"

    def get_app_version(self):
        """Get telegram_media_downloader version"""
        return utils.__version__

    def get_max_workers(self):
        """Get current max download task count"""
        return {'max_workers': self.app.max_download_task}

    def set_max_workers(self, n=None):
        """Set max download task count (1-6, hot-applies to the live worker pool)"""
        if n is None or n < 1 or n > 6:
            return {'error': "n must be 1-6"}, 400
        self.app.max_download_task = n
        # Leave enough transport slots for each file's independent fixed ranges.
        self.app.max_concurrent_transmissions = n * max(5, getattr(self.app, "file_download_concurrency", 1))
        # Apply to pyrogram client if available
        from module.pyrogram_extension import set_max_concurrent_transmissions
        try:
            from module.bot import _bot
            if _bot and _bot.client:
                set_max_concurrent_transmissions(_bot.client, self.app.max_concurrent_transmissions)
        except Exception:
            pass
        # Resize workers immediately. media_downloader runs as __main__; importing its module
        # name would create a second copy with an empty _main_client_ref. Use
        # sys.modules['__main__'], matching the Bot consumer's _active_downloads handling.
        try:
            import sys as _sys
            _md = _sys.modules.get("__main__")
            if _md is not None and hasattr(_md, "_worker_pool_set_target"):
                _md._worker_pool_set_target(n)
            else:
                logger.warning("set_max_workers: __main__ has no _worker_pool_set_target")
        except Exception as e:
            logger.warning(f"set_max_workers: worker pool adjust failed: {e}")
        # Persist to config file
        self.app.update_config(True)
        return {'max_workers': n}

    def get_download_list(self, already_down=None, offset=0, limit=0, search=""):
        """Get download list with task_id and status"""
        from module.download_stat import get_download_result
        # Removed: load_downloads() on empty result — it overwrites runtime data
        # with stale disk data when all downloads happen to be between states.
        # load_downloads() is already called at startup; trust the in-memory state.
        if already_down is None:
            return []

        already_down = already_down == "true"

        # Server-side search for completed downloads
        search = search.strip().lower()

        download_result = get_download_result()
        result = []
        for chat_id, messages in download_result.items():
            for idx, value in messages.items():
                is_already_down = value["down_byte"] == value["total_size"] and value["total_size"] > 0

                if already_down and not is_already_down:
                    continue
                if not already_down:
                    # Hide placeholder entries (total_size==0, down_byte==0) — these
                    # are created by consumer for pending tasks but aren't actively
                    # downloading yet. Only show tasks with real progress data.
                    if value.get("total_size", 0) == 0 and value.get("down_byte", 0) == 0:
                        continue
                    # Exclude completed history from active polling. Thousands of resident
                    # completed rows previously produced multi-megabyte responses every second.
                    # Completed history uses already_down=true pagination.
                    if is_already_down:
                        continue

                progress = value.get("progress", 0)
                if not progress and value["total_size"] > 0:
                    progress = round(value["down_byte"] / value["total_size"] * 100, 1)

                # Staleness check: if speed hasn't been updated in 3s (no Pyrogram callback), show 0
                import time as _now
                raw_speed = value["download_speed"]
                if raw_speed > 0 and not is_already_down:
                    last_update = value.get("end_time", 0)
                    if (_now.time() - last_update) > 3.0:
                        raw_speed = 0
                download_speed = format_byte(raw_speed) + "/s"

                # ETA calculation (use stale-checked speed)
                eta = ""
                speed = raw_speed
                if speed > 0 and not is_already_down:
                    remaining = value["total_size"] - value["down_byte"]
                    eta_seconds = int(remaining / speed)
                    if eta_seconds >= 3600:
                        eta = f"{eta_seconds // 3600}h{(eta_seconds % 3600) // 60:02d}m"
                    elif eta_seconds >= 60:
                        eta = f"{eta_seconds // 60}m{eta_seconds % 60:02d}s"
                    else:
                        eta = f"{eta_seconds}s"

                # Internal key for operations (stable across restarts)
                task_id = f"{chat_id}_{idx}"

                # Determine status: completed, paused, waiting, or active
                if is_already_down:
                    status = "completed"
                elif is_task_paused(task_id) or is_task_paused(value.get("task_id", "")):
                    status = "paused"
                elif value.get("total_size", 0) == 0 and value.get("down_byte", 0) == 0:
                    status = "waiting"
                else:
                    status = "active"

                # Distinguish fetching placeholders from downloads: total_size<=1 and
                # down_byte==0 indicate a new consumer placeholder. Pyrogram's first callback
                # replaces total_size with the real value.
                if status == "active" and value.get("total_size", 0) <= 1 and value.get("down_byte", 0) == 0:
                    phase = "fetching"
                else:
                    phase = "downloading"

                # Get chat title from cache
                source_title = value.get("source_chat_title", "")
                if not source_title and value.get("source_chat_id"):
                    source_title = get_chat_title(value["source_chat_id"]) or ""
                chat_title = source_title or get_chat_title(chat_id)

                # Format times
                end_time = value.get("end_time", 0)
                start_time = value.get("start_time", 0)
                completed_time = ""
                created_at = ""
                if start_time:
                    created_at = datetime.datetime.fromtimestamp(start_time).strftime("%m-%d %H:%M:%S")
                if is_already_down:
                    ts = end_time if end_time else start_time
                    if ts:
                        completed_time = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")

                # Server-side search filter (by task_id or filename)
                if search:
                    task_id_display = value.get("task_id_display", "") or task_id
                    searchable = f"{task_id} {task_id_display} {os.path.basename(value['file_name'])}".lower()
                    if search not in searchable:
                        continue

                result.append({
                    "task_id": str(task_id),
                    "chat": str(chat_id),
                    "chat_title": chat_title,
                    "id": str(idx),
                    "filename": os.path.basename(value["file_name"]) if value.get("file_name") else f'Message {idx} (pending download)',
                    "total_size": format_byte(value["total_size"]) if value["total_size"] > 0 else 'Unknown',
                    "total_size_bytes": value["total_size"],
                    "download_progress": str(progress),
                    "download_progress_raw": progress,
                    "download_speed": download_speed,
                    "eta": eta,
                    "save_path": value["file_name"].replace("\\", "/"),
                    "status": status,
                    "phase": phase,
                    "start_time": start_time,
                    "end_time": end_time,
                    "created_at": created_at,
                    "completed_time": completed_time,
                    "task_id_display": value.get("task_id_display", "") or task_id,
                })

        # Sort by time: active by start_time (newest first), completed by end_time (newest first)
        if already_down:
            result.sort(key=lambda x: x.get("end_time", 0) or x.get("start_time", 0), reverse=True)
        else:
            result.sort(key=lambda x: x.get("start_time", 0), reverse=True)

        total = len(result)

        # Apply pagination slice (for completed downloads with limit > 0)
        if already_down and limit > 0:
            result = result[offset:offset + limit]

        return result if not already_down else {
            "tasks": result,
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def web_get_failed_downloads(self):
        """Get list of failed downloads"""
        failed = get_failed_downloads()
        # Sort by failure timestamp, newest first
        failed = sorted(failed, key=lambda f: f.get("timestamp", 0), reverse=True)
        result = []
        for f in failed:
            chat_id = f.get("chat_id", "")
            chat_title = get_chat_title(chat_id)
            failed_time = ""
            ts = f.get("timestamp", 0)
            if ts:
                failed_time = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
            result.append({
                "task_id": str(f["task_id"]),
                "chat": str(chat_id),
                "chat_title": chat_title,
                "id": str(f.get("msg_id", "")),
                "filename": os.path.basename(f.get("file_name", "")),
                "error_message": f.get("error_message", "Unknown error"),
                "total_size": format_byte(f.get("total_size", 0)),
                "failed_time": failed_time,
                "source_link": f.get("source_link", ""),
                "from_user_id": f.get("from_user_id", "") or "",
            })
        return result

    def web_pause_task(self, task_id=None):
        """Pause a specific download task"""
        if not task_id:
            return {"code": "0", "message": "Task ID required"}
        pause_task(task_id)
        return {"code": "1", "message": "paused"}

    def web_resume_task(self, task_id=None):
        """Resume a specific download task"""
        if not task_id:
            return {"code": "0", "message": "Task ID required"}
        success = resume_task(task_id)
        if success:
            return {"code": "1", "message": "resumed"}
        return {"code": "0", "message": "Task not paused"}

    def _task_files(self, task_id):
        """Find exact recorded paths; failed rows are no longer in active results."""
        for chat_id, messages in get_download_result().items():
            for msg_id, value in messages.items():
                if str(task_id) in (f"{chat_id}_{msg_id}", str(value.get("task_id", ""))):
                    return value.get("file_name", ""), ""
        for value in get_failed_downloads():
            if str(value.get("task_id", "")) == str(task_id):
                return value.get("file_name", ""), value.get("temp_file_name", "")
        return "", ""

    def web_check_file_exists(self, task_id=None):
        """Check recorded output and failed-task staging files."""
        if not task_id:
            return {"code": "0", "message": "Task ID required"}
        from module.parallel_download import has_parts
        output, staging = self._task_files(task_id)
        path = output or staging
        exists = bool(output and os.path.isfile(output)) or bool(
            staging and (os.path.isfile(staging) or os.path.isfile(staging + ".temp") or has_parts(staging)))
        return {"code": "1", "exists": exists, "path": path.replace("\\", "/"),
                "filename": os.path.basename(path)}

    def web_delete_task(self, task_id=None, delete_file=False):
        """Cancel/remove a task; writers clean staging only when explicitly requested."""
        from module.parallel_download import discard_partial_download
        from module.pyrogram_extension import remove_download_cache
        if not task_id:
            return {"code": "0", "message": "Task ID required"}
        file_path, staging = self._task_files(task_id)
        # Finished cache entries can be cleared; active attempts clear their own cache.
        for chat_id, messages in get_download_result().items():
            for msg_id, value in messages.items():
                if str(task_id) in (f"{chat_id}_{msg_id}", str(value.get("task_id", ""))):
                    remove_download_cache(chat_id, msg_id, only_finished=True)
        # Mark cancellation before touching files. Active writers own staging cleanup.
        success = delete_task(task_id, discard_partial=delete_file)
        failed = remove_failed_download(task_id)
        if not success and not failed:
            return {"code": "0", "message": "Task not found"}
        file_deleted = False
        file_error = ""
        if delete_file:
            try:
                if file_path and os.path.isfile(file_path):
                    os.remove(file_path)
                    file_deleted = True
                if failed and staging:
                    discard_partial_download(staging)
            except OSError as error:
                file_error = str(error)
        return {"code": "1", "message": "deleted", "file_deleted": file_deleted,
                "file_error": file_error}

    def web_batch_delete(self, data=None):
        """Use the same cancellation and file policy for single and batch deletions."""
        if not isinstance(data, dict) or "task_ids" not in data:
            return {"code": "0", "message": "Task IDs required"}
        if not isinstance(data["task_ids"], list):
            return {"code": "0", "message": "Task IDs must be a list"}
        deleted = files_deleted = files_not_found = 0
        for task_id in dict.fromkeys(data["task_ids"]):
            result = self.web_delete_task(task_id, delete_file=data.get("delete_file", False))
            deleted += result["code"] == "1"
            files_deleted += bool(result.get("file_deleted"))
            files_not_found += bool(result.get("file_error"))
        return {"code": "1", "message": f"deleted {deleted} tasks", "deleted": deleted,
                "files_deleted": files_deleted, "files_not_found": files_not_found}

    def web_retry_task(self, task_id=None):
        """Retry a failed download task - re-download the file"""
        if not task_id:
            return {"code": "0", "message": "Task ID required"}

        # Find the failed entry to get chat_id + msg_id
        failed_list = get_failed_downloads()
        target = None
        for f in failed_list:
            if str(f.get("task_id", "")) == str(task_id):
                target = f
                break

        if not target:
            return {"code": "0", "message": "Task not found in failed list"}

        chat_id = target.get("chat_id")
        msg_id = target.get("msg_id")
        from_user_id = target.get("from_user_id", "") or ""
        source_link = target.get("source_link", "") or ""
        if not chat_id or not msg_id:
            return {"code": "0", "message": "Incomplete task data (missing chat_id/msg_id)"}

        # Remove from failed list first
        remove_failed_download(task_id)

        # Submit async retry to the main event loop
        try:
            if self.app and self.app.loop:
                asyncio.run_coroutine_threadsafe(
                    self.retry_download(chat_id, msg_id, from_user_id, source_link=source_link, original_task_id=task_id),
                    self.app.loop,
                )
                return {"code": "1", "message": 'Queued for retry'}
            return {"code": "0", "message": 'Application is still starting'}
        except Exception as e:
            return {"code": "0", "message": f'Retry failed: {str(e)}'}

    def web_batch_retry(self, data=None):
        """Batch retry multiple failed downloads"""
        if not data or "task_ids" not in data:
            return {"code": "0", "message": "Task IDs required"}

        task_ids = data["task_ids"]
        if not isinstance(task_ids, list):
            return {"code": "0", "message": "Task IDs must be a list"}

        failed_list = get_failed_downloads()
        queued = 0
        errors = []

        for task_id in task_ids:
            target = None
            for f in failed_list:
                if str(f.get("task_id", "")) == str(task_id):
                    target = f
                    break

            if not target:
                errors.append(f"{task_id}: not found")
                continue

            chat_id = target.get("chat_id")
            msg_id = target.get("msg_id")
            from_user_id = target.get("from_user_id", "")
            source_link = target.get("source_link", "") or ""
            if not chat_id or not msg_id:
                errors.append(f"{task_id}: incomplete data")
                continue

            # Remove from failed list first
            remove_failed_download(task_id)

            # Immediately insert a placeholder into bot_tasks.json so WebUI sees it
            from module.task_store import save_task as _save_placeholder
            _save_placeholder(
                task_id=f"retry_{task_id}",
                chat_id=int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
                url="",
                start_offset_id=0,
                end_offset_id=0,
                limit=1,
                download_filter=None,
                from_user_id=from_user_id or (int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id),
                task_type="retry",
                extra_data={"source_task_id": task_id, "message_id": msg_id, "pending": True},
            )

            # Submit async retry
            try:
                if self.app and self.app.loop:
                    asyncio.run_coroutine_threadsafe(
                        self.retry_download(chat_id, msg_id, from_user_id, placeholder_task_id=f"retry_{task_id}", source_link=source_link, original_task_id=task_id),
                        self.app.loop,
                    )
                    queued += 1
                else:
                    errors.append(f"{task_id}: app not initialized")
            except Exception as e:
                errors.append(f"{task_id}: {str(e)}")

        return {
            "code": "1",
            "message": (f"Queued {queued} tasks for retry; {len(errors)} failed" if errors else
                        f"Queued {queued} tasks for retry"),
            "queued": queued,
            "errors": errors,
        }

    def web_get_pending_list(self, offset=0, limit=100):
        """Get list of pending tasks (received but not started downloading)"""
        import time as _time
        pending = get_pending_tasks()
        # Filter out tasks already in _download_result (consumer created placeholder).
        # These will show up in the active download list — no need to duplicate in pending.
        from module.download_stat import get_download_result as _get_dr
        _dr = _get_dr()

        result = []
        for task in pending:
            # Exclude tasks already represented by _download_result placeholders from Pending;
            # they are about to appear in Active.
            _cid = task.get("chat_id")
            _mid = task.get("extra_data", {}).get("message_id")
            if _cid is not None and _mid is not None:
                try:
                    _cid_int = int(_cid)
                    _mid_int = int(_mid)
                    if _cid_int in _dr and _mid_int in _dr[_cid_int]:
                        continue
                except (ValueError, TypeError):
                    pass  # On unexpected types, conservatively keep the task visible in Pending.

            task_id = task.get("task_id", "")
            created_at = task.get("created_at", 0)
            created_at_str = ""
            wait_time = ""
            if created_at:
                created_at_str = datetime.datetime.fromtimestamp(created_at).strftime("%m-%d %H:%M:%S")
                elapsed = int(_time.time() - created_at)
                h, m = divmod(elapsed // 60, 60)
                wait_time = f"{h}h{m:02d}m" if h > 0 else f"{m}m"

            # Try to get chat title from task data or URL
            chat_title = task.get("extra_data", {}).get("chat_title", "")
            url = task.get("url", "")

            # Determine source type
            source_type = task.get("task_type", "download")
            if source_type == "download":
                source_type = 'Channel post'
            elif source_type == "forward":
                source_type = 'Forwarded message'
            else:
                source_type = 'Message'

            # Build filename from URL or extra_data
            filename = task.get("extra_data", {}).get("filename", "")
            if not filename and url:
                filename = url.split("/")[-1] if "/" in url else url
            if not filename:
                filename = f"task_{task_id}"

            total_size = task.get("extra_data", {}).get("total_size", "")
            if isinstance(total_size, (int, float)) and total_size > 0:
                total_size = format_byte(total_size)
            elif not total_size:
                total_size = 'Unknown'

            task_id_display = task.get("extra_data", {}).get("task_id_display", str(task_id))

            # Every pending task uses the Waiting label.
            queue_label = 'Waiting'

            result.append({
                "task_id": str(task_id),
                "task_id_display": task_id_display,
                "chat": str(task.get("chat_id", "")),
                "chat_title": chat_title,
                "filename": filename,
                "total_size": total_size,
                "url": url,
                "source_type": source_type,
                "created_at": created_at_str,
                "created_ts": created_at,
                "wait_time": wait_time,
                "queue_label": queue_label,
            })

        # Sort by created_at ascending (earliest first)
        result.sort(key=lambda x: x.get("created_ts", 0))

        # Paginate at 100 rows by default to avoid serializing thousands of pending tasks. The
        # frontend supplies an offset when scrolling to the next page.
        total = len(result)
        page = result[offset : offset + limit] if limit > 0 else result[offset:]
        return {"tasks": page, "total": total}

    def web_remove_pending(self, task_id=None):
        """Remove a pending task from the queue"""
        if not task_id:
            return {"code": "0", "message": "Task ID required"}
        # Try int conversion since task_store uses int task_ids
        try:
            task_id_int = int(task_id)
        except (ValueError, TypeError):
            task_id_int = task_id
        remove_task(task_id_int)
        return {"code": "1", "message": "removed"}
