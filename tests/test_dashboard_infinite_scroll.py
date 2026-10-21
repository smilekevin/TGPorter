"""Exercise every dashboard list, including skipped paging against the real Flask API."""

import json
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard tests')
class DashboardInfiniteScrollTests(unittest.TestCase):
    def run_script(self, body):
        page = (ROOT / 'module/templates/index.html').read_text()
        source = re.sub(r'{{.*?}}', "''", re.findall(r'<script>(.*?)</script>', page, re.S)[-1])
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const ticks = [];
const timers = [];
const context = {
    WebI18n: {language: 'en', t: text => text},
    document: {documentElement: {scrollTop: 0, scrollHeight: 2000, clientHeight: 800}},
    setTimeout: callback => {timers.push(callback); return timers.length;}, clearTimeout() {},
};
vm.createContext(context);
vm.runInContext(SOURCE, context);
const ui = context.app();
const response = value => ({ok: true, json: async () => value});
const rows = (count, prefix = 'row') => Array.from({length: count}, (_, i) =>
    ({task_id: prefix + i, filename: 'video-' + i + '.mp4', status: 'active'}));
const settle = () => new Promise(done => setImmediate(done));
ui.showToast = () => {};
(async () => {
BODY
})().catch(error => {console.error(error); process.exitCode = 1;});
'''.replace('SOURCE', json.dumps(source)).replace('BODY', body)
        result = subprocess.run(['node'], input=script, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_tab_uses_the_same_bottom_threshold(self):
        self.run_script(r'''
ui.activeTasks = rows(250);
ui.failedTasks = rows(250);
const calls = [];
context.authenticatedFetch = async url => {
    const tab = url.includes('already_down=true') ? 'completed' : url.includes('pending') ? 'pending' : 'skipped';
    calls.push(tab);
    return response({tasks: [{task_id: tab, status: tab}], total: 1});
};
for (const tab of ['active', 'pending', 'completed', 'skipped', 'failed']) {
    ui.activeTab = tab;
    context.document.documentElement.scrollTop = 899;
    await ui.onScroll();
    assert.equal(calls.length, 0);
    assert.equal(ui.activeVisibleLimit, 100);
    assert.equal(ui.failedVisibleLimit, 100);
}
context.document.documentElement.scrollTop = 900;
for (const tab of ['active', 'pending', 'completed', 'skipped', 'failed']) {
    ui.activeTab = tab;
    await ui.onScroll();
}
assert.deepEqual(calls, ['pending', 'completed', 'skipped']);
assert.equal(ui.filteredActiveTasks.length, 200);
assert.equal(ui.filteredFailedTasks.length, 200);
ui.disposed = true;
await ui.onScroll();
assert.equal(ui.failedVisibleLimit, 200);
''')

    def test_live_lists_search_before_slicing_and_select_only_displayed_rows(self):
        self.run_script(r'''
ui.activeTasks = rows(250);
ui.failedTasks = rows(250);
for (const tab of ['active', 'failed']) {
    ui.activeTab = tab;
    assert.equal(ui.visibleTaskCount, 100);
    assert.equal(ui.currentTaskTotal, 250);
    assert.equal(ui.listHasMore, true);
    ui.toggleSelectAll(tab);
    assert.equal(ui.selectedTasks.length, 100);
    assert.equal(ui.selectedTasks.includes('row100'), false);
    context.document.documentElement.scrollTop = 1200;
    await ui.onScroll();
    assert.equal(ui.visibleTaskCount, 200);
    assert.equal(ui.selectedTasks.length, 100, 'appended rows must not be selected automatically');
    await ui.onScroll();
    assert.equal(ui.visibleTaskCount, 250);
    assert.equal(ui.listHasMore, false);
    ui.taskSearch = 'video-249';
    assert.equal(ui.visibleTasks[0].task_id, 'row249', 'search includes rows outside the first page');
    ui.onTaskSearchInput();
    assert.equal(ui.activeVisibleLimit, 100);
    assert.equal(ui.failedVisibleLimit, 100);
    ui.taskSearch = '';
}
ui.$refs = {statusFilter: {open: true}, statusFilterButton: {focus() {}}};
ui.activeTasks[249].status = 'paused';
ui.activeVisibleLimit = 200;
ui.chooseActiveStatus('paused');
assert.equal(ui.filteredActiveTasks.length, 1);
assert.equal(ui.filteredActiveTasks[0].task_id, 'row249');
assert.equal(ui.activeVisibleLimit, 100);
''')

    def test_skipped_append_and_refresh_respect_real_api_page_cap(self):
        from module import web

        records = [{'task_id': f'row{i}', 'file_name': f'video-{i}.mp4', 'timestamp': 1000 - i}
                   for i in range(450)]
        with patch.object(web, '_app', SimpleNamespace(web_login_secret='')), \
                patch('module.download_stat.get_skipped_downloads', return_value=records):
            client = web.get_flask_app().test_client()
            pages = {}
            for offset, limit in [(0, 100), (100, 100), (200, 100), (0, 200), (200, 200)]:
                result = client.get(f'/get_skipped_downloads?offset={offset}&limit={limit}')
                self.assertEqual(result.status_code, 200)
                pages[f'{offset}:{limit}'] = result.json
            self.assertEqual(len(client.get('/get_skipped_downloads?limit=400').json['tasks']), 200)
            self.assertEqual(client.get('/get_skipped_downloads?search=VIDEO-449&limit=1').json['tasks'][0]['task_id'], 'row449')
        self.run_script('const pages = ' + json.dumps(pages) + ';\n' + r'''
