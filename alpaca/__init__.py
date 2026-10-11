VERSION = "1.2.2"

import os as _os

#: Where Alpaca's processes put SQLite temp files, under the project's runtime area (t-070 RC5).
SQLITE_TMP = (".alpaca", "tmp", "sqlite")
#: A temp file left there longer than this is stale: SQLite unlinks each temp file as soon as it
#: opens it, so a name can only remain from a process that died between the two calls.
SQLITE_TMP_STALE_S = 3600.0


def sqlite_tmpdir(root=None, environ=None):
    """Point SQLite's temp files (large sorts, temp B-trees, statement journals) at a private folder on
    the project's own filesystem, `<root>/.alpaca/tmp/sqlite` (mode 0700), unless `SQLITE_TMPDIR` is
    already set. With neither `SQLITE_TMPDIR` nor `TMPDIR` set, which is how the hooks run, SQLite uses
    /var/tmp, which on the owner's host is a busy spinning disk while the project is on NVMe: one
    `pwrite64` of the echo-index sort there slept 56 s (t-070 A-4). `temp_store` is left as it is, so
    what SQLite keeps in memory does not change; only where its temp files go.

    The library reads `SQLITE_TMPDIR` once, when `sqlite3` is first imported (sqlite3_initialize), so
    this runs from the package's own import, before any Alpaca module imports `sqlite3`. `root`
    defaults to the project found from the working folder (the `alpaca.paths` rule: the nearest
    folder above it that holds ALPACA-MANIFEST), not the tree that holds this package (t-070 round 2,
    I-2): `bin/alpaca-python` runs from the install root, so a hook keeps that project's folder, and a
    child started for another served root gets that root's from `alpaca.child.child_env`. Nothing is
    created where the root has no runtime area yet. Every component below `.alpaca` is opened without
    following a link and must be a folder of this user's (`alpaca._nofollow`, round 2 A-4), and the
    files older than `SQLITE_TMP_STALE_S` are removed through the folder's descriptor. Returns the
    folder, or None when nothing was set. Never raises."""
    env = _os.environ if environ is None else environ
    try:
        if env.get("SQLITE_TMPDIR"):
            return None
        if root is None:
            root = _project_root()
            if root is None:
                return None
        import time as _time
        from alpaca import _nofollow
        folder = _nofollow.prepare_below(root, SQLITE_TMP[1:], mode=0o700, runtime=SQLITE_TMP[0],
                                         older_than=_time.time() - SQLITE_TMP_STALE_S)
        if folder is None:
            return None
        env["SQLITE_TMPDIR"] = folder
        return folder
    except Exception:
        return None


def _project_root():
    """The project the working folder is in, or None (no ALPACA-MANIFEST above it, or no cwd)."""
    try:
        from alpaca import paths as _paths
        return _paths._walk(_os.getcwd())
    except Exception:
        return None

sqlite_tmpdir()
