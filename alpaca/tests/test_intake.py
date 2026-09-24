"""alpaca intake: a spec and a runbook in; checklist rows, task contracts and the profile out.

Covers the first intake, the dry run, an idempotent rerun, a changed success criterion, a removed
scenario, an added scenario, an OpenSpec change folder applied to the living specs, the refusals,
and the move of a spec-kit project to OpenSpec that keeps every row (docs/intake.md).
"""
import json
import os
import shutil
import stat

import pytest

from alpaca.tests.conftest import REPO

EXAMPLE = os.path.join(REPO, "templates", "runbook-example")


def _cli(argv, capsys):
    from alpaca import cli
    rc = cli.main(argv)
    out = capsys.readouterr().out
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, out


def _op(capsys, intent="Link shortener"):
    rc, out = _cli(["op", "new", intent, "--done-when", "every row proven"], capsys)
    assert rc == 0, out


def _counts(root):
    from alpaca import db
    conn = db.connect(root)
    return {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            for t in ("events", "rows", "tasks")}


@pytest.fixture
def kit(project):
    """The worked example (a spec-kit spec and its runbook) copied into the project, one open op."""
    dst = os.path.join(project, "domain")
    shutil.copytree(EXAMPLE, dst)
    return {"root": project, "spec": os.path.join(dst, "spec.md"), "runbook": os.path.join(dst, "runbook.yaml")}


def _edit(path, old, new):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert old in text, old
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new))


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ------------------------------------------------------------------------------ first intake
def test_first_intake_lands_rows_tasks_contracts_and_profile(kit, capsys):
    from alpaca import board, db, profile, taskcontract
    _op(capsys)
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0, out
    assert out["verdict"] == "PASS" and out["op"] == "op-001" and out["format"] == "spec-kit"
    added = {r["key"]: r for r in out["rows"]["added"]}
    assert sorted(added) == ["SC-001", "SC-002", "SC-003", "SC-004"]
    # a criterion shown by checks is a check row that names them; one judged at a gate is a review row
    assert added["SC-002"]["step"] == "check"
    assert "load-test/redirect-p95" in added["SC-002"]["shown_by"]
    assert added["SC-004"]["step"] == "review"
    assert "owner gate of stage release" in added["SC-004"]["shown_by"]

    conn = db.connect(kit["root"])
    stored = {r["id"]: r for r in db.rows(conn, "rows", "op='op-001'")}
    assert len(stored) == 4
    sc2 = stored[added["SC-002"]["row"]]
    assert sc2["phase"] == "verify" and sc2["step"] == "check"
    assert "50 ms at the 95th percentile" in sc2["statement"]
    assert sc2["proof"].startswith("local:.alpaca/intake/op-001/link-shortener/items/sc-002.")
    item_file = sc2["proof"][len("local:"):].rsplit(":", 1)[0]
    assert os.path.isfile(os.path.join(kit["root"], item_file))
    cards = board.view(conn, op="op-001")
    assert cards["counts"]["todo"] == 4

    # one task per stage and one per owner gate, each with a contract naming its profile stage
    titles = {t["key"]: t for t in out["tasks"]["added"]}
    assert sorted(titles) == ["gate:release", "stage:install", "stage:load-test", "stage:release",
                              "stage:restart-test", "stage:unit-test"]
    contracts = taskcontract.latest(conn)
    load = contracts[titles["stage:load-test"]["task"]]
    assert load["stage"] == "load-test"
    assert any("redirect-p95" in line and "(50)" in line for line in load["done_bar"])
    assert any(line.startswith("port-busy") for line in load["fail_cases"])
    assert any("out/load.json" in line for line in load["expected"])
    gate = contracts[titles["gate:release"]["task"]]
    assert any("approves" in line for line in gate["done_bar"])

    # the profile: project.yaml names intake_profile and its stages are the runbook's stages
    with open(os.path.join(kit["root"], "project.yaml"), encoding="utf-8") as fh:
        assert "profile: intake_profile" in fh.read()
    assert list(profile.load(kit["root"]).stages()) == ["install", "unit-test", "restart-test",
                                                         "load-test", "release"]
    assert {c["script"] for c in out["profile"]["plugin_checks"]} == {"checks/status_codes.py"}
    checks = profile.load(kit["root"]).doctor_checks(kit["root"], conn)
    assert any(name == "plugin check" and level == "ok" for name, level, _d in checks)
    # the run is on the record
    events = [e for e in db.events(conn, kind="intake")]
    assert len(events) == 1 and sorted(events[0]["data"]["items"]) == sorted(added)


