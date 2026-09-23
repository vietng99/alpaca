import json, os, shutil, subprocess, sys
from alpaca import cli, db, paths

ENV = lambda project: {**os.environ, "CLAUDE_PROJECT_DIR": project, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}
def hook(module, payload, project):
    return subprocess.run([sys.executable, "-m", module], input=json.dumps(payload), capture_output=True,
                          text=True, encoding="utf-8", cwd=project, env=ENV(project))

def test_stop_records_turn_end_and_renders_pad(project):
    cli.main(["init"])
    p = hook("alpaca.hooks.stop", {"session_id": "s1", "stop_hook_active": False}, project)
    assert p.returncode == 0 and p.stdout == ""
    assert db.events(db.connect(project), kind="turn-end")
    assert os.path.isfile(os.path.join(project, "RESUME.md"))

def test_session_end_builds_analytics(project):
    cli.main(["init"])
    p = hook("alpaca.hooks.session_end", {"session_id": "s1", "reason": "exit"}, project)
    assert p.returncode == 0, p.stderr
    conn = db.connect(project)
    assert db.events(conn, kind="session-end")[-1]["data"] == {"reason": "exit"}
    assert db.rows(conn, "sessions", "sid='s1'")[0]["ended"]
    assert os.path.isfile(os.path.join(project, "analytics", "index.html"))

def test_stop_snapshots_last_so_a_cut_off_copy_cannot_cost_the_turn(project, monkeypatch):
    """The transcript copy is the one step in Stop whose cost follows a file the hook does not own,
    and the hard hook deadline is a BaseException that no `except Exception` catches. Placed first,
    a copy cut off half way through would take the stop decision, the style lint, the data push and
    the analytics counter down with it. It goes last, and whatever it raises stays there."""
    from alpaca import export, transcripts
    from alpaca.hooks import stop
    cli.main(["init"])
    with open(os.path.join(project, "style", "banned.txt"), "w", encoding="utf-8") as fh:
        fh.write("delve\n")

    class Cutoff(BaseException):
        """Stands in for the hard hook deadline, which is a BaseException too."""

    order = []
    real_export = export.write_if_changed

    def note_export(root):
        order.append("export")
        return real_export(root)

    def cut_off(*a, **kw):
        order.append("snapshot")
        raise Cutoff()

    monkeypatch.setattr(export, "write_if_changed", note_export)
    monkeypatch.setattr(transcripts, "snapshot", cut_off)
    result = stop.handle({"session_id": "s1", "cwd": project, "reply": "We should delve in."})
    # the turn's own answer still reaches the caller, and everything before the copy ran.
    assert result.get("decision") == "block"
    assert order == ["export", "snapshot"]
    conn = db.connect(project)
    assert db.events(conn, kind="turn-end")
    assert db.meta_get(conn, "stops:s1") == "1"
    conn.close()
    assert os.path.isfile(os.path.join(project, "RESUME.md"))
    # negative: with the copy working, the same call still returns the same decision.
    order.clear()
    monkeypatch.setattr(transcripts, "snapshot", lambda *a, **kw: order.append("snapshot") or {})
    again = stop.handle({"session_id": "s1", "cwd": project, "reply": "We should delve in."})
    assert again.get("decision") == "block" and order == ["export", "snapshot"]


def test_stop_refreshes_and_rebuilds_the_page_on_tenth_stop(project):
    cli.main(["init"])
    conn = db.connect(project)
    tdir = paths.transcript_dir(project)
    os.makedirs(tdir, exist_ok=True)
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "transcript_small.jsonl")
    for sid in ("s1", "s2"):
        tpath = os.path.join(tdir, sid + ".jsonl")
        shutil.copy(fixture, tpath)
        db.upsert(conn, "sessions", "sid", {"sid": sid, "transcript": tpath, "started": "2026-09-15T00:00:00+00:00"})
    for i in range(9):
        p = hook("alpaca.hooks.stop", {"session_id": "s1"}, project)
        assert p.returncode == 0, p.stderr
    assert not os.path.isfile(os.path.join(project, ".alpaca", "analytics", "sessions", "s1.json"))
    assert not os.path.isfile(os.path.join(project, "analytics", "index.html"))
    p = hook("alpaca.hooks.stop", {"session_id": "s1"}, project)
    assert p.returncode == 0, p.stderr
    # Read folds no longer write a second JSON cache; explicit build publishes the page.
    assert not os.path.isfile(os.path.join(project, ".alpaca", "analytics", "sessions", "s1.json"))
    # the tenth stop also builds the merged page, so a long session shows before it ends
    idx = os.path.join(project, "analytics", "index.html")
    assert os.path.isfile(idx)
    with open(idx, encoding="utf-8") as fh:
        html = fh.read()
    assert "s1" in html and "s2" in html
