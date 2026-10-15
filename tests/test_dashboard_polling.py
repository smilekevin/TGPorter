"""Browser polling lifecycle checks without a Telegram session or browser install."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard tests')
class DashboardPollingTests(unittest.TestCase):
    def run_script(self, script):
        result = subprocess.run(['node'], input=script, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_visibility_slow_refresh_and_cleanup(self):
        source = (ROOT / 'module/static/dashboard_polling.js').read_text()
        self.run_script(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const timers = new Map();
const listeners = new Map();
let serial = 0;
const document = {
    hidden: false,
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: (name, callback) => {
        assert.equal(listeners.get(name), callback);
        listeners.delete(name);
    },
};
const context = {
    document,
    setTimeout: callback => { timers.set(++serial, callback); return serial; },
    clearTimeout: id => timers.delete(id),
};
vm.createContext(context);
vm.runInContext(SOURCE, context);
async function nextTick() {
    assert.equal(timers.size, 1);
    const [id, callback] = timers.entries().next().value;
    timers.delete(id);
    return callback();
}
(async () => {
    let count = 0;
    let resolve;
    const stop = context.startDashboardPolling(() => {
        count++;
        return new Promise(done => { resolve = done; });
    });
    const pending = nextTick();
    assert.equal(count, 1);
    assert.equal(timers.size, 0, 'slow requests must not queue another cycle');
    document.hidden = true;
    listeners.get('visibilitychange')();
    document.hidden = false;
    listeners.get('visibilitychange')();
    assert.equal(count, 1, 'returning during an active request must not duplicate it');
    resolve();
    await pending;
    assert.equal(timers.size, 1);
    document.hidden = true;
    listeners.get('visibilitychange')();
    assert.equal(timers.size, 0);
    document.hidden = false;
    listeners.get('visibilitychange')();
    assert.equal(count, 2, 'returning must immediately refresh');
    stop();
    resolve();
    await new Promise(done => setImmediate(done));
    assert.equal(timers.size, 0, 'destroy during a request must not restart polling');
    assert.equal(listeners.size, 0);
    const stopHidden = context.startDashboardPolling(() => { count++; });
    document.hidden = true;
    listeners.get('visibilitychange')();
    assert.equal(timers.size, 0);
    stopHidden();
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace('SOURCE', json.dumps(source)))

    def test_initialization_is_idempotent_and_parallel(self):
        page = (ROOT / 'module/templates/index.html').read_text()
        self.assertNotIn('x-init="init()"', page)
        script = re.findall(r'<script>(.*?)</script>', page, re.S)[-1]
        script = re.sub(r'{{.*?}}', "''", script)
        self.run_script(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
let polls = 0;
const listeners = new Map();
const context = {
    WebI18n: {language: 'en'}, Date,
    window: {
        addEventListener: (name, cb) => listeners.set(name, cb),
        removeEventListener: name => listeners.delete(name),
    },
    clearTimeout() {},
    startDashboardPolling: () => { polls++; return () => polls--; },
};
vm.createContext(context);
vm.runInContext(SOURCE, context);
(async () => {
    const app = context.app();
    const names = ['fetchVersion', 'fetchActive', 'fetchPending',
        'fetchCompleted', 'fetchMaxWorkers', 'fetchDownloadStatus', 'fetchFloodWait'];
    let calls = 0;
    let releases = [];
    names.forEach(name => app[name] = () => {
        calls++;
        return new Promise(done => releases.push(done));
    });
    const initial = app.init();
    await app.init();
    assert.equal(calls, 7, 'all initial requests should start without serial waits');
    assert.equal(polls, 0);
    releases.forEach(done => done());
    await initial;
    assert.equal(polls, 1);
    app.destroy();
    assert.equal(polls, 0);
    assert.equal(listeners.size, 0);
    const removed = context.app();
    releases = [];
    names.forEach(name => removed[name] = app[name]);
    const startup = removed.init();
    removed.destroy();
    releases.forEach(done => done());
    await startup;
    assert.equal(polls, 0, 'removed components must not begin polling after startup');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace('SOURCE', json.dumps(script)))

    def test_request_timeout_and_server_countdown(self):
        page = (ROOT / 'module/templates/index.html').read_text()
        script = re.findall(r'<script>(.*?)</script>', page, re.S)[-1]
        script = re.sub(r'{{.*?}}', "'test-csrf'", script)
        self.run_script(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const timers = new Map();
let serial = 0;
let request;
const context = {
    WebI18n: {language: 'en', t: value => value}, Headers, AbortController,
    setTimeout: (callback, delay) => { assert.equal(delay, 15000); timers.set(++serial, callback); return serial; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => { request = options; return {status: 200}; },
};
vm.createContext(context);
vm.runInContext(SOURCE, context);
(async () => {
    await context.authenticatedFetch('/status');
    assert.equal(request.headers.get('X-CSRF-Token'), 'test-csrf');
    assert.equal(timers.size, 0, 'successful reads must clear their timeout');
    context.fetch = (url, options) => new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(Error('aborted')));
    });
    const read = context.authenticatedFetch('/slow');
    timers.values().next().value();
    await assert.rejects(read, /aborted/);
    assert.equal(timers.size, 0);
    context.fetch = async () => {
        assert.equal(timers.size, 0, 'mutations must keep their existing server wait');
        return {status: 200};
    };
    await context.authenticatedFetch('/submit_download', {method: 'POST'});
    const app = context.app();
    app.floodWait = {active: true, remaining: 30, reason: ''};
    context.fetch = async () => ({status: 200, json: async () => ({active:true, remaining:29})});
    await app.fetchFloodWait();
    assert.equal(app.floodWait.remaining, 29, 'server countdown values must decrease');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace('SOURCE', json.dumps(script)))

    def test_production_page_serves_dashboard_assets(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from module import web

        app = web.get_flask_app()
        with patch.object(web, '_app', SimpleNamespace(web_login_secret='')):
            client = app.test_client()
            page = client.get('/').get_data(as_text=True)
            for asset in ('dashboard.css', 'dashboard_polling.js'):
                self.assertIn('/static/' + asset, page)
                response = client.get('/static/' + asset)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_search_filters_selection_and_discards_outdated_pages(self):
        page = (ROOT / 'module/templates/index.html').read_text()
        self.assertNotIn('overview-card', page)
        self.assertNotIn('toggleGlobalPause', page)
        self.assertNotIn('get_download_state', page)
        self.assertIn("selectActiveStatus('active')", page)
        self.assertIn("selectActiveStatus('paused')", page)
        self.assertEqual(page.count('id="task-search"'), 1)
        script = re.sub(r'{{.*?}}', "''", re.findall(r'<script>(.*?)</script>', page, re.S)[-1])
        self.run_script(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const context = { WebI18n: {language:'en'}, Headers, AbortController,
    setTimeout: () => 1, clearTimeout() {} };
vm.createContext(context);
vm.runInContext(SOURCE, context);
(async () => {
    const app = context.app();
    app.activeTasks = [{task_id:'1', filename:'Beach.mp4'}, {task_id:'2', filename:'other.zip'}];
    app.failedTasks = [{task_id:'3', task_id_display:'1015-9', filename:'bad.zip'}];
    app.taskSearch = ' bEaCh ';
    assert.equal(app.filteredActiveTasks.length, 1);
    app.toggleSelectAll('active');
    assert.deepEqual([...app.selectedTasks], ['1']);
    app.taskSearch = '1015-9';
    assert.equal(app.filteredFailedTasks.length, 1);
    app.toggleSelectAll('failed');
    assert.deepEqual([...app.selectedTasks], ['3']);
    app.taskSearch = 'old';
    let release;
    context.authenticatedFetch = async () => ({json: () => new Promise(done => {release = done;})});
    const outdated = app.loadMoreCompleted();
    await new Promise(done => setImmediate(done));
    app.taskSearch = 'new';
    app.onTaskSearchInput();
    assert.equal(app.selectedTasks.length, 0);
    release({tasks:[{task_id:'old', download_progress_raw:100}], total:1});
    await outdated;
    assert.equal(app.completedTasks.length, 0, 'old responses must not replace the new search');
    app.completedLoading = false;
    context.authenticatedFetch = async url => {
        assert.ok(url.includes('search=new'));
        return {json: async () => ({tasks:[{task_id:'new', download_progress_raw:100}], total:1})};
    };
    await app.loadMoreCompleted();
    assert.equal(app.completedTasks[0].task_id, 'new');
    app.pendingLoading = false;
    await app.fetchPending(true);
    assert.equal(app.pendingTasks[0].task_id, 'new');
    app.pendingLoading = false;
    app.taskSearch = 'old';
    context.authenticatedFetch = async () => ({json: () => new Promise(done => {release = done;})});
    const pending = app.fetchPending(true);
    await new Promise(done => setImmediate(done));
    app.taskSearch = 'newer';
    app.onTaskSearchInput();
    release({tasks:[{task_id:'old'}], total:1});
    await pending;
    assert.equal(app.pendingTasks.length, 0);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''.replace('SOURCE', json.dumps(script)))

    def test_pending_search_precedes_pagination_on_real_route(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from module import web

        tasks = [{'task_id': index, 'chat_id': 99, 'created_at': 0,
                  'extra_data': {'filename': f'video-{index}.mp4', 'task_id_display': f'1015-{index}'}}
                 for index in range(150)]
        with patch.object(web, '_app', SimpleNamespace(web_login_secret='')), \
                patch('module.download_service.get_pending_tasks', return_value=tasks), \
                patch('module.download_stat.get_download_result', return_value={}):
            client = web.get_flask_app().test_client()
            result = client.get('/get_pending_list?search=VIDEO-149&limit=1').json
            self.assertEqual(result['total'], 1)
            self.assertEqual(result['tasks'][0]['task_id'], '149')
            result = client.get('/get_pending_list?search=1015-14&offset=2&limit=1').json
            self.assertEqual(result['total'], 11)
            self.assertEqual(result['tasks'][0]['task_id'], '141')
            self.assertEqual(client.get('/get_pending_list?search=missing').json, {'tasks': [], 'total': 0})
            self.assertEqual(client.get('/get_pending_list?limit=100').json['total'], 150)

    def test_batch_pause_keeps_failed_selection(self):
        page = (ROOT / 'module/templates/index.html').read_text()
        script = re.sub(r'{{.*?}}', "''", re.findall(r'<script>(.*?)</script>', page, re.S)[-1])
        self.run_script(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const calls = [];
const context = {WebI18n:{language:'en'}};
vm.createContext(context);
vm.runInContext(SOURCE, context);
context.authenticatedFetch = async (url, options) => {
    assert.equal(options.method, 'POST');
    calls.push(url);
    return {json:async () => ({code:url.includes('failed') ? '0' : '1'})};
};
(async () => {
    const app = context.app();
    app.t = value => value;
    app.showToast = () => {};
    app.fetchActive = async () => {};
    app.activeTasks = [{task_id:'ok', status:'active'}, {task_id:'paused', status:'paused'}];
    app.selectedTasks = ['ok', 'paused'];
    assert.equal(app.canPauseSelected, false);
    assert.equal(app.canResumeSelected, false);
    assert.equal(app.mixedActiveSelection, true);
    await app.batchSetPaused(true);
    await app.batchSetPaused(false);
    assert.equal(calls.length, 0, 'mixed selections must not send mutations');
    app.activeStatusFilter = 'paused';
    app.selectedTasks = [];
    app.toggleSelectAll('active');
    assert.deepEqual([...app.selectedTasks], ['paused']);
    assert.equal(app.canResumeSelected, true);
    assert.equal(app.canPauseSelected, false);
    app.activeStatusFilter = 'active';
    app.selectedTasks = [];
    app.toggleSelectAll('active');
    assert.deepEqual([...app.selectedTasks], ['ok']);
    assert.equal(app.canPauseSelected, true);
    assert.equal(app.canResumeSelected, false);
    app.activeStatusFilter = 'all';
    app.selectActiveStatus('paused');
    assert.deepEqual([...app.selectedTasks], ['paused']);
    app.selectActiveStatus('active');
    assert.deepEqual([...app.selectedTasks], ['ok']);
    app.taskSearch = 'paused';
    app.selectActiveStatus('active');
    assert.equal(app.selectedTasks.length, 0, 'shortcuts must respect the current search');
    app.selectActiveStatus('paused');
    assert.deepEqual([...app.selectedTasks], ['paused']);
    app.taskSearch = '';
    app.activeStatusFilter = 'paused';
    app.selectActiveStatus('active');
    assert.equal(app.selectedTasks.length, 0, 'shortcuts must respect the current status filter');
    app.activeStatusFilter = 'all';
    app.activeTasks = [{task_id:'ok', status:'active'}, {task_id:'failed', status:'active'}];
    app.selectedTasks = ['ok', 'failed'];
    await app.batchSetPaused(true);
    assert.deepEqual(calls, ['/pause_task?task_id=ok', '/pause_task?task_id=failed']);
    assert.deepEqual([...app.selectedTasks], ['failed']);
    assert.equal(app.batchBusy, false);
    calls.length = 0;
    app.activeTasks[0].status = 'paused';
    app.selectedTasks = ['ok'];
    await app.batchSetPaused(false);
    assert.deepEqual(calls, ['/resume_task?task_id=ok']);
    assert.equal(app.selectedTasks.length, 0);
})().catch(error => {console.error(error); process.exitCode=1;});
'''.replace('SOURCE', json.dumps(script)))

    def test_pending_batch_cancellation_uses_queue_operation(self):
        page = (ROOT / 'module/templates/index.html').read_text()
        script = re.sub(r'{{.*?}}', "''", re.findall(r'<script>(.*?)</script>', page, re.S)[-1])
        self.run_script(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const calls = [];
const context = {WebI18n: {language:'en'}};
vm.createContext(context);
vm.runInContext(SOURCE, context);
context.authenticatedFetch = async url => {
    calls.push(url);
    return {json:async () => ({code:url.includes('bad') ? '0' : '1'})};
};
(async () => {
    const app = context.app();
    app.t = text => text;
    app.showToast = () => {};
    app.fetchPending = async () => {};
    app.activeTab = 'pending';
    app.pendingTasks = [{task_id:'good'},{task_id:'bad'}];
    app.pendingTotal = 2;
    app.selectedTasks = ['good','bad'];
    await app.batchDelete();
    assert.equal(app.deleteModal.isPending, true);
    assert.equal(app.deleteModal.deleteFile, false);
    await app.executeDelete();
    assert.deepEqual(calls, ['/remove_pending?task_id=good', '/remove_pending?task_id=bad']);
    assert.deepEqual([...app.selectedTasks], ['bad']);
    assert.equal(app.pendingTotal, 1);
    assert.equal(app.pendingTasks[0].task_id, 'bad');
})().catch(error => {console.error(error); process.exitCode=1;});
'''.replace('SOURCE', json.dumps(script)))
