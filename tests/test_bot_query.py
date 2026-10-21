"""Exercise Bot task queries against shared in-memory and pending task state."""

import copy
import html
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from module import bot, bot_query, download_service, download_stat
from module.download_service import DownloadService
from module.language import Language, set_language
from module.telegram import pyrogram


class BotQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = bot.DownloadBot()
        self.backend.app = SimpleNamespace()
        self.backend.allowed_user_ids = [200]
        self.client = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
        self.message = SimpleNamespace(text="/query", id=10, chat=SimpleNamespace(id=-100),
                                       from_user=SimpleNamespace(id=200))
        self.result = {100: {
            1: self.record(1, "active.mp4", 20),
            2: self.record(2, "done.mp4", 100),
        }}
        self.pending = [dict(task_id=3, chat_id=100, created_at=0, task_type="download",
                             extra_data=dict(message_id=3, filename="pending.mp4", task_id_display="1016-3"))]
        self.failed = [dict(task_id=4, chat_id=100, msg_id=4, file_name="failed.mp4",
                            error_message="Unknown error", timestamp=0)]
        for item in (
            patch.object(bot, "_bot", self.backend),
            patch.object(download_stat, "_download_result", self.result),
            patch.object(download_stat, "_failed_downloads", self.failed),
            patch.object(download_stat, "_paused_tasks", set()),
            patch.object(download_stat, "_chat_titles", {}),
            patch.object(download_service, "get_pending_tasks", side_effect=lambda **kwargs: self.pending),
        ):
            item.start()
            self.addCleanup(item.stop)
        set_language(Language.EN)
        self.addCleanup(set_language, Language.EN)
        self.service = DownloadService(self.backend.app)

    @staticmethod
    def record(task_id, name, down):
        return dict(task_id=task_id, task_id_display=f"1016-{task_id}", file_name=name,
                    down_byte=down, total_size=100, download_speed=10,
                    start_time=task_id, end_time=task_id)

    def callback(self, data):
        return SimpleNamespace(data=data, answer=AsyncMock(), from_user=SimpleNamespace(id=200),
                               message=SimpleNamespace(id=20, chat=SimpleNamespace(id=-100)))

    async def test_default_lists_all_categories_and_does_not_change_state(self):
        before = copy.deepcopy((self.result, self.pending, self.failed))
        await bot_query.query_tasks(self.client, self.message)
        call = self.client.send_message.call_args
        self.assertEqual(call.args[0], -100)
        text = call.args[1]
        for value in ("Downloading: 1", "Pending: 1", "Completed: 1", "Failed: 1",
                      "active.mp4", "done.mp4", "pending.mp4", "failed.mp4"):
            self.assertIn(value, text)
        self.assertIn("(4 tasks)", text)
        self.assertEqual(call.kwargs["reply_parameters"].message_id, 10)
        self.assertNotIn("reply_to_message_id", call.kwargs)
        self.assertEqual((self.result, self.pending, self.failed), before)

    async def test_completed_filter_and_page_clamp(self):
        self.result[100] = {i: self.record(i, f"done-{i}.mp4", 100) for i in range(1, 15)}
        text, keyboard = bot_query.build_task_query(self.service, "completed", 2)
        self.assertIn("Page 2/3 (14 tasks)", text)
        self.assertIn("done-8.mp4", text)
        self.assertNotIn("done-14.mp4", text)
        buttons = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("tasks:completed:1", buttons)
        self.assertIn("tasks:completed:3", buttons)
        text, keyboard = bot_query.build_task_query(self.service, "completed", 9999999)
        self.assertIn("Page 3/3", text)
        self.assertIn("done-1.mp4", text)
        self.assertNotIn("tasks:completed:4", str(keyboard))

    async def test_pending_matches_service_deduplication_and_paused_task_status(self):
        self.pending.append(dict(task_id=1, chat_id=100, extra_data=dict(message_id=1)))
        download_stat.pause_task("100_1")
        text, _ = bot_query.build_task_query(self.service)
        self.assertIn("Pending: 1", text)
        self.assertIn("<b>Paused</b>", text)
        pending, _ = bot_query.build_task_query(self.service, "pending")
        self.assertIn("pending.mp4", pending)
        self.assertNotIn("active.mp4", pending)

    async def test_valid_command_uses_category_and_page(self):
        self.message.text = "/query@my_bot completed 1"
        await bot_query.query_tasks(self.client, self.message)
        text = self.client.send_message.call_args.args[1]
        self.assertIn("done.mp4", text)
        self.assertNotIn("active.mp4", text)
        self.assertNotIn("pending.mp4", text)

    async def test_callback_refresh_edits_existing_chat_message(self):
        query = self.callback("tasks:pending:1")
        await bot.on_query_handler(self.client, query)
        query.answer.assert_awaited_once_with()
        call = self.client.edit_message_text.call_args
        self.assertEqual(call.args[:2], (-100, 20))
        self.assertIn("pending.mp4", call.args[2])
        self.client.send_message.assert_not_awaited()
        self.client.edit_message_text.side_effect = pyrogram.errors.MessageNotModified()
        await bot.on_query_handler(self.client, query)

    async def test_invalid_arguments_and_callbacks_never_read_task_state(self):
        with patch.object(bot_query, "build_task_query") as build:
            for suffix in ("active 0", "missing", "pending -1", "all inf", "all 1 extra", "all 99999999"):
                self.message.text = "/query " + suffix
                await bot_query.query_tasks(self.client, self.message)
                self.assertIn("Usage:", self.client.send_message.call_args.args[1])
            for data in ("tasks:all:0", "tasks:bad:1", "tasks:all:1:extra"):
                query = self.callback(data)
                await bot.on_query_handler(self.client, query)
                self.assertTrue(query.answer.call_args.kwargs["show_alert"])
            build.assert_not_called()
        self.client.edit_message_text.assert_not_awaited()

    async def test_empty_and_translated_results(self):
        self.result.clear()
        self.pending.clear()
        self.failed.clear()
        set_language("zh-cn")
        text, _ = bot_query.build_task_query(self.service)
        self.assertIn("下载任务", text)
        self.assertIn("暂无任务", text)
        self.assertIn("共 0 个任务", text)

    async def test_long_untrusted_labels_are_escaped_and_below_telegram_limit(self):
        self.result[100] = {i: self.record(i, "<b>" + "😀" * 2000 + ".mp4", 100)
                            for i in range(1, 7)}
        text, _ = bot_query.build_task_query(self.service, "completed")
        self.assertIn("&lt;b&gt;", text)
        visible = html.unescape(re.sub(r"<[^>]+>", "", text))
        self.assertLess(len(visible.encode("utf-16-le")) // 2, 4096)
        self.assertIn("Page 1/1 (6 tasks)", text)

    async def test_command_and_pagination_use_existing_user_allowlist(self):
        handlers = []
        self.backend.bot = SimpleNamespace(add_handler=lambda handler, **kwargs: handlers.append(handler))
        self.backend._register_bot_handlers()
        command = next(handler for handler in handlers if getattr(handler.callback, "__wrapped__", None) is bot_query.query_tasks)
        callback = next(handler for handler in handlers if getattr(handler.callback, "__wrapped__", None) is bot.on_query_handler)
        message = pyrogram.types.Message(
            id=10, text="/query", from_user=pyrogram.types.User(id=200),
            chat=pyrogram.types.Chat(id=200, type=pyrogram.enums.ChatType.PRIVATE))
        self.client.me = SimpleNamespace(username="my_bot")
        self.assertTrue(await command.filters(self.client, message))
        query = pyrogram.types.CallbackQuery(
            id="test", from_user=pyrogram.types.User(id=200), chat_instance="test",
            message=message, data="tasks:all:1")
        self.assertTrue(await callback.filters(self.client, query))
        message.from_user.id = query.from_user.id = 999
        self.assertFalse(await command.filters(self.client, message))
        self.assertFalse(await callback.filters(self.client, query))
