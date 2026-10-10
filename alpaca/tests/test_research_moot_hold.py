"""t-306: a loosen hold lifts as moot when its loosening is gone (design/research-engine/
in-session-runtime.md section 22).

The first refpack start after t-297 was refused CONTRACT-LOOSEN-UNGRANTED: t-297 had edited one
line of packs/refpack/README.md, a frozen pack file, and the start opened loosen hold 108083
(question q-008, card rc-004). t-306 reverts the file and teaches the start verb to lift an open
loosen hold as moot when the contract it would open now loosens none of the fields the hold names.

Done when (the contract of t-306):

  * packs/refpack/README.md matches its accepted hash cc18bd20...
    (test_the_refpack_readme_is_back_at_its_accepted_hash);
  * a start lifts an open loosen hold whose named fields no longer loosen, records the lift with
    the reason, and proceeds (test_a_reverted_pack_file_lifts_its_loosen_hold_as_moot_and_starts,
    test_a_reverted_task_override_lifts_its_loosen_hold_as_moot,
    test_the_refpack_readme_hold_lifts_as_moot_once_the_file_is_reverted);
  * a loosen hold whose fields still loosen still refuses the start until the owner grants
    (test_a_field_that_still_loosens_keeps_the_hold_until_the_owner_grants).

Fail cases:

  * an agent edit that still loosens a field is lifted without the owner: the test above, plus
    test_swapping_one_loosening_for_another_opens_a_new_hold (a revert of the named field with a
    new loosening elsewhere lifts the old hold and opens a new one; the start stays refused) and
    the fail-closed unit tests of holds.lift_moot and the start's re-check (a hold that names no
    field, a re-check that does not cover the hold's ref or cannot run, another gate);
  * q-008 still blocks t-111: the refpack test runs the live command's shape
    (`start --runbook packs/refpack/improve.yaml --control noop --via autoresearch`) against a
    hold opened the way 108083 was, and after the revert it starts with no owner grant.

The start runs the real loader and open_run in a small git project with the launch faked
(test_research_cli._real_project); those tests skip without bubblewrap. No test touches the
network or launches an agent runtime; all data is synthetic.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from alpaca import cli, db, ops, questions, review, taskcontract, util
from alpaca.gates import verdict as vc
from alpaca.posture import loops
from alpaca.research import contract, events, holds, jail, owner
from alpaca.tests import research_loopkit as kit
from alpaca.tests import research_packkit as packkit
from alpaca.tests.conftest import REPO
from alpaca.tests.devfiles import needs_dev_files
from alpaca.tests.plain_op import plain_op
from alpaca.tests.test_research_cli import (RB, _call, _conn, _ev, _grant, _real_project, _write,
                                            rec)  # noqa: F401 - rec is a fixture

needs_jail = pytest.mark.skipif(not jail.available()[0],
                                reason="bubblewrap is not available on this host")

PACK_REF = "contract:%s#-" % RB
MEASURE = "pack/measure.sh"
ACCEPTED_README = "cc18bd20e13e458fe432383863917fca110577f0ca85f7867e00c4314c8ed5a7"
REFPACK_RB = "packs/refpack/improve.yaml"


# ------------------------------------------------------------------------------ helpers
def _land_baseline(root, runbook=RB):
    """The pack-level baseline a first freeze lands (contract.land), from the files as committed."""
    conn = _conn(root)
    try:
        ev = contract.land(conn, dict(contract.preview(root, runbook)["pack"], grants=[]),
                           op=None, via="loader", session="test", actor="alpaca-research")
    finally:
        conn.close()
    return ev["id"]


def _commit_file(root, rel, data: bytes):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = os.stat(path).st_mode if os.path.exists(path) else None
    with open(path, "wb") as fh:
        fh.write(data)
    if mode is not None:
        os.chmod(path, mode)
    kit.git(root, "add", "-A")
    kit.git(root, "commit", "-q", "-m", "edit %s" % rel)


def _read(root, rel) -> bytes:
    with open(os.path.join(root, rel), "rb") as fh:
        return fh.read()


def _hold_on(root, ref):
    got = [h for h in _ev(root, events.HOLD, ref) if h["data"].get("gate") == "loosen"]
    assert got, "no loosen hold on %s" % ref
    return got[-1]


def _question(root, qid):
    conn = _conn(root)
    try:
        return questions._state(conn)[qid]
    finally:
        conn.close()


def _card_status(root, cid):
    conn = _conn(root)
    try:
        return review._state(conn)[cid]["status"]
    finally:
        conn.close()


def _halted(root, ref):
    conn = _conn(root)
    try:
        return loops.is_halted(conn, ref)
    finally:
        conn.close()


def _start_noop(capsys, runbook=RB, *extra):
    return _call(capsys, "start", "--runbook", runbook, "--control", "noop", *extra)


# ------------------------------------------------------------------------------ done bar 0
@needs_dev_files("packs/refpack/README.md")
def test_the_refpack_readme_is_back_at_its_accepted_hash():
    """The bytes the refpack baseline accepted (git show e744d25a:packs/refpack/README.md)."""
    data = _read(REPO, os.path.join("packs", "refpack", "README.md"))
    assert hashlib.sha256(data).hexdigest() == ACCEPTED_README


def test_the_live_hold_detail_names_its_field():
    """held_fields reads a hold opened before t-306 (no `fields` key) from its detail lines, the
    shape of event 108083, and the `fields` a hold records since t-306."""
    detail = ('files[packs/refpack/README.md]: accepted "%s", new "%s"\n'
              "owner, in your own terminal: bin/alpaca research grant --runbook "
              "packs/refpack/improve.yaml loosen --set 'files[packs/refpack/README.md]=\"%s\"'"
              % (ACCEPTED_README, "f" * 64, "f" * 64))
    assert holds.held_fields({"detail": detail}) == ["files[packs/refpack/README.md]"]
    assert holds.held_fields({"detail": "", "fields": ["research.stop.slots"]}) == \
        ["research.stop.slots"]
    assert holds.held_fields({"detail": "a loosen hold with no field line"}) == []


# ------------------------------------------------------------------------------ done bar 1
@needs_jail
def test_a_reverted_pack_file_lifts_its_loosen_hold_as_moot_and_starts(project, capsys,
                                                                       monkeypatch):
    root = _real_project(project, monkeypatch)
    baseline = _land_baseline(root)
    accepted = _read(root, MEASURE)
    _commit_file(root, MEASURE, accepted + b"# one edited line\n")
    code, body = _start_noop(capsys)
    assert code == vc.PAUSED and body["code"] == "CONTRACT-LOOSEN-UNGRANTED", body
    hold = _hold_on(root, PACK_REF)
    assert hold["data"]["fields"] == ["files[%s]" % MEASURE]
    # the hold records the baseline its loosening was measured against
    assert hold["data"]["measured"]["baseline"] == baseline
    qid, cid = hold["data"]["question"], hold["data"]["card"]
    assert _question(root, qid)["status"] == "open" and _card_status(root, cid) == "open"
    assert _halted(root, PACK_REF)
    # the file goes back to the accepted bytes: the next start lifts the hold as moot and starts
    _commit_file(root, MEASURE, accepted)
    code, body = _start_noop(capsys)
    assert code == vc.PASS, body
    assert body["start"]["moot_lifted"] == [hold["id"]]
    lifted = _ev(root, events.HOLD_LIFTED, PACK_REF)
    start = _ev(root, events.RUN_START)
    assert len(lifted) == 1 and len(start) == 1
    lift = lifted[0]["data"]
    assert lift["hold"] == hold["id"] and lift["answer"] == "moot" and lift["decision"] is None
    assert lift["gate"] == "loosen"
    assert lift["recheck"]["ref"] == PACK_REF
    assert lift["recheck"]["fields"] == ["files[%s]" % MEASURE]
    assert lift["recheck"]["loosened"] == [] and lift["recheck"]["changed"] == []
    assert lift["recheck"]["baseline"] == baseline
    assert lift["recheck"]["pack_sha256"] == start[0]["data"]["pack_sha256"]
    assert lifted[0]["id"] < start[0]["id"]            # lifted before the run opened
    # the owner question and the card are closed the way lift_hold closes them, and say why
    q = _question(root, qid)
    assert q["status"] == "resolved" and q["answer"].startswith("moot:")
    assert "not by an owner decision" in q["answer"]
    assert _card_status(root, cid) != "open"
    assert not _halted(root, PACK_REF)
    conn = _conn(root)
    try:
        assert holds.open_holds(conn, PACK_REF) == []
    finally:
        conn.close()
    assert _ev(root, events.GRANT) == []                 # no owner grant was needed


@needs_jail
def test_a_reverted_task_override_lifts_its_loosen_hold_as_moot(project, capsys, monkeypatch):
    root = _real_project(project, monkeypatch, usd=10)
    conn = _conn(root)
    tid = ops.add_task(conn, "op-001", "a research task", "research task", phase="build")
    taskcontract.record(conn, tid, {"done_bar": ["x"], "research": {"budget": {"usd": 20}}},
                        session="test", actor="human")
    conn.close()
    ref = "contract:%s#%s" % (RB, tid)
    code, body = _call(capsys, "start", "--runbook", RB, "--task", tid)
    assert code == vc.PAUSED and body["code"] == "CONTRACT-LOOSEN-UNGRANTED", body
    hold = _hold_on(root, ref)
    assert hold["data"]["fields"] == ["research.budget.usd"]
    # a first block: no baseline, so the hold records the pack it was measured against
    assert hold["data"]["measured"]["baseline"] is None
    assert len(hold["data"]["measured"]["pack_sha256"]) == 64
    # the override goes back under the pack cap: a tightening, no loosening left
    conn = _conn(root)
    taskcontract.record(conn, tid, {"done_bar": ["x"], "research": {"budget": {"usd": 5}}},
                        session="test", actor="human")
    conn.close()
    code, body = _call(capsys, "start", "--runbook", RB, "--task", tid)
    assert code == vc.PASS, body
    lifted = _ev(root, events.HOLD_LIFTED, ref)
    assert len(lifted) == 1 and lifted[0]["data"]["answer"] == "moot"
    assert lifted[0]["data"]["recheck"]["changed"] == [
        {"field": "research.budget.usd", "direction": "tightened"}]
    assert _ev(root, events.RUN_START)[0]["data"]["budget"]["usd"] == 5


# ------------------------------------------------------------------------------ done bar 2
@needs_jail
def test_a_field_that_still_loosens_keeps_the_hold_until_the_owner_grants(project, capsys,
                                                                          monkeypatch):
    root = _real_project(project, monkeypatch)
    _land_baseline(root)
    accepted = _read(root, MEASURE)
    _commit_file(root, MEASURE, accepted + b"# first edit\n")
    assert _start_noop(capsys)[0] == vc.PAUSED
    hold = _hold_on(root, PACK_REF)
    # an agent edits the file again: it still differs from the accepted bytes
    second = accepted + b"# second edit\n"
    _commit_file(root, MEASURE, second)
    code, body = _start_noop(capsys)
    assert code == vc.PAUSED and body["code"] == "RESEARCH-HOLD-OPEN", body
    assert "files[%s] still loosens" % MEASURE in body["reason"]
    assert body["decision"]["id"] == hold["id"]
    assert _ev(root, events.HOLD_LIFTED) == [] and _ev(root, events.RUN_START) == []
    assert _question(root, hold["data"]["question"])["status"] == "open"
    assert _card_status(root, hold["data"]["card"]) == "open"
    # nor can a direct call lift it: lift_moot checks the re-check itself
    conn = _conn(root)
    try:
        now = contract.loosen_recheck(conn, root, RB)
        assert now["refs"][PACK_REF]["loosened"] == ["files[%s]" % MEASURE]
        with pytest.raises(ValueError, match="still loosens"):
            holds.lift_moot(conn, root, hold=hold["id"], recheck=now, session="agent")
        assert holds.open_holds(conn, PACK_REF)[0]["id"] == hold["id"]
    finally:
        conn.close()
    assert _ev(root, events.HOLD_LIFTED) == []
    # the owner grants the new bytes at a terminal: then the start goes on, lifted by the grant
    new_sha = hashlib.sha256(second).hexdigest()
    code, body = _grant(capsys, monkeypatch, RB, "--runbook", RB, "loosen", "--set",
                        "files[%s]=%s" % (MEASURE, util.canonical_json(new_sha)))
    assert code == vc.PASS, body
    code, body = _start_noop(capsys)
    assert code == vc.PASS, body
    lifted = _ev(root, events.HOLD_LIFTED, PACK_REF)
    assert len(lifted) == 1 and lifted[0]["data"]["answer"] == "loosen"
    assert lifted[0]["data"]["decision"] and "moot_lifted" not in body["start"]


@needs_jail
def test_swapping_one_loosening_for_another_opens_a_new_hold(project, capsys, monkeypatch):
    """The named field goes back, another file of the pack comes in: the old hold is moot, but
    the start is still refused CONTRACT-LOOSEN-UNGRANTED and a new hold names the new field."""
    root = _real_project(project, monkeypatch)
    _land_baseline(root)
    accepted = _read(root, MEASURE)
    _commit_file(root, MEASURE, accepted + b"# edit\n")
    assert _start_noop(capsys)[0] == vc.PAUSED
    first = _hold_on(root, PACK_REF)
    _commit_file(root, MEASURE, accepted)
    _commit_file(root, "pack/notes.txt", b"a new file in the pack folder\n")
    code, body = _start_noop(capsys)
    assert code == vc.PAUSED and body["code"] == "CONTRACT-LOOSEN-UNGRANTED", body
    assert _ev(root, events.RUN_START) == []
    lifted = _ev(root, events.HOLD_LIFTED, PACK_REF)
    assert [e["data"]["hold"] for e in lifted] == [first["id"]]
    assert lifted[0]["data"]["answer"] == "moot"
    second = _hold_on(root, PACK_REF)
    assert second["id"] != first["id"]
    assert second["data"]["fields"] == ["files[pack/notes.txt]"]
    conn = _conn(root)
    try:
        assert [h["id"] for h in holds.open_holds(conn, PACK_REF)] == [second["id"]]
    finally:
        conn.close()
    # and that hold refuses the next start the same way
    code, body = _start_noop(capsys)
    assert code == vc.PAUSED and body["code"] == "RESEARCH-HOLD-OPEN"
    assert "files[pack/notes.txt] still loosens" in body["reason"]


# ------------------------------------------------------------------------------ fail closed
MEASURED = {"baseline": 1, "pack_sha256": "p" * 64}


def _loosen_hold(root, ref, detail, fields=None, measured=MEASURED):
    """A loosen hold measured against baseline 1 (the one _recheck reports) unless told."""
    conn = _conn(root)
    try:
        return holds.open_hold(conn, root, ref=ref, gate="loosen",
                               code=holds.HOLD_CODES["loosen"], detail=detail, op="op-001",
                               bound=None, session="t", fields=fields, measured=measured)
    finally:
        conn.close()


def _recheck(ref, loosened=(), *, baseline=1, pack="p" * 64, more=None):
    refs = {ref: {"loosened": list(loosened), "changed": []}}
    refs.update(more or {})
    return {"refs": refs, "pack_sha256": pack, "contract_sha256": "c" * 64,
            "files_sha256": "f" * 64, "baseline": baseline}


def _lifts(root):
    conn = _conn(root)
    try:
        return db.events(conn, kind=events.HOLD_LIFTED, limit=10 ** 9)
    finally:
        conn.close()


def test_lift_moot_refuses_every_case_it_cannot_prove(project):
    cli.main(["init"])
    plain_op("intent", "bar")
    ref = "contract:%s#-" % RB
    # a hold that names no field is never moot
    bare = _loosen_hold(project, ref, "a loosen hold with no field line")
    conn = _conn(project)
    try:
        with pytest.raises(ValueError, match="names no field"):
            holds.lift_moot(conn, project, hold=bare["id"], recheck=_recheck(ref), session="a")
        assert _lifts(project) == []
        # the owner lifts that one, so a second loosen hold can open on the ref
        holds.lift_hold(conn, project, hold=bare["id"], decision="dec-x", answer="loosen",
                        session="owner")
    finally:
        conn.close()
    named = _loosen_hold(project, ref, 'files[pack/a.txt]: accepted null, new "%s"' % ("a" * 64),
                         fields=["files[pack/a.txt]"])
    budget = _loosen_hold_of_gate(project, ref, "budget")
    conn = _conn(project)
    try:
        # a re-check that does not cover the hold's ref
        with pytest.raises(ValueError, match="does not cover"):
            holds.lift_moot(conn, project, hold=named["id"],
                            recheck=_recheck("contract:other.yaml#-"), session="a")
        # a re-check that still finds the field loosened
        with pytest.raises(ValueError, match="still loosens"):
            holds.lift_moot(conn, project, hold=named["id"],
                            recheck=_recheck(ref, ["files[pack/a.txt]"]), session="a")
        # another gate never lifts as moot
        with pytest.raises(ValueError, match="only a loosen hold"):
            holds.lift_moot(conn, project, hold=budget["id"], recheck=_recheck(ref), session="a")
        assert len(_lifts(project)) == 1                   # only the owner's lift above
        # positive: the re-check covers the ref and the named field is gone
        got = holds.lift_moot(conn, project, hold=named["id"], recheck=_recheck(ref), session="a")
        assert got["lifted"]["answer"] == "moot" and got["lifted"]["decision"] is None
        assert [h["id"] for h in holds.open_holds(conn, ref)] == [budget["id"]]
        # a second call returns the hold as lifted and records nothing more
        again = holds.lift_moot(conn, project, hold=named["id"], recheck=_recheck(ref), session="a")
        assert again["lifted"]["event"] == got["lifted"]["event"]
        assert len(_lifts(project)) == 2
    finally:
        conn.close()


def _loosen_hold_of_gate(root, ref, gate):
    conn = _conn(root)
    try:
        return holds.open_hold(conn, root, ref=ref, gate=gate, code=holds.HOLD_CODES[gate],
                               detail="held", op="op-001", bound=None, session="t")
    finally:
        conn.close()


def test_a_recheck_that_cannot_run_keeps_the_hold(rec, capsys):
    """The `rec` project's runbook is no pack the loader reads, so the re-check raises; the hold
    stays and the start is refused RESEARCH-HOLD-OPEN, as before t-306."""
    hold = _loosen_hold(rec.root, PACK_REF, "research.stop.slots: accepted 2, new 3")
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.PAUSED and body["code"] == "RESEARCH-HOLD-OPEN", body
    assert "could not run" in body["reason"] and body["decision"]["id"] == hold["id"]
    assert _ev(rec.root, events.HOLD_LIFTED) == [] and rec.fake.starts == []


def test_a_recheck_that_still_loosens_keeps_the_hold_with_the_fake_runner(rec, capsys,
                                                                          monkeypatch):
    """Without the jail: the start's re-check is read through contract.loosen_recheck, so a
    stand-in shows the decision both ways; open_run is the fake runner's."""
    hold = _loosen_hold(rec.root, PACK_REF, "research.stop.slots: accepted 2, new 3")
    seen = []

    def recheck(conn, root, runbook, task=None):
        seen.append((runbook, task))
        return _recheck(PACK_REF, loosened)

    monkeypatch.setattr(contract, "loosen_recheck", recheck)
    loosened = ["research.stop.slots"]
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.PAUSED and body["code"] == "RESEARCH-HOLD-OPEN", body
    assert "research.stop.slots still loosens" in body["reason"]
    assert rec.fake.starts == []
    loosened = ["research.budget.usd"]          # another field loosens: not the hold's
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.PASS, body
    assert body["start"]["moot_lifted"] == [hold["id"]] and len(rec.fake.starts) == 1
    assert seen == [(RB, None), (RB, None)]
    lifted = _ev(rec.root, events.HOLD_LIFTED, PACK_REF)
    assert len(lifted) == 1 and lifted[0]["data"]["answer"] == "moot"
    assert lifted[0]["data"]["recheck"]["loosened"] == ["research.budget.usd"]