def test_dry_run_prints_the_plan_and_writes_nothing(kit, capsys):
    _op(capsys)
    before = _counts(kit["root"])
    with open(os.path.join(kit["root"], "ALPACA-MANIFEST"), encoding="utf-8"):
        pass
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--dry-run", "--json"], capsys)
    assert rc == 0, out
    assert out["dry_run"] is True and len(out["rows"]["added"]) == 4 and len(out["tasks"]["added"]) == 6
    assert _counts(kit["root"]) == before
    assert not os.path.exists(os.path.join(kit["root"], ".alpaca", "intake"))
    assert not os.path.exists(os.path.join(kit["root"], "intake_profile.py"))
    assert not os.path.exists(os.path.join(kit["root"], "project.yaml")) or \
        "intake_profile" not in open(os.path.join(kit["root"], "project.yaml"), encoding="utf-8").read()
    # the text form says so
    rc, text = _cli(["intake", kit["spec"], kit["runbook"], "--dry-run"], capsys)
    assert rc == 0 and "dry run, nothing written" in text and "GATE alpaca-intake: PASS" in text


def test_rerun_with_the_same_spec_changes_nothing(kit, capsys):
    _op(capsys)
    assert _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)[0] == 0
    before = _counts(kit["root"])
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0, out
    assert out["changed"] is False
    assert len(out["rows"]["kept"]) == 4 and not out["rows"]["added"]
    assert not out["rows"]["superseded"] and not out["rows"]["withdrawn"]
    assert len(out["tasks"]["kept"]) == 6 and not out["tasks"]["added"] and not out["tasks"]["contracts"]
    assert _counts(kit["root"]) == before


# ------------------------------------------------------------------------------ a spec change
def test_a_changed_criterion_supersedes_only_its_row_and_keeps_the_other_verdicts(kit, capsys):
    from alpaca import board, db
    from alpaca.checklist import supersession, verdict_row
    from alpaca.gates import verdict
    _op(capsys)
    rc, first = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    ids = {r["key"]: r["row"] for r in first["rows"]["added"]}
    conn = db.connect(kit["root"])
    sc1 = db.rows(conn, "rows", "id=?", (ids["SC-001"],))[0]
    verdict_row.discharge(conn, sc1["id"], sc1["content_hash"], "test", verdict.PASS,
                          ["local:out/junit.xml"], "L2", "s1")

    _edit(kit["spec"], "within 50 ms at the 95th percentile", "within 30 ms at the 95th percentile")
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0, out
    assert [r["key"] for r in out["rows"]["superseded"]] == ["SC-002"]
    assert not out["rows"]["added"] and not out["rows"]["withdrawn"]
    kept = {r["key"]: r for r in out["rows"]["kept"]}
    assert sorted(kept) == ["SC-001", "SC-003", "SC-004"]
    assert kept["SC-001"]["status"] == "discharged" and kept["SC-001"]["row"] == ids["SC-001"]

    sup = out["rows"]["superseded"][0]
    assert sup["old"] == ids["SC-002"] and sup["new"] != ids["SC-002"]
    conn = db.connect(kit["root"])
    new = db.rows(conn, "rows", "id=?", (sup["new"],))[0]
    assert new["supersedes"] == ids["SC-002"] and "30 ms" in new["statement"]
    old = db.rows(conn, "rows", "id=?", (ids["SC-002"],))[0]
    assert old["superseded_by"] == sup["new"]          # the frozen original stays, pointed forward
    assert supersession.head(db.rows(conn, "rows", "1=1"), ids["SC-002"])["id"] == sup["new"]
    assert verdict_row.status_fold(conn, ids["SC-001"]) == verdict_row.DISCHARGED
    shown = {c["row_id"] for c in board.view(conn, op="op-001")["cards"]}
    assert sup["new"] in shown and ids["SC-002"] not in shown and len(shown) == 4

    # a second change to the same criterion supersedes the new head, and the chain holds
    _edit(kit["spec"], "within 30 ms", "within 25 ms")
    rc, again = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0, again
    assert again["rows"]["superseded"][0]["old"] == sup["new"]


