"""Which Alpaca this is: the release number, and whether the tree is a development or a release build.

`alpaca/__init__.py` VERSION is the number of the next (or current) release, bumped by hand when a
release is cut. `alpaca/_channel.py` says which kind of tree this is: the development tree carries
CHANNEL = "dev", and `alpaca release build` overlays templates/release/channel.py (CHANNEL =
"release") onto it, so every public tree reports the bare number and every development checkout
reports a pre-release of it that names its commit:

    release tree        1.0.0
    development tree    1.0.0-dev+g0123abcd4567            (clean alpaca/ and bin/)
                        1.0.0-dev+g0123abcd4567.dirty      (uncommitted changes there)
                        1.0.0-dev                          (no git, or not a checkout)

The dev form is a SemVer pre-release with build metadata, so it sorts before the release it leads
to. Only the harness code (alpaca/, bin/) decides dirtiness: the record under .alpaca/ changes on
every command and says nothing about which code is running.
"""
import os
import subprocess

from alpaca import VERSION
from alpaca._channel import CHANNEL

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5,
                          env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))


def info(root=PACKAGE_ROOT):
    """The version facts as a dict: version, base, channel, commit (dev only) and dirty (dev only)."""
    if CHANNEL == "release":
        return {"version": VERSION, "base": VERSION, "channel": "release"}
    out = {"version": VERSION + "-dev", "base": VERSION, "channel": "dev", "commit": None, "dirty": None}
    try:
        head = _git(root, "rev-parse", "--short=12", "HEAD")
        if head.returncode != 0 or not head.stdout.strip():
            return out
        out["commit"] = head.stdout.strip()
        changed = _git(root, "status", "--porcelain", "--untracked-files=no", "--", "alpaca", "bin")
        out["dirty"] = changed.returncode == 0 and bool(changed.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return out
    out["version"] += "+g" + out["commit"] + (".dirty" if out["dirty"] else "")
    return out


def string(root=PACKAGE_ROOT):
    return info(root)["version"]
