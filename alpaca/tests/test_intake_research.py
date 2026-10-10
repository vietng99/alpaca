"""t-103: `alpaca intake` for research runbooks (runbook format 3), design/research-engine/
runbook-format-3.md sections 2.3, 7.4, 7.7 and 14.3, FR-050, EC-020 and SC-014 of
design/research-engine/spec.md.

Done when (the contract of t-103):

  * a format 3 intake lands rows and the contract: one row per required spec item whose bar is the
    bars of its coverers, the research coverers included (a row shown only by them is labelled
    `research`); one task per stage whose statement names the stage's research role and whose
    contract names no stage; and one `research-contract` event (via "intake") holding the
    pack-level contract;
  * a re-intake of an unchanged runbook changes nothing: no event, no file, also after a commit
    that changes no file the contract freezes; a commit that changes one (a protected file outside
    the runbook folder) or a pack edit that loosens a field stops intake with exit 3 until an owner
    loosen grant names it, and then records the new baseline once;
  * every existing intake test passes (test_intake.py, test_intake_bar.py, test_intake_review3.py
    and test_start.py are not changed), and a stage task's `research` override is kept by a
    re-intake of a format 2 and of a format 3 runbook, also when intake records a new contract for
    the task while a live run is bound to it.

Fail case: intake writes a profile or a project.yaml change the owner did not ask for. Every
research intake here checks that project.yaml keeps its bytes and that no intake_profile.py
appears, also in a project whose profile a format 2 intake wrote earlier.

Every test copies the worked example (alpaca/tests/fixtures/research/sortbench/) into
packs/sortbench/ of a temporary git repository that is also the project root, as section 10 of
the design says. Grants are recorded as t-105's `grant` records them: an owner decision and a
research-grant event confirmed at the terminal. No test touches the network or starts an agent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from alpaca import cli, db, decisions, intake, intent, taskcontract, util
from alpaca.gates import verdict as vc
from alpaca.research import contract, events
from alpaca.tests.conftest import REPO

FIX = os.path.join(REPO, "alpaca", "tests", "fixtures", "research", "sortbench")
IMPROVE = "packs/sortbench/improve.yaml"
IMPROVE_SPEC = "packs/sortbench/improve-spec.md"
REPAIR = "packs/sortbench/repair.yaml"
REPAIR_SPEC = "packs/sortbench/repair-spec.md"
PACK_REF = "contract:%s#-" % IMPROVE


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


def _read(root, rel):
    with open(os.path.join(root, rel), "rb") as fh:
        return fh.read()


@pytest.fixture
def repo(project):
    """The worked example in packs/sortbench/ of a temporary git repository that is also the
    project root, committed once, with one open op."""
    root = project
    _write(root, "project.yaml", "# the sortbench test project\nname: sortbench-test\n")
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
    _write(root, "packs/sortbench/bench/measure.py", "print('METRIC: compares 17000')\n", 0o755)
    _write(root, "packs/sortbench/bench/prepare.py", "print('prepared')\n")
    _write(root, "packs/sortbench/holdout/inputs.json", "[5, 3, 1]\n")
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "base")
    assert cli.main(["op", "new", "Faster sortlib", "--done-when", "every row proven"]) == 0
    return root


@pytest.fixture
def conn(repo):
    return db.connect(repo)


def _intake(capsys, root, spec, runbook, *extra):
    """(exit code, parsed --json output) of one `alpaca intake`."""
    capsys.readouterr()
    code = cli.main(["--session", "test", "intake", os.path.join(root, spec), os.path.join(root, runbook), "--json"]
                    + list(extra))
    out = capsys.readouterr().out
    return code, json.loads(out)


def _event_count(conn):
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def _events_after(conn, n):
    return [(r["kind"], r["ref"]) for r in conn.execute("SELECT kind, ref FROM events WHERE id > ? ORDER BY id", (n,))]


def _files(root):
    """{relative path: bytes} of every file under the project root outside .git and the record."""
    out = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in names:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel.startswith(".alpaca/alpaca.db"):
                continue
            with open(full, "rb") as fh:
                out[rel] = fh.read()
    return out


def _contracts(conn):
    return db.events(conn, kind=contract.KIND_CONTRACT, limit=10 ** 9)


def _rows_by_key(result):
    return dict((r["key"], r) for group in result["rows"].values() for r in group)


def _grant(conn, root, ref_, terms):
    """What t-105's grant records: an owner decision and a research-grant event on ref_."""
    stamp = util.canonical_json(terms) + str(_event_count(conn))
    dec = decisions.record(conn, "done_when", "%s loosens %s (record through %s)" % (ref_, sorted(terms), stamp),
                           ["loosen", "keep the accepted values"], "loosen for %s" % ref_,
                           "the next intake or start of %s may use the granted values" % ref_,
                           pointer="research-grant:%s:loosen" % ref_, actor="owner", session="owner", root=root)
    events.emit(conn, events.GRANT, run=None, ref=ref_, op=None, session="owner", actor="owner",
                data={"ref": ref_, "gate": "loosen", "hold": None, "decision": dec["id"], "terms": {"set": terms},
                      "note": "test", "confirmed": "terminal", "by": "owner"})
    return dec["id"]


