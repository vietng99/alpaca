import json, os, sys
from alpaca import cli, paths
from alpaca.tests.test_doctor import _full

def test_init_creates_runtime_and_is_idempotent(project):
    assert cli.main(["init"]) == 0
    assert os.path.isfile(paths.db_path(project))
    assert cli.main(["init"]) == 0

def test_verify_pass(project, capsys):
    cli.main(["init"])
    assert cli.main(["verify"]) == 0
    assert "GATE alpaca-verify: PASS" in capsys.readouterr().out

def test_status_json(project, capsys):
    cli.main(["init"])
    assert cli.main(["status", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["root"] == project and d["events"] == 1 and d["onboarded"] is False

def test_usage_error_is_64(project):
    assert cli.main(["no-such-verb"]) == 64

def test_outside_project_is_blocked(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path); monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert cli.main(["status"]) == 2
    assert "BLOCKED" in capsys.readouterr().out

def test_unexpected_error_is_internal_67(project, monkeypatch, capsys):
    cli.main(["init"])
    def boom(args): raise RuntimeError("disk on fire")
    monkeypatch.setitem(cli.COMMANDS, "status", boom)
    assert cli.main(["status"]) == 67
    err = capsys.readouterr().err
    assert "GATE alpaca-status: FAIL (internal: RuntimeError: disk on fire)" in err and "Traceback" not in err

def test_doctor_reports_missing_pyyaml(project, monkeypatch, capsys):
    _full(project)
    cli.main(["init"])
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert cli.main(["doctor"]) == 2
    assert "PyYAML missing" in capsys.readouterr().out
    assert cli.main(["status", "--json"]) == 0


# ------------------------------------------- op-006: a verb knows the session it was run from
def _sessions_of(project):
    from alpaca import db
    conn = db.connect(project)
    try:
        return [e["session"] for e in db.events(conn, limit=50)]
    finally:
        conn.close()


def test_a_verb_with_no_session_flag_records_under_the_claude_session_id(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cafe1234-aaaa-bbbb")
    assert cli.main(["init"]) == 0
    assert _sessions_of(project) == ["cafe1234-aaaa-bbbb"]


def test_the_alpaca_session_id_wins_over_the_claude_one(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cafe1234-aaaa-bbbb")
    monkeypatch.setenv("ALPACA_SESSION_ID", "dddd5678-cccc")
    assert cli.main(["init"]) == 0
    assert _sessions_of(project) == ["dddd5678-cccc"]


def test_an_explicit_session_flag_wins_over_both(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cafe1234-aaaa-bbbb")
    monkeypatch.setenv("ALPACA_SESSION_ID", "dddd5678-cccc")
    assert cli.main(["--session", "eeee9999", "init"]) == 0
    assert _sessions_of(project) == ["eeee9999"]


def test_with_no_session_anywhere_the_write_still_lands_under_cli(project, monkeypatch):
    """The negative path: outside a session there is nobody to attribute to, and the fallback
    the verbs already carry is what records the write."""
    for name in cli.ENV_SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["init"]) == 0
    assert _sessions_of(project) == ["cli"]


def test_an_empty_session_variable_is_not_a_session(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "   ")
    assert cli.main(["init"]) == 0
    assert _sessions_of(project) == ["cli"]
