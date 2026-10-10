"""op-008 t-071 F-2: no item of the pinned evaluation question set ships.

The question set (the case file of the development-only evaluation tooling under .alpaca/) is
development state: it never ships, so a release candidate has no copy to compare against and this
test skips there. Where it is
present, every shipped file (the [mechanism] class of ALPACA-MANIFEST, the files git tracks there,
outside .alpaca/) is searched for each item's question text, each planted canary (`forbidden`),
each synthetic fixture quote and each synthetic candidate name. A test that needs the behaviour of
an item uses synthetic text of the same shape instead. ALPACA_QUESTION_SET_CASES names another copy
of the file (a worktree has no .alpaca/ of its own). A hit is reported by case id, item kind and
file only, never the text.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest

from alpaca import manifest
from alpaca.tests.conftest import REPO

#: Built from parts so this file does not name the tooling's path or its case file (the evaluation
#: isolation check, test_rc5_conformance.py, greps the shipped tree for both).
CASES = Path(os.environ.get("ALPACA_QUESTION_SET_CASES")
             or os.path.join(REPO, ".alpaca", "rune2" + "-development", "eval", "cases" + ".jsonl"))


def _needles(cases):
    """(case id, kind, text) for every item text a shipped file must not hold."""
    out = []
    for case in cases:
        cid = case.get("id", "?")
        question = (case.get("question") or "").strip().rstrip("?.! ")
        if question:
            out.append((cid, "question", question))
        out += [(cid, "canary", value) for value in case.get("forbidden") or [] if value]
        out += [(cid, "fixture-quote", gold["quote"]) for gold in case.get("gold") or []
                if gold.get("kind") == "fixture" and gold.get("quote")]
        if case.get("slice") == "synthetic":
            out += [(cid, "candidate", name) for name in case.get("candidates") or [] if name]
    return out


def _shipped_files():
    """Tracked files under the [mechanism] entries, minus the memory class and .alpaca/."""
    listed = subprocess.run(["git", "-C", REPO, "ls-files", "-z"], capture_output=True, check=True)
    tracked = [rel for rel in listed.stdout.decode("utf-8").split("\0") if rel]
    entries = [e.rstrip("/") for e in manifest.mechanism(REPO)]
    memory = [m.rstrip("/") for m in manifest.memory(REPO)]

    def under(rel, items):
        return any(rel == item or rel.startswith(item + "/") for item in items)

    return [rel for rel in tracked if under(rel, entries) and not under(rel, memory)
            and not rel.startswith(".alpaca/")]


def test_no_shipped_file_holds_a_question_set_item():
    if not CASES.is_file():
        pytest.skip("the question set is development state and is absent here (%s)" % CASES.name)
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    needles = _needles(cases)
    assert cases and needles, "the question set holds no items"
    hits = []
    for rel in _shipped_files():
        path = Path(REPO, rel)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits += ["%s %s in %s" % (cid, kind, rel) for cid, kind, needle in needles if needle in text]
    assert hits == [], "question-set items in shipped files (use synthetic text of the same shape): " + "; ".join(hits)
