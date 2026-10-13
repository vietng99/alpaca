"""t-096: the research contract loader (alpaca/research/contract.py) and the task override part of
a task contract (alpaca/taskcontract.py), design/research-engine/runbook-format-3.md sections 2.3,
7, 8 and 14.2, and architecture.md section 3.5.

Done when (the contract of t-096):

  * tests show override diffs recorded: the diff of section 10.5 comes back from merge, preview
    and freeze, and is written into the frozen contract.json and the intent record's bar, one
    entry per leaf with its pack value, task value, class and direction, and a granted loosening
    names its decision;
  * a contract edit during a run is refused and recorded: a task contract edit that changes a
    live run's frozen `research` part raises and records research-contract-drift
    (CONTRACT-EDIT-DURING-RUN), and drift records a pack or store edit on disk once; an edit of
    the frozen copy after the runner start (contract.json, a frozen file) and a swap of
    contract.json, the contract folder, a frozen file or a frozen folder for a link with the same
    bytes are refused by load_frozen, read_frozen and drift, and drift records each once;
  * format 2 runbooks load unchanged: the loader refuses a format 2 runbook and writes nothing,
    a task contract without `research` is what f5869e9 recorded, and a re-intake of the format 2
    worked example writes no event.

Fail case: a target loosened by an override without an owner-authorized record. Every class of
section 7.5 is exercised in both directions; each loosening FR-036 lists is refused without a
grant term and passes with one.

Every loader test copies the fixture of sections 10 and 11 into packs/sortbench/ of a temporary
git repository with its own project.yaml (section 10) and makes the store folder there,
untracked. Grants are written as t-105's `grant` will write them: an owner decision and a
research-grant event confirmed at the terminal. No test touches the network or launches an agent.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess

import pytest

from alpaca import db, decisions, intent, ops, taskcontract, util
from alpaca.gates import verdict as vc
from alpaca.research import contract, errors, events, owner, paths
from alpaca.tests.conftest import REPO

FIX = os.path.join(REPO, "alpaca", "tests", "fixtures", "research", "sortbench")
IMPROVE = "packs/sortbench/improve.yaml"
REPAIR = "packs/sortbench/repair.yaml"
PACK_REF_IMPROVE = "contract:%s#-" % IMPROVE
PACK_REF_REPAIR = "contract:%s#-" % REPAIR


# ------------------------------------------------------------------------------ fixtures
def _write(root, rel, text, mode=None):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def _edit(root, rel, old, new):
    path = os.path.join(root, rel)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert old in text, (rel, old)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new, 1))


def _git(root, *args):
    return subprocess.run(["git", "-C", root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"] + list(args),
                          check=True, capture_output=True, text=True).stdout


def _commit(root, message="edit"):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture
def repo(project):
    """The worked example in packs/sortbench/ of a temporary git repository that is also the
    project root, committed once; the store folder of case stable-keys made untracked."""
    root = project
    _write(root, "project.yaml", "name: sortbench-test\n")
    _write(root, ".gitignore", ".alpaca/\ncontracts/research/\n")
    _write(root, "pyproject.toml", "[project]\nname = \"sortlib\"\n")
    _write(root, "src/sortlib/__init__.py", "from .sort import sort\n")
    _write(root, "src/sortlib/sort.py", "def sort(items, key=None):\n    return sorted(items, key=key)\n")
    _write(root, "src/sortlib/merge.py", "def merge(a, b):\n    return sorted(a + b)\n")
    _write(root, "src/sortlib/keys.py", "def ident(x):\n    return x\n")
    _write(root, "src/sortlib/dispatch.py", "ENTRY_POINTS = ('sort',)\n")
    _write(root, "tests/test_sort.py", "def test_sort():\n    assert [1, 2] == [1, 2]\n")
    shutil.copytree(FIX, os.path.join(root, "packs", "sortbench"))
    _write(root, "packs/sortbench/tests/run_tests.py", "print('ALPACA-RESULTS-BEGIN')\nprint('ALPACA-RESULTS-END')\n")
    _write(root, "packs/sortbench/tests/test_edge.py", "def test_edge():\n    assert True\n")
    _write(root, "packs/sortbench/bench/measure.py", "print('METRIC: compares 17000')\n", 0o755)
    _write(root, "packs/sortbench/bench/prepare.py", "print('prepared')\n")
    _write(root, "packs/sortbench/holdout/inputs.json", "[5, 3, 1]\n")
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "base")
    _write(root, "contracts/research/sortbench-repair/stable-keys/run.sh", "echo reproduce\n", 0o755)
    _write(root, "contracts/research/sortbench-repair/stable-keys/fix-terms.txt", "0f4c2a9\n")
    return root


@pytest.fixture
def conn(repo):
    return db.connect(repo)


def _task(conn, research=None, done_bar="the run's champion meets both targets"):
    tid = ops.add_task(conn, "op-001", "a research task", "research task")
    data = {"done_bar": [done_bar]}
    if research is not None:
        data["research"] = research
    taskcontract.record(conn, tid, data, session="test", actor="human")
    return tid


def _grant(conn, root, ref_, terms, *, gate="loosen", confirmed="terminal", actor="owner", kind="done_when"):
    """What t-105's grant records: an owner decision and a research-grant event."""
    stamp = util.canonical_json(terms) + str(len(db.events(conn, limit=10 ** 9)))
    dec = decisions.record(conn, kind, "%s loosens %s (no hold open, record through event %s)" % (ref_, sorted(terms), stamp),
                           ["loosen", "keep the accepted values"], "loosen for %s" % ref_,
                           "the next start of %s may use the granted values" % ref_,
                           pointer="research-grant:%s:%s" % (ref_, gate), actor=actor, session="owner", root=root)
    events.emit(conn, events.GRANT, run=None, ref=ref_, op=None, session="owner", actor="owner",
                data={"ref": ref_, "gate": gate, "hold": None, "decision": dec["id"], "terms": {"set": terms},
                      "note": "test", "confirmed": confirmed, "by": "owner"})
    return dec["id"]


def _load(repo, rel=IMPROVE):
    return contract.load_block(os.path.join(repo, rel))


def _refusal(exc_info):
    exc = exc_info.value
    assert isinstance(exc, errors.ContractRefusal)
    assert exc.code == errors.CODES[exc.reason]
    return exc


def _check_task(conn, pack, override, task="t-900"):
    """merge, then check_override on the task layer; returns the diff."""
    _block, diffs = contract.merge(pack, override)
    contract.check_override([(contract.ref(pack["runbook"], task), diffs)], conn=conn)
    return diffs


def _baseline(conn, rel=IMPROVE):
    for event in reversed(db.events(conn, kind=events.CONTRACT, limit=10 ** 9)):
        if event["data"]["runbook"] == rel:
            return event["data"]
    return None


def _land(conn, pack):
    return contract.land(conn, pack, op=None, via="intake", session="test", actor="test")


def _events(conn):
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


# ------------------------------------------------------------------ the pack-level contract
def test_load_block_reads_the_pack_layers_the_commit_and_the_pack_files(repo):
    pack = _load(repo)
    assert pack["runbook"] == IMPROVE and pack["runbook_id"] == "sortbench-improve" and pack["pack"] == "sortbench"
    assert pack["repo"]["commit"] == _git(repo, "rev-parse", "HEAD").strip()
    assert pack["block"]["budget"] == {"usd": 40, "tokens": 3000000, "wall": 14400}
    assert pack["layers"]["research.budget.usd"] == "defaults"
    assert pack["layers"]["research.stop.slots"] == "runbook"
    assert pack["layers"]["research.metrics[compares].min_ok"] == "engine"
    assert pack["defaults"]["path"] == "packs/sortbench/pack.yaml"
    assert pack["spec"]["path"] == "packs/sortbench/improve-spec.md"
    # files: the other files of the runbook folder and every file a protected or holdout rule hits
    assert "pyproject.toml" in pack["files"] and "packs/sortbench/bench/measure.py" in pack["files"]
    assert "packs/sortbench/holdout/inputs.json" in pack["files"]
    assert "packs/sortbench/repair.yaml" in pack["files"]
    # not the runbook itself, its defaults file, a file no rule hits, or the untracked store
    for path in (IMPROVE, "packs/sortbench/pack.yaml", "src/sortlib/sort.py", "tests/test_sort.py",
                 "contracts/research/sortbench-repair/stable-keys/run.sh"):
        assert path not in pack["files"], path
    with open(os.path.join(repo, "pyproject.toml"), "rb") as fh:
        assert pack["files"]["pyproject.toml"] == util.sha256_hex(fh.read())


def test_the_loader_refuses_a_runbook_whose_check_fails_or_cannot_be_read(repo):
    _edit(repo, IMPROVE, "      margin: 4\n", "")
    with pytest.raises(errors.ContractRefusal) as err:
        _load(repo)
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-CHECK-FAILED" and exc.code == vc.FAIL
    assert "FIELD-MISSING research.metrics[1].margin" in exc.detail
    with pytest.raises(errors.ContractRefusal) as err:
        contract.load_block(os.path.join(repo, "packs/sortbench/absent.yaml"))
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-CHECK-BLOCKED" and exc.code == vc.BLOCKED


def test_a_pack_file_the_block_names_must_be_tracked_at_the_base_commit(repo):
    _write(repo, "packs/sortbench/lessons2.md", "- a lesson\n")
    _edit(repo, "packs/sortbench/pack.yaml", "file: lessons.md", "file: lessons2.md")
    with pytest.raises(errors.ContractRefusal) as err:
        _load(repo)
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-CHECK-FAILED" and "FILE-MISSING research.lessons.file" in exc.detail
    _commit(repo)
    assert _load(repo)["block"]["lessons"]["file"] == "lessons2.md"


def test_repo_unreadable_for_a_folder_that_is_no_repository_or_a_ref_that_does_not_resolve(repo, tmp_path):
    _edit(repo, "packs/sortbench/pack.yaml", "research:\n", "research:\n  repo:\n    ref: no-such-branch\n")
    with pytest.raises(errors.ContractRefusal) as err:
        _load(repo)
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-REPO-UNREADABLE" and exc.code == vc.BLOCKED
    _edit(repo, "packs/sortbench/pack.yaml", "ref: no-such-branch", "ref: HEAD")
    shutil.rmtree(os.path.join(repo, ".git"))
    with pytest.raises(errors.ContractRefusal) as err:
        _load(repo)
    assert _refusal(err).reason == "CONTRACT-REPO-UNREADABLE"


@pytest.mark.parametrize("form", ["absolute", "relative"])
def test_pack_outside_repo_for_an_absolute_and_a_relative_repo_path(repo, form):
    other = os.path.join(repo, "vendor", "other")
    os.makedirs(other)
    _git(other, "init", "-q", "-b", "main")
    _write(other, "README.md", "other\n")
    _commit(other)
    path = other if form == "absolute" else "vendor/other"
    _edit(repo, IMPROVE, "research:\n  mode: improve\n", "research:\n  mode: improve\n  repo:\n    path: %s\n" % path)
    with pytest.raises(errors.ContractRefusal) as err:
        _load(repo)
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-PACK-OUTSIDE-REPO" and exc.code == vc.FAIL
    # check_overlap refuses the same case on a contract object
    frozen = contract.from_block({"runbook": IMPROVE, "repo": {"path": path, "ref": "HEAD"}, "block": {}})
    with pytest.raises(errors.ContractRefusal) as err:
        contract.check_overlap(repo, frozen)
    assert _refusal(err).reason == "CONTRACT-PACK-OUTSIDE-REPO"


