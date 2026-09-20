#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import selectors
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FakeAPIHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        payloads = {
            "/health": {"status": "ok", "version": "installed-smoke"},
            "/v1/models": {"data": [{"id": "hermes-agent"}]},
            "/v1/capabilities": {
                "features": {"run_submission": True, "run_status": True}
            },
        }
        payload = payloads.get(self.path)
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args):  # noqa: A002
        return


def send(process: subprocess.Popen[str], message: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def read_response(process: subprocess.Popen[str]) -> dict:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        events = selector.select(timeout=10)
    finally:
        selector.close()
    if not events:
        raise RuntimeError("installed MCP entrypoint did not answer within 10 seconds")
    line = process.stdout.readline()
    if not line:
        stderr = process.stderr.read() if process.stderr else ""
        raise RuntimeError(f"installed MCP entrypoint exited early: {stderr}")
    return json.loads(line)


def terminate(process: subprocess.Popen[str]) -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entrypoint", required=True)
    args = parser.parse_args()

    api = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPIHandler)
    thread = threading.Thread(target=api.serve_forever, daemon=True)
    thread.start()

    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["INSTALLED_SMOKE_API_KEY"] = "installed-smoke-secret"
            process = subprocess.Popen(
                [
                    args.entrypoint,
                    "--api-url",
                    f"http://127.0.0.1:{api.server_port}",
                    "--api-key-env",
                    "INSTALLED_SMOKE_API_KEY",
                    "--state-db",
                    str(Path(tmp) / "bridge.db"),
                ],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                send(process, {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "installed-smoke", "version": "1"},
                    },
                })
                initialized = read_response(process)
                assert initialized["id"] == 1
                assert "serverInfo" in initialized["result"]

                send(process, {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                })
                send(process, {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "bridge_health", "arguments": {}},
                })
                health = read_response(process)
                payload = json.loads(health["result"]["content"][0]["text"])
                assert payload["ok"] is True
                assert payload["status"] == "healthy"
                assert payload["models"]["data"][0]["id"] == "hermes-agent"

                send(process, {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "live_health", "arguments": {}},
                })
                live = read_response(process)
                live_payload = json.loads(live["result"]["content"][0]["text"])
                assert live_payload["status"] == "unconfigured"
                assert live_payload["error_code"] == "gateway_not_configured"

                assert process.stdin is not None
                process.stdin.close()
                process.wait(timeout=5)
                stderr = process.stderr.read() if process.stderr else ""
                assert "installed-smoke-secret" not in stderr
            finally:
                terminate(process)
    finally:
        api.shutdown()
        api.server_close()

    print("installed artifact MCP stdio smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
