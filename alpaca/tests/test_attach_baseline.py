"""The gate baseline (AC-03) and the pre-mortem attacks of t-648 that concern it (build plan 6.2)."""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from alpaca import cli, db
from alpaca.attach import AttachError, baseline, config, gitops, safefs
from alpaca.tests.attachkit import (attached, counting_gates, git, set_gates, studio, upstream)  # noqa: F401


def events(root, kind):
    conn = db.connect(str(root))
    try:
        return [e for e in db.events(conn, limit=10 ** 9) if e["kind"] == kind]
    finally:
        conn.close()


def files(root):
    return sorted((Path(str(root)) / ".alpaca/attach/baseline").glob("*.json"))


def cfg_of(att):
    return config.load(str(att.root))


def tip(att):
    return git(att.app, "rev-parse", "refs/remotes/origin/main")


# ---- AC-03 ------------------------------------------------------------------------------------------------------------

def test_onboarding_baseline_lands_on_op_0_with_sha_and_each_gate_result(attached):
    """AC-03: attach records the base sha and, per gate, exit status, duration and output tail on op-0."""
    found = events(attached.root, "attach-baseline")
    assert len(found) == 1 and found[0]["op"] == "op-0"
    data = found[0]["data"]
    assert data["base_sha"] == attached.upstream.sha()
    assert [g["name"] for g in data["gates"]] == ["build", "test"]
    for gate in data["gates"]:
        assert gate["returncode"] == 0 and gate["passed"] is True and gate["timed_out"] is False
        assert isinstance(gate["duration_s"], float) and gate["duration_s"] >= 0
    assert "build" in data["gates"][0]["log_tail"] and "test" in data["gates"][1]["log_tail"]
    assert data["red"] == []


def test_baseline_file_has_the_planned_shape_and_is_keyed(attached):
    (one,) = files(attached.root)
    doc = json.loads(one.read_text())
    cfg = cfg_of(attached)
    sha = attached.upstream.sha()
    assert one.name == "%s.json" % baseline.key(cfg, sha) and re.fullmatch(r"[0-9a-f]{16}", doc["key"])
    assert doc["schema"] == 1 and doc["base_sha"] == sha and doc["gate_set"] == cfg.gate_set_digest()
    assert doc["created_at"] and doc["host"]
    for gate in doc["gates"]:
        assert set(gate) == {"name", "returncode", "timed_out", "passed", "duration_s", "detail", "log_tail", "definition"}
        assert gate["definition"]
    assert baseline.load(str(attached.root), sha, cfg) == doc
    assert stat_mode(one) == 0o600


def stat_mode(path):
    return os.stat(str(path)).st_mode & 0o777


def test_a_red_gate_is_recorded_not_raised(attached):
    set_gates(attached.root, [{"name": "broken", "command": ["sh", "-c", "echo oops; exit 3"], "timeout": 30},
                              {"name": "fine", "command": ["true"], "timeout": 30}])
    got = baseline.ensure(str(attached.root), tip(attached), task=None)
    broken, fine = got["gates"]
    assert (broken["returncode"], broken["passed"]) == (3, False) and "oops" in broken["log_tail"]
    assert broken["detail"] == "exit 3" and fine["passed"] is True
    assert events(attached.root, "attach-baseline")[-1]["data"]["red"] == ["broken"]


def test_every_gate_runs_even_when_an_earlier_one_fails(attached, tmp_path):
    """fail_fast is off (P9): gates `false` then `true` both run."""
    marker = tmp_path / "second-ran"
    set_gates(attached.root, [{"name": "first", "command": ["false"], "timeout": 30},
                              {"name": "second", "command": [sys.executable, "-c", "open(%r, 'w').write('x')" % str(marker)],
                               "timeout": 30}])
    got = baseline.ensure(str(attached.root), tip(attached))
    assert [g["passed"] for g in got["gates"]] == [False, True] and marker.exists()


def test_a_gate_that_edits_a_tracked_file_passes_with_a_note(attached):
    """P9: pass is exit status alone; the release rule `changed tracked input files` becomes a note."""
    set_gates(attached.root, [{"name": "dirty", "command": ["sh", "-c", "echo more >> README.md"], "timeout": 30}])
    (gate,) = baseline.ensure(str(attached.root), tip(attached))["gates"]
    assert gate["returncode"] == 0 and gate["passed"] is True
    assert "tracked input files" in gate["detail"] and gate["detail"].startswith("note:")


