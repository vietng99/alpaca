"""t-359 (FU-6): a call of a tool the job is not given is denied live for every role, from one
predicate and one forbidden-tool list (design/research-engine/in-session-runtime.md section 26).

Done bar: an off-list tool call is denied live for every role. The roles come from types.ROLES,
never from a hand-kept list, so a new role is covered by the same tests. Each test names, in its
docstring, the change to the code that turns it red.

Synthetic data only: a test project from the `project` fixture, a live marker and research-job
events written by GuardWorld (test_research_calltime.py), hand-made agent transcripts whose only
record is a job line, and fake agents of research_session_kit. No real transcript text, no owner
name, e-mail or home path. Ported from the pre-mortem attacks
.alpaca/proofs/op-009/t-359.logs/think/attack/a01 to a18 (a16 is a follow-up, section 26.9).
"""
from __future__ import annotations

import ast
import glob
import json
import os
import re
import subprocess
import sys
import time

import pytest

from alpaca.hooks import research_guard
from alpaca.research import (agent_transcript as at, audit, errors, events, jobs, launcher,
                             session_runtime, types, verifier)
from alpaca.tests.devfiles import needs_dev_files
from alpaca.tests.test_research_calltime import GuardWorld, _check_tools, gw, jw  # noqa: F401

PKG = os.path.dirname(os.path.dirname(os.path.abspath(at.__file__)))      # alpaca/
REPO = os.path.dirname(PKG)

#: off-list tools for a job given only Read and Bash: file tools the job lacks, a shell tool of the
#: runtime, spawning and messaging tools, a web tool, a todo tool, an MCP tool, a lowercase alias
OFF_LIST = [
    ("Write", {"file_path": "/nonexistent-t359/x.txt", "content": "x"}),
    ("Edit", {"file_path": "/nonexistent-t359/x.txt", "old_string": "a", "new_string": "b"}),
    ("MultiEdit", {"file_path": "/nonexistent-t359/x.txt", "edits": []}),
    ("NotebookEdit", {"notebook_path": "/nonexistent-t359/x.ipynb", "new_source": "x"}),
    ("Grep", {"pattern": "x"}),
    ("Glob", {"pattern": "*.py"}),
    ("Monitor", {"command": "true"}),
    ("Skill", {"skill": "synthetic-skill"}),
    ("Agent", {"prompt": "x", "description": "x", "subagent_type": "general-purpose"}),
    ("Task", {"prompt": "x", "description": "x"}),
    ("SendMessage", {"to": "x", "message": "x"}),
    ("WebFetch", {"url": "http://example.invalid/"}),
    ("TodoWrite", {"todos": []}),
    ("mcp__synthetic__tool", {"q": "x"}),
    ("bash", {"command": "true"}),
]

_N = [0]


# ---------------------------------------------------------------- helpers
def call(tool: str, inp: dict) -> dict:
    _N[0] += 1
    return {"tool": tool, "raw_input": inp, "id": "toolu_t359_%04d" % _N[0]}


def _job_id(role: str, n: int) -> str:
    return "t-901-%s-t%d" % (role, n)


def synth_job(g: GuardWorld, role: str, tools, *, agent: str = "a359synth0000001", n: int = 1,
              event_over=None) -> dict:
    """A job of `role` with `tools` and a hand-made transcript (the job line only)."""
    job_id = _job_id(role, n)
    recs = [{"type": "user", "agentId": agent, "isSidechain": True,
             "message": {"role": "user", "content": "ALPACA JOB %s NONCE n0nce%04d\nRead the "
                         "prompt file first." % (job_id, n)}}]
    cwd = os.path.join(g.root, "wt359", agent)
    os.makedirs(cwd, exist_ok=True)
    return g.job(agent, role=role, tools=tuple(tools), recs=recs, cwd=cwd, event_over=event_over)


def decision(g: GuardWorld, agent: str, c: dict, **over):
    """(permissionDecision or None, reason or '')."""
    out = g.handle(agent, c, **over)
    if not out:
        return None, ""
    h = out["hookSpecificOutput"]
    return h.get("permissionDecision"), str(h.get("permissionDecisionReason") or "")


def is_tool_deny(reason: str, tool: str, allowed) -> bool:
    """A denial about the tool: it names the tool and every tool of the job's own list."""
    return bool(reason) and tool in reason and all(t in reason for t in allowed)


