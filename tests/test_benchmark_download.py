"""Test the local benchmark pipeline without network access or real credentials."""

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from module.telegram import pyrogram
from pyrogram.file_id import FileId, FileType

from tools.benchmark_download import benchmark, message_target, prepare_session, resume_download
from test_parallel_download import FakeClient, MB, Stopped


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_tool_records_checkpoint_and_fetches_only_remaining_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            data = b"".join(bytes([i]) * MB for i in range(6)) + b"tail"
            file_id = FileId(file_type=FileType.DOCUMENT, dc_id=2, media_id=123,
                             access_hash=456).encode()
            client = FakeClient(data)
            client.get_messages = AsyncMock(return_value=SimpleNamespace(
                document=SimpleNamespace(file_id=file_id, file_size=len(data),
                                         file_unique_id="offline")))
            output = Path(directory) / "resume.bin"
            original = Path(directory) / "original.bin"
            original.write_bytes(data)
            with redirect_stdout(io.StringIO()), patch("tools.benchmark_download.os._exit", side_effect=Stopped), self.assertRaises(Stopped):
                await resume_download(client, "me", 42, {"download_part_size_mb": 1},
                                      output, 3, original, interrupt_after_mib=1)
            partial = json.loads(Path(str(output) + ".resume.json").read_text())
            self.assertTrue(partial["deliberately_interrupted"])
            self.assertFalse(partial["completed"])
            with redirect_stdout(io.StringIO()):
                report = await resume_download(client, "me", 42,
                                               {"download_part_size_mb": 100},
                                               output, 1, original)
            self.assertTrue(report["completed"])
            self.assertTrue(report["hash_matches"])
            self.assertGreaterEqual(report["resumed_from_bytes"], MB)
            self.assertEqual(len(report["saved_ranges"]), 7)
            self.assertEqual(report["request_offsets_mib"], report["expected_resume_offsets_mib"])
            self.assertEqual(output.read_bytes(), data)

    async def test_two_rounds_use_real_engine_and_save_matching_hashes_and_timings(self):
        with tempfile.TemporaryDirectory() as directory:
            data = b"".join(bytes([i]) * MB for i in range(4)) + b"tail"
            file_id = FileId(file_type=FileType.DOCUMENT, dc_id=2, media_id=123,
                             access_hash=456, file_reference=b"offline").encode()
            media = SimpleNamespace(file_id=file_id, file_size=len(data),
                                    file_unique_id="offline")
            client = FakeClient(data)
            client.get_messages = AsyncMock(return_value=SimpleNamespace(document=media))
            config = {"download_part_size_mb": 1}
            run_dir = Path(directory) / "run"
            with redirect_stdout(io.StringIO()):
                report = await benchmark(client, "me", 42, [1, 4], config, run_dir)
            self.assertTrue(report["completed"])
            self.assertTrue(report["hashes_match"])
            self.assertEqual([row["actual_workers"] for row in report["runs"]], [1, 4])
            expected = hashlib.sha256(data).hexdigest()
            self.assertTrue(all(row["sha256"] == expected for row in report["runs"]))
            self.assertTrue(all(row["total_seconds"] >= row["transfer_seconds"]
                                for row in report["runs"]))
            self.assertEqual(json.loads((run_dir / "results.json").read_text()), report)
            self.assertEqual(client.get_messages.await_count, 2)
            self.assertGreater(client.tracker["peak"], 1)

    async def test_wrong_expected_hash_does_not_mark_benchmark_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            data = b"x" * MB
            file_id = FileId(file_type=FileType.DOCUMENT, dc_id=2, media_id=123,
                             access_hash=456).encode()
            client = FakeClient(data)
            client.get_messages = AsyncMock(return_value=SimpleNamespace(
                document=SimpleNamespace(file_id=file_id, file_size=len(data),
                                         file_unique_id="offline")))
            expected = Path(directory) / "expected.bin"
            expected.write_bytes(b"different data")
            run_dir = Path(directory) / "run"
            with redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                await benchmark(client, "me", 42, [1],
                                {"download_part_size_mb": 1},
                                run_dir, expected)
            report = json.loads((run_dir / "results.json").read_text())
            self.assertFalse(report["completed"])
            self.assertFalse(report["hashes_match"])


class SessionAndTargetTests(unittest.TestCase):
    def test_session_copy_includes_committed_wal_without_changing_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.session"
            connection = sqlite3.connect(source)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE marker (value TEXT)")
                connection.execute("INSERT INTO marker VALUES ('offline session')")
                connection.commit()
                destination = prepare_session(Path(directory) / "benchmark", source)
                copy = sqlite3.connect(destination)
                try:
                    self.assertEqual(copy.execute("SELECT value FROM marker").fetchone(),
                                     ("offline session",))
                finally:
                    copy.close()
                self.assertEqual(connection.execute("SELECT count(*) FROM marker").fetchone()[0], 1)
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(ValueError):
                    prepare_session(Path(directory) / "benchmark", source)
            finally:
                connection.close()

    def test_private_public_and_topic_links_select_correct_message(self):
        examples = {
            "https://t.me/channel/123": ("channel", 123),
            "https://t.me/s/channel/123": ("channel", 123),
            "https://t.me/c/1234/567": (-1001234, 567),
            "https://t.me/c/1234/9/567": (-1001234, 567),
        }
        for link, expected in examples.items():
            self.assertEqual(message_target(link), expected)
        with self.assertRaises(ValueError):
            message_target("https://example.com/channel/123")


if __name__ == "__main__":
    unittest.main()