def test_a_gate_that_runs_out_of_time_is_red_with_timed_out(attached):
    set_gates(attached.root, [{"name": "slow", "command": ["sleep", "60"], "timeout": 1}])
    (gate,) = baseline.ensure(str(attached.root), tip(attached))["gates"]
    assert gate["timed_out"] is True and gate["passed"] is False and gate["detail"] == "timed out"


def test_results_keep_the_configured_gate_order_not_the_timeout_order(attached):
    set_gates(attached.root, [{"name": "slow-one", "command": ["true"], "timeout": 90},
                              {"name": "quick-one", "command": ["true"], "timeout": 5}])
    got = baseline.ensure(str(attached.root), tip(attached))
    assert [g["name"] for g in got["gates"]] == ["slow-one", "quick-one"]


# ---- one baseline per key (t-648.3, t-648.4) ------------------------------------------------------------------------

def test_parallel_ensure_writes_one_file_and_runs_each_gate_once(attached, tmp_path):
    """t-648.3: four callers at once; one file; each gate ran exactly once."""
    counter = tmp_path / "runs.txt"
    set_gates(attached.root, counting_gates(counter))
    before = len(files(attached.root))
    sha = tip(attached)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda i: baseline.ensure(str(attached.root), sha, task=None, session="s%d" % i), range(4)))
    assert len(files(attached.root)) == before + 1
    assert sorted(counter.read_text().split()) == ["one", "two"]
    assert len({json.dumps(r, sort_keys=True) for r in results}) == 1
    assert len([e for e in events(attached.root, "attach-baseline") if e["data"]["gates"][0]["name"] == "one"]) == 1
    assert not list((attached.root / ".alpaca/attach/baseline").glob(".tmp-*"))


def test_a_second_ensure_reads_the_file_and_runs_nothing(attached, tmp_path):
    counter = tmp_path / "runs.txt"
    set_gates(attached.root, counting_gates(counter))
    first = baseline.ensure(str(attached.root), tip(attached))
    second = baseline.ensure(str(attached.root), tip(attached))
    assert first == second and len(counter.read_text().split()) == 2


def test_key_changes_with_the_gate_set(attached):
    """t-648.4: another gate command, another name or another timeout is another baseline."""
    sha = tip(attached)
    set_gates(attached.root, [{"name": "test", "command": ["true"], "timeout": 5}])
    k1 = baseline.key(cfg_of(attached), sha)
    set_gates(attached.root, [{"name": "test", "command": ["false"], "timeout": 5}])
    k2 = baseline.key(cfg_of(attached), sha)
    set_gates(attached.root, [{"name": "renamed", "command": ["false"], "timeout": 5}])
    k3 = baseline.key(cfg_of(attached), sha)
    set_gates(attached.root, [{"name": "renamed", "command": ["false"], "timeout": 6}])
    k4 = baseline.key(cfg_of(attached), sha)
    assert len({k1, k2, k3, k4}) == 4


def test_a_new_gate_set_gets_a_new_baseline_with_new_definitions(attached):
    sha = tip(attached)
    original = [dict(g) for g in cfg_of(attached).gates]
    old = baseline.ensure(str(attached.root), sha)
    set_gates(attached.root, [{"name": "test", "command": ["true"], "timeout": 5}])
    new = baseline.ensure(str(attached.root), sha)
    assert new["key"] != old["key"] and len(files(attached.root)) == 2
    assert new["gates"][0]["definition"] != old["gates"][0]["definition"]
    set_gates(attached.root, original)                      # the old gate set finds its own file again
    assert baseline.load(str(attached.root), sha)["key"] == old["key"]


def test_the_key_changes_with_the_base_sha(attached):
    cfg = cfg_of(attached)
    assert baseline.key(cfg, "a" * 40) != baseline.key(cfg, "b" * 40)


@pytest.mark.parametrize("edit", [
    lambda d: d.update(gate_set="0" * 64),
    lambda d: d.update(key="0" * 16),
    lambda d: d.update(base_sha="0" * 40),
    lambda d: d.update(schema=2),
    lambda d: d["gates"].pop(),
])
def test_a_file_that_does_not_match_is_not_a_baseline(attached, edit):
    (one,) = files(attached.root)
    doc = json.loads(one.read_text())
    edit(doc)
    one.write_text(json.dumps(doc))
    assert baseline.load(str(attached.root), attached.upstream.sha()) is None