def _spec(w, role, tools, n):
    return w.make_spec(role, tools=tuple(tools), key="%s/%s/t359-%d" % (w.attempt, role, n))


def _issue(w, monkeypatch, role, tools, n) -> dict:
    """session_runtime.issue for a spec of `role` with `tools`. The test world's folders lie outside
    the launch's per-subject folders, so launcher.check_paths's folder refusal (and only that
    one) is passed over, as test_research_session_sources.py does."""
    real = launcher.check_paths

    def check(root, spec, contract):
        try:
            real(root, spec, contract)
        except errors.LaunchError as exc:
            if "per-subject folders" not in str(exc):
                raise
    monkeypatch.setattr(launcher, "check_paths", check)
    return session_runtime.issue(w.conn, w.root, _spec(w, role, tools, n), w.ctx, w.SCOPE)


def _job_events(w, jid: str) -> list:
    return [r for r in w.conn.execute("SELECT data FROM events WHERE kind=?", (events.JOB,))
            if json.loads(r["data"]).get("job") == jid]


# ---------------------------------------------------------------- 1. every role
@pytest.mark.parametrize("role", types.ROLES)
def test_every_off_list_tool_is_denied_live_for_every_role(gw, role):
    """a01. Red when `role not in jobs.GUARDED_ROLES` is put back before the tool step."""
    info = synth_job(gw, role, ("Read", "Bash"))
    missed = []
    for tool, inp in OFF_LIST:
        dec, why = decision(gw, info["agent"], call(tool, inp))
        if dec != "deny" or not is_tool_deny(why, tool, ("Read", "Bash")):
            missed.append((tool, dec, why[:80]))
    assert missed == [], "role %s: off-list calls not denied live: %r" % (role, missed)
    assert gw.deny_events(info["job"]) == [], "an off-list tool deny writes no event"


# ---------------------------------------------------------------- 2. one predicate
VOCAB = ["Read", "Bash", "Write", "Edit", "bash", "BASH", "write", "Write ", " Read", "MultiEdit",
         "NotebookEdit", "Monitor", "PowerShell", "Grep", "Glob", "LS", "mcp__synthetic__Write",
         "SubagentHandback", "ToolSearch", "Skill", "Agent", "TodoWrite"]


def _input(tool, cwd):
    low = tool.strip().lower()
    if low in ("bash", "monitor", "powershell"):
        return {"command": "cd %s && ls" % cwd}
    if low == "write":
        return {"file_path": os.path.join(cwd, "x.txt"), "content": "x"}
    if low in ("edit", "multiedit"):
        return {"file_path": os.path.join(cwd, "x.txt"), "old_string": "a", "new_string": "b"}
    if low == "read":
        return {"file_path": os.path.join(cwd, "x.txt")}
    return {"q": "x"}


@pytest.mark.parametrize("role,tools", [("implementer", ("Read", "Bash", "Write", "Edit")),
                                        ("proposer", ("Read", "Bash"))])
def test_the_guard_denies_exactly_what_k9_refuses(gw, role, tools):
    """a02. Red when the guard lowercases or strips the name, or tests it with its own `in`."""
    info = synth_job(gw, role, tools)
    wrong = []
    for name in VOCAB:
        dec, why = decision(gw, info["agent"], call(name, _input(name, info["folder"])))
        if at.off_list(name, list(tools)) != (dec == "deny"):
            wrong.append((repr(name), at.off_list(name, list(tools)), dec, why[:70]))
    assert wrong == [], wrong
    # the predicate is K9's set test, exact and case-sensitive
    k9 = set(tools) | set(at.RUNTIME_TOOLS)
    assert [n for n in VOCAB if at.off_list(n, list(tools)) != (n not in k9)] == []


@pytest.mark.parametrize("role,tools", [("implementer", ("Read", "Bash", "Write", "Edit")),
                                        ("proposer", ("Read", "Bash"))])
