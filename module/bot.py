"""Bot for media downloader"""

import asyncio
import os
import time
from datetime import datetime
from functools import wraps
from typing import Callable, List, Union

from module.telegram import pyrogram
from loguru import logger
from pyrogram import types
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from ruamel import yaml

import utils
from module.app import (
    Application,
    ChatDownloadConfig,
    ForwardStatus,
    QueryHandler,
    QueryHandlerStr,
    TaskNode,
    TaskType,
    UploadStatus,
)
from module.download_stat import (
    add_failed_download,
    delete_task as _delete_download_progress,
    set_chat_title,
)
from module.filter import Filter
from module.bot_query import query_tasks, query_tasks_callback
from module.task_store import save_task, complete_task, get_running_tasks, update_task_progress, get_pending_tasks, update_download_state
from module.get_chat_history_v2 import get_chat_history_v2
from lang import CATALOGS
from module.language import _t, parse_language
from module.pyrogram_extension import (
    check_user_permission,
    get_utf16_length,
    is_flood_wait_active,
    parse_link,
    proc_cache_forward,
    report_bot_forward_status,
    report_bot_status,
    retry,
    set_max_concurrent_transmissions,
    set_meta_data,
    upload_telegram_chat_message,
)
from utils.format import replace_date_time, validate_title
from utils.meta_data import MetaData

# pylint: disable = C0301, R0902

_BOT_HEARTBEAT_INTERVAL = 60
_BOT_HEARTBEAT_TIMEOUT = 10
_BOT_HEARTBEAT_FAILURES = 3
_BOT_RECONNECT_COOLDOWN = 300


def _cleanup_stopped_task(node):
    """Clear incomplete progress and record a manually stopped task as failed.

    This makes the task visible under Failed with the Manually stopped reason,
    removes lingering active progress and prevents restart recovery. Preserve
    already completed entries; only record rows with down_byte < total_size."""
    try:
        from module.download_stat import get_download_result
        download_result = get_download_result()
        removed = 0
        recorded = 0
        task_id_display = getattr(node, "task_id_display", "") or str(node.task_id)
        for chat_id, messages in list(download_result.items()):
            for msg_id, value in list(messages.items()):
                tid = str(value.get("task_id", ""))
                if tid != str(node.task_id) and tid != task_id_display:
                    continue
                total = value.get("total_size", 0)
                down = value.get("down_byte", 0)
                is_complete = total > 0 and down >= total
                if not is_complete:
                    # Build source link
                    source_link = ""
                    source_chat_id = value.get("source_chat_id", 0) or getattr(node, 'source_chat_id', 0)
                    source_message_id = value.get("source_message_id", 0) or getattr(node, 'source_message_id', 0)
                    if source_chat_id and source_message_id:
                        if str(source_chat_id).startswith("-100"):
                            link_id = str(source_chat_id)[4:]
                        else:
                            link_id = str(source_chat_id)
                        source_link = f"https://t.me/c/{link_id}/{source_message_id}"
                    # Record in failed list before deleting
                    add_failed_download(
                        chat_id=chat_id,
                        msg_id=msg_id,
                        task_id=task_id_display,
                        file_name=value.get("file_name", ""),
                        error_message='Manually stopped',
                        total_size=total,
                        source_link=source_link,
                            from_user_id=getattr(node, "from_user_id", "") or "",
                    )
                    recorded += 1
                    # Delete only incomplete entries from download result
                    _delete_download_progress(f"{chat_id}_{msg_id}", discard_partial=False)
                    removed += 1
        if removed > 0:
            logger.info(f"Cleaned up {removed} download entries for stopped task {task_id_display}")
        else:
            # No download_result entries found — download never started or already cleaned up.
            # Don't write empty failed records (msg_id=0, file_name="") — they're useless
            # and can't be retried via WebUI.
            logger.info(f"Stopped task {task_id_display} had no active download entries to clean")
    except Exception as e:
        logger.warning(f"Failed to cleanup stopped task {getattr(node, 'task_id_display', node.task_id)}: {e}")


def _record_pending_failures(node):
    """Record failed download_status entries that weren't caught by download_task.

    The criteria: entry exists in _download_result, belongs to this task,
    is incomplete (down_byte < total_size), AND has NEVER been touched by
    update_download_status (start_time == end_time means Pyrogram callback 
    never ran → never reached download_media's actual download loop).
    Entries that were touched by download (start_time < end_time) have already
    been handled by download_task's add_failed_download call, so we skip them
    to avoid duplicates.
    """
    try:
        from module.download_stat import get_download_result, get_failed_downloads
        download_result = get_download_result()
        # Collect existing failed composite keys to avoid duplicates
        existing_keys = {f"{f.get('chat_id', '')}_{f.get('msg_id', '')}" for f in get_failed_downloads()}
        recorded = 0
        task_id_display = getattr(node, "task_id_display", "") or str(node.task_id)
        for chat_id, messages in list(download_result.items()):
            for msg_id, value in list(messages.items()):
                tid = str(value.get("task_id", ""))
                if not (tid == str(node.task_id) or tid == task_id_display):
                    continue
                # Skip entries already recorded by download_task
                composite_key = f"{chat_id}_{msg_id}"
                if composite_key in existing_keys:
                    continue
                # Only catch entries that never started downloading
                # (down_byte == 0 or down_byte == total_size with same timestamps
                #  means Pyrogram progress callback never ran)
                if value.get("down_byte", 0) < value.get("total_size", 1) and value.get("total_size", 0) > 0:
                    # Build source link
                    source_link = ""
                    source_chat_id = value.get("source_chat_id", 0) or getattr(node, 'source_chat_id', 0)
                    source_message_id = value.get("source_message_id", 0) or getattr(node, 'source_message_id', 0)
                    if source_chat_id and source_message_id:
                        if str(source_chat_id).startswith("-100"):
                            link_id = str(source_chat_id)[4:]
                        else:
                            link_id = str(source_chat_id)
                        source_link = f"https://t.me/c/{link_id}/{source_message_id}"
                    add_failed_download(
                        chat_id=chat_id,
                        msg_id=msg_id,
                        task_id=task_id_display,
                        file_name=value.get("file_name", ""),
                        error_message='Interrupted download',
                        total_size=value.get("total_size", 0),
                        source_link=source_link,
                            from_user_id=getattr(node, "from_user_id", "") or "",
                    )
                    recorded += 1
        if recorded > 0:
            logger.warning(f"Recorded {recorded} pending failures for task {task_id_display}")
    except Exception as e:
        logger.warning(f"Failed to record pending failures for task {getattr(node, 'task_id_display', node.task_id)}: {e}")


