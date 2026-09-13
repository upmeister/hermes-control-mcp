from __future__ import annotations

import json
from typing import Callable

from .service import BridgeService

try:
    from mcp.server import MCPServer
except ImportError:  # pragma: no cover - exercised by the startup error path
    MCPServer = None  # type: ignore[assignment,misc]


_TOOL_DESCRIPTIONS = {
    "run_start": "Start one idempotent Hermes durable run in a named lane.",
    "run_status": "Read status and terminal output for one exact Hermes run.",
    "run_wait": "Poll one Hermes run until terminal or until the bounded wait expires.",
    "run_events": "Collect the live SSE events retained for one exact Hermes run.",
    "run_stop": "Request cooperative stop for one exact Hermes run.",
    "run_steer": "Queue course correction for one exact running Hermes run.",
    "session_history": "Read bounded durable message history for one exact Hermes session.",
    "bridge_health": "Run non-consuming Hermes health, models, and capabilities probes.",
}


def _json_result(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def create_server(service: BridgeService):
    """Create the allowlisted MCP server around an already configured service."""
    if MCPServer is None:
        raise RuntimeError("MCP server requires the 'mcp' package")
    server = MCPServer(
        "hermes-zcode-bridge",
        instructions=(
            "Thin Hermes Agent durable-runs bridge. Use exact lane/session/run/request identities. "
            "Unknown transport outcomes require explicit reconciliation; this server does not expose shell, "
            "CLI, configuration mutation, slash commands, or raw gateway RPC."
        ),
    )

    def register(name: str, fn: Callable, description: str) -> None:
        server.tool(name=name, description=description)(fn)

    def run_start(
        lane: str,
        prompt: str,
        session_id: str = "",
        model: str = "",
        provider: str = "",
        instructions: str = "",
        request_id: str = "",
        idempotency_key: str = "",
    ) -> str:
        """Start a durable run; repeat the same request_id to reconcile an uncertain submit."""
        return _json_result(service.start(
            lane=lane,
            prompt=prompt,
            session_id=session_id or None,
            model=model or None,
            provider=provider or None,
            instructions=instructions or None,
            request_id=request_id or None,
            idempotency_key=idempotency_key or None,
        ))

    def run_status(lane: str = "", run_id: str = "") -> str:
        """Return status for lane's latest run or the exact supplied run_id."""
        return _json_result(service.status(lane=lane or None, run_id=run_id or None))

    def run_wait(lane: str = "", run_id: str = "", timeout_seconds: float = 30.0) -> str:
        """Wait a bounded time, returning wait_timeout rather than hiding an active run."""
        return _json_result(service.wait(
            lane=lane or None, run_id=run_id or None, timeout_seconds=timeout_seconds
        ))

    def run_events(lane: str = "", run_id: str = "") -> str:
        """Collect currently available SSE events for a lane's latest or exact run."""
        return _json_result(service.events(lane=lane or None, run_id=run_id or None))

    def run_stop(lane: str = "", run_id: str = "") -> str:
        """Stop only the exact run addressed by run_id or lane's latest request."""
        return _json_result(service.stop(lane=lane or None, run_id=run_id or None))

    def run_steer(text: str, lane: str = "", run_id: str = "") -> str:
        """Steer only the exact run addressed by run_id or lane's latest request."""
        return _json_result(service.steer(text=text, lane=lane or None, run_id=run_id or None))

    def session_history(session_id: str, limit: int = 100) -> str:
        """Read bounded oldest-first history from an exact Hermes session ID."""
        return _json_result(service.history(session_id=session_id, limit=limit))

    def bridge_health() -> str:
        """Probe health/models/capabilities without submitting an LLM turn."""
        return _json_result(service.health())

    for name, fn in (
        ("run_start", run_start),
        ("run_status", run_status),
        ("run_wait", run_wait),
        ("run_events", run_events),
        ("run_stop", run_stop),
        ("run_steer", run_steer),
        ("session_history", session_history),
        ("bridge_health", bridge_health),
    ):
        register(name, fn, _TOOL_DESCRIPTIONS[name])
    return server
