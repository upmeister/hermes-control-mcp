"""Stage 2.2B regression matrix: first-class multi-profile routing.

Covers the brief's required tests: registry/migration, live profile
propagation, durable /p/<profile>/ routing with profile-scoped keys,
ambiguity/conflict fail-closed behavior, and the secret boundary.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from hermes_zcode_bridge.api import APIResponse, HermesAPIClient
from hermes_zcode_bridge.config import BridgeConfig
from hermes_zcode_bridge.live_client import LiveGatewayClient
from hermes_zcode_bridge.live_service import LiveService
from hermes_zcode_bridge.mcp_server import _TOOL_DESCRIPTIONS
from hermes_zcode_bridge.profiles import canonical_profile, named_profile_api_key
from hermes_zcode_bridge.registry import StateRegistry
from hermes_zcode_bridge.service import BridgeService, _fingerprint

from test_contract import FakeTransport, response
from test_live import FakeGateway

DEFAULT_KEY = "test-api-key"
NAMED_KEY = "named-key-canary-coder-0001"


def _make_profiles_root(tmp: str, profiles: dict[str, str]) -> Path:
    root = Path(tmp) / "profiles"
    for name, key in profiles.items():
        profile_dir = root / name
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / ".env").write_text(f"API_SERVER_KEY={key}\n", encoding="utf-8")
    return root


class RegistryProfileTests(unittest.TestCase):
    def test_legacy_rows_migrate_to_default_profile(self):
        # Legacy 2.2A-era schema: profile columns and lane_bindings do not exist.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE lanes (
                    lane TEXT PRIMARY KEY, session_id TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE requests (
                    request_id TEXT PRIMARY KEY, lane TEXT NOT NULL, session_id TEXT,
                    run_id TEXT, idempotency_key TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL, error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE live_requests (
                    request_id TEXT PRIMARY KEY, lane TEXT NOT NULL, session_id TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                    runtime_session_id TEXT, start_seq INTEGER, error_code TEXT, attribution TEXT,
                    proof_seq INTEGER, proof_epoch TEXT, proof_generation INTEGER,
                    inflight_sha256 TEXT, boundary_row_id INTEGER, boundary_count INTEGER,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                """
            )
            conn.execute("INSERT INTO lanes VALUES ('work', 'stored-legacy', 1.0)")
            conn.execute(
                "INSERT INTO requests(request_id, lane, session_id, run_id, idempotency_key,"
                " fingerprint, status, created_at, updated_at)"
                " VALUES ('req-1', 'work', 'stored-legacy', 'run-1', 'bridge:req-1', 'fp', 'completed', 1.0, 1.0)"
            )
            conn.execute(
                "INSERT INTO live_requests(request_id, lane, session_id, prompt_sha256,"
                " fingerprint, status, created_at, updated_at)"
                " VALUES ('live-1', 'work', 'stored-legacy', 'sha', 'fpl', 'unknown', 1.0, 1.0)"
            )
            conn.commit()
            conn.close()

            registry = StateRegistry(path)
            self.addCleanup(registry.close)
            # Legacy lane migrates to the default profile binding.
            self.assertEqual(registry.session_for_profile_lane("default", "work"), "stored-legacy")
            self.assertEqual(registry.profiles_for_lane("work"), ["default"])
            # Legacy request rows read as default.
            self.assertEqual(registry.request_by_id("req-1")["profile"], "default")
            self.assertEqual(registry.live_request_by_id("live-1")["profile"], "default")
            # The legacy lanes table is retained for rollback safety.
            self.assertEqual(registry.session_for_lane("work"), "stored-legacy")

            registry.close()
            # Migration is idempotent on reopen.
            reopened = StateRegistry(path)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.session_for_profile_lane("default", "work"), "stored-legacy")
            self.assertEqual(reopened.request_by_id("req-1")["profile"], "default")

    def test_same_lane_name_coexists_across_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = StateRegistry(Path(tmp) / "state.db")
            self.addCleanup(registry.close)
            registry.bind_profile_lane("default", "work", "stored-default")
            registry.bind_profile_lane("coder", "work", "stored-coder")

            self.assertEqual(registry.session_for_profile_lane("default", "work"), "stored-default")
            self.assertEqual(registry.session_for_profile_lane("coder", "work"), "stored-coder")
            self.assertEqual(registry.profiles_for_lane("work"), ["coder", "default"])
            self.assertEqual(registry.profiles_for_session("stored-coder"), ["coder"])
            # Only the default binding is dual-written into the legacy table.
            self.assertEqual(registry.session_for_lane("work"), "stored-default")

    def test_named_profile_api_key_resolves_from_profile_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_profiles_root(tmp, {"coder": NAMED_KEY})
            self.assertEqual(named_profile_api_key("coder", root), NAMED_KEY)
            self.assertIsNone(named_profile_api_key("ghost", root))
        # Path traversal can never escape the profiles root.
        with self.assertRaises(ValueError):
            canonical_profile("../escape")
        self.assertIsNone(named_profile_api_key("../escape", Path("/tmp")))

    def test_profile_syntax_mirrors_upstream(self):
        self.assertEqual(canonical_profile("default"), "default")
        self.assertEqual(canonical_profile("DEFAULT"), "default")
        self.assertEqual(canonical_profile(" Coder "), "coder")
        self.assertEqual(canonical_profile("a-b_c1"), "a-b_c1")
        for bad in ("", "  ", "../escape", "My Work", "-lead", "x" * 65, "up/er"):
            with self.assertRaises(ValueError, msg=bad):
                canonical_profile(bad)


