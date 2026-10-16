"""Round 4 of t-695 (op-059): the one-pass here-document scanner, the 2 s parsing budget, pytest without
TMPDIR, and the reader closes of check-adversarial-r3.md (MINOR 5, 7 to 11). Temp projects only; every
probe command is data handed to the guard, never run."""
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
    e = fx.make(project, tmp_path)
    sandbox.resolve(e.root, e.sid)
    sandbox.mark_wired(e.root, e.sid)              # a wired session: the unwired-pytest rule is not in play
    return e


def _d(e, command):
    return sandbox_guard.decide({"tool_name": "Bash", "tool_input": {"command": command},
                                 "session_id": e.sid, "cwd": e.root}, root=e.root)


def _reason(out):
    assert out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    return out["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------- 1. the scanner, as text
#: (command, what strip_heredocs gives, what it gives with join=True)
STRIP = [
    # MINOR 5: a "..." over several lines that closes on the line of the opener (commit and report)
    ("git commit -m \"a\n\nb\" && cat > f <<'EOF'\nbody > /x\nit's\nEOF\ntouch y",
     "git commit -m \"a\n\nb\" && cat > f <<'EOF'\ntouch y", None),
    # MINOR 10: openers bash does not see
    ("true # <<EOF\ntouch y\nEOF", "true # <<EOF\ntouch y\nEOF", None),
    ("echo $((1<<X))\ntouch y\nX", "echo $((1<<X))\ntouch y\nX", None),
    ("(( x = 1<<3 ))\ntouch y\nx", "(( x = 1<<3 ))\ntouch y\nx", None),
    ("cat <<END-OF > o\ndata\nEND-OF\ntouch y", "cat <<END-OF > o\ntouch y", None),
    ("cat <<E'O'F\nbody\nEOF\nls", "cat <<E'O'F\nls", None),
    # MINOR 11: a plain << body ends only at the exact line; <<- strips leading tabs
    ("cat <<EOF > o\n  EOF\ntouch y\nEOF\nls", "cat <<EOF > o\nls", None),
    ("cat <<-EOF\n\tbody\n\tEOF\nls", "cat <<-EOF\nls", None),
    # MINOR 9: a continuation is joined only outside a comment; the body starts after the joined line
    ("ls # note \\\ntouch y", "ls # note \\\ntouch y", "ls # note \\\ntouch y"),
    ("cat <<EOF \\\n> f\nbody\nEOF\nls", "cat <<EOF \\\n> f\nls", "cat <<EOF > f\nls"),
    ("pytest -q \\\n--basetemp=/x", "pytest -q \\\n--basetemp=/x", "pytest -q --basetemp=/x"),
    ("echo 'a\\\nb' > o", "echo 'a\\\nb' > o", "echo 'a\\\nb' > o"),
    ("echo \"a\\\nb\" > o", "echo \"a\\\nb\" > o", "echo \"ab\" > o"),
    # quoted, escaped and here-string forms start no body
    ("echo '<<EOF'\ntouch y", "echo '<<EOF'\ntouch y", None),
    ("echo \\<<EOF\ntouch y", "echo \\<<EOF\ntouch y", None),
    ("cat <<<EOF\ntouch y", "cat <<<EOF\ntouch y", None),
    ("echo $'it\\'s <<EOF'\ntouch y", "echo $'it\\'s <<EOF'\ntouch y", None),
    # bodies that are cut
    ("x=$(cat <<EOF\nbody\nEOF\n)\nls", "x=$(cat <<EOF\n)\nls", None),
    ("cat <<A <<B\na\nA\nb\nB\nls", "cat <<A <<B\nls", None),
    ("echo ${#x} <<EOF\nb\nEOF\nls", "echo ${#x} <<EOF\nls", None),
    ("echo a#b <<EOF\nb\nEOF\nls", "echo a#b <<EOF\nls", None),
    ("cat <<EOF # a comment\nbody\nEOF\nls", "cat <<EOF # a comment\nls", None),
    ("cat <<\\EOF\nbody\nEOF\nls", "cat <<\\EOF\nls", None),
    ("cat << 'EOF'\nbody\nEOF\nls", "cat << 'EOF'\nls", None),
    ("cat <<EOF\nno end", "cat <<EOF", None),
]


@pytest.mark.parametrize("command,plain,joined", STRIP)
def test_the_scanner_cuts_exactly_the_bodies_bash_reads(command, plain, joined):
    assert temp_guard.strip_heredocs(command) == plain
    assert temp_guard.strip_heredocs(command, join=True) == (plain if joined is None else joined)


@pytest.mark.parametrize("command,read", [
    ("ls # note \\\ntouch y", "ls \ntouch y"),
    ("true # <<EOF\ntouch y\nEOF", "true \ntouch y\nEOF"),
    ("# step 1\nmkdir x # make it", "\nmkdir x "),
    ("echo a#b ${#x} '#q' \"#d\" \\#e", "echo a#b ${#x} '#q' \"#d\" \\#e"),
    ("cat <<EOF # c\nbody\nEOF\nls", "cat <<EOF \nls"),
])
def test_the_guard_reads_a_command_with_its_comments_dropped(command, read):
    # shlex eats the newline with a comment, which ran the next line into the commented one
    assert temp_guard.strip_heredocs(command, join=True, comments=False) == read


def test_the_scanner_stops_at_its_deadline():
    with pytest.raises(temp_guard.TooComplex):
        temp_guard.strip_heredocs("x " * 100000, deadline=time.monotonic() - 1)
    with pytest.raises(temp_guard.TooComplex):
        temp_guard._split("x " * 100000, deadline=time.monotonic() - 1)


# ---------------------------------------------------------------- 2. the scanner, at the hook
SCANNER_OUT = [
    "ls # note \\\ntouch {o}/f",
    "true # <<EOF\ntouch {o}/f\nEOF",
    "echo $((1<<X))\ntouch {o}/f\nX",
    "cat <<END-OF > o.txt\ndata\nEND-OF\ntouch {o}/f",
    "cat <<EOF \\\n> {o}/f\nhello\nEOF",
    "git commit -q -m \"subject\n\nbody\" && cat > notes.md <<'EOF'\nit's done\nEOF\ntouch {o}/f",
]


@pytest.mark.parametrize("command", SCANNER_OUT)
def test_a_write_after_a_line_the_old_reader_misread_is_denied(env, command):
    assert _d(env, command.format(o=env.other)) is not None


SCANNER_IN = [
    # the real false deny of session 5f45272f: a commit message over several lines, then a report
    "git commit -q -m \"subject\n\nbody\n\nCo-Authored-By: C <n@example.com>\" && cat > notes.md <<'EOF'\n"
    "| case | cd S && cat > {o}/json.py |\nEOF",
    "cat <<EOF > o.txt\n  EOF\ntouch {o}/f\nEOF",
]


@pytest.mark.parametrize("command", SCANNER_IN)
def test_a_body_the_old_reader_read_as_commands_is_data(env, command):
    assert _d(env, command.format(o=env.other)) is None


# ---------------------------------------------------------------- 3. the 2 s budget
def test_the_budget_is_two_seconds():
    assert sandbox_guard.PARSE_BUDGET_S == 2.0


def test_a_command_over_the_budget_is_denied_with_a_short_message(env, monkeypatch):
    monkeypatch.setattr(sandbox_guard, "PARSE_BUDGET_S", 0.05)
    t = time.monotonic()
    out = _d(env, "cd a;" * 4000 + "touch inside.txt")
    dt = time.monotonic() - t
    reason = _reason(out)
    assert "command too long or complex for the sandbox guard" in reason and "split it" in reason
    assert len(reason) < 1000 and dt < 2.0


def test_check_adversarial_r3_slow_shapes_are_decided_well_inside_the_hook_timeout(env):
    for c in ("cd a;" * 7000 + "touch %s/f" % env.other, "touch %s/f; : " % env.other + "<<A" * 20000):
        t = time.monotonic()
        out = _d(env, c)
        dt = time.monotonic() - t
        assert out is not None and dt < 5.0, (len(c), dt)


def test_a_long_run_of_heredoc_openers_is_read_in_linear_time(env):
    t = time.monotonic()
    temp_guard.strip_heredocs("touch f; : " + "<<A" * 200000)
    assert time.monotonic() - t < 1.0


# ---------------------------------------------------------------- 4. pytest with TMPDIR dropped
DROPPED = [
    "unset TMPDIR; python -m pytest t.py",
    "unset TMPDIR PYTEST_DEBUG_TEMPROOT; python -m pytest t.py",
    "unset -v TMPDIR && pytest -q t.py",
    "env -u TMPDIR python -m pytest t.py",
    "env --unset=TMPDIR pytest t.py",
    "env -u TMPDIR -u PYTEST_DEBUG_TEMPROOT python -m pytest t.py",
    "env -i PATH=/usr/bin python -m pytest t.py",
    "env - PATH=/usr/bin pytest t.py",
    "env -iu X pytest t.py",
    "env --ignore-environment pytest --basetemp=$TMPDIR/b t.py",
    "TMPDIR=$TMPDIR env -i pytest t.py",
    "bash -c 'unset TMPDIR; pytest t.py'",
]


@pytest.mark.parametrize("command", DROPPED)
def test_pytest_with_tmpdir_dropped_is_denied(env, command):
    reason = _reason(_d(env, command))
    assert "TMPDIR" in reason and "/tmp" in reason


KEPT = [
    "env -i PATH=/usr/bin TMPDIR=$TMPDIR python -m pytest t.py",
    "unset PYTEST_DEBUG_TEMPROOT; python -m pytest t.py",
    "env -u FOO python -m pytest t.py",
    "unset TMPDIR; ls",
    "(unset TMPDIR); python -m pytest t.py",
    "unset TMPDIR; TMPDIR=.tmp pytest t.py",
    "unset TMPDIR; export TMPDIR=.tmp; pytest t.py",
]


@pytest.mark.parametrize("command", KEPT)
def test_pytest_that_keeps_or_resets_tmpdir_is_not_denied_for_it(env, command):
    assert _d(env, command) is None


def test_the_reader_names_tmp_as_the_target_of_a_tmpdir_drop():
    got = sandbox_guard.bash_targets("unset TMPDIR; python -m pytest t.py", "/w", "/tmp/sbxdeadbeef")
    assert got and got[0][0] == "/tmp"


# ---------------------------------------------------------------- 5. wrappers (MINOR 8)
WRAPPED_OUT = [
    "timeout 0.5 touch {o}/f", "timeout 1.5s rm -rf {o}", "exec -a name touch {o}/f",
    "uv run --with pytest-xdist pytest --basetemp={o}/bt t.py", "uv run --python 3.12 pytest --basetemp={o}/bt t.py",
    "uv run --directory {o} touch f", "flock {o}/lock touch inside.txt", "flock -w 5 inside.lock touch {o}/f",
]
WRAPPED_IN = ["timeout 0.5 touch inside.txt", "flock inside.lock touch inside.txt",
              "uv run --with x pytest --basetemp=$TMPDIR/b t.py", "exec -a name touch inside.txt"]


@pytest.mark.parametrize("command", WRAPPED_OUT)
def test_a_wrapper_with_its_options_is_read(env, command):
    assert _d(env, command.format(o=env.other)) is not None


@pytest.mark.parametrize("command", WRAPPED_IN)
def test_a_wrapper_that_stays_inside_is_not_a_deny(env, command):
    assert _d(env, command) is None


# ---------------------------------------------------------------- 6. variables (MINOR 7 and 11)
VARS_OUT = [
    "B={o} bash -c 'rm -rf $B'",
    "env B={o} bash -c 'touch $B/f'",
    "touch ${{B:-{o}}}/f",
    "B=({o}); touch $B",
    "for B in {o}; do touch $B/f; done",
    "HOME={o}; touch ~/.claude/f",
    "HOME={o}; cd; touch f",
]
VARS_IN = [
    "B={o}; for B in inside; do touch $B; done",
    "touch ${{TMPDIR:-/tmp}}/f",
    "for f in a b; do touch $f; done",
    "coproc cd {o}; touch notes.txt",
    "B=(inside); touch $B",
    "B=inside bash -c 'touch $B/f'",
]


@pytest.mark.parametrize("command", VARS_OUT)
def test_a_variable_the_command_sets_is_followed(env, command):
    assert _d(env, command.format(o=env.other)) is not None


@pytest.mark.parametrize("command", VARS_IN)
def test_a_variable_that_stays_inside_or_cannot_be_told_is_not_a_deny(env, command):
    assert _d(env, command.format(o=env.other)) is None


# ---------------------------------------------------------------- 7. round 5 (check-adversarial-r4.md B-4, B-5, MINOR 1, 3)
#: B-4: valid bash the tokenizer cannot read (and a second line bash fails on after the first ran). A
#: conservative scan of the raw text finds the write in front of it.
UNREADABLE_OUT = [
    """touch {o}/f; echo "$(echo '"')" """,
    """touch {o}/f; echo $'it\\'s'""",
    """touch {o}/f; n="$(grep -o '"label":"[^"]*"' x.jsonl | head -1)"; echo "$n" """,
    """touch {o}/f\necho "a line bash fails on only after the touch ran""",
    """echo 'unbalanced > {o}/x""",
    """echo $'it\\'s' > {o}/f""",
    """cp a.txt {o}/; echo "$(echo '"')" """,
]
UNREADABLE_IN = [
    """touch inside.txt; echo "$(echo '"')" """,
    """echo $'it\\'s'""",
    """n="$(grep -o '"label":"[^"]*"' x.jsonl | head -1)"; echo "$n" > inside.txt""",
    """echo $'it\\'s' > /dev/null 2>&1""",
]


@pytest.mark.parametrize("command", UNREADABLE_OUT)
def test_an_unreadable_command_with_a_write_outside_is_denied(env, command):
    assert _d(env, command.format(o=env.other)) is not None


@pytest.mark.parametrize("command", UNREADABLE_IN)
def test_an_unreadable_command_that_writes_inside_or_nothing_is_not_a_deny(env, command):
    assert _d(env, command.format(o=env.other)) is None


def _expansion(n, refs):
    return "touch {o}/f; B=x; " + "B=$B$B; " * n + "C=" + "$B" * refs + "; true"


@pytest.mark.parametrize("n,refs", [(18, 20000), (15, 20000), (30, 1)])
def test_a_variable_expansion_past_its_size_cap_is_denied_quickly(env, n, refs):
    t = time.monotonic()
    out = _d(env, _expansion(n, refs).format(o="inside"))
    dt = time.monotonic() - t
    assert "too long or complex" in _reason(out) and dt < 1.0, dt


def test_the_value_caps():
    assert sandbox_guard.MAX_VALUE_BYTES == 64 * 1024 and sandbox_guard.MAX_VALUES_BYTES == 1024 * 1024


@pytest.mark.parametrize("exc", [MemoryError, RecursionError])
def test_memory_or_recursion_error_is_a_deny_in_an_enforced_session_only(project, tmp_path, monkeypatch, exc):
    def boom(*a, **k):
        raise exc()
    monkeypatch.setattr(sandbox_guard, "bash_targets", boom)
    e = fx.make(project, tmp_path)
    assert "too long or complex" in _reason(_d(e, "ls"))
    f = fx.make(project, tmp_path / "x", op="op-090", confirm_paths=False, sid="sess-cccc", claim=False)
    fx.add_task(f.conn, "t-9", "op-090", sid="sess-cccc", worker="claude-c")
    assert sandbox_guard.decide({"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "sess-cccc",
                                 "cwd": f.root}, root=f.root) is None


R5_SID = "sess-r5bb0000-0000-0000-0000-000000000000"


@pytest.mark.parametrize("gb,n", [(3, 18), (16, 19)])
def test_the_real_hook_process_denies_the_r4_expansions_within_3_s(project, tmp_path, gb, n):
    import json
    import os
    import resource
    import subprocess
    from alpaca.tests.conftest import REPO
    e = fx.make(project, tmp_path, sid=R5_SID)
    cmd = _expansion(n, 20000).format(o=e.other)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": e.sid, "cwd": e.root})
    t = time.monotonic()
    try:
        p = subprocess.run([os.path.join(REPO, "bin", "alpaca-python"), "--isolated", "-m",
                            "alpaca.hooks.sandbox_guard"], input=payload, capture_output=True, text=True,
                           cwd=e.root, timeout=60, env={**os.environ, "CLAUDE_PROJECT_DIR": e.root},
                           preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_AS, (gb * 2**30, gb * 2**30)))
    finally:
        fx.rm_real_temp(R5_SID)
    dt = time.monotonic() - t
    assert '"deny"' in p.stdout and "too long or complex" in p.stdout, (p.stdout[:200], p.stderr[-300:])
    assert dt < 3.0, dt


