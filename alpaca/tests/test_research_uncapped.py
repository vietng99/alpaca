"""t-297: research runs are uncapped by default (owner decision dec-36f614298507,
design/research-engine/in-session-runtime.md section 21).

Done bar and fail cases of the task contract, each with a positive and a negative path:
- A run with no budget in the runbook or the project has no tokens, USD or wall cap and never
  opens a budget hold: a loop run that spends far past the old 3000000-token default keeps
  launching every role (test_an_uncapped_run_spends_far_past_the_old_default_and_never_holds).
- A cap set on purpose in a runbook, or raised by an owner grant, still holds as before
  (test_a_cap_set_on_purpose_still_holds_a_loop_run, test_a_cap_set_on_purpose_holds_until_a_grant).
- Spend is still priced, booked and shown in the state fold and the report for every launch.
- packs/refpack carries no default cap, and dropping a cap that a pre-t-297 baseline took from
  the pack defaults file is free, so a refpack start needs no loosen grant
  (test_a_refpack_start_with_the_caps_removed_needs_no_grant); dropping a cap set on purpose (in
  the runbook, or in a defaults file since t-297) is a loosening that needs the owner's loosen
  grant (test_a_runbook_cap_dropped_by_an_edit_needs_the_owner_loosen_grant).
- A wall cap set on purpose holds until a `--wall` grant raises it
  (test_a_wall_cap_set_on_purpose_holds_until_a_wall_grant).
- The three hold reasons of spend.check: (a) unpriced spend holds only under a dollar or token
  cap; (b) the project budget posture never holds a research launch, and research spend still
  books to the project allowance; (c) a verifier launch is never held.
- A cap of zero, below zero or null is refused by the checker, never read as no cap.
All data is synthetic.
"""
from __future__ import annotations

import datetime
import os

import pytest
import yaml

from alpaca import cli, db, decisions, ops, runbook, util
from alpaca.research import contract, errors, events, jail, owner, report, runner, spend, state
from alpaca.tests import research_loopkit as kit
from alpaca.tests import research_packkit as packkit
from alpaca.tests.conftest import REPO
from alpaca.tests.devfiles import names_as_data, needs_dev_files
from alpaca.tests.plain_op import plain_op

needs_jail = pytest.mark.skipif(not jail.available()[0],
                                reason="bubblewrap is not available on this host")

OLD_DEFAULT_TOKENS = 3000000          # packs/refpack/pack.yaml before t-297
OLD_CAPS = "  budget:\n    usd: 40\n    tokens: 3000000\n    wall: 14400\n"


# ------------------------------------------------------------------------------ helpers
def _record(project):
    cli.main(["init"])
    plain_op("intent", "bar")
    return db.connect(project)


def _fake_run(conn, run="rr-001", budget=None):
    events.emit(conn, events.RUN_START, run=run, ref=run, op="op-001", session="t",
                data={"mode": "improve", "budget": budget if budget is not None else
                      {"usd": None, "tokens": None, "wall": None},
                      "k": 2, "n": 1, "control": None, "base_commit": "b" * 40})
    return ops.add_task(conn, "op-001", "attempt", "attempt",
                        extra={"research": {"run": run, "role": "attempt", "key": run + ":r1:s0",
                                            "round": 1, "slot": 0}})


def _end(conn, run, task, key, *, tokens, priced=True):
    events.emit(conn, events.LAUNCH_END, run=run, ref=task, op=None, session="s", code=0,
                data={"key": key, "tokens": tokens, "priced": priced, "outcome": "ok"})


def _project_hold(conn, root):
    """Spend the project allowance past its reserve (fallback totals: small 100, reserve 10)."""
    from alpaca.resilience import budget
    budget.spend(conn, 1000.0, session="other-work", ref="not-research")
    assert budget.posture(conn, root=root) == budget.HOLD


def _grant_budget(conn, run, terms):
    events.emit(conn, events.GRANT, run=None, ref=run, op=None, session="owner", actor="owner",
                data={"ref": run, "gate": "budget", "confirmed": "terminal", "terms": terms})