def _bind_live_run(conn, task, override, run="rr-001"):
    """A live research run bound to `task` whose frozen contract holds `override` as the task's
    research part: an intent record whose bar is the contract object and a run-start event that
    names it (the shape contract.freeze and the runner write)."""
    rec = intent.record(conn, "research contract test (%s, task %s)" % (IMPROVE, task),
                        util.canonical_json({"task": task, "override": override}), "alpaca-research",
                        "local:%s@test" % IMPROVE, author_role=intent.AGENT_ROLE, session="test")
    events.emit(conn, events.RUN_START, run=run, ref=run, op=None, session="test", data={"intent": rec["id"]})


# ------------------------------------------------------------ a format 3 intake: rows and contract
def test_format_3_intake_lands_rows_tasks_and_the_contract(repo, conn, capsys):
    project_yaml = _read(repo, "project.yaml")
    before = _event_count(conn)
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.PASS and out["verdict"] == "PASS" and out["changed"] is True

    # rows: one per required item of the spec (4 SC and 3 EC), each with the bars of its coverers
    rows = _rows_by_key(out)
    assert sorted(rows) == ["EC-001", "EC-002", "EC-003", "SC-001", "SC-002", "SC-003", "SC-004"]
    assert [r["key"] for r in out["rows"]["added"]] and not out["rows"]["kept"]
    sc1 = rows["SC-001"]
    assert sc1["stage"] == "research" and sc1["step"] == "check"
    assert sc1["shown_by"] == "research metric:compares"
    assert sc1["bar"] == ("metric compares: lower is better, comparisons; deterministic, median (worse) of 1 repeats, "
                          "at least 1 ok; beats the champion by more than 0 comparisons; target <= 18000 (owner only)")
    assert sc1["source"] == "spec:SC-001"
    # EC-002 is shown by the same metric; EC-003 by the paths part, whose protected rules pack.yaml sets
    assert rows["EC-002"]["bar"] == sc1["bar"] and rows["EC-002"]["stage"] == "research"
    assert rows["EC-003"]["bar"] == ("editable src/sortlib/; protected packs/sortbench/, pyproject.toml; a change "
                                     "outside the editable rules or to a protected path breaks the attempt")
    assert rows["SC-004"]["bar"].startswith("stop when every target is met or after 5 rounds of 2 slots")
    assert rows["SC-002"]["bar"].endswith("target <= 120 (owner only)")
    # a row a gate stage's check shows keeps its stage position, as in format 2
    assert rows["SC-003"]["stage"] == "2/3 unit" and rows["SC-003"]["shown_by"] == "unit/unit-pass"
    assert rows["EC-001"]["stage"] == "2/3 unit"

    # tasks: one per stage, the statement names the research role, the contract names no stage
    assert [t["key"] for t in out["tasks"]["added"]] == ["stage:build", "stage:unit", "stage:holdout-bench"]
    adds = dict((e["ref"], e["data"]) for e in db.events(conn, kind="task-add", limit=10 ** 9))
    statements = dict((a["intake"]["key"], a["statement"]) for a in adds.values())
    assert statements["stage:build"].startswith("Gate stage build of research runbook sortbench-improve, run in "
                                                "every attempt worktree by the gate chain. The package compiles. "
                                                "Command: python3 -m compileall")
    assert statements["stage:holdout-bench"].startswith("Holdout stage holdout-bench of research runbook "
                                                        "sortbench-improve, run on the final champion only.")
    latest = taskcontract.latest(conn)
    for entry in out["tasks"]["added"]:
        assert entry["stage"] is None
        got = latest[entry["task"]]
        assert got["stage"] is None and "research" not in got
    unit = latest[[t["task"] for t in out["tasks"]["added"] if t["key"] == "stage:unit"][0]]
    assert any(line.startswith("unit/unit-pass (json-field)") for line in unit["done_bar"])

    # the contract: one research-contract event, via intake, equal to what the loader reads
    landed = _contracts(conn)
    assert len(landed) == 1
    event = landed[0]
    pack = contract.load_block(os.path.join(repo, IMPROVE))
    assert event["ref"] == "sortbench-improve" and event["op"] == out["op"] == "op-001"
    assert event["data"]["via"] == "intake" and event["data"]["runbook"] == IMPROVE
    assert event["data"]["pack_sha256"] == pack["pack_sha256"] == out["contract"]["pack_sha256"]
    assert event["data"]["grants"] == [] and event["data"]["mode"] == "improve"
    assert event["data"]["block"] == pack["block"] and event["data"]["files"] == pack["files"]
    assert "pyproject.toml" in event["data"]["files"] and "packs/sortbench/lessons.md" in event["data"]["files"]
    assert out["contract"]["action"] == "land" and out["contract"]["diff"] == []

    # one intake event; it names the project's profile (none) and no profile was written
    kinds = [k for k, _r in _events_after(conn, before)]
    assert kinds.count("intake") == 1 and kinds.count(contract.KIND_CONTRACT) == 1
    assert db.events(conn, kind="intake", limit=10 ** 9)[-1]["data"]["profile"] is None
    assert out["profile"]["module"] is None
    assert _read(repo, "project.yaml") == project_yaml
    assert not os.path.exists(os.path.join(repo, intake.PROFILE_FILE))