def test_garbage_in_the_file_is_not_a_baseline_and_ensure_replaces_it(attached):
    (one,) = files(attached.root)
    one.write_text("{not json")
    sha = attached.upstream.sha()
    assert baseline.load(str(attached.root), sha) is None
    got = baseline.ensure(str(attached.root), sha)
    assert got["base_sha"] == sha and json.loads(one.read_text())["key"] == got["key"]


def test_an_event_that_cannot_be_written_leaves_no_baseline_file(attached, monkeypatch):
    set_gates(attached.root, [{"name": "only", "command": ["true"], "timeout": 5}])

    def boom(*a, **k):
        raise AttachError("record is locked")
    monkeypatch.setattr(baseline, "_record", boom)
    with pytest.raises(AttachError, match="record is locked"):
        baseline.ensure(str(attached.root), tip(attached))
    assert baseline.load(str(attached.root), tip(attached)) is None


# ---- the seed and the run folder -------------------------------------------------------------------------------------

def test_the_run_folder_is_gone_after_a_run_and_a_leftover_is_cleared(attached):
    runs = attached.root / ".alpaca/attach/runs"
    sha = tip(attached)
    set_gates(attached.root, [{"name": "only", "command": ["true"], "timeout": 5}])
    key = baseline.key(cfg_of(attached), sha)
    stale = runs / "_baseline" / key
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("left by a killed run")
    baseline.ensure(str(attached.root), sha)
    assert not stale.exists()


def test_make_seed_holds_only_the_named_commit_and_changes_nothing_in_app(attached, tmp_path):
    head_before = git(attached.app, "rev-parse", "HEAD")
    status_before = git(attached.app, "status", "--porcelain", "--ignored")
    refs_before = git(attached.app, "for-each-ref")
    attached.upstream.commit({"later.txt": "x\n"}, "later")
    older = git(attached.app, "rev-parse", "HEAD")           # app stays on the older commit
    dest = safefs.make_dir(str(tmp_path), str(tmp_path / "seed"))
    seed = baseline.make_seed(str(attached.app), dest, {"refs/preview/base": older})
    assert git(seed, "rev-parse", "HEAD") == older == git(seed, "rev-parse", "refs/preview/base")
    assert git(seed, "status", "--porcelain") == ""
    assert git(seed, "rev-list", "--all", "--count") == git(attached.app, "rev-list", "--count", older)
    assert git(attached.app, "rev-parse", "HEAD") == head_before
    assert git(attached.app, "status", "--porcelain", "--ignored") == status_before
    assert git(attached.app, "for-each-ref") == refs_before


def test_make_seed_can_fetch_by_ref_name_and_verifies_the_sha(attached, tmp_path):
    tipsha = git(attached.app, "rev-parse", "HEAD")
    ok = safefs.make_dir(str(tmp_path), str(tmp_path / "ok"))
    baseline.make_seed(str(attached.app), ok, {"refs/preview/a": ("refs/heads/main", tipsha)})
    assert git(ok, "rev-parse", "refs/preview/a") == tipsha
    bad = safefs.make_dir(str(tmp_path), str(tmp_path / "bad"))
    other = git(attached.app, "rev-parse", "HEAD~1")
    with pytest.raises(AttachError, match="not the expected"):
        baseline.make_seed(str(attached.app), bad, {"refs/preview/a": ("refs/heads/main", other)})


@pytest.mark.parametrize("refs", [{}, {"refs/preview/x": "not-a-sha"}, {"preview/x": "a" * 40}, {"refs/../x": "a" * 40},
                                  {"refs/preview/x": ("--upload-pack=x", "a" * 40)}])
def test_make_seed_refuses_bad_input(attached, tmp_path, refs):
    dest = safefs.make_dir(str(tmp_path), str(tmp_path / "seed"))
    with pytest.raises(AttachError):
        baseline.make_seed(str(attached.app), dest, refs)


def test_make_seed_needs_an_existing_folder_of_its_own(attached, tmp_path):
    with pytest.raises(AttachError, match="not a folder"):
        baseline.make_seed(str(attached.app), str(tmp_path / "missing"), {"refs/preview/x": "a" * 40})
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(AttachError, match="not a folder"):
        baseline.make_seed(str(attached.app), str(tmp_path / "link"), {"refs/preview/x": "a" * 40})