def test_pack_sha256_ignores_a_commit_that_changes_no_file_of_files(repo):
    before = _load(repo)
    _edit(repo, "src/sortlib/sort.py", "sorted(items", "list(sorted(items")
    _commit(repo)
    after = _load(repo)
    assert after["repo"]["commit"] != before["repo"]["commit"]
    assert after["pack_sha256"] == before["pack_sha256"]
    _edit(repo, "pyproject.toml", "sortlib", "sortlib2")
    _commit(repo)
    assert _load(repo)["pack_sha256"] != before["pack_sha256"]


def test_land_records_a_baseline_once_per_pack_sha256(repo, conn):
    pack = _load(repo)
    first = _land(conn, pack)
    assert first is not None and first["kind"] == contract.KIND_CONTRACT == events.CONTRACT
    data = json.loads(first["data"])
    assert data["runbook"] == IMPROVE and data["pack_sha256"] == pack["pack_sha256"] and data["via"] == "intake"
    assert set(data) >= {"runbook", "runbook_id", "runbook_sha256", "defaults", "spec", "mode", "block", "stages",
                         "knobs", "files", "pack_sha256", "via", "grants"}
    assert _land(conn, pack) is None
    _edit(repo, IMPROVE, "rounds: 5", "rounds: 4")
    assert _land(conn, _load(repo)) is not None


# ------------------------------------------------------------------ the task layer and its diff
def test_the_override_of_section_10_5_gives_its_recorded_diff(repo, conn):
    pack = _load(repo)
    override = {"metrics": [{"id": "compares", "target": 17000}], "stop": {"slots": 3}}
    block, diffs = contract.merge(pack, override)
    assert [(d.field, d.pack, d.task, d.field_class, d.direction, d.grant) for d in diffs] == [
        ("research.metrics[compares].target", 18000, 17000, "target", "tightened", None),
        ("research.stop.slots", 2, 3, "lower", "loosened", None)]
    assert block["stop"]["slots"] == 3 and block["metrics"][0]["target"] == 17000
    # the metric's id names the item and sets nothing: no entry for it
    assert not [d for d in diffs if d.field.endswith(".id")]
    with pytest.raises(errors.ContractRefusal) as err:
        contract.check_override([("contract:%s#t-140" % IMPROVE, diffs)], conn=conn)
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-LOOSEN-UNGRANTED" and exc.code == vc.PAUSED
    assert exc.loosened == ({"ref": "contract:%s#t-140" % IMPROVE, "field": "research.stop.slots",
                             "accepted": 2, "new": 3},)
    assert exc.detail.count("\n") == 0 and "research.stop.slots" in exc.detail
    # without the slots change the tightened target alone needs no record
    _check_task(conn, pack, {"metrics": [{"id": "compares", "target": 17000}]}, task="t-140")


def test_no_override_is_the_pack_block_and_an_empty_diff(repo):
    pack = _load(repo)
    block, diffs = contract.merge(pack, None)
    assert block == pack["block"] and diffs == []


def test_a_grant_with_a_matching_term_passes_and_a_different_value_is_refused(repo, conn):
    pack = _load(repo)
    ref_ = contract.ref(IMPROVE, "t-140")
    _block, diffs = contract.merge(pack, {"stop": {"slots": 3}})
    _grant(conn, repo, ref_, {"research.stop.slots": 4})
    with pytest.raises(errors.ContractRefusal):
        contract.check_override([(ref_, diffs)], conn=conn)
    dec = _grant(conn, repo, ref_, {"research.stop.slots": 3})
    contract.check_override([(ref_, diffs)], conn=conn)
    assert owner.authorized(conn, ref_, "loosen") == (True, dec)
    # a grant on another ref does not count
    _block, other = contract.merge(pack, {"stop": {"rounds": 9}})
    with pytest.raises(errors.ContractRefusal):
        contract.check_override([(ref_, other)], conn=conn)


# FR-036's list, one loosening each (SC-013), with a tightening of the same field beside it where the
# field's class has one. (override loosening, the loosened field, override tightening or None)
FR036 = [
    ("target", {"metrics": [{"id": "compares", "target": 19000}]}, "research.metrics[compares].target",
     {"metrics": [{"id": "compares", "target": 17000}]}),
    ("fewer repeats", {"metrics": [{"id": "wall_ms", "repeats": 4, "seeds": [11, 12, 13, 14]}]},
     "research.metrics[wall_ms].repeats", {"metrics": [{"id": "compares", "repeats": 3}]}),
    ("fewer min_ok", {"metrics": [{"id": "wall_ms", "min_ok": 2}]}, "research.metrics[wall_ms].min_ok",
     {"metrics": [{"id": "wall_ms", "min_ok": 4}]}),
    ("smaller margin", {"metrics": [{"id": "wall_ms", "margin": 2}]}, "research.metrics[wall_ms].margin",
     {"metrics": [{"id": "wall_ms", "margin": 6}]}),
    ("changed command", {"metrics": [{"id": "compares", "command": "python3 other.py"}]},
     "research.metrics[compares].command", None),
    ("changed parser", {"metrics": [{"id": "compares", "parser": "json-field", "path": "${EVIDENCE_DIR}/c.json",
                                      "field": "compares"}]}, "research.metrics[compares].parser", None),
    ("changed direction", {"metrics": [{"id": "compares", "direction": "higher"}]},
     "research.metrics[compares].direction", None),
    ("looser accept rule", {"accept": {"after_targets": "pareto"}}, "research.accept.after_targets", None),
    ("later stop: rounds", {"stop": {"rounds": 8}}, "research.stop.rounds", {"stop": {"rounds": 3}}),
    ("later stop: slots", {"stop": {"slots": 3}}, "research.stop.slots", {"stop": {"slots": 1}}),
    ("shorter protected list", {"paths": {"protected": ["packs/sortbench/"]}}, "research.paths.protected",
     {"paths": {"protected": ["packs/sortbench/", "pyproject.toml", "tests/"]}}),
    ("wider editable allowlist", {"paths": {"editable": ["src/sortlib/", "src/extra/"]}}, "research.paths.editable",
     None),
    ("raised budget", {"budget": {"usd": 50}}, "research.budget.usd", {"budget": {"usd": 30}}),
]


@pytest.mark.parametrize("name,loose,field,tight", FR036, ids=[row[0] for row in FR036])
def test_each_fr036_loosening_needs_the_owner_and_its_tightening_does_not(repo, conn, name, loose, field, tight):
    pack = _load(repo)
    ref_ = contract.ref(IMPROVE, "t-900")
    _block, diffs = contract.merge(pack, loose)
    entry = [d for d in diffs if d.field == field]
    assert entry and entry[0].direction == "loosened", diffs
    with pytest.raises(errors.ContractRefusal) as err:
        contract.check_override([(ref_, diffs)], conn=conn)
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-LOOSEN-UNGRANTED" and field in [e["field"] for e in exc.loosened]
    # the owner's grant with a term for every loosened field lets it through
    _grant(conn, repo, ref_, dict((e["field"], e["new"]) for e in exc.loosened))
    contract.check_override([(ref_, diffs)], conn=conn)
    if tight is not None:
        _block, diffs = contract.merge(pack, tight)
        assert diffs and all(d.direction == "tightened" for d in diffs), diffs
        contract.check_override([(contract.ref(IMPROVE, "t-901"), diffs)], conn=conn)


def test_a_subset_list_tightens_only_with_rules_of_the_pack_list(repo, conn):
    _edit(repo, IMPROVE, "editable: [src/sortlib/]", "editable: [src/sortlib/, src/extra/]")
    _commit(repo)
    pack = _load(repo)
    _block, diffs = contract.merge(pack, {"paths": {"editable": ["src/sortlib/"]}})
    assert [(d.field, d.direction) for d in diffs] == [("research.paths.editable", "tightened")]
    contract.check_override([(contract.ref(IMPROVE, "t-900"), diffs)], conn=conn)
    # a rule the pack list lacks is a loosening, even one that names a narrower path (section 7.5)
    _block, diffs = contract.merge(pack, {"paths": {"editable": ["src/sortlib/sort.py"]}})
    assert [(d.field, d.direction) for d in diffs] == [("research.paths.editable", "loosened")]


def test_a_target_set_on_a_metric_that_had_none_is_a_loosening(repo, conn):
    _edit(repo, IMPROVE, "      target: 120\n", "")
    _commit(repo)
    pack = _load(repo)
    _block, diffs = contract.merge(pack, {"metrics": [{"id": "wall_ms", "target": 100}]})
    assert [(d.field, d.pack, d.task, d.direction) for d in diffs] == [
        ("research.metrics[wall_ms].target", None, 100, "loosened")]
    ref_ = contract.ref(IMPROVE, "t-900")
    with pytest.raises(errors.ContractRefusal):
        contract.check_override([(ref_, diffs)], conn=conn)
    _grant(conn, repo, ref_, {"research.metrics[wall_ms].target": 101})
    with pytest.raises(errors.ContractRefusal):
        contract.check_override([(ref_, diffs)], conn=conn)
    _grant(conn, repo, ref_, {"research.metrics[wall_ms].target": 100})
    contract.check_override([(ref_, diffs)], conn=conn)


# rule 9 of section 4: a command's timeout may only fall, a check's time may only rise, the three
# checking roles' effort may only move toward high, the work agents' settings are free
RULE9 = [
    ({"metrics": [{"id": "compares", "timeout": 200}]}, "tightened"),
    ({"metrics": [{"id": "compares", "timeout": 400}]}, "loosened"),
    ({"runtime": {"timeouts": {"audit": 1200}}}, "tightened"),
    ({"runtime": {"timeouts": {"audit": 600}}}, "loosened"),
    ({"runtime": {"timeouts": {"reviewer": 600}}}, "loosened"),
    ({"runtime": {"timeouts": {"proposer": 60}}}, "free"),
    ({"runtime": {"timeouts": {"implementer": 7200}}}, "free"),
    ({"runtime": {"effort": {"audit": "high"}}}, "tightened"),
    ({"runtime": {"effort": {"audit": "low"}}}, "loosened"),
    ({"runtime": {"effort": {"reproducer": "medium"}}}, "loosened"),
    ({"runtime": {"effort": {"proposer": "low"}}}, "free"),
    ({"runtime": {"retries": 1}}, "tightened"),
    ({"runtime": {"retries": 5}}, "loosened"),
    ({"verifier": {"timeout": 2400}}, "tightened"),
    ({"verifier": {"timeout": 1200}}, "loosened"),
    ({"verifier": {"rerun_timeout": 600}}, "loosened"),
    ({"gates": [{"stage": "build", "lock_timeout": 60}, {"stage": "unit"}]}, "free"),
]


@pytest.mark.parametrize("override,direction", RULE9)
def test_timeouts_and_effort_move_by_their_class(repo, conn, override, direction):
    pack = _load(repo)
    _block, diffs = contract.merge(pack, override)
    assert [d.direction for d in diffs] == [direction], diffs
    ref_ = contract.ref(IMPROVE, "t-900")
    if direction == "loosened":
        with pytest.raises(errors.ContractRefusal):
            contract.check_override([(ref_, diffs)], conn=conn)
    else:
        contract.check_override([(ref_, diffs)], conn=conn)


