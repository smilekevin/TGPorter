"""Exercise Web authorization without Telegram access or real session data."""

import asyncio
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from module.telegram import pyrogram
from module.telegram_login import TelegramLogin, WebLoginClient, WebLoginRequired


class TelegramLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = SimpleNamespace(api_id=123, api_hash="private", proxy={}, start_timeout=60,
                                   session_file_path=self.directory.name,
                                   settings=SimpleNamespace(setup_complete=True))
        self.user = pyrogram.types.User(id=123, first_name="Offline", username="offline")
        self.client = SimpleNamespace(connect=AsyncMock(return_value=False), disconnect=AsyncMock(),
            send_code=AsyncMock(return_value=SimpleNamespace(phone_code_hash="private-code-hash")),
            sign_in=AsyncMock(return_value=self.user), check_password=AsyncMock(return_value=self.user),
            sign_in_bot=AsyncMock(), log_out=AsyncMock(), start=AsyncMock(),
            stop=AsyncMock(), storage=SimpleNamespace(close=AsyncMock(), save=AsyncMock()),
            executor=Mock(), session=None, me=self.user, is_connected=True, is_initialized=False)
        self.start, self.stop = AsyncMock(), AsyncMock()
        self.factory = Mock(return_value=self.client)
        self.manager = TelegramLogin(self.app, self.start, self.stop, self.factory)
        self.addAsyncCleanup(self.manager._close)

    async def action(self, action, **values):
        await self.manager.submit(action, values, "browser-owner")
        await self.manager.operation

    async def test_phone_code_and_password_share_one_client_and_start_backend_once(self):
        await self.action("send_code", phone="+1234567890")
        self.assertEqual(self.manager.state, "code_required")
        self.client.sign_in.side_effect = pyrogram.errors.SessionPasswordNeeded
        await self.action("sign_in", code="12345")
        self.assertEqual(self.manager.state, "password_required")
        await self.action("password", password="two-step-secret")
        self.assertEqual(self.manager.state, "ready")
        self.factory.assert_called_once()
        self.start.assert_awaited_once_with(self.client)
        self.client.storage.save.assert_awaited_once()
        self.assertIsNone(self.manager.phone)
        self.assertIsNone(self.manager.code_hash)
        self.assertNotIn("two-step-secret", str(self.manager.__dict__))
        self.assertEqual(self.manager.status()["account"]["id"], 123)

    async def test_existing_authorized_session_needs_no_code(self):
        self.client.connect.return_value = True
        await self.action("connect")
        self.assertEqual(self.manager.state, "ready")
        self.client.send_code.assert_not_awaited()
        self.start.assert_awaited_once()

    async def test_bot_authentication_uses_same_backend(self):
        await self.action("bot", bot_token="123:private")
        self.client.sign_in_bot.assert_awaited_once_with("123:private")
        self.start.assert_awaited_once()
        self.assertNotIn("123:private", str(self.manager.__dict__))

    async def test_basic_login_requires_all_credentials_before_creating_a_client(self):
        self.app.bot_token = ""
        self.app.use_user_account = False
        with self.assertRaisesRegex(ValueError, "API ID, API Hash and Bot Token"):
            await self.manager.submit("login", {}, "browser-owner")
        self.factory.assert_not_called()

    async def test_basic_login_uses_saved_bot_token_by_default_or_requests_phone_code(self):
        self.app.bot_token = "123:saved-token"
        self.app.use_user_account = False
        await self.action("login")
        self.client.sign_in_bot.assert_awaited_once_with("123:saved-token")
        self.client.send_code.assert_not_awaited()
        self.assertEqual(self.manager.state, "ready")
        await self.manager._close()
        self.manager.authorized = False
        self.app.use_user_account = True
        with self.assertRaisesRegex(ValueError, "international phone"):
            await self.manager.submit("login", {"phone": "bad"}, "browser-owner")
        await self.action("login", phone="+1234567890")
        self.assertEqual(self.manager.state, "code_required")
        self.client.send_code.assert_awaited_once_with("+1234567890")

    async def test_changed_credentials_reconnect_without_a_service_restart_or_new_code(self):
        self.app.bot_token = "123:old-token"
        self.app.use_user_account = True
        self.app.active_settings = {"api_id": 123, "api_hash": "old-hash", "bot_token": self.app.bot_token,
                                    "use_user_account": True, "proxy": {}, "start_timeout": 60}
        self.app.settings.values = {**self.app.active_settings, "api_hash": "new-hash"}
        self.manager.client, self.manager.ready, self.manager.authorized = self.client, True, True
        self.client.connect.return_value = True
        self.client.get_me = AsyncMock(return_value=self.user)
        self.client.invoke = AsyncMock()
        def apply(app):
            self.assertIsNone(self.manager.client)
            self.stop.assert_awaited_once()
            app.api_hash = "new-hash"
            app.active_settings = dict(app.settings.values)
        with patch("module.configuration.apply_inactive_settings", side_effect=apply):
            await self.action("test")
        self.assertTrue(self.manager.ready)
        self.assertEqual(self.app.api_hash, "new-hash")
        self.client.send_code.assert_not_awaited()

    async def test_failed_reconfiguration_probe_closes_the_replacement_connection(self):
        self.app.active_settings = {"api_hash": "old-hash"}
        self.app.settings.values = {"api_hash": "new-hash"}
        self.manager.client, self.manager.ready, self.manager.authorized = self.client, True, True
        self.client.connect.side_effect = TimeoutError
        with patch("module.configuration.apply_inactive_settings"):
            await self.action("test")
        self.stop.assert_awaited_once()
        self.assertIsNone(self.manager.client)
        self.assertFalse(self.manager.ready)
        self.assertTrue(self.manager.authorized)
        self.assertEqual(self.manager.status()["connection_state"], "failed")

    async def test_account_change_stops_backend_and_archives_old_session_before_new_login(self):
        self.app.bot_token = "123:token"
        self.app.use_user_account = True
        self.app.active_settings = {"api_id": 123, "api_hash": "private", "bot_token": self.app.bot_token,
                                    "use_user_account": True, "proxy": {}, "start_timeout": 60}
        self.app.settings.values = {**self.app.active_settings, "use_user_account": False}
        path = Path(self.directory.name) / "media_downloader.session"
        path.write_bytes(b"offline saved session")
        self.manager.client, self.manager.ready, self.manager.authorized = self.client, True, True
        self.client.me = pyrogram.types.User(id=123, is_bot=True)
        def apply(app):
            self.stop.assert_awaited_once()
            self.assertFalse(path.exists())
            self.assertFalse(self.manager.authorized)
            app.use_user_account = False
            app.active_settings = dict(app.settings.values)
        with patch("module.configuration.apply_inactive_settings", side_effect=apply):
            await self.action("login")
        self.client.sign_in_bot.assert_awaited_once_with("123:token")
        backups = list(path.parent.glob("media_downloader.session.previous-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"offline saved session")
        self.assertTrue(self.manager.ready)

    async def test_saved_user_session_cannot_silently_override_bot_download_choice(self):
        self.app.bot_token = "123:token"
        self.app.use_user_account = False
        self.client.connect.return_value = True
        self.client.get_me = AsyncMock(return_value=self.user)
        await self.action("login")
        self.assertEqual(self.manager.state, "error")
        self.start.assert_not_awaited()
        self.assertIsNone(self.manager.client)

    async def test_upgrade_infers_original_bot_account_mode_from_authorized_session(self):
        self.app.bot_token = "123:token"
        self.app.use_user_account = True
        self.app.active_settings = {}
        self.app.settings.infer_account_mode = True
        self.app.settings.save = Mock()
        self.client.me = pyrogram.types.User(id=123, is_bot=True)
        self.client.get_me = AsyncMock(return_value=self.client.me)
        self.client.connect.return_value = True
        await self.action("connect")
        self.assertFalse(self.app.use_user_account)
        self.app.settings.save.assert_called_once_with({"use_user_account": False})
        self.assertFalse(self.app.settings.infer_account_mode)
        self.assertEqual(self.manager.state, "ready")

    async def test_backend_bot_validation_failure_never_reports_success(self):
        self.app.bot_token = "123:token"
        self.app.use_user_account = False
        self.start.side_effect = pyrogram.errors.AccessTokenInvalid
        await self.action("login")
        self.assertEqual(self.manager.state, "error")
        self.assertFalse(self.manager.ready)
        self.assertIsNone(self.manager.status()["account"])

    async def test_authorized_startup_failure_keeps_login_and_reconnects_without_a_phone_code(self):
        self.app.bot_token = "123:token"
        self.app.use_user_account = True
        self.client.connect.return_value = True
        self.client.get_me = AsyncMock(return_value=self.user)
        entered, release = asyncio.Event(), asyncio.Event()
        async def unavailable(client):
            self.manager.set_stage("Initializing command bot")
            entered.set()
            await release.wait()
            raise TimeoutError
        self.manager.start_backend = unavailable
        await self.manager.submit("connect", {}, "browser-owner")
        await entered.wait()
        status = self.manager.status()
        self.assertEqual(status["state"], "starting")
        self.assertTrue(status["authorized"])
        self.assertTrue(status["busy"])
        self.assertEqual(status["stage"], "Initializing command bot")
        release.set()
        await self.manager.operation
        status = self.manager.status()
        self.assertEqual(status["state"], "error")
        self.assertTrue(status["authorized"])
        self.assertTrue(status["can_logout"])
        self.assertIn("Saved login is kept", status["message"])
        self.manager.start_backend = self.start
        await self.action("login")
        self.assertEqual(self.manager.state, "ready")
        self.client.send_code.assert_not_awaited()

    async def test_connection_failure_without_saved_authorization_does_not_offer_logout(self):
        self.client.connect.side_effect = TimeoutError
        await self.action("connect")
        self.assertFalse(self.manager.status()["authorized"])
        self.assertFalse(self.manager.status()["can_logout"])

    async def test_saved_session_is_visible_before_any_network_connection(self):
        path = Path(self.directory.name) / "media_downloader.session"
        with sqlite3.connect(path) as database:
            database.execute("CREATE TABLE sessions (user_id INTEGER)")
            database.execute("INSERT INTO sessions VALUES (123)")
        database.close()
        manager = TelegramLogin(self.app, self.start, self.stop, self.factory)
        self.assertTrue(manager.status()["authorized"])
        self.assertEqual(manager.status()["connection_state"], "untested")
        self.assertTrue(manager.status()["can_logout"])
        self.factory.assert_not_called()

    async def test_connection_test_pings_existing_sessions_without_stopping_downloads(self):
        await self.action("bot", bot_token="123:token")
        self.client.invoke = AsyncMock()
        await self.action("test")
        self.client.invoke.assert_awaited_once()
        self.assertIsInstance(self.client.invoke.await_args.args[0], pyrogram.raw.functions.Ping)
        self.assertEqual(self.client.invoke.await_args.kwargs, {"retries": 1, "timeout": 10})
        self.assertEqual(self.manager.status()["connection_state"], "connected")
        self.stop.assert_not_awaited()
        self.client.invoke.side_effect = TimeoutError
        await self.action("test")
        self.assertTrue(self.manager.ready)
        self.assertTrue(self.manager.status()["authorized"])
        self.assertEqual(self.manager.status()["connection_state"], "failed")
        self.stop.assert_not_awaited()

    async def test_connection_test_without_a_session_cannot_create_a_login(self):
        with self.assertRaisesRegex(ValueError, "Sign in to Telegram"):
            await self.manager.submit("test", {}, "browser-owner")
        self.factory.assert_not_called()

    async def test_challenge_is_bound_to_browser_and_only_one_operation_runs(self):
        await self.action("send_code", phone="+1234567890")
        with self.assertRaises(ValueError):
            await self.manager.submit("sign_in", {"code": "12345"}, "another-browser")
        event = asyncio.Event()
        self.client.sign_in.side_effect = lambda *args: None
        async def blocked(*args):
            await event.wait()
            return self.user
        self.client.sign_in.side_effect = blocked
        await self.manager.submit("sign_in", {"code": "12345"}, "browser-owner")
        with self.assertRaises(ValueError):
            await self.manager.submit("send_code", {"phone": "+1234567890"}, "browser-owner")
        event.set()
        await self.manager.operation

    async def test_wrong_code_is_retryable_and_rate_limit_is_bounded(self):
        await self.action("send_code", phone="+1234567890")
        self.client.sign_in.side_effect = pyrogram.errors.PhoneCodeInvalid
        await self.action("sign_in", code="bad")
        self.assertEqual(self.manager.state, "code_required")
        self.client.sign_in.side_effect = pyrogram.errors.FloodWait(90)
        await self.action("sign_in", code="12345")
        self.assertGreater(self.manager.status()["retry_after"], 0)
        with self.assertRaises(ValueError):
            await self.manager.submit("sign_in", {}, "browser-owner")

    async def test_failure_and_cancellation_close_client_without_leaking_secret(self):
        self.client.send_code.side_effect = RuntimeError("credential-do-not-print")
        await self.action("send_code", phone="+1234567890")
        self.assertEqual(self.manager.state, "error")
        self.assertIsNone(self.manager.client)
        self.assertNotIn("credential-do-not-print", str(self.manager.status()))
        self.client.disconnect.assert_awaited()
        self.client.send_code.side_effect = None
        self.client.send_code.side_effect = asyncio.CancelledError
        await self.manager.submit("send_code", {"phone": "+1234567890"}, "browser-owner")
        await asyncio.gather(self.manager.operation, return_exceptions=True)
        self.assertIsNone(self.manager.client)

    async def test_logout_stops_backend_before_revoking_session(self):
        calls = []
        async def stop(client):
            calls.append("stop writers")
        async def logout():
            calls.append("logout")
        self.manager.stop_backend = stop
        self.client.log_out.side_effect = logout
        self.client.connect.return_value = True
        await self.action("connect")
        await self.action("logout")
        self.assertEqual(calls, ["stop writers", "logout"])
        self.assertEqual(self.manager.state, "logged_out")

    async def test_unconfigured_start_keeps_running_without_creating_client(self):
        self.app.settings.setup_complete = False
        runner = asyncio.create_task(self.manager.run())
        await asyncio.sleep(0)
        self.assertFalse(runner.done())
        self.factory.assert_not_called()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        self.assertTrue(self.manager.closed)

    async def test_start_never_reads_stdin_when_reauthorization_is_required(self):
        with self.assertRaises(WebLoginRequired):
            await WebLoginClient.authorize(None)

    async def test_terminal_login_authorizes_once_and_disables_stdin_for_future_reconnects(self):
        self.app.use_user_account, self.app.bot_token = True, "123:offline"
        self.app.settings.save = Mock()
        self.client.get_me = AsyncMock(return_value=self.user)
        async def authorize():
            self.assertTrue(self.client.allow_terminal_login)
            self.client.password, self.client.phone_code = "offline-password", "12345"
        self.client.authorize = AsyncMock(side_effect=authorize)
        with patch("module.telegram_login.sys.stdin.isatty", return_value=True):
            await self.manager.login_in_terminal()
        self.client.authorize.assert_awaited_once()
        self.assertFalse(self.client.allow_terminal_login)
        self.assertIsNone(self.client.password)
        self.assertIsNone(self.client.phone_code)
        self.start.assert_awaited_once_with(self.client)
        self.assertTrue(self.manager.ready)
        self.app.settings.save.assert_not_called()

    async def test_terminal_user_login_without_tty_does_not_attempt_authorization(self):
        self.app.use_user_account, self.app.bot_token = True, "123:offline"
        with patch("module.telegram_login.sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "interactive terminal"):
                await self.manager.login_in_terminal()
        self.factory.assert_not_called()

    async def test_terminal_failure_closes_client_without_echoing_password(self):
        self.app.use_user_account, self.app.bot_token = True, "123:offline"
        self.app.settings.save = Mock()
        self.client.authorize = AsyncMock(side_effect=RuntimeError("private-terminal-password"))
        with patch("module.telegram_login.sys.stdin.isatty", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "terminal login failed") as error:
                await self.manager.login_in_terminal()
        self.assertNotIn("private-terminal-password", str(error.exception))
        self.assertIsNone(self.manager.client)
        self.app.settings.save.assert_not_called()

    async def test_terminal_bot_login_uses_saved_token_without_needing_a_tty(self):
        self.app.use_user_account, self.app.bot_token = False, "123:offline"
        self.app.settings.save = Mock()
        self.client.get_me = AsyncMock(return_value=pyrogram.types.User(id=123, is_bot=True))
        self.client.authorize = AsyncMock()
        with patch("module.telegram_login.sys.stdin.isatty", return_value=False):
            await self.manager.login_in_terminal()
        self.assertEqual(self.factory.call_args.kwargs["bot_token"], "123:offline")
        self.client.authorize.assert_awaited_once()
        self.assertTrue(self.manager.ready)

    async def test_terminal_login_reuses_authorized_session_without_prompting(self):
        self.app.use_user_account, self.app.bot_token = True, "123:offline"
        self.manager.authorized = True
        self.client.connect.return_value = True
        self.client.get_me = AsyncMock(return_value=self.user)
        self.client.authorize = AsyncMock()
        with patch("module.telegram_login.sys.stdin.isatty", return_value=False):
            await self.manager.login_in_terminal()
        self.client.authorize.assert_not_awaited()
        self.assertTrue(self.manager.ready)

    async def test_expired_terminal_session_without_tty_does_not_fall_back_to_stdin(self):
        self.app.use_user_account, self.app.bot_token = True, "123:offline"
        self.manager.authorized = True
        self.client.authorize = AsyncMock()
        with patch("module.telegram_login.sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "terminal login failed"):
                await self.manager.login_in_terminal()
        self.client.authorize.assert_not_awaited()
        self.assertIsNone(self.manager.client)

    async def test_explicit_terminal_authorization_uses_sdk_only_while_enabled(self):
        client = object.__new__(WebLoginClient)
        client.allow_terminal_login = True
        with patch("module.telegram_login.HookClient.authorize", new_callable=AsyncMock) as authorize:
            await client.authorize()
        authorize.assert_awaited_once()
        client.allow_terminal_login = False
        with self.assertRaises(WebLoginRequired):
            await client.authorize()

    async def test_expiry_waiting_for_login_does_not_close_a_newly_authorized_backend(self):
        await self.manager.lock.acquire()
        self.manager.expires = 1
        expiry = asyncio.create_task(self.manager._expire_login())
        await asyncio.sleep(0)
        self.manager.client = self.client
        self.manager.ready = True
        self.manager.state = "ready"
        self.manager._clear_challenge()
        self.manager.lock.release()
        await expiry
        self.stop.assert_not_awaited()
        self.assertEqual(self.manager.state, "ready")

    async def test_cancel_releases_an_inflight_login_and_leaves_the_monitor_available(self):
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()
        self.client.send_code.side_effect = blocked
        await self.manager.submit("send_code", {"phone": "+1234567890"}, "browser-owner")
        await entered.wait()
        await self.manager.submit("cancel", {}, "browser-owner")
        self.assertIsNone(self.manager.client)
        self.assertEqual(self.manager.state, "logged_out")
        self.assertFalse(self.manager.closed)