def test_a_changed_runbook_stage_updates_that_contract_and_adds_no_task(kit, capsys):
    from alpaca import db, taskcontract
    _op(capsys)
    rc, first = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    tasks = {t["key"]: t["task"] for t in first["tasks"]["added"]}
    _edit(kit["runbook"], "when: port 8080 is already taken, so the service under test never starts",
          "when: port 8080 is taken")
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0, out
    assert out["tasks"]["contracts"] == [{"key": "stage:load-test", "task": tasks["stage:load-test"]}]
    assert not out["tasks"]["added"] and len(out["rows"]["kept"]) == 4
    conn = db.connect(kit["root"])
    assert "port-busy: port 8080 is taken" in taskcontract.latest(conn)[tasks["stage:load-test"]]["fail_cases"]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 6


# ------------------------------------------------------------------------------ OpenSpec
LIVING = """# links Specification

## Purpose

Shorten a long link to a code and send a client who opens the code to the long link.

## Requirements

### Requirement: Shorten
The service SHALL return a 7-character code for a valid URL.

#### Scenario: valid url
- **WHEN** a client posts an http URL
- **THEN** the service answers 201 with a 7-character code

#### Scenario: invalid url
- **WHEN** a client posts a string that is not a URL
- **THEN** the service answers 400

### Requirement: Redirect
The service SHALL redirect a known code.

#### Scenario: known code
- **WHEN** a client requests a known code
- **THEN** the service answers 301 within 50 ms at p95
"""

RUNBOOK = """runbook: 1
id: links
title: Links service
stages:
  - id: test
    run: python3 -m pytest -q
    checks:
      - id: tests-exit
        type: exit-code
        covers: [%s]
  - id: load
    needs: [test]
    run: python3 load.py
    checks:
      - id: p95
        type: json-field
        path: out/load.json
        field: p95_ms
        op: "<="
        value: 50
        covers: [%s]
"""


def _openspec(root, living=LIVING, tests="Shorten/valid url, Shorten/invalid url", load="Redirect/known code"):
    _write(os.path.join(root, "openspec", "specs", "links", "spec.md"), living)
    rb = os.path.join(root, "links", "runbook.yaml")
    _write(rb, RUNBOOK % (tests, load))
    return os.path.join(root, "openspec", "specs"), rb


def test_openspec_removed_scenario_is_withdrawn_and_waived(project, capsys):
    from alpaca import board, db
    from alpaca.checklist import verdict_row
    _op(capsys)
    specs, rb = _openspec(project)
    rc, first = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 0, first
    ids = {r["key"]: r["row"] for r in first["rows"]["added"]}
    assert sorted(ids) == ["links/Redirect/known code", "links/Shorten/invalid url", "links/Shorten/valid url"]

    living = LIVING.replace("""#### Scenario: invalid url
- **WHEN** a client posts a string that is not a URL
- **THEN** the service answers 400

""", "")
    specs, rb = _openspec(project, living, tests="Shorten/valid url")
    rc, out = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 0, out
    assert [(w["key"], w["old"]) for w in out["rows"]["withdrawn"]] == [("links/Shorten/invalid url",
                                                                          ids["links/Shorten/invalid url"])]
    assert not out["rows"]["superseded"] and not out["rows"]["added"] and len(out["rows"]["kept"]) == 2
    conn = db.connect(project)
    wid = out["rows"]["withdrawn"][0]["new"]
    row = db.rows(conn, "rows", "id=?", (wid,))[0]
    assert row["step"] == "withdrawn" and row["supersedes"] == ids["links/Shorten/invalid url"]
    assert "removed from the spec" in row["statement"] and "answers 400" in row["statement"]
    assert verdict_row.status_fold(conn, wid) == verdict_row.WAIVED_STATUS
    card = [c for c in board.view(conn, op="op-001")["cards"] if c["row_id"] == wid][0]
    assert card["column"] == "done"
    # rerun: the withdrawal is not repeated
    rc, again = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 0 and again["changed"] is False and not again["rows"]["withdrawn"]


