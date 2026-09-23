"""M0 done-when: copy, rename, onboard, doctor, crash, resume, analytics."""
import json, os, shutil, subprocess, sys
from alpaca import manifest
from alpaca.tests import proofkit
from alpaca.tests.conftest import REPO

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

def sh(args, cwd, env, stdin=None):
    return subprocess.run(args, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, encoding="utf-8")

def ok(args, cwd, env, stdin=None):
    r = sh(args, cwd, env, stdin)
    assert r.returncode == 0, r.stdout + r.stderr
    return r

def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()

_MEMORY_IGNORE = manifest.memory_ignore(REPO)


def _ignore(dirpath, names):
    skip = {".git", ".alpaca", "__pycache__", ".superpowers", "RESUME.md", "project.yaml", ".venv", ".pytest_cache", "dist"}
    if os.path.abspath(dirpath) == os.path.abspath(REPO):
        skip.add("analytics")
    # every [memory] path stays behind too, nested ones included (host-local tool installs and
    # build outputs under a mechanism dir): the copy stands for a fresh clone, which has none.
    memory = set(_MEMORY_IGNORE(dirpath, names))
    return [n for n in names if n in skip or n in memory]

def test_m0_end_to_end(tmp_path):
    # 1. copy the harness and rename it to the project name
    proj = tmp_path / "hello-cli"
    shutil.copytree(REPO, proj, ignore=_ignore)
    for f in ("hello.py", "Makefile"):
        shutil.copy(os.path.join(FIX, "sample_project", f), proj / f)
    cfg = tmp_path / "cfg"; cfg.mkdir()
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proj), "CLAUDE_CONFIG_DIR": str(cfg)}
    tdir = proj / ".alpaca" / "transcripts"; tdir.mkdir(parents=True)
    alpaca = [sys.executable, "-m", "alpaca"]
    # 2. first session: SessionStart hook -> not onboarded prompt
    s1 = {"session_id": "11111111-aaaa", "transcript_path": str(tdir / "11111111-aaaa.jsonl"), "cwd": str(proj), "source": "startup"}
    out = sh([sys.executable, "-m", "alpaca.hooks.session_start"], proj, env, json.dumps(s1)).stdout
    assert "FIRST CHAT" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    # 3. onboarding verb (the chat would run this after asking)
    r = sh(alpaca + ["--session", s1["session_id"], "onboard", "--name", "hello-cli", "--who", "alex:owner,robin:engineer",
                 "--what", "a hello CLI", "--task", "add --shout flag", "--task", "write README", "--preset", "plain-writing"], proj, env)
    assert r.returncode == 0, r.stdout + r.stderr
    r = sh(alpaca + ["doctor"], proj, env)
    # No collector or transcript bytes exist yet; coverage must be a warning.
    assert r.returncode == 1 and "WARN  observability" in r.stdout, r.stdout + r.stderr
    assert "FAIL" not in r.stdout
    assert "make test" in _read(proj / "project.yaml")
    # 4. work: open an op, add tasks, claim one, heartbeat, then the session dies (no session_end)
    ok(alpaca + ["op", "new", "add --shout flag", "--done-when", "hello --shout prints upper case"], proj, env)
    ok(alpaca + ["task", "add", "--title", "task", "op-001", "implement --shout", "--phase", "build"], proj, env)
    ok(alpaca + ["task", "add", "--title", "task", "op-001", "test --shout", "--phase", "verify"], proj, env)
    ok(alpaca + ["task", "claim", "t-001", "--by", "agent", "--minutes", "1"], proj, env)
    shutil.copy(os.path.join(FIX, "transcript_small.jsonl"), s1["transcript_path"])
    for i in range(3):
        sh([sys.executable, "-m", "alpaca.hooks.post_tool"], proj, env, json.dumps({"session_id": s1["session_id"], "tool_name": "Edit", "tool_input": {"file_path": "hello.py"}}))
    sh([sys.executable, "-m", "alpaca.hooks.stop"], proj, env, json.dumps({"session_id": s1["session_id"]}))
    # 5. next session resumes from disk: the pad names the first non-done task
    s2 = {"session_id": "22222222-bbbb", "transcript_path": str(tdir / "22222222-bbbb.jsonl"), "cwd": str(proj), "source": "startup"}
    ctx = json.loads(sh([sys.executable, "-m", "alpaca.hooks.session_start"], proj, env, json.dumps(s2)).stdout)["hookSpecificOutput"]["additionalContext"]
    assert "hello-cli" in ctx and "op-001" in ctx and "t-001" in ctx and "FIRST CHAT" not in ctx
    # E4: done needs a SEALED proof report; proofkit writes and seals one for t-001.
    proof_ptr = proofkit.seal_for(str(proj), "t-001", session=s2["session_id"])
    ok(alpaca + ["--session", s2["session_id"], "task", "move", "t-001", "done", "--proof", proof_ptr], proj, env)
    shutil.copy(os.path.join(FIX, "transcript_small.jsonl"), s2["transcript_path"])
    sh([sys.executable, "-m", "alpaca.hooks.session_end"], proj, env, json.dumps({"session_id": s2["session_id"], "reason": "exit"}))
    # 6. one file shows both sessions and the record
    html = _read(proj / "analytics" / "index.html")
    assert "11111111" in html and "22222222" in html and "hello-cli" in html
    assert sh(alpaca + ["verify"], proj, env).returncode == 0
    pad = _read(proj / "RESUME.md")
    assert "t-002" in pad and "test --shout" in pad