class DownloadBot:
    """Download bot"""

    def __init__(self):
        self.bot = None
        self.client = None
        self.add_download_task: Callable = None
        self.download_chat_task: Callable = None
        self.app = None
        self.listen_forward_chat: dict = {}
        self.config: dict = {}
        self._yaml = yaml.YAML()
        self.config_path = os.path.join(os.path.abspath("."), "bot.yaml")
        self.download_command: dict = {}
        self.filter = Filter()
        self.bot_info = None
        self.task_node: dict = {}
        self.is_running = True
        self.allowed_user_ids: List[Union[int, str]] = []
        self._listen_handler_ref = None

        meta = MetaData(datetime(2022, 8, 5, 14, 35, 12), 0, "", 0, 0, 0, "", 0)
        self.filter.set_meta_data(meta)

        self.download_filter: List[str] = []
        self.task_id: int = 0
        self.reply_task = None
        self.heartbeat_task = None
        self._heartbeat_failures = 0
        self._bot_reconnect_after = 0.0
        self._last_message_at = None
        self._active_handlers = {}

        # In-memory set of task_ids that are in asyncio Queue but not yet picked
        # up by a worker. Replaces the "queued" download_state — no longer written
        # to bot_tasks.json. Cleared on restart (workers re-fetch via get_messages).
        self._in_queue: set = set()
        # Cache of message objects from direct_download so consumer can skip
        # get_messages. {task_id: pyrogram.Message}. Cleared on restart.
        self._cached_messages: dict = {}
        self._consuming: set = set()
        self._active_downloads = 0
        self._pending_consumer_task = None
        self._range_tasks: dict = {}

    def _ensure_heartbeat(self):
        """Monitor the Bot session independently of downloads and progress replies."""
        if self.bot is None:
            return None
        if self.heartbeat_task is None or self.heartbeat_task.done():
            self.heartbeat_task = self.app.loop.create_task(self._heartbeat_loop())
            logger.info("Bot heartbeat started: interval={}s timeout={}s failure_threshold={}",
                        _BOT_HEARTBEAT_INTERVAL, _BOT_HEARTBEAT_TIMEOUT,
                        _BOT_HEARTBEAT_FAILURES)
        return self.heartbeat_task

    async def _heartbeat_loop(self):
        try:
            while self.is_running:
                expected = time.monotonic() + _BOT_HEARTBEAT_INTERVAL
                await asyncio.sleep(_BOT_HEARTBEAT_INTERVAL)
                if not self.is_running:
                    break
                delay = max(0, time.monotonic() - expected)
                if delay >= _BOT_HEARTBEAT_TIMEOUT:
                    logger.warning("Bot heartbeat delayed: event_loop_delay_s={:.1f}", delay)
                try:
                    await self._check_bot_health()
                except Exception as error:
                    logger.error("Bot heartbeat monitor error: {}", type(error).__name__)
        finally:
            logger.info("Bot heartbeat stopped")

    async def _check_bot_health(self):
        """Require a matching Telegram Pong; a connected flag alone is insufficient."""
        from module.pyrogram_extension import (
            _bot_conn_errors, _BOT_RECONNECT_THRESHOLD, _bot_flood_wait,
            _record_bot_flood_wait,
        )
        if self.bot is None:
            return
        remaining = _bot_flood_wait["until"] - time.time()
        if remaining > 0:
            logger.info("Bot heartbeat deferred: FLOOD_WAIT remaining={:.0f}s", remaining)
            return
        started = time.monotonic()
        ping_id = time.monotonic_ns() & ((1 << 63) - 1)
        try:
            async with asyncio.timeout(_BOT_HEARTBEAT_TIMEOUT):
                # Kurigram counts total attempts here; zero skips the send entirely.
                pong = await self.bot.invoke(
                    pyrogram.raw.functions.Ping(ping_id=ping_id), retries=1,
                    timeout=_BOT_HEARTBEAT_TIMEOUT, sleep_threshold=0, retry_delay=0)
                if not isinstance(pong, pyrogram.raw.types.Pong) or pong.ping_id != ping_id:
                    raise ConnectionError("Unexpected heartbeat response")
        except (pyrogram.errors.FloodWait, pyrogram.errors.FloodPremiumWait) as error:
            _record_bot_flood_wait(error.value)
            logger.warning("Bot heartbeat deferred: FLOOD_WAIT {}s", error.value)
            return
        except Exception as error:
            self._heartbeat_failures += 1
            logger.warning("Bot heartbeat failed: error={} elapsed_s={:.1f} failures={}/{} connected={}",
                           type(error).__name__, time.monotonic() - started,
                           self._heartbeat_failures, _BOT_HEARTBEAT_FAILURES,
                           getattr(self.bot, "is_connected", False))
        else:
            if self._heartbeat_failures:
                logger.info("Bot heartbeat recovered: failed_probes={}", self._heartbeat_failures)
            self._heartbeat_failures = 0
            now = time.monotonic()
            dispatcher = getattr(self.bot, "dispatcher", None)
            workers = getattr(dispatcher, "handler_worker_tasks", [])
            queue = getattr(dispatcher, "updates_queue", None)
            oldest_name, oldest_start = min(
                self._active_handlers.values(), key=lambda value: value[1], default=("none", now))
            logger.info(
                "Bot heartbeat OK: rtt_ms={:.0f} connected={} update_queue={} workers_alive={}/{} "
                "active_handlers={} oldest_handler_s={:.0f} oldest_handler={} last_message_age_s={}",
                (now - started) * 1000, getattr(self.bot, "is_connected", False),
                queue.qsize() if queue is not None else 0,
                sum(not task.done() for task in workers), len(workers),
                len(self._active_handlers), now - oldest_start, oldest_name,
                round(now - self._last_message_at) if self._last_message_at is not None else "none")

        # This loop owns reconnects so slow progress reports cannot prevent recovery.
        if (self._heartbeat_failures >= _BOT_HEARTBEAT_FAILURES
                or _bot_conn_errors["count"] >= _BOT_RECONNECT_THRESHOLD):
            now = time.monotonic()
            if now < self._bot_reconnect_after:
                logger.warning("Bot reconnect deferred: cooldown_remaining_s={:.0f}",
                               self._bot_reconnect_after - now)
                return
            self._bot_reconnect_after = now + _BOT_RECONNECT_COOLDOWN
            logger.warning("Bot heartbeat triggering reconnect: heartbeat_failures={} report_errors={}",
                           self._heartbeat_failures, _bot_conn_errors["count"])
            _bot_conn_errors["count"] = 0
            success = await self._reconnect_bot()
            cooldown = _BOT_RECONNECT_COOLDOWN if success else _BOT_HEARTBEAT_INTERVAL
            self._bot_reconnect_after = time.monotonic() + cooldown

    async def _observe_bot_message(self, client, message):
        """Log receipt before authorization without recording message content."""
        self._last_message_at = time.monotonic()
        sender = getattr(message, "from_user", None)
        allowed = sender is not None and sender.id in self.allowed_user_ids
        kind = "media" if message.media else "text"
        if message.text and message.text.startswith("/"):
            kind = "command"
        sent_at = getattr(message, "date", None)
        age = max(0, round(time.time() - sent_at.timestamp())) if sent_at is not None else "unknown"
        logger.info("Bot message received: message_id={} kind={} authorized={} message_age_s={}",
                    message.id, kind, allowed, age)

    def _logged_bot_handler(self, callback):
        """Trace handler completion independently of the Telegram receive path."""
        @wraps(callback)
        async def logged(client, update):
            task = asyncio.current_task()
            started = time.monotonic()
            message = getattr(update, "message", None) or update
            message_id = getattr(message, "id", "none")
            self._active_handlers[task] = (callback.__name__, started)
            logger.info("Bot handler started: handler={} message_id={}", callback.__name__, message_id)
            outcome = "completed"
            try:
                return await callback(client, update)
            except (pyrogram.StopPropagation, pyrogram.ContinuePropagation):
                raise
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except Exception as error:
                outcome = "failed"
                logger.warning("Bot handler failed: handler={} message_id={} error={}",
                               callback.__name__, message_id, type(error).__name__)
                raise
            finally:
                self._active_handlers.pop(task, None)
                logger.info("Bot handler finished: handler={} message_id={} outcome={} elapsed_s={:.1f}",
                            callback.__name__, message_id, outcome, time.monotonic() - started)
        return logged

    def _ensure_pending_consumer(self):
        """Keep one live consumer, including when the task store is empty."""
        if self._pending_consumer_task is None or self._pending_consumer_task.done():
            self._pending_consumer_task = self.app.loop.create_task(
                _pending_consumer_loop()
            )
            logger.info("Pending consumer started (interval=2s)")
        return self._pending_consumer_task

    def _start_range_recovery(self, task_data):
        """Reserve a slot while a recovered history scan feeds the workers."""
        task_id = task_data["task_id"]
        self._consuming.add(task_id)
        self._range_tasks[task_id] = self.app.loop.create_task(
            self._recover_range_task(task_data)
        )

    async def _recover_range_task(self, task_data):
        """Restore batch downloads/forwards that have no single message_id."""
        task_id = task_data["task_id"]
        node = None
        try:
            extra = task_data.get("extra_data") or {}
            is_forward = task_data.get("task_type") == "forward"
            node = TaskNode(
                chat_id=task_data["chat_id"],
                from_user_id=task_data.get("from_user_id"),
                bot=self.bot,
                task_id=task_id,
                task_id_display=extra.get("task_id_display", ""),
                task_type=TaskType.Forward if is_forward else TaskType.Download,
                upload_telegram_chat_id=extra.get("dst_chat_id"),
                download_filter=task_data.get("download_filter"),
                limit=task_data.get("limit", 0),
                start_offset_id=task_data.get("start_offset_id", 0),
                end_offset_id=task_data.get("end_offset_id", 0),
                topic_id=extra.get("topic_id", 0),
            )
            node._recovering_range = True
            self.add_task_node(node)
            update_download_state(task_id, "downloading")
            scan_failed = False
            try:
                if is_forward:
                    source = await self.client.get_chat(node.chat_id)
                    node.has_protected_content = source.has_protected_content
                    node.upload_user = self.client
                    if not node.upload_telegram_chat_id:
                        raise ValueError("Recovered forward task has no destination")

                if is_forward and not node.has_protected_content:
                    # These forwards finish inline, so the checkpoint is safe.
                    offset = task_data.get("last_message_id", node.start_offset_id)
                    async for item in get_chat_history_v2(
                        self.client, node.chat_id, limit=node.limit,
                        max_id=node.end_offset_id, offset_id=offset, reverse=True,
                    ):
                        await forward_normal_content(self.client, node, item)
                        update_task_progress(task_id, item.id)
                        if node.is_stop_transmission:
                            break
                else:
                    config = ChatDownloadConfig()
                    # A scan checkpoint can include files still in the lost
                    # in-memory queue. Rescan; download history skips saved files.
                    config.last_read_message_id = node.start_offset_id
                    config.download_filter = node.download_filter
                    await self.download_chat_task(self.client, config, node)
            except Exception as e:
                scan_failed = True
                logger.warning(f"History recovery failed for task {task_id}: {e}")

            # Drain files already queued before retrying a failed scan.
            while node.total_download_task < node.total_task and not node.is_stop_transmission:
                await asyncio.sleep(3)
            if scan_failed and not node.is_stop_transmission:
                update_download_state(task_id, "pending")
            else:
                complete_task(task_id)
                logger.info(f"History recovery finished for task {task_id}")
        except Exception as e:
            update_download_state(task_id, "pending")
            logger.warning(f"History recovery failed for task {task_id}: {e}")
        finally:
            self._consuming.discard(task_id)
            self._range_tasks.pop(task_id, None)
            if node is not None:
                node._recovering_range = False
                self.task_node.pop(task_id, None)

    def gen_task_id(self) -> int:
        """Gen task id"""
        self.task_id += 1
        return self.task_id

    async def submit_download_link(self, chat_id, message_id, url, topic_id=0):
        """Resolve a post's chat and durably enqueue it for the existing consumer."""
        if is_flood_wait_active():
            return {"code": "0", "message": "Telegram cooldown is active; retry after it expires."}
        try:
            chat = await asyncio.wait_for(self.client.get_chat(chat_id), timeout=10)
        except pyrogram.errors.FloodWait as error:
            from module.pyrogram_extension import _unified_flood_wait
            _unified_flood_wait["until"] = max(_unified_flood_wait["until"], time.time() + error.value + 5)
            _unified_flood_wait["reason"] = f"submit_download FloodWait {error.value}s"
            return {"code": "0", "message": "Telegram rate limit; retry after the cooldown expires."}
        except asyncio.TimeoutError:
            return {"code": "0", "message": "Telegram chat lookup timed out; no task was submitted."}
        except Exception:
            return {"code": "0", "message": "Cannot access this chat with the downloader's Telegram account."}
        if not chat or not isinstance(getattr(chat, "id", None), int):
            return {"code": "0", "message": "Telegram chat could not be resolved."}

        def accepted(task, duplicate=False):
            return {"code": "1", "status": task.get("download_state", "pending"),
                    "task_id": f"{chat.id}_{message_id}", "pending_task_id": str(task["task_id"]),
                    "task_id_display": task["extra_data"].get("task_id_display", ""),
                    "url": url, "duplicate": duplicate}

        # This runs on the download loop. No await occurs between deduplication,
        # ID allocation and persistence, so concurrent submissions cannot race.
        for task in get_running_tasks():
            if (str(task.get("chat_id")) == str(chat.id)
                    and str((task.get("extra_data") or {}).get("message_id")) == str(message_id)):
                return accepted(task, duplicate=True)
        node = TaskNode(chat_id=chat.id, from_user_id="", limit=1,
                        task_id=self.gen_task_id(), topic_id=topic_id)
        extra = {"message_id": message_id, "message_client": "user", "topic_id": topic_id,
                 "task_id_display": node.task_id_display, "submitted_via": "mcp",
                 "source_chat_id": chat.id, "source_message_id": message_id,
                 "source_chat_title": getattr(chat, "title", "") or "",
                 "chat_title": getattr(chat, "title", "") or ""}
        saved = save_task(node.task_id, chat.id, url, 0, 0, 1, None, "",
                          task_type="download", extra_data=extra)
        if not saved:
            return {"code": "0", "message": "Task could not be persisted; submission was not accepted."}
        # The consumer creates the live node and fetches the message later.
        # No Bot notification recipient is inferred for MCP submissions.
        self._ensure_pending_consumer()
        return accepted({"task_id": node.task_id, "extra_data": extra})

    def get_message_client(self, chat_id, from_user_id="", message_client=""):
        """Restore the account that owns a persisted message's chat/ID pair."""
        if message_client == "bot":
            return self.bot
        if message_client == "user":
            return self.client
        # Legacy direct/retry tasks did not store an account. An inbox task's
        # chat_id is its submitting user's ID; the user's client sees a different
        # dialog and different message IDs at that same numeric chat_id.
        if from_user_id and str(chat_id) == str(from_user_id):
            return self.bot
        return self.client

    def add_task_node(self, node: TaskNode):
        """Add task node"""
        self.task_node[node.task_id] = node

    def remove_task_node(self, task_id: int):
        """Remove task node"""
        self.task_node.pop(task_id)

    def stop_task(self, task_id: str, *, discard_partial=True):
        """Stop task"""
        if task_id == "all":
            for value in self.task_node.values():
                value.stop_transmission(discard_partial=discard_partial)
        else:
            try:
                task = self.task_node.get(int(task_id))
                if task:
                    task.stop_transmission(discard_partial=discard_partial)
            except Exception:
                return

    async def update_reply_message(self):
        """Update reply message"""
        while self.is_running:
            for key, value in self.task_node.copy().items():
                if value.is_running:
                    await report_bot_status(self.bot, value)

            for key, value in self.task_node.copy().items():
                if value.is_running and value.is_finish():
                    # Send final status update before completing
                    if not await report_bot_status(self.bot, value, immediate_reply=True):
                        # Keep the final result available until the Bot cooldown expires.
                        continue
                    # If stopped by user, move download progress to failed list and clear data
                    if value.is_stop_transmission:
                        _cleanup_stopped_task(value)
                    # Record any failed download_status entries that weren't caught by
                    # download_task (e.g. messages that were queued but never processed)
                    if not value.is_stop_transmission and value.failed_download_task > 0:
                        _record_pending_failures(value)
                    # Safety net: if media_downloader's immediate complete_task failed,
                    # ensure task is marked complete before removal
                    from module.task_store import complete_task
                    complete_task(value.task_id)
                    self.remove_task_node(key)
                    from module.task_store import get_pending_tasks
                    if get_pending_tasks():
                        _bot.app.loop.create_task(_consume_one_pending())
                elif value.is_running and not value.is_finish() and not value.is_stop_transmission:
                    # Detect running nodes whose counters have stalled after worker exit. Silent
                    # throttling can legitimately pause progress; exempt tasks with a heartbeat
                    # newer than the one-hour idle window.
                    try:
                        from module.download_stat import get_task_heartbeat_age
                        _hb_age = get_task_heartbeat_age(
                            f"{value.chat_id}_{value.task_id}"
                        )
                    except Exception:
                        _hb_age = -1
                    # A heartbeat within the last hour proves recent progress; keep the task
                    # active.
                    if 0 <= _hb_age < 3600:
                        continue
                    idle_time = time.time() - value.last_reply_time
                    if idle_time > 3600:  # No progress for one hour.
                        logger.warning(
                            f"Zombie task detected: {value.task_id_display} "
                            f"idle {int(idle_time)}s, total_task={value.total_task}, "
                            f"total_download_task={value.total_download_task}, force completing"
                        )
                        complete_task(value.task_id)
                        self.remove_task_node(key)
                        from module.task_store import get_pending_tasks
                        if get_pending_tasks():
                            _bot.app.loop.create_task(_consume_one_pending())
            await asyncio.sleep(1)

    async def recover_tasks(self):
        """Recover incomplete bot tasks from previous run.

        Two types of tasks need recovery:
        1. 'pending' - created but never started downloading → re-queue fresh
        2. 'downloading' - was actively downloading → re-execute (resume for forward, re-download for direct)
        """
        all_tasks = []
        try:
            running_tasks = get_running_tasks()
            if not running_tasks:
                logger.info("No incomplete bot tasks to recover")
                return

            # All tasks go through pending queue — no more direct recovery.
            # This controls download concurrency and prevents FLOOD_WAIT storms
            # on restart (previously 100+ tasks would resume simultaneously).
            all_tasks = running_tasks

            if not all_tasks:
                return

            # Reset all tasks to pending state so _consume_one_pending picks them up
            from module.task_store import update_download_state
            for task_data in all_tasks:
                tid = task_data.get("task_id")
                if tid is not None and task_data.get("download_state") != "pending":
                    update_download_state(tid, "pending")

            logger.info(f"Found {len(all_tasks)} tasks, all queued as pending (will consume one by one)")

            # FIX: sync self.task_id to max existing internal id to prevent
            # overwrite on restart. Without this, gen_task_id starts from 0
            # and new tasks reuse ids 1,2,3... overwriting existing tasks.
            try:
                max_tid = max(
                    (t.get("task_id") for t in all_tasks
                     if isinstance(t.get("task_id"), int)),
                    default=0
                )
                if max_tid > self.task_id:
                    self.task_id = max_tid
                    logger.info(f"Synced internal task_id counter to {max_tid} (max existing)")
            except Exception:
                pass

        except Exception as e:
            logger.warning(f"Task recovery failed: {e}")

        # After recovery, update persistent counter to max of all task display IDs
        try:
            from module.task_store import _set_seq, _parse_seq_from_display, _lock
            max_seq = 0
            import time
            today = time.strftime('%m%d')
            for task_data in all_tasks:
                extra = task_data.get("extra_data", {}) or {}
                display = extra.get("task_id_display", "")
                if display:
                    seq = _parse_seq_from_display(display)
                    if seq > max_seq:
                        max_seq = seq
            if max_seq > 0:
                with _lock:
                    _set_seq(today, max_seq)
                logger.info(f"Updated persistent task counter to {max_seq} (after recovering {len(all_tasks)} tasks)")
        except Exception as e:
            logger.warning(f"Failed to update task counter after recovery: {e}")

    async def _recover_forward_task(self, task_data, node, offset_id):
        """Recover a forward task from last checkpoint."""
        from module.pyrogram_extension import report_bot_status
        forward_failed = False
        try:
            async for item in get_chat_history_v2(
                self.client, node.chat_id,
                limit=node.limit, max_id=node.end_offset_id,
                offset_id=offset_id, reverse=True,
            ):
                if not node.has_protected_content:
                    await forward_normal_content(self.client, node, item)
                else:
                    await self.add_download_task(item, node)
                update_task_progress(node.task_id, item.id)
                if node.is_stop_transmission:
                    break
        except Exception as e:
            forward_failed = True
            logger.warning(f"Forward recovery failed for task {node.task_id}: {e}")
        finally:
            await report_bot_status(self.bot, node, immediate_reply=True)
            # Only mark as stopped if forward actually failed (exception thrown).
            # For non-protected content, total_task stays 0 because forward_normal_content
            # doesn't use add_download_task — that's normal, not a failure.
            if forward_failed:
                node.stop_transmission()
            complete_task(node.task_id)

    async def _recover_direct_task(self, task_data, node):
        """Recover a direct download task (single message)."""
        from module.pyrogram_extension import report_bot_status
        extra_data = task_data.get("extra_data", {})
        message_id = extra_data.get("message_id", 0)
        source_chat_id = extra_data.get("source_chat_id", 0)
        source_message_id = extra_data.get("source_message_id", 0)

        if not message_id and not source_message_id:
            logger.warning(f"Direct recovery failed: no message_id for task {node.task_id}")
            complete_task(node.task_id)
            return

        success = False
        msg = None
        try:
            # Try source channel first (more reliable for forwarded messages)
            if source_chat_id and source_message_id:
                msg = await self.client.get_messages(source_chat_id, source_message_id)
                if msg and msg.media:
                    logger.info(f"Recovery: re-downloading from source {source_chat_id}/{source_message_id} for task {node.task_id}")
                else:
                    logger.info(f"Recovery: source message has no media, trying original chat")
                    msg = None

            # Fallback: try original chat_id + message_id
            if not msg and message_id:
                msg = await self.client.get_messages(node.chat_id, message_id)

            if msg and msg.media:
                # Check if file already exists in download history
                from module.download_stat import get_download_result
                _dlr = get_download_result()
                _exists = False
                if node.chat_id in _dlr:
                    for _mid, _val in _dlr[node.chat_id].items():
                        if str(_val.get("task_id")) == str(node.task_id) and _val.get("down_byte", 0) > 0 and _val.get("down_byte") == _val.get("total_size"):
                            _exists = True
                            break
                if not _exists:
                    logger.info(f"Recovery: re-downloading message {message_id} for task {node.task_id}")
                    await self.add_download_task(msg, node)
                    node.is_running = True
                    # Wait for download to finish (no timeout — large files may take hours)
                    while node.total_task == 0 or node.total_download_task < node.total_task:
                        await asyncio.sleep(3)
                        if node.is_stop_transmission:
                            break
                else:
                    logger.info(f"Recovery: file already exists for task {node.task_id}, marking complete")
                    node.total_task = 1
                    node.total_download_task = 1
                    node.success_download_task = 1
                    success = True
                # Check if download actually succeeded
                if node.success_download_task > 0:
                    success = True
                else:
                    logger.warning(f"Recovery: task {node.task_id} completed but 0 successful downloads (total={node.total_task}, success={node.success_download_task})")
            else:
                logger.warning(f"Direct recovery: message not found or has no media (msg={msg is not None}, media={getattr(msg, 'media', None) if msg else None})")
        except Exception as e:
            logger.warning(f"Direct recovery failed for task {node.task_id}: {e}")
        finally:
            await report_bot_status(self.bot, node, immediate_reply=True)
            if not success:
                node.stop_transmission()
            if success:
                complete_task(node.task_id)
            else:
                # Cleanup temp files from failed recovery
                _cleanup_task_temp_files(node.chat_id)
                logger.info(f"Recovery task {node.task_id} not completed, will retry next restart")

    def assign_config(self, _config: dict):
        """assign config from str.

        Parameters
        ----------
        _config: dict
            application config dict

        Returns
        -------
        bool
        """

        self.download_filter = _config.get("download_filter", self.download_filter)

        return True

    def update_config(self):
        """Update config from str."""
        self.config["download_filter"] = self.download_filter

        with open(self.config_path, "w", encoding="utf-8") as yaml_file:
            self._yaml.dump(self.config, yaml_file)

    def _register_listen_handler(self):
        """Register a user-client NewMessage handler for configured channel events."""
        if self._listen_handler_ref:
            return  # Already registered.

        async def _on_new_message(client, message):
            chat_id = message.chat.id
            if not message.media:
                logger.debug(f"NewMessage handler: msg {message.id} from chat {chat_id} has no media, skipping")
                return

            # Check whether this channel belongs to listen_forward_chat.
            if chat_id in self.listen_forward_chat:
                node = self.listen_forward_chat[chat_id]
                if not node.is_running:
                    logger.debug(f"NewMessage handler: msg {message.id} from chat {chat_id} matches listen_forward but node not running")
                    return
                try:
                    if not node.has_protected_content:
                        await forward_normal_content(client, node, message)
                        from module.pyrogram_extension import report_bot_status
                        await report_bot_status(client, node, immediate_reply=True)
                    else:
                        await self.add_download_task(message, node)
                except Exception as e:
                    logger.exception(f"Listen handler error for chat {chat_id}: {e}")
                return

            # Check whether the chat is configured in config.yaml.
            if self.app and chat_id in self.app.chat_download_config:
                chat_config = self.app.chat_download_config[chat_id]
                # Only handle messages newer than last_read_message_id.
                if message.id <= chat_config.last_read_message_id:
                    logger.debug(f"NewMessage handler: msg {message.id} from config chat {chat_id} <= last_read {chat_config.last_read_message_id}, skipping")
                    return

                # Create a temporary TaskNode for this message.
                try:
                    from module.app import TaskNode
                    node = TaskNode(
                        chat_id=chat_id,
                        from_user_id=0,
                        limit=1,
                        bot=self.bot,
                        download_filter=chat_config.download_filter,
                    )
                    node.task_id_display = f"config-{chat_id}-{message.id}"

                    # Live listeners do not advance last_read_message_id; download_chat_task
                    # owns scan progress. Restart scans can then recover failed live downloads,
                    # while _is_exist prevents duplicates.

                    # Add to the download queue.
                    await self.add_download_task(message, node)
                    logger.info(f"Config chat {chat_id}: new message {message.id} queued for download")
                except Exception as e:
                    logger.exception(f"Config chat handler error for chat {chat_id}: {e}")
                return

            # The message belongs to no configured channel.
            logger.debug(f"NewMessage handler: msg {message.id} from chat {chat_id} not in any configured channel (listen_forward={list(self.listen_forward_chat.keys())}, config_chats={list(self.app.chat_download_config.keys()) if self.app else []})")

        handler = MessageHandler(
            _on_new_message,
            filters=pyrogram.filters.media  # Only handle media messages.
        )
        self.client.add_handler(handler)
        self._listen_handler_ref = handler
        logger.info("Listen handler registered on user client for real-time monitoring")

    async def start(
        self,
        app: Application,
        client: pyrogram.Client,
        add_download_task: Callable,
        download_chat_task: Callable,
    ):
        """Start bot"""
        self.bot = pyrogram.Client(
            app.application_name + "_bot",
            api_hash=app.api_hash,
            api_id=app.api_id,
            bot_token=app.bot_token,
            workdir=app.session_file_path,
            proxy=app.proxy or None,
        )

        # Command list
        commands = [
            types.BotCommand("help", _t("Help")),
            types.BotCommand("query", _t("Query download tasks")),
            types.BotCommand(
                "get_info", _t("Get group and user info from message link")
            ),
            types.BotCommand(
                "download",
                _t(
                    "To download the video, use the method to directly enter /download to view"
                ),
            ),
            types.BotCommand(
                "forward",
                _t("Forward video, use the method to directly enter /forward to view"),
            ),
            types.BotCommand(
                "listen_forward",
                _t(
                    "Listen forward, use the method to directly enter /listen_forward to view"
                ),
            ),
            types.BotCommand("add_filter", _t("Add download filter, use the method to directly enter /add_filter to view")),
            types.BotCommand("add_ad", _t("Add filter advertisement filter")),
            types.BotCommand("remove_ad", _t("Remove filter advertisement filter")),
            types.BotCommand("add_replace_ad", _t("Add replace advertisement filter")),
            types.BotCommand("remove_replace_ad", _t("Remove replace advertisement filter")),
            types.BotCommand("set_ad", _t("Set add advertisement")),
            types.BotCommand("set_language", _t("Set language")),
            types.BotCommand("stop", _t("Stop bot download or forward")),
        ]

        self.app = app
        self.client = client
        self.add_download_task = add_download_task
        self.download_chat_task = download_chat_task
        self.is_running = True
        set_max_concurrent_transmissions(
            self.bot, getattr(app, "max_concurrent_transmissions", 1)
        )

        # Normalize persisted states and reserve existing IDs before registering
        # handlers or starting the consumer. New commands cannot overwrite them.
        await self.recover_tasks()

        # load config
        if os.path.exists(self.config_path):
            with open(self.config_path, encoding="utf-8") as f:
                config = self._yaml.load(f.read())
                if config:
                    self.config = config
                    self.assign_config(self.config)

        await self.bot.start()

        self.bot_info = await self.bot.get_me()

        for allowed_user_id in self.app.allowed_user_ids:
            # Numeric allowlist entries identify senders directly; a Bot-owned
            # main account may not have met that user and cannot resolve it yet.
            if isinstance(allowed_user_id, int) or (
                isinstance(allowed_user_id, str) and allowed_user_id.lstrip("-").isdigit()
            ):
                self.allowed_user_ids.append(int(allowed_user_id))
                continue
            try:
                chat = await self.client.get_chat(allowed_user_id)
                self.allowed_user_ids.append(chat.id)
            except Exception as e:
                logger.warning(f"set allowed_user_ids error: {e}")

        admin = await self.client.get_me()
        if not getattr(admin, "is_bot", False):
            self.allowed_user_ids.append(admin.id)

        await self.bot.set_bot_commands(commands)

        self._register_bot_handlers()

        if not getattr(admin, "is_bot", False):
            try:
                await send_help_str(self.bot, admin.id)
            except Exception as e:
                logger.warning(f"Failed to send help message: {e}")

        self.reply_task = self.app.loop.create_task(self.update_reply_message())
        self._ensure_pending_consumer()

        self._ensure_heartbeat()

        # Register channel listeners automatically, including chats configured in config.yaml.
        if self.app.chat_download_config:
            self._register_listen_handler()
            logger.info("Auto-registered listen handler for config chats")

    def _register_bot_handlers(self):
        """Register all bot MessageHandlers and CallbackQueryHandler.

        Called from start() and _reconnect_bot() — stop() clears handlers,
        so they must be re-registered after every reconnect.
        """
        def add_handler(handler):
            handler.callback = self._logged_bot_handler(handler.callback)
            self.bot.add_handler(handler)

        self.bot.add_handler(MessageHandler(self._observe_bot_message), group=-1)
        add_handler(
            MessageHandler(
                query_tasks,
                filters=pyrogram.filters.command(["query"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                download_from_bot,
                filters=pyrogram.filters.command(["download"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                forward_messages,
                filters=pyrogram.filters.command(["forward"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                download_forward_media,
                filters=pyrogram.filters.media
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                download_from_link,
                filters=pyrogram.filters.regex(r"^https://t.me.*")
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                set_listen_forward_msg,
                filters=pyrogram.filters.command(["listen_forward"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                help_command,
                filters=pyrogram.filters.command(["help"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                get_info,
                filters=pyrogram.filters.command(["get_info"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                help_command,
                filters=pyrogram.filters.command(["start"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                set_language,
                filters=pyrogram.filters.command(["set_language"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                add_filter,
                filters=pyrogram.filters.command(["add_filter"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )

        add_handler(
            MessageHandler(
                add_filter_advertisement_filter,
                filters=pyrogram.filters.command(["add_ad"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                remove_filter_advertisement_filter,
                filters=pyrogram.filters.command(["remove_ad"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                add_replace_advertisement_filter,
                filters=pyrogram.filters.command(["add_replace_ad"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                remove_replace_advertisement_filter,
                filters=pyrogram.filters.command(["remove_replace_ad"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        add_handler(
            MessageHandler(
                set_add_advertisement,
                filters=pyrogram.filters.command(["set_ad"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )

        add_handler(
            MessageHandler(
                stop,
                filters=pyrogram.filters.command(["stop"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )

        add_handler(
            CallbackQueryHandler(
                on_query_handler, filters=pyrogram.filters.user(self.allowed_user_ids)
            )
        )

        # forward_to_comments handler (registered after reply_task in original start)
        add_handler(
            MessageHandler(
                forward_to_comments,
                filters=pyrogram.filters.command(["forward_to_comments"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )

    async def _reconnect_bot(self):
        """Restart the Bot client after persistent connection loss.

        Reuse the existing session without authentication. Return whether reconnect
        succeeded. Bound stop/start operations so half-dead TCP cannot permanently
        block update_reply_message and all subsequent progress reports."""
        try:
            logger.warning("Attempting bot reconnect: stop()...")
            try:
                await asyncio.wait_for(self.bot.stop(), timeout=30)
            except asyncio.TimeoutError:
                logger.error("bot.stop() timed out after 30s, force disconnect")
                try:
                    await asyncio.wait_for(self.bot.disconnect(), timeout=10)
                except Exception:
                    pass
                self.bot.is_connected = False
            except Exception as e:
                logger.warning(f"bot.stop() during reconnect failed (continuing): {e}")
                self.bot.is_connected = False
        except Exception as e:
            logger.warning(f"bot.stop() outer error (continuing): {e}")
            self.bot.is_connected = False

        try:
            logger.warning("Attempting bot reconnect: start()...")
            try:
                await asyncio.wait_for(self.bot.start(), timeout=30)
            except asyncio.TimeoutError:
                logger.error("bot.start() timed out after 30s, reconnect failed")
                return False
            self._register_bot_handlers()
            logger.success("Bot reconnected successfully — handlers re-registered")
            return True
        except Exception as e:
            logger.error(f"bot.start() during reconnect failed: {e}")
            return False


# Track consecutive Pending-consumer timeouts for retry backoff.
_pending_timeout_counts: dict = {}

_bot = DownloadBot()


def _cleanup_task_temp_files(chat_id: int):
    """Remove temp files for a specific chat from temp directory."""
    temp_dir = os.path.join(os.path.abspath("."), "temp")
    chat_dir = os.path.join(temp_dir, str(chat_id))
    if not os.path.isdir(chat_dir):
        return
    removed = 0
    for f in os.listdir(chat_dir):
        if f.endswith('.temp'):
            try:
                os.remove(os.path.join(chat_dir, f))
                removed += 1
            except OSError:
                pass
    # Remove directory if empty
    try:
        if not os.listdir(chat_dir):
            os.rmdir(chat_dir)
    except OSError:
        pass
    if removed:
        logger.info(f"Cleanup: removed {removed} stale temp files for chat {chat_id}")


async def start_download_bot(
    app: Application,
    client: pyrogram.Client,
    add_download_task: Callable,
    download_chat_task: Callable,
):
    """Start download bot"""
    await _bot.start(app, client, add_download_task, download_chat_task)


async def start_download_backend(app, client, add_download_task, download_chat_task):
    """Keep the existing queue/recovery backend alive for MCP without a Bot session."""
    if (_bot.app is app and _bot._pending_consumer_task is not None
            and not _bot._pending_consumer_task.done()):
        return
    _bot.app = app
    _bot.client = client
    _bot.bot = None
    _bot.add_download_task = add_download_task
    _bot.download_chat_task = download_chat_task
    _bot.is_running = True
    await _bot.recover_tasks()
    if _bot.reply_task is None or _bot.reply_task.done():
        _bot.reply_task = app.loop.create_task(_bot.update_reply_message())
    _bot._ensure_pending_consumer()
    logger.info("Download backend started for MCP without a Telegram Bot session")


async def stop_download_bot():
    """Stop download bot"""
    if _bot.bot:
        _bot.update_config()
    _bot.is_running = False
    _bot.stop_task("all", discard_partial=False)
    background_tasks = [
        task for task in [_bot.reply_task, _bot.heartbeat_task, _bot._pending_consumer_task,
                          *_bot._range_tasks.values()] if task is not None
    ]
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    _bot._pending_consumer_task = None
    _bot.heartbeat_task = None
    if _bot.bot:
        await _bot.bot.stop()


async def send_help_str(client: pyrogram.Client, chat_id):
    """
    Sends a help string to the specified chat ID using the provided client.

    Parameters:
        client (pyrogram.Client): The Pyrogram client used to send the message.
        chat_id: The ID of the chat to which the message will be sent.

    Returns:
        str: The help string that was sent.

    Note:
        The help string includes information about the TG Porter bot,
        its version, and the available commands.
    """

    update_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Github",
                    url="https://github.com/smilekevin/TGPorter/releases",
                ),
                InlineKeyboardButton(
                    "Join us", url="https://t.me/tg_porter"
                ),
            ]
        ]
    )
    latest_release_str = ""
    # try:
    #     latest_release = get_latest_release(_bot.app.proxy)

    #     latest_release_str = (
    #         f"{_t('New Version')}: [{latest_release['name']}]({latest_release['html_url']})\an"
    #         if latest_release
    #         else ""
    #     )
    # except Exception:
    #     latest_release_str = ""

    msg = (
            f"`\n🤖 {_t('TG Porter')}\n"
            f"🌐 {_t('Version')}: {utils.__version__}`\n"
            f"{latest_release_str}\n"
            f"{_t('Available commands:')}\n"
            f"/help - {_t('Show help')}\n"
            f"/start - {_t('Show help')}\n"
            f"/query [all|active|pending|completed|failed] [page] - {_t('Query download tasks')}\n"
            f"/get_info <link> - {_t('Get group/channel info from a message link')}\n"
            f"/download <link> <start_id> <end_id> [filter] - {_t('Download messages in bulk')}\n"
            f"/forward <src_link> <dst_link> <start_id> <end_id> [filter] - {_t('Forward messages to the destination group')}\n"
            f"/forward_to_comments <src_link> <dst_link> <start_id> <end_id> - {_t('Forward media to comments')}\n"
            f"/listen_forward <src_link> <dst_link> [filter] - {_t('Watch and automatically forward new messages')}\n"
            f"/stop - {_t('Stop downloads, forwarding or listening (choose with buttons)')}\n"
            f"/set_language {'|'.join(CATALOGS)} - {_t('Set language')}\n"
            f"/add_filter <filter> - {_t('Set download filter')}\n\n"
            f"{_t('**Quick actions (no command needed):**')}\n"
            f"• {_t('Send a Telegram post link')} - {_t('Directly download a single message')}\n"
            f"• {_t('Forward a media message to the bot')} - {_t('Download or re-upload this media')}\n\n"
            f"{_t('**Notes**')}\n"
            f"• `[option]` {_t('optional parameter, not required')}\n"
            f"• {_t('start_id=1 starts at the beginning; end_id=0 continues to the end')}\n"
        )

    await client.send_message(chat_id, msg, reply_markup=update_keyboard)


async def help_command(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Sends a message with the available commands and their usage.

    Parameters:
        client (pyrogram.Client): The client instance.
        message (pyrogram.types.Message): The message object.

    Returns:
        None
    """

    await send_help_str(client, message.chat.id)


async def set_language(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Set the language of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    if len(message.text.split()) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid language. Use /set_language {languages}", languages="/".join(CATALOGS)),
        )
        return

    language = message.text.split()[1]

    try:
        language = parse_language(language)
        _bot.app.set_language(language)
        await client.send_message(
            message.from_user.id, f"{_t('Language set to')} {CATALOGS[language.value]['name']}"
        )
    except ValueError:
        await client.send_message(
            message.from_user.id,
            _t("Invalid language. Use /set_language {languages}", languages="/".join(CATALOGS)),
        )


async def get_info(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Async function that retrieves information from a group message link.
    """

    msg = _t("Invalid command format. Please use /get_info group_message_link")

    args = message.text.split()
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            msg,
        )
        return

    chat_id, message_id, _ = await parse_link(_bot.client, args[1])

    entity = None
    if chat_id:
        entity = await _bot.client.get_chat(chat_id)

    if entity:
        if message_id:
            _message = await retry(_bot.client.get_messages, args=(chat_id, message_id))
            if _message:
                meta_data = MetaData()
                set_meta_data(meta_data, _message)
                msg = (
                    f"`\n"
                    f"{_t('Group/Channel')}\n"
                    f"├─ {_t('id')}: {entity.id}\n"
                    f"├─ {_t('first name')}: {entity.first_name}\n"
                    f"├─ {_t('last name')}: {entity.last_name}\n"
                    f"└─ {_t('name')}: {entity.username}\n"
                    f"{_t('Message')}\n"
                )

                for key, value in meta_data.data().items():
                    if key == "send_name":
                        msg += f"└─ {key}: {value or None}\n"
                    else:
                        msg += f"├─ {key}: {value or None}\n"

                msg += "`"
    await client.send_message(
        message.from_user.id,
        msg,
    )


async def add_filter(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /add_filter your filter"),
        )
        return

    filter_str = replace_date_time(args[1])
    res, err = _bot.filter.check_filter(filter_str)
    if res:
        _bot.app.down = args[1]
        await client.send_message(
            message.from_user.id, f"{_t('Add download filter')} : {args[1]}"
        )
    else:
        await client.send_message(
            message.from_user.id, f"{err}\n{_t('Check error, please add again')}"
        )
    return


async def add_filter_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /add_ad filter"),
        )
        return

    filter_str = args[1]

    if filter_str in _bot.app.filter_advertisement_list:
        await client.send_message(message.from_user.id, _t("Filter already exists: {filter}", filter=filter_str))
        return

    _bot.app.filter_advertisement_list.append(filter_str)
    await client.send_message(message.from_user.id, f"{_t('Add filter')}: {filter_str}")
    _bot.app.save_app_data(True)


async def remove_filter_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Add or remove advertisement filter
    """

    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /remove_ad filter"),
        )
        return

    filter_str = args[1]
    if filter_str in _bot.app.filter_advertisement_list:
        _bot.app.filter_advertisement_list.remove(filter_str)
        await client.send_message(
            message.from_user.id, f"{_t('Remove filter')} : {args[1]}"
        )

        _bot.app.save_app_data(True)
    else:
        await client.send_message(
            message.from_user.id, f"{_t('Filter')} : {args[1]} {_t('not exist')}"
        )


async def set_add_advertisement(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Add or remove advertisement filter
    """

    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /set_ad mesage_link advertisement"),
        )
        return

    mesage_link = args[1]
    advertisement_str = None if len(args) < 3 else args[2]

    try:
        chat_id, _, _ = await parse_link(_bot.client, mesage_link)
        if advertisement_str:
            _bot.app.group_add_advertisement[chat_id] = advertisement_str
            _bot.app.save_app_data(True)
            await client.send_message(
                message.from_user.id, _t("Set advertisement: {advertisement}", advertisement=advertisement_str)
            )
        else:
            if chat_id in _bot.app.group_add_advertisement:
                del _bot.app.group_add_advertisement[chat_id]
                _bot.app.save_app_data(True)
                await client.send_message(
                    message.from_user.id, _t("Removed advertisement for chat {chat}", chat=chat_id)
                )
            else:
                await client.send_message(
                    message.from_user.id, _t("No advertisement set for chat {chat}", chat=chat_id)
                )
    except Exception as e:
        await client.send_message(
            message.from_user.id, f"{_t('Parse link error')}: {e}"
        )
        return


class MessageProcessor:
    """Helper class for processing message captions and entities."""

    def __init__(self, raw_message, filter_str):
        self.raw_message = raw_message
        self.raw_caption = raw_message.caption
        self.filter_str = filter_str
        self.raw_filter_str = pyrogram.parser.utils.add_surrogates(filter_str)
        self.raw_caption_str = pyrogram.parser.utils.add_surrogates(raw_message.caption)
        self.idx = self.raw_caption_str.find(self.raw_filter_str)
        self.start_offset = self.idx
        self.end_offset = self.idx + get_utf16_length(filter_str)
        self.filtered_entities = []

    # pylint: disable = R0916
    def process_entities(self):
        """Process and filter message entities."""
        for entity in self.raw_message.caption_entities:
            cur_start_offset = entity.offset
            cur_end_offset = entity.offset + entity.length

            # Check if entity should be included
            if (
                (
                    cur_start_offset >= self.start_offset
                    and cur_end_offset <= self.end_offset
                )
                or (
                    cur_start_offset < self.start_offset
                    and cur_end_offset > self.start_offset
                )
                or (
                    cur_start_offset < self.end_offset
                    and cur_end_offset > self.end_offset
                )
            ):
                self.filtered_entities.append(entity)

        self.filtered_entities.sort(key=lambda x: x.offset)

    def get_total_span(self):
        """Calculate the total span for text extraction."""
        if self.filtered_entities:
            first_entity = self.filtered_entities[0]
            last_entity = self.filtered_entities[-1]
            return (
                min(self.start_offset, first_entity.offset),
                max(self.end_offset, last_entity.offset + last_entity.length),
            )
        return (self.start_offset, self.end_offset)

    def extract_text(self, total_span):
        """Extract and process text with adjusted entity offsets."""
        text = self.raw_caption[total_span[0] : total_span[1]]
        for entity in self.filtered_entities:
            entity.offset -= total_span[0]
        return pyrogram.parser.Parser.unparse(text, self.filtered_entities, True)


async def proc_replace_advertisement(mesage_link: str, filter_str: str):
    """
    Process and replace advertisement content in a message.

    This function takes a message link and a filter string, retrieves the message,
    and processes its caption by handling entities and filtering advertisement content.
    It preserves the formatting and entities while replacing the specified filter text.

    Args:
        mesage_link (str): The link to the Telegram message
        filter_str (str): The string to filter/replace in the message caption

    Returns:
        str: The processed caption with preserved formatting and entities

    Raises:
        Exception: If there are issues parsing the message link or accessing the message
    """
    chat_id, message_id, _ = await parse_link(_bot.client, mesage_link)
    raw_message = await retry(_bot.client.get_messages, args=(chat_id, message_id))

    processor = MessageProcessor(raw_message, filter_str)
    processor.process_entities()
    total_span = processor.get_total_span()
    return processor.extract_text(total_span)


async def add_replace_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=2)
    if len(args) != 3:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /add_replace_ad your filter"),
        )
        return

    mesage_link = args[1]
    filter_str = args[2]

    try:
        filter_str = await proc_replace_advertisement(mesage_link, filter_str)
        _bot.app.replace_advertisement_list.append(filter_str)
        _bot.app.save_app_data(True)
        await client.send_message(
            message.from_user.id, f"{_t('Add filter')} : {filter_str}"
        )
    except Exception as e:
        await client.send_message(
            message.from_user.id, f"{_t('Add filter')} : {filter_str}\n{e}"
        )
        return


async def remove_replace_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=2)
    if len(args) != 3:
        await client.send_message(
            message.from_user.id,
            _t(
                "Invalid command format. Please use /remove_replace_ad mesage_link advertisement_filter"
            ),
        )
        return

    mesage_link = args[1]
    filter_str = args[2]

    try:
        filter_str = await proc_replace_advertisement(mesage_link, filter_str)

        if filter_str in _bot.app.replace_advertisement_list:
            _bot.app.replace_advertisement_list.remove(filter_str)
            await client.send_message(
                message.from_user.id, f"{_t('Remove filter')} : {filter_str}"
            )
        else:
            await client.send_message(
                message.from_user.id,
                f"{_t('Filter not found')}: {filter_str}",
            )
        _bot.app.save_app_data(True)
    except Exception as e:
        await client.send_message(
            message.from_user.id, f"{_t('Add filter')} : {filter_str}\n{e}"
        )
        return


async def direct_download(
    download_bot: DownloadBot,
    chat_id: Union[str, int],
    message: pyrogram.types.Message,
    download_message: pyrogram.types.Message,
    client: pyrogram.Client = None,
    source_chat_id: int = 0,
    source_message_id: int = 0,
    source_chat_title: str = "",
):
    """Direct Download

    Args:
        source_chat_id: Original source channel ID (for forwarded messages recovery)
        source_message_id: Original source message ID (for forwarded messages recovery)
        source_chat_title: Source channel title (for web display)
    """

    replay_message = _t("Direct download...")
    last_reply_message = await download_bot.bot.send_message(
        message.from_user.id, replay_message, reply_parameters=types.ReplyParameters(message_id=message.id)
    )

    node = TaskNode(
        chat_id=chat_id,
        from_user_id=message.from_user.id,
        reply_message_id=last_reply_message.id,
        replay_message=replay_message,
        limit=1,
        bot=download_bot.bot,
        task_id=_bot.gen_task_id(),
    )

    # The consumer selects the message-owning account before queueing. Bot
    # inbox media must be downloaded with the Bot's account/file references.
    node.source_chat_title = source_chat_title
    node.source_chat_id = source_chat_id
    node.source_message_id = source_message_id

    _bot.add_task_node(node)

    extra_data = {
        "message_id": download_message.id,
        "task_id_display": node.task_id_display,
    }
    owner = getattr(download_message, "_client", None) or download_bot.get_message_client(
        chat_id, message.from_user.id
    )
    extra_data["message_client"] = "bot" if owner is download_bot.bot else "user"
    if source_chat_id and source_message_id:
        extra_data["source_chat_id"] = source_chat_id
        extra_data["source_message_id"] = source_message_id
    if source_chat_title:
        extra_data["source_chat_title"] = source_chat_title

    save_task(
        task_id=node.task_id,
        chat_id=chat_id,
        url="",
        start_offset_id=0,
        end_offset_id=0,
        limit=1,
        download_filter=None,
        from_user_id=message.from_user.id,
        task_type="direct",
        extra_data=extra_data,
    )

    # Cache the message so consumer can skip get_messages.
    # Do NOT queue directly — consumer controls when to feed workers.
    _bot._cached_messages[node.task_id] = download_message
    node.is_running = True

    # Do NOT trigger _consume_one_pending directly — let the periodic
    # _pending_consumer_loop (every 2s) pick it up. This ensures the
    # concurrency guard is always respected.


async def download_forward_media(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Downloads the media from a forwarded message.

    Parameters:
        client (pyrogram.Client): The client instance.
        message (pyrogram.types.Message): The message object.

    Returns:
        None
    """

    if message.media and getattr(message, message.media.value):
        origin = message.forward_origin
        source_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        # If forwarded from a channel/group, download from source
        if source_chat:
            source_chat_id = source_chat.id
            source_message_id = getattr(origin, "message_id", None) or 0
            source_chat_title = source_chat.title or ""

            download_message = None
            if source_message_id:
                source_msg = await retry(
                    _bot.client.get_messages,
                    args=(source_chat_id, source_message_id),
                )
                if source_msg and source_msg.media:
                    await direct_download(
                        _bot, source_chat_id, message, source_msg, client,
                        source_chat_id=source_chat_id,
                        source_message_id=source_message_id,
                        source_chat_title=source_chat_title,
                    )
                    return
                # Source message deleted or no media — try link-based fetch
                # The forwarded message's file reference may be stale. Construct
                # a link and use parse_link → get_messages to get a fresh copy.
                logger.info(
                    f"Forward: source msg {source_message_id} has no media, "
                    f"trying link-based fetch"
                )
                try:
                    from module.pyrogram_extension import parse_link
                    # Build source link: private channels use /c/, public use /username/
                    fwd_chat = source_chat
                    if fwd_chat.username:
                        link_str = f"https://t.me/{fwd_chat.username}/{source_message_id}"
                    else:
                        link_id = str(source_chat_id)
                        if link_id.startswith("-100"):
                            link_id = link_id[4:]
                        link_str = f"https://t.me/c/{link_id}/{source_message_id}"
                    link_chat_id, link_msg_id, _ = await parse_link(_bot.client, link_str)
                    if link_chat_id and link_msg_id:
                        download_message = await retry(
                            _bot.client.get_messages,
                            args=(link_chat_id, link_msg_id),
                        )
                        if download_message and download_message.media:
                            logger.info(
                                f"Forward: link-based fetch succeeded for "
                                f"{link_chat_id}/{link_msg_id}"
                            )
                            await direct_download(
                                _bot, link_chat_id, message, download_message, client,
                                source_chat_id=source_chat_id,
                                source_message_id=source_message_id,
                                source_chat_title=source_chat_title,
                            )
                            return
                except Exception as e:
                    logger.warning(f"Forward: link-based fetch failed: {e}")

            # All fallbacks failed — fall through to direct download with the
            # original forwarded message (last resort)
            await direct_download(_bot, message.from_user.id, message, message, client)
            return

        # Direct upload or forward from user (no source channel info)
        await direct_download(_bot, message.from_user.id, message, message, client)
        return

    await client.send_message(
        message.from_user.id,
        f"1. {_t('Direct download, directly forward the message to your robot')}\n\n",
        parse_mode=pyrogram.enums.ParseMode.HTML,
    )


async def download_from_link(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Downloads a single message from a Telegram link.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the Telegram link.

    Returns:
        None
    """

    if not message.text or not message.text.startswith("https://t.me"):
        return

    msg = (
        f"1. {_t('Directly download a single message')}\n"
        "<i>https://t.me/12000000/1</i>\n\n"
    )

    text = message.text.split()
    if len(text) != 1:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )

    chat_id, message_id, _ = await parse_link(_bot.client, text[0])

    entity = None
    if chat_id:
        entity = await _bot.client.get_chat(chat_id)
    if entity:
        title = entity.title or entity.first_name or str(entity.id)
        set_chat_title(entity.id, title)
        if message_id:
            download_message = await retry(
                _bot.client.get_messages, args=(chat_id, message_id)
            )
            if download_message:
                await direct_download(_bot, entity.id, message, download_message)
            else:
                await client.send_message(
                    message.from_user.id,
                    f"{_t('From')} {entity.title} {_t('download')} {message_id} {_t('error')}!",
                    reply_parameters=types.ReplyParameters(message_id=message.id),
                )
        return

    await client.send_message(
        message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
    )


# pylint: disable = R0912, R0915,R0914


async def download_from_bot(client: pyrogram.Client, message: pyrogram.types.Message):
    """Download from bot"""

    msg = (
        f"{_t('Parameter error, please enter according to the reference format')}:\n\n"
        f"1. {_t('Download all messages of common group')}\n"
        "<i>/download https://t.me/fkdhlg 1 0</i>\n\n"
        f"{_t('The private group (channel) link is a random group message link')}\n\n"
        f"2. {_t('The download starts from the N message to the end of the M message')}. "
        f"{_t('When M is 0, it means the last message. The filter is optional')}\n"
        f"<i>/download https://t.me/12000000 N M [filter]</i>\n\n"
    )

    args = message.text.split(maxsplit=4)
    if not message.text or len(args) < 4:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )
        return

    url = args[1]
    try:
        start_offset_id = int(args[2])
        end_offset_id = int(args[3])
    except Exception:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )
        return

    limit = 0
    if end_offset_id:
        if end_offset_id < start_offset_id:
            raise ValueError(
                f"end_offset_id < start_offset_id, {end_offset_id} < {start_offset_id}"
            )

        limit = end_offset_id - start_offset_id + 1

    download_filter = args[4] if len(args) > 4 else None

    if download_filter:
        download_filter = replace_date_time(download_filter)
        res, err = _bot.filter.check_filter(download_filter)
        if not res:
            await client.send_message(
                message.from_user.id, err, reply_parameters=types.ReplyParameters(message_id=message.id)
            )
            return
    entity = None
    try:
        chat_id, _, _ = await parse_link(_bot.client, url)
        if chat_id:
            entity = await _bot.client.get_chat(chat_id)
        if entity:
            chat_title = entity.title or entity.first_name or str(entity.id)
            set_chat_title(entity.id, chat_title)
            chat_download_config = ChatDownloadConfig()
            chat_download_config.last_read_message_id = start_offset_id
            chat_download_config.download_filter = download_filter
            reply_message = _t("Download messages {start}–{end} from {chat}",
                               start=start_offset_id, end=end_offset_id, chat=chat_title)
            last_reply_message = await client.send_message(
                message.from_user.id, reply_message, reply_parameters=types.ReplyParameters(message_id=message.id)
            )
            node = TaskNode(
                chat_id=entity.id,
                from_user_id=message.from_user.id,
                reply_message_id=last_reply_message.id,
                replay_message=reply_message,
                limit=limit,
                start_offset_id=start_offset_id,
                end_offset_id=end_offset_id,
                bot=_bot.bot,
                task_id=_bot.gen_task_id(),
            )
            _bot.add_task_node(node)
            save_task(
                task_id=node.task_id,
                chat_id=entity.id,
                url=url,
                start_offset_id=start_offset_id,
                end_offset_id=end_offset_id,
                limit=limit,
                download_filter=download_filter,
                from_user_id=message.from_user.id,
                task_type="download",
                extra_data={"task_id_display": node.task_id_display},
            )
            _bot.app.loop.create_task(
                _bot.download_chat_task(_bot.client, chat_download_config, node)
            )
    except Exception as e:
        await client.send_message(
            message.from_user.id,
            f"{_t('chat input error, please enter the channel or group link')}\n\n"
            f"{_t('Error type')}: {e.__class__}"
            f"{_t('Exception message')}: {e}",
        )
        return


async def get_forward_task_node(
    client: pyrogram.Client,
    message: pyrogram.types.Message,
    task_type: TaskType,
    src_chat_link: str,
    dst_chat_link: str,
    offset_id: int = 0,
    end_offset_id: int = 0,
    download_filter: str = None,
    reply_comment: bool = False,
):
    """Get task node"""
    limit: int = 0

    if end_offset_id:
        if end_offset_id < offset_id:
            await client.send_message(
                message.from_user.id,
                f" end_offset_id({end_offset_id}) < start_offset_id({offset_id}),"
                f" end_offset_id{_t('must be greater than')} offset_id",
            )
            return None

        limit = end_offset_id - offset_id + 1

    src_chat_id, _, _ = await parse_link(_bot.client, src_chat_link)
    dst_chat_id, target_msg_id, topic_id = await parse_link(_bot.client, dst_chat_link)

    if not src_chat_id or not dst_chat_id:
        logger.info(f"{src_chat_id} {dst_chat_id}")
        await client.send_message(
            message.from_user.id,
            _t("Invalid chat link") + f"{src_chat_id} {dst_chat_id}",
            reply_parameters=types.ReplyParameters(message_id=message.id),
        )
        return None

    try:
        src_chat = await _bot.client.get_chat(src_chat_id)
        dst_chat = await _bot.client.get_chat(dst_chat_id)
    except Exception as e:
        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid chat link')} {e}",
            reply_parameters=types.ReplyParameters(message_id=message.id),
        )
        logger.exception(f"get chat error: {e}")
        return None

    me = await client.get_me()
    if dst_chat.id == me.id:
        # TODO: when bot receive message judge if download
        await client.send_message(
            message.from_user.id,
            _t("Cannot be forwarded to this bot, will cause an infinite loop"),
            reply_parameters=types.ReplyParameters(message_id=message.id),
        )
        return None

    if download_filter:
        download_filter = replace_date_time(download_filter)
        res, err = _bot.filter.check_filter(download_filter)
        if not res:
            await client.send_message(
                message.from_user.id, err, reply_parameters=types.ReplyParameters(message_id=message.id)
            )

    last_reply_message = await client.send_message(
        message.from_user.id,
        _t("Forwarding message, please wait..."),
        reply_parameters=types.ReplyParameters(message_id=message.id),
    )

    node = TaskNode(
        chat_id=src_chat.id,
        from_user_id=message.from_user.id,
        upload_telegram_chat_id=dst_chat_id,
        reply_message_id=last_reply_message.id,
        replay_message=last_reply_message.text,
        has_protected_content=src_chat.has_protected_content,
        download_filter=download_filter,
        limit=limit,
        start_offset_id=offset_id,
        end_offset_id=end_offset_id,
        bot=_bot.bot,
        task_id=_bot.gen_task_id(),
        task_type=task_type,
        topic_id=topic_id,
    )

    if target_msg_id and reply_comment:
        node.reply_to_message = await _bot.client.get_discussion_message(
            dst_chat_id, target_msg_id
        )

    _bot.add_task_node(node)

    node.upload_user = _bot.client
    if not dst_chat.type is pyrogram.enums.ChatType.BOT:
        has_permission = await check_user_permission(_bot.client, me.id, dst_chat.id)
        if has_permission:
            node.upload_user = _bot.bot

    if node.upload_user is _bot.client:
        await client.edit_message_text(
            message.from_user.id,
            last_reply_message.id,
            "Note that the robot may not be in the target group,"
            " use the user account to forward",
        )

    return node


# pylint: disable = R0914
async def forward_message_impl(
    client: pyrogram.Client, message: pyrogram.types.Message, reply_comment: bool
):
    """
    Forward message
    """

    async def report_error(client: pyrogram.Client, message: pyrogram.types.Message):
        """Report error"""

        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid command format')}."
            f"{_t('Please use')} "
            "/forward https://t.me/c/src_chat https://t.me/c/dst_chat "
            f"1 400 `[`{_t('Filter')}`]`\n",
        )

    args = message.text.split(maxsplit=5)
    if len(args) < 5:
        await report_error(client, message)
        return

    src_chat_link = args[1]
    dst_chat_link = args[2]

    try:
        offset_id = int(args[3])
        end_offset_id = int(args[4])
    except Exception:
        await report_error(client, message)
        return

    download_filter = args[5] if len(args) > 5 else None

    node = await get_forward_task_node(
        client,
        message,
        TaskType.Forward,
        src_chat_link,
        dst_chat_link,
        offset_id,
        end_offset_id,
        download_filter,
        reply_comment,
    )

    if not node:
        return

    # Persist forward task for crash recovery
    save_task(
        task_id=node.task_id,
        chat_id=node.chat_id,
        url=src_chat_link,
        start_offset_id=offset_id,
        end_offset_id=end_offset_id,
        limit=node.limit,
        download_filter=download_filter,
        from_user_id=message.from_user.id,
        task_type="forward",
        extra_data={"dst_chat_id": node.upload_telegram_chat_id, "dst_chat_link": dst_chat_link, "task_id_display": node.task_id_display, "topic_id": node.topic_id},
    )

    if not node.has_protected_content:
        forward_failed = False
        try:
            async for item in get_chat_history_v2(  # type: ignore
                _bot.client,
                node.chat_id,
                limit=node.limit,
                max_id=node.end_offset_id,
                offset_id=offset_id,
                reverse=True,
            ):
                await forward_normal_content(client, node, item)
                # Update progress for crash recovery
                update_task_progress(node.task_id, item.id)
                if node.is_stop_transmission:
                    await client.edit_message_text(
                        message.from_user.id,
                        node.reply_message_id,
                        f"{_t('Stop Forward')}",
                    )
                    break
        except Exception as e:
            forward_failed = True
            await client.edit_message_text(
                message.from_user.id,
                node.reply_message_id,
                f"{_t('Error forwarding message')} {e}",
            )
        finally:
            await report_bot_status(client, node, immediate_reply=True)
            # Only mark as stopped if forward threw an exception
            if forward_failed:
                node.stop_transmission()
            complete_task(node.task_id)
    else:
        await forward_msg(node, offset_id)
        complete_task(node.task_id)


async def forward_messages(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Forwards messages from one chat to another.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """
    return await forward_message_impl(client, message, False)


async def forward_normal_content(
    client: pyrogram.Client, node: TaskNode, message: pyrogram.types.Message
):
    """Forward normal content"""
    forward_ret = ForwardStatus.FailedForward
    caption = message.caption
    if caption:
        caption = validate_title(caption)
        _bot.app.set_caption_name(node.chat_id, message.media_group_id, caption)
    else:
        caption = _bot.app.get_caption_name(node.chat_id, message.media_group_id)

    if caption and _bot.app.is_match_advertisement(caption):
        forward_ret = ForwardStatus.SkipForward
        if message.media_group_id:
            # TODO
            node.upload_status[message.id] = UploadStatus.SkipUpload
        return

    if node.download_filter:
        meta_data = MetaData()
        set_meta_data(meta_data, message, caption)
        _bot.filter.set_meta_data(meta_data)
        if not _bot.filter.exec(node.download_filter):
            forward_ret = ForwardStatus.SkipForward
            if message.media_group_id:
                node.upload_status[message.id] = UploadStatus.SkipUpload
                await proc_cache_forward(_bot.client, node, message, False, _bot.app)
            await report_bot_forward_status(client, node, forward_ret)
            return

    await upload_telegram_chat_message(
        _bot.client, node.upload_user, _bot.app, node, message
    )


async def forward_msg(node: TaskNode, message_id: int):
    """Forward normal message"""

    chat_download_config = ChatDownloadConfig()
    chat_download_config.last_read_message_id = message_id
    chat_download_config.download_filter = node.download_filter  # type: ignore

    await _bot.download_chat_task(_bot.client, chat_download_config, node)


# Global flood wait cooldown is now unified in pyrogram_extension._unified_flood_wait

async def _consume_one_pending():
    """Consume exactly one pending task from bot_tasks.json.
    Single-shot, no loop. Sends TG notification on consumption.
    """
    import logging
    logger = logging.getLogger("bot.pending")
    task_id = None
    reserved = False
    queued = False
    msg = None
    try:
        from module.task_store import get_pending_tasks, update_download_state, remove_task
        add_download_task = _bot.add_download_task
        pending = get_pending_tasks()
        if not pending:
            return
        # Reservations cover fetching, notification and queue handoff. The same
        # task can be in both sets during handoff, so count their union once.
        reserved_count = len(_bot._in_queue | _bot._consuming)
        max_tasks = max(1, getattr(_bot.app, "max_download_task", 5))
        active = _bot._active_downloads
        if active + reserved_count >= max_tasks:
            logger.debug(
                "Pending consumer: no free slot (active=%s reserved=%s max=%s)",
                active, reserved_count, max_tasks,
            )
            return

        # Find first truly pending task (not already in asyncio Queue)
        task_data = None
        for t in pending:
            if (t.get("download_state", "pending") == "pending"
                    and t.get("task_id") not in _bot._in_queue
                    and t.get("task_id") not in _bot._consuming):
                task_data = t
                break
        if not task_data:
            return
        task_id = task_data.get("task_id")
        if not task_id:
            return

        chat_id = task_data.get("chat_id")
        extra = task_data.get("extra_data", {}) or {}
        msg_id = extra.get("message_id") or extra.get("source_message_id")
        from_user_id = task_data.get("from_user_id", "")
        task_type = task_data.get("task_type", "")

        # Config tasks are handled by download_all_chat, skip them
        if task_type == "config":
            remove_task(task_id)
            logger.info(f"Skipped config task {task_id} (handled by download_all_chat)")
            return

        if not chat_id:
            logger.warning("Pending task %s has no chat_id", task_id)
            return
        client = (_bot.get_message_client(chat_id, from_user_id, extra.get("message_client", ""))
                  if msg_id else _bot.client)
        if not client:
            return

        # Check unified flood wait cooldown (shared with edit_message, download_media)
        if is_flood_wait_active():
            from module.pyrogram_extension import get_flood_wait_remaining
            remaining = int(get_flood_wait_remaining())
            logger.debug(f"Pending consumer: unified FLOOD_WAIT cooldown, {remaining}s remaining")
            return

        try:
            cid = int(chat_id)
        except (ValueError, TypeError):
            cid = chat_id

        if not msg_id:
            if task_type in ("download", "forward"):
                _bot._start_range_recovery(task_data)
            else:
                logger.warning("Pending task %s has no message_id", task_id)
            return

        # Hold this reservation across every await until queue handoff succeeds.
        _bot._consuming.add(task_id)
        reserved = True
        # Use cached message from direct_download if available (skip get_messages)
        cached_msg = _bot._cached_messages.pop(task_id, None)
        if cached_msg:
            msg = cached_msg
        else:
            try:
                msg = await asyncio.wait_for(client.get_messages(cid, int(msg_id)), timeout=300)
            except asyncio.TimeoutError:
                # A 300-second get_messages timeout may be silent throttling, even for more than
                # 24 hours. Keep the task pending and back off indefinitely rather than
                # declaring failure after three timeouts.
                _pending_timeout_counts[task_id] = _pending_timeout_counts.get(task_id, 0) + 1
                consecutive = _pending_timeout_counts[task_id]
                from module.pyrogram_extension import _unified_flood_wait
                backoff = min(60 * consecutive, 600)  # Increase from 60 seconds up to a 600-second cap.
                _unified_flood_wait["until"] = max(_unified_flood_wait["until"], time.time() + backoff + 5)
                _unified_flood_wait["reason"] = f'get_messages timeout, possibly rate limited; chat {cid}, message {msg_id}, attempt {consecutive}'
                if consecutive % 5 == 1:
                    logger.warning(
                        f"Pending consumer: get_messages TIMEOUT (300s) x{consecutive} for "
                        f"chat {cid} msg {msg_id}, backoff {backoff}s. "
                        f"Task stays pending (never fails on network)."
                    )
                return  # Keep the task pending.
            except (pyrogram.errors.FloodWait, pyrogram.errors.FloodPremiumWait) as e:
                # Don't move to failed list — keep pending, set unified cooldown
                wait_val = getattr(e, "value", 60)
                from module.pyrogram_extension import _unified_flood_wait
                _unified_flood_wait["until"] = max(_unified_flood_wait["until"], time.time() + wait_val + 5)
                _unified_flood_wait["reason"] = f"FloodWait {wait_val}s on get_messages chat {cid} msg {msg_id}"
                logger.warning(
                    f"Pending consumer: FLOOD_WAIT {wait_val}s on get_messages "
                    f"(chat {cid} msg {msg_id}), pausing consumer. Task stays pending."
                )
                # Notify user about rate limit pause
                if from_user_id and _bot and _bot.bot:
                    try:
                        notify_text = (
                            f"⏸️ {_t('Telegram rate limit pause')}\n" +
                            _t("Wait {seconds} seconds; task {task} stays pending",
                               seconds=wait_val, task=extra.get('task_id_display', str(task_id))) + "\n" +
                            f"{_t('Reason')}: FloodWait (get_messages)"
                        )
                        from module.pyrogram_extension import send_bot_notification
                        await send_bot_notification(_bot.bot, from_user_id, notify_text)
                    except Exception:
                        pass
                return
            except Exception as e:
                # Treat timeout-like exceptions as rate limits defensively.
                if isinstance(e, TimeoutError):
                    # Network errors only back off; they do not consume a failure budget.
                    _pending_timeout_counts[task_id] = _pending_timeout_counts.get(task_id, 0) + 1
                    consecutive = _pending_timeout_counts[task_id]
                    backoff = min(60 * consecutive, 600)
                    from module.pyrogram_extension import _unified_flood_wait
                    _unified_flood_wait["until"] = max(_unified_flood_wait["until"], time.time() + backoff + 5)
                    _unified_flood_wait["reason"] = f'get_messages connection timeout, possibly rate limited; chat {cid}, message {msg_id}, attempt {consecutive}'
                    if consecutive % 5 == 1:
                        logger.warning(
                            f"Pending consumer: TimeoutError x{consecutive} for chat {cid} msg {msg_id}: {e}, "
                            f"backoff {backoff}s. Task stays pending (never fails on network)."
                        )
                    return  # Keep pending; do not remove_task.
                # Reuse _pending_timeout_counts for network failures during reconnect. An
                # unstarted client once caused many recoverable tasks to fail; use cooldowns
                # while keeping them pending.
                _net_err_str = str(e)
                _is_net_err = (
                    "client has not been started" in _net_err_str.lower()
                    or "connectionerror" in _net_err_str.lower()
                    or "connection" in _net_err_str.lower()
                    or "timeout" in _net_err_str.lower()
                    or "flood" in _net_err_str.lower()
                )
                if _is_net_err:
                    # Network get_messages failures never exhaust a retry budget. Three consumer
                    # polls can occur within nine seconds, so a three-failure rule incorrectly
                    # kills tasks during outages. Increase backoff to 60, 120 and 180 seconds
                    # and resume when networking recovers.
                    _pending_timeout_counts[task_id] = _pending_timeout_counts.get(task_id, 0) + 1
                    consecutive = _pending_timeout_counts[task_id]
                    backoff = min(60 * consecutive, 180)
                    from module.pyrogram_extension import _unified_flood_wait
                    _unified_flood_wait["until"] = max(_unified_flood_wait["until"], time.time() + backoff + 5)
                    _unified_flood_wait["reason"] = (
                        f'get_messages network error; chat {cid}, message {msg_id}, attempt {consecutive}'
                    )
                    if consecutive % 5 == 1:  # Log at most once per five consecutive failures.
                        logger.warning(
                            f"Pending consumer: get_messages network error x{consecutive} "
                            f"for chat {cid} msg {msg_id}: {e}, "
                            f"backoff {backoff}s. Task stays pending (never fails on network)."
                        )
                    return  # Keep pending; do not remove_task.
                logger.warning(f"Pending consumer: get_messages failed for chat {cid} msg {msg_id}: {e}, moving to failed")
                try:
                    from module.download_stat import add_failed_download
                    task_id_display = extra.get("task_id_display", str(task_id))
                    add_failed_download(
                        chat_id=cid,
                        msg_id=msg_id,
                        task_id=task_id_display,
                        file_name="",
                        error_message=f'Failed to fetch message: {e}',
                        total_size=0,
                        source_link="",
                        from_user_id=str(from_user_id) if from_user_id else "",
                    )
                except Exception:
                    pass
                remove_task(task_id)
                return
        if not msg or msg.empty:
            logger.warning(f"Pending consumer: msg {msg_id} not found in chat {cid}, moving to failed")
            try:
                from module.download_stat import add_failed_download
                task_id_display = extra.get("task_id_display", str(task_id))
                source_link = ""
                if extra.get("source_chat_id") and extra.get("source_message_id"):
                    sid = extra["source_chat_id"]
                    link_id = str(sid)[4:] if str(sid).startswith("-100") else str(sid)
                    source_link = f"https://t.me/c/{link_id}/{extra['source_message_id']}"
                add_failed_download(
                    chat_id=cid,
                    msg_id=msg_id,
                    task_id=task_id_display,
                    file_name="",
                    error_message='Message not found or deleted',
                    total_size=0,
                    source_link=source_link,
                    from_user_id=str(from_user_id) if from_user_id else "",
                )
            except Exception:
                pass
            remove_task(task_id)
            return
        # Clear consecutive timeout counts after get_messages succeeds.
        _pending_timeout_counts.pop(task_id, None)
        node = _bot.task_node.get(int(task_id)) if str(task_id).isdigit() else _bot.task_node.get(task_id)
        if not node:
            from module.app import TaskNode
            # Preserve original task_id_display and source info from extra_data
            # so recovered tasks keep their original display ID (e.g. 0626-784)
            # instead of generating a new one (0628-1241)
            node = TaskNode(
                chat_id=cid,
                from_user_id=from_user_id or cid,
                reply_message_id=0,
                limit=1,
                bot=_bot.bot,
                task_id=task_id,
                task_id_display=extra.get("task_id_display", ""),
                topic_id=extra.get("topic_id", 0),
            )
            node.source_chat_id = extra.get("source_chat_id", 0)
            node.source_message_id = extra.get("source_message_id", 0)
            node.source_chat_title = extra.get("source_chat_title", "")
            _bot.add_task_node(node)
        owner = getattr(msg, "_client", None) or client
        node.client = _bot.bot if owner is _bot.bot else None
        # Mark as in-queue (in-memory, not persisted). Worker will remove on pickup.
        _bot._in_queue.add(task_id)
        node.is_running = True

        # Create placeholder in _download_result so WebUI shows the task immediately.
        # Pyrogram's update_download_status will overwrite total_size/file_name/down_byte
        # when the actual download starts.
        # Set total_size=1 (not 0) so web.py doesn't filter it out as a "placeholder"
        # entry. WebUI shows a file-information placeholder until the real size arrives.
        from module.download_stat import _download_result
        if cid not in _download_result:
            _download_result[cid] = {}
        if int(msg_id) not in _download_result[cid]:
            _download_result[cid][int(msg_id)] = {
                "down_byte": 0,
                "total_size": 1,  # Non-zero so web.py doesn't hide this entry
                "progress": 0,
                "file_name": 'Fetching file information...',
                "start_time": time.time(),
                "end_time": 0,
                "download_speed": 0,
                "each_second_total_download": 0,
                "task_id": task_id,
                "task_id_display": getattr(node, "task_id_display", str(task_id)),
                "source_chat_title": getattr(msg.chat, "title", "") or extra.get("source_chat_title", ""),
                "source_chat_id": cid,
                "source_message_id": int(msg_id),
            }
            logger.info(f"Placeholder created in _download_result for task {task_id} (chat {cid} msg {msg_id})")

        # Send notification BEFORE add_download_task to avoid race condition:
        # if we queue first, worker may finish a small file before send_message
        # returns, causing reply_message_id=0 and missing final status notification
        # Skip if reply_message_id already set (e.g. recovery task already sent B6)
        if from_user_id and _bot and _bot.bot and not node.reply_message_id:
            try:
                chat_name = getattr(msg.chat, "title", str(cid))
                notify_text = (
                    f"📥 {_t('Pending download started')}\n"
                    f"{_t('Message')}: {msg_id}\n"
                    f"{_t('Task')}: {node.task_id_display}\n"
                    f"{_t('Group')}: {chat_name}"
                )
                recovery_msg = await asyncio.wait_for(
                    _bot.bot.send_message(int(from_user_id), notify_text), timeout=30
                )
                node.reply_message_id = recovery_msg.id
                node.last_edit_msg = ""       # Force first progress update to trigger
            except Exception as e:
                logger.warning(f"Pending consumer notification failed: {e}")

        queued = await add_download_task(msg, node)
        if not queued:
            logger.warning("Pending consumer: queue rejected task %s; will retry", task_id)
            return
        # Persist the handoff only after the queue confirms acceptance.
        update_download_state(task_id, "downloading")
        logger.info(f"Pending consumer: queued task {task_id} (msg {msg_id}) to download queue")
    except Exception as e:
        logger.warning(f"Pending consumer error: {e}")
    finally:
        if reserved:
            _bot._consuming.discard(task_id)
            if not queued and task_id in _bot._in_queue:
                _bot._in_queue.discard(task_id)
                if msg is not None:
                    _bot._cached_messages[task_id] = msg
async def _pending_consumer_loop():
    """Periodically fill the worker queue with pending tasks.

    Runs every 2s. Each tick consumes ONE pending task — the concurrency
    guard inside _consume_one_pending checks active workers + reserved tasks
    >= max_download_task and returns early if all slots are full.
    This prevents over-filling when get_messages is fast (cached messages).
    """
    import logging
    logger = logging.getLogger("bot.pending_loop")
    while _bot.is_running:
        try:
            await asyncio.sleep(2)
            from module.task_store import get_pending_tasks
            pending = get_pending_tasks()
            if pending:
                await _consume_one_pending()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Pending consumer loop error: {e}")


async def set_listen_forward_msg(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the chat to listen for forwarded messages.
    """
    args = message.text.split(maxsplit=3)

    if len(args) < 3:
        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid command format')}. {_t('Please use')} /listen_forward "
            f"https://t.me/c/src_chat https://t.me/c/dst_chat [{_t('Filter')}]\n",
        )
        return

    src_chat_link = args[1]
    dst_chat_link = args[2]
    download_filter = args[3] if len(args) > 3 else None

    node = await get_forward_task_node(
        client,
        message,
        TaskType.ListenForward,
        src_chat_link,
        dst_chat_link,
        download_filter=download_filter,
    )

    if not node:
        return

    if node.chat_id in _bot.listen_forward_chat:
        _bot.remove_task_node(_bot.listen_forward_chat[node.chat_id].task_id)

    node.is_running = True
    _bot.listen_forward_chat[node.chat_id] = node

    # Use a NewMessage handler for live events instead of polling.
    _bot._register_listen_handler()
    _bot._ensure_pending_consumer()


async def stop(client: pyrogram.Client, message: pyrogram.types.Message):
    """Stops listening for forwarded messages."""

    await client.send_message(
        message.chat.id,
        _t("Please select:"),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        _t("Stop Download"), callback_data="stop_download"
                    ),
                    InlineKeyboardButton(
                        _t("Stop Forward"), callback_data="stop_forward"
                    ),
                ],
                [  # Second row
                    InlineKeyboardButton(
                        _t("Stop Listen Forward"), callback_data="stop_listen_forward"
                    )
                ],
            ]
        ),
    )


async def stop_task(
    client: pyrogram.Client,
    query: pyrogram.types.CallbackQuery,
    queryHandler: str,
    task_type: TaskType,
):
    """Stop task"""
    label = {
        TaskType.Download: "download",
        TaskType.Forward: "Forward",
        TaskType.ListenForward: "Listen for forwarded messages",
    }[task_type]
    if query.data == queryHandler:
        buttons: List[InlineKeyboardButton] = []
        temp_buttons: List[InlineKeyboardButton] = []
        for key, value in _bot.task_node.copy().items():
            if not value.is_finish() and value.task_type is task_type:
                if len(temp_buttons) == 3:
                    buttons.append(temp_buttons)
                    temp_buttons = []
                temp_buttons.append(
                    InlineKeyboardButton(
                        f"{key}", callback_data=f"{queryHandler} task {key}"
                    )
                )
        if temp_buttons:
            buttons.append(temp_buttons)

        if buttons:
            buttons.insert(
                0,
                [
                    InlineKeyboardButton(
                        _t("all"), callback_data=f"{queryHandler} task all"
                    )
                ],
            )
            await client.edit_message_text(
                query.message.from_user.id,
                query.message.id,
                f"{_t('Stop')} {_t(label)}...",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await client.edit_message_text(
                query.message.from_user.id,
                query.message.id,
                f"{_t('No Task')}",
            )
    else:
        task_id = query.data.split(" ")[2]
        await client.edit_message_text(
            query.message.from_user.id,
            query.message.id,
            f"{_t('Stop')} {_t(label)}...",
        )
        _bot.stop_task(task_id)


async def on_query_handler(
    client: pyrogram.Client, query: pyrogram.types.CallbackQuery
):
    """
    Asynchronous function that handles query callbacks.

    Parameters:
        client (pyrogram.Client): The Pyrogram client object.
        query (pyrogram.types.CallbackQuery): The callback query object.

    Returns:
        None
    """

    if query.data and query.data.startswith("tasks:"):
        await query_tasks_callback(client, query)
        return

    for it in QueryHandler:
        queryHandler = QueryHandlerStr.get_str(it.value)
        if queryHandler in query.data:
            await stop_task(client, query, queryHandler, TaskType(it.value))


async def forward_to_comments(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Forwards specified media to a designated comment section.

    Usage: /forward_to_comments <source_chat_link> <destination_chat_link> <msg_start_id> <msg_end_id>

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.
    """
    return await forward_message_impl(client, message, True)
