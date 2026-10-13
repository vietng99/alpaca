"""(done_bar[0], class E): RESUME.md gets a '## Not now' section after '## Open tasks'
that lists every deferred task once, with its reason and trigger, across every op, and never
lists a task that has left the deferred state. Today pad.py:313 drops a deferred task silently."""
import re

from alpaca.tests.not_now_kit import (not_now, HEAD, R1, W1, add_task, blocks, defer, entries, finish, has_id, new_op, ok,
                  resume, section, setup)
from alpaca import pad


def test_section_sits_after_open_tasks_and_lists_reason_and_trigger(project):
    setup(project, 3)
    defer("t-002", "vendor has not answered", "when the vendor answers")
    defer("t-003", "needs the parser first", "after:t-001")
    text = resume(project)
    lines = text.splitlines()
    assert HEAD in lines, "no '## Not now' section in RESUME.md"
    assert lines.index("## Open tasks") < lines.index(HEAD) < lines.index("## Recent events")
    body = section(text)
    for tid, reason, when in (("t-002", "vendor has not answered", "when the vendor answers"),
                              ("t-003", "needs the parser first", "after:t-001")):
        assert len(entries(body, tid)) == 1, (tid, body)
        assert any(reason in b and when in b for b in blocks(body, tid)), (tid, body)
    assert entries(body, "t-001") == [], "an open task is listed under Not now"
    open_body = section(text, "## Open tasks")
    assert entries(open_body, "t-001")
    assert not entries(open_body, "t-002") and not entries(open_body, "t-003")


def test_the_written_file_and_the_status_text_carry_the_section(project):
    setup(project, 2)
    defer("t-002")
    path = pad.write(project)
    disk = open(path, encoding="utf-8").read()
    body = section(disk)
    assert body is not None and any(R1 in b and W1 in b for b in blocks(body, "t-002"))


def test_the_newest_deferral_wins_and_the_task_shows_once(project):
    setup(project, 1)
    defer("t-001", "first reason", "first trigger")
    ok("task", "move", "t-001", "open")
    defer("t-001", "second reason", "second trigger")
    body = not_now(resume(project))
    joined = "\n".join(body)
    assert "second reason" in joined and "second trigger" in joined
    assert "first reason" not in joined and "first trigger" not in joined
    assert len(entries(body, "t-001")) == 1


def test_a_task_moved_back_to_open_leaves_not_now(project):
    setup(project, 1)
    defer("t-001")
    ok("task", "move", "t-001", "open")
    text = resume(project)
    body = section(text) or []
    assert not any(has_id(l, "t-001") for l in body)
    assert any(has_id(l, "t-001") for l in section(text, "## Open tasks"))


def test_deferred_task_of_a_closed_op_is_listed_with_its_op(project):
    """E2: op close settles a deferred task (t-616); resume points and Progress only follow open or
    recently closed ops, so an op filter would lose the parked task for good."""
    setup(project, 2)
    defer("t-002", "parked for later", "when the owner asks")
    finish(project, "t-001")
    ok("op", "close", "op-001", "--basis", "b")
    op2 = new_op()
    add_task(op2, "next op task", "next op statement")
    body = not_now(resume(project))
    assert body is not None
    hits = blocks(body, "t-002")
    assert hits and any("op-001" in b for b in hits), body


def test_line_breaks_and_pipes_in_reason_and_trigger_stay_on_one_line(project):
    setup(project, 2)
    defer("t-002", "line one\nline two | piped", "after:t-001\nthen more text")
    for raw in resume(project).splitlines():
        s = raw.strip()
        assert not s.startswith("line two") and not s.startswith("then more"), raw


def test_long_reason_and_trigger_are_cut(project):
    setup(project, 1)
    defer("t-001", "r" * 3000, "w" * 3000)
    body = not_now(resume(project))
    assert body and max(len(l) for l in body) <= 3 * pad.RESUME_TEXT_MAX + 200, max(len(l) for l in body)


def test_many_deferred_tasks_are_all_accounted_for(project):
    setup(project, 30)
    for i in range(1, 31):
        defer("t-%03d" % i, "reason %d" % i, "trigger %d" % i)
    body = not_now(resume(project))
    listed = {"t-%03d" % i for i in range(1, 31) if any(has_id(l, "t-%03d" % i) for l in body)}
    missing = 30 - len(listed)
    if missing:
        assert any(re.search(r"\b%d\b.*more" % missing, l) for l in body), \
            "%d deferred tasks are hidden with no count" % missing


def test_no_deferred_task_renders_cleanly(project):
    setup(project, 1)
    text = resume(project)
    assert "trigger met" not in text.lower()
    body = section(text)
    assert body is None or not any(has_id(l, "t-001") for l in body)
