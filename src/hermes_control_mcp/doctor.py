from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from .api import APIError, HermesAPIClient
from .config import BridgeConfig, ConfigError
from .live_client import LiveError, LiveGatewayClient
from .profiles import DEFAULT_PROFILE, PROFILE_ID_RE, canonical_profile, named_profile_api_key


def discover_named_profiles(profiles_root: Path) -> list[str]:
    """Return canonical profile-directory names without creating or mutating anything."""
    try:
        entries = list(Path(profiles_root).iterdir())
    except OSError:
        return []
    names: list[str] = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        name = entry.name
        if PROFILE_ID_RE.fullmatch(name):
            names.append(name)
    return sorted(set(names))


def _redact_config_secrets(config: BridgeConfig, text: str) -> str:
    redacted = str(text)
    resolvers = (
        config.resolved_api_key,
        config.resolved_gateway_token,
        config.resolved_gateway_access_token,
        config.resolved_gateway_refresh_token,
        config.resolved_gateway_ticket,
    )
    for resolve in resolvers:
        try:
            value = resolve()
        except (ConfigError, OSError):
            # Redaction must not turn an already-safe configuration error into
            # a second exception (for example when the API key is missing).
            continue
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _safe_error(exc: Exception, *, config: BridgeConfig | None = None) -> tuple[str, str]:
    code = str(getattr(exc, "code", "") or "doctor_check_failed")
    message = str(exc)
    if config is not None:
        message = _redact_config_secrets(config, message)
    return code, message


def _api_probe(client: HermesAPIClient, profile: str) -> dict[str, Any]:
    try:
        health = client.health(profile=profile)
        capabilities = client.capabilities(profile=profile)
        models = client.models(profile=profile)
    except (APIError, ConfigError, ValueError) as exc:
        code, message = _safe_error(exc, config=client.config)
        return {
            "ok": False,
            "status": "failed",
            "error_code": code,
            "error": message,
        }

    data = models.get("data") if isinstance(models, dict) else None
    model_count = len(data) if isinstance(data, list) else None
    version = health.get("version") if isinstance(health, dict) else None
    return {
        "ok": True,
        "status": "ready",
        "version": str(version) if version is not None else None,
        "model_count": model_count,
        "capabilities_reachable": isinstance(capabilities, dict),
    }


def _live_probe(
    config: BridgeConfig,
    *,
    client_factory: Callable[[BridgeConfig], LiveGatewayClient],
) -> dict[str, Any]:
    configured = bool(config.gateway_owner_lease_path is not None or config.gateway_url)
    if not configured:
        return {
            "ok": False,
            "required": False,
            "tier": "experimental",
            "status": "unconfigured",
            "error_code": "live_not_configured",
            "error": (
                "Live shared-session attach is optional for the public beta. "
                "Configure a compatible owner/native attach seam to enable it."
            ),
        }

    client = client_factory(config)
    try:
        client.connect()
        health = client.health()
        return {
            "ok": True,
            "required": False,
            "tier": "experimental",
            "status": "ready",
            "auth_mode": health.get("auth_mode"),
            "connection_state": health.get("connection_state"),
            "replay_epoch": health.get("replay_epoch"),
        }
    except LiveError as exc:
        code, message = _safe_error(exc, config=config)
        return {
            "ok": False,
            "required": False,
            "tier": "experimental",
            "status": "unavailable",
            "error_code": code,
            "error": message,
        }
    finally:
        try:
            client.shutdown()
        except Exception:
            pass


