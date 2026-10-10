"""alpaca/disk.py: the declared cleanup of dead scratch, stale /tmp leftovers and old run trees."""
import json
import os
import time

import pytest

from alpaca import child, cli, db, disk
from alpaca import project as projectfile

OLD = time.time() - 72 * 3600          # older than the 48 h grace and the 2 d prune age
ANCIENT = time.time() - 20 * 86400     # older than the 14 d open-session limit
SID_LIVE, SID_DEAD, SID_ME = "aaaa-live", "bbbb-dead", "cccc-me"
SID_OPEN, SID_RECENT_END = "gggg-open", "hhhh-ended-now"


def _age(path, when=OLD):
    """Backdate a folder and everything under it (the scratch rules look at the deepest write)."""
    if not os.path.isdir(path):
        os.utime(path, (when, when), follow_symlinks=False)
        return
    _age_tree(path, when)


def _age_tree(path, when=OLD):
    """Backdate everything under path, for the rules that look at the deepest write."""
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for n in filenames + dirnames:
            os.utime(os.path.join(dirpath, n), (when, when), follow_symlinks=False)
    os.utime(path, (when, when), follow_symlinks=False)


def _mk(path, files=("f.txt",)):
    os.makedirs(path, exist_ok=True)
    for f in files:
        with open(os.path.join(path, f), "w") as fh:
            fh.write("x" * 100)
    return path


@pytest.fixture
def setup(project, tmp_path):
    scratch_top = tmp_path / "claude-top"
    slug_dir = scratch_top / disk.paths.escape_cwd(os.path.realpath(project))
    tmp_root = tmp_path / "fake-tmp"
    tmp_root.mkdir()
    policy = {"grace_hours": 48, "ended_grace_hours": 12, "open_session_days": 14,
              "sweep_every_hours": 6, "scratch_base": str(scratch_top),
              "tmp_root": str(tmp_root), "tmp_globs": ["t[0-9]*-*", "pytest-t-*"],
              "prune": [{"glob": ".alpaca/verify/*/vf-*/v[0-9]*", "task_part": 2, "min_age_days": 2},
                        {"glob": ".alpaca/releases/*/checks", "keep_newest": 1, "min_age_days": 2}],
              "warn_gib": {"project": 30}}
    projectfile.save(project, {"name": "t", "disk": policy})
    return {"root": project, "slug": str(slug_dir), "tmp": str(tmp_root), "policy": policy}