def test_format_3_intake_prints_the_contract_and_no_profile(repo, capsys):
    capsys.readouterr()
    assert cli.main(["--session", "test", "intake", os.path.join(repo, IMPROVE_SPEC), os.path.join(repo, IMPROVE)]) == 0
    text = capsys.readouterr().out
    assert "profile: not written: a research runbook sets no profile; the project's profile stays unset" in text
    assert "contract: research-contract of sortbench-improve (packs/sortbench/improve.yaml)" in text
    assert "a new baseline is recorded" in text
    assert "(stage:build)" in text and "(stage None)" not in text
    assert text.rstrip().endswith("GATE alpaca-intake: PASS")


def test_format_3_dry_run_writes_nothing(repo, conn, capsys):
    before, files = _event_count(conn), _files(repo)
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE, "--dry-run")
    assert code == vc.PASS and out["dry_run"] is True and out["changed"] is True
    assert out["contract"]["action"] == "land"
    assert _event_count(conn) == before and _files(repo) == files


def test_repair_runbook_rows_name_cases_and_pass_to_pass(repo, conn, capsys):
    code, out = _intake(capsys, repo, REPAIR_SPEC, REPAIR)
    assert code == vc.PASS
    rows = _rows_by_key(out)
    assert rows["SC-001"]["bar"].startswith("repair case stable-keys: stable_equal_keys, stable_all_same_key FAIL "
                                            "on base 0f4c2a9e1b7d3c5a8e6f0b2d4c6a8e0f1b3d5c7a and PASS on the fix")
    assert rows["SC-002"]["bar"].startswith("repair case none-key: the generated reproducer FAIL on the run base")
    assert rows["SC-003"]["bar"] == ("pass-to-pass: every test that passes on the base still passes, at least 12 tests")
    assert rows["SC-003"]["shown_by"] == "research research:pass-to-pass"
    assert all(r["stage"] == "research" for r in rows.values())
    added = dict((t["key"], t["task"]) for t in out["tasks"]["added"])
    assert sorted(added) == ["stage:build", "stage:prepare"]
    adds = dict((e["ref"], e["data"]["statement"]) for e in db.events(conn, kind="task-add", limit=10 ** 9))
    assert adds[added["stage:prepare"]].startswith("Prepare stage prepare of research runbook sortbench-repair, run "
                                                   "once per repair case on its base")
    assert _contracts(conn)[-1]["data"]["mode"] == "repair"


