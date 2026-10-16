"""The one place attach writes to disk (op-062 t-647, fix round 3).

Attach promises to keep its files only inside the studio. Three fix rounds closed one writer after
another that followed a link out of the studio (a plain open, a makedirs, a temp file at a name an
attacker can guess). The rule now has one owner: every function here takes the studio `root`, walks
the path from the real root folder with `lstat`, and refuses a link or a non-folder at any folder of
the chain and at the leaf. Files are written through `tempfile.mkstemp` (random name, `O_EXCL` and
`O_NOFOLLOW`) and `os.replace`, so a hard link at the target keeps its outside bytes. The test
`alpaca/tests/test_attach_write_guard.py` fails the suite on any create, replace, make or open for
writing in `alpaca/attach/` outside this file.

A path may be spelled from `root` as the caller has it or from `realpath(root)` (a studio reached
through a link has both spellings in use); the walk always starts at the real root. Every function
raises `AttachError`, never `OSError`.

Limits, stated so no one reads more into this module than it does:

* Deletes (`list_names`, `remove_file`, `remove_tree`, `clear_path`, after the writes) open every folder of
  the chain from the real root with `O_DIRECTORY|O_NOFOLLOW` and act through the descriptor, so a folder swapped
  for a link after a check is not followed (t-652 round 2, R2-07).
* The path functions above the deletes walk with `lstat` and then write by path. A folder swapped for a link
  between the check and the write, by a process that can already write inside the studio while the attach
  lock is held, is narrowed by a second check after the file exists and is not closed. `Folder` (end
  of this file) is the `dir_fd` walk that removes that gap: the proof seal and the verifier keep and
  lay the attached repo's bytes only through it (op-062 t-650 fix round 4).
* The record (`alpaca/db.py`) and `project.yaml` (`alpaca/project.py`) are written by modules every
  verb shares, not by attach. The promise covers the files attach itself writes.
* The guard proves "no Python file write by name outside this file". It does not see `os.sendfile`,
  `mmap`, `ctypes` or a shell redirect inside a child process.
* A folder that a child process writes into (`git clone`, and in t-648 `git worktree add`, seeds and
  gate run folders) gets its path from `make_dir`. The lock files (`fetch.lock`,
  `baseline/<key>.lock`) come from `open_lock`.
"""
from __future__ import annotations

import errno
import hashlib
import os
import secrets
import shutil
import stat
import tempfile

from alpaca.attach import AttachError

#: the sentence every link refusal uses (the tests pin its "is a link" part)
LINK = "%s is a link; attach keeps its files only inside the studio"
NOT_FOLDER = "%s is not a folder; attach keeps its files only inside the studio"
OUTSIDE = "%s lies outside the studio; attach keeps its files only inside the studio"


class WriteFailed(AttachError):
    """An `OSError` while writing, shaped as a refusal. `strerror` is the system's reason."""

    def __init__(self, message, strerror=""):
        super().__init__(message)
        self.strerror = strerror


# ------------------------------------------------------------------------------------- the path

def _short(parts):
    return "/".join(parts[-2:])


def _resolve(root, path):
    """(real root, parts below it). Refuses a path outside the studio, an empty path and any `..`."""
    root_abs = os.path.abspath(str(root))
    root_real = os.path.realpath(root_abs)
    text = str(path)
    if not os.path.isabs(text):
        text = os.path.join(root_abs, text)
    if ".." in text.split(os.sep):
        raise AttachError(OUTSIDE % os.path.basename(text.rstrip(os.sep)))
    full = os.path.abspath(text)
    for base in (root_abs, root_real):
        if full.startswith(base.rstrip(os.sep) + os.sep):
            rel = full[len(base.rstrip(os.sep)) + 1:]
            parts = rel.split(os.sep)
            if all(parts) and "." not in parts:
                return root_real, parts
    raise AttachError(OUTSIDE % (os.path.basename(full.rstrip(os.sep)) or full))


