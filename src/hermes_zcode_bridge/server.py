from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .api import HermesAPIClient
from .config import BridgeConfig, ConfigError, DEFAULT_API_URL, DEFAULT_API_KEY_ENV, default_state_db
from .mcp_server import create_server
from .registry import StateRegistry
from .service import BridgeService


logger = logging.getLogger("hermes_zcode_bridge")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCP stdio bridge for Hermes Agent durable runs")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Hermes API Server base URL (non-secret)")
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV, help="Environment variable containing API key")
    parser.add_argument("--env-file", type=Path, help="Optional server-side dotenv file to read the API key from")
    parser.add_argument("--state-db", type=Path, default=default_state_db(), help="Safe local registry SQLite path")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP request timeout in seconds")
    parser.add_argument("--poll-interval", type=float, default=0.5, help="Default wait polling interval")
    parser.add_argument("--log-level", default="WARNING", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), stream=sys.stderr)
    try:
        config = BridgeConfig(
            api_url=args.api_url,
            state_db=args.state_db,
            request_timeout=args.timeout,
            poll_interval=args.poll_interval,
            api_key_env=args.api_key_env,
            env_file=args.env_file,
        )
        registry = StateRegistry(config.state_db)
        client = HermesAPIClient(config)
        service = BridgeService(client, registry)
        server = create_server(service)
    except (ConfigError, OSError, RuntimeError) as exc:
        logger.error("Bridge startup failed: %s", exc)
        return 2

    async def run() -> None:
        await server.run_stdio_async()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    finally:
        registry.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
