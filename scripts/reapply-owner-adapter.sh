#!/usr/bin/env bash
# Re-apply the out-of-tree Hermes owner-adapter seam after a Hermes update.
#
# Why this exists
# ---------------
# The live attach tier needs a private owner boundary inside Hermes (a
# same-user Unix-domain WebSocket plus a process-bound lease). That seam is
# an out-of-tree patch, so `hermes update` removes it. The symptom is a lease
# file that still exists but points at a dead PID: the bridge then reports
# "owner process is not live" and refuses to attach.
#
# This script re-applies the patch and reports what to restart. It does NOT
# start Hermes, restart services, or touch live state - those are deliberate
# operator decisions.
#
# Usage:
#   scripts/reapply-owner-adapter.sh [--check]
#
#   --check   Report whether the seam is present and the lease is live,
#             without modifying anything. Exit 0 = ready, 1 = not ready,
#             2 = usage error.
set -euo pipefail

HERMES_ROOT="${HERMES_HOME:-$HOME/.hermes}"
AGENT_DIR="${HERMES_AGENT_DIR:-$HERMES_ROOT/hermes-agent}"
PATCH_REF="${OWNER_ADAPTER_PATCH_REF:-90f1126ba39bf0d4ebbc9bffe71bf5d4f6d84508}"
LEASE_PATH="${OWNER_ADAPTER_LEASE:-$HERMES_ROOT/runtime/owner_adapter/owner_adapter.json}"
OWNER_MODULE="hermes_cli/web_server_owner.py"
OWNER_ROUTER="hermes_cli/web_routers/local_owner.py"
CONFIG_GATE="owner_adapter:"

CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

[[ -d "$AGENT_DIR/.git" ]] || die "not a Hermes checkout: $AGENT_DIR"
cd "$AGENT_DIR"

seam_present() {
  [[ -f "$OWNER_MODULE" && -f "$OWNER_ROUTER" ]]
}

lease_state() {
  if [[ ! -f "$LEASE_PATH" ]]; then
    echo "absent"
    return
  fi
  local pid
  pid="$($AGENT_DIR/venv/bin/python3 -c "
import json,sys
try:
    print(json.load(open('$LEASE_PATH'))['pid'])
except Exception:
    print('')
" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    echo "unreadable"
  elif [[ -d "/proc/$pid" ]]; then
    echo "live (pid $pid)"
  else
    echo "stale (pid $pid is not running)"
  fi
}

if [[ "$CHECK_ONLY" == "1" ]]; then
  if seam_present; then
    echo "owner-adapter seam: PRESENT"
  else
    echo "owner-adapter seam: ABSENT (run without --check to re-apply)"
  fi
  echo "owner lease:        $(lease_state)"
  if seam_present && [[ "$(lease_state)" == live* ]]; then
    exit 0
  fi
  exit 1
fi

if seam_present; then
  echo "owner-adapter seam already present - nothing to apply."
  echo "If the lease is stale, restart the Hermes dashboard/DESKTOP owner"
  echo "process; it rewrites the lease on start."
  exit 0
fi

git cat-file -e "$PATCH_REF^{commit}" 2>/dev/null \
  || die "patch commit $PATCH_REF not found in $AGENT_DIR (fetch it or set OWNER_ADAPTER_PATCH_REF)"

PATCH_FILE="$(mktemp -t owner-adapter-XXXXXX.patch)"
trap 'rm -f "$PATCH_FILE"' EXIT
git show "$PATCH_REF" >"$PATCH_FILE"

# -3 (3-way) matters: the seam is carried across upstream releases, so context
# lines shift. A plain `git apply` fails on any of the three Python files
# after a few releases; 3-way reconciles them against the current tree.
if ! git apply -3 "$PATCH_FILE"; then
  git apply -R --stat "$PATCH_FILE" >/dev/null 2>&1 || true
  die "patch did not apply cleanly - resolve it manually:
  cd $AGENT_DIR && git show $PATCH_REF | git apply -3
Resolve the reported conflicts, then restart the Hermes owner process."
fi

git diff --check || die "patch applied but 'git diff --check' reported whitespace errors"

if ! grep -q "$CONFIG_GATE" "$HERMES_ROOT/config.yaml" 2>/dev/null; then
  echo "note: '$CONFIG_GATE' is not enabled in $HERMES_ROOT/config.yaml." >&2
  echo "      add 'dashboard: { owner_adapter: { enabled: true } }' to use the live tier." >&2
fi

echo "owner-adapter seam re-applied."
echo "Next: restart the Hermes owner process (dashboard / Desktop) so it"
echo "rewrites the lease, then verify with: $0 --check"