def test_repair_command_timeouts_fall_and_repeats_rise_without_the_owner(repo, conn):
    pack = _load(repo, REPAIR)
    for override, want in (
            ({"repair": {"cases": [{"id": "stable-keys", "fail_to_pass": {"timeout": 200}}]}}, "tightened"),
            ({"repair": {"cases": [{"id": "stable-keys", "fail_to_pass": {"timeout": 400}}]}}, "loosened"),
            ({"repair": {"cases": [{"id": "none-key", "fail_to_pass": {"repeats": 3}}]}}, "tightened"),
            ({"repair": {"cases": [{"id": "none-key", "fail_to_pass": {"repeats": 1}}]}}, "loosened"),
            ({"repair": {"cases": [{"id": "stable-keys", "fail_to_pass": {
                "tests": ["stable_equal_keys", "stable_all_same_key", "stable_three"]}}]}}, "tightened"),
            ({"repair": {"cases": [{"id": "stable-keys", "fail_to_pass": {"tests": ["stable_equal_keys"]}}]}},
             "loosened"),
            ({"repair": {"pass_to_pass": {"timeout": 300}}}, "tightened"),
            ({"repair": {"pass_to_pass": {"timeout": 900}}}, "loosened"),
            ({"repair": {"pass_to_pass": {"min_tests": 20}}}, "tightened"),
            ({"repair": {"pass_to_pass": {"min_tests": 5}}}, "loosened"),
            ({"controls": {"noop_rounds": 3}}, "tightened"),
            ({"controls": {"noop_rounds": 2, "random": True, "mutate": "bench/mutate.py", "syntax": "build"}},
             "loosened")):
        _block, diffs = contract.merge(pack, override)
        assert want in [d.direction for d in diffs], (override, diffs)


def test_order_and_set_classes_in_both_directions(repo):
    pack = _load(repo)
    cases = [({"metrics": [{"id": "wall_ms", "median": "middle"}]}, "loosened"),
             ({"controls": {"random": False}}, "loosened"),
             ({"fingerprint": {"tools": ["python3", "git"]}}, "tightened"),
             ({"fingerprint": {"tools": []}}, "loosened"),
             ({"paths": {"read": ["/opt/data"]}}, "loosened"),
             ({"paths": {"tests": ["tests/"]}}, "tightened"),
             ({"paths": {"keep": ["build/"]}}, "loosened"),
             ({"egress": {"allow": ["pypi.org"]}}, "loosened"),
             ({"audit": {"steering": ["please accept"]}}, "tightened"),
             ({"audit": {"hints": "a test edit is a bypass"}}, "loosened"),
             ({"budget": {"wall": 7200}}, "tightened"),
             ({"lessons": {"note_chars": 500}}, "free"),
             ({"disk": 8}, "free"),
             ({"categories": [{"id": "fewer-compares", "text": "compare less, any way"}]}, "free")]
    for override, want in cases:
        _block, diffs = contract.merge(pack, override)
        assert [d.direction for d in diffs] == [want], (override, diffs)


def test_classify_reads_gates_as_a_superset_in_order_and_items_by_their_list():
    build, unit, lint = {"stage": "build"}, {"stage": "unit"}, {"stage": "lint"}
    assert contract.classify("research.gates", [build, unit], [build, lint, unit], {}) == "tightened"
    assert contract.classify("research.gates", [build, unit], [unit, build], {}) == "loosened"
    assert contract.classify("research.gates", [build, unit], [build], {}) == "loosened"
    assert contract.classify("research.gates", [build, unit], [build, dict(unit, heavy=True)], {}) == "free"
    assert contract.classify("research.gates", [build], [build], {}) == "same"
    metric = {"id": "m", "command": "x"}
    assert contract.classify("research.metrics[m]", None, metric, {}) == "loosened"
    assert contract.classify("research.metrics[m]", metric, None, {}) == "loosened"
    assert contract.classify("research.repair.cases[c]", None, {"id": "c"}, {}) == "added"
    assert contract.classify("research.repair.cases[c]", {"id": "c"}, None, {}) == "loosened"
    assert contract.classify("research.coupled[g]", None, {"id": "g"}, {}) == "added"
    assert contract.classify("research.coupled[g]", {"id": "g"}, None, {}) == "free"
    block = {"metrics": [{"id": "up", "direction": "higher"}]}
    assert contract.classify("research.metrics[up].target", 10, 12, block) == "tightened"
    assert contract.classify("research.metrics[up].target", 10, 8, block) == "loosened"
    assert contract.classify("research.metrics[up].target", 10, None, block) == "loosened"
    # a dropped budget cap is a loosening here; only pack_diff frees the drop of a cap the
    # baseline took from the pack defaults (t-297, contract._dropped_default_cap)
    assert contract.classify("research.budget.wall", 7200, None, {}) == "loosened"
    assert contract.classify("research.budget.usd", 40, None, {}) == "loosened"
    assert contract.classify("research.budget.tokens", 3000000, None, {}) == "loosened"
    assert contract.classify("research.budget.usd", 40, 80, {}) == "loosened"
    assert contract.classify("research.budget.usd", None, 80, {}) == "tightened"
    assert contract.classify("research.budget.usd", 80, 40, {}) == "tightened"
    assert contract.classify("research.accept.after_targets", None, "stop", {}) == "tightened"
    assert contract.classify("research.accept.after_targets", None, "pareto", {}) == "loosened"
    assert contract.classify("research.controls.random", False, True, {}) == "tightened"
    assert contract.classify("research.paths.hidden", ["a/"], ["a/", "t/"], {}, protected=["t/x.py"]) == "loosened"
    assert contract.classify("research.paths.hidden", ["a/"], ["a/", "t/"], {}, protected=["u/x.py"]) == "tightened"
    assert contract.classify("research.paths.tests", ["a/"], ["a/", "t/"], {}, protected=["t/x.py"]) == "tightened"


def test_a_new_proposal_field_category_or_group_is_added(repo, conn):
    pack = _load(repo)
    _block, diffs = contract.merge(pack, {"proposal": [{"field": "effort", "type": "int"}],
                                          "categories": [{"id": "tiny", "text": "a small change"}]})
    assert [(d.field, d.pack, d.direction) for d in diffs] == [
        ("research.categories[tiny]", None, "added"), ("research.proposal[effort]", None, "added")]
    assert diffs[1].task == {"field": "effort", "type": "int", "required": True}
    contract.check_override([(contract.ref(IMPROVE, "t-900"), diffs)], conn=conn)


@pytest.mark.parametrize("override,why", [
    ({"metrics": [{"id": "swaps", "command": "x", "parser": "markers", "unit": "n", "direction": "lower",
                   "kind": "deterministic", "timeout": 60}]}, "may not add a metric"),
    ({"mode": "repair"}, "class none"),
    ({"paths": {"covers": ["SC-001"]}}, "class none"),
    ({"stop": {"source": "owner:x"}}, "class none"),
    ({"stop": {"slots": 99}}, "RANGE research.stop.slots"),
    ({"stop": {"sloths": 3}}, "FIELD-UNKNOWN research.stop.sloths"),
    ({"budget": {"usd": "<FILL:dollars>"}}, "FIELD-UNFILLED research.budget.usd"),
])
def test_an_override_that_sets_a_none_field_adds_a_metric_or_breaks_a_rule_is_invalid(repo, override, why):
    with pytest.raises(errors.ContractRefusal) as err:
        contract.merge(_load(repo), override)
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-OVERRIDE-INVALID" and exc.code == vc.FAIL and why in exc.detail, exc.detail
    with pytest.raises(errors.ContractRefusal):
        contract.merge(_load(repo), ["not", "a", "mapping"])


def test_a_task_layer_may_not_add_a_repair_case(repo):
    with pytest.raises(errors.ContractRefusal) as err:
        contract.merge(_load(repo, REPAIR), {"repair": {"cases": [{"id": "third", "bug_report": "bugs/none-key.md",
                                                                    "fail_to_pass": {"origin": "generate"}}]}})
    assert _refusal(err).reason == "CONTRACT-OVERRIDE-INVALID" and "may not add a case" in err.value.detail


def test_raising_a_deterministic_metrics_repeats_derives_min_ok_again(repo):
    pack = _load(repo)
    assert pack["block"]["metrics"][0]["min_ok"] == 1
    block, diffs = contract.merge(pack, {"metrics": [{"id": "compares", "repeats": 10}]})
    assert block["metrics"][0]["repeats"] == 10 and block["metrics"][0]["min_ok"] == 10
    assert [d.field for d in diffs] == ["research.metrics[compares].repeats"]


def test_a_lessons_file_pointed_into_a_holdout_path_is_refused(repo):
    _write(repo, "packs/sortbench/holdout/notes.md", "held-out notes\n")
    with pytest.raises(errors.ContractRefusal) as err:
        contract.merge(_load(repo), {"lessons": {"file": "holdout/notes.md"}})
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-OVERRIDE-INVALID" and "PATHS-OVERLAP research.lessons.file" in exc.detail


@pytest.mark.parametrize("override,field", [
    ({"paths": {"hidden": ["packs/sortbench/tests/"]}}, "research.paths.hidden"),
    ({"holdout": {"paths": ["packs/sortbench/holdout/", "packs/sortbench/tests/"]}}, "research.holdout.paths"),
])
def test_a_hidden_or_holdout_rule_over_a_protected_test_is_a_loosening(repo, conn, override, field):
    pack = _load(repo)
    _block, diffs = contract.merge(pack, override)
    assert [(d.field, d.direction) for d in diffs] == [(field, "loosened")]
    ref_ = contract.ref(IMPROVE, "t-900")
    with pytest.raises(errors.ContractRefusal) as err:
        contract.check_override([(ref_, diffs)], conn=conn)
    assert [e["field"] for e in err.value.loosened] == [field]
    _grant(conn, repo, ref_, {field: diffs[0].task})
    contract.check_override([(ref_, diffs)], conn=conn)


def test_a_hidden_rule_that_hits_no_protected_file_is_a_tightening(repo, conn):
    _block, diffs = contract.merge(_load(repo), {"paths": {"hidden": ["docs/results/"]}})
    assert [(d.field, d.direction) for d in diffs] == [("research.paths.hidden", "tightened")]
    contract.check_override([(contract.ref(IMPROVE, "t-900"), diffs)], conn=conn)


# ------------------------------------------------------------------- a pack edit between runs
def _pack_check(conn, repo, rel=IMPROVE):
    pack = _load(repo, rel)
    diffs = contract.pack_diff(pack, _baseline(conn, rel))
    contract.check_override([("contract:%s#-" % rel, diffs)], conn=conn)
    return pack, diffs


def _pack_refused(conn, repo, rel=IMPROVE):
    with pytest.raises(errors.ContractRefusal) as err:
        _pack_check(conn, repo, rel)
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-LOOSEN-UNGRANTED" and all(e["ref"] == "contract:%s#-" % rel for e in exc.loosened)
    return dict((e["field"], e) for e in exc.loosened)


def test_a_first_block_has_no_baseline_and_is_accepted_as_written(repo, conn):
    assert contract.pack_diff(_load(repo), None) == []
    _pack_check(conn, repo)


def test_a_widened_gate_check_needs_its_stage_term(repo, conn):
    _land(conn, _load(repo))
    _edit(repo, IMPROVE, 'op: "=="\n        value: 0', 'op: "<="\n        value: 3')
    _commit(repo)
    loosened = _pack_refused(conn, repo)
    assert list(loosened) == ["stages[unit]"]
    assert loosened["stages[unit]"]["accepted"]["checks"][0]["op"] == "=="
    stage = loosened["stages[unit]"]["new"]
    _grant(conn, repo, PACK_REF_IMPROVE, {"stages[unit]": stage})
    pack, _diffs = _pack_check(conn, repo)
    assert _land(conn, pack) is not None


