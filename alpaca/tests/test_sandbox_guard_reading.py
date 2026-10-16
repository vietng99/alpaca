"""How alpaca/hooks/sandbox_guard.py reads a Bash command (op-059 t-695, round 2): the command position
past shell keywords and wrappers, the working directory as a stack, quoted operators, the must-fix
hardening items, and the known limits pinned as "no decision". Temp projects only."""
import json
import os
import secrets
import sqlite3
import subprocess
import time

import pytest

from alpaca import db, sandbox
from alpaca.hooks import sandbox_guard, temp_guard
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


def _d(e, payload):
    return sandbox_guard.decide(payload, root=e.root)


# ---------------------------------------------------------------- 1. command position (item 4)
KEYWORDS = [
    "if true; then touch {o}/f; fi",
    "for i in 1 2; do cp a {o}/; done",
    "while false; do :; done; if [ -d x ]; then rm -rf {o}; fi",
    "if [ -d x ]; then :; else mkdir -p {o}; fi",
    "if false; then :; elif true; then touch {o}/f; fi",
    "until false; do touch {o}/f; break; done",
    "! touch {o}/f",
    "{{ touch {o}/f; }}",
    "(touch {o}/f)",
    "time touch {o}/f",
    "for i in 1; do pytest --basetemp {o}/bt t.py; done",
    "while true; do python -m pytest --basetemp={o}/bt t.py; done",
]
WRAPPED_SHELLS = [
    "bash -c 'touch {o}/f'", "sh -c \"mkdir {o}\"", "bash -lc 'touch {o}/f'", "bash -ec 'touch {o}/f'",
    "bash -elc 'touch {o}/f'", "timeout 60 bash -c 'touch {o}/f'", "env bash -c 'touch {o}/f'",
    "env A=1 bash -c 'touch {o}/f'", "nohup bash -c 'touch {o}/f'", "setsid bash -c 'touch {o}/f'",
    "nice -n 5 bash -c 'touch {o}/f'", "bash --norc -c 'touch {o}/f'", "FOO=1 bash -c 'touch {o}/f'",
    "bash -c 'if true; then touch {o}/f; fi'", "ionice -c 3 bash -c 'touch {o}/f'",
]


@pytest.mark.parametrize("command", KEYWORDS + WRAPPED_SHELLS)
def test_a_command_after_a_shell_keyword_or_a_wrapper_is_read(env, command):
    assert _d(env, _bash(env, command.format(o=env.other))) is not None, command


@pytest.mark.parametrize("command", [
    "if true; then touch {r}/f; fi", "for i in 1; do cp a {r}/; done", "! touch {r}/f",
    "echo then touch {o}/f",                      # a keyword that is only an argument
    "echo do rm -rf {o}",
    "bash -c 'echo then; touch {r}/f'",
    "timeout 60 bash -c 'touch {r}/f'",
    "for i in 1; do pytest --basetemp=$TMPDIR/t1 t.py; done",
])
def test_keywords_and_wrappers_do_not_make_an_allowed_or_argument_word_a_deny(env, command):
    assert _d(env, _bash(env, command.format(o=env.other, r=env.root))) is None, command


def test_the_unwired_pytest_rule_sees_a_pytest_after_a_keyword(env):
    out = _d(env, _bash(env, "for f in a; do pytest t.py; done"))
    assert out is not None and "--basetemp" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert _d(env, _bash(env, "if true; then timeout 60 python -m pytest t.py; fi")) is not None


def test_command_head_skips_keywords_assignments_and_wrappers_only_at_the_front():
    head = temp_guard.command_head
    assert head(["then", "touch", "x"]) == ["touch", "x"]
    assert head(["do", "FOO=1", "timeout", "60", "pytest", "t"]) == ["pytest", "t"]
    assert head(["!", "touch", "x"]) == ["touch", "x"]
    assert head(["{", "touch", "x"]) == ["touch", "x"]
    assert head(["elif", "nohup", "setsid", "ls"]) == ["ls"]
    assert head(["echo", "then", "touch", "x"]) == ["echo", "then", "touch", "x"]
    assert head([]) == []


