#!/usr/bin/env bash
# Policy-bound cutover (M4.15). PROPOSED: dry-run is the default.
#
#   ./cutover.sh              # verify archive/live baseline and print the exact delta
#   ./cutover.sh --dry-run    # same, no live writes
#   ./cutover.sh --apply      # back up every overwrite, then install verified members
#   ./cutover.sh --rollback   # restore latest backup and remove paths introduced by it
#
# Test overrides are explicit environment variables. Production defaults are resolved from this
# script's own location (rename-safe: no folder-name literal, no absolute path), not the caller's
# current working directory.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "$HERE/.." && pwd -P)"
HELPER="$HERE/package_candidate.py"
ARCHIVE="${ALPACA_ARCHIVE:-$ROOT/dist/candidate.tar.gz}"
LIVE="${ALPACA_LIVE_ROOT:-$ROOT}"
BACKUPS="${ALPACA_BACKUP_ROOT:-$ROOT/dist/cutover-backup}"
MODE="${1:---dry-run}"

case "$MODE" in
  --dry-run)
    echo "CUTOVER DRY-RUN -- no files will be changed"
    python3 "$HELPER" verify-archive --archive "$ARCHIVE"
    python3 "$HELPER" changes --archive "$ARCHIVE" --live "$LIVE"
    echo "CUTOVER DRY-RUN COMPLETE -- no files changed"
    ;;
  --apply)
    # An ambient ALPACA_LIVE_ROOT redirects the one-way door away from this script's own default.
    # That must never be invisible: pass a durable redirect note so the helper stamps it into the
    # append-only ledger and the receipt, naming ALPACA_LIVE_ROOT and the root it pointed the door at.
    if [ -n "${ALPACA_LIVE_ROOT:-}" ]; then
      python3 "$HELPER" apply --archive "$ARCHIVE" --live "$LIVE" --backup-root "$BACKUPS" \
        --redirect-note "ALPACA_LIVE_ROOT ambient redirect: the one-way door's live root was overridden from the script default to $LIVE"
    else
      python3 "$HELPER" apply --archive "$ARCHIVE" --live "$LIVE" --backup-root "$BACKUPS"
    fi
    ;;
  --rollback)
    python3 "$HELPER" rollback --live "$LIVE" --backup-root "$BACKUPS"
    ;;
  *)
    echo "usage: $0 [--dry-run|--apply|--rollback]" >&2
    exit 2
    ;;
esac