def test_a_new_and_a_changed_knob_need_their_terms(repo, conn):
    _land(conn, _load(repo))
    _edit(repo, IMPROVE, "\nstages:\n", "\nknobs:\n  - {id: CUTOFF, description: cut-over size, type: int, default: 16}\n"
                                        "\nstages:\n")
    _commit(repo)
    loosened = _pack_refused(conn, repo)
    assert list(loosened) == ["knobs[CUTOFF]"] and loosened["knobs[CUTOFF]"]["accepted"] is None
    _grant(conn, repo, PACK_REF_IMPROVE, {"knobs[CUTOFF]": loosened["knobs[CUTOFF]"]["new"]})
    pack, _diffs = _pack_check(conn, repo)
    _land(conn, pack)
    _edit(repo, IMPROVE, "default: 16", "default: 32")
    _commit(repo)
    assert list(_pack_refused(conn, repo)) == ["knobs[CUTOFF]"]


def test_changed_new_and_removed_pack_files_need_their_terms(repo, conn):
    _land(conn, _load(repo))
    _edit(repo, "packs/sortbench/bench/measure.py", "17000", "16000")
    _write(repo, "packs/sortbench/tests/conftest.py", "import pytest\n")
    os.remove(os.path.join(repo, "packs/sortbench/bench/prepare.py"))
    _commit(repo)
    loosened = _pack_refused(conn, repo)
    assert sorted(loosened) == ["files[packs/sortbench/bench/measure.py]", "files[packs/sortbench/bench/prepare.py]",
                                "files[packs/sortbench/tests/conftest.py]"]
    assert loosened["files[packs/sortbench/bench/prepare.py]"]["new"] is None
    assert loosened["files[packs/sortbench/tests/conftest.py]"]["accepted"] is None
    terms = dict((f, e["new"]) for f, e in loosened.items())
    del terms["files[packs/sortbench/bench/prepare.py]"]
    _grant(conn, repo, PACK_REF_IMPROVE, terms)
    assert list(_pack_refused(conn, repo)) == ["files[packs/sortbench/bench/prepare.py]"]
    _grant(conn, repo, PACK_REF_IMPROVE, dict(terms, **{"files[packs/sortbench/bench/prepare.py]": None}))
    _pack_check(conn, repo)


def test_an_uncommitted_edit_changes_nothing_and_a_committed_one_is_compared(repo, conn):
    _land(conn, _load(repo))
    _edit(repo, "packs/sortbench/bench/measure.py", "17000", "16000")
    _pack, diffs = _pack_check(conn, repo)
    assert diffs == []
    _commit(repo)
    assert list(_pack_refused(conn, repo)) == ["files[packs/sortbench/bench/measure.py]"]


def test_a_changed_spec_path_needs_its_term(repo, conn):
    _land(conn, _load(repo))
    os.rename(os.path.join(repo, "packs/sortbench/improve-spec.md"), os.path.join(repo, "packs/sortbench/spec2.md"))
    _edit(repo, IMPROVE, "spec: improve-spec.md", "spec: spec2.md")
    _commit(repo)
    loosened = _pack_refused(conn, repo)
    assert loosened["spec"]["accepted"] == "packs/sortbench/improve-spec.md"
    assert loosened["spec"]["new"] == "packs/sortbench/spec2.md"
    others = dict((f, e["new"]) for f, e in loosened.items() if f != "spec")
    _grant(conn, repo, PACK_REF_IMPROVE, others)
    assert list(_pack_refused(conn, repo)) == ["spec"]
    _grant(conn, repo, PACK_REF_IMPROVE, dict(others, spec="packs/sortbench/spec2.md"))
    _pack_check(conn, repo)


def test_an_edit_that_only_tightens_one_runbook_needs_a_grant_for_the_other(repo, conn):
    _land(conn, _load(repo, IMPROVE))
    _land(conn, _load(repo, REPAIR))
    _edit(repo, REPAIR, "rounds: 3", "rounds: 2")
    _commit(repo)
    _pack, diffs = _pack_check(conn, repo, REPAIR)
    assert [(d["field"], d["direction"]) for d in diffs] == [("research.stop.rounds", "tightened")]
    assert list(_pack_refused(conn, repo, IMPROVE)) == ["files[packs/sortbench/repair.yaml]"]


def test_a_pack_edit_that_adds_a_metric_or_drops_a_metric_or_case_needs_the_owner(repo, conn):
    _land(conn, _load(repo, IMPROVE))
    _land(conn, _load(repo, REPAIR))
    _edit(repo, IMPROVE, "  accept:\n", "    - id: swaps\n      command: python3 ${CONTRACT_DIR}/packs/sortbench/bench/"
          "measure.py --metric swaps\n      parser: markers\n      unit: swaps\n      direction: lower\n"
          "      kind: deterministic\n      timeout: 60\n      covers: [SC-001]\n  accept:\n")
    _commit(repo)
    loosened = _pack_refused(conn, repo, IMPROVE)
    assert list(loosened) == ["research.metrics[swaps]"] and loosened["research.metrics[swaps]"]["accepted"] is None
    _grant(conn, repo, PACK_REF_IMPROVE, {"research.metrics[swaps]": loosened["research.metrics[swaps]"]["new"]})
    pack, _diffs = _pack_check(conn, repo, IMPROVE)
    _land(conn, pack)
    # dropping it again is a loosening with the value null
    _git(repo, "revert", "--no-edit", "HEAD")
    loosened = _pack_refused(conn, repo, IMPROVE)
    assert loosened["research.metrics[swaps]"]["new"] is None
    _grant(conn, repo, PACK_REF_IMPROVE, {"research.metrics[swaps]": None})
    _pack_check(conn, repo, IMPROVE)
    # a case a pack edit drops
    with open(os.path.join(repo, REPAIR), encoding="utf-8") as fh:
        text = fh.read()
    start = text.index("      - id: none-key")
    end = text.index("  coupled:")
    with open(os.path.join(repo, REPAIR), "w", encoding="utf-8") as fh:
        fh.write(text[:start] + text[end:])
    _edit(repo, "packs/sortbench/repair-spec.md", "- **SC-002**: sort(items, key=None) sorts by the items themselves.\n", "")
    _commit(repo)
    loosened = _pack_refused(conn, repo, REPAIR)
    assert "research.repair.cases[none-key]" in loosened
    _grant(conn, repo, PACK_REF_REPAIR, dict((f, e["new"]) for f, e in loosened.items()))
    _pack_check(conn, repo, REPAIR)


def test_a_commit_to_a_protected_file_outside_the_runbook_folder_needs_its_term(repo, conn):
    _land(conn, _load(repo))
    # a pack edit that adds a protected rule is a tightening: its files come in with no entry of their own
    _edit(repo, "packs/sortbench/pack.yaml", "protected: [packs/sortbench/, pyproject.toml]",
          "protected: [packs/sortbench/, pyproject.toml, tests/, conftest.py]")
    _commit(repo)
    pack, diffs = _pack_check(conn, repo)
    assert [(d["field"], d["direction"]) for d in diffs] == [("research.paths.protected", "tightened")]
    assert "tests/test_sort.py" in pack["files"]
    _land(conn, pack)
    _edit(repo, "pyproject.toml", "sortlib", "sortlib-fast")
    _edit(repo, "tests/test_sort.py", "assert [1, 2] == [1, 2]", "assert True")
    _commit(repo)
    loosened = _pack_refused(conn, repo)
    assert sorted(loosened) == ["files[pyproject.toml]", "files[tests/test_sort.py]"]
    _grant(conn, repo, PACK_REF_IMPROVE, dict((f, e["new"]) for f, e in loosened.items()))
    pack, _diffs = _pack_check(conn, repo)
    assert _land(conn, pack) is not None and _baseline(conn)["files"]["tests/test_sort.py"] == \
        loosened["files[tests/test_sort.py]"]["new"]
    # a new conftest.py at the root, which the bare-name rule hits
    _write(repo, "conftest.py", "import sys\n")
    _commit(repo)
    assert list(_pack_refused(conn, repo)) == ["files[conftest.py]"]


def test_a_dropped_protected_rule_needs_only_its_own_term(repo, conn):
    _land(conn, _load(repo))
    _edit(repo, "packs/sortbench/pack.yaml", "protected: [packs/sortbench/, pyproject.toml]",
          "protected: [packs/sortbench/]")
    _commit(repo)
    loosened = _pack_refused(conn, repo)
    assert list(loosened) == ["research.paths.protected"]
    _grant(conn, repo, PACK_REF_IMPROVE, {"research.paths.protected": ["packs/sortbench/"]})
    _pack_check(conn, repo)


@pytest.mark.parametrize("old,new,field", [
    ("  paths:\n    editable: [src/sortlib/]\n", "  paths:\n    editable: [src/sortlib/]\n    hidden: [packs/sortbench/tests/]\n",
     "research.paths.hidden"),
    ("paths: [packs/sortbench/holdout/]", "paths: [packs/sortbench/holdout/, packs/sortbench/tests/]",
     "research.holdout.paths"),
])
def test_a_hidden_or_holdout_rule_a_pack_edit_adds_over_a_protected_file_needs_its_term(repo, conn, old, new, field):
    _land(conn, _load(repo))
    _edit(repo, IMPROVE, old, new)
    _commit(repo)
    loosened = _pack_refused(conn, repo)
    assert list(loosened) == [field]
    _grant(conn, repo, PACK_REF_IMPROVE, {field: loosened[field]["new"]})
    _pack_check(conn, repo)


# ----------------------------------------------------------------------- freeze and the run
def test_freeze_writes_the_contract_the_copies_and_the_intent_record(repo, conn):
    view = contract.preview(repo, IMPROVE)
    frozen, fields = contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=None, session="test")
    folder = paths.contract_dir(repo, "rr-001")
    with open(os.path.join(folder, "contract.json"), encoding="utf-8") as fh:
        text = fh.read()
    obj = json.loads(text)
    assert text == util.canonical_json(obj)
    # the preview's hashes are the frozen ones
    for key in ("contract_sha256", "pack_sha256", "files_sha256"):
        assert fields[key] == view[key] == obj[key]
    assert frozen.sha256 == fields["contract_sha256"] and frozen.files_sha256 == fields["files_sha256"]
    assert set(fields) == {"contract_sha256", "pack_sha256", "files_sha256", "intent", "override_diff",
                           "runbook_sha256", "base_commit", "block", "stamps"}
    assert fields["base_commit"] == obj["repo"]["commit"] == _git(repo, "rev-parse", "HEAD").strip()
    with open(os.path.join(folder, "files.json"), encoding="utf-8") as fh:
        listing = fh.read()
    manifest = json.loads(listing)
    assert util.sha256_hex(listing) == obj["files_sha256"]
    # protected files in files/, held-out files in holdout/ and not in files/
    assert "files/pyproject.toml" in manifest and "files/packs/sortbench/improve.yaml" in manifest
    assert "holdout/packs/sortbench/holdout/inputs.json" in manifest
    assert "files/packs/sortbench/holdout/inputs.json" not in manifest
    assert not os.path.exists(os.path.join(folder, "files", "packs", "sortbench", "holdout"))
    assert os.access(os.path.join(folder, "files", "packs", "sortbench", "bench", "mutate.py"), os.X_OK)
    for key, meta in manifest.items():
        with open(os.path.join(folder, key), "rb") as fh:
            assert util.sha256_hex(fh.read()) == meta["sha256"], key
    # every copy is read-only, and the stamps name the inode and ctime of every frozen path
    for key in list(manifest) + ["contract.json", "files.json"]:
        assert not os.stat(os.path.join(folder, key)).st_mode & 0o222, key
    assert set(fields["stamps"]) >= set(manifest) | {".", "contract.json", "files.json", "files", "holdout"}
    assert "files/packs/sortbench/bench" in fields["stamps"]
    for rel, (ino, ctime) in fields["stamps"].items():
        st = os.lstat(os.path.join(folder, rel))
        assert (st.st_ino, st.st_ctime_ns) == (ino, ctime), rel
    # the intent record holds the contract as its bar and traces to the runbook's bytes
    rec = intent.get(conn, fields["intent"])
    assert rec["done_when"] == text and rec["author"] == "alpaca-research" and rec["author_role"] == "agent"
    assert rec["label"] == "research contract sortbench-improve (%s, task -)" % IMPROVE
    assert rec["pointer"] == "local:%s@%s" % (IMPROVE, obj["runbook_sha256"])
    # the pack-level baseline is landed once
    assert _baseline(conn)["via"] == "loader"
    # and load_frozen accepts the untouched copy
    events.emit(conn, events.RUN_START, run="rr-001", ref="rr-001", op=None, session="test",
                data={"intent": fields["intent"], "contract_sha256": fields["contract_sha256"],
                      "files_sha256": fields["files_sha256"], "stamps": fields["stamps"]})
    assert contract.load_frozen(conn, repo, "rr-001") == frozen
    assert contract.read_frozen(repo, "rr-001") == frozen


