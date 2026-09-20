from __future__ import annotations

import argparse
import asyncio
import logging
import shlex
import subprocess
import sys
from pathlib import Path

from .api import HermesAPIClient
from . import client_config
from .client_config import (
    CLIENT_CONFIG_DESTINATIONS,
    DEFAULT_SERVER_NAME,
    SUPPORTED_CLIENTS,
)
from .config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_API_URL,
    DEFAULT_GATEWAY_ACCESS_TOKEN_ENV,
    DEFAULT_GATEWAY_REFRESH_TOKEN_ENV,
    DEFAULT_GATEWAY_TOKEN_ENV,
    BridgeConfig,
    ConfigError,
    default_state_db,
)
from .doctor import doctor_json, format_doctor_report, run_doctor
from .mcp_server import create_server
from .registry import StateRegistry
from .service import BridgeService


logger = logging.getLogger("hermes_control_mcp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCP control plane for Hermes Agent durable runs and live TUI")
    parser.add_argument(
        "command", nargs="?", default="serve", choices=("serve", "doctor", "client-config"),
        help=(
            "serve MCP over stdio (default), run non-consuming readiness checks (doctor), "
            "or print a non-mutating client MCP config snippet (client-config; same-host "
            "first, --ssh for remote Hermes hosts; no remote HTTP MCP)"
        ),
    )
    parser.add_argument(
        "client", nargs="?", default=None, choices=SUPPORTED_CLIENTS,
        help="client-config only: which MCP host to generate configuration for",
    )
    parser.add_argument(
        "--ssh", metavar="HOST", default=None,
        help=(
            "client-config only: launch the bridge on HOST over SSH so the bridge and "
            "Hermes secrets stay on the Hermes host; remote HTTP MCP is not implemented"
        ),
    )
    parser.add_argument(
        "--name", default=None,
        help=f"client-config only: MCP server name in the generated config (default: {DEFAULT_SERVER_NAME})",
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Hermes API Server base URL (non-secret)")
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV, help="Environment variable containing API key")
    parser.add_argument("--env-file", type=Path, help="Optional server-side dotenv file to read keys/tokens from")
    parser.add_argument("--state-db", type=Path, default=default_state_db(), help="Safe local registry SQLite path")
    parser.add_argument(
        "--profiles-root", type=Path, default=None,
        help="Named-profiles root for profile-scoped API keys (default: $HERMES_HOME/profiles)",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP request timeout in seconds")
    parser.add_argument("--poll-interval", type=float, default=0.5, help="Default wait polling interval")
    parser.add_argument("--gateway-url", help="Hermes TUI WebSocket URL, e.g. ws://127.0.0.1:9119/api/ws")
    parser.add_argument(
        "--gateway-owner-lease", type=Path,
        help="Private local owner-adapter lease JSON (uses same-user Unix WebSocket; no dashboard token)",
    )
    parser.add_argument("--gateway-http-url", help="Optional HTTP origin for /api/auth/ws-ticket")
    parser.add_argument(
        "--gateway-token-env", default=DEFAULT_GATEWAY_TOKEN_ENV,
        help="Env name for a reusable loopback dashboard token (no credential value in args)",
    )
    parser.add_argument(
        "--gateway-access-token-env", default=DEFAULT_GATEWAY_ACCESS_TOKEN_ENV,
        help="Env name for a dashboard access token used to mint fresh WS tickets",
    )
    parser.add_argument(
        "--gateway-refresh-token-env", default=DEFAULT_GATEWAY_REFRESH_TOKEN_ENV,
        help="Env name for an optional native refresh token (rotated only in process memory)",
    )
    parser.add_argument(
        "--gateway-auth-provider", default="",
        help="Optional dashboard auth provider name for native refresh",
    )
    parser.add_argument(
        "--gateway-ticket-env", default="",
        help="Optional env name for one pre-minted single-use WS ticket",
    )
    parser.add_argument("--gateway-connect-timeout", type=float, default=15.0)
    parser.add_argument("--gateway-request-timeout", type=float, default=120.0)
    parser.add_argument("--gateway-heartbeat-interval", type=float, default=15.0)
    parser.add_argument("--gateway-heartbeat-timeout", type=float, default=45.0)
    parser.add_argument("--gateway-event-buffer-max", type=int, default=512)
    parser.add_argument("--gateway-event-buffer-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--gateway-event-buffer-total-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument(
        "--profile", action="append", default=[],
        help="doctor only: named Hermes profile to probe; repeat for multiple profiles",
    )
    parser.add_argument(
        "--all-profiles", action="store_true",
        help="doctor only: probe every syntactically valid named profile directory",
    )
    parser.add_argument(
        "--require-live", action="store_true",
        help="doctor only: make unavailable live owner/native attach a hard failure",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="doctor only: emit one JSON readiness object instead of human-readable text",
    )
    parser.add_argument("--log-level", default="WARNING", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def _run_client_config(args: argparse.Namespace) -> int:
    """Generate one client config payload on stdout; guidance/warnings on stderr."""
    warnings: list[str] = []
    discovery = None
    try:
        name = client_config.validate_server_name(args.name or DEFAULT_SERVER_NAME)
        if args.ssh is not None:
            discovery = client_config.discover_ssh(args.ssh)
            plan = client_config.build_ssh_plan(args.client, name, discovery)
        else:
            plan = client_config.build_local_plan(args.client, name, warn=warnings.append)
        payload = client_config.render_plan(plan, args.client)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"client-config: {exc}", file=sys.stderr)
        return 2

    sys.stdout.write(payload)
    for message in warnings:
        print(f"warning: {message}", file=sys.stderr)
    print(
        f"Generated non-mutating {args.client} configuration for MCP server {name!r}; no files were written.",
        file=sys.stderr,
    )
    destination = CLIENT_CONFIG_DESTINATIONS.get(args.client)
    if destination:
        print(f"Usual destination: {destination}", file=sys.stderr)
    if discovery is not None:
        hint = f"ssh {shlex.quote(discovery.host)} {shlex.quote(discovery.remote_command + ' doctor')}"
        print(f"Recommended readiness check before connecting: {hint}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "client-config":
        if args.client is None:
            parser.error(
                "client-config requires a target client: one of " + ", ".join(SUPPORTED_CLIENTS)
            )
        return _run_client_config(args)
    if args.client is not None:
        parser.error(f"unrecognized arguments: {args.client}")
    if args.ssh is not None or args.name is not None:
        parser.error("--ssh/--name are only valid with the client-config command")
    logging.basicConfig(level=getattr(logging, args.log_level), stream=sys.stderr)
    registry: StateRegistry | None = None
    service: BridgeService | None = None
    try:
        config = BridgeConfig(
            api_url=args.api_url,
            state_db=args.state_db,
            profiles_root=args.profiles_root,
            request_timeout=args.timeout,
            poll_interval=args.poll_interval,
            api_key_env=args.api_key_env,
            env_file=args.env_file,
            gateway_url=args.gateway_url,
            gateway_owner_lease_path=args.gateway_owner_lease,
            gateway_http_url=args.gateway_http_url,
            gateway_token_env=args.gateway_token_env,
            gateway_access_token_env=args.gateway_access_token_env,
            gateway_refresh_token_env=args.gateway_refresh_token_env,
            gateway_auth_provider=args.gateway_auth_provider,
            gateway_ticket_env=args.gateway_ticket_env,
            gateway_connect_timeout=args.gateway_connect_timeout,
            gateway_request_timeout=args.gateway_request_timeout,
            gateway_heartbeat_interval=args.gateway_heartbeat_interval,
            gateway_heartbeat_timeout=args.gateway_heartbeat_timeout,
            gateway_event_buffer_max=args.gateway_event_buffer_max,
            gateway_event_buffer_bytes=args.gateway_event_buffer_bytes,
            gateway_event_buffer_total_bytes=args.gateway_event_buffer_total_bytes,
        )
        if args.command == "doctor":
            report = run_doctor(
                config,
                profiles=args.profile,
                all_profiles=bool(args.all_profiles),
                require_live=bool(args.require_live),
            )
            print(doctor_json(report) if args.json else format_doctor_report(report))
            return 0 if report.get("ok") else 2

        registry = StateRegistry(config.state_db)
        client = HermesAPIClient(config)
        service = BridgeService(client, registry)
        server = create_server(service)
    except (ConfigError, OSError, RuntimeError) as exc:
        logger.error("Bridge startup failed: %s", exc)
        if registry is not None:
            registry.close()
        return 2

    async def run() -> None:
        await server.run_stdio_async()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    finally:
        if service is not None:
            service.close()
        if registry is not None:
            registry.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
