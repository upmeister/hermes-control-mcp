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
    "live_session_open": (
        "Connect to the existing Hermes TUI WebSocket and create or resume one durable session lane. "
        "Here session_id is the stored/durable ID to resume; the response session_id is the runtime ID "
        "and stored_session_id is the durable ID. Prefer lane for subsequent reads and control calls."
    ),
    "live_prompt": (
        "Submit one prompt to a live TUI session; never retries an unknown acknowledgement. "
        "When supplied explicitly, session_id is the runtime ID; prefer lane instead of copying a stored ID."
    ),
    "live_wait": "Wait for a live prompt's message.start/message.complete pair with a bounded timeout.",
    "live_events": (
        "Read bounded live TUI events after an optional per-session sequence cursor. "
        "When supplied explicitly, session_id is the runtime ID; prefer lane instead of copying a stored ID."
    ),
    "live_status": (
        "Read exact live TUI session status without submitting a prompt. "
        "When supplied explicitly, session_id is the runtime ID; prefer lane instead of copying a stored ID."
    ),
    "live_history": (
        "Read exact live TUI session history for recovery/reconciliation. "
        "When supplied explicitly, session_id is the runtime ID; prefer lane instead of copying a stored ID."
    ),
    "live_steer": (
        "Queue exact-session live TUI steering text. "
        "When supplied explicitly, session_id is the runtime ID; prefer lane instead of copying a stored ID."
    ),
    "live_interrupt": (
        "Interrupt the exact live TUI session cooperatively. "
        "When supplied explicitly, session_id is the runtime ID; prefer lane instead of copying a stored ID."
    ),
    "live_reconcile": "Reconcile an unknown live prompt against durable history without resubmitting it.",
    "live_reconnect": "Reconnect the live TUI WebSocket and replay retained per-session events.",
    "live_health": "Report live TUI connection, auth-mode, bounded-buffer, and replay state without an LLM turn.",
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
            "Thin Hermes Agent durable-runs and live-TUI bridge. Use exact lane/session/run/request identities. "
            "For live_session_open, its session_id argument is a stored/durable ID; the response session_id "
            "is the runtime ID and stored_session_id is durable. For live prompt/status/history/events/steer/interrupt, "
            "an explicit session_id is a runtime ID; prefer lane for stable routing and never pass a stored ID there. "
            "Unknown transport outcomes require explicit reconciliation; live prompts are never silently retried. "
            "The live TUI surface uses the private owner attach route and does not expose shell, CLI, configuration "
            "mutation, raw gateway dispatch, or slash commands."
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

    def live_session_open(
        lane: str,
        session_id: str = "",
        title: str = "",
        cwd: str = "",
        profile: str = "",
        model: str = "",
        provider: str = "",
        close_on_disconnect: bool = False,
    ) -> str:
        """Open a live lane; supplied session_id is the stored/durable id to resume.

        The response session_id is the ephemeral runtime id; prefer lane for
        later reads and control calls.
        """
        return _json_result(service.live_session_open(
            lane=lane, session_id=session_id or None, title=title or None, cwd=cwd or None,
            profile=profile or None, model=model or None, provider=provider or None,
            close_on_disconnect=close_on_disconnect,
        ))

    def live_prompt(
        lane: str,
        text: str,
        session_id: str = "",
        request_id: str = "",
        queued: bool = False,
        wait_seconds: float = 0.0,
    ) -> str:
        """Submit one live prompt; explicit session_id is a runtime id.

        Prefer lane; wait_seconds optionally collects the final event.
        """
        return _json_result(service.live_prompt(
            lane=lane, text=text, session_id=session_id or None,
            request_id=request_id or None, queued=queued, wait_seconds=wait_seconds,
        ))

    def live_wait(request_id: str = "", lane: str = "", timeout_seconds: float = 120.0) -> str:
        """Wait for the exact request or latest prompt in a lane."""
        return _json_result(service.live_wait(
            request_id=request_id or None, lane=lane or None, timeout_seconds=timeout_seconds,
        ))

    def live_events(lane: str = "", session_id: str = "", after_seq: int = 0) -> str:
        """Read buffered events; explicit session_id is a runtime id.

        Prefer lane; this does not ask the backend to replay events.
        """
        return _json_result(service.live_events(
            lane=lane or None, session_id=session_id or None, after_seq=after_seq,
        ))

    def live_status(lane: str = "", session_id: str = "") -> str:
        """Read live status; explicit session_id is a runtime id. Prefer lane."""
        return _json_result(service.live_status(lane=lane or None, session_id=session_id or None))

    def live_history(lane: str = "", session_id: str = "") -> str:
        """Read live history; explicit session_id is a runtime id. Prefer lane."""
        return _json_result(service.live_history(lane=lane or None, session_id=session_id or None))

    def live_steer(text: str, lane: str = "", session_id: str = "") -> str:
        """Queue text; explicit session_id is a runtime id. Prefer lane."""
        return _json_result(service.live_steer(text=text, lane=lane or None, session_id=session_id or None))

    def live_interrupt(lane: str = "", session_id: str = "") -> str:
        """Interrupt a live session; explicit session_id is a runtime id. Prefer lane."""
        return _json_result(service.live_interrupt(lane=lane or None, session_id=session_id or None))

    def live_reconcile(request_id: str) -> str:
        """Reconcile unknown live prompt state from durable history, never by resubmitting."""
        return _json_result(service.live_reconcile(request_id=request_id))

    def live_reconnect() -> str:
        """Reconnect the live socket and perform bounded event replay."""
        return _json_result(service.live_reconnect())

    def live_health() -> str:
        """Report live transport state without submitting a turn."""
        return _json_result(service.live_health())

    for name, fn in (
        ("run_start", run_start),
        ("run_status", run_status),
        ("run_wait", run_wait),
        ("run_events", run_events),
        ("run_stop", run_stop),
        ("run_steer", run_steer),
        ("session_history", session_history),
        ("bridge_health", bridge_health),
        ("live_session_open", live_session_open),
        ("live_prompt", live_prompt),
        ("live_wait", live_wait),
        ("live_events", live_events),
        ("live_status", live_status),
        ("live_history", live_history),
        ("live_steer", live_steer),
        ("live_interrupt", live_interrupt),
        ("live_reconcile", live_reconcile),
        ("live_reconnect", live_reconnect),
        ("live_health", live_health),
    ):
        register(name, fn, _TOOL_DESCRIPTIONS[name])
    return server
