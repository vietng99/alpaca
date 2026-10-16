"""Round 3 hardening of the sandbox write guard (op-059 t-695, investigate-r2.md section 5): two false
denies (a backslash here-document, a `cd` in a background or piped segment), the breach class of the
trial (same-command variables, wrapper option values, backslash-newline) and the tamper rows (the
guard's own cache, env and denial files, a cache dated in the future). Temp projects only."""
import json
import os
import time

import pytest

from alpaca import sandbox
from alpaca.hooks import sandbox_guard, temp_guard
from alpaca.tests import sandbox_fixtures as fx


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


def _d(e, payload):
    return sandbox_guard.decide(payload, root=e.root)


def _wire(e):
    """A session whose Bash calls source the env file: the unwired-pytest rule does not apply."""
    sandbox.resolve(e.root, e.sid)
    sandbox.mark_wired(e.root, e.sid)
    assert sandbox.is_wired(e.root, e.sid)


# ---------------------------------------------------------------- 2a. here-documents
def test_strip_heredocs_cuts_a_backslash_quoted_body_and_ignores_a_quoted_marker():
    sh = temp_guard.strip_heredocs
    assert sh("cat <<\\EOF\nbody\nEOF\nls") == "cat <<\\EOF\nls"
    assert sh("cat <<-'EOF'\n\tbody\n\tEOF\nls") == "cat <<-'EOF'\nls"
    assert sh('cat <<"EOF"\nbody\nEOF\nls') == 'cat <<"EOF"\nls'
    assert sh('echo "<<EOF"\nls\nEOF') == 'echo "<<EOF"\nls\nEOF'
    assert sh("echo '<<EOF'; cat <<X\nb\nX\nls") == "echo '<<EOF'; cat <<X\nls"


@pytest.mark.parametrize("command", [
    "cat <<\\EOF > out.txt\necho y > {o}/f\nEOF",
    "cat <<-EOF > out.txt\n\techo y > {o}/f\n\tEOF",
    "cat <<\"EOF\" > out.txt\ntouch {o}/f\nEOF",
    "cat <<\\EOF > out.txt\nit's\nEOF",
])
def test_a_backslash_or_quoted_here_document_body_is_data_not_commands(env, command):
    assert _d(env, _bash(env, command.format(o=env.other))) is None, command


@pytest.mark.parametrize("command", [
    "cat <<\\EOF > out.txt\nit's\nEOF\ntouch {o}/f",
    "echo \"<<EOF\"\ntouch {o}/f",
    "echo '<<EOF'\ntouch {o}/f",
    "cat <<\\EOF > out.txt\nx\nEOF\ntouch {o}/f",
    "cat <<EOF > {o}/f\nx\nEOF",
])
def test_the_line_after_a_here_document_or_a_quoted_marker_is_still_read(env, command):
    assert _d(env, _bash(env, command.format(o=env.other))) is not None, command


# ---------------------------------------------------------------- 2b. cd in a background or piped segment
@pytest.mark.parametrize("command", [
    "cd /tmp & touch notes.txt", "cd /tmp | touch notes.txt", "ls | cd /tmp; touch notes.txt",
    "pushd /tmp & touch notes.txt", "cd /tmp |& touch notes.txt", "cd /tmp &\ntouch notes.txt",
    "popd | cat; touch notes.txt",
])
def test_a_cd_that_runs_in_the_background_or_a_pipe_does_not_move_the_guard(env, command):
    assert _d(env, _bash(env, command)) is None, command


@pytest.mark.parametrize("command", [
    "cd {o} && touch f", "cd {o} || touch f", "cd {o}; touch f", "x & cd {o}; touch f",
    "ls | wc & cd {o} && touch f", "pushd {o} && touch f", "cd /tmp & touch {o}/f",
])
def test_a_cd_in_the_foreground_still_moves_the_guard(env, command):
    assert _d(env, _bash(env, command.format(o=env.other))) is not None, command


# ---------------------------------------------------------------- 3a. same-command variables
@pytest.mark.parametrize("command", [
    "B={o}/x; rm -rf $B",
    "B={o}/bt; pytest --basetemp=$B t.py",
    "export TMPDIR={o}; touch $TMPDIR/f",
    "declare -x T={o}; touch ${{T}}/f",
    "A={o}; B=$A/sub; touch $B/f",
    "readonly B={o}; touch $B/f",
    "local B={o}; touch $B/f",
    "B={o}; (touch $B/f)",
    "B={o}; bash -c 'touch $B/f'",
    "B={o}; if true; then touch $B/f; fi",
    "B={o} && touch $B/f",
    "TMPDIR={o}/x pytest t.py",
    "PYTEST_DEBUG_TEMPROOT={o}/x python -m pytest t.py",
    "env TMPDIR={o}/x pytest t.py",
    "export TMPDIR={o}; pytest t.py",
    "export PYTEST_DEBUG_TEMPROOT={o}; python -m pytest t.py",
])
def test_a_variable_set_in_the_same_command_is_followed_into_its_targets(env, command):
    assert _d(env, _bash(env, command.format(o=env.other))) is not None, command


