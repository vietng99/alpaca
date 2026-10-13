"""(done_bar[1], classes A2, A3, F1): with a recorded plan, an opened phase and no
prerequisites, a deferred task falls through taskplan.py:259-266 to 'ready' today, so CHECKLIST.md
names it as 'Next action: Continue ...' and the hub roadmap shows it Ready. A deferred node needs
its own state and a 'Not now' label (LABELS must carry it or LABELS[state] raises), its reasons
must be the deferral reason and trigger (never the task's why, which work_record puts under
'reason'), its dependents stay not ready, and the 'tasks remain' count leaves it out."""
import re
from pathlib import Path

from alpaca.tests.not_now_kit import (blocks, checklist, defer, entries, nodes, open_phase, plan, setup)
from alpaca import db, taskplan

LIVE = ("ready", "working", "waiting", "resources", "approval", "assignment_expired", "unplanned")


def _planned(project, n=3, after=None):
    conn = setup(project, n)
    open_phase(conn)
    plan(conn, [("t-%03d" % i, (after or {}).get("t-%03d" % i, [])) for i in range(1, n + 1)])
    return conn


def test_deferred_node_is_not_ready_and_says_not_now(project):
    conn = _planned(project)
    assert nodes(project, conn)["t-001"]["state"] == "ready", "setup: t-001 must start ready"
    defer("t-001", "the vendor has not answered", "after:t-002")
    got = nodes(project, conn)
    node = got["t-001"]
    assert node["state"] not in LIVE, node
    assert "not now" in node["label"].lower(), node
    assert node["state"] in taskplan.LABELS
    ready = [n for n in got.values() if n["state"] == "ready"]
    assert all("t-001" not in n["alternatives"] for n in ready)


def test_deferred_node_reasons_are_the_deferral_not_the_why(project):
    conn = _planned(project)
    db.patch(conn, "tasks", "id", "t-001", {"why": "the purpose of the task"})
    defer("t-001", "the vendor has not answered", "when the vendor ships v2")
    reasons = " ".join(nodes(project, conn)["t-001"]["reasons"])
    assert "the vendor has not answered" in reasons and "when the vendor ships v2" in reasons
    assert "the purpose of the task" not in reasons


def test_checklist_next_action_never_names_a_deferred_task(project):
    conn = _planned(project)
    defer("t-001")
    text = checklist(project)
    nxt = [l for l in text.splitlines() if l.startswith("Next action:")]
    assert nxt and "t-001" not in nxt[0] and "t-002" in nxt[0], nxt


def test_checklist_with_only_deferred_planned_work_has_no_next_task(project):
    conn = _planned(project, 1)
    defer("t-001")
    nxt = [l for l in checklist(project).splitlines() if l.startswith("Next action:")]
    assert nxt and "t-001" not in nxt[0], nxt


def test_checklist_remain_count_leaves_deferred_out(project):
    conn = _planned(project)
    defer("t-003")
    m = re.search(r"(\d+) tasks? remain", checklist(project))
    assert m and int(m.group(1)) == 2, m and m.group(0)


def test_checklist_lists_deferred_with_reason_and_trigger(project):
    conn = _planned(project)
    defer("t-002", "the vendor has not answered", "when the vendor ships v2")
    hits = blocks(checklist(project), "t-002")
    assert any("the vendor has not answered" in b and "when the vendor ships v2" in b for b in hits), hits
    assert any("not now" in b.lower() for b in hits), hits


def test_dependent_of_a_deferred_prerequisite_is_not_ready(project):
    conn = _planned(project, 2, after={"t-002": ["t-001"]})
    defer("t-001")
    node = nodes(project, conn)["t-002"]
    assert node["state"] != "ready" and "t-001" in " ".join(node["reasons"] + node["blockers"])


def test_hub_roadmap_never_shows_deferred_ready(project):
    from alpaca import hub
    conn = _planned(project)
    defer("t-001")
    view = hub.overview(Path(project))
    got = {n["id"]: n for op in view["roadmap"]["operations"] for g in op["groups"] for n in g["nodes"]}
    assert got["t-001"]["state"] not in LIVE
