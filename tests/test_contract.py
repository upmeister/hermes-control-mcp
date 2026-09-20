from __future__ import annotations

import json
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from hermes_zcode_bridge.api import APIError, APIResponse, HermesAPIClient
from hermes_zcode_bridge.config import BridgeConfig, ConfigError
from hermes_zcode_bridge.registry import REGISTRY_SCHEMA_VERSION, RegistryError, StateRegistry
from hermes_zcode_bridge.service import BridgeService


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": dict(headers),
            "body": body,
            "timeout": timeout,
        })
        return self.handler(method, url, headers, body, timeout)


def response(status: int, payload: object, headers: dict[str, str] | None = None) -> APIResponse:
    return APIResponse(
        status=status,
        headers=headers or {"Content-Type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )


class BridgeContractTests(unittest.TestCase):
    def make_service(self, handler):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = BridgeConfig(
            api_url="http://bridge.test:8642",
            api_key="test-api-key",
            state_db=Path(tmp.name) / "state.db",
            request_timeout=3,
        )
        transport = FakeTransport(handler)
        registry = StateRegistry(config.state_db)
        self.addCleanup(registry.close)
        client = HermesAPIClient(config, transport=transport)
        return BridgeService(client, registry), transport, registry

    def test_lane_mapping_survives_registry_reopen_without_prompt_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            registry = StateRegistry(path)
            registry.bind_lane("code/review", "session-exact-1")
            registry.save_request(
                request_id="req-1",
                lane="code/review",
                session_id="session-exact-1",
                run_id="run-1",
                idempotency_key="bridge:req-1",
                fingerprint="fp",
                status="queued",
            )
            registry.close()

            reopened = StateRegistry(path)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.session_for_lane("code/review"), "session-exact-1")
            saved = reopened.request_by_id("req-1")
            self.assertEqual(saved["run_id"], "run-1")
            self.assertNotIn("prompt", saved)

    def test_registry_promotes_legacy_user_version_zero_to_public_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            raw = sqlite3.connect(path)
            raw.execute("PRAGMA user_version=0")
            raw.close()

            registry = StateRegistry(path)
            self.addCleanup(registry.close)
            self.assertEqual(registry.schema_version, REGISTRY_SCHEMA_VERSION)

            verify = sqlite3.connect(path)
            self.addCleanup(verify.close)
            version = int(verify.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(version, REGISTRY_SCHEMA_VERSION)

    def test_registry_refuses_future_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            raw = sqlite3.connect(path)
            raw.execute(f"PRAGMA user_version={REGISTRY_SCHEMA_VERSION + 1}")
            raw.commit()
            raw.close()

            with self.assertRaisesRegex(RegistryError, "newer than supported"):
                StateRegistry(path)

    def test_start_sends_explicit_session_and_structured_result(self):
        def handler(method, url, headers, body, timeout):
            self.assertEqual(method, "POST")
            self.assertEqual(urlparse(url).path, "/v1/runs")
            self.assertEqual(json.loads(body), {
                "input": "inspect the diff",
                "session_id": "sid-1",
                "model": "model-exact",
                "provider": "custom:inference",
            })
            self.assertEqual(headers["Authorization"], "Bearer test-api-key")
            self.assertEqual(headers["Idempotency-Key"], "bridge:req-1")
            return response(202, {"run_id": "run-1", "status": "started", "replayed": False})

        service, transport, registry = self.make_service(handler)
        result = service.start(
            lane="code/review",
            prompt="inspect the diff",
            session_id="sid-1",
            model="model-exact",
            provider="custom:inference",
            request_id="req-1",
        )

        self.assertEqual(result["request_id"], "req-1")
        self.assertEqual(result["run_id"], "run-1")
        self.assertEqual(result["session_id"], "sid-1")
        self.assertEqual(result["status"], "started")
        self.assertIsNone(result["error_code"])
        self.assertEqual(registry.session_for_lane("code/review"), "sid-1")
        self.assertEqual(len(transport.calls), 1)

    def test_multiline_prompt_preserves_newlines_and_tabs(self):
        prompt = "first line\n\tsecond line\r\nthird line"

        def handler(method, url, headers, body, timeout):
            self.assertEqual(json.loads(body)["input"], prompt)
            return response(202, {"run_id": "run-multiline", "status": "started", "replayed": False})

        service, _, _ = self.make_service(handler)
        result = service.start(
            lane="lane", prompt=prompt, session_id="sid-multiline", request_id="req-multiline"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["run_id"], "run-multiline")

    def test_same_request_id_is_local_replay_without_second_post(self):
        calls = []

        def handler(method, url, headers, body, timeout):
            calls.append((method, url))
            return response(202, {"run_id": "run-1", "status": "started", "replayed": False})

        service, transport, _ = self.make_service(handler)
        first = service.start(lane="lane", prompt="same", request_id="req-1")
        second = service.start(lane="lane", prompt="same", request_id="req-1")

        self.assertEqual(first["run_id"], second["run_id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(transport.calls[0]["headers"]["Idempotency-Key"], "bridge:req-1")

    def test_uncertain_submit_can_only_reconcile_with_same_key(self):
        attempts = []

        def handler(method, url, headers, body, timeout):
            attempts.append(headers["Idempotency-Key"])
            if len(attempts) == 1:
                raise TimeoutError("accepted status is unknown")
            return response(202, {"run_id": "run-recovered", "status": "started", "replayed": True})

        service, _, registry = self.make_service(handler)
        uncertain = service.start(lane="lane", prompt="same", request_id="req-1")
        recovered = service.start(lane="lane", prompt="same", request_id="req-1")

        self.assertEqual(uncertain["status"], "unknown")
        self.assertEqual(uncertain["error_code"], "transport_unknown")
        self.assertEqual(recovered["run_id"], "run-recovered")
        self.assertEqual(attempts, ["bridge:req-1", "bridge:req-1"])
        self.assertEqual(registry.request_by_id("req-1")["status"], "started")

    def test_different_session_cannot_silently_rebind_lane(self):
        def handler(method, url, headers, body, timeout):
            return response(202, {"run_id": "run-1", "status": "started", "replayed": False})

        service, transport, registry = self.make_service(handler)
        service.start(lane="lane", prompt="first", session_id="sid-1", request_id="req-1")
        result = service.start(lane="lane", prompt="second", session_id="sid-2", request_id="req-2")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "lane_session_conflict")
        self.assertEqual(registry.session_for_lane("lane"), "sid-1")
        self.assertEqual(len(transport.calls), 1)

    def test_status_updates_session_mapping_and_answer(self):
        def handler(method, url, headers, body, timeout):
            path = urlparse(url).path
            if method == "POST":
                return response(202, {"run_id": "run-1", "status": "started", "replayed": False})
            self.assertEqual(path, "/v1/runs/run-1")
            return response(200, {
                "object": "hermes.run",
                "run_id": "run-1",
                "session_id": "sid-from-server",
                "status": "completed",
                "output": "done",
            })

        service, _, registry = self.make_service(handler)
        service.start(lane="lane", prompt="work", request_id="req-1")
        result = service.status(lane="lane", run_id="run-1")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["answer"], "done")
        self.assertEqual(result["session_id"], "sid-from-server")
        self.assertEqual(registry.session_for_lane("lane"), "sid-from-server")

    def test_stop_and_steer_use_exact_run_id(self):
        seen = []

        def handler(method, url, headers, body, timeout):
            path = urlparse(url).path
            seen.append((method, path, json.loads(body) if body else None))
            if path == "/v1/runs":
                return response(202, {"run_id": "run-exact", "status": "started", "replayed": False})
            if path.endswith("/steer"):
                return response(200, {"run_id": "run-exact", "accepted": True})
            if path.endswith("/stop"):
                return response(200, {"run_id": "run-exact", "status": "stopping"})
            raise AssertionError(path)

        service, _, _ = self.make_service(handler)
        service.start(lane="lane", prompt="work", request_id="req-1")
        service.steer(run_id="run-exact", text="focus on tests")
        service.stop(run_id="run-exact")

        self.assertEqual(seen[1], ("POST", "/v1/runs/run-exact/steer", {"input": "focus on tests"}))
        self.assertEqual(seen[2], ("POST", "/v1/runs/run-exact/stop", {}))

    def test_wait_returns_terminal_answer_after_status_transition(self):
        status_calls = 0

        def handler(method, url, headers, body, timeout):
            nonlocal status_calls
            path = urlparse(url).path
            if path == "/v1/runs":
                return response(202, {"run_id": "run-wait", "status": "started", "replayed": False})
            status_calls += 1
            if status_calls == 1:
                return response(200, {"run_id": "run-wait", "session_id": "sid-wait", "status": "running"})
            return response(200, {
                "object": "hermes.run", "run_id": "run-wait", "session_id": "sid-wait",
                "status": "completed", "output": "finished",
            })

        service, _, _ = self.make_service(handler)
        service.start(lane="lane", prompt="work", session_id="sid-wait", request_id="req-wait")
        result = service.wait(run_id="run-wait", timeout_seconds=1, poll_interval_seconds=0.01)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["answer"], "finished")
        self.assertEqual(status_calls, 2)

    def test_events_parses_sse_data_frames_for_exact_run(self):
        sse = (
            b"event: message.delta\ndata: {\"event\":\"message.delta\",\"delta\":\"hi\"}\n\n"
            b"event: run.completed\ndata: {\"event\":\"run.completed\",\"run_id\":\"run-events\",\"status\":\"completed\"}\n\n"
        )

        def handler(method, url, headers, body, timeout):
            path = urlparse(url).path
            if path == "/v1/runs":
                return response(202, {"run_id": "run-events", "status": "started", "replayed": False})
            return APIResponse(200, {"Content-Type": "text/event-stream"}, sse)

        service, _, _ = self.make_service(handler)
        service.start(lane="lane", prompt="work", session_id="sid-events", request_id="req-events")
        result = service.events(run_id="run-events")

        self.assertEqual(result["event_count"], 2)
        self.assertEqual(result["events"][0]["delta"], "hi")
        self.assertEqual(result["status"], "completed")

    def test_client_reads_key_from_server_env_file_without_exposing_it_to_request_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text('TEST_FILE_KEY="file-secret"\n', encoding="utf-8")
            config = BridgeConfig(
                api_url="http://bridge.test", env_file=env_file, api_key_env="TEST_FILE_KEY",
                state_db=Path(tmp) / "state.db"
            )
            captured = {}

            def handler(method, url, headers, body, timeout):
                captured.update(headers)
                return response(200, {"status": "ok"})

            client = HermesAPIClient(config, transport=FakeTransport(handler))
            client.get("/health")
            self.assertEqual(captured["Authorization"], "Bearer file-secret")
            self.assertNotIn("file-secret", config.api_url)

    def test_history_uses_oldest_order_and_structured_messages(self):
        def handler(method, url, headers, body, timeout):
            self.assertEqual(method, "GET")
            parsed = urlparse(url)
            self.assertEqual(parsed.path, "/api/sessions/sid-1/messages")
            self.assertEqual(parsed.query, "order=oldest&limit=20")
            return response(200, {"session_id": "sid-1", "data": [{"role": "user", "content": "hi"}]})

        service, _, _ = self.make_service(handler)
        result = service.history(session_id="sid-1", limit=20)
        self.assertEqual(result["session_id"], "sid-1")
        self.assertEqual(result["messages"][0]["content"], "hi")

    def test_http_error_redacts_api_key_and_preserves_error_code(self):
        def handler(method, url, headers, body, timeout):
            return response(401, {"error": {"code": "invalid_api_key", "message": "Bearer test-api-key rejected"}})

        config = BridgeConfig(api_url="http://bridge.test", api_key="test-api-key")
        client = HermesAPIClient(config, transport=FakeTransport(handler))
        with self.assertRaises(APIError) as caught:
            client.get("/health")
        self.assertEqual(caught.exception.code, "invalid_api_key")
        self.assertNotIn("test-api-key", str(caught.exception))

    def test_non_json_http_error_preserves_status_for_waf_or_proxy_diagnostics(self):
        def handler(method, url, headers, body, timeout):
            return APIResponse(503, {"Content-Type": "text/html"}, b"<html>upstream unavailable</html>")

        config = BridgeConfig(api_url="http://bridge.test", api_key="test-api-key")
        client = HermesAPIClient(config, transport=FakeTransport(handler))
        with self.assertRaises(APIError) as caught:
            client.get("/health")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, "http_503")
        self.assertNotIn("test-api-key", str(caught.exception))