def test_temp_guard_finds_pytest_and_its_basetemp_in_a_loop_and_a_nohup_shell():
    assert temp_guard.basetemps("for i in 1 2; do pytest --basetemp /var/x/t t.py; done") == ["/var/x/t"]
    assert temp_guard.basetemps("nohup bash -lc 'python -m pytest --basetemp=/var/y t.py'") == ["/var/y"]
    assert temp_guard.notes("while true; do pytest --basetemp /var/x/t t.py; done")


# ---------------------------------------------------------------- 2. the working directory is a stack (item 5)
@pytest.mark.parametrize("command", [
    "bash -c 'cd /tmp && ls' ; touch notes.txt",
    "sh -c 'cd /tmp; ls'; echo x > out.txt",
    "pushd /tmp >/dev/null && ls && popd >/dev/null && touch notes.txt",
    "(cd /tmp && ls); touch notes.txt",
    "(cd /tmp; ls) && touch notes.txt",
    "x=$(cd /tmp && pwd); touch notes.txt",
    "bash -c 'cd /tmp'; bash -c 'cd /var'; touch notes.txt",
    "cd {studio} && (cd /tmp && ls); touch notes.txt",
])
def test_a_cd_inside_a_child_shell_or_a_subshell_does_not_leak(env, command):
    assert _d(env, _bash(env, command.format(studio=env.studio))) is None, command


@pytest.mark.parametrize("command", [
    "(cd {o} && touch f)",
    "(cd {o}; touch f)",
    "bash -c 'cd {o} && touch f'",
    "pushd {o} && touch f",
    "cd {o} && (ls); touch f",
    "cd {o} && bash -c 'ls'; touch f",
    "pushd /tmp && pushd {o} && touch f",
    "(cd {studio}; true); cd {o}; touch f",
])
def test_a_cd_that_does_apply_is_followed(env, command):
    assert _d(env, _bash(env, command.format(o=env.other, studio=env.studio))) is not None, command


def test_popd_with_nothing_pushed_makes_the_directory_unknown_so_relative_targets_are_not_judged(env):
    assert _d(env, _bash(env, "popd; touch f")) is None
    assert _d(env, _bash(env, "popd; touch %s/f" % env.other)) is not None      # absolute: still judged


def test_the_stack_pops_to_the_cwd_the_payload_gave(env):
    out = _d(env, _bash(env, "pushd %s && popd && touch f" % env.studio, cwd="/tmp"))
    assert out is not None and "/tmp/f" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------- 3. pytest that writes nothing (item 5)
@pytest.mark.parametrize("command", [
    "pytest --version", "pytest -V", "pytest --help", "pytest -h", "python -m pytest --collect-only -q t.py",
    "pytest --co t.py", "pytest --fixtures", "pytest --markers",
])
def test_an_unwired_pytest_call_that_writes_nothing_is_not_denied(env, command):
    assert not sandbox.is_wired(env.root, env.sid)
    assert _d(env, _bash(env, command)) is None, command


def test_an_unwired_pytest_run_is_still_denied_and_a_collect_only_basetemp_outside_too(env):
    assert _d(env, _bash(env, "pytest -q t.py")) is not None
    assert _d(env, _bash(env, "pytest --collect-only --basetemp /tmp/zz t.py")) is not None


# ---------------------------------------------------------------- 4. a quoted operator is a word (item 5)
@pytest.mark.parametrize("command", [
    "grep -c '>' /etc/hosts", 'grep -c ">" /etc/hosts', "grep -c '>>' /etc/hosts", "echo a \\> b",
    "awk '$1 == \">\"' /etc/hosts", "grep '|' /etc/hosts", "grep -c '&' /etc/hosts",
    "echo '>' >> {r}/f", "grep -n '<' /etc/hosts > {r}/out",
])
def test_a_quoted_or_escaped_lone_operator_is_not_a_redirection(env, command):
    assert _d(env, _bash(env, command.format(r=env.root))) is None, command