def _need_folder(cur, name, parts_so_far, *, create, made):
    """lstat `cur/name` as a folder of the chain; make it (one `os.mkdir`) when `create`. Returns its path."""
    nxt = os.path.join(cur, name)
    try:
        st = os.lstat(nxt)
    except FileNotFoundError:
        if not create:
            raise AttachError("%s does not exist" % _short(parts_so_far)) from None
        try:
            os.mkdir(nxt)
        except FileExistsError:
            pass                                    # made by someone else meanwhile: it is checked below
        except OSError as exc:
            raise WriteFailed("%s cannot be created: %s" % (_short(parts_so_far), exc.strerror), exc.strerror) from None
        else:
            if made is not None:
                made.append(nxt)
        try:
            st = os.lstat(nxt)
        except OSError as exc:
            raise WriteFailed("%s cannot be read: %s" % (_short(parts_so_far), exc.strerror), exc.strerror) from None
    except OSError as exc:                          # EACCES on a folder of the chain, ELOOP, ENAMETOOLONG, ...
        raise WriteFailed("%s cannot be read: %s" % (_short(parts_so_far), exc.strerror), exc.strerror) from None
    if stat.S_ISLNK(st.st_mode):
        raise AttachError(LINK % name)
    if not stat.S_ISDIR(st.st_mode):
        raise AttachError(NOT_FOLDER % name)
    return nxt


def chain(root, path, *, create=False, made=None):
    """(real parent folder, leaf name) of `path`, which must lie under `root` (absolute, or relative to it).
    Every folder from the real root down to the parent is lstat'd: a link raises `LINK % name`, a
    non-folder raises `NOT_FOLDER % name`. With `create=True` each missing folder is made with one
    `os.mkdir` (never makedirs) and appended to `made` right after its mkdir succeeded, shallowest
    first, so a later failure still leaves the caller a complete list to undo."""
    root_real, parts = _resolve(root, path)
    cur = root_real
    for i, name in enumerate(parts[:-1]):
        cur = _need_folder(cur, name, parts[:i + 1], create=create, made=made)
    return cur, parts[-1]


# ------------------------------------------------------------------------------------ the writes

def _umask():
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    old = os.umask(0)
    os.umask(old)
    return old


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def write_bytes(root, path, data, *, mode=None, create_only=False, made=None) -> None:
    """The one way attach writes a file. Parent folders are made (and listed in `made`) when missing.

    `mode` is the exact file mode (set with `fchmod`, so the umask does not change it). With `mode=None`
    an existing regular target keeps its mode and a new one gets `0o644` less the umask. `create_only`
    refuses an existing target and creates it exclusively; its path is appended to `made` right after
    the file exists. Without `create_only` the bytes go to a new file in the same folder that then
    replaces the target, so a link or a hard link at the target is replaced, not written through."""
    data = bytes(data)
    parent, name = chain(root, path, create=True, made=made)
    leaf = os.path.join(parent, name)
    try:
        st = os.lstat(leaf)
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise WriteFailed("%s cannot be written: %s" % (name, exc.strerror), exc.strerror) from None
    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            raise AttachError(LINK % name)
        if create_only:
            raise AttachError("%s already exists" % name)
        if not stat.S_ISREG(st.st_mode):
            raise AttachError("%s is not a plain file; attach keeps its files only inside the studio" % name)
    if mode is None:
        mode = stat.S_IMODE(st.st_mode) if st is not None else 0o644 & ~_umask()
    try:
        if create_only:
            _create_only(parent, leaf, name, data, mode, made)
        else:
            _replace(parent, leaf, name, data, mode)
    except OSError as exc:
        raise WriteFailed("%s cannot be written: %s" % (name, exc.strerror), exc.strerror) from None


def _moved(parent):
    """True when `parent` no longer resolves to itself (a folder of the chain was swapped for a link)."""
    return os.path.realpath(parent) != parent