ui.activeTab = 'skipped';
const requests = [];
context.authenticatedFetch = async url => {
    const params = new URL(url, 'http://localhost').searchParams;
    const key = params.get('offset') + ':' + params.get('limit');
    requests.push(key);
    assert.ok(pages[key], key);
    return response(pages[key]);
};
await ui.fetchSkipped();
assert.equal(ui.skippedTasks.length, 100);
assert.equal(ui.skippedTotal, 450);
ui.selectedTasks = ['row1'];
await ui.loadMoreSkipped();
await ui.loadMoreSkipped();
assert.equal(ui.skippedTasks.length, 300);
assert.equal(ui.skippedOffset, 300);
assert.deepEqual([...ui.selectedTasks], ['row1']);
await ui.fetchSkipped();
assert.deepEqual(requests.slice(-2), ['0:200', '200:100']);
assert.equal(ui.skippedTasks.length, 300, 'polling preserves every loaded page');
assert.equal(ui.skippedHasMore, true);
''')

    def test_skipped_refresh_after_deletion_replaces_loaded_range(self):
        self.run_script(r'''
ui.activeTab = 'skipped';
let data = rows(350);
context.authenticatedFetch = async url => {
    const params = new URL(url, 'http://localhost').searchParams;
    const start = Number(params.get('offset'));
    const limit = Math.min(200, Number(params.get('limit')));
    return response({tasks: data.slice(start, start + limit), total: data.length});
};
await ui.fetchSkipped();
await ui.loadMoreSkipped();
await ui.loadMoreSkipped();
ui.selectedTasks = ['row1', 'row299'];
data = [rows(1, 'new')[0], ...data.filter(task => !['row1', 'row200'].includes(task.task_id))];
await ui.fetchSkipped();
assert.equal(ui.skippedOffset, 300);
assert.equal(ui.skippedTasks[0].task_id, 'new0');
assert.equal(ui.skippedTasks[299].task_id, 'row300');
assert.equal(ui.skippedTasks.some(task => task.task_id === 'row200'), false);
assert.deepEqual([...ui.selectedTasks], ['row299']);
data = data.slice(0, 5);
await ui.fetchSkipped();
assert.equal(ui.skippedTasks.length, 5);
assert.equal(ui.skippedOffset, 5);
assert.equal(ui.skippedHasMore, false);
assert.equal(ui.selectedTasks.length, 0);
''')

    def test_skipped_loading_deduplicates_requests_and_retries_failed_pages(self):
        self.run_script(r'''
ui.skippedTasks = rows(100);
ui.skippedOffset = 100;
let release;
let calls = 0;
context.authenticatedFetch = async () => {
    calls++;
    return new Promise(done => {release = done;});
};
const loading = ui.loadMoreSkipped();
await ui.loadMoreSkipped();
assert.equal(calls, 1);
release({ok: false});
await loading;
assert.equal(ui.skippedLoading, false);
assert.equal(ui.skippedOffset, 100);
assert.equal(ui.skippedHasMore, true);
context.authenticatedFetch = async url => {
    assert.ok(url.includes('offset=100'));
    return response({tasks: rows(10, 'next'), total: 110});
};
await ui.loadMoreSkipped();
assert.equal(ui.skippedTasks.length, 110);
assert.equal(ui.skippedHasMore, false);
await ui.loadMoreSkipped();
''')

    def test_skipped_search_and_refresh_discard_stale_pages(self):
        self.run_script(r'''