# ------------------------------------------------------------------------------ the pack
@needs_dev_files("packs/refpack")
def test_the_reference_pack_sets_no_default_cap():
    """packs/refpack/pack.yaml and both runbooks carry no research.budget after t-297."""
    for name in ("pack.yaml", "improve.yaml", "repair.yaml"):
        with open(os.path.join(REPO, "packs", "refpack", name), encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        assert "budget" not in (data.get("research") or {}), name


# ------------------------------------------------------------------------------ the format
def test_a_runbook_with_no_budget_checks_clean_and_loads_with_no_cap(project):
    root = kit.make_project(project, budget=False)
    got = runbook.check(os.path.join(root, kit.RUNBOOK))
    assert got["errors"] == [], got["errors"]
    frozen = contract.from_block(contract.preview(root, kit.RUNBOOK)["contract"])
    assert (frozen.budget_usd, frozen.budget_tokens, frozen.budget_wall_s) == (None, None, None)


def test_a_runbook_with_a_budget_loads_the_caps_it_sets(project):
    """The negative path of the test above: a cap set on purpose is read as that cap."""
    root = kit.make_project(project, budget=True, usd=7)
    frozen = contract.from_block(contract.preview(root, kit.RUNBOOK)["contract"])
    assert (frozen.budget_usd, frozen.budget_tokens, frozen.budget_wall_s) == (7.0, 1000000, None)


@pytest.mark.parametrize("cap,code", [
    ("usd: 0", "RANGE"), ("usd: -5", "RANGE"), ("tokens: 0", "RANGE"), ("tokens: -1", "RANGE"),
    ("wall: 0", "RANGE"), ("usd: null", "FIELD-TYPE"), ("tokens: null", "FIELD-TYPE"),
])
def test_a_cap_of_zero_below_zero_or_null_is_refused_never_read_as_no_cap(project, cap, code):
    root = kit.make_project(project, budget=False)
    path = os.path.join(root, kit.RUNBOOK)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    text = text.replace("  disk:", "  budget:\n    %s\n  disk:" % cap)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    found = {(e["code"], e["where"]) for e in runbook.check(path)["errors"]}
    assert (code, "research.budget.%s" % cap.split(":")[0]) in found, found


@pytest.mark.parametrize("cap,want", [
    ("tokens: 5000", (None, 5000, None)), ("wall: 600", (None, None, 600)), ("usd: 3", (3.0, None, None)),
])
def test_a_budget_block_with_one_cap_checks_clean_and_leaves_the_others_uncapped(project, cap, want):
    """Every cap of research.budget is optional (runbook.BUDGET_KEYS): a block that sets one cap
    checks clean, and the caps it leaves out load as None (check round s1, minor m10)."""
    root = kit.make_project(project, budget=False)
    path = os.path.join(root, kit.RUNBOOK)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace("  disk:", "  budget:\n    %s\n  disk:" % cap))
    kit.git(root, "commit", "-q", "-am", "one cap")
    got = runbook.check(path)
    assert got["errors"] == [], got["errors"]
    frozen = contract.from_block(contract.preview(root, kit.RUNBOOK)["contract"])
    assert (frozen.budget_usd, frozen.budget_tokens, frozen.budget_wall_s) == want


# ------------------------------------------------------------------------------ spend.check
def test_an_uncapped_run_is_never_held_and_its_spend_is_still_counted(project):
    conn = _record(project)
    task = _fake_run(conn)
    spend.book(conn, "rr-001", task, cost_usd=5000.0, tokens=50000000, session="s")
    _end(conn, "rr-001", task, "k1", tokens=50000000)
    _end(conn, "rr-001", task, "k2", tokens=7, priced=False)          # (a) unpriced spend
    got = spend.check(conn, project, "rr-001", role="proposer")
    assert got.posture == "GO" and got.reason == ""
    assert (got.usd_cap, got.tokens_cap, got.wall_cap_s) == (None, None, None)
    assert got.usd_spent == 5000.0 and got.tokens_spent == 50000007 and got.unpriced == 1
    for role in ("proposer", "implementer", "notes", "auditor", "verifier", "reproducer-writer",
                 "reproducer-reviewer"):
        assert spend.check(conn, project, "rr-001", role=role).posture == "GO", role
    with spend.reserve(conn, project, "rr-001", "k3", role="implementer", runtime="claude",
                       slots=2) as (chk, share):
        assert chk.posture == "GO" and share is None                   # no dollar cap, no share