def _create_only(parent, leaf, name, data, mode, made):
    try:
        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        raise AttachError("%s already exists" % name) from None
    if made is not None:
        made.append(leaf)
    try:
        try:
            if _moved(parent):
                raise AttachError(LINK % name)
            os.fchmod(fd, mode)
            _write_all(fd, data)
        finally:
            os.close(fd)
    except BaseException:
        _unlink_quiet(leaf)
        raise


def _replace(parent, leaf, name, data, mode):
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".tmp-", suffix=".tmp")
    try:
        try:
            if _moved(parent):
                raise AttachError(LINK % name)
            os.fchmod(fd, mode)
            _write_all(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, leaf)
    except BaseException:
        _unlink_quiet(tmp)
        raise


def remove(root, path) -> bool:
    """Delete the plain file at `path` and return True; return False when nothing is there (the file or a
    folder above it is missing). The path is walked from the real root with `lstat` like every other
    function here: a link at any folder of the chain or at the leaf raises AttachError and deletes
    nothing, and so does a leaf that is not a plain file. A bare `os.unlink` of a path spelled through
    the studio follows a link at a folder of the chain and deletes a file outside the studio (t-648
    adversarial round 1, B1); the rule for later code is that attach deletes a file only through this
    function. Raises AttachError, never OSError."""
    root_real, parts = _resolve(root, path)
    cur = root_real
    for i, name in enumerate(parts):
        nxt = os.path.join(cur, name)
        try:
            st = os.lstat(nxt)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise WriteFailed("%s cannot be read: %s" % (_short(parts[:i + 1]), exc.strerror), exc.strerror) from None
        if stat.S_ISLNK(st.st_mode):
            raise AttachError(LINK % name)
        if i < len(parts) - 1:
            if not stat.S_ISDIR(st.st_mode):
                raise AttachError(NOT_FOLDER % name)
            cur = nxt
    if not stat.S_ISREG(st.st_mode):
        raise AttachError("%s is not a plain file; attach keeps its files only inside the studio" % parts[-1])
    if _moved(cur):
        raise AttachError(LINK % parts[-1])
    try:
        os.unlink(nxt)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise WriteFailed("%s cannot be deleted: %s" % (parts[-1], exc.strerror), exc.strerror) from None
    return True


def _walk_to_leaf(root, path):
    """(parent folder, leaf name, lstat of the leaf or None) of `path`: every folder above the leaf is
    lstat'd as a real folder (a link raises AttachError); a missing folder above, or a missing leaf, gives
    a leaf lstat of None."""
    root_real, parts = _resolve(root, path)
    cur = root_real
    for i, name in enumerate(parts):
        nxt = os.path.join(cur, name)
        try:
            st = os.lstat(nxt)
        except FileNotFoundError:
            return cur, name, None
        except OSError as exc:
            raise WriteFailed("%s cannot be read: %s" % (_short(parts[:i + 1]), exc.strerror), exc.strerror) from None
        if i == len(parts) - 1:
            return cur, name, st
        if stat.S_ISLNK(st.st_mode):
            raise AttachError(LINK % name)
        if not stat.S_ISDIR(st.st_mode):
            raise AttachError(NOT_FOLDER % name)
        cur = nxt
    raise AttachError(OUTSIDE % str(path))                      # not reached: _resolve returns at least one part


def drop_link(root, path) -> bool:
    """Delete the symbolic link at `path` and return True. Return False when nothing is there, or when what
    is there is not a link (it is left alone). Every folder above the leaf is walked from the real root with
    `lstat`; a link in the chain raises AttachError and deletes nothing. The one delete for a folder that a
    swap turned into a link: it removes the link itself, never what it points to."""
    parent, name, st = _walk_to_leaf(root, path)
    if st is None or not stat.S_ISLNK(st.st_mode):
        return False
    if _moved(parent):
        raise AttachError(LINK % name)
    try:
        os.unlink(os.path.join(parent, name))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise WriteFailed("%s cannot be deleted: %s" % (name, exc.strerror), exc.strerror) from None
    return True


