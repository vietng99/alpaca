import json, os, subprocess, sys
from alpaca import db, paths
from pathlib import Path

def run_hook(module, payload, cwd, env_extra=None):
    env = dict(os.environ); env.update(env_extra or {})
    p = subprocess.run([sys.executable, "-m", module], input=json.dumps(payload), capture_output=True,
                       text=True, encoding="utf-8", cwd=cwd, env=env)
    return p.returncode, p.stdout, p.stderr

def test_session_start_inits_and_injects_pad(project):
    rc, out, err = run_hook("alpaca.hooks.session_start",
        {"session_id": "abcd1234-0000", "transcript_path": "/tmp/x.jsonl", "cwd": project, "source": "startup"},
        cwd=project, env_extra={"CLAUDE_PROJECT_DIR": project, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))})
    assert rc == 0, err
    d = json.loads(out)
    ctx = d["hookSpecificOutput"]["additionalContext"]
    assert d["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Alpaca BOOT" in ctx and "not onboarded" in ctx
    conn = db.connect(project)
    assert db.rows(conn, "sessions", "sid=?", ("abcd1234-0000",))[0]["transcript"] == "/tmp/x.jsonl"
    assert [e["kind"] for e in db.events(conn)][-1] == "session-start"

def test_session_start_reads_autodrive_level(project, tmp_path):
    cfg = Path(paths.config_dir(project)); (cfg / ".autodrive-level.abcd1234-0000").write_text("L4\n", encoding="utf-8")
    rc, out, _ = run_hook("alpaca.hooks.session_start", {"session_id": "abcd1234-0000", "transcript_path": "", "cwd": project},
        cwd=project, env_extra={"CLAUDE_PROJECT_DIR": project, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))})
    assert rc == 0 and "level L4" in json.loads(out)["hookSpecificOutput"]["additionalContext"]

def test_session_start_fails_open_on_garbage(project):
    p = subprocess.run([sys.executable, "-m", "alpaca.hooks.session_start"], input="not json", capture_output=True,
                       text=True, encoding="utf-8", cwd=project,
                       env={**os.environ, "CLAUDE_PROJECT_DIR": project, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))})
    assert p.returncode == 0 and p.stdout == ""

def test_resume_keeps_original_started(project):
    env = {"CLAUDE_PROJECT_DIR": project, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}
    run_hook("alpaca.hooks.session_start", {"session_id": "abcd1234-0000", "transcript_path": "/tmp/x.jsonl", "cwd": project, "source": "startup"}, cwd=project, env_extra=env)
    first = db.rows(db.connect(project), "sessions", "sid=?", ("abcd1234-0000",))[0]["started"]
    run_hook("alpaca.hooks.session_start", {"session_id": "abcd1234-0000", "transcript_path": "/tmp/x.jsonl", "cwd": project, "source": "resume"}, cwd=project, env_extra=env)
    row = db.rows(db.connect(project), "sessions", "sid=?", ("abcd1234-0000",))[0]
    assert row["started"] == first
    assert [e["kind"] for e in db.events(db.connect(project), kind="session-start")].count("session-start") == 2


def test_session_start_exports_the_session_id_to_later_bash_calls(project, tmp_path):
    """Flow receipts and gate runs take their origin from ALPACA_SESSION_ID. The hook arms it
    through Claude Code's env file, so no agent turn is spent passing --session."""
    env_file = tmp_path / "claude-env.sh"
    env = {"CLAUDE_PROJECT_DIR": project, "CLAUDE_ENV_FILE": str(env_file),
           "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}
    payload = {"session_id": "abcd1234-0000", "transcript_path": "", "cwd": project, "source": "startup"}
    for _ in range(2):      # startup, then a resume: one line, not two
        rc, _out, err = run_hook("alpaca.hooks.session_start", payload, cwd=project, env_extra=env)
        assert rc == 0, err
    assert env_file.read_text() == "export ALPACA_SESSION_ID=abcd1234-0000\n"
    got = subprocess.run(["bash", "-c", ". %s && printf %%s \"$ALPACA_SESSION_ID\"" % env_file],
                         capture_output=True, text=True)
    assert got.stdout == "abcd1234-0000"


def test_session_start_without_an_env_file_still_boots(project):
    env = {"CLAUDE_PROJECT_DIR": project, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}
    env_clean = {k: v for k, v in os.environ.items() if k != "CLAUDE_ENV_FILE"}
    p = subprocess.run([sys.executable, "-m", "alpaca.hooks.session_start"],
                       input=json.dumps({"session_id": "s-no-env", "cwd": project}), capture_output=True,
                       text=True, encoding="utf-8", cwd=project, env={**env_clean, **env})
    assert p.returncode == 0 and "Alpaca BOOT" in p.stdout
