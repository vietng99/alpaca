"""`alpaca attach check`: gate rules against the baseline (AC-05, AC-06) and the pre-mortem attacks of t-649 (build plan 6.3)."""
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from alpaca import cli, db
from alpaca.attach import AttachError, baseline, check, config, reap
from alpaca.tests.attachkit import (add_worktree, attached, commit_in, git, project_yaml, set_attach, set_gates,  # noqa: F401
                                    studio, upstream)

TASK = "t-001"


def py_gate(name, code, timeout=60):
    return {"name": name, "command": [sys.executable, "-B", "-c", code], "timeout": timeout}


#: a gate that is red while a file named `broken` is in the tree
TEST_GATE = py_gate("test", "import os, sys; sys.exit(1 if os.path.exists('broken') else 0)")
BUILD_GATE = py_gate("build", "import sys; sys.exit(0)")


def events(root, kind):
    conn = db.connect(str(root))
    try:
        return [e for e in db.events(conn, limit=10 ** 9) if e["kind"] == kind]
    finally:
        conn.close()


def setup(att, gates=(BUILD_GATE, TEST_GATE), upstream_files=None, task=TASK):
    """Gates set, optionally an upstream commit first, then the work branch of `task` cut from the fresh base."""
    if upstream_files:
        att.upstream.commit(upstream_files, "upstream before the cut")
    set_gates(att.root, list(gates))
    return add_worktree(att, task)


def the_check(att, task=TASK):
    return check.run(str(att.root), task)


def gate(res, name, where="head_results"):
    return {g["name"]: g for g in getattr(res, where)}[name]


# ---- AC-05: green at base, red at head --------------------------------------------------------------------------------

def test_a_gate_green_at_base_and_red_at_head_fails_the_check(attached):
    """AC-05."""
    wt = setup(attached)
    commit_in(wt, {"broken": "x\n"}, "break the test")
    res = the_check(attached)
    assert res.verdict == "FAIL" and res.conflicts == []
    # the merged tree carries the branch's break too, so the preview run is red as well
    assert [(r["gate"], r["at"]) for r in res.regressions] == [("test", "head"), ("test", "preview")]
    assert gate(res, "test")["passed"] is False and gate(res, "test")["returncode"] == 1
    assert gate(res, "build")["passed"] is True
    assert res.red_at_base == [] and res.needs_owner_review is False
    assert res.reason == "gate regression: test (head), test (preview)"


def test_a_green_branch_passes(attached):
    wt = setup(attached)
    head = commit_in(wt, {"src/new.py": "x = 1\n"}, "a harmless change")
    res = the_check(attached)
    assert res.verdict == "PASS" and res.head == head and res.regressions == [] and res.reason == ""
    assert res.base_sha == res.preview_base == attached.upstream.sha()
    assert [g["passed"] for g in res.head_results] == [True, True] == [g["passed"] for g in res.preview_results]


def test_a_gate_that_times_out_at_head_is_a_regression(attached):
    slow = py_gate("test", "import os, time; os.path.exists('slow') and time.sleep(60)", timeout=2)
    wt = setup(attached, gates=(BUILD_GATE, slow))
    commit_in(wt, {"slow": "x\n"}, "make the test slow")
    res = the_check(attached)
    assert res.verdict == "FAIL"
    assert gate(res, "test")["timed_out"] is True and gate(res, "test")["passed"] is False
    assert [(r["gate"], r["at"], r["timed_out"]) for r in res.regressions] == [("test", "head", True), ("test", "preview", True)]


def test_a_branch_with_no_commit_of_its_own_passes(attached):
    setup(attached)
    assert the_check(attached).verdict == "PASS"


# ---- AC-06: red at base ----------------------------------------------------------------------------------------------

def test_a_gate_red_at_base_does_not_fail_the_check_but_needs_owner_review(attached):
    """AC-06 (the check side): the gate stays red, the check passes, the result says the handoff needs the owner."""
    wt = setup(attached, upstream_files={"broken": "already red upstream\n"})
    commit_in(wt, {"src/new.py": "x = 1\n"}, "an unrelated change")
    res = the_check(attached)
    assert res.verdict == "PASS" and res.regressions == []
    assert res.red_at_base == ["test"] and res.needs_owner_review is True
    assert gate(res, "test")["passed"] is False and gate(res, "test", "preview_results")["passed"] is False
    assert gate(res, "build")["passed"] is True


