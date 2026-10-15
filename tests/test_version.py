"""Verify image metadata and real Git source-version fallbacks offline."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import utils.version as version_module


ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for item in (
            patch.dict(os.environ, APP_VERSION=""),
            patch.object(version_module, "__file__", str(self.root / "utils/version.py")),
        ):
            item.start()
            self.addCleanup(item.stop)

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Version Test", "-c", "user.email=test@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.root, stderr=subprocess.DEVNULL, text=True, timeout=10,
        ).strip()

    def init_repo(self):
        self.git("init")
        (self.root / "source.txt").write_text("first\n")
        self.git("add", "source.txt")
        self.git("commit", "-m", "Initial source")

    def test_image_version_wins_without_running_git(self):
        with patch.dict(os.environ, APP_VERSION=" v1.2.3-rc.1 "), \
                patch.object(version_module.subprocess, "check_output") as command:
            self.assertEqual(version_module.get_version(), "v1.2.3-rc.1")
            command.assert_not_called()

    def test_no_tags_uses_commit_and_tracked_edits_are_marked_dirty(self):
        self.init_repo()
        commit = self.git("rev-parse", "--short", "HEAD")
        self.assertEqual(version_module.get_version(), commit)
        (self.root / "source.txt").write_text("edited\n")
        self.assertEqual(version_module.get_version(), commit + "-dirty")

    def test_release_tags_are_preserved_and_later_commits_are_distinguishable(self):
        self.init_repo()
        self.git("tag", "v1.2.3")
        with patch.dict(os.environ, APP_VERSION="  "):
            self.assertEqual(version_module.get_version(), "v1.2.3")
        (self.root / "source.txt").write_text("next\n")
        self.git("commit", "-am", "Next source")
        commit = self.git("rev-parse", "--short", "HEAD")
        self.assertEqual(version_module.get_version(), f"v1.2.3-1-g{commit}")

    def test_annotated_tag_and_worktree_git_file_are_supported(self):
        self.init_repo()
        self.git("tag", "-a", "1.2.3", "-m", "Release")
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            self.git("worktree", "add", "--detach", str(checkout), "HEAD")
            self.assertTrue((checkout / ".git").is_file())
            with patch.object(version_module, "__file__", str(checkout / "utils/version.py")):
                self.assertEqual(version_module.get_version(), "1.2.3")

    def test_exported_source_does_not_pick_up_an_unrelated_parent_repository(self):
        self.init_repo()
        self.git("tag", "v9.9.9")
        nested = self.root / "exported"
        nested.mkdir()
        with patch.object(version_module, "__file__", str(nested / "utils/version.py")), \
                patch.object(version_module.subprocess, "check_output") as command:
            self.assertEqual(version_module.get_version(), "unknown")
            command.assert_not_called()

    def test_missing_git_broken_metadata_and_timeouts_do_not_break_startup(self):
        (self.root / ".git").mkdir()
        for error in (
            FileNotFoundError("git"),
            subprocess.CalledProcessError(128, "git"),
            subprocess.TimeoutExpired("git", 2),
            UnicodeError("invalid tag encoding"),
        ):
            with self.subTest(error=type(error).__name__), \
                    patch.object(version_module.subprocess, "check_output", side_effect=error):
                self.assertEqual(version_module.get_version(), "unknown")

    def test_existing_version_export_and_startup_metadata_use_image_version(self):
        result = subprocess.check_output(
            [sys.executable, "-B", "-c",
             "import utils; from utils.meta import APP_VERSION; "
             "print(utils.__version__); print(APP_VERSION)"],
            cwd=ROOT, env={**os.environ, "APP_VERSION": "v1.2.3"}, text=True, timeout=10,
        )
        self.assertEqual(result.splitlines(), ["v1.2.3", "Telegram Media Downloader v1.2.3"])


if __name__ == "__main__":
    unittest.main()