def test_a_basetemp_variable_gives_the_rm_and_the_pytest_target(env):
    got = sandbox_guard.bash_targets("B=%s/x; rm -rf $B; pytest --basetemp=$B t.py" % env.other, env.root,
                                     "/tmp/sbxfake")
    assert got == [(env.other + "/x", "rm"), (env.other + "/x", "pytest")], got


@pytest.mark.parametrize("command", [
    "(B={o}); touch $B/f",
    "B=$(echo {o}); touch $B/f",
    "B=`echo {o}`; touch $B/f",
    "B={o} touch $B/f",                                  # a prefix assignment is not seen by its own command
    "B={o}; unset B; touch $B/f",
    "unset B; touch $B/f",
    "B={o}; B+=/x; touch $B/f",
    "B=\"{o} /etc\"; rm $B",                             # a value with a space is word-split: unknown
    "B={r}/ok; touch $B/f",
    "export TMPDIR=$TMPDIR/x; touch $TMPDIR/f",
    "TMPDIR={r}/t pytest t.py",
    "A=$X; touch $A/f",
])
def test_a_variable_the_guard_cannot_know_or_that_stays_inside_gives_no_decision(env, command):
    assert _d(env, _bash(env, command.format(o=env.other, r=env.root))) is None, command


# ---------------------------------------------------------------- 3b. wrapper option values
@pytest.mark.parametrize("command", [
    "sudo -u x bash -c 'touch {o}/f'", "sudo -u root touch {o}/f", "sudo -Eu root touch {o}/f",
    "sudo -uroot touch {o}/f", "sudo -g wheel -u root touch {o}/f", "sudo --user root touch {o}/f",
    "sudo --user=root touch {o}/f", "sudo -D /x touch {o}/f",
    "env -u FOO touch {o}/f", "env -u A -u B touch {o}/f", "env --unset FOO touch {o}/f",
    "env -u ALPACA_ROOT -u CLAUDE_PROJECT_DIR python -m pytest --basetemp={o}/t136-proof",
    "stdbuf -o L touch {o}/f", "stdbuf -i0 -oL touch {o}/f", "stdbuf -e 0 touch {o}/f",
    "nice -n 5 touch {o}/f", "nice --adjustment 5 touch {o}/f",
    "ionice -c 3 -n 7 touch {o}/f", "ionice -p 5 touch {o}/f",
    "timeout -s KILL 5 touch {o}/f", "timeout -k 5 10 touch {o}/f", "timeout --signal=KILL 5 touch {o}/f",
    "timeout --signal KILL 5 touch {o}/f", "timeout --kill-after 5 10 touch {o}/f",
    "sudo -u root env -u X touch {o}/f",
])
def test_a_wrapper_option_value_is_skipped_so_the_real_command_is_read(env, command):
    assert _d(env, _bash(env, command.format(o=env.other))) is not None, command


@pytest.mark.parametrize("command", [
    "sudo -u root touch {r}/f", "env -u FOO touch f", "timeout -s KILL 5 touch f", "stdbuf -o L touch f",
    "env -C {r}/sub touch f", "echo x | env --chdir {r} tee f", "sudo -D {r} touch f",
    "env -C $UNKNOWN touch f",                           # the folder cannot be told: a relative target is unjudged
])
def test_a_wrapper_that_writes_inside_is_not_a_deny(env, command):
    assert _d(env, _bash(env, command.format(o=env.other, r=env.root))) is None, command


@pytest.mark.parametrize("command", [
    "env -C {o} touch f", "echo x | env -C {o} tee f", "sudo -D {o} touch f", "env --chdir={o} touch f",
    "env --chdir {o} touch f", "sudo --chdir={o} touch f", "env -C /tmp -C {o} touch f",
])
def test_env_and_sudo_chdir_set_the_folder_of_the_command(env, command):
    assert _d(env, _bash(env, command.format(o=env.other))) is not None, command


def test_the_command_head_skips_a_wrapper_option_and_its_value():
    head = temp_guard.command_head
    assert head(["sudo", "-u", "x", "touch", "f"]) == ["touch", "f"]
    assert head(["sudo", "-uroot", "touch", "f"]) == ["touch", "f"]
    assert head(["sudo", "-Eu", "root", "touch", "f"]) == ["touch", "f"]
    assert head(["env", "-u", "A", "-u", "B", "cmd", "-u", "z"]) == ["cmd", "-u", "z"]
    assert head(["timeout", "-s", "KILL", "5", "pytest", "t"]) == ["pytest", "t"]
    assert head(["timeout", "--signal=KILL", "5", "pytest"]) == ["pytest"]
    assert head(["nice", "-n", "5", "ls"]) == ["ls"]
    assert head(["ionice", "-c", "3", "ls"]) == ["ls"]
    assert head(["stdbuf", "-oL", "ls"]) == ["ls"]
    assert head(["env", "-S", "touch x", "ls"]) == []         # a split string hides the command: unread
    assert head(["touch", "-d", "now", "f"]) == ["touch", "-d", "now", "f"]


