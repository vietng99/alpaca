"""File effects below a project's runtime area that follow no link (t-070 round 2, class C-A rule 4).

A file effect (a create, write, chmod or unlink) under `<root>/.alpaca/` must stay inside it. A path
string is resolved by the kernel at every use, so a component that an agent replaced by a link sends
the effect elsewhere: check round 1 found the SQLite temp sweep setting an outside folder to 0700
and deleting a file of the user's through a `.alpaca/tmp` link (A-4,
.alpaca/proofs/op-008/t-070.logs/attacks/b04_sqlite_tmpdir_links.out). Here every component is
opened with `O_NOFOLLOW | O_DIRECTORY` relative to its parent's descriptor, and every effect is made
through the descriptor of the folder it acts in.

Imports only `os` and `stat`, so `alpaca/__init__.py` can use it before any module imports
`sqlite3`. The vault has the same walk of its own (`alpaca/wiki/store/db.py`
`open_vault_directory`, `alpaca/wiki/ingest/drain.py` `_projection_target`).
"""
import os
import stat

_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
              | getattr(os, "O_CLOEXEC", 0))


class Refused(OSError):
    """A component is a link, not a folder, or not the user's."""


def open_runtime(root, runtime=".alpaca", create=False):
    """A descriptor of `<root>/<runtime>`, refused when that is a link, or absent unless `create`.
    The root itself is the caller's choice and is resolved as given. With `create` the root and the
    runtime folder (mode 0700) are made when absent, as the `mkdir(parents=True)` these creates
    replaced made them; a link standing at `<runtime>` is never followed (mkdir does not follow it,
    and the open below refuses it)."""
    if create:
        os.makedirs(root, exist_ok=True)
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        if create:
            try:
                os.mkdir(runtime, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        return os.open(runtime, _DIR_FLAGS, dir_fd=root_fd)
    finally:
        os.close(root_fd)


def open_dir_below(runtime_fd, parts, create=True, mode=0o700, chmod=True):
    """A descriptor of the folder `parts` names below `runtime_fd`, each component opened without
    following a link and created (with `mode`) when `create` and absent. Every component must be a
    folder owned by this user; the last one gets `mode` (fchmod) when it has another and `chmod`. Raises
    `Refused` (or the OSError of the walk) and leaves no descriptor open."""
    fd = os.dup(runtime_fd)
    try:
        for i, part in enumerate(parts):
            if create:
                try:
                    os.mkdir(part, mode, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise Refused(exc.errno, "not a plain folder: %s" % "/".join(parts[:i + 1])) from exc
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise Refused("not this user's folder: %s" % "/".join(parts[:i + 1]))
        if chmod and stat.S_IMODE(os.fstat(fd).st_mode) != mode:
            os.fchmod(fd, mode)
        return fd
    except BaseException:
        os.close(fd)
        raise


def sweep_old(dir_fd, older_than):
    """Unlink every plain file in the folder `dir_fd` whose mtime is before `older_than`, through
    the descriptor; a link, a folder or a name that changed meanwhile is left. Returns the count."""
    removed = 0
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_mtime < older_than:
                    os.unlink(entry.name, dir_fd=dir_fd)
                    removed += 1
            except OSError:
                pass
    return removed


def prepare_below(root, parts, *, mode=0o700, older_than=None, runtime=".alpaca"):
    """Open (creating) `<root>/<runtime>/<parts...>` with no link followed, give it `mode`, sweep
    files older than `older_than` from it, and return its path; None when the runtime area is absent
    or any component is refused. Never raises."""
    try:
        runtime_fd = open_runtime(root, runtime)
    except OSError:
        return None
    try:
        fd = open_dir_below(runtime_fd, tuple(parts), create=True, mode=mode)
    except OSError:
        return None
    finally:
        os.close(runtime_fd)
    try:
        if older_than is not None:
            sweep_old(fd, older_than)
    finally:
        os.close(fd)
    return os.path.join(root, runtime, *parts)


def open_file_below(root, parts, name, flags, mode=0o600, *, create=True, runtime=".alpaca"):
    """A descriptor of the file `<root>/<runtime>/<parts...>/<name>`, no link followed at any level:
    the folders as `open_dir_below` walks them (created when `create`, an existing folder's mode
    left alone), the file opened with `O_NOFOLLOW` relative to its folder's descriptor. `flags` are
    the caller's (`O_CREAT` and the like). Raises `Refused` or the OSError of the walk."""
    runtime_fd = open_runtime(root, runtime, create=create)
    try:
        dir_fd = open_dir_below(runtime_fd, tuple(parts), create=create, chmod=False)
    finally:
        os.close(runtime_fd)
    try:
        return os.open(name, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), mode,
                       dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def write_below(root, parts, name, data, mode=0o600, *, runtime=".alpaca"):
    """Write `data` (bytes) to `<root>/<runtime>/<parts...>/<name>` atomically, the file carrying
    `mode` from its creation, through the folder's descriptor so no link is followed at any level
    (a link already at `name` is replaced, not followed)."""
    runtime_fd = open_runtime(root, runtime, create=True)
    try:
        dir_fd = open_dir_below(runtime_fd, tuple(parts), create=True, chmod=False)
    finally:
        os.close(runtime_fd)
    tmp = ".tmp-%s-%s" % (name, os.urandom(6).hex())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | getattr(os, "O_CLOEXEC", 0), mode, dir_fd=dir_fd)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(dir_fd)