def test_two_research_runbooks_load_together(repo, conn, capsys):
    """SC-025: the improve and the repair runbook of one pack take intake in one project and one op;
    each lands its own contract, keyed by its runbook, and the stage ids both use (build) stay
    apart."""
    project_yaml = _read(repo, "project.yaml")
    assert _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)[0] == vc.PASS
    code, out = _intake(capsys, repo, REPAIR_SPEC, REPAIR)
    assert code == vc.PASS
    landed = _contracts(conn)
    assert [(e["ref"], e["data"]["runbook"]) for e in landed] == [("sortbench-improve", IMPROVE),
                                                                   ("sortbench-repair", REPAIR)]
    marks = [e["data"]["intake"] for e in db.events(conn, kind="task-add", limit=10 ** 9)]
    assert sorted((m["runbook"], m["key"]) for m in marks if m["key"] == "stage:build") == [
        ("sortbench-improve", "stage:build"), ("sortbench-repair", "stage:build")]
    assert _read(repo, "project.yaml") == project_yaml
    assert not os.path.exists(os.path.join(repo, intake.PROFILE_FILE))
    # each re-intake is still a no-op with the other runbook's contract on the record
    before = _event_count(conn)
    assert _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)[1]["changed"] is False
    assert _intake(capsys, repo, REPAIR_SPEC, REPAIR)[1]["changed"] is False
    assert _event_count(conn) == before


# ------------------------------------------------------------------- the fail case: no profile
def test_research_intake_keeps_a_profile_an_earlier_intake_wrote(repo, conn, capsys):
    """A format 2 intake in the same project wrote intake_profile.py and `profile: intake_profile`;
    a research intake afterwards changes neither and records the profile it found."""
    shutil.copytree(os.path.join(REPO, "templates", "runbook-example"), os.path.join(repo, "domain"))
    assert _intake(capsys, repo, "domain/spec.md", "domain/runbook.yaml")[0] == vc.PASS
    project_yaml, profile_py = _read(repo, "project.yaml"), _read(repo, intake.PROFILE_FILE)
    assert b"profile: intake_profile" in project_yaml
    _commit(repo, "the format 2 domain")
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.PASS
    assert _read(repo, "project.yaml") == project_yaml and _read(repo, intake.PROFILE_FILE) == profile_py
    assert out["profile"]["module"] == intake.PROFILE_MODULE
    assert out["profile"]["action"] == "not written: a research runbook sets no profile"
    assert db.events(conn, kind="intake", limit=10 ** 9)[-1]["data"]["profile"] == intake.PROFILE_MODULE


