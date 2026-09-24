"""alpaca intake with item format 2: every row carries its bar, its stage and its source.

Covers the item file of format 2 and the row statement with the bar, supersession on a bar
change (a threshold, a knob default, an op, a path, a field, a detect, a then, an approve text)
and not on a stage-only or source-only change, the move of a project intaken with item format 1
(BAR-BASELINE), the rows of edge cases, the run order of the rows in the text and the JSON, and
the `waiting on <stage>` marker of `alpaca board show --op` (docs/intake.md).
"""
import copy
import json
import os
import shutil

import pytest

from alpaca.tests.conftest import REPO
from alpaca.tests.test_intake import _cli, _counts, _edit, _op, _write  # noqa: F401

EXAMPLE2 = os.path.join(REPO, "templates", "runbook-example")
EXAMPLE1 = os.path.join(REPO, "alpaca", "tests", "fixtures", "runbook-example-v1")

SC2_BAR = ("load-test/redirect-p95: json-field out/load.json redirect.p95_ms <= 50 (knob P95_LIMIT_MS, "
           "owner only); load-test/redirect-codes: plugin checks/status_codes.py out/load.json 301 exits 0")
EC3_BAR = ("fail load-test/port-busy: detect regex-in-file out/load.log matches /Address already in use/ "
           "then run free-port")
#: the rows of the format 2 example in run order: stage position, then key
RUN_ORDER = ["EC-001", "EC-002", "SC-001", "SC-003", "EC-003", "SC-002", "SC-004"]


@pytest.fixture
def kit2(project):
    """The format 2 worked example (edge cases, a fail case with detect/then, a recovery stage,
    sources) copied into the project."""
    dst = os.path.join(project, "domain")
    shutil.copytree(EXAMPLE2, dst)
    return {"root": project, "spec": os.path.join(dst, "spec.md"), "runbook": os.path.join(dst, "runbook.yaml")}


def _intake(kit, capsys, *extra):
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"] + list(extra), capsys)
    assert rc == 0, out
    return out


def _rows(out):
    return {r["key"]: r for group in ("added", "kept", "superseded", "withdrawn") for r in out["rows"][group]}


def _stored(root, rid):
    from alpaca import db
    return db.rows(db.connect(root), "rows", "id=?", (rid,))[0]


def _item_file(root, row):
    path = row["proof"][len("local:"):].rsplit(":", 1)[0]
    with open(os.path.join(root, path), encoding="utf-8") as fh:
        return path, fh.read()


def _last_event(root):
    from alpaca import db
    return db.events(db.connect(root), kind="intake")[-1]["data"]


# ------------------------------------------------------------------------------ B1: item format 2
def test_the_item_file_has_format_2_with_bar_stage_and_source(kit2, capsys):
    _op(capsys)
    out = _intake(kit2, capsys)
    rows = _rows(out)
    sc2 = rows["SC-002"]
    assert sc2["bar"] == SC2_BAR
    assert sc2["stage"] == "4/5 load-test" and sc2["source"] == "spec:SC-002"
    stored = _stored(kit2["root"], sc2["row"])
    path, text = _item_file(kit2["root"], stored)
    lines = text.splitlines()
    assert "format 2" in lines[0]
    assert "| key | criterion | shown by | bar | stage | source | kind | supersedes |" in lines
    row_line = [l for l in lines if l.startswith("| SC-002 |")][0]
    assert "| %s | 4/5 load-test | spec:SC-002 | check | - |" % SC2_BAR in row_line
    # the row statement carries the bar
    assert stored["statement"] == ("SC-002: A redirect answers within 50 ms at the 95th percentile under 200 "
                                   "requests per second. Bar: %s." % SC2_BAR)
    # a check with no source, an owner gate, a fail case
    assert rows["SC-001"]["source"] == "-"
    assert rows["SC-001"]["bar"] == ('unit-test/tests-exit: exit-code exit == 0; unit-test/tests-no-failures: '
                                     'regex-in-file out/junit.xml does not match /failures="[1-9]/')
    assert rows["SC-004"]["bar"].startswith("owner approves: the owner reads the load report")
    assert rows["SC-004"]["stage"] == "5/5 release" and rows["SC-004"]["step"] == "review"
    assert rows["SC-003"]["bar"] == "restart-test/restart-lost: json-field out/restart.json lost == 0"
    # the intake event entry per key carries the bar and the stage
    entry = _last_event(kit2["root"])["items"]["SC-002"]
    assert entry["bar"] == SC2_BAR and entry["stage"] == "4/5 load-test" and entry["row"] == sc2["row"]


