"""Exercise task submissions and file choices through production Flask routes."""

import asyncio
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import media_downloader as downloader
from module import download_stat, web, task_store
from module.app import DownloadStatus, TaskNode


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
            patch.object(task_store, '_TASKS_FILE', str(self.root / 'tasks.json')),
            patch.object(download_stat, '_download_result', {1: {2: {
                'task_id': 'task-1', 'file_name': str(self.file), 'down_byte': 5, 'total_size': 5,
            }}}),
            patch.object(download_stat, '_skipped_downloads', {}),
            patch.object(download_stat, '_failed_downloads', [{
                'task_id': 'failed-1', 'file_name': str(self.file), 'temp_file_name': str(self.staging),
            }]),
            patch.object(download_stat, '_cancelled_tasks', set()),
            patch.object(download_stat, '_discarded_tasks', set()),
            patch.object(download_stat, '_paused_tasks', set()),
            patch.object(download_stat, '_download_streams', {}),
            patch.object(download_stat, '_download_state', download_stat.DownloadState.Downloading),
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

    def test_live_stream_counts_in_production_routes_follow_pause_and_resume(self):
        record = download_stat._download_result[1][2]
        record.update(total_size=100, download_speed=0, start_time=0, end_time=0)
        self.assertEqual(self.client.get('/get_download_status').json['active_streams'], 0)
        self.assertEqual(self.client.get('/get_download_list?already_down=false').json[0]['active_streams'], 0)
        with download_stat.track_download_stream(1, 2, 'task-1'), \
                download_stat.track_download_stream(1, 2, 'task-1'), \
                download_stat.track_download_stream(2, 3, 'task-2'):
            self.assertEqual(self.client.get('/get_download_status').json['active_streams'], 3)
            rows = self.client.get('/get_download_list?already_down=false').json
            self.assertEqual(rows[0]['active_streams'], 2)
            self.assertTrue(download_stat.pause_task('1_2'))
            self.assertEqual(self.client.get('/get_download_status').json['active_streams'], 1)
            self.assertEqual(self.client.get('/get_download_list?already_down=false').json[0]['active_streams'], 0)
            self.assertTrue(download_stat.resume_task('1_2'))
            self.assertEqual(self.client.get('/get_download_status').json['active_streams'], 3)
        record['down_byte'] = 100
        # Saved history never supplies a live stream count, even if it has an old field.
        record['active_streams'] = 99
        completed = self.client.get('/get_download_list?already_down=true').json['tasks']
        self.assertEqual(completed[0]['active_streams'], 0)
        self.assertEqual(self.client.get('/get_download_status').json['active_streams'], 0)

    def test_rounded_progress_does_not_complete_a_file_before_its_last_byte(self):
        record = download_stat._download_result[1][2]
        total = 100 * 1024 * 1024
        record.update(total_size=total, down_byte=total - 1, progress=100.0,
                      download_speed=0, start_time=1, end_time=1)
        active = self.client.get('/get_download_list?already_down=false').json
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['status'], 'active')
        self.assertEqual(active[0]['download_progress_raw'], 99.9)
        self.assertEqual(self.client.get('/get_completed_count').json['total'], 0)
        self.assertEqual(self.client.get('/get_download_list?already_down=true').json['tasks'], [])
        download_stat.pause_task('1_2')
        self.assertEqual(self.client.get('/get_download_list?already_down=false').json[0]['status'], 'paused')
        download_stat.resume_task('1_2')
        record['down_byte'] = total
        self.assertEqual(self.client.get('/get_download_list?already_down=false').json, [])
        completed = self.client.get('/get_download_list?already_down=true').json
        self.assertEqual(completed['total'], 1)
        self.assertEqual(completed['tasks'][0]['status'], 'completed')
        self.assertEqual(completed['tasks'][0]['download_progress_raw'], 100)
        self.assertEqual(self.client.get('/get_completed_count').json['total'], 1)

    def test_preparation_is_explicit_and_one_byte_files_are_downloads(self):
        download_stat._download_result.clear()
        node = TaskNode(chat_id=1, task_id='task-1', limit=1)
        download_stat.prepare_download(2, node)
        row = self.client.get('/get_download_list?already_down=false').json[0]
        self.assertEqual((row['status'], row['phase'], row['total_size_bytes']),
                         ('active', 'preparing', 0))
        self.assertEqual(row['wait_reason'], 'Fetching file information...')
        asyncio.run(download_stat.update_download_status(
            0, 1, 2, str(self.file), 1, node, None, initial=True))
        row = self.client.get('/get_download_list?already_down=false').json[0]
        self.assertEqual(row['phase'], 'preparing')
        self.assertEqual(row['wait_reason'], 'Establishing download connections')
        with download_stat.track_download_stream(1, 2, 'task-1'):
            row = self.client.get('/get_download_list?already_down=false').json[0]
            self.assertEqual((row['phase'], row['wait_reason']), ('downloading', ''))
        # No bytes and no live stream must not revert a started file to preparation.
        row = self.client.get('/get_download_list?already_down=false').json[0]
        self.assertEqual(row['phase'], 'downloading')
        download_stat.pause_task('1_2')
        self.assertEqual(self.client.get('/get_download_list?already_down=false').json[0]['status'], 'paused')
        download_stat.resume_task('1_2')
        asyncio.run(download_stat.update_download_status(1, 1, 2, str(self.file), 1, node, None))
        self.assertEqual(self.client.get('/get_download_list?already_down=false').json, [])
        self.assertEqual(self.client.get('/get_download_list?already_down=true').json['total'], 1)

    def test_wait_reason_respects_pause_and_healthy_streams(self):
        record = download_stat._download_result[1][2]
        record.update(total_size=100, down_byte=5, download_speed=0, start_time=1, end_time=1,
                      phase='downloading')
        from module.download_service import DownloadService
        with patch.object(DownloadService, 'get_flood_wait', return_value={'active': True, 'remaining': 35}):
            row = self.client.get('/get_download_list?already_down=false').json[0]
            self.assertEqual(row['wait_seconds'], 35)
            self.assertEqual(row['wait_reason'], 'Waiting for Telegram cooldown: {seconds} seconds')
            with download_stat.track_download_stream(1, 2, 'task-1'):
                row = self.client.get('/get_download_list?already_down=false').json[0]
                self.assertEqual(row['wait_reason'], '')
            download_stat.set_download_state(download_stat.DownloadState.StopDownload)
            row = self.client.get('/get_download_list?already_down=false').json[0]
            self.assertEqual(row['wait_reason'], 'All downloads are paused')
            download_stat.pause_task('1_2')
            row = self.client.get('/get_download_list?already_down=false').json[0]
            self.assertEqual((row['status'], row['wait_reason']), ('paused', ''))

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

    def test_existing_file_skip_survives_reload_and_production_routes(self):
        node = TaskNode(chat_id=1, task_id=1, limit=1)
        node.task_id_display = '1019-1'
        message = SimpleNamespace(id=2, chat=SimpleNamespace(username=None, title=''),
                                  text=None, media=True)
        config = SimpleNamespace(enable_download_txt=False, set_download_id=lambda *args: None,
                                 media_types=[], file_formats={})
        for state in ('created', 'reloaded', 'resubmitted'):
            with self.subTest(state=state):
                if state == 'reloaded':
                    download_stat._download_result.clear()
                    download_stat._skipped_downloads.clear()
                    download_stat.load_downloads()
                else:
                    with patch.object(downloader, 'app', config), patch.object(
                            downloader, 'download_media', AsyncMock(return_value=(
                                DownloadStatus.SkipDownload, str(self.file), ''))), patch.object(
                            downloader, 'upload_telegram_chat', AsyncMock()):
                        asyncio.run(downloader.download_task(None, message, node))
                self.assertEqual(self.client.get('/get_download_list?already_down=false').json, [])
                self.assertEqual(self.client.get('/get_completed_count').json['total'], 0)
                self.assertEqual(self.client.get('/get_download_list?already_down=true').json['tasks'], [])
                rows = self.client.get('/get_skipped_downloads?search=video').json
                self.assertEqual(rows['total'], 1)
                row = rows['tasks'][0]
                self.assertEqual(row['task_id'], '1_2')
                self.assertEqual(row['reason'], 'already download,download skipped')
                self.assertEqual(row['file_name'], self.file.name)
        self.assertEqual(self.post('/delete_skipped?task_id=1_2').json['code'], '1')
        self.assertTrue(self.file.exists())

    def test_deleted_task_is_not_restored_as_skipped(self):
        node = TaskNode(chat_id=1, task_id='task-1', limit=1)
        self.post('/delete_task?task_id=1_2')
        message = SimpleNamespace(id=2)
        download_stat.record_skipped_download(message, node, 'already download,download skipped')
        self.assertFalse(download_stat._skipped_downloads)
        self.assertFalse(download_stat._download_result)

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

    def test_active_task_reports_its_recorded_staging_cache(self):
        self.file.unlink()
        download_stat._download_result[1][2]['temp_file_name'] = str(self.staging)
        result = self.post('/check_file_exists?task_id=task-1').json
        self.assertTrue(result['cache_exists'])
        self.assertEqual(result['cache_paths'], [str(self.parts)])

    def test_final_output_is_not_reported_as_its_own_cache(self):
        download_stat._download_result[1][2]['temp_file_name'] = str(self.file)
        result = self.post('/check_file_exists?task_id=task-1').json
        self.assertTrue(result['output_exists'])
        self.assertFalse(result['cache_exists'])
        self.assertEqual(result['cache_paths'], [])

    def test_partial_next_to_final_path_is_reported_and_preserved_without_file_deletion(self):
        record = download_stat._download_result[1][2]
        record['temp_file_name'] = str(self.file)
        self.file.unlink()
        partial = Path(str(self.file) + '.temp')
        partial.write_bytes(b'checkpoint')
        result = self.post('/check_file_exists?task_id=task-1').json
        self.assertFalse(result['output_exists'])
        self.assertTrue(result['cache_exists'])
        self.assertEqual(result['cache_paths'], [str(partial)])
        self.assertEqual(self.post('/delete_task?task_id=task-1').json['code'], '1')
        self.assertEqual(partial.read_bytes(), b'checkpoint')

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
    ui.activeTasks = [
        {task_id: 'a', filename: 'a', status: 'active', phase: 'preparing'},
        {task_id: 'b', filename: 'b', status: 'active', phase: 'downloading'},
        {task_id: 'c', filename: 'c', status: 'paused', phase: 'preparing'},
    ];
    for (const [filter, expected] of [['preparing', ['a']], ['active', ['b']], ['paused', ['c']]]) {
        ui.activeStatusFilter = filter;
        assert.deepEqual(ui.filteredActiveTasks.map(task => task.task_id), expected);
    }
    ui.$refs = {statusFilter: {open: true}, statusFilterButton: {focus() {}}};
    ui.selectedTasks = ['a'];
    ui.chooseActiveStatus('preparing');
    assert.equal(ui.activeStatusFilterLabel, 'Preparing');
    assert.deepEqual(ui.filteredActiveTasks.map(task => task.task_id), ['a']);
    assert.deepEqual(ui.selectedTasks, []);
    assert.equal(ui.$refs.statusFilter.open, false);
    ui.chooseActiveStatus('all');
    assert.equal(ui.activeStatusFilterLabel, 'All');
    ui.activeTasks = [];
    ui.activeStatusFilter = 'all';
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

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_shared_toolbar_selects_only_visible_rows_in_each_tab(self):
        html = self.client.get('/').get_data(as_text=True)
        script = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)[-1]
        result = subprocess.run(['node', '-e', '''
const assert = require('node:assert/strict');
global.WebI18n = {language: 'en', t: text => text};
''' + script + '''
const ui = app();
ui.activeTasks = [
    {task_id: 'active', filename: 'match.mp4', status: 'active'},
    {task_id: 'paused', filename: 'match.mp4', status: 'paused'},
];
ui.failedTasks = [
    {task_id: 'failed', filename: 'match.mp4'},
    {task_id: 'hidden', filename: 'other.mp4'},
];
ui.completedTasks = [{task_id: 'completed'}];
ui.pendingTasks = [{task_id: 'pending'}];
ui.skippedTasks = [{task_id: 'skipped'}];
ui.pendingTotal = 4;
ui.completedTotal = 8;
ui.skippedTotal = 2;
ui.taskSearch = 'match';
ui.activeStatusFilter = 'paused';
for (const [tab, id, total] of [
    ['active', 'paused', 2], ['pending', 'pending', 4],
    ['completed', 'completed', 8], ['failed', 'failed', 2], ['skipped', 'skipped', 2],
]) {
    ui.activeTab = tab;
    ui.selectedTasks = [];
    assert.equal(ui.visibleTaskCount, 1);
    assert.equal(ui.currentTaskTotal, total);
    assert.equal(ui.allVisibleSelected, false);
    ui.toggleSelectAll(tab);
    assert.deepEqual(ui.selectedTasks, [id]);
    assert.equal(ui.allVisibleSelected, true);
    ui.toggleSelectAll(tab);
    assert.deepEqual(ui.selectedTasks, []);
    assert.equal(ui.allVisibleSelected, false);
}
ui.completedTasks = [];
ui.activeTab = 'completed';
assert.equal(ui.allVisibleSelected, false);
assert.equal(ui.visibleTaskCount, 0);
'''], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_mobile_search_pull_preserves_native_scrolling_and_active_queries(self):
        html = self.client.get('/').get_data(as_text=True)
        script = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)[-1]
        result = subprocess.run(['node', '-e', '''
const assert = require('node:assert/strict');
global.WebI18n = {language: 'en', t: text => text};
let compact = true, focused = 0, prevented = 0;
global.window = {scrollY: 0, matchMedia: () => ({matches: compact})};
global.document = {getElementById: () => ({focus: () => focused++})};
''' + script + '''
const ui = app();
ui.$nextTick = callback => callback();
const touch = (x, y, control = false) => ({
    touches: [{clientX: x, clientY: y}], target: {closest: () => control},
    cancelable: true, preventDefault: () => prevented++,
});
// Upward scrolling, horizontal gestures, controls, and a scrolled page stay native.
for (const [start, end, scroll, desktop] of [
    [touch(100, 100), touch(100, 20), 0, false],
    [touch(100, 100), touch(180, 155), 0, false],
    [touch(100, 100, true), touch(100, 170), 0, false],
    [touch(100, 100), touch(100, 170), 30, false],
    [touch(100, 100), touch(100, 170), 0, true],
]) {
    window.scrollY = scroll;
    compact = !desktop;
    ui.startSearchPull(start);
    ui.moveSearchPull(end);
    assert.equal(ui.taskSearchExpanded, false);
    assert.equal(prevented, 0);
}
compact = true;
window.scrollY = 0;
ui.startSearchPull(touch(100, 100));
ui.moveSearchPull(touch(100, 125));
assert.equal(ui.taskSearchExpanded, false);
ui.moveSearchPull(touch(100, 160));
assert.equal(ui.taskSearchExpanded, true);
assert.equal(focused, 0, 'Pulling must not open the software keyboard');
assert.equal(prevented, 2);
ui.toggleTaskSearch();
assert.equal(ui.taskSearchExpanded, false);
ui.toggleTaskSearch();
assert.equal(ui.taskSearchExpanded, true);
assert.equal(focused, 1);
ui.taskSearch = 'video';
ui.toggleTaskSearch();
assert.equal(ui.taskSearchExpanded, true, 'An active filter must stay visible');
assert.equal(ui.taskSearch, 'video');
'''], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_navigation_falls_back_on_overflow_and_restores_when_space_returns(self):
        html = self.client.get('/').get_data(as_text=True)
        script = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)[-1]
        result = subprocess.run(['node', '-e', '''
const assert = require('node:assert/strict');
global.WebI18n = {language: 'en', t: text => text};
let portrait = false;
global.window = {matchMedia: () => ({matches: portrait})};
''' + script + '''
const ui = app();
const tabs = {clientWidth: 600, scrollWidth: 500};
ui.$refs = {downloadTabs: tabs};
ui.updateNavigationLayout();
assert.equal(ui.navigationCollapsed, false);
// A longer translation or count must trigger bottom navigation.
tabs.scrollWidth = 650;
ui.updateNavigationLayout();
assert.equal(ui.navigationCollapsed, true);
// More space restores the same top navigation without clearing selection.
ui.selectedTasks = ['active_001'];
tabs.clientWidth = 700;
ui.updateNavigationLayout();
assert.equal(ui.navigationCollapsed, false);
assert.deepEqual(ui.selectedTasks, ['active_001']);
portrait = true;
ui.updateNavigationLayout();
assert.equal(ui.navigationCollapsed, true);
// Late resize notifications cannot mutate an unmounted component.
ui.disposed = true;
portrait = false;
ui.updateNavigationLayout();
assert.equal(ui.navigationCollapsed, true);
'''], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pause_moves_to_pending_and_resume_stays_queued_through_real_routes(self):
        node = TaskNode(chat_id=1, task_id='task-1', task_id_display='1021-1')
        download_stat._download_result[1][2].update(down_byte=2, total_size=5)
        download_stat.prepare_download(2, node)
        self.assertEqual(self.post('/pause_task?task_id=1_2').json['code'], '1')
        self.assertEqual(self.client.get('/get_download_list?already_down=false').json, [])
        paused = self.client.get('/get_pending_list?status=paused').json
        self.assertEqual(paused['total'], 1)
        self.assertEqual(paused['tasks'][0]['status'], 'paused')
        self.assertEqual(self.post('/resume_task?task_id=1_2').json['code'], '1')
        self.assertEqual(self.client.get('/get_download_list?already_down=false').json, [])
        queued = self.client.get('/get_pending_list?status=pending').json
        self.assertEqual(queued['total'], 1)
        self.assertEqual(queued['tasks'][0]['queue_label'], 'Queued')

    def test_pending_exposes_message_identity_without_changing_queue_operation_id(self):
        tasks = [
            {'task_id': 'queue-1', 'chat_id': 1, 'extra_data': {'message_id': 2}},
            {'task_id': 'batch', 'chat_id': 1, 'extra_data': {}},
        ]
        with patch('module.download_service.get_pending_tasks', return_value=tasks), \
                patch.object(download_stat, 'get_download_result', return_value={}):
            pending = self.client.get('/get_pending_list').json['tasks']
        self.assertEqual(pending[0]['task_id'], 'queue-1')
        self.assertEqual(pending[0]['id'], '2')
        self.assertEqual(pending[1]['id'], '')
        download_stat._download_result[1][2].update(
            down_byte=1, download_speed=0, start_time=0, end_time=0)
        active = self.client.get('/get_download_list?already_down=false').json[0]
        self.assertEqual((pending[0]['chat'], pending[0]['id']), (active['chat'], active['id']))
