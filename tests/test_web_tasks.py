"""Exercise task submissions and file choices through production Flask routes."""

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from module import download_stat, web


class WebTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'video.mp4'
        self.file.write_bytes(b'video')
        self.staging = self.root / 'partial'
        self.parts = Path(str(self.staging) + '.parts')
        self.parts.mkdir()
        (self.parts / 'manifest.json').write_text('{}')
        self.config = SimpleNamespace(web_login_secret='password', loop=None)
        self.app = web.get_flask_app()
        for item in (
            patch.object(web, '_app', self.config),
            patch.object(download_stat, '_download_result', {1: {2: {
                'task_id': 'task-1', 'file_name': str(self.file), 'down_byte': 5, 'total_size': 5,
            }}}),
            patch.object(download_stat, '_failed_downloads', [{
                'task_id': 'failed-1', 'file_name': str(self.file), 'temp_file_name': str(self.staging),
            }]),
            patch.object(download_stat, '_cancelled_tasks', set()),
            patch.object(download_stat, '_discarded_tasks', set()),
            patch.object(download_stat, '_HISTORY_FILE', str(self.root / 'history.json')),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = self.app.test_client()
        self.client.get('/login')
        with self.client.session_transaction() as session:
            csrf = session['web_csrf_token']
        self.client.post('/login', data={'password': 'password', 'csrf_token': csrf})
        with self.client.session_transaction() as session:
            self.headers = {'X-CSRF-Token': session['web_csrf_token']}

    def post(self, url, **kwargs):
        return self.client.post(url, headers=self.headers, **kwargs)

    def test_submission_validates_json_and_link_without_touching_telegram(self):
        self.assertEqual(self.client.post('/submit_download', json={'url': 'https://t.me/a/1'}).status_code, 400)
        for payload in (None, [], 'text'):
            self.assertEqual(self.post('/submit_download', json=payload).status_code, 400)
        for payload in ({}, {'url': 'https://evil.example/123'}, {'url': 123}):
            self.assertEqual(self.post('/submit_download', json=payload).json['code'], '0')
        response = self.post('/submit_download', json={'url': 'https://t.me/channel_name/123'})
        self.assertEqual(response.json['code'], '0')
        self.assertIn('not ready', response.json['message'])

    def test_completed_delete_preserves_file_by_default(self):
        self.assertEqual(self.post('/delete_task?task_id=task-1').json['code'], '1')
        self.assertTrue(self.file.exists())
        self.assertFalse(download_stat._discarded_tasks)

    def test_failed_delete_finds_output_and_cleans_only_its_own_staging(self):
        self.assertTrue(self.post('/check_file_exists?task_id=failed-1').json['exists'])
        other = self.root / 'unrelated.parts'
        other.mkdir()
        response = self.post('/delete_task?task_id=failed-1&delete_file=true').json
        self.assertEqual(response['code'], '1')
        self.assertTrue(response['file_deleted'])
        self.assertFalse(self.file.exists())
        self.assertFalse(self.parts.exists())
        self.assertTrue(other.exists())

    def test_failed_cache_without_output_reports_existing_cache_path(self):
        self.file.unlink()
        for suffix in ('.parts', '.temp', ''):
            with self.subTest(suffix=suffix):
                cache = Path(str(self.staging) + suffix)
                if suffix != '.parts':
                    cache.write_bytes(b'partial')
                result = self.post('/check_file_exists?task_id=failed-1').json
                self.assertTrue(result['exists'])
                self.assertFalse(result['output_exists'])
                self.assertTrue(result['cache_exists'])
                self.assertEqual(result['cache_paths'], [str(cache)])
                self.assertEqual(result['path'], str(cache))
                self.assertEqual(result['filename'], cache.name)
                if cache.is_dir():
                    shutil.rmtree(cache)
                else:
                    cache.unlink()

    def test_output_and_cache_are_reported_separately(self):
        result = self.post('/check_file_exists?task_id=failed-1').json
        self.assertTrue(result['output_exists'])
        self.assertTrue(result['cache_exists'])
        self.assertEqual(result['path'], str(self.file))
        self.assertEqual(result['cache_paths'], [str(self.parts)])
        completed = self.post('/check_file_exists?task_id=task-1').json
        self.assertTrue(completed['output_exists'])
        self.assertFalse(completed['cache_exists'])

    def test_no_files_reports_no_cache(self):
        self.file.unlink()
        shutil.rmtree(self.parts)
        result = self.post('/check_file_exists?task_id=failed-1').json
        self.assertFalse(result['exists'])
        self.assertFalse(result['output_exists'])
        self.assertFalse(result['cache_exists'])
        self.assertEqual(result['cache_paths'], [])

    def test_failed_delete_can_preserve_output_and_staging(self):
        self.assertEqual(self.post('/delete_task?task_id=failed-1').json['code'], '1')
        self.assertTrue(self.file.exists())
        self.assertTrue(self.parts.exists())

    def test_failed_staging_path_survives_history_reload(self):
        download_stat.add_failed_download(1, 3, 'persisted-failure', '', 'offline',
                                          temp_file_name=str(self.staging))
        download_stat._failed_downloads = []
        download_stat.load_downloads()
        result = self.post('/check_file_exists?task_id=persisted-failure').json
        self.assertTrue(result['exists'])
        self.post('/delete_task?task_id=persisted-failure&delete_file=true')
        self.assertFalse(self.parts.exists())

    def test_cancel_marks_partial_discard_only_when_selected(self):
        result = download_stat._download_result[1][2]
        result['down_byte'] = 1
        self.post('/delete_task?task_id=task-1&delete_file=true')
        self.assertIn('task-1', download_stat._cancelled_tasks)
        self.assertIn('task-1', download_stat._discarded_tasks)

    def test_batch_failed_deletion_uses_file_choice(self):
        response = self.post('/batch_delete', json={'task_ids': ['failed-1'], 'delete_file': True}).json
        self.assertEqual(response['deleted'], 1)
        self.assertEqual(response['files_deleted'], 1)
        self.assertFalse(self.file.exists())
        self.assertFalse(self.parts.exists())

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_frontend_defaults_requests_and_submission_failures(self):
        html = self.client.get('/').get_data(as_text=True)
        script = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)[-1]
        result = subprocess.run(['node', '-e', '''
const assert = require('node:assert/strict');
global.WebI18n = {language: 'zh', t: text => text};
let requests = [];
let duplicate = false;
let fileStatus = {exists: true};
global.fetch = async (url, options) => {
    requests.push({url, options});
    return {status: 200, json: async () => url === '/submit_download'
        ? {code: '1', duplicate} : {code: '1', ...fileStatus}};
};
''' + script + '''
(async () => {
    const ui = app();
    ui.showToast = () => {};
    ui.fetchPending = async () => {};
    ui.fetchActive = async () => {};
    for (const tab of ['completed', 'active', 'failed']) {
        ui.activeTab = tab;
        await ui.deleteTask('task-1');
        assert.equal(ui.deleteModal.show, true);
        assert.equal(ui.deleteModal.deleteFile, tab !== 'completed');
        ui.selectedTasks = ['task-1'];
        await ui.batchDelete();
        assert.equal(ui.deleteModal.deleteFile, tab !== 'completed');
    }
    fileStatus = {exists: true, output_exists: false, cache_exists: true,
                  filename: 'partial.parts', path: '/temp/partial.parts'};
    await ui.deleteTask('failed-1');
    assert.equal(ui.deleteModal.fileExists, true);
    assert.equal(ui.deleteModal.cacheOnly, true);
    assert.equal(ui.deleteModal.filePath, '/temp/partial.parts');
    fileStatus = {exists: true, output_exists: true, cache_exists: false};
    await ui.deleteTask('task-1');
    assert.equal(ui.deleteModal.cacheOnly, false);
    fileStatus = {exists: false, output_exists: false, cache_exists: false};
    await ui.deleteTask('missing');
    assert.equal(ui.deleteModal.fileExists, false);
    assert.equal(ui.deleteModal.cacheOnly, false);
    ui.downloadUrl = ' https://t.me/channel_name/123 ';
    await ui.submitDownload();
    const sent = requests.find(r => r.url === '/submit_download');
    assert.equal(JSON.parse(sent.options.body).url, 'https://t.me/channel_name/123');
    assert.ok(sent.options.headers.get('X-CSRF-Token'));
    assert.equal(ui.downloadUrl, '');
    assert.equal(ui.submitting, false);
    assert.equal(ui.activeTab, 'pending');
    ui.activeTab = 'failed';
    await ui.deleteTask('task-1');
    ui.deleteModal.deleteFile = false;
    await ui.executeDelete();
    assert.ok(requests.some(r => r.url === '/delete_task?task_id=task-1'));
    await ui.deleteTask('task-1');
    await ui.executeDelete();
    assert.ok(requests.some(r => r.url === '/delete_task?task_id=task-1&delete_file=true'));

    global.fetch = async () => ({status: 200, json: async () => ({code: '0', message: 'offline'})});
    ui.downloadUrl = 'https://t.me/channel_name/456';
    await ui.submitDownload();
    assert.equal(ui.downloadUrl, 'https://t.me/channel_name/456');
    assert.equal(ui.submitting, false);
})().catch(e => { console.error(e); process.exitCode = 1; });
'''], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
