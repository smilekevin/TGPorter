"""Durable file holds and loop-owned interruption of downloads and preparation."""

import asyncio
import os
import time
from dataclasses import dataclass, field

from module import task_store


@dataclass
class Attempt:
    task: asyncio.Task
    node: object
    message_id: int
    paused: bool = False
    released: asyncio.Event = field(default_factory=asyncio.Event)


_attempts = {}
_queued = {}
_locks = {}


def register_attempt(task, node, message_id):
    attempt = Attempt(task, node, message_id)
    _attempts[f'{node.chat_id}_{message_id}'] = attempt
    return attempt


def release_attempt(attempt):
    key = f'{attempt.node.chat_id}_{attempt.message_id}'
    if _attempts.get(key) is attempt:
        _attempts.pop(key)
    attempt.released.set()


def remember_queued(queue, message, node):
    _queued[f'{node.chat_id}_{message.id}'] = (queue, message, node)


def forget_queued(node, message_id):
    _queued.pop(f'{node.chat_id}_{message_id}', None)


def pause_metadata(message_id, node):
    """Snapshot ownership and delivery settings before the original scan ends."""
    from module.bot import _bot
    return {
        'task_id': f'{node.chat_id}_{message_id}', 'owner_task_id': node.task_id,
        'chat_id': node.chat_id, 'url': '', 'start_offset_id': 0, 'end_offset_id': 0,
        'limit': 1, 'last_message_id': 0, 'status': 'running', 'task_type': 'direct',
        'download_filter': node.download_filter, 'from_user_id': node.from_user_id,
        'created_at': time.time(),
        'extra_data': {
            'message_id': message_id, 'task_id_display': node.task_id_display,
            'message_client': 'bot' if node.client is not None and node.client is _bot.bot else 'user',
            'source_chat_id': node.source_chat_id, 'source_message_id': node.source_message_id,
            'source_chat_title': node.source_chat_title, 'topic_id': node.topic_id,
            'bypass_filters': node.bypass_filters, 'dst_chat_id': node.upload_telegram_chat_id,
            'has_protected_content': node.has_protected_content,
            'upload_client': 'bot' if node.upload_user is not None and node.upload_user is _bot.bot else 'user',
        },
    }


def _snapshot(task_id):
    from module.download_stat import get_download_result
    for chat_id, messages in get_download_result().items():
        for message_id, entry in messages.items():
            key = f'{chat_id}_{message_id}'
            if str(task_id) not in (key, str(entry.get('task_id', ''))):
                continue
            if entry.get('total_size', 0) > 0 and entry.get('down_byte') == entry['total_size']:
                return None
            metadata = entry.get('pause_metadata')
            if metadata is None:
                return None
            result = dict(metadata, extra_data=dict(metadata['extra_data']))
            result['extra_data'].update(
                filename=entry.get('display_filename') or os.path.basename(entry.get('file_name', '')),
                total_size=entry.get('total_size', 0),
                temp_file_name=entry.get('temp_file_name', ''), file_name=entry.get('file_name', ''),
                down_byte=entry.get('down_byte', 0),
            )
            return result
    return None


def persist_pause(task_id):
    return task_store.set_task_paused(task_id, True, _snapshot(task_id))


def detach_file(node, message_id):
    """Remove only this unfinished file from its old owner's accounting."""
    from module.bot import _bot
    from module.download_stat import delete_download_result_entry, clear_task_heartbeat
    from module.pyrogram_extension import remove_download_cache
    delete_download_result_entry(node.chat_id, message_id)
    clear_task_heartbeat(f'{node.chat_id}_{message_id}')
    remove_download_cache(node.chat_id, message_id)
    node.download_temp_paths.pop(message_id, None)
    if message_id in node.download_status:
        node.download_status.pop(message_id)
        node.total_task = max(node.total_download_task, node.total_task - 1)
    _bot._in_queue.discard(node.task_id)
    if node.total_task == 0:
        node.is_running = False
        # A resumed file reuses its durable identity; do not let Bot reporting
        # complete that held task after interruption.
        if _bot.task_node.get(node.task_id) is node:
            _bot.task_node.pop(node.task_id)


async def set_paused(task_id, paused):
    """Persist first, then await connection/disk cleanup before releasing the hold."""
    snapshot = _snapshot(task_id) if paused else None
    key = snapshot['task_id'] if snapshot else str(task_id)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        record = task_store.set_task_paused(task_id, paused, snapshot)
        if record is None:
            return {'code': '0', 'message': 'Unable to save task pause state' if paused else 'Task not paused'}
        key = str(record['task_id'])
        from module.download_stat import pause_task, resume_task
        if paused:
            pause_task(key)
            attempt = _attempts.get(key)
            if attempt is not None:
                attempt.paused = True
                attempt.task.cancel()
                await attempt.released.wait()
            queued = _queued.pop(key, None)
            if queued is not None:
                queue, message, node = queued
                retained = []
                while not queue.empty():
                    item = queue.get_nowait()
                    if item[0] is message and item[1] is node:
                        queue.task_done()
                    else:
                        retained.append(item)
                        queue.task_done()
                for item in retained:
                    queue.put_nowait(item)
                detach_file(node, message.id)
        else:
            resume_task(key)
        return {'code': '1', 'message': 'paused' if paused else 'resumed',
                'pending_task_id': key}
