"""The guard hook and the readers it shares with the scans (design/research-engine/in-session-runtime.md
sections 7, 8.1 and 12.3).

With no jail a research agent runs with the session's rights. The guard (alpaca/hooks/research_guard.py)
is the PreToolUse hook that denies a subagent's call that reaches the control plane: the record, the
hooks, the settings, the engine's own code, another agent's transcript, a process that outlives the
call, a git command that writes. It does nothing for the driver's own calls and nothing without the
live marker of a run. The pool line carries the agent id (the second witness of every tool call), and
the scans that resolve a call's paths start every call from the driving session's folder, because a
subagent's shell is not kept between calls.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

from alpaca import pool
from alpaca.hooks import research_guard
from alpaca.research import paths, pathscan, session_engine as eng, session_runtime
from alpaca.tests import research_session_kit as rk


@pytest.fixture
def world(project, monkeypatch):
    w = rk.JudgeWorld(monkeypatch, project)
    # the live marker a run's start writes, and a job whose agent calls are judged
    session_runtime.mark_live(w.root, w.sid, w.SCOPE, pin=None)
    jid = w.issue("implementer")
    w.hand()
    a = w.run_agent(jid, stop=False)
    w.jid, w.agent = jid, a
    yield w
    w.close()


def _payload(w, tool, tool_input, *, agent=True, **extra):
    p = {"session_id": w.sid, "cwd": w.root, "transcript_path": w.ctx["transcript"],
         "tool_name": tool, "tool_input": tool_input}
    if agent:
        p.update(agent_id=w.agent.agent_id, agent_type="general-purpose")
    p.update(extra)
    return p


def _bash(w, command, **kw):
    return research_guard.handle(_payload(w, "Bash", {"command": command}, **kw))


def _denied(out):
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def _ok(out):
    """The call goes ahead: no output, or the allow decision that marks a subagent's Bash call."""
    return out is None or out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_guard_denies_subagent_control_plane_calls(world):
    w = world
    for command in ("bin/alpaca research status --json",
                    "cd %s && bin/alpaca task list" % w.cwd,
                    "sqlite3 %s/.alpaca/alpaca.db .tables" % w.root,
                    "python3 -c 'from alpaca import db; db.connect(\".\")'",
                    "python3 -m alpaca.hooks.subagent_stop",
                    "sudo true"):
        assert _denied(_bash(w, command)), command
    # a path of the record or of another agent's folder, named by any call
    assert _denied(research_guard.handle(_payload(w, "Read", {
        "file_path": os.path.join(w.root, ".alpaca", "research", "rr-001", "events.json")})))
    assert _denied(research_guard.handle(_payload(w, "Read", {
        "file_path": os.path.join(w.ctx["cfg"], "projects", "-proj", "other.jsonl")})))
    # a process that outlives the call, and a git command that writes a repository
    assert _denied(_bash(w, "cd %s && nohup sleep 100 &" % w.cwd))
    assert _denied(research_guard.handle(_payload(w, "Bash", {
        "command": "sleep 5", "run_in_background": True})))
    assert _denied(_bash(w, "cd %s && git commit -am x" % w.cwd))
    assert _denied(_bash(w, "cd %s && git stash" % w.cwd))
    # every denial is on the log the live round saves
    log = os.path.join(w.root, ".alpaca", "research", "live", "%s.denials.jsonl" % w.sid)
    assert len(open(log, encoding="utf-8").read().splitlines()) >= 9


def test_guard_allows_the_work_of_a_job(world):
    w = world
    for command in ("cd %s && cat src/value.txt" % w.cwd, "cd %s && git diff" % w.cwd,
                    "cd %s && git log --oneline" % w.cwd, "cd %s && ls -la" % w.cwd,
                    "cd %s && python3 -m pytest -q" % w.cwd):
        assert _ok(_bash(w, command)), command
    assert research_guard.handle(_payload(w, "Write", {
        "file_path": os.path.join(w.cwd, "src", "value.txt"), "content": "1\n"})) is None


def test_guard_allows_own_prompt_read(world):
    w = world
    prompt = jobs_prompt(w)
    assert prompt.startswith(os.path.join(w.root, ".alpaca", "research"))
    assert research_guard.handle(_payload(w, "Read", {"file_path": prompt})) is None
    assert _ok(_bash(w, "cat %s" % prompt))
    # control: another job's prompt is the control plane
    other = world.issue("notes")
    other_prompt = os.path.join(w.root, ".alpaca", "research", "rr-001", "jobs", other, "prompt.txt")
    assert _denied(research_guard.handle(_payload(w, "Read", {"file_path": other_prompt})))


def jobs_prompt(w):
    from alpaca.research import jobs
    return jobs.read_job(w.root, w.SCOPE, w.jid)["prompt_path"]


def test_guard_denies_text_source_writes(world):
    w = world
    targets = (os.path.join(w.ctx["cfg"], ".autodrive-context.%s" % w.sid),
               os.path.join(w.root, "CLAUDE.md"),
               os.path.join(w.root, ".claude", "settings.json"),
               os.path.join(w.ctx["cfg"], "settings.json"),
               os.path.join(w.ctx["cfg"], "projects", "-proj", "memory", "MEMORY.md"))
    for target in targets:
        assert _denied(research_guard.handle(_payload(w, "Write", {"file_path": target,
                                                                    "content": "x"}))), target
        assert _denied(_bash(w, "echo APPROVED >> %s" % target)), target
    # the driver's own call is never denied
    for target in targets:
        assert research_guard.handle(_payload(w, "Write", {"file_path": target, "content": "x"},
                                              agent=False)) is None


def test_guard_denies_engine_writes_and_spares_an_alpaca_target_worktree(world):
    w = world
    install = eng.install_root()
    assert _denied(research_guard.handle(_payload(w, "Write", {
        "file_path": os.path.join(install, "alpaca", "hooks", "research_guard.py"),
        "content": "x"})))
    assert _denied(research_guard.handle(_payload(w, "Write", {
        "file_path": os.path.join(install, "alpaca", "runlog", "quotes.py"), "content": "x"})))
    assert _denied(_bash(w, "cd %s && sed -i s/a/b/ %s" % (w.cwd, os.path.join(
        install, "alpaca", "research", "accept.py"))))
    # a target that is Alpaca itself: the worktree's own alpaca/research/ is its own folder
    target = os.path.join(w.cwd, "alpaca", "research", "x.py")
    assert research_guard.handle(_payload(w, "Write", {"file_path": target, "content": "x"})) is None


