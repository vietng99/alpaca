#!/usr/bin/env bash
# Create dependencies only in this copy's .venv. No host tools or configuration are installed.
set -euo pipefail

ALPACA_BOOT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ALPACA_BOOT_PYTHON="${ALPACA_PYTHON:-python3}"
ALPACA_BOOT_CHECK=0
ALPACA_BOOT_OFFLINE=0
ALPACA_BOOT_WHEELS=""

usage() {
  cat <<'EOF'
Usage: bash setup/bootstrap.sh [--check] [--python PATH] [--offline] [--wheelhouse PATH]
Creates only the local .venv, installs requirements.txt, and checks Python dependencies.
--check checks the existing .venv without installing or writing anything.
--offline disables package-index access; provide cached wheels with --wheelhouse.
Python 3.10 or newer is required. Python 3.11+ also supports wiki TOML configuration.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --check) ALPACA_BOOT_CHECK=1; shift ;;
    --offline) ALPACA_BOOT_OFFLINE=1; shift ;;
    --python|--wheelhouse)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      if [ "$1" = --python ]; then ALPACA_BOOT_PYTHON="$2"; else ALPACA_BOOT_WHEELS="$2"; fi
      shift 2 ;;
    *) usage >&2; exit 2 ;;
  esac
done

unset PYTHONHOME PYTHONPATH PIP_TARGET PIP_PREFIX PIP_USER
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
export PIP_CONFIG_FILE=/dev/null
cd "$ALPACA_BOOT_ROOT"
[ -f ALPACA-MANIFEST ] || { printf '%s\n' 'Missing ALPACA-MANIFEST beside setup.' >&2; exit 1; }
[ -f requirements.txt ] || { printf '%s\n' 'Missing requirements.txt.' >&2; exit 1; }
[ ! -L .venv ] || { printf '%s\n' 'Refusing a symlinked .venv.' >&2; exit 1; }

if [ "$ALPACA_BOOT_CHECK" -eq 0 ]; then
  "$ALPACA_BOOT_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10+ required")'
  if [ ! -x .venv/bin/python ]; then
    "$ALPACA_BOOT_PYTHON" -m venv .venv
  fi
  .venv/bin/python -c 'from pathlib import Path; import sys; expected=Path(sys.argv[1]).resolve(); sys.exit(0 if sys.prefix != sys.base_prefix and Path(sys.prefix).resolve() == expected else "Refusing pip outside this copy local .venv")' "$ALPACA_BOOT_ROOT/.venv"
  ALPACA_BOOT_PIP_ARGS=(--disable-pip-version-check)
  if [ "$ALPACA_BOOT_OFFLINE" -eq 1 ]; then ALPACA_BOOT_PIP_ARGS+=(--no-index); fi
  if [ -n "$ALPACA_BOOT_WHEELS" ]; then ALPACA_BOOT_PIP_ARGS+=(--find-links "$ALPACA_BOOT_WHEELS"); fi
  .venv/bin/python -m pip install "${ALPACA_BOOT_PIP_ARGS[@]}" -r requirements.txt
fi

[ -x .venv/bin/python ] || { printf '%s\n' 'No local .venv. Run bootstrap without --check.' >&2; exit 1; }
.venv/bin/python -c 'from pathlib import Path; import sys; expected=Path(sys.argv[1]).resolve(); sys.exit(0 if sys.prefix != sys.base_prefix and Path(sys.prefix).resolve() == expected else "Expected this copy local .venv")' "$ALPACA_BOOT_ROOT/.venv"
.venv/bin/python - <<'PY'
from importlib import metadata
from pathlib import Path
import sys
from packaging.requirements import Requirement

if sys.version_info < (3, 10):
    raise SystemExit('Python 3.10+ required')
import yaml
import pytest
import html5lib

for line in Path('requirements.txt').read_text(encoding='utf-8').splitlines():
    line = line.split('#', 1)[0].strip()
    if not line:
        continue
    requirement = Requirement(line)
    if requirement.marker and not requirement.marker.evaluate():
        continue
    pins = list(requirement.specifier)
    if len(pins) != 1 or pins[0].operator != '==' or '*' in pins[0].version:
        raise SystemExit('Bootstrap requires exact dependency pins: ' + line)
    name, wanted = requirement.name, pins[0].version
    found = metadata.version(name)
    if found != wanted:
        raise SystemExit(name + ': expected ' + wanted + ', found ' + found)
print('Alpaca bootstrap: Python and pinned dependencies ready in .venv')
PY

# alpaca-root.pth (t-428): earlier bootstraps wrote the install root into the venv's site-packages,
# which put the whole root on the import path of every interpreter of the venv. bin/alpaca-python now
# starts the package by its location (python -I alpaca/boot.py), so no .pth is written, and a legacy
# one is removed; --check reports it and writes nothing.
ALPACA_BOOT_SITE="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
ALPACA_BOOT_PTH="$ALPACA_BOOT_SITE/alpaca-root.pth"
if [ -f "$ALPACA_BOOT_PTH" ]; then
  if [ "$ALPACA_BOOT_CHECK" -eq 0 ]; then
    rm -f -- "$ALPACA_BOOT_PTH"
  else
    printf '%s\n' 'a legacy alpaca-root.pth puts the install root on the import path: bash setup/bootstrap.sh removes it' >&2
  fi
fi

"$ALPACA_BOOT_ROOT/bin/alpaca-python" --isolated -m alpaca --help >/dev/null
printf '%s\n' 'Ready: bin/alpaca --help'
