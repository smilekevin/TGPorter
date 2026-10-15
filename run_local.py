#!/usr/bin/env python3
"""
本地开发专用 - 独立 Flask Web UI + Mock 数据
不依赖项目中的 module/web.py, 不依赖 pyrogram
用法: python run_local.py
访问: http://localhost:5000
"""

import os, json

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, Response, jsonify, render_template, request

import utils

app = Flask(__name__, 
    template_folder=os.path.join(os.path.dirname(__file__), 'module', 'templates'),
    static_folder=os.path.join(os.path.dirname(__file__), 'module', 'static'),
    static_url_path='/module/static')

app.secret_key = "tdl-dev"

# ====== Mock 数据 ======
MOCK_ACTIVE = [
    {
        "task_id": "active_001",
        "chat": "摄影爱好者群",
        "id": "10472",
        "filename": "2024_Summer_Vacation_Beach_Sunset_4K_HDR.mp4",
        "total_size": "1.2 GB",
        "total_size_bytes": 1288490188,
        "download_progress": "67.3",
        "download_progress_raw": 67.3,
        "download_speed": "8.5 MB/s",
        "save_path": "/app/downloads/摄影爱好者群/2024_Summer_Vacation.mp4",
        "status": "active"
    }
]

MOCK_COMPLETED = [
    {
        "task_id": "done_001",
        "chat": "设计素材共享",
        "id": "8915",
        "filename": "Figma_UI_Kit_2025_Complete_Pack.zip",
        "total_size": "342 MB",
        "total_size_bytes": 358612992,
        "download_progress": "100.0",
        "download_progress_raw": 100.0,
        "download_speed": "0 B/s",
        "save_path": "/app/downloads/设计素材共享/Figma_UI_Kit_2025_Complete_Pack.zip",
        "status": "completed"
    }
]

MOCK_FAILED = [
    {
        "task_id": "fail_001",
        "chat": "电影资源分享",
        "id": "15623",
        "filename": "[4K-HDR] Dune.Part.Two.2024.2160p.mkv",
        "error_message": "文件引用过期，重试3次后跳过 (file reference expired after 3 retries)",
        "total_size": "18.5 GB"
    }
]

# ====== API 路由 ======
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/get_download_list")
def get_download_list():
    already_down = request.args.get("already_down") == "true"
    data = MOCK_COMPLETED if already_down else MOCK_ACTIVE
    return Response(json.dumps(data), mimetype='application/json')

@app.route("/get_failed_downloads")
def get_failed_downloads():
    return Response(json.dumps(MOCK_FAILED), mimetype='application/json')

@app.route("/get_download_status")
def get_download_status():
    return jsonify(download_speed="8.5 MB/s", upload_speed="0.00 B/s")

@app.route("/get_app_version")
def get_app_version():
    return utils.__version__

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
    return jsonify(code="1", message="paused")

@app.route("/resume_task", methods=["POST"])
def resume_task():
    return jsonify(code="1", message="resumed")

if __name__ == "__main__":
    port = 5000
    print("=" * 50)
    print("  Telegram Media Downloader - Dev Mode")
    print(f"  浏览器: http://localhost:{port}")
    print("  修改前端文件后刷新即可")
    print("=" * 50)
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.run("0.0.0.0", port, debug=True, use_reloader=True)