def test_the_contract_hash_ignores_the_task_and_the_commit_and_covers_the_bar(repo):
    obj = contract.preview(repo, IMPROVE)["contract"]
    base = contract.contract_sha256(obj)
    for key, value in (("task", "t-1"), ("ref", "x"), ("grants", ["dec-1"]), ("layers", {}), ("override_diff", [1]),
                       ("override", {"stop": {"slots": 1}})):
        assert contract.contract_sha256(dict(obj, **{key: value})) == base, key
    # the host realpaths of the read paths and tools depend on the caller's PATH, cwd and machine:
    # two objects that differ only there hash the same, so preview and freeze agree across processes
    assert contract.contract_sha256(dict(obj, read_real=["/elsewhere/data"], tools_real=["/opt/bin/python3"])) == base
    assert contract.contract_sha256(dict(obj, repo=dict(obj["repo"], commit="0" * 40))) == base
    assert contract.contract_sha256(dict(obj, stages=[])) != base
    assert contract.contract_sha256(dict(obj, files_sha256="0")) != base
    assert contract.contract_sha256(dict(obj, block=dict(obj["block"], disk=3))) != base


def test_freeze_records_the_task_diff_and_the_grant_that_allowed_it(repo, conn):
    tid = _task(conn, {"metrics": [{"id": "compares", "target": 17000}], "stop": {"slots": 3}})
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=tid, session="test")
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-LOOSEN-UNGRANTED" and [e["ref"] for e in exc.loosened] == [contract.ref(IMPROVE, tid)]
    assert not os.path.exists(paths.contract_dir(repo, "rr-001"))
    dec = _grant(conn, repo, contract.ref(IMPROVE, tid), {"research.stop.slots": 3})
    frozen, fields = contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=tid, session="test")
    assert fields["override_diff"] == [
        {"field": "research.metrics[compares].target", "pack": 18000, "task": 17000, "class": "target",
         "direction": "tightened", "grant": None},
        {"field": "research.stop.slots", "pack": 2, "task": 3, "class": "lower", "direction": "loosened",
         "grant": dec}]
    assert frozen.slots == 3 and frozen.targets[0].value == 17000 and frozen.grants == (dec,)
    assert frozen.task == tid and frozen.ref == contract.ref(IMPROVE, tid)
    assert "decision:%s" % dec in intent.get(conn, fields["intent"])["pointer"]
    # the grant is used: the next start needs a new one
    assert owner.authorized(conn, contract.ref(IMPROVE, tid), "loosen")[0] is False
    obj = json.loads(open(os.path.join(paths.contract_dir(repo, "rr-001"), "contract.json")).read())
    # the diff is in the frozen contract.json and in the intent record's bar, not only returned
    assert obj["override_diff"] == fields["override_diff"]
    assert json.loads(intent.get(conn, fields["intent"])["done_when"])["override_diff"] == fields["override_diff"]
    assert obj["layers"]["research.stop.slots"] == "task" and obj["layers"]["research.budget.usd"] == "defaults"
    assert obj["override"] == {"metrics": [{"id": "compares", "target": 17000}], "stop": {"slots": 3}}


def test_freeze_refuses_a_loosening_of_both_layers_in_one_refusal(repo, conn):
    _land(conn, _load(repo))
    _edit(repo, IMPROVE, "rounds: 5", "rounds: 6")
    _commit(repo)
    tid = _task(conn, {"stop": {"slots": 3}})
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=tid, session="test")
    exc = _refusal(err)
    assert [(e["ref"], e["field"]) for e in exc.loosened] == [
        (contract.ref(IMPROVE, tid), "research.stop.slots"), (PACK_REF_IMPROVE, "research.stop.rounds")]
    assert len(exc.detail.splitlines()) == 2


def test_a_store_case_is_frozen_into_hidden_and_a_missing_run_sh_refuses(repo, conn):
    frozen, fields = contract.freeze(conn, repo, "rr-001", runbook=REPAIR, task=None, session="test")
    folder = paths.contract_dir(repo, "rr-001")
    assert frozen.reproducers == {"stable-keys": "contract/hidden/stable-keys"}
    run_sh = os.path.join(folder, "hidden", "stable-keys", "run.sh")
    with open(run_sh, "rb") as fh:
        body = fh.read()
    manifest = json.load(open(os.path.join(folder, "files.json")))
    assert manifest["hidden/stable-keys/run.sh"]["sha256"] == util.sha256_hex(body)
    assert os.access(run_sh, os.X_OK)
    # ${REPRODUCER_DIR} of a store case is that copy; the case's fix terms are read from it
    assert paths.hidden_dir(repo, "rr-001", "stable-keys") == os.path.join(repo, ".alpaca", "research", "rr-001",
                                                                          frozen.reproducers["stable-keys"])
    assert [r.fix_terms for r in frozen.repair] == [("0f4c2a9",), ()]
    assert frozen.repair[0].base == "0f4c2a9e1b7d3c5a8e6f0b2d4c6a8e0f1b3d5c7a" and frozen.repair[1].base is None
    os.remove(os.path.join(repo, "contracts/research/sortbench-repair/stable-keys/run.sh"))
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, repo, "rr-002", runbook=REPAIR, task=None, session="test")
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-REPRODUCER-MISSING" and exc.code == vc.BLOCKED
    assert not os.path.exists(paths.contract_dir(repo, "rr-002"))


def _start(conn, repo, run="rr-001", runbook=IMPROVE, task=None):
    frozen, fields = contract.freeze(conn, repo, run, runbook=runbook, task=task, session="test")
    events.emit(conn, events.RUN_START, run=run, ref=run, op=None, session="test",
                data={"intent": fields["intent"], "contract_sha256": fields["contract_sha256"],
                      "files_sha256": fields["files_sha256"], "override_diff": fields["override_diff"],
                      "stamps": fields["stamps"]})
    return frozen, fields


def _writable(path):
    """What any same-user writer can do first: give a read-only frozen copy its write bit back."""
    os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
    return path


def test_load_frozen_refuses_a_changed_contract_or_frozen_file(repo, conn):
    _start(conn, repo)
    folder = paths.contract_dir(repo, "rr-001")
    target = os.path.join(folder, "files", "packs", "sortbench", "bench", "measure.py")
    original = _git(repo, "show", "HEAD:packs/sortbench/bench/measure.py")      # still in git
    if os.geteuid() != 0:                     # the copy is read-only: a plain write fails
        with pytest.raises(PermissionError):
            open(target, "a").close()
    with open(_writable(target), "a", encoding="utf-8") as fh:
        fh.write("# softer\n")
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.load_frozen(conn, repo, "rr-001")
    assert err.value.reason == "RUN-CONTRACT-CHANGED" and err.value.code == vc.FAIL
    assert "measure.py of rr-001 changed" in err.value.detail
    # the bytes put back do not undo the edit: the file's ctime moved, and no writer sets it back
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(original)
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.load_frozen(conn, repo, "rr-001")
    assert "files/packs/sortbench/bench/measure.py of rr-001 was written" in err.value.detail
    assert "changed" not in err.value.detail
    # a file the manifest does not list, an edited contract.json, a missing contract.json
    _start(conn, repo, run="rr-002")
    _write(paths.contract_dir(repo, "rr-002"), "files/extra.py", "print('added')\n")
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.load_frozen(conn, repo, "rr-002")
    assert "does not list: files/extra.py" in err.value.detail
    _start(conn, repo, run="rr-003")
    path = _writable(os.path.join(paths.contract_dir(repo, "rr-003"), "contract.json"))
    text = open(path, encoding="utf-8").read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace('"slots":2', '"slots":9'))
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.load_frozen(conn, repo, "rr-003")
    assert "intent record" in err.value.detail
    os.remove(path)
    with pytest.raises(errors.ResearchRefusal):
        contract.read_frozen(repo, "rr-003")


def test_a_contract_edit_during_a_run_is_refused_and_recorded(repo, conn):
    tid = _task(conn, {"metrics": [{"id": "compares", "target": 17000}]})
    _start(conn, repo, task=tid)
    before = _events(conn)
    with pytest.raises(ValueError) as err:
        taskcontract.record(conn, tid, {"done_bar": ["x"], "research": {"stop": {"slots": 1}}},
                            session="agent", actor="agent")
    assert "CONTRACT-EDIT-DURING-RUN" in str(err.value)
    drift = db.events(conn, kind=events.CONTRACT_DRIFT)
    assert len(drift) == 1 and _events(conn) == before + 1
    data = drift[0]["data"]
    assert data["code"] == "CONTRACT-EDIT-DURING-RUN" and data["verdict"] == "FAIL" and data["what"] == "override"
    assert data["frozen"] == {"metrics": [{"id": "compares", "target": 17000}]}
    assert data["now"] == {"stop": {"slots": 1}} and data["run"] == "rr-001"
    assert set(data) >= {"what", "field", "frozen", "now", "code"} and "task" not in data     # architecture 8.3
    assert taskcontract.latest(conn)[tid]["research"] == {"metrics": [{"id": "compares", "target": 17000}]}
    # an edit that keeps the frozen part passes; another task is not bound to the run
    taskcontract.record(conn, tid, {"done_bar": ["y"], "research": {"metrics": [{"id": "compares", "target": 17000}]}},
                        session="agent", actor="agent")
    other = _task(conn)
    taskcontract.record(conn, other, {"done_bar": ["z"], "research": {"stop": {"slots": 1}}}, session="agent", actor="agent")
    # after the run's finalize the task may change again
    events.emit(conn, events.FINALIZE, run="rr-001", ref="rr-001", op=None, session="test", code=vc.PASS,
                data={"state": "finished"})
    taskcontract.record(conn, tid, {"done_bar": ["x"], "research": {"stop": {"slots": 1}}}, session="agent", actor="agent")
    assert len(db.events(conn, kind=events.CONTRACT_DRIFT)) == 1


