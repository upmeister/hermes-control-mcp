from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from hermes_zcode_bridge.config import BridgeConfig
from hermes_zcode_bridge.live_client import LiveError, LiveGatewayClient, LiveTransportUnknown
from hermes_zcode_bridge.local_attach import process_start_marker
from hermes_zcode_bridge.live_service import LiveService
from hermes_zcode_bridge.registry import StateRegistry


_CLOSE = object()


class FakeSocket:
    def __init__(self, handler):
        self.handler = handler
        self.loop = asyncio.get_running_loop()
        self.incoming: asyncio.Queue[object] = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    def push(self, frame: dict) -> None:
        self.loop.call_soon_threadsafe(self.incoming.put_nowait, json.dumps(frame, ensure_ascii=False))

    def disconnect(self) -> None:
        self.loop.call_soon_threadsafe(self.incoming.put_nowait, _CLOSE)

    async def recv(self):
        value = await self.incoming.get()
        if value is _CLOSE:
            raise ConnectionError("fake socket disconnected")
        return value

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.sent.append(frame)
        await self.handler(self, frame)

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.disconnect()


class FakeGateway:
    """Scripted stand-in for the deployed TUI gateway's client-visible contract.

    Models the source-verified session state the bridge relies on: a running
    flag, the inflight turn's stripped user text (exposed by session.activate),
    per-session event sequence numbers, and durable history rows with row_id.
    """

    def __init__(self):
        self.sockets: list[FakeSocket] = []
        self.connect_kwargs: list[dict] = []
        self.connect_urls: list[str] = []
        self.epoch = "epoch-1"
        self.prompt_calls = 0
        self.replay_calls = 0
        self.session_counter = 0
        self.mode = "normal"
        self.complete_delay: float | None = 0.15
        self.inflight_enabled = True
        self.history_rows: list[dict] = []
        self.turns: dict[str, dict] = {}

    def turn_state(self, session_id: str) -> dict:
        return self.turns.setdefault(session_id, {"running": False, "inflight_user": None, "seq": 0})

    def emit_event(self, socket: FakeSocket, session_id: str, kind: str, payload: dict | None = None) -> int:
        state = self.turn_state(session_id)
        state["seq"] += 1
        params: dict = {"type": kind, "session_id": session_id, "seq": state["seq"]}
        if payload is not None:
            params["payload"] = payload
        socket.push({"jsonrpc": "2.0", "method": "event", "params": params})
        return state["seq"]

    def inject_foreign_turn(self, socket: FakeSocket, session_id: str, text: str = "foreign-answer") -> None:
        self.emit_event(socket, session_id, "message.start")
        self.emit_event(socket, session_id, "message.complete", {"status": "complete", "text": text})

    def _complete_turn(self, socket: FakeSocket, session_id: str) -> None:
        # Source-faithful deployed ordering (prompt_turn._complete_turn_payload
        # -> _run_after_agent_ready): the gateway clears the inflight snapshot
        # BEFORE emitting message.complete and clears running=False later, so a
        # completion poll can legitimately observe running=true, inflight=None.
        state = self.turn_state(session_id)
        state["inflight_user"] = None
        self.history_rows.append({"role": "assistant", "text": "done", "row_id": len(self.history_rows) + 1})
        self.emit_event(socket, session_id, "message.complete", {"status": "complete", "text": "done"})
        asyncio.get_running_loop().call_later(0.05, self._end_turn, session_id)

    def _end_turn(self, session_id: str) -> None:
        self.turn_state(session_id)["running"] = False

    async def connect(self, url: str, **kwargs):
        self.connect_urls.append(url)
        self.connect_kwargs.append(kwargs)
        async def handler(socket: FakeSocket, frame: dict):
            method = frame.get("method")
            rid = frame.get("id")
            if method == "gateway.ping":
                socket.push({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})
            elif method == "session.create":
                self.session_counter += 1
                socket.push({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {
                        "session_id": f"runtime-{self.session_counter}",
                        "stored_session_id": "stored-1",
                        "message_count": 0,
                        "messages": [],
                    },
                })
            elif method == "session.resume":
                socket.push({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {
                        "session_id": "runtime-resumed",
                        "stored_session_id": frame["params"]["session_id"],
                        "message_count": 0,
                        "messages": [],
                    },
                })
            elif method == "session.activate":
                result = {
                    "session_id": frame["params"]["session_id"],
                    "stored_session_id": "stored-1",
                    "message_count": 0,
                    "messages": [],
                }
                if self.inflight_enabled:
                    state = self.turn_state(frame["params"]["session_id"])
                    result["running"] = state["running"]
                    if state["running"]:
                        result["inflight"] = {
                            "user": state["inflight_user"] or "", "assistant": "", "streaming": True,
                        }
                socket.push({"jsonrpc": "2.0", "id": rid, "result": result})
            elif method == "session.events.since":
                self.replay_calls += 1
                if self.mode == "race":
                    socket.push({
                        "jsonrpc": "2.0", "method": "event",
                        "params": {"type": "message.complete", "session_id": "runtime-1", "seq": 4,
                                   "payload": {"text": "live"}},
                    })
                    socket.push({
                        "jsonrpc": "2.0", "id": rid,
 "result": {
     "session_id": frame["params"]["session_id"],
     "events": [{"type": "message.delta", "session_id": "runtime-1", "seq": 3,
                 "payload": {"text": "gap"}}],
                            "latest_seq": 4,
                            "truncated": False,
                            "epoch": self.epoch,
                            "count": 1,
                            "open_requests": [],
                        },
                    })
                else:
                    socket.push({
                        "jsonrpc": "2.0", "id": rid,
                        "result": {"session_id": frame["params"]["session_id"], "events": [],
                                   "latest_seq": 1, "truncated": False, "epoch": self.epoch,
                                   "count": 0, "open_requests": []},
                    })
            elif method == "prompt.submit":
                self.prompt_calls += 1
                if self.mode == "unknown":
                    await socket.close()
                    return
                if self.mode == "busy":
                    socket.push({"jsonrpc": "2.0", "id": rid, "result": {"status": "queued"}})
                    return
                session_id = frame["params"]["session_id"]
                state = self.turn_state(session_id)
                state["running"] = True
                state["inflight_user"] = frame["params"]["text"]
                socket.push({"jsonrpc": "2.0", "id": rid, "result": {"status": "streaming"}})
                self.emit_event(socket, session_id, "message.start")
                if self.complete_delay is not None:
                    asyncio.get_running_loop().call_later(
                        self.complete_delay, self._complete_turn, socket, session_id)
            elif method == "session.status":
                socket.push({"jsonrpc": "2.0", "id": rid, "result": {"output": "Hermes TUI Status"}})
            elif method == "session.history":
                messages = [dict(row) for row in self.history_rows]
                socket.push({"jsonrpc": "2.0", "id": rid, "result": {"count": len(messages), "messages": messages}})
            elif method == "session.steer":
                socket.push({"jsonrpc": "2.0", "id": rid, "result": {"status": "queued"}})
            elif method == "session.interrupt":
                socket.push({"jsonrpc": "2.0", "id": rid, "result": {"status": "interrupted"}})
            else:
                socket.push({"jsonrpc": "2.0", "id": rid, "result": {}})

        socket = FakeSocket(handler)
        self.sockets.append(socket)
        socket.push({
            "jsonrpc": "2.0", "method": "event",
            "params": {"type": "gateway.ready", "payload": {
                "heartbeat": False, "replay_epoch": self.epoch,
            }},
        })
        return socket


