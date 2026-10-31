"""Requester directory fields use task ownership rather than media authorship."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import media_downloader as downloader
from module.app import Application, DownloadStatus


class DownloadPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = Application.__new__(Application)
        self.app.save_path = str(self.root / "downloads")
        self.app.temp_save_path = str(self.root / "downloads/.temp")
        self.app.file_path_prefix = ["from_user_id", "chat_title", "media_datetime"]
        self.app.date_format = "%Y_%m"
        self.app.file_name_prefix = ["file_name"]
        self.app.file_name_prefix_split = " - "
        self.app.get_caption_name = lambda *args: None
        self.message = SimpleNamespace(
            id=61, chat=SimpleNamespace(id=-10001, title="Source channel"),
            date=datetime(2026, 10, 22), caption=None, media_group_id=None,
            photo=None, from_user=SimpleNamespace(id=999), text="Saved text",
            _client=SimpleNamespace(name="download-account"),
        )

    def test_directory_order_and_optional_field_preserve_existing_layout(self):
        self.assertEqual(self.app.get_file_save_path("video", "channel", "2026_10", 123),
                         str(self.root / "downloads/123/channel/2026_10"))
        self.app.file_path_prefix = ["media_type", "from_user_id", "chat_title"]
        self.assertEqual(self.app.get_file_save_path("video", "channel", "2026_10", "456"),
                         str(self.root / "downloads/video/456/channel"))
        self.app.file_path_prefix = ["chat_title", "media_datetime"]
        self.assertEqual(self.app.get_file_save_path("video", "channel", "2026_10", 123),
                         str(self.root / "downloads/channel/2026_10"))

    def test_missing_or_invalid_requester_cannot_become_a_path(self):
        for requester in (None, "", 0, "0", -10001, "../escape", "/absolute", "@username"):
            with self.subTest(requester=requester):
                self.assertEqual(self.app.get_file_save_path("video", "channel", "2026_10", requester),
                                 str(self.root / "downloads/shared/channel/2026_10"))

    async def test_media_paths_use_requester_for_all_naming_branches(self):
        media = SimpleNamespace(mime_type="video/mp4", file_name="record.mp4",
                                file_unique_id="stable-media", date=self.message.date)
        for kind in ("video", "photo", "voice", "video_note"):
            for requester in (123, "456", None):
                with self.subTest(kind=kind, requester=requester), patch.object(downloader, "app", self.app):
                    output, staging, _ = await downloader._get_media_meta(
                        -10001, self.message, media, kind, from_user_id=requester)
                    directory = str(requester) if requester else "shared"
                    self.assertEqual(Path(output).parent,
                                     self.root / "downloads" / directory / "Source channel/2026_10")
                    self.assertEqual(staging, output)
                    self.assertEqual(Path(staging + '.temp'), Path(str(output) + '.temp'))

    async def test_text_files_use_requester_and_shared_fallback(self):
        for requester in (123, "456", None):
            with self.subTest(requester=requester):
                status, output = await downloader.save_msg_to_file(
                    self.app, -10001, self.message, requester)
                directory = str(requester) if requester else "shared"
                expected = self.root / "downloads" / directory / "Source channel/2026_10/61.txt"
                self.assertIs(status, DownloadStatus.SuccessDownload)
                self.assertEqual(Path(output), expected)
                self.assertEqual(expected.read_text(), "Saved text")

    async def test_long_unicode_name_is_truncated_before_appending_cache_suffixes(self):
        media = SimpleNamespace(mime_type='video/mp4', file_name='视频' * 100 + '.mp4',
                                file_unique_id='stable-media', date=self.message.date)
        with patch.object(downloader, 'app', self.app):
            output, staging, _ = await downloader._get_media_meta(
                -10001, self.message, media, 'video', from_user_id=123)
        self.assertEqual(staging, output)
        self.assertLessEqual(len(Path(output).name.encode('utf-8')), 230)
        self.assertTrue(output.endswith('.mp4'))
        for suffix in ('.temp', '.parts'):
            path = Path(staging + suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.mkdir() if suffix == '.parts' else path.write_bytes(b'cache')
            self.assertTrue(path.exists())
