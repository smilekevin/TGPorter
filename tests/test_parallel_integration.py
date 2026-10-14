"""Exercise the real download entry point and progress state with fake MTProto data."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pyrogram
from pyrogram import enums, types
from pyrogram.client import Cache
from pyrogram.file_id import FileId, FileType

import media_downloader as downloader
from module import download_stat, pyrogram_extension, web
from module.app import Application, DownloadStatus, TaskNode
from module.parallel_download import parts_path
from test_parallel_download import FakeClient, MB


class ParallelIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.data = b"".join(bytes([i]) * MB for i in range(6)) + b"tail"
        self.client = FakeClient(self.data)
        self.client.download_media = AsyncMock(side_effect=AssertionError("Unexpected legacy download"))
        self.client.stop_transmission = Mock(side_effect=pyrogram.StopTransmission)
        self.owner = SimpleNamespace(get_messages=AsyncMock())
        self.message = self.media_message(b"old-reference")
        self.owner.get_messages.return_value = self.media_message(b"new-reference")
        self.app = SimpleNamespace(
            media_types=["video"], file_formats={"video": ["all"]},
            enable_download_txt=False, hide_file_name=False,
            set_download_id=Mock(), upload_file=AsyncMock(return_value=False),
            max_download_task=5, file_download_concurrency=3,
            download_min_part_size_mb=1,
        )
        self.node = TaskNode(chat_id=100, task_id=1, limit=1)
        self.node.total_task = 1
        self.result = {}
        real_sleep = asyncio.sleep

        async def short_sleep(delay):
            await real_sleep(min(delay, 0.001))

        patches = [
            patch.object(downloader, "app", self.app),
            patch.object(downloader, "_get_media_meta", AsyncMock(return_value=(
                str(self.path / "saved.mp4"), str(self.path / "partial.mp4"), "mp4"))),
            patch.object(downloader, "_maybe_reconnect_client", AsyncMock(return_value=False)),
            patch.object(downloader, "_add_failed_download", Mock()),
            patch.object(download_stat, "_download_result", self.result),
            patch.object(download_stat, "_download_lock", asyncio.Lock()),
            patch.object(download_stat, "_paused_tasks", set()),
            patch.object(download_stat, "_cancelled_tasks", set()),
            patch.object(download_stat, "_download_state", download_stat.DownloadState.Downloading),
            patch.object(download_stat, "_task_heartbeat", {}),
            patch.object(download_stat, "save_downloads", Mock()),
            patch.object(pyrogram_extension, "_unified_flood_wait", {"until": 0, "reason": ""}),
            patch.object(pyrogram_extension, "_download_cache", Cache(1024)),
            patch.object(downloader.asyncio, "sleep", short_sleep),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def media_message(self, reference):
        file_id = FileId(file_type=FileType.VIDEO, dc_id=2, media_id=101,
                         access_hash=3, file_reference=reference).encode()
        return types.Message(
            client=self.owner, id=61,
            chat=types.Chat(id=100, type=enums.ChatType.PRIVATE),
            media=enums.MessageMediaType.VIDEO,
            video=SimpleNamespace(file_size=len(self.data), date=datetime.now(),
                                  file_id=file_id, file_unique_id="stable-file-identity"),
        )

    async def test_entry_point_downloads_and_records_only_one_aggregate_ui_entry(self):
        await downloader.download_task(self.client, self.message, self.node)
        self.assertEqual((self.path / "saved.mp4").read_bytes(), self.data)
        self.assertEqual(self.node.success_download_task, 1)
        self.assertEqual(self.node.download_status[61], DownloadStatus.SuccessDownload)
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(list(self.result), [100])
        self.assertEqual(list(self.result[100]), [61])
        self.assertEqual(self.result[100][61]["down_byte"], len(self.data))
        self.assertEqual(self.result[100][61]["progress"], 100.0)
        self.client.download_media.assert_not_called()

    async def test_small_file_uses_one_range_and_skips_merge(self):
        self.app.download_min_part_size_mb = 16
        status, path, error = await downloader.download_media(
            self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
        self.assertEqual(status, DownloadStatus.SuccessDownload)
        self.assertEqual(Path(path).read_bytes(), self.data)
        self.assertEqual(self.client.calls, [(0, 7)])
        self.assertEqual(error, "")
        self.assertFalse(parts_path(str(self.path / "partial.mp4")).exists())

    async def test_live_config_changes_do_not_resize_started_file(self):
        async def before(_):
            self.app.file_download_concurrency = 1
            self.app.download_min_part_size_mb = 100
        self.client.before_chunk = before
        await downloader.download_task(self.client, self.message, self.node)
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual((self.path / "saved.mp4").read_bytes(), self.data)

    async def test_reference_refresh_preserves_parts_and_uses_message_owner(self):
        failed_once = False
        async def before(index):
            nonlocal failed_once
            if index == 1 and not failed_once:
                failed_once = True
                raise pyrogram.errors.FileReferenceExpired()
        self.client.before_chunk = before
        await downloader.download_task(self.client, self.message, self.node)
        self.assertEqual((self.path / "saved.mp4").read_bytes(), self.data)
        self.owner.get_messages.assert_awaited_once_with(chat_id=100, message_ids=61)
        self.assertEqual(self.node.success_download_task, 1)
        self.assertTrue(any(offset == 1 for offset, _ in self.client.calls))

    async def test_deleting_paused_file_stops_without_creating_failed_task(self):
        download_stat.pause_task(self.node.task_id)
        task = asyncio.create_task(downloader.download_media(
            self.client, self.message, self.app.media_types, self.app.file_formats, self.node))
        await asyncio.sleep(0.01)
        download_stat._cancelled_tasks.add(str(self.node.task_id))
        status, _, _ = await asyncio.wait_for(task, 2)
        self.assertEqual(status, DownloadStatus.SkipDownload)
        self.assertFalse((self.path / "saved.mp4").exists())
        self.assertEqual(self.client.calls, [])

    async def test_restart_restores_one_ui_progress_entry_without_counting_old_bytes_as_speed(self):
        entered = asyncio.Event()
        async def block_second(index):
            if index == 1:
                entered.set()
                await asyncio.Event().wait()
        self.client.before_chunk = block_second
        task = asyncio.create_task(downloader.download_task(self.client, self.message, self.node))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        directory = parts_path(str(self.path / "partial.mp4"))
        saved = sum(path.stat().st_size for path in directory.glob("*.part"))
        self.assertGreater(saved, 0)
        self.result.clear()  # A new process has no previous in-memory statistics.
        self.client.calls.clear()
        self.app.file_download_concurrency = 1
        self.app.download_min_part_size_mb = 100
        resumed = asyncio.Event()
        release = asyncio.Event()
        async def block_network(_):
            resumed.set()
            await release.wait()
        self.client.before_chunk = block_network
        task = asyncio.create_task(downloader.download_task(self.client, self.message, self.node))
        try:
            await asyncio.wait_for(resumed.wait(), 2)
            self.assertEqual(self.result[100][61]["down_byte"], saved)
            self.assertEqual(self.result[100][61]["download_speed"], 0)
            with web._flask_app.test_request_context("/get_download_list?already_down=false"):
                response = web.get_download_list()
                data = response.get_json() if hasattr(response, "get_json") else json.loads(response)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["download_progress_raw"], round(saved / len(self.data) * 100, 1))
        finally:
            release.set()
            await task
        self.assertEqual((self.path / "saved.mp4").read_bytes(), self.data)

    async def test_real_pyrogram_get_file_parallel_rpc_and_swallowed_error_recovery(self):
        client = pyrogram.Client("offline-range-test", api_id=1, api_hash="offline",
                                 in_memory=True, no_updates=True,
                                 max_concurrent_transmissions=4)
        self.addCleanup(client.executor.shutdown)
        requests = []
        tracker = {"active": 0, "peak": 0}
        expired = False
        async def invoke(request, **_):
            nonlocal expired
            requests.append(request)
            tracker["active"] += 1
            tracker["peak"] = max(tracker["peak"], tracker["active"])
            try:
                await asyncio.sleep(0)
                if request.offset == MB and not expired:
                    expired = True
                    raise pyrogram.errors.FileReferenceExpired()
                return pyrogram.raw.types.upload.File(
                    type=pyrogram.raw.types.storage.FileUnknown(), mtime=0,
                    bytes=self.data[request.offset:request.offset + request.limit],
                )
            finally:
                tracker["active"] -= 1
        session = SimpleNamespace(invoke=invoke)
        with patch("pyrogram.client.get_session", AsyncMock(return_value=session)):
            await downloader.download_task(client, self.message, self.node)
        self.assertEqual((self.path / "saved.mp4").read_bytes(), self.data)
        self.assertGreater(tracker["peak"], 1)
        self.assertEqual(client.get_file_semaphore._value, 4)
        self.assertTrue(all(request.offset % MB == 0 and request.limit == MB
                            for request in requests))
        self.owner.get_messages.assert_awaited_once_with(chat_id=100, message_ids=61)
        self.assertEqual(self.node.success_download_task, 1)


class DownloadConfigTests(unittest.TestCase):
    def setUp(self):
        self.app = Application("unused-config.yaml", "unused-data.yaml")
        self.addCleanup(self.app.loop.close)
        self.addCleanup(self.app.executor.shutdown)
        self.base = {"api_id": "test", "api_hash": "test", "media_types": [],
                     "file_formats": {}}

    def test_defaults_keep_one_range_and_existing_worker_count(self):
        self.app.assign_config({**self.base, "max_download_task": 3})
        self.assertEqual(self.app.file_download_concurrency, 1)
        self.assertEqual(self.app.download_min_part_size_mb, 16)
        self.assertFalse(hasattr(self.app, "download_concurrency"))

    def test_removed_global_setting_is_dropped_on_save(self):
        self.app.config = {**self.base, "download_concurrency": 1}
        self.app.assign_config(self.app.config)
        self.app.update_config(False)
        self.assertNotIn("download_concurrency", self.app.config)

    def test_settings_survive_config_save_and_raise_transport_limit_if_needed(self):
        self.app.assign_config({**self.base, "max_download_task": 3,
                                "file_download_concurrency": 12,
                                "download_min_part_size_mb": 8})
        self.app.update_config(False)
        self.assertEqual(self.app.config["file_download_concurrency"], 12)
        self.assertEqual(self.app.config["download_min_part_size_mb"], 8)
        self.assertEqual(self.app.max_concurrent_transmissions, 36)

    def test_invalid_concurrency_and_minimum_are_rejected(self):
        for key in ("file_download_concurrency", "download_min_part_size_mb"):
            for value in (0, -1, True, 1.5, "4", None):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.app.assign_config({**self.base, key: value})


if __name__ == "__main__":
    unittest.main()