@pytest.mark.parametrize("command", [
    "echo x > {o}/f", "echo '>' > {o}/f", "echo \">\" >> {o}/f", "grep '>' /etc/hosts > {o}/f",
    "echo 'a > b' > {o}/f",
])
def test_a_real_redirection_next_to_a_quoted_operator_is_still_read(env, command):
    assert _d(env, _bash(env, command.format(o=env.other))) is not None, command


# ---------------------------------------------------------------- 5. the record lookup is cut short (item 5)
def test_the_record_lookup_waits_half_a_second_not_ten(env, monkeypatch):
    seen = {}
    real = db.connect_readonly

    def spy(root, **kw):
        seen.update(kw)
        return real(root, **kw)
    monkeypatch.setattr(db, "connect_readonly", spy)
    sandbox.clear_cache(env.root)
    assert sandbox.resolve(env.root, env.sid) is not None
    assert seen == {"timeout": sandbox.LOOKUP_TIMEOUT_S} and sandbox.LOOKUP_TIMEOUT_S == 0.5


def test_a_locked_record_uses_a_cache_up_to_200_seconds_old_and_no_decision_beyond(env, monkeypatch):
    first = sandbox.resolve(env.root, env.sid)
    assert first is not None
    cache = sandbox.cache_path(env.root, env.sid)
    entry = json.load(open(cache))

    def age(seconds):                             # round 5: an entry carries its session and hash (MINOR 3)
        aged = dict(entry, at=time.time() - seconds)
        aged["hash"] = sandbox._seal(aged)
        json.dump(aged, open(cache, "w"))

    def locked(root, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "connect_readonly", locked)
    assert sandbox.CACHE_S * 10 == 200.0
    for seconds in (60, 190):
        age(seconds)
        got = sandbox.resolve(env.root, env.sid)
        assert got is not None and got["ops"] == first["ops"], seconds
        assert _d(env, _call(env, "Write", file_path=os.path.join(env.other, "x"), content="y")) is not None
    for seconds in (210, 3600):
        age(seconds)
        with pytest.raises(sqlite3.OperationalError):
            sandbox.resolve(env.root, env.sid)
        assert _d(env, _call(env, "Write", file_path=os.path.join(env.other, "x"), content="y")) is None
    os.unlink(cache)
    assert _d(env, _call(env, "Write", file_path=os.path.join(env.other, "x"), content="y")) is None


def test_a_record_held_under_an_exclusive_lock_costs_the_hook_process_well_under_ten_seconds(env):
    sandbox.clear_cache(env.root)
    db_path = os.path.join(env.root, ".alpaca", "alpaca.db")
    env.conn.close()
    import gc
    gc.collect()                                  # no connection of this process may still hold the file
    locker = subprocess.Popen(["python3", "-c", (
        "import sqlite3,sys,time\n"
        "c=sqlite3.connect(sys.argv[1],isolation_level=None,timeout=20)\n"
        "c.execute('PRAGMA locking_mode=EXCLUSIVE')\n"
        "c.execute('BEGIN EXCLUSIVE')\n"
        "c.execute('CREATE TABLE IF NOT EXISTS lock_probe(x)')\n"
        "print('locked',flush=True)\n"
        "time.sleep(30)\n"), db_path], stdout=subprocess.PIPE, text=True)
    try:
        assert locker.stdout.readline().strip() == "locked"
        payload = json.dumps(_bash(env, "touch %s/f" % env.other))
        t = time.monotonic()
        p = subprocess.run([os.path.join(REPO, "bin", "alpaca-python"), "--isolated", "-m",
                            "alpaca.hooks.sandbox_guard"], input=payload, capture_output=True, text=True,
                           cwd=env.root, env={**os.environ, "CLAUDE_PROJECT_DIR": env.root}, timeout=60)
        dt = time.monotonic() - t
    finally:
        locker.kill()
        locker.wait()
    assert p.returncode == 0 and p.stdout.strip() == "" and dt < 5.0, (dt, p.stderr[:200])


