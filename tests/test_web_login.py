"""Exercise password login through the real Flask routes, without Telegram."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import utils
from module import download_stat, download_service, web
from module.app import Application


class WebLoginTests(unittest.TestCase):
    def setUp(self):
        self.app = web.get_flask_app()
        self.config = SimpleNamespace(web_login_secret="test-password")
        patches = [
            patch.object(web, "_app", self.config),
            patch.dict(self.app.config, TESTING=True),
            patch.object(download_stat, "_download_state", download_stat.DownloadState.Downloading),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = self.app.test_client()

    def csrf_token(self):
        with self.client.session_transaction() as session:
            return session["web_csrf_token"]

    def login(self, password="test-password"):
        self.client.get("/login")
        return self.client.post("/login", data={
            "password": password, "csrf_token": self.csrf_token(),
        })

    def test_dashboard_redirects_to_login_when_password_is_configured(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")
        response = self.client.get("/", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sign in to dashboard", response.get_data(as_text=True))
        self.assertIn('type="password"', response.get_data(as_text=True))
        self.assertNotIn("test-password", response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_version_api_and_dashboard_preserve_the_resolved_version(self):
        self.login()
        with patch.object(utils, "__version__", "v1.2.3-rc.1"):
            response = self.client.get("/get_app_version")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_data(as_text=True), "v1.2.3-rc.1")
        dashboard = self.client.get("/").get_data(as_text=True)
        self.assertIn('x-text="version"', dashboard)
        self.assertNotIn("'v' + version", dashboard)

    def test_all_dashboard_apis_reject_anonymous_requests_before_running_handlers(self):
        with patch.object(download_service, "get_download_result", Mock()) as get_result, \
                patch.object(download_service, "set_download_state", Mock()) as set_state:
            for route in self.app.url_map.iter_rules():
                if route.endpoint in {"static", "index", "login", "logout", "configuration.configuration"}:
                    continue
                method = "POST" if "POST" in route.methods else "GET"
                with self.subTest(route=route.rule, method=method):
                    response = self.client.open(route.rule, method=method)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.get_json()["error"], "authentication_required")
            get_result.assert_not_called()
            set_state.assert_not_called()

    def test_static_assets_remain_available_on_login_page(self):
        with self.client.get("/static/icons/icon-192.png") as response:
            self.assertEqual(response.status_code, 200)

    def test_wrong_password_does_not_authorize_dashboard_or_apis(self):
        response = self.login("wrong-password")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Incorrect password. Please try again.", response.get_data(as_text=True))
        self.assertNotIn("wrong-password", response.get_data(as_text=True))
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/get_download_state").status_code, 401)

    def test_correct_password_authorizes_dashboard_and_real_download_controls(self):
        response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])
        self.assertNotIn("test-password", response.headers["Set-Cookie"])
        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn('action="/logout"', dashboard.get_data(as_text=True))
        self.assertIn("authenticatedFetch", dashboard.get_data(as_text=True))
        self.assertEqual(self.client.get("/get_download_state").get_data(as_text=True), "downloading")
        response = self.client.post("/set_download_state?state=pause", headers={
            "X-CSRF-Token": self.csrf_token(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/get_download_state").get_data(as_text=True), "paused")

    def test_login_and_download_controls_require_csrf_token(self):
        self.client.get("/login")
        response = self.client.post("/login", data={"password": "test-password"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/get_download_state").status_code, 401)
        self.login()
        for token in (None, "wrong-token"):
            with self.subTest(token=token):
                headers = {"X-CSRF-Token": token} if token else {}
                response = self.client.post("/set_download_state?state=pause", headers=headers)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"], "invalid_csrf_token")
                self.assertIs(download_stat.get_download_state(), download_stat.DownloadState.Downloading)

    def test_login_rotates_existing_session_and_csrf_token(self):
        self.client.get("/login")
        old_token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["old_data"] = "discard-me"
        self.login()
        with self.client.session_transaction() as session:
            self.assertNotIn("old_data", session)
            self.assertNotEqual(session["web_csrf_token"], old_token)
            self.assertNotIn("test-password", str(dict(session)))

    def test_remember_me_is_opt_in_and_survives_a_new_browser_session(self):
        response = self.login()
        self.assertNotIn("Expires=", response.headers["Set-Cookie"])
        self.client.post("/logout", data={"csrf_token": self.csrf_token()})
        self.client.get("/login")
        response = self.client.post("/login", data={
            "password": "test-password", "csrf_token": self.csrf_token(), "remember": "on",
        })
        self.assertIn("Expires=", response.headers["Set-Cookie"])
        with self.client.session_transaction() as session:
            self.assertTrue(session.permanent)
        # Simulate reopening a browser with the persisted signed cookie.
        cookie = response.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        reopened = self.app.test_client()
        reopened.set_cookie("session", cookie, domain="localhost")
        self.assertEqual(reopened.get("/").status_code, 200)

    def test_logout_revokes_access_and_shows_login_again(self):
        self.login()
        response = self.client.post("/logout", data={"csrf_token": self.csrf_token()},
                                    follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sign in to dashboard", response.get_data(as_text=True))
        self.assertEqual(self.client.get("/").headers["Location"], "/login")
        self.assertEqual(self.client.get("/get_download_state").status_code, 401)

    def test_changing_password_invalidates_existing_login(self):
        self.login()
        self.config.web_login_secret = "new-password"
        self.assertEqual(self.client.get("/").headers["Location"], "/login")
        self.assertEqual(self.client.get("/get_download_state").status_code, 401)
        self.assertEqual(self.login("new-password").status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_expired_session_requires_login(self):
        self.login()
        expired_time = time.time() + 8 * 24 * 60 * 60
        with patch("itsdangerous.timed.time.time", return_value=expired_time):
            self.assertEqual(self.client.get("/").headers["Location"], "/login")
            self.assertEqual(self.client.get("/get_download_state").status_code, 401)

    def test_old_hardcoded_signing_key_cannot_authorize_requests(self):
        valid_token = web._login_token()
        with patch.dict(self.app.config, SECRET_KEY=b"tdl"):
            forged_cookie = self.app.session_interface.get_signing_serializer(self.app).dumps({
                "web_auth": valid_token,
            })
        self.client.set_cookie("session", forged_cookie, domain="localhost")
        self.assertEqual(self.client.get("/get_download_state").status_code, 401)

    def test_replacing_signing_key_invalidates_existing_login(self):
        self.login()
        with patch.dict(self.app.config, SECRET_KEY=b"replacement-key-after-restart"):
            self.assertEqual(self.client.get("/").headers["Location"], "/login")
            self.assertEqual(self.client.get("/get_download_state").status_code, 401)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the frontend regression")
    def test_frontend_sends_csrf_header_and_redirects_when_session_expires(self):
        self.login()
        html = self.client.get("/").get_data(as_text=True)
        script = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)[-1]
        result = subprocess.run(["node", "-e", """
