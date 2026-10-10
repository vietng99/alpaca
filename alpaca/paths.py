import os, re, stat

MANIFEST = "ALPACA-MANIFEST"

class RootNotFound(Exception):
    pass

def _walk(start):
    cur = os.path.realpath(start)
    while True:
        if os.path.isfile(os.path.join(cur, MANIFEST)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise RootNotFound("no %s above %s" % (MANIFEST, start))
        cur = parent


def root(start=None) -> str:
    """Explicit input, then cwd, then an environment fallback outside a project.

    An inherited environment cannot redirect a process already inside another
    Alpaca copy. Claude hooks pass their cwd explicitly.
    """
    if start is not None:
        return _walk(start)
    try:
        return _walk(os.getcwd())
    except RootNotFound:
        env = os.environ.get("ALPACA_ROOT")
        if env:
            return _walk(env)
        raise

def manifest_path(root_dir: str) -> str:
    """The path to the harness manifest inside a project root. The single place the manifest
    file name is joined to a root, so alpaca.manifest and alpaca.doctor never spell it two ways."""
    return os.path.join(root_dir, MANIFEST)

def runtime_dir(root_dir: str) -> str:
    return os.path.join(root_dir, ".alpaca")

def db_path(root_dir: str) -> str:
    return os.path.join(runtime_dir(root_dir), "alpaca.db")

def config_dir(root_dir=None) -> str:
    """Operator posture belongs to this project, never an account's home."""
    return os.path.join(runtime_dir(root(root_dir)), "config")

def escape_cwd(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", os.path.abspath(path))

def transcript_dir(root_dir: str) -> str:
    """Explicitly imported transcripts for this project only."""
    return os.path.join(runtime_dir(root_dir), "transcripts")

def vault_dir(root_dir: str) -> str:
    """The Rune2 vault, as `alpaca/wiki/config.py` `Config.for_project` places it."""
    return os.path.join(runtime_dir(root_dir), "wiki")

#: The one folder of the vault directory that is not Rune2's: the record's decision pages
#: (`alpaca/decisions.py` `wiki_dir`), which the hub's Library reads.
VAULT_RECORD_PAGES = "decisions"

def _identity(path):
    """(device, inode) of `path` after links, or None when it cannot be stat'ed."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def _climb(root_dir, path):
    """Yield (identity, name just below) for the real path of `path` and each folder above it, leaf
    first. The walk ends at the project root's folder (by device and inode): a path inside the root
    meets every runtime folder before the root, so nothing above it can change an answer (op-008
    t-463, review minor M-1). A path outside the root climbs to `/`."""
    top = _identity(root_dir)
    here = os.path.realpath(os.fspath(path))
    below = None
    while True:
        ident = _identity(here)
        yield ident, below
        if ident is not None and ident == top:
            return
        parent = os.path.dirname(here)
        if parent == here:
            return
        here, below = parent, os.path.basename(here)


def in_vault(root_dir: str, path) -> bool:
    """True when `path` lies inside the Rune2 vault, outside its `decisions/` folder (op-008 t-441).

    The vault holds a withdrawn source's note and every quarantined capture (`wiki/private/`) as
    plain files; only Rune2's doors may serve what it holds (`alpaca/wiki/servable.py`), so every
    other reader of project files refuses a path this answers True for. The path is resolved first
    (symlinks, `..`), and each folder above it is compared with the vault by device and inode, so a
    link, a bind mount or a case alias that names the vault another way is still inside it. The
    vault folder itself is False (a browser may list it, to reach `decisions/`); a name directly
    below it other than `decisions` is True, whatever its case, so a folder Rune2 adds later is
    covered too."""
    vault = _identity(vault_dir(root_dir))
    if vault is None:
        return False
    for ident, below in _climb(root_dir, path):
        if ident == vault:
            return below is not None and below != VAULT_RECORD_PAGES
    return False


#: Folders of the runtime directory that hold private runtime state: imported session transcripts,
#: the observability state and the tool pool (every tool call of every session).
PRIVATE_RUNTIME_DIRS = ("transcripts", "observability", "pool")
#: Folders of the project root that hold an agent's own session transcripts.
PRIVATE_AGENT_DIRS = ((".claude", "projects"), (".codex", "sessions"))
#: Secret files by name, wherever they sit: the dashboard login (`alpaca/serve.py` `files_auth`),
#: its cookie key and authenticator secret (`alpaca/weblogin.py`), the public tunnel's address,
#: the service environment and the usual credential files.
PRIVATE_NAMES = frozenset({"files-auth", "web-session-key", "totp-secret", "terminal-url",
                           "service.env", ".env", "auth.json", "credentials.json",
                           ".credentials.json", ".netrc", ".git-credentials", ".pgpass"})
#: Secret or database files by ending: the record db and any SQLite file with its journal, keys,
#: certificates and tokens.
PRIVATE_ENDINGS = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3",
                   ".sqlite-wal", ".sqlite-shm", ".pem", ".key", ".token")
PRIVATE_PREFIXES = (".env.", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
#: The suffix of a proof's kept-evidence folder (`alpaca/proof.py` `KEPT_SUFFIX`).
KEPT_FOLDER_SUFFIX = ".evidence"
_NUMBER_PREFIX = re.compile(r"^[0-9]+-")


def _secret_name(name, kept) -> bool:
    """True when the file name `name` is a secret's. The name is read with a leading `NN-` number
    taken off, since the seal names a kept copy `NN-<basename>`; inside a kept folder (`kept`) it is
    also read with a leading dot put back, since the seal's slug drops it (`.env` is kept as
    `NN-env`)."""
    low = name.lower()
    names = {low, _NUMBER_PREFIX.sub("", low)}
    if kept:
        names |= {"." + n for n in names if not n.startswith(".")}
    return any(n in PRIVATE_NAMES or n.endswith(PRIVATE_ENDINGS) or n.startswith(PRIVATE_PREFIXES)
               for n in names)


def private_runtime(root_dir: str, path) -> bool:
    """True when `path` is a private runtime file: one only the machine's operator reads (op-008
    t-463). Every reader that hands project files to someone else (the dashboard's file readers,
    the proof seal that copies evidence into `.alpaca/proofs/`) refuses it, as it refuses
    `in_vault`.

    Private is: a secret by name (`PRIVATE_NAMES`, `PRIVATE_ENDINGS`, `PRIVATE_PREFIXES`), read for
    both the name as given and the name after links, with an evidence number taken off; and
    anything inside the runtime directory's transcripts, observability or tool pool, or an agent's
    session folder at the root, found by device and inode on the real path, so a link or another
    name for the folder (the live project's `.te -> .alpaca`) is still inside."""
    given = os.fspath(path)
    real = os.path.realpath(given)
    for p in {given, real}:
        if _secret_name(os.path.basename(p),
                        os.path.basename(os.path.dirname(p)).endswith(KEPT_FOLDER_SUFFIX)):
            return True
    closed = [os.path.join(runtime_dir(root_dir), d) for d in PRIVATE_RUNTIME_DIRS]
    closed += [os.path.join(root_dir, *parts) for parts in PRIVATE_AGENT_DIRS]
    closed = {i for i in map(_identity, closed) if i is not None}
    if not closed:
        return False
    return any(ident in closed for ident, _below in _climb(root_dir, real))


#: Top-level folders of the project the dashboard's file browser never serves.
BROWSER_ROOT_DENY = frozenset({".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "analytics"})
#: The folders of the runtime directory the browser serves (of `wiki/`, only `decisions/`; see
#: `in_vault`).
BROWSER_RUNTIME_OPEN = ("proofs", "wiki")
#: The first bytes of every SQLite database file; the browser refuses a file that starts so.
SQLITE_MAGIC = b"SQLite format 3\x00"


def browser_serves(root_dir: str, full) -> bool:
    """True when the dashboard's file browser (`alpaca/serve.py` `_files_resolve`) serves the file
    or folder at `full`, a real path, by its path. The browser's one rule, also the proof seal's
    rule for what it may copy into the served `.evidence/` folder (op008/followups review B-2):

    - inside the project root, and not below a top-level `BROWSER_ROOT_DENY` folder;
    - no part of the path starts with a dot (`.git`, `.claude`, `.codex`, `.ssh`, `.netrc`, any
      `.env*`), except a leading runtime folder `.alpaca`, and under it only `proofs/` and `wiki/`;
    - not in the Rune2 vault (`in_vault`) and not a private runtime file (`private_runtime`);
    - not a `.tar.gz` archive.

    The browser's text reader also refuses a SQLite file by its first bytes (`SQLITE_MAGIC`), and
    the seal applies that check to the bytes it copies."""
    base = os.path.realpath(root_dir)
    full = os.fspath(full)
    if not (full == base or full.startswith(base + os.sep)):
        return False
    relnorm = "" if full == base else os.path.relpath(full, base)
    parts = [] if relnorm in ("", ".") else relnorm.split(os.sep)
    if parts and parts[0] in BROWSER_ROOT_DENY:
        return False
    runtime = os.path.basename(runtime_dir(base))
    for idx, part in enumerate(parts):
        if part.startswith(".") and not (idx == 0 and part == runtime):
            return False
    if parts and parts[0] == runtime and not (len(parts) >= 2 and parts[1] in BROWSER_RUNTIME_OPEN):
        return False
    if in_vault(base, full) or private_runtime(base, full):
        return False
    if (parts[-1] if parts else "").lower().endswith(".tar.gz"):
        return False
    return True


def _opened_path(fd, given):
    """The path the kernel holds for the open descriptor `fd`: `/proc/self/fd` on Linux,
    `F_GETPATH` on macOS. Elsewhere the real path of `given` taken after the open, which the
    caller then matches to the descriptor by device and inode."""
    try:
        found = os.readlink("/proc/self/fd/%d" % fd)
        if os.path.isabs(found):
            return found
    except OSError:
        pass
    try:
        import fcntl
        if hasattr(fcntl, "F_GETPATH"):
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
    except (ImportError, OSError, ValueError):
        pass
    return os.path.realpath(given)


def open_checked(path, allowed):
    """Open the regular file at `path` for reading in binary mode, then check the file that was
    opened, not the name: the path the kernel holds for the descriptor must pass `allowed(real)`
    and still name the same file (device and inode). Returns the file object, or None.

    This closes the check-then-open race (op-008 t-463, review R-3): a reader that checks a path
    and later opens it by name follows whatever link a writer put on that path in between. The
    last part is opened with O_NOFOLLOW and O_NONBLOCK (a link there, or a pipe that would hang
    the reader, is refused); a folder on the way that became a link is caught by the check of the
    opened file's own path, which names the folder it really sits in."""
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_CLOEXEC", 0))
    try:
        fd = os.open(os.fspath(path), flags)
    except (OSError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        real = _opened_path(fd, path) if stat.S_ISREG(st.st_mode) else None
        ok = (real is not None and bool(allowed(real))
              and _identity(real) == (st.st_dev, st.st_ino))
    except (OSError, ValueError):
        ok = False
    except BaseException:
        os.close(fd)
        raise
    if ok:
        return os.fdopen(fd, "rb")
    os.close(fd)
    return None
