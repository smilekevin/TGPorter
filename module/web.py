"""web ui for media download"""

import datetime
import hmac
import logging
import secrets
import threading

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from module.download_service import DownloadService, _async_retry_download
from module.app import Application

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)
logger = logging.getLogger("web")

_flask_app = Flask(__name__, static_folder='static', static_url_path='/static')
_flask_app.secret_key = secrets.token_bytes(32)
_flask_app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_REFRESH_EACH_REQUEST=False,
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=7),
)
# Always reload templates from disk on each request — needed for NAS deployments
# where index.html is updated by the deploy script without restarting the container.
_flask_app.config["TEMPLATES_AUTO_RELOAD"] = True
# Disable response caching for index.html so browser/proxy don't serve stale versions
# after a deploy.
_flask_app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
# Force Jinja to not cache (belt-and-suspenders: ensure auto-reload really works)
_flask_app.jinja_env.auto_reload = True
_flask_app.jinja_env.cache = {}


def get_flask_app() -> Flask:
    """get flask app instance"""
    return _flask_app


def run_web_server(app: Application, asgi_app=None):
    """Serve Flask and optional MCP through one ASGI listener."""
    import uvicorn
    uvicorn.run(asgi_app or create_web_app(app), host=app.web_host, port=app.web_port,
                log_level="debug" if app.debug_web else "warning",
                access_log=False, proxy_headers=False)


def create_web_app(app: Application):
    """Build the production listener; MCP calls shared operations without HTTP."""
    from contextlib import asynccontextmanager
    from a2wsgi import WSGIMiddleware
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    global _app
    _app = app
    _flask_app.debug = getattr(app, "debug_web", False)
    mcp_app = None
    if getattr(app, "mcp_enabled", False):
        if not getattr(app, "mcp_token", ""):
            raise ValueError("mcp_enabled requires a non-empty mcp_token")
        from module.mcp_server import create_http_app, create_server
        server = create_server(_download_service(), read_only=getattr(app, "mcp_read_only", False))
        mcp_app = create_http_app(server, app)

    @asynccontextmanager
    async def lifespan(_):
        if mcp_app is not None:
            # Mounted ASGI applications do not automatically run their lifespan.
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        else:
            yield

    async def disabled_mcp(request):
        return JSONResponse({"error": "mcp_disabled"}, status_code=404)

    return Starlette(routes=[
        Route("/mcp", mcp_app or disabled_mcp, methods=["GET", "POST", "DELETE"]),
        Mount("/", app=WSGIMiddleware(_flask_app)),
    ], lifespan=lifespan)


# pylint: disable = W0603
_app: Application = None


def _web_login_secret() -> str:
    """An empty password keeps the existing password-free mode."""
    value = getattr(_app, "web_login_secret", "")
    return "" if value is None else str(value)


def _login_token() -> str:
    """Bind sessions to the current password without putting it in the cookie."""
    return hmac.new(
        _flask_app.secret_key, _web_login_secret().encode("utf-8"), "sha256"
    ).hexdigest()


def _is_authenticated() -> bool:
    token = session.get("web_auth")
    return isinstance(token, str) and hmac.compare_digest(token, _login_token())


def _csrf_token() -> str:
    if "web_csrf_token" not in session:
        session["web_csrf_token"] = secrets.token_urlsafe(32)
    return session["web_csrf_token"]


@_flask_app.before_request
def require_web_login():
    """Require a password session and CSRF for dashboard and Web API requests."""
    if not _web_login_secret() or request.endpoint == "static":
        return None
    if request.endpoint != "login" and not _is_authenticated():
        if request.endpoint == "index":
            return redirect(url_for("login"))
        return jsonify(error="authentication_required", login_url=url_for("login")), 401
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        expected = session.get("web_csrf_token", "")
        submitted = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(
            expected.encode("utf-8"), submitted.encode("utf-8")
        ):
            if request.endpoint == "login":
                return render_template(
                    "login.html", error="页面已过期，请重新输入密码。", csrf_token=_csrf_token()
                ), 400
            return jsonify(error="invalid_csrf_token"), 400
    return None


@_flask_app.after_request
def prevent_authenticated_response_cache(response):
    if _web_login_secret() and request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


@_flask_app.route("/login", methods=["GET", "POST"])
def login():
    if not _web_login_secret() or _is_authenticated():
        return redirect(url_for("index"))
    error = None
    status = 200
    if request.method == "POST":
        password = request.form.get("password", "")
        if hmac.compare_digest(password.encode("utf-8"), _web_login_secret().encode("utf-8")):
            session.clear()
            session["web_auth"] = _login_token()
            session.permanent = request.form.get("remember") == "on"
            _csrf_token()
            return redirect(url_for("index"))
        error = "密码错误，请重试。"
        status = 401
    return render_template("login.html", error=error, csrf_token=_csrf_token()), status