# ------------------------------------------------------------------------------ the live case
@needs_jail
@needs_dev_files("packs/refpack", "packs/refpack-toyzip")
def test_the_refpack_readme_hold_lifts_as_moot_once_the_file_is_reverted(project, capsys,
                                                                         monkeypatch):
    """The live shape: the refpack baseline accepts README.md; one line of it is edited (as t-297
    did), the noop control start opens a loosen hold on contract:packs/refpack/improve.yaml#-
    naming files[packs/refpack/README.md]; once the file is back at the accepted bytes the same
    start lifts the hold as moot, closes its question, and starts with no owner grant."""
    root = packkit.pack_project(project, slots=1)
    cli.main(["init"])
    plain_op("intent", "bar")
    _real_project_patches(monkeypatch, root)
    _land_baseline(root, REFPACK_RB)
    readme = "packs/refpack/README.md"
    accepted = _read(root, readme)
    _commit_file(root, readme, accepted.replace(b"defaults both runbooks share",
                                                b"defaults the runbooks share", 1))
    argv = ["--via", "autoresearch"]
    code, body = _start_noop(capsys, REFPACK_RB, *argv)
    assert code == vc.PAUSED and body["code"] == "CONTRACT-LOOSEN-UNGRANTED", body
    ref = "contract:%s#-" % REFPACK_RB
    hold = _hold_on(root, ref)
    assert hold["data"]["fields"] == ["files[%s]" % readme]
    assert 'accepted "%s"' % hashlib.sha256(accepted).hexdigest() in hold["data"]["detail"]
    _commit_file(root, readme, accepted)
    code, body = _start_noop(capsys, REFPACK_RB, *argv)
    assert code == vc.PASS, body
    lift = _ev(root, events.HOLD_LIFTED, ref)
    assert len(lift) == 1 and lift[0]["data"]["answer"] == "moot"
    assert _question(root, hold["data"]["question"])["status"] == "resolved"
    assert _ev(root, events.GRANT) == []


