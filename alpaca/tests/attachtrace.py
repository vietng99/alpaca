"""Test support for t-650 fix round 4: a caller-agnostic tracer of path writes.

The write guard (test_attach_write_guard.py) reads the source of `alpaca/attach/` and proves no second owner of
writes exists there. It does not see the studio's own writers in `alpaca/proof.py` and `alpaca/research/`,
which an attach seal and an attach verification must not use for the attached repo's bytes. The tracer wraps
every Python call that creates, replaces, removes or opens for writing a path by name, records the ones that
land in a watched place and name the code that made them, and ignores any call made relative to a descriptor
(`dir_fd`), which is how `safefs.Folder` writes.

    tracer = Tracer(WATCH)
    tracer.install(monkeypatch)
    ... run the seal or the verification ...
    assert not tracer.hits

A call is attributed to the innermost frame in the product code (a file under `alpaca/`, not under
`alpaca/tests/`), so a standard library helper that does the write for a product function is attributed to the
product function. The same tracer, run on a plain project, must record writes (a tracer that sees nothing
proves nothing): the tests say so.
"""
import builtins
import os
import re
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRODUCT = os.path.join(REPO, "alpaca") + os.sep
TESTS = os.path.join(REPO, "alpaca", "tests") + os.sep

#: where product bytes of an attach seal and an attach verification must never be written by name
WATCH = (
    re.compile(r"/\.alpaca/proofs/[^/]+/[^/]+\.evidence(/|$)"),
    re.compile(r"/\.alpaca/proofs/[^/]+/\.[^/]+\.evidence\."),
    re.compile(r"\.md\.seal\.json$"),
    re.compile(r"/\.alpaca/verify/[^/]+/[^/]+/app\.patch$"),
    re.compile(r"/\.alpaca/verify/.*/(tree|base)/app(/|$)"),
    re.compile(r"/\.alpaca/verify/.*/packet/evidence(/|$)"),
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_DIR_FD = ("dir_fd", "src_dir_fd", "dst_dir_fd")


#: which positional arguments of a call name a path it changes (the source of a copy is only read)
_CHANGED = {"open": (0,), "os.open": (0,), "os.makedirs": (0,), "os.mkdir": (0,), "os.replace": (0, 1),
            "os.rename": (0, 1), "os.symlink": (1,), "os.link": (1,), "os.remove": (0,), "os.unlink": (0,),
            "os.rmdir": (0,), "shutil.copyfile": (1,), "shutil.copy": (1,), "shutil.copy2": (1,),
            "tempfile.mkstemp": ()}


def _paths(args, kwargs, label):
    """The paths a call changes: the positional arguments `_CHANGED` names, and `dst`, `dir` or `path` given by name."""
    out = []
    values = [args[i] for i in _CHANGED[label] if i < len(args)]
    values += [kwargs[k] for k in ("path", "dst", "dir", "file", "name") if k in kwargs]
    for value in values:
        if isinstance(value, (str, bytes, os.PathLike)):
            try:
                out.append(os.fsdecode(os.fspath(value)))
            except (TypeError, ValueError):
                pass
    return out


class Tracer:
    def __init__(self, watch=WATCH):
        self.watch, self.hits, self.calls = watch, [], 0

    # -- what a call is
    def _writes(self, label, args, kwargs):
        if label == "open":
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
            return isinstance(mode, str) and any(c in mode for c in "wax+")
        if label == "os.open":
            flags = args[1] if len(args) > 1 else kwargs.get("flags", 0)
            return bool(flags & _WRITE_FLAGS)
        return True

    def _caller(self):
        frame = sys._getframe(3)
        while frame is not None:
            name = frame.f_code.co_filename
            if name.startswith(PRODUCT) and not name.startswith(TESTS):
                return os.path.relpath(name, REPO)
            frame = frame.f_back
        return "(outside alpaca/)"

    def _check(self, label, args, kwargs):
        if any(k in kwargs for k in _DIR_FD):
            return
        self.calls += 1
        if not self._writes(label, args, kwargs):
            return
        for text in _paths(args, kwargs, label):
            full = os.path.abspath(text)
            if any(w.search(full) for w in self.watch):
                self.hits.append((label, full, self._caller()))
                return

    # -- installing
    def install(self, mp):
        for mod, name, label in (
                (builtins, "open", "open"), (os, "open", "os.open"), (os, "makedirs", "os.makedirs"),
                (os, "mkdir", "os.mkdir"), (os, "replace", "os.replace"), (os, "rename", "os.rename"),
                (os, "symlink", "os.symlink"), (os, "link", "os.link"), (os, "remove", "os.remove"),
                (os, "unlink", "os.unlink"), (os, "rmdir", "os.rmdir"), (shutil, "copyfile", "shutil.copyfile"),
                (shutil, "copy", "shutil.copy"), (shutil, "copy2", "shutil.copy2"),
                (tempfile, "mkstemp", "tempfile.mkstemp")):
            mp.setattr(mod, name, self._wrap(getattr(mod, name), label))
        try:                                     # the verifier keeps its own alias of the builtin open
            from alpaca.research import verifier
            mp.setattr(verifier, "_fopen", builtins.open)
        except ImportError:
            pass

    def _wrap(self, orig, label):
        tracer = self

        def traced(*args, **kwargs):
            tracer._check(label, args, kwargs)
            return orig(*args, **kwargs)
        traced.__name__ = getattr(orig, "__name__", label)
        return traced

    # -- reading the result
    def from_product(self, relfile):
        return [h for h in self.hits if h[2] == relfile]

    def describe(self):
        return ["%s %s (from %s)" % h for h in self.hits]
