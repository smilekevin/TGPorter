#!/usr/bin/env python3
"""Standalone Flask WebUI with mock data for local development.

Does not import module/web.py or require Pyrogram.
Run: python run_local.py
Open: http://localhost:5088"""

import os, json, time

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, Response, jsonify, render_template, request

import utils
from lang import template_context

app = Flask(__name__, 
    template_folder=os.path.join(os.path.dirname(__file__), 'module', 'templates'),
    static_folder=os.path.join(os.path.dirname(__file__), 'module', 'static'),
    static_url_path='/module/static')

app.context_processor(template_context)

app.secret_key = "tdl-dev"

# Mock data.
MOCK_ACTIVE = [
    {
        "task_id": "active_001",
        "chat": "Photography group",
        "id": "10472",
        "filename": "2024_Summer_Vacation_Beach_Sunset_4K_HDR.mp4",
        "total_size": "1.2 GB",
        "total_size_bytes": 1288490188,
        "download_progress": "67.3",
        "download_progress_raw": 67.3,
        "download_speed": "8.5 MB/s",
        "active_streams": 3,
        "save_path": "/app/downloads/Photography group/2024_Summer_Vacation.mp4",
        "status": "active"
    }
]

MOCK_COMPLETED = [
    {
        "task_id": "done_001",
        "chat": "Design resources",
        "id": "8915",
        "filename": "Figma_UI_Kit_2025_Complete_Pack.zip",
        "total_size": "342 MB",
        "total_size_bytes": 358612992,
        "download_progress": "100.0",
        "download_progress_raw": 100.0,
        "download_speed": "0 B/s",
        "active_streams": 0,
        "save_path": "/app/downloads/Design resources/Figma_UI_Kit_2025_Complete_Pack.zip",
        "status": "completed"
    }
]

MOCK_FAILED = [
    {
        "task_id": "fail_001",
        "chat": "Movie resources",
        "id": "15623",
        "filename": "[4K-HDR] Dune.Part.Two.2024.2160p.mkv",
        "error_message": "File reference expired (failed after 3 retries)",
        "total_size": "18.5 GB"
    }
]

MOCK_ACTIVE.extend([
    dict(MOCK_ACTIVE[0], task_id="active_002", filename="Mountain_Trail_4K.mp4",
         download_progress="24.5", download_progress_raw=24.5, download_speed="3.2 MB/s"),
    dict(MOCK_ACTIVE[0], task_id="active_003", filename="Workshop_Recording.mp4",
         download_progress="0", download_progress_raw=0, download_speed="0 B/s",
         phase="preparing", wait_reason="Establishing download connections", active_streams=0),
])
MOCK_PENDING = [
    {"task_id": f"pending_{index:03d}", "task_id_display": f"1021-{index + 3}",
     "filename": filename, "chat_title": "Media archive", "chat": "Media archive",
     "total_size": size, "source_type": "Channel post", "url": f"https://t.me/example/{index}",
     "created_at": "10-21 10:30:00", "created_ts": time.time() - 600 + index,
     "wait_time": "10m", "queue_label": "Paused" if paused else "Queued",
     "status": "paused" if paused else "pending", "wait_seconds": 0,
     "wait_reason": "Paused; resume manually" if paused else "Waiting for download slot"}
    for index, (filename, size, paused) in enumerate([
        ("City_Walk_Episode_01.mp4", "820 MB", False),
        ("Design_Assets.zip", "245 MB", True),
        ("Ocean_Documentary.mp4", "1.8 GB", False),
        ("Architecture_Collection.zip", "640 MB", True),
        ("Piano_Concert.flac", "320 MB", False),
        ("Travel_Film_4K.mp4", "2.1 GB", True),
    ], 1)
]

# API routes.
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/get_download_list")
def get_download_list():
    already_down = request.args.get("already_down") == "true"
    data = MOCK_COMPLETED if already_down else MOCK_ACTIVE
    if already_down:
        query = request.args.get("search", "").strip().lower()
        data = [task for task in data if not query or query in
                f"{task['task_id']} {task['filename']}".lower()]
        offset = max(0, request.args.get("offset", 0, type=int))
        limit = max(0, request.args.get("limit", 0, type=int))
        tasks = data[offset:offset + limit] if limit else data
        return jsonify(tasks=tasks, total=len(data), offset=offset, limit=limit)
    return Response(json.dumps(data), mimetype='application/json')