# ------------------------------------------------------------------------------ B4: edge-case rows
def test_every_edge_case_gets_a_row(kit2, capsys):
    _op(capsys)
    out = _intake(kit2, capsys)
    added = {r["key"]: r for r in out["rows"]["added"]}
    assert sorted(added) == sorted(RUN_ORDER) and out["required"] == 7
    ec3 = added["EC-003"]
    assert ec3["step"] == "check" and ec3["bar"] == EC3_BAR
    assert ec3["stage"] == "4/5 load-test" and ec3["source"] == "spec:EC-003"
    assert "fail case port-busy of stage load-test" in ec3["shown_by"]
    stored = _stored(kit2["root"], ec3["row"])
    assert stored["statement"].startswith("EC-003: When the port the service listens on is already taken")
    assert stored["statement"].endswith("Bar: %s." % EC3_BAR)
    assert added["EC-001"]["stage"] == "2/5 unit-test" and added["EC-001"]["bar"] == \
        "unit-test/tests-exit: exit-code exit == 0"


# ------------------------------------------------------------------------------ B3: run order
def test_rows_are_listed_in_run_order_in_the_json_and_the_text(kit2, capsys):
    _op(capsys)
    rc, dry = _cli(["intake", kit2["spec"], kit2["runbook"], "--dry-run", "--json"], capsys)
    assert rc == 0, dry
    assert [r["key"] for r in dry["rows"]["added"]] == RUN_ORDER
    rc, text = _cli(["intake", kit2["spec"], kit2["runbook"]], capsys)
    assert rc == 0, text
    marks = [l.split()[1] for l in text.splitlines() if l.startswith("  + ") and l.split()[1] in RUN_ORDER]
    assert marks == RUN_ORDER, text
    assert "[4/5 load-test]" in text and "bar: %s" % SC2_BAR in text
    # a rerun keeps them all, still in run order
    out = _intake(kit2, capsys)
    assert out["changed"] is False and [r["key"] for r in out["rows"]["kept"]] == RUN_ORDER


# ------------------------------------------------------------------------------ B2: a bar change
def _change_and_intake(kit2, capsys, edits, discharge="SC-001"):
    """First intake, a PASS verdict on one row, the runbook edits, intake again."""
    from alpaca import db
    from alpaca.checklist import verdict_row
    from alpaca.gates import verdict
    _op(capsys)
    first = _intake(kit2, capsys)
    ids = {r["key"]: r["row"] for r in first["rows"]["added"]}
    conn = db.connect(kit2["root"])
    row = _stored(kit2["root"], ids[discharge])
    verdict_row.discharge(conn, row["id"], row["content_hash"], "test", verdict.PASS, ["local:out/x"], "L2", "s1")
    for old, new in edits:
        _edit(kit2["runbook"], old, new)
    out = _intake(kit2, capsys)
    return ids, out