const assert = require('node:assert/strict');
let captured;
let redirected;
global.fetch = async (url, options) => {
    captured = { url, options };
    return { status: 200 };
};
global.window = { location: { replace: url => { redirected = url; } } };
""" + script + """
(async () => {
    const response = await authenticatedFetch('/set_download_state?state=pause', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    assert.equal(response.status, 200);
    assert.equal(captured.url, '/set_download_state?state=pause');
    assert.equal(captured.options.method, 'POST');
    assert.equal(captured.options.body, '{}');
    assert.equal(captured.options.headers.get('Content-Type'), 'application/json');
    assert.equal(captured.options.headers.get('X-CSRF-Token'), """ + json.dumps(self.csrf_token()) + """);
    global.fetch = async () => ({ status: 401 });
    await assert.rejects(() => authenticatedFetch('/get_download_state'));
    assert.equal(redirected, '/login');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_numeric_and_unicode_passwords_work(self):
        for configured, submitted in ((123, "123"), (0, "0"), ("中文密码🔒", "中文密码🔒")):
            with self.subTest(password=configured):
                self.client = self.app.test_client()
                self.config.web_login_secret = configured
                self.assertEqual(self.login(submitted).status_code, 302)
                self.assertEqual(self.client.get("/").status_code, 200)

    def test_empty_or_unset_password_preserves_password_free_mode(self):
        for configured in ("", None):
            with self.subTest(password=configured):
                self.config.web_login_secret = configured
                self.client = self.app.test_client()
                response = self.client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('action="/logout"', response.get_data(as_text=True))
                self.assertEqual(self.client.get("/login").headers["Location"], "/")
                self.assertEqual(self.client.post("/set_download_state?state=continue").status_code, 200)
        del self.config.web_login_secret
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_config_parser_normalizes_null_and_numeric_passwords(self):
        app = Application("unused-config.yaml", "unused-data.json")
        self.addCleanup(app.loop.close)
        for value, expected in ((None, ""), ("", ""), (123, "123"), (0, "0")):
            with self.subTest(value=value):
                app.assign_config({
                    "api_id": "test", "api_hash": "test", "media_types": [],
                    "file_formats": {}, "web_login_secret": value,
                })
                self.assertEqual(app.web_login_secret, expected)


class PersistentWebSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)

    def test_signing_key_is_private_stable_and_shared_by_initializers(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            keys = list(executor.map(web._load_session_key, [self.path] * 16))
        self.assertEqual(len(keys[0]), 32)
        self.assertTrue(all(key == keys[0] for key in keys))
        self.assertEqual((self.path / "web_session.key").read_bytes(), keys[0])
        self.assertEqual(os.stat(self.path / "web_session.key").st_mode & 0o777, 0o600)
        self.assertEqual(list(self.path.iterdir()), [self.path / "web_session.key"])
        self.assertNotEqual(web._load_session_key(self.path / "other"), keys[0])

    def test_invalid_key_and_storage_failure_do_not_fall_back_to_a_temporary_key(self):
        key_path = self.path / "web_session.key"
        key_path.write_bytes(b"incomplete")
        with self.assertRaisesRegex(ValueError, "Invalid Web session signing key"):
            web._load_session_key(self.path)
        self.assertEqual(key_path.read_bytes(), b"incomplete")
        key_path.unlink()
        with patch.object(web.os, "link", side_effect=PermissionError("read-only data")):
            with self.assertRaises(PermissionError):
                web._load_session_key(self.path)
        self.assertEqual(list(self.path.iterdir()), [])

    def test_real_process_restart_preserves_cookies_csrf_and_seven_day_expiry(self):
        script = '''
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from module import web

config = SimpleNamespace(data_path=sys.argv[1], web_login_secret="test-password")
web.create_web_app(config)
app = web.get_flask_app()
client = app.test_client()
if len(sys.argv) == 3:
    client.get("/login")
    with client.session_transaction() as session:
        csrf = session["web_csrf_token"]
    response = client.post("/login", data={
        "password": "test-password", "csrf_token": csrf, "remember": sys.argv[2],
    })
    assert response.status_code == 302
    with client.session_transaction() as session:
        csrf = session["web_csrf_token"]
    print(json.dumps({"cookie": client.get_cookie("session").value, "csrf": csrf,
                      "persistent": "Expires=" in response.headers["Set-Cookie"]}))
else:
    client.set_cookie("session", sys.argv[3])
    assert client.get("/").status_code == 200
    assert client.get("/get_download_state").status_code == 200
    assert client.post("/set_download_state?state=pause").status_code == 400
    assert client.post("/set_download_state?state=pause", headers={
        "X-CSRF-Token": sys.argv[4],
    }).status_code == 200
    with patch("itsdangerous.timed.time.time", return_value=time.time() + 6 * 86400):
        assert client.get("/get_download_state").status_code == 200
    with patch("itsdangerous.timed.time.time", return_value=time.time() + 8 * 86400):
        assert client.get("/get_download_state").status_code == 401
    config.web_login_secret = "changed-password"
    assert client.get("/get_download_state").status_code == 401
'''
        for remember in ("", "on"):
            with self.subTest(remember=remember):
                data_path = str(self.path / (remember or "browser-session"))
                command = [sys.executable, "-B", "-c", script, data_path, remember]
                first = subprocess.run(command, capture_output=True, text=True, timeout=30)
                self.assertEqual(first.returncode, 0, first.stderr)
                login = json.loads(first.stdout)
                self.assertEqual(login["persistent"], remember == "on")
                second = subprocess.run(command + [login["cookie"], login["csrf"]],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(second.returncode, 0, second.stderr)


if __name__ == "__main__":
    unittest.main()
