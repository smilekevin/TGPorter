"""Authenticated Configuration and setup routes; SDK work stays on the main loop."""

import asyncio
import copy
import hmac
import re
import secrets
from concurrent.futures import TimeoutError as FutureTimeout

from flask import Blueprint, jsonify, render_template, request, session

from module.settings import CONNECTION_FIELDS, FIELDS, LIMITS, LIVE_FIELDS, RESTART_FIELDS, SECRETS, validate_settings


blueprint = Blueprint("configuration", __name__)
LISTENER_FIELDS = {"web_host", "web_port", "debug_web", "mcp_enabled", "mcp_token",
                   "mcp_read_only", "mcp_allowed_hosts", "log_level"}
PASSWORD_PATTERN = r"(?=[\s\S]*[a-z])(?=[\s\S]*[A-Z])(?=[\s\S]*[0-9])[\s\S]{8,512}"
PASSWORD_HELP = "Use at least 8 characters, including uppercase and lowercase letters and a number."


def _validate_new_password(password):
    """Apply the current policy to newly created passwords, preserving saved logins."""
    if not isinstance(password, str) or re.fullmatch(PASSWORD_PATTERN, password) is None:
        raise ValueError(PASSWORD_HELP)


def _context():
    from module import web
    return web, web._app


def _submit(coroutine):
    _, app = _context()
    if not app.loop.is_running():
        coroutine.close()
        return jsonify(error="Downloader loop is unavailable"), 503
    future = asyncio.run_coroutine_threadsafe(coroutine, app.loop)
    try:
        return jsonify(future.result(timeout=5))
    except FutureTimeout:
        future.cancel()
        return jsonify(error="Operation timed out. Refresh Configuration before retrying."), 503
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except Exception:
        return jsonify(error="Unable to save settings or complete the operation"), 500


def _payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def _view(app):
    with app.settings.lock:
        active = getattr(app, "active_settings", app.settings.values)
        restart = [key for key in FIELDS if key in RESTART_FIELDS
                   and app.settings.values[key] != active.get(key)]
        return {"settings": app.settings.public_values(),
                "configured_secrets": [key for key in SECRETS if app.settings.values[key]],
                "proxy_summary": {key: app.settings.values["proxy"][key]
                                  for key in ("scheme", "hostname", "port")
                                  if key in app.settings.values["proxy"]} or None,
                "restart_required": restart,
                "reconnect_required": [key for key in CONNECTION_FIELDS
                                       if app.settings.values[key] != active.get(key)],
                "reauthorize_required": bool(app.telegram_login and getattr(app.telegram_login, "_account_change", lambda *args: False)(
                    app.settings.values, active)),
                "telegram": app.telegram_login.status() if app.telegram_login else {"state": "unconfigured", "busy": False}}


def apply_inactive_settings(app):
    """Apply backend settings only while no Telegram session owns downloads."""
    manager = app.telegram_login
    if manager and manager.client is not None:
        return
    listener = {key: copy.deepcopy(getattr(app, key)) for key in LISTENER_FIELDS}
    app.apply_settings()
    app.pre_run(initialize_upload=False)
    for key, value in listener.items():
        setattr(app, key, value)
    for key in FIELDS.keys() - LISTENER_FIELDS:
        app.active_settings[key] = copy.deepcopy(app.settings.values[key])


@blueprint.route("/configuration")
def configuration():
    web, app = _context()
    from module.filter import MAX_DOWNLOAD_FILTERS, MAX_FILTER_LENGTH
    from utils import __version__
    schema = [{"key": key, "section": section, "label": label,
               "type": "secret" if key in SECRETS else type(default).__name__,
               "limits": LIMITS.get(key), "live": key in LIVE_FIELDS, "restart": key in RESTART_FIELDS}
              for key, (default, section, label) in FIELDS.items()]
    return render_template("configuration.html", setup_required=not app.settings.setup_complete,
                           csrf_token=web._csrf_token(), schema=schema,
                           header_version=__version__,
                           password_pattern=PASSWORD_PATTERN, password_help=PASSWORD_HELP,
                           filter_limits={"length": MAX_FILTER_LENGTH, "count": MAX_DOWNLOAD_FILTERS},
                           sections=["Basic configuration", "Downloads", "Web and MCP", "Advanced"])


@blueprint.route("/api/setup", methods=["POST"])
def setup():
    web, app = _context()
    try:
        data = _payload()
        code, password = data.get("code"), data.get("password")
        if not isinstance(code, str):
            raise ValueError("Invalid or already used setup code")
        _validate_new_password(password)
    except ValueError as error:
        return jsonify(error=str(error)), 400

    async def claim():
        with app._app_data_lock, app.settings.lock:
            if app.settings.setup_complete or not app.setup_token or not hmac.compare_digest(
                    code.encode(), app.setup_token.encode()):
                raise ValueError("Invalid or already used setup code")
            app.settings.save({"web_login_secret": password}, setup_complete=True)
            app.web_login_secret = password
            app.setup_token = ""
        return {"success": True}

    response = _submit(claim())
    if not isinstance(response, tuple) and response.status_code == 200:
        session.clear()
        session["web_auth"] = web._login_token()
        session["configuration_owner"] = secrets.token_urlsafe(24)
        response = jsonify(success=True, csrf_token=web._csrf_token())
    return response


