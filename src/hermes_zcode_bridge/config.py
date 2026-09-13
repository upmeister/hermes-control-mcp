from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


DEFAULT_API_URL = "http://127.0.0.1:8642"
DEFAULT_API_KEY_ENV = "API_SERVER_KEY"


class ConfigError(ValueError):
    """The bridge cannot start safely with the supplied configuration."""


def hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


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


def resolve_api_key(
    *, api_key: str | None = None, api_key_env: str = DEFAULT_API_KEY_ENV, env_file: Path | None = None
) -> str:
    """Resolve a key from process scope first, then the server-side Hermes dotenv file."""
    if api_key is not None:
        if not api_key:
            raise ConfigError("API key must not be empty")
        return api_key
    from_process = os.environ.get(api_key_env)
    if from_process:
        return from_process
    dotenv = env_file or (hermes_home() / ".env")
    from_file = read_env_value(dotenv, api_key_env)
    if from_file:
        return from_file
    raise ConfigError(f"Missing API key in process environment or {dotenv}")


@dataclass(slots=True)
class BridgeConfig:
    api_url: str = DEFAULT_API_URL
    api_key: str | None = None
    state_db: Path = default_state_db()
    request_timeout: float = 120.0
    poll_interval: float = 0.5
    api_key_env: str = DEFAULT_API_KEY_ENV
    env_file: Path | None = None

    def __post_init__(self) -> None:
        self.api_url = self.api_url.rstrip("/")
        if not self.api_url:
            raise ConfigError("API URL must not be empty")
        self.state_db = Path(self.state_db).expanduser()
        if self.env_file is not None:
            self.env_file = Path(self.env_file).expanduser()
        if self.request_timeout <= 0:
            raise ConfigError("request_timeout must be positive")
        if self.poll_interval <= 0:
            raise ConfigError("poll_interval must be positive")

    def resolved_api_key(self) -> str:
        return resolve_api_key(api_key=self.api_key, api_key_env=self.api_key_env, env_file=self.env_file)