CHANGES = {
    "threshold": ("SC-003", [("        field: lost\n        op: \"==\"\n        value: 0\n",
                              "        field: lost\n        op: \"==\"\n        value: 1\n")],
                  "restart-test/restart-lost: json-field out/restart.json lost == 1"),
    "knob default": ("SC-002", [("    type: float\n    default: 50\n", "    type: float\n    default: 40\n")],
                     "redirect.p95_ms <= 40 (knob P95_LIMIT_MS, owner only)"),
    "op": ("SC-002", [("        field: redirect.p95_ms\n        op: \"<=\"\n",
                       "        field: redirect.p95_ms\n        op: \"<\"\n")], "redirect.p95_ms < 50"),
    "field": ("SC-002", [("field: redirect.p95_ms", "field: redirect.p95")], "out/load.json redirect.p95 <= 50"),
    "path": ("SC-003", [("        path: out/restart.json\n        field: lost",
                         "        path: out/restart-2.json\n        field: lost")], "out/restart-2.json lost == 0"),
    "pattern": ("SC-001", [("pattern: 'failures=\"[1-9]'", "pattern: 'failures=\"[1-9][0-9]*'")],
                "/failures=\"[1-9][0-9]*/"),
    "detect": ("EC-003", [("pattern: 'Address already in use'", "pattern: 'EADDRINUSE'")],
               "matches /EADDRINUSE/ then run free-port"),
    "then": ("EC-003", [("then: {run: free-port}", "then: retry")], "then retry"),
    "approve": ("SC-004", [("the owner reads the load report and the README walk-through",
                            "the owner reads the load report, the restart report and the README walk-through")],
                "owner approves: the owner reads the load report, the restart report"),
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_a_bar_change_supersedes_exactly_that_row(kit2, capsys, change):
    key, edits, want = CHANGES[change]
    discharge = "SC-003" if key != "SC-003" else "SC-001"
    ids, out = _change_and_intake(kit2, capsys, edits, discharge=discharge)
    assert [r["key"] for r in out["rows"]["superseded"]] == [key], out["rows"]
    assert not out["rows"]["added"] and not out["rows"]["withdrawn"]
    kept = {r["key"]: r for r in out["rows"]["kept"]}
    assert sorted(kept) == sorted(k for k in RUN_ORDER if k != key)
    assert kept[discharge]["status"] == "discharged" and kept[discharge]["row"] == ids[discharge]
    sup = out["rows"]["superseded"][0]
    assert sup["old"] == ids[key] and want in sup["bar"], sup
    new = _stored(kit2["root"], sup["new"])
    assert new["supersedes"] == ids[key]
    if sup["step"] == "check":                  # a review row's statement names the gate, not the bar
        assert want in new["statement"], new["statement"]
    assert want in _item_file(kit2["root"], new)[1]
    # the next run keeps the new row
    again = _intake(kit2, capsys)
    assert again["changed"] is False and not again["rows"]["superseded"]


def test_a_knob_that_no_bar_names_supersedes_nothing(kit2, capsys):
    _op(capsys)
    _intake(kit2, capsys)
    _edit(kit2["runbook"], "    type: int\n    default: 2\n", "    type: int\n    default: 4\n")
    out = _intake(kit2, capsys)
    assert not out["rows"]["superseded"] and len(out["rows"]["kept"]) == 7


# ------------------------------------------------------------------------------ B2: stage or source only
def test_a_stage_only_change_keeps_every_row(kit2, capsys):
    import yaml
    _op(capsys)
    first = _intake(kit2, capsys)
    ids = {r["key"]: r["row"] for r in first["rows"]["added"]}
    # load-test before restart-test: both need only unit-test, so only the positions move
    with open(kit2["runbook"], encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    stages = data["stages"]
    names = [s["id"] for s in stages]
    i, j = names.index("restart-test"), names.index("load-test")
    stages[i], stages[j] = stages[j], stages[i]
    with open(kit2["runbook"], "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False)
    out = _intake(kit2, capsys)
    assert not out["rows"]["superseded"] and not out["rows"]["added"] and not out["rows"]["withdrawn"]
    kept = {r["key"]: r for r in out["rows"]["kept"]}
    assert {k: r["row"] for k, r in kept.items()} == ids
    assert kept["SC-003"]["stage"] == "4/5 restart-test" and kept["SC-002"]["stage"] == "3/5 load-test"
    assert [r["key"] for r in out["rows"]["kept"]] == ["EC-001", "EC-002", "SC-001", "EC-003", "SC-002",
                                                       "SC-003", "SC-004"]
    # the event records the stage it has now
    entry = _last_event(kit2["root"])["items"]["SC-003"]
    assert entry["stage"] == "4/5 restart-test" and entry["row"] == ids["SC-003"]
    again = _intake(kit2, capsys)
    assert again["changed"] is False


def test_a_source_only_change_keeps_the_row(kit2, capsys):
    _op(capsys)
    first = _intake(kit2, capsys)
    ids = {r["key"]: r["row"] for r in first["rows"]["added"]}
    _edit(kit2["runbook"], "        covers: [SC-002]\n        source: spec:SC-002\n",
          "        covers: [SC-002]\n        source: owner:decision-7\n")
    out = _intake(kit2, capsys)
    assert not out["rows"]["superseded"] and len(out["rows"]["kept"]) == 7
    kept = {r["key"]: r for r in out["rows"]["kept"]}
    assert kept["SC-002"]["row"] == ids["SC-002"] and kept["SC-002"]["source"] == "owner:decision-7"
    assert _last_event(kit2["root"])["items"]["SC-002"]["source"] == "owner:decision-7"
    # a bar change after it still supersedes
    _edit(kit2["runbook"], "    type: float\n    default: 50\n", "    type: float\n    default: 45\n")
    out = _intake(kit2, capsys)
    assert [r["key"] for r in out["rows"]["superseded"]] == ["SC-002"]
    assert out["rows"]["superseded"][0]["old"] == ids["SC-002"]


# ------------------------------------------------------------------------------ B2: from item format 1
#: the step model and the item file layout of item format 1, as intake wrote them before format 2
F1_HEADER = ("Intake item file, format 1. alpaca intake wrote this file from a spec and a runbook "
             "and never edits it; a later intake that sees a change writes a new file.")
F1_OBLIGATIONS = {
    "check": "{item}: {cell:criterion} Shown by {cell:shown by}.",
    "review": "{item}: {cell:criterion} Judged by the owner at {cell:shown by}.",
    "withdrawn": "{item} was removed from the spec, so this row withdraws it and needs no "
                 "discharge. It read: {cell:criterion}",
}


def _f1_text(key, crit, shown, kind, supersedes="-"):
    def cell(text):
        return " ".join(str(text).split()).replace("\\", "\\\\").replace("|", "\\|")
    return ("%s\n\n| key | criterion | shown by | kind | supersedes |\n|---|---|---|---|---|\n"
            "| %s | %s | %s | %s | %s |\n" % (F1_HEADER, cell(key), cell(crit), cell(shown), kind, cell(supersedes)))


def _format1_intake(root, spec, rb, op="op-001"):
    """The record an intake at item format 1 left: format 1 item files, rows made with the format 1
    step model and landed by the bridge, and an intake event whose entries hold only row and step."""
    from alpaca import db, intake, runbook, util
    from alpaca.checklist import bridge, step_model
    from alpaca.gates import verdict
    conn = db.connect(root)
    spec_data, shape = intake.read_spec(spec)
    checked = runbook.check(rb, spec=spec_data)
    assert checked["code"] == 0, checked["errors"]
    data, _report = runbook.load(rb)
    rb_id = data["id"]
    check_stage = {c["id"]: s["id"] for s in data["stages"] for c in (s.get("checks") or [])}
    covered = checked["coverage"]["covered"]
    model = copy.deepcopy(intake.STEP_MODEL)
    for step in model["steps"]:
        step["obligation"] = F1_OBLIGATIONS[step["key"]]
    model_path = os.path.join(root, ".alpaca", "intake", "step-model.json")
    util.write_text(model_path, json.dumps(model, indent=1, sort_keys=True) + "\n")
    loaded = step_model.load(model_path)
    item_dir = ".alpaca/intake/%s/%s/items" % (op, rb_id)
    batch, items = [], {}
    for item in [i for i in spec_data["items"] if i["required"]]:
        key = intake.item_key(item)
        who = covered[item["id"]]
        shown = ", ".join("%s/%s" % (check_stage[w], w) if not w.startswith("gate:")
                          else "the owner gate of stage %s" % w[5:] for w in who)
        kind = "check" if [w for w in who if not w.startswith("gate:")] else "review"
        text = _f1_text(key, intake.criterion(item), shown, kind)
        rel = "%s/%s.%s.md" % (item_dir, intake._slug(key), util.sha256_hex(text)[:12])
        row = intake._derive(loaded, root, rel, text, kind, op, "cli", "alpaca-intake")
        batch.append(row)
        items[key] = {"row": row["id"], "step": kind}
    landed = bridge.apply(conn, batch, session="cli", actor="alpaca-intake", op=op)
    assert landed["verdict"] == verdict.PASS, landed
    rel_rb = os.path.relpath(rb, root).replace(os.sep, "/")
    db.append_event(conn, session="cli", actor="alpaca-intake", kind="intake", op=op, ref=rb_id,
                    data={"op": op, "runbook": rel_rb, "runbook_id": rb_id,
                          "spec": os.path.relpath(spec, root).replace(os.sep, "/"), "spec_shape": shape,
                          "format": spec_data["format"], "items": items, "tasks": {},
                          "profile": "intake_profile",
                          "changes": {"added": sorted(items), "kept": [], "superseded": [], "withdrawn": [],
                                      "tasks_added": [], "contracts": []}})
    return items


@pytest.fixture
def kit1(project):
    dst = os.path.join(project, "domain")
    shutil.copytree(EXAMPLE1, dst)
    return {"root": project, "spec": os.path.join(dst, "spec.md"), "runbook": os.path.join(dst, "runbook.yaml")}


def _files_under(root):
    out = {}
    base = os.path.join(root, ".alpaca", "intake")
    for folder, _dirs, names in os.walk(base):
        for name in names:
            if name.endswith(".md"):
                full = os.path.join(folder, name)
                with open(full, encoding="utf-8") as fh:
                    out[os.path.relpath(full, root)] = fh.read()
    return out


def test_a_format_1_project_moves_to_format_2_with_bar_baseline(kit1, capsys):
    from alpaca import db
    from alpaca.checklist import verdict_row
    from alpaca.gates import verdict
    _op(capsys)
    items = _format1_intake(kit1["root"], kit1["spec"], kit1["runbook"])
    assert sorted(items) == ["SC-001", "SC-002", "SC-003", "SC-004"]
    conn = db.connect(kit1["root"])
    sc1 = _stored(kit1["root"], items["SC-001"]["row"])
    assert "Shown by" in sc1["statement"]
    verdict_row.discharge(conn, sc1["id"], sc1["content_hash"], "test", verdict.PASS, ["local:out/x"], "L2", "s1")
    before = _files_under(kit1["root"])

    out = _intake(kit1, capsys)
    # every row kept, none rewritten, each key warned once
    assert not out["rows"]["superseded"] and not out["rows"]["added"] and not out["rows"]["withdrawn"]
    kept = {r["key"]: r for r in out["rows"]["kept"]}
    assert {k: r["row"] for k, r in kept.items()} == {k: v["row"] for k, v in items.items()}
    assert kept["SC-001"]["status"] == "discharged"
    baseline = sorted(w.split()[1].rstrip(":") for w in out["warnings"] if w.startswith("BAR-BASELINE "))
    assert baseline == ["SC-001", "SC-002", "SC-003", "SC-004"], out["warnings"]
    assert _files_under(kit1["root"]) == before
    assert _stored(kit1["root"], items["SC-001"]["row"])["statement"] == sc1["statement"]
    # the new event holds the bar as the baseline
    entry = _last_event(kit1["root"])["items"]["SC-002"]
    assert entry["row"] == items["SC-002"]["row"] and "redirect-p95" in entry["bar"] and entry["stage"]

    # the baseline is recorded once: a rerun warns no more and changes nothing
    again = _intake(kit1, capsys)
    assert again["changed"] is False and not [w for w in again["warnings"] if w.startswith("BAR-BASELINE")]

    # from now on a bar change supersedes, and the new row cites the format 1 row
    _edit(kit1["runbook"], "        field: lost\n        op: \"==\"\n        value: 0\n",
          "        field: lost\n        op: \"==\"\n        value: 1\n")
    out = _intake(kit1, capsys)
    assert [r["key"] for r in out["rows"]["superseded"]] == ["SC-003"]
    sup = out["rows"]["superseded"][0]
    assert sup["old"] == items["SC-003"]["row"]
    new = _stored(kit1["root"], sup["new"])
    assert new["supersedes"] == items["SC-003"]["row"] and "lost == 1" in new["statement"]
    assert "format 2" in _item_file(kit1["root"], new)[1].splitlines()[0]
    kept = {r["key"]: r for r in out["rows"]["kept"]}
    assert kept["SC-001"]["status"] == "discharged" and kept["SC-001"]["row"] == items["SC-001"]["row"]
    assert _files_under(kit1["root"]).items() >= before.items()


def test_a_format_1_row_whose_criterion_changed_is_superseded_not_baselined(kit1, capsys):
    _op(capsys)
    items = _format1_intake(kit1["root"], kit1["spec"], kit1["runbook"])
    _edit(kit1["spec"], "within 50 ms at the 95th percentile", "within 30 ms at the 95th percentile")
    out = _intake(kit1, capsys)
    assert [r["key"] for r in out["rows"]["superseded"]] == ["SC-002"]
    assert out["rows"]["superseded"][0]["old"] == items["SC-002"]["row"]
    baseline = sorted(w.split()[1].rstrip(":") for w in out["warnings"] if w.startswith("BAR-BASELINE "))
    assert baseline == ["SC-001", "SC-003", "SC-004"]


# ------------------------------------------------------------------------------ B3: the board
def test_board_lists_intake_rows_in_run_order_and_marks_waiting_rows(kit2, capsys):
    from alpaca import board, db
    from alpaca.checklist import verdict_row
    from alpaca.gates import verdict
    _op(capsys)
    out = _intake(kit2, capsys)
    ids = {r["key"]: r["row"] for r in out["rows"]["added"]}
    conn = db.connect(kit2["root"])
    v = board.view(conn, op="op-001")
    order = [c["intake"]["key"] for c in v["cards"] if c.get("intake")]
    assert order == RUN_ORDER
    waiting = {c["intake"]["key"]: c["intake"]["waiting_on"] for c in v["cards"] if c.get("intake")}
    # unit-test needs install, which has no row; restart-test and load-test need unit-test;
    # release needs restart-test and load-test
    assert waiting == {"EC-001": None, "EC-002": None, "SC-001": None, "SC-003": "unit-test",
                       "EC-003": "unit-test", "SC-002": "unit-test", "SC-004": "restart-test"}
    rc, text = _cli(["board", "show", "--op", "op-001"], capsys)
    assert rc == 0
    lines = [l for l in text.splitlines() if l.startswith("  [")]
    shown = [k for l in lines for k in RUN_ORDER if (" %s " % k) in l]
    assert shown == RUN_ORDER, text
    assert "SC-003 [3/5 restart-test] waiting on unit-test" in text

    def discharge(key):
        row = _stored(kit2["root"], ids[key])
        verdict_row.discharge(conn, row["id"], row["content_hash"], "test", verdict.PASS, ["local:x"], "L2", "s1")

    for key in ("EC-001", "EC-002", "SC-001"):
        discharge(key)
    waiting = {c["intake"]["key"]: c["intake"]["waiting_on"] for c in board.view(conn, op="op-001")["cards"]
               if c.get("intake")}
    assert waiting["SC-003"] is None and waiting["SC-002"] is None and waiting["SC-004"] == "restart-test"
    discharge("SC-003")
    waiting = {c["intake"]["key"]: c["intake"]["waiting_on"] for c in board.view(conn, op="op-001")["cards"]
               if c.get("intake")}
    assert waiting["SC-004"] == "load-test"
    # a done row is never marked
    assert waiting["SC-003"] is None


# ------------------------------------------------------------------------------ recovery stages and tasks
#: the stage tasks and gate tasks of the format 2 example: none for the recovery stage free-port
TASK_KEYS = ["gate:release", "stage:install", "stage:load-test", "stage:release", "stage:restart-test",
             "stage:unit-test"]


def test_a_recovery_stage_gets_no_task_and_stays_a_profile_stage(kit2, capsys):
    from alpaca import db, profile, taskcontract
    _op(capsys)
    out = _intake(kit2, capsys)
    tasks = {t["key"]: t for t in out["tasks"]["added"]}
    # free-port runs only when the fail case port-busy sends to it: it has no task of its own
    assert sorted(tasks) == TASK_KEYS
    assert "free-port" in out["stages"] and "free-port" in out["profile"]["stages"]
    assert "free-port" in list(profile.load(kit2["root"]).stages())
    contracts = taskcontract.latest(db.connect(kit2["root"]))
    load = contracts[tasks["stage:load-test"]["task"]]["fail_cases"]
    busy = [l for l in load if l.startswith("port-busy: ")]
    assert len(busy) == 1, load
    line = busy[0]
    assert "port 8080 is already taken" in line
    assert "detect (regex-in-file): out/load.log matches /Address already in use/" in line
    assert "then run the recovery stage free-port: python3 tools/free_port.py 8080 --out out/free-port.json" in line
    assert "free-port/port-free (json-field): out/free-port.json field free == True" in line
    assert "then this stage runs again as one more attempt" in line and "ends FAIL" in line
    unit = contracts[tasks["stage:unit-test"]["task"]]["fail_cases"]
    stop = [l for l in unit if l.startswith("import-error: ")]
    assert len(stop) == 1, unit
    assert ("detect (regex-in-file): out/junit.xml matches /ModuleNotFoundError|ImportError|SyntaxError/"
            in stop[0])
    assert "then stop: the stage ends FAIL without another attempt" in stop[0]
    # a rerun keeps the contracts and adds no task
    again = _intake(kit2, capsys)
    assert not again["tasks"]["added"] and not again["tasks"]["contracts"]


def _example2():
    import yaml
    with open(os.path.join(EXAMPLE2, "runbook.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_every_check_of_a_format_2_runbook_is_in_a_task_contract():
    from alpaca import intake
    data = _example2()
    plan = intake._task_plan(data, "domain/runbook.yaml")
    assert sorted(key for key, _t, _s, _c in plan) == TASK_KEYS
    lines = "\n".join(line for _k, _t, _s, contract in plan
                      for line in contract["done_bar"] + contract["fail_cases"])
    for stage in data["stages"]:
        for chk in stage.get("checks") or []:
            assert "%s/%s (" % (stage["id"], chk["id"]) in lines, chk["id"]


THEN_TEXTS = {
    "retry": "then retry: another attempt through the retry rule of the stage",
    "ask-owner": "then ask the owner: the stage pauses for a decision",
    "stop": "then stop: the stage ends FAIL without another attempt",
}


@pytest.mark.parametrize("then", sorted(THEN_TEXTS))
def test_every_fail_case_line_names_its_detect_and_its_then(then):
    from alpaca import intake
    data = _example2()
    load = [s for s in data["stages"] if s["id"] == "load-test"][0]
    load["fails"][0]["then"] = then
    load["fails"].append({"id": "no-log", "when": "the load run wrote no log."})
    load["fails"].append({"id": "slow-disk", "when": "the disk is slow",
                          "detect": {"type": "json-field", "path": "out/load.json", "field": "disk_ms",
                                     "op": ">", "value": "${P95_LIMIT_MS}"}, "then": "ask-owner"})
    plan = {key: contract for key, _t, _s, contract in intake._task_plan(data, "domain/runbook.yaml")}
    fails = plan["stage:load-test"]["fail_cases"]
    busy = [l for l in fails if l.startswith("port-busy: ")][0]
    assert "detect (regex-in-file): out/load.log matches /Address already in use/" in busy
    assert THEN_TEXTS[then] in busy and "free-port" not in busy
    # a fail case without detect keeps its format 1 line
    assert "no-log: the load run wrote no log." in fails
    slow = [l for l in fails if l.startswith("slow-disk: ")][0]
    assert "detect (json-field): out/load.json field disk_ms > ${P95_LIMIT_MS} (50)" in slow
    assert THEN_TEXTS["ask-owner"] in slow


# ------------------------------------------------------------------------------ the move to OpenSpec
EC3_TEXT = ("When the port the service listens on is already taken, the load test frees it and runs again "
            "instead of reporting a latency failure.")


def test_the_move_to_openspec_carries_the_edge_cases():
    from alpaca import start
    text = start.moved_spec_text(os.path.join(EXAMPLE2, "spec.md"), "domain/spec.md")
    for ec in ("EC-001", "EC-002", "EC-003"):
        assert "### Requirement: %s\n" % ec in text and "#### Scenario: %s\n" % ec in text, ec
    assert "#### Scenario: EC-003\n\n%s\n" % EC3_TEXT in text
    assert "The system SHALL handle edge case EC-003." in text
    # SC first, then EC, then FR
    assert text.index("Requirement: SC-004") < text.index("Requirement: EC-001") < text.index("Requirement: FR-001")


def test_an_edge_case_scenario_keeps_its_id_and_is_required(tmp_path):
    from alpaca import runbook
    assert runbook.scenario_alias("EC-003") == ("EC-003", "")
    assert runbook.scenario_alias("EC-003: port taken") == ("EC-003", "port taken")
    spec = tmp_path / "spec.md"
    spec.write_text("## Requirements\n\n### Requirement: EC-003\n\nThe system SHALL handle it.\n\n"
                    "#### Scenario: EC-003\n\n%s\n" % EC3_TEXT, encoding="utf-8")
    items = runbook.parse_spec(str(spec))["items"]
    assert [(i["alias"], i["kind"], i["required"]) for i in items] == [("EC-003", "EC", True)]


def test_moving_a_format_2_project_to_openspec_keeps_every_row_and_its_verdict(kit2, capsys):
    from alpaca import db, runbook, start
    from alpaca.checklist import verdict_row
    from alpaca.gates import verdict
    _op(capsys)
    first = _intake(kit2, capsys)
    ids = {r["key"]: r["row"] for r in first["rows"]["added"]}
    conn = db.connect(kit2["root"])
    for key in ("EC-003", "SC-001"):
        row = _stored(kit2["root"], ids[key])
        verdict_row.discharge(conn, row["id"], row["content_hash"], "test", verdict.PASS, ["local:out/x"], "L2", "s1")
    text = start.moved_spec_text(kit2["spec"], "domain/spec.md")
    specs = os.path.join(kit2["root"], "openspec", "specs")
    _write(os.path.join(specs, "link-shortener", "spec.md"), text)
    # the runbook covers the moved spec: covers [EC-003] finds the scenario named EC-003
    checked = runbook.check(kit2["runbook"], specs)
    assert checked["verdict"] == "PASS", checked["errors"]
    assert checked["coverage"]["covered"]["link-shortener/EC-003/EC-003"] == ["fail:load-test/port-busy"]
    rc, out = _cli(["intake", specs, kit2["runbook"], "--json"], capsys)
    assert rc == 0, out
    assert out["format"] == "openspec" and out["changed"] is False
    kept = {r["key"]: r for r in out["rows"]["kept"]}
    assert [r["key"] for r in out["rows"]["kept"]] == RUN_ORDER
    assert {k: r["row"] for k, r in kept.items()} == ids
    assert kept["EC-003"]["status"] == "discharged" and kept["SC-001"]["status"] == "discharged"
    assert not out["rows"]["added"] and not out["rows"]["superseded"] and not out["rows"]["withdrawn"]
    # a later OpenSpec change to the edge case keeps it a required item keyed EC-003: its row is
    # superseded, not withdrawn
    change = os.path.join(kit2["root"], "openspec", "changes", "port-in-use")
    _write(os.path.join(change, "proposal.md"), "## Why\n\nSay what the load test does on a busy port.\n")
    _write(os.path.join(change, "specs", "link-shortener", "spec.md"),
           "## MODIFIED Requirements\n\n### Requirement: EC-003\n\nThe system SHALL handle edge case EC-003.\n\n"
           "#### Scenario: EC-003\n\nWhen the port is already taken, the load test frees it and runs again.\n")
    rc, out = _cli(["intake", change, kit2["runbook"], "--json"], capsys)
    assert rc == 0, out
    assert [(r["key"], r["old"]) for r in out["rows"]["superseded"]] == [("EC-003", ids["EC-003"])]
    assert not out["rows"]["withdrawn"] and not out["rows"]["added"]


def test_the_docs_describe_recovery_tasks_and_the_edge_case_move():
    def read(rel):
        with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
            return " ".join(fh.read().split())
    intake_doc, format_doc = read("docs/intake.md"), read("docs/runbook-format.md")
    assert "A recovery stage gets no task" in intake_doc
    assert "stays a profile stage" in intake_doc
    assert "### Requirement: EC-001" in intake_doc and "#### Scenario: EC-001" in intake_doc
    assert "an `SC` or `EC` id is a required item" in intake_doc
    assert "an `SC` or `EC` id is a required item" in format_doc


# ------------------------------------------------------------------------------ review and trial fixes
OPENSPEC_EC_NAMED = """# Service Specification

## Purpose

A service.

## Requirements

### Requirement: Startup

The service SHALL start.

#### Scenario: Starts clean

- WHEN started
- THEN it listens

#### Scenario: EC-001 port taken

- WHEN the port is taken
- THEN it exits with a clear message
"""

SVC_RUNBOOK = """runbook: %d
id: svc
title: Service
stages:
  - id: start
    run: ./start.sh
    checks:
      - id: start-exit
        type: exit-code
        covers: ["Startup/Starts clean", "Startup/EC-001 port taken"]
"""


@pytest.mark.parametrize("fmt", [1, 2])
def test_an_openspec_scenario_named_ec_keeps_its_scenario_row_key(project, capsys, fmt):
    """OpenSpec is unchanged (design A1): `#### Scenario: EC-001 port taken` under `### Requirement:
    Startup` keys its row `Startup/EC-001 port taken`, as before format 2, and its criterion keeps
    the id in the text."""
    spec = os.path.join(project, "domain", "spec.md")
    rb = os.path.join(project, "domain", "runbook.yaml")
    _write(spec, OPENSPEC_EC_NAMED)
    _write(rb, SVC_RUNBOOK % fmt)
    _op(capsys)
    rc, out = _cli(["intake", spec, rb, "--json"], capsys)
    assert rc == 0, out
    added = {r["key"]: r for r in out["rows"]["added"]}
    assert sorted(added) == ["Startup/EC-001 port taken", "Startup/Starts clean"], sorted(added)
    stored = _stored(project, added["Startup/EC-001 port taken"]["row"])
    assert stored["statement"].startswith("Startup/EC-001 port taken: EC-001 port taken: ")
    rc, again = _cli(["intake", spec, rb, "--json"], capsys)
    assert rc == 0 and again["changed"] is False


def test_a_change_adding_an_ec_named_scenario_keeps_its_scenario_key(tmp_path):
    from alpaca import intake
    living = tmp_path / "openspec" / "specs" / "svc" / "spec.md"
    _write(str(living), "# svc\n\n## Requirements\n\n### Requirement: Other\n\nIt SHALL work.\n\n"
                        "#### Scenario: Works\n\nIt works.\n")
    delta = tmp_path / "openspec" / "changes" / "add-startup" / "specs" / "svc" / "spec.md"
    _write(str(tmp_path / "openspec" / "changes" / "add-startup" / "proposal.md"), "# add startup\n")
    _write(str(delta), "## ADDED Requirements\n\n### Requirement: Startup\n\nIt SHALL start.\n\n"
                       "#### Scenario: EC-001 port taken\n\nIt exits with a clear message.\n")
    spec = intake.effective_change(str(tmp_path / "openspec" / "changes" / "add-startup"))
    keys = sorted(intake.item_key(i) for i in spec["items"])
    assert keys == ["svc/Other/Works", "svc/Startup/EC-001 port taken"], keys


def test_flipping_owner_only_is_not_a_bar_change(kit2, capsys):
    """owner_only says who may change a knob, not what a check judges: it is not in the list of
    bar changes (docs/intake.md), so flipping it keeps every row and its verdict."""
    ids, out = _change_and_intake(kit2, capsys, [("    max: 1000\n    owner_only: true\n",
                                                  "    max: 1000\n    owner_only: false\n")],
                                  discharge="SC-002")
    assert not out["rows"]["superseded"] and not out["rows"]["added"], out["rows"]
    kept = {r["key"]: r for r in out["rows"]["kept"]}
    assert kept["SC-002"]["row"] == ids["SC-002"] and kept["SC-002"]["status"] == "discharged"
    # a threshold change after it still supersedes
    _edit(kit2["runbook"], "    type: float\n    default: 50\n", "    type: float\n    default: 45\n")
    out = _intake(kit2, capsys)
    assert [r["key"] for r in out["rows"]["superseded"]] == ["SC-002"]


def test_a_knob_in_plugin_args_shows_its_value_in_the_contract():
    """A `${KNOB}` in a plugin's args is a threshold like a json-field value: the contract line
    shows the knob's value next to it, as the bar does."""
    from alpaca import intake
    data = _example2()
    load = [s for s in data["stages"] if s["id"] == "load-test"][0]
    codes = [c for c in load["checks"] if c["id"] == "redirect-codes"][0]
    codes["args"] = ["out/load.json", "${P95_LIMIT_MS}"]
    plan = {key: contract for key, _t, _s, contract in intake._task_plan(data, "domain/runbook.yaml")}
    line = [l for l in plan["stage:load-test"]["done_bar"] if l.startswith("load-test/redirect-codes ")][0]
    assert "plugin checks/status_codes.py out/load.json ${P95_LIMIT_MS} (50) passes" in line, line


def test_the_fail_cases_of_a_recovery_stage_are_in_the_contract_that_sends_to_it():
    """A recovery stage has no task, so the fail case line that sends to it also names the
    recovery stage's own fail cases: nothing in the bar is left out of every contract."""
    from alpaca import intake
    data = _example2()
    free = [s for s in data["stages"] if s["id"] == "free-port"][0]
    free["fails"] = [{"id": "still-busy", "when": "the port stays taken",
                      "detect": {"type": "json-field", "path": "out/free-port.json", "field": "free",
                                 "op": "==", "value": False}, "then": "stop"}]
    plan = {key: contract for key, _t, _s, contract in intake._task_plan(data, "domain/runbook.yaml")}
    busy = [l for l in plan["stage:load-test"]["fail_cases"] if l.startswith("port-busy: ")][0]
    assert "free-port fail case still-busy: the port stays taken" in busy, busy
    assert "detect (json-field): out/free-port.json field free == False" in busy
    assert busy.count("then stop: the stage ends FAIL without another attempt") == 1