def test_openspec_added_scenario_adds_one_row(project, capsys):
    _op(capsys)
    specs, rb = _openspec(project)
    assert _cli(["intake", specs, rb, "--json"], capsys)[0] == 0
    living = LIVING + """
#### Scenario: unknown code
- **WHEN** a client requests an unknown code
- **THEN** the service answers 404
"""
    specs, rb = _openspec(project, living, load="Redirect/known code, Redirect/unknown code")
    rc, out = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 0, out
    assert [r["key"] for r in out["rows"]["added"]] == ["links/Redirect/unknown code"]
    assert len(out["rows"]["kept"]) == 3 and not out["rows"]["superseded"] and not out["rows"]["withdrawn"]


CHANGE = """## MODIFIED Requirements

### Requirement: Redirect
The service SHALL redirect a known code within 30 ms.

#### Scenario: known code
- **WHEN** a client requests a known code
- **THEN** the service answers 301 within 30 ms at p95

## ADDED Requirements

### Requirement: Stats
The service SHALL count redirects.

#### Scenario: count
- **WHEN** a code is followed three times
- **THEN** its stats show 3

## REMOVED Requirements

### Requirement: Shorten
**Reason**: links are created by the admin tool now
"""

MERGED = """# links Specification

## Purpose

Shorten a long link to a code and send a client who opens the code to the long link.

## Requirements

### Requirement: Redirect
The service SHALL redirect a known code within 30 ms.

#### Scenario: known code
- **WHEN** a client requests a known code
- **THEN** the service answers 301 within 30 ms at p95

### Requirement: Stats
The service SHALL count redirects.

#### Scenario: count
- **WHEN** a code is followed three times
- **THEN** its stats show 3
"""


def test_openspec_change_folder_is_applied_to_the_living_specs(project, capsys):
    _op(capsys)
    specs, rb = _openspec(project)
    rc, first = _cli(["intake", specs, rb, "--json"], capsys)
    ids = {r["key"]: r["row"] for r in first["rows"]["added"]}
    change = os.path.join(project, "openspec", "changes", "faster-redirect")
    _write(os.path.join(change, "proposal.md"), "## Why\n\nRedirects must be faster.\n")
    _write(os.path.join(change, "specs", "links", "spec.md"), CHANGE)
    # the old runbook still covers the removed scenarios: refused, nothing written
    before = _counts(project)
    rc, out = _cli(["intake", change, rb, "--json"], capsys)
    assert rc == 1 and out["verdict"] == "FAIL"
    assert any("COVERS-UNKNOWN" in d for d in out["detail"]) and any("SPEC-UNCOVERED" in d for d in out["detail"])
    assert _counts(project) == before
    # runbook check reads the change folder the same way
    rc, checked = _cli(["runbook", "check", rb, "--spec", change, "--json"], capsys)
    assert rc == 1 and any(e["code"] == "SPEC-UNCOVERED" and e["where"] == "links/Stats/count"
                           for e in checked["errors"])

    _write(rb, RUNBOOK.replace("value: 50", "value: 30") % ("Stats/count", "Redirect/known code"))
    rc, checked = _cli(["runbook", "check", rb, "--spec", change, "--json"], capsys)
    assert rc == 0, checked
    rc, out = _cli(["intake", change, rb, "--json"], capsys)
    assert rc == 0, out
    assert out["spec_shape"] == "change"
    assert [(r["key"], r["old"]) for r in out["rows"]["superseded"]] == [("links/Redirect/known code",
                                                                          ids["links/Redirect/known code"])]
    assert [r["key"] for r in out["rows"]["added"]] == ["links/Stats/count"]
    assert sorted(r["key"] for r in out["rows"]["withdrawn"]) == ["links/Shorten/invalid url",
                                                                  "links/Shorten/valid url"]
    # after the archive the living spec holds the change: intake of it changes nothing
    _write(os.path.join(project, "openspec", "specs", "links", "spec.md"), MERGED)
    shutil.rmtree(change)
    rc, after = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 0 and after["changed"] is False, after
    assert len(after["rows"]["kept"]) == 2


