import datetime, hashlib, json, os, tempfile

# M2.2: the process-wide clock. None means the real wall clock; a test installs a FixedClock
# with set_clock so now_iso (and everything routed through it: the pad renderer, the analytics
# fold, the export) becomes deterministic without those call sites having to thread a clock.
_CLOCK = None

def set_clock(clock) -> None:
    """Install a Clock (any zero-arg callable returning an offset-bearing ISO string), or None
    to restore the real wall clock. Tests set a FixedClock; production never calls this."""
    global _CLOCK
    _CLOCK = clock

def get_clock():
    return _CLOCK

def now_iso() -> str:
    if _CLOCK is not None:
        return _CLOCK()
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()

def read_text(path) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()

def write_text(path, text) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    u = os.umask(0); os.umask(u)
    os.chmod(tmp, 0o666 & ~u)
    os.replace(tmp, path)


def from_caller(path):
    """A command-line path, read from the folder the command was run in.

    bin/alpaca-python changes to the install root before Python starts, and passes the folder it
    was called from as ALPACA_CALLER_CWD. A relative path is joined to that folder, but only when
    Python is running in the install root (the wrapper's doing); `python -m alpaca` run elsewhere
    reads it from its own folder as usual. None and absolute paths come back unchanged."""
    caller = os.environ.get("ALPACA_CALLER_CWD", "")
    if not path or os.path.isabs(path) or not os.path.isabs(caller):
        return path
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        in_root = os.path.samefile(os.getcwd(), root)
    except OSError:
        in_root = False
    return os.path.join(caller, path) if in_root else path
