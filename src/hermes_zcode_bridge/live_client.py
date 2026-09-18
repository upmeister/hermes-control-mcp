from __future__ import annotations

import asyncio
import copy
import json
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import BridgeConfig


_JSON_MISSING = object()
_MAX_RPC_ID_CHARS = 128
_MAX_METHOD_CHARS = 128
_MAX_EVENT_TYPE_CHARS = 128
_MAX_SESSION_ID_CHARS = 255
_MAX_REPLAY_EPOCH_CHARS = 128
_MAX_EVENT_PAYLOAD_BYTES = 4 * 1024 * 1024
_MAX_FRAME_BYTES = 16 * 1024 * 1024


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _strict_json_loads(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("frame is not valid UTF-8") from exc
    if not isinstance(raw, str):
        raise ValueError("frame is not JSON text")
    if len(raw.encode("utf-8")) > _MAX_FRAME_BYTES:
        raise ValueError("frame exceeds the maximum size")
    return json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_non_finite_constant,
        parse_float=_finite_float,
    )


def _bounded_text(value: Any, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise ValueError(f"{field} is invalid")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{field} contains control characters")
    if len(value.encode("utf-8")) > max_chars * 4:
        raise ValueError(f"{field} is too large")
    return value


def _rpc_id(value: Any) -> str:
    return _bounded_text(value, field="JSON-RPC id", max_chars=_MAX_RPC_ID_CHARS)


def _protocol_error(message: str) -> LiveError:
    return LiveError(f"live gateway protocol violation: {message}", code="protocol_violation")


def _validate_rpc_error(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"code", "message", "data"} or "code" not in value or "message" not in value:
        raise ValueError("JSON-RPC error object is invalid")
    code = value["code"]
    if type(code) is not int or not -32768 <= code <= 32768:
        raise ValueError("JSON-RPC error code is invalid")
    _bounded_text(value["message"], field="JSON-RPC error message", max_chars=512)
    return value


class LiveError(RuntimeError):
    """A live TUI transport operation failed with a safe diagnostic."""

    def __init__(self, message: str, *, code: str = "live_error") -> None:
        self.code = code
        super().__init__(message)


class LiveTransportUnknown(LiveError):
    """The request may have reached Hermes, but its acknowledgement was lost."""

    def __init__(self, message: str = "Live Hermes transport outcome is unknown; reconcile with status/history") -> None:
        super().__init__(message, code="transport_unknown")


class LiveAuthError(LiveError):
    """The configured live gateway credential cannot open a WS connection."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="gateway_auth_failed")


class LiveRPCError(LiveError):
    """Hermes returned a JSON-RPC error frame."""

    def __init__(self, code: int | str | None, message: str, data: Any = None) -> None:
        self.rpc_code = code
        self.data = data
        super().__init__(message[:2000], code="rpc_error")


@dataclass(slots=True)
class _PendingCall:
    future: asyncio.Future
    method: str


@dataclass(slots=True)
class _BufferedEvent:
    seq: int | None
    event: dict[str, Any]
    size: int


class LiveGatewayClient:
    """Persistent client for Hermes's TUI JSON-RPC WebSocket transport.

    MCP handlers are synchronous while ``websockets`` is asynchronous. A single
    private event loop thread owns the socket; public methods submit coroutines
    to it and never create a per-call socket. The client deliberately does not
    reconnect or retry a mutating RPC behind the caller's back: an absent reply
    is an explicit :class:`LiveTransportUnknown`.
    """

    _LIVE_MUTATIONS = frozenset({
        "session.create", "session.resume", "prompt.submit", "session.steer", "session.redirect",
        "session.interrupt", "session.activate", "session.close",
    })
    _MAX_FRAME_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        config: BridgeConfig,
        *,
        connector: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.config = config
        self._connector = connector
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._state = "idle"
        self._state_lock = threading.RLock()
        self._connection_task: asyncio.Task | None = None
        self._socket: Any = None
        self._ready_event: asyncio.Event | None = None
        self._connect_error: LiveError | None = None
        self._next_id = 0
        self._pending: dict[str, _PendingCall] = {}
        self._retired_request_ids: deque[str] = deque(maxlen=512)
        self._retired_request_id_set: set[str] = set()
        self._replay_epoch: str | None = None
        self._epoch_changed_on_connect = False
        self._replay_hold: dict[str, list[dict[str, Any]]] | None = None
        self._replay_hold_bytes = 0
        self._replay_gap_allowed: set[str] = set()
        self._last_replay: dict[str, Any] = {
            "replayed": 0, "truncated": [], "errors": [], "epoch_changed": False,
        }
        self._last_connect_url = ""
        self._ticket_consumed = False
        self._runtime_access_token: str | None = None
        self._runtime_refresh_token: str | None = None
        self._last_inbound = 0.0
        self._heartbeat_task: asyncio.Task | None = None

        # Event state is intentionally memory-only. Durable history/status remain
        # the recovery authority and the SQLite bridge registry never receives
        # raw event/prompt content.
        self._events: OrderedDict[str, deque[_BufferedEvent]] = OrderedDict()
        self._event_bytes: dict[str, int] = {}
        self._event_evicted_through: dict[str, int] = {}
        self._event_total_bytes = 0
        self._watermarks: dict[str, int] = {}
        self._events_condition = threading.Condition(self._state_lock)

    # ----- lifecycle -----------------------------------------------------

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def last_connect_url(self) -> str:
        """Test/diagnostic hook; never returned by MCP health (may contain a legacy token)."""
        with self._state_lock:
            return self._last_connect_url

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self._state = state
            self._events_condition.notify_all()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._state_lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            self._loop_ready.clear()

            def runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                with self._state_lock:
                    self._loop = loop
                    self._loop_ready.set()
                loop.run_forever()
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

            self._thread = threading.Thread(target=runner, name="hermes-live-ws", daemon=True)
            self._thread.start()
        if not self._loop_ready.wait(timeout=self.config.gateway_connect_timeout):
            raise LiveError("live WebSocket event loop failed to start", code="gateway_loop_start_failed")
        with self._state_lock:
            if self._loop is None:
                raise LiveError("live WebSocket event loop is unavailable", code="gateway_loop_start_failed")
            return self._loop

    def _run(self, awaitable: Coroutine[Any, Any, Any], *, timeout: float, method: str | None = None) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(awaitable, loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            future.cancel()
            try:
                cleanup = asyncio.run_coroutine_threadsafe(self._disconnect_async(), loop)
                cleanup.result(timeout=7.0)
            except Exception:
                pass
            if method in self._LIVE_MUTATIONS:
                raise LiveTransportUnknown() from exc
            raise LiveError("live gateway operation timed out", code="gateway_timeout") from exc

    def connect(self) -> dict[str, Any]:
        """Open the persistent socket and reconcile retained event cursors."""
        if self.state == "open":
            return self.health()
        timeout = self.config.gateway_connect_timeout + self.config.gateway_request_timeout + 5.0
        result = self._run(self._connect_async(), timeout=timeout)
        return result if isinstance(result, dict) else self.health()

    async def _connect_async(self) -> dict[str, Any]:
        if self._state == "open" and self._socket is not None:
            return self._last_replay
        if self._connection_task is None or self._connection_task.done():
            self._connect_error = None
            self._epoch_changed_on_connect = False
            self._ready_event = asyncio.Event()
            self._set_state("connecting")
            self._connection_task = asyncio.create_task(self._connection_loop())
        ready = self._ready_event
        if ready is None:
            raise LiveError("live gateway ready wait is unavailable", code="gateway_connect_failed")
        try:
            await asyncio.wait_for(ready.wait(), timeout=self.config.gateway_connect_timeout)
        except asyncio.TimeoutError as exc:
            if self._connection_task is not None:
                self._connection_task.cancel()
                await self._cancel_and_drain_task(self._connection_task)
            self._set_state("error")
            raise LiveError("live gateway did not send gateway.ready", code="gateway_ready_timeout") from exc
        if self._connect_error is not None:
            error = self._connect_error
            raise error
        if self._state != "open":
            raise LiveError("live gateway closed during handshake", code="gateway_connect_failed")
        try:
            # Hermes uses this one-shot handshake to decide whether it may send
            # approval/clarify/secret/vault requests. Bridge has no handler for
            # those server→client requests, so advertise false explicitly.
            await self._call_async(
                "client.capabilities", {"server_requests": False},
                timeout=min(10.0, self.config.gateway_request_timeout),
            )
        except LiveError:
            await self._disconnect_async()
            raise
        if self._epoch_changed_on_connect:
            self._last_replay = {
                "replayed": 0, "truncated": [], "errors": [], "epoch_changed": True,
            }
            return self._last_replay
        self._last_replay = await self._replay_async()
        return self._last_replay

    async def _connection_loop(self) -> None:
        socket = None
        ready = self._ready_event
        try:
            owner_target = await asyncio.to_thread(self._owner_attach_target)
            url = owner_target.uri if owner_target is not None else await asyncio.to_thread(self._connect_parameters)
            self._last_connect_url = url
            kwargs = {
                "open_timeout": self.config.gateway_connect_timeout,
                "close_timeout": 5.0,
                # Hermes has its own gateway.ping RPC heartbeat; this avoids a
                # second liveness policy fighting a long model/tool turn.
                "ping_interval": None,
                "max_size": self._MAX_FRAME_BYTES,
                "proxy": None,
            }
            if owner_target is not None:
                from websockets.asyncio.client import unix_connect

                socket = await unix_connect(str(owner_target.socket_path), uri=url, **kwargs)
            else:
                connector = self._connector
                if connector is None:
                    from websockets.asyncio.client import connect

                    connector = connect
                socket = await connector(url, **kwargs)
            self._socket = socket
            while True:
                raw = await asyncio.wait_for(socket.recv(), timeout=self.config.gateway_connect_timeout)
                self._last_inbound = time.monotonic()
                response = self._handle_frame(raw)
                if response is not None:
                    await socket.send(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
                if ready is not None and ready.is_set():
                    break
            if ready is None or not ready.is_set():
                raise LiveError("live gateway handshake did not include gateway.ready", code="gateway_ready_missing")
            self._set_state("open")
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(socket))
            while True:
                raw = await socket.recv()
                self._last_inbound = time.monotonic()
                response = self._handle_frame(raw)
                if response is not None:
                    await socket.send(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
        except LiveError as exc:
            if ready is not None and not ready.is_set():
                self._connect_error = exc
                ready.set()
            elif self._state not in {"closing", "closed"}:
                self._set_state("error")
        except asyncio.CancelledError:
            if ready is not None and not ready.is_set():
                self._connect_error = LiveError("live gateway connection cancelled", code="gateway_connect_cancelled")
                ready.set()
            raise
        except Exception as exc:  # noqa: BLE001 - library-specific close exceptions vary
            if ready is not None and not ready.is_set():
                message = "live gateway WebSocket connection failed"
                if self._http_status(exc) in {401, 403}:
                    self._connect_error = LiveAuthError(message)
                else:
                    self._connect_error = LiveError(message, code="gateway_connect_failed")
                ready.set()
            elif self._state not in {"closing", "closed"}:
                self._set_state("closed")
        finally:
            heartbeat = self._heartbeat_task
            self._heartbeat_task = None
            if heartbeat is not None and heartbeat is not asyncio.current_task():
                await self._cancel_and_drain_task(heartbeat)
            if socket is not None:
                try:
                    await socket.close()
                except Exception:
                    pass
            if self._socket is socket:
                self._socket = None
            self._fail_pending_unknown()
            if ready is not None and not ready.is_set():
                if self._connect_error is None:
                    self._connect_error = LiveError("live gateway closed before gateway.ready", code="gateway_connect_failed")
                ready.set()
            if self._state not in {"closing", "closed", "error"}:
                self._set_state("closed")

    @staticmethod
    def _http_status(exc: BaseException) -> int | None:
        for candidate in (exc, getattr(exc, "response", None)):
            for name in ("status_code", "status"):
                value = getattr(candidate, name, None)
                if isinstance(value, int):
                    return value
        return None

    async def _heartbeat_loop(self, socket: Any) -> None:
        interval = self.config.gateway_heartbeat_interval
        deadline = self.config.gateway_heartbeat_timeout
        if interval <= 0 or deadline <= 0:
            return
        while self._socket is socket and self._state == "open":
            await asyncio.sleep(interval)
            if self._socket is not socket or self._state != "open":
                return
            if time.monotonic() - self._last_inbound >= deadline:
                try:
                    await socket.close()
                except Exception:
                    pass
                return
            try:
                await self._call_async("gateway.ping", {}, timeout=min(interval, deadline))
            except Exception:
                try:
                    await socket.close()
                except Exception:
                    pass
                return

    def reconnect(self) -> dict[str, Any]:
        """Close the current generation, then open a fresh one and replay its gap."""
        timeout = self.config.gateway_connect_timeout + self.config.gateway_request_timeout + 7.0
        return self._run(self._reconnect_async(), timeout=timeout)

    async def _reconnect_async(self) -> dict[str, Any]:
        await self._disconnect_async()
        return await self._connect_async()

    def close(self) -> None:
        """Disconnect the current socket but keep the event loop reusable."""
        if self._loop is None:
            self._set_state("closed")
            return
        self._run(self._disconnect_async(), timeout=7.0)

    async def _disconnect_async(self) -> None:
        self._set_state("closing")
        socket, task = self._socket, self._connection_task
        try:
            if socket is not None:
                try:
                    await socket.close()
                except Exception:
                    pass
        finally:
            if task is not None and task is not asyncio.current_task():
                await self._cancel_and_drain_task(task)
            self._fail_pending_unknown()
            self._set_state("closed")

    @staticmethod
    async def _cancel_and_drain_task(task: asyncio.Task) -> None:
        """Cancel a transport task and await it so no reader survives close()."""
        if task.done():
            try:
                task.result()
            except BaseException:
                pass
            return
        await asyncio.sleep(0)
        if task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except BaseException:
            # A transport may have its own cancellation cleanup. Deliver a second
            # cancellation and yield once before the bounded drain wait; this
            # closes the common "caught cancel, then await cleanup" race.
            for _ in range(2):
                if task.done():
                    break
                task.cancel()
                await asyncio.sleep(0)
            if not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                except BaseException:
                    pass

    def shutdown(self) -> None:
        """Stop the private event loop during bridge process shutdown."""
        loop = self._loop
        if loop is None:
            return
        try:
            self._run(self._disconnect_async(), timeout=7.0)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=7.0)
        with self._state_lock:
            self._loop = None
            self._thread = None
            self._state = "closed"

    # ----- RPC -----------------------------------------------------------

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        if self.state != "open":
            self.connect()
        timeout_value = float(timeout or self.config.gateway_request_timeout)
        return self._run(
            self._call_async(method, params or {}, timeout=timeout_value),
            timeout=timeout_value + 3.0,
            method=method,
        )

    async def _call_async(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        socket = self._socket
        if socket is None or self._state != "open":
            raise LiveError("live gateway is not connected", code="gateway_not_connected")
        self._next_id += 1
        request_id = f"z{self._next_id}"
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _PendingCall(future, method)
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            await socket.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
        except Exception as exc:
            self._pending.pop(request_id, None)
            self._retire_request_id(request_id)
            raise LiveTransportUnknown() if method in self._LIVE_MUTATIONS else LiveError(
                "live gateway send failed", code="gateway_send_failed") from exc
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            self._retire_request_id(request_id)
            if not future.done():
                future.cancel()
            raise
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            self._retire_request_id(request_id)
            if method in self._LIVE_MUTATIONS:
                raise LiveTransportUnknown() from exc
            raise LiveError("live gateway request timed out", code="gateway_timeout") from exc

    def _fail_pending_unknown(self) -> None:
        for request_id, pending in list(self._pending.items()):
            self._pending.pop(request_id, None)
            self._retire_request_id(request_id)
            if not pending.future.done():
                pending.future.set_exception(LiveTransportUnknown())

    def _retire_request_id(self, request_id: str) -> None:
        if request_id in self._retired_request_id_set:
            return
        if len(self._retired_request_ids) == self._retired_request_ids.maxlen:
            oldest = self._retired_request_ids.popleft()
            self._retired_request_id_set.discard(oldest)
        self._retired_request_ids.append(request_id)
        self._retired_request_id_set.add(request_id)

    @staticmethod
    def _rpc_error_frame(code: int, message: str, request_id: str | None) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _validate_envelope(frame: Any) -> tuple[str, str | None, dict[str, Any] | None]:
        if not isinstance(frame, dict):
            raise _protocol_error("JSON-RPC frame must be an object")
        if frame.get("jsonrpc") != "2.0":
            raise _protocol_error("JSON-RPC version must be 2.0")
        if "method" in frame:
            method = _bounded_text(frame.get("method"), field="JSON-RPC method", max_chars=_MAX_METHOD_CHARS)
            if method == "event":
                if set(frame) != {"jsonrpc", "method", "params"} or not isinstance(frame.get("params"), dict):
                    raise _protocol_error("event notification envelope is invalid")
                return "event", None, frame["params"]
            if set(frame) - {"jsonrpc", "id", "method", "params"}:
                raise _protocol_error("server request envelope contains unknown members")
            raw_id = frame.get("id", _JSON_MISSING)
            if raw_id is _JSON_MISSING:
                return "invalid_server_request", None, None
            try:
                request_id = _rpc_id(raw_id)
            except ValueError:
                return "invalid_server_request", None, None
            if "params" in frame and not isinstance(frame["params"], dict):
                return "invalid_server_request", None, None
            return "server_request", request_id, frame.get("params") or {}
        if set(frame) - {"jsonrpc", "id", "result", "error"}:
            raise _protocol_error("response envelope contains unknown members")
        if "id" not in frame:
            raise _protocol_error("response id is missing")
        try:
            request_id = _rpc_id(frame["id"])
        except ValueError as exc:
            raise _protocol_error(str(exc)) from exc
        has_result = "result" in frame
        has_error = "error" in frame
        if has_result == has_error:
            raise _protocol_error("response must contain exactly one of result or error")
        if has_error:
            try:
                _validate_rpc_error(frame["error"])
            except ValueError as exc:
                raise _protocol_error(str(exc)) from exc
        return "response", request_id, frame

    def _handle_frame(self, raw: Any) -> dict[str, Any] | None:
        try:
            frame = _strict_json_loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _protocol_error(str(exc)) from exc
        kind, request_id, payload = self._validate_envelope(frame)
        if kind == "invalid_server_request":
            return self._rpc_error_frame(-32600, "invalid request", None)
        if kind == "server_request":
            return self._rpc_error_frame(-32601, "server requests are not supported", request_id)
        if kind == "response":
            assert isinstance(payload, dict) and request_id is not None
            pending = self._pending.pop(request_id, None)
            if pending is None or pending.future.done():
                raise _protocol_error(f"response id is not pending: {request_id}")
            self._retire_request_id(request_id)
            if "error" in payload:
                error = payload["error"]
                pending.future.set_exception(LiveRPCError(error["code"], error["message"], error.get("data")))
            else:
                pending.future.set_result(payload["result"])
            return None
        assert kind == "event" and isinstance(payload, dict)
        event = payload
        if event.get("type") == "gateway.ready":
            ready_payload = event.get("payload")
            if not isinstance(ready_payload, dict):
                raise _protocol_error("gateway.ready payload is invalid")
            try:
                epoch = _bounded_text(ready_payload.get("replay_epoch"), field="replay_epoch", max_chars=_MAX_REPLAY_EPOCH_CHARS)
            except ValueError as exc:
                raise _protocol_error(str(exc)) from exc
            self._adopt_replay_epoch(epoch)
            if self._ready_event is not None:
                self._ready_event.set()
            return None
        try:
            sid = self._validate_event(event)
        except ValueError as exc:
            raise _protocol_error(str(exc)) from exc
        if sid is not None and self._replay_hold is not None and sid in self._replay_hold:
            parked = self._replay_hold[sid]
            size = self._event_size(event)
            if len(parked) >= self.config.gateway_event_buffer_max or size > self.config.gateway_event_buffer_bytes:
                raise _protocol_error("replay capture limit exceeded")
            current_bytes = getattr(self, "_replay_hold_bytes", 0)
            if current_bytes + size > self.config.gateway_event_buffer_total_bytes:
                raise _protocol_error("replay capture limit exceeded")
            parked.append(copy.deepcopy(event))
            self._replay_hold_bytes = current_bytes + size
            return None
        self._accept_event(event)
        return None

    def _adopt_replay_epoch(self, epoch: str) -> None:
        if self._replay_epoch is not None and self._replay_epoch != epoch:
            with self._events_condition:
                self._watermarks.clear()
                self._events.clear()
                self._event_bytes.clear()
                self._event_evicted_through.clear()
                self._event_total_bytes = 0
                self._replay_gap_allowed.clear()
            self._epoch_changed_on_connect = True
        self._replay_epoch = epoch

    # ----- replay + event buffer ----------------------------------------

    @staticmethod
    def _event_size(event: dict[str, Any]) -> int:
        try:
            return len(json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8", errors="replace"))
        except Exception:
            return 0

    @staticmethod
    def _event_seq(event: dict[str, Any]) -> int | None:
        value = event.get("seq")
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    @staticmethod
    def _validate_event(event: dict[str, Any], *, expected_session: str | None = None) -> str | None:
        try:
            encoded_size = len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("event payload is not JSON-serializable") from exc
        if encoded_size > _MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError("event payload exceeds the maximum size")
        event_type = _bounded_text(event.get("type"), field="event type", max_chars=_MAX_EVENT_TYPE_CHARS)
        if "session_id" not in event:
            if "seq" in event:
                raise ValueError("sessionless event must not carry seq")
            return None
        raw_session_id = event.get("session_id")
        if raw_session_id == "":
            if "seq" in event:
                raise ValueError("sessionless event must not carry seq")
            if expected_session is not None:
                raise ValueError("event session_id does not match requested session")
            return None
        sid = _bounded_text(raw_session_id, field="event session_id", max_chars=_MAX_SESSION_ID_CHARS)
        if expected_session is not None and sid != expected_session:
            raise ValueError("event session_id does not match requested session")
        seq = event.get("seq")
        if type(seq) is not int or seq < 1:
            raise ValueError("event seq is invalid")
        if expected_session is not None and seq > 0:
            return sid
        return sid

    def _drop_session_state(self, sid: str) -> None:
        buffer = self._events.pop(sid, None)
        if buffer is not None:
            self._event_total_bytes -= self._event_bytes.pop(sid, 0)
        else:
            self._event_bytes.pop(sid, None)
        self._watermarks.pop(sid, None)
        self._event_evicted_through.pop(sid, None)
        self._replay_gap_allowed.discard(sid)

    def _ensure_session_slot(self, sid: str) -> bool:
        if sid in self._watermarks:
            return True
        limit = getattr(self.config, "gateway_event_sessions_max", 256)
        if len(self._watermarks) >= limit:
            oldest = next(iter(self._watermarks), None)
            if oldest is None:
                return False
            self._drop_session_state(oldest)
        self._watermarks[sid] = 0
        return True

    def _accept_event(self, event: dict[str, Any], *, allow_gap: bool = False) -> bool:
        sid = self._validate_event(event)
        if sid is None:
            return False
        seq = event["seq"]
        with self._events_condition:
            if not self._ensure_session_slot(sid):
                return False
            previous = self._watermarks.get(sid, 0)
            if seq <= previous:
                return False
            if previous and seq != previous + 1 and not allow_gap and sid not in self._replay_gap_allowed:
                return False
            self._watermarks[sid] = seq
            stored = copy.deepcopy(event)
            size = self._event_size(stored)
            if size > self.config.gateway_event_buffer_bytes:
                self._event_evicted_through[sid] = max(self._event_evicted_through.get(sid, 0), seq)
                self._events_condition.notify_all()
                return True
            buffer = self._events.get(sid)
            if buffer is None:
                buffer = deque()
                self._events[sid] = buffer
                self._event_bytes[sid] = 0
            else:
                self._events.move_to_end(sid)
            item = _BufferedEvent(seq, stored, size)
            buffer.append(item)
            self._event_bytes[sid] += size
            self._event_total_bytes += size
            while len(buffer) > self.config.gateway_event_buffer_max or self._event_bytes[sid] > self.config.gateway_event_buffer_bytes:
                removed = buffer.popleft()
                self._event_bytes[sid] -= removed.size
                self._event_total_bytes -= removed.size
                if removed.seq is not None:
                    self._event_evicted_through[sid] = max(self._event_evicted_through.get(sid, 0), removed.seq)
            while self._event_total_bytes > self.config.gateway_event_buffer_total_bytes:
                removed_any = False
                for old_sid, old_buffer in self._events.items():
                    if old_buffer:
                        removed = old_buffer.popleft()
                        self._event_bytes[old_sid] -= removed.size
                        self._event_total_bytes -= removed.size
                        if removed.seq is not None:
                            self._event_evicted_through[old_sid] = max(self._event_evicted_through.get(old_sid, 0), removed.seq)
                        removed_any = True
                        break
                if not removed_any:
                    break
            self._events_condition.notify_all()
            return True

    def _validate_replay_result(
        self, sid: str, last_seen: int, result: Any, parked: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        if not isinstance(result, dict):
            raise ValueError("replay response is not an object")
        required = {"session_id", "events", "latest_seq", "truncated", "count", "epoch", "open_requests"}
        if set(result) != required:
            raise ValueError("replay response shape is invalid")
        if result.get("session_id") != sid:
            raise ValueError("replay response session_id does not match request")
        events = result.get("events")
        if not isinstance(events, list) or len(events) > self.config.gateway_event_buffer_max:
            raise ValueError("replay events are invalid")
        latest_seq = result.get("latest_seq")
        if type(latest_seq) is not int or latest_seq < 0:
            raise ValueError("replay latest_seq is invalid")
        truncated = result.get("truncated")
        if type(truncated) is not bool:
            raise ValueError("replay truncated must be boolean")
        if result.get("count") != len(events):
            raise ValueError("replay count is inconsistent")
        epoch = result.get("epoch")
        if not isinstance(epoch, str) or not epoch or len(epoch) > _MAX_REPLAY_EPOCH_CHARS:
            raise ValueError("replay epoch is invalid")
        if self._replay_epoch is not None and epoch != self._replay_epoch:
            raise ValueError("replay epoch changed")
        if not isinstance(result.get("open_requests"), list) or len(result["open_requests"]) > self.config.gateway_event_buffer_max:
            raise ValueError("replay open_requests is invalid")
        replay_events: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("replay event is not an object")
            try:
                self._validate_event(event, expected_session=sid)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            replay_events.append(event)
        parked_events: list[dict[str, Any]] = []
        for event in parked:
            if not isinstance(event, dict):
                raise ValueError("parked event is not an object")
            self._validate_event(event, expected_session=sid)
            parked_events.append(event)
        combined = list(replay_events)
        previous = last_seen
        for event in replay_events:
            seq = event["seq"]
            if seq <= previous:
                raise ValueError("replay sequence regressed or duplicated")
            if not truncated and seq != previous + 1:
                raise ValueError("replay sequence is not contiguous")
            previous = seq
        duplicate_parked_seqs: set[int] = set()
        for event in parked_events:
            seq = event["seq"]
            if seq == previous and combined and event == combined[-1] and seq not in duplicate_parked_seqs:
                # A live frame may race with replay and be returned by both
                # channels. It is safe to discard exactly one byte-identical
                # duplicate; conflicting or repeated duplicates stay fatal.
                duplicate_parked_seqs.add(seq)
                continue
            if seq <= previous:
                raise ValueError("parked event regressed or duplicated")
            if not truncated and seq != previous + 1:
                raise ValueError("parked sequence is not contiguous")
            combined.append(event)
            previous = seq
        if latest_seq < last_seen or latest_seq < (previous if combined else last_seen):
            raise ValueError("replay latest_seq is behind validated events")
        if not truncated and latest_seq != (previous if combined else last_seen):
            raise ValueError("replay latest_seq is inconsistent")
        return combined, truncated

    async def _replay_async(self) -> dict[str, Any]:
        with self._state_lock:
            entries = list(self._watermarks.items())[:getattr(self.config, "gateway_event_sessions_max", 256)]
        replayed = 0
        truncated: list[str] = []
        errors: list[str] = []
        epoch_changed = False
        for sid, snapshot_last_seen in entries:
            with self._state_lock:
                last_seen = self._watermarks.get(sid, snapshot_last_seen)
                self._replay_hold = {sid: []}
                self._replay_hold_bytes = 0
            valid = False
            result: Any = None
            try:
                result = await self._call_async(
                    "session.events.since", {"session_id": sid, "last_seen": last_seen},
                    timeout=min(10.0, self.config.gateway_request_timeout),
                )
                parked = list(self._replay_hold.get(sid, [])) if self._replay_hold is not None else []
                combined, is_truncated = self._validate_replay_result(sid, last_seen, result, parked)
                if is_truncated:
                    truncated.append(sid)
                    self._replay_gap_allowed.add(sid)
                replay_count = len(combined)
                for event in combined:
                    if not self._accept_event(event, allow_gap=is_truncated):
                        raise ValueError("validated replay event was not accepted")
                replayed += replay_count
                valid = True
            except LiveError as exc:
                errors.append(exc.code)
            except ValueError as exc:
                errors.append("invalid_replay_response")
                if "replay epoch changed" in str(exc) and isinstance(result, dict):
                    epoch = result.get("epoch")
                    if isinstance(epoch, str) and epoch:
                        self._adopt_replay_epoch(epoch)
                        epoch_changed = True
            finally:
                with self._state_lock:
                    self._replay_hold = None
                    self._replay_hold_bytes = 0
                if not valid:
                    continue
        return {
            "replayed": replayed,
            "truncated": truncated,
            "errors": errors,
            "epoch_changed": epoch_changed,
        }

    def watermarks(self) -> dict[str, int]:
        with self._state_lock:
            return dict(self._watermarks)

    def events(self, session_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._state_lock:
            buffer = self._events.get(session_id)
            if buffer is None:
                return []
            return [copy.deepcopy(item.event) for item in buffer if item.seq is None or item.seq > after_seq]

    def events_truncated(self, session_id: str, *, after_seq: int = 0) -> bool:
        """True when the requested cursor predates an event evicted from the local ring."""
        with self._state_lock:
            return after_seq < self._event_evicted_through.get(session_id, 0)

    def next_completion(self, session_id: str, *, after_seq: int = 0, timeout: float = 120.0) -> dict[str, Any] | None:
        """Wait for the next ``message.complete`` event after ``after_seq``.

        Returns ``None`` on timeout. Deliberately candidate-only: whether a
        completion belongs to a specific bridge request is an ownership decision
        the service makes with gateway-side evidence, not a buffer-order fact.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._events_condition:
            while True:
                buffer = self._events.get(session_id) or ()
                for item in buffer:
                    if item.seq is not None and item.seq > after_seq and item.event.get("type") == "message.complete":
                        return copy.deepcopy(item.event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._events_condition.wait(timeout=remaining)

    # ----- diagnostics ---------------------------------------------------

    def health(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "status": "healthy" if self._state == "open" else "unavailable",
                "connection_state": self._state,
                "auth_mode": self.config.live_auth_mode(),
                "replay_epoch": self._replay_epoch,
                "watermarks": dict(self._watermarks),
                "event_sessions": len(self._events),
                "event_count": sum(len(buffer) for buffer in self._events.values()),
                "event_bytes": self._event_total_bytes,
                "event_evicted_sessions": sum(1 for value in self._event_evicted_through.values() if value),
                "last_replay": copy.deepcopy(self._last_replay),
                "pending_requests": len(self._pending),
            }

    # ----- authentication ------------------------------------------------

    def _owner_attach_target(self):
        lease_path = self.config.gateway_owner_lease_path
        if lease_path is None:
            return None
        from .local_attach import OwnerAttachError, load_owner_attach_target

        try:
            return load_owner_attach_target(lease_path)
        except OwnerAttachError as exc:
            raise LiveError(str(exc), code="owner_attach_failed") from exc

    def _connect_parameters(self) -> str:
        if not self.config.gateway_url:
            raise LiveError("gateway_url is not configured", code="gateway_not_configured")
        access_token = self._runtime_access_token or self.config.resolved_gateway_access_token()
        ticket = self.config.resolved_gateway_ticket()
        token = self.config.resolved_gateway_token()
        if access_token or self.config.resolved_gateway_refresh_token():
            fresh_ticket = self._mint_ticket(access_token or "")
            return self._with_auth_query(self.config.gateway_url, "ticket", fresh_ticket)
        if ticket:
            if self._ticket_consumed:
                raise LiveAuthError("configured one-use gateway ticket is already consumed; provide a fresh ticket")
            self._ticket_consumed = True
            return self._with_auth_query(self.config.gateway_url, "ticket", ticket)
        if token:
            parts = urlsplit(self.config.gateway_url)
            query = list(parse_qsl(parts.query, keep_blank_values=True))
            query.append(("token", token))
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        raise LiveAuthError(
            "live gateway authentication is not configured; set an access-token env for gated mode "
            "or a legacy dashboard token for loopback mode")

    @staticmethod
    def _with_auth_query(url: str, key: str, value: str) -> str:
        parts = urlsplit(url)
        query = [(name, item) for name, item in parse_qsl(parts.query, keep_blank_values=True) if name != key]
        query.append((key, value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _mint_ticket(self, access_token: str) -> str:
        base = self._gateway_http_base()
        ticket_url = base.rstrip("/") + "/api/auth/ws-ticket"
        refresh_url = base.rstrip("/") + "/auth/native/refresh"
        refresh_token = self._runtime_refresh_token or self.config.resolved_gateway_refresh_token()
        try:
            import httpx
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=self.config.gateway_connect_timeout) as client:
                if not access_token:
                    if not refresh_token or not self._refresh_access_token(client, refresh_url, refresh_token):
                        raise LiveAuthError("live gateway access token is missing and refresh failed")
                    access_token = self._runtime_access_token or ""
                response = client.post(
                    ticket_url,
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                )
                if response.status_code in {401, 403} and refresh_token:
                    if not self._refresh_access_token(client, refresh_url, refresh_token):
                        raise LiveAuthError("live gateway access token and refresh token were rejected")
                    access_token = self._runtime_access_token or ""
                    response = client.post(
                        ticket_url,
                        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                    )
        except Exception as exc:  # noqa: BLE001 - do not surface credential-bearing URL/library text
            if isinstance(exc, LiveAuthError):
                raise
            raise LiveAuthError("live gateway WS ticket request failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise LiveAuthError(f"live gateway WS ticket request rejected (HTTP {response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LiveAuthError("live gateway WS ticket response was invalid") from exc
        value = payload.get("ticket") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value or len(value) > 512:
            raise LiveAuthError("live gateway WS ticket response did not contain a valid ticket")
        return value

    def _gateway_http_base(self) -> str:
        base = self.config.gateway_http_url
        if base:
            return base
        parts = urlsplit(self.config.gateway_url or "")
        scheme = "https" if parts.scheme == "wss" else "http"
        path = parts.path
        suffix = "/api/ws"
        if path.endswith(suffix):
            path = path[:-len(suffix)]
        return urlunsplit((scheme, parts.netloc, path.rstrip("/"), "", ""))

    def _refresh_access_token(self, client: Any, url: str, refresh_token: str) -> bool:
        body: dict[str, str] = {"refresh_token": refresh_token}
        provider = self.config.gateway_auth_provider.strip()
        if provider:
            body["provider"] = provider
        try:
            response = client.post(url, json=body, headers={"Accept": "application/json"})
            if response.status_code < 200 or response.status_code >= 300:
                return False
            payload = response.json()
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        access_token = payload.get("access_token")
        new_refresh_token = payload.get("refresh_token")
        if not isinstance(access_token, str) or not access_token:
            return False
        self._runtime_access_token = access_token
        if isinstance(new_refresh_token, str) and new_refresh_token:
            self._runtime_refresh_token = new_refresh_token
        return True
