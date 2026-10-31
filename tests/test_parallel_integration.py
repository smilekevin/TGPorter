"""Exercise the real download entry point and progress state with fake MTProto data."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from module.telegram import pyrogram
from pyrogram import enums, types
from pyrogram.file_id import FileId, FileType

import media_downloader as downloader
from module import bot, download_stat, parallel_download, pyrogram_extension, web, task_store, download_control
from module.app import Application, DownloadStatus, ForwardStatus, TaskNode, TaskType
from module.download_service import DownloadService
from module.parallel_download import parts_path
from test_parallel_download import checkpoint_lengths
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
            download_part_size_mb=2,
        )
        self.node = TaskNode(chat_id=100, task_id=1, limit=1)
        self.node.total_task = 1
        self.result = {}
        self.real_get_media_meta = downloader._get_media_meta
        real_sleep = asyncio.sleep

        async def short_sleep(delay):
            await real_sleep(min(delay, 0.001))

        patches = [
            patch("module.parallel_download.CONNECTION_START_INTERVAL", 0),
            patch("module.parallel_download._wait_network_retry", AsyncMock()),
            patch.object(downloader, "app", self.app),
            patch.object(downloader, "_get_media_meta", AsyncMock(return_value=(
                str(self.path / "saved.mp4"), str(self.path / "partial.mp4"), "mp4"))),
            patch.object(downloader, "_maybe_reconnect_client", AsyncMock(return_value=False)),
            patch.object(downloader, "_add_failed_download", Mock()),
            patch.object(download_stat, "_download_result", self.result),
            patch.object(download_stat, "_skipped_downloads", {}),
            patch.object(download_stat, "_total_download_size", 0),
            patch.object(download_stat, "_download_streams", {}),
            patch.object(download_stat, "_download_lock", asyncio.Lock()),
            patch.object(download_stat, "_paused_tasks", set()),
            patch.object(download_stat, "_cancelled_tasks", set()),
            patch.object(download_stat, "_discarded_tasks", set()),
            patch.object(download_stat, "_download_state", download_stat.DownloadState.Downloading),
            patch.object(download_stat, "_task_heartbeat", {}),
            patch.object(download_stat, "save_downloads", Mock()),
            patch.object(pyrogram_extension, "_unified_flood_wait", {"until": 0, "reason": ""}),
            patch.object(pyrogram_extension, "_download_cache", {}),
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

    def use_real_media_metadata(self):
        self.app.temp_save_path = str(self.path / "temp")
        self.app.date_format = ""
        self.app.get_file_save_path = Mock(return_value=str(self.path / "downloads"))
        self.app.get_file_name = lambda message_id, name, caption, file_size=None: f"{message_id} - {name}"
        self.app.get_caption_name = Mock(return_value=None)
        self.message.chat.title = "摄影交流频道 ｜测试🐝"
        self.message.date = datetime(2026, 10, 16, 11, 2, 57)
        self.message.video.mime_type = "video/quicktime"
        self.message.video.file_name = "飞，一.mov"
        return patch.object(downloader, "_get_media_meta", self.real_get_media_meta)

    async def test_final_path_temp_resumes_with_fresh_task_and_saved_part_settings(self):
        with self.use_real_media_metadata():
            self.node.from_user_id = 123
            self.app.save_path = str(self.path / "downloads")
            self.app.file_path_prefix = ["from_user_id", "chat_title", "media_datetime"]
            self.app.get_file_save_path = lambda *args: Application.get_file_save_path(self.app, *args)
            expected_output, _, _ = await downloader._get_media_meta(
                100, self.message, self.message.video, 'video', from_user_id=123)
            entered = asyncio.Event()
            async def block(index):
                if index >= 1:
                    entered.set()
                    await asyncio.Event().wait()
            self.client.before_chunk = block
            task = asyncio.create_task(downloader.download_media(
                self.client, self.message, self.app.media_types, self.app.file_formats, self.node))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                staging = Path(self.node.download_temp_paths[61])
                self.assertEqual(str(staging), expected_output)
                self.assertTrue(Path(str(staging) + '.temp').exists())
                self.assertFalse(staging.exists())
                self.assertTrue((parts_path(str(staging)) / "manifest.json").exists())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertGreater(sum(checkpoint_lengths(staging)), 0)
            self.result.clear()
            self.client.calls.clear()
            self.client.before_chunk = None
            downloader._cleanup_stale_temp_files()
            self.node = TaskNode(chat_id=100, from_user_id=123, task_id=2, limit=1)
            self.app.file_download_concurrency = 1
            self.app.download_part_size_mb = 100
            status, output, _ = await downloader.download_media(
                self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
            self.assertEqual(status, DownloadStatus.SuccessDownload)
            self.assertEqual(output, expected_output)
            self.assertEqual(Path(output).name, "61 - 飞，一.mov")
            self.assertEqual(Path(output).relative_to(self.app.save_path).parts[0], "123")
            self.assertEqual(Path(output).read_bytes(), self.data)
            self.assertEqual(self.node.download_temp_paths[61], str(staging))
            self.assertNotIn(0, [offset for offset, _ in self.client.calls])
            self.assertTrue(staging.exists())
            self.assertFalse(Path(str(staging) + '.temp').exists())
            self.assertFalse(parts_path(str(staging)).exists())

    async def test_two_requesters_publish_separate_files_with_identical_source_media(self):
        with self.use_real_media_metadata():
            self.app.save_path = str(self.path / "downloads")
            self.app.file_path_prefix = ["from_user_id", "chat_title", "media_datetime"]
            self.app.date_format = "%Y_%m"
            self.app.get_file_save_path = lambda *args: Application.get_file_save_path(self.app, *args)
            outputs = []
            for requester in (123, 456):
                self.node.from_user_id = requester
                status, output, _ = await downloader.download_media(
                    self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
                self.assertIs(status, DownloadStatus.SuccessDownload)
                self.assertEqual(Path(output).relative_to(self.app.save_path).parts[0], str(requester))
                self.assertEqual(Path(output).read_bytes(), self.data)
                outputs.append(output)
            self.assertNotEqual(outputs[0], outputs[1])
            self.assertTrue(all(Path(output).exists() for output in outputs))

    async def test_serial_download_uses_final_name_temp_without_a_same_file_move(self):
        with self.use_real_media_metadata():
            del self.app.file_download_concurrency
            async def serial(message, file_name, **kwargs):
                output = Path(file_name)
                output.parent.mkdir(parents=True, exist_ok=True)
                partial = Path(file_name + '.temp')
                self.assertEqual(partial, output.with_name(output.name + '.temp'))
                partial.write_bytes(self.data)
                partial.replace(output)
                return str(output)
            self.client.download_media.side_effect = serial
            status, output, _ = await downloader.download_media(
                self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
            self.assertIs(status, DownloadStatus.SuccessDownload)
            self.assertEqual(Path(output).read_bytes(), self.data)
            self.assertFalse(Path(output + '.temp').exists())

    async def test_serial_resume_keeps_the_suffix_when_final_filename_ends_in_temp(self):
        output = self.path / 'video.temp'
        partial = Path(str(output) + '.temp')
        partial.write_bytes(self.data[:MB])
        async def get_file(file_id, size, limit, offset, **kwargs):
            self.assertEqual(offset, 1)
            yield self.data[offset * MB:]
        client = SimpleNamespace(get_file=get_file)
        resumed = await downloader._resume_download_from_temp(
            client, self.message, str(output), len(self.data), self.node, str(output), 0)
        self.assertTrue(resumed)
        downloader._move_to_download_path(str(output), str(output))
        self.assertEqual(output.read_bytes(), self.data)
        self.assertFalse(partial.exists())

    async def test_existing_uuid_checkpoint_resumes_before_using_new_final_path_layout(self):
        from module.staging import get_staging_path
        with self.use_real_media_metadata():
            legacy = get_staging_path(self.app.temp_save_path, '', 100, 61,
                                      self.message.video.file_unique_id,
                                      int(self.message.date.timestamp()), '')
            entered = asyncio.Event()
            async def block(index):
                if index >= 1:
                    entered.set()
                    await asyncio.Event().wait()
            self.client.before_chunk = block
            task = asyncio.create_task(parallel_download.download_file(
                self.client, FileId.decode(self.message.video.file_id), len(self.data),
                legacy, self.message.video.file_unique_id, 3, AsyncMock(), AsyncMock(), 2 * MB))
            try:
                await asyncio.wait_for(entered.wait(), 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertGreater(sum(checkpoint_lengths(Path(legacy))), 0)
            self.client.calls.clear()
            self.client.before_chunk = None
            status, output, _ = await downloader.download_media(
                self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
            self.assertIs(status, DownloadStatus.SuccessDownload)
            self.assertEqual(self.node.download_temp_paths[61], legacy)
            self.assertEqual(Path(output).read_bytes(), self.data)
            self.assertNotIn(0, [offset for offset, _ in self.client.calls])
            self.assertFalse(parts_path(legacy).exists())

    async def test_existing_staging_path_resumes_through_real_metadata(self):
        with self.use_real_media_metadata():
            legacy = str(Path(self.app.temp_save_path) / self.message.chat.title / "61 - 飞，一.mov")
            entered = asyncio.Event()
            async def block(index):
                if index >= 1:
                    entered.set()
                    await asyncio.Event().wait()
            self.client.before_chunk = block
            task = asyncio.create_task(parallel_download.download_file(
                self.client, FileId.decode(self.message.video.file_id), len(self.data),
                legacy, self.message.video.file_unique_id, 3, AsyncMock(), AsyncMock(), 2 * MB))
            try:
                await asyncio.wait_for(entered.wait(), 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.client.calls.clear()
            self.client.before_chunk = None
            status, output, _ = await downloader.download_media(
                self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
            self.assertEqual(status, DownloadStatus.SuccessDownload)
            self.assertEqual(self.node.download_temp_paths[61], legacy)
            self.assertEqual(Path(output).read_bytes(), self.data)
            self.assertNotIn(0, [offset for offset, _ in self.client.calls])
            self.assertFalse(parts_path(legacy).exists())

    async def test_media_metadata_includes_selected_size_before_extension(self):
        with self.use_real_media_metadata():
            self.app.file_name_prefix = ["file_name", "file_size"]
            self.app.file_name_prefix_split = " - "
            self.app.get_file_name = lambda *args: Application.get_file_name(self.app, *args)
            output, staging, _ = await downloader._get_media_meta(
                100, self.message, self.message.video, "video")
            self.assertEqual(Path(output).name, "飞，一 - 6.0MB.mov")
            self.app.file_name_prefix = ["file_name"]
            unchanged, same_staging, _ = await downloader._get_media_meta(
                100, self.message, self.message.video, "video")
            self.assertEqual(Path(unchanged).name, "飞，一.mov")
            self.assertEqual(staging, output)
            self.assertEqual(same_staging, unchanged)

    async def test_metadata_identity_without_unique_id_survives_reference_refresh(self):
        with self.use_real_media_metadata():
            self.message.video.file_unique_id = None
            _, first, _ = await downloader._get_media_meta(100, self.message, self.message.video, "video")
            decoded = FileId.decode(self.message.video.file_id)
            decoded.file_reference = b"new-reference"
            self.message.video.file_id = decoded.encode()
            _, second, _ = await downloader._get_media_meta(100, self.message, self.message.video, "video")
            self.assertEqual(first, second)

    async def test_web_cancellation_finds_generated_staging_and_keeps_other_downloads(self):
        with self.use_real_media_metadata():
            entered = asyncio.Event()
            async def block(index):
                if index >= 1:
                    entered.set()
                    await asyncio.Event().wait()
            self.client.before_chunk = block
            other = self.path / "temp" / "other.tmp"
            other.parent.mkdir(parents=True)
            other.write_bytes(b"keep")
            task = asyncio.create_task(downloader.download_media(
                self.client, self.message, self.app.media_types, self.app.file_formats, self.node))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                staging = self.node.download_temp_paths[61]
                cache = self.web_request("/check_file_exists?task_id=100_61")
                self.assertTrue(cache["cache_exists"])
                self.assertIn(staging + ".parts", cache["cache_paths"])
                self.web_request("/delete_task?task_id=100_61&delete_file=true")
                status, _, _ = await asyncio.wait_for(task, 2)
                self.assertEqual(status, DownloadStatus.SkipDownload)
                self.assertFalse(parts_path(staging).exists())
                self.assertEqual(other.read_bytes(), b"keep")
                self.assertEqual(self.client.tracker["active"], 0)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_startup_keeps_generated_checkpoints_without_inferring_output_paths(self):
        from module.staging import get_staging_path
        root = self.path / "temp"
        root.mkdir()
        self.app.temp_save_path = str(root)
        staging = get_staging_path(str(root), "main", 100, 61, "media", 1792119777, "")
        generated = Path(staging + ".temp")
        generated.write_bytes(b"resume")
        legacy = root / "legacy.mov.temp"
        legacy.write_bytes(b"old")
        downloads = self.path / "configured-downloads"
        downloads.mkdir()
        self.app.save_path = str(downloads)
        (downloads / Path(staging).name).write_bytes(b"finished")
        (downloads / "legacy.mov").write_bytes(b"finished")
        downloader._cleanup_stale_temp_files()
        self.assertEqual(generated.read_bytes(), b"resume")
        self.assertFalse(legacy.exists())

    async def test_entry_point_downloads_and_records_only_one_aggregate_ui_entry(self):
        counts = []
        async def before(index):
            counts.append(download_stat.get_active_download_streams('100_61'))
        self.client.before_chunk = before
        await downloader.download_task(self.client, self.message, self.node)
        self.assertEqual(max(counts), 3)
        self.assertEqual(download_stat.get_active_download_streams(), 0)
        self.assertEqual((self.path / "saved.mp4").read_bytes(), self.data)
        self.assertEqual(self.node.success_download_task, 1)
        self.assertEqual(self.node.download_status[61], DownloadStatus.SuccessDownload)
        self.assertEqual(len(self.client.calls), 4)
        self.assertEqual(list(self.result), [100])
        self.assertEqual(list(self.result[100]), [61])
        self.assertEqual(self.result[100][61]["down_byte"], len(self.data))
        self.assertEqual(self.result[100][61]["progress"], 100.0)
        self.client.download_media.assert_not_called()

    async def test_existing_file_is_skipped_without_new_traffic(self):
        output = self.path / "saved.mp4"
        output.write_bytes(self.data)
        self.node.task_id_display = "1019-1"
        self.node.source_chat_title = "Existing media"
        for placeholder in (False, True, "restored"):
            with self.subTest(placeholder=placeholder):
                self.result.clear()
                if placeholder == "restored":
                    self.result["100"] = {"61": {
                        "down_byte": len(self.data), "total_size": len(self.data),
                        "file_name": str(output), "task_id": "1",
                    }}
                elif placeholder:
                    await download_stat.update_download_status(
                        0, 1, 61, str(output), 10, self.node, self.client, initial=True)
                with patch.object(download_stat, "_total_download_size", 123):
                    await downloader.download_task(self.client, self.message, self.node)
                    self.assertEqual(download_stat._total_download_size, 123)
                service = DownloadService(self.app)
                self.assertEqual(service.get_download_list(already_down="false"), [])
                self.assertEqual(service.get_completed_count(), {"total": 0})
                self.assertEqual(service.get_download_list(already_down="true")["tasks"], [])
                rows = service.web_get_skipped_downloads()["tasks"]
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["reason"], "already download,download skipped")
                self.assertEqual(row["file_name"], output.name)
                self.assertEqual(row["task_id_display"], "1019-1")
                self.assertEqual(row["chat_title"], "Existing media")
                self.assertEqual(self.node.download_status[61], DownloadStatus.SkipDownload)
                self.app.set_download_id.assert_called_with(self.node, 61, DownloadStatus.SkipDownload)
                self.assertEqual(self.node.success_download_task, 0)
                self.assertEqual(self.node.total_download_byte, 0)
                download_stat.save_downloads.assert_called()
        self.assertEqual(output.read_bytes(), self.data)
        self.assertFalse(self.client.calls)
        self.client.download_media.assert_not_awaited()
        self.app.upload_file.assert_not_awaited()

    async def test_manual_download_bypasses_expression_type_and_format_filters(self):
        self.node.bypass_filters = True
        self.node.download_filter = "media_file_size > 100GB"
        self.app.media_types = ["photo"]
        self.app.file_formats = {"video": ["mkv"]}
        await downloader.download_task(self.client, self.message, self.node)
        self.assertEqual((self.path / "saved.mp4").read_bytes(), self.data)
        self.assertEqual(self.node.download_status[61], DownloadStatus.SuccessDownload)
        self.client.download_media.assert_not_awaited()

    async def test_filtered_missing_media_and_cancelled_skips_are_not_completed(self):
        for reason in ("filter", "no-media", "cancel"):
            with self.subTest(reason=reason):
                self.result.clear()
                await download_stat.update_download_status(
                    0, 1, 61, "pending", 10, self.node, self.client, initial=True)
                message = self.media_message(b"reference")
                formats = {"video": ["mkv"]} if reason == "filter" else {"video": ["all"]}
                if reason == "no-media":
                    message.video = None
                    message.media = None
                with patch.object(self.app, "file_formats", formats), patch.object(
                        downloader, "_download_parallel_media", AsyncMock(side_effect=pyrogram.StopTransmission)):
                    await downloader.download_task(self.client, message, self.node)
                self.assertFalse(self.result)
                self.assertEqual(DownloadService(self.app).get_completed_count(), {"total": 0})

    async def test_complete_serial_file_found_after_transfer_error_is_skipped(self):
        del self.app.file_download_concurrency
        for location in ("partial.mp4", "saved.mp4"):
            with self.subTest(location=location):
                self.result.clear()
                async def finish_then_raise(*args, **kwargs):
                    (self.path / location).write_bytes(self.data)
                    raise parallel_download.DownloadWorkersExhausted("Transfer failed after writing")
                with patch.object(self.client, "download_media", AsyncMock(side_effect=finish_then_raise)):
                    await downloader.download_task(self.client, self.message, self.node)
                service = DownloadService(self.app)
                self.assertEqual(service.get_completed_count(), {"total": 0})
                row = service.web_get_skipped_downloads()["tasks"][0]
                self.assertEqual(row["reason"], "already download,download skipped")
                self.assertEqual(row["file_name"], "saved.mp4")
                self.assertEqual((self.path / "saved.mp4").read_bytes(), self.data)
                (self.path / "saved.mp4").unlink()

    async def test_existing_text_file_is_recorded_as_skipped(self):
        self.message.video = None
        self.message.media = None
        self.message.text = "Saved message"
        self.app.enable_download_txt = True
        self.app.temp_save_path = str(self.path)
        self.app.date_format = ""
        self.app.get_file_save_path = Mock(return_value="")
        self.app.get_file_name = Mock(return_value="saved")
        output = self.path / "saved.txt"
        output.write_text(self.message.text)
        await downloader.download_task(self.client, self.message, self.node)
        service = DownloadService(self.app)
        self.assertEqual(service.get_completed_count(), {"total": 0})
        row = service.web_get_skipped_downloads()["tasks"][0]
        self.assertEqual(row["file_name"], output.name)
        self.assertEqual(row["reason"], "already download,download skipped")
        self.assertEqual(output.read_text(), self.message.text)

    async def test_new_progress_clears_previous_skip_reason(self):
        await download_stat.update_download_status(
            100, 100, 61, "saved.mp4", 10, self.node, self.client)
        self.result[100][61]["completion_reason"] = "already download,download skipped"
        await download_stat.update_download_status(
            0, 100, 61, "saved.mp4", 10, self.node, self.client, initial=True)
        self.assertNotIn("completion_reason", self.result[100][61])

    async def test_repeated_flood_waits_do_not_exhaust_attempts(self):
        for error in (pyrogram.errors.FloodWait, pyrogram.errors.FloodPremiumWait):
            with self.subTest(error=error):
                download = AsyncMock(side_effect=[error(900) for _ in range(4)] +
                                     [str(self.path / "partial.mp4")])
                async def complete(*args, **kwargs):
                    result = await download()
                    Path(result).write_bytes(self.data)
                    return result
                with patch.object(downloader, "_download_parallel_media", complete), patch.object(
                        downloader, "_wait_download_flood", AsyncMock(return_value=True)) as wait:
                    status, path, reason = await downloader.download_media(
                        self.client, self.message, self.app.media_types,
                        self.app.file_formats, self.node)
                self.assertEqual(status, DownloadStatus.SuccessDownload)
                self.assertEqual(wait.await_count, 4)
                self.assertTrue(all(call.args[0] == 900 for call in wait.await_args_list))
                Path(path).unlink()

    async def test_flood_wait_publishes_cooldown_and_honors_cancellation(self):
        self.node.is_stop_transmission = True
        result = await downloader._wait_download_flood(900, 61, self.node, self.client)
        self.assertFalse(result)
        self.assertGreater(pyrogram_extension._unified_flood_wait["until"],
                           downloader.time.time() + 899)
        self.assertEqual(pyrogram_extension._unified_flood_wait["reason"], "FLOOD_WAIT")

    async def test_manual_pause_survives_flood_cooldown_expiry(self):
        clock = SimpleNamespace(time=downloader.time.time,
                                monotonic=Mock(side_effect=[0, 1000]))
        download_stat.pause_task("100_61")
        with patch.object(downloader, "time", clock), patch.object(
                pyrogram_extension, "get_flood_wait_remaining", return_value=0):
            task = asyncio.create_task(downloader._wait_download_flood(
                900, 61, self.node, self.client))
            try:
                for _ in range(20):
                    await asyncio.sleep(0)
                    if download_stat.is_task_paused("100_61"):
                        break
                self.assertTrue(download_stat.is_task_paused("100_61"))
                self.assertFalse(download_stat.is_task_paused(self.node.task_id))
                self.assertFalse(task.done())
                self.assertTrue(download_stat.resume_task("100_61"))
                self.assertTrue(await asyncio.wait_for(task, 2))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_600_second_flood_wait_does_not_require_manual_resume(self):
        clock = SimpleNamespace(time=downloader.time.time,
                                monotonic=Mock(side_effect=[0, 601]))
        with patch.object(downloader, "time", clock), patch.object(
                pyrogram_extension, "get_flood_wait_remaining", return_value=0):
            self.assertTrue(await downloader._wait_download_flood(
                600, 61, self.node, self.client))
        self.assertFalse(download_stat.is_task_paused("100_61"))

    async def test_long_flood_wait_automatically_resumes(self):
        clock = SimpleNamespace(time=downloader.time.time,
                                monotonic=Mock(side_effect=[0, 1000]))
        with patch.object(downloader, "time", clock), patch.object(
                pyrogram_extension, "get_flood_wait_remaining", return_value=0):
            self.assertTrue(await downloader._wait_download_flood(
                900, 61, self.node, self.client))
        self.assertFalse(download_stat.is_task_paused("100_61"))

    async def test_real_pool_resumes_after_long_shared_cooldown_without_manual_action(self):
        now = [0]
        clock = SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0])
        failed = False

        async def before(_):
            nonlocal failed
            if not failed:
                failed = True
                raise pyrogram.errors.FloodWait(900)

        self.client.before_chunk = before
        with patch.object(downloader, "time", clock), patch.object(
                pyrogram_extension, "time", clock), patch.object(parallel_download, "time", clock):
            task = asyncio.create_task(downloader._download_parallel_media(
                self.client, self.message, str(self.path / "partial.mp4"), len(self.data),
                self.node, "partial.mp4", 0, 3, 2 * MB))
            try:
                for _ in range(50):
                    await asyncio.sleep(0.001)
                    if pyrogram_extension._unified_flood_wait["until"]:
                        break
                self.assertEqual(pyrogram_extension._unified_flood_wait["until"], 905)
                self.assertFalse(download_stat.is_task_paused("100_61"))
                now[0] = 904
                await asyncio.sleep(0.01)
                self.assertEqual(len(self.client.calls), 1)
                self.assertFalse(task.done())
                now[0] = 905
                path = await asyncio.wait_for(task, 2)
                self.assertEqual(Path(path).read_bytes(), self.data)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_pool_exhaustion_does_not_restart_file_or_mislabel_bad_request(self):
        async def before(_):
            raise pyrogram.errors.AuthBytesInvalid()
        self.client.before_chunk = before
        status, _, reason = await downloader.download_media(
            self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
        self.assertEqual(status, DownloadStatus.FailedDownload)
        self.assertEqual(len(self.client.calls), 5)
        self.assertIn("AuthBytesInvalid", reason)
        self.assertNotIn("File reference expired", reason)
        self.owner.get_messages.assert_not_awaited()
        downloader._maybe_reconnect_client.assert_not_awaited()

    async def test_upload_flood_cannot_shorten_existing_download_cooldown(self):
        downloader._publish_download_flood(900)
        deadline = pyrogram_extension._unified_flood_wait["until"]
        with patch.object(pyrogram_extension, "_upload_telegram_chat_message", AsyncMock(
                side_effect=[pyrogram.errors.FloodWait(2), ForwardStatus.SuccessForward])):
            result = await pyrogram_extension.upload_telegram_chat_message(
                self.client, self.client, self.app, self.node, self.message, "offline.mp4")
        self.assertEqual(result, ForwardStatus.SuccessForward)
        self.assertEqual(pyrogram_extension._unified_flood_wait["until"], deadline)

    async def test_reference_refresh_rejects_replaced_media_without_mixing_parts(self):
        self.app.file_download_concurrency = 1
        self.owner.get_messages.return_value.video.file_unique_id = "replacement"

        async def before(index):
            if index == 1:
                raise pyrogram.errors.FileReferenceExpired()
        self.client.before_chunk = before
        status, _, reason = await downloader.download_media(
            self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
        self.assertEqual(status, DownloadStatus.FailedDownload)
        self.assertIn("different file", reason)
        self.assertFalse((self.path / "saved.mp4").exists())
        self.assertEqual(checkpoint_lengths(self.path / "partial.mp4")[0], MB)

    async def test_small_file_uses_one_range_and_no_part_files(self):
        self.app.download_part_size_mb = 16
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
            self.app.download_part_size_mb = 100
        self.client.before_chunk = before
        await downloader.download_task(self.client, self.message, self.node)
        self.assertEqual(len(self.client.calls), 4)
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

    async def start_blocked_download(self):
        entered = asyncio.Event()

        async def block(index):
            if index >= 1:
                if index == 1:
                    entered.set()
                await asyncio.Event().wait()

        self.client.before_chunk = block
        task = asyncio.create_task(downloader.download_media(
            self.client, self.message, self.app.media_types, self.app.file_formats, self.node))
        await asyncio.wait_for(entered.wait(), 2)
        directory = parts_path(str(self.path / "partial.mp4"))
        self.assertEqual((self.path / "partial.mp4.temp").stat().st_size, len(self.data))
        return task

    def web_request(self, url, **kwargs):
        self.app.web_login_secret = ""
        with patch.object(web, "_app", self.app):
            response = web.get_flask_app().test_client().post(url, **kwargs)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["code"], "1")
        return response.get_json()

    def assert_cancelled_temp_removed(self):
        self.assertFalse(parts_path(str(self.path / "partial.mp4")).exists())
        self.assertFalse((self.path / "partial.mp4.temp").exists())
        self.assertFalse((self.path / "partial.mp4").exists())
        self.assertEqual(self.client.tracker["active"], 0)

    async def test_web_cancel_without_file_deletion_preserves_resumable_parts(self):
        task = await self.start_blocked_download()
        self.web_request("/delete_task?task_id=100_61&delete_file=false")
        status, _, _ = await asyncio.wait_for(task, 2)
        self.assertEqual(status, DownloadStatus.SkipDownload)
        directory = parts_path(str(self.path / "partial.mp4"))
        self.assertTrue((directory / "manifest.json").exists())
        self.assertGreater(sum(checkpoint_lengths(self.path / "partial.mp4")), 0)
        self.assertEqual(self.client.tracker["active"], 0)
        # Retained data must still produce the original bytes on a new attempt.
        self.node = TaskNode(chat_id=100, task_id=2, limit=1)
        self.client.before_chunk = None
        status, path, _ = await downloader.download_media(
            self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
        self.assertEqual(status, DownloadStatus.SuccessDownload)
        self.assertEqual(Path(path).read_bytes(), self.data)

    async def test_webpage_delete_cleans_temp_and_same_message_can_be_downloaded_again(self):
        completed = self.path / "other-completed.mp4"
        completed.write_bytes(b"keep completed download")
        task = await self.start_blocked_download()
        self.web_request("/delete_task?task_id=100_61&delete_file=true")
        status, _, _ = await asyncio.wait_for(task, 2)
        self.assertEqual(status, DownloadStatus.SkipDownload)
        self.assert_cancelled_temp_removed()
        self.assertEqual(completed.read_bytes(), b"keep completed download")
        self.node = TaskNode(chat_id=100, task_id=2, limit=1)
        self.client.before_chunk = None
        status, path, _ = await downloader.download_media(
            self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
        self.assertEqual(status, DownloadStatus.SuccessDownload)
        self.assertEqual(Path(path).read_bytes(), self.data)

    async def test_web_api_delete_by_numeric_task_id_cleans_paused_temp(self):
        task = await self.start_blocked_download()
        download_stat.pause_task(self.node.task_id)
        self.web_request(f"/delete_task?task_id={self.node.task_id}&delete_file=true")
        status, _, _ = await asyncio.wait_for(task, 2)
        self.assertEqual(status, DownloadStatus.SkipDownload)
        self.assert_cancelled_temp_removed()

    async def test_batch_web_api_delete_cleans_temp(self):
        task = await self.start_blocked_download()
        result = self.web_request("/batch_delete", json={"task_ids": ["100_61"], "delete_file": True})
        self.assertEqual(result["deleted"], 1)
        status, _, _ = await asyncio.wait_for(task, 2)
        self.assertEqual(status, DownloadStatus.SkipDownload)
        self.assert_cancelled_temp_removed()

    async def test_deleting_one_file_does_not_cancel_sibling_file_in_same_task(self):
        blocked = asyncio.Event()
        release = asyncio.Event()

        async def meta(chat_id, message, media, kind, from_user_id=None):
            return str(self.path / f"saved-{message.id}.mp4"), str(self.path / f"partial-{message.id}.mp4"), "mp4"

        async def before(index):
            if index == 1:
                blocked.set()
                await release.wait()

        second = self.media_message(b"reference")
        second.id = 62
        self.client.before_chunk = before
        with patch.object(downloader, "_get_media_meta", AsyncMock(side_effect=meta)):
            tasks = [asyncio.create_task(downloader.download_media(
                self.client, message, self.app.media_types, self.app.file_formats, self.node))
                for message in (self.message, second)]
            try:
                await asyncio.wait_for(blocked.wait(), 2)
                while len(self.result.get(100, {})) < 2:
                    await asyncio.sleep(0)
                self.web_request("/delete_task?task_id=100_61&delete_file=true")
                cancelled, _, _ = await asyncio.wait_for(tasks[0], 2)
                self.assertEqual(cancelled, DownloadStatus.SkipDownload)
                self.assertFalse(tasks[1].done())
                self.assertFalse(parts_path(str(self.path / "partial-61.mp4")).exists())
                self.assertTrue(parts_path(str(self.path / "partial-62.mp4")).exists())
            finally:
                release.set()
                results = await asyncio.gather(*tasks)
        self.assertEqual(results[1][0], DownloadStatus.SuccessDownload)
        self.assertEqual((self.path / "saved-62.mp4").read_bytes(), self.data)

    async def test_legacy_serial_cancel_removes_nonempty_temp_without_retry(self):
        del self.app.file_download_concurrency

        async def serial(*args, **kwargs):
            self.assertEqual(download_stat.get_active_download_streams('100_61'), 1)
            (self.path / "partial.mp4.temp").write_bytes(b"partial data")
            await download_stat.update_download_status(
                MB, len(self.data), self.message.id, str(self.path / "saved.mp4"),
                0, self.node, self.client)
            self.web_request("/delete_task?task_id=100_61&delete_file=true")
            return None

        self.client.download_media.side_effect = serial
        status, _, _ = await downloader.download_media(
            self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
        self.assertEqual(status, DownloadStatus.SkipDownload)
        self.client.download_media.assert_awaited_once()
        self.assertEqual(download_stat.get_active_download_streams(), 0)
        self.assert_cancelled_temp_removed()

    async def cancel_two_files(self, numeric):
        release = asyncio.Event()

        async def before(index):
            if index == 1:
                await release.wait()

        async def meta(chat_id, message, media, kind, from_user_id=None):
            return str(self.path / f"saved-{message.id}.mp4"), str(self.path / f"partial-{message.id}.mp4"), "mp4"

        self.client.before_chunk = before
        second = self.media_message(b"reference")
        second.id = 62
        with patch.object(downloader, "_get_media_meta", AsyncMock(side_effect=meta)):
            tasks = [asyncio.create_task(downloader.download_media(
                self.client, message, self.app.media_types, self.app.file_formats, self.node))
                for message in (self.message, second)]
            try:
                async def entries_ready():
                    while len(self.result.get(100, {})) < 2:
                        await asyncio.sleep(0)
                await asyncio.wait_for(entries_ready(), 2)
                if numeric:
                    self.web_request(f"/delete_task?task_id={self.node.task_id}&delete_file=true")
                else:
                    result = self.web_request("/batch_delete", json={"task_ids": ["100_61", "100_62"], "delete_file": True})
                    self.assertEqual(result["deleted"], 2)
                results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(result[0] == DownloadStatus.SkipDownload for result in results))
        self.assertEqual(self.result, {})
        self.assertEqual(self.client.tracker["active"], 0)
        for message_id in (61, 62):
            self.assertFalse(parts_path(str(self.path / f"partial-{message_id}.mp4")).exists())

    async def test_numeric_web_api_delete_cancels_all_files_in_task(self):
        await self.cancel_two_files(numeric=True)

    async def test_batch_web_api_delete_cleans_multiple_files(self):
        await self.cancel_two_files(numeric=False)

    async def test_delete_before_final_publication_removes_staged_output(self):
        real_ready = download_stat.wait_download_ready
        deleted = False

        async def ready(message_id, node, client):
            nonlocal deleted
            if not deleted and (self.path / "partial.mp4").exists():
                deleted = True
                self.web_request("/delete_task?task_id=100_61&delete_file=true")
            await real_ready(message_id, node, client)

        with patch.object(download_stat, "wait_download_ready", ready):
            status, _, _ = await downloader.download_media(
                self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
        self.assertTrue(deleted)
        self.assertEqual(status, DownloadStatus.SkipDownload)
        self.assert_cancelled_temp_removed()
        self.assertFalse((self.path / "saved.mp4").exists())

    async def test_bot_cancel_callback_cleans_temp_for_one_task_and_all_tasks(self):
        for selected in (str(self.node.task_id), "all"):
            with self.subTest(selected=selected):
                self.node.is_stop_transmission = False
                self.node.discard_partial = False
                task = await self.start_blocked_download()
                controller = bot.DownloadBot()
                controller.task_node[self.node.task_id] = self.node
                query = SimpleNamespace(data=f"stop_download task {selected}",
                                        message=SimpleNamespace(id=9, from_user=SimpleNamespace(id=10)))
                gateway = SimpleNamespace(edit_message_text=AsyncMock())
                with patch.object(bot, "_bot", controller):
                    await bot.stop_task(gateway, query, "stop_download", TaskType.Download)
                status, _, _ = await asyncio.wait_for(task, 2)
                self.assertEqual(status, DownloadStatus.SkipDownload)
                self.assert_cancelled_temp_removed()

    async def test_bot_shutdown_stop_preserves_checkpoint_for_restart(self):
        task = await self.start_blocked_download()
        controller = bot.DownloadBot()
        controller.task_node[self.node.task_id] = self.node
        controller.stop_task("all", discard_partial=False)
        status, _, _ = await asyncio.wait_for(task, 2)
        self.assertEqual(status, DownloadStatus.SkipDownload)
        self.assertTrue(parts_path(str(self.path / "partial.mp4")).exists())
        self.assertEqual(self.client.tracker["active"], 0)

    async def test_delete_during_preallocation_cleans_checkpoint_and_data(self):
        real_ready = download_stat.wait_download_ready
        deleted = False

        async def ready(message_id, node, client):
            nonlocal deleted
            scratch = self.path / "partial.mp4.temp"
            if (not deleted and scratch.exists() and scratch.stat().st_size > 0
                    and self.result.get(100, {}).get(61)):
                deleted = True
                self.web_request("/delete_task?task_id=100_61&delete_file=true")
            await real_ready(message_id, node, client)

        with patch.object(download_stat, "wait_download_ready", ready):
            status, _, _ = await downloader.download_media(
                self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
        self.assertTrue(deleted)
        self.assertEqual(status, DownloadStatus.SkipDownload)
        self.assert_cancelled_temp_removed()

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
        saved = sum(checkpoint_lengths(self.path / "partial.mp4"))
        self.assertGreater(saved, 0)
        self.result.clear()  # A new process has no previous in-memory statistics.
        download_stat._total_download_size = 0
        self.client.calls.clear()
        self.app.file_download_concurrency = 1
        self.app.download_part_size_mb = 100
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
            self.assertEqual(download_stat._total_download_size, 0)
            with web._flask_app.test_request_context("/get_download_list?already_down=false"):
                response = web.get_download_list()
                data = response.get_json() if hasattr(response, "get_json") else json.loads(response)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["download_progress_raw"], round(saved / len(self.data) * 100, 1))
        finally:
            release.set()
            await task
        self.assertEqual((self.path / "saved.mp4").read_bytes(), self.data)

    async def test_published_recovery_persists_history_without_counting_old_transfer(self):
        class CallbackInterrupted(Exception):
            pass
        async def progress(current, total):
            if current == total:
                raise CallbackInterrupted()
        with self.assertRaises(CallbackInterrupted):
            await parallel_download.download_file(
                self.client, FileId.decode(self.message.video.file_id), len(self.data),
                str(self.path / "partial.mp4"), self.message.video.file_unique_id,
                3, progress, AsyncMock(), 2 * MB, destination=str(self.path / "saved.mp4"))
        self.client.calls.clear()
        download_stat.save_downloads.reset_mock()
        status, path, _ = await downloader.download_media(
            self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
        self.assertEqual(status, DownloadStatus.SuccessDownload)
        self.assertEqual(Path(path).read_bytes(), self.data)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(download_stat._total_download_size, 0)
        self.assertEqual(self.result[100][61]["download_speed"], 0)
        download_stat.save_downloads.assert_called()

    async def test_storage_failure_fails_once_without_reconnecting_or_marking_complete(self):
        import errno
        from module.partial_file import PartialFile
        self.app.file_download_concurrency = 1
        with patch.object(PartialFile, "write_at", side_effect=OSError(errno.ENOSPC, "disk full")):
            status, output, reason = await downloader.download_media(
                self.client, self.message, self.app.media_types, self.app.file_formats, self.node)
        self.assertEqual(status, DownloadStatus.FailedDownload)
        self.assertIsNone(output)
        self.assertIn("disk full", reason)
        self.assertEqual(len(self.client.calls), 1)
        downloader._maybe_reconnect_client.assert_not_awaited()
        self.assertEqual(pyrogram_extension._unified_flood_wait["until"], 0)
        self.assertFalse((self.path / "saved.mp4").exists())
        self.assertLess(self.result[100][61]["down_byte"], len(self.data))

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
        with patch("module.download_connection._DownloadClient.get_session", AsyncMock(return_value=session)):
            await downloader.download_task(client, self.message, self.node)
        self.assertEqual((self.path / "saved.mp4").read_bytes(), self.data)
        self.assertGreater(tracker["peak"], 1)
        self.assertEqual(client.get_file_semaphore._value, 4)
        self.assertTrue(all(request.offset % MB == 0 and request.limit == MB
                            for request in requests))
        self.owner.get_messages.assert_awaited_once_with(chat_id=100, message_ids=61)
        self.assertEqual(self.node.success_download_task, 1)


    async def test_durable_pause_closes_real_part_workers_and_resumes_saved_ranges(self):
        import hashlib
        self.app.is_running = True
        self.app.loop = asyncio.get_running_loop()
        controller = bot.DownloadBot()
        controller.app = self.app
        controller.client = self.client
        controller.add_download_task = downloader.add_download_task
        self.client.get_messages = AsyncMock(return_value=self.message)
        entered = asyncio.Event()
        async def before(index):
            if index >= 1:
                entered.set()
                await asyncio.Event().wait()
        self.client.before_chunk = before
        queue = downloader._DownloadQueue(self.client)
        with patch.object(bot, '_bot', controller), \
                patch.object(task_store, '_TASKS_FILE', str(self.path / 'tasks.json')), \
                patch.object(task_store, '_COUNTER_FILE', str(self.path / 'counter.json')), \
                patch.object(download_control, '_attempts', {}), \
                patch.object(download_control, '_queued', {}), \
                patch.object(download_control, '_locks', {}), \
                patch.object(downloader, 'queue', queue), \
                patch.object(downloader, '_live_workers', 0):
            try:
                self.node.total_task = 0
                await downloader.add_download_task(self.message, self.node)
                await asyncio.wait_for(entered.wait(), 3)
                result = await asyncio.to_thread(DownloadService(self.app).web_pause_task, '100_61')
                self.assertEqual(result['code'], '1')
                self.assertEqual(self.client.tracker['active'], 0)
                self.assertEqual(controller._active_downloads, 0)
                checkpoint = json.loads((parts_path(str(self.path / 'partial.mp4')) / 'manifest.json').read_text())
                self.assertGreater(sum(checkpoint['committed_bytes']), 0)
                self.assertFalse((self.path / 'saved.mp4').exists())
                await controller.recover_tasks()
                self.assertEqual(task_store.get_pending_tasks(), [])
                result = await asyncio.to_thread(DownloadService(self.app).web_resume_task, '100_61')
                self.assertEqual(result['code'], '1')
                self.assertTrue(queue.empty())
                self.client.calls.clear()
                self.client.before_chunk = None
                # Changes to settings cannot repartition the saved plan.
                self.app.file_download_concurrency = 1
                self.app.download_part_size_mb = 64
                await bot._consume_one_pending()
                queue.ensure_workers()
                await asyncio.wait_for(asyncio.gather(*queue.workers), 5)
                output = (self.path / 'saved.mp4').read_bytes()
                self.assertEqual(hashlib.sha256(output).digest(), hashlib.sha256(self.data).digest())
                self.assertTrue(all(offset > 0 for offset, _ in self.client.calls))
                self.assertFalse(parts_path(str(self.path / 'partial.mp4')).exists())
            finally:
                self.app.is_running = False
                await queue.stop()


class DownloadConfigTests(unittest.TestCase):
    def setUp(self):
        self.app = Application("unused-config.yaml", "unused-data.json")
        self.addCleanup(self.app.loop.close)
        self.addCleanup(self.app.executor.shutdown)
        self.base = {"api_id": "test", "api_hash": "test", "media_types": [],
                     "file_formats": {}}

    def test_defaults_keep_one_range_and_existing_worker_count(self):
        self.app.assign_config({**self.base, "max_download_task": 3})
        self.assertEqual(self.app.file_download_concurrency, 1)
        self.assertEqual(self.app.download_part_size_mb, 64)
        self.assertFalse(hasattr(self.app, "download_concurrency"))

    def test_removed_global_setting_is_ignored_without_rewriting_config(self):
        self.app.config = {**self.base, "download_concurrency": 1}
        self.app.assign_config(self.app.config)
        self.app.save_app_data(False)
        self.assertEqual(self.app.config["download_concurrency"], 1)
        self.assertFalse(hasattr(self.app, "download_concurrency"))

    def test_runtime_saves_do_not_copy_download_settings_into_config(self):
        self.app.assign_config({**self.base, "max_download_task": 3,
                                "file_download_concurrency": 12,
                                "download_part_size_mb": 8})
        self.app.save_app_data(False)
        self.assertEqual(self.app.file_download_concurrency, 12)
        self.assertEqual(self.app.download_part_size_mb, 8)
        self.assertNotIn("file_download_concurrency", self.app.config)
        self.assertNotIn("download_part_size_mb", self.app.config)
        self.assertEqual(self.app.max_concurrent_transmissions, 36)

    def test_legacy_minimum_is_read_without_rewriting_config(self):
        self.app.config = {**self.base, "download_min_part_size_mb": 16}
        self.app.assign_config(self.app.config)
        self.app.save_app_data(False)
        self.assertEqual(self.app.download_part_size_mb, 16)
        self.assertEqual(self.app.config["download_min_part_size_mb"], 16)
        self.assertNotIn("download_part_size_mb", self.app.config)

    def test_invalid_concurrency_and_part_size_are_rejected(self):
        for key in ("file_download_concurrency", "download_part_size_mb"):
            for value in (0, -1, True, 1.5, "4", None):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.app.assign_config({**self.base, key: value})


if __name__ == "__main__":
    unittest.main()