def _real_project_patches(monkeypatch, root):
    """test_research_cli._real_project's stand-ins, for a project made by research_packkit."""
    from alpaca.research import controls, runner
    from alpaca.tests.test_research_cli import FakeRunner
    monkeypatch.setattr(runner, "_check_session_runtime",
                        lambda runtime, root: ("control" if runtime == "control" else "claude",
                                               "test"))
    monkeypatch.setattr(runner, "launch", FakeRunner(root).launch)
    monkeypatch.setattr(controls, "burn_in", lambda conn, sha: {"state": "done", "run": "rr-0",
                                                                   "rounds": 2, "of": 2})
    for name in owner.AGENT_MARKERS:
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------------------ check round s1
# (check-adversarial-s1.md B1, M1, M3): a re-check is moot evidence only against the baseline the
# hold was measured against, a legacy detail line is never cut short, and the lift waits for the
# start's other checks.
@needs_jail
def test_a_changed_runbook_id_keeps_a_still_loosened_hold(project, capsys, monkeypatch):
    """B1: the frozen file stays edited and only the runbook's `id:` changes; the baseline lookup
    (id and path) finds none, the pack diff is empty, and the hold must still refuse the start."""
    root = _real_project(project, monkeypatch)
    _land_baseline(root)
    accepted = _read(root, MEASURE)
    _commit_file(root, MEASURE, accepted + b"# still edited\n")
    assert _start_noop(capsys)[1]["code"] == "CONTRACT-LOOSEN-UNGRANTED"
    hold = _hold_on(root, PACK_REF)
    text = _read(root, RB)
    assert b"id: loop-improve\n" in text
    _commit_file(root, RB, text.replace(b"id: loop-improve\n", b"id: loop-improve-x\n", 1))
    conn = _conn(root)
    try:
        now = contract.loosen_recheck(conn, root, RB)
    finally:
        conn.close()
    assert now["baseline"] is None and now["refs"][PACK_REF]["loosened"] == []
    for _ in range(2):
        code, body = _start_noop(capsys)
        assert code == vc.PAUSED and body["code"] == "RESEARCH-HOLD-OPEN", body
        assert "not baseline %s" % hold["data"]["measured"]["baseline"] in body["reason"]
        assert body["decision"]["id"] == hold["id"]
    assert _ev(root, events.HOLD_LIFTED) == [] and _ev(root, events.RUN_START) == []
    assert _question(root, hold["data"]["question"])["status"] == "open"
    assert _card_status(root, hold["data"]["card"]) == "open"
    # the id goes back with the file still edited: the baseline is found, the field still loosens
    _commit_file(root, RB, text)
    code, body = _start_noop(capsys)
    assert code == vc.PAUSED and "files[%s] still loosens" % MEASURE in body["reason"], body
    assert _ev(root, events.HOLD_LIFTED) == []