def test_a_change_that_modifies_a_missing_requirement_is_refused(project, capsys):
    _op(capsys)
    specs, rb = _openspec(project)
    change = os.path.join(project, "openspec", "changes", "bad")
    _write(os.path.join(change, "proposal.md"), "## Why\n\nx\n")
    _write(os.path.join(change, "specs", "links", "spec.md"),
           "## MODIFIED Requirements\n\n### Requirement: Nope\nThe service SHALL x.\n\n#### Scenario: y\n- z\n")
    rc, out = _cli(["intake", change, rb, "--json"], capsys)
    assert rc == 1 and any("Nope" in d for d in out["detail"])


def test_a_scenario_named_by_a_spec_kit_id_keeps_that_key(project, capsys):
    from alpaca import runbook
    _op(capsys)
    living = LIVING.replace("#### Scenario: known code", "#### Scenario: SC-002 known code")
    specs, rb = _openspec(project, living, load="SC-002")
    item = [i for i in runbook.parse_spec(specs)["items"] if i.get("alias")][0]
    assert item["alias"] == "SC-002" and item["required"] is True
    rc, out = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 0, out
    assert "SC-002" in [r["key"] for r in out["rows"]["added"]]
    # an FR id names an optional item: no row, and an uncovered one is only a warning
    living = LIVING.replace("#### Scenario: invalid url", "#### Scenario: FR-002")
    specs, rb = _openspec(project, living, tests="Shorten/valid url")
    rc, out = _cli(["runbook", "check", rb, "--spec", specs, "--json"], capsys)
    assert rc == 0 and any(w["code"] == "FR-UNCOVERED" for w in out["warnings"])


def test_moving_a_spec_kit_project_to_openspec_keeps_every_row(kit, capsys):
    from alpaca import start
    _op(capsys)
    rc, first = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    ids = sorted(r["row"] for r in first["rows"]["added"])
    text = start.moved_spec_text(kit["spec"], "domain/spec.md")
    _write(os.path.join(kit["root"], "openspec", "specs", "link-shortener", "spec.md"), text)
    rc, out = _cli(["intake", os.path.join(kit["root"], "openspec", "specs"), kit["runbook"], "--json"], capsys)
    assert rc == 0, out
    assert out["format"] == "openspec" and out["changed"] is False
    assert sorted(r["row"] for r in out["rows"]["kept"]) == ids


# ------------------------------------------------------------------------------ refusals
def test_a_runbook_that_leaves_a_criterion_uncovered_is_refused(kit, capsys):
    _op(capsys)
    _edit(kit["runbook"], "covers: [SC-003, FR-006]", "covers: [FR-006]")
    before = _counts(kit["root"])
    rc, out = _cli(["intake", kit["spec"], kit["runbook"]], capsys)
    assert rc == 1
    assert "ERROR SPEC-UNCOVERED SC-003" in out and "GATE alpaca-intake: FAIL" in out
    assert _counts(kit["root"]) == before


