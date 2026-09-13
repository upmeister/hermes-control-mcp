from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import BridgeConfig


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
        self._replay_epoch: str | None = None
        self._epoch_changed_on_connect = False
        self._replay_hold: dict[str, list[dict[str, Any]]] | None = None
        self._last_replay: dict[str, Any] = {
            "replayed": 0, "truncated": [], "errors": [], "epoch_changed": False,
        }
        self._last_connect_url = ""
        self._ticket_consumed = False
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
            self._set_state("error")
            raise LiveError("live gateway did not send gateway.ready", code="gateway_ready_timeout") from exc
        if self._connect_error is not None:
            error = self._connect_error
            raise error
        if self._state != "open":
            raise LiveError("live gateway closed during handshake", code="gateway_connect_failed")
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
            url = await asyncio.to_thread(self._connect_parameters)
            self._last_connect_url = url
            connector = self._connector
            if connector is None:
                from websockets.asyncio.client import connect
                connector = connect
            kwargs = {
                "open_timeout": self.config.gateway_connect_timeout,
                "close_timeout": 5.0,
                # Hermes has its own gateway.ping RPC heartbeat; this avoids a
                # second liveness policy fighting a long model/tool turn.
                "ping_interval": None,
                "max_size": self._MAX_FRAME_BYTES,
                "proxy": None,
            }
            socket = await connector(url, **kwargs)
            self._socket = socket
            while True:
                raw = await asyncio.wait_for(socket.recv(), timeout=self.config.gateway_connect_timeout)
                self._last_inbound = time.monotonic()
                self._handle_frame(raw)
                if ready is not None and ready.is_set():
                    break
            if ready is None or not ready.is_set():
                raise LiveError("live gateway handshake did not include gateway.ready", code="gateway_ready_missing")
            self._set_state("open")
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(socket))
            while True:
                raw = await socket.recv()
                self._last_inbound = time.monotonic()
                self._handle_frame(raw)
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
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None
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
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass
        if task is not None and task is not asyncio.current_task() and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except Exception:
                task.cancel()
        self._fail_pending_unknown()
        self._set_state("closed")

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
            raise LiveTransportUnknown() if method in self._LIVE_MUTATIONS else LiveError(
                "live gateway send failed", code="gateway_send_failed") from exc
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            if method in self._LIVE_MUTATIONS:
                raise LiveTransportUnknown() from exc
            raise LiveError("live gateway request timed out", code="gateway_timeout") from exc

    def _fail_pending_unknown(self) -> None:
        for request_id, pending in list(self._pending.items()):
            self._pending.pop(request_id, None)
            if not pending.future.done():
                pending.future.set_exception(LiveTransportUnknown())

    def _handle_frame(self, raw: Any) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            frame = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(frame, dict):
            return
        request_id = frame.get("id")
        if request_id is not None:
            pending = self._pending.pop(str(request_id), None)
            if pending is None or pending.future.done():
                return
            if isinstance(frame.get("error"), dict):
                error = frame["error"]
                pending.future.set_exception(LiveRPCError(error.get("code"), str(error.get("message") or "Hermes RPC failed"), error.get("data")))
            else:
                pending.future.set_result(frame.get("result"))
            return
        if frame.get("method") != "event" or not isinstance(frame.get("params"), dict):
            return
        event = frame["params"]
        if event.get("type") == "gateway.ready":
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            epoch = payload.get("replay_epoch")
            if isinstance(epoch, str) and epoch:
                self._adopt_replay_epoch(epoch)
            if self._ready_event is not None:
                self._ready_event.set()
            return
        sid = event.get("session_id")
        if isinstance(sid, str) and self._replay_hold is not None and sid in self._replay_hold:
            self._replay_hold[sid].append(copy.deepcopy(event))
            return
        self._accept_event(event)

    def _adopt_replay_epoch(self, epoch: str) -> None:
        if self._replay_epoch is not None and self._replay_epoch != epoch:
            with self._events_condition:
                self._watermarks.clear()
                self._events.clear()
                self._event_bytes.clear()
                self._event_evicted_through.clear()
                self._event_total_bytes = 0
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

    def _accept_event(self, event: dict[str, Any]) -> None:
        with self._events_condition:
            if not isinstance(event, dict) or not event.get("type"):
                return
            sid = event.get("session_id")
            seq = self._event_seq(event)
            if isinstance(sid, str) and seq is not None:
                previous = self._watermarks.get(sid, 0)
                if seq <= previous:
                    return
                self._watermarks[sid] = seq
            if not isinstance(sid, str) or not sid:
                return
            stored = copy.deepcopy(event)
            size = self._event_size(stored)
            if size > self.config.gateway_event_buffer_bytes:
                if seq is not None:
                    self._event_evicted_through[sid] = max(self._event_evicted_through.get(sid, 0), seq)
                self._events_condition.notify_all()
                return
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

    async def _replay_async(self) -> dict[str, Any]:
        with self._state_lock:
            entries = list(self._watermarks.items())
            hold = {sid: [] for sid, _last_seen in entries}
            self._replay_hold = hold
        replayed = 0
        truncated: list[str] = []
        errors: list[str] = []
        try:
            for sid, last_seen in entries:
                try:
                    result = await self._call_async(
                        "session.events.since", {"session_id": sid, "last_seen": last_seen},
                        timeout=min(10.0, self.config.gateway_request_timeout),
                    )
                except LiveError as exc:
                    errors.append(exc.code)
                    continue
                if not isinstance(result, dict):
                    errors.append("invalid_replay_response")
                    continue
                epoch = result.get("epoch")
                if isinstance(epoch, str) and epoch:
                    if self._replay_epoch is not None and epoch != self._replay_epoch:
                        self._adopt_replay_epoch(epoch)
                        errors.append("replay_epoch_changed")
                        continue
                    self._replay_epoch = epoch
                if result.get("truncated"):
                    truncated.append(sid)
                events = result.get("events")
                if not isinstance(events, list):
                    continue
                for event in events:
                    if isinstance(event, dict):
                        before = self._watermarks.get(sid, 0)
                        self._accept_event(event)
                        if self._watermarks.get(sid, 0) > before:
                            replayed += 1
        finally:
            with self._state_lock:
                parked = self._replay_hold or {}
                self._replay_hold = None
            for events in parked.values():
                for event in events:
                    before = self._watermarks.get(event.get("session_id", ""), 0)
                    self._accept_event(event)
                    if self._watermarks.get(event.get("session_id", ""), 0) > before:
                        replayed += 1
        return {
            "replayed": replayed,
            "truncated": truncated,
            "errors": errors,
            "epoch_changed": bool("replay_epoch_changed" in errors),
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

    def wait_for_completion(self, session_id: str, *, after_seq: int = 0, timeout: float = 120.0) -> dict[str, Any] | None:
        """Wait for the next ``message.start`` → ``message.complete`` pair."""
        deadline = time.monotonic() + max(0.0, timeout)
        started = False
        start_seq: int | None = None
        with self._events_condition:
            while True:
                buffer = self._events.get(session_id) or ()
                for item in buffer:
                    if item.seq is not None and item.seq <= after_seq:
                        continue
                    kind = item.event.get("type")
                    if not started and kind == "message.start":
                        started = True
                        start_seq = item.seq
                        continue
                    if started and kind == "message.complete":
                        if start_seq is None or item.seq is None or item.seq > start_seq:
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

    def _connect_parameters(self) -> str:
        if not self.config.gateway_url:
            raise LiveError("gateway_url is not configured", code="gateway_not_configured")
        access_token = self.config.resolved_gateway_access_token()
        ticket = self.config.resolved_gateway_ticket()
        token = self.config.resolved_gateway_token()
        if access_token:
            fresh_ticket = self._mint_ticket(access_token)
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
        base = self.config.gateway_http_url
        if not base:
            parts = urlsplit(self.config.gateway_url or "")
            scheme = "https" if parts.scheme == "wss" else "http"
            path = parts.path
            suffix = "/api/ws"
            if path.endswith(suffix):
                path = path[:-len(suffix)]
            base = urlunsplit((scheme, parts.netloc, path.rstrip("/"), "", ""))
        url = base.rstrip("/") + "/api/auth/ws-ticket"
        try:
            import httpx
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=self.config.gateway_connect_timeout) as client:
                response = client.post(url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001 - do not surface credential-bearing URL/library text
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
