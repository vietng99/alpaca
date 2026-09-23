"""The portable lifecycle must isolate state and report real outcomes."""
import json
from pathlib import Path

import pytest

from alpaca import cli, db, paths
from alpaca.analytics import build_index


def invoke(monkeypatch, *args):
    if "operator" not in cli.VERB_MODULES:
        monkeypatch.setattr(cli, "VERB_MODULES", cli.VERB_MODULES + ("operator",))
    return cli.main(list(args))


def test_explicit_root_and_cwd_ignore_foreign_claude_project(project, tmp_path, monkeypatch):
    other = tmp_path / "foreign"
    other.mkdir()
    (other / paths.MANIFEST).write_text("foreign\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
    assert paths.root(project) == project
    assert paths.root() == project
    assert not (other / ".alpaca").exists()


def test_local_root_wins_over_ambient_alpaca_root(project, tmp_path, monkeypatch):
    foreign = tmp_path / "foreign-alpaca"
    foreign.mkdir()
    (foreign / paths.MANIFEST).write_text("foreign\n")
    monkeypatch.setenv("ALPACA_ROOT", str(foreign))
    assert paths.root() == project
    assert paths.root(project) == project
    monkeypatch.chdir(tmp_path)
    assert paths.root() == str(foreign)


def test_operator_state_and_transcripts_are_project_local(project, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "foreign-config"))
    assert paths.config_dir() == str(Path(project) / ".alpaca" / "config")
    assert paths.transcript_dir(project) == str(Path(project) / ".alpaca" / "transcripts")


def test_explicit_start_does_not_launch_dashboard(project, monkeypatch, capsys):
    from alpaca import serve
    launches = []
    monkeypatch.setattr(serve, "ensure_running", lambda root: launches.append(root))
    assert invoke(monkeypatch, "--session", "codex-a", "session", "start", "--operator", "codex") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["session"] == "codex-a" and "Alpaca BOOT" in result["context"]
    assert launches == []


def test_lifecycle_records_checkpoint_and_end_without_transcript(project, monkeypatch, capsys):
    assert invoke(monkeypatch, "--session", "codex-a", "session", "start") == 0
    assert invoke(monkeypatch, "--session", "codex-a", "session", "checkpoint", "--note", "Next: validate output") == 0
    assert invoke(monkeypatch, "--session", "codex-a", "session", "end", "--reason", "complete") == 0
    conn = db.connect(project)
    try:
        kinds = [e["kind"] for e in db.events(conn, session="codex-a")]
        assert "session-checkpoint" in kinds and "session-end" in kinds
        assert db.rows(conn, "sessions", "sid=?", ("codex-a",))[0]["ended"]
        assert db.verify_chain(conn)[0]
    finally:
        conn.close()
    assert (Path(project) / ".alpaca" / "wiki" / "raw").is_dir()
    assert (Path(project) / "analytics" / "index.html").is_file()


def test_session_identity_and_levels_do_not_cross_operators(project, monkeypatch):
    from alpaca.posture import level
    assert invoke(monkeypatch, "--session", "codex-a", "session", "start", "--level", "L4") == 0
    assert invoke(monkeypatch, "--session", "claude-a", "session", "start", "--operator", "claude") == 0
    assert invoke(monkeypatch, "--session", "codex-a", "session", "start", "--operator", "claude") == 2
    conn = db.connect(project)
    try:
        assert level.in_force(conn, "codex-a", root=project) == 4
        assert level.in_force(conn, "claude-a", root=project) == 2
    finally:
        conn.close()


def test_missing_and_unsafe_session_ids_are_refused(project, monkeypatch):
    assert invoke(monkeypatch, "session", "checkpoint") == 2
    assert invoke(monkeypatch, "--session", "../escape", "session", "start") == 2
    assert not Path(paths.db_path(project)).exists()


def test_explicit_checkpoint_failure_is_nonzero(project, monkeypatch):
    from alpaca.wiki.ingest import drain
    assert invoke(monkeypatch, "--session", "codex-a", "session", "start") == 0
    def broken(*args, **kwargs):
        raise OSError("capture failed")
    monkeypatch.setattr(drain, "run", broken)
    assert invoke(monkeypatch, "--session", "codex-a", "session", "checkpoint") == 67


def test_analytics_include_record_only_session_with_unknown_usage(project):
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": "codex-a", "started": "2026-09-17T00:00:00+00:00"})
    db.append_event(conn, session="codex-a", actor="codex", kind="session-start", data={})
    conn.close()
    sessions = build_index.collect(project)
    assert len(sessions) == 1 and sessions[0]["sid"] == "codex-a"
    assert sessions[0]["usage_available"] is False
    assert sessions[0]["tokens"]["output"] is None
    assert sessions[0]["cost_notional_usd"] is None
    assert build_index._fold(project, sessions)["project"]["sessions"] == 1


def test_analytics_ignore_cwd_history_and_ambient_transcripts(project, monkeypatch, tmp_path):
    from alpaca import project as config
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    td = Path(paths.transcript_dir(str(foreign)))
    td.mkdir(parents=True)
    (td / "old-session.jsonl").write_text('{}\n')
    config.save(project, {"cwd_history": [str(foreign)]})
    conn = db.connect(project)
    try:
        assert build_index._transcripts(project, conn) == {}
    finally:
        conn.close()


def test_generated_session_can_resume_with_recorded_level(project, monkeypatch, capsys):
    assert invoke(monkeypatch, "session", "start", "--level", "L4") == 0
    first = json.loads(capsys.readouterr().out)
    assert first["session"].startswith("codex-")
    assert "| level L4 |" in first["context"]
    assert invoke(monkeypatch, "--session", first["session"], "session", "start") == 0
    resumed = json.loads(capsys.readouterr().out)
    assert "| level L4 |" in resumed["context"]


def test_explicit_stop_reports_lint_block_and_internal_failure(project, monkeypatch, tmp_path, capsys):
    from alpaca.posture import loops
    assert invoke(monkeypatch, "--session", "codex-a", "session", "start") == 0
    capsys.readouterr()
    (Path(project) / "style" / "banned.txt").write_text("forbiddenphrase\n")
    reply = tmp_path / "reply.txt"
    reply.write_text("This contains forbiddenphrase.")
    assert invoke(monkeypatch, "--session", "codex-a", "session", "stop", "--reply-file", str(reply)) == 2
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
    def broken(*args, **kwargs):
        raise OSError("cannot check stop rules")
    monkeypatch.setattr(loops, "stop_should_refuse", broken)
    assert invoke(monkeypatch, "--session", "codex-a", "session", "stop") == 67


def test_checkpoint_note_is_preserved_in_wiki_capture(project, monkeypatch):
    assert invoke(monkeypatch, "--session", "codex-a", "session", "start") == 0
    assert invoke(monkeypatch, "--session", "codex-a", "session", "checkpoint", "--note", "Continue with the green synthesis report") == 0
    notes = (Path(project) / ".alpaca" / "wiki" / "raw").rglob("*.md")
    assert any("Continue with the green synthesis report" in p.read_text() for p in notes)
