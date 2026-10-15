"""The Web API uses browser sessions; MCP credentials never bypass them."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from module import download_stat, web


class WebSessionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(web_login_secret="browser-password", mcp_token="agent-token")
        self.app = web.get_flask_app()
        for item in (
            patch.object(web, "_app", self.config),
            patch.dict(self.app.config, TESTING=True),
            patch.object(download_stat, "_download_state", download_stat.DownloadState.Downloading),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = self.app.test_client()

    def login(self):
        self.client.get("/login")
        with self.client.session_transaction() as session:
            csrf = session["web_csrf_token"]
        self.assertEqual(self.client.post("/login", data={
            "password": self.config.web_login_secret, "csrf_token": csrf,
        }).status_code, 302)
        with self.client.session_transaction() as session:
            return session["web_csrf_token"]

    def test_bearer_token_never_authenticates_web_reads_or_writes(self):
        for value in ("Bearer agent-token", "Bearer browser-password", "Bearer wrong", "Basic agent-token", ""):
            headers = {"Authorization": value}
            with self.subTest(authorization=value):
                self.assertEqual(self.client.get("/").headers["Location"], "/login")
                for method, path in (("GET", "/get_download_state"),
                                     ("POST", "/set_download_state?state=pause"),
                                     ("POST", "/batch_delete")):
                    response = self.client.open(path, method=method, headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.get_json()["error"], "authentication_required")
                    self.assertNotIn("WWW-Authenticate", response.headers)
                self.assertIs(download_stat.get_download_state(), download_stat.DownloadState.Downloading)

    def test_bearer_header_cannot_replace_csrf_on_a_valid_session(self):
        csrf = self.login()
        for token in (self.config.mcp_token, "wrong"):
            with self.subTest(token=token):
                headers = {"Authorization": "Bearer " + token}
                self.assertEqual(self.client.get("/get_download_state", headers=headers).status_code, 200)
                response = self.client.post("/set_download_state?state=pause", headers=headers)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"], "invalid_csrf_token")
                self.assertIs(download_stat.get_download_state(), download_stat.DownloadState.Downloading)
        response = self.client.post("/set_download_state?state=pause", headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200)
        self.assertIs(download_stat.get_download_state(), download_stat.DownloadState.StopDownload)

    def test_query_and_body_mcp_tokens_cannot_authenticate_web_api(self):
        self.assertEqual(self.client.get("/get_download_state", query_string={
            "mcp_token": self.config.mcp_token,
        }).status_code, 401)
        self.assertEqual(self.client.post("/set_download_state?state=pause", json={
            "mcp_token": self.config.mcp_token,
        }).status_code, 401)

    def test_rotating_mcp_token_does_not_change_browser_session(self):
        csrf = self.login()
        self.config.mcp_token = "replacement-token"
        self.assertEqual(self.client.get("/get_download_state").status_code, 200)
        self.config.mcp_token = ""
        response = self.client.post("/set_download_state?state=pause", headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200)

    def test_mcp_secret_never_appears_in_browser_pages(self):
        self.assertNotIn(self.config.mcp_token, self.client.get("/login").get_data(as_text=True))
        self.login()
        self.assertNotIn(self.config.mcp_token, self.client.get("/").get_data(as_text=True))

    def test_empty_password_preserves_public_web_mode_independently_of_mcp(self):
        self.config.web_login_secret = ""
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/get_download_state").status_code, 200)
        self.assertEqual(self.client.post("/set_download_state?state=pause").status_code, 200)


if __name__ == "__main__":
    unittest.main()
