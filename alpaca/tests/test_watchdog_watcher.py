"""The watchdog watcher (design/watchdog/design.md sections 3 and 10): one line per new event, none when idle."""
import io
import json
import os
import sqlite3
import subprocess

import pytest

from alpaca import db, messages, paths
from alpaca.watchdog import watcher


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True)


@pytest.fixture
def repo(project, monkeypatch):
    """A throwaway project that is a git repo with a clean main, a record, and no transcripts."""
    _git(project, "init", "-q", "-b", "main")
    for key, value in [("user.name", "T"), ("user.email", "t@localhost"), ("commit.gpgsign", "false")]:
        _git(project, "config", key, value)
    with open(os.path.join(project, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write(".alpaca/\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    db.connect(project).close()
    monkeypatch.setattr(watcher, "_dirty_sessions", lambda root, files: [])
    monkeypatch.setattr(watcher, "_sessions", lambda root, now: [])
    monkeypatch.setattr(watcher, "_touched", lambda root, trees, now: {})
    return project


def _kinds(events):
    return [e["kind"] for e in events]


def _baseline(repo):
    events, state = watcher.tick(repo)
    assert _kinds(events) == ["baseline"]
    return state


def _linked(repo, tmp_path, name="wt", detach=False):
    path = str(tmp_path / name)
    args = ["worktree", "add", "-q", path] + (["--detach"] if detach else ["-b", name])
    _git(repo, *args)
    return path


def _post(repo, body="please check"):
    conn = db.connect(repo)
    try:
        return messages.post(conn, "worker", "watchdog", "question", body)["id"]
    finally:
        conn.close()


def _append(repo):
    conn = db.connect(repo)
    try:
        db.append_event(conn, session="s", actor="a", kind="beat", ref="r", data={"i": 1})
    finally:
        conn.close()


def _raw(repo, sql, *params):
    """Edit the throwaway record out of band, as the corruption tests do (drops the update guard)."""
    conn = sqlite3.connect(paths.db_path(repo))
    try:
        conn.execute("DROP TRIGGER IF EXISTS events_no_update")
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_baseline_line_once_then_quiet(repo):
    events, state = watcher.tick(repo)
    assert _kinds(events) == ["baseline"]
    line = watcher.format_line(events[0])
    assert line.startswith("WATCHDOG baseline start :: ")
    assert watcher.tick(repo, state)[0] == []


def test_baseline_skips_old_requests_and_worktrees(repo, tmp_path):
    _post(repo)
    _linked(repo, tmp_path, detach=True)
    events, state = watcher.tick(repo)
    assert _kinds(events) == ["baseline"]
    assert watcher.tick(repo, state)[0] == []


def test_request_fires_once(repo):
    state = _baseline(repo)
    mid = _post(repo, "look at the worktrees")
    events, state = watcher.tick(repo, state)
    assert _kinds(events) == ["request"] and events[0]["key"] == str(mid)
    assert "look at the worktrees" in events[0]["summary"]
    assert watcher.tick(repo, state)[0] == []


def test_request_ignores_other_recipients(repo):
    state = _baseline(repo)
    conn = db.connect(repo)
    messages.post(conn, "worker", "someone-else", "question", "not for the watchdog")
    conn.close()
    assert watcher.tick(repo, state)[0] == []


def test_worktree_new(repo, tmp_path, monkeypatch):
    state = _baseline(repo)
    path = _linked(repo, tmp_path)
    events, state = watcher.tick(repo, state)
    assert [e for e in events if e["kind"] == "worktree-new"][0]["key"] == path
    assert [e["kind"] for e in watcher.tick(repo, state)[0]] == []


def test_worktree_orphan_detached_and_dedupe(repo, tmp_path):
    state = _baseline(repo)
    path = _linked(repo, tmp_path, detach=True)
    events, state = watcher.tick(repo, state)
    assert _kinds(events) == ["worktree-new", "worktree-orphan"]
    assert events[1]["key"].startswith(path + "@")
    assert watcher.tick(repo, state)[0] == []


def test_worktree_orphan_merged_branch(repo, tmp_path):
    state = _baseline(repo)
    _linked(repo, tmp_path)     # a new branch with no commit of its own: merged into main
    events, _ = watcher.tick(repo, state)
    assert "worktree-orphan" in _kinds(events)


def test_worktree_with_live_session_is_not_an_orphan(repo, tmp_path, monkeypatch):
    state = _baseline(repo)
    path = _linked(repo, tmp_path, detach=True)
    live = [{"sid": "s1", "cwd": path, "last_beat": 1000.0, "ended": None, "touched": {}}]
    monkeypatch.setattr(watcher, "_sessions", lambda root, now: live)
    events, _ = watcher.tick(repo, state, now=1100.0)
    assert _kinds(events) == ["worktree-new"]


def test_worktree_touched_within_a_day_is_not_an_orphan(repo, tmp_path, monkeypatch):
    state = _baseline(repo)
    path = _linked(repo, tmp_path, detach=True)
    real = os.path.realpath(path)
    monkeypatch.setattr(watcher, "_sessions", lambda root, now: [
        {"sid": "s1", "cwd": "/elsewhere", "last_beat": 1.0, "ended": None, "touched": {}}])
    monkeypatch.setattr(watcher, "_touched", lambda root, trees, now: {"s1": {real: now - 60}})
    events, _ = watcher.tick(repo, state, now=10_000.0)
    assert "worktree-orphan" not in _kinds(events)


def test_orphan_fires_again_after_it_clears(repo, tmp_path):
    state = _baseline(repo)
    path = _linked(repo, tmp_path, detach=True)
    events, state = watcher.tick(repo, state)
    assert "worktree-orphan" in _kinds(events)
    _git(repo, "worktree", "remove", "--force", path)
    events, state = watcher.tick(repo, state)
    assert events == [] and state["fired"]["worktree-orphan"] == []
    _linked(repo, tmp_path, detach=True)
    events, _ = watcher.tick(repo, state)
    assert _kinds(events) == ["worktree-new", "worktree-orphan"]


def test_shared_dirty_fires_on_change_only(repo, monkeypatch):
    state = _baseline(repo)
    monkeypatch.setattr(watcher, "_dirty_sessions", lambda root, files: ["s-a", "s-b"])
    with open(os.path.join(repo, "x.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")
    events, state = watcher.tick(repo, state)
    assert _kinds(events) == ["shared-dirty"] and events[0]["key"] == "s-a,s-b"
    assert watcher.tick(repo, state)[0] == []
    with open(os.path.join(repo, "y.txt"), "w", encoding="utf-8") as fh:
        fh.write("y")
    assert watcher.tick(repo, state)[0] == []          # same session set: not new
    monkeypatch.setattr(watcher, "_dirty_sessions", lambda root, files: ["s-a"])
    with open(os.path.join(repo, "z.txt"), "w", encoding="utf-8") as fh:
        fh.write("z")
    events, state = watcher.tick(repo, state)           # one session: clears
    assert events == []
    monkeypatch.setattr(watcher, "_dirty_sessions", lambda root, files: ["s-a", "s-b"])
    with open(os.path.join(repo, "w.txt"), "w", encoding="utf-8") as fh:
        fh.write("w")
    assert _kinds(watcher.tick(repo, state)[0]) == ["shared-dirty"]   # came back: fires again


def test_shared_dirty_needs_two_sessions(repo, monkeypatch):
    state = _baseline(repo)
    monkeypatch.setattr(watcher, "_dirty_sessions", lambda root, files: ["s-a"])
    with open(os.path.join(repo, "x.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")
    assert watcher.tick(repo, state)[0] == []


def test_verify_fail_then_ok(repo):
    _append(repo)
    _append(repo)
    state = _baseline(repo)
    assert state["verify"]["ok"] is True
    original = sqlite3.connect(paths.db_path(repo)).execute("SELECT data FROM events WHERE id=1").fetchone()[0]
    _raw(repo, "UPDATE events SET data='{\"i\": 99}' WHERE id=1")
    _append(repo)                                        # the event count moved: verify runs
    events, state = watcher.tick(repo, state)
    assert _kinds(events) == ["verify-fail"] and "event 1" in events[0]["key"]
    _append(repo)
    assert watcher.tick(repo, state)[0] == []            # still the same failure: no second line
    _raw(repo, "UPDATE events SET data=? WHERE id=1", original)
    _append(repo)
    events, state = watcher.tick(repo, state)
    assert _kinds(events) == ["verify-ok"]
    assert watcher.tick(repo, state)[0] == []


def test_verify_skipped_when_event_count_is_unchanged(repo, monkeypatch):
    state = _baseline(repo)
    monkeypatch.setattr(db, "verify_chain", lambda conn: pytest.fail("verify ran with no new event"))
    assert watcher.tick(repo, state)[0] == []


def test_error_prints_once_per_message(repo, monkeypatch):
    state = _baseline(repo)
    def boom(root):
        raise watcher.gitview.GitError("boom")
    monkeypatch.setattr(watcher.gitview, "worktrees", boom)
    events, state = watcher.tick(repo, state)
    assert _kinds(events) == ["error"]
    assert watcher.format_line(events[0]) == "WATCHDOG error git worktree list :: boom"
    assert watcher.tick(repo, state)[0] == []


def test_format_line_json(repo):
    event = {"kind": "request", "key": "7", "summary": "x", "data": {"id": 7}}
    assert json.loads(watcher.format_line(event, as_json=True)) == event
    assert watcher.format_line(event) == "WATCHDOG request 7 :: x"


def test_cursor_saved_atomically_and_only_when_changed(repo):
    out = io.StringIO()
    assert watcher.run(repo, once=True, out=out) == 0
    assert out.getvalue().startswith("WATCHDOG baseline")
    cursor = watcher._cursor_path(repo)
    assert os.path.isfile(cursor) and not os.path.exists(cursor + ".tmp")
    before = os.stat(cursor).st_mtime_ns
    out = io.StringIO()
    assert watcher.run(repo, once=True, out=out) == 0
    assert out.getvalue() == "" and os.stat(cursor).st_mtime_ns == before


def test_run_once_prints_a_request_line_and_flushes(repo):
    watcher.run(repo, once=True, out=io.StringIO())
    _post(repo, "hello")
    out = io.StringIO()
    watcher.run(repo, once=True, out=out)
    assert out.getvalue().startswith("WATCHDOG request ") and out.getvalue().endswith("\n")


def test_run_loops_until_interrupted(repo):
    calls = []
    def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            raise KeyboardInterrupt
    out = io.StringIO()
    assert watcher.run(repo, interval=5, out=out, sleep=sleep) == 0
    assert calls == [5, 5] and out.getvalue().count("\n") == 1


def test_broadcast_message_is_not_a_request(repo):
    state = _baseline(repo)
    conn = db.connect(repo)
    try:
        for to in ("all", "everyone", "watchdog-x", "*"):
            messages.post(conn, "worker", to, "note", "broadcast")
    finally:
        conn.close()
    assert "request" not in _kinds(watcher.tick(repo, state)[0])


def test_scratch_and_restore_worktrees_give_no_worktree_events(repo, tmp_path):
    state = _baseline(repo)
    for sub in ("scratch", "restore"):
        d = os.path.join(repo, ".alpaca", "watchdog", sub)
        os.makedirs(d, exist_ok=True)
        _git(repo, "worktree", "add", "-q", "--detach", os.path.join(d, "x1"))
    events, state = watcher.tick(repo, state)
    assert events == []
    assert watcher.tick(repo, state)[0] == []


def test_watch_writes_no_ref_and_no_index(repo, tmp_path):
    wt = _linked(repo, tmp_path, detach=True)
    with open(os.path.join(wt, "n.txt"), "w") as fh:
        fh.write("x\n")

    def snap():
        refs = subprocess.run(["git", "-C", repo, "for-each-ref"], capture_output=True, text=True).stdout
        idx = []
        for p in (os.path.join(repo, ".git", "index"), os.path.join(repo, ".git", "worktrees", "wt", "index")):
            idx.append(open(p, "rb").read() if os.path.exists(p) else None)
        return refs, idx

    before = snap()
    state = _baseline(repo)
    watcher.tick(repo, state)
    assert snap() == before
