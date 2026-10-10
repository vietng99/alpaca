"""One helper for shipped tests that need a development-only file (op-008 t-072b).

A release candidate ships alpaca/tests (ALPACA-MANIFEST [mechanism]) but not the development tree
around it: tools/, packs/, design/, te/ and most of docs/ stay behind (ALPACA-MANIFEST and the
forbidden rules of alpaca/release/build.py), and neither a project made by alpaca new nor an
upgraded one carries them. A shipped test that reads such a file names it here:

    pytestmark = needs_dev_files("tools/k_replay.py")       # a mark for a module, class or test
    require_dev_files("packs/refpack")                      # a call: at module level, in a test,
                                                            # a fixture or a helper it calls
    names_as_data("packs/refpack/")                         # named as data, never read from here

Where the file is absent the test skips with a stated reason. The one exception is the development
tree itself: a file its git index tracks but its work tree lacks is a lost development file, and the
test fails instead, so a skip never hides it. Naming a path that ships is an error: a shipped file
that is absent is a broken candidate, not a reason to skip.
alpaca/tests/test_dev_files_guarded.py fails when a shipped test names a development-only path
without this helper.
"""
import os
from pathlib import PurePosixPath
import subprocess

import pytest

from alpaca import manifest
from alpaca.release.build import forbidden, is_memory

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Written into the candidate by the build itself (project.yaml release.generated_manifest runs
#: setup/gen_manifest.py --write, whose LOCK_NAME this is), so it ships without a manifest entry.
GENERATED = ("MANIFEST.json",)


def _norm(rel):
    text = str(rel).replace(os.sep, "/")
    if not text.strip("/") or os.path.isabs(text) or ".." in text.split("/"):
        raise ValueError("a development file is named by its path relative to the tree: %r" % rel)
    return PurePosixPath(text.strip("/")).as_posix()


def ships(rel, root=None):
    """True when rel (a file or a folder) is in the release ship set of the tree at root:
    covered by an ALPACA-MANIFEST [mechanism] entry and not dropped by a build.py forbidden rule
    or the [memory] class. A folder ships when its own entry, or an entry inside it, does."""
    rel = _norm(rel)
    if rel in GENERATED:
        return True
    classes = manifest.classes(root or REPO)
    if forbidden(rel) or is_memory(rel, [e.strip("/") for e in classes["memory"]]):
        return False
    for entry in classes["mechanism"]:
        entry = entry.strip("/")
        if rel == entry or rel.startswith(entry + "/") or entry.startswith(rel + "/"):
            return True
    return False


def missing(*rels, root=None):
    """The named development-only paths absent from the tree at root. Raises ValueError for a
    path that ships with the release (see the module docstring)."""
    if not rels:
        raise ValueError("name at least one development file")
    root, out = root or REPO, []
    for rel in map(_norm, rels):
        if ships(rel, root):
            raise ValueError("%s ships with the release (ALPACA-MANIFEST); a shipped file is never "
                             "a reason to skip" % rel)
        if not os.path.exists(os.path.join(root, rel)):
            out.append(rel)
    return out


def lost(absent, root=None):
    """The absent paths the git index of root still tracks: development files the work tree lost."""
    if not absent:
        return []
    try:
        done = subprocess.run(["git", "-C", root or REPO, "ls-files", "-z", "--", *absent],
                              capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    tracked = [p for p in done.stdout.decode("utf-8", "replace").split("\0") if p]
    return [rel for rel in absent if any(p == rel or p.startswith(rel + "/") for p in tracked)]


def _reason(absent):
    return ("needs development-only file(s) %s, which this tree does not carry (a release, or a "
            "project made from one, ships no such file: ALPACA-MANIFEST); runs in the development "
            "tree" % ", ".join(absent))


def _lost_reason(gone):
    return "the development tree tracks %s but lost it from the work tree" % ", ".join(gone)


def needs_dev_files(*rels):
    """A pytest mark: decorate a test or a class, or set pytestmark = needs_dev_files(...)."""
    absent = missing(*rels)
    gone = lost(absent)
    if gone:
        return pytest.mark.dev_files_lost(*gone, reason=_lost_reason(gone))
    if absent:
        return pytest.mark.skip(reason=_reason(absent))
    return pytest.mark.dev_files(*rels)


def require_dev_files(*rels):
    """The same check as a call: at module level before code that reads the file (the whole module
    skips), or inside a test, a fixture or a helper a test calls (that test skips)."""
    absent = missing(*rels)
    gone = lost(absent)
    if gone:
        pytest.fail(_lost_reason(gone), pytrace=False)
    if absent:
        pytest.skip(_reason(absent), allow_module_level=True)


def names_as_data(*rels):
    """Declare development-only paths a module names only as data: a path a test expects to be
    absent, a file of a scratch tree the test builds, a value a constant must hold. The module never
    reads them from this tree, so nothing skips; the guard reads the declaration. A declaration in a
    function covers that function only; one at module level covers the module."""
    for rel in rels:
        _norm(rel)


def pytest_runtest_setup(item):
    """conftest hook: a needs_dev_files test whose development file the tree lost fails."""
    mark = item.get_closest_marker("dev_files_lost")
    if mark is not None:
        pytest.fail(mark.kwargs["reason"], pytrace=False)