def test_lift_moot_needs_the_baseline_the_hold_was_measured_against(project):
    """B1, unit form: a re-check with no baseline, or another baseline, proves nothing about the
    hold's fields; lift_moot refuses and records nothing."""
    cli.main(["init"])
    plain_op("intent", "bar")
    hold = _loosen_hold(project, PACK_REF, 'files[pack/a]: accepted "%s", new "%s"'
                        % ("a" * 64, "b" * 64), fields=["files[pack/a]"])
    conn = _conn(project)
    try:
        for baseline in (None, 2):
            with pytest.raises(ValueError, match="not baseline 1"):
                holds.lift_moot(conn, project, hold=hold["id"],
                                recheck=_recheck(PACK_REF, baseline=baseline), session="agent")
            assert holds.moot_reason(conn, hold["id"], _recheck(PACK_REF, baseline=baseline))
        assert _lifts(project) == []
        assert holds.moot_reason(conn, hold["id"], _recheck(PACK_REF)) is None
    finally:
        conn.close()


def _baseline_event(root, runbook=RB):
    conn = _conn(root)
    try:
        return events.emit(conn, events.CONTRACT, run=None, ref="loop-improve", op=None,
                           session="t", actor="alpaca-research",
                           data={"runbook": runbook, "pack_sha256": "p" * 64})["id"]
    finally:
        conn.close()


