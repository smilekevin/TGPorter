"""Exercise the fresh-deployment filesystem layout without Telegram access."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RuntimePathsTests(unittest.TestCase):
    def test_new_deployment_creates_runtime_files_under_one_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = root / 'config.yaml'
            config.write_text('api_id: test\napi_hash: test\nmedia_types: []\nfile_formats: {}\n')
            original = config.read_bytes()
            config.chmod(0o444)
            script = '''
import json
from pathlib import Path
import media_downloader as downloader
from module import bot, download_stat, task_store
from module.app import TaskNode

app = downloader.app
try:
    app.load_config()
    assert Path(app.app_data_file).read_text() == "{}\\n"
    app.pre_run()
    node = TaskNode(100, task_id=7)
    assert task_store.save_task(node.task_id, 100, "", 0, 0, 1, None, 100,
                               extra_data={"task_id_display": node.task_id_display})
    download_stat.save_downloads()
    bot._bot.update_config()
    app.save_app_data()
    print(json.dumps({"data": app.app_data_file, "log": app.log_file_path,
                      "sessions": app.session_file_path, "temp": app.temp_save_path}))
finally:
    app.loop.close()
    app.executor.shutdown()
'''
            result = subprocess.run(
                [sys.executable, '-B', '-c', script], cwd=root,
                env={**os.environ, 'PYTHONPATH': str(ROOT)},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = json.loads(result.stdout)
            self.assertEqual(paths, {
                'data': str(root / 'data/data.json'),
                'log': str(root / 'data/log'),
                'sessions': str(root / 'data/sessions'),
                'temp': str(root / 'downloads/.temp'),
            })
            for name in ('data.json', 'bot.yaml', 'log/bot_tasks.json',
                         'log/task_counter.json', 'log/download_history.json'):
                self.assertTrue((root / 'data' / name).is_file(), name)
            self.assertTrue((root / 'data/sessions').is_dir())
            self.assertTrue((root / 'downloads/.temp').is_dir())
            self.assertEqual(set(path.name for path in root.iterdir()), {'config.yaml', 'data', 'downloads'})
            self.assertEqual(config.read_bytes(), original)

    def test_custom_download_directory_owns_staging_and_bot_cleanup(self):
        from unittest.mock import patch
        from module import bot
        from module.app import Application

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = Application(str(root / 'config.yaml'), str(root / 'data/data.json'))
            try:
                app.assign_config({'api_id': 'test', 'api_hash': 'test', 'media_types': [],
                                   'file_formats': {}, 'save_path': str(root / 'media')})
                self.assertEqual(app.temp_save_path, str(root / 'media/.temp'))
                app.pre_run()
                target = Path(app.temp_save_path) / '100'
                target.mkdir()
                (target / 'partial.temp').write_bytes(b'partial')
                other = Path(app.temp_save_path) / '200'
                other.mkdir()
                (other / 'partial.temp').write_bytes(b'other')
                with patch.object(bot._bot, 'app', app):
                    bot._cleanup_task_temp_files(100)
                self.assertFalse(target.exists())
                self.assertTrue((other / 'partial.temp').exists())
            finally:
                app.loop.close()
                app.executor.shutdown()


if __name__ == '__main__':
    unittest.main()