# ---------------------------------------------------------------- 6. a huge command is denied, not skipped
def test_a_command_over_64_kb_after_heredoc_stripping_is_denied_with_a_clear_message(env):
    pad = "echo " + "x" * (sandbox_guard.MAX_COMMAND_BYTES + 10)
    out = _d(env, _bash(env, "touch %s/f; %s" % (env.other, pad)))
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "bytes after here-document bodies, too long for the sandbox guard to read" in reason
    assert "split it" in reason and "here-document or a file" in reason
    assert _d(env, _bash(env, pad)) is not None, "a harmless command is denied too: its size is the reason"
    last = [json.loads(x) for x in open(os.path.join(env.root, sandbox_guard.DENIALS))][-1]
    assert last["tool"] == "Bash" and "64 KB" in last["target"]
    body = "cat > %s/f <<'EOF'\n%s\nEOF" % (env.root, "line > /tmp/x\n" * 20000)      # the body is data
    assert _d(env, _bash(env, body)) is None


def test_a_command_under_the_limit_is_still_read(env):
    inside = "echo " + "x" * 60000
    assert len(inside) < sandbox_guard.MAX_COMMAND_BYTES
    assert _d(env, _bash(env, inside)) is None
    t = time.monotonic()
    assert _d(env, _bash(env, "touch %s/f; %s" % (env.other, inside))) is not None
    assert time.monotonic() - t < 3.0


@pytest.mark.parametrize("pad", ["comment", "string", "arg"])
def test_padding_a_command_past_64_kb_switches_nothing_off(env, pad):
    filler = "A" * (sandbox_guard.MAX_COMMAND_BYTES + 10)
    c = {"comment": "touch %s/f # %s" % (env.other, filler),
         "string": ": '%s'; touch %s/f" % (filler, env.other),
         "arg": "touch %s/f %s" % (env.other, filler)}[pad]
    t = time.monotonic()
    assert _d(env, _bash(env, c)) is not None
    assert time.monotonic() - t < 1.0


def test_a_hundred_thousand_character_quoted_word_is_denied_and_costs_no_time(env):
    t = time.monotonic()
    assert _d(env, _bash(env, "python -c '" + "x = 1; " * 20000 + "' > %s/o" % env.other)) is not None
    assert time.monotonic() - t < 1.0


# ---------------------------------------------------------------- 7. the one-line hardening (item 5)
@pytest.mark.parametrize("command", [
    "echo x > /dev/fd/../../tmp/adv", "echo x > /dev/stdout/../../tmp/adv", "echo x > /proc/self/fd/../../tmp/x",
])
def test_a_device_path_that_climbs_out_is_judged_by_where_it_lands(env, command):
    assert _d(env, _bash(env, command)) is not None, command


@pytest.mark.parametrize("command", ["echo x > /dev/fd/2", "echo x >&2", "echo x > /dev/stderr",
                                     "echo x > /dev/null", "cmd 2> /dev/null"])
def test_the_device_files_stay_allowed(env, command):
    assert _d(env, _bash(env, command)) is None, command


def test_a_tilde_path_in_a_file_tool_is_judged_as_the_home_folder(env):
    assert _d(env, _call(env, "Write", file_path="~/adv-x", content="y")) is not None
    assert _d(env, _call(env, "Edit", file_path="~/work/a.py", old_string="a", new_string="b")) is not None
    assert _d(env, _call(env, "Write", file_path="~/.claude/projects/x/m.md", content="y")) is None


def test_a_link_then_dotdot_is_resolved_by_the_kernel_rule(env):
    deep = os.path.join(env.other, "deep")
    os.makedirs(deep)
    os.symlink(deep, os.path.join(env.root, "lnk"))                    # root/lnk -> studio-2/deep
    assert _d(env, _bash(env, "echo x > lnk/../escaped.txt")) is not None
    assert _d(env, _bash(env, "touch lnk/../escaped.txt")) is not None
    target = os.path.join(env.root, "lnk", "..", "escaped.txt")
    assert _d(env, _call(env, "Write", file_path=target, content="x")) is not None
    assert _d(env, _bash(env, "echo x > sub/../ok.txt")) is None        # no link: stays in the root