@blueprint.route("/api/configuration", methods=["GET", "POST"])
def settings():
    web, app = _context()
    if request.method == "GET":
        return jsonify(_view(app))
    try:
        data = _payload()
        updates = data.get("settings")
        if not isinstance(updates, dict) or updates.keys() - FIELDS.keys():
            raise ValueError("Unknown configuration field")
        if updates.get("web_login_secret", "") != "":
            _validate_new_password(updates["web_login_secret"])
        # Blank secret inputs mean unchanged. Explicit clearing uses a separate list.
        updates = {key: value for key, value in updates.items() if key not in SECRETS or value != ""}
        clear = data.get("clear_secrets", [])
        if not isinstance(clear, list) or any(key not in SECRETS - {"web_login_secret"} for key in clear):
            raise ValueError("Invalid secret field")
        updates.update({key: {} if key == "proxy" else "" for key in clear})
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except TypeError:
        return jsonify(error="Invalid configuration update"), 400

    async def save():
        with app._app_data_lock, app.settings.lock:
            manager = app.telegram_login
            operation = getattr(manager, "operation", None)
            if (operation is not None and not operation.done()
                    and any(key in updates and updates[key] != app.settings.values[key] for key in CONNECTION_FIELDS)):
                raise ValueError("Wait for the current Telegram operation before changing login settings")
            # Proxy credentials remain write-only; blank inputs preserve them.
            # An empty proxy object explicitly disables and removes the proxy.
            if isinstance(updates.get("proxy"), dict) and updates["proxy"]:
                updates["proxy"] = {**app.settings.values["proxy"], **updates["proxy"]}
            values = validate_settings({**app.settings.values, **updates})
            app.settings.save(values)
            if "use_user_account" in updates:
                app.settings.infer_account_mode = False
            app.apply_live_settings()
            if not getattr(manager, "authorized", False):
                apply_inactive_settings(app)
            from module.bot import _bot
            if "allowed_user_ids" in updates and _bot.app is app and _bot.bot is not None:
                _bot.set_live_allowed_users(values["allowed_user_ids"])
        if "language" in updates and _bot.app is app:
            _bot.schedule_commands_refresh()
        # Reuse the live pool/transport update path on the owning loop.
        from module.pyrogram_extension import set_max_concurrent_transmissions
        if app.telegram_login and app.telegram_login.ready:
            set_max_concurrent_transmissions(app.telegram_login.client, app.max_concurrent_transmissions)
            if _bot.app is app and _bot.bot is not None:
                set_max_concurrent_transmissions(_bot.bot, app.max_concurrent_transmissions)
            import sys
            main = sys.modules.get("__main__")
            if main and hasattr(main, "_worker_pool_set_target"):
                main._worker_pool_set_target(app.max_download_task)
        return _view(app)

    response = _submit(save())
    if not isinstance(response, tuple) and response.status_code == 200:
        # Password rotation invalidates all other sessions, retaining the editor.
        session["web_auth"] = web._login_token()
    return response


@blueprint.route("/api/configuration/validate-host", methods=["POST"])
def validate_host():
    """Check an editor entry using the same rules as saved MCP settings, without saving."""
    from module.mcp_server import validate_allowed_hosts
    try:
        entry = _payload().get("host")
        if not isinstance(entry, str) or len(entry) > 260:
            raise ValueError
        host = validate_allowed_hosts([entry])[0]
    except ValueError:
        return jsonify(error="Enter a hostname or IP with an optional port (1-65535). URLs, paths and wildcards are not allowed."), 400
    return jsonify(host=host)


@blueprint.route("/api/configuration/validate-filter", methods=["POST"])
def validate_filter():
    """Validate an editor draft with the same rules used by settings persistence."""
    from module.filter import validate_download_filter
    try:
        expression = validate_download_filter(_payload().get("expression"))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    return jsonify(expression=expression)


@blueprint.route("/api/telegram", methods=["POST"])
def telegram():
    _, app = _context()
    try:
        data = _payload()
        if (not isinstance(data.get("action"), str)
                or any(not isinstance(value, str) or len(value) > 512 for value in data.values())):
            raise ValueError("Invalid Telegram operation")
    except ValueError as error:
        return jsonify(error=str(error)), 400
    owner = session.setdefault("configuration_owner", secrets.token_urlsafe(24))

    async def dispatch():
        if app.telegram_login is None:
            raise ValueError("Telegram service is unavailable")
        apply_inactive_settings(app)
        return await app.telegram_login.submit(data["action"], data, owner)

    return _submit(dispatch())
