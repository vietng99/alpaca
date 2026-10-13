"""(done_bar[0], done_bar[2], class G2): `alpaca status` prints the Not now section with
each task's reason and trigger and marks a met structured trigger; `alpaca status --json` keeps
its declared key table (alpaca/surface.py) and its open count never takes a deferred task."""
import json
import os

from alpaca.tests.not_now_kit import (HEAD, R1, W1, blocks, defer, entries, finish, marked, run, section, setup)
from alpaca import surface


def test_status_text_lists_reason_and_trigger(project):
    setup(project, 2)
    defer("t-002")
    code, out = run("status")
    assert code == 0
    body = section(out)
    assert body is not None, "alpaca status prints no '## Not now' section"
    assert any(R1 in b and W1 in b for b in blocks(body, "t-002")), body
    assert not marked(out), "a free-text trigger printed 'trigger met'"
    disk = open(os.path.join(project, "RESUME.md"), encoding="utf-8").read()
    assert section(disk) == body, "the printed status and the RESUME.md it wrote disagree"


def test_status_text_marks_a_met_trigger(project):
    setup(project, 2)
    defer("t-002", "needs the first task", "after:t-001")
    finish(project, "t-001")
    code, out = run("status")
    assert code == 0
    hits = blocks(section(out) or [], "t-002")
    assert hits and any(marked(b) for b in hits), out


def test_status_json_keeps_its_declared_keys(project):
    setup(project, 2)
    defer("t-002")
    code, out = run("status", "--json")
    assert code == 0
    got = set(json.loads(out))
    declared = set(surface.TABLE["status"]["keys"])
    assert got - {"op_index"} <= declared, "status --json grew keys the surface table does not list: %s" % (got - declared)
    assert declared - {"op_index"} <= got


def test_status_json_open_count_leaves_deferred_out(project):
    setup(project, 3)
    defer("t-002")
    d = json.loads(run("status", "--json")[1])
    assert d["tasks_open"] == 2
