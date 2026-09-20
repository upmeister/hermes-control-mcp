from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


DEFAULT_API_URL = "http://127.0.0.1:8642"
DEFAULT_API_KEY_ENV = "API_SERVER_KEY"
DEFAULT_GATEWAY_TOKEN_ENV = "HERMES_DASHBOARD_SESSION_TOKEN"
DEFAULT_GATEWAY_ACCESS_TOKEN_ENV = "HERMES_DASHBOARD_ACCESS_TOKEN"
DEFAULT_GATEWAY_REFRESH_TOKEN_ENV = "HERMES_DASHBOARD_REFRESH_TOKEN"


class ConfigError(ValueError):
    """The bridge cannot start safely with the supplied configuration."""


def hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def default_profiles_root() -> Path:
    """Named-profiles root, mirroring Hermes ``hermes_cli.profiles`` layout."""
    return hermes_home() / "profiles"


def default_state_db() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")).expanduser()
    return state_home / "hermes-zcode-bridge" / "bridge.db"


def read_env_value(path: Path, key: str) -> str | None:
    """Read one dotenv-style value without exporting or logging the secret."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        name, separator, raw = candidate.partition("=")
        if separator and name.strip() == key:
            raw = raw.strip()
            if not raw:
                return ""
            try:
                parsed = shlex.split(raw, comments=False, posix=True)
            except ValueError as exc:
                raise ConfigError(f"Invalid value for {key} in {path}") from exc
            return parsed[0] if parsed else ""
    return None


def _resolve_secret(explicit: str | None, env_name: str, env_file: Path | None) -> str | None:
    """Resolve a secret without putting its value into config diagnostics."""
    if explicit is not None:
        return explicit or None
    if env_name:
        from_process = os.environ.get(env_name)
        if from_process:
            return from_process
    if env_file is not None and env_name:
        from_file = read_env_value(env_file, env_name)
        if from_file:
            return from_file
    return None


def resolve_api_key(
    *, api_key: str | None = None, api_key_env: str = DEFAULT_API_KEY_ENV, env_file: Path | None = None
) -> str:
    """Resolve a key from process scope first, then the server-side Hermes dotenv file."""
    value = _resolve_secret(api_key, api_key_env, env_file or (hermes_home() / ".env"))
    if value:
        return value
    dotenv = env_file or (hermes_home() / ".env")
    raise ConfigError(f"Missing API key in process environment or {dotenv}")


def _validate_gateway_url(value: str, *, field_name: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"ws", "wss"} or not parts.hostname:
        raise ConfigError(f"{field_name} must be a ws:// or wss:// URL")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ConfigError(f"{field_name} must not contain userinfo or a fragment")
    auth_params = {"token", "ticket", "internal"}
    if any(key in auth_params for key, _value in parse_qsl(parts.query, keep_blank_values=True)):
        raise ConfigError(f"{field_name} must not contain credentials in its query")
    return value


def _validate_gateway_http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError("gateway_http_url must be an http:// or https:// URL")
    if parts.username is not None or parts.password is not None or parts.fragment or parts.query:
        raise ConfigError("gateway_http_url must not contain userinfo, query credentials, or a fragment")
    return value.rstrip("/")


@dataclass(slots=True)
class BridgeConfig:
    api_url: str = DEFAULT_API_URL
    api_key: str | None = field(default=None, repr=False)
    state_db: Path = field(default_factory=default_state_db)
    # Non-secret routing root for named-profile .env resolution; a named
    # profile's own API_SERVER_KEY is read from <profiles_root>/<profile>/.env.
    profiles_root: Path | None = None
    request_timeout: float = 120.0
    poll_interval: float = 0.5
    api_key_env: str = DEFAULT_API_KEY_ENV
    env_file: Path | None = None
    # Live TUI gateway. URL is behavioral configuration; credentials remain env/file-only.
    gateway_url: str | None = None
    # Explicit local owner-adapter lease. This mode uses a same-user Unix socket
    # and never resolves dashboard credentials or web tickets.
    gateway_owner_lease_path: Path | None = None
    gateway_http_url: str | None = None
    gateway_token: str | None = field(default=None, repr=False)
    gateway_token_env: str = DEFAULT_GATEWAY_TOKEN_ENV
    gateway_access_token: str | None = field(default=None, repr=False)
    gateway_access_token_env: str = DEFAULT_GATEWAY_ACCESS_TOKEN_ENV
    gateway_refresh_token: str | None = field(default=None, repr=False)
    gateway_refresh_token_env: str = DEFAULT_GATEWAY_REFRESH_TOKEN_ENV
    gateway_auth_provider: str = ""
    gateway_ticket: str | None = field(default=None, repr=False)
    gateway_ticket_env: str = ""
    gateway_connect_timeout: float = 15.0
    gateway_request_timeout: float = 120.0
    gateway_heartbeat_interval: float = 15.0
    gateway_heartbeat_timeout: float = 45.0
    gateway_event_buffer_max: int = 512
    gateway_event_buffer_bytes: int = 4 * 1024 * 1024
    gateway_event_buffer_total_bytes: int = 64 * 1024 * 1024
    gateway_event_sessions_max: int = 256

    def __post_init__(self) -> None:
        self.api_url = self.api_url.rstrip("/")
        if not self.api_url:
            raise ConfigError("API URL must not be empty")
        self.state_db = Path(self.state_db).expanduser()
        if self.profiles_root is not None:
            self.profiles_root = Path(self.profiles_root).expanduser()
        if self.env_file is not None:
            self.env_file = Path(self.env_file).expanduser()
        if self.gateway_owner_lease_path is not None:
            self.gateway_owner_lease_path = Path(self.gateway_owner_lease_path).expanduser()
        if self.request_timeout <= 0:
            raise ConfigError("request_timeout must be positive")
        if self.poll_interval <= 0:
            raise ConfigError("poll_interval must be positive")
        if self.gateway_url:
            self.gateway_url = _validate_gateway_url(self.gateway_url.rstrip("/"), field_name="gateway_url")
        if self.gateway_http_url:
            self.gateway_http_url = _validate_gateway_http_url(self.gateway_http_url)
        for name, value in (
            ("gateway_connect_timeout", self.gateway_connect_timeout),
            ("gateway_request_timeout", self.gateway_request_timeout),
            ("gateway_heartbeat_timeout", self.gateway_heartbeat_timeout),
        ):
            if value <= 0:
                raise ConfigError(f"{name} must be positive")
        if self.gateway_heartbeat_interval < 0:
            raise ConfigError("gateway_heartbeat_interval must not be negative")
        if (
            self.gateway_event_buffer_max < 1
            or self.gateway_event_buffer_bytes < 1
            or self.gateway_event_buffer_total_bytes < 1
            or self.gateway_event_sessions_max < 1
        ):
            raise ConfigError("live event buffer limits must be positive")

    def resolved_api_key(self) -> str:
        return resolve_api_key(api_key=self.api_key, api_key_env=self.api_key_env, env_file=self.env_file)

    def resolved_profiles_root(self) -> Path:
        return self.profiles_root if self.profiles_root is not None else default_profiles_root()

    def resolved_gateway_token(self) -> str | None:
        return _resolve_secret(self.gateway_token, self.gateway_token_env, self.env_file or (hermes_home() / ".env"))

    def resolved_gateway_access_token(self) -> str | None:
        return _resolve_secret(self.gateway_access_token, self.gateway_access_token_env, self.env_file or (hermes_home() / ".env"))

    def resolved_gateway_ticket(self) -> str | None:
        return _resolve_secret(self.gateway_ticket, self.gateway_ticket_env, self.env_file or (hermes_home() / ".env"))

    def resolved_gateway_refresh_token(self) -> str | None:
        return _resolve_secret(self.gateway_refresh_token, self.gateway_refresh_token_env, self.env_file or (hermes_home() / ".env"))

    def live_auth_mode(self) -> str:
        if self.gateway_owner_lease_path is not None:
            return "owner_adapter"
        if self.resolved_gateway_access_token() and self.resolved_gateway_refresh_token():
            return "access_token_refresh"
        if self.resolved_gateway_access_token():
            return "access_token"
        if self.resolved_gateway_refresh_token():
            return "refresh_token"
        if self.resolved_gateway_ticket():
            return "ticket"
        if self.resolved_gateway_token():
            return "legacy_token"
        return "unconfigured"
