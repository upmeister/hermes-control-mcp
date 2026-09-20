from __future__ import annotations

import json
from typing import Callable

from .service import BridgeService

try:
    from mcp.server import MCPServer
except ImportError:  # pragma: no cover - exercised by the startup error path
    MCPServer = None  # type: ignore[assignment,misc]


_TOOL_DESCRIPTIONS = {
    "run_start": (
        "Start one idempotent Hermes durable run in a named lane. Optional profile routes the run "
        "through that profile's /p/<profile>/ API and credentials; omitted means the default profile. "
        "A request_id already used under a different profile conflicts instead of rerouting."
    ),
    "run_status": (
        "Read status and terminal output for one exact Hermes run. Optional profile must match the "
        "run's stored profile; lane-only lookups infer the profile and fail closed if the lane exists "
        "under multiple profiles."
    ),
    "run_wait": "Poll one Hermes run until terminal or until the bounded wait expires. Optional profile follows run_status semantics.",
    "run_events": "Collect the live SSE events retained for one exact Hermes run. Optional profile follows run_status semantics.",
    "run_stop": "Request cooperative stop for one exact Hermes run. Optional profile follows run_status semantics.",
    "run_steer": "Queue course correction for one exact running Hermes run. Optional profile follows run_status semantics.",
    "session_history": (
        "Read bounded durable message history for one exact Hermes session. Optional profile routes "
        "through that profile's /p/<profile>/ prefix; omitted infers the session's locally known "
        "profile (fail-closed when ambiguous) and otherwise uses the default profile."
    ),
    "bridge_health": "Run non-consuming Hermes health, models, and capabilities probes. Optional profile probes through that profile's prefix.",
    "live_session_open": (
        "Connect to the existing Hermes TUI WebSocket and create or resume one durable session lane "
        "under a profile. Optional profile: supplied names the (profile, lane) binding exactly; "
        "omitted infers the lane's single bound profile and fails with lane_profile_ambiguous when "
        "several profiles share the lane name; an unbound lane uses the default profile. Here "
        "session_id is the stored/durable ID to resume; the response session_id is the runtime ID "
        "and stored_session_id is the durable ID. Prefer lane for subsequent reads and control calls."
    ),
    "live_prompt": (
        "Submit one prompt to a live TUI session; never retries an unknown acknowledgement. Optional "
        "profile follows live_session_open inference; the profile is carried on the submit and stored "
        "with the request. When supplied explicitly, session_id is the runtime ID; prefer lane instead "
        "of copying a stored ID."
    ),
    "live_wait": (
        "Wait for this request's own live completion with a bounded timeout. The request's stored "
        "profile governs; a supplied different profile conflicts. Lane-addressed waits infer the "
        "profile and fail closed when ambiguous. Shared-session ownership is proven via gateway "
        "inflight evidence; unprovable or foreign-turn cases return conservative "
        "ambiguous_turn/completion_not_observed states instead of another client's answer."
    ),
    "live_events": (
        "Read bounded live TUI events after an optional per-session sequence cursor. Optional profile "
        "follows live_status inference. When supplied explicitly, session_id is the runtime ID; prefer "
        "lane instead of copying a stored ID."
    ),
    "live_status": (
        "Read exact live TUI session status without submitting a prompt. Optional profile: omitted "
        "infers the lane's or stored session's single bound profile (fail-closed when ambiguous); "
        "when supplied explicitly, session_id is the runtime ID; prefer lane."
    ),
    "live_history": (
        "Read exact live TUI session history for recovery/reconciliation. Optional profile follows "
        "live_status inference. When supplied explicitly, session_id is the runtime ID; prefer lane."
    ),
    "live_steer": (
        "Queue exact-session live TUI steering text. Optional profile follows live_status inference. "
        "When supplied explicitly, session_id is the runtime ID; prefer lane instead of copying a stored ID."
    ),
    "live_interrupt": (
        "Interrupt the exact live TUI session cooperatively. Optional profile follows live_status "
        "inference. When supplied explicitly, session_id is the runtime ID; prefer lane."
    ),
    "live_reconcile": (
        "Reconcile an unknown live prompt against post-boundary durable history (pre-submit row cursor) "
        "without resubmitting it; the request's stored profile governs and no profile argument is "
        "accepted. Ambiguous matches stay unknown."
    ),
    "live_reconnect": (
        "Reconnect the live TUI WebSocket and replay retained per-session events; every remembered "
        "(profile, lane) reopens under its stored profile. Needs no profile argument."
    ),
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
            "Profile routing: an omitted profile uses the default profile, infers a lane's or session's single "
            "bound profile, and fails closed (lane_profile_ambiguous / conflict errors) instead of guessing; "
            "named profiles route through /p/<profile>/ with their own credentials and never inherit the "
            "default key. "
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
        profile: str = "",
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
            profile=profile or None,
        ))

    def run_status(lane: str = "", run_id: str = "", profile: str = "") -> str:
        """Return status for lane's latest run or the exact supplied run_id."""
        return _json_result(service.status(lane=lane or None, run_id=run_id or None, profile=profile or None))

    def run_wait(lane: str = "", run_id: str = "", timeout_seconds: float = 30.0, profile: str = "") -> str:
        """Wait a bounded time, returning wait_timeout rather than hiding an active run."""
        return _json_result(service.wait(
            lane=lane or None, run_id=run_id or None, timeout_seconds=timeout_seconds,
            profile=profile or None,
        ))

    def run_events(lane: str = "", run_id: str = "", profile: str = "") -> str:
        """Collect currently available SSE events for a lane's latest or exact run."""
        return _json_result(service.events(lane=lane or None, run_id=run_id or None, profile=profile or None))

    def run_stop(lane: str = "", run_id: str = "", profile: str = "") -> str:
        """Stop only the exact run addressed by run_id or lane's latest request."""
        return _json_result(service.stop(lane=lane or None, run_id=run_id or None, profile=profile or None))

    def run_steer(text: str, lane: str = "", run_id: str = "", profile: str = "") -> str:
        """Steer only the exact run addressed by run_id or lane's latest request."""
        return _json_result(service.steer(text=text, lane=lane or None, run_id=run_id or None, profile=profile or None))

    def session_history(session_id: str, limit: int = 100, profile: str = "") -> str:
        """Read bounded oldest-first history from an exact Hermes session ID."""
        return _json_result(service.history(session_id=session_id, limit=limit, profile=profile or None))

    def bridge_health(profile: str = "") -> str:
        """Probe health/models/capabilities without submitting an LLM turn."""
        return _json_result(service.health(profile=profile or None))

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
        profile: str = "",
    ) -> str:
        """Submit one live prompt; explicit session_id is a runtime id.

        Prefer lane; wait_seconds optionally collects the final event.
        """
        return _json_result(service.live_prompt(
            lane=lane, text=text, session_id=session_id or None,
            request_id=request_id or None, queued=queued, wait_seconds=wait_seconds,
            profile=profile or None,
        ))

    def live_wait(request_id: str = "", lane: str = "", timeout_seconds: float = 120.0, profile: str = "") -> str:
        """Wait for the exact request or latest prompt in a lane."""
        return _json_result(service.live_wait(
            request_id=request_id or None, lane=lane or None, timeout_seconds=timeout_seconds,
            profile=profile or None,
        ))

    def live_events(lane: str = "", session_id: str = "", after_seq: int = 0, profile: str = "") -> str:
        """Read buffered events; explicit session_id is a runtime id.

        Prefer lane; this does not ask the backend to replay events.
        """
        return _json_result(service.live_events(
            lane=lane or None, session_id=session_id or None, after_seq=after_seq,
            profile=profile or None,
        ))

    def live_status(lane: str = "", session_id: str = "", profile: str = "") -> str:
        """Read live status; explicit session_id is a runtime id. Prefer lane."""
        return _json_result(service.live_status(lane=lane or None, session_id=session_id or None, profile=profile or None))

    def live_history(lane: str = "", session_id: str = "", profile: str = "") -> str:
        """Read live history; explicit session_id is a runtime id. Prefer lane."""
        return _json_result(service.live_history(lane=lane or None, session_id=session_id or None, profile=profile or None))

    def live_steer(text: str, lane: str = "", session_id: str = "", profile: str = "") -> str:
        """Queue text; explicit session_id is a runtime id. Prefer lane."""
        return _json_result(service.live_steer(text=text, lane=lane or None, session_id=session_id or None, profile=profile or None))

    def live_interrupt(lane: str = "", session_id: str = "", profile: str = "") -> str:
        """Interrupt a live session; explicit session_id is a runtime id. Prefer lane."""
        return _json_result(service.live_interrupt(lane=lane or None, session_id=session_id or None, profile=profile or None))

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
