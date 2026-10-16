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
LAUNCHER_NEW=""
# CURRENT_MOVED is 1 from the moment `current` may have been pointed at the new release until the
# launcher is in place; OLD_CURRENT is where it pointed before.
CURRENT_MOVED=0
OLD_CURRENT=""
# Whatever way this script ends, its stage and an unfinished launcher file go, and a `current` that
# moved without its launcher goes back; a finished install has moved the launcher already, so there
# is nothing left to undo then.
cleanup() {
  if [ "$CURRENT_MOVED" = 1 ]; then
    CURRENT_MOVED=0
    if [ -n "$OLD_CURRENT" ]; then ln -sfn "$OLD_CURRENT" "$PREFIX/current" 2>/dev/null || true
    elif [ -L "$PREFIX/current" ]; then rm -f "$PREFIX/current"; fi
  fi
  if [ -n "$LAUNCHER_NEW" ]; then rm -f "$LAUNCHER_NEW"; fi
  if [ -d "$STAGE/stale" ]; then chmod -R u+w "$STAGE/stale" 2>/dev/null || true; fi
  rm -rf "$STAGE"
}
trap cleanup EXIT
# Ctrl-C, kill and a closed terminal end the script through the trap above (bash runs it once the
# command in progress is over), so none of them leaves a stage folder or an unfinished launcher.
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
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
  # The folder was checked when it was installed, not since: a copy left by an earlier install that
  # stopped, or one changed afterwards, must not become current unchecked. The checked source is the
  # reference (its MANIFEST.json and its gen_manifest.py), never the folder's own.
  if cmp -s "$SRC/MANIFEST.json" "$DEST/MANIFEST.json" \
     && "$PY" "$SRC/setup/gen_manifest.py" --root "$DEST" --verify > "$STAGE/verify-dest.log" 2>&1; then
    say "release $ID is already installed"
  else
    # -ef compares the files themselves, so a releases/ folder that is a link (moved to another disk)
    # still shows which release is current; a missing `current` is never the same file.
    if [ "$PREFIX/current" -ef "$DEST" ]; then
      sed 's/^/  /' "$STAGE/verify-dest.log" 2>/dev/null | tail -20 >&2 || true
      die "the current release folder $DEST does not match its MANIFEST.json; nothing was changed"
    fi
    say "release folder $DEST does not match its MANIFEST.json; replacing it with the checked copy"
    mv "$DEST" "$STAGE/stale" || die "cannot move the release folder $DEST aside; nothing was changed"
    mv "$SRC" "$DEST"
    say "installed release $ID at $DEST"
  fi
else
  mv "$SRC" "$DEST"
  say "installed release $ID at $DEST"
fi
# The release's .venv must work, not just exist: a .venv left half-built by an earlier run (pip failed
# after the folder was made) is checked, rebuilt, and removed again when it still cannot be built, so
# a failed bootstrap never leaves a venv that a later install would take as done.
if [ "$BOOTSTRAP" -eq 1 ]; then
  if [ -x "$DEST/.venv/bin/python" ] && bash "$DEST/setup/bootstrap.sh" --check > "$STAGE/check.log" 2>&1; then
    say "the release's local .venv is ready"
  else
    if [ -e "$DEST/.venv" ] || [ -L "$DEST/.venv" ]; then say "the release's local .venv is not ready; rebuilding it"
    else say "creating the release's local .venv"; fi
    bash "$DEST/setup/bootstrap.sh" --python "$PY" > "$STAGE/bootstrap.log" 2>&1 || {
      tail -20 "$STAGE/bootstrap.log" >&2
      rm -rf "$DEST/.venv"
      die "bootstrap of $DEST failed; its .venv was removed, so a retry starts clean"; }
  fi
fi

# Everything that can fail runs before the release becomes current: the launcher file is written
# first, then `current` moves, then the launcher is put in place (a rename). A failure up to that
# point leaves the old `current` and the old launcher as they were.
mkdir -p "$BIN_DIR" || die "cannot create $BIN_DIR"
BIN_DIR="$(cd "$BIN_DIR" && pwd -P)"
q="'${PREFIX//\'/\'\\\'\'}'"
qb="'${BIN_DIR//\'/\'\\\'\'}'"
LAUNCHER_NEW="$LAUNCHER.new"
cat > "$LAUNCHER_NEW" <<EOF || die "cannot write the launcher file $LAUNCHER_NEW"
#!/bin/sh
$MARK; delete it to uninstall.
# Inside an Alpaca project it runs that project's own bin/alpaca; anywhere else it runs the
# installed release, so \`alpaca new\` works from any folder.
ALPACA_HOME=$q
ALPACA_BIN_DIR=$qb
# \`alpaca update\` runs the installed release, never a project's copy: that copy may predate the
# verb, and the update replaces its files while it runs.
if [ "\${1-}" = update ]; then shift; exec "\$ALPACA_HOME/current/bin/alpaca" update --prefix "\$ALPACA_HOME" --bin-dir "\$ALPACA_BIN_DIR" "\$@"; fi
d=\$(pwd -P)
while :; do
  if [ -f "\$d/ALPACA-MANIFEST" ] && [ -x "\$d/bin/alpaca" ]; then exec "\$d/bin/alpaca" "\$@"; fi
  [ "\$d" = / ] && break
  d=\$(dirname "\$d")
done
exec "\$ALPACA_HOME/current/bin/alpaca" "\$@"
EOF
chmod 0755 "$LAUNCHER_NEW" || die "cannot write the launcher at $LAUNCHER"
OLD_CURRENT="$(readlink "$PREFIX/current" 2>/dev/null || true)"
CURRENT_MOVED=1
ln -sfn "releases/$ID" "$PREFIX/current" || die "cannot move $PREFIX/current"
if ! mv -f "$LAUNCHER_NEW" "$LAUNCHER"; then
  if [ -n "$OLD_CURRENT" ]; then ln -sfn "$OLD_CURRENT" "$PREFIX/current" || true; fi
  die "cannot put the launcher at $LAUNCHER; current is back at ${OLD_CURRENT:-its old place}"
fi
LAUNCHER_NEW=""
CURRENT_MOVED=0
say "launcher at $LAUNCHER"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "$BIN_DIR is not on your PATH; add it, for example: export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
say "ok. Next: alpaca new"
