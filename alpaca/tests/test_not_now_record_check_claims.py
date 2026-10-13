"""(class D1): record_check (gates/record_check.py) reads RESUME.md, CHECKLIST.md and
data.json and turns '<word>: <open|blocked|failed|waived|discharged>' into a status claim on a row
that does not exist, which fails as RECORD-UNBACKED-CLAIM. Reason and trigger are agent free text
and often start with exactly those words. A label format like 'reason: <text>' or
'Comes back when: <text>' makes a gate failure out of a sentence; the Not now lines must not form
a claim (quote the text, or word the label so the status word does not follow a colon)."""
import json
import os

import pytest

from alpaca.tests.not_now_kit import defer, entries, setup
from alpaca import export, pad
from alpaca.gates import record_check

TEXTS = [("t-001", "open question with the vendor", "blocked until legal signs"),
         ("t-002", "failed review twice", "waived by owner or date:2099-01-01"),
         ("t-003", "discharged elsewhere, keep for later", "open: after:t-004")]
NEEDLES = ["open question", "blocked until", "failed review", "waived by", "discharged elsewhere",
           "after:t-004"]


@pytest.fixture
def written(project):
    setup(project, 4)
    for tid, reason, when in TEXTS:
        defer(tid, reason, when)
    pad.write(project)
    export.write(project)
    return project


@pytest.mark.parametrize("name", ["RESUME.md", "CHECKLIST.md", "data.json"])
def test_not_now_lines_form_no_status_claim(written, name):
    text = open(os.path.join(written, name), encoding="utf-8").read()
    bad = [c for c in record_check.extract_claims(text)
           if any(n in c.raw for n in NEEDLES) or any(t in c.raw for t, _, _ in TEXTS)]
    assert bad == [], bad


def test_record_check_verdict_unchanged_by_deferrals(project):
    from alpaca.gates import verdict as vc
    setup(project, 4)
    pad.write(project); export.write(project)
    before = record_check.check(project)
    for tid, reason, when in TEXTS:
        defer(tid, reason, when)
    pad.write(project); export.write(project)
    assert record_check.check(project) == before
