import os, re

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
