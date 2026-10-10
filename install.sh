#!/usr/bin/env bash
# Install one verified Alpaca release and one `alpaca` launcher. Writes only two places:
#   <prefix>/releases/<id>/ and <prefix>/current   (default prefix: ~/.local/share/alpaca)
#   <bin-dir>/alpaca                               (default bin-dir: ~/.local/bin)
# Nothing else on the host changes: no shell profile, no global agent configuration.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install.sh [source] [--prefix DIR] [--bin-dir DIR] [--python PY] [--no-bootstrap] [--force]

Source (one of; default: --repo https://github.com/vietng99/alpaca.git --ref main):
  --repo URL [--ref REF]      shallow git clone of a release repository
  --archive FILE --sha256 HEX a release archive (.tar.gz) and the sha256 it must have
  --from DIR                  an unpacked release tree already on this machine

Every source is checked with the tree's own setup/gen_manifest.py --verify (each file against the
sha256 in MANIFEST.json) before anything is installed; an archive is also checked against --sha256.
A release that fails a check installs nothing.

  --prefix DIR     where releases are kept (default: ${XDG_DATA_HOME:-~/.local/share}/alpaca)
  --bin-dir DIR    where the launcher goes (default: ~/.local/bin)
  --python PY      interpreter for the release's .venv (default: python3)
  --no-bootstrap   do not create the release's .venv (alpaca new then needs PyYAML on PY)
  --force          replace a <bin-dir>/alpaca that this installer did not write
EOF
}

REPO="https://github.com/vietng99/alpaca.git"
REF="main"
ARCHIVE="" SHA="" FROM="" MODE="repo"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/alpaca"
BIN_DIR="$HOME/.local/bin"
PY="${ALPACA_PYTHON:-python3}"
BOOTSTRAP=1 FORCE=0
MARK="# alpaca launcher, written by Alpaca install.sh"

need() { [ "$#" -ge 2 ] || { usage >&2; exit 64; }; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --repo) need "$@"; REPO="$2"; MODE=repo; shift 2 ;;
    --ref) need "$@"; REF="$2"; shift 2 ;;
    --archive) need "$@"; ARCHIVE="$2"; MODE=archive; shift 2 ;;
    --sha256) need "$@"; SHA="$2"; shift 2 ;;
    --from) need "$@"; FROM="$2"; MODE=from; shift 2 ;;
    --prefix) need "$@"; PREFIX="$2"; shift 2 ;;
    --bin-dir) need "$@"; BIN_DIR="$2"; shift 2 ;;
    --python) need "$@"; PY="$2"; shift 2 ;;
    --no-bootstrap) BOOTSTRAP=0; shift ;;
    --force) FORCE=1; shift ;;
    *) usage >&2; exit 64 ;;
  esac
done

say() { printf 'alpaca install: %s\n' "$*"; }
die() { printf 'alpaca install: FAIL (%s)\n' "$*" >&2; exit 1; }

command -v "$PY" >/dev/null 2>&1 || die "no $PY; install Python 3.10 or newer, or pass --python"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "$PY is older than Python 3.10"

LAUNCHER="$BIN_DIR/alpaca"
if [ -e "$LAUNCHER" ] && ! grep -qF "$MARK" "$LAUNCHER" 2>/dev/null && [ "$FORCE" -ne 1 ]; then
  die "$LAUNCHER exists and was not written by this installer; pass --force to replace it"
fi

mkdir -p "$PREFIX/releases"
PREFIX="$(cd "$PREFIX" && pwd -P)"
STAGE="$(mktemp -d "$PREFIX/.stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
SRC="$STAGE/tree"

