"""(done_bar[1]): a deferred task is never the Next action: not on the RESUME line, not
on a resume point's next line, not on data.json's next_action, not when it is the lowest id and
not when it is the only task left."""
from alpaca.tests.not_now_kit import (add_task, data, defer, entries, has_id, new_op, resume, section, setup)
from alpaca import pad


def _next_line(text):
    body = section(text, "## Next action")
    return " ".join(l for l in body if l.strip())


def test_lowest_id_deferred_is_skipped(project):
    setup(project, 2)
    defer("t-001")
    assert pad.next_action(project).startswith("t-002")
    text = resume(project)
    assert not has_id(_next_line(text), "t-001") and has_id(_next_line(text), "t-002")
    assert not has_id(data(project)["pulse"]["now"]["next_action"] or "", "t-001")


def test_only_deferred_left_is_never_next(project):
    setup(project, 1)
    defer("t-001")
    assert not has_id(pad.next_action(project), "t-001")
    text = resume(project)
    assert not has_id(_next_line(text), "t-001")
    for line in text.splitlines():
        if line.strip().startswith("- next:"):
            assert not has_id(line, "t-001"), line


def test_resume_point_next_skips_deferred_per_op(project):
    setup(project, 2)
    defer("t-001")
    op2 = new_op()
    add_task(op2, "second op task", "second op statement")
    defer("t-003")
    text = resume(project)
    nexts = [l for l in text.splitlines() if l.strip().startswith("- next:")]
    assert nexts and not any(has_id(l, "t-001") or has_id(l, "t-003") for l in nexts), nexts
    assert any(has_id(l, "t-002") for l in nexts)
