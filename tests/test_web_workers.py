"""Persist live concurrency in runtime JSON while keeping startup configuration read-only."""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from module import bot, web
from module.app import Application


class WebWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_file = self.root / 'config.yaml'
        self.data_file = self.root / 'data.json'
        self.config_file.write_text(
            '# Keep the startup limit and this comment.\n'
            'api_id: 123\napi_hash: test\nmedia_types: []\nfile_formats: {}\n'
            'max_download_task: 2\nfile_download_concurrency: 8\n', encoding='utf-8')
        self.data_file.write_text('{"chat": []}\n', encoding='utf-8')
        self.app = self.load_application()
        self.client = web.get_flask_app().test_client()
        self.telegram_client = Mock()
        patches = (
            patch.object(web, '_app', self.app),
            patch.object(bot, '_bot', SimpleNamespace(client=self.telegram_client)),
            patch.object(self.app.settings, 'save', wraps=self.app.settings.save),
            patch.object(sys.modules['__main__'], '_worker_pool_set_target', create=True),
            patch('module.pyrogram_extension.set_max_concurrent_transmissions'),
        )
        self.mocks = []
        for item in patches:
            self.mocks.append(item.start())
            self.addCleanup(item.stop)
        self.resize = self.mocks[3]
        self.resize_transmissions = self.mocks[4]

    def load_application(self):
        app = Application(str(self.config_file), str(self.data_file))
        self.addCleanup(app.loop.close)
        self.addCleanup(app.executor.shutdown)
        app.load_config()
        app.settings.setup_complete = True
        return app

    def test_live_adjustments_save_runtime_json_and_resize_workers(self):
        original = self.config_file.read_bytes()
        for workers in (1, 6):
            with self.subTest(workers=workers):
                response = self.client.post(f'/set_max_workers?n={workers}')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json, {'max_workers': workers})
                self.assertEqual(self.client.get('/get_max_workers').json, {'max_workers': workers})
                self.assertEqual(self.app.max_download_task, workers)
                self.assertEqual(self.app.file_download_concurrency, 8)
                self.assertEqual(self.app.max_concurrent_transmissions, workers * 8)
                self.resize.assert_called_with(workers)
                self.resize_transmissions.assert_called_with(self.telegram_client, workers * 8)
                self.app.settings.save.assert_called_with({'max_download_task': workers})
                self.assertEqual(self.app.config['max_download_task'], 2)
                self.assertEqual(self.config_file.read_bytes(), original)
                self.assertEqual(json.loads((self.data_file.parent / 'settings.json').read_text())['max_download_task'], workers)
        self.assertEqual(self.load_application().max_download_task, 6)

    def test_later_runtime_saves_keep_worker_limit_even_without_configured_default(self):
        for configured in (2, None):
            with self.subTest(configured=configured):
                if configured is None:
                    self.app.config.pop('max_download_task')
                    self.config_file.write_text(
                        self.config_file.read_text().replace('max_download_task: 2\n', ''))
                self.assertEqual(self.client.post('/set_max_workers?n=3').status_code, 200)
                # Completion and shutdown still save runtime state separately.
                self.app.save_app_data(False)
                self.app.save_app_data(True)
                self.assertEqual(self.app.max_download_task, 3)
                self.assertEqual(self.app.config.get('max_download_task'), configured)
                restarted = self.load_application()
                self.assertEqual(restarted.max_download_task, 3)
                self.assertEqual(restarted.max_concurrent_transmissions, 24)
                self.assertEqual(restarted.config.get('max_download_task'), 3)

    def test_invalid_limits_leave_runtime_and_config_unchanged(self):
        original = self.config_file.read_bytes(), self.data_file.read_bytes()
        for query in ('', '?n=0', '?n=7', '?n=invalid'):
            with self.subTest(query=query):
                response = self.client.post('/set_max_workers' + query)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json, {'error': 'n must be 1-6'})
        self.assertEqual(self.app.max_download_task, 2)
        self.resize.assert_not_called()
        self.resize_transmissions.assert_not_called()
        self.app.settings.save.assert_not_called()
        self.assertEqual((self.config_file.read_bytes(), self.data_file.read_bytes()), original)


if __name__ == '__main__':
    unittest.main()