def _iso(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def _plan(s, **kw):
    kw.setdefault("held", [])
    kw.setdefault("cited", set())
    kw.setdefault("sessions", {
        SID_LIVE: {"live": True, "ended": None, "last": None},
        SID_DEAD: {"live": False, "ended": "2026-01-01T00:00:00+00:00",
                   "last": "2026-01-01T00:00:00+00:00"},
        SID_OPEN: {"live": False, "ended": None, "last": "2026-01-01T00:00:00+00:00"},
        SID_RECENT_END: {"live": False, "ended": _iso(time.time() - 3600),
                         "last": _iso(time.time() - 3600)}})
    kw.setdefault("tasks", {"t-001": "done", "t-002": "doing"})
    kw.setdefault("reports", "")
    return disk.plan(s["root"], **kw)


def _removed(p):
    return sorted(os.path.basename(c["path"]) if c["kind"] != "prune" else c["path"]
                  for c in p["remove"])


def test_no_disk_block_removes_nothing(project):
    p = disk.plan(project, held=[], cited=set(), sessions={}, tasks={})
    assert p == {"policy": False, "remove": [], "keep": []}
    assert disk.spawn_sweep(project, "s1", "start") is False


def test_scratch_ended_session_goes_after_its_grace_others_stay(setup):
    for sid in (SID_LIVE, SID_DEAD, SID_ME, SID_OPEN, SID_RECENT_END):
        _mk(os.path.join(setup["slug"], sid, "scratchpad"))
        _age(os.path.join(setup["slug"], sid))
    p = _plan(setup, current_sid=SID_ME)
    assert _removed(p) == [SID_DEAD]
    kept = {os.path.basename(c["path"]): c["reason"] for c in p["keep"]}
    assert "live" in kept[SID_LIVE]
    assert "running this sweep" in kept[SID_ME]
    assert "not ended" in kept[SID_OPEN]            # open in the record: idle, maybe not dead
    assert "ended" in kept[SID_RECENT_END] and "under 12 h" in kept[SID_RECENT_END]


def test_scratch_open_or_unknown_session_needs_a_long_deep_quiet(setup):
    for sid in (SID_OPEN, "dddd-unknown-old", "eeee-unknown-deep-write"):
        d = _mk(os.path.join(setup["slug"], sid, "scratchpad", "rev"))
        _age(d, ANCIENT)
        _age(os.path.dirname(d), ANCIENT)
        _age(os.path.join(setup["slug"], sid), ANCIENT)
    deep = os.path.join(setup["slug"], "eeee-unknown-deep-write", "scratchpad", "rev", "f.txt")
    os.utime(deep, None)                           # a write deep down, top level still old
    p = _plan(setup)
    assert _removed(p) == ["dddd-unknown-old", SID_OPEN]
    assert any("unknown to the record" in c["reason"] and "deep-write" in c["path"]
               for c in p["keep"])


def test_no_record_means_no_scratch_sweep(setup):
    d = _mk(os.path.join(setup["slug"], "dddd-unknown-old", "scratchpad"))
    _age(os.path.dirname(d), ANCIENT)
    assert [c for c in _plan(setup, sessions=None)["remove"] if c["kind"] == "scratch"] == []
    assert disk._sessions(setup["root"] + "/nowhere") is None


def test_sessions_map_from_a_real_record(setup):
    conn = db.connect(setup["root"])
    db.upsert(conn, "sessions", "sid", {"sid": "s-ended", "started": _iso(OLD), "ended": _iso(OLD)})
    db.upsert(conn, "sessions", "sid", {"sid": "s-open", "started": _iso(OLD)})
    conn.close()
    m = disk._sessions(setup["root"])
    assert m["s-ended"]["ended"] and not m["s-ended"]["live"]
    assert m["s-open"]["ended"] is None and not m["s-open"]["live"]


def test_scratch_of_linked_worktree_sessions_counts_other_projects_do_not(setup):
    wt = setup["slug"] + "--alpaca-worktrees-x"
    other = setup["slug"] + "-old"                 # a sibling project, e.g. .../proj-old
    for base in (wt, other):
        _mk(os.path.join(base, SID_DEAD, "scratchpad"))
        _age(os.path.join(base, SID_DEAD))
    paths_removed = [c["path"] for c in _plan(setup)["remove"]]
    assert os.path.join(wt, SID_DEAD) in paths_removed
    assert not any(p.startswith(other) for p in paths_removed)


def test_keep_file_and_held_paths_keep_a_folder(setup):
    d = _mk(os.path.join(setup["slug"], SID_DEAD, "scratchpad"))
    _age(os.path.dirname(d))
    open(os.path.join(d, disk.KEEP_FILE), "w").close()
    _age(os.path.dirname(d))
    p = _plan(setup)
    assert p["remove"] == [] and ".keep" in p["keep"][0]["reason"]
    os.remove(os.path.join(d, disk.KEEP_FILE))
    _age(os.path.dirname(d))
    p = _plan(setup, held=[os.path.join(d, "f.txt")])
    assert p["remove"] == [] and "process" in p["keep"][0]["reason"]


def test_held_paths_sees_a_real_process_cwd(tmp_path):
    import subprocess, sys
    d = _mk(str(tmp_path / "busy"))
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=d)
    try:
        time.sleep(0.2)
        assert os.path.realpath(d) in [os.path.realpath(h) for h in disk.held_paths()]
    finally:
        proc.kill(); proc.wait()


def test_scratch_bases_follow_claude_code_tmpdir(project, tmp_path, monkeypatch):
    top = tmp_path / "ctmp"
    base = top / ("claude-%d" % os.getuid()) / disk.paths.escape_cwd(os.path.realpath(project))
    base.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(top))
    assert str(base) in disk.scratch_bases(project, {"scratch_base": None})