def test_a_gate_only_stage_with_checks_is_refused_so_no_check_is_lost(kit, capsys):
    # Without `run` the release stage is only an owner gate: intake makes an approval task for it
    # and no stage task, so its two checks would sit in no task contract.
    _op(capsys)
    _edit(kit["runbook"], "    run: ./deploy.sh production --health-out out/health.json\n", "")
    before = _counts(kit["root"])
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--dry-run"], capsys)
    assert rc == 1, out
    assert "CHECKS-WITHOUT-RUN stages[4].checks" in out and "GATE alpaca-intake: FAIL" in out
    assert _counts(kit["root"]) == before


def test_every_check_of_an_accepted_runbook_is_in_a_task_contract(kit):
    import yaml
    from alpaca import intake
    with open(kit["runbook"], encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    plan = intake._task_plan(data, "domain/runbook.yaml")
    bars = "\n".join(line for _key, _title, _statement, contract in plan for line in contract["done_bar"])
    for stage in data["stages"]:
        for chk in stage.get("checks") or []:
            assert "%s/%s (" % (stage["id"], chk["id"]) in bars, chk["id"]


def test_intake_needs_an_open_op(kit, capsys):
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 2 and out["verdict"] == "BLOCKED" and "op new" in out["reason"]


def test_a_project_profile_without_the_runbook_stages_is_refused(kit, capsys):
    _op(capsys)
    _write(os.path.join(kit["root"], "mydomain", "profile.py"),
           "from alpaca.profile import Profile\n\nclass P(Profile):\n    def stages(self):\n"
           "        return ['build']\n\nPROFILE = P\n")
    _write(os.path.join(kit["root"], "project.yaml"), "name: x\nprofile: mydomain.profile\n")
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 2 and "mydomain.profile" in out["reason"] and "install" in out["reason"]


def test_a_runbook_outside_the_project_is_refused(project, tmp_path, capsys):
    _op(capsys)
    outside = tmp_path / "elsewhere"
    shutil.copytree(EXAMPLE, outside)
    rc, out = _cli(["intake", str(outside / "spec.md"), str(outside / "runbook.yaml"), "--json"], capsys)
    assert rc == 2 and "outside the project" in out["reason"]


# ------------------------------------------------------------------------------ the pieces it reuses
def test_obligation_template_reads_a_table_cell():
    from alpaca.checklist import synthesis
    step = {"key": "check", "obligation": "{item}: {cell:Criterion} by {cell:shown  by} {cell:none}"}
    item = {"key": "SC-001", "cells": {"key": "SC-001", "criterion": "it works", "shown by": "a/b"}}
    assert synthesis._statement(step, item, {"path": "x.md"}) == "SC-001: it works by a/b {cell:none}"


def test_task_add_keeps_its_cli_behaviour(project, capsys):
    from alpaca import db
    _op(capsys)
    rc, out = _cli(["task", "add", "op-001", "do the thing", "--title", "Thing"], capsys)
    assert rc == 0 and "t-001 added" in out
    conn = db.connect(project)
    ev = db.events(conn, kind="task-add")[-1]
    assert ev["data"] == {"statement": "do the thing", "title": "Thing", "phase": None, "why": None}


# ------------------------------------------------------------------------------ review fixes (t-008 REVIEW.md)
def _row(root, rid):
    from alpaca import db
    return db.rows(db.connect(root), "rows", "id=?", (rid,))[0]


def test_a_criterion_changed_and_changed_back_supersedes_again_and_intake_goes_on(kit, capsys):
    """F1: 50 ms -> 30 ms -> 50 ms. The revert is a third row that cites the 30 ms row; it is not
    the frozen first row derived again, so the bridge takes it and later intakes still run."""
    _op(capsys)
    rc, first = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    original = {r["key"]: r["row"] for r in first["rows"]["added"]}["SC-002"]
    _edit(kit["spec"], "within 50 ms at the 95th percentile", "within 30 ms at the 95th percentile")
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0, out
    thirty = out["rows"]["superseded"][0]["new"]
    _edit(kit["spec"], "within 30 ms at the 95th percentile", "within 50 ms at the 95th percentile")
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0, out
    assert [(r["key"], r["old"]) for r in out["rows"]["superseded"]] == [("SC-002", thirty)]
    back = out["rows"]["superseded"][0]["new"]
    assert back not in (original, thirty)
    row = _row(kit["root"], back)
    assert row["supersedes"] == thirty and "50 ms" in row["statement"]
    rc, again = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0 and again["changed"] is False, again
    # and the op is not stuck: a runbook change after the revert still lands
    _edit(kit["runbook"], "when: port 8080 is already taken, so the service under test never starts",
          "when: port 8080 is taken")
    rc, later = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0 and [t["key"] for t in later["tasks"]["contracts"]] == ["stage:load-test"], later


def test_a_removed_criterion_restored_with_the_same_text_comes_back(kit, capsys):
    """F1: a spec-kit SC removed (withdrawn and waived) and then restored with its old text."""
    from alpaca import db
    from alpaca.checklist import verdict_row
    _op(capsys)
    rc, first = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    original = {r["key"]: r["row"] for r in first["rows"]["added"]}["SC-003"]
    spec_text = open(kit["spec"], encoding="utf-8").read()
    rb_text = open(kit["runbook"], encoding="utf-8").read()
    _edit(kit["spec"], "- **SC-003**: No link is lost when the service restarts.\n", "")
    _edit(kit["runbook"], "covers: [SC-003, FR-006]", "covers: [FR-006]")
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0, out
    withdrawn = out["rows"]["withdrawn"][0]["new"]
    _write(kit["spec"], spec_text)
    _write(kit["runbook"], rb_text)
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0, out
    assert [(r["key"], r["old"]) for r in out["rows"]["superseded"]] == [("SC-003", withdrawn)]
    back = out["rows"]["superseded"][0]["new"]
    assert back != original
    row = _row(kit["root"], back)
    assert row["step"] == "check" and row["supersedes"] == withdrawn
    conn = db.connect(kit["root"])
    assert verdict_row.status_fold(conn, back) == "open"
    rc, again = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0 and again["changed"] is False, again
    # removed a second time: a second withdrawal, not the first one derived again
    _edit(kit["spec"], "- **SC-003**: No link is lost when the service restarts.\n", "")
    _edit(kit["runbook"], "covers: [SC-003, FR-006]", "covers: [FR-006]")
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 0, out
    assert out["rows"]["withdrawn"][0]["old"] == back
    assert out["rows"]["withdrawn"][0]["new"] != withdrawn


def test_a_removed_openspec_scenario_restored_comes_back(project, capsys):
    """F1 for OpenSpec: a scenario removed and then restored with the same text."""
    _op(capsys)
    specs, rb = _openspec(project)
    rc, first = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 0, first
    living = LIVING.replace("""#### Scenario: invalid url
- **WHEN** a client posts a string that is not a URL
- **THEN** the service answers 400

""", "")
    specs, rb = _openspec(project, living, tests="Shorten/valid url")
    rc, out = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 0 and len(out["rows"]["withdrawn"]) == 1, out
    specs, rb = _openspec(project)
    rc, out = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 0, out
    assert [r["key"] for r in out["rows"]["superseded"]] == ["links/Shorten/invalid url"]
    rc, again = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 0 and again["changed"] is False, again


def test_a_renamed_runbook_id_is_refused(kit, capsys):
    """L1: the rows and tasks are keyed by the runbook id; a new id on the same file would add a
    second set next to the first, so intake refuses and names the old id."""
    _op(capsys)
    assert _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)[0] == 0
    before = _counts(kit["root"])
    _edit(kit["runbook"], "id: link-shortener", "id: link-shortener-2")
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 2 and out["verdict"] == "BLOCKED", out
    assert "link-shortener" in out["reason"] and "link-shortener-2" in out["reason"]
    assert _counts(kit["root"]) == before


