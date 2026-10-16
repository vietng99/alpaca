"""alpaca/hooks/sandbox_guard.py: a write outside the op's signed sandbox answer is refused at the hook
(op-059 t-695). Temp projects only; the live record is never opened."""
import glob
import json
import os
import secrets
import subprocess
import sys

import pytest

from alpaca import sandbox
from alpaca.hooks import sandbox_guard
from alpaca.tests import sandbox_fixtures as fx
from alpaca.tests.conftest import REPO


@pytest.fixture(autouse=True)
def temp_base(tmp_path, monkeypatch):
    return fx.use_temp_base(monkeypatch, tmp_path)


@pytest.fixture
def env(project, tmp_path):
    return fx.make(project, tmp_path)


def _call(e, tool, **tin):
    return {"tool_name": tool, "tool_input": tin, "session_id": e.sid, "cwd": e.root}


def _bash(e, command, cwd=None):
    p = _call(e, "Bash", command=command)
    p["cwd"] = cwd or e.root
    return p


def _decide(e, payload):
    return sandbox_guard.decide(payload, root=e.root)


def _reason(out):
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    return out["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------- 5. DONE BAR 1
def test_write_outside_the_answer_is_refused_at_the_hook(env):
    out = _decide(env, _call(env, "Write", file_path=os.path.join(env.other, "x"), content="y"))
    reason = _reason(out)
    assert "op-062" in reason and "owner-confirmed list event %s" % env.set_event in reason
    assert env.other in reason
    assert "the session temp root" in reason and "sbxsess-aaa" in reason and "removed at session end" in reason
    assert "alpaca sandbox widen op-062" in reason
    assert _decide(env, _call(env, "Write", file_path=os.path.join(env.studio, "x"), content="y")) is None


def test_a_free_text_answer_without_a_confirmed_list_blocks_nothing(project, tmp_path):
    """Round 4 class change: the owner's free-text answer alone is a proposal; nothing is enforced (what
    the guard did before t-695) until the owner confirms a path list with `alpaca sandbox set`."""
    e = fx.make(project, tmp_path, confirm_paths=False)
    _all_none(e)
    fx.confirm(e.conn, e.op, ["root", e.studio])
    sandbox.clear_cache(e.root)
    assert _decide(e, _call(e, "Write", file_path=os.path.join(e.other, "x"), content="y")) is not None
    assert _decide(e, _call(e, "Write", file_path=os.path.join(e.studio, "x"), content="y")) is None


def test_the_hook_process_denies_on_stdout_and_notes_it(project, tmp_path):
    """The same payload through a real hook process (its own session id, so the real `/tmp/sbx<sid8>`
    it makes is this test's and is removed at the end): the deny on stdout, exit 0, one line in the notes."""
    sid = "sess-%s-0000-0000-0000-000000000000" % secrets.token_hex(4)
    env = fx.make(project, tmp_path, sid=sid)
    payload = json.dumps(_call(env, "Write", file_path=os.path.join(env.other, "x"), content="y"))
    child_env = {**os.environ, "CLAUDE_PROJECT_DIR": env.root}
    try:
        p = subprocess.run([os.path.join(REPO, "bin", "alpaca-python"), "--isolated", "-m",
                            "alpaca.hooks.sandbox_guard"], input=payload, capture_output=True, text=True,
                           cwd=env.root, env=child_env, timeout=60)
        made = os.path.isdir("/tmp/sbx" + sid[:8])
    finally:
        fx.rm_real_temp(sid)
    assert made and not os.path.exists("/tmp/sbx" + sid[:8])
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    lines = open(os.path.join(env.root, ".alpaca", "sandbox", "denials.jsonl")).read().splitlines()
    note = json.loads(lines[-1])
    assert note["tool"] == "Write" and note["ops"] == ["op-062"] and note["event"] == [env.set_event]


def test_a_sibling_folder_with_the_same_prefix_is_outside(env):
    assert _reason(_decide(env, _call(env, "Edit", file_path=env.studio + "-2/a", old_string="a",
                                       new_string="b")))


def test_a_notebook_and_a_multiedit_are_checked_too(env):
    assert _decide(env, _call(env, "NotebookEdit", notebook_path=env.other + "/n.ipynb"))
    assert _decide(env, _call(env, "MultiEdit", file_path=env.other + "/n.py", edits=[]))


# ---------------------------------------------------------------- 6. Bash denies
@pytest.mark.parametrize("command", [
    "mkdir {other}",
    "echo x > /tmp/f",
    "cd /tmp && touch f",
    "ls | tee -a /tmp/x",
    "cp a {other}/",
    "git clone https://example.org/r.git {other}",
    "git -C {root} worktree add {other}/wt",
    "pytest --basetemp /tmp/x t.py",
    "bash -c 'echo > /tmp/x'",
    "cat > /tmp/x <<'EOF'\nbody\nEOF",
    "FOO=1 timeout 60 rm -rf /tmp/zz",
    "touch ok.txt 2>/dev/null; mv a /tmp/b",
    "sed -i 's/a/b/' /tmp/f.txt",
    "curl -sSo /tmp/page https://example.org",
    "tar -czf /tmp/o.tgz src",
    "dd if=/dev/zero of=/tmp/z bs=1 count=1",
    "python -m venv {other}/v",
    "rm -rf /tmp/pytest-of-x/*",
    "install -m 755 a /usr/local/bin/a",
    "echo hi &> /tmp/log",
])
def test_bash_write_outside_is_denied(env, command):
    out = _decide(env, _bash(env, command.format(other=env.other, root=env.root)))
    assert out is not None, command
    _reason(out)


# ---------------------------------------------------------------- 7. allowed
@pytest.mark.parametrize("command", [
    "echo x > {root}/a.txt",
    "mkdir -p {studio}/sub && cp a {studio}/sub/",
    "touch {root}/.alpaca/notes/x",
    "echo x > $TMPDIR/x",
    "echo x > ${{TMPDIR}}/x",
    "make test 2>/dev/null",
    "cmd 2>&1 >/dev/null",
    "cat /tmp/x",
    "ls /etc > {root}/out",
    "cat >> {root}/f <<'EOF'\necho > /tmp/x\nEOF",
    "pytest --basetemp=$TMPDIR/t1 alpaca/tests/test_x.py",
    "git -C {root} worktree add {root}/.alpaca/worktrees/w -b br",
    "git clone https://example.org/r.git {studio}/r",
    "cd {studio} && git init && touch f",
    "tar -xf a.tgz -C {studio}",
    "ln -s {root}/a {root}/b",
    "echo x | tee {root}/t",
    "rm -rf {root}/build",
])
def test_bash_write_inside_is_allowed(env, command):
    cmd = command.format(root=env.root, studio=env.studio)
    assert _decide(env, _bash(env, cmd)) is None, cmd


def test_writes_to_scratchpad_claude_config_and_devices_are_allowed(env, tmp_path, monkeypatch):
    scratch = tmp_path / "claude-top" / "slug"
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(tmp_path))
    pad = os.path.join(str(tmp_path), "claude-%d" % os.getuid())
    from alpaca import paths
    slug = os.path.join(pad, paths.escape_cwd(env.root))
    os.makedirs(os.path.join(slug, env.sid, "scratchpad"))
    claude = os.path.expanduser("~/.claude/projects/x/memory/m.md")
    for target in (os.path.join(slug, env.sid, "scratchpad", "o.txt"), claude):
        assert _decide(env, _call(env, "Write", file_path=target, content="y")) is None, target
    assert _decide(env, _bash(env, "echo x > /dev/stderr")) is None