def test_unpriced_spend_holds_only_under_a_dollar_or_token_cap(project):
    conn = _record(project)
    task = _fake_run(conn, budget={"usd": None, "tokens": None, "wall": 3600})
    _end(conn, "rr-001", task, "k1", tokens=9, priced=False)
    assert spend.check(conn, project, "rr-001", role="proposer").posture == "GO"   # a wall cap only
    for run, cap in (("rr-002", {"usd": 50.0, "tokens": None, "wall": None}),
                     ("rr-003", {"usd": None, "tokens": 10 ** 9, "wall": None})):
        t = _fake_run(conn, run=run, budget=cap)
        _end(conn, run, t, run + ":k1", tokens=9, priced=False)
        got = spend.check(conn, project, run, role="proposer")
        assert got.posture == "HOLD" and "nobody priced" in got.reason, run


def test_the_project_posture_never_holds_a_research_run_and_spend_still_books(project):
    """The orchestrator's point 1 (t-297 addendum): with the project allowance spent past its
    reserve, a research launch is GO and its spend still books to the project through
    budget.spend; the posture is reported, not held on."""
    from alpaca.resilience import budget
    conn = _record(project)
    task = _fake_run(conn)
    _project_hold(conn, project)
    got = spend.check(conn, project, "rr-001", role="proposer")
    assert got.posture == "GO" and got.project_posture == "HOLD" and "project" not in got.reason
    assert spend.check(conn, project, None, role="proposer").posture == "GO"
    before = budget.spent(conn)
    spend.book(conn, "rr-001", task, cost_usd=2.5, tokens=10, session="s")
    assert budget.spent(conn) == before + 2.5
    assert spend.totals(conn, "rr-001")["usd_spent"] == 2.5
    # a run cap set on purpose still holds under the same posture
    t = _fake_run(conn, run="rr-002", budget={"usd": 1.0, "tokens": None, "wall": None})
    spend.book(conn, "rr-002", t, cost_usd=2.0, tokens=1, session="s")
    got = spend.check(conn, project, "rr-002", role="proposer")
    assert got.posture == "HOLD" and "dollar cap" in got.reason and "project" not in got.reason


def test_a_cap_set_on_purpose_holds_until_a_grant(project):
    conn = _record(project)
    task = _fake_run(conn, budget={"usd": 10.0, "tokens": 1000, "wall": None})
    assert spend.check(conn, project, "rr-001", role="proposer").posture == "GO"
    spend.book(conn, "rr-001", task, cost_usd=1.0, tokens=1, session="s")
    _end(conn, "rr-001", task, "k1", tokens=5000)
    got = spend.check(conn, project, "rr-001", role="implementer")
    assert got.posture == "HOLD" and "token cap" in got.reason and got.tokens_cap == 1000
    _grant_budget(conn, "rr-001", {"usd": None, "tokens": 10000, "wall": None})
    got = spend.check(conn, project, "rr-001", role="implementer")
    assert got.posture == "GO" and got.tokens_cap == 10000


def test_a_wall_cap_set_on_purpose_holds_until_a_wall_grant(project, monkeypatch):
    """Done bar 2 for the wall cap (check round s1, contract B1): a runbook wall cap of 3600 s
    holds every non-verifier role once the runner has been alive past it, the owner's grant must
    raise it with --wall, and the raised cap lets the run go on. The same five hours on a run with
    no cap never hold (the old pack default was 14400 s)."""
    conn = _record(project)
    _fake_run(conn, budget={"usd": None, "tokens": None, "wall": 3600})
    _fake_run(conn, run="rr-002")
    for run in ("rr-001", "rr-002"):
        events.emit(conn, events.RUNNER, run=run, ref=run, op=None, session="t", data={})
    assert spend.check(conn, project, "rr-001", role="proposer").posture == "GO"
    later = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)).isoformat()
    monkeypatch.setattr(util, "now_iso", lambda: later)
    got = spend.check(conn, project, "rr-001", role="proposer")
    assert got.posture == "HOLD" and "wall-time cap is reached" in got.reason, got
    assert got.wall_cap_s == 3600 and got.wall_s_spent >= 5 * 3600 - 60
    for role in ("implementer", "notes", "auditor"):
        assert spend.check(conn, project, "rr-001", role=role).posture == "HOLD", role
    assert spend.check(conn, project, "rr-001", role="verifier").posture == "GO"
    uncapped = spend.check(conn, project, "rr-002", role="proposer")
    assert uncapped.posture == "GO" and uncapped.wall_cap_s is None and uncapped.wall_s_spent > 14400
    # the reached wall cap must be raised by the grant, and raised above itself
    with pytest.raises(owner.OwnerRefusal):
        owner._check_budget_terms(conn, project, "rr-001", {})
    with pytest.raises(owner.OwnerRefusal):
        owner._check_budget_terms(conn, project, "rr-001", {"wall": 3600})
    after, _given = owner._check_budget_terms(conn, project, "rr-001", {"wall": 36000})
    assert after["wall"] == 36000
    _grant_budget(conn, "rr-001", {"usd": None, "tokens": None, "wall": 36000})
    got = spend.check(conn, project, "rr-001", role="proposer")
    assert got.posture == "GO" and got.wall_cap_s == 36000, got


