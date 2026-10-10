"""Watchdog gap tests (op-021 t-213 contract lens MINOR 1-5): live-record stop, 24 h session rule, CLI layer,
rollback event, postcheck drift on a lost undo ref. Throwaway repositories only."""
import json
import os
import subprocess
import sys
import time

import pytest

from alpaca import db
from alpaca.tests.conftest import REPO
from alpaca.watchdog import postcheck, preflight, runs, undo


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], check=check, capture_output=True, text=True).stdout


@pytest.fixture
def repo(project, tmp_path):
    git(project, "init", "-q", "-b", "main")
    for k, v in [("user.name", "T"), ("user.email", "t@localhost"), ("commit.gpgsign", "false")]:
        git(project, "config", k, v)
    (tmp_path / "proj" / ".gitignore").write_text(".alpaca/\n", encoding="utf-8")
    (tmp_path / "proj" / "f.txt").write_text("base\n", encoding="utf-8")
    git(project, "add", ".gitignore", "ALPACA-MANIFEST", "f.txt")
    git(project, "commit", "-qm", "init")
    return project


def _wt(repo, tmp_path, name):
    path = str(tmp_path / name)
    git(repo, "worktree", "add", "-q", "-b", name, path)
    return path


def _pf(repo, *cmd, sessions=(), **kw):
    return preflight.run(repo, ["git", "-C", repo, *cmd], sessions=list(sessions), claims={}, **kw)


# 1. live record ---------------------------------------------------------------------------------

def test_stop_when_the_branch_would_bring_in_a_changed_live_record(project, tmp_path):
    # same setup as test_git_guard; the project has no .gitignore so the db can be tracked
    git(project, "init", "-q", "-b", "main")
    for k, v in [("user.name", "T"), ("user.email", "t@localhost"), ("commit.gpgsign", "false")]:
        git(project, "config", k, v)
    git(project, "add", "ALPACA-MANIFEST")
    git(project, "commit", "-qm", "init")
    dbp = os.path.join(project, ".alpaca", "alpaca.db")
    open(dbp, "w").write("one")
    git(project, "add", "-f", ".alpaca/alpaca.db")
    git(project, "commit", "-qm", "tracked record")
    git(project, "branch", "old")
    git(project, "checkout", "-q", "old")
    open(dbp, "w").write("two")
    git(project, "commit", "-qam", "branch changed record")
    git(project, "checkout", "-q", "main")
    git(project, "rm", "-q", "--cached", ".alpaca/alpaca.db")
    git(project, "commit", "-qm", "untrack record")
    os.remove(dbp)  # the stand-in bytes are not a database; the preflight makes a real, untracked one
    for cmd in (("merge", "old"), ("checkout", "old")):
        res = preflight.run(project, ["git", "-C", project, *cmd], sessions=[], claims={})
        assert res["verdict"] == "stop", (cmd, res["reasons"])
        assert any("writes_live_record" in r["pointer"] for r in res["reasons"]), res["reasons"]


# 2. 24 h rule -----------------------------------------------------------------------------------

def test_risky_when_a_session_touched_the_target_within_24h(repo, tmp_path):
    wt = _wt(repo, tmp_path, "idle")
    now = time.time()
    recent = {"sid": "s-recent-1234567", "cwd": wt, "last_beat": now - 5 * 3600, "ended": None}
    res = _pf(repo, "worktree", "remove", wt, sessions=[recent])
    assert res["verdict"] == "risky", res["reasons"]
    assert any("within 24 h" in r["text"] and r["level"] == "risky" for r in res["reasons"])
    old = dict(recent, last_beat=now - 30 * 3600)
    assert _pf(repo, "worktree", "remove", wt, sessions=[old])["verdict"] == "safe"
    elsewhere = dict(recent, cwd=str(tmp_path))
    assert _pf(repo, "worktree", "remove", wt, sessions=[elsewhere])["verdict"] == "safe"


# 3. CLI layer -----------------------------------------------------------------------------------

def _cli(cwd, *args):
    env = dict(os.environ, PYTHONPATH=REPO)
    env.pop("ALPACA_ROOT", None)
    env.pop("CLAUDE_PROJECT_DIR", None)
    return subprocess.run([sys.executable, "-m", "alpaca", *args], cwd=cwd, env=env, capture_output=True, text=True)


def _gate_wd(repo, tmp_path):
    wt = _wt(repo, tmp_path, "cliwt")
    out = _cli(repo, "watchdog", "preflight", "--json", "--", "git", "worktree", "remove", wt)
    assert out.returncode == 0, out.stdout + out.stderr
    return wt, json.loads(out.stdout)


