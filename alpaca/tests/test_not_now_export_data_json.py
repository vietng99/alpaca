"""(done_bar[0] for data.json, class G): data.json carries each deferred task with its
reason, its trigger text and a met flag; the reason and trigger cross the JSON boundary byte for
byte (render.for_json is identity there, never the pad's one-line cut); pulse.now.tasks puts a
deferred task before the done ones (export.py:92 sorts it after done today); a deferred entry in
pulse.open_tasks, if kept, says it is deferred; two renders agree."""
import json

from alpaca.tests.not_now_kit import data, defer, finish, met_flags, not_now_entries, setup, strings
from alpaca import export


def test_each_deferred_task_has_reason_trigger_and_met(project):
    setup(project, 3)
    defer("t-002", "the vendor has not answered", "when the vendor ships v2")
    defer("t-003", "needs the parser first", "after:t-001")
    payload = data(project)
    for tid, reason, when in (("t-002", "the vendor has not answered", "when the vendor ships v2"),
                              ("t-003", "needs the parser first", "after:t-001")):
        rows = not_now_entries(payload, tid)
        assert rows, "%s has no Not now entry with a met flag in data.json" % tid
        vals = [list(strings(r)) for r in rows]
        assert all(reason in v and when in v for v in vals), rows
        assert all(True not in met_flags(r) for r in rows), "a free-text or unmet trigger is flagged met"
    assert not_now_entries(payload, "t-001") == []


def test_reason_and_trigger_survive_byte_for_byte(project):
    setup(project, 2)
    reason = "na\u00efve | pipe \\ backslash " + "x" * 400
    when = "after:t-001 \u2013 then " + "y" * 400
    defer("t-002", reason, when)
    rows = not_now_entries(data(project), "t-002")
    assert rows and all(reason in list(strings(r)) and when in list(strings(r)) for r in rows)


def test_deferred_sorts_before_done_in_now_tasks(project):
    setup(project, 3)
    finish(project, "t-001")
    defer("t-002")
    ids = [t["id"] for t in data(project)["pulse"]["now"]["tasks"]]
    assert ids.index("t-002") < ids.index("t-001"), ids


def test_open_tasks_entry_for_deferred_says_so(project):
    setup(project, 2)
    defer("t-002")
    for t in data(project)["pulse"]["open_tasks"]:
        if t["id"] == "t-002":
            assert t["status"] == "deferred"


def test_two_renders_agree(project):
    setup(project, 2)
    defer("t-002", "parked", "date:2001-01-01")
    a = json.loads(export.render(project)); b = json.loads(export.render(project))
    a.pop("generated", None); b.pop("generated", None)
    assert a == b
