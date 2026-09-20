"""Client-config onboarding: one typed stdio plan rendered per MCP host.

The generator is deliberately non-mutating and secret-free: it never writes
client, bridge, or Hermes configuration files, never resolves credential
values, and renders one deterministic config payload. stdout carries only the
payload; human guidance and warnings belong on stderr.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import state_home


BRIDGE_COMMAND = "hermes-control-mcp"
DEFAULT_SERVER_NAME = "hermes"
SUPPORTED_CLIENTS: tuple[str, ...] = ("zcode", "claude-code", "cursor", "codex", "vscode")
SSH_DISCOVERY_TIMEOUT_SECONDS = 20.0

# MCP server-name identity charset: safe as a JSON key, a TOML bare table key,
# and a per-client state DB filename component. Anything else is rejected
# instead of being silently rewritten into an ambiguous identity.
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Fixed remote probe, executed by the remote login shell as one ssh argv
# element. It contains no user-controlled fragments and prints two lines: the
# discovered bridge executable (possibly empty when absent), then the remote
# home directory. stderr is not captured so ordinary ssh host-key/password
# prompts reach the user's terminal unchanged.
SSH_PROBE_COMMAND = (
    'p="$(command -v hermes-control-mcp)" || p=""; '
    "printf '%s\\n' \"$p\" \"$HOME\""
)


class ClientConfigError(ValueError):
    """Invalid client-config input or failed discovery; messages are stderr-safe."""


@dataclass(frozen=True, slots=True)
class StdioServerPlan:
    """One normalized stdio MCP server deployment; renderers only translate this."""

    name: str
    command: str
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SSHDiscovery:
    """Remote execution facts found by one bounded read-only ssh preflight."""

    host: str
    remote_command: str
    remote_home: str


SSHRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]
WarnFn = Callable[[str], None]


def validate_client(client: str) -> str:
    if client not in SUPPORTED_CLIENTS:
        raise ClientConfigError(
            f"Unsupported client {client!r}; expected one of: {', '.join(SUPPORTED_CLIENTS)}"
        )
    return client


def validate_server_name(name: str) -> str:
    if not SERVER_NAME_RE.fullmatch(name):
        raise ClientConfigError(
            f"Invalid MCP server name {name!r}: use 1-64 characters from letters, "
            "digits, '_' or '-', starting with a letter or digit."
        )
    return name


def validate_ssh_host(host: str) -> str:
    # The host is one local ssh argv element. Reject option- and shell-looking
    # destinations instead of interpreting them; OpenSSH resolves aliases,
    # ProxyJump, ports and identity files from its own configuration.
    if (
        not host
        or host.startswith("-")
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in host)
    ):
        raise ClientConfigError(
            f"Invalid SSH host {host!r}: pass a plain OpenSSH destination "
            "(an alias from ~/.ssh/config or user@host) without options or whitespace."
        )
    return host


def client_state_db(client: str, server_name: str, *, remote_home: str | None = None) -> Path:
    """Absolute per-client bridge registry path; the file is never created here.

    One MCP host gets its own state DB so concurrently running bridge processes
    never share the public-beta one-process-per-DB resource.
    """
    if remote_home is not None:
        base = Path(remote_home) / ".local" / "state" / "hermes-control-mcp"
    else:
        base = state_home() / "hermes-control-mcp"
    return base / "clients" / f"{client}-{server_name}.db"


def discover_local_bridge_command(
    *,
    executable_override: str | None = None,
    which: Callable[[str], str | None] | None = None,
    warn: WarnFn | None = None,
) -> str:
    """Prefer the actually installed bridge executable over a bare command name.

    GUI-launched MCP hosts do not necessarily inherit the interactive shell
    PATH, so an absolute discovered path is preferred; the bare fallback keeps
    generating but warns.
    """
    if executable_override:
        return executable_override
    lookup = which if which is not None else shutil.which
    found = lookup(BRIDGE_COMMAND)
    if found:
        # which() can return a cwd-relative path (for example PATH="." ->
        # "./hermes-control-mcp"). Generated configs must not depend on a GUI
        # host's working directory, so normalize to absolute without chasing
        # symlinks (resolving them could embed venv internals).
        return os.path.abspath(found)
    if warn is not None:
        warn(
            f"{BRIDGE_COMMAND} was not found on PATH; emitting the bare command name. "
            "GUI-launched MCP hosts may not inherit your shell PATH, so prefer "
            "'uv tool install hermes-control-mcp' or an absolute executable path."
        )
    return BRIDGE_COMMAND


def _default_ssh_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, stdout=subprocess.PIPE, text=True, timeout=timeout, check=False)


def discover_ssh(
    host: str,
    *,
    timeout: float = SSH_DISCOVERY_TIMEOUT_SECONDS,
    runner: SSHRunner | None = None,
) -> SSHDiscovery:
    """One bounded, read-only ssh preflight: remote bridge executable and home.

    No remote writes, no ~/.ssh/config parsing (OpenSSH resolves the host
    itself), no credential values returned or logged.
    """
    validate_ssh_host(host)
    argv = ["ssh", "-T", host, SSH_PROBE_COMMAND]
    run = runner if runner is not None else _default_ssh_runner
    try:
        completed = run(argv, timeout)
    except subprocess.TimeoutExpired as exc:
        raise ClientConfigError(
            f"SSH discovery on {host!r} timed out after {timeout:g}s. Verify the host "
            "is reachable and ssh is not waiting for an interactive answer."
        ) from exc
    except OSError as exc:
        raise ClientConfigError(f"Could not run the local ssh client for {host!r}: {exc}") from exc
    if completed.returncode != 0:
        raise ClientConfigError(
            f"SSH discovery on {host!r} failed with exit code {completed.returncode}. "
            "Verify the host alias, authentication, host-key acceptance, and that the "
            "remote account can run commands."
        )
    remote_command, remote_home = _parse_ssh_probe_output(host, completed.stdout)
    return SSHDiscovery(host=host, remote_command=remote_command, remote_home=remote_home)


def _parse_ssh_probe_output(host: str, stdout: str) -> tuple[str, str]:
    lines = stdout.splitlines()
    executable = lines[0].strip() if len(lines) >= 1 else ""
    home = lines[1].strip() if len(lines) >= 2 else ""
    if not executable:
        raise ClientConfigError(
            f"hermes-control-mcp was not found in the remote PATH on {host!r}. Install it "
            "on the Hermes host (for example 'uv tool install hermes-control-mcp') and retry."
        )
    _validate_discovered_value(host, "remote executable", executable)
    _validate_discovered_value(host, "remote home directory", home)
    return executable, home


def _validate_discovered_value(host: str, label: str, value: str) -> None:
    if not value.startswith("/") or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ClientConfigError(
            f"SSH discovery on {host!r} returned a malformed {label} {value!r}; "
            "refusing to generate an ambiguous config."
        )


def build_local_plan(
    client: str,
    server_name: str,
    *,
    executable_override: str | None = None,
    which: Callable[[str], str | None] | None = None,
    warn: WarnFn | None = None,
) -> StdioServerPlan:
    validate_client(client)
    validate_server_name(server_name)
    command = discover_local_bridge_command(
        executable_override=executable_override, which=which, warn=warn
    )
    db = client_state_db(client, server_name)
    return StdioServerPlan(name=server_name, command=command, args=("--state-db", str(db)))


def build_ssh_plan(client: str, server_name: str, discovery: SSHDiscovery) -> StdioServerPlan:
    validate_client(client)
    validate_server_name(server_name)
    db = client_state_db(client, server_name, remote_home=discovery.remote_home)
    # The remote command string is one local argv element; shlex.join quotes
    # every word so discovered paths cannot change the remote shell grammar.
    remote_argv = shlex.join([discovery.remote_command, "--state-db", str(db)])
    return StdioServerPlan(name=server_name, command="ssh", args=("-T", discovery.host, remote_argv))


def _stdio_entry(plan: StdioServerPlan, *, with_type: bool) -> dict[str, object]:
    entry: dict[str, object] = {"command": plan.command, "args": list(plan.args)}
    if with_type:
        entry = {"type": "stdio", **entry}
    return entry


def render_plan(plan: StdioServerPlan, client: str) -> str:
    """Render one plan for one supported client; deterministic and payload-only."""
    validate_client(client)
    if client == "zcode":
        payload: dict[str, object] = {"mcp": {"servers": {plan.name: _stdio_entry(plan, with_type=False)}}}
    elif client == "claude-code":
        payload = {"mcpServers": {plan.name: _stdio_entry(plan, with_type=False)}}
    elif client == "cursor":
        payload = {"mcpServers": {plan.name: _stdio_entry(plan, with_type=True)}}
    elif client == "vscode":
        payload = {"servers": {plan.name: _stdio_entry(plan, with_type=True)}}
    else:  # codex
        return _render_codex_toml(plan)
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


_TOML_BASIC_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def toml_basic_string(value: str) -> str:
    """Escape one TOML basic string without adding a TOML-writing dependency."""
    out: list[str] = ['"']
    for ch in value:
        escape = _TOML_BASIC_ESCAPES.get(ch)
        if escape is not None:
            out.append(escape)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _render_codex_toml(plan: StdioServerPlan) -> str:
    # server_name is validated against SERVER_NAME_RE, so it is a safe TOML
    # bare key and needs no quoting in the table header.
    lines = [
        f"[mcp_servers.{plan.name}]",
        f"command = {toml_basic_string(plan.command)}",
        f"args = [{', '.join(toml_basic_string(arg) for arg in plan.args)}]",
    ]
    return "\n".join(lines) + "\n"


CLIENT_CONFIG_DESTINATIONS: dict[str, str] = {
    "zcode": "ZCode native user config: ~/.zcode/cli/config.json (or Settings -> MCP Servers)",
    "claude-code": "Claude Code project config: .mcp.json (or 'claude mcp add')",
    "cursor": "Cursor config: ~/.cursor/mcp.json (global) or .cursor/mcp.json (project)",
    "codex": "Codex user config: ~/.codex/config.toml",
    "vscode": "VS Code MCP config: .vscode/mcp.json or the user MCP configuration",
}