def test_a_baseline_leaves_app_pristine(attached):
    set_gates(attached.root, [{"name": "only", "command": ["true"], "timeout": 5}])
    baseline.ensure(str(attached.root), tip(attached))
    assert git(attached.app, "status", "--porcelain", "--ignored") == ""
    assert git(attached.app, "worktree", "list").count("\n") == 0


def test_inherited_git_variables_do_not_reach_the_gates(attached, monkeypatch):
    """P4, X1: with GIT_DIR pointing at the studio, a gate still sees its own clone; the variable is back after."""
    monkeypatch.setenv("GIT_DIR", str(attached.root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(attached.root))
    set_gates(attached.root, [{"name": "where", "command": ["git", "rev-parse", "--show-toplevel"], "timeout": 30}])
    (gate,) = baseline.ensure(str(attached.root), tip(attached))["gates"]
    assert gate["passed"] is True and "gate-0" in gate["log_tail"]
    assert os.environ["GIT_DIR"] == str(attached.root / ".git") and os.environ["GIT_WORK_TREE"] == str(attached.root)


def test_the_token_never_reaches_a_gate(attached, monkeypatch):
    """P5: the gate prints the value of each credential variable itself, so the answer does not depend on
    where in a long `env` listing the line falls (the earlier test searched a 2000-character tail)."""
    names = ("ALPACA_ATTACH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "PUBLIC_REPO_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS")
    for name in names:
        monkeypatch.setenv(name, "sekrit-token-value")
    printer = "import os; print('SEEN=' + '|'.join(os.environ.get(n, '') for n in %r))" % (names,)
    set_gates(attached.root, [{"name": "env", "command": [sys.executable, "-c", printer], "timeout": 30}])
    (gate,) = baseline.ensure(str(attached.root), tip(attached))["gates"]
    assert gate["passed"] is True and "SEEN=|||||" in gate["log_tail"], gate["log_tail"]
    assert "sekrit-token-value" not in gate["log_tail"] and "sekrit-token-value" not in json.dumps(
        events(attached.root, "attach-baseline")[-1]["data"])
    assert all(os.environ[n] == "sekrit-token-value" for n in names)                 # back for the push that follows


def test_the_scrub_hides_the_credentials_and_puts_them_back(monkeypatch):
    for name in ("ALPACA_ATTACH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "PUBLIC_REPO_TOKEN"):
        monkeypatch.setenv(name, "v-" + name)
    monkeypatch.setenv("KEEP", "yes")
    with gitops.process_env_scrubbed():
        assert not [n for n in ("ALPACA_ATTACH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "PUBLIC_REPO_TOKEN") if n in os.environ]
        assert os.environ["KEEP"] == "yes"
    assert os.environ["ALPACA_ATTACH_TOKEN"] == "v-ALPACA_ATTACH_TOKEN" and gitops.token() == "v-ALPACA_ATTACH_TOKEN"


def test_ensure_needs_a_full_sha_and_a_clone(attached):
    for bad in ("main", "abc", "--upload-pack=x", "", None, "A" * 40 + "z"):
        with pytest.raises(AttachError, match="not a full commit sha"):
            baseline.ensure(str(attached.root), bad)


def test_ensure_for_a_sha_the_clone_does_not_hold_fails_and_leaves_nothing(attached):
    with pytest.raises(AttachError):
        baseline.ensure(str(attached.root), "c" * 40)
    assert baseline.load(str(attached.root), "c" * 40) is None
    assert not (attached.root / ".alpaca/attach/runs/_baseline").exists() or not list(
        (attached.root / ".alpaca/attach/runs/_baseline").iterdir())


def test_a_link_where_the_baseline_folder_goes_is_refused(attached, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    folder = attached.root / ".alpaca/attach/baseline"
    for item in folder.iterdir():
        item.unlink()
    folder.rmdir()
    folder.symlink_to(outside)
    set_gates(attached.root, [{"name": "only", "command": ["true"], "timeout": 5}])
    with pytest.raises(AttachError, match="is a link"):
        baseline.ensure(str(attached.root), tip(attached))
    assert list(outside.iterdir()) == []


# ---- the verb ---------------------------------------------------------------------------------------------------------

def test_the_baseline_verb_reports_each_gate_and_reuses_the_recorded_one(attached, capsys):
    assert cli.main(["attach", "baseline"]) == cli.PASS
    out = capsys.readouterr().out
    assert "recorded before" in out and "gate build: PASS exit 0" in out and "GATE alpaca-attach-baseline: PASS" in out
    assert len(events(attached.root, "attach-baseline")) == 1


def test_the_baseline_verb_records_a_new_base_after_a_fetch(attached, capsys):
    new = attached.upstream.commit({"x.txt": "1\n"}, "advance")
    assert cli.main(["attach", "baseline", "--fetch", "--json"]) == cli.PASS
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "PASS" and out["receipt"]["base_sha"] == new and out["receipt"]["gates"] == {"build": True, "test": True}
    assert len(files(attached.root)) == 2 and len(events(attached.root, "attach-baseline")) == 2


def test_the_baseline_verb_refuses_a_bad_sha(attached):
    assert cli.main(["attach", "baseline", "--sha", "nope"]) == cli.FAIL


def test_the_baseline_verb_on_a_plain_project_fails_with_a_reason(studio, capsys):
    assert cli.main(["attach", "baseline"]) == cli.FAIL
    assert "not attached" in capsys.readouterr().out


# ---- fix round 1, N1: a baseline file counts only with its record event --------------------------------------------------

def test_a_hand_written_baseline_file_without_an_event_is_not_a_baseline(attached):
    sha = tip(attached)
    set_gates(attached.root, [{"name": "g", "command": ["true"], "timeout": 5}])
    cfg = cfg_of(attached)
    fake = {"schema": 1, "key": baseline.key(cfg, sha), "base_sha": sha, "gate_set": cfg.gate_set_digest(),
            "gates": [{"name": "g", "passed": False, "returncode": 1, "timed_out": False}]}
    target = baseline.path(str(attached.root), sha, cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fake))
    assert baseline.load(str(attached.root), sha) is None
    got = baseline.ensure(str(attached.root), sha)
    assert got["gates"][0]["passed"] is True
    assert baseline.load(str(attached.root), sha) is not None


def test_a_baseline_file_edited_after_the_run_is_not_a_baseline(attached):
    sha = tip(attached)
    first = baseline.ensure(str(attached.root), sha)
    assert baseline.load(str(attached.root), sha) is not None
    target = baseline.path(str(attached.root), sha)
    edited = json.loads(target.read_text())
    edited["gates"][0]["passed"] = not edited["gates"][0]["passed"]
    target.write_text(json.dumps(edited))
    assert baseline.load(str(attached.root), sha) is None
    again = baseline.ensure(str(attached.root), sha)
    assert [g["passed"] for g in again["gates"]] == [g["passed"] for g in first["gates"]]


def test_a_failed_event_removes_the_file_it_wrote_and_a_retry_redoes_both(attached, monkeypatch):
    set_gates(attached.root, [{"name": "g", "command": ["true"], "timeout": 5}])
    sha = tip(attached)

    def boom(*a, **k):
        raise AttachError("record is locked")
    monkeypatch.setattr(baseline, "_record", boom)
    with pytest.raises(AttachError, match="record is locked"):
        baseline.ensure(str(attached.root), sha)
    assert not baseline.path(str(attached.root), sha).exists()                         # file and event go together
    monkeypatch.undo()
    assert baseline.ensure(str(attached.root), sha)["gates"][0]["passed"] is True


def test_a_link_at_the_baseline_folder_is_not_followed_when_the_event_fails(attached, monkeypatch, tmp_path):
    """The undo of a failed event deletes the file this call wrote and nothing a link points at (B1 sibling)."""
    set_gates(attached.root, [{"name": "g", "command": ["true"], "timeout": 5}])
    sha = tip(attached)
    target = baseline.path(str(attached.root), sha)
    outside = tmp_path / "outside-baseline"
    outside.mkdir()
    (outside / target.name).write_text("keep\n")
    real_write = config.write_json_atomic

    def write_then_swap(path, payload, **kw):
        real_write(path, payload, **kw)
        target.unlink()
        os.rename(str(target.parent), str(target.parent) + ".real")
        os.symlink(str(outside), str(target.parent))

    def boom(*a, **k):
        raise AttachError("record is locked")
    monkeypatch.setattr(config, "write_json_atomic", write_then_swap)
    monkeypatch.setattr(baseline, "_record", boom)
    with pytest.raises(AttachError, match="record is locked"):
        baseline.ensure(str(attached.root), sha)
    assert (outside / target.name).read_text() == "keep\n"