def test_guard_ignores_the_driver(world):
    w = world
    for command in ("bin/alpaca research status", "sqlite3 x", "git commit -am x"):
        assert _bash(w, command, agent=False) is None


def test_guard_needs_the_marker(world):
    w = world
    session_runtime.unmark_live(w.root, w.sid, w.SCOPE)
    assert _ok(_bash(w, "bin/alpaca research status"))


def test_guard_fails_open(world, monkeypatch):
    w = world
    # a payload the hook cannot read prints nothing and exits 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("{ not json"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    with pytest.raises(SystemExit) as exc:
        research_guard.main()
    assert exc.value.code in (0, None) and out.getvalue() == ""
    # a tool input of the wrong type is read as no call
    assert research_guard.handle(_payload(w, "Bash", "not a mapping")) is None


# ================================================================ the pool line and the scans
def test_pool_line_carries_agent_id():
    line = pool.line("s1", "pre", {"tool_name": "Bash", "tool_input": {"command": "ls"},
                                   "tool_use_id": "t1", "agent_id": "a1", "agent_type": "general-purpose"})
    assert tuple(line) == pool.KEYS and pool.KEYS[-2:] == ("agent_id", "agent_type")
    assert line["agent_id"] == "a1" and line["agent_type"] == "general-purpose"
    # the driver's own call carries neither
    main = pool.line("s1", "pre", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert main["agent_id"] is None and main["agent_type"] is None


def test_pathscan_reset_cwd(tmp_path):
    folder = str(tmp_path / "work")
    root = str(tmp_path)
    call = {"tool": "Bash", "kind": "shell", "input": "cat src/notes.txt", "paths": []}
    # a call that forgets its cd reads as a path under the shell's folder, outside the job's
    hits = pathscan.outside([call], cwd=folder, allowed=[folder], ignore_prefixes=(),
                            watched=(root,), reset_cwd=root)
    assert hits and hits[0]["path"] == os.path.join(root, "src", "notes.txt")
    # with the cd inside the same call it is inside the folder
    cd = {"tool": "Bash", "kind": "shell", "input": "cd %s && cat src/notes.txt" % folder, "paths": []}
    assert pathscan.outside([cd], cwd=folder, allowed=[folder], ignore_prefixes=(),
                            watched=(root,), reset_cwd=root) == []
    # the shell is not kept: a cd of one call does not carry to the next
    two = [{"tool": "Bash", "kind": "shell", "input": "cd %s" % folder, "paths": []}, call]
    assert pathscan.outside(two, cwd=folder, allowed=[folder], ignore_prefixes=(),
                            watched=(root,), reset_cwd=root)
    assert pathscan.outside(two, cwd=folder, allowed=[folder], ignore_prefixes=(),
                            watched=(root,)) == []        # the old reading keeps the shell


def test_runtime_tools_are_not_off_list(tmp_path):
    folder = str(tmp_path / "try")
    calls = [{"tool": "SubagentHandback", "kind": "other", "input": {"message": "done"}, "paths": []},
             {"tool": "ToolSearch", "kind": "other", "input": {"query": "x"}, "paths": []}]
    allow = ("cat", "ls", "cd")
    assert pathscan.shell_allowed(calls, allow=allow, try_folder=folder, client=None)
    assert pathscan.shell_allowed(calls, allow=allow, try_folder=folder, client=None,
                                  runtime_tools=session_runtime.RUNTIME_TOOLS) == []
    # any other call of kind other is still a hit
    other = [{"tool": "Skill", "kind": "other", "input": {}, "paths": []}]
    assert pathscan.shell_allowed(other, allow=allow, try_folder=folder, client=None,
                                  runtime_tools=session_runtime.RUNTIME_TOOLS)


def test_control_hits_names_and_paths(world):
    w = world
    here = w.root
    allowed = (w.cwd,)

    def hits(command):
        call = {"tool": "Bash", "kind": "shell", "input": command, "paths": []}
        return eng.control_hits([call], here=here, root=w.root, allowed=allowed)

    assert hits("bin/alpaca research status") and hits("sqlite3 .alpaca/alpaca.db .tables")
    assert hits("cat .alpaca/alpaca.db") and hits("python3 -c 'import alpaca'")
    assert hits("git commit -m x") and hits("sudo ls")
    # the text of a file a call writes is not a name that runs: a pack whose own code says sqlite3
    write = {"tool": "Write", "kind": "write", "input": {"file_path": os.path.join(w.cwd, "t.py"),
                                                           "content": "import sqlite3\n"},
             "paths": [os.path.join(w.cwd, "t.py")]}
    assert eng.control_hits([write], here=here, root=w.root, allowed=allowed) == []
    # the verifier's request client carries a command the driver runs, not this shell
    client = {"tool": "Bash", "kind": "shell",
              "input": "cd %s && python3 packet/bin/rerun c1 --shell 'python3 -m alpaca doctor'" % w.cwd,
              "paths": []}
    assert eng.control_hits([client], here=here, root=w.root, allowed=allowed) == []
    # a read under another agent's transcript folder is a hit, a persisted output is not
    read = {"tool": "Read", "kind": "read", "input": {"file_path": os.path.join(
        w.ctx["cfg"], "projects", "-proj", "x.jsonl")}, "paths": [os.path.join(
            w.ctx["cfg"], "projects", "-proj", "x.jsonl")]}
    assert eng.control_hits([read], here=here, root=w.root, allowed=allowed)
    announced = os.path.join(w.ctx["cfg"], "projects", "-proj", "s", "tool-results", "o.txt")
    read2 = dict(read, paths=[announced], input={"file_path": announced})
    assert eng.control_hits([read2], here=here, root=w.root, allowed=allowed + (announced,)) == []


# ================================================================ the import path and the engine pin
def test_guard_denies_root_module_writes(world):
    """A module an agent writes at the top of the engine's root is on the import path of any process
    that starts from there, so the guard denies the write whatever tool names it."""
    w = world
    install = eng.install_root()
    for target in (os.path.join(install, "json.py"), os.path.join(install, "brotli", "__init__.py"),
                   os.path.join(install, "sitecustomize.py")):
        assert _denied(research_guard.handle(_payload(w, "Write", {"file_path": target,
                                                                    "content": "x"}))), target
        assert _denied(_bash(w, "echo 'import os' > %s" % target)), target
    # control: a file of the job's own folder is not an import root
    own = os.path.join(w.cwd, "json.py")
    assert research_guard.handle(_payload(w, "Write", {"file_path": own, "content": "x"})) is None


def test_guard_follows_the_cd_of_the_same_call(world):
    """op-009 t-248 (rr-010 F40): a write after `cd <own folder> &&` lands in that folder, not in
    the folder the call starts in, and echo's words name no path; a write the cd does not move
    (a subshell's cd, `cd -`, a cd that may fail) is still read in the call's start folder. The
    guard reads a call against the project its payload cwd names, so the relative cases are read
    with control_hits started in the install root, where a top-level module is an import-root
    write."""
    w = world
    install = eng.install_root()
    own = w.cwd
    for command in ("mkdir -p %s && cd %s && cat > proto.py <<'EOF'\nx = 1\nEOF" % (own, own),
                    "cd %s && sed -i s/a/b/ cm2.py" % own,
                    "echo 'see pkg/foo.py' | python3 -c 'pass'"):
        assert _ok(_bash(w, command)), command
    assert _denied(_bash(w, "cd %s && echo 'import os' > %s"
                         % (own, os.path.join(install, "sitecustomize.py"))))

    def hits(command):
        return eng.control_hits([{"kind": "shell", "tool": "Bash", "input": {"command": command}}],
                                here=install, root=w.root, allowed=(own,))
    for command in ("mkdir -p %s && cd %s && cat > proto.py <<'EOF'\nx = 1\nEOF" % (own, own),
                    "cd %s && sed -i s/a/b/ cm2.py" % own):
        assert hits(command) == [], command
    for command in ("cat > json.py", "cd /tmp && cd - && cat > json.py",
                    "(cd %s); cat > json.py" % own, "cd /does-not-exist-t248; cat > json.py",
                    "cd %s || cat > json.py" % own):
        assert hits(command), command


def test_guard_denies_a_write_to_a_lazy_module(world):
    w = world
    install = eng.install_root()
    for rel in (("alpaca", "runlog", "quotes.py"), ("alpaca", "runlog", "reviews.py"),
                ("alpaca", "research", "accept.py"), ("alpaca", "formation", "manifest.py")):
        target = os.path.join(install, *rel)
        assert _denied(research_guard.handle(_payload(w, "Write", {"file_path": target,
                                                                    "content": "x"}))), target
        assert _denied(_bash(w, "cd %s && sed -i s/a/b/ %s" % (w.cwd, target))), target


def test_guard_stat_pass_denies_the_next_call(project, monkeypatch):
    """A file touched in the pinned engine makes the guard deny the next call of a subagent and
    write engine-changed.json, which the runner's watch reads."""
    w = rk.JudgeWorld(monkeypatch, project, pinned=True)
    try:
        jid = w.issue("implementer")
        w.hand()
        a = w.run_agent(jid, stop=False)
        w.jid, w.agent = jid, a
        # the live marker that register_driver wrote carries the pin
        assert os.path.isfile(session_runtime.live_path(w.root, w.sid))
        assert _ok(_bash(w, "cd %s && ls" % w.cwd))
        os.utime(os.path.join(os.path.realpath(eng.install_root()), "bin", "alpaca"), None)
        out = _bash(w, "cd %s && ls" % w.cwd)
        assert _denied(out) and "the run is ending" in out["hookSpecificOutput"]["permissionDecisionReason"]
        assert os.path.isfile(os.path.join(paths.run_dir(w.root, w.SCOPE), "engine-changed.json"))
    finally:
        w.close()


# ------------------------------------------------------------------ a subagent never drives a run
# No environment variable tells a driver's shell from a subagent's (CLAUDE_CODE_CHILD_SESSION is set
# in both, .alpaca/proofs/op-009/t-173.logs/live-round/env-measure.txt), so the rule sits at the tool
# boundary: the payload of a subagent's call carries agent_id, the driver's does not.
DRIVING = ("bin/alpaca research start --runbook r.yaml",
           "bin/alpaca --session abc research resume rr-001",
           "./bin/alpaca research extend rr-001",
           "bin/alpaca research stop rr-001",
           "bin/alpaca research next-job rr-001 --wait 0",
           "bin/alpaca research submit j-1 --nonce n --agent-id a",
           "bin/alpaca proof verify t-001",
           "alpaca research start --runbook r.yaml",
           "python3 -m alpaca research start --runbook r.yaml",
           "python3 -m alpaca --session abc proof verify t-001",
           "bin/alpaca-python --isolated -m alpaca research next-job rr-001",
           "cd /repo && env X=1 /repo/bin/alpaca research submit j-1 --nonce n --agent-id a",
           "bash -c 'bin/alpaca research start --runbook r.yaml'",
           "python3 -c \"import subprocess; subprocess.run(['bin/alpaca', 'research', 'start'])\"")
NOT_DRIVING = ("bin/alpaca research status --json", "bin/alpaca task list", "bin/alpaca proof new t-1",
               "bin/alpaca research report rr-001", "echo start research", "ls bin/alpaca")


def _no_marker(w):
    path = os.path.join(w.root, ".alpaca", "research", "live", "%s.json" % pool.slug(w.sid))
    assert os.path.isfile(path)
    os.remove(path)


def _reason(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("live", [True, False])
def test_guard_denies_every_driving_verb_for_a_subagent_and_allows_the_driver(world, live):
    w = world
    if not live:
        _no_marker(w)
    for command in DRIVING:
        out = _bash(w, command)
        assert _denied(out), (live, command)
        assert "RUN-DRIVER-IS-SUBAGENT" in _reason(out), out
        # the driver's own call (no agent_id) is never denied by this rule
        assert _bash(w, command, agent=False) is None, (live, command)
    if live:
        # the denials are on the log the live round saves
        log = os.path.join(w.root, ".alpaca", "research", "live", "%s.denials.jsonl" % w.sid)
        assert len(open(log, encoding="utf-8").read().splitlines()) == len(DRIVING)


def test_the_driving_rule_without_a_marker_leaves_the_other_calls_alone(world):
    w = world
    _no_marker(w)
    # with no live run the control-plane rules do not apply, and a verb that does not drive a run is
    # no hit of the driving rule
    for command in NOT_DRIVING:
        assert _ok(_bash(w, command)), command


def test_driving_verb_hit_reads_the_command_not_the_words(world):
    def hit(text):
        return eng.driving_verb_hit({"tool": "Bash", "kind": "shell", "input": text})
    assert hit("bin/alpaca research start --runbook r.yaml") == "alpaca research start"
    assert hit("python3 -m alpaca --session s proof verify t-1") == "alpaca proof verify"
    assert hit("bin/alpaca research next-job rr-1") == "alpaca research next-job"
    for command in NOT_DRIVING:
        assert hit(command) is None, command
    # a call that is not a shell call, and a text that does not parse, name nothing
    assert eng.driving_verb_hit({"tool": "Read", "kind": "read", "input": {}}) is None


def test_the_guard_stays_fail_open_on_a_parse_failure(world, monkeypatch):
    w = world
    _no_marker(w)

    def boom(call):
        raise RuntimeError("parse failure")
    monkeypatch.setattr(eng, "driving_verb_hit", boom)
    assert _ok(_bash(w, "bin/alpaca research start --runbook r.yaml"))


#: forms the parse of the guard does not open, or that name the entry point another way (check round
#: s1, adversarial B1 and B2); each one runs a driving verb
TEXT_FORMS = (
    "bin/alpaca --sess abc research start --runbook r.yaml",
    "python3 -m alpaca --s abc research stop rr-001",
    "echo $(bin/alpaca research start --runbook r.yaml)",
    "echo `bin/alpaca research stop rr-001`",
    "eval \"bin/alpaca research start --runbook r.yaml\"",
    "echo 'bin/alpaca research next-job rr-001' | bash",
    "echo start | xargs bin/alpaca research",
    "timeout 9 bash -c 'bin/alpaca research stop rr-001'",
    "printf 'bin/alpaca research stop rr-001' > x.sh && bash x.sh",
    "A=bin/alpaca; $A research start --runbook r.yaml",
    "echo $(bin/alpaca proof verify t-001)",
    "/r/.venv/bin/python -P alpaca/__main__.py research stop rr-001",
    "python3 /repo/alpaca/__main__.py --session abc proof verify t-001",
    "python3 -c \"import runpy,sys; sys.argv=['a']+'research stop rr-001'.split(); "
    "runpy.run_module('alpaca', run_name='__main__')\"",
)


@pytest.mark.parametrize("live", [True, False])
def test_the_guard_denies_the_textual_forms_of_a_driving_verb(world, live):
    w = world
    if not live:
        _no_marker(w)
    for command in TEXT_FORMS:
        out = _bash(w, command)
        assert _denied(out), (live, command)
        assert "RUN-DRIVER-IS-SUBAGENT" in _reason(out), (command, out)
        assert _bash(w, command, agent=False) is None, command


#: x-args forms that name the driver's entry: each stays a driving hit (t-356, 27.3)
XARGS_ENTRY = (
    'echo start | xargs bin/alpaca research',  # bin entry
    'echo start | xargs python3 -m alpaca research',  # module entry
    'echo start | xargs python3 -m alpaca.cli research',  # module .cli
    'echo stop | xargs python3 -m alpaca.__main__ research',  # module .__main__
    'echo stop | xargs python3 alpaca/__main__.py research',  # entry file
    'echo stop | xargs python3 alpaca/ research',  # package folder, slash
    'echo start | xargs python3 x/../alpaca/. research',  # package folder, dot
    'echo start | xargs alpaca research',  # bare entry on PATH
    'B=bin/alpaca; echo start | xargs $B research',  # variable entry
    'echo start | xargs "$P"/bin/alpaca research',  # quoted parts
    'echo verify t-1 | xargs bin/alpaca proof',  # proof verb
    'python3 - <<\'EOF\'\nimport subprocess\nsubprocess.run("""echo start |\nxargs bin/alpaca research""", shell=True)\nEOF',  # python string over lines
    'python3 -c "import subprocess; subprocess.run([\'xargs\',\'bin/alpaca\',\'research\'], input=b\'start\')"',  # subprocess list
    "cat <<'EOF' | sh\necho start |\nxargs bin/alpaca research\nEOF",  # pipe into sh over lines
)
#: x-args words next to a module or a folder of the package, never its entry: no hit (t-356, 27.3).
#: Row 0 is t-351 attack a21 test 1 byte for byte.
XARGS_NOT_ENTRY = (
    'cd /r && .venv/bin/python - <<\'PYEOF\'\nfrom alpaca.research import session_engine\nc = "xargs -a s/list cp -t ."\n# start\nPYEOF',  # a21: package.module
    'git ls-files alpaca/research | xargs grep -n "def stop"',  # package/dir path
    'ls .alpaca | xargs grep -c stop research.log',  # dot-prefixed folder
    'ls docs/alpaca-research | xargs grep -l verify',  # hyphen name after
    'ls my-alpaca && cat research.md | xargs grep stop',  # hyphen name before
    'ls myalpaca | xargs grep stop research.md',  # word before
)
#: the package folder or the package itself as a bare word is an entry word, so these stay x-args
#: hits by design (27.3, residuals)
XARGS_PACKAGE_WORD = (
    "cd /r/alpaca && git ls-files research | xargs grep -n 'start'",
    "python3 - <<'EOF'\nfrom alpaca import cli\nfrom alpaca.research import session_engine\n"
    "args = ['xargs', '-n1']\n# then start the job\nEOF",
)


def test_the_xargs_form_needs_a_word_that_names_the_entry(world):
    # t-356 (27.3): the package counts for the x-args rule only as a word that names the entry
    def hit(text, tool="Bash"):
        return eng.driving_verb_hit({"tool": tool, "kind": "shell", "input": text})
    for command in XARGS_ENTRY + XARGS_PACKAGE_WORD:
        assert (hit(command) or "").startswith("alpaca via xargs "), command
    for command in XARGS_NOT_ENTRY:
        assert hit(command) is None, command
    # one function for every shell tool: the guard entry and the driver audit read through it
    for tool in ("Monitor", "PowerShell", "mcp__terminal__run_in_terminal", "command_execution"):
        assert hit(XARGS_NOT_ENTRY[0], tool) is None, tool
        assert (hit(XARGS_ENTRY[0], tool) or "").startswith("alpaca via xargs "), tool
    # at the guard: a21's call goes through, the x-args driving call is still denied
    w = world
    _no_marker(w)
    assert _ok(_bash(w, XARGS_NOT_ENTRY[0]))
    out = _bash(w, XARGS_ENTRY[0])
    assert _denied(out) and "RUN-DRIVER-IS-SUBAGENT" in _reason(out), out


#: a subprocess driving call after an escaped quote in an unquoted heredoc, where bash removes the
#: backslash before the program reads the body (t-356 check round s2, N2-ESC): the reader reads
#: no program language, so the whole text keeps it a hit (27.3, Reach)
ESCAPED_HEREDOC_DRIVING = (
    'python3 - <<EOF\ns = "a\\\\" # " ; __import__("os").system("bin/alpaca research start") #"\nEOF',
    'node - <<EOF\nvar s = "a\\\\" // " ; require("child_process").execSync("bin/alpaca research start") //"\nEOF',
)


def test_an_escape_in_an_unquoted_heredoc_hides_no_driving_call(world):
    for command in ESCAPED_HEREDOC_DRIVING:
        assert eng.driving_verb_hit({"tool": "Bash", "kind": "shell", "input": command}) \
            == "alpaca research start", command
        out = _bash(world, command)
        assert _denied(out) and "RUN-DRIVER-IS-SUBAGENT" in _reason(out), (command, out)


def test_the_guard_reads_every_tool_that_takes_a_command(world):
    # Monitor, PowerShell and a terminal tool run the same shell (check round s1, adversarial B4)
    w = world
    shells = ("Bash", "Monitor", "PowerShell", "mcp__terminal__run_in_terminal")
    for tool in shells:
        out = research_guard.handle(_payload(w, tool, {"command": "bin/alpaca research stop rr-001"}))
        assert _denied(out) and "RUN-DRIVER-IS-SUBAGENT" in _reason(out), tool
        assert research_guard.handle(
            _payload(w, tool, {"command": "bin/alpaca research stop rr-001"}, agent=False)) is None
    # the world's implementer is given Read, Write, Edit and Bash: the other shell tools are off its
    # list and get the tool step of section 26 (t-359), before any scan
    out = research_guard.handle(_payload(w, "Monitor", {"command": "ls bin"}))
    assert _denied(out) and _reason(out).startswith("JOB-TOOL Monitor:"), out
    # a job whose list gives those tools still has each shell call read by the scans
    jid = w.issue("implementer", key="%s/implementer/t2" % w.attempt,
                  tools=("Read", "Write", "Edit") + shells)
    w.hand()
    other = rk.JudgeWorld.agent(w, jid)        # the fixture's `w.agent` holds its first agent
    other.start()
    other.work()
    other.write()
    for tool in shells:
        assert _ok(research_guard.handle(_payload(w, tool, {"command": "ls bin"},
                                                  agent_id=other.agent_id))), tool
    # a tool that carries no command is not read as a shell call
    assert research_guard._call_of("Read", {"file_path": "x"})["kind"] == "read"
    assert research_guard._call_of("Grep", {"command": "bin/alpaca research stop"})["kind"] == "read"


def test_the_scans_flag_a_driving_verb_in_a_text_the_guard_cannot_parse(world):
    # rule S15 and K15 read the transcript with `control_hits`: a call that runs the control plane
    # in a form the guard does not see is still flagged, `alpaca proof verify` included (check round
    # s1, adversarial B3)
    w = world
    for text in ("python3 -c \"import subprocess as s; s.run(['bin/alpaca','research','start'])\"",
                 "echo $(bin/alpaca proof verify t-001)",
                 "eval \"bin/alpaca proof verify t-001\"",
                 "echo 'bin/alpaca proof verify t-001' | bash",
                 "timeout 9 bash -c 'bin/alpaca proof verify t-001'",
                 "cd %s && python3 -m alpaca proof verify t-1" % w.cwd):
        calls = [{"tool": "Bash", "kind": "shell", "input": text}]
        hits = eng.control_hits(calls, here=w.cwd, root=w.root)
        assert hits and hits[0]["why"].startswith("runs "), text


def test_guard_allows_a_write_in_the_jobs_own_scratch_folder_and_denies_another_jobs(world):
    # t-191 F29: each job has a private scratch folder under its own job folder, which lies under
    # the research folders the guard treats as the control plane; the job's own scratch is one of
    # its own paths, another job's folder is not
    w = world
    mine = paths.job_scratch(w.root, w.SCOPE, w.jid)
    assert os.path.isdir(mine)
    target = os.path.join(mine, "proto.py")
    assert research_guard.handle(_payload(w, "Write", {"file_path": target, "content": "x"})) is None
    assert _ok(_bash(w, "cd %s && python3 %s" % (w.cwd, target)))
    assert research_guard.handle(_payload(w, "Read", {"file_path": target})) is None
    other = w.issue("notes", try_n=9)
    foreign = os.path.join(paths.job_scratch(w.root, w.SCOPE, other), "proto.py")
    assert _denied(research_guard.handle(_payload(w, "Write", {"file_path": foreign,
                                                              "content": "x"})))
    assert _denied(research_guard.handle(_payload(w, "Read", {"file_path": foreign})))


# ================================================================ t-351 F48 (ii): a denied call is a call that did not run
#
# rr-021: the guard denied 14 of the 17 binding S15 candidates before they ran, but a plain-string
# denial left no `research-guard-deny` event, so the audit read every denied call as one that ran and
# broke three honest attempts. A job whose role is not guarded now gets the event for each string
# denial; jobs.ran_calls turns the call into a stub by tool use id, input hash and deny text hash.
from alpaca.research import events as rev, jobs                                # noqa: E402

DENIED_CALLS = {
    "control-plane": "cd {cwd} && cp {cwd}/src/a.py y.py && cat > .claude/settings.json <<'EOF'\n{{}}\nEOF",
    "control-plane-abs": "cat > {root}/rt_t351.py <<'EOF'\nx = 1\nEOF",
    "background": "cd {cwd} && nohup sleep 100 &",
    "driver": "cd {cwd} && bin/alpaca research next-job rr-001",
}


def _calls_of(w):
    return {k: v.format(cwd=w.cwd, root=eng.install_root()) for k, v in DENIED_CALLS.items()}


def _deny_events(w):
    return jobs.deny_events(w.conn, w.jid, w.agent.agent_id)


def _result_call(command, tid, out=None, ran_output="ok"):
    """The call as launcher.tool_calls reads it from the agent transcript: a denied call's result is
    the hook-error text the runtime writes, a call that ran has its own output."""
    if out is not None and _denied(out):
        output, error = "PreToolUse:Bash hook error: %s" % _reason(out), True
    else:
        output, error = ran_output, False
    return {"tool": "Bash", "kind": "shell", "id": tid, "input": command,
            "raw_input": {"command": command}, "paths": [], "text": "", "output": output,
            "error": error}


def test_every_string_denial_of_an_implementer_writes_an_event_and_the_call_reads_as_not_run(world):
    w = world
    for n, (rule, command) in enumerate(sorted(_calls_of(w).items())):
        tid = "toolu_s%d" % n
        out = _bash(w, command, tool_use_id=tid)
        assert _denied(out), (rule, _reason(out) if out else None)
        ev = [e for e in _deny_events(w) if e["data"]["tool_use_id"] == tid]
        assert len(ev) == 1, rule
        data = ev[0]["data"]
        assert data["rule"] in jobs.DENY_EVENT_RULES and data["role"] == "implementer", data
        assert data["job"] == w.jid and data["agent_id"] == w.agent.agent_id and data["tool"] == "Bash"
        assert data["input_sha256"] == jobs.call_sha256({"command": command})
        assert data["reason_sha256"] == jobs.text_sha256(_reason(out))
        assert data["n"] == len(_deny_events(w))
        ran = jobs.ran_calls(w.conn, w.jid, w.agent.agent_id, [_result_call(command, tid, out)])
        assert ran[0]["kind"] == "denied", rule
    assert {e["data"]["rule"] for e in _deny_events(w)} == {"control-plane", "background", "driver"}
    # the denials log the live round keeps still gets a line for each
    log = os.path.join(w.root, ".alpaca", "research", "live", "%s.denials.jsonl" % w.sid)
    assert len(open(log, encoding="utf-8").read().splitlines()) == len(DENIED_CALLS)


def test_the_event_alone_makes_no_stub_a_result_that_is_not_the_hook_error_text_stays_a_call_that_ran(world):
    w = world
    command = _calls_of(w)["control-plane"]
    out = _bash(w, command, tool_use_id="toolu_x")
    assert _denied(out)
    # the decision never reached the runtime: the call ran and printed its own output
    ran = _result_call(command, "toolu_x", ran_output="")
    assert jobs.ran_calls(w.conn, w.jid, w.agent.agent_id, [ran])[0]["kind"] == "shell"
    # the runtime cut the text by one character: not the guard's reason, so not a stub
    bent = _result_call(command, "toolu_x", out)
    bent["output"] = bent["output"][:-1]
    assert jobs.ran_calls(w.conn, w.jid, w.agent.agent_id, [bent])[0]["kind"] == "shell"
    # the same text under another tool use id is not the denied call
    other = _result_call(command, "toolu_other", out)
    assert jobs.ran_calls(w.conn, w.jid, w.agent.agent_id, [other])[0]["kind"] == "shell"


def test_a_stubbed_denial_never_hides_a_later_write_of_the_project_root_that_ran(world):
    w = world
    install = eng.install_root()
    root_mod = os.path.join(install, "rt_t351.py")
    c1 = _calls_of(w)["control-plane"]
    out1 = _bash(w, c1, tool_use_id="toolu_a1")
    c2 = "cat > %s <<'EOF'\nx = 1\nEOF" % root_mod
    calls = [_result_call(c1, "toolu_a1", out1), _result_call(c2, "toolu_a2", ran_output=""),
             _result_call(c1, "toolu_a3", ran_output="")]
    ran = jobs.ran_calls(w.conn, w.jid, w.agent.agent_id, calls)
    assert [c["kind"] for c in ran] == ["denied", "shell", "shell"]
    hits = eng.control_hits(ran, here=w.root, root=w.root, allowed=(w.cwd,))
    assert sorted({h["line"] for h in hits}) == [2, 3], hits
    assert any("rt_t351.py" in h["why"] for h in hits if h["line"] == 2)


def test_past_the_cap_an_implementer_is_still_denied_with_its_own_text_and_no_event(world):
    w = world
    for n in range(jobs.GUARD_DENY_CAP):
        rev.emit(w.conn, rev.GUARD_DENY, run=None, ref=w.jid, op=None, session=w.sid,
                 actor=jobs.GUARD_ACTOR, data={"job": w.jid, "agent_id": w.agent.agent_id,
                                               "tool": "Bash", "tool_use_id": "old%d" % n,
                                               "rule": "control-plane", "n": n + 1})
    command = _calls_of(w)["control-plane-abs"]
    out = _bash(w, command, tool_use_id="toolu_cap")
    assert _denied(out)
    text = _reason(out)
    assert text.startswith("this call writes") and "never reaches the control plane" in text, text
    assert "deny-cap" not in text and "pass is void" not in text and "AUDIT" not in text
    assert len(_deny_events(w)) == jobs.GUARD_DENY_CAP
    # no event: the call reads as run, which fails closed
    ran = jobs.ran_calls(w.conn, w.jid, w.agent.agent_id, [_result_call(command, "toolu_cap", out)])
    assert ran[0]["kind"] == "shell"


def test_a_string_denial_with_no_marker_or_no_job_keeps_the_string_and_writes_no_event(world):
    w = world
    _no_marker(w)
    out = _bash(w, _calls_of(w)["driver"], tool_use_id="toolu_nm")
    assert _denied(out) and "RUN-DRIVER-IS-SUBAGENT" in _reason(out)
    assert _deny_events(w) == []


def test_the_control_plane_deny_text_names_the_absolute_form_and_the_construct(world):
    w = world
    first = "this call names %s; a research job never reaches the control plane" % os.path.join(
        w.root, ".claude", "settings.json")
    want = os.path.join(w.cwd, ".claude", "settings.json")
    heredoc = "cat > .claude/settings.json <<'EOF'\n{}\nEOF"
    for chain, construct in (("cp %s/src/a.py y.py && " % w.cwd, "a `cp` call"),
                             ("ls; ", "a `;`"), ("ls\n", "a line break"),
                             ("echo $HOME && ", "a `$` in the call"),
                             ("cd %s && " % w.cwd, "a second `cd`")):
        command = "cd %s && %s%s" % (w.cwd, chain, heredoc)
        text = _reason(_bash(w, command, tool_use_id="toolu_t"))
        assert text.startswith(first + ". The relative name was read in %s, where the call starts, "
                               "because %s came before it in the cd chain; the same file by its "
                               "absolute path is %s." % (w.root, construct, want)), (construct, text)
        # a pure function of the call: the audit matches the text by its hash
        assert _reason(_bash(w, command, tool_use_id="toolu_u")) == text
    # a call with no cd at all and an absolute root write get the first sentence and nothing else,
    # and no root path is ever offered as the form to use
    for command in ("cat > .claude/settings.json <<'EOF'\n{}\nEOF", _calls_of(w)["control-plane-abs"]):
        text = _reason(_bash(w, command, tool_use_id="toolu_v"))
        assert text.endswith("a research job never reaches the control plane"), text
    # a cd folder outside the job's own folders is not offered either
    outside = os.path.join(w.root, "elsewhere")
    os.makedirs(outside, exist_ok=True)
    text = _reason(_bash(w, "cd %s && ls; %s" % (outside, heredoc), tool_use_id="toolu_w"))
    assert text.endswith("a research job never reaches the control plane"), text


def test_the_cd_reader_names_the_construct_and_accepts_the_same_folders(world):
    w = world
    from alpaca.research import pathscan
    chains = ["cd %s && cat > a.txt <<'EOF'\nx\nEOF" % w.cwd,
              "cd %s && cp x y && cat > a.txt <<'EOF'\nx\nEOF" % w.cwd,
              "mkdir -p %s/sub && cd %s/sub && ls; cat > a.txt <<'EOF'\nx\nEOF" % (w.cwd, w.cwd),
              "cd %s && ls || cat > a.txt <<'EOF'\nx\nEOF" % w.cwd,
              "cd %s && cd %s && cat > a.txt <<'EOF'\nx\nEOF" % (w.cwd, w.cwd),
              "cd '%s' && cat > a.txt <<'EOF'\nx\nEOF" % w.cwd,
              "cd %s/../x && cat > a.txt <<'EOF'\nx\nEOF" % w.cwd,
              "ls && cat > a.txt <<'EOF'\nx\nEOF"]
    got = []
    for text in chains:
        parsed = pathscan.unwrap(pathscan.parse(text))
        folders, note = eng._folders_why(text, parsed, w.root)
        assert folders == eng._folders(text, parsed, w.root)
        got.append((note["folder"] is not None, [bool(x) for x in note["why"]][-1]))
        assert len(note["why"]) == len(folders)
    # the last command (the heredoc write) is placed in the cd folder alone only in the first chain;
    # a call with no cd at all has no construct to name
    assert got[0] == (True, False)
    assert [flag for _have, flag in got[1:]] == [True] * 6 + [False]


# ================================================================ t-351 F47: an expired job runs no call
#
# rr-021 t-344: the agent of an expired try kept its Write tool and put a file into the attempt
# worktree that the next try then found. The guard now denies every call of a job that has a
# `research-job-expired` event (state read from the record, never from the agent cache), also for the
# straggler of an earlier driver session whose own marker is gone.
EXPIRED_TEXT = ("JOB-EXPIRED: job %s has expired and no longer runs; end your turn now with no "
                "further call")


def _expire(w):
    assert jobs.expire(w.conn, w.root, w.SCOPE, w.jid, "context", session=w.sid)


def _write_call(w, name="toy.py", tid="toolu_w"):
    return _payload(w, "Write", {"file_path": os.path.join(w.cwd, "src", name), "content": "x"},
                    tool_use_id=tid)


def test_the_guard_denies_every_call_of_an_expired_job_read_from_the_record(world):
    w = world
    # the first call is allowed and fills the agent cache (a copy of job.json with no state)
    assert not _denied(_bash(w, "cd %s && ls" % w.cwd, tool_use_id="toolu_0"))
    cache = os.path.join(w.root, ".alpaca", "research", "live", "agents", "%s.json" % w.agent.agent_id)
    assert os.path.isfile(cache)
    _expire(w)
    texts = set()
    for n, payload in enumerate((
            _payload(w, "Bash", {"command": "cd %s && cat > %s/src/toy.py <<'EOF'\nx\nEOF" % (w.cwd, w.cwd)},
                     tool_use_id="toolu_1"),
            _write_call(w, tid="toolu_2"),
            _payload(w, "Read", {"file_path": os.path.join(w.cwd, "src", "value.txt")},
                     tool_use_id="toolu_3"),
            _payload(w, "Glob", {"pattern": "*"}, tool_use_id="toolu_4"))):
        out = research_guard.handle(payload)
        assert _denied(out), payload["tool_name"]
        texts.add(_reason(out))
    assert texts == {EXPIRED_TEXT % w.jid}
    ev = _deny_events(w)
    assert [e["data"]["rule"] for e in ev] == ["expired"] * 4
    assert {e["data"]["tool"] for e in ev} == {"Bash", "Write", "Read", "Glob"}
    assert ev[0]["data"]["reason_sha256"] == jobs.text_sha256(EXPIRED_TEXT % w.jid)


def test_an_open_job_is_untouched_by_the_expired_rule(world):
    w = world
    assert not _denied(_bash(w, "cd %s && ls" % w.cwd, tool_use_id="toolu_o1"))
    assert not _denied(research_guard.handle(_write_call(w, tid="toolu_o2")))
    assert _deny_events(w) == []
    # another job's expiry does not reach this one
    other = w.issue("notes", try_n=7)
    assert jobs.expire(w.conn, w.root, w.SCOPE, other, "context", session=w.sid)
    assert not _denied(_bash(w, "cd %s && ls" % w.cwd, tool_use_id="toolu_o3"))


def test_the_expired_deny_leaves_a_stub_record_and_no_text_depends_on_time(world):
    w = world
    _expire(w)
    command = "cd %s && cat > %s/src/toy.py <<'EOF'\nx\nEOF" % (w.cwd, w.cwd)
    out = _bash(w, command, tool_use_id="toolu_e1")
    assert _denied(out)
    ran = jobs.ran_calls(w.conn, w.jid, w.agent.agent_id,
                         [_result_call(command, "toolu_e1", out)])
    assert ran[0]["kind"] == "denied"
    assert _reason(_bash(w, command, tool_use_id="toolu_e2")) == _reason(out)


NEW_DRIVER = "c0ffee00-1111-4222-8333-444455556666"


def _resume_in_another_session(w):
    session_runtime.unmark_live(w.root, w.sid, w.SCOPE)
    session_runtime.mark_live(w.root, NEW_DRIVER, w.SCOPE, pin=None)


def test_a_straggler_of_an_old_driver_session_is_denied_when_its_job_expired(world):
    w = world
    command = "cd %s && cat > %s/src/toy.py <<'EOF'\nx\nEOF" % (w.cwd, w.cwd)
    cache = os.path.join(w.root, ".alpaca", "research", "live", "agents", "%s.json" % w.agent.agent_id)
    # 1. the agent made a call before the resume, so its cache exists
    assert not _denied(_bash(w, "cd %s && ls" % w.cwd, tool_use_id="toolu_s0"))
    assert os.path.isfile(cache)
    _expire(w)
    _resume_in_another_session(w)
    out = _bash(w, command, tool_use_id="toolu_s1")
    assert _denied(out) and _reason(out) == EXPIRED_TEXT % w.jid
    assert [e["data"]["rule"] for e in _deny_events(w)] == ["expired"]
    # 2. no cache: the job comes from the job line of the agent's transcript
    os.remove(cache)
    out = _bash(w, command, tool_use_id="toolu_s2")
    assert _denied(out) and _reason(out) == EXPIRED_TEXT % w.jid
    assert [e["data"]["rule"] for e in _deny_events(w)] == ["expired", "expired"]


def test_a_straggler_is_left_alone_when_its_job_is_open_or_no_marker_is_live_or_no_job_is_found(world):
    w = world
    command = "cd %s && cat > %s/src/toy.py <<'EOF'\nx\nEOF" % (w.cwd, w.cwd)
    cache = os.path.join(w.root, ".alpaca", "research", "live", "agents", "%s.json" % w.agent.agent_id)
    # the job is open: no verdict, as before
    _resume_in_another_session(w)
    assert _ok(_bash(w, command, tool_use_id="toolu_n1"))
    # the job expired and no marker of any session is live: the transcript of an agent with no
    # cache is not read at all, so there is no verdict, as before
    _expire(w)
    session_runtime.unmark_live(w.root, NEW_DRIVER, w.SCOPE)
    if os.path.isfile(cache):
        os.remove(cache)
    assert research_guard.handle(_payload(w, "Bash", {"command": command}, tool_use_id="toolu_n2")) is None
    # a live marker exists, but the agent head names no job of any live scope: no verdict
    session_runtime.mark_live(w.root, NEW_DRIVER, w.SCOPE, pin=None)
    stranger = _payload(w, "Bash", {"command": command}, tool_use_id="toolu_n3")
    stranger["agent_id"] = "a-nobody-0000"
    assert research_guard.handle(stranger) is None
    assert _deny_events(w) == []


def test_the_expired_read_failing_lets_the_call_through(world, monkeypatch):
    w = world
    _expire(w)

    def boom(*a, **k):
        raise RuntimeError("no record")
    monkeypatch.setattr(jobs, "job_ended", boom)
    assert not _denied(_bash(w, "cd %s && ls" % w.cwd, tool_use_id="toolu_f1"))


# ---------------------------------------------------------------- every ended job (t-357)
# Section 28.5: the guard denies every call of a job that has expired, was refused or was
# accepted, on the marker path and on the straggler path (t-357 attack a09, t-351 attack b05). A
# job read from the job line of an agent's first turn counts only when the line's nonce hashes to
# the job's nonce_sha256 and, on the straggler path, when the payload's session was handed the job
# (attack a10).
ENDED_TEXT = ("JOB-ENDED: job %s has ended (%s) and no longer runs; end your turn now with no "
              "further call")


def _end(w, how):
    if how == "expired":
        _expire(w)
    elif how == "refused":
        rev.emit(w.conn, rev.JOB_REFUSED, run=None, ref=w.attempt, op=None, session=w.sid,
                 actor="research-runner", data={"job": w.jid, "scope": w.SCOPE, "rule": "K9"})
    else:
        rev.emit(w.conn, rev.JOB_RESULT, run=None, ref=w.attempt, op=None, session=w.sid,
                 actor="research-runner", data={"job": w.jid, "scope": w.SCOPE})
    assert jobs.fold(w.conn, w.SCOPE)[w.jid]["state"] == how
    assert jobs.job_ended(w.conn, w.jid, w.SCOPE) == how


@pytest.mark.parametrize("path", ["marker", "straggler"])
@pytest.mark.parametrize("how", ["expired", "refused", "accepted"])
def test_every_call_of_an_ended_job_is_denied_on_both_paths(world, how, path):
    w = world
    command = "cd %s && cat > %s/src/left.py <<'EOF'\nx\nEOF" % (w.cwd, w.cwd)
    assert _ok(_bash(w, command, tool_use_id="toolu_x0"))       # open: allowed, fills the cache
    _end(w, how)
    if path == "straggler":
        _resume_in_another_session(w)
    out = _bash(w, command, tool_use_id="toolu_x1")
    assert _denied(out), (how, path)
    want = EXPIRED_TEXT % w.jid if how == "expired" else ENDED_TEXT % (w.jid, how)
    assert _reason(out) == want
    assert [e["data"]["rule"] for e in _deny_events(w)] == ["expired"]


def test_an_open_job_is_never_denied_as_ended(world):
    w = world
    assert jobs.job_ended(w.conn, w.jid, w.SCOPE) is None
    for tid in ("toolu_y0", "toolu_y1"):
        assert _ok(_bash(w, "cd %s && cat > %s/src/x.py <<'EOF'\nx\nEOF" % (w.cwd, w.cwd),
                         tool_use_id=tid))


def _second_agent(w, *, nonce=None):
    """A second agent of the job with no agent cache, whose first user turn is the job's spawn
    prompt with `nonce` in place of the job's own (None keeps it)."""
    real = w.entries[w.jid]["nonce"]
    a = rk.JudgeWorld.agent(w, w.jid)
    a.start()
    a.work()
    if nonce is not None:
        first = a.records[0]["message"]
        assert real in first["content"]
        first["content"] = first["content"].replace(real, nonce)
    a.write()
    return a


def _bash_as(w, agent, command, **kw):
    p = _payload(w, "Bash", {"command": command}, **kw)
    p["agent_id"] = agent.agent_id
    return research_guard.handle(p)


def test_a_header_with_a_wrong_nonce_resolves_no_job_and_writes_no_cache(world):
    w = world
    _expire(w)
    _resume_in_another_session(w)
    stranger = _second_agent(w, nonce="0" * 32)
    cache = os.path.join(w.root, ".alpaca", "research", "live", "agents",
                         "%s.json" % stranger.agent_id)
    assert _ok(_bash_as(w, stranger, "cd %s && ls src" % w.cwd, tool_use_id="toolu_z1"))
    assert not os.path.exists(cache)
    # the job's real nonce from the session that was handed the job is still denied (t-351 a16)
    straggler = _second_agent(w)
    out = _bash_as(w, straggler, "cd %s && ls src" % w.cwd, tool_use_id="toolu_z2")
    assert _denied(out) and _reason(out) == EXPIRED_TEXT % w.jid


def test_a_real_header_quoted_in_a_session_not_handed_the_job_is_not_denied(world):
    """G4: on the straggler path the payload's session must be one that was handed the job."""
    w = world
    _expire(w)
    _resume_in_another_session(w)
    quoted = _second_agent(w)
    out = _bash_as(w, quoted, "cd %s && ls src" % w.cwd, tool_use_id="toolu_z3",
                   session_id="0ddba11d-2222-4333-8444-555566667777")
    assert _ok(out)
    assert _deny_events(w) == []
