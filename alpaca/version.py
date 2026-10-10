"""Which Alpaca this is: the release number, and whether the tree is a development or a release build.

`alpaca/__init__.py` VERSION is the number of the next (or current) release, bumped by hand when a
release is cut. Every tree shows that number alone, "Alpaca 1.2.0". `alpaca/_channel.py` says which
kind of tree it is: the development tree carries CHANNEL = "dev", and `alpaca release build` overlays
templates/release/channel.py (CHANNEL = "release") onto it. The channel, and for a development
checkout its commit and whether the harness code (alpaca/, bin/) has uncommitted changes, travel
beside the number for the hub's tooltip and `alpaca status --json`; `build` joins them into one
SemVer string, 1.2.0-dev+g0123abcd4567.dirty, for logs and bug reports.
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
    """The version facts: version (the bare number), base, channel, build, and for a development
    tree commit and dirty."""
    if CHANNEL == "release":
        return {"version": VERSION, "base": VERSION, "channel": "release", "build": VERSION}
    out = {"version": VERSION, "base": VERSION, "channel": "dev", "commit": None, "dirty": None,
           "build": VERSION + "-dev"}
    try:
        head = _git(root, "rev-parse", "--short=12", "HEAD")
        if head.returncode != 0 or not head.stdout.strip():
            return out
        out["commit"] = head.stdout.strip()
        changed = _git(root, "status", "--porcelain", "--untracked-files=no", "--", "alpaca", "bin")
        out["dirty"] = changed.returncode == 0 and bool(changed.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return out
    out["build"] += "+g" + out["commit"] + (".dirty" if out["dirty"] else "")
    return out


def string(root=PACKAGE_ROOT):
    return info(root)["version"]
