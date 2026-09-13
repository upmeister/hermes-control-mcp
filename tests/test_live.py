from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from hermes_zcode_bridge.config import BridgeConfig
from hermes_zcode_bridge.live_client import LiveError, LiveGatewayClient, LiveTransportUnknown
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
    def __init__(self):
        self.sockets: list[FakeSocket] = []
        self.connect_kwargs: list[dict] = []
        self.connect_urls: list[str] = []
        self.epoch = "epoch-1"
        self.prompt_calls = 0
        self.replay_calls = 0
        self.session_counter = 0
        self.mode = "normal"

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
                            "events": [{"type": "message.delta", "session_id": "runtime-1", "seq": 3,
                                        "payload": {"text": "gap"}}],
                            "latest_seq": 4,
                            "truncated": False,
                            "epoch": self.epoch,
                        },
                    })
                else:
                    socket.push({
                        "jsonrpc": "2.0", "id": rid,
                        "result": {"events": [], "latest_seq": 1, "truncated": False, "epoch": self.epoch},
                    })
            elif method == "prompt.submit":
                self.prompt_calls += 1
                if self.mode == "unknown":
                    await socket.close()
                    return
                socket.push({"jsonrpc": "2.0", "id": rid, "result": {"status": "streaming"}})
                socket.push({
                    "jsonrpc": "2.0", "method": "event",
                    "params": {"type": "message.start", "session_id": frame["params"]["session_id"], "seq": 1},
                })
                socket.push({
                    "jsonrpc": "2.0", "method": "event",
                    "params": {"type": "message.complete", "session_id": frame["params"]["session_id"], "seq": 2,
                               "payload": {"status": "complete", "text": "done"}},
                })
            elif method == "session.status":
                socket.push({"jsonrpc": "2.0", "id": rid, "result": {"output": "Hermes TUI Status"}})
            elif method == "session.history":
                socket.push({"jsonrpc": "2.0", "id": rid, "result": {"messages": []}})
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
        self.wait_until(lambda: len(client.events("runtime-1")) == 2)
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
        self.assertEqual(gateway.sockets[0].sent[0]["method"], "session.create")
        self.assertEqual(gateway.sockets[0].sent[1]["method"], "session.resume")

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


if __name__ == "__main__":
    unittest.main()
