"""Non-interactive Telegram authorization owned by the downloader event loop."""

import asyncio
import os
import sqlite3
import sys
import time
from contextlib import closing

from loguru import logger

from module.telegram import pyrogram
from module.pyrogram_extension import HookClient
from module.settings import CONNECTION_FIELDS

BASIC_FIELDS = CONNECTION_FIELDS


class WebLoginRequired(Exception):
    """A session needs authorization through the Configuration page."""


class WebLoginClient(HookClient):
    async def load_session(self):
        await super().load_session()
        if self.api_id and await self.storage.api_id() != int(self.api_id):
            await self.storage.api_id(int(self.api_id))

    async def authorize(self, *args, **kwargs):
        # Only an explicit --login startup may read terminal input.
        if getattr(self, "allow_terminal_login", False):
            return await super().authorize(*args, **kwargs)
        raise WebLoginRequired("Telegram login is required in Configuration")


class TelegramLogin:
    """Keep one client, one login attempt and all SDK calls on the owning loop."""

    def __init__(self, app, start_backend, stop_backend, client_factory=WebLoginClient):
        self.app = app
        self.start_backend = start_backend
        self.stop_backend = stop_backend
        self.client_factory = client_factory
        self.client = None
        self.ready = False
        self.state = "unconfigured"
        self.message = ""
        self.account = None
        self.operation = None
        self.owner = None
        self.phone = None
        self.code_hash = None
        self.expires = 0
        self.retry_at = 0
        self.lock = asyncio.Lock()
        self.closed = False
        self.validate_bot_credentials = True
        self.stage = ""
        self.authorized = self._has_saved_login()
        self.connection_state = "untested"

    def _has_saved_login(self):
        """Inspect only the authorization marker, without opening another client."""
        from pathlib import Path
        path = Path(self.app.session_file_path) / "media_downloader.session"
        if not path.is_file():
            return False
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as database:
                row = database.execute("SELECT user_id FROM sessions LIMIT 1").fetchone()
                return bool(row and row[0])
        except sqlite3.Error:
            return False

    def set_stage(self, stage):
        """Publish credential-free startup progress on the owning loop."""
        self.stage = stage
        logger.info("Telegram configuration stage: {}", stage)

    def status(self):
        state = "error" if self.ready and not self.client.is_connected else self.state
        return {"state": state, "busy": bool(self.operation and not self.operation.done()),
                "has_client": self.client is not None,
                "stage": self.stage, "authorized": self.authorized,
                "can_logout": self.authorized,
                "connection_state": self.connection_state,
                "message": self.message, "account": self.account,
                "retry_after": max(0, int(self.retry_at - time.monotonic() + 0.999))}

    async def login_in_terminal(self):
        """Authorize an explicit --login request before exposing browser operations."""
        if not self.app.api_id or not self.app.api_hash:
            raise ValueError("Configure API ID and API Hash before terminal login")
        if not self.app.use_user_account and not self.app.bot_token:
            raise ValueError("Enter a Bot Token before terminal login in bot download mode")
        if not self.authorized and self.app.use_user_account and not sys.stdin.isatty():
            raise ValueError("Telegram --login requires an interactive terminal. Start with a TTY, "
                             "or omit --login and sign in through Configuration.")
        logger.info("Telegram --login: completing authorization in the terminal")
        self.client = self.client_factory(
            "media_downloader", api_id=self.app.api_id, api_hash=self.app.api_hash,
            bot_token=None if self.app.use_user_account else self.app.bot_token,
            proxy=self.app.proxy or None, workdir=self.app.session_file_path,
            start_timeout=self.app.start_timeout, no_updates=False, hide_password=True,
        )
        try:
            async with self.lock:
                authorized = await self.client.connect()
                if not authorized:
                    if self.app.use_user_account and not sys.stdin.isatty():
                        raise ValueError("Telegram user login requires an interactive terminal")
                    self.client.allow_terminal_login = True
                    try:
                        await self.client.authorize()
                    finally:
                        self.client.allow_terminal_login = False
                        self.client.password = self.client.phone_code = self.client.phone_number = None
                self.authorized = True
                user = await self.client.get_me()
                if getattr(self.app.settings, "infer_account_mode", False):
                    self.app.settings.save({"use_user_account": not bool(user.is_bot)})
                    self.app.use_user_account = not bool(user.is_bot)
                    self.app.active_settings["use_user_account"] = self.app.use_user_account
                    self.app.settings.infer_account_mode = False
                if bool(user.is_bot) == self.app.use_user_account or (
                        user.is_bot and self.app.bot_token
                        and str(user.id) != self.app.bot_token.split(":", 1)[0]):
                    raise ValueError("Existing Telegram session does not match the configured download account")
                await self._activate()
        except BaseException as error:
            await self._close()
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise RuntimeError(f"Telegram terminal login failed ({type(error).__name__}). "
                               "Retry in an interactive terminal; existing sessions are preserved.") from None

    async def submit(self, action, data, owner):
        if self.closed:
            raise ValueError("Service is stopping")
        if action == "cancel" and self.operation and not self.operation.done():
            self.operation.cancel()
            await asyncio.gather(self.operation, return_exceptions=True)
            self.state = "logged_out"
            return self.status()
        if self.operation and not self.operation.done():
            raise ValueError("A Telegram operation is already running")
        if action not in {"cancel", "logout"} and time.monotonic() < self.retry_at:
            raise ValueError("Wait before trying Telegram again")
        if action not in {"test", "login", "connect", "send_code", "sign_in", "password", "bot", "logout", "cancel"}:
            raise ValueError("Unknown Telegram operation")
        if action == "test" and not self.authorized:
            raise ValueError("Sign in to Telegram before testing the connection")
        saved = getattr(self.app.settings, "values", {})
        active = getattr(self.app, "active_settings", saved)
        account_change = self._account_change(saved, active)
        if action == "test" and account_change:
            raise ValueError("Verify login to change the download account before testing")
        if action == "login":
            credentials = saved or {key: getattr(self.app, key) for key in ("api_id", "api_hash", "bot_token", "use_user_account")}
            if not credentials["api_id"] or not credentials["api_hash"] or not credentials["bot_token"]:
                raise ValueError("Enter API ID, API Hash and Bot Token before signing in")
            if credentials["use_user_account"] and (account_change or not (self.ready or self.authorized)):
                phone = data.get("phone", "")
                if not phone.startswith("+") or not phone[1:].isdigit() or not 6 <= len(phone) <= 20:
                    raise ValueError("Use an international phone number")
        if action in {"sign_in", "password"}:
            if any(saved.get(key) != active.get(key) for key in BASIC_FIELDS):
                raise ValueError("Telegram settings changed. Cancel and verify login again.")
        if action in {"sign_in", "password"} and owner != self.owner:
            raise ValueError("Continue login in the browser that requested the code")
        self.operation = asyncio.create_task(self._perform(action, dict(data), owner))
        return self.status()

    @staticmethod
    def _account_change(saved, active):
        if not saved or not active:
            return False
        return saved.get("use_user_account") != active.get("use_user_account") or (
            not saved.get("use_user_account") and
            saved.get("bot_token", "").split(":", 1)[0] != active.get("bot_token", "").split(":", 1)[0])

    async def _apply_connection_settings(self):
        """Stop owners before adopting credentials; retain task data and old sessions."""
        saved = getattr(self.app.settings, "values", {})
        active = getattr(self.app, "active_settings", saved)
        if not any(saved.get(key) != active.get(key) for key in CONNECTION_FIELDS):
            return
        account_change = self._account_change(saved, active)
        token_change = saved.get("bot_token", "").split(":", 1)[0] != active.get("bot_token", "").split(":", 1)[0]
        self.set_stage("Applying Telegram settings")
        await self._close()
        names = ["media_downloader"] if account_change else []
        if token_change:
            names.append(getattr(self.app, "application_name", "media_downloader") + "_bot")
        for name in names:
            path = os.path.join(self.app.session_file_path, name + ".session")
            if os.path.isfile(path):
                os.replace(path, path + f".previous-{time.time_ns()}")
        if account_change:
            self.authorized = False
        from module.configuration import apply_inactive_settings
        apply_inactive_settings(self.app)

    async def _connect(self):
        if self.client is not None:
            return
        if not self.app.api_id or not self.app.api_hash:
            raise ValueError("Save API ID and API Hash before signing in")
        self.client = self.client_factory(
            "media_downloader", api_id=self.app.api_id, api_hash=self.app.api_hash,
            proxy=self.app.proxy or None, workdir=self.app.session_file_path,
            start_timeout=self.app.start_timeout, no_updates=False,
        )
        self.set_stage("Connecting download account")
        authorized = await self.client.connect()
        if authorized:
            # Reusing a session must never silently ignore a new account choice.
            if hasattr(self.app, "use_user_account"):
                user = await self.client.get_me()
                if getattr(self.app.settings, "infer_account_mode", False):
                    # Older settings had no account-mode switch. Resolve it from
                    # their authorized session rather than replacing that account.
                    self.app.settings.save({"use_user_account": not bool(user.is_bot)})
                    self.app.use_user_account = not bool(user.is_bot)
                    self.app.active_settings["use_user_account"] = self.app.use_user_account
                    self.app.settings.infer_account_mode = False
                expected_bot = not self.app.use_user_account
                if bool(user.is_bot) != expected_bot or (
                        user.is_bot and self.app.bot_token
                        and str(user.id) != self.app.bot_token.split(":", 1)[0]):
                    await self._close()
                    self.state = "error"
                    self.message = "Sign out of Telegram before changing the download account"
                    return
            await self._activate()
        else:
            self.state = "logged_out"
            self.expires = time.monotonic() + 600

    async def _activate(self):
        # Let SDK start() perform its complete update-state/dispatcher initialization
        # after storage has been authorized, without a second client or session.
        await self.client.storage.save()
        self.authorized = True
        self.state = "starting"
        self.set_stage("Starting download service")
        await self.client.disconnect()
        await self.start_backend(self.client)
        self.ready = True
        user = self.client.me
        self.account = {"id": user.id, "name": user.first_name or "",
                        "username": user.username or ""}
        self.state = "ready"
        self.stage = ""
        self.connection_state = "connected"
        self._clear_challenge()

    def _clear_challenge(self):
        self.owner = self.phone = self.code_hash = None
        self.expires = 0

    async def _close(self):
        client = self.client
        try:
            if client is not None:
                if self.ready:
                    await self.stop_backend(client)
                elif client.is_initialized:
                    await client.stop()
                elif client.is_connected:
                    await client.disconnect()
                else:
                    # A timed-out connect may have opened storage/transport already.
                    session = getattr(client, "session", None)
                    if session is not None:
                        await session.stop()
                    if getattr(client.storage, "conn", None) is not None:
                        await client.storage.close()
        finally:
            if client is not None:
                client.executor.shutdown(wait=False)
            self.client = None
            self.ready = False
            self.account = None
            self._clear_challenge()

    async def _perform(self, action, data, owner):
        async with self.lock:
            was_ready = self.ready
            self.message = ""
            if action not in {"cancel", "logout"}:
                self.connection_state = "testing"
            try:
                async with asyncio.timeout(120):
                    await self._action(action, data, owner)
            except pyrogram.errors.FloodWait as error:
                self.connection_state = "failed"
                self.retry_at = time.monotonic() + error.value
                self.message = "Telegram rate limit. Try again after the countdown."
                if self.authorized and not self.ready:
                    self.state = "error"
            except pyrogram.errors.SessionPasswordNeeded:
                self.connection_state = "untested"
                self.state = "password_required"
            except (pyrogram.errors.PhoneCodeInvalid, pyrogram.errors.PasswordHashInvalid):
                self.connection_state = "untested"
                self.message = "Incorrect code or password. Try again."
            except pyrogram.errors.PhoneCodeExpired:
                self.connection_state = "untested"
                self.state = "logged_out"
                self._clear_challenge()
                self.message = "Code expired. Request a new code."
            except asyncio.CancelledError:
                self.connection_state = "untested"
                await self._close()
                raise
            except Exception as error:
                # RPC exceptions can contain supplied phone numbers or credentials.
                # Return and log only the exception type, never its message.
                stage = self.stage or "Connecting download account"
                logger.warning("Telegram configuration operation failed ({}, stage={})", type(error).__name__, stage)
                if not (action == "test" and was_ready and self.ready):
                    await self._close()
                    self.state = "error"
                self.connection_state = "failed"
                self.message = (f"Telegram startup failed during {stage}. Saved login is kept; try reconnecting."
                                if self.authorized else "Telegram connection or login failed. Check settings and try again.")

    async def _action(self, action, data, owner):
        if action in {"cancel", "logout"}:
            if action == "logout" and self.ready:
                # Stop writers first. log_out() removes only this account's session;
                # task records and download checkpoints remain available for recovery.
                await self.stop_backend(self.client)
                self.ready = False
                try:
                    await self.client.start()
                    await self.client.log_out()
                except (pyrogram.errors.RPCError, ConnectionError, OSError, WebLoginRequired):
                    self.message = "Local session cleared. Telegram could not confirm remote sign-out."
            await self._close()
            if action == "logout":
                path = os.path.join(self.app.session_file_path, "media_downloader.session")
                for suffix in ("", "-journal", "-wal", "-shm"):
                    if os.path.isfile(path + suffix):
                        os.unlink(path + suffix)
                self.authorized = False
            self.stage = ""
            self.connection_state = "untested"
            self.state = "logged_out"
            return
        if action in {"connect", "test"} and self.state == "error":
            await self._close()
        if action in {"login", "connect", "test"}:
            await self._apply_connection_settings()
        if action == "login":
            action = "send_code" if self.app.use_user_account else "bot"
            data["bot_token"] = self.app.bot_token
        await self._connect()
        if self.client is None:
            return
        if action == "test" and self.ready:
            self.set_stage("Testing Telegram connection")
            clients = [self.client]
            from module.bot import _bot
            if _bot.app is self.app and _bot.bot is not None and _bot.bot is not self.client:
                clients.append(_bot.bot)
            for client in clients:
                await client.invoke(pyrogram.raw.functions.Ping(ping_id=0), retries=1, timeout=10)
            self.connection_state = "connected"
            self.stage = ""
            return
        if self.ready:
            return
        if action == "connect":
            return
        if action in {"send_code", "bot"}:
            self.owner = owner
            if action == "bot":
                self.set_stage("Authorizing download bot")
                token = data.get("bot_token", "")
                if not isinstance(token, str) or not token or len(token) > 256:
                    raise ValueError("Invalid Bot Token")
                await self.client.sign_in_bot(token)
                await self._activate()
                return
            phone = data.get("phone", "")
            if not isinstance(phone, str) or not phone.startswith("+") or not phone[1:].isdigit() or not 6 <= len(phone) <= 20:
                raise ValueError("Use an international phone number")
            self.set_stage("Requesting Telegram code")
            sent = await self.client.send_code(phone)
            self.phone, self.code_hash = phone, sent.phone_code_hash
            self.state = "code_required"
            self.connection_state = "untested"
            self.expires = time.monotonic() + 600
        elif action in {"sign_in", "password"}:
            if not self.expires or time.monotonic() >= self.expires:
                raise ValueError("Login expired")
            if action == "sign_in":
                if self.state != "code_required":
                    raise ValueError("Request a code first")
                self.set_stage("Verifying Telegram code")
                user = await self.client.sign_in(self.phone, self.code_hash, data.get("code", ""))
                if not isinstance(user, pyrogram.types.User):
                    raise ValueError("Sign in with an existing Telegram account")
            else:
                if self.state != "password_required":
                    raise ValueError("Password is not required")
                self.set_stage("Verifying Telegram password")
                await self.client.check_password(data.get("password", ""))
            await self._activate()

    async def run(self):
        """Restore an existing session and expire abandoned authorization attempts."""
        try:
            if (self.app.settings.setup_complete or getattr(self.app, "public_web", False)) and self.app.api_id and self.app.api_hash:
                session = os.path.join(self.app.session_file_path, "media_downloader.session")
                if os.path.isfile(session):
                    await self.submit("connect", {}, None)
                    await asyncio.gather(self.operation, return_exceptions=True)
                else:
                    self.state = "logged_out"
            while True:
                await asyncio.sleep(15)
                await self._expire_login()
        finally:
            self.closed = True
            if self.operation and not self.operation.done():
                self.operation.cancel()
                await asyncio.gather(self.operation, return_exceptions=True)
            await self._close()

    async def _expire_login(self):
        async with self.lock:
            # A successful authorization may clear the deadline while expiry waits
            # for an in-flight SDK call. Never close the newly started backend.
            if self.expires and time.monotonic() >= self.expires:
                await self._close()
                self.state = "logged_out"
