"""Integrated single-port WebUI, API and MCP regression tests without Telegram."""

import asyncio
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import utils
from module import bot, download_stat, task_store, web
from module.mcp_server import create_server
from module.download_service import DownloadService
from module.app import Application


class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.done_file = self.path / "completed.mp4"
        self.done_file.write_bytes(b"completed")
        self.config = SimpleNamespace(
            web_login_secret="test-browser-password", mcp_token="test-mcp-token",
            max_download_task=2, save_app_data=Mock(), loop=None,
            mcp_enabled=True, mcp_read_only=False,
            mcp_allowed_hosts=["NAS.EXAMPLE.COM", "192.168.1.20:5000"],
        )
        patches = (
            patch.object(web, "_app", self.config),
            patch.object(download_stat, "_download_state", download_stat.DownloadState.Downloading),
            patch.object(download_stat, "_download_result", {100: {
                1: self.download("active.mp4", complete=False),
                2: self.download(str(self.done_file), complete=True),
            }}),
            patch.object(download_stat, "_failed_downloads", [{
                "task_id": "failed-1", "chat_id": 100, "msg_id": 3,
                "file_name": "failed.mp4", "error_message": "offline failure", "timestamp": time.time(),
            }]),
            patch.object(download_stat, "_paused_tasks", set()),
            patch.object(download_stat, "_cancelled_tasks", set()),
            patch.object(download_stat, "_discarded_tasks", set()),
            patch.object(download_stat, "_HISTORY_FILE", str(self.path / "history.json")),
            patch.object(task_store, "_TASKS_FILE", str(self.path / "tasks.json")),
            patch.object(task_store, "_COUNTER_FILE", str(self.path / "counter.json")),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        task_store.save_task(7, 100, "", 0, 0, 1, None, 100, extra_data={"filename": "pending.mp4"})
        self.service = DownloadService(self.config)
        self.server = create_server(self.service)
        await self.start_listener()

    async def start_listener(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.runtime = uvicorn.Server(uvicorn.Config(web.create_web_app(self.config),
                                                   log_level="critical", access_log=False))
        self.thread = threading.Thread(target=lambda: self.runtime.run(sockets=[self.sock]), daemon=True)
        self.thread.start()
        for _ in range(300):
            if self.runtime.started:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.runtime.started)

    async def asyncTearDown(self):
        self.runtime.should_exit = True
        await asyncio.to_thread(self.thread.join, 5)
        self.sock.close()
        self.assertFalse(self.thread.is_alive())

    @staticmethod
    def download(filename, complete):
        return {"file_name": filename, "total_size": 100, "down_byte": 100 if complete else 25,
                "download_speed": 0, "start_time": 1, "end_time": 2, "task_id_display": "display-only"}

    def assert_success(self, result):
        self.assertFalse(result.is_error, result.content)
        self.assertIsInstance(result.structured_content, dict)
        return result.structured_content

    async def login_browser(self, http):
        await http.get(self.url + "/login")
        serializer = web.get_flask_app().session_interface.get_signing_serializer(web.get_flask_app())
        csrf = serializer.loads(http.cookies.get("session"))["web_csrf_token"]
        response = await http.post(self.url + "/login", data={
            "password": self.config.web_login_secret, "csrf_token": csrf,
        })
        self.assertEqual(response.status_code, 302)
        return serializer.loads(http.cookies.get("session"))["web_csrf_token"]

    async def test_discovery_exposes_typed_tools_and_destructive_annotations(self):
        async with Client(self.server) as client:
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            self.assertEqual(len(tools), 13)
            self.assertTrue(tools["get_status"].annotations.read_only_hint)
            self.assertTrue(tools["delete_task"].annotations.destructive_hint)
            self.assertFalse(tools["delete_task"].input_schema["properties"]["delete_file"]["default"])
            self.assertEqual(tools["list_downloads"].input_schema["properties"]["limit"]["maximum"], 500)
            self.assertIn("not task_id_display", tools["pause_task"].input_schema["properties"]["task_id"]["description"])
            self.assertNotIn(self.config.mcp_token, str(listed))

    async def test_status_preserves_the_shared_runtime_version(self):
        with patch.object(utils, "__version__", "v1.2.3-rc.1"):
            async with Client(create_server(self.service)) as client:
                status = self.assert_success(await client.call_tool("get_status"))
                self.assertEqual(status["version"], "v1.2.3-rc.1")

    async def test_status_and_all_four_lists_use_shared_operations(self):
        async with Client(self.server) as client:
            status = self.assert_success(await client.call_tool("get_status"))
            self.assertEqual(status["download_state"], "downloading")
            self.assertEqual(status["max_workers"], 2)
            self.assertEqual(status["completed_total"], 1)
            self.assertEqual(status["bot_status"]["status"], "disabled")
            expected = {"active": "100_1", "completed": "100_2", "pending": "7", "failed": "failed-1"}
            for state, task_id in expected.items():
                with self.subTest(state=state):
                    result = self.assert_success(await client.call_tool("list_downloads", {"status": state}))
                    self.assertEqual(result["total"], 1)
                    self.assertEqual(result["tasks"][0]["task_id"], task_id)

    async def test_http_status_shares_bot_health_with_web_without_probing_telegram(self):
        backend = bot.DownloadBot()
        backend.app = self.config
        backend.bot = SimpleNamespace(is_connected=True, invoke=AsyncMock())
        flood_wait = {"until": 0}
        clock = SimpleNamespace(time=lambda: 1000, monotonic=lambda: 1000)
        headers = {"Authorization": "Bearer " + self.config.mcp_token}
        with patch.object(bot, "_bot", backend), patch.object(bot, "time", clock), \
                patch("module.pyrogram_extension._bot_flood_wait", flood_wait):
            async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=5) as http:
                # MCP credentials alone must not authenticate the Web API.
                self.assertEqual((await http.get(self.url + "/get_bot_status")).status_code, 401)
                await self.login_browser(http)
                async with Client(streamable_http_client(self.url + "/mcp", http_client=http)) as client:
                    for state in ("connected", "unresponsive", "reconnecting", "disconnected",
                                  "unknown", "flood_wait", "stopped", "disabled"):
                        with self.subTest(state=state):
                            self.config.bot_token = "private-bot-token" if state != "disabled" else ""
                            backend.is_running = state != "stopped"
                            backend.bot.is_connected = state != "disconnected"
                            flood_wait["until"] = 1120 if state == "flood_wait" else 0
                            backend._publish_connection_status(
                                "unknown" if state == "flood_wait" else state,
                                checked=True, rtt_ms=42 if state == "connected" else None)
                            result = self.assert_success(await client.call_tool("get_status"))
                            browser = await http.get(self.url + "/get_bot_status")
                            self.assertEqual(result["bot_status"], browser.json())
                            self.assertEqual(result["bot_status"]["status"], state)
                            self.assertNotIn("private-bot-token", str(result))
                backend.bot.invoke.assert_not_called()

    async def test_completed_search_and_pagination_preserve_totals(self):
        async with Client(self.server) as client:
            result = self.assert_success(await client.call_tool("list_downloads", {
                "status": "completed", "search": "completed.mp4", "offset": 1, "limit": 1,
            }))
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["tasks"], [])
            active = self.assert_success(await client.call_tool("list_downloads", {"offset": 1, "limit": 1}))
            self.assertEqual(active["total"], 1)
            self.assertEqual(active["tasks"], [])

    async def test_global_pause_and_resume_translate_next_button_action_correctly(self):
        async with Client(self.server) as client:
            paused = self.assert_success(await client.call_tool("set_download_state", {"state": "paused"}))
            self.assertEqual(paused, {"state": "paused"})
            self.assertIs(download_stat.get_download_state(), download_stat.DownloadState.StopDownload)
            resumed = self.assert_success(await client.call_tool("set_download_state", {"state": "downloading"}))
            self.assertEqual(resumed, {"state": "downloading"})
            self.assertIs(download_stat.get_download_state(), download_stat.DownloadState.Downloading)

    async def test_individual_pause_resume_and_worker_setting_reach_real_handlers(self):
        async with Client(self.server) as client:
            self.assert_success(await client.call_tool("pause_task", {"task_id": "100_1"}))
            self.assertTrue(download_stat.is_task_paused("100_1"))
            self.assert_success(await client.call_tool("resume_task", {"task_id": "100_1"}))
            self.assertFalse(download_stat.is_task_paused("100_1"))
            workers = self.assert_success(await client.call_tool("set_max_workers", {"workers": 3}))
            self.assertEqual(workers, {"max_workers": 3})
            self.assertEqual(self.config.max_download_task, 3)
            self.config.save_app_data.assert_called_once_with(True)

    async def test_api_business_failure_is_a_tool_error_instead_of_false_success(self):
        async with Client(self.server) as client:
            for name in ("resume_task", "retry_task", "delete_task"):
                with self.subTest(tool=name):
                    result = await client.call_tool(name, {"task_id": "does-not-exist"})
                    self.assertTrue(result.is_error)
                    self.assertIn("Download operation rejected", result.content[0].text)
            batch = self.assert_success(await client.call_tool("batch_retry_tasks", {"task_ids": ["missing"]}))
            self.assertEqual(batch["queued"], 0)
            self.assertTrue(batch["errors"])

    async def test_retry_tools_submit_to_downloader_loop_and_report_acceptance(self):
        self.config.loop = asyncio.get_running_loop()
        retry = patch.object(self.service, "retry_download", new_callable=AsyncMock)
        queued = retry.start()
        self.addCleanup(retry.stop)
        async with Client(self.server) as client:
            single = self.assert_success(await client.call_tool("retry_task", {"task_id": "failed-1"}))
            self.assertEqual(single["code"], "1")
            queued.assert_awaited_once()
            download_stat._failed_downloads.append({
                "task_id": "failed-2", "chat_id": 100, "msg_id": 4,
                "file_name": "failed-2.mp4", "error_message": "offline failure",
            })
            batch = self.assert_success(await client.call_tool("batch_retry_tasks", {"task_ids": ["failed-2", "missing"]}))
            self.assertEqual(batch["queued"], 1)
            self.assertEqual(len(batch["errors"]), 1)
            self.assertEqual(queued.await_count, 2)

    async def test_file_check_and_default_delete_preserve_downloaded_file(self):
        async with Client(self.server) as client:
            checked = self.assert_success(await client.call_tool("check_task_file", {"task_id": "100_2"}))
            self.assertTrue(checked["exists"])
            deleted = self.assert_success(await client.call_tool("delete_task", {"task_id": "100_2"}))
            self.assertFalse(deleted["file_deleted"])
            self.assertTrue(self.done_file.exists())
            self.assertNotIn(2, download_stat.get_download_result()[100])

    async def test_failed_file_and_partial_deletion_via_mcp(self):
        failed_file = self.path / "failed.mp4"
        failed_file.write_bytes(b"partial output")
        staging = self.path / "failed-staging.mp4"
        parts = Path(str(staging) + ".parts")
        parts.mkdir()
        (parts / "manifest.json").write_text("{}")
        download_stat._failed_downloads[0].update(
            file_name=str(failed_file), temp_file_name=str(staging))
        async with Client(self.server) as client:
            checked = self.assert_success(await client.call_tool("check_task_file", {"task_id": "failed-1"}))
            self.assertTrue(checked["exists"])
            deleted = self.assert_success(await client.call_tool("delete_task", {
                "task_id": "failed-1", "delete_file": True,
            }))
            self.assertTrue(deleted["file_deleted"])
            self.assertFalse(failed_file.exists())
            self.assertFalse(parts.exists())
            self.assertTrue(self.done_file.exists())

    async def test_explicit_file_deletion_and_pending_removal(self):
        async with Client(self.server) as client:
            deleted = self.assert_success(await client.call_tool("batch_delete_tasks", {
                "task_ids": ["100_2"], "delete_file": True,
            }))
            self.assertEqual(deleted["files_deleted"], 1)
            self.assertFalse(self.done_file.exists())
            self.assert_success(await client.call_tool("remove_pending_task", {"task_id": "7"}))
            self.assertEqual(task_store.get_pending_tasks(), [])

    async def test_parameter_validation_rejects_unsafe_coercion_before_shared_operations(self):
        operations = ("web_pause_task", "get_download_list", "set_max_workers",
                      "web_batch_delete", "web_batch_retry", "web_delete_task")
        calls = []
        for name in operations:
            item = patch.object(self.service, name, Mock())
            calls.append(item.start())
            self.addCleanup(item.stop)
        async with Client(self.server) as client:
            for name, args in (
                ("pause_task", {"task_id": ""}), ("pause_task", {"task_id": 123}),
                ("list_downloads", {"limit": 0}), ("list_downloads", {"offset": -1}),
                ("list_downloads", {"limit": 501}), ("list_downloads", {"limit": True}),
                ("list_downloads", {"status": "unknown"}), ("list_downloads", {"search": "unsupported"}),
                ("set_max_workers", {"workers": 7}), ("batch_delete_tasks", {"task_ids": []}),
                ("batch_retry_tasks", {"task_ids": ["x"] * 101}),
                ("delete_task", {"task_id": "100_2", "delete_file": "false"}),
            ):
                with self.subTest(tool=name, args=args):
                    result = await client.call_tool(name, args)
                    self.assertTrue(result.is_error)
            for operation in calls:
                operation.assert_not_called()

    async def test_read_only_mode_omits_mutations_and_rejects_write_calls(self):
        async with Client(create_server(self.service, read_only=True)) as client:
            tools = await client.list_tools()
            self.assertEqual({tool.name for tool in tools.tools}, {"get_status", "list_downloads", "check_task_file"})
            self.assert_success(await client.call_tool("get_status"))
            self.assertTrue((await client.call_tool("set_download_state", {"state": "paused"})).is_error)
            self.assertIs(download_stat.get_download_state(), download_stat.DownloadState.Downloading)

    async def test_shared_operations_need_neither_loopback_http_nor_flask_context(self):
        with patch("httpx2.Client.request", side_effect=AssertionError("MCP must not call HTTP")), \
                patch("httpx2.AsyncClient.request", side_effect=AssertionError("MCP must not call HTTP")), \
                patch.dict(web.get_flask_app().view_functions,
                           web_get_download_state=Mock(side_effect=AssertionError("No Flask dispatch"))):
            async with Client(self.server) as client:
                status = self.assert_success(await client.call_tool("get_status"))
                self.assertEqual(status["max_workers"], 2)
                self.assert_success(await client.call_tool("set_download_state", {"state": "paused"}))
        self.assertIs(download_stat.get_download_state(), download_stat.DownloadState.StopDownload)

    async def test_single_port_serves_web_login_static_api_and_real_mcp_protocol(self):
        headers = {"Authorization": "Bearer " + self.config.mcp_token}
        async with httpx2.AsyncClient(trust_env=False, timeout=5) as http:
            home = await http.get(self.url + "/")
            self.assertEqual(home.status_code, 302)
            self.assertEqual(home.headers["location"], "/login")
            login = await http.get(self.url + "/login")
            self.assertIn('type="password"', login.text)
            static = await http.get(self.url + "/static/manifest.json")
            self.assertEqual(static.status_code, 200)
            self.assertEqual(static.json()["name"], "TG Porter")
            self.assertEqual(static.json()["short_name"], "TGPorter")
            self.assertEqual((await http.get(self.url + "/get_download_state")).status_code, 401)
            self.assertEqual((await http.get(self.url + "/get_download_state", headers=headers)).status_code, 401)
            csrf = await self.login_browser(http)
            async with httpx2.AsyncClient(headers=headers, trust_env=False) as mcp_http, \
                    Client(streamable_http_client(self.url + "/mcp", http_client=mcp_http)) as client:
                self.assertEqual(len((await client.list_tools()).tools), 13)
                for state in ("active", "completed", "pending", "failed"):
                    result = self.assert_success(await client.call_tool("list_downloads", {"status": state}))
                    self.assertEqual(result["total"], 1)
                self.assert_success(await client.call_tool("set_download_state", {"state": "paused"}))
                self.assertEqual((await http.get(self.url + "/get_download_state")).text, "paused")
                response = await http.post(self.url + "/set_download_state?state=continue",
                                           headers={"X-CSRF-Token": csrf})
                self.assertEqual(response.status_code, 200)
                status = self.assert_success(await client.call_tool("get_status"))
                self.assertEqual(status["download_state"], "downloading")
                self.assertEqual(status["bot_status"]["status"], "disabled")

    async def test_unexpected_operation_errors_never_expose_credentials(self):
        with patch.object(self.service, "web_pause_task", side_effect=RuntimeError(self.config.mcp_token)):
            async with Client(self.server) as client:
                result = await client.call_tool("pause_task", {"task_id": "100_1"})
                self.assertTrue(result.is_error)
                self.assertIn("query task state", result.content[0].text)
                self.assertNotIn(self.config.mcp_token, result.content[0].text)

    async def test_http_token_host_origin_and_cookie_boundaries(self):
        async with httpx2.AsyncClient(trust_env=False, timeout=5) as http:
            for method in ("GET", "POST", "DELETE"):
                for headers in (
                    {}, {"Authorization": "Bearer wrong-token"}, {"Cookie": "session=unused"},
                    {"Authorization": "Bearer " + self.config.web_login_secret},
                    {"Authorization": "Bearer"}, {"Authorization": "Basic " + self.config.mcp_token},
                    {"Authorization": "Bearer " + self.config.mcp_token + " extra"},
                ):
                    response = await http.request(method, self.url + "/mcp", headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["cache-control"], "no-store")
            response = await http.post(self.url + "/mcp", params={"token": self.config.mcp_token},
                                       json={"mcp_token": self.config.mcp_token})
            self.assertEqual(response.status_code, 401)
            for extra in ({"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"}):
                response = await http.post(self.url + "/mcp", headers={
                    "Authorization": "Bearer " + self.config.mcp_token, **extra,
                }, json={})
                self.assertIn(response.status_code, (403, 421))
            public = await http.post(self.url + "/mcp", headers={
                "Authorization": "Bearer " + self.config.mcp_token,
                "Host": "nas.example.com", "Origin": "https://nas.example.com",
            }, json={})
            self.assertIn(public.status_code, (400, 406))
            lan = await http.post(self.url + "/mcp", headers={
                "Authorization": "Bearer " + self.config.mcp_token, "Host": "192.168.1.20:5000",
            }, json={})
            self.assertIn(lan.status_code, (400, 406))
            wrong_port = await http.post(self.url + "/mcp", headers={
                "Authorization": "Bearer " + self.config.mcp_token, "Host": "192.168.1.20:5001",
            }, json={})
            self.assertEqual(wrong_port.status_code, 421)
            # A real valid browser session still cannot authenticate MCP.
            await self.login_browser(http)
            self.assertEqual((await http.get(self.url + "/")).status_code, 200)
            self.assertEqual((await http.post(self.url + "/mcp", json={})).status_code, 401)
            self.assertEqual((await http.post(self.url + "/set_download_state?state=pause")).status_code, 400)

    async def test_empty_browser_password_does_not_make_mcp_public_and_rotation_revokes_token(self):
        self.config.web_login_secret = ""
        old = self.config.mcp_token
        async with httpx2.AsyncClient(trust_env=False, timeout=5) as http:
            self.assertEqual((await http.get(self.url + "/")).status_code, 200)
            self.assertEqual((await http.get(self.url + "/get_download_state")).status_code, 200)
            self.assertEqual((await http.post(self.url + "/mcp", json={})).status_code, 401)
            self.config.mcp_token = "replacement-token"
            self.assertEqual((await http.post(self.url + "/mcp", headers={
                "Authorization": "Bearer " + old,
            }, json={})).status_code, 401)
            self.config.mcp_token = ""
            self.assertEqual((await http.post(self.url + "/mcp", headers={
                "Authorization": "Bearer replacement-token",
            }, json={})).status_code, 401)

    async def test_empty_allowlists_keep_local_defaults_and_reject_remote_headers(self):
        self.runtime.should_exit = True
        await asyncio.to_thread(self.thread.join, 5)
        self.sock.close()
        self.config.mcp_allowed_hosts = []
        await self.start_listener()
        async with httpx2.AsyncClient(trust_env=False, headers={
            "Authorization": "Bearer " + self.config.mcp_token,
        }) as http:
            self.assertIn((await http.post(self.url + "/mcp", json={})).status_code, (400, 406))
            self.assertEqual((await http.post(self.url + "/mcp", json={}, headers={
                "Host": "nas.example.com",
            })).status_code, 421)
            self.assertEqual((await http.post(self.url + "/mcp", json={}, headers={
                "Origin": "https://nas.example.com",
            })).status_code, 403)

    async def test_disabled_mcp_keeps_webui_and_api_on_same_listener(self):
        self.runtime.should_exit = True
        await asyncio.to_thread(self.thread.join, 5)
        self.config.mcp_enabled = False
        self.sock.close()
        await self.start_listener()
        async with httpx2.AsyncClient(trust_env=False) as http:
            for method in ("GET", "POST", "DELETE"):
                self.assertEqual((await http.request(method, self.url + "/mcp")).status_code, 404)
            self.assertEqual((await http.get(self.url + "/login")).status_code, 200)
            self.assertEqual((await http.get(self.url + "/get_max_workers", headers={
                "Authorization": "Bearer " + self.config.mcp_token,
            })).status_code, 401)
            await self.login_browser(http)
            self.assertEqual((await http.get(self.url + "/get_max_workers")).json(), {"max_workers": 2})

    async def test_mcp_read_only_mode_does_not_restrict_authenticated_browser(self):
        self.runtime.should_exit = True
        await asyncio.to_thread(self.thread.join, 5)
        self.sock.close()
        self.config.mcp_read_only = True
        await self.start_listener()
        headers = {"Authorization": "Bearer " + self.config.mcp_token}
        async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=5) as http:
            csrf = await self.login_browser(http)
            async with Client(streamable_http_client(self.url + "/mcp", http_client=http)) as client:
                self.assertEqual({t.name for t in (await client.list_tools()).tools},
                                 {"get_status", "list_downloads", "check_task_file"})
                self.assertTrue((await client.call_tool("set_download_state", {"state": "paused"})).is_error)
                status = self.assert_success(await client.call_tool("get_status"))
                self.assertEqual(status["download_state"], "downloading")
                self.assertEqual(status["bot_status"]["status"], "disabled")
                self.assertEqual((await http.post(self.url + "/set_download_state?state=pause")).status_code, 400)
                response = await http.post(self.url + "/set_download_state?state=pause",
                                           headers={"X-CSRF-Token": csrf})
                self.assertEqual(response.status_code, 200)
                self.assertIs(download_stat.get_download_state(), download_stat.DownloadState.StopDownload)


