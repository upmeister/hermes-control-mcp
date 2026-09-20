from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_control_mcp.api import APIError
from hermes_control_mcp.config import BridgeConfig
from hermes_control_mcp.doctor import discover_named_profiles, format_doctor_report, run_doctor


class FakeAPIClient:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.calls: list[tuple[str, str]] = []

    def health(self, *, profile: str | None = None):
        target = profile or "default"
        self.calls.append(("health", target))
        return {"status": "ok", "version": "test-hermes"}

    def capabilities(self, *, profile: str | None = None):
        target = profile or "default"
        self.calls.append(("capabilities", target))
        return {"features": {"run_submission": True}}

    def models(self, *, profile: str | None = None):
        target = profile or "default"
        self.calls.append(("models", target))
        return {"data": [{"id": f"model-{target}"}]}


class FailingTransportAPIClient(FakeAPIClient):
    def health(self, *, profile: str | None = None):
        raise APIError(
            0,
            "transport_unknown",
            "Hermes API transport outcome is unknown; retry explicitly with the same request ID",
            retryable=True,
        )


class FakeLiveClient:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True
        return {"replayed": 0}

    def health(self):
        return {
            "status": "healthy",
            "connection_state": "open" if self.connected else "idle",
            "auth_mode": "owner",
            "replay_epoch": "epoch-test",
        }

    def shutdown(self):
        self.closed = True


class DoctorTests(unittest.TestCase):
    def make_config(
        self,
        tmp: str,
        *,
        owner_lease: Path | None = None,
    ) -> BridgeConfig:
        return BridgeConfig(
            api_url="http://bridge.test:8642",
            api_key="default-test-key",
            state_db=Path(tmp) / "state.db",
            profiles_root=Path(tmp) / "profiles",
            gateway_owner_lease_path=owner_lease,
            request_timeout=1,
        )

    @staticmethod
    def write_profile_key(root: Path, profile: str, key: str = "named-test-key") -> None:
        path = root / profile
        path.mkdir(parents=True, exist_ok=True)
        (path / ".env").write_text(f"API_SERVER_KEY={key}\n", encoding="utf-8")

    def test_durable_ready_live_unconfigured_is_public_beta_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            report = run_doctor(config, api_client_factory=FakeAPIClient)

        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["capability_tiers"]["durable"], "stable")
        self.assertEqual(report["capability_tiers"]["live"], "experimental_unavailable")
        self.assertEqual(report["live"]["error_code"], "live_not_configured")
        self.assertFalse(report["live"]["required"])
        self.assertTrue(report["warnings"])

    def test_require_live_turns_unconfigured_live_into_hard_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            report = run_doctor(
                config,
                require_live=True,
                api_client_factory=FakeAPIClient,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "not_ready")
        self.assertTrue(report["live"]["required"])

    def test_missing_default_api_key_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_key_env = "HERMES_CONTROL_MCP_TEST_MISSING_KEY"
            config = BridgeConfig(
                api_url="http://bridge.test:8642",
                api_key=None,
                api_key_env=missing_key_env,
                env_file=Path(tmp) / "missing.env",
                state_db=Path(tmp) / "state.db",
                profiles_root=Path(tmp) / "profiles",
                request_timeout=1,
            )
            with patch.dict(os.environ, {missing_key_env: ""}):
                report = run_doctor(config)

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "not_ready")
        self.assertEqual(report["core"]["error_code"], "api_key_missing")
        self.assertIn("Missing API key", report["core"]["error"])

    def test_configured_live_probe_can_be_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp, owner_lease=Path(tmp) / "owner.json")
            report = run_doctor(
                config,
                require_live=True,
                api_client_factory=FakeAPIClient,
                live_client_factory=FakeLiveClient,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["capability_tiers"]["live"], "experimental_ready")
        self.assertEqual(report["live"]["auth_mode"], "owner")

    def test_missing_named_key_fails_closed_without_default_key_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            (config.resolved_profiles_root() / "coder").mkdir(parents=True)
            report = run_doctor(
                config,
                profiles=["coder"],
                api_client_factory=FakeAPIClient,
            )

        self.assertFalse(report["ok"])
        named = report["profiles"]["coder"]
        self.assertEqual(named["error_code"], "profile_key_unavailable")
        self.assertFalse(named["api_key_present"])
        rendered = format_doctor_report(report)
        self.assertNotIn("default-test-key", rendered)

    def test_named_profile_probe_succeeds_with_own_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            self.write_profile_key(config.resolved_profiles_root(), "coder")
            report = run_doctor(
                config,
                profiles=["coder"],
                api_client_factory=FakeAPIClient,
            )

        self.assertTrue(report["ok"])
        self.assertTrue(report["profiles"]["coder"]["api_key_present"])
        self.assertEqual(report["profiles"]["coder"]["status"], "ready")

    def test_all_profiles_discovers_only_canonical_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            self.write_profile_key(root, "coder")
            self.write_profile_key(root, "research-1")
            (root / "NotValid").mkdir(parents=True)
            (root / "..evil").mkdir(parents=True)
            (root / "README.txt").write_text("not a profile", encoding="utf-8")

            self.assertEqual(discover_named_profiles(root), ["coder", "research-1"])

            config = self.make_config(tmp)
            report = run_doctor(
                config,
                all_profiles=True,
                api_client_factory=FakeAPIClient,
            )
            self.assertEqual(sorted(report["profiles"]), ["coder", "research-1"])
            self.assertTrue(report["ok"])

    def test_human_report_includes_effective_config_and_actionable_transport_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                api_url="http://127.0.0.1:8642",
                api_key="default-test-key",
                state_db=Path(tmp) / "state.db",
                profiles_root=Path(tmp) / "profiles",
                request_timeout=1,
            )
            report = run_doctor(
                config,
                api_client_factory=FailingTransportAPIClient,
            )
            rendered = format_doctor_report(report)

        self.assertFalse(report["ok"])
        self.assertIn("http://127.0.0.1:8642", rendered)
        self.assertIn("API key ............ PRESENT (explicit configuration)", rendered)
        self.assertIn("curl http://127.0.0.1:8642/health", rendered)
        self.assertIn("127.0.0.1 refers to the machine running hermes-control-mcp", rendered)
        self.assertNotIn("default-test-key", rendered)

    def test_human_report_never_prints_api_key_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            self.write_profile_key(
                config.resolved_profiles_root(),
                "coder",
                key="DOCTOR_NAMED_SECRET_CANARY",
            )
            report = run_doctor(
                config,
                profiles=["coder"],
                api_client_factory=FakeAPIClient,
            )
            rendered = format_doctor_report(report)

        self.assertNotIn("default-test-key", rendered)
        self.assertNotIn("DOCTOR_NAMED_SECRET_CANARY", rendered)


if __name__ == "__main__":
    unittest.main()
