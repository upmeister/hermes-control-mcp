from __future__ import annotations

import hashlib
import threading
import time
import uuid
from typing import Any

from .live_client import LiveAuthError, LiveError, LiveGatewayClient, LiveRPCError, LiveTransportUnknown
from .profiles import DEFAULT_PROFILE
from .registry import StateRegistry
from .service import InputError, _fingerprint, _optional_profile, _profile_input, _validate_text, _visible_id


class LiveService:
    """Safe MCP-facing facade over one persistent TUI WebSocket client."""

    def __init__(self, client: LiveGatewayClient, registry: StateRegistry) -> None:
        self.client = client
        self.registry = registry
        self._lock = threading.RLock()
        # runtime ids are process-local and keyed by (profile, lane): the same
        # lane name may legally exist in two profiles, so a lane-only key would
        # route nondeterministically. The durable (profile, lane) binding is
        # the source used to resume after this MCP process is restarted.
        self._runtimes: dict[tuple[str, str], str] = {}
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
        return LiveService._result(
            status="failed", request_id=request_id, error_code=getattr(exc, "code", "invalid_input"),
            error=str(exc)
        )

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

    def _running_turn_snapshot(self, runtime: str, profile: str = DEFAULT_PROFILE) -> dict[str, Any] | None:
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
                {"session_id": runtime, "omit_messages": True, "profile": profile},
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

    def _prove_running_claim(self, runtime: str, inflight_sha256: str, profile: str = DEFAULT_PROFILE) -> tuple[int, str, int] | None:
        """Prove the currently running turn was claimed by our own submit.

        The submit ack was ``streaming``, so the gateway claimed our turn. The
        inflight prompt text must hash to our submitted text; only then is the
        running turn provably ours. Returns the post-proof replay watermark,
        epoch, and connection generation so later completions can be ordered
        after the proof and invalidated by any reconnect.
        """
        snapshot = self._running_turn_snapshot(runtime, profile)
        if snapshot is None or not snapshot["running"] or not self._inflight_matches(snapshot, inflight_sha256):
            return None
        health = self.client.health()
        epoch = health.get("replay_epoch")
        if not isinstance(epoch, str) or not epoch:
            return None
        return self.client.watermarks().get(runtime, 0), epoch, self.client.connection_generation()

    def _capture_boundary(self, runtime: str, profile: str = DEFAULT_PROFILE) -> tuple[int | None, int | None]:
        """Capture the redacted pre-submit durable boundary for reconciliation.

        Reads ``session.history`` (durable rows) and stores only the highest
        user ``row_id`` plus the row count — never prompt or body text. A
        boundary of 0 is a valid empty-history boundary; None means the
        boundary is unknown and reconciliation must stay conservative.
        """
        try:
            reply = self.client.request("session.history", {"session_id": runtime, "profile": profile})
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

    def _remember_runtime(self, profile: str, lane: str, runtime: str, stored: str) -> None:
        with self._lock:
            self._runtimes[(profile, lane)] = runtime
            self._stored_by_runtime[runtime] = stored
        # Runtime ids belong to one TUI gateway process. A reconnect or backend
        # restart may mint a new runtime id for the same durable lane; update
        # only the redacted identity records, never a stored prompt. The scan
        # is scoped to this lane's own profile.
        for request in self.registry.live_requests_for_profile_lane(profile, lane):
            if request.get("runtime_session_id") != runtime:
                self.registry.update_live_request(request["request_id"], runtime_session_id=runtime)
        # Reconnect replay must address this session under the same profile.
        self.client.set_session_profile(runtime, profile)

    def _lane_profile(self, lane: str, profile: str | None) -> tuple[str, dict[str, Any] | None]:
        """Resolve a lane's governing profile with fail-closed ambiguity.

        A supplied profile wins. When omitted, a lane bound in exactly one
        profile infers it, a lane bound in multiple profiles fails with
        ``lane_profile_ambiguous``, and an unbound lane uses the default
        profile for creation/start.
        """
        if profile is not None:
            return _profile_input(profile), None
        bound = self.registry.profiles_for_lane(lane)
        if len(bound) > 1:
            return DEFAULT_PROFILE, self._result(
                status="failed", error_code="lane_profile_ambiguous",
                error=(f"Lane {lane!r} is bound under multiple profiles {bound}; "
                       "supply an explicit profile"),
            )
        return (bound[0] if bound else DEFAULT_PROFILE), None

    def _open_profile(
        self, lane: str, profile: str | None, supplied_session: str | None
    ) -> tuple[str, dict[str, Any] | None]:
        """Resolve live-open profile, allowing an exact known stored session to infer it.

        A supplied profile is always explicit. When omitted, an exact stored
        session known under one profile outranks lane/default inference. An
        ambiguous exact session fails closed rather than guessing.
        """
        supplied_profile = _optional_profile(profile)
        if supplied_profile is not None:
            return supplied_profile, None
        if supplied_session:
            session_profiles = sorted(
                set(self.registry.profiles_for_session(supplied_session))
                | set(self.registry.profiles_for_request_session(supplied_session))
            )
            if len(session_profiles) > 1:
                return DEFAULT_PROFILE, self._result(
                    status="failed", error_code="lane_profile_ambiguous",
                    error=(f"The supplied stored session is locally known under multiple profiles "
                           f"{session_profiles}; supply an explicit profile"),
                )
            if len(session_profiles) == 1:
                return session_profiles[0], None
        return self._lane_profile(lane, None)

    def _runtime_for_lane(
        self, profile: str, lane: str, supplied: str | None = None, *, reopen: bool = False
    ) -> tuple[str | None, dict[str, Any] | None]:
        if supplied:
            # Lane+runtime addressing must not bypass the runtime's stored
            # profile binding: a supplied runtime known under other profiles is
            # a conflict, never a reroute (and emits no gateway frame).
            bound = self._profiles_for_runtime(supplied)
            if bound and profile not in bound:
                return None, self._result(
                    status="failed", error_code="request_profile_conflict",
                    error=(f"This live session belongs to profile(s) {bound}; "
                           f"refusing to route it through profile {profile!r}"),
                )
            return supplied, None
        with self._lock:
            runtime = self._runtimes.get((profile, lane))
        if runtime:
            return runtime, None
        if reopen and self.registry.session_for_profile_lane(profile, lane):
            opened = self.open(lane=lane, profile=profile)
            if opened.get("ok"):
                return str(opened["session_id"]), None
            return None, opened
        return None, self._result(status="failed", error_code="live_session_not_open", error="Call live_session_open first")

    _TRANSIENT_RESUME_4007 = "session no longer live; retry resume"

    def _resume_stored(self, target: str, profile: str = DEFAULT_PROFILE) -> Any:
        # Hermes reattach race (session_lifecycle._reattach_refusal): a resume
        # can lose the race against a reap/retire of the live record it just
        # looked up and receive the transient JSON-RPC 4007 semantic that
        # explicitly instructs the client to retry resume. Exactly ONE
        # immediate retry of the identical resume is allowed: re-attaching
        # repeats the same rebuild operation against the same stored identity
        # and is not a prompt/steer/interrupt mutation retry. The retry reuses
        # the identical profile-scoped params — dropping profile would resolve
        # the stored id under the wrong profile home. Genuine 4007
        # "session not found" (an empty draft that never became durable, for
        # example) receives zero retries and stays fail-closed; a second
        # transient failure surfaces through the normal structured error path.
        params = {"session_id": target, "profile": profile}
        try:
            return self.client.request("session.resume", params)
        except LiveRPCError as exc:
            normalized = " ".join(str(exc).split()).lower()
            if exc.rpc_code != 4007 or normalized != self._TRANSIENT_RESUME_4007:
                raise
            return self.client.request("session.resume", dict(params))

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
            profile, profile_error = self._open_profile(lane, profile, supplied)
            if profile_error is not None:
                return profile_error
            current = self.registry.session_for_profile_lane(profile, lane)
            if supplied and current and supplied != current:
                return self._result(
                    status="failed", error_code="lane_session_conflict",
                    error="The lane is already bound to a different durable session_id",
                )
            if supplied:
                bound_profiles = self.registry.profiles_for_session(supplied)
                if bound_profiles and profile not in bound_profiles:
                    return self._result(
                        status="failed", error_code="lane_profile_conflict",
                        error=(f"The supplied session belongs to profile lane(s) {bound_profiles}; "
                               f"refusing to bind it under profile {profile!r}"),
                    )
            connected = self._ensure_connected()
            if connected is not None:
                return connected
            target = supplied or current
            if target:
                reply = self._resume_stored(target, profile)
            else:
                params: dict[str, Any] = {
                    "source": "tool", "close_on_disconnect": bool(close_on_disconnect),
                    "profile": profile,
                }
                for key, value in (
                    ("title", title), ("cwd", cwd),
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
            activated = self.client.request(
                "session.activate", {"session_id": runtime, "omit_messages": True, "profile": profile}
            )
            if not isinstance(activated, dict) or activated.get("session_id") != runtime:
                return self._result(
                    status="failed", error_code="invalid_response",
                    error="Hermes live session activation did not confirm the runtime session identity",
                )
            activated_stored = activated.get("stored_session_id") or stored
            if not isinstance(activated_stored, str) or not activated_stored:
                return self._result(status="failed", error_code="invalid_response", error="Hermes live session activation omitted stored identity")
            stored = activated_stored
            self.registry.bind_profile_lane(profile, lane, stored)
            self._remember_runtime(profile, lane, runtime, stored)
            result = self._result(
                status="connected", session_id=runtime, stored_session_id=stored,
                profile=profile,
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
        profile: str | None = None,
    ) -> dict[str, Any]:
        try:
            lane = _validate_text(lane, "lane", max_length=200)
            text = _validate_text(text, "text", max_length=100_000, allow_common_whitespace=True)
            if not text.strip():
                raise InputError("text must not be blank")
            wait = max(0.0, min(float(wait_seconds), 3600.0))
            supplied_profile = _optional_profile(profile)
            request_id = _visible_id(request_id, "request_id", max_length=128) if request_id else f"live_req_{uuid.uuid4().hex}"
            existing_request = self.registry.live_request_by_id(request_id)
            if existing_request is not None:
                record_profile = str(existing_request.get("profile") or DEFAULT_PROFILE)
                if supplied_profile is not None and supplied_profile != record_profile:
                    return self._result(
                        status="failed", request_id=request_id,
                        session_id=existing_request.get("runtime_session_id"),
                        stored_session_id=existing_request.get("session_id"),
                        profile=record_profile, error_code="request_profile_conflict",
                        error=(f"request_id was already used under profile {record_profile!r}; "
                               "profile boundaries are never rerouted"),
                    )
                profile = record_profile
            else:
                profile, profile_error = self._lane_profile(lane, supplied_profile)
                if profile_error is not None:
                    return profile_error
            runtime, error = self._runtime_for_lane(profile, lane, session_id)
            if error is not None:
                return error
            durable_session = self.registry.session_for_profile_lane(profile, lane) or self._stored_by_runtime.get(runtime or "") or runtime
            fingerprint = _fingerprint(lane, {
                "session_id": durable_session, "text": text, "queued": bool(queued),
            }, profile)
            prompt_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            # The gateway strips the running turn's user text into the inflight
            # snapshot; hash the stripped form so the claim proof compares equal.
            inflight_sha = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc, request_id=request_id)

        start_seq = self.client.watermarks().get(runtime or "", 0)
        boundary_row_id, boundary_count = self._capture_boundary(runtime or "", profile)
        reserved = self.registry.reserve_live_request({
            "request_id": request_id, "lane": lane, "profile": profile,
            "session_id": self.registry.session_for_profile_lane(profile, lane) or runtime or "",
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
            if (
                record.get("fingerprint") != fingerprint or record.get("lane") != lane
                or str(record.get("profile") or DEFAULT_PROFILE) != profile
            ):
                return self._result(
                    status="failed", request_id=request_id, session_id=runtime,
                    error_code="request_id_conflict", error="request_id was already used with a different live prompt",
                )
            return self._result(
                status=str(record.get("status") or "unknown"), request_id=request_id,
                session_id=record.get("runtime_session_id") or runtime,
                stored_session_id=record.get("session_id"), profile=profile,
                error_code=record.get("error_code") or ("transport_unknown" if record.get("status") == "unknown" else None),
                error="Reconcile this request with live_reconcile; it was not submitted again" if record.get("status") == "unknown" else None,
                replayed=True, event_cursor=record.get("start_seq"),
                attribution=record.get("attribution"),
            )
        try:
            params: dict[str, Any] = {"session_id": runtime, "text": text, "profile": profile}
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
                proof = self._prove_running_claim(runtime or "", inflight_sha, profile)
                if proof is not None:
                    attribution, proof_seq, proof_epoch, proof_generation = "claimed", proof[0], proof[1], proof[2]
            self.registry.update_live_request(
                request_id, status=status, runtime_session_id=runtime, start_seq=start_seq,
                error_code=None, attribution=attribution, proof_seq=proof_seq,
                proof_epoch=proof_epoch, proof_generation=proof_generation,
            )
            result = self._result(
                status=status, request_id=request_id, session_id=runtime,
                stored_session_id=self.registry.session_for_profile_lane(profile, lane), profile=profile,
                event_cursor=start_seq, attribution=attribution,
            )
            if wait > 0 and status in {"streaming", "queued"}:
                return self.wait(request_id=request_id, timeout_seconds=wait)
            return result
        except LiveError as exc:
            status = "unknown" if isinstance(exc, LiveTransportUnknown) else "failed"
            self.registry.update_live_request(request_id, status=status, error_code=exc.code)
            return self._error(exc, request_id=request_id, session_id=runtime)

    def _mark_awaiting_recovery(self, request_id: str, error_code: str | None) -> dict[str, Any] | None:
        # A conservative wait result that hands the request to durable recovery
        # must persist that state: live_reconcile accepts only rows stored as
        # unknown, so leaving a streaming/claimed row behind would make the
        # result's own recovery advice unactionable. The ordinary bounded
        # wait_timeout (the turn is still running) and the retriable
        # turn-state-unavailable ambiguous_turn deliberately stay unmarked.
        #
        # The write is a terminal-preserving compare-and-set: a stale
        # concurrent waiter that observed a conservative condition must not
        # erase a terminal outcome another waiter already committed. When this
        # waiter loses that race it replays the delivered terminal row instead
        # of returning its own stale conservative result.
        if self.registry.mark_live_request_awaiting_recovery(request_id, error_code):
            return None
        record = self.registry.live_request_by_id(request_id)
        if record is not None and record.get("status") in {"completed", "failed", "interrupted", "reconciled"}:
            return self._result(
                status=str(record["status"]), request_id=request_id,
                session_id=record.get("runtime_session_id"), stored_session_id=record.get("session_id"),
                replayed=True, error_code=record.get("error_code"),
                attribution=record.get("attribution"),
            )
        return None

    def wait(
        self, *, request_id: str | None = None, lane: str | None = None, timeout_seconds: float = 120.0,
        profile: str | None = None,
    ) -> dict[str, Any]:
        try:
            if profile is not None:
                profile = _profile_input(profile)
            if request_id:
                request_id = _visible_id(request_id, "request_id", max_length=128)
                record = self.registry.live_request_by_id(request_id)
            elif lane:
                lane = _validate_text(lane, "lane", max_length=200)
                if profile is not None:
                    profile = _profile_input(profile)
                    record = self.registry.latest_live_request_for_profile_lane(profile, lane)
                else:
                    lane_profiles = self.registry.live_request_profiles_for_lane(lane)
                    if not lane_profiles:
                        # No request rows yet: fall back to the lane bindings so
                        # a shared lane name is ambiguous even before any wait.
                        lane_profiles = self.registry.profiles_for_lane(lane)
                    if len(lane_profiles) > 1:
                        return self._result(
                            status="failed", error_code="lane_profile_ambiguous",
                            error=(f"Lane {lane!r} has live state under multiple profiles {lane_profiles}; "
                                   "supply an explicit profile"),
                        )
                    record = self.registry.latest_live_request_for_profile_lane(
                        lane_profiles[0] if lane_profiles else DEFAULT_PROFILE, lane
                    )
                request_id = str(record.get("request_id")) if record else None
            else:
                raise InputError("request_id or lane is required")
            if record is None:
                return self._result(
                    status="failed", request_id=request_id, profile=profile or DEFAULT_PROFILE,
                    error_code="request_not_found", error="No live prompt matches the request",
                )
            # The stored request record owns the profile: a supplied different
            # profile is a conflict, never a reroute of the wait target.
            record_profile = str(record.get("profile") or DEFAULT_PROFILE)
            if profile is not None and profile != record_profile:
                return self._result(
                    status="failed", request_id=request_id, error_code="request_profile_conflict",
                    error=(f"This live request belongs to profile {record_profile!r}; "
                           f"refusing to wait through profile {profile!r}"),
                )
            profile = record_profile
            if record.get("status") == "unknown":
                return self._result(
                    status="unknown", request_id=request_id,
                    session_id=record.get("runtime_session_id"), stored_session_id=record.get("session_id"),
                    profile=profile,
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
                stored_session_id=stored, profile=profile, replayed=True,
                error_code=record.get("error_code"),
                attribution=record.get("attribution"),
            )
        attribution = record.get("attribution")
        inflight_sha = str(record.get("inflight_sha256") or "")
        if attribution != "claimed" or not inflight_sha:
            # Without claimed-turn evidence the next completion on this shared
            # session may belong to another attached client. Never return it.
            marked = self._mark_awaiting_recovery(request_id, "ambiguous_turn")
            if marked is not None:
                return marked
            return self._result(
                status="unknown",
                request_id=request_id, session_id=runtime, stored_session_id=stored,
                profile=profile,
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
            snapshot = self._running_turn_snapshot(runtime, profile)
            if snapshot is None or not snapshot["running"] or not self._inflight_matches(snapshot, inflight_sha):
                marked = self._mark_awaiting_recovery(request_id, "completion_not_observed")
                if marked is not None:
                    return marked
                return self._result(
                    status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    profile=profile,
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
            marked = self._mark_awaiting_recovery(request_id, "completion_not_observed")
            if marked is not None:
                return marked
            return self._result(
                status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                profile=profile,
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
            if (
                self.client.connection_generation() != current_generation
                or self.client.health().get("replay_epoch") != current_epoch
            ):
                # Checked after EVERY blocking event read, candidate or not: a
                # reconnect (or epoch rotation) while this wait was blocked
                # voids the proof watermark, so no buffered ordering is
                # admissible anymore and the turn must not be reported as
                # still running either.
                marked = self._mark_awaiting_recovery(request_id, "completion_not_observed")
                if marked is not None:
                    return marked
                return self._result(
                    status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    profile=profile,
                    error_code="completion_not_observed", replayed=True,
                    error=("The live connection was re-established during the wait; buffered ordering "
                           "cannot attribute completions. Use live_reconcile/live_history for durable recovery."),
                    attribution=attribution,
                )
            if event is None:
                snapshot = self._running_turn_snapshot(runtime, profile)
                if snapshot is not None and not snapshot["running"] and snapshot.get("inflight_user") is None:
                    marked = self._mark_awaiting_recovery(request_id, "completion_not_observed")
                    if marked is not None:
                        return marked
                    return self._result(
                        status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                        profile=profile,
                        error_code="completion_not_observed", replayed=True,
                        error=("The claimed turn is no longer running and its completion was not observed on this "
                               "connection; use live_reconcile/live_history for durable recovery."),
                        attribution=attribution,
                    )
                return self._result(
                    status="running", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    profile=profile,
                    error_code="wait_timeout",
                    error="Live prompt is still active; call live_wait or live_events again",
                    attribution=attribution,
                )
            if self.client.events_truncated(runtime, after_seq=cursor):
                # The ring evicted events between the proof cursor and here, so
                # buffer order no longer proves whose completion this is.
                marked = self._mark_awaiting_recovery(request_id, "completion_not_observed")
                if marked is not None:
                    return marked
                return self._result(
                    status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    profile=profile,
                    error_code="completion_not_observed", replayed=True,
                    error="Live event buffer was evicted before the completion; use live_reconcile/live_history.",
                    attribution=attribution,
                )
            snapshot = self._running_turn_snapshot(runtime, profile)
            if snapshot is None:
                return self._result(
                    status="running", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    profile=profile,
                    error_code="ambiguous_turn", replayed=True,
                    error="Gateway turn-state evidence is unavailable; completion ownership cannot be proven.",
                    attribution=attribution,
                )
            if self.client.replay_degraded(runtime):
                # This connection's replay lost events for the session; the
                # buffered candidate ordering cannot prove whose completion
                # this is (failure matrix: replay truncated -> authoritative
                # recovery or conservative result).
                marked = self._mark_awaiting_recovery(request_id, "completion_not_observed")
                if marked is not None:
                    return marked
                return self._result(
                    status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                    profile=profile,
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
                            marked = self._mark_awaiting_recovery(request_id, "completion_not_observed")
                            if marked is not None:
                                return marked
                            return self._result(
                                status="unknown", request_id=request_id, session_id=runtime,
                                stored_session_id=stored, profile=profile,
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
                    marked = self._mark_awaiting_recovery(request_id, "completion_not_observed")
                    if marked is not None:
                        return marked
                    return self._result(
                        status="unknown", request_id=request_id, session_id=runtime, stored_session_id=stored,
                        profile=profile,
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
                stored_session_id=stored, profile=profile, answer=answer,
                error_code=error_code, error=str(payload.get("error")) if payload.get("error") else None,
                event=event, event_seq=event.get("seq"), attribution=attribution,
            )

    def _resolve_live_target(
        self, *, lane: str | None, session_id: str | None, profile: str | None, reopen: bool = False
    ) -> tuple[str | None, str, dict[str, Any] | None]:
        """Resolve (runtime, profile, error) for a live read/control call.

        Lane-addressed calls infer the lane's profile with fail-closed
        ambiguity; runtime-addressed calls resolve the profile against the
        stored binding for that runtime — omitted infers it (fail-closed when
        ambiguous) and a supplied different profile is a conflict, never a
        reroute. An unknown runtime routes through the supplied profile or the
        default profile (backward compatibility).
        """
        try:
            if lane:
                lane = _validate_text(lane, "lane", max_length=200)
                resolved_profile, error = self._lane_profile(lane, profile)
                if error is not None:
                    return None, DEFAULT_PROFILE, error
                runtime, error = self._runtime_for_lane(resolved_profile, lane, session_id, reopen=reopen)
                return runtime, resolved_profile, error
            if session_id:
                runtime = _visible_id(session_id, "session_id")
                bound = self._profiles_for_runtime(runtime)
                if profile is not None:
                    canonical = _profile_input(profile)
                    if bound and canonical not in bound:
                        return None, DEFAULT_PROFILE, self._result(
                            status="failed", error_code="request_profile_conflict",
                            error=(f"This live session belongs to profile(s) {bound}; "
                                   f"refusing to route it through profile {canonical!r}"),
                        )
                    return runtime, canonical, None
                if len(bound) > 1:
                    return None, DEFAULT_PROFILE, self._result(
                        status="failed", error_code="lane_profile_ambiguous",
                        error=(f"This live session is bound under multiple profiles {bound}; "
                               "supply an explicit profile"),
                    )
                return runtime, (bound[0] if bound else DEFAULT_PROFILE), None
            raise InputError("lane or session_id is required")
        except (InputError, TypeError, ValueError) as exc:
            return None, DEFAULT_PROFILE, self._input_error(exc)

    def _profiles_for_runtime(self, runtime: str) -> list[str]:
        """Locally bound profiles for one runtime id (bindings + request rows)."""
        stored = self._stored_by_runtime.get(runtime)
        profiles: set[str] = set(self.registry.live_request_profiles_for_runtime(runtime))
        if stored:
            profiles.update(self.registry.profiles_for_session(stored))
        return sorted(profiles)

    def events(self, *, lane: str | None = None, session_id: str | None = None, after_seq: int = 0,
               profile: str | None = None) -> dict[str, Any]:
        try:
            after = max(0, int(after_seq))
        except (TypeError, ValueError) as exc:
            return self._input_error(exc)
        runtime, profile, error = self._resolve_live_target(lane=lane, session_id=session_id, profile=profile, reopen=bool(lane))
        if error is not None:
            return error
        events = self.client.events(runtime or "", after_seq=after)
        return self._result(
            status="connected", session_id=runtime, profile=profile,
            events=events, event_count=len(events), after_seq=after,
            latest_seq=self.client.watermarks().get(runtime or "", 0),
            truncated=self.client.events_truncated(runtime or "", after_seq=after),
            replay_epoch=self.client.health().get("replay_epoch"),
        )

    def status(self, *, lane: str | None = None, session_id: str | None = None,
               profile: str | None = None) -> dict[str, Any]:
        runtime, profile, error = self._resolve_live_target(lane=lane, session_id=session_id, profile=profile, reopen=bool(lane))
        if error is not None:
            return error
        try:
            reply = self.client.request("session.status", {"session_id": runtime, "profile": profile})
            return self._result(status="connected", session_id=runtime, profile=profile, stored_session_id=self._stored_by_runtime.get(runtime or ""), gateway_result=reply)
        except LiveError as exc:
            return self._error(exc, session_id=session_id)

    def history(self, *, lane: str | None = None, session_id: str | None = None,
                profile: str | None = None) -> dict[str, Any]:
        runtime, profile, error = self._resolve_live_target(lane=lane, session_id=session_id, profile=profile, reopen=bool(lane))
        if error is not None:
            return error
        try:
            reply = self.client.request("session.history", {"session_id": runtime, "profile": profile})
            messages = reply.get("messages", []) if isinstance(reply, dict) else []
            return self._result(status="connected", session_id=runtime, profile=profile, stored_session_id=self._stored_by_runtime.get(runtime or ""), messages=messages, message_count=len(messages), gateway_result=reply)
        except LiveError as exc:
            return self._error(exc, session_id=session_id)

    def steer(self, *, text: str, lane: str | None = None, session_id: str | None = None,
              profile: str | None = None) -> dict[str, Any]:
        try:
            text = _validate_text(text, "text", max_length=100_000, allow_common_whitespace=True)
            if not text.strip():
                raise InputError("text must not be blank")
        except (InputError, TypeError, ValueError) as exc:
            return self._input_error(exc)
        runtime, profile, error = self._resolve_live_target(lane=lane, session_id=session_id, profile=profile)
        if error is not None:
            return error
        try:
            reply = self.client.request("session.steer", {"session_id": runtime, "text": text, "profile": profile})
            return self._result(status=str(reply.get("status") or "queued") if isinstance(reply, dict) else "queued", session_id=runtime, profile=profile, gateway_result=reply)
        except LiveError as exc:
            return self._error(exc, session_id=session_id)

    def interrupt(self, *, lane: str | None = None, session_id: str | None = None,
                  profile: str | None = None) -> dict[str, Any]:
        runtime, profile, error = self._resolve_live_target(lane=lane, session_id=session_id, profile=profile)
        if error is not None:
            return error
        try:
            reply = self.client.request("session.interrupt", {"session_id": runtime, "profile": profile})
            return self._result(status="interrupted", session_id=runtime, profile=profile, gateway_result=reply)
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
            record_profile = str(record.get("profile") or DEFAULT_PROFILE)
            # Request identity owns the scope: reconcile always resumes and
            # reads durable history under the request's own stored profile.
            opened = self.open(lane=lane, profile=record_profile)
            if not opened.get("ok"):
                return opened
            runtime = str(opened["session_id"])
            history = self.history(session_id=runtime, profile=record_profile)
            if not history.get("ok"):
                return history
            messages = history.get("messages") if isinstance(history.get("messages"), list) else []
            boundary_row_id = record.get("boundary_row_id")
            if boundary_row_id is None:
                # No pre-submit durable boundary: text equality across the whole
                # history proves nothing about THIS submit. Stay conservative.
                return self._result(
                    status="unknown", request_id=request_id, session_id=runtime,
                    stored_session_id=record.get("session_id"), profile=record_profile,
                    error_code="reconcile_boundary_missing",
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
                    stored_session_id=record.get("session_id"), profile=record_profile,
                    error_code="ambiguous_history_match",
                    error=("Identical post-boundary prompts are indistinguishable in durable history; the ambiguous "
                           "submit was not proven and was not resubmitted."),
                    reconciliation="post_boundary_ambiguous",
                )
            if post_boundary_matches == 1:
                self.registry.update_live_request(request_id, status="reconciled", runtime_session_id=runtime, error_code=None)
                return self._result(
                    status="reconciled", request_id=request_id, session_id=runtime,
                    stored_session_id=record.get("session_id"), profile=record_profile,
                    reconciliation="history_match_post_boundary",
                    warning=("Exactly one post-boundary durable user row matches this prompt; identical older "
                             "prompts were excluded by the pre-submit boundary."),
                )
            return self._result(
                status="unknown", request_id=request_id, session_id=runtime,
                stored_session_id=record.get("session_id"), profile=record_profile,
                error_code="transport_unknown",
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
                bindings = list(self._runtimes)
            for profile, lane in bindings:
                # A reconnect may rotate runtime ids but must not rotate
                # profile identity: every remembered (profile, lane) reopens
                # under its stored profile binding.
                stored = self.registry.session_for_profile_lane(profile, lane)
                if not stored:
                    continue
                opened = self.open(lane=lane, profile=profile, session_id=stored)
                resumed[f"{profile}:{lane}"] = {
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