def test_a_hold_from_before_t306_is_measured_against_the_baseline_before_it(project):
    """A hold with no `measured` (event 108083's shape) was measured against the newest
    research-contract event of its runbook path recorded before it; a baseline landed after the
    hold, or none at all, never makes it moot."""
    cli.main(["init"])
    plain_op("intent", "bar")
    detail = 'files[pack/a]: accepted "%s", new "%s"' % ("a" * 64, "b" * 64)
    # no baseline before the hold: a re-check with none is still not moot
    bare = _loosen_hold(project, PACK_REF, detail, measured=None)
    assert "measured" not in bare or bare["measured"] is None
    conn = _conn(project)
    try:
        assert "no baseline" in holds.moot_reason(conn, bare["id"], _recheck(PACK_REF,
                                                                           baseline=None))
        holds.lift_hold(conn, project, hold=bare["id"], decision="dec-x", answer="loosen",
                        session="owner")
    finally:
        conn.close()
    other = _baseline_event(project, runbook="other.yaml")
    before = _baseline_event(project)
    hold = _loosen_hold(project, PACK_REF, detail, measured=None)
    after = _baseline_event(project)
    conn = _conn(project)
    try:
        for wrong in (None, other, after):
            assert holds.moot_reason(conn, hold["id"], _recheck(PACK_REF, baseline=wrong)), wrong
            with pytest.raises(ValueError):
                holds.lift_moot(conn, project, hold=hold["id"],
                                recheck=_recheck(PACK_REF, baseline=wrong), session="agent")
        got = holds.lift_moot(conn, project, hold=hold["id"],
                              recheck=_recheck(PACK_REF, baseline=before), session="agent")
        assert got["lifted"]["answer"] == "moot"
    finally:
        conn.close()


