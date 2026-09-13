from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from .api import APIError, HermesAPIClient
from .registry import StateRegistry


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})


class InputError(ValueError):
    """Caller input is unsafe or incomplete."""


def _validate_text(
    value: str, field: str, *, max_length: int = 255, required: bool = True,
    allow_common_whitespace: bool = False,
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InputError(f"{field} must be a string")
    if required and not value:
        raise InputError(f"{field} must not be empty")
    if len(value) > max_length:
        raise InputError(f"{field} is too long")
    allowed = "\t\n\r" if allow_common_whitespace else ""
    if any((ord(char) < 32 and char not in allowed) or ord(char) == 127 for char in value):
        raise InputError(f"{field} contains control characters")
    return value


def _visible_id(value: str, field: str, *, max_length: int = 255) -> str:
    checked = _validate_text(value, field, max_length=max_length)
    if any(ord(char) < 33 or ord(char) > 126 for char in checked):
        raise InputError(f"{field} must contain visible ASCII characters")
    return checked


def _safe_refs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:500] for item in value if isinstance(item, str) and item][:50]


def _fingerprint(lane: str, body: dict[str, Any]) -> str:
    # The prompt is hashed, never persisted in the bridge registry.
    encoded = json.dumps({"lane": lane, "body": body}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _record_error(exc: APIError) -> tuple[str, str | None]:
    return ("unknown" if exc.code == "transport_unknown" else "failed", exc.code)


class BridgeService:
    """Orchestrate safe lane/request state over the Hermes runs API."""

    def __init__(self, client: HermesAPIClient, registry: StateRegistry):
        self.client = client
        self.registry = registry
        from .live_client import LiveGatewayClient
        from .live_service import LiveService
        self.live = LiveService(LiveGatewayClient(client.config), registry)

    @staticmethod
    def _result(
        *,
        status: str,
        request_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        answer: str | None = None,
        error_code: str | None = None,
        error: str | None = None,
        replayed: bool = False,
        **extra: Any,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": error_code is None and status not in {"failed", "unknown"},
            "request_id": request_id,
            "run_id": run_id,
            "session_id": session_id,
            "status": status,
            "answer": answer,
            "error_code": error_code,
            "error": error,
            "replayed": bool(replayed),
            "artifact_refs": _safe_refs(extra.pop("artifact_refs", [])),
            "commit_refs": _safe_refs(extra.pop("commit_refs", [])),
        }
        result.update(extra)
        return result

    @staticmethod
    def _input_error(exc: Exception, *, request_id: str | None = None) -> dict[str, Any]:
        return BridgeService._result(
            status="failed", request_id=request_id, error_code="invalid_input", error=str(exc)
        )

    @staticmethod
    def _api_error(exc: APIError, *, request_id: str | None = None, run_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        status, code = _record_error(exc)
        return BridgeService._result(
            status=status, request_id=request_id, run_id=run_id, session_id=session_id,
            error_code=code, error=str(exc)
        )

    def _lane_session(self, lane: str, supplied: str | None) -> tuple[str | None, dict[str, Any] | None]:
        selected = supplied if supplied else self.registry.session_for_lane(lane)
        if selected is not None:
            selected = _validate_text(selected, "session_id")
        current = self.registry.session_for_lane(lane)
        if supplied and current and supplied != current:
            return None, self._result(
                status="failed", error_code="lane_session_conflict",
                error="The lane is already bound to a different session_id"
            )
        return selected, None

    def _existing_result(self, record: dict[str, Any], *, replayed: bool = True) -> dict[str, Any]:
        return self._result(
            status=str(record.get("status") or "unknown"),
            request_id=str(record.get("request_id")),
            run_id=record.get("run_id"),
            session_id=record.get("session_id"),
            error_code=record.get("error_code"),
            replayed=replayed,
        )

    def start(
        self,
        *,
        lane: str,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        instructions: str | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        try:
            lane = _validate_text(lane, "lane", max_length=200)
            prompt = _validate_text(prompt, "prompt", max_length=100_000, allow_common_whitespace=True)
            if not prompt.strip():
                raise InputError("prompt must not be blank")
            if session_id:
                session_id = _validate_text(session_id, "session_id")
            if model:
                model = _validate_text(model, "model")
            if provider:
                provider = _validate_text(provider, "provider")
            if instructions:
                instructions = _validate_text(
                    instructions, "instructions", max_length=100_000, allow_common_whitespace=True
                )
            request_id = _visible_id(request_id, "request_id", max_length=128) if request_id else f"req_{uuid.uuid4().hex}"
            selected_session, lane_error = self._lane_session(lane, session_id)
            if lane_error is not None:
                lane_error["request_id"] = request_id
                return lane_error
            idempotency_key = (
                _visible_id(idempotency_key, "idempotency_key") if idempotency_key else f"bridge:{request_id}"
            )
            if len(idempotency_key) > 255:
                raise InputError("idempotency_key is too long")
            body_for_fingerprint: dict[str, Any] = {"input": prompt}
            for key, value in (
                ("session_id", session_id), ("model", model), ("provider", provider), ("instructions", instructions)
            ):
                if value:
                    body_for_fingerprint[key] = value
            fingerprint = _fingerprint(lane, body_for_fingerprint)
        except InputError as exc:
            return self._input_error(exc, request_id=request_id)

        existing = self.registry.request_by_id(request_id)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                return self._result(
                    status="failed", request_id=request_id, run_id=existing.get("run_id"),
                    session_id=existing.get("session_id"), error_code="request_id_conflict",
                    error="request_id was already used with a different request payload"
                )
            # A normal duplicate is a local replay. Unknown/pending records are
            # intentionally sent below with their original Idempotency-Key.
            if existing.get("status") not in {"unknown", "pending"} and existing.get("run_id"):
                return self._existing_result(existing)
            if existing.get("status") == "unknown" and existing.get("run_id"):
                try:
                    reconciled = self.status(str(existing["run_id"]))
                    if reconciled.get("error_code") is None:
                        return reconciled
                except Exception:
                    pass
            idempotency_key = str(existing["idempotency_key"])
        else:
            by_key = self.registry.request_by_idempotency(idempotency_key)
            if by_key is not None and by_key.get("request_id") != request_id:
                if by_key.get("fingerprint") == fingerprint:
                    return self._existing_result(by_key)
                return self._result(
                    status="failed", request_id=request_id, error_code="idempotency_key_conflict",
                    error="idempotency_key was already used with a different request payload"
                )
            self.registry.save_request(
                request_id=request_id, lane=lane, session_id=selected_session, run_id=None,
                idempotency_key=idempotency_key, fingerprint=fingerprint, status="pending"
            )

        try:
            payload = self.client.submit_run(
                prompt=prompt, idempotency_key=idempotency_key, session_id=selected_session,
                model=model, provider=provider, instructions=instructions,
            )
        except APIError as exc:
            status, code = _record_error(exc)
            self.registry.update_request(request_id, status=status, error_code=code)
            return self._api_error(exc, request_id=request_id, session_id=selected_session)

        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            self.registry.update_request(request_id, status="unknown", error_code="invalid_response")
            return self._result(
                status="unknown", request_id=request_id, session_id=selected_session,
                error_code="invalid_response", error="Hermes API did not return a run_id"
            )
        response_session = payload.get("session_id")
        effective_session = response_session if isinstance(response_session, str) and response_session else selected_session
        # Current Hermes uses run_id as the session fallback when no session was
        # supplied. Status/history can later replace it if a future server returns
        # a distinct durable session id.
        if effective_session is None:
            effective_session = run_id
        status = str(payload.get("status") or "started")
        replayed = bool(payload.get("replayed"))
        self.registry.update_request(
            request_id, status=status, run_id=run_id, session_id=effective_session,
            error_code=None
        )
        self.registry.bind_lane(lane, effective_session)
        return self._result(
            status=status, request_id=request_id, run_id=run_id, session_id=effective_session,
            answer=payload.get("output") if isinstance(payload.get("output"), str) else None,
            replayed=replayed,
            artifact_refs=payload.get("artifact_refs"), commit_refs=payload.get("commit_refs")
        )

    def _resolve_run(self, *, lane: str | None = None, run_id: str | None = None) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
        if run_id:
            run_id = _validate_text(run_id, "run_id")
            return run_id, self.registry.request_by_run(run_id), None
        if lane:
            lane = _validate_text(lane, "lane", max_length=200)
            record = self.registry.latest_request_for_lane(lane)
            if record and record.get("run_id"):
                return str(record["run_id"]), record, None
        return None, None, self._result(status="failed", error_code="run_not_found_local", error="No local run matches the supplied lane/run_id")

    @staticmethod
    def _server_result(payload: dict[str, Any], *, request_id: str | None, known_run_id: str | None, known_session_id: str | None, replayed: bool = False) -> dict[str, Any]:
        run_id = payload.get("run_id") if isinstance(payload.get("run_id"), str) else known_run_id
        session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else known_session_id
        raw_error = payload.get("error")
        if isinstance(raw_error, dict):
            error_text = str(raw_error.get("message") or raw_error)
            error_code = str(raw_error.get("code")) if raw_error.get("code") else None
        else:
            error_text = str(raw_error) if raw_error else None
            error_code = str(payload.get("error_code")) if payload.get("error_code") else None
        answer = payload.get("output") if isinstance(payload.get("output"), str) else payload.get("answer")
        return BridgeService._result(
            status=str(payload.get("status") or "unknown"), request_id=request_id, run_id=run_id,
            session_id=session_id, answer=answer if isinstance(answer, str) else None,
            error_code=error_code, error=error_text, replayed=replayed,
            artifact_refs=payload.get("artifact_refs"), commit_refs=payload.get("commit_refs")
        )

    def status(self, *, lane: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        try:
            resolved_run, record, local_error = self._resolve_run(lane=lane, run_id=run_id)
        except InputError as exc:
            return self._input_error(exc)
        if local_error is not None:
            return local_error
        request_id = record.get("request_id") if record else None
        known_session = record.get("session_id") if record else None
        try:
            payload = self.client.status(str(resolved_run))
        except APIError as exc:
            return self._api_error(exc, request_id=request_id, run_id=resolved_run, session_id=known_session)
        result = self._server_result(
            payload, request_id=str(request_id) if request_id else None,
            known_run_id=resolved_run, known_session_id=known_session
        )
        if record:
            fields: dict[str, Any] = {"status": result["status"]}
            if result.get("session_id"):
                fields["session_id"] = result["session_id"]
            if result.get("error_code"):
                fields["error_code"] = result["error_code"]
            self.registry.update_request(str(request_id), **fields)
            if result.get("session_id"):
                self.registry.bind_lane(str(record["lane"]), str(result["session_id"]))
        return result

    def wait(
        self, *, lane: str | None = None, run_id: str | None = None,
        timeout_seconds: float = 30.0, poll_interval_seconds: float | None = None
    ) -> dict[str, Any]:
        try:
            timeout = max(0.0, min(float(timeout_seconds), 3600.0))
            interval = max(0.05, min(float(poll_interval_seconds or self.client.config.poll_interval), 10.0))
        except (TypeError, ValueError) as exc:
            return self._input_error(InputError("timeout_seconds and poll_interval_seconds must be numbers"))
        deadline = time.monotonic() + timeout
        first = True
        latest: dict[str, Any] | None = None
        while first or time.monotonic() <= deadline:
            first = False
            latest = self.status(lane=lane, run_id=run_id)
            if latest.get("status") in TERMINAL_STATUSES or latest.get("error_code"):
                return latest
            if time.monotonic() >= deadline:
                break
            time.sleep(interval)
        assert latest is not None
        latest["ok"] = False
        latest["error_code"] = "wait_timeout"
        latest["error"] = "Run is still active; call run_wait again or run_status to reconcile"
        return latest

    def events(self, *, lane: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        try:
            resolved_run, record, local_error = self._resolve_run(lane=lane, run_id=run_id)
        except InputError as exc:
            return self._input_error(exc)
        if local_error is not None:
            return local_error
        try:
            events = self.client.events(str(resolved_run))
        except APIError as exc:
            return self._api_error(
                exc, request_id=record.get("request_id") if record else None,
                run_id=resolved_run, session_id=record.get("session_id") if record else None
            )
        terminal = next((event for event in reversed(events) if str(event.get("event", "")).startswith("run.")), {})
        result = self._server_result(
            terminal, request_id=record.get("request_id") if record else None,
            known_run_id=resolved_run, known_session_id=record.get("session_id") if record else None
        )
        result["events"] = events
        result["event_count"] = len(events)
        return result

    def stop(self, *, lane: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        try:
            resolved_run, record, local_error = self._resolve_run(lane=lane, run_id=run_id)
        except InputError as exc:
            return self._input_error(exc)
        if local_error is not None:
            return local_error
        try:
            payload = self.client.stop(str(resolved_run))
        except APIError as exc:
            return self._api_error(exc, request_id=record.get("request_id") if record else None, run_id=resolved_run, session_id=record.get("session_id") if record else None)
        result = self._server_result(payload, request_id=record.get("request_id") if record else None, known_run_id=resolved_run, known_session_id=record.get("session_id") if record else None)
        if record:
            self.registry.update_request(str(record["request_id"]), status=result["status"])
        return result

    def steer(self, *, text: str, lane: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        try:
            text = _validate_text(text, "text", max_length=100_000, allow_common_whitespace=True)
            if not text.strip():
                raise InputError("text must not be blank")
            resolved_run, record, local_error = self._resolve_run(lane=lane, run_id=run_id)
        except InputError as exc:
            return self._input_error(exc)
        if local_error is not None:
            return local_error
        try:
            payload = self.client.steer(str(resolved_run), text)
        except APIError as exc:
            return self._api_error(exc, request_id=record.get("request_id") if record else None, run_id=resolved_run, session_id=record.get("session_id") if record else None)
        result = self._server_result(payload, request_id=record.get("request_id") if record else None, known_run_id=resolved_run, known_session_id=record.get("session_id") if record else None)
        if result["status"] == "unknown":
            result["status"] = "running"
        return result

    def history(self, *, session_id: str, limit: int = 100) -> dict[str, Any]:
        try:
            session_id = _validate_text(session_id, "session_id")
            limit = max(1, min(int(limit), 500))
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc if isinstance(exc, Exception) else InputError(str(exc)))
        try:
            payload = self.client.history(session_id, limit=limit)
        except APIError as exc:
            return self._api_error(exc, session_id=session_id)
        messages = payload.get("data", []) if isinstance(payload.get("data", []), list) else []
        return self._result(
            status="completed", session_id=str(payload.get("session_id") or session_id),
            messages=messages, pagination=payload.get("pagination") or {}
        )

    def health(self) -> dict[str, Any]:
        try:
            health = self.client.health()
            models = self.client.models()
            capabilities = self.client.capabilities()
        except APIError as exc:
            return self._api_error(exc)
        return self._result(
            status="healthy", health=health, models=models, capabilities=capabilities
        )

    # The live surface is kept in live_service.py so the durable-runs facade
    # does not become a second protocol implementation.
    def live_session_open(self, **kwargs: Any) -> dict[str, Any]:
        return self.live.open(**kwargs)

    def live_prompt(self, **kwargs: Any) -> dict[str, Any]:
        return self.live.prompt(**kwargs)

    def live_wait(self, **kwargs: Any) -> dict[str, Any]:
        return self.live.wait(**kwargs)

    def live_events(self, **kwargs: Any) -> dict[str, Any]:
        return self.live.events(**kwargs)

    def live_status(self, **kwargs: Any) -> dict[str, Any]:
        return self.live.status(**kwargs)

    def live_history(self, **kwargs: Any) -> dict[str, Any]:
        return self.live.history(**kwargs)

    def live_steer(self, **kwargs: Any) -> dict[str, Any]:
        return self.live.steer(**kwargs)

    def live_interrupt(self, **kwargs: Any) -> dict[str, Any]:
        return self.live.interrupt(**kwargs)

    def live_reconcile(self, **kwargs: Any) -> dict[str, Any]:
        return self.live.reconcile(**kwargs)

    def live_reconnect(self) -> dict[str, Any]:
        return self.live.reconnect()

    def live_health(self) -> dict[str, Any]:
        return self.live.health()

    def close(self) -> None:
        self.live.shutdown()