# ---------------------------------------------------------------- 8. known limits, pinned as "no decision" (item 6)
LIMITS = [
    "cp -at {o} a",                         # combined short flags that end in -t
    "tar -xf a.tar -C{o}",                  # a value stuck to the flag
    "ln -s {o} l && echo x > l/f",          # a link made earlier in the same command
    "sort -o {o}/f in.txt",
    "printf 'a\\n' | xargs cp -t {o}",
    "find . -name '*.o' -exec cp {{}} {o} \\;",
    "env -S 'touch {o}/f'",                 # a split string hides the command
    "python -c \"open('{o}/f','w')\"",
    "make out={o}/x",
    "./build.sh {o}",
    "cp <(echo a) {o}/f",                   # a process substitution
    "mkdir $NOPE",
    # round 3: stated in the docs, never denied
    "echo $(date)>{o}/f", "(ls)>{o}/f", "ls;>{o}/f", "true&&>{o}/f", "ls|>{o}/f", "cat <>{o}/f",
    "echo a#b > {o}/f", "curl https://e.org/#frag -o {o}/f",
    "function f {{ touch {o}/f; }}; f",
    "echo \"$(touch {o}/f)\"", "echo `touch {o}/f`",
    "bash -c \"bash -c \\\"bash -c 'touch {o}/f'\\\"\"",
    "touch {{{o},/var}}/f",
]
#: Reads a wired session does not see either: pytest options that write a file, beside --basetemp.
WIRED_LIMITS = ["python -m pytest -o cache_dir={o}/c t.py", "pytest --junitxml={o}/x.xml t.py"]


@pytest.mark.parametrize("command", LIMITS)
def test_known_limit_is_no_decision(env, command):
    assert _d(env, _bash(env, command.format(o=env.other))) is None, command


@pytest.mark.parametrize("command", WIRED_LIMITS)
def test_known_limit_of_a_wired_session_is_no_decision(env, command):
    sandbox.resolve(env.root, env.sid)
    sandbox.mark_wired(env.root, env.sid)
    assert sandbox.is_wired(env.root, env.sid)
    assert _d(env, _bash(env, command.format(o=env.other))) is None, command


def test_two_sessions_with_the_same_first_eight_characters_share_a_temp_root_name():
    a = "codex-01a0e67d-ac76-7592-b1f3-9bc8a585d03e"
    b = "codex-01a0e681-567e-7362-a775-c3e268f0bd49"
    assert sandbox.temp_root(None, a) == sandbox.temp_root(None, b)      # told apart by the marker only


def test_the_module_doc_and_the_docs_state_each_limit():
    doc = " ".join(sandbox_guard.__doc__.split())
    for phrase in ("cp -at", "tar -C", "symlink made earlier", "sort -o", "xargs", "find -exec",
                   "env -S", "process substitution", "writes inside programs",
                   "glued to a separator", "`#` inside a word", "`function` keyword",
                   "inside double quotes", "third nested", "brace list", "pytest -o cache_dir",
                   "--junitxml", "first 8 characters", "widen race"):
        assert phrase in doc, phrase
    for gone in ("timeout -s KILL 5 cmd", "`env -C DIR`;"):
        assert gone not in doc, gone
    text = " ".join(open(os.path.join(REPO, "docs", "temp-and-scratch.md"), encoding="utf-8").read().split())
    for phrase in ("cp -at DIR", "tar -C<dir>", "ln -s OUT l", "sort -o FILE", "find -exec", "env -S",
                   "process substitution", "a write made inside a program", "glued to a separator",
                   "`#` inside a word", "`function` keyword", "inside double quotes", "third nested",
                   "brace list", "pytest -o cache_dir", "--junitxml", "first 8 characters", "widen race"):
        assert phrase in text, phrase
    for gone in ("timeout -s KILL 5 cmd", "env -C DIR;"):
        assert gone not in text, gone
