from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FakeAPIHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler API
        payloads = {
            "/health": {"status": "ok"},
            "/v1/models": {"data": [{"id": "hermes-agent"}]},
            "/v1/capabilities": {"features": {"run_submission": True, "run_status": True}},
        }
        payload = payloads.get(self.path)
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):  # noqa: A002 - stdlib handler API
        return


class StdioEntrypointTests(unittest.TestCase):
    def test_stdio_entrypoint_discovers_tools_and_calls_health_without_llm(self):
        api = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPIHandler)
        thread = threading.Thread(target=api.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(api.shutdown)
        self.addCleanup(api.server_close)

        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            env["TEST_BRIDGE_KEY"] = "test-api-key"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hermes_zcode_bridge.server",
                    "--api-url",
                    f"http://127.0.0.1:{api.server_port}",
                    "--api-key-env",
                    "TEST_BRIDGE_KEY",
                    "--state-db",
                    str(Path(tmp) / "bridge.db"),
                ],
                cwd=Path(__file__).parents[1],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(self._terminate, process)

            self.assertIsNotNone(process.stdin)
            self.assertIsNotNone(process.stdout)
            self._send(process, {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "contract-test", "version": "1"},
                },
            })
            initialized = self._read_response(process)
            self.assertEqual(initialized["id"], 1)
            self.assertIn("serverInfo", initialized["result"])

            self._send(process, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            self._send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            listed = self._read_response(process)
            names = {tool["name"] for tool in listed["result"]["tools"]}
            self.assertIn("run_start", names)
            self.assertIn("run_wait", names)
            self.assertIn("bridge_health", names)
            self.assertNotIn("shell_exec", names)

            self._send(process, {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "bridge_health", "arguments": {}},
            })
            called = self._read_response(process)
            self.assertEqual(called["id"], 3)
            content = called["result"]["content"]
            payload = json.loads(content[0]["text"])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "healthy")
            self.assertEqual(payload["models"]["data"][0]["id"], "hermes-agent")
            process.stdin.close()
            process.wait(timeout=5)
            stderr = process.stderr.read() if process.stderr else ""
            self.assertNotIn("test-api-key", stderr)

    @staticmethod
    def _send(process, message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    @staticmethod
    def _read_response(process):
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        events = selector.select(timeout=5)
        if not events:
            raise AssertionError("MCP server did not answer within 5 seconds")
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr else ""
            raise AssertionError(f"MCP server exited before response: {stderr}")
        return json.loads(line)

    @staticmethod
    def _terminate(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


if __name__ == "__main__":
    unittest.main()