def test_red_at_base_is_read_from_the_baseline_not_from_the_head(attached):
    """A gate that is red at base and green at head (the branch fixed it) is still marked red at base."""
    wt = setup(attached, upstream_files={"broken": "red\n"})
    git(wt, "rm", "-q", "broken")
    git(wt, "commit", "-q", "-m", "fix it", env={"GIT_AUTHOR_NAME": "Alpaca agent", "GIT_AUTHOR_EMAIL": "alpaca-agent@localhost",
                                                 "GIT_COMMITTER_NAME": "Alpaca agent", "GIT_COMMITTER_EMAIL": "alpaca-agent@localhost"})
    res = the_check(attached)
    assert res.verdict == "PASS" and res.red_at_base == ["test"] and gate(res, "test")["passed"] is True


def test_a_gate_that_upstream_broke_alone_is_red_at_the_preview_base_not_a_regression(attached):
    """R14: the preview is judged at the commit it merges into. Green at the cut, red at the newest upstream base
    for a reason that is upstream's own: the check passes and the handoff needs the owner."""
    wt = setup(attached)
    commit_in(wt, {"src/new.py": "x = 1\n"}, "a change")
    attached.upstream.commit({"broken": "someone else broke the test\n"}, "upstream breaks the test")
    res = the_check(attached)
    assert res.verdict == "PASS" and res.conflicts == [] and res.regressions == []
    assert gate(res, "test")["passed"] is True                                  # the branch alone is green
    assert gate(res, "test", "preview_results")["passed"] is False
    assert res.red_at_base == [] and res.red_at_preview_base == ["test"] and res.needs_owner_review is True
    assert res.preview_base == attached.upstream.sha() != res.base_sha
    assert any("red at the preview base: test" in line for line in check._lines(res))


#: red while VERSION and EXPECTED differ
VERSION_GATE = py_gate("test", "import sys; sys.exit(0 if open('VERSION').read() == open('EXPECTED').read() else 1)")


def test_a_gate_that_upstream_fixed_and_the_merge_breaks_again_is_a_regression(attached):
    """N1: red at the cut X, green at the newest base Y, red on the merge. Not excused as red at base."""
    wt = setup(attached, gates=(BUILD_GATE, VERSION_GATE), upstream_files={"VERSION": "1\n", "EXPECTED": "2\n"})
    attached.upstream.commit({"VERSION": "2\n"}, "upstream fixes the version test")
    commit_in(wt, {"EXPECTED": "1\n", "lib.py": "a = 2\n"}, "the branch pins the old value")
    res = the_check(attached)
    assert res.verdict == "FAIL" and res.conflicts == []
    assert gate(res, "test")["passed"] is True and gate(res, "test", "preview_results")["passed"] is False
    assert [(r["gate"], r["at"]) for r in res.regressions] == [("test", "preview")]
    assert res.red_at_base == ["test"] and res.red_at_preview_base == [] and res.needs_owner_review is True
    assert res.reason == "gate regression: test (preview)"


def test_a_gate_red_at_both_bases_is_not_a_regression_and_needs_the_owner(attached):
    wt = setup(attached, upstream_files={"broken": "red\n"})
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    attached.upstream.commit({"src/up.py": "y = 1\n"}, "upstream moves, the gate stays red")
    res = the_check(attached)
    assert res.verdict == "PASS" and res.red_at_base == ["test"] and res.red_at_preview_base == ["test"]
    assert res.needs_owner_review is True


def test_the_baseline_at_the_merge_target_runs_once_per_new_upstream_commit(attached):
    wt = setup(attached)
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    before = len(events(attached.root, "attach-baseline"))
    the_check(attached)
    assert len(events(attached.root, "attach-baseline")) == before                  # upstream did not move: no second baseline
    attached.upstream.commit({"src/up.py": "y = 1\n"}, "upstream moves")
    res = the_check(attached)
    assert len(events(attached.root, "attach-baseline")) == before + 1
    assert baseline.load(str(attached.root), res.preview_base) is not None
    the_check(attached)
    assert len(events(attached.root, "attach-baseline")) == before + 1              # the new baseline is reused


def test_an_older_event_without_the_preview_base_list_still_reads():
    old = {"task": TASK, "head": "a" * 40, "base_sha": "b" * 40, "preview_base": "b" * 40, "verdict": "PASS",
           "conflicts": [], "head_results": [], "preview_results": [], "red_at_base": [], "regressions": [],
           "needs_owner_review": False, "gate_set": "g", "path": "p"}
    assert check.CheckResult.from_dict(old).red_at_preview_base == []