case "$MODE" in
  repo)
    command -v git >/dev/null 2>&1 || die "git is needed to fetch $REPO"
    say "fetching $REPO ($REF)"
    git clone -q --depth 1 --branch "$REF" "$REPO" "$SRC" || die "git clone of $REPO at $REF failed"
    rm -rf "$SRC/.git" ;;
  archive)
    [ -f "$ARCHIVE" ] || die "no archive at $ARCHIVE"
    [[ "$SHA" =~ ^[0-9a-f]{64}$ ]] || die "--archive needs --sha256 with 64 lowercase hex digits"
    if command -v sha256sum >/dev/null 2>&1; then actual="$(sha256sum -- "$ARCHIVE" | cut -d' ' -f1)"
    else actual="$(shasum -a 256 -- "$ARCHIVE" | cut -d' ' -f1)"; fi
    [ "$actual" = "$SHA" ] || die "archive sha256 is $actual, expected $SHA"
    mkdir -p "$STAGE/unpack"
    tar -xzf "$ARCHIVE" -C "$STAGE/unpack" || die "archive could not be unpacked"
    if [ -f "$STAGE/unpack/ALPACA-MANIFEST" ]; then
      mv "$STAGE/unpack" "$SRC"
    else
      top=("$STAGE"/unpack/*)
      [ "${#top[@]}" -eq 1 ] && [ -f "${top[0]}/ALPACA-MANIFEST" ] || die "archive holds no Alpaca tree"
      mv "${top[0]}" "$SRC"
    fi ;;
  from)
    [ -f "$FROM/ALPACA-MANIFEST" ] || die "$FROM is not an Alpaca tree (no ALPACA-MANIFEST)"
    grep -Eq '^template:[[:space:]]*true' "$FROM/project.yaml" 2>/dev/null \
      || die "$FROM is an onboarded project, not a release; install from a release tree"
    mkdir -p "$SRC"
    tar -C "$FROM" --exclude=./.git --exclude=./.venv --exclude=./.alpaca -cf - . | tar -C "$SRC" -xf - ;;
esac

[ -f "$SRC/MANIFEST.json" ] && [ -f "$SRC/setup/gen_manifest.py" ] || die "the release has no MANIFEST.json to check"
if ! grep -Eq '^template:[[:space:]]*true' "$SRC/project.yaml" 2>/dev/null; then
  die "the source is an onboarded project, not a release; install from a release tree"
fi
say "checking every file against MANIFEST.json"
if ! "$PY" "$SRC/setup/gen_manifest.py" --root "$SRC" --verify > "$STAGE/verify.log" 2>&1; then
  sed 's/^/  /' "$STAGE/verify.log" | tail -20 >&2
  die "the release does not match its MANIFEST.json; nothing was installed"
fi

ID="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["tree_digest"][:12])' "$SRC/MANIFEST.json")"
DEST="$PREFIX/releases/$ID"
if [ -d "$DEST" ]; then
  say "release $ID is already installed"
else
  mv "$SRC" "$DEST"
  say "installed release $ID at $DEST"
fi
if [ "$BOOTSTRAP" -eq 1 ] && [ ! -x "$DEST/.venv/bin/python" ]; then
  say "creating the release's local .venv"
  bash "$DEST/setup/bootstrap.sh" --python "$PY" > "$STAGE/bootstrap.log" 2>&1 || {
    tail -20 "$STAGE/bootstrap.log" >&2; die "bootstrap of $DEST failed"; }
fi
ln -sfn "releases/$ID" "$PREFIX/current"

mkdir -p "$BIN_DIR"
q="'${PREFIX//\'/\'\\\'\'}'"
cat > "$LAUNCHER.new" <<EOF
#!/bin/sh
$MARK; delete it to uninstall.
# Inside an Alpaca project it runs that project's own bin/alpaca; anywhere else it runs the
# installed release, so \`alpaca new\` works from any folder.
ALPACA_HOME=$q
d=\$(pwd -P)
while :; do
  if [ -f "\$d/ALPACA-MANIFEST" ] && [ -x "\$d/bin/alpaca" ]; then exec "\$d/bin/alpaca" "\$@"; fi
  [ "\$d" = / ] && break
  d=\$(dirname "\$d")
done
exec "\$ALPACA_HOME/current/bin/alpaca" "\$@"
EOF
chmod 0755 "$LAUNCHER.new" && mv -f "$LAUNCHER.new" "$LAUNCHER"
say "launcher at $LAUNCHER"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "$BIN_DIR is not on your PATH; add it, for example: export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
say "ok. Next: alpaca new"