def test_a_hold_measured_with_no_baseline_needs_the_same_pack(project):
    """A task-layer hold at a first block has no baseline; it is moot only while the pack is the
    one it was measured against, so the pack cannot be edited to take the loosening in."""
    cli.main(["init"])
    plain_op("intent", "bar")
    ref = "contract:%s#t-001" % RB
    hold = _loosen_hold(project, ref, "research.budget.usd: accepted 10, new 20",
                        fields=["research.budget.usd"],
                        measured={"baseline": None, "pack_sha256": "p" * 64})
    conn = _conn(project)
    try:
        why = holds.moot_reason(conn, hold["id"], _recheck(ref, baseline=None, pack="q" * 64))
        assert why and "pack is not the one" in why
        assert holds.moot_reason(conn, hold["id"], _recheck(ref, baseline=1)) is not None
        assert holds.moot_reason(conn, hold["id"], _recheck(ref, baseline=None)) is None
    finally:
        conn.close()


def test_a_named_field_loosened_in_another_layer_keeps_the_hold(project):
    """A task-layer hold whose field the pack layer now loosens (the pack was edited to take the
    override in) is not moot."""
    cli.main(["init"])
    plain_op("intent", "bar")
    ref = "contract:%s#t-001" % RB
    hold = _loosen_hold(project, ref, "research.budget.usd: accepted 10, new 20",
                        fields=["research.budget.usd"])
    conn = _conn(project)
    try:
        more = {PACK_REF: {"loosened": ["research.budget.usd"], "changed": []}}
        with pytest.raises(ValueError, match="research.budget.usd still loosens"):
            holds.lift_moot(conn, project, hold=hold["id"], recheck=_recheck(ref, more=more),
                            session="agent")
        assert _lifts(project) == []
    finally:
        conn.close()