def test_a_verifier_launch_is_never_held_and_its_spend_still_counts(project):
    conn = _record(project)
    task = _fake_run(conn, budget={"usd": 1.0, "tokens": 1000, "wall": None})
    spend.book(conn, "rr-001", task, cost_usd=3.0, tokens=1, session="s")
    _end(conn, "rr-001", task, "k1", tokens=5000)
    _end(conn, "rr-001", task, "k2", tokens=1, priced=False)
    _project_hold(conn, project)
    got = spend.check(conn, project, "rr-001", role="verifier")
    assert got.posture == "GO" and got.usd_spent == 3.0 and got.tokens_spent == 5001
    with spend.reserve(conn, project, "rr-001", "kv", role="verifier", runtime="claude",
                       slots=2) as (chk, share):
        assert chk.posture == "GO" and share is None
    # negative path: another role of the same run is held
    assert spend.check(conn, project, "rr-001", role="proposer").posture == "HOLD"


def test_an_owner_budget_grant_is_not_refused_on_the_project_posture(project):
    """owner._check_budget_terms no longer refuses a grant with no new caps at a project posture
    of HOLD; the rest of its rules stand (a reached cap must be raised)."""
    conn = _record(project)
    task = _fake_run(conn, budget={"usd": 10.0, "tokens": None, "wall": None})
    _project_hold(conn, project)
    after, given = owner._check_budget_terms(conn, project, "rr-001", {})
    assert after["usd"] == 10.0 and given == {"usd": None, "tokens": None, "wall": None}
    spend.book(conn, "rr-001", task, cost_usd=11.0, tokens=1, session="s")
    with pytest.raises(owner.OwnerRefusal):
        owner._check_budget_terms(conn, project, "rr-001", {})


def test_the_start_check_no_longer_asks_the_project_posture():
    """runner.open_run had a RUN-BUDGET-HOLD refusal on the project posture before t-297."""
    import inspect
    text = inspect.getsource(runner.open_run)
    assert "spend.check(" not in text and "RUN-BUDGET-HOLD" not in text


# ------------------------------------------------------------------------------ the pack diff
def _refpack(root, pack_text):
    names_as_data("packs/refpack/pack.yaml")                     # the copy pack_project made in root
    path = os.path.join(root, "packs", "refpack", "pack.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(pack_text)
    kit.git(root, "add", "-A")
    kit.git(root, "commit", "-q", "-m", "pack edit")


@needs_dev_files("packs/refpack", "packs/refpack-toyzip")
def test_a_refpack_start_with_the_caps_removed_needs_no_grant(project):
    """The record's baselines of the refpack runbooks (events 65028 and 92293, landed before t-297)
    hold the old pack.yaml caps and no `layers` map. Against such a baseline, dropping a cap the
    pack defaults file gave is free (contract._dropped_default_cap in pack_diff), so the next
    freeze of a start lands with no loosen grant. A baseline landed since t-297 records `layers`;
    a cap written there was set on purpose, so dropping it, like raising it, is a loosening that
    needs the owner's grant."""
    root = packkit.pack_project(project, slots=1)
    pack_file = os.path.join(root, "packs", "refpack", "pack.yaml")
    with open(pack_file, encoding="utf-8") as fh:
        uncapped = fh.read()
    assert "budget" not in yaml.safe_load(uncapped)["research"]
    capped = uncapped.replace("  disk:", OLD_CAPS + "  disk:")
    rb = "packs/refpack/improve.yaml"
    conn = _record(root)
    _refpack(root, capped)
    # the pre-t-297 baseline: the event shape contract.land wrote then, with no layer map
    contract.land(conn, dict(contract.preview(root, rb)["pack"], grants=[], layers=None), op=None,
                  via="loader", session="test", actor="research-runner")
    assert "layers" not in contract._baseline_event(conn, "refpack-improve", rb)["data"]
    _refpack(root, uncapped)
    frozen, fields = contract.freeze(conn, root, "rr-002", runbook=rb, task=None, session="test")
    assert frozen.budget_usd is None and frozen.budget_tokens is None and frozen.budget_wall_s is None
    assert not [e for e in events_of(conn, "research-grant")]
    # negative path 1: a cap written in pack.yaml since t-297 is set on purpose; dropping it needs a grant
    _refpack(root, capped)
    contract.freeze(conn, root, "rr-003", runbook=rb, task=None, session="test")    # tightened, no grant
    landed = contract._baseline_event(conn, "refpack-improve", rb)["data"]
    assert landed["layers"]["research.budget.usd"] == "defaults"
    _refpack(root, uncapped)
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, root, "rr-004", runbook=rb, task=None, session="test")
    assert err.value.reason == "CONTRACT-LOOSEN-UNGRANTED"
    assert sorted(e["field"] for e in err.value.loosened) == [
        "research.budget.tokens", "research.budget.usd", "research.budget.wall"]
    # negative path 2: a raised cap against the same baseline is a loosening with no grant
    _refpack(root, capped.replace("usd: 40", "usd: 80"))
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, root, "rr-005", runbook=rb, task=None, session="test")
    assert err.value.reason == "CONTRACT-LOOSEN-UNGRANTED"
    assert [e["field"] for e in err.value.loosened] == ["research.budget.usd"]