#: MINOR 1: TMPDIR lost in ways round 4 did not see: unexported, or set empty (tempfile skips an empty TMPDIR).
TMP_LOST = [
    "export -n TMPDIR PYTEST_DEBUG_TEMPROOT; python -m pytest t.py",
    "declare +x TMPDIR PYTEST_DEBUG_TEMPROOT; python -m pytest t.py",
    "TMPDIR= PYTEST_DEBUG_TEMPROOT= python -m pytest t.py",
    "export TMPDIR= PYTEST_DEBUG_TEMPROOT=; python -m pytest t.py",
    "env TMPDIR= PYTEST_DEBUG_TEMPROOT= python -m pytest t.py",
    "typeset +x TMPDIR; pytest t.py",
]
TMP_KEPT = [
    "export -n TMPDIR; export TMPDIR; python -m pytest t.py",
    "export -n FOO; python -m pytest t.py",
    "TMPDIR= ls",
]


@pytest.mark.parametrize("command", TMP_LOST)
def test_pytest_with_tmpdir_unexported_or_empty_is_denied(env, command):
    reason = _reason(_d(env, command))
    assert "TMPDIR" in reason and "/tmp" in reason


@pytest.mark.parametrize("command", TMP_KEPT)
def test_pytest_that_keeps_tmpdir_exported_is_not_denied(env, command):
    assert _d(env, command) is None


