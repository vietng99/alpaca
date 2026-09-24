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
