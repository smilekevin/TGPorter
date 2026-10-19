"""Start the real entry point without YAML, credentials, sessions or stdin."""

import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def test_real_entrypoint_serves_setup_and_shuts_down_without_telegram_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            (root / "data").mkdir()
            # Only select an isolated listener; there are no API credentials or sessions.
            (root / "data/settings.json").write_text(json.dumps({"web_host": "127.0.0.1", "web_port": port}))
            with (root / "startup.log").open("w+") as output:
                process = subprocess.Popen([sys.executable, "-B", str(ROOT / "media_downloader.py")],
                    cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                    env={**os.environ, "PYTHONPATH": str(ROOT)})
                try:
                    deadline = time.monotonic() + 15
                    response = None
                    while time.monotonic() < deadline:
                        try:
                            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                            connection.request("GET", "/configuration")
                            response = connection.getresponse()
                            html = response.read().decode()
                            connection.close()
                            break
                        except OSError:
                            if process.poll() is not None:
                                break
                            time.sleep(0.05)
                    self.assertIsNone(process.poll(), (root / "startup.log").read_text())
                    self.assertIsNotNone(response)
                    self.assertEqual(response.status, 200)
                    self.assertIn("setup-form", html)
                    self.assertIn("configuration.js", html)
                    code = re.search(r"First-time setup code: (\S+)", (root / "data/log/tdl.log").read_text())[1]
                    self.assertNotIn(code, html)
                    self.assertFalse((root / "config.yaml").exists())
                    self.assertEqual(list((root / "data/sessions").iterdir()), [])
                    self.assertTrue((root / "data/data.json").is_file())
                finally:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                self.assertEqual(process.returncode, 0, (root / "startup.log").read_text())
