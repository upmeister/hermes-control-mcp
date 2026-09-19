from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from .config import BridgeConfig
from .profiles import PROFILE_ID_RE, named_profile_api_key


@dataclass(slots=True)
class APIResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class APIError(RuntimeError):
    """An HTTP/API or transport error safe to return to the MCP caller."""

    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False):
        self.status = int(status)
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


def _redact(text: str, *secrets: str | None) -> str:
    safe = str(text or "")
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "[REDACTED]")
    safe = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", safe)
    safe = re.sub(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", safe)
    return safe[:2000]


def _json_body(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise APIError(502, "invalid_response", "Hermes API returned invalid JSON") from exc
    return parsed if isinstance(parsed, dict) else {"data": parsed}


class HermesAPIClient:
    """Small stdlib HTTP client for the authenticated Hermes API Server.

    The default profile keeps the configured api_url and default key. A named
    profile routes through the Hermes multiplex prefix ``/p/<profile>/...`` and
    resolves that profile's OWN ``API_SERVER_KEY`` from its profile .env at
    request time; the default key is never borrowed, and a missing named key
    fails closed. Key rotation needs no bridge state migration because named
    keys are re-read per request.
    """

    def __init__(self, config: BridgeConfig, *, transport: Callable[..., APIResponse] | None = None):
        self.config = config
        self._api_key = config.resolved_api_key()
        # Every key this client can ever send is tracked for error redaction:
        # an upstream error message may embed any configured key, not only the
        # key used by the current request. Key values are never logged.
        self._known_keys: set[str] = {self._api_key} if self._api_key else set()
        self._transport = transport

    def _endpoint(self, profile: str | None) -> tuple[str, str]:
        """(base_url, bearer_key) for the requested profile.

        Raises APIError (never leaking key material) when the profile id is
        invalid, the configured api_url cannot take a profile prefix, or the
        named profile has no usable key of its own.
        """
        if not profile or profile == "default":
            return self.config.api_url, self._api_key
        if not PROFILE_ID_RE.match(profile):
            raise APIError(0, "invalid_profile", f"refusing to route invalid profile id through the API")
        parts = urlsplit(self.config.api_url)
        if parts.path not in ("", "/"):
            raise APIError(
                0,
                "profile_route_config_error",
                "api_url already contains a path; /p/<profile> routing requires a root api_url",
            )
        key = named_profile_api_key(profile, self.config.resolved_profiles_root())
        if not key:
            raise APIError(
                0,
                "profile_key_unavailable",
                f"Named profile {profile!r} has no usable API_SERVER_KEY in its profile .env; "
                "the default profile key is never inherited for named profiles",
            )
        self._known_keys.add(key)
        return f"{parts.scheme}://{parts.netloc}/p/{profile}", key

    def _url(self, path: str, base: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return base + path

    def _headers(self, key: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "hermes-zcode-bridge/0.1",
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> APIResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return APIResponse(response.status, dict(response.headers.items()), response.read())
        except urllib.error.HTTPError as exc:
            return APIResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read())
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise APIError(
                0,
                "transport_unknown",
                "Hermes API transport outcome is unknown; retry explicitly with the same request ID",
                retryable=True,
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        raw: bool = False,
        profile: str | None = None,
    ) -> dict[str, Any] | bytes:
        base, key = self._endpoint(profile)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else None
        headers = self._headers(key, extra_headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = (self._transport or self._urllib_transport)(
                method, self._url(path, base), headers, body, self.config.request_timeout
            )
        except APIError:
            raise
        except (TimeoutError, socket.timeout, OSError) as exc:
            raise APIError(
                0,
                "transport_unknown",
                "Hermes API transport outcome is unknown; retry explicitly with the same request ID",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise APIError(0, "transport_unknown", "Hermes API transport failed; reconcile before retrying", retryable=True) from exc

        if response.status < 200 or response.status >= 300:
            try:
                data = _json_body(response.body)
            except APIError:
                data = {}
            raw_error = data.get("error", data)
            if isinstance(raw_error, dict):
                code = str(raw_error.get("code") or f"http_{response.status}")
                message = str(raw_error.get("message") or raw_error.get("detail") or "Hermes API request failed")
            else:
                code = f"http_{response.status}"
                message = str(raw_error or "Hermes API request failed")
            raise APIError(
                response.status,
                code,
                _redact(message, *self._known_keys),
                retryable=response.status in {408, 409, 425, 429} or response.status >= 500,
            )
        if raw:
            return response.body
        return _json_body(response.body)

    def get(self, path: str) -> dict[str, Any]:
        result = self._request("GET", path)
        return result if isinstance(result, dict) else {}

    def submit_run(
        self,
        *,
        prompt: str,
        idempotency_key: str,
        session_id: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        instructions: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"input": prompt}
        for key, value in (
            ("session_id", session_id),
            ("model", model),
            ("provider", provider),
            ("instructions", instructions),
        ):
            if value:
                payload[key] = value
        result = self._request(
            "POST",
            "/v1/runs",
            payload=payload,
            extra_headers={"Idempotency-Key": idempotency_key},
            profile=profile,
        )
        return result if isinstance(result, dict) else {}

    def status(self, run_id: str, *, profile: str | None = None) -> dict[str, Any]:
        result = self._request("GET", f"/v1/runs/{urllib.parse.quote(run_id, safe='')}", profile=profile)
        return result if isinstance(result, dict) else {}

    def stop(self, run_id: str, *, profile: str | None = None) -> dict[str, Any]:
        result = self._request(
            "POST", f"/v1/runs/{urllib.parse.quote(run_id, safe='')}/stop", payload={}, profile=profile
        )
        return result if isinstance(result, dict) else {}

    def steer(self, run_id: str, text: str, *, profile: str | None = None) -> dict[str, Any]:
        result = self._request(
            "POST", f"/v1/runs/{urllib.parse.quote(run_id, safe='')}/steer", payload={"input": text},
            profile=profile,
        )
        return result if isinstance(result, dict) else {}

    def history(self, session_id: str, *, limit: int = 100, profile: str | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode({"order": "oldest", "limit": max(1, min(int(limit), 500))})
        result = self._request(
            "GET", f"/api/sessions/{urllib.parse.quote(session_id, safe='')}/messages?{query}",
            profile=profile,
        )
        return result if isinstance(result, dict) else {}

    def events(self, run_id: str, *, timeout: float | None = None, profile: str | None = None) -> list[dict[str, Any]]:
        """Collect the server's SSE stream; status polling remains recovery authority."""
        raw = self._request(
            "GET",
            f"/v1/runs/{urllib.parse.quote(run_id, safe='')}/events",
            extra_headers={"Accept": "text/event-stream"},
            raw=True,
            profile=profile,
        )
        if not isinstance(raw, bytes):
            return []
        events: list[dict[str, Any]] = []
        for block in raw.decode("utf-8", errors="replace").split("\n\n"):
            data_lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
            if not data_lines:
                continue
            try:
                parsed = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events

    def health(self, *, profile: str | None = None) -> dict[str, Any]:
        result = self._request("GET", "/health", profile=profile)
        return result if isinstance(result, dict) else {}

    def models(self, *, profile: str | None = None) -> dict[str, Any]:
        result = self._request("GET", "/v1/models", profile=profile)
        return result if isinstance(result, dict) else {}

    def capabilities(self, *, profile: str | None = None) -> dict[str, Any]:
        result = self._request("GET", "/v1/capabilities", profile=profile)
        return result if isinstance(result, dict) else {}