def _loosen_grant(conn, root, ref_, terms):
    """An owner decision and the research-grant event of a loosen grant (as owner.grant records)."""
    dec = decisions.record(conn, "done_when", "%s loosens %s" % (ref_, sorted(terms)),
                           ["loosen", "keep the accepted values"], "loosen for %s" % ref_,
                           "the next start of %s may use the granted values" % ref_,
                           pointer="research-grant:%s:loosen" % ref_, actor="owner", session="owner",
                           root=root)
    events.emit(conn, events.GRANT, run=None, ref=ref_, op=None, session="owner", actor="owner",
                data={"ref": ref_, "gate": "loosen", "hold": None, "decision": dec["id"],
                      "terms": {"set": terms}, "note": "test", "confirmed": "terminal", "by": "owner"})


def _drop_runbook_budget(root, usd):
    path = os.path.join(root, kit.RUNBOOK)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    cut = "  budget:\n    usd: %s\n    tokens: 1000000\n" % usd
    assert cut in text
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(cut, ""))
    kit.git(root, "commit", "-q", "-am", "drop the budget")


def test_a_runbook_cap_dropped_by_an_edit_needs_the_owner_loosen_grant(project):
    """Check round s1, design and adversarial B1: a cap the runbook sets is a cap set on purpose.
    An edit that drops it is a loosening of the baseline, so the next freeze is refused with
    CONTRACT-LOOSEN-UNGRANTED until the owner grants the drop; then the run is uncapped."""
    root = kit.make_project(project, budget=True, usd=7)
    conn = _record(root)
    contract.freeze(conn, root, "rr-001", runbook=kit.RUNBOOK, task=None, session="test")
    landed = contract._baseline_event(conn, "loop-improve", kit.RUNBOOK)
    assert landed["data"]["layers"]["research.budget.usd"] == "runbook"
    _drop_runbook_budget(root, 7)
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, root, "rr-002", runbook=kit.RUNBOOK, task=None, session="test")
    assert err.value.reason == "CONTRACT-LOOSEN-UNGRANTED"
    assert sorted(e["field"] for e in err.value.loosened) == ["research.budget.tokens", "research.budget.usd"]
    _loosen_grant(conn, root, contract.ref(kit.RUNBOOK, None),
                  {"research.budget.usd": None, "research.budget.tokens": None})
    frozen, _fields = contract.freeze(conn, root, "rr-002", runbook=kit.RUNBOOK, task=None, session="test")
    assert (frozen.budget_usd, frozen.budget_tokens) == (None, None)


def test_intake_refuses_a_runbook_edit_that_drops_its_cap_with_no_grant(project):
    """The intake side of the same rule (alpaca/intake.py, _research_contract): a runbook edit that
    drops the runbook's own cap is refused as a loosening with no owner grant; the refpack start
    against a pre-t-297 baseline is not (the test below and the refpack test above)."""
    from alpaca import intake
    root = kit.make_project(project, budget=True, usd=7)
    conn = _record(root)
    contract.freeze(conn, root, "rr-001", runbook=kit.RUNBOOK, task=None, session="test")
    got = intake._research_contract(root, conn, os.path.join(root, kit.RUNBOOK))
    assert got["diff"] == []
    _drop_runbook_budget(root, 7)
    with pytest.raises(intake.IntakeError) as err:
        intake._research_contract(root, conn, os.path.join(root, kit.RUNBOOK))
    text = " ".join(err.value.detail)
    assert "CONTRACT-LOOSEN-UNGRANTED" in text and "research.budget.usd" in text, text