def test_k9_refuses_the_names_the_guard_denies(jw, role, tools):
    """a02, end to end through jobs.check_tools: each name the predicate puts off the list is
    JOB-TOOL-OFF-LIST, a listed and a runtime name are not. Red when K9 drops off_list."""
    names = [("bash", True), ("Write ", True), ("Monitor", True), ("mcp__synthetic__Write", True),
             ("Skill", True), ("Bash", False), ("ToolSearch", False)]
    for n, (name, off) in enumerate(names):
        assert at.off_list(name, list(tools)) is off, name
        jid = jw.issue(role, key="%s/%s/k9-%d" % (jw.attempt, role, n), tools=tools)
        jw.hand()
        a = jw.agent(jid, behavior=lambda r, j, name=name: {"calls": [
            (name, {"command": "cd %s && ls" % j["cwd"]}, "ok")]})
        a.start(); a.work(); a.write(); a.stop()
        if off:
            with pytest.raises(jobs.Problem) as exc:
                _check_tools(jw, jid, a)
            assert exc.value.code == "JOB-TOOL-OFF-LIST", name
        else:
            _check_tools(jw, jid, a)


# ---------------------------------------------------------------- 3. empty and missing lists
def _bare_job(g: GuardWorld, role: str, n: int) -> dict:
    """A job whose first research-job event has no `allowed_tools` field at all (an old or a
    damaged event): jobs.find_job_event reads the first event of the job."""
    jid = _job_id(role, n)
    folder = os.path.join(g.root, "wt359", "a359bare%08d" % n)
    data = {"job": jid, "scope": g.SCOPE, "role": role, "cwd": folder, "shell_cwd": g.root,
            "read_paths": [folder], "write_paths": [], "scanned_by": "scope"}
    events.emit(g.conn, events.JOB, run=None, ref=g.SCOPE, op=None, session=g.sid,
                actor="operator", data=data)
    return synth_job(g, role, ("Read", "Bash", "Write"), agent="a359bare%08d" % n, n=n)


@pytest.mark.parametrize("role", ["proposer", "implementer", "verifier"])
def test_an_empty_or_missing_list_allows_only_the_runtime_tools(gw, role):
    """a03 part 1. Red when the `not allowed` escape comes back (or a missing field is no rule)."""
    empty = synth_job(gw, role, ())
    bare = _bare_job(gw, role, 2)
    for info in (empty, bare):
        dec, why = decision(gw, info["agent"], call("Write", {"file_path": "/nonexistent-t359/x",
                                                             "content": "x"}))
        assert dec == "deny" and "Write" in why, (info["job"], why)
        # check round 1 (m1, M3): every role reads the same words for an empty list
        assert "Write: this job is given no tool of its own. This call did not run" in why, why
        assert "given only" not in why, why
        dec, why = decision(gw, info["agent"], call("ToolSearch", {"query": "x"}))
        assert dec != "deny", (info["job"], why)


# ---------------------------------------------------------------- 4. the issue
@pytest.mark.parametrize("tools,words", [
    ((), "has no tools"),
    (("Bash",), "has no Read tool"),
    (("Read", "Bash", "Agent"), "Agent, which no job may have"),
    (("Read", "Bash", "Task"), "Task, which no job may have"),
    (("Read", "Bash", "Skill"), "Skill, which no job may have"),
    (("Read", "Bash", "SendMessage"), "SendMessage, which no job may have"),
])
def test_issue_refuses_a_job_born_lost(jw, monkeypatch, tools, words):
    """a03 part 2, a12. Red when the `_check_tools` call leaves `issue`."""
    ok = _issue(jw, monkeypatch, "implementer", ("Read", "Bash"), 0)
    assert "job" in ok, "control issue failed: %r" % ok
    got = _issue(jw, monkeypatch, "implementer", tools, 1)
    assert got.get("result") is not None and got["result"]["outcome"] == "start-failed", got
    assert words in got["result"]["reason"], got["result"]["reason"]
    jid = session_runtime._job_id(_spec(jw, "implementer", tools, 1).key)
    assert _job_events(jw, jid) == [], "a refused issue writes no research-job event"
    key = _spec(jw, "implementer", tools, 1).key
    infra = [json.loads(r["data"]) for r in jw.conn.execute(
        "SELECT data FROM events WHERE kind=?", (events.INFRA,))]
    assert [d for d in infra if d.get("key") == key and d.get("class") == "start-failed"
            and words in d.get("detail", "")], infra