class LiveClientTests(unittest.TestCase):
    def make_config(self, gateway: FakeGateway, **overrides):
        values = {
            "api_key": "api-test",
            "gateway_url": "ws://gateway.test/api/ws",
            "gateway_token": "gateway-test-token",
            "gateway_connect_timeout": 1.0,
            "gateway_request_timeout": 1.0,
            "gateway_heartbeat_interval": 0.0,
            "gateway_event_buffer_max": 8,
            "gateway_event_buffer_bytes": 100_000,
        }
        values.update(overrides)
        return BridgeConfig(**values), gateway

    def wait_until(self, predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.assertTrue(predicate())

    def test_connect_and_request_use_one_persistent_socket(self):
        gateway = FakeGateway()
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=gateway.connect)
        self.addCleanup(client.shutdown)

        client.connect()
        result = client.request("session.create", {"source": "tool"})

        self.assertEqual(result["stored_session_id"], "stored-1")
        self.assertEqual(len(gateway.sockets), 1)
        query = parse_qs(urlsplit(client.last_connect_url).query)
        self.assertEqual(query["token"], ["gateway-test-token"])
        self.assertNotIn("subprotocols", gateway.connect_kwargs[0])
        capabilities = [frame for frame in gateway.sockets[0].sent if frame.get("method") == "client.capabilities"]
        self.assertEqual(len(capabilities), 1)
        self.assertEqual(capabilities[0]["params"], {"server_requests": False})

    def test_gateway_ready_without_replay_epoch_is_a_protocol_error(self):
        gateway = FakeGateway()
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=gateway.connect)
        client._ready_event = asyncio.Event()

        with self.assertRaisesRegex(LiveError, "replay_epoch") as caught:
            client._handle_frame({
                "jsonrpc": "2.0", "method": "event",
                "params": {"type": "gateway.ready", "payload": {}},
            })

        self.assertEqual(caught.exception.code, "protocol_violation")
        self.assertFalse(client._ready_event.is_set())

    def test_invalid_json_rpc_envelope_is_fail_closed(self):
        gateway = FakeGateway()
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=gateway.connect)

        for raw in (
            '{"jsonrpc":"2.0","id":"z1","result":{},"result":{}}',
            '{"jsonrpc":"2.0","id":"z1","result":{},"error":null}',
            '{"jsonrpc":"2.0","id":"z1","result":{"value":1e999}}',
        ):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(LiveError, "protocol"):
                    client._handle_frame(raw)

    def test_unexpected_server_request_gets_json_rpc_method_not_found(self):
        gateway = FakeGateway()
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=gateway.connect)

        reply = client._handle_frame({
            "jsonrpc": "2.0", "id": "srq-test", "method": "approval",
            "params": {"session_id": "runtime-1"},
        })

        self.assertEqual(reply, {
            "jsonrpc": "2.0", "id": "srq-test",
            "error": {"code": -32601, "message": "server requests are not supported"},
        })

    def test_replay_validation_rejects_cross_session_gaps_and_truthy_truncated(self):
        gateway = FakeGateway()
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=gateway.connect)
        client._replay_epoch = "epoch-1"
        client._watermarks["session-a"] = 4
        base = {
            "session_id": "session-a",
            "events": [],
            "latest_seq": 4,
            "truncated": False,
            "count": 0,
            "epoch": "epoch-1",
            "open_requests": [],
        }
        cases = [
            {**base, "events": [{"type": "message.delta", "session_id": "session-b", "seq": 5}], "latest_seq": 5, "count": 1},
            {**base, "events": [{"type": "message.delta", "session_id": "session-a", "seq": 6}], "latest_seq": 6, "count": 1},
            {**base, "truncated": "false"},
        ]
        for response in cases:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    client._validate_replay_result("session-a", 4, response, [])
                self.assertEqual(client.watermarks(), {"session-a": 4})

    def test_duplicate_response_id_is_a_protocol_violation(self):
        gateway = FakeGateway()
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=gateway.connect)
        self.addCleanup(client.shutdown)
        client.connect()
        client.request("gateway.ping", {})
        response_id = client._retired_request_ids[-1]
        with self.assertRaisesRegex(LiveError, "not pending"):
            client._handle_frame({"jsonrpc": "2.0", "id": response_id, "result": {"ok": True}})

    def test_close_drains_connection_task_after_outer_cancellation(self):
        gateway = FakeGateway()
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=gateway.connect)
        self.addCleanup(client.shutdown)
        client.connect()

        async def cancel_disconnect():
            task = asyncio.create_task(client._disconnect_async())
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return client._connection_task

        connection_task = client._run(cancel_disconnect(), timeout=3.0)
        self.assertIsNotNone(connection_task)
        self.assertTrue(connection_task.done())

    def test_owner_global_event_with_empty_session_id_does_not_break_handshake(self):
        gateway = FakeGateway()
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=gateway.connect)
        client._handle_frame({
            "jsonrpc": "2.0", "method": "event",
            "params": {"type": "setup.ready", "session_id": "", "payload": {"provider_configured": True}},
        })
        self.assertEqual(client.watermarks(), {})

    def test_owner_health_is_configured_without_gateway_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            lease = Path(tmp) / "owner.json"
            gateway = FakeGateway()
            config = BridgeConfig(
                api_key="api-test", gateway_owner_lease_path=lease,
                gateway_connect_timeout=1.0, gateway_request_timeout=1.0,
                gateway_heartbeat_interval=0.0,
            )
            client = LiveGatewayClient(config, connector=gateway.connect)
            self.addCleanup(client.shutdown)
            service = LiveService(client, StateRegistry(Path(tmp) / "state.db"))
            self.addCleanup(service.registry.close)
            with patch.object(client, "connect", return_value=None):
                result = service.health()
            self.assertEqual(result["status"], "healthy")

    def test_owner_attach_uses_unix_connect_and_never_injected_tcp_connector(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "owner"
            runtime.mkdir(mode=0o700)
            socket_path = runtime / "owner_adapter.sock"
            lease_path = runtime / "owner_adapter.json"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen(1)
            socket_path.chmod(0o600)
            lease_path.write_text(json.dumps({
                "version": 1,
                "runtime_id": "runtime-owner",
                "pid": os.getpid(),
                "process_start": process_start_marker(os.getpid()),
                "profile_home": str(Path(tmp) / "profile"),
                "socket_path": str(socket_path),
                "lease_path": str(lease_path),
                "route": "/api/owner/ws",
                "transport": "websocket-unix",
                "host": "127.0.0.1",
                "port": 49119,
            }), encoding="utf-8")
            lease_path.chmod(0o600)
            self.addCleanup(listener.close)

            gateway = FakeGateway()
            config = BridgeConfig(
                api_key="api-test",
                gateway_owner_lease_path=lease_path,
                gateway_connect_timeout=1.0,
                gateway_request_timeout=1.0,
                gateway_heartbeat_interval=0.0,
            )
            def unexpected_tcp_connector(*_args, **_kwargs):
                raise AssertionError("owner mode must not use the injected TCP connector")

            client = LiveGatewayClient(config, connector=unexpected_tcp_connector)
            self.addCleanup(client.shutdown)
            with patch("websockets.asyncio.client.unix_connect", new=gateway.connect):
                client.connect()

            self.assertEqual(config.live_auth_mode(), "owner_adapter")
            self.assertEqual(gateway.connect_urls, [str(socket_path.resolve())])
            self.assertEqual(gateway.connect_kwargs[0]["uri"].split("?")[0], "ws://127.0.0.1:49119/api/owner/ws")
            query = parse_qs(urlsplit(gateway.connect_kwargs[0]["uri"]).query)
            self.assertEqual(query["runtime_id"], ["runtime-owner"])
            self.assertNotIn("token", query)
            self.assertNotIn("ticket", query)

    def test_access_token_mints_a_fresh_ticket_for_each_connection(self):
        tickets: list[str] = []

        class TicketHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                assert self.path == "/prefix/api/auth/ws-ticket"
                assert self.headers.get("Authorization") == "Bearer access-token"
                tickets.append(f"ticket-{len(tickets) + 1}")
                body = json.dumps({"ticket": tickets[-1]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        ticket_server = ThreadingHTTPServer(("127.0.0.1", 0), TicketHandler)
        ticket_thread = threading.Thread(target=ticket_server.serve_forever, daemon=True)
        ticket_thread.start()
        self.addCleanup(ticket_server.server_close)
        self.addCleanup(ticket_server.shutdown)
        self.addCleanup(ticket_thread.join, 2.0)

        gateway = FakeGateway()
        config, _ = self.make_config(
            gateway,
            gateway_token="",
            gateway_access_token="access-token",
            gateway_url=f"ws://127.0.0.1:{ticket_server.server_port}/prefix/api/ws",
        )
        client = LiveGatewayClient(config, connector=gateway.connect)
        self.addCleanup(client.shutdown)
        client.connect()
        client.reconnect()

        self.assertEqual(tickets, ["ticket-1", "ticket-2"])
        self.assertEqual(parse_qs(urlsplit(gateway.connect_urls[0]).query)["ticket"], ["ticket-1"])
        self.assertEqual(parse_qs(urlsplit(gateway.connect_urls[1]).query)["ticket"], ["ticket-2"])
        self.assertNotIn("subprotocols", gateway.connect_kwargs[0])
        self.assertNotIn("subprotocols", gateway.connect_kwargs[1])

    def test_expired_access_token_refreshes_once_before_minting_ticket(self):
        ticket_authorizations: list[str] = []
        refresh_bodies: list[dict] = []

        class RefreshHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if self.path == "/prefix/api/auth/ws-ticket":
                    ticket_authorizations.append(self.headers.get("Authorization", ""))
                    if len(ticket_authorizations) == 1:
                        self.send_response(401)
                        self.end_headers()
                        return
                    payload = {"ticket": "ticket-after-refresh"}
                elif self.path == "/prefix/auth/native/refresh":
                    refresh_bodies.append(json.loads(body))
                    payload = {
                        "access_token": "access-after-refresh",
                        "refresh_token": "refresh-after-refresh",
                        "provider": "stub",
                    }
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                encoded = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args):
                return

        ticket_server = ThreadingHTTPServer(("127.0.0.1", 0), RefreshHandler)
        ticket_thread = threading.Thread(target=ticket_server.serve_forever, daemon=True)
        ticket_thread.start()
        self.addCleanup(ticket_server.server_close)
        self.addCleanup(ticket_server.shutdown)
        self.addCleanup(ticket_thread.join, 2.0)

        gateway = FakeGateway()
        config, _ = self.make_config(
            gateway,
            gateway_token="",
            gateway_access_token="expired-access",
            gateway_refresh_token="refresh-before",
            gateway_auth_provider="stub",
            gateway_url=f"ws://127.0.0.1:{ticket_server.server_port}/prefix/api/ws",
        )
        client = LiveGatewayClient(config, connector=gateway.connect)
        self.addCleanup(client.shutdown)

        client.connect()

        self.assertEqual(ticket_authorizations, [
            "Bearer expired-access", "Bearer access-after-refresh",
        ])
        self.assertEqual(refresh_bodies, [{
            "refresh_token": "refresh-before", "provider": "stub",
        }])
        self.assertEqual(parse_qs(urlsplit(gateway.connect_urls[0]).query)["ticket"], ["ticket-after-refresh"])

        gateway2 = FakeGateway()
        config2, _ = self.make_config(
            gateway2,
            gateway_token="",
            gateway_access_token="",
            gateway_refresh_token="refresh-only",
            gateway_auth_provider="stub",
            gateway_url=f"ws://127.0.0.1:{ticket_server.server_port}/prefix/api/ws",
        )
        client2 = LiveGatewayClient(config2, connector=gateway2.connect)
        self.addCleanup(client2.shutdown)
        client2.connect()

        self.assertEqual(ticket_authorizations[-1], "Bearer access-after-refresh")
        self.assertEqual(refresh_bodies[-1], {
            "refresh_token": "refresh-only", "provider": "stub",
        })
        self.assertEqual(parse_qs(urlsplit(gateway2.connect_urls[0]).query)["ticket"], ["ticket-after-refresh"])

    def test_reconnect_replays_gap_before_racing_live_event(self):
        gateway = FakeGateway()
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=gateway.connect)
        self.addCleanup(client.shutdown)
        client.connect()
        client.request("session.create", {"source": "tool"})
        first = gateway.sockets[0]
        first.push({"jsonrpc": "2.0", "method": "event", "params": {
            "type": "message.start", "session_id": "runtime-1", "seq": 1,
        }})
        first.push({"jsonrpc": "2.0", "method": "event", "params": {
            "type": "message.delta", "session_id": "runtime-1", "seq": 2,
            "payload": {"text": "before"},
        }})
        self.wait_until(lambda: client.watermarks().get("runtime-1") == 2)

        gateway.mode = "race"
        client.reconnect()
        self.wait_until(lambda: client.watermarks().get("runtime-1") == 4)

        events = client.events("runtime-1")
        self.assertEqual([event["seq"] for event in events], [1, 2, 3, 4])
        self.assertEqual(gateway.replay_calls, 1)

    def test_replay_epoch_change_discards_old_cursor(self):
        gateway = FakeGateway()
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=gateway.connect)
        self.addCleanup(client.shutdown)
        client.connect()
        client.request("session.create", {"source": "tool"})
        socket = gateway.sockets[0]
        socket.push({"jsonrpc": "2.0", "method": "event", "params": {
            "type": "message.start", "session_id": "runtime-1", "seq": 7,
        }})
        self.wait_until(lambda: client.watermarks().get("runtime-1") == 7)

        gateway.epoch = "epoch-2"
        gateway.mode = "normal"
        before = gateway.replay_calls
        client.reconnect()

        self.assertEqual(client.watermarks(), {})
        self.assertEqual(gateway.replay_calls, before)
        self.assertEqual(client.health()["replay_epoch"], "epoch-2")

    def test_event_buffer_is_bounded(self):
        gateway = FakeGateway()
        config, _ = self.make_config(gateway, gateway_event_buffer_max=2)
        client = LiveGatewayClient(config, connector=gateway.connect)
        self.addCleanup(client.shutdown)
        client.connect()
        client.request("session.create", {"source": "tool"})
        socket = gateway.sockets[0]
        for seq in range(1, 4):
            socket.push({"jsonrpc": "2.0", "method": "event", "params": {
                "type": "message.delta", "session_id": "runtime-1", "seq": seq,
            }})
        self.wait_until(lambda: [e["seq"] for e in client.events("runtime-1")] == [2, 3])
        self.assertEqual([e["seq"] for e in client.events("runtime-1")], [2, 3])
        self.assertTrue(client.events_truncated("runtime-1", after_seq=0))
        self.assertFalse(client.events_truncated("runtime-1", after_seq=2))

    def test_http_auth_rejection_has_a_distinct_error_code(self):
        class Rejected(Exception):
            response = SimpleNamespace(status_code=403)

        async def rejected_connector(*_args, **_kwargs):
            raise Rejected("forbidden")

        gateway = FakeGateway()
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=rejected_connector)
        self.addCleanup(client.shutdown)
        with self.assertRaises(LiveError) as caught:
            client.connect()
        self.assertEqual(caught.exception.code, "gateway_auth_failed")

    def test_unknown_rpc_outcome_is_explicit(self):
        gateway = FakeGateway()
        gateway.mode = "unknown"
        config, _ = self.make_config(gateway)
        client = LiveGatewayClient(config, connector=gateway.connect)
        self.addCleanup(client.shutdown)
        client.connect()
        with self.assertRaises(LiveTransportUnknown):
            client.request("prompt.submit", {"session_id": "runtime-1", "text": "do not retry"})
        self.assertEqual(gateway.prompt_calls, 1)


