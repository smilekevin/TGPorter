"""Read Bot heartbeat state through the production Flask API without Telegram."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from module import bot, web


class WebBotStatusTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(web_login_secret="", bot_token="private-token")
        self.backend = bot.DownloadBot()
        self.backend.app = self.app
        self.backend.bot = SimpleNamespace(is_connected=True, invoke=AsyncMock())
        self.client = web.get_flask_app().test_client()
        for item in (patch.object(web, "_app", self.app),
                     patch.object(bot, "_bot", self.backend),
                     patch("module.pyrogram_extension._bot_flood_wait", {"until": 0})):
            item.start()
            self.addCleanup(item.stop)

    def test_read_only_status_contains_no_credentials_and_does_not_probe(self):
        self.backend._publish_connection_status("connected", checked=True, rtt_ms=42)
        for _ in range(2):
            response = self.client.get("/get_bot_status")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.json["status"], "connected")
            self.assertEqual(response.json["rtt_ms"], 42)
            self.assertNotIn("private-token", response.get_data(as_text=True))
            self.assertEqual(set(response.json), {
                "status", "last_checked_at", "last_success_at", "rtt_ms", "failures",
                "flood_wait_remaining", "heartbeat_interval",
            })
        self.backend.bot.invoke.assert_not_called()

    def test_disabled_and_starting_apps_do_not_reuse_another_apps_bot(self):
        self.backend._publish_connection_status("connected", checked=True, rtt_ms=42)
        self.app.bot_token = ""
        self.assertEqual(self.client.get("/get_bot_status").json["status"], "disabled")
        self.app.bot_token = "private-token"
        self.backend.app = SimpleNamespace()
        result = self.client.get("/get_bot_status").json
        self.assertEqual(result["status"], "connecting")
        self.assertIsNone(result["last_checked_at"])
        self.assertIsNone(result["rtt_ms"])

    def test_password_session_protects_status_and_bearer_does_not_bypass_it(self):
        self.app.web_login_secret = "test-password"
        for headers in ({}, {"Authorization": "Bearer private-token"}):
            self.assertEqual(self.client.get("/get_bot_status", headers=headers).status_code, 401)
        self.client.get("/login")
        with self.client.session_transaction() as session:
            csrf = session["web_csrf_token"]
        self.client.post("/login", data={"password": "test-password", "csrf_token": csrf})
        self.assertEqual(self.client.get("/get_bot_status").status_code, 200)


if __name__ == "__main__":
    unittest.main()
