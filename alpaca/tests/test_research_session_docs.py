"""The documents the session runtime changes (in-session-runtime.md sections 14 and 15): the
superseded notes at the top of three design documents, the three prompt lines, the sentence of the
attempt proof, the operator and manual text.
"""
from __future__ import annotations

import os
import re

import pytest

from alpaca.tests.conftest import REPO
from alpaca.tests.devfiles import needs_dev_files, require_dev_files

DESIGN = os.path.join(REPO, "design", "research-engine")
DOCS = ("architecture", "verifier", "operator-surface")
TAIL = "agents run as subagents of the driving session, never headless."
HEAD = "Superseded in part by design/research-engine/in-session-runtime.md: sections "


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


def _section15_rows(doc):
    require_dev_files("design/research-engine/in-session-runtime.md")
    text = _read("design", "research-engine", "in-session-runtime.md")
    block = text.split("## 15. Superseded design sections", 1)[1].split("## 16.", 1)[0]
    rows = []
    for line in block.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")] if line.startswith("|") else []
        if len(cells) >= 3 and cells[0] == "`design/research-engine/%s.md`" % doc:
            rows.append(cells[1])
    return rows


def _expand(note):
    """The section numbers a note names, with each `A to B` range expanded over its last part."""
    names = set(re.findall(r"\d+(?:\.\d+)*", note))
    for m in re.finditer(r"(\d+(?:\.\d+)*) to (\d+(?:\.\d+)*)", note):
        a, b = m.group(1).split("."), m.group(2).split(".")
        if a[:-1] == b[:-1] and a[-1].isdigit() and b[-1].isdigit():
            for n in range(int(a[-1]), int(b[-1]) + 1):
                names.add(".".join(a[:-1] + [str(n)]))
    return names


def _numbers_of(cell):
    """The section numbers a cell of the section 15 table names: the number that opens each item
    of the cell ("3.6 launcher.py ...", "6. The headless ..., 6.1 to 6.3") and the dotted numbers."""
    out = set(re.findall(r"(?<![\w.])(\d+\.\d+(?:\.\d+)*)(?![\w])", cell))
    first = re.match(r"(\d+)\.?\s", cell)
    if first:
        out.add(first.group(1))
    return out


@needs_dev_files("design/research-engine")
@pytest.mark.parametrize("doc", DOCS)
def test_the_superseded_note_sits_under_the_title_and_names_every_section(doc):
    lines = _read("design", "research-engine", "%s.md" % doc).split("\n")
    assert lines[0].startswith("# Research engine") and lines[1] == ""
    note = lines[2]
    assert note.startswith(HEAD) and note.endswith(TAIL) and lines[3] == "", note[:80]
    named = _expand(note)
    rows = _section15_rows(doc)
    assert rows, doc
    for cell in rows:
        for number in _numbers_of(cell):
            assert number in named, (doc, number, cell[:60])
    # the section 12.2 to 12.4 note says the python blocks were rewritten
    if doc == "architecture":
        assert "first python block of 12.2, 12.3 and 12.4 was rewritten" in note


def test_the_note_check_fails_on_a_missing_section():
    note = HEAD + "sections 3.6 and 4.1; " + TAIL
    assert "4.4" not in _expand(note) and "3.6" in _expand(note)
    assert {"4.3.1", "4.3.2", "4.3.5"} <= _expand("sections 4.3.1 to 4.3.5")


def test_the_three_prompt_lines():
    impl = _read("alpaca", "research", "prompts", "implementer.md")
    assert "leave the change in the working tree" in impl and "you may commit" not in impl
    audit = _read("alpaca", "research", "prompts", "audit.md")
    assert "its worktree is not yours to change" in audit and "not in your jail" not in audit
    prop = _read("alpaca", "research", "prompts", "proposer.md")
    assert "a checkout of the current champion that you must not change" in prop
    assert "read-only checkout" not in prop


def test_the_attempt_proof_says_what_is_true_now():
    text = _read("alpaca", "research", "proofs.py")
    assert "headless launcher in a jail" not in text
    assert "subagent of the driving session" in text and "agent id" in text


def test_operators_and_the_manual_describe_the_session_runtime():
    ops = _read("docs", "operators.md")
    assert "## Research runs in the session" in ops and "research next-job" in ops
    assert "SubagentStart" in ops and "research guard" in ops and "nine events" in ops
    man = _read("MANUAL.md")
    for needle in ("`next-job <run>", "`submit <job>", "alpaca.hooks.research_guard",
                   "alpaca.hooks.subagent_start", "research-job-result", "research-driver-audit"):
        assert needle in man, needle
    html = _read("docs", "manual.html")
    assert "next-job" in html and "research_guard" in html