ui.activeTab = 'skipped';
ui.skippedOffset = 100;
ui.skippedTasks = rows(100);
let release;
context.authenticatedFetch = async () => new Promise(done => {release = done;});
const old = ui.loadMoreSkipped();
ui.taskSearch = 'new';
ui.onTaskSearchInput();
release(response({tasks: rows(100), total: 200}));
await old;
assert.equal(ui.skippedTasks.length, 0);
assert.equal(ui.skippedLoading, true, 'the old request cannot clear the new search loading state');
context.authenticatedFetch = async url => {
    assert.ok(url.includes('search=new'));
    assert.ok(url.includes('offset=0'));
    return response({tasks: rows(1, 'new'), total: 1});
};
await ui.fetchSkipped(true);
assert.equal(ui.skippedTasks[0].task_id, 'new0');
assert.equal(ui.skippedLoading, false);
ui.taskSearch = '';
ui.skippedHasMore = true;
context.authenticatedFetch = async () => new Promise(done => {release = done;});
const append = ui.loadMoreSkipped();
context.authenticatedFetch = async () => response({tasks: rows(1, 'replacement'), total: 1});
await ui.fetchSkipped();
release(response({tasks: rows(100, 'stale'), total: 200}));
await append;
assert.equal(ui.skippedTasks.length, 1);
assert.equal(ui.skippedTasks[0].task_id, 'replacement0');
''')

    def test_short_viewport_fills_without_scrolling_and_errors_do_not_spin(self):
        self.run_script(r'''
ui.activeTab = 'skipped';
ui.skippedLimit = 2;
ui.$nextTick = callback => ticks.push(callback);
context.document.documentElement.scrollHeight = 700;
const data = rows(5);
let calls = 0;
context.authenticatedFetch = async url => {
    calls++;
    const params = new URL(url, 'http://localhost').searchParams;
    const offset = Number(params.get('offset'));
    const limit = Number(params.get('limit'));
    return response({tasks: data.slice(offset, offset + limit), total: data.length});
};
await ui.fetchSkipped();
for (let i = 0; ticks.length && i < 10; i++) {
    ticks.shift()();
    await settle();
}
assert.equal(calls, 3);
assert.equal(ui.skippedTasks.length, 5);
assert.equal(ui.skippedHasMore, false);
ui.skippedHasMore = true;
context.authenticatedFetch = async () => {calls++; throw new Error('offline');};
await ui.loadMoreSkipped();
assert.equal(ticks.length, 0, 'failed requests must wait for scrolling or polling before retrying');
ui.queueScrollCheck();
ui.disposed = true;
ticks.shift()();
assert.equal(calls, 4, 'queued checks must stop after the dashboard is removed');
''')

    def test_pending_refresh_reopens_exhausted_list_when_new_tasks_arrive(self):
        self.run_script(r'''
ui.pendingLimit = 2;
ui.pendingTasks = rows(2);
ui.pendingOffset = ui.pendingTotal = 2;
ui.pendingHasMore = false;
ui.fetchActive = async () => true;
context.authenticatedFetch = async () => response({tasks: rows(2), total: 5});
await ui.pollPendingRefresh();
assert.equal(ui.pendingHasMore, true);
assert.equal(ui.pendingOffset, 2);
context.authenticatedFetch = async () => response({tasks: [rows(1, 'remaining')[0]], total: 1});
await ui.pollPendingRefresh();
assert.equal(ui.pendingOffset, 1);
assert.equal(ui.pendingHasMore, false);
assert.equal(ui.pendingTasks.length, 1);
''')

    def test_shared_loader_handles_loading_refresh_failure_and_stale_results_for_every_tab(self):
        self.run_script(r'''
for (const tab of ['active', 'pending', 'completed', 'skipped', 'failed']) {
    const app = context.app();
    app.activeTab = tab;
    app.showToast = () => {};
    let finish;
    const source = app.taskListSource(tab);
    const row = {task_id: 'same', filename: 'first.mp4', status: tab};
    const payload = task => source.snapshot ? [task] : {tasks: [task], total: 1};
    context.authenticatedFetch = async () => new Promise(done => {finish = done;});
    const first = app.loadTaskList(tab, {mode: 'refresh'});
    assert.equal(app[tab + 'Loading'], true, tab);
    assert.equal(app.searchLoading, true, tab);
    assert.equal(app.taskListIsEmpty(tab), false, 'do not announce empty while a first read is pending');
    assert.equal(await app.loadTaskList(tab, {mode: 'refresh'}), false, 'coalesce concurrent requests');
    finish(response(payload(row)));
    assert.equal(await first, true);
    assert.equal(app[tab + 'Loading'], false);
    const original = app[tab + 'Tasks'][0];
    context.authenticatedFetch = async () => response(payload({...row, filename: 'updated.mp4'}));
    await app.loadTaskList(tab, {mode: 'refresh'});
    assert.equal(app[tab + 'Tasks'][0], original, 'refreshes must retain card identity');
    assert.equal(original.filename, 'updated.mp4');
    for (const bad of [{ok: false}, response({tasks: null, total: 1})]) {
        context.authenticatedFetch = async () => bad;
        assert.equal(await app.loadTaskList(tab, {mode: 'refresh'}), false);
        assert.equal(app[tab + 'Tasks'][0], original);
        assert.equal(app[tab + 'Loading'], false);
    }
    context.authenticatedFetch = async () => new Promise(done => {finish = done;});
    const old = app.loadTaskList(tab, {mode: 'refresh'});
    app.taskSearch = 'new query';
    finish(response(payload({...row, filename: 'stale.mp4'})));
    assert.equal(await old, false);
    assert.equal(original.filename, 'updated.mp4');
    assert.equal(app[tab + 'Loading'], false);
    context.authenticatedFetch = async () => response(source.snapshot ? [] : {tasks: [], total: 0});
    await app.loadTaskList(tab, {mode: 'refresh'});
    assert.equal(app.taskListIsEmpty(tab), true);
    assert.equal(app.taskListIsFiltered(tab), true);
}
''')

    def test_shared_append_keeps_selection_and_refresh_preserves_all_loaded_pages(self):
        self.run_script(r'''
