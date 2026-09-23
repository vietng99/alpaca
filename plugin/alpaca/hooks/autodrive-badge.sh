#!/usr/bin/env bash
# Resolve the opted-in Alpaca project, including a plugin copied into an operator cache.
set -euo pipefail
ALPACA_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ALPACA_HOOK_ROOT="${ALPACA_ROOT:-${CLAUDE_PROJECT_DIR:-}}"
if [ ! -f "$ALPACA_HOOK_ROOT/ALPACA-MANIFEST" ]; then
  ALPACA_HOOK_ROOT="$(cd "$ALPACA_HOOK_DIR/../../.." && pwd -P)"
fi
[ -f "$ALPACA_HOOK_ROOT/ALPACA-MANIFEST" ] || exit 0
export ALPACA_ROOT="$ALPACA_HOOK_ROOT"
exec "$ALPACA_HOOK_ROOT/bin/alpaca-python" "$ALPACA_HOOK_DIR/session_state.py" badge "$@"
