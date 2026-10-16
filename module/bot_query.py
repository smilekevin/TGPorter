"""Read-only Bot task listings backed by the shared download service."""

import html

from module.download_service import DownloadService
from module.language import _t
from module.telegram import pyrogram
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


_PAGE_SIZE = 6
_STATUS_LABELS = {
    "all": "All statuses",
    "active": "Downloading",
    "pending": "Pending",
    "completed": "Completed",
    "failed": "Failed",
}


def _parse_query(status="all", page="1"):
    if status not in _STATUS_LABELS or not page.isascii() or not page.isdecimal() or len(page) > 7:
        raise ValueError("Invalid task query")
    page = int(page)
    if page < 1:
        raise ValueError("Invalid task query page")
    return status, page


def _text(value, limit=80):
    """Keep dynamic labels on one line and bound the rendered Telegram message."""
    value = " ".join(str(value or "").split())
    return html.escape(value if len(value) <= limit else value[:limit - 1] + "…")


def build_task_query(service, status="all", page=1):
    """Return one bounded page with category counts and read-only navigation."""
    groups = {
        "active": service.get_download_list(already_down="false"),
        "pending": service.web_get_pending_list(limit=0)["tasks"],
        "completed": service.get_download_list(already_down="true")["tasks"],
        "failed": service.web_get_failed_downloads(),
    }
    rows = [(kind, task) for kind, tasks in groups.items()
            if status in ("all", kind) for task in tasks]
    pages = max(1, (len(rows) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(1, page), pages)
    lines = [f"<b>{_t('Download tasks')}</b>"]
    lines.extend(f"{_t(_STATUS_LABELS[kind])}: {len(tasks)}" for kind, tasks in groups.items())
    lines.append("")
    for kind, task in rows[(page - 1) * _PAGE_SIZE:page * _PAGE_SIZE]:
        label = "Paused" if task.get("status") == "paused" else _STATUS_LABELS[kind]
        display_id = task.get("task_id_display") or task["task_id"]
        filename = task.get("filename") or _t("Message {id} (pending download)", id=task.get("id", "?"))
        lines.append(f"<b>{_t(label)}</b> · <code>{_text(display_id, 40)}</code> · {_text(filename)}")
        detail = _text(task.get("chat_title") or task.get("chat"), 50)
        if kind == "active":
            detail += f" · {_text(task['download_progress'], 10)}% · {_text(task['download_speed'], 24)}"
        elif kind == "failed":
            detail += f" · {_text(_t(task.get('error_message', 'Unknown error')), 80)}"
        else:
            detail += f" · {_text(task.get('total_size'), 24)}"
        lines.append(detail)
    if not rows:
        lines.append(_t("No tasks"))
    lines.extend(["", _t("Page {page}/{pages} ({total} tasks)", page=page, pages=pages, total=len(rows))])
    tabs = [InlineKeyboardButton(_t(label), callback_data=f"tasks:{kind}:1")
            for kind, label in _STATUS_LABELS.items()]
    navigation = []
    if page > 1:
        navigation.append(InlineKeyboardButton(_t("Previous"), callback_data=f"tasks:{status}:{page - 1}"))
    navigation.append(InlineKeyboardButton(_t("Refresh"), callback_data=f"tasks:{status}:{page}"))
    if page < pages:
        navigation.append(InlineKeyboardButton(_t("Next"), callback_data=f"tasks:{status}:{page + 1}"))
    return "\n".join(lines), InlineKeyboardMarkup([tabs[:3], tabs[3:], navigation])


async def query_tasks(client, message):
    """Handle /query with optional category and one-based page number."""
    args = (message.text or "").split()
    try:
        if len(args) > 3:
            raise ValueError("Too many task query arguments")
        status, page = _parse_query(args[1] if len(args) > 1 else "all",
                                    args[2] if len(args) > 2 else "1")
    except ValueError:
        await client.send_message(message.chat.id, _t("Usage: /query [all|active|pending|completed|failed] [page]"))
        return
    from module.bot import _bot
    text, keyboard = build_task_query(DownloadService(_bot.app), status, page)
    await client.send_message(message.chat.id, text, reply_to_message_id=message.id,
                              reply_markup=keyboard, parse_mode=pyrogram.enums.ParseMode.HTML)


async def query_tasks_callback(client, query):
    """Refresh or paginate the existing query message without changing tasks."""
    try:
        prefix, status, page = query.data.split(":")
        if prefix != "tasks":
            raise ValueError("Invalid task query callback")
        status, page = _parse_query(status, page)
    except (ValueError, AttributeError):
        await query.answer(_t("Usage: /query [all|active|pending|completed|failed] [page]"), show_alert=True)
        return
    await query.answer()
    from module.bot import _bot
    text, keyboard = build_task_query(DownloadService(_bot.app), status, page)
    try:
        await client.edit_message_text(query.message.chat.id, query.message.id, text,
                                       reply_markup=keyboard, parse_mode=pyrogram.enums.ParseMode.HTML)
    except pyrogram.errors.MessageNotModified:
        pass