def test_research_intake_never_edits_project_yaml_without_a_profile_key(repo, capsys, monkeypatch):
    """With no `profile:` key the format 2 path would add one; the research path never calls the
    writers, which this test makes fail loudly."""
    def refuse(*_a, **_k):
        raise AssertionError("a research intake wrote the profile or project.yaml")
    monkeypatch.setattr(intake, "set_project_key", refuse)
    monkeypatch.setattr(intake, "project_edit", refuse)
    monkeypatch.setattr(intake, "_profile_plan", refuse)
    project_yaml = _read(repo, "project.yaml")
    assert _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)[0] == vc.PASS
    assert _read(repo, "project.yaml") == project_yaml
    assert not os.path.exists(os.path.join(repo, intake.PROFILE_FILE))


# -------------------------------------------------------------- re-intake of an unchanged runbook
def test_reintake_of_an_unchanged_runbook_changes_nothing(repo, conn, capsys):
    assert _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)[0] == vc.PASS
    before, files = _event_count(conn), _files(repo)
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.PASS and out["changed"] is False
    assert out["contract"]["action"] == "kept" and out["contract"]["diff"] == []
    assert not out["rows"]["added"] and len(out["rows"]["kept"]) == 7 and not out["tasks"]["contracts"]
    assert _event_count(conn) == before and _files(repo) == files
    # a commit that changes no file the contract freezes leaves pack_sha256 and the record as they are
    _write(repo, "src/sortlib/sort.py", "def sort(items, key=None):\n    return list(sorted(items, key=key))\n")
    _commit(repo, "a faster sort")
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.PASS and out["changed"] is False
    assert _event_count(conn) == before and len(_contracts(conn)) == 1


def test_reintake_after_a_protected_commit_needs_the_owner(repo, conn, capsys):
    """A commit to a protected file outside the runbook folder (pyproject.toml) changes what every
    run freezes: the re-intake stops with exit 3 and writes nothing until an owner loosen grant
    names files[pyproject.toml] with its new sha256; then it records the new baseline once."""
    assert _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)[0] == vc.PASS
    _write(repo, "pyproject.toml", "[project]\nname = \"sortlib\"\nversion = \"2\"\n")
    _commit(repo, "a new version")
    before, files = _event_count(conn), _files(repo)
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.PAUSED and out["verdict"] == vc.name_of(vc.PAUSED)
    assert "loosens its baseline" in out["reason"] and PACK_REF in out["reason"]
    assert len(out["detail"]) == 1 and out["detail"][0].startswith(
        "CONTRACT-LOOSEN-UNGRANTED %s files[pyproject.toml]: accepted " % PACK_REF)
    assert _event_count(conn) == before and _files(repo) == files
    # the dry run refuses the same way
    assert _intake(capsys, repo, IMPROVE_SPEC, IMPROVE, "--dry-run")[0] == vc.PAUSED
    new_sha = util.sha256_hex(_read(repo, "pyproject.toml"))
    # a grant with another value grants nothing
    _grant(conn, repo, PACK_REF, {"files[pyproject.toml]": "0" * 64})
    assert _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)[0] == vc.PAUSED
    decision = _grant(conn, repo, PACK_REF, {"files[pyproject.toml]": new_sha})
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.PASS and out["contract"]["action"] == "land"
    assert [(d["field"], d["direction"], d["grant"]) for d in out["contract"]["diff"]] == [
        ("files[pyproject.toml]", "loosened", decision)]
    landed = _contracts(conn)
    assert len(landed) == 2 and landed[-1]["data"]["grants"] == [decision]
    assert landed[-1]["data"]["files"]["pyproject.toml"] == new_sha
    # only the contract changed: rows and tasks are kept, and the next re-intake is a no-op
    assert not out["rows"]["added"] and not out["rows"]["superseded"] and not out["tasks"]["contracts"]
    before = _event_count(conn)
    assert _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)[1]["changed"] is False
    assert _event_count(conn) == before