def test_the_issue_check_names_the_one_list():
    """The issue's check reads FORBIDDEN_TOOLS: every name of the list is refused, and the tools of
    the shipped archetypes are not."""
    class Spec:
        role = "implementer"
    for name in at.FORBIDDEN_TOOLS:
        Spec.tools = ("Read", "Bash", name)
        with pytest.raises(errors.LaunchError):
            session_runtime._check_tools(Spec)
    for line in glob.glob(os.path.join(REPO, "agents", "*.md")):
        got = re.search(r"^tools:\s*(.+)$", open(line, encoding="utf-8").read(), re.M)
        if got:
            Spec.tools = tuple(t.strip() for t in got.group(1).split(","))
            session_runtime._check_tools(Spec)


# ---------------------------------------------------------------- 5. the event is the source
def _forge(g, info, **fields):
    cache = os.path.join(g.root, ".alpaca", "research", "live", "agents", "%s.json" % info["agent"])
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    data = dict(info["data"], **fields)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    with open(os.path.join(os.path.dirname(info["prompt"]), "job.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(data, workdir=info["folder"]), fh)


def test_the_list_comes_from_the_event(gw):
    """a04. Red when the guard reads the role or the list from `_job_of`'s cache or job.json."""
    info = synth_job(gw, "proposer", ("Read", "Bash"))
    _forge(gw, info, allowed_tools=["Read", "Bash", "Write", "Edit"], role="verifier")
    dec, why = decision(gw, info["agent"], call("Write", {"file_path": os.path.join(
        info["folder"], "x.txt"), "content": "x"}))
    assert dec == "deny" and why.startswith("JOB-TOOL Write:"), why
    other = synth_job(gw, "implementer", ("Read", "Bash", "Write", "Edit"), agent="a359synth0000002",
                      n=2)
    _forge(gw, other, allowed_tools=["Read"])
    dec, why = decision(gw, other["agent"], call("Write", {"file_path": os.path.join(
        other["folder"], "x.txt"), "content": "x"}))
    assert dec != "deny", why


# ---------------------------------------------------------------- 6. Bash is not marked past the list
@pytest.mark.parametrize("role,tools", [("proposer", ("Read",)), ("implementer", ("Read", "Write")),
                                        ("verifier", ("Read",))])
def test_an_off_list_bash_is_denied_not_marked(gw, role, tools):
    """a05. Red when shell tools are exempt from the step (the marking allow would run them)."""
    info = synth_job(gw, role, tools)
    out = gw.handle(info["agent"], call("Bash", {"command": "cd %s && ls" % info["folder"]}))
    assert out is not None, "an off-list Bash got no decision"
    h = out["hookSpecificOutput"]
    assert h["permissionDecision"] == "deny" and "updatedInput" not in h, h


# ---------------------------------------------------------------- 7. no over-deny
@pytest.mark.parametrize("role", types.ROLES)
def test_listed_and_runtime_tools_still_run(gw, role):
    """a06. Red when RUNTIME_TOOLS leaves `off_list` (SubagentHandback ends every job)."""
    info = synth_job(gw, role, ("Read", "Bash"))
    dec, why = decision(gw, info["agent"], call("Read", {"file_path": info["prompt"]}))
    assert dec != "deny", why
    for tool in at.RUNTIME_TOOLS:
        dec, why = decision(gw, info["agent"], call(tool, {"query": "x"} if tool == "ToolSearch"
                                                    else {"result": "x"}))
        assert dec != "deny", (tool, why)
    if role in jobs.GUARDED_ROLES:
        return
    agent = "a359synth0000009"
    own = synth_job(gw, role, ("Read", "Bash", "Write", "Edit"), agent=agent, n=9,
                    event_over={"write_paths": [os.path.join(gw.root, "wt359", agent)]})
    dec, why = decision(gw, agent, call("Bash", {"command": "cd %s && ls" % own["folder"]}))
    assert dec != "deny", why
    dec, why = decision(gw, agent, call("Write", {"file_path": os.path.join(own["folder"], "x.txt"),
                                                  "content": "x"}))
    assert dec != "deny", why


# ---------------------------------------------------------------- 8. before the scans, no event
def test_the_tool_step_comes_before_the_scans_and_writes_no_event(gw):
    """a07, a08. Red when the step moves after `background_hit` or the control-plane scan, or when
    its Denial carries an event (70 such events would fill GUARD_DENY_CAP)."""
    info = synth_job(gw, "proposer", ("Read", "Bash"))
    dec, why = decision(gw, info["agent"], call("Monitor", {"command": "cd %s && sleep 1 &"
                                                           % info["folder"]}))
    assert dec == "deny" and why.startswith("JOB-TOOL Monitor:") and "outlives" not in why, why
    notes = synth_job(gw, "notes", ("Read", "Bash"), agent="a359synth0000003", n=3)
    target = os.path.join(gw.root, ".claude", "settings.json")
    dec, why = decision(gw, notes["agent"], call("Write", {"file_path": target, "content": "{}"}))
    assert dec == "deny" and why.startswith("JOB-TOOL Write:") and "control plane" not in why, why
    assert gw.deny_events(info["job"]) == [] and gw.deny_events(notes["job"]) == []
    for i in range(jobs.GUARD_DENY_CAP + 6):
        tool = ("Write", "Skill", "Monitor")[i % 3]
        inp = {"file_path": "/nonexistent-t359/%d" % i, "content": "x"} if tool == "Write" else \
            {"skill": "synthetic"} if tool == "Skill" else {"command": "true"}
        dec, why = decision(gw, info["agent"], call(tool, inp))
        assert dec == "deny" and is_tool_deny(why, tool, ("Read", "Bash")), (i, tool, why[:90])
    assert gw.deny_events(info["job"]) == []
    dec, why = decision(gw, info["agent"], call("Bash", {"command": "cd %s && sleep 1 &"
                                                        % info["folder"]}))
    assert dec == "deny" and "outlives the call" in why, why
    evs = gw.deny_events(info["job"])
    assert len(evs) == 1 and evs[0]["rule"] == "background" and evs[0]["n"] == 1, evs


# ---------------------------------------------------------------- 9. K9 is the owner's
@pytest.mark.parametrize("role", ["proposer", "implementer", "notes"])
def test_a_denied_off_list_call_still_loses_the_job(jw, role):
    """a09. Red when `_denied` stubs an event of a tool off the list, or `tool` leaves
    NON_ROLE_RULES: K9 would then excuse a denied call, which only the owner may decide (8.1)."""
    tools = ("Read", "Bash")
    jid = jw.issue(role, key="%s/%s/t359" % (jw.attempt, role), tools=tools)
    jw.hand()
    deny = "JOB-TOOL Write: this job is given only Read and Bash. This call did not run."
    ids = {}

    def edit(records, agent):
        for r in records:
            for item in (r.get("message") or {}).get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_use" and item["name"] == "Write":
                    ids["w"] = (item["id"], item["input"])
                if isinstance(item, dict) and item.get("type") == "tool_result" and ids.get("w") \
                        and item["tool_use_id"] == ids["w"][0]:
                    item["content"] = "PreToolUse:Write hook error: %s" % deny
                    item["is_error"] = True
    a = jw.agent(jid, behavior=lambda r, j: {"calls": [
        ("Write", {"file_path": os.path.join(j["cwd"], "x.txt"), "content": "x"}, "ok")]}, edit=edit)
    a.start(); a.work(); a.write(); a.stop()
    uid, inp = ids["w"]
    events.emit(jw.conn, events.GUARD_DENY, run=None, ref=jid, op=None, session=jw.sid,
                actor=jobs.GUARD_ACTOR,
                data={"job": jid, "agent_id": a.agent_id, "role": role, "tool": "Write",
                      "tool_use_id": uid, "input_sha256": jobs.call_sha256(inp), "rule": "tool",
                      "why": "off-list", "reason_sha256": jobs.text_sha256(deny), "n": 1})
    assert "tool" in jobs.NON_ROLE_RULES
    with pytest.raises(jobs.Problem) as exc:
        _check_tools(jw, jid, a)
    assert exc.value.code == "JOB-TOOL-OFF-LIST"


# ---------------------------------------------------------------- 10. one list
SPAWNING = {"Agent", "Skill", "Task", "SendMessage", "Workflow", "spawn_agent"}
TOOL_WORDS = sorted(SPAWNING | {"WebFetch", "WebSearch", "Monitor", "TodoWrite", "Write", "Edit",
                                "Grep", "Glob", "MultiEdit", "NotebookEdit", "ListAgents", "Read",
                                "Bash"})


def _spawning_literals() -> list:
    hits = []
    for f in sorted(glob.glob(os.path.join(PKG, "research", "*.py"))
                    + glob.glob(os.path.join(PKG, "hooks", "*.py"))):
        tree = ast.parse(open(f, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                names = [e.value for e in node.elts
                         if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if len([n for n in names if n in SPAWNING]) >= 2:
                    hits.append("%s:%d" % (os.path.relpath(f, PKG), node.lineno))
    return hits


def _tool_rule(tools) -> str:
    job = {"job": "t-901-proposer-t1", "nonce": "n0nce", "role": "proposer", "cwd": "/w",
           "read_paths": ["/w"], "write_paths": [], "scratch": "/s", "allowed_tools": list(tools),
           "shell_cwd": "/r"}
    head = session_runtime.build_prompt(job, "")
    return re.search(r"- Tool rule\.(.*?)(?=\n- |\n-----)", head, re.S).group(1)


def test_one_forbidden_tool_list():
    """a10. Red when SPAWN_TOOLS (or any second literal) comes back, when the launcher keeps a list
    of its own, or when the Tool rule names a tool again."""
    hits = _spawning_literals()
    assert len(hits) == 1 and hits[0].startswith("research/agent_transcript.py:"), hits
    assert not hasattr(jobs, "SPAWN_TOOLS")
    assert launcher._DISALLOWED_TOOLS is at.FORBIDDEN_TOOLS

    class Spec:
        tools = ("Read", "Bash")
        bash_allow = ()
    assert launcher._claude_settings(Spec())["permissions"]["deny"] == list(at.FORBIDDEN_TOOLS)
    assert not set(at.FORBIDDEN_TOOLS) & set(at.RUNTIME_TOOLS)
    for tools in (("Read", "Bash"), ("Read", "Bash", "Write", "Edit")):
        rule = _tool_rule(tools)
        named = [t for t in TOOL_WORDS if re.search(r"(?<![A-Za-z_])%s(?![A-Za-z_])" % t, rule)]
        stray = [t for t in named if t not in tools and t not in at.RUNTIME_TOOLS]
        assert stray == [], "the Tool rule names %s, outside the job's list" % stray
        assert all(t in rule for t in at.RUNTIME_TOOLS), rule
        assert "end your turn with no further call" in re.sub(r"\s+", " ", rule), rule


@needs_dev_files("design/research-engine")
def test_the_launcher_spec_cites_the_one_list():
    """Check round 1 (design B1). Red when a design spec of the launcher (architecture.md 6.2) writes
    its own --disallowedTools or settings deny literal again instead of citing FORBIDDEN_TOOLS.
    study-fesens.md is a study of FeSens's launcher and keeps FeSens's list."""
    folder = os.path.join(REPO, "design", "research-engine")
    names = re.compile(r"(?<![A-Za-z_])(%s)(?![A-Za-z_])" % "|".join(at.FORBIDDEN_TOOLS))
    hits, cited = [], 0
    for path in sorted(glob.glob(os.path.join(folder, "*.md"))):
        if os.path.basename(path) == "study-fesens.md":
            continue
        text = open(path, encoding="utf-8").read()
        for n, line in enumerate(text.splitlines(), 1):
            if "--disallowedTools" in line or re.search(r'"deny":\s*\[', line):
                if "FORBIDDEN_TOOLS" in line:
                    cited += 1
                if names.search(line):
                    hits.append("%s:%d" % (os.path.basename(path), n))
    assert hits == [], "a design spec repeats the forbidden-tool list: %s" % hits
    assert cited >= 2, "architecture.md 6.2 no longer cites FORBIDDEN_TOOLS for the flag and the deny"


# ---------------------------------------------------------------- 11. no list in prose
def test_no_prompt_text_names_a_tool_list():
    """a11, part 1. Red when "(Read and Bash)" comes back in one of the four texts."""
    prompts = os.path.join(PKG, "research", "prompts")
    for name in ("verify.md", "audit.md"):
        assert "(Read and Bash)" not in open(os.path.join(prompts, name), encoding="utf-8").read()
    assert "(Read and Bash)" not in verifier.RULE_WORDS["tool"]
    assert "(Read and Bash)" not in audit.RULE_WORDS_18["tool"]


@pytest.mark.parametrize("role", ["verifier", "auditor"])
def test_a_tool_deny_names_only_the_jobs_own_list(gw, role):
    """a11, part 2. Red when a literal list returns to the tool words of either role."""
    info = synth_job(gw, role, ("Read", "Bash", "Grep"))
    dec, why = decision(gw, info["agent"], call("Write", {"file_path": "/nonexistent-t359/x",
                                                         "content": "x"}))
    assert dec == "deny" and "Read, Bash, Grep" in why, why
    assert "(Read and Bash)" not in why and "Read and Bash" not in why, why


# ---------------------------------------------------------------- 12. stragglers
def test_a_straggler_off_list_call_is_denied(gw):
    """a13. Red when the tool step of `_straggler_denial` goes (only an expired job is denied)."""
    info = synth_job(gw, "proposer", ("Read", "Bash"))
    # the straggler's driver session was handed the job (a real straggler's payload session is
    # the session that ran next-job; t-357 counts a header job on this path only then, 28.5)
    events.emit(gw.conn, events.JOB_HANDED, run=None, ref=gw.SCOPE, op=None,
                session="t359-other-session", actor="operator",
                data={"job": info["job"], "scope": gw.SCOPE, "session": "t359-other-session"})
    dec, why = decision(gw, info["agent"], call("Write", {"file_path": "/nonexistent-t359/x",
                                                         "content": "x"}),
                        session_id="t359-other-session")
    assert dec == "deny" and why.startswith("JOB-TOOL Write:"), (dec, why)
    # a listed call of the same straggler still gets no verdict
    dec, why = decision(gw, info["agent"], call("Read", {"file_path": info["prompt"]}),
                        session_id="t359-other-session")
    assert dec is None, why


# ---------------------------------------------------------------- 13. the deny text
FILE_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit")
OTHER_TOOLS = ("Agent", "Skill", "Task", "SendMessage", "Workflow", "WebFetch", "WebSearch")


@pytest.mark.parametrize("tool,inp", [("Write", {"file_path": "/nonexistent-t359/x", "content": "x"}),
                                      ("Skill", {"skill": "synthetic"}),
                                      ("Monitor", {"command": "true"})])
def test_the_deny_text_of_a_job_of_another_role(gw, tool, inp):
    """a14. Red when the JOB text copies the verifier's "your pass goes on", drops "did not run" or
    "End your turn", or names a file or spawning tool besides the denied one and the job's list."""
    allowed = ("Read", "Bash")
    info = synth_job(gw, "proposer", allowed)
    dec, why = decision(gw, info["agent"], call(tool, inp))
    assert dec == "deny", why
    assert why == ("JOB-TOOL %s: this job is given only Read and Bash. This call did not run, and "
                   "this job is lost: the job check refuses every job that called a tool it is "
                   "not given. End your turn now with no further call." % tool), why
    assert "goes on" not in why
    words = set(re.findall(r"[A-Za-z_]+", why))
    assert [t for t in FILE_TOOLS if t in words and t not in allowed and t != tool] == []
    assert [t for t in OTHER_TOOLS if t in words and t != tool] == []


@pytest.mark.parametrize("role,code,mod", [("verifier", "VERIFY", verifier), ("auditor", "AUDIT", audit)])
def test_the_deny_text_of_a_verifier_and_an_auditor_is_unchanged(gw, role, code, mod):
    """18.1's text, byte for byte (pinned also by test_research_calltime.py)."""
    info = synth_job(gw, role, ("Read", "Bash"))
    dec, why = decision(gw, info["agent"], call("Skill", {"skill": "synthetic"}))
    assert why == ("%s-TOOL Skill: this job is given only Read and Bash. This call did not run, and "
                   "this pass is void: the job check refuses every call of a tool the job is not "
                   "given. %s" % (code, mod.RULE_WORDS["tool"])), why


# ---------------------------------------------------------------- 14. matcher, helpers, cost
def test_the_hook_matcher_covers_every_tool():
    """a15. Red when the research guard's matcher is narrowed (Monitor, Skill, mcp tools drop)."""
    cfg = json.load(open(os.path.join(REPO, ".claude", "settings.json"), encoding="utf-8"))
    entries = [e for e in cfg["hooks"]["PreToolUse"]
               if any("alpaca.hooks.research_guard" in h.get("command", "") for h in e["hooks"])]
    assert len(entries) == 1, entries
    assert entries[0].get("matcher", "") in ("", "*", ".*"), entries[0]


def test_a_helper_with_no_job_line_is_not_tool_denied(gw):
    """a18. Red when the guard denies by default when no job resolves (the driver's own helper
    agents would lose every tool for the whole run)."""
    helper = "a359helper000001"
    path = os.path.join(gw.w.ctx["subagents"], "agent-%s.jsonl" % helper)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "agentId": helper, "isSidechain": True,
                             "message": {"role": "user", "content": "Summarise a synthetic file."}})
                 + "\n")
    target = os.path.join(gw.root, "wt359", "helper-note.txt")
    out = {}
    for n, (tool, inp) in enumerate((("Write", {"file_path": target, "content": "x"}),
                                     ("Grep", {"pattern": "x", "path": os.path.dirname(target)}),
                                     ("Skill", {"skill": "synthetic"})), 1):
        p = {"session_id": gw.sid, "cwd": gw.root, "transcript_path": path, "tool_name": tool,
             "tool_input": inp, "tool_use_id": "toolu_t359_helper_%d" % n, "agent_id": helper,
             "agent_type": "general-purpose"}
        res = research_guard.handle(p) or {}
        out[tool] = res.get("hookSpecificOutput", {}).get("permissionDecision") or "no verdict"
    assert all(v != "deny" for v in out.values()), out


def test_guard_cost_for_every_role(gw, monkeypatch):
    """a17. 40 mixed proposer calls stay well inside the hook timeout, and each call opens the
    record read-only at most once. Red when the step adds a second record read per call."""
    from alpaca import db
    info = synth_job(gw, "proposer", ("Read", "Bash"))
    opened = [0]
    real = db.connect_readonly

    def counted(root):
        opened[0] += 1
        return real(root)
    monkeypatch.setattr(db, "connect_readonly", counted)
    took = []
    for i in range(40):
        if i % 2:
            c = call("Bash", {"command": "cd %s && ls" % info["folder"]})
        else:
            c = call("Write", {"file_path": "/nonexistent-t359/%d" % i, "content": "x"})
        t0 = time.monotonic()
        decision(gw, info["agent"], c)
        took.append(time.monotonic() - t0)
    # the median call stays at the old 0.1 s a call; a loaded host stretches a few calls, so the
    # slowest has only to stay well inside the 10 s hook timeout
    took.sort()
    assert took[len(took) // 2] < 0.1 and took[-1] < 2.0, took
    # per call: the expired check and the tool step (one read each, as on main); no third read.
    # The one more read is the agent's first call, which fills the agent cache (_job_of).
    assert opened[0] <= 2 * 40 + 1, opened[0]


# ---------------------------------------------------------------- 15. the hook entry itself
def test_the_hook_entry_denies_an_off_list_write(gw):
    """The real entry point, `python -m alpaca.hooks.research_guard` with the payload on stdin,
    prints the deny decision (main, common.fail_open). Red when main stops printing it."""
    info = synth_job(gw, "proposer", ("Read", "Bash"))
    c = call("Write", {"file_path": "/nonexistent-t359/x", "content": "x"})
    payload = gw.payload(info["agent"], c)
    env = dict(os.environ, PYTHONPATH=REPO)
    got = subprocess.run([sys.executable, "-m", "alpaca.hooks.research_guard"],
                         input=json.dumps(payload), capture_output=True, text=True, cwd=gw.root,
                         env=env, timeout=60)
    assert got.returncode == 0, got.stderr
    out = json.loads(got.stdout.strip().splitlines()[-1])
    h = out["hookSpecificOutput"]
    assert h["permissionDecision"] == "deny" and h["permissionDecisionReason"].startswith(
        "JOB-TOOL Write:"), out
    # and a listed call through the same entry is not denied
    payload = gw.payload(info["agent"], call("Read", {"file_path": info["prompt"]}))
    got = subprocess.run([sys.executable, "-m", "alpaca.hooks.research_guard"],
                         input=json.dumps(payload), capture_output=True, text=True, cwd=gw.root,
                         env=env, timeout=60)
    assert got.returncode == 0 and "deny" not in got.stdout, got.stdout