def test_held_fields_never_cuts_a_legacy_field_short():
    """M1: a hold with no `fields` is read from its detail; a file path that holds ': accepted '
    is named whole (and its shorter split too, which can only keep a hold open)."""
    path = "files[pack/x: accepted y, new z.txt]"
    detail = '%s: accepted "%s", new "%s"' % (path, "a" * 64, "b" * 64)
    got = holds.held_fields({"detail": detail})
    assert path in got and "files[pack/x" in got


def test_a_moot_hold_stays_open_when_a_later_start_check_refuses(rec, capsys, monkeypatch):
    """M3: the start lifts a moot hold only after checks 3 and 4; a start the owner's stop
    refuses leaves the hold, its question and its card open."""
    hold = _loosen_hold(rec.root, PACK_REF, "research.stop.slots: accepted 2, new 3")
    monkeypatch.setattr(contract, "loosen_recheck",
                        lambda conn, root, runbook, task=None: _recheck(PACK_REF))
    monkeypatch.setattr(owner, "_stopped_run", lambda conn, runbook: "rr-009")
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code != vc.PASS and body["code"] == "RESEARCH-RUNBOOK-STOPPED", body
    assert _ev(rec.root, events.HOLD_LIFTED) == [] and rec.fake.starts == []
    conn = _conn(rec.root)
    try:
        assert [h["id"] for h in holds.open_holds(conn, PACK_REF)] == [hold["id"]]
    finally:
        conn.close()
    monkeypatch.setattr(owner, "_stopped_run", lambda conn, runbook: None)
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.PASS and body["start"]["moot_lifted"] == [hold["id"]], body
    assert len(_ev(rec.root, events.HOLD_LIFTED, PACK_REF)) == 1


def test_a_hold_naming_two_fields_stays_open_while_one_still_loosens(project):
    """`fields` names one field and the detail two: the hold is moot only when neither loosens."""
    cli.main(["init"])
    plain_op("intent", "bar")
    detail = ('files[pack/a]: accepted "%s", new "%s"\nfiles[pack/b]: accepted "%s", new "%s"'
              % ("a" * 64, "b" * 64, "c" * 64, "d" * 64))
    hold = _loosen_hold(project, PACK_REF, detail, fields=["files[pack/a]"])
    assert holds.held_fields(hold) == ["files[pack/a]", "files[pack/b]"]
    conn = _conn(project)
    try:
        with pytest.raises(ValueError, match="files\\[pack/b\\] still loosens"):
            holds.lift_moot(conn, project, hold=hold["id"],
                            recheck=_recheck(PACK_REF, ["files[pack/b]"]), session="agent")
        assert [h["id"] for h in holds.open_holds(conn, PACK_REF)] == [hold["id"]]
        assert holds.moot_reason(conn, hold["id"], _recheck(PACK_REF)) is None
    finally:
        conn.close()