# ---- untrusted gate code and the order of the run (N8, N9) -------------------------------------------------------------

EVIL_GATE = py_gate("build", "import os, sys; os.path.exists('evil.py') and exec(open('evil.py').read()); sys.exit(0)")


def test_every_git_fact_is_made_before_the_first_gate_runs(attached, tmp_path):
    out = tmp_path / "seen"
    spy = py_gate("build", "import os; open(%r, 'a').write(' '.join(str(os.path.isdir('../../' + d + '/.git')) "
                           "for d in ('seed-head', 'seed-preview')) + '\\n')" % str(out))
    wt = setup(attached, gates=(spy, TEST_GATE))
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    attached.upstream.commit({"src/up.py": "y = 1\n"}, "upstream moves")
    warm_baselines(attached)
    out.write_text("")
    res = the_check(attached)
    assert res.verdict == "PASS"
    assert out.read_text().split("\n")[:-1] == ["True True", "True True"]              # head set and preview set


def warm_baselines(att):
    cfg = config.load(str(att.root))
    from alpaca.attach import worktree
    baseline.ensure(str(att.root), worktree.fetch_base(str(att.root), cfg))


def test_a_head_gate_cannot_hide_a_conflict_with_a_seed_of_its_own(attached):
    """N8: branch code that runs in a head gate makes `../../seed-preview` with `* merge=union` in its info/attributes."""
    evil = ("import os, subprocess\n"
            "run = os.path.dirname(os.path.dirname(os.getcwd()))\n"
            "seed = os.path.join(run, 'seed-preview')\n"
            "if os.path.basename(os.path.dirname(os.getcwd())) == 'work-head':\n"
            "    try:\n"
            "        os.mkdir(seed)\n"
            "    except OSError:\n"
            "        pass\n"
            "    subprocess.run(['git', 'init', '-q', seed], check=True)\n"
            "    open(os.path.join(seed, '.git', 'info', 'attributes'), 'w').write('* merge=union\\n')\n")
    wt = setup(attached, gates=(EVIL_GATE, TEST_GATE), upstream_files={"src/main.py": "print('base')\n"})
    commit_in(wt, {"evil.py": evil, "src/main.py": "print('branch')\n"}, "branch edit with a test helper")
    attached.upstream.commit({"src/main.py": "print('upstream')\n"}, "upstream edit")
    res = the_check(attached)
    assert res.verdict == "FAIL" and res.conflicts == ["src/main.py"] and res.preview_results == []


def test_a_gate_that_moves_the_head_seed_fails_the_check(attached):
    """N9, across gates: gate 1 checks out the parent in the seed, so gate 2 would clone another tree."""
    evil = ("import os, subprocess\n"
            "seed = os.path.join(os.path.dirname(os.path.dirname(os.getcwd())), 'seed-head')\n"
            "subprocess.run(['git', '-C', seed, 'checkout', '-q', '--detach', 'HEAD~1'], check=False)\n")
    wt = setup(attached, gates=(EVIL_GATE, TEST_GATE))
    commit_in(wt, {"evil.py": evil, "broken": "1\n"}, "break the test, with a helper")
    res = the_check(attached)
    assert res.verdict == "FAIL" and "a gate changed the head seed" in res.reason
    assert res.preview_results == []                                                   # nothing runs on a moved seed


def test_a_gate_that_moves_the_preview_seed_fails_the_check(attached):
    evil = ("import os, subprocess\n"
            "seed = os.path.join(os.path.dirname(os.path.dirname(os.getcwd())), 'seed-preview')\n"
            "subprocess.run(['git', '-C', seed, 'checkout', '-q', '--detach', 'HEAD~1'], check=True)\n")
    wt = setup(attached, gates=(EVIL_GATE, TEST_GATE))
    commit_in(wt, {"evil.py": evil, "src/new.py": "x = 1\n"}, "a helper")
    res = the_check(attached)
    assert res.verdict == "FAIL" and "a gate changed the preview seed" in res.reason and res.preview_results == []


def test_a_gate_that_dirties_a_seed_fails_the_check(attached):
    evil = ("import os\n"
            "seed = os.path.join(os.path.dirname(os.path.dirname(os.getcwd())), 'seed-head')\n"
            "open(os.path.join(seed, 'stray.txt'), 'w').write('x')\n")
    wt = setup(attached, gates=(EVIL_GATE, TEST_GATE))
    commit_in(wt, {"evil.py": evil}, "a helper")
    res = the_check(attached)
    assert res.verdict == "FAIL" and "a gate changed the head seed" in res.reason