def run_doctor(
    config: BridgeConfig,
    *,
    profiles: Iterable[str] | None = None,
    all_profiles: bool = False,
    require_live: bool = False,
    api_client_factory: Callable[[BridgeConfig], HermesAPIClient] = HermesAPIClient,
    live_client_factory: Callable[[BridgeConfig], LiveGatewayClient] = LiveGatewayClient,
) -> dict[str, Any]:
    """Run non-consuming readiness checks for the durable core and optional live tier.

    The doctor never submits a Hermes run, prompt, steer, interrupt or config
    mutation. Protected API probes prove routing/auth; the live probe performs
    only the existing gateway handshake when live attach is configured.
    """
    requested: list[str] = []
    for raw in profiles or ():
        requested.append(canonical_profile(raw))
    if all_profiles:
        requested.extend(discover_named_profiles(config.resolved_profiles_root()))
    requested = sorted(set(profile for profile in requested if profile != DEFAULT_PROFILE))

    profile_reports: dict[str, Any] = {}
    api_client: HermesAPIClient | None = None
    try:
        api_client = api_client_factory(config)
        core = _api_probe(api_client, DEFAULT_PROFILE)
    except (ConfigError, APIError, ValueError) as exc:
        code, message = _safe_error(exc, config=config)
        core = {
            "ok": False,
            "status": "failed",
            "error_code": code,
            "error": message,
        }

    if api_client is not None:
        for profile in requested:
            key_present = bool(named_profile_api_key(profile, config.resolved_profiles_root()))
            if not key_present:
                profile_reports[profile] = {
                    "ok": False,
                    "status": "failed",
                    "error_code": "profile_key_unavailable",
                    "error": (
                        f"Named profile {profile!r} has no API_SERVER_KEY in its profile .env; "
                        "the default profile key is never borrowed."
                    ),
                    "api_key_present": False,
                }
                continue
            report = _api_probe(api_client, profile)
            report["api_key_present"] = True
            profile_reports[profile] = report
    else:
        for profile in requested:
            profile_reports[profile] = {
                "ok": False,
                "status": "not_checked",
                "error_code": "default_api_unavailable",
                "error": "Default API client is unavailable; named profile was not probed.",
                "api_key_present": bool(
                    named_profile_api_key(profile, config.resolved_profiles_root())
                ),
            }

    live = _live_probe(config, client_factory=live_client_factory)
    live["required"] = bool(require_live)

    requested_profiles_ok = all(report.get("ok") for report in profile_reports.values())
    core_ok = bool(core.get("ok"))
    live_ok = bool(live.get("ok")) or not require_live
    overall_ok = core_ok and requested_profiles_ok and live_ok

    warnings: list[str] = []
    if not live.get("ok") and not require_live:
        warnings.append(
            "Live shared-session attach is optional/experimental in the public beta; "
            "durable API readiness is evaluated independently."
        )
    if all_profiles and not requested:
        warnings.append("No named profile directories were discovered.")

    return {
        "ok": overall_ok,
        "status": "ready" if overall_ok else "not_ready",
        "capability_tiers": {
            "durable": "stable" if core_ok else "unavailable",
            "live": "experimental_ready" if live.get("ok") else "experimental_unavailable",
        },
        "core": core,
        "profiles": profile_reports,
        "live": live,
        "warnings": warnings,
    }


def format_doctor_report(report: dict[str, Any]) -> str:
    """Human-readable report that never includes credential values."""
    lines = [
        "Hermes MCP bridge doctor",
        f"overall: {'READY' if report.get('ok') else 'NOT READY'}",
        "",
        "Durable core",
    ]
    core = report.get("core") or {}
    lines.append(
        f"  default API ........ {'PASS' if core.get('ok') else 'FAIL'}"
        + (f" ({core.get('error_code')})" if core.get("error_code") else "")
    )
    if core.get("version"):
        lines.append(f"  Hermes version ..... {core.get('version')}")
    if core.get("model_count") is not None:
        lines.append(f"  models endpoint .... PASS ({core.get('model_count')} listed)")

    profiles = report.get("profiles") or {}
    if profiles:
        lines.extend(["", "Named profiles"])
        for name in sorted(profiles):
            item = profiles[name]
            state = "PASS" if item.get("ok") else "FAIL"
            suffix = f" ({item.get('error_code')})" if item.get("error_code") else ""
            lines.append(f"  {name:<20} {state}{suffix}")

    live = report.get("live") or {}
    lines.extend(["", "Live shared-session tier"])
    live_state = "PASS" if live.get("ok") else "OPTIONAL/UNAVAILABLE"
    if live.get("required") and not live.get("ok"):
        live_state = "FAIL (required)"
    suffix = f" ({live.get('error_code')})" if live.get("error_code") else ""
    lines.append(f"  owner/native attach  {live_state}{suffix}")

    warnings = report.get("warnings") or []
    if warnings:
        lines.extend(["", "Notes"])
        lines.extend(f"  - {warning}" for warning in warnings)
    return "\n".join(lines)


def doctor_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True)