def test_cited_paths_read_seal_pointers(project):
    d = os.path.join(project, ".alpaca", "proofs", "op-1")
    os.makedirs(d)
    with open(os.path.join(d, "t-1.seal.json"), "w") as fh:
        json.dump({"evidence": [{"pointer": "local:.alpaca/verify/t-1/x.log"},
                                {"pointer": "remote:abc"}]}, fh)
    assert disk.cited_paths(project) == {os.path.realpath(os.path.join(project, ".alpaca/verify/t-1/x.log"))}


def test_tmp_globs_age_and_owner(setup):
    for n in ("t173-full", "pytest-t-9", "tmux-1000", "t174-new"):
        _mk(os.path.join(setup["tmp"], n))
    for n in ("t173-full", "pytest-t-9", "tmux-1000"):
        _age(os.path.join(setup["tmp"], n))
    p = _plan(setup)
    assert _removed(p) == ["pytest-t-9", "t173-full"]
    assert any(c["path"].endswith("t174-new") and "grace" in c["reason"] for c in p["keep"])


def test_prune_waits_for_done_task_age_and_spares_cited(setup):
    root = setup["root"]
    done_v1 = _mk(os.path.join(root, ".alpaca/verify/t-001/vf-t-001-1/v1"))
    _mk(os.path.join(root, ".alpaca/verify/t-001/vf-t-001-1"), files=("claims.json",))
    open_v1 = _mk(os.path.join(root, ".alpaca/verify/t-002/vf-t-002-1/v1"))
    for d in (done_v1, open_v1):
        _age(d)
    p = _plan(setup)
    assert [c["path"] for c in p["remove"]] == [done_v1]
    assert os.path.exists(os.path.join(root, ".alpaca/verify/t-001/vf-t-001-1/claims.json"))
    assert any(c["path"] == open_v1 and "not done" in c["reason"] for c in p["keep"])
    p = _plan(setup, cited={os.path.realpath(os.path.join(done_v1, "f.txt"))})
    assert p["remove"] == [] and any("proof" in c["reason"] for c in p["keep"])


def test_prune_keep_newest(setup):
    root = setup["root"]
    a = _mk(os.path.join(root, ".alpaca/releases/aaa/checks"))
    b = _mk(os.path.join(root, ".alpaca/releases/bbb/checks"))
    _age(a, OLD - 3600)
    _age(b, OLD)
    p = _plan(setup)
    assert [c["path"] for c in p["remove"]] == [a]
    assert any(c["path"] == b and "newest" in c["reason"] for c in p["keep"])


def test_prune_spares_a_tree_a_receipt_names(setup):
    root = setup["root"]
    rule = {"glob": ".alpaca/releases/*/checks", "min_age_days": 2,
            "named_in": ".alpaca/releases/receipts/*.json", "name_part": 2}
    projectfile.save(root, {"name": "t", "disk": dict(setup["policy"], prune=[rule])})
    shipped = _mk(os.path.join(root, ".alpaca/releases/aaa111/checks"))
    dropped = _mk(os.path.join(root, ".alpaca/releases/bbb222/checks"))
    for d in (shipped, dropped):
        _age(d)
    _mk(os.path.join(root, ".alpaca/releases/receipts"), files=())
    with open(os.path.join(root, ".alpaca/releases/receipts/x-publish.json"), "w") as fh:
        json.dump({"attempt": "aaa111"}, fh)
    p = _plan(setup)
    assert [c["path"] for c in p["remove"]] == [dropped]
    assert any(c["path"] == shipped and "names aaa111" in c["reason"] for c in p["keep"])
    os.remove(os.path.join(root, ".alpaca/releases/receipts/x-publish.json"))
    p = _plan(setup)                               # no receipts at all: keep everything
    assert p["remove"] == [] and all("named_in" in c["reason"] for c in p["keep"])