def test_drift_records_each_edit_of_the_pack_or_the_store_once_and_the_run_keeps_its_copy(repo, conn):
    _start(conn, repo, run="rr-001", runbook=REPAIR)
    assert contract.drift(conn, repo, "rr-001") == []
    _edit(repo, REPAIR, "min_tests: 12", "min_tests: 1")
    _write(repo, "contracts/research/sortbench-repair/stable-keys/run.sh", "exit 0\n", 0o755)
    _edit(repo, "pyproject.toml", "sortlib", "sortlib-lax")
    lines = contract.drift(conn, repo, "rr-001")
    fields = sorted(line.split(":")[0] for line in lines)
    # the runbook itself, its frozen copy under files/, a protected file outside the folder, the store
    assert fields == ["runbook files[packs/sortbench/repair.yaml]", "runbook files[pyproject.toml]",
                      "runbook reproducers[stable-keys]/run.sh", "runbook runbook"]
    recorded = db.events(conn, kind=events.CONTRACT_DRIFT)
    assert len(recorded) == 4 and all(e["data"]["code"] == "CONTRACT-EDIT-DURING-RUN" for e in recorded)
    assert contract.drift(conn, repo, "rr-001") == lines
    assert len(db.events(conn, kind=events.CONTRACT_DRIFT)) == 4
    # the run never uses the edit: its frozen copy is unchanged and still loads
    frozen = contract.load_frozen(conn, repo, "rr-001")
    assert frozen.pass_to_pass.min_tests == 12
    with open(os.path.join(paths.contract_dir(repo, "rr-001"), "hidden", "stable-keys", "run.sh")) as fh:
        assert fh.read() == "echo reproduce\n"


def test_drift_records_an_override_edit_of_the_bound_task(repo, conn):
    tid = _task(conn, {"stop": {"slots": 1}})
    _start(conn, repo, task=tid)
    # a run start that names no intent record binds no task
    events.emit(conn, events.RUN_START, run="rr-009", ref="rr-009", op=None, session="test", data={"mode": "improve"})
    # a contract change written behind the guard's back (a raw event) is still caught at round open
    db.append_event(conn, session="raw", actor="agent", kind=taskcontract.KIND, op="op-001", ref=tid,
                    data={"done_bar": ["x"], "research": {"stop": {"slots": 2}}})
    lines = contract.drift(conn, repo, "rr-001")
    assert lines == ['override research: frozen {"stop":{"slots":1}}, now {"stop":{"slots":2}}']
    assert db.events(conn, kind=events.CONTRACT_DRIFT)[-1]["data"]["what"] == "override"


def _drift_events(conn):
    return db.events(conn, kind=events.CONTRACT_DRIFT)


def test_an_edit_of_the_frozen_contract_json_after_runner_start_is_refused_and_recorded(repo, conn):
    _start(conn, repo)
    contract.load_frozen(conn, repo, "rr-001")                # the runner start passes
    path = _writable(os.path.join(paths.contract_dir(repo, "rr-001"), "contract.json"))
    obj = json.loads(open(path, encoding="utf-8").read())
    obj["block"]["paths"]["editable"] = ["src/", "packs/"]
    obj["block"]["metrics"][0]["target"] = 99999999
    obj["contract_sha256"] = contract.contract_sha256(obj)    # the edit re-hashes itself
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(util.canonical_json(obj))
    # what launch reads at every launch refuses it
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.read_frozen(repo, "rr-001")
    assert err.value.reason == "RUN-CONTRACT-CHANGED"
    # the round open records it once and refuses to go on with it
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.drift(conn, repo, "rr-001")
    assert err.value.reason == "RUN-CONTRACT-CHANGED" and "intent record" in err.value.detail
    recorded = _drift_events(conn)
    assert len(recorded) == 1
    data = recorded[0]["data"]
    assert (data["what"], data["field"], data["code"], data["run"]) == (
        "frozen", "contract.json", "CONTRACT-EDIT-DURING-RUN", "rr-001")
    with pytest.raises(errors.ResearchRefusal):
        contract.drift(conn, repo, "rr-001")
    assert len(_drift_events(conn)) == 1
    with pytest.raises(errors.ResearchRefusal):
        contract.load_frozen(conn, repo, "rr-001")


def test_read_frozen_compares_contract_json_with_the_bar_even_where_the_hash_does_not_look(repo, conn):
    frozen, _fields = _start(conn, repo)
    assert contract.read_frozen(repo, "rr-001") == frozen
    path = _writable(os.path.join(paths.contract_dir(repo, "rr-001"), "contract.json"))
    obj = json.loads(open(path, encoding="utf-8").read())
    obj["read_real"] = ["/somewhere/else"]                   # outside the hash, inside the bar
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(util.canonical_json(obj))
    assert contract.contract_sha256(obj) == obj["contract_sha256"]
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.read_frozen(repo, "rr-001")
    assert "intent record" in err.value.detail


def test_read_frozen_before_the_run_start_holds_contract_json_to_its_own_hash(repo, conn):
    frozen, _fields = contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=None, session="test")
    assert contract.read_frozen(repo, "rr-001") == frozen
    path = _writable(os.path.join(paths.contract_dir(repo, "rr-001"), "contract.json"))
    text = open(path, encoding="utf-8").read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace('"slots":2', '"slots":9'))
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.read_frozen(repo, "rr-001")
    assert "contract_sha256" in err.value.detail


def test_an_edit_of_a_frozen_file_after_runner_start_is_recorded_at_the_round_open(repo, conn):
    _start(conn, repo)
    contract.load_frozen(conn, repo, "rr-001")
    target = os.path.join(paths.contract_dir(repo, "rr-001"), "files", "packs", "sortbench", "bench", "measure.py")
    with open(_writable(target), "a", encoding="utf-8") as fh:
        fh.write("print('METRIC: compares 1')\n")
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.drift(conn, repo, "rr-001")
    assert err.value.reason == "RUN-CONTRACT-CHANGED"
    recorded = _drift_events(conn)
    assert [(e["data"]["what"], e["data"]["field"]) for e in recorded] == [
        ("frozen", "files/packs/sortbench/bench/measure.py")]
    assert recorded[0]["data"]["now"] == util.sha256_hex(open(target, "rb").read())


def _swap_for_link(path, tmp_path):
    """Move `path` (a file or a folder) to storage outside the project and leave a symbolic link to
    it with the same bytes; returns where the bytes now live."""
    away = tmp_path / ("away-" + os.path.basename(path))
    shutil.move(path, str(away))
    os.symlink(str(away), path)
    return away


@pytest.mark.parametrize("which, field, launch_refuses", [
    ("contract.json", "contract.json", True),
    ("", "contract", True),
    ("files/packs/sortbench/bench/measure.py", "files/packs/sortbench/bench/measure.py", False),
    ("files/packs/sortbench/bench", "files/packs/sortbench/bench", False),
])
def test_a_frozen_path_swapped_for_a_link_with_the_same_bytes_is_refused_and_recorded(
        repo, conn, tmp_path, which, field, launch_refuses):
    _start(conn, repo)
    folder = paths.contract_dir(repo, "rr-001")
    path = os.path.join(folder, which) if which else folder
    away = _swap_for_link(path, tmp_path)
    assert os.path.islink(path)
    # the same bytes are reachable through the link, and still nothing is read through it
    if os.path.isfile(str(away)):
        with open(path, "rb") as fh:
            assert fh.read() == away.read_bytes()
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.load_frozen(conn, repo, "rr-001")
    assert err.value.reason == "RUN-CONTRACT-CHANGED" and "link" in err.value.detail
    if launch_refuses:
        with pytest.raises(errors.ResearchRefusal):
            contract.read_frozen(repo, "rr-001")
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.drift(conn, repo, "rr-001")
    assert err.value.reason == "RUN-CONTRACT-CHANGED"
    recorded = _drift_events(conn)
    assert [(e["data"]["what"], e["data"]["field"], e["data"]["now"]) for e in recorded] == [
        ("frozen", field, "not a plain file or folder")]
    assert all(e["data"]["code"] == "CONTRACT-EDIT-DURING-RUN" for e in recorded)


def test_a_special_file_in_the_frozen_folders_is_refused(repo, conn):
    _start(conn, repo)
    fifo = os.path.join(paths.contract_dir(repo, "rr-001"), "files", "packs", "sortbench", "pipe")
    os.mkfifo(fifo)
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.load_frozen(conn, repo, "rr-001")
    assert "files/packs/sortbench/pipe" in err.value.detail


def _loosen_frozen_json(repo, run, target=99999):
    """Edit the frozen contract.json as an actor would: a looser target and a recomputed
    contract_sha256; returns the new text."""
    path = _writable(os.path.join(paths.contract_dir(repo, run), "contract.json"))
    obj = json.loads(open(path, encoding="utf-8").read())
    for item in obj["block"]["metrics"]:
        if item["id"] == "compares":
            item["target"] = target
    obj["contract_sha256"] = contract.contract_sha256(obj)
    text = util.canonical_json(obj)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def _stamps_now(folder, stamps):
    """Fresh stamps of the same paths, as an actor who re-stamped the copy would write them."""
    return dict((rel, [os.lstat(os.path.join(folder, rel)).st_ino, os.lstat(os.path.join(folder, rel)).st_ctime_ns])
                for rel in stamps)


def test_the_grant_terms_are_the_terminal_events_and_a_conflicting_event_voids_them(repo, conn):
    tid = _task(conn, {"metrics": [{"id": "compares", "target": 99999}]})
    ref_ = contract.ref(IMPROVE, tid)
    dec = _grant(conn, repo, ref_, {"research.metrics[compares].target": 19000})
    # an appended event, not confirmed at the terminal, names the owner's decision with other terms
    events.emit(conn, events.GRANT, run=None, ref=ref_, op=None, session="agent", actor="agent",
                data={"ref": ref_, "gate": "loosen", "hold": None, "decision": dec,
                      "terms": {"set": {"research.metrics[compares].target": 99999}},
                      "note": "appended", "confirmed": "none", "by": "agent"})
    assert owner.authorized(conn, ref_, "loosen") == (True, dec)
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=tid, session="test")
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-LOOSEN-UNGRANTED"
    assert [(e["field"], e["new"]) for e in exc.loosened] == [("research.metrics[compares].target", 99999)]
    assert not os.path.lexists(paths.contract_dir(repo, "rr-001"))
    # while two events give one decision different terms, even the owner's own terms grant nothing
    taskcontract.record(conn, tid, {"done_bar": ["x"], "research": {"metrics": [{"id": "compares", "target": 19000}]}},
                        session="test", actor="human")
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=tid, session="test")
    assert _refusal(err).reason == "CONTRACT-LOOSEN-UNGRANTED"
    # the same owner grant with no conflicting event is accepted with its own terms
    other = _task(conn, {"metrics": [{"id": "compares", "target": 19000}]})
    dec2 = _grant(conn, repo, contract.ref(IMPROVE, other), {"research.metrics[compares].target": 19000})
    frozen, fields = contract.freeze(conn, repo, "rr-002", runbook=IMPROVE, task=other, session="test")
    assert frozen.targets[0].value == 19000 and fields["override_diff"][0]["grant"] == dec2


