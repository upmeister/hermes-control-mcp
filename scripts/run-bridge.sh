#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HERMES_ROOT="${HERMES_HOME:-$HOME/.hermes}"

# The Hermes-installed venv carries the MCP SDK used by the current server.
# Keep an explicit override for other installs; never pass credentials in args.
if [[ "$PYTHON_BIN" == "python3" && -x "$HERMES_ROOT/hermes-agent/venv/bin/python3" ]]; then
  PYTHON_BIN="$HERMES_ROOT/hermes-agent/venv/bin/python3"
fi

export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" -m hermes_zcode_bridge.server "$@"