def test_a_pack_edit_that_loosens_a_target_needs_a_grant(repo, conn, capsys):
    assert _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)[0] == vc.PASS
    before = _event_count(conn)
    _edit(repo, IMPROVE, "      target: 18000\n", "      target: 19000\n")
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.PAUSED
    assert out["detail"] == ["CONTRACT-LOOSEN-UNGRANTED %s research.metrics[compares].target: accepted 18000, new 19000"
                             % PACK_REF]
    assert _event_count(conn) == before


def test_a_pack_edit_that_tightens_a_target_lands_with_no_grant(repo, conn, capsys):
    """A tighter target is a new baseline with no owner record, and the row whose bar names it is
    superseded."""
    code, first = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    old_row = _rows_by_key(first)["SC-001"]["row"]
    _edit(repo, IMPROVE, "      target: 18000\n", "      target: 17000\n")
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.PASS
    assert [(d["field"], d["direction"]) for d in out["contract"]["diff"]] == [
        ("research.metrics[compares].target", "tightened")]
    superseded = dict((r["key"], r) for r in out["rows"]["superseded"])
    assert superseded["SC-001"]["old"] == old_row and superseded["SC-001"]["bar"].endswith("target <= 17000 (owner only)")
    assert len(_contracts(conn)) == 2 and _contracts(conn)[-1]["data"]["grants"] == []


def test_a_loader_refusal_stops_intake_and_writes_nothing(repo, conn, capsys):
    """The lessons file is on disk but no longer tracked: the loader refuses it (FILE-MISSING,
    CONTRACT-CHECK-FAILED), so intake ends FAIL before it writes a row, a task or an event."""
    _git(repo, "rm", "-q", "--cached", "packs/sortbench/lessons.md")
    _git(repo, "-c", "user.name=T", "commit", "-q", "-m", "untrack the lessons")
    before, files = _event_count(conn), _files(repo)
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.FAIL and out["verdict"] == "FAIL"
    assert out["reason"] == "the research contract loader refused the runbook: CONTRACT-CHECK-FAILED"
    assert any("FILE-MISSING research.lessons.file" in line for line in out["detail"])
    assert _event_count(conn) == before and _files(repo) == files


# ------------------------------------------------------- a task override survives a re-intake
def _task_of(conn, key):
    """The id of the task an intake added for `key` (stage:<id> or gate:<id>)."""
    for e in db.events(conn, kind="task-add", limit=10 ** 9):
        if (e["data"].get("intake") or {}).get("key") == key:
            return e["ref"]
    raise AssertionError(key)


def test_format_3_reintake_keeps_override(repo, conn, capsys):
    assert _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)[0] == vc.PASS
    tid = _task_of(conn, "stage:unit")
    stored = dict((k, v) for k, v in taskcontract.latest(conn)[tid].items() if k not in ("event_id", "at", "by"))
    part = {"stop": {"slots": 1}}
    taskcontract.record(conn, tid, dict(stored, research=part), session="test", actor="human")
    before = _event_count(conn)
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.PASS and out["changed"] is False and not out["tasks"]["contracts"]
    assert _event_count(conn) == before and taskcontract.latest(conn)[tid]["research"] == part
    # one stage edited (a stage change the owner grants): the new contract carries the part unchanged
    _bind_live_run(conn, tid, part)
    _edit(repo, IMPROVE, "    needs: [build]\n    run: >-\n", "    needs: [build]\n    inputs: [src/sortlib/]\n    run: >-\n")
    _commit(repo, "the unit stage reads src/sortlib/")
    stage = [s for s in contract.load_block(os.path.join(repo, IMPROVE))["stages"] if s["id"] == "unit"][0]
    _grant(conn, repo, PACK_REF, {"stages[unit]": stage})
    code, out = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.PASS
    assert [t["task"] for t in out["tasks"]["contracts"]] == [tid]
    now = taskcontract.latest(conn)[tid]
    assert now["research"] == part and now["input"][0] == "src/sortlib/" and now["stage"] is None
    assert db.events(conn, kind=events.CONTRACT_DRIFT, limit=10 ** 9) == []