def test_prune_spares_a_path_a_proof_report_names_and_files_with_dirs_only(setup):
    root = setup["root"]
    rule = {"glob": ".alpaca/releases/*/checks/*", "dirs_only": True, "min_age_days": 2}
    projectfile.save(root, {"name": "t", "disk": dict(setup["policy"], prune=[rule])})
    named = _mk(os.path.join(root, ".alpaca/releases/aaa/checks/gate-1"))
    bulk = _mk(os.path.join(root, ".alpaca/releases/aaa/checks/gate-0"))
    _mk(os.path.join(root, ".alpaca/releases/aaa/checks"), files=("dev-pytest.log",))
    for d in (named, bulk, os.path.join(root, ".alpaca/releases/aaa/checks/dev-pytest.log")):
        _age(d)
    p = _plan(setup, reports="see .alpaca/releases/aaa/checks/gate-1 for the run")
    assert [c["path"] for c in p["remove"]] == [bulk]
    assert any(c["path"] == named and "proof report" in c["reason"] for c in p["keep"])
    p = _plan(setup, reports=None)                  # read from the real (empty) proofs dir
    assert sorted(c["path"] for c in p["remove"]) == [bulk, named]


def test_prune_glob_cannot_leave_the_root(setup):
    projectfile.save(setup["root"], {"name": "t", "disk": dict(setup["policy"], prune=[{"glob": "../*"}])})
    assert any("under the project root" in e for e in disk.policy_errors(setup["root"]))
    assert _plan(setup)["remove"] == []


@pytest.mark.parametrize("mode", [0o500, 0o300, 0o000])
def test_apply_removes_records_and_handles_locked_dirs(setup, mode):
    d = _mk(os.path.join(setup["slug"], SID_DEAD, "scratchpad", "ro", "deep"))
    _age_tree(os.path.join(setup["slug"], SID_DEAD))
    os.chmod(d, mode)                              # a locked folder inside a locked folder
    os.chmod(os.path.dirname(d), mode)
    _age(os.path.join(setup["slug"], SID_DEAD))
    planned = _plan(setup)
    db.connect(setup["root"]).close()
    r = disk.apply(setup["root"], planned, session="s-test")
    assert not os.path.exists(os.path.join(setup["slug"], SID_DEAD))
    assert r["failed"] == [] and r["bytes"] > 0
    conn = db.connect(setup["root"])
    ev = conn.execute("SELECT data FROM events WHERE kind='disk-gc'").fetchall()
    assert len(ev) == 1 and json.loads(ev[0][0])["removed"][0]["kind"] == "scratch"


def test_apply_never_follows_a_symlink(setup, tmp_path):
    outside = _mk(str(tmp_path / "outside"))
    os.makedirs(setup["slug"], exist_ok=True)
    link = os.path.join(setup["slug"], SID_DEAD)
    os.symlink(outside, link)
    assert _plan(setup)["remove"] == []          # a symlinked session folder is never a candidate
    disk.remove(link)
    assert os.path.exists(os.path.join(outside, "f.txt"))


def test_sweep_runs_when_due_and_stamps(setup):
    _mk(os.path.join(setup["tmp"], "t9-full"))
    _age(os.path.join(setup["tmp"], "t9-full"))
    db.connect(setup["root"]).close()
    r = disk.sweep(setup["root"], session=SID_ME, mode="start")
    assert r is not None and not os.path.exists(os.path.join(setup["tmp"], "t9-full"))
    assert disk.sweep(setup["root"], session=SID_ME, mode="start") is None   # just swept


def test_sweep_is_locked(setup):
    fh = disk._lock(setup["root"])
    try:
        assert disk.sweep(setup["root"], session="x", mode="start") is None
    finally:
        fh.close()


def test_doctor_check_warns_over_threshold(setup, monkeypatch):
    monkeypatch.setattr(disk, "usage", lambda root, policy=None: {"project": 31 * disk.GIB,
                                                                 "scratch": None, "tmp": 1})
    rows = []
    disk.doctor_check(setup["root"], lambda n, lvl, d="": rows.append((n, lvl, d)))
    [(name, level, detail)] = rows
    assert (name, level) == ("disk", "warn") and ".alpaca over 30G" in detail