def test_read_frozen_before_the_run_start_needs_a_freeze_record_whose_bar_is_contract_json(repo, conn):
    _frozen, fields = contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=None, session="test")
    text = _loosen_frozen_json(repo, "rr-001")
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.read_frozen(repo, "rr-001")
    assert "intent record" in err.value.detail
    # an intent record with the new bar that freeze did not write: another label, or freeze's label
    # with another author
    label = "research contract sortbench-improve (%s, task -)" % IMPROVE
    forged = intent.record(conn, "research contract sortbench-improve (forged)", text, "agent", "local:x",
                           author_role=intent.AGENT_ROLE, session="agent")
    intent.record(conn, label, text, "agent", "local:x", author_role=intent.AGENT_ROLE, session="agent")
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.read_frozen(repo, "rr-001")
    assert "intent record" in err.value.detail
    # a run-start that names such a record is refused too
    events.emit(conn, events.RUN_START, run="rr-001", ref="rr-001", op=None, session="agent",
                data={"intent": forged["id"], "contract_sha256": json.loads(text)["contract_sha256"],
                      "files_sha256": fields["files_sha256"],
                      "stamps": _stamps_now(paths.contract_dir(repo, "rr-001"), fields["stamps"])})
    for check in (lambda: contract.load_frozen(conn, repo, "rr-001"), lambda: contract.read_frozen(repo, "rr-001")):
        with pytest.raises(errors.ResearchRefusal) as err:
            check()
        assert "not one freeze wrote" in err.value.detail


def test_a_later_run_start_that_names_another_contract_is_refused_and_recorded(repo, conn):
    _frozen, fields = _start(conn, repo)
    contract.load_frozen(conn, repo, "rr-001")
    text = _loosen_frozen_json(repo, "rr-001")
    # the strongest forgery: freeze's own label and author, fresh stamps, and a second run-start
    rec = intent.record(conn, "research contract sortbench-improve (%s, task -)" % IMPROVE, text, "alpaca-research",
                        "local:x", author_role=intent.AGENT_ROLE, session="agent")
    events.emit(conn, events.RUN_START, run="rr-001", ref="rr-001", op=None, session="agent",
                data={"intent": rec["id"], "contract_sha256": json.loads(text)["contract_sha256"],
                      "files_sha256": fields["files_sha256"],
                      "stamps": _stamps_now(paths.contract_dir(repo, "rr-001"), fields["stamps"])})
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.load_frozen(conn, repo, "rr-001")
    assert "later research-run-start event of rr-001" in err.value.detail
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.read_frozen(repo, "rr-001")
    assert "later research-run-start event of rr-001" in err.value.detail
    with pytest.raises(errors.ResearchRefusal):
        contract.drift(conn, repo, "rr-001")
    recorded = _drift_events(conn)
    assert sorted(e["data"]["field"] for e in recorded) == ["contract.json", "run-start"]
    assert all(e["data"]["what"] == "frozen" and e["data"]["code"] == "CONTRACT-EDIT-DURING-RUN" for e in recorded)
    start = [e for e in recorded if e["data"]["field"] == "run-start"][0]["data"]
    assert start["frozen"]["intent"] == fields["intent"] and start["now"]["intent"] == rec["id"]
    # the task a live run is bound to is read from the first run-start too
    with pytest.raises(errors.ResearchRefusal):
        contract.drift(conn, repo, "rr-001")
    assert len(_drift_events(conn)) == 2


def test_a_run_start_without_stamps_is_refused(repo, conn):
    _frozen, fields = contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=None, session="test")
    events.emit(conn, events.RUN_START, run="rr-001", ref="rr-001", op=None, session="test",
                data={"intent": fields["intent"], "contract_sha256": fields["contract_sha256"],
                      "files_sha256": fields["files_sha256"]})
    for check in (lambda: contract.load_frozen(conn, repo, "rr-001"), lambda: contract.read_frozen(repo, "rr-001")):
        with pytest.raises(errors.ResearchRefusal) as err:
            check()
        assert "carries no stamps" in err.value.detail


def test_an_edit_undone_between_two_checks_is_refused_at_the_launch_and_recorded(repo, conn):
    _start(conn, repo)
    contract.load_frozen(conn, repo, "rr-001")                # the runner start passes
    field = "files/packs/sortbench/bench/measure.py"
    target = os.path.join(paths.contract_dir(repo, "rr-001"), field)
    before, mode = open(target, "rb").read(), stat.S_IMODE(os.stat(target).st_mode)
    with open(_writable(target), "w", encoding="utf-8") as fh:
        fh.write("print('METRIC: compares 1')\n")              # a gate would run this softer copy now
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.read_frozen(repo, "rr-001")                  # the next launch refuses it
    assert "%s of rr-001 was written" % field in err.value.detail
    with open(target, "wb") as fh:                            # put back before the next round opens
        fh.write(before)
    os.chmod(target, mode)
    assert open(target, "rb").read() == before and stat.S_IMODE(os.stat(target).st_mode) == mode
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.drift(conn, repo, "rr-001")
    assert err.value.reason == "RUN-CONTRACT-CHANGED"
    st = os.lstat(target)
    recorded = _drift_events(conn)
    assert [(e["data"]["what"], e["data"]["field"], e["data"]["now"]) for e in recorded] == [
        ("frozen", field, [st.st_ino, st.st_ctime_ns])]
    with pytest.raises(errors.ResearchRefusal):
        contract.load_frozen(conn, repo, "rr-001")
    with pytest.raises(errors.ResearchRefusal):
        contract.drift(conn, repo, "rr-001")
    assert len(_drift_events(conn)) == 1


@pytest.mark.parametrize("up", [1, 2], ids=[".alpaca/research", ".alpaca"])
def test_a_folder_above_the_run_swapped_for_a_link_is_refused_and_recorded(repo, conn, up):
    _start(conn, repo)
    above = paths.run_dir(repo, "rr-001")
    for _ in range(up):
        above = os.path.dirname(above)
    moved = os.path.join(repo, "src", "sortlib", "moved")     # a folder an agent may edit
    shutil.copytree(above, moved, symlinks=True)
    shutil.rmtree(above)
    os.symlink(moved, above)
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.load_frozen(conn, repo, "rr-001")
    assert "a folder on the way is a link" in err.value.detail
    with pytest.raises(errors.ResearchRefusal) as err:
        contract.read_frozen(repo, "rr-001")
    assert "a folder on the way is a link" in err.value.detail
    with pytest.raises(errors.ResearchRefusal):
        contract.drift(conn, repo, "rr-001")
    recorded = _drift_events(conn)
    assert [(e["data"]["what"], e["data"]["field"], e["data"]["now"]) for e in recorded] == [
        ("frozen", "contract", "not a plain file or folder")]


def test_a_runbook_reached_through_a_folder_link_keys_the_baseline_of_its_real_path(repo, conn):
    _start(conn, repo)
    _edit(repo, IMPROVE, "      target: 18000\n", "      target: 99999\n")
    _commit(repo)
    os.symlink("sortbench", os.path.join(repo, "packs", "alias"))
    alias = "packs/alias/improve.yaml"
    assert contract.preview(repo, alias)["contract"]["runbook"] == IMPROVE
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, repo, "rr-002", runbook=alias, task=None, session="test")
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-LOOSEN-UNGRANTED"
    assert [(e["ref"], e["field"]) for e in exc.loosened] == [(PACK_REF_IMPROVE, "research.metrics[compares].target")]


# --------------------------------------------------------------------------- overlap rules
def test_contract_unprotected_for_a_tracked_pack_file_no_protected_rule_hits(repo, conn):
    _edit(repo, "packs/sortbench/pack.yaml", "protected: [packs/sortbench/, pyproject.toml]",
          "protected: [packs/sortbench/*.yaml, packs/sortbench/*.md, pyproject.toml]")
    _commit(repo)
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=None, session="test")
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-UNPROTECTED" and "packs/sortbench/bench/measure.py" in exc.detail
    assert not os.path.exists(paths.contract_dir(repo, "rr-001"))


def _frozen_with(repo, **block_changes):
    """The frozen contract of the improve runbook with parts of its block changed; a changed
    fingerprint part drops the preview's resolved tools, so check_overlap resolves them again."""
    obj = contract.preview(repo, IMPROVE)["contract"]
    block = dict(obj["block"])
    for part, value in block_changes.items():
        block[part] = dict(block[part], **value) if isinstance(value, dict) else value
    tools = [] if "fingerprint" in block_changes else obj["tools_real"]
    return contract.from_block(dict(obj, block=block, tools_real=tools))


@pytest.mark.parametrize("editable,what", [
    (["src/sortlib/", "alpaca/research/"], "alpaca/"),
    (["src/sortlib/", "bin/"], "bin/"),
    (["src/sortlib/", "packs/sortbench/bench/measure.py"], "protected rule packs/sortbench/"),
    (["src/sortlib/", "pyproject.toml"], "protected rule pyproject.toml"),
    (["src/sortlib/", "contracts/research/"], "contracts/research/"),
    (["src/sortlib/", "packs/sortbench/holdout/inputs.json"], "packs/sortbench/holdout/"),
])
def test_an_editable_rule_over_engine_code_a_protected_file_the_store_or_held_out_data_is_refused(repo, editable, what):
    frozen = _frozen_with(repo, paths={"editable": editable})
    with pytest.raises(errors.ContractRefusal) as err:
        contract.check_overlap(repo, frozen, run="rr-001")
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-ALLOWLIST-OVERLAP" and exc.code == vc.FAIL and what in exc.detail, exc.detail
    contract.check_overlap(repo, _frozen_with(repo), run="rr-001")


def test_the_allowlist_overlap_is_refused_and_recorded_by_freeze_before_the_run(repo, conn):
    # a glob that admits a protected file is refused at freeze and nothing is written
    _edit(repo, IMPROVE, "editable: [src/sortlib/]", "editable: [src/sortlib/, py*.toml]")
    _commit(repo)
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=None, session="test")
    assert _refusal(err).reason == "CONTRACT-ALLOWLIST-OVERLAP" and "pyproject.toml" in err.value.detail
    assert not os.path.exists(paths.contract_dir(repo, "rr-001"))
    assert db.events(conn, kind="intent-record") == []


def _read_only(folder):
    for base, dirs, names in os.walk(folder):
        for name in dirs:
            os.chmod(os.path.join(base, name), 0o555)
    os.chmod(folder, 0o555)


@pytest.fixture
def outside(tmp_path):
    """A folder outside the project root, the home and the runtime folders."""
    folder = tmp_path / "shared-data"
    folder.mkdir()
    (folder / "sub").mkdir()
    (folder / "sub" / "data.txt").write_text("data\n", encoding="utf-8")
    yield str(folder)
    for base, dirs, _names in os.walk(str(folder)):
        for name in dirs:
            os.chmod(os.path.join(base, name), 0o755)
    os.chmod(str(folder), 0o755)


def test_a_read_only_read_path_outside_the_forbidden_paths_passes(repo, outside):
    _read_only(outside)
    frozen = _frozen_with(repo, paths={"read": [outside]})
    frozen = contract.from_block(dict(contract.preview(repo, IMPROVE)["contract"], block=frozen.block,
                                      read_real=[os.path.realpath(outside)]))
    contract.check_overlap(repo, frozen, run="rr-001")
    assert contract.check_read_paths(frozen) == []


@pytest.mark.parametrize("make,finding", [
    ("socket", "socket"), ("writable", "writable"), ("world", "writable")])
