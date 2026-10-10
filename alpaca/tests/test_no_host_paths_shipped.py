"""op-008 t-071 F-4: no shipped file holds a path of this host.

The release ships the source commit's allowlisted files (alpaca/release/build.py selected, overlays
applied). None of them may hold the host's pooled accounts folder (alpaca/ownerword.py
ACCOUNTS_ROOT) or the home folder of whoever runs the tests; packed fixtures (`.gz.b64`) are read
unpacked. Test fixtures write such paths as placeholders (`@HOME@`, `@ACCOUNTS@`) or derive them.
Two product modules name the accounts layout on purpose and are the only exceptions. A hit is
reported by file and kind, never the path.
"""
import base64
import binascii
import gzip
import os
import subprocess
import zlib

import pytest

from alpaca import ownerword
from alpaca.tests.conftest import REPO

#: product code that reads the pooled accounts layout (alpaca/ownerword.py, alpaca/analytics/burn.py)
LAYOUT_READERS = {"alpaca/ownerword.py", "alpaca/analytics/burn.py"}


def _shipped():
    from alpaca.release import build, config
    try:
        cfg = config.load_config(REPO)
        return build.selected(REPO, cfg)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        pytest.skip("no release source here (%s)" % type(exc).__name__)


def _needles():
    out = [("accounts-root", ownerword.ACCOUNTS_ROOT.rstrip("/") + "/")]
    home = os.path.realpath(os.path.expanduser("~")).rstrip("/")
    if home.count("/") >= 2:                       # a real home folder, not "/" or "/root"
        out.append(("home", home + "/"))
    return out


def _content(rel, path):
    data = path.read_bytes()
    if rel.endswith(".gz.b64"):
        try:
            return gzip.decompress(base64.decodebytes(data))
        except (binascii.Error, OSError, EOFError, zlib.error):
            return data
    return data


def test_no_shipped_file_holds_a_host_path():
    needles = _needles()
    hits = []
    for rel, path in _shipped().items():
        data = _content(rel, path)
        for kind, needle in needles:
            if kind == "accounts-root" and rel in LAYOUT_READERS:
                continue
            if needle.encode("utf-8") in data:
                hits.append("%s (%s)" % (rel, kind))
    assert hits == [], "host paths in shipped files (use a placeholder or derive the path): " + ", ".join(hits)