def test_a_field_inside_a_loosened_item_still_loosens(project):
    """B2 (check round s2): pack_diff reports a leaf under an item that entered or left as the
    item alone, so a held leaf counts as loosened when its item is, and a held item counts as
    loosened when a leaf inside it is. A path that only shares a prefix does not."""
    cli.main(["init"])
    plain_op("intent", "bar")
    leaf = _loosen_hold(project, PACK_REF, "research.metrics[value].target: accepted 10, new 50",
                        fields=["research.metrics[value].target"])
    task_ref = "contract:%s#t-001" % RB  # one open loosen hold per ref, so the item's is a task's
    item = _loosen_hold(project, task_ref, "research.metrics[value]: accepted {}, new null",
                        fields=["research.metrics[value]"])
    conn = _conn(project)
    try:
        renamed = _recheck(PACK_REF, ["research.metrics[value]", "research.metrics[value2]"])
        why = holds.moot_reason(conn, leaf["id"], renamed)
        assert why is not None and "research.metrics[value].target still loosens" in why, why
        assert "loosened as research.metrics[value]" in why and "value2" not in why, why
        with pytest.raises(ValueError, match="still loosens"):
            holds.lift_moot(conn, project, hold=leaf["id"], recheck=renamed, session="agent")
        inner = _recheck(task_ref, ["research.metrics[value].target"])
        assert "research.metrics[value] still loosens" in holds.moot_reason(conn, item["id"],
                                                                            inner)
        whole = _recheck(PACK_REF, ["research.metrics"])
        assert holds.moot_reason(conn, leaf["id"], whole) is not None
        # a sibling item or a longer name that only shares the prefix leaves the hold moot
        assert holds.moot_reason(conn, leaf["id"],
                                 _recheck(PACK_REF, ["research.metrics[value2]",
                                                     "research.metrics[value].targetx"])) is None
        assert holds.moot_reason(conn, item["id"],
                                 _recheck(task_ref, ["research.metrics[value2].target"])) is None
        assert [h["id"] for h in holds.open_holds(conn, PACK_REF)] == [leaf["id"]]
        assert [h["id"] for h in holds.open_holds(conn, task_ref)] == [item["id"]]
    finally:
        conn.close()
    assert _lifts(project) == []


@needs_jail
def test_a_loosened_target_under_a_renamed_metric_keeps_its_hold(project, capsys, monkeypatch):
    """B2 end to end: the target goes from 10 to 50 and opens a hold on the target leaf; the
    agent then renames the metric and keeps target 50. The re-check lists the target only as its
    item, research.metrics[value], so the old hold must stay open, its question unanswered, and no
    run starts."""
    root = _real_project(project, monkeypatch)
    _land_baseline(root)
    text = _read(root, RB)
    assert b"      target: 10\n" in text and b"    - id: value\n" in text
    _commit_file(root, RB, text.replace(b"      target: 10\n", b"      target: 50\n", 1))
    code, body = _start_noop(capsys)
    assert code == vc.PAUSED and body["code"] == "CONTRACT-LOOSEN-UNGRANTED", body
    hold = _hold_on(root, PACK_REF)
    assert hold["data"]["fields"] == ["research.metrics[value].target"]
    _commit_file(root, RB, _read(root, RB).replace(b"    - id: value\n", b"    - id: value2\n", 1))
    code, body = _start_noop(capsys)
    assert code == vc.PAUSED and body["code"] == "RESEARCH-HOLD-OPEN", body
    assert "research.metrics[value].target still loosens" in body["reason"], body
    assert _ev(root, events.HOLD_LIFTED) == [] and _ev(root, events.RUN_START) == []
    assert _question(root, hold["data"]["question"])["status"] == "open"
    assert _card_status(root, hold["data"]["card"]) == "open"
    conn = _conn(root)
    try:
        now = contract.loosen_recheck(conn, root, RB)
        assert "research.metrics[value]" in now["refs"][PACK_REF]["loosened"]
        assert "research.metrics[value].target" not in now["refs"][PACK_REF]["loosened"]
        assert [h["id"] for h in holds.open_holds(conn, PACK_REF)] == [hold["id"]]
    finally:
        conn.close()