def test_a_chain_drift_halt_is_a_gate_line(kit, capsys, monkeypatch):
    """L3: a Halt from supersession (here a prev_hash break) is a BLOCKED gate line, not a traceback."""
    from alpaca.checklist import Halt, supersession
    from alpaca.gates import verdict as vc
    _op(capsys)
    assert _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)[0] == 0
    _edit(kit["spec"], "within 50 ms at the 95th percentile", "within 30 ms at the 95th percentile")

    def drift(rows):
        raise Halt(vc.BLOCKED, supersession.R_CHAIN_DRIFT, "row 2 prev_hash=x, chain expected y")
    monkeypatch.setattr(supersession, "verify_chain", drift)
    before = _counts(kit["root"])
    rc, out = _cli(["intake", kit["spec"], kit["runbook"]], capsys)
    assert rc == 2, out
    assert "SUPERSESSION-CHAIN-DRIFT" in out and "GATE alpaca-intake: BLOCKED" in out
    assert _counts(kit["root"]) == before


def test_a_level_that_cannot_be_read_refuses_the_waiver(project, capsys):
    """L4: the level in force cannot be read (a bad default_level), so the withdrawal is refused
    before anything is written, instead of a waiver recorded at a level nobody set."""
    _op(capsys)
    specs, rb = _openspec(project)
    assert _cli(["intake", specs, rb, "--json"], capsys)[0] == 0
    with open(os.path.join(project, "project.yaml"), "a", encoding="utf-8") as fh:
        fh.write("default_level: banana\n")
    living = LIVING.replace("""#### Scenario: invalid url
- **WHEN** a client posts a string that is not a URL
- **THEN** the service answers 400

""", "")
    specs, rb = _openspec(project, living, tests="Shorten/valid url")
    before = _counts(project)
    rc, out = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 2 and out["verdict"] == "BLOCKED" and "level" in out["reason"], out
    assert _counts(project) == before