@app.route("/get_completed_count")
def get_completed_count():
    return jsonify(total=len(MOCK_COMPLETED))

@app.route("/get_failed_downloads")
def get_failed_downloads():
    return Response(json.dumps(MOCK_FAILED), mimetype='application/json')

@app.route("/get_download_status")
def get_download_status():
    return jsonify(download_speed="8.5 MB/s", upload_speed="0.00 B/s", active_streams=3)

@app.route("/get_app_version")
def get_app_version():
    return utils.__version__

@app.route("/get_bot_status")
def get_bot_status():
    # Mock mode has no Telegram session to probe.
    return jsonify(status="disabled", last_checked_at=None, last_success_at=None,
                   rtt_ms=None, failures=0, flood_wait_remaining=0, heartbeat_interval=60)

@app.route("/set_download_state", methods=["POST"])
def set_download_state():
    return "pause"

@app.route("/delete_task", methods=["POST"])
def delete_task():
    return jsonify(code="1", message="deleted")

@app.route("/retry_task", methods=["POST"])
def retry_task():
    return jsonify(code="1", message="retry queued")

@app.route("/pause_task", methods=["POST"])
def pause_task():
    task_id = request.args.get('task_id')
    task = next((task for task in MOCK_PENDING if task['task_id'] == task_id), None)
    if task is None:
        active = next((task for task in MOCK_ACTIVE if task['task_id'] == task_id), None)
        if active is None:
            return jsonify(code="0", message="Task not found")
        MOCK_ACTIVE.remove(active)
        task = dict(active, created_ts=time.time(), created_at="10-21 10:40:00",
                    wait_time="0m", source_type="Channel post", url="", wait_seconds=0)
        MOCK_PENDING.append(task)
    task.update(status='paused', queue_label='Paused', wait_reason='Paused; resume manually')
    return jsonify(code="1", message="paused", pending_task_id=task_id)

@app.route("/resume_task", methods=["POST"])
def resume_task():
    task_id = request.args.get('task_id')
    task = next((task for task in MOCK_PENDING if task['task_id'] == task_id), None)
    if task is None or task['status'] != 'paused':
        return jsonify(code="0", message="Task not paused")
    task.update(status='pending', queue_label='Queued', wait_reason='Waiting for download slot')
    return jsonify(code="1", message="resumed", pending_task_id=task_id)


@app.route('/get_pending_list')
def get_pending_list():
    query = request.args.get('search', '').strip().lower()
    status = request.args.get('status', 'all')
    rows = [task for task in MOCK_PENDING if
            (status == 'all' or task['status'] == status) and
            (not query or query in f"{task['task_id']} {task.get('task_id_display', '')} {task['filename']}".lower())]
    rows.sort(key=lambda task: task['created_ts'])
    offset = max(0, request.args.get('offset', 0, type=int))
    limit = max(0, request.args.get('limit', 100, type=int))
    return jsonify(tasks=rows[offset:offset + limit] if limit else rows[offset:], total=len(rows))


@app.route('/remove_pending', methods=['POST'])
def remove_pending():
    MOCK_PENDING[:] = [task for task in MOCK_PENDING if task['task_id'] != request.args.get('task_id')]
    return jsonify(code='1', message='removed')


@app.route('/get_flood_wait')
def get_flood_wait():
    return jsonify(active=False, remaining=0, reason='', throttle={})


@app.route('/get_skipped_downloads')
def get_skipped_downloads():
    return jsonify(tasks=[], total=0)


if __name__ == "__main__":
    port = int(os.environ.get('TGPORTER_MOCK_PORT', '5088'))
    print("=" * 50)
    print("  TG Porter - Dev Mode")
    print(f"  Browser: http://localhost:{port}")
    print("  Refresh after editing frontend files")
    print("=" * 50)
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.run("127.0.0.1", port, debug=True, use_reloader=False)