class LiveServiceTests(unittest.TestCase):
    def make_service(self, gateway: FakeGateway):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = BridgeConfig(
            api_key="api-test", gateway_url="ws://gateway.test/api/ws", gateway_token="gateway-token",
            gateway_connect_timeout=1.0, gateway_request_timeout=1.0, gateway_heartbeat_interval=0.0,
            state_db=Path(tmp.name) / "state.db",
        )
        registry = StateRegistry(config.state_db)
        self.addCleanup(registry.close)
        client = LiveGatewayClient(config, connector=gateway.connect)
        self.addCleanup(client.shutdown)
        return LiveService(client, registry), registry

    def test_open_binds_lane_to_durable_id_and_resume_uses_runtime_id(self):
        gateway = FakeGateway()
        service, registry = self.make_service(gateway)

        opened = service.open(lane="coding")
        self.assertTrue(opened["ok"])
        self.assertEqual(opened["session_id"], "runtime-1")
        self.assertEqual(opened["stored_session_id"], "stored-1")
        self.assertEqual(registry.session_for_lane("coding"), "stored-1")

        resumed = service.open(lane="coding")
        self.assertEqual(resumed["session_id"], "runtime-resumed")
        methods = [frame["method"] for frame in gateway.sockets[0].sent]
        self.assertEqual(methods, [
            "client.capabilities", "session.create", "session.activate", "session.resume", "session.activate",
        ])

    def test_unknown_prompt_is_not_resubmitted_for_same_request_id(self):
        gateway = FakeGateway()
        gateway.mode = "unknown"
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")

        first = service.prompt(
            lane="coding", session_id=opened["session_id"], text="do not duplicate", request_id="live-1"
        )
        second = service.prompt(
            lane="coding", session_id=opened["session_id"], text="do not duplicate", request_id="live-1"
        )

        self.assertEqual(first["status"], "unknown")
        self.assertEqual(first["error_code"], "transport_unknown")
        self.assertEqual(second["status"], "unknown")
        self.assertEqual(second["replayed"], True)
        self.assertEqual(gateway.prompt_calls, 1)
        self.assertEqual(
            [frame["method"] for frame in gateway.sockets[0].sent].count("prompt.submit"),
            1,
        )
        wait = service.wait(request_id="live-1", timeout_seconds=0)
        self.assertEqual(wait["status"], "unknown")
        self.assertEqual(gateway.prompt_calls, 1)

    def test_wait_returns_the_matching_live_completion(self):
        gateway = FakeGateway()
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")

        text = "finish this\n\twithout flattening"
        result = service.prompt(
            lane="coding", session_id=opened["session_id"], text=text, request_id="live-complete",
            wait_seconds=1,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["answer"], "done")
        self.assertEqual(result["request_id"], "live-complete")
        prompt_frames = [frame for frame in gateway.sockets[0].sent if frame["method"] == "prompt.submit"]
        self.assertEqual(prompt_frames[0]["params"]["text"], text)

    def test_request_identity_survives_runtime_session_rotation(self):
        gateway = FakeGateway()
        service, registry = self.make_service(gateway)
        opened = service.open(lane="coding")
        first = service.prompt(
            lane="coding", session_id=opened["session_id"], text="same durable request", request_id="live-rotate",
            wait_seconds=1,
        )
        self.assertEqual(first["status"], "completed")

        service.reconnect()
        record = registry.live_request_by_id("live-rotate")
        assert record is not None
        self.assertEqual(record["runtime_session_id"], "runtime-resumed")
        second = service.prompt(
            lane="coding", session_id="runtime-resumed", text="same durable request", request_id="live-rotate",
        )
        self.assertTrue(second["replayed"])
        self.assertEqual(gateway.prompt_calls, 1)

    # ----- H1: shared-turn completion attribution --------------------------

    def test_foreign_completion_between_submit_and_local_turn_is_rejected(self):
        gateway = FakeGateway()
        gateway.complete_delay = 0.5
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")
        socket = gateway.sockets[0]
        runtime = opened["session_id"]

        timer = threading.Timer(0.2, lambda: gateway.inject_foreign_turn(socket, runtime, "foreign-answer"))
        timer.start()
        self.addCleanup(timer.join, 1.0)

        result = service.prompt(
            lane="coding", session_id=runtime, text="local question", request_id="live-foreign", wait_seconds=2,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["answer"], "done")
        self.assertNotEqual(result.get("answer"), "foreign-answer")

    def test_foreign_completion_alone_never_satisfies_local_wait(self):
        gateway = FakeGateway()
        gateway.complete_delay = None
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")
        runtime = opened["session_id"]
        service.prompt(
            lane="coding", session_id=runtime, text="local question", request_id="live-foreign-only",
        )
        gateway.inject_foreign_turn(gateway.sockets[0], runtime, "foreign-answer")

        waited = service.wait(request_id="live-foreign-only", timeout_seconds=0.5)

        self.assertEqual(waited["status"], "running")
        self.assertEqual(waited["error_code"], "wait_timeout")
        self.assertIsNone(waited.get("answer"))

    def test_queued_submit_wait_is_conservative(self):
        gateway = FakeGateway()
        gateway.mode = "busy"
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")

        result = service.prompt(
            lane="coding", session_id=opened["session_id"], text="queued question",
            request_id="live-queued", queued=True, wait_seconds=1,
        )

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["attribution"], "unproven")
        waited = service.wait(request_id="live-queued", timeout_seconds=1)
        self.assertEqual(waited["error_code"], "ambiguous_turn")
        self.assertIsNone(waited.get("answer"))
        self.assertEqual(gateway.prompt_calls, 1)

    def test_unprovable_claim_keeps_wait_conservative_even_with_buffered_completion(self):
        gateway = FakeGateway()
        gateway.inflight_enabled = False
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")

        result = service.prompt(
            lane="coding", session_id=opened["session_id"], text="no proof",
            request_id="live-noproof",
        )

        self.assertEqual(result["status"], "streaming")
        self.assertEqual(result["attribution"], "unproven")
        waited = service.wait(request_id="live-noproof", timeout_seconds=0.5)
        self.assertEqual(waited["error_code"], "ambiguous_turn")
        self.assertIsNone(waited.get("answer"))

    def test_wait_after_rotation_reports_completion_not_observed(self):
        gateway = FakeGateway()
        gateway.complete_delay = None
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")
        service.prompt(
            lane="coding", session_id=opened["session_id"], text="lost turn", request_id="live-rotate-wait",
        )
        gateway.epoch = "epoch-2"

        service.reconnect()
        waited = service.wait(request_id="live-rotate-wait", timeout_seconds=0.5)

        self.assertEqual(waited["status"], "unknown")
        self.assertEqual(waited["error_code"], "completion_not_observed")
        self.assertIsNone(waited.get("answer"))

    def test_wait_after_rotation_reproves_still_running_turn(self):
        gateway = FakeGateway()
        gateway.complete_delay = None
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")
        text = "still running"
        service.prompt(
            lane="coding", session_id=opened["session_id"], text=text, request_id="live-rotate-running",
        )
        gateway.epoch = "epoch-2"

        service.reconnect()
        resumed_state = gateway.turn_state("runtime-resumed")
        resumed_state["running"] = True
        resumed_state["inflight_user"] = text

        waited = service.wait(request_id="live-rotate-running", timeout_seconds=0.4)

        self.assertEqual(waited["status"], "running")
        self.assertEqual(waited["error_code"], "wait_timeout")
        self.assertIsNone(waited.get("answer"))

    def test_wait_after_same_epoch_reconnect_reproves_and_completes(self):
        gateway = FakeGateway()
        gateway.complete_delay = None
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")
        text = "survives a reconnect"
        service.prompt(
            lane="coding", session_id=opened["session_id"], text=text, request_id="live-reconnect-ok",
        )

        # Plain reconnect: same replay epoch, new connection generation, so the
        # proof watermark continuity is gone and a fresh inflight proof is
        # required before any completion may be accepted.
        service.reconnect()
        resumed_state = gateway.turn_state("runtime-resumed")
        resumed_state["running"] = True
        resumed_state["inflight_user"] = text
        waited = service.wait(request_id="live-reconnect-ok", timeout_seconds=0.3)
        self.assertEqual(waited["status"], "running")

        gateway.turn_state("runtime-resumed")["inflight_user"] = None
        gateway.emit_event(
            gateway.sockets[-1], "runtime-resumed", "message.complete",
            {"status": "complete", "text": "done"},
        )
        gateway.turn_state("runtime-resumed")["running"] = False
        waited = service.wait(request_id="live-reconnect-ok", timeout_seconds=1)

        self.assertEqual(waited["status"], "completed")
        self.assertEqual(waited["answer"], "done")

    def test_identical_text_foreign_completion_is_never_returned_while_claim_runs(self):
        gateway = FakeGateway()
        gateway.complete_delay = None
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")
        runtime = opened["session_id"]
        service.prompt(
            lane="coding", session_id=runtime, text="same text", request_id="live-same-text",
        )
        # A foreign turn with a byte-identical prompt produces an inflight
        # snapshot and completion payload indistinguishable from ours; under a
        # claimed run it must still never satisfy the local wait.
        gateway.inject_foreign_turn(gateway.sockets[0], runtime, "done")

        waited = service.wait(request_id="live-same-text", timeout_seconds=0.5)

        self.assertEqual(waited["status"], "running")
        self.assertEqual(waited["error_code"], "wait_timeout")
        self.assertIsNone(waited.get("answer"))

    def test_concurrent_same_request_id_submits_exactly_once(self):
        gateway = FakeGateway()
        service, registry = self.make_service(gateway)
        opened = service.open(lane="coding")
        runtime = opened["session_id"]
        barrier = threading.Barrier(2)
        results: dict[str, dict] = {}

        def submit(tag: str) -> None:
            barrier.wait(timeout=2)
            results[tag] = service.prompt(
                lane="coding", session_id=runtime, text="only once",
                request_id="live-race", wait_seconds=1.5,
            )

        threads = [threading.Thread(target=submit, args=(tag,)) for tag in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(gateway.prompt_calls, 1)
        self.assertEqual(len(results), 2)
        statuses = {result["status"] for result in results.values()}
        self.assertTrue(statuses <= {"completed", "streaming", "pending"}, statuses)
        self.assertTrue("completed" in statuses)
        replayed_flags = [bool(result.get("replayed")) for result in results.values()]
        self.assertEqual(sorted(replayed_flags), [False, True])

    # ----- H2: boundary-aware unknown-submit reconciliation -----------------

    def test_reconcile_ignores_older_identical_prompt(self):
        gateway = FakeGateway()
        gateway.history_rows = [{"role": "user", "text": "same text", "row_id": 1}]
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")
        gateway.mode = "unknown"
        submitted = service.prompt(
            lane="coding", session_id=opened["session_id"], text="same text", request_id="live-old",
        )
        self.assertEqual(submitted["status"], "unknown")
        gateway.mode = "normal"

        result = service.reconcile(request_id="live-old")

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reconciliation"], "not_observed")

    def test_reconcile_matches_post_boundary_prompt(self):
        gateway = FakeGateway()
        gateway.history_rows = [{"role": "user", "text": "old unrelated", "row_id": 1}]
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")
        gateway.mode = "unknown"
        service.prompt(
            lane="coding", session_id=opened["session_id"], text="retry text", request_id="live-new",
        )
        gateway.mode = "normal"
        gateway.history_rows.append({"role": "user", "text": "retry text", "row_id": 2})

        result = service.reconcile(request_id="live-new")

        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["reconciliation"], "history_match_post_boundary")

    def test_reconcile_identical_prompts_across_boundary_count_only_post_boundary(self):
        gateway = FakeGateway()
        gateway.history_rows = [{"role": "user", "text": "same text", "row_id": 1}]
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")
        gateway.mode = "unknown"
        service.prompt(
            lane="coding", session_id=opened["session_id"], text="same text", request_id="live-across",
        )
        gateway.mode = "normal"
        gateway.history_rows.append({"role": "user", "text": "same text", "row_id": 2})

        result = service.reconcile(request_id="live-across")

        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["reconciliation"], "history_match_post_boundary")

    def test_reconcile_multiple_post_boundary_identical_prompts_stay_conservative(self):
        gateway = FakeGateway()
        gateway.history_rows = [{"role": "user", "text": "seed", "row_id": 1}]
        service, _ = self.make_service(gateway)
        opened = service.open(lane="coding")
        gateway.mode = "unknown"
        service.prompt(
            lane="coding", session_id=opened["session_id"], text="dup", request_id="live-dup",
        )
        gateway.mode = "normal"
        gateway.history_rows.extend([
            {"role": "user", "text": "dup", "row_id": 2},
            {"role": "user", "text": "dup", "row_id": 3},
        ])

        result = service.reconcile(request_id="live-dup")

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "ambiguous_history_match")
        self.assertEqual(result["reconciliation"], "post_boundary_ambiguous")

    def test_reconcile_without_boundary_metadata_is_conservative(self):
        gateway = FakeGateway()
        service, registry = self.make_service(gateway)
        service.open(lane="coding")
        registry.save_live_request(
            request_id="legacy", lane="coding", session_id="stored-1",
            prompt_sha256="0" * 64, fingerprint="legacy-fp", status="unknown",
        )
        gateway.history_rows = [{"role": "user", "text": "whatever", "row_id": 1}]

        result = service.reconcile(request_id="legacy")

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "reconcile_boundary_missing")
        self.assertEqual(result["reconciliation"], "legacy_record_without_boundary")

    def test_legacy_registry_database_gets_attribution_columns(self):
        gateway = FakeGateway()
        service, registry = self.make_service(gateway)
        registry.close()
        legacy_path = registry.path
        legacy_path.unlink()

        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            CREATE TABLE lanes (lane TEXT PRIMARY KEY, session_id TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE requests (
                request_id TEXT PRIMARY KEY, lane TEXT NOT NULL, session_id TEXT, run_id TEXT,
                idempotency_key TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE live_requests (
                request_id TEXT PRIMARY KEY, lane TEXT NOT NULL, session_id TEXT NOT NULL,
                prompt_sha256 TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                runtime_session_id TEXT, start_seq INTEGER, error_code TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO live_requests(request_id, lane, session_id, prompt_sha256, fingerprint, status,"
            " created_at, updated_at) VALUES ('legacy-row', 'coding', 'stored-1', '0hash', 'fp', 'unknown', 0, 0)"
        )
        conn.commit()
        conn.close()

        reopened = StateRegistry(legacy_path)
        self.addCleanup(reopened.close)
        reopened.save_live_request(
            request_id="fresh", lane="coding", session_id="stored-1",
            prompt_sha256="1" * 64, fingerprint="fp2", status="pending",
            attribution="claimed", proof_seq=4, proof_epoch="epoch-1",
            inflight_sha256="2" * 64, boundary_row_id=0, boundary_count=0,
        )
        record = reopened.live_request_by_id("fresh")
        assert record is not None
        self.assertEqual(record["attribution"], "claimed")
        self.assertEqual(record["boundary_row_id"], 0)
        legacy = reopened.live_request_by_id("legacy-row")
        assert legacy is not None
        self.assertIsNone(legacy["boundary_row_id"])


if __name__ == "__main__":
    unittest.main()
