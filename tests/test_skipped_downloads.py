"""Skipped history persists and forced downloads use the existing owning-account queue."""

import asyncio
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from module import bot, download_stat, task_store, web
from module.app import Application, TaskNode
from module.telegram import pyrogram
from pyrogram import enums, types


class SkippedDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Application(str(self.root / "missing.yaml"), str(self.root / "data/data.json"))
        self.app.load_config()
        self.app.settings.save({"web_login_secret": "OfflinePassword1"}, setup_complete=True)
        self.app.apply_settings()
        self.backend = bot.DownloadBot()
        self.backend.app = self.app
        self.backend.client = SimpleNamespace()
        self.backend.bot = SimpleNamespace(get_messages=AsyncMock())
        self.backend._ensure_pending_consumer = Mock()
        self.backend.add_download_task = AsyncMock(return_value=True)
        self.backend.bot.get_messages.return_value = types.Message(
            id=61, client=self.backend.bot, empty=False,
            chat=types.Chat(id=123, type=enums.ChatType.PRIVATE), media=enums.MessageMediaType.VIDEO,
            video=SimpleNamespace(file_name="clip.mp4"))
        self.node = TaskNode(123, task_id=1, download_filter="media_file_size > 100MB")
        self.node.client = self.backend.bot
        for item in (
            patch.object(web, "_app", self.app), patch.object(bot, "_bot", self.backend),
            patch.object(bot, "is_flood_wait_active", return_value=False),
            patch.object(download_stat, "_skipped_downloads", {}),
            patch.object(download_stat, "_failed_downloads", []),
            patch.object(download_stat, "_download_result", {}),
            patch.object(download_stat, "_chat_titles", {}),
            patch.object(download_stat, "_HISTORY_FILE", str(self.root / "history.json")),
            patch.object(task_store, "_TASKS_FILE", str(self.root / "tasks.json")),
            patch.object(task_store, "_COUNTER_FILE", str(self.root / "counter.json")),
        ):
            item.start()
            self.addCleanup(item.stop)
        web.create_web_app(self.app)
        self.client = web.get_flask_app().test_client()
        self.client.get("/login")
        with self.client.session_transaction() as session:
            session["web_auth"] = web._login_token()
            self.csrf = {"X-CSRF-Token": session["web_csrf_token"]}
        self.thread = threading.Thread(target=self.app.loop.run_forever)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.backend.is_running = False
        self.app.loop.call_soon_threadsafe(self.app.loop.stop)
        self.thread.join(timeout=5)
        self.app.loop.close()
        self.app.executor.shutdown()
        self.temp.cleanup()

    def record(self):
        message = self.backend.bot.get_messages.return_value
        download_stat.record_skipped_download(message, self.node, "No download filter matched")

    def test_persistence_deduplication_and_deletion_preserve_files(self):
        self.record()
        self.record()
        download_stat._skipped_downloads.clear()
        download_stat.load_downloads()
        result = self.client.get("/get_skipped_downloads?search=clip&limit=1").json
        self.assertEqual(result["total"], 1)
        entry = result["tasks"][0]
        self.assertEqual(entry["download_filter"], self.node.download_filter)
        self.assertEqual(entry["message_client"], "bot")
        self.assertEqual(entry["reason"], "No download filter matched")
        protected = self.root / "existing.mp4"
        protected.write_bytes(b"keep")
        self.assertEqual(self.client.post("/delete_skipped?task_id=123_61", headers=self.csrf).json["code"], "1")
        self.assertEqual(self.client.get("/get_skipped_downloads").json["total"], 0)
        self.assertEqual(protected.read_bytes(), b"keep")
        self.assertEqual(json.loads((self.root / "history.json").read_text())["skipped"], [])

    def test_force_download_persists_bypass_and_restores_bot_owned_message(self):
        self.node.topic_id = 77
        self.record()
        response = self.client.post("/download_skipped?task_id=123_61", headers=self.csrf)
        self.assertEqual(response.json["code"], "1", response.json)
        saved = task_store.get_pending_tasks()[0]
        self.assertIs(saved["extra_data"]["bypass_filters"], True)
        self.assertEqual(saved["download_filter"], "")
        self.assertEqual(saved["extra_data"]["message_client"], "bot")
        self.assertEqual(saved["extra_data"]["topic_id"], 77)
        self.assertEqual(download_stat.get_skipped_downloads(), [])
        asyncio.run_coroutine_threadsafe(bot._consume_one_pending(), self.app.loop).result(timeout=5)
        message, node = self.backend.add_download_task.call_args.args
        self.assertIs(node.client, self.backend.bot)
        self.assertTrue(node.bypass_filters)
        self.assertEqual(node.download_filter, "")
        self.backend.bot.get_messages.assert_awaited_once_with(123, 61)

    def test_failed_persistence_does_not_remove_skipped_record(self):
        self.record()
        with patch.object(bot, "save_task", return_value=False):
            response = self.client.post("/download_skipped?task_id=123_61", headers=self.csrf)
        self.assertEqual(response.json["code"], "0")
        self.assertEqual(len(download_stat.get_skipped_downloads()), 1)
        self.backend._ensure_pending_consumer.assert_not_called()

    def test_duplicate_force_download_retains_record_without_second_task(self):
        self.record()
        self.assertEqual(self.client.post("/download_skipped?task_id=123_61", headers=self.csrf).json["code"], "1")
        self.record()
        result = self.client.post("/download_skipped?task_id=123_61", headers=self.csrf).json
        self.assertEqual(result["code"], "0")
        self.assertEqual(len(task_store.get_pending_tasks()), 1)
        self.assertEqual(len(download_stat.get_skipped_downloads()), 1)

    def test_failed_history_write_keeps_deleted_record(self):
        self.record()
        with patch.object(download_stat.os, "replace", side_effect=OSError("offline")):
            self.assertEqual(self.client.post("/delete_skipped?task_id=123_61", headers=self.csrf).json["code"], "0")
        self.assertEqual(len(download_stat.get_skipped_downloads()), 1)

    def test_skipped_mutations_require_login_and_csrf(self):
        self.record()
        anonymous = web.get_flask_app().test_client()
        self.assertEqual(anonymous.get("/get_skipped_downloads").status_code, 401)
        for path in ("/delete_skipped", "/download_skipped"):
            self.assertEqual(anonymous.post(path + "?task_id=123_61").status_code, 401)
            self.assertEqual(self.client.post(path + "?task_id=123_61").status_code, 400)
        self.assertEqual(len(download_stat.get_skipped_downloads()), 1)