def test_format_2_reintake_keeps_override(project, capsys):
    """runbook-format-3.md section 2.3: a stage task of templates/runbook-example whose newest
    contract carries a `research` part. A re-intake of the unchanged runbook plans no change, the
    record gains no event and the part stays. With one stage edited, intake records a contract
    that carries the stored part unchanged; while a live run names the task that write passes the
    guard, and the record gains no research-contract-drift event."""
    shutil.copytree(os.path.join(REPO, "templates", "runbook-example"), os.path.join(project, "domain"))
    assert cli.main(["op", "new", "Link shortener", "--done-when", "every row proven"]) == 0
    code, out = _intake(capsys, project, "domain/spec.md", "domain/runbook.yaml")
    assert code == vc.PASS and "contract" not in out
    conn = db.connect(project)
    tid = _task_of(conn, "stage:install")
    stored = dict((k, v) for k, v in taskcontract.latest(conn)[tid].items() if k not in ("event_id", "at", "by"))
    assert stored["stage"] == "install" and "research" not in stored
    part = {"stop": {"slots": 1}}
    taskcontract.record(conn, tid, dict(stored, research=part), session="test", actor="human")
    before = _event_count(conn)
    code, out = _intake(capsys, project, "domain/spec.md", "domain/runbook.yaml")
    assert code == vc.PASS and out["changed"] is False and [t["task"] for t in out["tasks"]["kept"]].count(tid) == 1
    assert _event_count(conn) == before and taskcontract.latest(conn)[tid]["research"] == part

    _bind_live_run(conn, tid, part)
    # the guard is live for this task: a contract that drops the part is refused and recorded
    with pytest.raises(ValueError):
        taskcontract.record(conn, tid, stored, session="test", actor="human")
    assert len(db.events(conn, kind=events.CONTRACT_DRIFT, limit=10 ** 9)) == 1
    _edit(project, "domain/runbook.yaml", "    inputs: [requirements.txt]\n", "    inputs: [requirements.txt, setup.cfg]\n")
    before = _event_count(conn)
    code, out = _intake(capsys, project, "domain/spec.md", "domain/runbook.yaml")
    assert code == vc.PASS and [t["task"] for t in out["tasks"]["contracts"]] == [tid]
    now = dict((k, v) for k, v in taskcontract.latest(conn)[tid].items() if k not in ("event_id", "at", "by"))
    assert now == dict(stored, input=["requirements.txt", "setup.cfg"], research=part)
    assert len(db.events(conn, kind=events.CONTRACT_DRIFT, limit=10 ** 9)) == 1
    assert [k for k, _r in _events_after(conn, before)] == ["task-contract", "intake"]


def test_format_2_intake_output_has_no_research_keys(project, capsys):
    """A format 2 intake prints the same --json keys as before format 3 and still writes its
    profile; the research contract path is never taken."""
    shutil.copytree(os.path.join(REPO, "templates", "runbook-example"), os.path.join(project, "domain"))
    assert cli.main(["op", "new", "Link shortener", "--done-when", "every row proven"]) == 0
    code, out = _intake(capsys, project, "domain/spec.md", "domain/runbook.yaml")
    assert code == vc.PASS
    assert sorted(out) == ["changed", "dry_run", "format", "op", "profile", "required", "rows", "runbook",
                           "runbook_id", "spec", "spec_shape", "stages", "tasks", "verdict", "warnings"]
    assert out["profile"]["module"] == intake.PROFILE_MODULE and out["profile"]["action"] == "write"
    conn = db.connect(project)
    assert _contracts(conn) == []
    assert all(t["stage"] for t in out["tasks"]["added"])
