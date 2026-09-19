from __future__ import annotations

import hashlib
import threading
import time
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

    # ----- shared-turn ownership evidence ---------------------------------

    def _running_turn_snapshot(self, runtime: str) -> dict[str, Any] | None:
        """Read the gateway's live turn state via ``session.activate``.

        Source contract (deployed Hermes 90f1126b): the activate payload carries
        ``running`` and an ``inflight`` snapshot whose ``user`` field is the
        running turn's prompt text. Activation attaches this client without
        displacing existing subscribers, so polling is safe while Desktop/TUI
        stays attached. Returns None when the gateway evidence is unavailable;
        callers must then stay conservative.
        """
        try:
            reply = self.client.request(
                "session.activate",
                {"session_id": runtime, "omit_messages": True},
                timeout=min(10.0, self.client.config.gateway_request_timeout),
            )
        except LiveError:
            return None
        if not isinstance(reply, dict):
            return None
        inflight = reply.get("inflight")
        error_marker = False
        if isinstance(inflight, dict):
            error_marker = bool(inflight.get("error")) or str(inflight.get("status") or "") == "error"
        return {
            "running": bool(reply.get("running")),
            "inflight_user": inflight.get("user") if isinstance(inflight, dict) else None,
            "inflight_error": error_marker,
        }

    @staticmethod
    def _inflight_matches(snapshot: dict[str, Any], inflight_sha256: str) -> bool:
        user = snapshot.get("inflight_user")
        if not isinstance(user, str):
            return False
        return hashlib.sha256(user.encode("utf-8")).hexdigest() == inflight_sha256

    def _prove_running_claim(self, runtime: str, inflight_sha256: str) -> tuple[int, str, int] | None:
        """Prove the currently running turn was claimed by our own submit.

        The submit ack was ``streaming``, so the gateway claimed our turn. The
        inflight prompt text must hash to our submitted text; only then is the
        running turn provably ours. Returns the post-proof replay watermark,
        epoch, and connection generation so later completions can be ordered
        after the proof and invalidated by any reconnect.
        """
        snapshot = self._running_turn_snapshot(runtime)
        if snapshot is None or not snapshot["running"] or not self._inflight_matches(snapshot, inflight_sha256):
            return None
        health = self.client.health()
        epoch = health.get("replay_epoch")
        if not isinstance(epoch, str) or not epoch:
            return None
        return self.client.watermarks().get(runtime, 0), epoch, self.client.connection_generation()

    def _capture_boundary(self, runtime: str) -> tuple[int | None, int | None]:
        """Capture the redacted pre-submit durable boundary for reconciliation.

        Reads ``session.history`` (durable rows) and stores only the highest
        user ``row_id`` plus the row count — never prompt or body text. A
        boundary of 0 is a valid empty-history boundary; None means the
        boundary is unknown and reconciliation must stay conservative.
        """
        try:
            reply = self.client.request("session.history", {"session_id": runtime})
        except LiveError:
            return None, None
        messages = reply.get("messages") if isinstance(reply, dict) else None
        if not isinstance(messages, list):
            return None, None
        max_row_id = 0
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            row_id = message.get("row_id")
            if isinstance(row_id, int) and not isinstance(row_id, bool) and row_id > max_row_id:
                max_row_id = row_id
        count = reply.get("count")
        return max_row_id, count if isinstance(count, int) else None

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
            activated = self.client.request("session.activate", {"session_id": runtime, "omit_messages": True})
            if not isinstance(activated, dict) or activated.get("session_id") != runtime:
                return self._result(
                    status="failed", error_code="invalid_response",
                    error="Hermes live session activation did not confirm the runtime session identity",
                )
            activated_stored = activated.get("stored_session_id") or stored
            if not isinstance(activated_stored, str) or not activated_stored:
                return self._result(status="failed", error_code="invalid_response", error="Hermes live session activation omitted stored identity")
            stored = activated_stored
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
            # The gateway strips the running turn's user text into the inflight
            # snapshot; hash the stripped form so the claim proof compares equal.
            inflight_sha = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc, request_id=request_id)

        start_seq = self.client.watermarks().get(runtime or "", 0)
        boundary_row_id, boundary_count = self._capture_boundary(runtime or "")
        reserved = self.registry.reserve_live_request({
            "request_id": request_id, "lane": lane,
            "session_id": self.registry.session_for_lane(lane) or runtime or "",
            "prompt_sha256": prompt_sha, "fingerprint": fingerprint, "status": "pending",
            "runtime_session_id": runtime, "start_seq": start_seq, "error_code": None,
            "inflight_sha256": inflight_sha,
            "boundary_row_id": boundary_row_id, "boundary_count": boundary_count,
            "created_at": time.time(), "updated_at": time.time(),
        })
        if not reserved:
            # Another call atomically won this request_id first. Exactly one
            # submit happens per request_id even under concurrency: the loser
            # replays the winner's record or conflicts, never resubmits.
            record = self.registry.live_request_by_id(request_id)
            if record is None:
                return self._result(
                    status="failed", request_id=request_id, session_id=runtime,
                    error_code="request_id_conflict", error="request_id reservation state is unavailable",
                )
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
                attribution=record.get("attribution"),
            )
        try:
            params: dict[str, Any] = {"session_id": runtime, "text": text}
            if queued:
                params["queued"] = True
            reply = self.client.request("prompt.submit", params)
            if not isinstance(reply, dict):
                raise LiveError("Hermes live prompt response was not an object", code="invalid_response")
            status = str(reply.get("status") or "streaming")
            # Ownership proof: only a "streaming" ack means the gateway claimed
            # OUR turn; the inflight snapshot must then hash to our prompt.
            # Queued (or any other) ack leaves the next completion unattributable.
            attribution = "unproven"
            proof_seq: int | None = None
            proof_epoch: str | None = None
            proof_generation: int | None = None
            if status == "streaming":
                proof = self._prove_running_claim(runtime or "", inflight_sha)
                if proof is not None:
                    attribution, proof_seq, proof_epoch, proof_generation = "claimed", proof[0], proof[1], proof[2]
            self.registry.update_live_request(
                request_id, status=status, runtime_session_id=runtime, start_seq=start_seq,
                error_code=None, attribution=attribution, proof_seq=proof_seq,
                proof_epoch=proof_epoch, proof_generation=proof_generation,
            )
            result = self._result(
                status=status, request_id=request_id, session_id=runtime,
                stored_session_id=self.registry.session_for_lane(lane), event_cursor=start_seq,
                attribution=attribution,
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
        record_status = str(record.get("status") or "pending")
        stored = record.get("session_id")
        if record_status in {"completed", "failed", "interrupted", "reconciled"}:
            # This request's terminal outcome was already delivered. Re-waiting
            # could only attribute a later — possibly foreign — completion.
            return self._result(
                status=record_status, request_id=request_id, session_id=runtime,
                stored_session_id=stored, replayed=True,
                error_code=record.get("error_code"),
                attribution=record.get("attribution"),
            )
        attribution = record.get("attribution")
        inflight_sha = str(record.get("inflight_sha256") or "")
        if attribution != "claimed" or not inflight_sha:
            # Without claimed-turn evidence the next completion on this shared
            # session may belong to another attached client. Never return it.
            return self._result(
                status=record_status if record_status != "pending" else "running",
                request_id=request_id, session_id=runtime, stored_session_id=stored,
                error_code="ambiguous_turn", replayed=True,
                error=("Ownership of the next gateway completion cannot be proven for this request; "
                       "use live_reconcile for durable evidence. Nothing was attributed or resubmitted."),
                attribution=attribution,
            )
        current_epoch = self.client.health().get("replay_epoch")
        current_generation = self.client.connection_generation()
        proof_seq = record.get("proof_seq")
        cursor = None
        if (
            isinstance(proof_seq, int)
            and record.get("proof_epoch") == current_epoch
            and record.get("proof_generation") == current_generation
        ):
            cursor = proof_seq
        if cursor is None:
            # The proof watermark belongs to a previous replay epoch or a
            # previous connection. A reconnect (even within the same epoch) can
            # silently drop events, so stream continuity must be re-proven: the
            # only safe way to keep waiting is a fresh inflight proof of the
            # still-running turn; otherwise the completion is authoritative-
            # recovery territory (live_reconcile/live_history).
            snapshot = self._running_turn_snapshot(runtime)
            if snapshot is None or not snapshot["running"] or not self._inflight_matches(snapshot, inflight_sha):
                return self._result(
                    status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    error_code="completion_not_observed", replayed=True,
                    error=("The claimed turn is no longer running and its completion was not observed on this "
                           "connection; use live_reconcile/live_history for durable recovery."),
                    attribution=attribution,
                )
            cursor = self.client.watermarks().get(runtime, 0)
            # Persist the re-proven cursor so later wait calls continue from the
            # fresh connection's watermark instead of the stale proof cursor.
            self.registry.update_live_request(
                request_id, proof_seq=cursor,
                proof_epoch=self.client.health().get("replay_epoch"),
                proof_generation=self.client.connection_generation(),
            )
        if self.client.replay_degraded(runtime):
            # This connection's replay lost events (for any runtime id — a
            # resume rotates the id, and the degradation must survive that);
            # buffered ordering cannot attribute completions.
            return self._result(
                status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                error_code="completion_not_observed", replayed=True,
                error="Replay for this connection was truncated; use live_reconcile/live_history for durable recovery.",
                attribution=attribution,
            )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                event = None
            else:
                try:
                    event = self.client.next_completion(runtime, after_seq=cursor, timeout=remaining)
                except LiveError as exc:
                    return self._error(exc, request_id=request_id, session_id=runtime)
            if event is None:
                snapshot = self._running_turn_snapshot(runtime)
                if snapshot is not None and not snapshot["running"] and snapshot.get("inflight_user") is None:
                    return self._result(
                        status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                        error_code="completion_not_observed", replayed=True,
                        error=("The claimed turn is no longer running and its completion was not observed on this "
                               "connection; use live_reconcile/live_history for durable recovery."),
                        attribution=attribution,
                    )
                return self._result(
                    status="running", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    error_code="wait_timeout",
                    error="Live prompt is still active; call live_wait or live_events again",
                    attribution=attribution,
                )
            if (
                self.client.connection_generation() != current_generation
                or self.client.health().get("replay_epoch") != current_epoch
            ):
                # A reconnect (or epoch rotation) happened while this wait was
                # blocked. The proof watermark belongs to the previous
                # connection, so no buffered candidate is admissible anymore,
                # regardless of the gateway-side snapshot.
                return self._result(
                    status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    error_code="completion_not_observed", replayed=True,
                    error=("The live connection was re-established during the wait; buffered ordering "
                           "cannot attribute completions. Use live_reconcile/live_history for durable recovery."),
                    attribution=attribution,
                )
            if self.client.events_truncated(runtime, after_seq=cursor):
                # The ring evicted events between the proof cursor and here, so
                # buffer order no longer proves whose completion this is.
                return self._result(
                    status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    error_code="completion_not_observed", replayed=True,
                    error="Live event buffer was evicted before the completion; use live_reconcile/live_history.",
                    attribution=attribution,
                )
            snapshot = self._running_turn_snapshot(runtime)
            if snapshot is None:
                return self._result(
                    status="running", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    error_code="ambiguous_turn", replayed=True,
                    error="Gateway turn-state evidence is unavailable; completion ownership cannot be proven.",
                    attribution=attribution,
                )
            if self.client.replay_degraded(runtime):
                # This connection's replay lost events for the session; the
                # buffered candidate ordering cannot prove whose completion
                # this is (failure matrix: replay truncated -> authoritative
                # recovery or conservative result).
                return self._result(
                    status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    error_code="completion_not_observed", replayed=True,
                    error="Replay for this session was truncated; use live_reconcile/live_history for durable recovery.",
                    attribution=attribution,
                )
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            raw_status = str(payload.get("status") or "complete")
            inflight_user = snapshot.get("inflight_user")
            if inflight_user is not None:
                if self._inflight_matches(snapshot, inflight_sha):
                    if snapshot.get("inflight_error"):
                        if raw_status == "error":
                            # Our claimed turn FAILED: the gateway retains the
                            # failed inflight snapshot (with the error marker)
                            # while emitting the terminal failure completion, so
                            # this candidate is our failure and must be
                            # reported. A success payload under a retained
                            # failure snapshot is a non-conforming ordering and
                            # must never be attributed.
                            pass
                        else:
                            return self._result(
                                status="unknown", request_id=request_id, session_id=runtime,
                                stored_session_id=stored,
                                error_code="completion_not_observed", replayed=True,
                                error=("The claimed turn failed but the buffered completion is not its terminal "
                                       "event; use live_reconcile/live_history for durable recovery."),
                                attribution=attribution,
                            )
                    else:
                        # The proven running turn is ours and healthy; its
                        # completion cannot have been emitted yet (the gateway
                        # clears the inflight snapshot before emitting
                        # message.complete), so this completion belongs to
                        # another turn; skip it and keep waiting.
                        candidate_seq = event.get("seq")
                        cursor = candidate_seq if isinstance(candidate_seq, int) else cursor
                        continue
                else:
                    # A turn whose prompt is not ours is live in the gateway
                    # state while a completion is already buffered (a conforming
                    # gateway never emits a turn's completion before clearing its
                    # inflight snapshot). This ordering cannot attribute the
                    # candidate.
                    return self._result(
                        status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                        error_code="completion_not_observed", replayed=True,
                        error=("A foreign attached-client turn is live; the local completion was not observed. "
                               "Use live_reconcile/live_history for durable recovery."),
                        attribution=attribution,
                    )
            # No inflight snapshot exists: the claimed turn has ended (the
            # gateway clears the inflight snapshot before emitting the
            # completion) and no foreign turn is live. Under the gateway busy
            # gate no foreign completion can precede ours after the proof
            # cursor on a continuous connection, so this candidate is the
            # local completion.
            status = {"complete": "completed", "error": "failed", "cancelled": "interrupted"}.get(raw_status, raw_status)
            error_code = "live_turn_failed" if status == "failed" else None
            # A failed or interrupted turn has no answer: the gateway's
            # terminal error payload carries fallback failure copy in `text`
            # (prompt_turn._complete_turn_payload mirrors it into `error` as
            # str(error_value or raw)), which is failure detail, never the
            # turn's answer.
            answer = payload.get("text") if isinstance(payload.get("text"), str) else None
            if status != "completed":
                answer = None
            self.registry.update_live_request(request_id, status=status, error_code=error_code)
            return self._result(
                status=status, request_id=request_id, session_id=runtime,
                stored_session_id=stored, answer=answer,
                error_code=error_code, error=str(payload.get("error")) if payload.get("error") else None,
                event=event, event_seq=event.get("seq"), attribution=attribution,
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
            boundary_row_id = record.get("boundary_row_id")
            if boundary_row_id is None:
                # No pre-submit durable boundary: text equality across the whole
                # history proves nothing about THIS submit. Stay conservative.
                return self._result(
                    status="unknown", request_id=request_id, session_id=runtime,
                    stored_session_id=record.get("session_id"), error_code="reconcile_boundary_missing",
                    error=("This request has no pre-submit durable boundary, so history text equality cannot prove "
                           "the ambiguous submit was persisted. Verify manually; nothing was resubmitted."),
                    reconciliation="legacy_record_without_boundary",
                )
            prompt_sha = str(record.get("prompt_sha256") or "")
            post_boundary_matches = 0
            unordered_match = False
            for message in messages:
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                text = message.get("text")
                if not isinstance(text, str):
                    text = str(message.get("content", ""))
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != prompt_sha:
                    continue
                row_id = message.get("row_id")
                if isinstance(row_id, int) and not isinstance(row_id, bool):
                    if row_id > int(boundary_row_id):
                        post_boundary_matches += 1
                else:
                    # A matching user row without durable row identity cannot be
                    # ordered against the boundary.
                    unordered_match = True
            if unordered_match or post_boundary_matches > 1:
                self.registry.update_live_request(request_id, error_code="ambiguous_history_match")
                return self._result(
                    status="unknown", request_id=request_id, session_id=runtime,
                    stored_session_id=record.get("session_id"), error_code="ambiguous_history_match",
                    error=("Identical post-boundary prompts are indistinguishable in durable history; the ambiguous "
                           "submit was not proven and was not resubmitted."),
                    reconciliation="post_boundary_ambiguous",
                )
            if post_boundary_matches == 1:
                self.registry.update_live_request(request_id, status="reconciled", runtime_session_id=runtime, error_code=None)
                return self._result(
                    status="reconciled", request_id=request_id, session_id=runtime,
                    stored_session_id=record.get("session_id"), reconciliation="history_match_post_boundary",
                    warning=("Exactly one post-boundary durable user row matches this prompt; identical older "
                             "prompts were excluded by the pre-submit boundary."),
                )
            return self._result(
                status="unknown", request_id=request_id, session_id=runtime,
                stored_session_id=record.get("session_id"), error_code="transport_unknown",
                error="No post-boundary durable evidence was found; the prompt was not submitted again.",
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
        configured = bool(
            self.client.config.gateway_url or self.client.config.gateway_owner_lease_path is not None
        )
        if not configured:
            return self._result(
                status="unconfigured", error_code="gateway_not_configured",
                error="gateway_url or gateway_owner_lease_path is not configured; live tools are disabled",
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