def test_a_taken_folder_name_is_refused_by_the_claim(attached):
    folder = attached.root / ".alpaca/attach/runs/probe"
    folder.mkdir(parents=True)
    with pytest.raises(AttachError, match="already exists"):
        check._claim(str(attached.root), ".alpaca/attach/runs/probe")
    assert check._claim(str(attached.root), ".alpaca/attach/runs/probe-new").endswith("probe-new")


def test_the_merge_preview_refuses_a_seed_folder_that_is_not_empty(attached):
    from alpaca.attach import safefs
    wt = setup(attached)
    seed = safefs.make_dir(str(attached.root), ".alpaca/attach/runs/probe/seed")
    (Path(seed) / "planted.txt").write_text("x")
    with pytest.raises(AttachError, match="needs an empty folder"):
        check.merge_preview(str(attached.app), seed, branch="alpaca/%s" % TASK, base_ref="refs/remotes/origin/main")
    safefs.remove_tree(str(attached.root), ".alpaca/attach/runs/probe")


def test_the_merge_preview_refuses_a_seed_with_merge_attributes_of_its_own(attached, monkeypatch):
    from alpaca.attach import safefs
    setup(attached)
    real = baseline.make_seed

    def seeded(app, dest, refs):
        out = real(app, dest, refs)
        Path(dest, ".git", "info").mkdir(exist_ok=True)
        Path(dest, ".git", "info", "attributes").write_text("* merge=union\n")
        return out

    monkeypatch.setattr(baseline, "make_seed", seeded)
    seed = safefs.make_dir(str(attached.root), ".alpaca/attach/runs/probe/seed")
    with pytest.raises(AttachError, match="merge attributes of its own"):
        check.merge_preview(str(attached.app), seed, branch="alpaca/%s" % TASK, base_ref="refs/remotes/origin/main")
    safefs.remove_tree(str(attached.root), ".alpaca/attach/runs/probe")


# ---- the exit-status rule (P9, pre-mortem t-649.4) ---------------------------------------------------------------------

def test_every_gate_runs_whatever_fails_and_none_is_skipped(attached):
    first = py_gate("first", "import sys; sys.exit(3)", timeout=30)
    second = py_gate("second", "import sys; sys.exit(0)", timeout=60)
    third = py_gate("third", "import sys; sys.exit(0)", timeout=90)
    wt = setup(attached, gates=(first, second, third))
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    res = the_check(attached)
    for results in (res.head_results, res.preview_results):
        assert [g["name"] for g in results] == ["first", "second", "third"]
        assert [g["returncode"] for g in results] == [3, 0, 0]                   # none SKIPPED, none without a code
    assert res.red_at_base == ["first"] and res.verdict == "PASS"


def test_a_gate_that_edits_a_tracked_file_and_exits_zero_passes_with_a_note(attached):
    edit = py_gate("test", "open('README.md', 'a').write('touched\\n')")
    wt = setup(attached, gates=(BUILD_GATE, edit))
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    res = the_check(attached)
    g = gate(res, "test")
    assert res.verdict == "PASS" and g["passed"] is True and g["returncode"] == 0
    assert g["detail"].startswith("note: ") and "not a failure" in g["detail"]


def test_a_failing_gate_is_failed_by_its_exit_status_alone(attached):
    wt = setup(attached)
    commit_in(wt, {"broken": "x\n"}, "break")
    res = the_check(attached)
    assert gate(res, "test")["detail"] == "exit 1"


# ---- refusals ---------------------------------------------------------------------------------------------------------

def test_an_empty_gate_list_is_refused(attached):
    setup(attached)
    block = dict(project_yaml(attached.root)["attach"])
    block["gates"] = []
    set_attach(attached.root, block)
    with pytest.raises(AttachError, match="no attach gates"):
        the_check(attached)
    assert not list((attached.root / ".alpaca/attach/checks").glob("*/*.json"))


def test_a_dirty_work_folder_is_refused_before_any_gate_runs(attached, tmp_path):
    counter = tmp_path / "ran"
    from alpaca.tests.attachkit import counting_gates
    wt = setup(attached, gates=counting_gates(counter, ("one",)))
    ran = counter.read_text() if counter.exists() else ""
    (wt / "README.md").write_text("edited\n", encoding="utf-8")
    with pytest.raises(AttachError, match="uncommitted changes"):
        the_check(attached)
    (wt / "README.md").write_text("# demo app\n", encoding="utf-8")
    (wt / "stray.txt").write_text("untracked\n", encoding="utf-8")
    with pytest.raises(AttachError, match="uncommitted changes"):
        the_check(attached)
    assert (counter.read_text() if counter.exists() else "") == ran
    assert events(attached.root, "attach-check") == []