def test_reads_are_never_checked(env):
    assert _decide(env, _call(env, "Read", file_path="/etc/passwd")) is None
    assert _decide(env, _call(env, "Grep", pattern="x", path="/tmp")) is None


def test_a_symlink_inside_an_allowed_folder_is_judged_by_where_it_points(env, tmp_path):
    out = tmp_path / "elsewhere"
    out.mkdir()
    os.symlink(str(out), os.path.join(env.studio, "link"))
    assert _reason(_decide(env, _call(env, "Write", file_path=os.path.join(env.studio, "link", "f"),
                                       content="y")))


def test_a_glob_is_judged_by_its_folder(env):
    assert _decide(env, _bash(env, "rm -f /tmp/pytest-*"))
    assert _decide(env, _bash(env, "rm -f {}/build/*.o".format(env.root))) is None


def test_cd_changes_what_a_relative_target_means(env):
    assert _decide(env, _bash(env, "cd {} && touch f".format(env.studio))) is None
    assert _decide(env, _bash(env, "touch f", cwd="/tmp"))


# ---------------------------------------------------------------- 8. what it cannot see
@pytest.mark.parametrize("command", [
    "python -c \"open('/tmp/x','w')\"",
    "mkdir $D",
    "./script.sh",
    "cd $X && touch f",
    "echo x > $(mktemp)",
    "mktemp",
    "make out=/tmp/x",
])
def test_writes_the_guard_cannot_see_are_not_decided(env, command):
    assert _decide(env, _bash(env, command)) is None


def test_an_unreadable_command_is_scanned_for_its_write_shapes(env):
    """Round 5 (check-adversarial-r4.md B-4): a command the tokenizer cannot read is scanned conservatively
    for its write shapes, no longer skipped."""
    assert _decide(env, _bash(env, "echo 'unbalanced > /tmp/x")) is not None


# ---------------------------------------------------------------- 9. no sandbox, no decision
OUTSIDE = ["mkdir {other}", "echo x > /tmp/f", "pytest --basetemp /tmp/x t.py"]


