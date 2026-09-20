"""Hermes profile identity helpers.

Canonical profile IDs mirror the deployed/upstream Hermes contract
(``hermes_cli.profiles._PROFILE_ID_RE``): lowercase ASCII, matched by
``^[a-z0-9][a-z0-9_-]{0,63}$`` with ``default`` as the special alias for the
default Hermes home. Hermes remains the authority for profile existence and
reserved names; the bridge only enforces the syntax so a profile string can
never become filesystem traversal or unescaped URL path input.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import read_env_value

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_PROFILE = "default"
NAMED_PROFILE_KEY_ENV = "API_SERVER_KEY"


def canonical_profile(value: object) -> str:
    """Canonicalize and validate one profile id (never returns an empty string)."""
    if not isinstance(value, str):
        raise ValueError("profile must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError("profile must not be empty")
    if stripped.casefold() == DEFAULT_PROFILE:
        return DEFAULT_PROFILE
    lowered = stripped.lower()
    if not PROFILE_ID_RE.match(lowered):
        raise ValueError(
            f"invalid profile id {value!r}: must match ^[a-z0-9][a-z0-9_-]{{0,63}}$ (lowercase)"
        )
    return lowered


def named_profile_env_path(profile: str, profiles_root: Path) -> Path:
    """``<profiles_root>/<profile>/.env``; refuses any non-canonical profile id."""
    if not PROFILE_ID_RE.match(profile):
        raise ValueError(f"invalid profile id {profile!r}")
    return Path(profiles_root) / profile / ".env"


def named_profile_api_key(profile: str, profiles_root: Path) -> str | None:
    """Resolve a named profile's own ``API_SERVER_KEY`` from its profile .env.

    Fail-closed by contract: a missing or empty key returns None and the caller
    must never fall back to the default profile's key. The value is never
    logged, persisted, or echoed into errors.
    """
    if not PROFILE_ID_RE.match(profile):
        return None
    value = read_env_value(named_profile_env_path(profile, profiles_root), NAMED_PROFILE_KEY_ENV)
    return value or None
