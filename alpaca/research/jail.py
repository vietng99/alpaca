"""The bubblewrap jail: the availability probe and the argv of an agent, gate or holdout jail.

Every kind runs with its own network namespace (--unshare-net): the Internet and the host's
loopback services are out of reach. An agent jail reaches its provider only through the egress
socket folder bound read-only at EGRESS_MOUNT. Everything is mounted at its own host path, and
what is not listed does not exist inside.

Design: design/research-engine/architecture.md sections 5.1, 5.2 and 12.7 (FR-019, FR-027,
FR-045). The mount order is the order of section 5.2: bwrap applies mount options in the order
given, so the tmpfs over each clone's .git/worktrees/ comes after the .git bind and before the
launch's admin folder.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from alpaca.research.types import JAIL_KINDS, JailSpec

ETC = ("resolv.conf", "hosts", "nsswitch.conf", "passwd", "group", "localtime", "ld.so.cache",
       "ssl", "ca-certificates", "pki", "alternatives")

GATE_PATH = "/usr/local/bin:/usr/bin:/bin"            # the fixed part of a gate or holdout PATH
EGRESS_MOUNT = "/run/alpaca-egress"                  # where an agent jail sees its socket folder
EGRESS_PORT = 8119                                   # jail_forward's port on the jail's own loopback

_PROBE_TIMEOUT_S = 30
_AVAILABLE: tuple | None = None                      # the cached probe result of this process


def binary() -> str | None:
    """shutil.which("bwrap")"""
    return shutil.which("bwrap")


def version() -> str | None:
    """The first line `bwrap --version` prints, or None when bwrap is missing or fails."""
    exe = binary()
    if not exe:
        return None
    try:
        done = subprocess.run([exe, "--version"], capture_output=True, text=True,
                              timeout=_PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (done.stdout or "").strip()
    if done.returncode != 0 or not out:
        return None
    return out.splitlines()[0]


def _prefix() -> list:
    """The part of every jail that does not depend on the spec (section 5.2)."""
    out = [binary() or "bwrap",
           "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
           "--unshare-cgroup-try", "--unshare-net",
           "--die-with-parent", "--new-session",
           "--ro-bind", "/usr", "/usr",
           "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
           "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/sbin", "/sbin"]
    for name in ETC:
        path = "/etc/%s" % name
        out += ["--ro-bind-try", path, path]
    out += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    return out


def _absolute(path: str, what: str) -> str:
    value = str(path)
    if not os.path.isabs(value):
        raise ValueError("jail %s must be an absolute path: %r" % (what, value))
    return value


def argv(spec: "JailSpec") -> list:
    """The bwrap prefix of section 5.2 for spec, --unshare-net for every kind, the tmpfs over each
    spec.git_hide worktrees/ folder placed after the binds and before spec.admins; the caller
    appends '--' and the command.

    Raises ValueError for a kind outside JAIL_KINDS, for spec.egress on any kind but "agent"
    (only an agent jail reaches its provider, and only through the egress proxy), and for a
    relative path."""
    if spec.kind not in JAIL_KINDS:
        raise ValueError("unknown jail kind %r" % (spec.kind,))
    if spec.egress is not None and spec.kind != "agent":
        raise ValueError("only an agent jail binds an egress socket folder, not %r" % spec.kind)
    out = _prefix()
    for path in spec.ro:
        p = _absolute(path, "ro path")
        out += ["--ro-bind", p, p]
    for path in spec.rw:
        p = _absolute(path, "rw path")
        out += ["--bind", p, p]
    for git_dir in spec.git_hide:
        p = _absolute(git_dir, "git_hide path")
        out += ["--tmpfs", os.path.join(p, "worktrees")]
    for admin, writable in spec.admins:
        p = _absolute(admin, "admin folder")
        out += ["--bind" if writable else "--ro-bind", p, p]
    for source, destination in spec.overlays:
        out += ["--ro-bind", _absolute(source, "overlay source"),
                _absolute(destination, "overlay destination")]
    if spec.egress is not None:
        out += ["--ro-bind", _absolute(spec.egress, "egress folder"), EGRESS_MOUNT]
    out += ["--chdir", _absolute(spec.cwd, "cwd")]
    return out


def available(*, refresh: bool = False) -> tuple:
    """(True, version) when `bwrap --unshare-user --unshare-pid --unshare-net --die-with-parent
    --ro-bind /usr /usr ... --ro-bind <a temporary folder> /run/alpaca-egress /usr/bin/true`
    exits 0, the shape every agent and gate jail uses; else (False, stderr). Cached per process.

    The probe is argv() of an agent spec whose egress folder is a fresh temporary folder, with
    the jail's own root as its cwd (the temporary folder lies under the host /tmp, which the
    jail's tmpfs hides), so it runs the very prefix the engine runs."""
    global _AVAILABLE
    if _AVAILABLE is not None and not refresh:
        return _AVAILABLE
    if not binary():
        _AVAILABLE = (False, "bwrap not found on PATH")
        return _AVAILABLE
    probe = tempfile.mkdtemp(prefix="alpaca-jail-probe-")
    try:
        cmd = argv(JailSpec(kind="agent", cwd="/", egress=probe)) + ["--", "/usr/bin/true"]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            _AVAILABLE = (False, str(exc))
            return _AVAILABLE
        if done.returncode == 0:
            _AVAILABLE = (True, version())
        else:
            _AVAILABLE = (False, (done.stderr or "").strip() or "bwrap exited %d" % done.returncode)
        return _AVAILABLE
    finally:
        shutil.rmtree(probe, ignore_errors=True)
