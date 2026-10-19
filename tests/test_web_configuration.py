"""Validate Configuration against production Flask routes and a real owning loop."""

import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from module import web
from module.app import Application
from lang import set_language


class WebConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(set_language, "en")
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.app = Application(str(self.root / "missing.yaml"), str(self.root / "data/data.json"))
        self.app.load_config()
        self.app.settings.save({"save_path": str(self.root / "downloads")})
        self.app.apply_settings()
        self.app.setup_token = "offline-setup-code"
        self.app.telegram_login = SimpleNamespace(client=None, ready=False,
            status=lambda: {"state": "logged_out", "busy": False}, submit=AsyncMock(return_value={"busy": True}))
        self.flask = web.get_flask_app()
        self.patch = patch.object(web, "_app", self.app)
        self.patch.start()
        web.create_web_app(self.app)
        self.client = self.flask.test_client()
        self.thread = threading.Thread(target=self.app.loop.run_forever)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.app.loop.call_soon_threadsafe(self.app.loop.stop)
        self.thread.join(timeout=5)
        self.app.loop.close()
        self.app.executor.shutdown()
        self.patch.stop()
        self.directory.cleanup()

    def csrf(self, client=None):
        with (client or self.client).session_transaction() as session:
            return {"X-CSRF-Token": session["web_csrf_token"]}

    def setup(self):
        self.client.get("/configuration")
        response = self.client.post("/api/setup", headers=self.csrf(), json={
            "code": "offline-setup-code", "password": "OfflinePassword1"})
        self.assertEqual(response.status_code, 200, response.json)

    def save(self, settings, **other):
        return self.client.post("/api/configuration", headers=self.csrf(), json={"settings": settings, **other})

    def test_first_visit_redirects_to_setup_and_requires_both_csrf_and_code(self):
        self.assertEqual(self.client.get("/").headers["Location"], "/configuration")
        self.assertEqual(self.client.get("/get_download_state").status_code, 401)
        response = self.client.get("/configuration")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.app.setup_token, response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        payload = {"code": "offline-setup-code", "password": "OfflinePassword1"}
        self.assertEqual(self.client.post("/api/setup", json=payload).status_code, 400)
        self.assertEqual(self.client.post("/api/setup", headers=self.csrf(), json={**payload, "code": "wrong"}).status_code, 400)
        self.assertFalse(self.app.settings.setup_complete)
        self.setup()
        self.assertEqual(self.app.setup_token, "")
        self.assertTrue(json.loads(Path(self.app.settings.path).read_text())["_setup_complete"])
        self.assertEqual(self.client.post("/api/setup", headers=self.csrf(), json=payload).status_code, 400)

    def test_configuration_and_telegram_require_browser_session_and_csrf(self):
        self.setup()
        anonymous = self.flask.test_client()
        for path in ("/api/configuration", "/api/telegram", "/api/configuration/validate-host"):
            self.assertEqual(anonymous.post(path, headers={"Authorization": "Bearer agent"}, json={}).status_code, 401)
            self.assertEqual(self.client.post(path, json={}).status_code, 400)
        self.assertEqual(anonymous.get("/configuration").headers["Location"], "/login")

    def test_shared_header_signs_out_management_session_in_private_and_public_mode(self):
        self.setup()
        for public in (False, True):
            with self.subTest(public=public):
                self.client.get("/login")
                self.client.post("/login", headers=self.csrf(), data={"password": "OfflinePassword1"})
                self.assertEqual(self.save({"public_web": public}).status_code, 200)
                for path in ("/", "/configuration"):
                    page = self.client.get(path)
                    self.assertEqual(page.status_code, 200)
                    html = page.get_data(as_text=True)
                    self.assertIn('action="/logout" method="post"', html)
                    self.assertIn('name="csrf_token" value="' + self.csrf()["X-CSRF-Token"] + '"', html)
                self.assertEqual(self.client.post("/logout").status_code, 400)
                self.assertEqual(self.client.get("/api/configuration").status_code, 200)
                response = self.client.post("/logout", data={"csrf_token": self.csrf()["X-CSRF-Token"]})
                self.assertEqual(response.headers["Location"], "/login")
                self.assertEqual(self.client.get("/api/configuration").status_code, 401)
                page = self.client.get("/")
                self.assertEqual(page.status_code, 200 if public else 302)
                self.assertNotIn('action="/logout"', page.get_data(as_text=True))

    def test_mcp_host_validation_is_read_only_and_matches_saved_settings(self):
        self.setup()
        original = Path(self.app.settings.path).read_bytes()
        for entry, expected in (("NAS.EXAMPLE.COM", "nas.example.com"), ("nas.example.com:5000", "nas.example.com:5000"),
                                ("192.168.1.20:5000", "192.168.1.20:5000"), ("[::1]:5000", "[::1]:5000")):
            response = self.client.post("/api/configuration/validate-host", headers=self.csrf(), json={"host": entry})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json["host"], expected)
            self.assertEqual(Path(self.app.settings.path).read_bytes(), original)
        for entry in ("https://nas.example.com", "nas.example.com/path", "user:pass@nas.example.com",
                      "*.example.com", "bad..example.com", "host..", "-bad.example.com", "host:0", "host:65536",
                      "host:bad", "999.1.2.3", "中文", None):
            response = self.client.post("/api/configuration/validate-host", headers=self.csrf(), json={"host": entry})
            self.assertEqual(response.status_code, 400, entry)
            self.assertIn("hostname or IP", response.json["error"])
            self.assertEqual(Path(self.app.settings.path).read_bytes(), original)
            self.assertEqual(self.save({"mcp_allowed_hosts": [entry]}).status_code, 400, entry)

    def test_setup_accepts_eight_characters_only_with_all_three_character_types(self):
        self.client.get("/configuration")
        original = Path(self.app.settings.path).read_bytes()
        for password in ("Abc1234", "abcdefgh", "ABCDEFGH", "12345678", "Abcdefgh", "abcdefg1",
                         "ABCDEFG1", "Aa1" + "x" * 510, None):
            with self.subTest(password=password):
                response = self.client.post("/api/setup", headers=self.csrf(), json={
                    "code": "offline-setup-code", "password": password})
                self.assertEqual(response.status_code, 400, response.json)
                self.assertIn("at least 8 characters", response.json["error"])
                self.assertEqual(Path(self.app.settings.path).read_bytes(), original)
        response = self.client.post("/api/setup", headers=self.csrf(), json={
            "code": "offline-setup-code", "password": "Abcd1234"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.app.web_login_secret, "Abcd1234")

    def test_password_updates_enforce_the_same_policy_and_blank_keeps_saved_password(self):
        self.setup()
        original = Path(self.app.settings.path).read_bytes()
        for password in ("Abc1234", "NoDigitsHere", "no-uppercase1", "NO-LOWERCASE1", None, 12345678):
            with self.subTest(password=password):
                response = self.save({"web_login_secret": password})
                self.assertEqual(response.status_code, 400, response.json)
                self.assertIn("at least 8 characters", response.json["error"])
                self.assertEqual(Path(self.app.settings.path).read_bytes(), original)
        self.assertEqual(self.save({"web_login_secret": "Newpass2"}).status_code, 200)
        self.assertEqual(self.save({"web_login_secret": ""}).status_code, 200)
        self.assertEqual(self.app.web_login_secret, "Newpass2")

    def test_existing_passwords_can_still_log_in_and_be_preserved(self):
        self.app.settings.save({"web_login_secret": "old-password"}, setup_complete=True)
        self.app.apply_settings()
        self.client.get("/login")
        response = self.client.post("/login", headers=self.csrf(), data={"password": "old-password"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/api/configuration").status_code, 200)
        self.assertEqual(self.save({"web_login_secret": ""}).status_code, 200)
        self.assertEqual(self.app.web_login_secret, "old-password")

    def test_save_is_atomic_moves_language_and_workers_and_preserves_checkpoint(self):
        self.setup()
        self.app.app_data["unrelated"] = "keep"
        response = self.save({"language": "fr", "max_download_task": 6, "file_download_concurrency": 8})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.app.language.value, "fr")
        self.assertEqual(self.app.max_download_task, 6)
        self.assertEqual(self.app.max_concurrent_transmissions, 48)
        self.app.save_app_data()
        saved = json.loads(Path(self.app.settings.path).read_text())
        self.assertEqual(saved["language"], "fr")
        self.assertEqual(saved["max_download_task"], 6)
        self.assertEqual(json.loads(Path(self.app.app_data_file).read_text()), {"unrelated": "keep", "chat": []})

    def test_credentials_need_reconnect_but_only_listener_settings_need_restart(self):
        self.setup()
        self.app.telegram_login.client = SimpleNamespace()
        self.app.telegram_login.ready = False
        old = self.app.proxy
        response = self.save({"proxy": {"scheme": "http", "hostname": "localhost", "port": 1080},
                              "mcp_enabled": True, "mcp_token": "private-agent-token"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.app.proxy, old)
        self.assertFalse(self.app.mcp_enabled)
        self.assertNotIn("proxy", response.json["restart_required"])
        self.assertIn("proxy", response.json["reconnect_required"])
        self.assertIn("mcp_enabled", response.json["restart_required"])
        self.assertNotIn("private-agent-token", response.get_data(as_text=True))

    def test_download_settings_apply_live_without_replacing_active_scan_state(self):
        self.setup()
        self.app.telegram_login.client = SimpleNamespace()
        self.app.telegram_login.ready = False
        scans = self.app.chat_download_config
        changes = {"file_download_concurrency": 8, "download_part_size_mb": 32,
                   "media_types": ["video"], "file_formats": {"video": ["mkv"]},
                   "save_path": str(self.root / "new-downloads"), "file_path_prefix": ["media_type"],
                   "allowed_user_ids": [12345]}
        response = self.save(changes)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["restart_required"], [])
        self.assertEqual(response.json["reconnect_required"], [])
        self.assertEqual(self.app.file_download_concurrency, 8)
        self.assertEqual(self.app.download_part_size_mb, 32)
        self.assertEqual(self.app.file_formats, {"video": ["mkv"]})
        self.assertEqual(self.app.file_path_prefix, ["media_type"])
        self.assertEqual(self.app.allowed_user_ids, [12345])
        self.assertIs(self.app.chat_download_config, scans)

    def test_saved_disconnected_login_retains_account_settings_until_verification(self):
        self.setup()
        self.app.telegram_login.authorized = True
        old = self.app.use_user_account
        response = self.save({"use_user_account": not old, "api_hash": "replacement-hash"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.app.use_user_account, old)
        self.assertEqual(self.app.active_settings["use_user_account"], old)
        self.assertIn("use_user_account", response.json["reconnect_required"])
        self.assertNotIn("api_hash", response.json["restart_required"])

    def test_credentials_cannot_change_during_an_inflight_authorization(self):
        self.setup()
        original = Path(self.app.settings.path).read_bytes()
        self.app.telegram_login.operation = SimpleNamespace(done=lambda: False)
        response = self.save({"api_hash": "new-value"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Path(self.app.settings.path).read_bytes(), original)

    def test_secrets_are_never_echoed_and_blank_preserves_or_clear_removes(self):
        self.setup()
        self.assertEqual(self.save({"api_hash": "do-not-return", "bot_token": "private-bot"}).status_code, 200)
        self.assertNotIn("do-not-return", self.client.get("/api/configuration").get_data(as_text=True))
        self.assertNotIn("private-bot", self.client.get("/configuration").get_data(as_text=True))
        self.assertEqual(self.save({"api_hash": ""}).status_code, 200)
        self.assertEqual(self.app.settings.values["api_hash"], "do-not-return")
        self.assertEqual(self.save({}, clear_secrets=["bot_token"]).status_code, 200)
        self.assertEqual(self.app.settings.values["bot_token"], "")

    def test_invalid_configuration_does_not_change_settings_or_live_values(self):
        self.setup()
        original = Path(self.app.settings.path).read_bytes()
        for settings in ({"max_download_task": 0}, {"file_download_concurrency": True},
                         {"web_login_secret": "short"}, {"not-a-setting": "secret"},
                         {"proxy": {"password": "do-not-return"}}):
            with self.subTest(settings=settings):
                response = self.save(settings)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("do-not-return", response.get_data(as_text=True))
                self.assertEqual(Path(self.app.settings.path).read_bytes(), original)
        self.assertEqual(self.app.max_download_task, 5)

    def test_password_rotation_invalidates_other_sessions(self):
        self.setup()
        second = self.flask.test_client()
        second.get("/login")
        second.post("/login", headers=self.csrf(second), data={"password": "OfflinePassword1"})
        self.assertEqual(second.get("/api/configuration").status_code, 200)
        self.assertEqual(self.save({"web_login_secret": "ReplacementPassword2"}).status_code, 200)
        self.assertEqual(self.client.get("/api/configuration").status_code, 200)
        self.assertEqual(second.get("/api/configuration").status_code, 401)

    def test_login_is_submitted_to_owning_loop_without_waiting_for_telegram(self):
        self.setup()
        response = self.client.post("/api/telegram", headers=self.csrf(), json={"action": "send_code", "phone": "+1234567890"})
        self.assertEqual(response.status_code, 200)
        self.app.telegram_login.submit.assert_awaited_once()
        self.assertEqual(self.app.telegram_login.submit.await_args.args[0], "send_code")

    def test_public_mode_keeps_configuration_private(self):
        self.setup()
        self.assertEqual(self.save({"public_web": True}).status_code, 200)
        anonymous = self.flask.test_client()
        self.assertEqual(anonymous.get("/").status_code, 200)
        self.assertEqual(anonymous.get("/get_download_state").status_code, 200)
        self.assertEqual(anonymous.get("/api/configuration").status_code, 401)
        self.assertEqual(anonymous.post("/api/telegram", json={"action": "logout"}).status_code, 401)
        self.assertEqual(anonymous.post("/api/configuration/validate-host", json={"host": "nas.example.com"}).status_code, 401)
        self.assertEqual(anonymous.get("/configuration").headers["Location"], "/login")
        self.assertEqual(self.client.get("/api/configuration").status_code, 200)

    def test_combined_web_access_mode_preserves_password_and_can_restore_private_access(self):
        self.setup()
        anonymous = self.flask.test_client()
        for public in (True, False):
            response = self.save({"public_web": public, "web_login_secret": ""})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.app.web_login_secret, "OfflinePassword1")
            self.assertEqual(self.app.public_web, public)
            self.assertEqual(anonymous.get("/get_download_state").status_code, 200 if public else 401)
            self.assertEqual(anonymous.get("/api/configuration").status_code, 401)
            self.assertEqual(self.client.get("/api/configuration").status_code, 200)
        response = self.save({"public_web": True, "web_login_secret": "short"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.app.public_web)

    def test_native_form_orders_credentials_before_login_and_hides_channel_editor(self):
        self.setup()
        html = self.client.get("/configuration").get_data(as_text=True)
        self.assertLess(html.index('id="setting-api_id"'), html.index('id="setting-api_hash"'))
        self.assertLess(html.index('id="setting-api_hash"'), html.index('id="setting-bot_token"'))
        self.assertLess(html.index('id="setting-bot_token"'), html.index('id="telegram-submit"'))
        self.assertLess(html.index('id="telegram-submit"'), html.index('id="setting-proxy"'))
        for key in ("media_types", "file_formats", "file_path_prefix", "allowed_user_ids"):
            self.assertIn(f'data-editor="{key}"', html)
        self.assertNotIn('<textarea', html)
        self.assertNotIn('id="setting-chat"', html)
        self.assertNotIn('data-clear-secret=', html)
        self.assertNotIn('data-i18n="Applies immediately"', html)

    def test_native_selection_payload_preserves_order_types_and_existing_channels(self):
        self.setup()
        channel = [{"chat_id": -1001, "last_read_message_id": 42}]
        self.app.settings.save({"chat": channel})
        changes = {"media_types": ["video", "document"], "file_formats": {"video": ["mkv", "mp4"], "document": ["all"]},
                   "file_path_prefix": ["media_datetime", "chat_title"], "file_name_prefix": ["caption", "message_id"],
                   "allowed_user_ids": ["me", 12345, "@tester"], "use_user_account": True}
        response = self.save(changes)
        self.assertEqual(response.status_code, 200, response.json)
        for key, value in changes.items():
            self.assertEqual(self.app.settings.values[key], value)
        self.assertEqual(self.app.settings.values["chat"], channel)

    def test_proxy_switch_and_host_edits_preserve_write_only_credentials(self):
        self.setup()
        proxy = {"scheme": "socks5", "hostname": "proxy", "port": 1080,
                 "username": "private-user", "password": "private-password"}
        self.assertEqual(self.save({"proxy": proxy}).status_code, 200)
        data = self.client.get("/api/configuration")
        self.assertEqual(data.json["proxy_summary"], {"scheme": "socks5", "hostname": "proxy", "port": 1080})
        self.assertNotIn("private-user", data.get_data(as_text=True))
        self.assertNotIn("private-password", data.get_data(as_text=True))
        self.assertEqual(self.save({"proxy": {"hostname": "replacement"}}).status_code, 200)
        self.assertEqual(self.app.settings.values["proxy"]["password"], "private-password")
        self.assertEqual(self.save({"proxy": {}}).status_code, 200)
        self.assertEqual(self.app.settings.values["proxy"], {})