def test_a_task_without_a_work_branch_is_refused(attached):
    setup(attached)
    with pytest.raises(AttachError, match="has no work branch"):
        the_check(attached, "t-002")


@pytest.mark.parametrize("bad", ["", "t-", "T-1", "t-1; rm", "../t-1", "t-1234567", None, 7])
def test_a_bad_task_id_is_refused(attached, bad):
    setup(attached)
    with pytest.raises(AttachError, match="cannot name a work branch"):
        the_check(attached, bad)


def test_a_work_folder_off_its_branch_is_refused(attached):
    wt = setup(attached)
    git(wt, "checkout", "-q", "--detach")
    with pytest.raises(AttachError, match="cannot be checked"):
        the_check(attached)


def test_a_changed_clone_config_is_refused(attached):
    """P8: a rewritten remote url (or a hook path) in app/.git/config stops the check."""
    setup(attached)
    git(attached.app, "config", "remote.origin.url", "https://example.invalid/other.git")
    with pytest.raises(AttachError, match="app git config changed"):
        the_check(attached)


def test_a_project_that_is_not_attached_is_refused(studio):
    with pytest.raises(AttachError, match="not attached"):
        check.run(str(studio), TASK)


# ---- baseline comparison ----------------------------------------------------------------------------------------------

def test_a_gate_set_change_after_the_baseline_runs_a_new_baseline(attached):
    wt = setup(attached)
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    before = len(events(attached.root, "attach-baseline"))
    the_check(attached)
    assert len(events(attached.root, "attach-baseline")) == before                 # the cut's baseline is reused
    changed = dict(TEST_GATE, timeout=61)
    set_gates(attached.root, [BUILD_GATE, changed])
    res = the_check(attached)
    assert len(events(attached.root, "attach-baseline")) == before + 1
    assert res.gate_set == config.load(str(attached.root)).gate_set_digest()
    assert baseline.load(str(attached.root), res.base_sha) is not None


def test_the_baseline_is_not_rerun_by_a_second_check(attached, tmp_path):
    from alpaca.tests.attachkit import counting_gates
    counter = tmp_path / "ran"
    wt = setup(attached, gates=counting_gates(counter, ("one", "two")))
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    base_runs = counter.read_text().split()
    the_check(attached)
    the_check(attached)
    assert counter.read_text().split()[len(base_runs):] == ["one", "two"] * 4        # head + preview, twice; no baseline


# ---- the result: file, record, latest ----------------------------------------------------------------------------------

def test_the_result_is_kept_in_a_file_and_in_the_record(attached):
    wt = setup(attached)
    commit_in(wt, {"broken": "x\n"}, "break")
    res = the_check(attached)
    path = attached.root / res.path
    assert path.parent == attached.root / ".alpaca/attach/checks" / TASK and path.name == res.check_id + ".json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    doc = json.loads(path.read_text())
    assert doc["verdict"] == "FAIL" and doc["head"] == res.head and doc["gate_set"] == res.gate_set
    assert hashlib.sha256(path.read_bytes()).hexdigest() == res.file_sha256
    (ev,) = events(attached.root, "attach-check")
    assert ev["ref"] == TASK and ev["data"]["file_sha256"] == res.file_sha256 and ev["data"]["head"] == res.head
    assert ev["data"]["verdict"] == "FAIL" and ev["data"]["path"] == res.path


def test_each_gate_keeps_its_output_tail_in_the_result(attached):
    loud = py_gate("test", "print('hello from the gate')")
    setup(attached, gates=(BUILD_GATE, loud))
    res = the_check(attached)
    assert "hello from the gate" in gate(res, "test")["log_tail"]
    assert "hello from the gate" in gate(res, "test", "preview_results")["log_tail"]


def test_latest_returns_the_newest_recorded_check(attached):
    wt = setup(attached)
    assert check.latest(str(attached.root), TASK) is None
    first = the_check(attached)
    commit_in(wt, {"broken": "x\n"}, "break")
    second = the_check(attached)
    got = check.latest(str(attached.root), TASK)
    assert got.check_id == second.check_id != first.check_id and got.verdict == "FAIL" and got.head == second.head
    assert got.regressions == second.regressions and got.red_at_base == second.red_at_base
    assert check.latest(str(attached.root), "t-002") is None and check.latest(str(attached.root), "bad") is None