def test_a_read_path_with_a_socket_or_a_writable_folder_is_refused(repo, outside, make, finding,
                                                                   monkeypatch):
    if make == "socket":
        # bound by a relative name from inside its folder: a deep tmp path (a release gate's own
        # TMPDIR) would pass the Unix socket path limit
        with monkeypatch.context() as here:
            here.chdir(os.path.join(outside, "sub"))
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind("agent.sock")
            sock.close()
    _read_only(outside)
    if make == "writable":
        os.chmod(os.path.join(outside, "sub"), 0o755)
    elif make == "world":
        os.chmod(os.path.join(outside, "sub"), 0o557)
    obj = contract.preview(repo, IMPROVE)["contract"]
    block = dict(obj["block"], paths=dict(obj["block"]["paths"], read=[outside]))
    frozen = contract.from_block(dict(obj, block=block, read_real=[os.path.realpath(outside)]))
    found = contract.check_read_paths(frozen)
    assert found and all(finding in f for f in found) and os.path.join(outside, "sub") in found[0]
    with pytest.raises(errors.ContractRefusal) as err:
        contract.check_overlap(repo, frozen, run="rr-001")
    assert _refusal(err).reason == "CONTRACT-ALLOWLIST-OVERLAP"


@pytest.mark.parametrize("where", ["root", "alpaca", "home", "run"])
def test_a_read_path_inside_a_forbidden_path_is_refused(repo, where, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = {"root": repo, "alpaca": os.path.join(repo, "alpaca"), "home": str(home),
              "run": os.path.join(repo, ".alpaca", "research", "rr-001")}[where]
    os.makedirs(target, exist_ok=True)
    obj = contract.preview(repo, IMPROVE)["contract"]
    block = dict(obj["block"], paths=dict(obj["block"]["paths"], read=[target]))
    frozen = contract.from_block(dict(obj, block=block, read_real=[os.path.realpath(target)]))
    with pytest.raises(errors.ContractRefusal) as err:
        contract.check_overlap(repo, frozen, run="rr-001")
    assert _refusal(err).reason == "CONTRACT-ALLOWLIST-OVERLAP" and "forbidden path" in err.value.detail


def test_a_tool_that_resolves_nowhere_or_inside_the_project_is_refused(repo, monkeypatch, tmp_path):
    bin_dir = tmp_path / "tools"
    bin_dir.mkdir()
    tool = bin_dir / "sorttool"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    inside = os.path.join(repo, "localbin")
    os.makedirs(inside)
    _write(repo, "localbin/rogue", "#!/bin/sh\nexit 0\n", 0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([inside, str(bin_dir), os.environ.get("PATH", "")]))
    ok = _frozen_with(repo, fingerprint={"tools": ["sorttool"]})
    contract.check_overlap(repo, ok, run="rr-001")
    # a tool found only inside the project root is left off the tool PATH, so it resolves nowhere
    rogue = _frozen_with(repo, fingerprint={"tools": ["rogue"]})
    with pytest.raises(errors.ContractRefusal) as err:
        contract.check_overlap(repo, rogue, run="rr-001")
    assert "tool rogue does not resolve" in err.value.detail
    frozen_inside = contract.from_block(dict(contract.preview(repo, IMPROVE)["contract"],
                                             tools_real=[os.path.join(inside, "rogue")]))
    with pytest.raises(errors.ContractRefusal) as err:
        contract.check_overlap(repo, frozen_inside, run="rr-001")
    assert "inside forbidden path" in err.value.detail


# ------------------------------------------------------------------ refusal codes and verdicts
def test_every_loader_code_is_raised_as_a_contract_refusal_with_its_verdict(repo, conn):
    want = {"RESEARCH-NOT-FORMAT-3": vc.FAIL, "CONTRACT-CHECK-FAILED": vc.FAIL, "CONTRACT-CHECK-BLOCKED": vc.BLOCKED,
            "CONTRACT-TASK-UNKNOWN": vc.FAIL, "CONTRACT-REPO-UNREADABLE": vc.BLOCKED,
            "CONTRACT-PACK-OUTSIDE-REPO": vc.FAIL, "CONTRACT-REPRODUCER-MISSING": vc.BLOCKED,
            "CONTRACT-OVERRIDE-INVALID": vc.FAIL, "CONTRACT-LOOSEN-UNGRANTED": vc.PAUSED,
            "CONTRACT-ALLOWLIST-OVERLAP": vc.FAIL, "CONTRACT-UNPROTECTED": vc.FAIL,
            "CONTRACT-EDIT-DURING-RUN": vc.FAIL, "RUN-CONTRACT-CHANGED": vc.FAIL}
    for reason, code in want.items():
        assert errors.CODES[reason] == code, reason
    with pytest.raises(errors.ContractRefusal) as err:
        contract.preview(repo, IMPROVE, task="t-404", conn=conn)
    exc = _refusal(err)
    assert exc.reason == "CONTRACT-TASK-UNKNOWN" and exc.code == vc.FAIL
    with pytest.raises(ValueError):
        contract.preview(repo, IMPROVE, task="t-404")


def test_ref_names_the_runbook_and_the_task():
    assert contract.ref("packs/sortbench/improve.yaml", "t-140") == "contract:packs/sortbench/improve.yaml#t-140"
    assert contract.ref("packs/sortbench/improve.yaml", None) == "contract:packs/sortbench/improve.yaml#-"


# ------------------------------------------------------------------------ owner.authorized
def test_authorized_needs_a_terminal_grant_from_the_owner_of_the_right_kind(repo, conn):
    ref_ = contract.ref(IMPROVE, "t-1")
    assert owner.authorized(conn, ref_, "loosen") == (False, "RESEARCH-OWNER-GRANT-MISSING")
    _grant(conn, repo, ref_, {"research.stop.slots": 3}, confirmed="pty")
    _grant(conn, repo, ref_, {"research.stop.slots": 3}, actor="agent")
    _grant(conn, repo, ref_, {"research.stop.slots": 3}, kind="go")
    assert owner.authorized(conn, ref_, "loosen")[0] is False
    dec = _grant(conn, repo, ref_, {"research.stop.slots": 3})
    assert owner.authorized(conn, ref_, "loosen") == (True, dec)
    assert owner.authorized(conn, ref_, "loosen", decision_id="dec-other")[0] is False
    assert owner.authorized(conn, contract.ref(IMPROVE, "t-2"), "loosen")[0] is False
    # a budget grant answers only the hold open now
    hold = events.emit(conn, events.HOLD, run=None, ref=ref_, op=None, session="test", code=vc.PAUSED,
                       reason="RUN-BUDGET-HOLD", data={"ref": ref_, "gate": "budget", "detail": "caps"})
    stale = _grant(conn, repo, ref_, {}, gate="budget", kind="go")
    assert owner.authorized(conn, ref_, "budget")[0] is False
    dec = decisions.record(conn, "go", "start held on the budget (hold %s)" % hold["id"], ["start", "keep"], "start",
                           "runs at the project budget", pointer="research-grant:%s:budget:%s" % (ref_, hold["id"]),
                           actor="owner", session="owner", root=repo)
    events.emit(conn, events.GRANT, run=None, ref=ref_, op=None, session="owner", actor="owner",
                data={"ref": ref_, "gate": "budget", "hold": hold["id"], "decision": dec["id"], "terms": {},
                      "note": "", "confirmed": "terminal", "by": "owner"})
    assert owner.authorized(conn, ref_, "budget") == (True, dec["id"])
    events.emit(conn, "research-hold-lifted", run=None, ref=ref_, op=None, session="test",
                data={"hold": hold["id"], "decision": dec["id"], "answer": "budget"})
    assert owner.authorized(conn, ref_, "budget")[0] is False
    assert stale != dec["id"]


# ------------------------------------------------------ format 2 compatibility (section 2.3)
def test_loader_refuses_format_2_and_writes_nothing(repo, conn):
    shutil.copytree(os.path.join(REPO, "templates", "runbook-example"), os.path.join(repo, "domain"))
    before = _events(conn)
    with pytest.raises(errors.ContractRefusal) as err:
        contract.load_block(os.path.join(repo, "domain", "runbook.yaml"))
    exc = _refusal(err)
    assert exc.reason == "RESEARCH-NOT-FORMAT-3" and exc.code == vc.FAIL
    with pytest.raises(errors.ContractRefusal):
        contract.freeze(conn, repo, "rr-001", runbook="domain/runbook.yaml", task=None, session="test")
    assert _events(conn) == before
    assert not os.path.exists(os.path.join(repo, ".alpaca", "research"))


def test_task_contract_without_research_is_unchanged():
    data = {"input": ["requirements.txt"], "expected": [], "done_bar": ["install exits 0"],
            "fail_cases": ["the checks still fail after 2 attempt(s)"], "source": "domain/runbook.yaml stages[0]"}
    out = taskcontract.check(data)
    assert out == {"input": ["requirements.txt"], "expected": [], "done_bar": ["install exits 0"],
                   "fail_cases": ["the checks still fail after 2 attempt(s)"], "stage": None,
                   "source": "domain/runbook.yaml stages[0]"}
    assert list(out) == ["input", "expected", "done_bar", "fail_cases", "stage", "source"]
    kept = taskcontract.check(dict(data, research={"stop": {"slots": 1}}))
    assert kept["research"] == {"stop": {"slots": 1}}
    for bad in (["a"], "text", {"x": "y" * (taskcontract.MAX_RESEARCH + 1)}, {"x": {1, 2}}):
        with pytest.raises(ValueError):
            taskcontract.check(dict(data, research=bad))


def test_task_contract_record_without_run_is_unchanged(repo, conn, capsys):
    from alpaca import cli
    tid = ops.add_task(conn, "op-001", "a stage task", "stage task")
    before = _events(conn)
    path = _write(repo, "contract.json", json.dumps({"done_bar": ["install exits 0"], "input": ["requirements.txt"]}))
    assert cli.main(["--session", "test", "task", "contract", tid, "--file", path]) == 0
    capsys.readouterr()
    new = conn.execute("SELECT kind, ref, data FROM events WHERE id > ?", (before,)).fetchall()
    assert [(r["kind"], r["ref"]) for r in new] == [("task-contract", tid)]
    assert json.loads(new[0]["data"]) == {"input": ["requirements.txt"], "expected": [], "done_bar": ["install exits 0"],
                                          "fail_cases": [], "stage": None, "source": None}


def test_format_2_reintake_writes_no_event(project, capsys):
    """The f5869e9 intake of templates/runbook-example records the six-key contracts of the fixture
    (alpaca/tests/fixtures/research/format2-intake-contracts.json); with the guard in place the
    same intake records the same contracts, and a re-intake of the unchanged runbook writes no
    event."""
    from alpaca import cli
    with open(os.path.join(REPO, "alpaca", "tests", "fixtures", "research", "format2-intake-contracts.json")) as fh:
        want = json.load(fh)["contracts"]
    shutil.copytree(os.path.join(REPO, "templates", "runbook-example"), os.path.join(project, "domain"))
    assert cli.main(["op", "new", "Link shortener", "--done-when", "every row proven"]) == 0
    argv = ["intake", os.path.join(project, "domain", "spec.md"), os.path.join(project, "domain", "runbook.yaml"), "--json"]
    assert cli.main(argv) == 0
    conn = db.connect(project)
    got = [{"ref": e["ref"], "data": e["data"]} for e in db.events(conn, kind="task-contract", limit=10 ** 9)]
    assert got == want
    before = _events(conn)
    assert cli.main(argv) == 0
    capsys.readouterr()
    assert _events(conn) == before