@_flask_app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def init_web(app: Application):
    """
    Initialize and start the web server.

    Args:
        app: The Application instance.

    Returns:
        None.
    """
    global _app
    _app = app
    # Load download history into memory so WebUI shows completed tasks
    from module.download_stat import load_downloads
    load_downloads()
    logger = logging.getLogger("web.init")
    logger.info("download_history loaded into memory")
    # Construct before starting the thread so invalid MCP settings fail startup.
    asgi_app = create_web_app(app)
    threading.Thread(target=run_web_server, args=(app, asgi_app), daemon=not app.debug_web).start()


@_flask_app.route("/")
def index():
    """Render the dashboard after the shared authentication check."""
    login_required = bool(_web_login_secret())
    return render_template(
        "index.html", web_login_required=login_required,
        web_csrf_token=_csrf_token() if login_required else "",
    )


def _download_service():
    return DownloadService(_app, retry_download=_async_retry_download)


def _service_response(result):
    if isinstance(result, tuple):
        payload, status = result
        return jsonify(payload), status
    return jsonify(result) if isinstance(result, (dict, list)) else result


@_flask_app.route("/submit_download", methods=["POST"])
def web_submit_download():
    """Accept one post link through the shared durable submission path."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(code="0", message="JSON object required"), 400
    return _service_response(_download_service().submit_download(data.get("url")))


@_flask_app.route("/get_completed_count")
def get_completed_count():
    return _service_response(_download_service().get_completed_count())


@_flask_app.route("/get_download_status")
def get_download_speed():
    return _service_response(_download_service().get_download_speed())


@_flask_app.route("/get_flood_wait")
def get_flood_wait():
    return _service_response(_download_service().get_flood_wait())


@_flask_app.route("/get_download_state")
def web_get_download_state():
    return _service_response(_download_service().web_get_download_state())


@_flask_app.route("/set_download_state", methods=["POST"])
def web_set_download_state():
    return _service_response(_download_service().web_set_download_state(state=request.args.get("state")))


@_flask_app.route("/get_app_version")
def get_app_version():
    return _service_response(_download_service().get_app_version())


@_flask_app.route("/get_max_workers")
def get_max_workers():
    return _service_response(_download_service().get_max_workers())


@_flask_app.route("/set_max_workers", methods=["POST"])
def set_max_workers():
    return _service_response(_download_service().set_max_workers(n=request.args.get("n", type=int)))


@_flask_app.route("/get_download_list")
def get_download_list():
    return _service_response(_download_service().get_download_list(
        already_down=request.args.get("already_down"),
        offset=request.args.get("offset", 0, type=int),
        limit=request.args.get("limit", 0, type=int), search=request.args.get("search", ""),
    ))


@_flask_app.route("/get_failed_downloads")
def web_get_failed_downloads():
    return _service_response(_download_service().web_get_failed_downloads())


@_flask_app.route("/pause_task", methods=["POST"])
def web_pause_task():
    return _service_response(_download_service().web_pause_task(task_id=request.args.get("task_id")))


@_flask_app.route("/resume_task", methods=["POST"])
def web_resume_task():
    return _service_response(_download_service().web_resume_task(task_id=request.args.get("task_id")))


@_flask_app.route("/check_file_exists", methods=["POST"])
def web_check_file_exists():
    return _service_response(_download_service().web_check_file_exists(task_id=request.args.get("task_id")))


@_flask_app.route("/delete_task", methods=["POST"])
def web_delete_task():
    return _service_response(_download_service().web_delete_task(
        task_id=request.args.get("task_id"),
        delete_file=request.args.get("delete_file", "false").lower() == "true",
    ))


@_flask_app.route("/batch_delete", methods=["POST"])
def web_batch_delete():
    return _service_response(_download_service().web_batch_delete(data=request.get_json(silent=True)))


@_flask_app.route("/retry_task", methods=["POST"])
def web_retry_task():
    return _service_response(_download_service().web_retry_task(task_id=request.args.get("task_id")))


@_flask_app.route("/batch_retry", methods=["POST"])
def web_batch_retry():
    return _service_response(_download_service().web_batch_retry(data=request.get_json(silent=True)))


@_flask_app.route("/get_pending_list")
def web_get_pending_list():
    return _service_response(_download_service().web_get_pending_list(
        offset=request.args.get("offset", 0, type=int), limit=request.args.get("limit", 100, type=int),
    ))


@_flask_app.route("/remove_pending", methods=["POST"])
def web_remove_pending():
    return _service_response(_download_service().web_remove_pending(task_id=request.args.get("task_id")))