# ---------------------------------------------------------------- 3c. backslash-newline
@pytest.mark.parametrize("command", [
    "pytest -q \\\n  --basetemp={o}/bt t.py",
    "rm -rf \\\n  {o}",
    "touch {r}/a \\\n  {o}/b",
    "python -m pytest \\\n  -q \\\n  --basetemp {o}/bt t.py",
    "echo a\\\\\ntouch {o}/f",                           # an escaped backslash is not a continuation
])
def test_a_backslash_newline_continuation_is_joined(env, command):
    assert _d(env, _bash(env, command.format(o=env.other, r=env.root))) is not None, command


@pytest.mark.parametrize("command", [
    "echo a \\\n  > {r}/f",
    "cat <<EOF > {r}/f\nfoo \\\n  {o}/x\nEOF",
    "pytest -q \\\n  --basetemp=$TMPDIR/t1 t.py",
])
def test_a_continuation_that_stays_inside_is_not_a_deny(env, command):
    assert _d(env, _bash(env, command.format(o=env.other, r=env.root))) is None, command


# ---------------------------------------------------------------- 4. tamper
def _guarded(env):
    sandbox.resolve(env.root, env.sid)
    return [sandbox.cache_path(env.root, env.sid), sandbox.env_path(env.root, env.sid),
            sandbox.wired_path(env.root, env.sid), os.path.join(env.root, sandbox_guard.DENIALS),
            os.path.join(env.root, ".alpaca", "sandbox", "new.json")]


def test_the_agent_cannot_write_the_guards_own_cache_env_wired_or_denial_files(env):
    for path in _guarded(env):
        for tool in ("Write", "Edit", "MultiEdit"):
            assert _d(env, _call(env, tool, file_path=path, content="{}")) is not None, (tool, path)
        for command in ("echo '{}' > %s", "rm -f %s", "mv x %s", "echo x | tee %s", "truncate -s0 %s",
                        "cp x %s", "sed -i s/a/b/ %s"):
            assert _d(env, _bash(env, command % path)) is not None, (command, path)
    assert _d(env, _bash(env, "rm -rf .alpaca/sandbox")) is not None
    assert _d(env, _bash(env, "echo x > .alpaca/sandbox/cache/a.json")) is not None
    assert _d(env, _bash(env, "cd .alpaca/sandbox && touch f")) is not None


def test_the_rest_of_the_record_area_stays_writable(env):
    ok = os.path.join(env.root, ".alpaca")
    for path in (ok + "/worktrees/w/f.py", ok + "/alpaca.db-journal", ok + "/sandboxes/x", ok + "/sandbox-x",
                 ok + "/proofs/op-1/t-1.md"):
        assert _d(env, _call(env, "Write", file_path=path, content="y")) is None, path


def test_the_cache_carries_the_guarded_path_and_covers_refuses_it_before_the_always_list(env):
    sb = sandbox.resolve(env.root, env.sid)
    guarded = os.path.join(env.root, ".alpaca", "sandbox")
    assert sb["guarded"][0] == guarded                     # round 5: then the session transcripts (MINOR 4)
    assert all(g.endswith(".jsonl") for g in sb["guarded"][1:]) and len(sb["guarded"]) > 1
    assert not sandbox.covers(guarded + "/cache/x.json", sb) and not sandbox.covers(guarded, sb)
    assert sandbox.covers(os.path.join(env.root, ".alpaca", "sandbox-other"), sb)
    assert sandbox.covers(os.path.join(env.root, "src", "a.py"), sb)


def test_a_cache_dated_in_the_future_or_of_a_wrong_shape_is_stale(env):
    first = sandbox.resolve(env.root, env.sid)
    cache = sandbox.cache_path(env.root, env.sid)
    for entry in ({"at": time.time() + 10 ** 9, "root": env.root, "sandbox": None},
                  {"at": time.time(), "root": env.root, "sandbox": "everything"},
                  {"at": time.time(), "root": env.root, "sandbox": [1]},
                  {"at": time.time() + 30, "root": env.root, "sandbox": None}):
        json.dump(entry, open(cache, "w"))
        got = sandbox.resolve(env.root, env.sid)
        assert got is not None and got["roots"] == first["roots"], entry
        assert _d(env, _bash(env, "touch %s/f" % env.other)) is not None, entry
    fresh = {"at": time.time() - 5, "root": env.root, "sid": env.sid, "sandbox": None}
    json.dump(fresh, open(cache, "w"))
    assert sandbox.resolve(env.root, env.sid) is not None, "round 5: an entry with no hash is ignored"
    fresh["hash"] = sandbox._seal(fresh)
    json.dump(fresh, open(cache, "w"))
    assert sandbox.resolve(env.root, env.sid) is None, "a fresh sealed cache entry is still used"