def test_a_file_written_into_checks_does_not_change_what_latest_reports(attached):
    """The files are output for people; the record decides."""
    wt = setup(attached)
    commit_in(wt, {"broken": "x\n"}, "break")
    res = the_check(attached)
    forged = dict(json.loads((attached.root / res.path).read_text()), verdict="PASS", regressions=[])
    (attached.root / res.path).write_text(json.dumps(forged))
    (attached.root / ".alpaca/attach/checks" / TASK / "99999999T999999Z-forged.json").write_text(json.dumps(forged))
    got = check.latest(str(attached.root), TASK)
    assert got.verdict == "FAIL" and got.check_id == res.check_id


def test_a_failed_record_leaves_no_file(attached, monkeypatch):
    setup(attached)

    def boom(*a, **k):
        raise RuntimeError("record is gone")
    monkeypatch.setattr(check, "_record", boom)
    with pytest.raises(RuntimeError):
        the_check(attached)
    assert not list((attached.root / ".alpaca/attach/checks").glob("*/*.json"))
    assert not (attached.root / ".alpaca/attach/runs" / TASK).exists()


# ---- where the work happens (T10) and the lock -------------------------------------------------------------------------

def test_the_gates_run_in_clones_under_the_studio_run_folder(attached, tmp_path):
    where = tmp_path / "cwds"
    spy = py_gate("spy", "import os; open(%r, 'a').write(os.getcwd() + '\\n')" % str(where))
    setup(attached, gates=(spy,))
    base_runs = where.read_text().split()
    the_check(attached)
    cwds = where.read_text().split()[len(base_runs):]
    runs = os.path.realpath(str(attached.root / ".alpaca/attach/runs"))
    assert len(cwds) == 2 and all(os.path.realpath(c).startswith(runs + os.sep) for c in cwds)


def test_two_checks_of_one_task_at_once_both_finish(attached):
    wt = setup(attached)
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: the_check(attached), range(2)))
    assert [r.verdict for r in results] == ["PASS", "PASS"] and results[0].check_id != results[1].check_id
    assert not (attached.root / ".alpaca/attach/runs" / TASK).exists()
    assert len(events(attached.root, "attach-check")) == 2


def test_the_check_changes_nothing_in_the_app_checkout(attached):
    wt = setup(attached)
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    attached.upstream.commit({"fresh.txt": "x\n"}, "upstream moves")
    before = (git(attached.app, "rev-parse", "HEAD"), git(attached.app, "symbolic-ref", "HEAD"),
              git(attached.app, "status", "--porcelain", "--ignored"), git(attached.app, "config", "--local", "--list"))
    the_check(attached)
    after = (git(attached.app, "rev-parse", "HEAD"), git(attached.app, "symbolic-ref", "HEAD"),
             git(attached.app, "status", "--porcelain", "--ignored"), git(attached.app, "config", "--local", "--list"))
    assert after == before


# ---- the verb ---------------------------------------------------------------------------------------------------------

def test_the_verb_prints_the_gate_line_and_exits_by_the_verdict(attached, capsys):
    wt = setup(attached)
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    capsys.readouterr()
    assert cli.main(["attach", "check", TASK]) == cli.PASS
    out = capsys.readouterr().out
    assert "GATE alpaca-attach-check: PASS" in out and "gate test: head PASS, preview PASS" in out
    commit_in(wt, {"broken": "x\n"}, "break")
    assert cli.main(["attach", "check", TASK]) == cli.FAIL
    out = capsys.readouterr().out
    assert "regression: gate test head" in out
    assert out.rstrip().endswith("GATE alpaca-attach-check: FAIL (gate regression: test (head), test (preview))")


def test_the_verb_names_red_at_base(attached, capsys):
    wt = setup(attached, upstream_files={"broken": "red\n"})
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    capsys.readouterr()
    assert cli.main(["attach", "check", TASK]) == cli.PASS
    out = capsys.readouterr().out
    assert "red at base: test (the handoff needs an owner review)" in out and "gate test: head FAIL, preview FAIL, red at base" in out