class DurableProfileRoutingTests(unittest.TestCase):
    def make_service(self, handler, *, profiles: dict[str, str] | None = None, api_url: str = "http://bridge.test:8642"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = BridgeConfig(
            api_url=api_url,
            api_key=DEFAULT_KEY,
            state_db=Path(tmp.name) / "state.db",
            request_timeout=3,
            profiles_root=_make_profiles_root(tmp.name, profiles or {}),
        )
        transport = FakeTransport(handler)
        registry = StateRegistry(config.state_db)
        self.addCleanup(registry.close)
        client = HermesAPIClient(config, transport=transport)
        return BridgeService(client, registry), transport

    def test_default_run_uses_unprefixed_route_and_default_key(self):
        seen: list[tuple[str, str]] = []

        def handler(method, url, headers, body, timeout):
            seen.append((urlparse(url).path, headers["Authorization"]))
            return response(202, {"run_id": "run-1", "status": "started", "replayed": False})

        service, transport = self.make_service(handler)
        result = service.start(lane="code/review", prompt="inspect", request_id="req-1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["profile"], "default")
        self.assertEqual(seen, [("/v1/runs", f"Bearer {DEFAULT_KEY}")])

    def test_named_run_uses_profile_prefix_and_named_key(self):
        seen: list[tuple[str, str]] = []

        def handler(method, url, headers, body, timeout):
            seen.append((urlparse(url).path, headers["Authorization"]))
            return response(202, {"run_id": "run-1", "status": "started", "replayed": False})

        service, _ = self.make_service(handler, profiles={"coder": NAMED_KEY})
        result = service.start(lane="code/review", prompt="inspect", request_id="req-1", profile="coder")
        self.assertTrue(result["ok"])
        self.assertEqual(result["profile"], "coder")
        self.assertEqual(seen, [("/p/coder/v1/runs", f"Bearer {NAMED_KEY}")])

    def test_run_controls_stay_on_originating_profile(self):
        seen: list[tuple[str, str]] = []

        def handler(method, url, headers, body, timeout):
            path = urlparse(url).path
            seen.append((path, headers["Authorization"]))
            if path.endswith("/events"):
                return APIResponse(200, {"Content-Type": "text/event-stream"}, b"data: {}\n\n")
            return response(200, {"run_id": "run-1", "status": "running", "replayed": False})

        service, _ = self.make_service(handler, profiles={"coder": NAMED_KEY})
        started = service.start(lane="lane-a", prompt="inspect", request_id="req-1", profile="coder")
        self.assertTrue(started["ok"])

        status = service.status(run_id="run-1")
        self.assertEqual(status["profile"], "coder")
        self.assertEqual(seen[-1], ("/p/coder/v1/runs/run-1", f"Bearer {NAMED_KEY}"))

        stopped = service.stop(run_id="run-1")
        self.assertEqual(stopped["profile"], "coder")
        self.assertEqual(seen[-1], ("/p/coder/v1/runs/run-1/stop", f"Bearer {NAMED_KEY}"))

        steered = service.steer(text="course-correct", run_id="run-1")
        self.assertEqual(steered["profile"], "coder")
        self.assertEqual(seen[-1], ("/p/coder/v1/runs/run-1/steer", f"Bearer {NAMED_KEY}"))

        events = service.events(run_id="run-1")
        self.assertEqual(events["profile"], "coder")
        self.assertEqual(seen[-1], ("/p/coder/v1/runs/run-1/events", f"Bearer {NAMED_KEY}"))

    def test_named_session_history_uses_named_prefix_and_key(self):
        seen: list[tuple[str, str]] = []

        def handler(method, url, headers, body, timeout):
            seen.append((urlparse(url).path, headers["Authorization"]))
            return response(200, {"data": [], "pagination": {}})

        service, _ = self.make_service(handler, profiles={"coder": NAMED_KEY})
        result = service.history(session_id="sess-1", profile="coder")
        self.assertTrue(result["ok"])
        self.assertEqual(result["profile"], "coder")
        self.assertEqual(seen, [("/p/coder/api/sessions/sess-1/messages", f"Bearer {NAMED_KEY}")])

    def test_missing_named_key_fails_closed_without_network_use(self):
        seen: list[dict] = []

        def handler(method, url, headers, body, timeout):
            seen.append({"url": url, "auth": headers.get("Authorization")})
            return response(200, {})

        service, transport = self.make_service(handler, profiles={})
        result = service.start(lane="lane-a", prompt="inspect", request_id="req-1", profile="coder")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "profile_key_unavailable")
        # Fail-closed: nothing reached the network and the default key was never sent.
        self.assertEqual(transport.calls, [])
        self.assertNotIn(DEFAULT_KEY, json.dumps(result))

    def test_invalid_profile_is_rejected_before_filesystem_or_network(self):
        seen: list[str] = []

        def handler(method, url, headers, body, timeout):
            seen.append(url)
            return response(200, {})

        service, transport = self.make_service(handler)
        for bad in ("../escape", "My Work", "x" * 65, "up/er"):
            result = service.start(lane="lane-a", prompt="inspect", request_id="req-1", profile=bad)
            self.assertEqual(result["error_code"], "invalid_profile", msg=bad)
        self.assertEqual(transport.calls, [])
        live_error = service.live_session_open(lane="lane-b", profile="../escape")
        self.assertEqual(live_error["error_code"], "invalid_profile")

    def test_unknown_profile_prefix_surfaces_structured_error(self):
        def handler(method, url, headers, body, timeout):
            if urlparse(url).path.startswith("/p/ghost/"):
                return response(404, {"error": {"code": "profile_not_served", "message": "Unknown or unconfigured profile"}})
            return response(202, {"run_id": "run-1", "status": "started", "replayed": False})

        service, _ = self.make_service(handler, profiles={"ghost": NAMED_KEY})
        result = service.start(lane="lane-a", prompt="inspect", request_id="req-1", profile="ghost")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "profile_not_served")
        self.assertNotIn(NAMED_KEY, json.dumps(result))

    def test_profiled_api_url_with_path_fails_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                api_url="http://bridge.test:8642/base",
                api_key=DEFAULT_KEY,
                state_db=Path(tmp) / "state.db",
                request_timeout=3,
                profiles_root=_make_profiles_root(tmp, {"coder": NAMED_KEY}),
            )
            transport = FakeTransport(lambda *args: response(200, {}))
            registry = StateRegistry(config.state_db)
            self.addCleanup(registry.close)
            service = BridgeService(HermesAPIClient(config, transport=transport), registry)
            result = service.start(lane="lane-a", prompt="inspect", request_id="req-1", profile="coder")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "profile_route_config_error")
            self.assertEqual(transport.calls, [])

    def test_same_request_id_across_profiles_conflicts(self):
        seen: list[str] = []

        def handler(method, url, headers, body, timeout):
            seen.append(urlparse(url).path)
            return response(202, {"run_id": "run-1", "status": "started", "replayed": False})

        service, _ = self.make_service(handler, profiles={"coder": NAMED_KEY})
        first = service.start(lane="lane-a", prompt="inspect", request_id="req-1")
        self.assertTrue(first["ok"])
        second = service.start(lane="lane-a", prompt="inspect", request_id="req-1", profile="coder")
        self.assertFalse(second["ok"])
        self.assertEqual(second["error_code"], "request_profile_conflict")
        self.assertFalse(second["replayed"])
        # The reverse direction conflicts too; nothing was resubmitted.
        self.assertEqual(seen, ["/v1/runs"])
        third = service.start(lane="lane-a", prompt="inspect", request_id="req-2", profile="coder")
        self.assertTrue(third["ok"])
        fourth = service.start(lane="lane-a", prompt="inspect", request_id="req-2")
        self.assertFalse(fourth["ok"])
        self.assertEqual(fourth["error_code"], "request_profile_conflict")
        self.assertEqual(seen, ["/v1/runs", "/p/coder/v1/runs"])

    def test_profile_participates_in_request_fingerprint(self):
        body = {"input": "inspect"}
        self.assertNotEqual(_fingerprint("lane", body, "coder"), _fingerprint("lane", body))
        # Default-profile fingerprints stay byte-compatible with pre-2.2B rows.
        self.assertEqual(_fingerprint("lane", body), _fingerprint("lane", body, "default"))

        def handler(method, url, headers, body, timeout):
            return response(202, {"run_id": "run-1", "status": "started", "replayed": False})

        service, _ = self.make_service(handler, profiles={"coder": NAMED_KEY})
        first = service.start(lane="lane-a", prompt="inspect", request_id="req-1", idempotency_key="idem-1")
        self.assertTrue(first["ok"])
        # The same explicit idempotency key under another profile may not replay.
        second = service.start(lane="lane-b", prompt="inspect", request_id="req-2", idempotency_key="idem-1", profile="coder")
        self.assertFalse(second["ok"])
        self.assertEqual(second["error_code"], "idempotency_key_conflict")

    def test_explicit_run_id_cannot_be_rerouted_across_profiles(self):
        calls: list[str] = []

        def handler(method, url, headers, body, timeout):
            calls.append(urlparse(url).path)
            return response(200, {"run_id": "run-1", "status": "running", "replayed": False})

        service, _ = self.make_service(handler, profiles={"coder": NAMED_KEY})
        started = service.start(lane="lane-a", prompt="inspect", request_id="req-1", profile="coder")
        self.assertTrue(started["ok"])

        conflicting = service.status(run_id="run-1", profile="default")
        self.assertFalse(conflicting["ok"])
        self.assertEqual(conflicting["error_code"], "request_profile_conflict")

        ok = service.status(run_id="run-1")
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["profile"], "coder")
        self.assertEqual(calls, ["/p/coder/v1/runs", "/p/coder/v1/runs/run-1"])

    def test_ambiguous_local_session_rejects_unrelated_supplied_profile(self):
        def handler(method, url, headers, body, timeout):
            return response(200, {"data": [], "pagination": {}})

        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                api_url="http://bridge.test:8642", api_key=DEFAULT_KEY,
                state_db=Path(tmp) / "state.db", request_timeout=3,
                profiles_root=_make_profiles_root(tmp, {"coder": NAMED_KEY, "ghost": "ghost-key-canary-0002"}),
            )
            transport = FakeTransport(handler)
            registry = StateRegistry(config.state_db)
            self.addCleanup(registry.close)
            service = BridgeService(HermesAPIClient(config, transport=transport), registry)
            registry.bind_profile_lane("default", "work", "stored-both")
            registry.bind_profile_lane("coder", "work", "stored-both")

            # An exact ID ambiguous across local profiles fails closed when the
            # supplied profile matches neither binding.
            unrelated = service.history(session_id="stored-both", profile="ghost")
            self.assertFalse(unrelated["ok"])
            self.assertEqual(unrelated["error_code"], "request_profile_conflict")
            self.assertEqual(transport.calls, [])

            # A supplied profile that matches one binding resolves the ambiguity.
            resolved = service.history(session_id="stored-both", profile="coder")
            self.assertTrue(resolved["ok"])
            self.assertEqual(urlparse(transport.calls[-1]["url"]).path, "/p/coder/api/sessions/stored-both/messages")

    def test_lane_lookup_ambiguity_fails_closed_but_explicit_profile_resolves(self):
        def handler(method, url, headers, body, timeout):
            return response(202, {"run_id": f"run-{len(urlparse(url).path)}", "status": "started", "replayed": False})

        service, _ = self.make_service(handler, profiles={"coder": NAMED_KEY})
        self.assertTrue(service.start(lane="shared", prompt="one", request_id="req-d")["ok"])
        self.assertTrue(service.start(lane="shared", prompt="two", request_id="req-c", profile="coder")["ok"])

        ambiguous = service.status(lane="shared")
        self.assertFalse(ambiguous["ok"])
        self.assertEqual(ambiguous["error_code"], "lane_profile_ambiguous")

        resolved = service.status(lane="shared", profile="coder")
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["profile"], "coder")