def test_a_project_yaml_that_cannot_be_edited_in_place_is_refused(kit, capsys):
    """L5: a project.yaml the text edit cannot keep (a flow mapping) is never rewritten through a
    full dump that drops its comments."""
    _op(capsys)
    path = os.path.join(kit["root"], "project.yaml")
    text = "# keep this comment\n{name: shortener, owner: me}\n"
    _write(path, text)
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--dry-run", "--json"], capsys)
    assert rc == 2 and "project.yaml" in out["reason"], out
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 2 and "project.yaml" in out["reason"], out
    assert open(path, encoding="utf-8").read() == text


def test_a_hand_written_intake_profile_is_not_overwritten(kit, capsys):
    """L5: intake_profile.py that intake did not write is left alone and intake refuses."""
    _op(capsys)
    path = os.path.join(kit["root"], "intake_profile.py")
    _write(path, "# my own profile\nPROFILE = None\n")
    before = _counts(kit["root"])
    rc, out = _cli(["intake", kit["spec"], kit["runbook"], "--json"], capsys)
    assert rc == 2 and "intake_profile.py" in out["reason"], out
    assert open(path, encoding="utf-8").read() == "# my own profile\nPROFILE = None\n"
    assert _counts(kit["root"]) == before


def test_row_statement_is_plain_text_and_the_item_file_has_a_format_line(project, capsys):
    """L6: the criterion loses its markdown emphasis and ends with a period before "Shown by", and
    the item file names its format, so a later layout change is a deliberate one."""
    _op(capsys)
    specs, rb = _openspec(project)
    rc, out = _cli(["intake", specs, rb, "--json"], capsys)
    assert rc == 0, out
    rid = {r["key"]: r["row"] for r in out["rows"]["added"]}["links/Shorten/valid url"]
    row = _row(project, rid)
    assert "**" not in row["statement"], row["statement"]
    assert "WHEN a client posts an http URL" in row["statement"]
    assert "7-character code. Shown by test/tests-exit." in row["statement"], row["statement"]
    item = row["proof"][len("local:"):].rsplit(":", 1)[0]
    text = open(os.path.join(project, item), encoding="utf-8").read()
    assert "format 1" in text.splitlines()[0]
    from alpaca import intake
    assert intake.criterion({"text": "it works"}) == "it works."
    assert intake.criterion({"text": "Is it *fast*?"}) == "Is it fast?"
