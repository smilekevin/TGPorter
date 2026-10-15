"""Exercise SDK imports in fresh interpreters with Python 3.14 loop semantics."""

from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TelegramImportTests(unittest.TestCase):
    def run_script(self, script):
        result = subprocess.run(
            [sys.executable, "-B", "-W", "error::ResourceWarning", "-c", textwrap.dedent(script)],
            cwd=ROOT, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ResourceWarning", result.stderr)

    def test_import_without_loop_leaves_no_current_loop(self):
        self.run_script('''
            import asyncio
            asyncio.set_event_loop(None)
            from module.telegram import pyrogram
            from module import bot, download_connection, web
            assert pyrogram.Client
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                pass
            else:
                raise AssertionError("SDK import leaked its temporary loop")
        ''')

    def test_import_preserves_existing_loop(self):
        self.run_script('''
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                from module.telegram import pyrogram
                assert asyncio.get_event_loop() is loop
                assert not loop.is_closed()
            finally:
                asyncio.set_event_loop(None)
                loop.close()
        ''')

    def test_import_inside_running_loop_preserves_async_calls(self):
        self.run_script('''
            import asyncio
            async def main():
                from module.telegram import pyrogram
                from pyrogram.sync import async_to_sync
                loop = asyncio.get_running_loop()
                class Probe:
                    async def check(self):
                        return asyncio.get_running_loop()
                async_to_sync(Probe, "check")
                assert await Probe().check() is loop
                assert asyncio.get_running_loop() is loop
            asyncio.run(main())
        ''')
