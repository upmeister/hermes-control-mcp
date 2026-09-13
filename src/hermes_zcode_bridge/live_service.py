from __future__ import annotations

import hashlib
import threading
import uuid
from typing import Any

from .live_client import LiveAuthError, LiveError, LiveGatewayClient, LiveRPCError, LiveTransportUnknown
from .registry import StateRegistry
from .service import InputError, _fingerprint, _validate_text, _visible_id


class LiveService:
    """Safe MCP-facing facade over one persistent TUI WebSocket client."""

    def __init__(self, client: LiveGatewayClient, registry: StateRegistry) -> None:
        self.client = client
        self.registry = registry
        self._lock = threading.RLock()
        # runtime ids are process-local; the durable lane table is the source
        # used to resume them after this MCP process is restarted.
        self._runtimes: dict[str, str] = {}
        self._stored_by_runtime: dict[str, str] = {}

    @staticmethod
    def _result(
        *,
        status: str,
        request_id: str | None = None,
        session_id: str | None = None,
        stored_session_id: str | None = None,
        answer: str | None = None,
        error_code: str | None = None,
        error: str | None = None,
        replayed: bool = False,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "ok": error_code is None and status not in {"failed", "unknown", "unconfigured"},
            "request_id": request_id,
            "session_id": session_id,
            "runtime_session_id": session_id,
            "stored_session_id": stored_session_id,
            "status": status,
            "answer": answer,
            "error_code": error_code,
            "error": error,
            "replayed": bool(replayed),
            **extra,
        }

    @staticmethod
    def _input_error(exc: Exception, *, request_id: str | None = None) -> dict[str, Any]:
        return LiveService._result(status="failed", request_id=request_id, error_code="invalid_input", error=str(exc))

    def _error(self, exc: Exception, *, request_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        # Safe snapshot only: health intentionally omits endpoint and all
        # credential values, but explains auth-vs-network failures.
        connection = self._connection_snapshot(self.client)
        if isinstance(exc, LiveTransportUnknown):
            return LiveService._result(
                status="unknown", request_id=request_id, session_id=session_id,
                error_code="transport_unknown", error=str(exc), connection=connection,
            )
        if isinstance(exc, LiveRPCError):
            rpc_code = str(exc.rpc_code) if exc.rpc_code is not None else "unknown"
            return LiveService._result(
                status="failed", request_id=request_id, session_id=session_id,
                error_code=f"rpc_{rpc_code}", error=str(exc), rpc_data=exc.data, connection=connection,
            )
        if isinstance(exc, LiveAuthError):
            return LiveService._result(
                status="failed", request_id=request_id, session_id=session_id,
                error_code=exc.code, error=str(exc), connection=connection,
            )
        code = exc.code if isinstance(exc, LiveError) else "live_error"
        return LiveService._result(
            status="failed", request_id=request_id, session_id=session_id,
            error_code=code, error=str(exc), connection=connection,
        )

    @staticmethod
    def _connection_snapshot(client: Any) -> dict[str, Any] | None:
        if client is None:
            return None
        try:
            health = client.health()
            return {
                "connection_state": health.get("connection_state"),
                "auth_mode": health.get("auth_mode"),
                "replay_epoch": health.get("replay_epoch"),
                "last_replay": health.get("last_replay"),
            }
        except Exception:
            return None

    def _ensure_connected(self) -> dict[str, Any] | None:
        try:
            self.client.connect()
        except LiveError as exc:
            return self._error(exc)
        return None

    @staticmethod
    def _optional_text(value: Any, field: str, *, max_length: int = 255) -> str | None:
        if value is None or value == "":
            return None
        return _validate_text(str(value), field, max_length=max_length)

    def _remember_runtime(self, lane: str, runtime: str, stored: str) -> None:
        with self._lock:
            self._runtimes[lane] = runtime
            self._stored_by_runtime[runtime] = stored
        # Runtime ids belong to one TUI gateway process. A reconnect or backend
        # restart may mint a new runtime id for the same durable lane; update
        # only the redacted identity records, never a stored prompt.
        for request in self.registry.live_requests_for_lane(lane):
            if request.get("runtime_session_id") != runtime:
                self.registry.update_live_request(request["request_id"], runtime_session_id=runtime)

    def _runtime_for_lane(self, lane: str, supplied: str | None = None, *, reopen: bool = False) -> tuple[str | None, dict[str, Any] | None]:
        with self._lock:
            runtime = supplied or self._runtimes.get(lane)
        if runtime:
            return runtime, None
        if reopen and self.registry.session_for_lane(lane):
            opened = self.open(lane=lane)
            if opened.get("ok"):
                return str(opened["session_id"]), None
            return None, opened
        return None, self._result(status="failed", error_code="live_session_not_open", error="Call live_session_open first")

    def open(
        self,
        *,
        lane: str,
        session_id: str | None = None,
        title: str | None = None,
        cwd: str | None = None,
        profile: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        close_on_disconnect: bool = False,
    ) -> dict[str, Any]:
        try:
            lane = _validate_text(lane, "lane", max_length=200)
            supplied = _visible_id(session_id, "session_id") if session_id else None
            current = self.registry.session_for_lane(lane)
            if supplied and current and supplied != current:
                return self._result(
                    status="failed", error_code="lane_session_conflict",
                    error="The lane is already bound to a different durable session_id",
                )
            connected = self._ensure_connected()
            if connected is not None:
                return connected
            target = supplied or current
            if target:
                reply = self.client.request("session.resume", {"session_id": target})
            else:
                params: dict[str, Any] = {
                    "source": "tool", "close_on_disconnect": bool(close_on_disconnect),
                }
                for key, value in (
                    ("title", title), ("cwd", cwd), ("profile", profile),
                    ("model", model), ("provider", provider),
                ):
                    if value not in (None, ""):
                        params[key] = self._optional_text(value, key, max_length=100_000 if key == "cwd" else 1000)
                reply = self.client.request("session.create", params)
            if not isinstance(reply, dict):
                return self._result(status="failed", error_code="invalid_response", error="Hermes live session response was not an object")
            runtime = reply.get("session_id")
            stored = reply.get("stored_session_id") or reply.get("resumed") or target
            if not isinstance(runtime, str) or not runtime or not isinstance(stored, str) or not stored:
                return self._result(status="failed", error_code="invalid_response", error="Hermes live session response omitted session identity")
            self.registry.bind_lane(lane, stored)
            self._remember_runtime(lane, runtime, stored)
            result = self._result(
                status="connected", session_id=runtime, stored_session_id=stored,
                messages=reply.get("messages") if isinstance(reply.get("messages"), list) else [],
                message_count=reply.get("message_count"), info=reply.get("info") or {},
                replay=self.client.health().get("last_replay") or {},
            )
            return result
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc)
        except LiveError as exc:
            return self._error(exc)

    def prompt(
        self,
        *,
        lane: str,
        text: str,
        session_id: str | None = None,
        request_id: str | None = None,
        queued: bool = False,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        try:
            lane = _validate_text(lane, "lane", max_length=200)
            text = _validate_text(text, "text", max_length=100_000, allow_common_whitespace=True)
            if not text.strip():
                raise InputError("text must not be blank")
            wait = max(0.0, min(float(wait_seconds), 3600.0))
            runtime, error = self._runtime_for_lane(lane, session_id)
            if error is not None:
                return error
            request_id = _visible_id(request_id, "request_id", max_length=128) if request_id else f"live_req_{uuid.uuid4().hex}"
            durable_session = self.registry.session_for_lane(lane) or self._stored_by_runtime.get(runtime or "") or runtime
            fingerprint = _fingerprint(lane, {
                "session_id": durable_session, "text": text, "queued": bool(queued),
            })
            prompt_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc, request_id=request_id)

        record = self.registry.live_request_by_id(request_id)
        if record is not None:
            if record.get("fingerprint") != fingerprint or record.get("lane") != lane:
                return self._result(
                    status="failed", request_id=request_id, session_id=runtime,
                    error_code="request_id_conflict", error="request_id was already used with a different live prompt",
                )
            return self._result(
                status=str(record.get("status") or "unknown"), request_id=request_id,
                session_id=record.get("runtime_session_id") or runtime,
                stored_session_id=record.get("session_id"),
                error_code=record.get("error_code") or ("transport_unknown" if record.get("status") == "unknown" else None),
                error="Reconcile this request with live_reconcile; it was not submitted again" if record.get("status") == "unknown" else None,
                replayed=True, event_cursor=record.get("start_seq"),
            )

        start_seq = self.client.watermarks().get(runtime or "", 0)
        self.registry.save_live_request(
            request_id=request_id, lane=lane, session_id=self.registry.session_for_lane(lane) or runtime or "",
            prompt_sha256=prompt_sha, fingerprint=fingerprint, status="pending",
            runtime_session_id=runtime, start_seq=start_seq,
        )
        try:
            params: dict[str, Any] = {"session_id": runtime, "text": text}
            if queued:
                params["queued"] = True
            reply = self.client.request("prompt.submit", params)
            if not isinstance(reply, dict):
                raise LiveError("Hermes live prompt response was not an object", code="invalid_response")
            status = str(reply.get("status") or "streaming")
            self.registry.update_live_request(request_id, status=status, runtime_session_id=runtime, start_seq=start_seq, error_code=None)
            result = self._result(
                status=status, request_id=request_id, session_id=runtime,
                stored_session_id=self.registry.session_for_lane(lane), event_cursor=start_seq,
            )
            if wait > 0 and status in {"streaming", "queued"}:
                return self.wait(request_id=request_id, timeout_seconds=wait)
            return result
        except LiveError as exc:
            status = "unknown" if isinstance(exc, LiveTransportUnknown) else "failed"
            self.registry.update_live_request(request_id, status=status, error_code=exc.code)
            return self._error(exc, request_id=request_id, session_id=runtime)

    def wait(
        self, *, request_id: str | None = None, lane: str | None = None, timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        try:
            if request_id:
                request_id = _visible_id(request_id, "request_id", max_length=128)
                record = self.registry.live_request_by_id(request_id)
            elif lane:
                lane = _validate_text(lane, "lane", max_length=200)
                record = self.registry.latest_live_request_for_lane(lane)
                request_id = str(record.get("request_id")) if record else None
            else:
                raise InputError("request_id or lane is required")
            if record is None:
                return self._result(status="failed", request_id=request_id, error_code="request_not_found", error="No live prompt matches the request")
            if record.get("status") == "unknown":
                return self._result(
                    status="unknown", request_id=request_id,
                    session_id=record.get("runtime_session_id"), stored_session_id=record.get("session_id"),
                    error_code="transport_unknown", error="Reconcile this request before attempting any retry",
                    replayed=True,
                )
            runtime = str(record.get("runtime_session_id") or "")
            if not runtime:
                return self._result(status="failed", request_id=request_id, error_code="live_session_not_open", error="Live prompt has no runtime session")
            timeout = max(0.0, min(float(timeout_seconds), 3600.0))
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc, request_id=request_id)
        try:
            event = self.client.wait_for_completion(runtime, after_seq=int(record.get("start_seq") or 0), timeout=timeout)
        except LiveError as exc:
            return self._error(exc, request_id=request_id, session_id=runtime)
        if event is None:
            return self._result(
                status="running", request_id=request_id, session_id=runtime,
                stored_session_id=record.get("session_id"), error_code="wait_timeout",
                error="Live prompt is still active; call live_wait or live_events again",
            )
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        raw_status = str(payload.get("status") or "complete")
        status = {"complete": "completed", "error": "failed", "cancelled": "interrupted"}.get(raw_status, raw_status)
        error_code = "live_turn_failed" if status == "failed" else None
        self.registry.update_live_request(request_id, status=status, error_code=error_code)
        return self._result(
            status=status, request_id=request_id, session_id=runtime,
            stored_session_id=record.get("session_id"), answer=payload.get("text") if isinstance(payload.get("text"), str) else None,
            error_code=error_code, error=str(payload.get("error")) if payload.get("error") else None,
            event=event, event_seq=event.get("seq"),
        )

    def events(self, *, lane: str | None = None, session_id: str | None = None, after_seq: int = 0) -> dict[str, Any]:
        try:
            lane = _validate_text(lane, "lane", max_length=200) if lane else ""
            runtime, error = self._runtime_for_lane(lane, session_id, reopen=bool(lane))
            if error is not None:
                return error
            after = max(0, int(after_seq))
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc)
        events = self.client.events(runtime or "", after_seq=after)
        return self._result(
            status="connected", session_id=runtime,
            events=events, event_count=len(events), after_seq=after,
            latest_seq=self.client.watermarks().get(runtime or "", 0),
            truncated=self.client.events_truncated(runtime or "", after_seq=after),
            replay_epoch=self.client.health().get("replay_epoch"),
        )

    def status(self, *, lane: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        try:
            lane = _validate_text(lane, "lane", max_length=200) if lane else ""
            runtime, error = self._runtime_for_lane(lane, session_id, reopen=bool(lane))
            if error is not None:
                return error
            reply = self.client.request("session.status", {"session_id": runtime})
            return self._result(status="connected", session_id=runtime, stored_session_id=self._stored_by_runtime.get(runtime or ""), gateway_result=reply)
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc)
        except LiveError as exc:
            return self._error(exc, session_id=session_id)

    def history(self, *, lane: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        try:
            lane = _validate_text(lane, "lane", max_length=200) if lane else ""
            runtime, error = self._runtime_for_lane(lane, session_id, reopen=bool(lane))
            if error is not None:
                return error
            reply = self.client.request("session.history", {"session_id": runtime})
            messages = reply.get("messages", []) if isinstance(reply, dict) else []
            return self._result(status="connected", session_id=runtime, stored_session_id=self._stored_by_runtime.get(runtime or ""), messages=messages, message_count=len(messages), gateway_result=reply)
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc)
        except LiveError as exc:
            return self._error(exc, session_id=session_id)

    def steer(self, *, text: str, lane: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        try:
            text = _validate_text(text, "text", max_length=100_000, allow_common_whitespace=True)
            if not text.strip():
                raise InputError("text must not be blank")
            lane = _validate_text(lane, "lane", max_length=200) if lane else ""
            runtime, error = self._runtime_for_lane(lane, session_id)
            if error is not None:
                return error
            reply = self.client.request("session.steer", {"session_id": runtime, "text": text})
            return self._result(status=str(reply.get("status") or "queued") if isinstance(reply, dict) else "queued", session_id=runtime, gateway_result=reply)
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc)
        except LiveError as exc:
            return self._error(exc, session_id=session_id)

    def interrupt(self, *, lane: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        try:
            lane = _validate_text(lane, "lane", max_length=200) if lane else ""
            runtime, error = self._runtime_for_lane(lane, session_id)
            if error is not None:
                return error
            reply = self.client.request("session.interrupt", {"session_id": runtime})
            return self._result(status="interrupted", session_id=runtime, gateway_result=reply)
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc)
        except LiveError as exc:
            return self._error(exc, session_id=session_id)

    def reconcile(self, *, request_id: str) -> dict[str, Any]:
        try:
            request_id = _visible_id(request_id, "request_id", max_length=128)
            record = self.registry.live_request_by_id(request_id)
            if record is None:
                return self._result(status="failed", request_id=request_id, error_code="request_not_found", error="No live prompt matches the request")
            if record.get("status") != "unknown":
                return self._result(
                    status=str(record.get("status") or "unknown"), request_id=request_id,
                    session_id=record.get("runtime_session_id"), stored_session_id=record.get("session_id"), replayed=True,
                    error_code=record.get("error_code"),
                )
            lane = str(record.get("lane") or "")
            opened = self.open(lane=lane)
            if not opened.get("ok"):
                return opened
            runtime = str(opened["session_id"])
            history = self.history(session_id=runtime)
            if not history.get("ok"):
                return history
            messages = history.get("messages") if isinstance(history.get("messages"), list) else []
            match = any(
                hashlib.sha256(str(message.get("content", "")).encode("utf-8")).hexdigest() == record.get("prompt_sha256")
                for message in messages if isinstance(message, dict) and message.get("role") == "user"
            )
            if match:
                self.registry.update_live_request(request_id, status="reconciled", runtime_session_id=runtime, error_code=None)
                return self._result(
                    status="reconciled", request_id=request_id, session_id=runtime,
                    stored_session_id=record.get("session_id"), reconciliation="history_match",
                    warning="History match proves the text was persisted, but identical repeated prompts cannot be distinguished.",
                )
            return self._result(
                status="unknown", request_id=request_id, session_id=runtime,
                stored_session_id=record.get("session_id"), error_code="transport_unknown",
                error="The prompt was not found in durable history; it was not submitted again.",
                reconciliation="not_observed",
            )
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc, request_id=request_id)
        except LiveError as exc:
            return self._error(exc, request_id=request_id)

    def reconnect(self) -> dict[str, Any]:
        try:
            replay = self.client.reconnect()
            resumed: dict[str, Any] = {}
            with self._lock:
                lanes = list(self._runtimes)
            for lane in lanes:
                stored = self.registry.session_for_lane(lane)
                if not stored:
                    continue
                opened = self.open(lane=lane, session_id=stored)
                resumed[lane] = {
                    "ok": opened.get("ok"), "session_id": opened.get("session_id"),
                    "stored_session_id": opened.get("stored_session_id"), "error_code": opened.get("error_code"),
                }
            replay_errors = replay.get("errors") if isinstance(replay, dict) else []
            replay_truncated = replay.get("truncated") if isinstance(replay, dict) else []
            degraded = bool(replay_errors or replay_truncated or (isinstance(replay, dict) and replay.get("epoch_changed")))
            return self._result(
                status="degraded" if degraded else "connected",
                error_code=("replay_truncated" if replay_truncated else "replay_partial") if degraded else None,
                error=("Some live events were evicted; use live_history for authoritative recovery" if replay_truncated
                       else "Live connection recovered but replay was partial; use live_status/history" if degraded else None),
                replay=replay, resumed=resumed, connection=self.client.health(),
            )
        except LiveError as exc:
            return self._error(exc)

    def health(self) -> dict[str, Any]:
        if not self.client.config.gateway_url:
            return self._result(
                status="unconfigured", error_code="gateway_not_configured",
                error="gateway_url is not configured; live tools are disabled",
                connection=self.client.health(),
            )
        if self.client.state != "open":
            try:
                self.client.connect()
            except LiveError as exc:
                return self._error(exc)
        return self._result(status="healthy", connection=self.client.health())

    def close(self) -> dict[str, Any]:
        try:
            self.client.close()
            return self._result(status="closed", connection=self.client.health())
        except LiveError as exc:
            return self._error(exc)

    def shutdown(self) -> None:
        self.client.shutdown()
