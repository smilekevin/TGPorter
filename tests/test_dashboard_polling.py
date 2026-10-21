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
    setTimeout: (callback, delay) => { assert.equal(delay, 3000); timers.set(++serial, callback); return serial; },
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
let observers = 0;
const listeners = new Map();
const context = {
    WebI18n: {language: 'en'}, Date,
    ResizeObserver: class { constructor() { observers++; } observe() {} disconnect() { observers--; } },
    window: {
        matchMedia: () => ({matches: false}),
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
    const attachDom = target => {
        target.$nextTick = callback => callback();
        target.$refs = {downloadNavigation: {}, downloadTabs: {children: [], clientWidth: 800, scrollWidth: 500}};
    };
    attachDom(app);
    const names = ['fetchVersion', 'fetchActive', 'fetchPending',
        'fetchCompleted', 'fetchSkipped', 'fetchFailed', 'fetchDownloadStatus', 'fetchFloodWait', 'fetchBotStatus'];
    let calls = 0;
    let releases = [];
    names.forEach(name => app[name] = () => {
        calls++;
        return new Promise(done => releases.push(done));
    });
    const initial = app.init();
    await app.init();
    assert.equal(calls, names.length, 'all initial requests should start without serial waits');
    assert.equal(polls, 0);
    releases.forEach(done => done());
    await initial;
    assert.equal(polls, 1);
    assert.equal(observers, 1);
    app.destroy();
    assert.equal(observers, 0);
    assert.equal(polls, 0);
    assert.equal(listeners.size, 0);
    const removed = context.app();
    attachDom(removed);
    releases = [];
    names.forEach(name => removed[name] = app[name]);
    const startup = removed.init();
    removed.destroy();
    releases.forEach(done => done());
    await startup;
    assert.equal(polls, 0, 'removed components must not begin polling after startup');
    assert.equal(observers, 0);
    assert.equal(listeners.size, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace('SOURCE', json.dumps(script)))

    def test_bot_status_refresh_and_failed_reads_clear_healthy_state(self):
        page = (ROOT / 'module/templates/index.html').read_text()
        script = re.sub(r'{{.*?}}', "''", re.findall(r'<script>(.*?)</script>', page, re.S)[-1])
        self.run_script(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const context = {WebI18n: {language:'en', t: (text, locale, values = {}) =>
    Object.entries(values).reduce((result, [key, value]) => result.replaceAll('{' + key + '}', value), text)}};
vm.createContext(context);
vm.runInContext(SOURCE, context);
(async () => {
    const app = context.app();
    const status = {status:'connected', rtt_ms:42, last_checked_at:1000};
    context.authenticatedFetch = async url => {
        assert.equal(url, '/get_bot_status');
        return {ok:true, json:async () => status};
    };
    await app.fetchBotStatus();
    assert.equal(app.botStatus.status, 'connected');
    assert.equal(app.botStatus.rtt_ms, 42);
    assert.equal(app.botStatusLabel, 'Connected');
    assert.ok(app.botLastChecked);
    assert.ok(app.botStatusDetails.includes('Telegram Bot: Connected'));
    assert.ok(app.botStatusDetails.includes('Latency: 42 ms'));
    assert.ok(app.botStatusDetails.includes('Last checked: ' + app.botLastChecked));
    for (const failure of [async () => {throw Error('network');},
        async () => ({ok:false}), async () => ({ok:true, json:async () => {throw Error('invalid JSON');}})]) {
        app.botStatus = status;
        context.authenticatedFetch = failure;
        await app.fetchBotStatus();
        assert.equal(app.botStatus.status, 'unknown');
        assert.equal(app.botStatus.rtt_ms, null);
        assert.equal(app.botLastChecked, '');
        assert.equal(app.botStatusDetails, 'Telegram Bot: Unknown');
    }
    app.botStatus = {status:'flood_wait', rtt_ms:null, last_checked_at:1000, flood_wait_remaining:30};
    assert.ok(app.botStatusDetails.includes('Retry in 30s'));
    context.authenticatedFetch = async () => ({ok:true, json:async () => ({status:'disabled', last_checked_at:null})});
    await app.fetchBotStatus();
    assert.equal(app.botStatusLabel, 'Not configured');
})().catch(error => {console.error(error); process.exitCode=1;});
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

    def test_skipped_page_selection_and_partial_delete(self):
        page = (ROOT / 'module/templates/index.html').read_text()
        script = re.sub(r'{{.*?}}', "''", re.findall(r'<script>(.*?)</script>', page, re.S)[-1])
        self.run_script(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const context = {WebI18n: {language:'en'}, Headers, AbortController};
vm.createContext(context);
vm.runInContext(SOURCE, context);
(async () => {
    const app = context.app();
    app.activeTab = 'skipped';
    app.skippedTasks = [{task_id:'a'}, {task_id:'b'}];
    app.toggleSelectAll('skipped');
    assert.deepEqual([...app.selectedTasks], ['a', 'b']);
    app.toggleSelectAll('skipped');
    assert.equal(app.selectedTasks.length, 0);
    app.toggleSelectAll('skipped');
    const requests = [];
    context.authenticatedFetch = async (url, options) => {
        assert.equal(options.method, 'POST');
        requests.push(url);
        return {json: async () => ({code: url.endsWith('a') ? '1' : '0'})};
    };
    app.showToast = () => {};
    app.t = key => key;
    app.fetchSkipped = async () => {};
    app.confirmBatchDelete = () => {throw new Error('must not offer file deletion');};
    await app.batchDelete();
    assert.deepEqual(requests, ['/delete_skipped?task_id=a', '/delete_skipped?task_id=b']);
    assert.deepEqual([...app.selectedTasks], ['b']);
    assert.equal(app.batchBusy, false);
    context.authenticatedFetch = async () => ({json: async () => ({code:'1'})});
    await app.batchDelete();
    assert.equal(app.selectedTasks.length, 0);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''.replace('SOURCE', json.dumps(script)))

    def test_search_filters_selection_and_discards_outdated_pages(self):
        page = (ROOT / 'module/templates/index.html').read_text()
        self.assertNotIn('overview-card', page)
        self.assertNotIn('toggleGlobalPause', page)
        self.assertNotIn('get_download_state', page)
        self.assertIn("selectActiveStatus('active')", page)
        self.assertNotIn("selectActiveStatus('paused')", page)
        self.assertIn("selectPendingStatus('paused')", page)
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
    release({tasks:[{task_id:'old', status:'completed', download_progress_raw:100}], total:1});
    await outdated;
    assert.equal(app.completedTasks.length, 0, 'old responses must not replace the new search');
    app.completedLoading = false;
    context.authenticatedFetch = async url => {
        assert.ok(url.includes('search=new'));
        return {json: async () => ({tasks:[{task_id:'new', status:'completed', download_progress_raw:100}], total:1})};
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
    app.fetchPending = async () => {};
    app.activeTab = 'pending';
    app.pendingTasks = [{task_id:'ok', status:'pending'}, {task_id:'paused', status:'paused'}];
    app.selectedTasks = ['ok', 'paused'];
    assert.equal(app.canPauseSelected, false);
    assert.equal(app.canResumeSelected, false);
    assert.equal(app.mixedActiveSelection, true);
    await app.batchSetPaused(true);
    await app.batchSetPaused(false);
    assert.equal(calls.length, 0, 'mixed selections must not send mutations');
    app.selectPendingStatus('paused');
    assert.deepEqual([...app.selectedTasks], ['paused']);
    assert.equal(app.canResumeSelected, true);
    assert.equal(app.canPauseSelected, false);
    app.selectPendingStatus('pending');
    assert.deepEqual([...app.selectedTasks], ['ok']);
    assert.equal(app.canPauseSelected, true);
    assert.equal(app.canResumeSelected, false);
    app.activeTab = 'active';
    app.activeTasks = [{task_id:'ok', status:'active'}, {task_id:'failed', status:'active'}];
    app.selectedTasks = ['ok', 'failed'];
    await app.batchSetPaused(true);
    assert.deepEqual(calls, ['/pause_task?task_id=ok', '/pause_task?task_id=failed']);
    assert.deepEqual([...app.selectedTasks], ['failed']);
    assert.equal(app.batchBusy, false);
    calls.length = 0;
    app.activeTab = 'pending';
    app.pendingTasks = [{task_id:'ok', status:'paused'}];
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