#: MINOR 3: the guard's own folder, written by a copy into .alpaca/ (the cache, then the guard off).
INTO_GUARD = [
    "cp -r stage/sandbox .alpaca/", "cp -r stage/sandbox .alpaca", "rsync -a stage/ .alpaca/",
    "mv stage/sandbox .alpaca/", "cp -a stage/. .alpaca/", "tar -C .alpaca -xf x.tar", "tar -xf x.tar -C .alpaca/",
    "ln -s {root}/stage/sandbox .alpaca/", "cd .alpaca && cp -r ../stage/sandbox .", "install -d .alpaca/sandbox/cache",
]
NEXT_TO_GUARD = ["cp -r stage/notes .alpaca/", "cp a.txt .", "rsync -a stage/ backup/", "cp -r stage/sandbox backup/",
                 "mkdir -p .alpaca/notes", "tar -xf x.tar"]


@pytest.fixture
def staged(env):
    import os
    os.makedirs(os.path.join(env.root, "stage", "sandbox", "cache"))
    os.makedirs(os.path.join(env.root, "stage", "notes"))
    return env


@pytest.mark.parametrize("command", INTO_GUARD)
def test_a_copy_into_the_guards_own_folder_is_denied(staged, command):
    assert _d(staged, command.format(root=staged.root)) is not None


@pytest.mark.parametrize("command", NEXT_TO_GUARD)
def test_a_copy_next_to_the_guards_folder_is_not_a_deny(staged, command):
    assert _d(staged, command.format(root=staged.root)) is None


def test_a_planted_cache_entry_is_ignored(env):
    import json
    import os
    assert sandbox.resolve(env.root, env.sid) is not None
    path = sandbox.cache_path(env.root, env.sid)
    with open(path, "w") as fh:
        json.dump({"at": time.time(), "root": env.root, "sandbox": None}, fh)
    assert sandbox.resolve(env.root, env.sid) is not None
    other = sandbox.cache_path(env.root, "sess-other")                    # another session's valid entry, copied
    sandbox.resolve(env.root, "sess-other")
    os.makedirs(os.path.dirname(other), exist_ok=True)
    with open(other, "w") as fh:
        json.dump({"at": time.time(), "root": env.root, "sid": "sess-other", "sandbox": None}, fh)
    with open(other) as src, open(path, "w") as dst:
        dst.write(src.read())
    assert sandbox.resolve(env.root, env.sid) is not None