class LiveProfileTests(unittest.TestCase):
    def make_service(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = BridgeConfig(
            api_key="api-test", gateway_url="ws://gateway.test/api/ws", gateway_token="gateway-token",
            gateway_connect_timeout=1.0, gateway_request_timeout=1.0, gateway_heartbeat_interval=0.0,
            state_db=Path(tmp.name) / "state.db",
        )
        registry = StateRegistry(config.state_db)
        self.addCleanup(registry.close)
        gateway = FakeGateway()
        client = LiveGatewayClient(config, connector=gateway.connect)
        self.addCleanup(client.shutdown)
        return LiveService(client, registry), registry, gateway

    def frames(self, gateway, index=0):
        return gateway.sockets[index].sent

    def all_frames(self, gateway):
        return [frame for socket in gateway.sockets for frame in socket.sent]

    def test_named_create_passes_profile(self):
        service, registry, gateway = self.make_service()
        opened = service.open(lane="repo", profile="coder")
        self.assertTrue(opened["ok"])
        self.assertEqual(opened["profile"], "coder")
        create = next(f for f in self.frames(gateway) if f["method"] == "session.create")
        self.assertEqual(create["params"]["profile"], "coder")
        activate = next(f for f in self.frames(gateway) if f["method"] == "session.activate")
        self.assertEqual(activate["params"]["profile"], "coder")
        self.assertEqual(registry.session_for_profile_lane("coder", "repo"), "stored-1")
        self.assertIsNone(registry.session_for_profile_lane("default", "repo"))
        # A named binding must never collapse into the legacy global lane key.
        self.assertIsNone(registry.session_for_lane("repo"))

    def test_restart_resumes_named_lane_with_same_profile(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = BridgeConfig(
            api_key="api-test", gateway_url="ws://gateway.test/api/ws", gateway_token="gateway-token",
            gateway_connect_timeout=1.0, gateway_request_timeout=1.0, gateway_heartbeat_interval=0.0,
            state_db=Path(tmp.name) / "state.db",
        )
        registry = StateRegistry(config.state_db)
        gateway1 = FakeGateway()
        client1 = LiveGatewayClient(config, connector=gateway1.connect)
        self.addCleanup(client1.shutdown)
        service1 = LiveService(client1, registry)
        self.assertTrue(service1.open(lane="repo", profile="coder")["ok"])
        registry.close()

        # Simulate a bridge process restart against the same durable registry.
        registry2 = StateRegistry(config.state_db)
        self.addCleanup(registry2.close)
        gateway2 = FakeGateway()
        client2 = LiveGatewayClient(config, connector=gateway2.connect)
        self.addCleanup(client2.shutdown)
        service2 = LiveService(client2, registry2)

        resumed = service2.open(lane="repo")  # omitted profile infers the single binding
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["profile"], "coder")
        resume = next(f for f in self.frames(gateway2) if f["method"] == "session.resume")
        self.assertEqual(resume["params"], {"session_id": "stored-1", "profile": "coder"})

    def test_transient_4007_retry_preserves_session_id_and_profile(self):
        service, _, gateway = self.make_service()
        self.assertTrue(service.open(lane="repo", profile="coder")["ok"])
        gateway.resume_errors.append({"code": 4007, "message": "session no longer live; retry resume"})

        reopened = service.open(lane="repo", profile="coder", session_id="stored-1")
        self.assertTrue(reopened["ok"])
        self.assertEqual(gateway.resume_calls, 2)
        resumes = [f for f in self.frames(gateway) if f["method"] == "session.resume"]
        self.assertEqual(len(resumes), 2)
        # The retry repeats the identical profile-scoped params.
        self.assertEqual(resumes[0]["params"], resumes[1]["params"])
        self.assertEqual(resumes[0]["params"], {"session_id": "stored-1", "profile": "coder"})

    def test_live_calls_carry_the_resolved_profile(self):
        service, registry, gateway = self.make_service()
        self.assertTrue(service.open(lane="repo", profile="coder")["ok"])

        prompted = service.prompt(lane="repo", text="hello named", wait_seconds=0)
        self.assertTrue(prompted["ok"])
        self.assertEqual(prompted["profile"], "coder")
        submits = [f for f in self.frames(gateway) if f["method"] == "prompt.submit"]
        self.assertEqual(len(submits), 1)
        self.assertEqual(submits[0]["params"]["profile"], "coder")
        # Boundary capture and ownership-proof activation are profile-scoped too.
        for method in ("session.history", "session.activate"):
            scoped = [f for f in self.frames(gateway) if f["method"] == method]
            self.assertTrue(scoped)
            self.assertTrue(all(f["params"]["profile"] == "coder" for f in scoped), msg=method)

        status = service.status(lane="repo")
        self.assertEqual(status["profile"], "coder")
        history = service.history(lane="repo")
        self.assertEqual(history["profile"], "coder")
        steer = service.steer(text="nudge", lane="repo")
        self.assertEqual(steer["profile"], "coder")
        interrupt = service.interrupt(lane="repo")
        self.assertEqual(interrupt["profile"], "coder")
        for method in ("session.status", "session.steer", "session.interrupt"):
            frames = [f for f in self.frames(gateway) if f["method"] == method]
            self.assertTrue(frames, msg=method)
            self.assertEqual(frames[-1]["params"]["profile"], "coder", msg=method)
        # The durable record stores the profile for recovery.
        self.assertEqual(registry.live_request_by_id(prompted["request_id"])["profile"], "coder")

    def test_reconnect_reopens_same_named_lanes_in_both_profiles(self):
        service, _, gateway = self.make_service()
        self.assertTrue(service.open(lane="work")["ok"])  # default
        self.assertTrue(service.open(lane="work", profile="coder")["ok"])

        # The reconnect reopens every remembered (profile, lane) under its own
        # stored profile; resumes land on the post-reconnect socket.
        reconnected = service.reconnect()
        self.assertTrue(reconnected["ok"])
        resumed = reconnected["resumed"]
        self.assertEqual(sorted(resumed), ["coder:work", "default:work"])
        self.assertTrue(all(entry["ok"] for entry in resumed.values()))
        resumes = [f for f in self.all_frames(gateway) if f["method"] == "session.resume"]
        profiles = sorted(f["params"]["profile"] for f in resumes)
        self.assertEqual(profiles, ["coder", "default"])

    def test_reconcile_reads_history_under_stored_profile(self):
        service, registry, gateway = self.make_service()
        opened = service.open(lane="repo", profile="coder")
        self.assertTrue(opened["ok"])
        gateway.history_rows.append({"role": "user", "text": "named prompt", "row_id": 1})
        registry.save_live_request(
            request_id="live-1", lane="repo", profile="coder", session_id="stored-1",
            prompt_sha256=hashlib.sha256(b"named prompt").hexdigest(), fingerprint="fp",
            status="unknown", runtime_session_id=opened["session_id"],
            boundary_row_id=0, boundary_count=0, start_seq=0,
        )

        reconciled = service.reconcile(request_id="live-1")
        self.assertEqual(reconciled["status"], "reconciled")
        self.assertEqual(reconciled["profile"], "coder")
        histories = [f for f in self.frames(gateway) if f["method"] == "session.history"]
        self.assertTrue(histories)
        self.assertTrue(all(f["params"]["profile"] == "coder" for f in histories))

    def test_no_named_profile_operation_falls_back_to_default(self):
        service, registry, gateway = self.make_service()
        self.assertTrue(service.open(lane="work", profile="coder")["ok"])
        prompted = service.prompt(lane="work", text="named lane prompt", wait_seconds=0)
        self.assertTrue(prompted["ok"])

        status = service.status(lane="work")  # omitted profile must infer coder
        self.assertEqual(status["profile"], "coder")
        wait = service.wait(lane="work", timeout_seconds=0.0)
        self.assertIn(wait["error_code"], {"wait_timeout", "ambiguous_turn", "completion_not_observed"})
        self.assertEqual(wait["profile"], "coder")
        events = service.events(lane="work")
        self.assertEqual(events["profile"], "coder")

    def test_lane_name_in_two_profiles_is_ambiguous_without_explicit_profile(self):
        service, _, gateway = self.make_service()
        self.assertTrue(service.open(lane="work")["ok"])
        self.assertTrue(service.open(lane="work", profile="coder")["ok"])

        ambiguous = service.status(lane="work")
        self.assertFalse(ambiguous["ok"])
        self.assertEqual(ambiguous["error_code"], "lane_profile_ambiguous")
        resolved = service.status(lane="work", profile="coder")
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["profile"], "coder")
        # Lane-addressed waits are ambiguous too, and explicit profiles resolve.
        self.assertEqual(service.wait(lane="work", timeout_seconds=0.0)["error_code"], "lane_profile_ambiguous")
        self.assertEqual(service.wait(lane="work", timeout_seconds=0.0, profile="coder")["profile"], "coder")

    def test_known_runtime_with_conflicting_profile_fails_closed(self):
        service, _, gateway = self.make_service()
        opened = service.open(lane="repo", profile="coder")
        self.assertTrue(opened["ok"])
        frames_before = len(self.all_frames(gateway))

        conflict = service.status(session_id=opened["session_id"], profile="default")
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error_code"], "request_profile_conflict")
        history_conflict = service.history(session_id=opened["session_id"], profile="default")
        self.assertFalse(history_conflict["ok"])
        self.assertEqual(history_conflict["error_code"], "request_profile_conflict")
        # No gateway read was issued under the wrong profile.
        self.assertEqual(len(self.all_frames(gateway)), frames_before)

        # The omitted profile infers the stored binding instead.
        inferred = service.status(session_id=opened["session_id"])
        self.assertTrue(inferred["ok"])
        self.assertEqual(inferred["profile"], "coder")

    def test_lane_plus_runtime_conflicting_profile_fails_closed(self):
        # The lane branch must not bypass the runtime's stored profile binding:
        # lane+runtime addressing with a conflicting profile is a conflict, not
        # a reroute, and never emits a gateway frame or reserves a prompt.
        service, _, gateway = self.make_service()
        opened = service.open(lane="repo", profile="coder")
        self.assertTrue(opened["ok"])
        runtime = opened["session_id"]
        frames_before = len(self.all_frames(gateway))

        calls = (
            lambda: service.status(lane="repo", session_id=runtime, profile="default"),
            lambda: service.history(lane="repo", session_id=runtime, profile="default"),
            lambda: service.events(lane="repo", session_id=runtime, profile="default"),
            lambda: service.steer(text="nudge", lane="repo", session_id=runtime, profile="default"),
            lambda: service.interrupt(lane="repo", session_id=runtime, profile="default"),
            lambda: service.prompt(lane="repo", text="must not submit", session_id=runtime, profile="default"),
        )
        for call in calls:
            result = call()
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "request_profile_conflict")
        self.assertEqual(len(self.all_frames(gateway)), frames_before)

        # Lane+runtime with the omitted profile still infers the lane binding.
        inferred = service.status(lane="repo", session_id=runtime)
        self.assertTrue(inferred["ok"])
        self.assertEqual(inferred["profile"], "coder")

    def test_reconnect_replay_carries_the_stored_profile(self):
        service, _, gateway = self.make_service()
        self.assertTrue(service.open(lane="repo", profile="coder")["ok"])
        prompted = service.prompt(lane="repo", text="replay me", wait_seconds=0)
        self.assertTrue(prompted["ok"])

        reconnected = service.reconnect()
        self.assertTrue(reconnected["ok"])
        replays = [f for f in self.all_frames(gateway) if f["method"] == "session.events.since"]
        self.assertTrue(replays)
        self.assertTrue(all(f["params"]["profile"] == "coder" for f in replays), msg=replays)