@needs_dev_files("packs/refpack", "packs/refpack-toyzip")
def test_a_baseline_with_no_layer_map_frees_only_a_cap_the_runbook_did_not_set(project, tmp_path):
    """A baseline landed before t-297 has no `layers` map (events 65028 and 92293 of the live
    record). pack_diff then frees a dropped cap only when the runbook bytes are the baseline's,
    so the runbook did not set the cap: the refpack case. A runbook edit that drops its own cap
    stays a loosening."""
    root = packkit.pack_project(project, slots=1)
    pack_file = os.path.join(root, "packs", "refpack", "pack.yaml")
    with open(pack_file, encoding="utf-8") as fh:
        uncapped = fh.read()
    rb = "packs/refpack/improve.yaml"
    conn = _record(root)
    _refpack(root, uncapped.replace("  disk:", OLD_CAPS + "  disk:"))
    contract.freeze(conn, root, "rr-001", runbook=rb, task=None, session="test")
    legacy = dict(contract._baseline_event(conn, "refpack-improve", rb)["data"])
    legacy.pop("layers")
    _refpack(root, uncapped)
    diff = contract.pack_diff(contract.preview(root, rb)["pack"], legacy)
    budget_rows = [(d["field"], d["direction"]) for d in diff if ".budget." in d["field"]]
    assert sorted(budget_rows) == [("research.budget.tokens", "free"), ("research.budget.usd", "free"),
                                   ("research.budget.wall", "free")], diff
    # negative path: the loop runbook sets its own cap; with the runbook edited, the drop is loosened
    root2 = kit.make_project(str(tmp_path / "loop"), budget=True, usd=7)
    before = contract.preview(root2, kit.RUNBOOK)["pack"]
    legacy2 = {"block": before["block"], "stages": before["stages"], "knobs": before["knobs"],
               "spec": before["spec"], "files": before["files"], "runbook_sha256": before["runbook_sha256"]}
    _drop_runbook_budget(root2, 7)
    after = contract.preview(root2, kit.RUNBOOK)["pack"]
    rows = [(d["field"], d["direction"]) for d in contract.pack_diff(after, legacy2) if ".budget." in d["field"]]
    assert sorted(rows) == [("research.budget.tokens", "loosened"), ("research.budget.usd", "loosened")]
    # the same runbook bytes as the baseline: a cap the runbook does not set came from the
    # defaults file the baseline names, so its drop is free; with no defaults file it stays loosened
    same = dict(legacy2, runbook_sha256=after["runbook_sha256"])
    rows = [d["direction"] for d in contract.pack_diff(after, same) if ".budget." in d["field"]]
    assert rows == ["loosened", "loosened"], rows
    named = dict(same, defaults={"path": "pack/pack.yaml", "sha256": "0" * 64})
    rows = [d["direction"] for d in contract.pack_diff(after, named) if ".budget." in d["field"]]
    assert rows == ["free", "free"], rows


def events_of(conn, kind):
    return [dict(r) for r in conn.execute("SELECT id, ref, data FROM events WHERE kind=?",
                                         (kind,)).fetchall()]


# ------------------------------------------------------------------------------ loop runs
class _Big(kit.Agents):
    """Every launch spends 2000000 tokens and 40 dollars."""
    tokens = 2000000
    cost_usd = 40.0


class _Unpriced(kit.Agents):
    """Every launch spends 2000000 tokens that no rate card prices. Its research-launch names a
    paid runtime: a control launch spends nothing and is never counted unpriced (spend.totals)."""
    tokens = 2000000
    priced = False

    def _launch(self, spec, conn):
        return super()._launch(dict(spec, runtime="claude"), conn)


def _start(project, monkeypatch, agents_cls, *, budget, rounds=2, slots=2, project_hold=False):
    root = kit.make_project(project, rounds=rounds, slots=slots, budget=budget)
    agents = agents_cls(root)
    kit.install(monkeypatch, root, agents)
    if project_hold:
        conn = db.connect(root)
        try:
            _project_hold(conn, root)
        finally:
            conn.close()
    got = kit.start(root)
    return root, got["run"], agents