def test_the_verb_json_has_the_surface_keys(attached, capsys):
    wt = setup(attached)
    commit_in(wt, {"broken": "x\n"}, "break")
    capsys.readouterr()
    assert cli.main(["attach", "check", TASK, "--json"]) == cli.FAIL
    doc = json.loads(capsys.readouterr().out)
    assert doc["verdict"] == "FAIL" and doc["task"] == TASK
    assert doc["receipt"]["head"] == git(wt, "rev-parse", "HEAD") and doc["receipt"]["regressions"][0]["gate"] == "test"
    assert set(doc) == {"verdict", "task", "receipt", "reason"}


def test_the_verb_is_blocked_by_a_refusal(attached, capsys):
    setup(attached)
    capsys.readouterr()
    assert cli.main(["attach", "check", "t-002"]) == cli.BLOCKED
    assert "GATE alpaca-attach-check: BLOCKED (t-002 has no work branch" in capsys.readouterr().out


def test_the_verb_in_a_plain_project_is_blocked(project, capsys):
    cli.main(["init"])
    capsys.readouterr()
    assert cli.main(["attach", "check", TASK]) == cli.BLOCKED
    assert "this project is not attached" in capsys.readouterr().out


# ---- environment and wording (adversarial N3b, N10) ---------------------------------------------------------------------

def test_an_inherited_git_attr_source_does_not_reach_the_check(attached, monkeypatch):
    wt = setup(attached)
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    monkeypatch.setenv("GIT_ATTR_SOURCE", "HEAD")
    assert the_check(attached).verdict == "PASS"


def test_an_upstream_with_an_unrelated_history_is_refused_with_its_real_cause(attached):
    wt = setup(attached)
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    up = attached.upstream
    git(up.work, "checkout", "-q", "--orphan", "fresh")
    git(up.work, "rm", "-rq", "--cached", ".")
    (up.work / "only.txt").write_text("new world\n")
    git(up.work, "add", "only.txt")
    git(up.work, "commit", "-q", "-m", "unrelated")
    git(up.work, "push", "-q", "-f", "origin", "fresh:%s" % up.branch)
    with pytest.raises(AttachError, match="share no history"):
        the_check(attached)


# ---- what a gate leaves running (adversarial round 2: N9b, N9c, N3d) ----------------------------------------------------

#: a gate that runs `evil.py` from the tree when there is one (a test runner loading the branch's conftest.py); its
#: short timeout makes it run first (the release runner sorts gates by timeout)
EVIL_GATE = py_gate("build", "import os, sys; os.path.exists('evil.py') and exec(open('evil.py').read()); sys.exit(0)",
                    timeout=30)

#: double fork plus setsid: the grandchild writes its pid to PIDS and sleeps; the middle process leaves only once the
#: pid is written, so the gate exits 0 with the grandchild alive and outside the gate's process group
DAEMON = r'''
import os, sys, time
PIDS = %r
if os.fork() == 0:
    os.setsid()
    child = os.fork()
    if child == 0:
        for fd in range(3):
            try: os.close(fd)
            except OSError: pass
        with open(PIDS, 'a') as fh: fh.write('%%d\n' %% os.getpid())
        os.chdir('/')
        time.sleep(120)
        os._exit(0)
    end = time.time() + 20
    while time.time() < end and str(child) not in (open(PIDS).read().split() if os.path.exists(PIDS) else []):
        time.sleep(0.01)
    os._exit(0)
os.wait()
'''


def _alive(pids) -> list:
    return [p for p in pids if os.path.exists("/proc/%s" % p)]


def _pids(path) -> list:
    return path.read_text().split() if path.exists() else []


def _kill_all(pids):
    import signal
    for p in _alive(pids):
        try:
            os.kill(int(p), signal.SIGKILL)
        except OSError:
            pass


@pytest.mark.skipif(not reap.SUPPORTED, reason="the subreaper is Linux only")
def test_a_process_a_gate_leaves_running_does_not_outlive_the_check(attached, tmp_path):
    pids = tmp_path / "pids"
    wt = setup(attached, gates=(EVIL_GATE, py_gate("test", "import sys; sys.exit(0)")))
    commit_in(wt, {"evil.py": DAEMON % str(pids)}, "a helper that stays behind")
    try:
        res = the_check(attached)
        left = _pids(pids)
        assert res.verdict == "PASS"
        assert len(left) == 2, left                                      # one per gate set: head and preview
        assert _alive(left) == []
    finally:
        _kill_all(_pids(pids))


