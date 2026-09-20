#!/usr/bin/env bash
set -euo pipefail

# Historical private-deployment wrapper. New integrations should use
# run-control-mcp.sh or the installed hermes-control-mcp console script.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run-control-mcp.sh" "$@"