def test_cli_preflight_exit_codes(repo, tmp_path):
    assert _cli(repo, "watchdog", "preflight", "--", "git", "push").returncode == 2
    assert _cli(repo, "watchdog", "preflight", "--", "git", "gc").returncode == 1
    wt = _wt(repo, tmp_path, "safe1")
    out = _cli(repo, "watchdog", "preflight", "--", "git", "worktree", "remove", wt)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "preflight wd-" in out.stdout and ": safe" in out.stdout


def test_cli_preflight_json_keys(repo, tmp_path):
    _, doc = _gate_wd(repo, tmp_path)
    assert {"id", "verdict", "report"} <= set(doc)
    assert doc["verdict"] == "safe" and runs.ID.match(doc["id"]) and os.path.isfile(doc["report"])


def test_cli_without_a_sub_verb_exits_64(repo):
    assert _cli(repo, "watchdog").returncode == 64


def test_cli_preflight_without_a_command_exits_64(repo):
    out = _cli(repo, "watchdog", "preflight")
    assert out.returncode == 64 and "GATE alpaca-watchdog-preflight: FAIL" in out.stdout


def test_cli_undo_rollback_postcheck_on_a_real_run(repo, tmp_path):
    wt, doc = _gate_wd(repo, tmp_path)
    rid = doc["id"]
    out = _cli(repo, "watchdog", "undo", rid)
    assert out.returncode == 0, out.stdout + out.stderr
    git(repo, "worktree", "remove", wt)
    git(repo, "branch", "-D", "cliwt")
    dry = _cli(repo, "watchdog", "rollback", "--dry-run", rid)
    assert dry.returncode == 0 and "recreate-branch" in dry.stdout and not os.path.exists(wt), dry.stdout + dry.stderr
    real = _cli(repo, "watchdog", "rollback", rid)
    assert real.returncode == 0 and os.path.isdir(wt), real.stdout + real.stderr
    # the preflight said the worktree would go; it is back, so postcheck reports drift (exit 1)
    pc = _cli(repo, "watchdog", "postcheck", rid)
    assert pc.returncode == 1 and "drift" in pc.stdout, pc.stdout + pc.stderr
    git(repo, "worktree", "remove", wt)
    ok = _cli(repo, "watchdog", "postcheck", "--json", rid)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert json.loads(ok.stdout)["result"] == "ok"


@pytest.mark.parametrize("verb", [["undo"], ["rollback", "--dry-run"], ["postcheck"]])
def test_cli_unknown_run_id_is_exit_1_with_a_gate_line(repo, verb):
    out = _cli(repo, "watchdog", *verb, "wd-20200101-000000-abcdef")
    assert out.returncode == 1 and "GATE alpaca-watchdog-%s: FAIL" % verb[0] in out.stdout, out.stdout + out.stderr
    bad = _cli(repo, "watchdog", *verb, "not-an-id")
    assert bad.returncode == 1 and "GATE alpaca-watchdog-" in bad.stdout


# 4. rollback event ------------------------------------------------------------------------------

def _events(root, kind):
    conn = db.connect_readonly(root)
    try:
        return [json.loads(r[0]) if isinstance(r[0], str) else r[0]
                for r in conn.execute("SELECT data FROM events WHERE kind = ?", (kind,)).fetchall()]
    finally:
        conn.close()


def test_rollback_appends_the_record_event_and_dry_run_does_not(repo, tmp_path):
    wt = _wt(repo, tmp_path, "evt")
    res = _pf(repo, "worktree", "remove", "--force", wt)
    undo.create(repo, res["id"])
    git(repo, "worktree", "remove", "--force", wt)
    git(repo, "branch", "-D", "evt")
    undo.rollback(repo, res["id"], dry_run=True)
    assert _events(repo, "watchdog-rollback") == []
    undo.rollback(repo, res["id"])
    got = _events(repo, "watchdog-rollback")
    assert len(got) == 1 and got[0]["id"] == res["id"] and got[0]["ok"] is True
    ok, reason = db.verify_chain(db.connect_readonly(repo))
    assert ok, reason


# 5. postcheck on a lost undo ref ------------------------------------------------------------------

def test_postcheck_reports_drift_when_an_undo_ref_was_deleted(repo, tmp_path):
    wt = _wt(repo, tmp_path, "lost")
    res = _pf(repo, "worktree", "remove", wt)
    made = undo.create(repo, res["id"])
    git(repo, "worktree", "remove", wt)
    assert postcheck.run(repo, res["id"])["result"] == "ok"
    victim = made["refs"][0]["ref"]
    git(repo, "update-ref", "-d", victim)
    out = postcheck.run(repo, res["id"])
    assert out["result"] == "drift"
    assert any(victim in m and "no longer resolves" in m for m in out["mismatches"]), out["mismatches"]