class MCPConfigurationTests(unittest.TestCase):
    def configure(self, values):
        app = Application("unused-config.yaml", "unused-data.json")
        self.addCleanup(app.loop.close)
        app.assign_config({"api_id": "test", "api_hash": "test", "media_types": [], "file_formats": {}, **values})
        return app

    def test_disabled_by_default_and_token_required_when_enabled(self):
        app = self.configure({})
        self.assertFalse(app.mcp_enabled)
        self.assertFalse(app.mcp_read_only)
        self.assertEqual(app.mcp_token, "")
        self.assertEqual(app.mcp_allowed_hosts, [])
        with self.assertRaisesRegex(ValueError, "mcp_token"):
            self.configure({"mcp_enabled": True})
        self.assertTrue(self.configure({"mcp_enabled": True, "mcp_token": "token"}).mcp_enabled)
        with self.assertRaisesRegex(ValueError, "mcp_token"):
            web.create_web_app(SimpleNamespace(mcp_enabled=True, mcp_token=""))

    def test_token_validation_and_disabled_values(self):
        for value, expected in (("agent-token", "agent-token"), (None, ""), ("", "")):
            with self.subTest(value=value):
                self.assertEqual(self.configure({"mcp_token": value}).mcp_token, expected)
        for value in (123, False, [], {}, "has space", "line\nbreak", "trailing ", " leading", "中文", "null\x00byte", "del\x7f"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "mcp_token"):
                self.configure({"mcp_token": value})

    def test_invalid_config_is_rejected_before_listener_starts(self):
        for values in ({"mcp_enabled": "false"}, {"mcp_read_only": 1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.configure(values)

    def test_allowlist_validation_rejects_urls_paths_wildcards_and_bad_types(self):
        invalid_hosts = [None, "nas.example.com", [123], [""], ["*"], ["nas.example.com:*"],
                         ["https://nas.example.com"], ["nas.example.com/mcp"], ["user:pass@nas.example.com"],
                         ["nas.example.com?token=x"], ["nas.example.com#x"], ["nas.example.com:bad"],
                         ["nas.example.com:65536"], ["nas.example.com:"], ["nas.example.com:0"], ["has space"]]
        for value in invalid_hosts:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.configure({"mcp_allowed_hosts": value})

    def test_host_allowlist_normalizes_case_deduplicates_and_accepts_ipv6(self):
        app = self.configure({
            "mcp_allowed_hosts": ["NAS.EXAMPLE.COM", "nas.example.com", "192.168.1.20:5000", "[::1]:5000"],
        })
        self.assertEqual(app.mcp_allowed_hosts, ["nas.example.com", "192.168.1.20:5000", "[::1]:5000"])


if __name__ == "__main__":
    unittest.main()