class ProfileSecretBoundaryTests(unittest.TestCase):
    def test_registry_stores_profile_names_but_never_keys(self):
        def handler(method, url, headers, body, timeout):
            return response(202, {"run_id": "run-1", "status": "started", "replayed": False})

        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                api_url="http://bridge.test:8642", api_key=DEFAULT_KEY,
                state_db=Path(tmp) / "state.db", request_timeout=3,
                profiles_root=_make_profiles_root(tmp, {"coder": NAMED_KEY}),
            )
            registry = StateRegistry(config.state_db)
            self.addCleanup(registry.close)
            client = HermesAPIClient(config, transport=FakeTransport(handler))
            service = BridgeService(client, registry)
            self.assertTrue(service.start(lane="lane-a", prompt="inspect", request_id="req-1", profile="coder")["ok"])

            db_bytes = Path(config.state_db).read_bytes()
            self.assertIn(b"coder", db_bytes)
            self.assertNotIn(NAMED_KEY.encode(), db_bytes)
            self.assertNotIn(DEFAULT_KEY.encode(), db_bytes)

    def test_errors_and_results_never_carry_secret_values(self):
        def leaking_handler(method, url, headers, body, timeout):
            return response(500, {"error": {"code": "boom", "message": f"auth failed near {NAMED_KEY}"}})

        def default_key_leaking_handler(method, url, headers, body, timeout):
            # A named-profile request whose error message embeds the DEFAULT
            # key: redaction must cover every key the client knows, not only
            # the key used by the current request.
            return response(500, {"error": {"code": "boom", "message": f"auth failed near {DEFAULT_KEY}"}})

        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                api_url="http://bridge.test:8642", api_key=DEFAULT_KEY,
                state_db=Path(tmp) / "state.db", request_timeout=3,
                profiles_root=_make_profiles_root(tmp, {"coder": NAMED_KEY}),
            )
            registry = StateRegistry(config.state_db)
            self.addCleanup(registry.close)
            client = HermesAPIClient(config, transport=FakeTransport(leaking_handler))
            service = BridgeService(client, registry)
            result = service.start(lane="lane-a", prompt="inspect", request_id="req-1", profile="coder")
            self.assertFalse(result["ok"])
            self.assertIn("[REDACTED]", result["error"])
            self.assertNotIn(NAMED_KEY, json.dumps(result))
            self.assertNotIn(DEFAULT_KEY, json.dumps(result))

            client2 = HermesAPIClient(config, transport=FakeTransport(default_key_leaking_handler))
            service2 = BridgeService(client2, registry)
            result2 = service2.start(lane="lane-b", prompt="inspect", request_id="req-2", profile="coder")
            self.assertFalse(result2["ok"])
            self.assertIn("[REDACTED]", result2["error"])
            self.assertNotIn(DEFAULT_KEY, json.dumps(result2))

    def test_mcp_tool_descriptions_carry_no_secret_values(self):
        serialized = json.dumps(_TOOL_DESCRIPTIONS)
        self.assertNotIn(NAMED_KEY, serialized)
        self.assertNotIn(DEFAULT_KEY, serialized)
        # Profile routing semantics are documented on the tools themselves.
        self.assertIn("lane_profile_ambiguous", _TOOL_DESCRIPTIONS["live_session_open"])
        self.assertIn("profile", _TOOL_DESCRIPTIONS["run_start"])


if __name__ == "__main__":
    unittest.main()