class ConfigContractTests(unittest.TestCase):
    def test_live_access_token_resolves_from_the_server_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text('HERMES_DASHBOARD_ACCESS_TOKEN="access-token-value"\n', encoding="utf-8")
            config = BridgeConfig(api_key="api", env_file=env_file)

            self.assertEqual(config.resolved_gateway_access_token(), "access-token-value")
            self.assertEqual(config.live_auth_mode(), "access_token")

    def test_gateway_url_rejects_credentials_in_query(self):
        with self.assertRaises(ConfigError):
            BridgeConfig(api_key="api", gateway_url="wss://gateway.test/api/ws?token=secret")

    def test_owner_lease_is_a_distinct_local_auth_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                api_key="api",
                gateway_owner_lease_path=Path(tmp) / "owner_adapter.json",
            )

        self.assertEqual(config.live_auth_mode(), "owner_adapter")


class MCPContractTests(unittest.TestCase):
    def test_mcp_surface_is_allowlisted(self):
        from hermes_zcode_bridge.mcp_server import create_server

        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(api_url="http://bridge.test", api_key="x", state_db=Path(tmp) / "state.db")
            registry = StateRegistry(config.state_db)
            self.addCleanup(registry.close)
            service = BridgeService(HermesAPIClient(config, transport=FakeTransport(lambda *args: response(200, {}))), registry)
            server = create_server(service)
            names = {tool.name for tool in asyncio.run(server.list_tools())}

        self.assertIn("run_start", names)
        self.assertIn("run_wait", names)
        self.assertIn("run_status", names)
        self.assertIn("run_stop", names)
        self.assertIn("run_steer", names)
        self.assertIn("session_history", names)
        self.assertIn("bridge_health", names)
        for name in {
            "live_session_open", "live_prompt", "live_wait", "live_events",
            "live_status", "live_history", "live_steer", "live_interrupt",
            "live_reconcile", "live_reconnect", "live_health",
        }:
            self.assertIn(name, names)
        self.assertNotIn("shell_exec", names)
        self.assertNotIn("cli_exec", names)

    def test_live_session_id_descriptions_separate_stored_and_runtime_namespaces(self):
        from hermes_zcode_bridge.mcp_server import create_server

        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(api_url="http://bridge.test", api_key="x", state_db=Path(tmp) / "state.db")
            registry = StateRegistry(config.state_db)
            self.addCleanup(registry.close)
            service = BridgeService(
                HermesAPIClient(config, transport=FakeTransport(lambda *args: response(200, {}))),
                registry,
            )
            server = create_server(service)
            tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

        open_description = (tools["live_session_open"].description or "").lower()
        self.assertIn("session_id is the stored/durable id", open_description)
        self.assertIn("response session_id is the runtime id", open_description)
        self.assertIn("prefer lane", open_description)

        for name in {
            "live_prompt",
            "live_events",
            "live_status",
            "live_history",
            "live_steer",
            "live_interrupt",
        }:
            description = (tools[name].description or "").lower()
            self.assertIn("explicitly, session_id is the runtime id", description, name)
            self.assertIn("prefer lane", description, name)


if __name__ == "__main__":
    unittest.main()
