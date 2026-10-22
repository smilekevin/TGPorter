"""New caches follow the final filename while existing checkpoints remain usable."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from module.staging import get_staging_path, is_staging_name


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy = self.root / "频道 ｜🐝" / "飞，一.mov"

    def path(self, **overrides):
        values = dict(directory=str(self.root), account="main", chat_id=100,
                      message_id=61, media_identity="stable-media", timestamp=1792119777,
                      legacy_path=str(self.legacy))
        values.update(overrides)
        return Path(get_staging_path(**values))

    def test_same_media_reuses_flat_ascii_name_after_restart_and_title_change(self):
        path = self.path()
        self.assertEqual(path.parent, self.root)
        self.assertTrue(is_staging_name(path.name))
        self.assertTrue(path.name.isascii())
        path.write_bytes(b"checkpoint")
        self.assertEqual(self.path(legacy_path=str(self.root / "renamed" / "other.mov")), path)
        self.assertEqual(path.read_bytes(), b"checkpoint")
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(lambda _: self.path(), range(8))), [path] * 8)

    def test_different_accounts_messages_chats_and_media_cannot_share_staging(self):
        paths = {self.path(), self.path(account="bot"), self.path(chat_id=200),
                 self.path(message_id=62), self.path(media_identity="different-media")}
        self.assertEqual(len(paths), 5)

    def test_legacy_base_sdk_and_parts_checkpoints_are_reused_without_changes(self):
        for suffix in ("", ".temp", ".parts"):
            with self.subTest(suffix=suffix):
                checkpoint = Path(str(self.legacy) + suffix)
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                if suffix == ".parts":
                    checkpoint.mkdir()
                else:
                    checkpoint.write_bytes(b"keep")
                self.assertEqual(self.path(), self.legacy)
                self.assertTrue(checkpoint.exists())
                if checkpoint.is_dir():
                    checkpoint.rmdir()
                else:
                    self.assertEqual(checkpoint.read_bytes(), b"keep")
                    checkpoint.unlink()

    def test_new_parts_take_precedence_over_legacy_data(self):
        path = self.path()
        Path(str(path) + ".parts").mkdir()
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_bytes(b"old")
        self.assertEqual(self.path(), path)
        self.assertEqual(self.legacy.read_bytes(), b"old")

    def test_invalid_oversized_and_outside_legacy_paths_do_not_affect_new_name(self):
        expected = self.path()
        for legacy in (str(self.root / ("a" * 400)), str(self.root / "bad\0name"),
                       str(self.root / ".." / "outside.mov")):
            with self.subTest(legacy=legacy):
                self.assertEqual(self.path(legacy_path=legacy), expected)

    def test_generated_name_recognition_does_not_match_legacy_names(self):
        self.assertFalse(is_staging_name("movie.tmp"))
        self.assertFalse(is_staging_name(self.path().name + ".temp"))
        self.assertFalse(is_staging_name(self.path().name + ".parts"))

    def test_new_download_uses_the_final_directory_and_basename(self):
        final = self.root / 'downloads' / '123' / '频道' / '飞，一.mov'
        self.assertEqual(self.path(final_path=str(final)), final)
        self.assertEqual(Path(str(self.path(final_path=str(final))) + '.temp'),
                         final.with_name('飞，一.mov.temp'))
        self.assertFalse(final.parent.exists())

    def test_old_uuid_checkpoint_is_reused_after_switching_to_final_paths(self):
        old = self.path()
        Path(str(old) + '.parts').mkdir()
        Path(str(old) + '.temp').write_bytes(b'old progress')
        final = self.root / 'downloads' / 'video.mp4'
        self.assertEqual(self.path(final_path=str(final)), old)
        self.assertEqual(Path(str(old) + '.temp').read_bytes(), b'old progress')

    def test_existing_final_path_checkpoint_takes_precedence_over_old_cache(self):
        old = self.path()
        Path(str(old) + '.parts').mkdir()
        final = self.root / 'downloads' / 'video.mp4'
        final.parent.mkdir()
        Path(str(final) + '.temp').write_bytes(b'new progress')
        self.assertEqual(self.path(final_path=str(final)), final)
        self.assertTrue(Path(str(old) + '.parts').exists())

    def test_final_name_changes_preserve_the_previous_cache(self):
        final = self.root / 'original.mp4'
        checkpoint = Path(str(final) + '.temp')
        checkpoint.write_bytes(b'keep')
        renamed = self.root / 'renamed.mp4'
        self.assertEqual(self.path(final_path=str(renamed)), renamed)
        self.assertEqual(checkpoint.read_bytes(), b'keep')
