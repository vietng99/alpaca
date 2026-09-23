"""M1.15 -- the composed door: workspace_guard first, phase defaults, project contracts.

Proves the door composition properties the boundary cases in test_phase_gate.py rely on:

  * workspace_guard is the FIRST precondition: a containment breach BLOCKS the door before the
    phase defaults are even folded;
  * the generic defaults are shipped for the five phases exactly as the 5.3 spec table states;
  * a project's own contracts under contracts/<phase>/ are composed into the door and a failing
    contract closes it;
  * an empty phase universe is a vacuous universal, never a pass (BLOCKED);
  * an unknown boundary is refused, not guessed.
"""
import os
import stat

import pytest

from alpaca import cli, db
from alpaca.checklist import Halt, artifact, step_model, synthesis, verdict_row
from alpaca.gates import verdict as vc
from alpaca.phase import defaults, doors

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS = os.path.join(REPO, "step-models")
NOTMP = frozenset()

SPEC = """# Sample spec

## Acceptance

| item  | statement                          | oracle class | proof kind |
| ----- | ---------------------------------- | ------------ | ---------- |
| AC-01 | the parser reads a clean table     | unit         | test       |
| AC-02 | residue outside the table is BLOCK | unit         | test       |
"""


def _setup(project):
    cli.main(["init"])
    return db.connect(project)


def _persist(conn, row):
    dbrow = {c: row.get(k) for c, k in (
        ("id", "id"), ("kind", "kind"), ("op", "op"), ("phase", "phase"), ("step", "step"),
        ("statement", "statement"), ("proof", "proof"), ("where_", "where"), ("how", "how"),
        ("when_", "when"), ("why", "why"), ("session", "session"), ("operator", "operator"),
        ("status", "status"), ("tag", "tag"), ("content_hash", "content_hash"),
        ("prev_hash", "prev_hash"), ("supersedes", "supersedes"))}
    db.upsert(conn, "rows", "id", dbrow)
    return row


def _discharged_requirement(conn, project, op="op-001"):
    p = os.path.join(project, "spec.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(SPEC)
    art = artifact.parse(p, "item")
    model = step_model.load(os.path.join(MODELS, "requirement.json"))
    rows = synthesis.synthesize(model, art, op=op)
    for r in rows:
        _persist(conn, r)
        verdict_row.discharge(conn, r["id"], r["content_hash"], "probe", vc.PASS, [], "L5", "s1")
    return rows


def _script(dirpath, name, exit_code):
    os.makedirs(dirpath, exist_ok=True)
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit %d\n" % exit_code)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


# ---------------------------------------------------------------- generic defaults shipped
def test_generic_defaults_cover_the_five_phases_verbatim():
    assert tuple(defaults.GENERIC_DEFAULTS) == defaults.LADDER
    # exactly as the 5.3 spec table states them (a couple of anchor lines per phase).
    assert defaults.GENERIC_DEFAULTS["requirement"][0] == "spec file exists"
    assert "closure R2 PASS" in defaults.GENERIC_DEFAULTS["requirement"]
    assert defaults.GENERIC_DEFAULTS["verify"][0] == "test command exit 0"
    assert defaults.GENERIC_DEFAULTS["release"][-1] == \
        "ship decision recorded (human-only, every level)"
    for phase in defaults.LADDER:
        assert defaults.GENERIC_DEFAULTS[phase], "every phase ships a non-empty default set"


# ---------------------------------------------------------------- workspace_guard first
def test_workspace_guard_is_the_first_link(project):
    conn = _setup(project)
    lk = doors.links(conn, "op-001", "requirement->design", root=project, tmp_prefixes=NOTMP)
    assert lk[0][0] == "workspace-guard"


def test_a_containment_breach_blocks_the_door_before_the_defaults(project):
    conn = _setup(project)
    _discharged_requirement(conn, project)
    # a root that resolves under a scratch/tmp root fails workspace_guard's NO-TMP invariant
    # (default tmp prefixes ON), so the door BLOCKS on its first precondition even though every
    # obligation is discharged.
    code = doors.run(conn, "op-001", "requirement->design", level="L5", root=project,
                     tmp_prefixes=None, session="s1")
    assert code == vc.BLOCKED


# ---------------------------------------------------------------- project contracts composed
def test_a_failing_project_contract_closes_the_door(project):
    conn = _setup(project)
    _discharged_requirement(conn, project)
    _script(os.path.join(project, "contracts", "requirement"), "bad.sh", vc.FAIL)
    code = doors.run(conn, "op-001", "requirement->design", level="L5", root=project,
                     tmp_prefixes=NOTMP, session="s1")
    assert code == vc.FAIL


def test_a_passing_project_contract_is_composed_and_lets_the_door_open(project):
    conn = _setup(project)
    _discharged_requirement(conn, project)
    _script(os.path.join(project, "contracts", "requirement"), "ok.sh", vc.PASS)
    lk = doors.links(conn, "op-001", "requirement->design", root=project, tmp_prefixes=NOTMP)
    assert any(n.startswith("contracts:") for n, _ in lk)
    code = doors.run(conn, "op-001", "requirement->design", level="L5", root=project,
                     tmp_prefixes=NOTMP, session="s1")
    assert code == vc.PASS


# ---------------------------------------------------------------- vacuous universe / unknown
def test_an_empty_phase_universe_is_blocked_never_a_vacuous_pass(project):
    conn = _setup(project)
    code = doors.run(conn, "op-001", "requirement->design", level="L5", root=project,
                     tmp_prefixes=NOTMP, session="s1")
    assert code == vc.BLOCKED


def test_an_unknown_boundary_is_refused(project):
    conn = _setup(project)
    with pytest.raises(Halt) as ex:
        doors.run(conn, "op-001", "build->release", level="L5", root=project, tmp_prefixes=NOTMP)
    assert ex.value.verdict == vc.BLOCKED