def test_retention_gc_cli_dry_run_then_apply(setup, capsys, monkeypatch):
    monkeypatch.setattr(disk, "held_paths", lambda: [])
    d = _mk(os.path.join(setup["tmp"], "t9-full"))
    _age(d)
    db.connect(setup["root"]).close()
    assert cli.main(["retention", "gc"]) == cli.PASS
    assert "would remove" in capsys.readouterr().out and os.path.exists(d)
    assert cli.main(["retention", "gc", "--apply"]) == cli.PASS
    out = capsys.readouterr().out
    assert not os.path.exists(d) and "removed tmp" in out and "removed 1 path(s)" in out


def test_spawn_sweep_starts_a_detached_process_only_when_due(setup, monkeypatch):
    assert disk.spawn_sweep(setup["root"], "s1", "end") is False            # off under tests
    monkeypatch.delenv("ALPACA_DISK_SWEEP")
    calls = []
    monkeypatch.setattr(disk.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    assert disk.spawn_sweep(setup["root"], "s1", "end") is True
    argv, kw = calls[-1]
    # through the one launcher (t-070 round 2 C-A; t-428): python -I alpaca/boot.py --path <json> -m
    assert argv[1:4] == ["-I", child.boot_path(), "--path"] and argv[5:8] == ["-m", "alpaca.disk", "sweep"]
    assert kw["start_new_session"] is True and "PYTHONPATH" not in kw["env"]
    with open(os.path.join(disk.paths.config_dir(setup["root"]), disk.STAMP_NAME), "w") as fh:
        fh.write("0\n")
    assert disk.spawn_sweep(setup["root"], "s1", "start") is False          # swept recently
    os.utime(os.path.join(disk.paths.config_dir(setup["root"]), disk.STAMP_NAME), (OLD, OLD))
    assert disk.spawn_sweep(setup["root"], "unknown", "start") is False


def test_session_end_starts_the_sweep(setup, monkeypatch):
    from alpaca.hooks import session_end
    seen = []
    monkeypatch.setattr(disk, "spawn_sweep", lambda root, sid, mode: seen.append((sid, mode)))
    session_end._start_disk_sweep(setup["root"], "s1", {"reason": "prompt_input_exit"})
    assert seen == [("s1", "end")]


def test_main_entry_never_raises(setup):
    assert disk._main(["sweep", "--root", setup["root"] + "/missing", "--session", "s",
                       "--mode", "start"]) == 0


def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo_with_worktrees(setup):
    root = setup["root"]
    projectfile.save(root, {"name": "t", "disk": dict(setup["policy"], prune=[],
                                                       worktrees={"min_age_days": 2})})
    _git(root, "init", "-q", "-b", "main")
    with open(os.path.join(root, ".gitignore"), "w") as fh:
        fh.write(".alpaca/\n__pycache__/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    wts = {}
    for name, branch in (("op001-done", "op001/done"), ("op002-open", "op002/open"),
                         ("op001-unmerged", "op001/unmerged"), ("op001-dirty", "op001/dirty"),
                         ("scratch-x", "x/scratch"), ("op001-reused", "op002/reused"),
                         ("op001-ignored", "op001/ignored"), ("op001-skip", "op001/skip")):
        path = os.path.join(root, ".alpaca", "worktrees", name)
        _git(root, "worktree", "add", "-q", "-b", branch, path, "main")
        wts[name] = path
    with open(os.path.join(wts["op001-unmerged"], "new.txt"), "w") as fh:
        fh.write("x")
    _git(wts["op001-unmerged"], "add", "new.txt")
    _git(wts["op001-unmerged"], "commit", "-qm", "unmerged")
    with open(os.path.join(wts["op001-dirty"], "junk.txt"), "w") as fh:
        fh.write("x")
    os.makedirs(os.path.join(wts["op001-ignored"], ".alpaca"))
    with open(os.path.join(wts["op001-ignored"], ".alpaca", "alpaca.db"), "w") as fh:
        fh.write("a worktree's own record")          # ignored by .gitignore, still wanted
    os.makedirs(os.path.join(wts["op001-done"], "__pycache__"))
    with open(os.path.join(wts["op001-done"], "__pycache__", "x.pyc"), "w") as fh:
        fh.write("cache")                            # ignored junk does not hold a worktree
    _git(wts["op001-skip"], "update-index", "--skip-worktree", ".gitignore")
    for p in wts.values():
        _age(p)
    conn = db.connect(root)                          # apply re-reads op status from the record
    db.upsert(conn, "ops", "id", {"id": "op-001", "intent": "x", "status": "closed"})
    db.upsert(conn, "ops", "id", {"id": "op-002", "intent": "y", "status": "open"})
    conn.close()
    return setup, wts


def test_worktrees_of_closed_ops_merged_and_clean_go(repo_with_worktrees):
    setup, wts = repo_with_worktrees
    ops = {"op-001": "closed", "op-002": "open"}
    p = _plan(setup, ops=ops)
    assert [c["path"] for c in p["remove"] if c["kind"] == "worktree"] == [wts["op001-done"]]
    kept = {os.path.basename(c["path"]): c["reason"] for c in p["keep"] if c["kind"] == "worktree"}
    assert "op-002 is open" in kept["op002-open"]
    assert "not merged" in kept["op001-unmerged"]
    assert "uncommitted or untracked" in kept["op001-dirty"]
    assert "no op" in kept["scratch-x"]
    assert "op-002 is open" in kept["op001-reused"]  # the branch names an open op
    assert "ignored files" in kept["op001-ignored"]
    assert "skip-worktree" in kept["op001-skip"]
    db.connect(setup["root"]).close()
    r = disk.apply(setup["root"], p, session="s-test")
    assert r["failed"] == [] and not os.path.exists(wts["op001-done"])
    import subprocess
    branches = subprocess.run(["git", "-C", setup["root"], "branch", "--list", "op001/done"],
                              capture_output=True, text=True).stdout
    assert branches.strip() == ""                  # merged branch deleted with -d
    assert os.path.exists(wts["op001-unmerged"]) and os.path.exists(wts["op001-dirty"])


def test_worktree_apply_rechecks_head(repo_with_worktrees):
    setup, wts = repo_with_worktrees
    p = _plan(setup, ops={"op-001": "closed", "op-002": "open"})
    with open(os.path.join(wts["op001-done"], "late.txt"), "w") as fh:
        fh.write("x")
    _git(wts["op001-done"], "add", "late.txt")
    _git(wts["op001-done"], "commit", "-qm", "a commit after the plan")
    db.connect(setup["root"]).close()
    r = disk.apply(setup["root"], p, session="s-test")
    assert r["removed"] == [] and "HEAD moved" in r["failed"][0]["error"]
    assert os.path.exists(os.path.join(wts["op001-done"], "late.txt"))


def test_worktrees_wait_for_quiet_and_need_a_record(repo_with_worktrees):
    setup, wts = repo_with_worktrees
    os.utime(wts["op001-done"], None)
    p = _plan(setup, ops={"op-001": "closed"})
    assert not [c for c in p["remove"] if c["kind"] == "worktree"]
    assert [c for c in _plan(setup, ops=None)["keep"] if c["kind"] == "worktree"] == []


def test_rune2_scratch_rule_waits_for_done(setup):
    root = setup["root"]
    rule = {"glob": ".alpaca/rune2-development/execution/t-*/*scratch", "dirs_only": True,
            "task_part": 3, "min_age_days": 2}
    projectfile.save(root, {"name": "t", "disk": dict(setup["policy"], prune=[rule])})
    done = _mk(os.path.join(root, ".alpaca/rune2-development/execution/t-001/scratch"))
    attack = _mk(os.path.join(root, ".alpaca/rune2-development/execution/t-001/attack-scratch"))
    live = _mk(os.path.join(root, ".alpaca/rune2-development/execution/t-002/scratch"))
    _mk(os.path.join(root, ".alpaca/rune2-development/execution/t-001"), files=("report.md",))
    for d in (done, attack, live):
        _age(d)
    p = _plan(setup)
    assert sorted(c["path"] for c in p["remove"]) == sorted([attack, done])
    assert any(c["path"] == live and "not done" in c["reason"] for c in p["keep"])