def make_dir(root, path, *, exclusive=False, made=None) -> str:
    """Make a folder at `path` (and the missing folders above it) and return its proved real path.

    Without `exclusive` an existing real folder is accepted. With `exclusive=True` the last part must
    not exist: its `FileExistsError` propagates unchanged, so the caller can use the mkdir as a claim.
    The rule for later code: a folder a child process writes into (git clone, git worktree add, seeds,
    gate run folders) gets its path from this function, never from a plain string."""
    root_real, parts = _resolve(root, path)
    cur = root_real
    for i, name in enumerate(parts[:-1]):
        cur = _need_folder(cur, name, parts[:i + 1], create=True, made=made)
    if not exclusive:
        return _need_folder(cur, parts[-1], parts, create=True, made=made)
    leaf = os.path.join(cur, parts[-1])
    try:
        os.mkdir(leaf)
    except FileExistsError:
        raise
    except OSError as exc:
        raise WriteFailed("%s cannot be created: %s" % (_short(parts), exc.strerror), exc.strerror) from None
    if made is not None:
        made.append(leaf)
    return leaf


def read_bytes(root, path):
    """The bytes of a regular file reached with no link in the chain or at the leaf, else None. Never raises."""
    try:
        parent, name = chain(root, path)
        fd = os.open(os.path.join(parent, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except (AttachError, OSError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)


#: the most `read_existing` reads (the files it serves are state.json and the exclude file)
READ_LIMIT = 8 << 20


def read_existing(root, path, *, limit=READ_LIMIT):
    """The bytes of the plain file at `path`, or None when nothing is there (the file or a folder above
    it is missing, proved by `lstat` raising `FileNotFoundError`). A file that is there but cannot be
    used raises AttachError: a link, a non-file, an unreadable file (mode 000), a file larger than `limit`.
    Use it where the answer "absent" later decides a rollback by deleting: `read_bytes` folds an
    unreadable file into None, and a rollback that treats None as "there was nothing" deletes a file the
    owner had. Raises AttachError, never OSError."""
    root_real, parts = _resolve(root, path)
    cur = root_real
    for i, name in enumerate(parts):
        nxt = os.path.join(cur, name)
        try:
            st = os.lstat(nxt)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WriteFailed("%s cannot be read: %s" % (_short(parts[:i + 1]), exc.strerror), exc.strerror) from None
        if stat.S_ISLNK(st.st_mode):
            raise AttachError(LINK % name)
        if i < len(parts) - 1:
            if not stat.S_ISDIR(st.st_mode):
                raise AttachError(NOT_FOLDER % name)
            cur = nxt
    leaf = parts[-1]
    if not stat.S_ISREG(st.st_mode):
        raise AttachError("%s is not a plain file; attach keeps its files only inside the studio" % leaf)
    try:
        fd = os.open(nxt, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AttachError(LINK % leaf) from None
        raise WriteFailed("%s exists but cannot be read: %s; attach will not replace a file it cannot put back"
                          % (leaf, exc.strerror), exc.strerror) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AttachError("%s is not a plain file; attach keeps its files only inside the studio" % leaf)
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                raise AttachError("%s is larger than %d bytes; attach will not replace a file it cannot put back"
                                  % (leaf, limit))
            chunks.append(chunk)
    except OSError as exc:
        raise WriteFailed("%s exists but cannot be read: %s" % (leaf, exc.strerror), exc.strerror) from None
    finally:
        os.close(fd)


def open_lock(root, path) -> int:
    """An fd for a lock file inside the studio (the caller flocks it and closes it). Parent folders are
    made; a link in the chain or at the leaf is refused. The file is created `0o600` and kept."""
    parent, name = chain(root, path, create=True)
    leaf = os.path.join(parent, name)
    try:
        fd = os.open(leaf, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AttachError(LINK % name) from None
        raise WriteFailed("%s cannot be opened: %s" % (name, exc.strerror), exc.strerror) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or _moved(parent):
            raise AttachError(LINK % name)
    except BaseException:
        os.close(fd)
        raise
    return fd


# ------------------------------------------------------------------------------------ the deletes

def _open_folder(root, parts):
    """An fd for the folder `parts` below the real root, or None when a folder of the chain is missing.
    Every step is `os.open(name, O_DIRECTORY|O_NOFOLLOW, dir_fd=parent)`, so a folder swapped for a link
    after an earlier check is refused here and a later delete acts on the folder that was opened, not on
    a path that can change again. A link or a non-folder raises AttachError."""
    fd = os.open(os.path.realpath(os.path.abspath(str(root))), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    for i, name in enumerate(parts):
        try:
            nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        except FileNotFoundError:
            os.close(fd)
            return None
        except OSError as exc:
            kind = None
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                try:
                    kind = stat.S_ISLNK(os.lstat(name, dir_fd=fd).st_mode)
                except OSError:
                    kind = None
            os.close(fd)
            if kind is True:
                raise AttachError(LINK % name) from None
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise AttachError(NOT_FOLDER % name) from None
            raise WriteFailed("%s cannot be read: %s" % (_short(parts[:i + 1]), exc.strerror), exc.strerror) from None
        os.close(fd)
        fd = nxt
    return fd


def _folder_parts(root, path):
    root_real, parts = _resolve(root, path)
    return parts


def list_names(root, path) -> list:
    """The names in the folder `path` of the studio ([] when the folder is missing). The folder is opened
    from the real root one `O_NOFOLLOW` step at a time; a link in the chain raises AttachError."""
    parts = _folder_parts(root, path)
    fd = _open_folder(root, parts)
    if fd is None:
        return []
    try:
        return sorted(os.listdir(fd))
    except OSError as exc:
        raise WriteFailed("%s cannot be listed: %s" % (_short(parts), exc.strerror), exc.strerror) from None
    finally:
        os.close(fd)


def remove_file(root, path) -> bool:
    """Delete the file (or link) at `path` without following a link anywhere in its chain. True when it
    was deleted, False when it was not there. A folder at `path` is refused (use `remove_tree`)."""
    parts = _folder_parts(root, path)
    fd = _open_folder(root, parts[:-1])
    if fd is None:
        return False
    try:
        try:
            os.unlink(parts[-1], dir_fd=fd)
        except FileNotFoundError:
            return False
        except IsADirectoryError:
            raise AttachError("%s is a folder, not a file" % parts[-1]) from None
        except OSError as exc:
            raise WriteFailed("%s cannot be removed: %s" % (parts[-1], exc.strerror), exc.strerror) from None
        return True
    finally:
        os.close(fd)


def _remove_at(root, path, *, folder_only):
    """The one delete of a tree. The parent is opened from the real root through `O_NOFOLLOW` steps
    (`_open_folder`: a link in the chain raises AttachError) and the leaf is acted on through that
    descriptor, so a folder swapped for a link after the walk is not followed. A real folder goes through
    `shutil.rmtree(name, dir_fd=)`, which walks by descriptor and removes a link inside the tree as a link.
    A link or a file at the leaf is unlinked when `folder_only` is False and left alone when it is True.
    Returns "missing" (nothing was there, or a folder above is missing), "kept" (a link or a file left
    alone), "gone" (nothing is left at `path`) or "stayed" (some of it stayed: a read-only folder)."""
    parts = _folder_parts(root, path)
    fd = _open_folder(root, parts[:-1])
    if fd is None:
        return "missing"
    name = parts[-1]
    try:
        try:
            st = os.lstat(name, dir_fd=fd)
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            raise WriteFailed("%s cannot be read: %s" % (name, exc.strerror), exc.strerror) from None
        if not stat.S_ISDIR(st.st_mode):
            if folder_only:
                return "kept"
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise WriteFailed("%s cannot be removed: %s" % (name, exc.strerror), exc.strerror) from None
            return "gone"
        shutil.rmtree(name, ignore_errors=True, dir_fd=fd)
        try:
            os.lstat(name, dir_fd=fd)
        except FileNotFoundError:
            return "gone"
        except OSError:
            return "stayed"
        return "stayed"
    finally:
        os.close(fd)


def remove_tree(root, path) -> bool:
    """Delete the real folder at `path` with everything in it and return True when it is gone. Return False
    when nothing is there (or a folder above is missing), when what is there is not a real folder (a link or
    a file is left alone: a swapped link goes through `drop_link`), or when some of it stayed. A link in the
    chain raises AttachError and deletes nothing; a link inside the tree is removed as a link, never
    followed. For the folders attach made and now takes back (a work folder of a failed cut, its admin
    folder in the clone's .git, a baseline run folder). t-648 fix round 2 (U1) set this contract; the
    descriptor walk under it is t-652 round 2 (R2-07), joined at the op-062 integration of t-652."""
    return _remove_at(root, path, folder_only=True) == "gone"


def clear_path(root, path) -> bool:
    """Make sure nothing is left at `path`, never following a link: a real folder is deleted with its
    content as in `remove_tree`, a link or a file at `path` is unlinked (the link itself, never its
    target). True when nothing is left at `path` (a missing path is True), False when some of it stayed
    (a read-only folder). A link in the chain raises AttachError. For the study folder, whose entries an
    agent can replace with a link or a file and which a prune or a fresh snapshot must clear (t-652 round
    2, R2-07; named apart from `remove_tree` at the op-062 integration of t-652)."""
    return _remove_at(root, path, folder_only=False) in ("gone", "missing")


# ------------------------------------------------------------------------- the descriptor walk

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def masked(mode) -> int:
    """`mode` less the process umask: the mode an `open(path, "wb")` or a `mkdir` would have given."""
    return mode & ~_umask()


def _one_name(name) -> str:
    if (not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\0" in name
            or os.sep in name):
        raise AttachError("%r is not one file name" % (name,))
    return name


def _classify(fd, name):
    """The refusal for a part that `open(O_DIRECTORY|O_NOFOLLOW)` could not enter: a link or a non-folder."""
    try:
        st = os.lstat(name, dir_fd=fd)
    except OSError:
        return AttachError(NOT_FOLDER % name)
    if stat.S_ISLNK(st.st_mode):
        return AttachError(LINK % name)
    return AttachError(NOT_FOLDER % name)


def _enter(fd, name, shown, *, create):
    """A descriptor for the folder `name` of `fd`, opened with `O_NOFOLLOW`. `create` makes it (one mkdir
    relative to `fd`) when it is missing. A link raises `LINK % name`, a non-folder `NOT_FOLDER % name`."""
    for attempt in (0, 1):
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            if not create or attempt:
                raise AttachError("%s does not exist" % _short(shown)) from None
            try:
                os.mkdir(name, 0o777, dir_fd=fd)
            except FileExistsError:
                pass                                # made by someone else meanwhile: opened (and checked) next
            except OSError as exc:
                raise WriteFailed("%s cannot be created: %s" % (_short(shown), exc.strerror), exc.strerror) from None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _classify(fd, name) from None
            raise WriteFailed("%s cannot be opened: %s" % (_short(shown), exc.strerror), exc.strerror) from None
    raise AttachError("%s does not exist" % _short(shown))      # not reached


class Folder:
    """A folder held by descriptor. Every name below it is made, written, moved and removed relative to
    that descriptor, so no path is resolved twice and a link swapped in after a check changes nothing.

    `Folder.at(root, path)` walks from the real studio root one part at a time with `O_NOFOLLOW` and
    refuses a link or a non-folder at any part (`LINK` / `NOT_FOLDER`). The method names avoid the names
    the write guard (alpaca/tests/test_attach_write_guard.py) matches, so a caller outside this file
    reads as a `Folder` call and not as a path write.

    Limits: a folder above this one moved out of the studio while the descriptor is held takes the
    writes with it (the same boundary the module docstring states); git child processes write where git
    decides."""

    def __init__(self, fd, label):
        self._fd, self.label = fd, label

    # -- reaching a folder
    @classmethod
    def at(cls, root, path, *, create=True):
        root_real, parts = _resolve(root, path)
        try:
            cur = os.open(root_real, _DIR_FLAGS)
        except OSError as exc:
            raise WriteFailed("the studio folder cannot be opened: %s" % exc.strerror, exc.strerror) from None
        try:
            for i, name in enumerate(parts):
                nxt = _enter(cur, name, parts[:i + 1], create=create)
                os.close(cur)
                cur = nxt
        except BaseException:
            os.close(cur)
            raise
        return cls(cur, parts[-1])

    def sub(self, name, *, create=False, exclusive=False):
        """The folder `name` below this one. `exclusive` makes it and raises `FileExistsError` unchanged when
        anything is there (a claim, like `make_dir(exclusive=True)`)."""
        name = _one_name(name)
        if exclusive:
            try:
                os.mkdir(name, 0o777, dir_fd=self._fd)
            except FileExistsError:
                raise
            except OSError as exc:
                raise WriteFailed("%s cannot be created: %s" % (name, exc.strerror), exc.strerror) from None
            create = False
        return Folder(_enter(self._fd, name, [self.label, name], create=create), name)

    def fresh_sub(self, prefix):
        """(name, Folder) of a new folder `prefix` + 16 random hex digits."""
        prefix = _one_name(prefix)
        for _ in range(8):
            name = prefix + secrets.token_hex(8)
            try:
                os.mkdir(name, 0o777, dir_fd=self._fd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise WriteFailed("%s cannot be created: %s" % (name, exc.strerror), exc.strerror) from None
            return name, Folder(_enter(self._fd, name, [self.label, name], create=False), name)
        raise AttachError("no free name for a new folder in %s" % self.label)

    # -- reading what is there
    def kind(self, name):
        """'dir', 'link', 'file', 'other' or None (nothing there), by `lstat` relative to this folder."""
        name = _one_name(name)
        try:
            st = os.lstat(name, dir_fd=self._fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WriteFailed("%s cannot be read: %s" % (name, exc.strerror), exc.strerror) from None
        if stat.S_ISDIR(st.st_mode):
            return "dir"
        if stat.S_ISLNK(st.st_mode):
            return "link"
        return "file" if stat.S_ISREG(st.st_mode) else "other"

    def has(self, name):
        return self.kind(name) is not None

    def names(self):
        try:
            return sorted(os.listdir(self._fd))
        except OSError as exc:
            raise WriteFailed("%s cannot be listed: %s" % (self.label, exc.strerror), exc.strerror) from None

    # -- writing
    def put(self, name, data, *, mode=None):
        """Write `data` as a regular file `name`. The bytes go to a random `.tmp-` name made with `O_EXCL` and
        `O_NOFOLLOW`, then replace `name` by descriptor: a link or a hard link already at `name` is replaced,
        never written through. `mode` is the exact file mode (default `0o666` less the umask)."""
        name, data = _one_name(name), bytes(data)
        mode = masked(0o666) if mode is None else mode
        tmp = ".tmp-" + secrets.token_hex(8)
        fd = None
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                         dir_fd=self._fd)
            os.fchmod(fd, mode)
            _write_all(fd, data)
            os.close(fd)
            fd = None
            os.replace(tmp, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            self._unlink_quiet(tmp)
            raise WriteFailed("%s cannot be written: %s" % (name, exc.strerror), exc.strerror) from None
        except BaseException:
            if fd is not None:
                os.close(fd)
            self._unlink_quiet(tmp)
            raise

    def put_stream(self, name, fh, *, mode=None, want_sha=None, limit=None):
        """Write what `fh` (a binary file object, read in blocks) holds as `name`, as `put` does, and return the
        sha256 hex digest of the bytes written. With `want_sha` the file is put in place only when the digest
        is that value, else nothing is left and the result is None; with `limit` it stops and returns None
        once more than `limit` bytes were read. Memory is one block, whatever the size."""
        name = _one_name(name)
        mode = masked(0o666) if mode is None else mode
        tmp = ".tmp-" + secrets.token_hex(8)
        fd = None
        done = False
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                         dir_fd=self._fd)
            os.fchmod(fd, mode)
            h, seen = hashlib.sha256(), 0
            while True:
                block = fh.read(1 << 20)
                if not block:
                    break
                seen += len(block)
                if limit is not None and seen > limit:
                    return None
                h.update(block)
                _write_all(fd, block)
            digest = h.hexdigest()
            if want_sha is not None and digest != want_sha:
                return None
            os.close(fd)
            fd = None
            os.replace(tmp, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            done = True
            return digest
        except OSError as exc:
            raise WriteFailed("%s cannot be written: %s" % (name, exc.strerror), exc.strerror) from None
        finally:
            if fd is not None:
                os.close(fd)
            if not done:
                self._unlink_quiet(tmp)

    def put_new(self, name, data, *, mode=None):
        """Write `data` as `name`, which must not exist (`O_EXCL|O_NOFOLLOW` at the name itself)."""
        name, data = _one_name(name), bytes(data)
        mode = masked(0o666) if mode is None else mode
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                         dir_fd=self._fd)
        except FileExistsError:
            raise AttachError("%s already exists" % name) from None
        except OSError as exc:
            raise WriteFailed("%s cannot be written: %s" % (name, exc.strerror), exc.strerror) from None
        try:
            try:
                os.fchmod(fd, mode)
                _write_all(fd, data)
            finally:
                os.close(fd)
        except BaseException as exc:
            self._unlink_quiet(name)
            if isinstance(exc, OSError):
                raise WriteFailed("%s cannot be written: %s" % (name, exc.strerror), exc.strerror) from None
            raise

    def add_link(self, name, target):
        name = _one_name(name)
        try:
            os.symlink(target, name, dir_fd=self._fd)
        except FileExistsError:
            raise AttachError("%s already exists" % name) from None
        except OSError as exc:
            raise WriteFailed("%s cannot be linked: %s" % (name, exc.strerror), exc.strerror) from None

    def move_entry(self, old, new):
        """Rename `old` to `new` inside this folder, by descriptor."""
        old, new = _one_name(old), _one_name(new)
        try:
            os.rename(old, new, src_dir_fd=self._fd, dst_dir_fd=self._fd)
        except OSError as exc:
            raise WriteFailed("%s cannot be moved to %s: %s" % (old, new, exc.strerror), exc.strerror) from None

    # -- removing
    def _unlink_quiet(self, name):
        try:
            os.unlink(name, dir_fd=self._fd)
        except OSError:
            pass

    def drop(self, name):
        """Remove `name`: a folder with everything below it (never following a link), anything else by one
        unlink. A link is unlinked and its target is left alone. Returns False when nothing was there."""
        kind = self.kind(name)
        if kind is None:
            return False
        try:
            if kind == "dir":
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise AttachError("%s cannot be removed safely on this platform" % name)
                shutil.rmtree(name, dir_fd=self._fd)
            else:
                os.unlink(name, dir_fd=self._fd)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise WriteFailed("%s cannot be removed: %s" % (name, exc.strerror), exc.strerror) from None
        return True

    def drop_prefixed(self, prefix):
        """Remove every name that starts with `prefix`. Returns how many went."""
        prefix = _one_name(prefix)
        return sum(1 for n in self.names() if n.startswith(prefix) and self.drop(n))

    # -- the descriptor
    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