for (const tab of ['active', 'pending', 'completed', 'skipped', 'failed']) {
    const app = context.app();
    app.activeTab = tab;
    app.showToast = () => {};
    const source = app.taskListSource(tab);
    const data = rows(250).map(task => ({...task, status: tab}));
    context.authenticatedFetch = async url => {
        if (source.snapshot) return response(data);
        const params = new URL(url, 'http://localhost').searchParams;
        const start = Number(params.get('offset'));
        const limit = Number(params.get('limit'));
        return response({tasks: data.slice(start, start + limit), total: data.length});
    };
    await app.loadTaskList(tab, {mode: 'refresh'});
    assert.equal(app.visibleTaskCount, 100, tab);
    app.selectedTasks = ['row1'];
    await app.loadMoreTasks();
    assert.equal(app.visibleTaskCount, 200, tab);
    assert.deepEqual([...app.selectedTasks], ['row1']);
    await app.loadTaskList(tab, {mode: 'refresh'});
    assert.equal(app.visibleTaskCount, 200, tab);
    await app.loadMoreTasks();
    assert.equal(app.visibleTaskCount, 250, tab);
    assert.equal(app.listHasMore, false, tab);
}
''')

    def test_background_refresh_keeps_empty_and_populated_lists_visible(self):
        self.run_script(r'''
for (const tab of ['active', 'pending', 'completed', 'skipped', 'failed']) {
    for (const count of [0, 1]) {
        const app = context.app();
        app.activeTab = tab;
        app.showToast = () => {};
        const source = app.taskListSource(tab);
        const tasks = rows(count).map(task => ({...task, status: tab}));
        const data = source.snapshot ? tasks : {tasks, total: count};
        context.authenticatedFetch = async () => response(data);
        await app.loadTaskList(tab, {mode: 'refresh'});
        let finish;
        context.authenticatedFetch = async () => new Promise(done => {finish = done;});
        const refresh = app.loadTaskList(tab, {mode: tab === 'completed' ? 'head' : 'refresh'});
        assert.equal(app[tab + 'Loading'], true, 'still serialize requests during a background read');
        assert.equal(app.searchLoading, false, tab + ' must not announce background polling');
        assert.equal(app.taskListIsEmpty(tab), count === 0, 'retain the established empty state');
        assert.equal(app.visibleTaskCount, count, 'retain established rows');
        finish(response(data));
        await refresh;
        assert.equal(app.searchLoading, false);
    }
}
''')

    def test_first_read_reset_and_append_still_announce_loading(self):
        self.run_script(r'''
for (const tab of ['active', 'pending', 'completed', 'skipped', 'failed']) {
    const app = context.app();
    app.activeTab = tab;
    app.showToast = () => {};
    const source = app.taskListSource(tab);
    let finish;
    const empty = source.snapshot ? [] : {tasks: [], total: 0};
    context.authenticatedFetch = async () => new Promise(done => {finish = done;});
    const first = app.loadTaskList(tab, {mode: 'refresh'});
    assert.equal(app.searchLoading, true, tab);
    assert.equal(app.taskListIsEmpty(tab), false);
    finish(response(empty));
    await first;
    const reset = app.loadTaskList(tab, {reset: true, mode: 'refresh'});
    assert.equal(app.searchLoading, true, 'explicit filters/search reset must indicate loading');
    assert.equal(app.taskListIsEmpty(tab), false);
    finish(response(empty));
    await reset;
    if (!source.snapshot) {
        app[tab + 'HasMore'] = true;
        const page = app.loadMoreTasks();
        assert.equal(app.searchLoading, true, 'appending a server page must indicate loading');
        finish(response(empty));
        await page;
    }
}
''')