def _holds(root, run):
    return [e["data"] for e in kit.events_of(root, events.HOLD, run)]


def _why(root, run):
    """The run's finalize events and the tail of its runner log, for a failed assertion."""
    log = os.path.join(root, ".alpaca", "research", run, "runner.log")
    tail = open(log, encoding="utf-8").read()[-2000:] if os.path.exists(log) else ""
    return kit.events_of(root, events.FINALIZE, run), tail


@needs_jail
def test_an_uncapped_run_spends_far_past_the_old_default_and_never_holds(project, monkeypatch):
    """Done bar 1 and the addendum: the project allowance is spent past its reserve before the
    start, the runbook sets no budget, and every launch spends 2000000 tokens and 40 dollars.
    The run starts, launches every role in both rounds, ends at its round limit with no budget
    hold, and books, records and reports every launch's spend."""
    from alpaca.resilience import budget
    root, run, agents = _start(project, monkeypatch, _Big, budget=False, project_hold=True)
    out = runner.launch(root, run, foreground=True, session="test")
    assert [h["code"] for h in _holds(root, run)] == ["RUN-ROUNDS-REACHED"], _why(root, run)
    roles = {c[0] for c in agents.calls}
    assert {"proposer", "implementer", "notes"} <= roles, roles
    rounds = [e["data"]["round"] for e in kit.events_of(root, events.ROUND, run)]
    assert rounds == [0, 1, 2]
    conn = db.connect(root)
    try:
        assert not [r for r in conn.execute("SELECT data FROM events WHERE kind='loop-halt' AND "
                                            "json_extract(data, '$.instrument')='research-budget'")]
        fold = state.fold(conn, run, root)
        bud = fold["budget"]
        assert bud["tokens_spent"] >= len(agents.calls) * 2000000 > OLD_DEFAULT_TOKENS
        assert bud["usd_spent"] == pytest.approx(40.0 * len(agents.calls))
        assert (bud["usd_cap"], bud["tokens_cap"], bud["wall_cap"]) == (None, None, None)
        assert bud["posture"] == budget.HOLD and bud["unpriced"] == 0
        spends = [e for e in db.events(conn, kind=budget.KIND_SPEND, limit=10 ** 9)
                  if e["ref"] != "not-research"]
        assert len(spends) == len(agents.calls)
        built = report.build(conn, root, run)
        text = report.render(built)
        assert "- dollars: %s spent of no cap" % report._cell(built["budget"]["usd_spent"]) in text
        assert "- tokens: %s spent of no cap" % report._cell(built["budget"]["tokens_spent"]) in text
        start = kit.events_of(root, events.RUN_START, run)[0]["data"]
        assert start["budget"] == {"usd": None, "tokens": None, "wall": None}
    finally:
        conn.close()


@needs_jail
def test_an_uncapped_run_with_unpriced_spend_never_holds(project, monkeypatch):
    """Hold reason (a): every launch ends unpriced (the rr-010 q-006 case); with no cap set the
    run is not held, and the unpriced launches are counted in the fold."""
    root, run, agents = _start(project, monkeypatch, _Unpriced, budget=False, rounds=1)
    runner.launch(root, run, foreground=True, session="test")
    assert [h["code"] for h in _holds(root, run)] == ["RUN-ROUNDS-REACHED"], _why(root, run)
    conn = db.connect(root)
    try:
        bud = state.fold(conn, run, root)["budget"]
        assert bud["unpriced"] >= len([c for c in agents.calls]) > 0
        assert bud["usd_spent"] == 0.0 and bud["tokens_spent"] > 0
    finally:
        conn.close()


@needs_jail
def test_a_cap_set_on_purpose_still_holds_a_loop_run(project, monkeypatch):
    """Done bar 2: the same spend under the runbook's own caps (10 USD, 1000000 tokens) opens the
    budget hold after the first launch, as before t-297."""
    root, run, agents = _start(project, monkeypatch, _Big, budget=True)
    out = runner.launch(root, run, foreground=True, session="test")
    held = _holds(root, run)
    assert held and held[0]["code"] == "RUN-BUDGET-HOLD" and held[0]["gate"] == "budget", held
    assert "cap" in held[0]["detail"]
    assert out["code"] == 3
    assert len(agents.calls) < 4
