"""Run the actual dashboard script against completion, pagination and request races."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard tests')
class DashboardCompletionTests(unittest.TestCase):
    def run_script(self, body):
        page = (ROOT / 'module/templates/index.html').read_text()
        source = re.sub(r'{{.*?}}', "''", re.findall(r'<script>(.*?)</script>', page, re.S)[-1])
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const context = {WebI18n:{language:'en'}};
vm.createContext(context);
vm.runInContext(SOURCE, context);
const ui = context.app();
const completed = id => ({task_id:id, status:'completed', download_progress_raw:100});
const response = value => ({ok:true, json:async()=>value});
(async()=>{
BODY
})().catch(error=>{console.error(error);process.exitCode=1;});
'''.replace('SOURCE', json.dumps(source)).replace('BODY', body)
        result = subprocess.run(['node'], input=script, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_status_keeps_rounded_active_and_paused_tasks_visible(self):
        self.run_script(r'''
const rows = ['active','paused'].map(status=>({task_id:status,status,download_progress_raw:100}));
context.authenticatedFetch = async url => {
    assert.equal(url,'/get_download_list?already_down=false');
    return response(rows);
};
await ui.fetchActive();
assert.deepEqual([...ui.activeTasks.map(task=>task.task_id)],['active','paused']);
''')

    def test_completion_is_loaded_before_removing_active_row_and_preserves_pages(self):
        self.run_script(r'''
ui.activeTasks=[{task_id:'new',status:'active',download_progress_raw:90}];
ui.completedLimit=2;
ui.completedTasks=[completed('old1'),completed('old0')];
ui.completedOffset=ui.completedLastTotal=ui.completedTotal=2;
const calls=[];
context.authenticatedFetch=async url=>{
    calls.push(url);
    if(url.includes('already_down=false'))return response([]);
    if(url==='/get_completed_count')return response({total:3});
    assert.equal(ui.activeTasks.length,1,'keep the previous active row until the completion check finishes');
    return response({tasks:[completed('new'),completed('old1')],total:3});
};
await ui.fetchActive();
assert.equal(ui.activeTasks.length,0);
assert.deepEqual([...ui.completedTasks.map(task=>task.task_id)],['new','old1','old0']);
assert.equal(ui.completedOffset,3);
assert.equal(ui.completedLastTotal,3);
assert.equal(ui.completedHasMore,false);
assert.equal(calls.length,3,'completion must not wait for the ten-second timer');
''')

    def test_pending_handoff_waits_for_successful_active_refresh(self):
        self.run_script(r'''
ui.pendingTasks=[{task_id:'pending'}];
ui.pendingTotal=1;
let readable=false;
context.authenticatedFetch=async url=>{
    if(url.includes('/get_pending_list'))return response({tasks:[],total:0});
    assert.equal(url,'/get_download_list?already_down=false');
    assert.equal(ui.pendingTasks.length,1,'do not remove a queued row before reading Active');
    if(!readable)throw Error('offline');
    return response([{task_id:'prepared',status:'active',phase:'preparing'}]);
};
await ui.pollPendingRefresh();
assert.equal(ui.pendingTasks.length,1,'preserve the row when the receiving list is unavailable');
readable=true;
await ui.pollPendingRefresh();
assert.equal(ui.pendingTasks.length,0);
assert.equal(ui.activeTasks[0].phase,'preparing');
''')

    def test_failed_list_fetch_does_not_acknowledge_count_and_retries(self):
        self.run_script(r'''
for(const failure of ['network','http','json']){
    const app=context.app();
    let attempts=0;
    context.authenticatedFetch=async url=>{
        if(url==='/get_completed_count')return response({total:1});
        attempts++;
        if(attempts===1){
            if(failure==='network')throw Error('offline');
            if(failure==='http')return {ok:false,json:async()=>({tasks:[],total:1})};
            return {ok:true,json:async()=>{throw Error('bad JSON');}};
        }
        return response({tasks:[completed('new')],total:1});
    };
    await app.pollCompletedCount();
    assert.equal(app.completedLastTotal,0);
    assert.equal(app.completedTotal,0);
    assert.equal(app.completedRefreshPending,true);
    await app.pollCompletedCount();
    assert.equal(attempts,2);
    assert.equal(app.completedTasks[0].task_id,'new');
    assert.equal(app.completedLastTotal,1);
    assert.equal(app.completedRefreshPending,false);
}
''')

    def test_switching_completed_refreshes_even_if_total_did_not_change(self):
        self.run_script(r'''
ui.completedTasks=[completed('old')];
ui.completedTotal=ui.completedLastTotal=1;
const calls=[];
context.authenticatedFetch=async url=>{
    calls.push(url);
    if(url==='/get_completed_count')return response({total:1});
    return response({tasks:[completed('replacement')],total:1});
};
await ui.showCompleted();
assert.equal(ui.activeTab,'completed');
assert.equal(calls.length,2);
assert.deepEqual([...ui.completedTasks.map(task=>task.task_id)],['replacement']);
''')

    def test_head_refresh_uses_list_snapshot_and_large_bursts_do_not_skip_pages(self):
        self.run_script(r'''
ui.completedLimit=2;
ui.completedLastTotal=ui.completedTotal=2;
ui.completedTasks=[completed('old1'),completed('old0')];
ui.completedOffset=2;
context.authenticatedFetch=async url=>url==='/get_completed_count' ? response({total:3}) :
    response({tasks:[completed('new4'),completed('new3')],total:6});
await ui.pollCompletedCount();
assert.equal(ui.completedLastTotal,6,'use the successful list snapshot, not the earlier count');
assert.deepEqual([...ui.completedTasks.map(task=>task.task_id)],['new4','new3']);
assert.equal(ui.completedOffset,2);
assert.equal(ui.completedHasMore,true);
context.authenticatedFetch=async url=>{
    assert.ok(url.includes('offset=2&limit=2'));
    return response({tasks:[completed('new2'),completed('new1')],total:6});
};
await ui.loadMoreCompleted();
assert.deepEqual([...ui.completedTasks.map(task=>task.task_id)],['new4','new3','new2','new1']);
assert.equal(ui.completedOffset,4);
''')

    def test_refreshes_coalesce_and_new_request_during_fetch_is_not_lost(self):
        self.run_script(r'''
let release;
let lists=0;
let counts=0;
context.authenticatedFetch=async url=>{
    if(url==='/get_completed_count'){counts++;return response({total:1});}
    lists++;
    return {ok:true,json:()=>new Promise(done=>{release=done;})};
};
const first=ui.pollCompletedCount(true);
while(!release)await new Promise(done=>setImmediate(done));
await ui.pollCompletedCount(true);
assert.equal(counts,1,'do not overlap refresh requests');
release({tasks:[completed('one')],total:1});
await first;
assert.equal(ui.completedRefreshPending,true,'a newer completion check must survive the old response');
context.authenticatedFetch=async url=>url==='/get_completed_count' ? response({total:2}) :
    response({tasks:[completed('two'),completed('one')],total:2});
await ui.pollCompletedCount();
assert.deepEqual([...ui.completedTasks.map(task=>task.task_id)],['two','one']);
assert.equal(ui.completedRefreshPending,false);
''')

    def test_pagination_cannot_acknowledge_new_head_rows_it_did_not_fetch(self):
        self.run_script(r'''
ui.completedLimit=2;
ui.completedTasks=[completed('old3'),completed('old2')];
ui.completedLastTotal=ui.completedTotal=4;
ui.completedOffset=2;
context.authenticatedFetch=async()=>response({tasks:[completed('old2'),completed('old1')],total:5});
await ui.loadMoreCompleted();
assert.equal(ui.completedLastTotal,4);
context.authenticatedFetch=async url=>url==='/get_completed_count' ? response({total:5}) :
    response({tasks:[completed('new'),completed('old3')],total:5});
await ui.pollCompletedCount();
assert.deepEqual([...ui.completedTasks.map(task=>task.task_id)],['new','old3','old2','old1']);
assert.equal(ui.completedOffset,4);
assert.equal(ui.completedHasMore,true);
''')

    def test_query_change_discards_inflight_head_without_blocking_search(self):
        self.run_script(r'''
let release;
context.authenticatedFetch=async url=>url==='/get_completed_count' ? response({total:1}) :
    {ok:true,json:()=>new Promise(done=>{release=done;})};
const old=ui.pollCompletedCount(true);
while(!release)await new Promise(done=>setImmediate(done));
ui.taskSearch='match';
ui.completedRequestId++;
context.authenticatedFetch=async url=>{
    if(url==='/get_failed_downloads')return response([]);
    assert.ok(url.includes('search=match'));
    return response({tasks:[completed('match')],total:1});
};
await ui.showCompleted();
release({tasks:[completed('stale')],total:1});
await old;
assert.deepEqual([...ui.completedTasks.map(task=>task.task_id)],['match']);
assert.equal(ui.completedLastTotal,0,'filtered counts must not acknowledge unfiltered history');
''')

    def test_failed_refresh_retries_on_next_cycle_instead_of_ten_second_timer(self):
        self.run_script(r'''
let now=0;
let cycle;
let listAttempts=0;
context.Date={now:()=>now};
context.window={addEventListener(){},removeEventListener(){}};
context.startDashboardPolling=refresh=>{cycle=refresh;return ()=>{};};
context.authenticatedFetch=async url=>{
    if(url==='/get_completed_count')return response({total:1});
    assert.ok(url.startsWith('/get_download_list?already_down=true'), 'only completed-list requests count as refresh attempts');
    if(++listAttempts===1)throw Error('offline');
    return response({tasks:[completed('new')],total:1});
};
await ui.pollCompletedCount();
for(const name of ['fetchVersion','fetchActive','fetchPending','fetchCompleted','fetchSkipped',
    'fetchDownloadStatus','fetchFloodWait','fetchBotStatus'])ui[name]=async()=>{};
ui.$nextTick=()=>{};
await ui.init();
now=1000;
await cycle();
assert.equal(listAttempts,2,'pending refresh must not wait for the ten-second fallback');
assert.equal(ui.completedTasks[0].task_id,'new');
assert.equal(ui.completedRefreshPending,false);
''')

    def test_single_and_batch_deletion_use_the_same_confirmed_head_refresh(self):
        self.run_script(r'''
context.WebI18n.t=text=>text;
for(const batch of [false,true]){
    const app=context.app();
    app.showToast=()=>{};
    app.completedTasks=[completed('deleted')];
    app.completedTotal=app.completedLastTotal=1;
    app.deleteModal={show:true,isBatch:batch,isPending:false,taskIds:['deleted'],deleteFile:false};
    const calls=[];
    context.authenticatedFetch=async(url,options)=>{
        calls.push(url);
        if(url.startsWith('/delete_task')||url==='/batch_delete'){
            assert.equal(options.method,'POST');
            return response({code:'1',deleted:1});
        }
        if(url==='/get_completed_count')return response({total:1});
        return response({tasks:[completed('new')],total:1});
    };
    await app.executeDelete();
    assert.equal(app.completedTasks[0].task_id,'new','same-total replacement must appear after deletion');
    assert.equal(app.completedLastTotal,1);
    assert.equal(app.completedOffset,1);
    assert.equal(app.deleteModal.show,false);
    assert.equal(calls.length,3);
}
''')

    def test_active_completion_refresh_respects_current_search(self):
        self.run_script(r'''
ui.taskSearch='match';
ui.activeTasks=[{task_id:'match',status:'active',download_progress_raw:90}];
context.authenticatedFetch=async url=>{
    if(url.includes('already_down=false'))return response([]);
    if(url==='/get_failed_downloads')return response([]);
    assert.ok(url.includes('search=match'));
    return response({tasks:[completed('match')],total:1});
};
await ui.fetchActive();
assert.equal(ui.activeTasks.length,0);
assert.equal(ui.completedTasks[0].task_id,'match');
assert.equal(ui.taskSearch,'match');
''')

    def test_pending_filter_change_rejects_the_previous_inflight_page(self):
        self.run_script(r'''
ui.queueScrollCheck = () => {};
let finishPrevious;
const urls = [];
context.authenticatedFetch = async url => {
    urls.push(url);
    if (url.includes('status=all')) return new Promise(resolve => { finishPrevious = resolve; });
    assert.ok(url.includes('status=paused'));
    return response({tasks:[{task_id:'held',status:'paused'}],total:1});
};
const previous = ui.fetchPending(true);
ui.pendingStatusFilter = 'paused';
await ui.fetchPending(true);
finishPrevious(response({tasks:[{task_id:'queued',status:'pending'}],total:1}));
await previous;
assert.deepEqual([...ui.pendingTasks.map(task => task.task_id)], ['held']);
assert.equal(ui.pendingTotal, 1);
assert.equal(ui.pendingLoading, false);
assert.equal(urls.length, 2);
''')