@pytest.mark.skipif(not reap.SUPPORTED, reason="the subreaper is Linux only")
def test_a_process_left_by_one_gate_is_gone_before_the_next_gate_runs(attached, tmp_path):
    """N9b: a leftover of gate 0 could otherwise edit gate 1's clone; gate 1 here fails if the leftover is alive."""
    pids = tmp_path / "pids"
    probe = py_gate("probe", "import os, sys; p = %r; pids = open(p).read().split() if os.path.exists(p) else []; "
                             "sys.exit(1 if any(os.path.exists('/proc/' + x) for x in pids) else 0)" % str(pids))
    wt = setup(attached, gates=(EVIL_GATE, probe))
    commit_in(wt, {"evil.py": DAEMON % str(pids)}, "a helper that stays behind")
    try:
        res = the_check(attached)
        assert _pids(pids), "the helper never started"
        assert res.verdict == "PASS" and res.regressions == []
        assert gate(res, "probe")["passed"] and gate(res, "probe", "preview_results")["passed"]
    finally:
        _kill_all(_pids(pids))


@pytest.mark.skipif(not reap.SUPPORTED, reason="the subreaper is Linux only")
def test_the_check_puts_the_subreaper_flag_back_and_spares_the_callers_own_child(attached, tmp_path):
    import subprocess
    assert reap.subreaper() == 0
    wt = setup(attached, gates=(EVIL_GATE, py_gate("test", "import sys; sys.exit(0)")))
    commit_in(wt, {"evil.py": DAEMON % str(tmp_path / "pids")}, "a helper that stays behind")
    own = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        assert the_check(attached).verdict == "PASS"
        assert reap.subreaper() == 0
        assert own.poll() is None, "the reaper killed a child the caller had before the check"
    finally:
        own.kill()
        own.wait()
        _kill_all(_pids(tmp_path / "pids"))


@pytest.mark.skipif(not reap.SUPPORTED, reason="the subreaper is Linux only")
def test_the_reaper_stops_a_double_forked_process_and_its_children(tmp_path):
    import subprocess
    pids = tmp_path / "pids"
    with reap.Reaper() as reaper:
        assert reap.subreaper() == 1
        subprocess.run([sys.executable, "-c", DAEMON % str(pids)], start_new_session=True, check=True, timeout=30)
        assert _alive(_pids(pids)), "the helper did not stay behind"
        assert reaper.reap() == 1
        assert _alive(_pids(pids)) == []
    assert reap.subreaper() == 0


def test_a_plant_in_the_preview_seed_while_git_merges_is_refused(attached, monkeypatch):
    """N9c, second layer: `* merge=union` written into the seed while merge-tree runs and removed again."""
    from alpaca.attach import gitops
    wt = setup(attached, upstream_files={"src/main.py": "print(0)\n"})
    commit_in(wt, {"src/main.py": "print('branch')\n"}, "branch edit")
    attached.upstream.commit({"src/main.py": "print('upstream')\n"}, "upstream edit")
    real = gitops.run

    def planting(repo, *args, **kw):
        if "merge-tree" not in args:
            return real(repo, *args, **kw)
        attrs = Path(str(repo), ".git", "info", "attributes")
        attrs.write_text("* merge=union\n")
        try:
            return real(repo, *args, **kw)
        finally:
            attrs.unlink()
    monkeypatch.setattr(gitops, "run", planting)
    with pytest.raises(AttachError, match="changed the merge preview seed while git merged"):
        the_check(attached)
    monkeypatch.setattr(gitops, "run", real)
    res = the_check(attached)
    assert res.verdict == "FAIL" and res.conflicts == ["src/main.py"]


def test_an_inherited_template_hook_does_not_reach_the_gate_clones(attached, monkeypatch, tmp_path):
    """N3d: the release runner clones each gate folder with the process environment."""
    version = py_gate("test", "import sys; sys.exit(0 if open('VERSION').read() == open('EXPECTED').read() else 1)")
    wt = setup(attached, gates=(version,), upstream_files={"VERSION": "1\n", "EXPECTED": "1\n"})
    commit_in(wt, {"EXPECTED": "2\n"}, "the branch breaks the gate")
    tpl = tmp_path / "tpl"
    (tpl / "hooks").mkdir(parents=True)
    hook = tpl / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\ncase \"$PWD\" in */gate-*) echo 1 > EXPECTED ;; esac\n")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tpl))
    res = the_check(attached)
    assert res.verdict == "FAIL" and [(r["gate"], r["at"]) for r in res.regressions] == [("test", "head"), ("test", "preview")]
    assert os.environ["GIT_TEMPLATE_DIR"] == str(tpl)                    # put back after the gates
