"""Session-source visibility: operator-selectable ``source`` for live sessions.

Hermes hides ``kanban``/``tool``/``oneshot`` sessions from human-facing
``session.list`` (``INTERNAL_LISTING_SOURCES``). The bridge used to hardcode
``source="tool"``, so every MCP-driven session stayed invisible in Desktop.

Two independent properties are pinned here:

1. the default stays ``tool`` (unchanged public-beta behaviour), and
2. an operator-selected source is validated against the values that would
   change Hermes session *ownership* semantics, not just visibility.

The second point is the real hazard. ``tui_gateway/session_lifecycle.py``
defines ``_NON_GATEWAY_SOURCES``; any other source that resolves to a gateway
``Platform`` becomes gateway-owned, which suppresses ``db.end_session()`` and
can start the Groundhog Day loop. ``source="desktop"`` is a different trap:
it enables automatic lease/row cleanup on ws_disconnect/idle_timeout. A purely
visibility-driven flag must therefore refuse both classes rather than forward
an arbitrary caller string to Hermes.
"""

from __future__ import annotations

import unittest

from hermes_control_mcp.config import ConfigError
from hermes_control_mcp.live_service import LiveService

# The bridge ships as a standalone package and must not import hermes internals,
# so validation is an allowlist of sources proven safe against two upstream
# rules in tui_gateway/session_lifecycle.py:
#   1. _NON_GATEWAY_SOURCES: a source outside this set that also resolves to a
#      gateway Platform becomes gateway-owned, which suppresses end_session()
#      and can start the Groundhog Day loop. "tool" is NOT in that set and is
#      still safe, because it is not a Platform either.
#   2. "desktop" enables automatic lease/row cleanup on ws_disconnect.
ALLOWED_SOURCES = frozenset({
    "tool", "tui", "cli", "webui", "subagent", "test", "acp",
})

# Observed hermes-agent 0.21.4 gateway.config.Platform members. A new upstream
# platform must never become reachable through this flag, which is why the
# bridge validates by allowlist rather than by denying known platform names.
GATEWAY_PLATFORMS = frozenset({
    "api_server", "discord", "email", "local", "matrix", "signal", "slack",
    "sms", "telegram", "webhook", "whatsapp", "yuanbao",
})

# Sources Hermes hides from human-facing session.list
# (INTERNAL_LISTING_SOURCES in tui_gateway/methods_session.py).
HIDDEN_SOURCES = frozenset({"kanban", "tool", "oneshot"})


class StubClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def request(self, method, params):
        self.requests.append((method, dict(params)))
        if method == "session.create":
            return {"session_id": "runtime-1", "stored_session_id": "stored-1"}
        return {"ok": True}


class StubRegistry:
    def session_for_profile_lane(self, profile, lane):
        return None

    def profiles_for_session(self, session_id):
        return []

    def bind_lane(self, *args, **kwargs):
        return None


def _service(source):
    return LiveService(StubClient(), StubRegistry(), source=source)


class SessionSourceConfigTests(unittest.TestCase):
    """The value must arrive from the operator, validated, with a safe default."""

    def test_default_source_is_tool(self):
        self.assertEqual(_service(None).session_source, "tool")

    def test_explicit_tool_is_accepted(self):
        self.assertEqual(_service("tool").session_source, "tool")

    def test_visible_source_is_accepted(self):
        self.assertEqual(_service("tui").session_source, "tui")

    def test_unknown_source_is_rejected(self):
        # A typo must fail loudly rather than silently producing a hidden or
        # gateway-owned session.
        with self.assertRaises(ConfigError):
            _service("tuii")

    def test_empty_source_is_rejected(self):
        with self.assertRaises(ConfigError):
            _service("")

    def test_source_is_bounded(self):
        with self.assertRaises(ConfigError):
            _service("t" * 201)

    def test_source_is_normalized(self):
        self.assertEqual(_service("  TUI  ").session_source, "tui")

    def test_desktop_source_is_rejected(self):
        # desktop enables automatic lease cleanup and _end_session_on_close
        # suppression: bridge sessions must stay durable across reconnects.
        with self.assertRaises(ConfigError):
            _service("desktop")

    def test_gateway_owned_candidate_is_rejected(self):
        # e.g. "telegram" is not in _NON_GATEWAY_SOURCES, so Hermes would treat
        # it as gateway-owned and suppress end_session -> Groundhog Day loop.
        with self.assertRaises(ConfigError):
            _service("telegram")

    def test_kanban_is_rejected(self):
        # kanban is gateway/lifecycle-managed and hidden from listings.
        with self.assertRaises(ConfigError):
            _service("kanban")

    def test_allowlist_excludes_every_known_gateway_platform(self):
        # A platform source would be gateway-owned in Hermes, so it must never
        # be reachable through this flag. ("local" is both a Platform member
        # and an explicit _NON_GATEWAY_SOURCES exclusion upstream, so it would
        # be safe - it is left out anyway: safety that depends on an upstream
        # exclusion list is a poor thing to expose as a public option.)
        self.assertEqual(ALLOWED_SOURCES & GATEWAY_PLATFORMS, frozenset())

    def test_allowlist_excludes_desktop(self):
        # desktop triggers automatic lease/row cleanup on disconnect.
        self.assertNotIn("desktop", ALLOWED_SOURCES)

    def test_allowlist_excludes_kanban(self):
        # kanban is lifecycle-managed by the kanban dispatcher.
        self.assertNotIn("kanban", ALLOWED_SOURCES)

    def test_allowlist_contains_the_default(self):
        # The documented default must be reachable, or operators cannot
        # restore public-beta behaviour.
        self.assertIn("tool", ALLOWED_SOURCES)

    def test_visible_sources_are_reachable(self):
        # The whole point of the feature: at least one allowlisted source must
        # not be hidden by INTERNAL_LISTING_SOURCES.
        visible = ALLOWED_SOURCES - HIDDEN_SOURCES
        self.assertTrue(visible)


class SessionSourcePropagationTests(unittest.TestCase):
    """The configured source must actually reach ``session.create``."""

    def test_default_propagates_tool(self):
        client = StubClient()
        service = LiveService(client, StubRegistry(), source=None)
        service.open(lane="demo", session_id=None, profile="default",
                     title=None, cwd=None, model=None, provider=None,
                     close_on_disconnect=False)
        self.assertEqual(client.requests[0][0], "session.create")
        self.assertEqual(client.requests[0][1]["source"], "tool")

    def test_configured_source_propagates(self):
        client = StubClient()
        service = LiveService(client, StubRegistry(), source="tui")
        service.open(lane="demo", session_id=None, profile="default",
                     title=None, cwd=None, model=None, provider=None,
                     close_on_disconnect=False)
        self.assertEqual(client.requests[0][1]["source"], "tui")


if __name__ == "__main__":
    unittest.main()
