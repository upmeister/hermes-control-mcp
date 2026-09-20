from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .api import HermesAPIClient
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
        "command", nargs="?", default="serve", choices=("serve", "doctor"),
        help="serve MCP over stdio (default) or run non-consuming readiness checks",
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