def _all_none(e, root=None):
    for c in OUTSIDE:
        assert sandbox_guard.decide(_bash(e, c.format(other=e.other)), root=root or e.root) is None, c
    assert sandbox_guard.decide(_call(e, "Write", file_path="/tmp/q", content="y"), root=root or e.root) is None
    assert not os.path.exists(sandbox.env_path(root or e.root, e.sid))


def test_an_op_with_no_batch_is_never_blocked(project, tmp_path):
    _all_none(fx.make(project, tmp_path, batch=False))


def test_a_sandbox_item_still_open_blocks_nothing(project, tmp_path):
    _all_none(fx.make(project, tmp_path, state="open"))


def test_a_session_with_no_claim_and_not_the_asker_blocks_nothing(project, tmp_path):
    e = fx.make(project, tmp_path, claim=False)
    _all_none(e)


def test_no_record_at_all_blocks_nothing(project, tmp_path):
    e = fx.make(project, tmp_path)
    os.unlink(os.path.join(e.root, ".alpaca", "alpaca.db"))
    sandbox.clear_cache(e.root)
    _all_none(e)


def test_an_unreadable_record_blocks_nothing(project, tmp_path):
    e = fx.make(project, tmp_path)
    e.conn.close()
    for suffix in ("-wal", "-shm"):                      # the WAL would still hold the first page
        if os.path.exists(os.path.join(e.root, ".alpaca", "alpaca.db" + suffix)):
            os.unlink(os.path.join(e.root, ".alpaca", "alpaca.db" + suffix))
    with open(os.path.join(e.root, ".alpaca", "alpaca.db"), "wb") as fh:
        fh.write(b"not a database" * 100)
    sandbox.clear_cache(e.root)
    _all_none(e)


def test_hostile_payloads_decide_nothing(env):
    for payload in ({}, {"tool_name": "Bash"}, {"tool_name": "Bash", "session_id": env.sid, "tool_input": {}},
                    {"tool_name": "Write", "session_id": env.sid, "tool_input": {"file_path": 7}},
                    {"tool_name": "Write", "session_id": "unknown", "tool_input": {"file_path": "/tmp/x"}}):
        assert _decide(env, payload) is None


# ---------------------------------------------------------------- 10. DONE BAR 2
def test_sandboxed_pytest_run_leaves_nothing_in_tmp(env, tmp_path, temp_base):
    token = secrets.token_hex(4)
    assert _decide(env, _call(env, "Write", file_path=os.path.join(env.root, "ok.txt"), content="y")) is None
    env_file = sandbox.env_path(env.root, env.sid)
    assert os.path.isfile(env_file), "the guard writes the env file when a sandbox binds the session"
    temp_root = os.path.join(temp_base, "sbx" + env.sid[:8])
    assert os.path.isdir(temp_root)

    work = tmp_path / "child"
    work.mkdir()
    (work / "pytest.ini").write_text("[pytest]\n")
    (work / ("test_tok%s.py" % token)).write_text(
        "import os, tempfile\n"
        "def test_leaves_%s(tmp_path):\n"
        "    fd, mk = tempfile.mkstemp(prefix=%r)\n"
        "    os.close(fd)\n"
        "    print('PATHS', tmp_path, mk)\n" % (token, token))
    child_env = {k: v for k, v in os.environ.items()
                 if k not in ("TMPDIR", "PYTEST_DEBUG_TEMPROOT", "PYTEST_ADDOPTS", "TEMP", "TMP")}
    cmd = ". %s; %s -m pytest -q -s -p no:cacheprovider -c %s %s" % (
        env_file, sys.executable, work / "pytest.ini", work / ("test_tok%s.py" % token))
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd=str(work), env=child_env,
                       timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    line = [l for l in p.stdout.splitlines() if l.startswith("PATHS")][0]
    paths = line.split()[1:]
    assert len(paths) == 2 and all(x.startswith(temp_root + os.sep) for x in paths), paths
    assert glob.glob("/tmp/*%s*" % token) == []
    assert glob.glob("/tmp/pytest-of-*/pytest-*/*%s*" % token) == []


# ---------------------------------------------------------------- 11. a session started before the deploy
def test_unwired_session_must_name_a_basetemp_for_pytest(env, temp_base):
    temp_root = os.path.join(temp_base, "sbx" + env.sid[:8])
    reason = _reason(_decide(env, _bash(env, "python -m pytest -q alpaca/tests/test_x.py")))
    assert "--basetemp=%s/<task>-<role>" % temp_root in reason
    assert _decide(env, _bash(env, "python -m pytest --basetemp=$TMPDIR/t1 x.py")) is None
    assert _decide(env, _bash(env, "TMPDIR=%s python -m pytest x.py" % temp_root)) is None
    sandbox.mark_wired(env.root, env.sid)
    assert sandbox.is_wired(env.root, env.sid)
    assert _decide(env, _bash(env, "python -m pytest -q alpaca/tests/test_x.py")) is None
