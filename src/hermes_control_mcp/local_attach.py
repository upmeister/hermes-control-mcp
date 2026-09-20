from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlunsplit


OWNER_ADAPTER_ROUTE = "/api/owner/ws"
OWNER_ADAPTER_TRANSPORT = "websocket-unix"
OWNER_ADAPTER_VERSION = 1
_MAX_LEASE_BYTES = 64 * 1024


class OwnerAttachError(ValueError):
    """The owner lease cannot prove a safe local attach target."""


def process_start_marker(pid: int) -> str | None:
    """Return the Linux process-start marker used to fence PID reuse."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        _comm, fields = raw.rsplit(") ", 1)
        values = fields.split()
        return f"proc:{values[19]}"
    except (OSError, IndexError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class OwnerAttachTarget:
    """Non-secret connection target derived from one owner lease."""

    lease_path: Path
    socket_path: Path
    uri: str
    runtime_id: str
    pid: int
    process_start: str
    profile_home: Path


def _fail(message: str) -> OwnerAttachError:
    return OwnerAttachError(f"owner attach lease rejected: {message}")


def _private_owner(path: Path, *, label: str, kind: str) -> os.stat_result:
    _reject_symlink_components(path, label=label)
    try:
        info = path.lstat()
    except OSError as exc:
        raise _fail(f"{label} is unavailable") from exc
    if kind == "file" and not stat.S_ISREG(info.st_mode):
        raise _fail(f"{label} is not a regular file")
    if kind == "socket" and not stat.S_ISSOCK(info.st_mode):
        raise _fail(f"{label} is not a Unix socket")
    if info.st_uid != os.geteuid():
        raise _fail(f"{label} owner does not match the current uid")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise _fail(f"{label} is not private")
    return info


def _private_parent(path: Path, *, label: str) -> None:
    parent = path.parent
    _reject_symlink_components(parent, label=f"{label} parent")
    try:
        info = parent.lstat()
    except OSError as exc:
        raise _fail(f"{label} parent is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise _fail(f"{label} parent is not a directory")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise _fail(f"{label} parent is not private")


def _string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or any(ord(char) < 0x20 for char in value):
        raise _fail(f"{key} is invalid")
    return value


def _reject_symlink_components(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise _fail(f"{label} contains a symlink component")
        except FileNotFoundError:
            break
        except OSError as exc:
            raise _fail(f"{label} path identity is unavailable") from exc


def _absolute_path(value: str, *, key: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise _fail(f"{key} must be absolute")
    path = Path(os.path.abspath(str(path)))
    _reject_symlink_components(path, label=key)
    if path.is_symlink():
        raise _fail(f"{key} must not be a symlink")
    return path


def _identity_query(*, runtime_id: str, pid: int, process_start: str, profile_home: str) -> str:
    # Only owner identity is sent. In particular, never copy token/ticket/internal
    # credentials from a lease or from the dashboard configuration.
    return urlencode(
        {
            "runtime_id": runtime_id,
            "pid": str(pid),
            "process_start": process_start,
            "profile_home": profile_home,
        }
    )


def load_owner_attach_target(lease_path: str | Path) -> OwnerAttachTarget:
    """Read and validate an owner adapter lease without opening a network socket."""
    lease_input = Path(lease_path).expanduser()
    if not lease_input.is_absolute():
        raise _fail("lease path must be absolute")
    lease = _absolute_path(str(lease_input), key="lease path")
    _private_parent(lease, label="lease")
    info = _private_owner(lease, label="lease", kind="file")
    if info.st_size > _MAX_LEASE_BYTES:
        raise _fail("lease is too large")
    try:
        payload = json.loads(lease.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("lease JSON is unreadable") from exc
    if not isinstance(payload, dict):
        raise _fail("lease JSON is not an object")

    version = payload.get("version")
    if type(version) is not int or version != OWNER_ADAPTER_VERSION:
        raise _fail("unsupported lease version")
    if payload.get("route") != OWNER_ADAPTER_ROUTE:
        raise _fail("route is not the private owner route")
    if payload.get("transport") != OWNER_ADAPTER_TRANSPORT:
        raise _fail("transport is not Unix WebSocket")

    runtime_id = _string(payload, "runtime_id")
    process_start = _string(payload, "process_start")
    profile_text = _string(payload, "profile_home")
    socket_text = _string(payload, "socket_path")
    lease_text = _string(payload, "lease_path")

    pid = payload.get("pid")
    if type(pid) is not int or pid <= 0:
        raise _fail("pid is invalid")
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        raise _fail("owner process is not live") from exc
    current_process_start = process_start_marker(pid)
    if current_process_start is None or current_process_start != process_start:
        raise _fail("owner process start identity does not match")
    port = payload.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise _fail("endpoint port is invalid")
    host = _string(payload, "host")
    if any(char in host for char in "/?#@"):
        raise _fail("host contains URL delimiters")

    declared_lease = _absolute_path(lease_text, key="lease_path")
    if declared_lease != lease:
        raise _fail("lease path does not match the opened lease")
    socket_path = _absolute_path(socket_text, key="socket_path")
    if socket_path.parent != lease.parent:
        raise _fail("socket and lease are not in the same private runtime")
    _private_parent(socket_path, label="socket")
    _private_owner(socket_path, label="socket", kind="socket")

    profile_home = _absolute_path(profile_text, key="profile_home")
    netloc_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    uri = urlunsplit(
        (
            "ws",
            f"{netloc_host}:{port}",
            OWNER_ADAPTER_ROUTE,
            _identity_query(
                runtime_id=runtime_id,
                pid=pid,
                process_start=process_start,
                profile_home=str(profile_home),
            ),
            "",
        )
    )
    return OwnerAttachTarget(
        lease_path=lease,
        socket_path=socket_path,
        uri=uri,
        runtime_id=runtime_id,
        pid=pid,
        process_start=process_start,
        profile_home=profile_home,
    )
