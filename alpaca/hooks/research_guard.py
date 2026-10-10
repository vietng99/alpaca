"""PreToolUse guard of the research engine's session runtime (in-session-runtime.md section 12.3).

With no jail an agent that a research run spawns runs with the session's rights, so it could reach
the control plane of Alpaca: the record, the hooks, the settings, the engine's own code, a transcript
of another agent. This hook is prevention at the tool boundary for the textual and path forms, and a
fast alarm for the rest; the pin, the scans and the judge's checks are what decide.

Two rules apply to a subagent's call whether or not a run is live. A shell call that runs a driving
verb (`alpaca research start`, `resume`, `extend`, `stop`, `next-job`, `submit`, `alpaca proof verify`)
is denied, because only the driving session drives a run. No environment variable tells a subagent's
shell from the driver's (in-session-runtime.md section 3.4), so while the driver session has a
live run the guard also returns an allow decision with `updatedInput` for every Bash call of a
subagent of that session: the whole input, its command put
behind `export ALPACA_RESEARCH_AGENT=<agent id>;` (alpaca/research/agent_marker.py). The verbs that
drive a run refuse when that variable is set, and a command that names the variable is denied, so a
subagent cannot unset it. The driver's calls carry no `agent_id` and are never touched. The rest does nothing unless
the call comes from a subagent (the payload carries `agent_id`; the driver's own calls are never
denied) and unless the live marker `.alpaca/research/live/<driver session>.json`
exists, which `start`, `resume` and `extend` write and `finalize` removes. For a subagent's call
it finds the agent's job from the first line of its transcript (`ALPACA JOB <job> NONCE <nonce>`),
reads that job's `job.json`, takes the deciding fields from the append-only research-job event
(opening the record read-only once per agent) and caches the answer, then prints
one PreToolUse deny decision when the call names an entry of CONTROL_NAMES or a path of the control
plane that is not the job's own folder, writes the engine set, `bin/`, `setup/`, the venv or a hook
script, starts a process that outlives the call, or is a git command that writes a repository. On
each call of a subagent it also runs the stat pass of the engine pin and denies the call when a
pinned file was touched. Each denial is appended to `<driver session>.denials.jsonl`.

For a subagent whose job resolves, of any role, a call of a tool that is neither in the
`allowed_tools` of the job's `research-job` event nor one of the runtime's own
(`agent_transcript.off_list`, the test K9 runs after the job) is denied first, before any role
rule or scan, and writes no event; K9 still refuses the job (in-session-runtime.md section 26).
A verifier or an auditor is told that the pass is void, any other role that the job is lost and
to end its turn. A straggler whose job resolves gets the same step.

For a subagent whose job has the role `verifier` or `auditor` the guard also applies that role's
shell rules to the call before it runs (in-session-runtime.md section 18.1): the same rule
function the after-pass judge runs (`verifier.role_rules`, `audit.role_rules`), over this one
call. A call that breaks a rule is denied with the rule's text, writes one `research-guard-deny`
event (job, agent id, the call's `tool_use_id`, the hash of its input and of the deny text), and
does not run, so the pass goes on; a call of a tool the job is not given is denied with a text that
says the pass is void and writes no event. The role and the tools come from the append-only
`research-job` event on every call, never from job.json or the cache.

For a subagent whose job resolves and has any other role (proposer, implementer, notes, the
reproducer roles) each plain-string denial (a driving verb, the marker variable, a changed engine,
a background process, the control plane) is a Denial of a rule word of `jobs.DENY_EVENT_RULES` and
writes the same `research-guard-deny` event, so that the audit reads the call as one that did not
run (`jobs.ran_calls`; t-351, in-session-runtime.md section 23). A verifier's or an auditor's
string denial writes no event. Past `jobs.GUARD_DENY_CAP` events a job of another role is denied
with its own text and no event, so its calls read as run (fail closed).

Every call of a job that has ended is denied, whatever the tool or the role (t-351 F47, section 10;
t-357, section 28.5). An expired job gets "JOB-EXPIRED: job <id> has expired and no longer runs; end
your turn now with no further call"; a refused or accepted job gets "JOB-ENDED: job <id> has ended
(<state>) and no longer runs; end your turn now with no further call". Both are rule `expired`, with
an event for a job of a non-guarded role. The state is read from the record (`jobs.job_ended`), never
from the agent cache. A straggler whose driver session has no live marker (the run was resumed in
another session) is denied the same way: its job comes from the agent cache, or from the job line of
its transcript looked up in the scopes of the live markers of other sessions. A job read from the
job line counts only when the line's nonce hashes to the `nonce_sha256` of the job's research-job
event and, on the straggler path, when a research-job-handed event of the job names the payload's
session (t-357, section 28.5), so a stranger that quotes a job id with another nonce, or a real
header in a session that was never handed the job, is no job.

Fail-open like `git_guard`: any parse failure prints nothing and the call goes ahead. The guard
exists for Claude Code only; detection, not the guard, is what the design relies on for anything it
must not allow.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re

from alpaca.hooks import common
from alpaca.research import agent_marker

#: seconds the guard waits for the record's lock before it denies with no event (section 18.1)
EVENT_LOCK_S = 2.0

_JOB_LINE = re.compile(r"ALPACA JOB (\S+) NONCE (\S+)")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_READ_TOOLS = ("Read", "Glob", "Grep", "LS", "NotebookRead")


def _marker(root: str, session: str):
    from alpaca import pool
    path = os.path.join(root, ".alpaca", "research", "live", "%s.json" % pool.slug(session))
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


def _agent_files(payload: dict, agent_id: str) -> list:
    """The agent's transcript file: the payload's own `transcript_path` when it is the agent's file
    (<session folder>/subagents/**/agent-<id>.jsonl), and the file beside the session transcript
    otherwise (the driver's transcript path, <session folder>.jsonl)."""
    transcript = payload.get("transcript_path") or ""
    if not transcript:
        return []
    name = "agent-%s.jsonl" % agent_id
    out = []
    if os.path.basename(transcript) == name and os.path.isfile(transcript):
        out.append(transcript)
    stem = os.path.splitext(transcript)[0]
    out += glob.glob(os.path.join(stem, "subagents", "**", name), recursive=True)[:1]
    return out


def _agent_head(payload: dict, agent_id: str) -> str:
    """The first user record's text of the agent's transcript."""
    for path in _agent_files(payload, agent_id)[:1]:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for _ in range(4):
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("type") == "user":
                        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
                        content = msg.get("content")
                        if isinstance(content, list):
                            content = " ".join(str(c.get("text", "")) for c in content
                                               if isinstance(c, dict))
                        return str(content or "")
        except OSError:
            return ""
    return ""


def _job_of(root: str, marker: dict, payload: dict, agent_id: str, *, handed_to: str | None = None):
    """The job.json of the agent's job, cached under live/agents/<agent id>.json. A job read from
    the job line of the agent's first turn counts only when the line's nonce hashes to the job
    event's `nonce_sha256` and, with `handed_to` (the straggler path), when that session was
    handed the job (section 28.5); otherwise None and no cache is written."""
    cache = os.path.join(root, ".alpaca", "research", "live", "agents", "%s.json" % agent_id)
    try:
        with open(cache, encoding="utf-8") as fh:
            got = json.load(fh)
        if isinstance(got, dict):
            return got
    except (OSError, ValueError):
        pass
    found = _JOB_LINE.search(_agent_head(payload, agent_id))
    if not found:
        return None
    job_id = found.group(1)
    for scope in marker.get("scopes") or []:
        from alpaca.research import paths
        try:
            path = os.path.join(paths.job_dir(root, scope, job_id), "job.json")
        except ValueError:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                job = json.load(fh)
        except (OSError, ValueError):
            continue
        job = _event_backed(root, job_id, job, nonce=found.group(2), handed_to=handed_to)
        if job is None:
            return None             # not the job's nonce, or not its session (section 28.5)
        slim = {k: job.get(k) for k in ("job", "scope", "cwd", "workdir", "read_paths",
                                        "write_paths", "scratch", "prompt_path", "shell_cwd", "role",
                                        "allowed_tools")}
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(slim, fh)
        except OSError:
            pass
        return slim
    return None


def _event_backed(root: str, job_id: str, job: dict, *, nonce: str | None = None,
                  handed_to: str | None = None):
    """job.json with the deciding fields taken from the research-job event, which is append-only
    (jobs.event_backed). With `nonce` (the job line of the agent's first turn), None when the
    event holds a `nonce_sha256` that the nonce does not hash to: a header with a wrong nonce
    names no job (t-357, in-session-runtime.md section 28.5). With `handed_to`, None also when no
    research-job-handed event of the job names that session, so a quote of a real header in an
    unrelated session is no job. With no record to read, job.json stands, as the guard is
    fail-open."""
    try:
        from alpaca import db
        from alpaca.research import events as rev, jobs
        conn = db.connect_readonly(root)
        try:
            event = jobs.find_job_event(conn, job_id)
            handed = []
            if event is not None and handed_to is not None:
                handed = [ev["session"] for ev in jobs.events_of(
                    conn, str((event["data"] or {}).get("scope") or ""), (rev.JOB_HANDED,),
                    job=job_id)]
        finally:
            conn.close()
        if event is not None:
            want = (event["data"] or {}).get("nonce_sha256")
            if nonce is not None and want and hashlib.sha256(
                    str(nonce).encode("utf-8")).hexdigest() != want:
                return None
            if handed_to is not None and handed_to not in handed:
                return None
            return jobs.event_backed(job, event["data"])
    except Exception:  # noqa: BLE001 - fail-open
        pass
    return job


_SHELL_TOOLS = ("Bash", "bash", "Monitor", "PowerShell")


def _call_of(tool: str, tool_input: dict) -> dict:
    """The call the scans read: {tool, kind, input, paths}."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool in _SHELL_TOOLS or tool.endswith("run_in_terminal") or (
            tool not in _WRITE_TOOLS and tool not in _READ_TOOLS
            and isinstance(tool_input.get("command"), str)):
        # Bash and every other tool that takes a command string run it in a shell
        return {"tool": tool, "kind": "shell", "input": str(tool_input.get("command") or ""),
                "paths": [], "raw_input": tool_input}
    paths_ = [str(tool_input[k]) for k in ("file_path", "notebook_path", "path")
              if isinstance(tool_input.get(k), str)]
    kind = "write" if tool in _WRITE_TOOLS else "read" if tool in _READ_TOOLS else "other"
    return {"tool": tool, "kind": kind, "input": tool_input, "paths": paths_,
            "raw_input": tool_input}


def _driving_call(payload: dict) -> str | None:
    """The driving verb a subagent's shell call (Bash, Monitor, PowerShell, a run_in_terminal tool) runs (session_engine.driving_verb_hit), or None.
    It applies with no live marker too, so a subagent cannot start a run either. A cheap text test
    comes first so that a call that does not name alpaca never loads the engine module; any
    failure is no hit (fail-open)."""
    try:
        tool_input = payload.get("tool_input")
        text = tool_input.get("command") if isinstance(tool_input, dict) else None
        tool = str(payload.get("tool_name") or "")
        if not isinstance(text, str) or "alpaca" not in text:
            return None
        call = _call_of(tool, tool_input)
        if call["kind"] != "shell":
            return None
        from alpaca.research import session_engine as eng
        return eng.driving_verb_hit(call)
    except Exception:  # noqa: BLE001 - fail-open
        return None


def _names_marker(payload: dict) -> bool:
    """True when the subagent's shell call names the variable that marks its shell."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    return agent_marker.names_marker(tool_input.get("command"))


class Denial:
    """A denial of a job's call: the deny text, and the `research-guard-deny` event to write before
    the decision is printed (None for a call of a tool the job is not given, of any role, which
    writes none; section 26)."""

    def __init__(self, text: str, event: dict | None = None):
        self.text, self.event = text, event

    def __str__(self) -> str:
        return self.text


def _exact_files(payload: dict, agent_id: str, tool_input) -> set:
    """The persisted outputs of the agent's own tool results so far, each an exact file
    (agent_transcript.persisted_outputs). Read only when the call names a `tool-results` folder."""
    try:
        if "tool-results" not in json.dumps(tool_input, default=str):
            return set()
        from alpaca.research import agent_transcript as at
        files = _agent_files(payload, agent_id)
        if not files:
            return set()
        return at.persisted_outputs(at.read_records(files[0]), at.results_dir_of(files[0]))
    except Exception:  # noqa: BLE001 - fail-open: no file counts
        return set()


def _list_words(tools) -> str:
    tools = [str(t) for t in tools]
    return " and ".join(tools) if len(tools) <= 2 else ", ".join(tools)


def _tool_text(role, tool: str, allowed) -> str:
    """The deny text of a call of a tool the job is not given (section 26). A verifier's or an
    auditor's text is the one of section 18.1; any other role is told that the call did not run,
    that the job is lost all the same (K9), and to end its turn, as the header's Tool rule says.
    An empty list (only an old event holds one: the issue refuses it) reads "given no tool of its
    own" for every role."""
    words = _list_words(allowed)
    given = "only %s" % words if words else "no tool of its own"
    if role in ("verifier", "auditor"):
        if role == "verifier":
            from alpaca.research import verifier as mod
        else:
            from alpaca.research import audit as mod
        code = "VERIFY" if role == "verifier" else "AUDIT"
        return ("%s-TOOL %s: this job is given %s. This call did not run, and this pass "
                "is void: the job check refuses every call of a tool the job is not given. %s"
                % (code, tool, given, mod.RULE_WORDS["tool"]))
    return ("JOB-TOOL %s: this job is given %s. This call did not run, and this job is lost: "
            "the job check refuses every job that called a tool it is not given. End your turn now "
            "with no further call." % (tool, given))


def _role_denial(payload: dict, root: str, call: dict, agent_id: str, exact: set, *,
                 tool_only: bool = False):
    """A Denial for a subagent's call of a job, or None. First, for every role, the tool step of
    section 26: a call of a tool that is neither in the `allowed_tools` of the job's research-job
    event nor one of the runtime's own (agent_transcript.off_list, the test K9 runs) is denied with
    no event; a missing or non-list field is the empty list, as K9 reads it. Then, for a verifier
    or an auditor job, the role's rules (section 18.1). The role, the tools, the cwd, the shell
    folder, the prompt file and the slot are read from the job's research-job event; with no job
    line or no readable event the guard applies neither step and the judge alone decides.
    `tool_only` stops after the tool step (a straggler's call, section 26)."""
    try:
        found = _JOB_LINE.search(_agent_head(payload, agent_id))
        if not found:
            return None
        job_id = found.group(1)
        from alpaca import db
        from alpaca.research import agent_transcript as at, events as rev, jobs
        conn = db.connect_readonly(root)
        try:
            jev = jobs.find_job_event(conn, job_id)
            if jev is None:
                return None
            data = jev["data"]
            role = data.get("role")
            allowed = data.get("allowed_tools")
            allowed = allowed if isinstance(allowed, list) else []   # as K9 reads it
            tool = str(payload.get("tool_name") or "")
            if at.off_list(tool, allowed):
                # every role, before any role rule or scan; no event: K9 still refuses the job
                return Denial(_tool_text(role, tool, allowed))
            if tool_only or role not in jobs.GUARDED_ROLES or not allowed or tool in at.RUNTIME_TOOLS:
                return None
            code = "VERIFY" if role == "verifier" else "AUDIT"
            if role == "verifier":
                from alpaca.research import verifier as mod
            else:
                from alpaca.research import audit as mod
            folder = data.get("cwd")
            shell = data.get("shell_cwd")
            if not folder:
                return None
            if role == "verifier":
                group = str(data.get("group") or "")
                vf, slot = group.split("/")[0], data.get("verifier_slot")
                others = mod._other_homes(conn, vf, slot) if vf and slot else []
                hits = mod.role_rules(root, folder, [call], other_homes=others, shell_cwd=shell,
                                      extra_allowed=(data.get("prompt_path"),),
                                      exact_allowed=tuple(exact), runtime_tools=at.RUNTIME_TOOLS)
            else:
                hits = mod.role_rules(folder, [call], shell_cwd=shell, runtime_tools=at.RUNTIME_TOOLS)
            if not hits:
                return None
            # the allow list names the form (`command-substitution`, `process-substitution`); the
            # path rule only reads the word the form made as a path it cannot place, so the form
            # leads the denial and its words come first
            hits = sorted(hits, key=lambda h: {"allow-list": 0, "path": 1}.get(h["rule"], 2))
            first = hits[0]
            keys = []
            for h in hits:
                for k in h["keys"]:
                    if k not in keys and k in mod.RULE_WORDS:
                        keys.append(k)
            keys = keys or ["program" if "program" in mod.RULE_WORDS else "program-off-list"]
            why = str(first["why"]).replace("\n", " ")[:400]
            text = "%s-RULE %s: %s. This call did not run and your pass goes on. %s" % (
                code, keys[0], why, " ".join(mod.RULE_WORDS[k] for k in keys[:3]))
            tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
            scope = data.get("scope")
            event = {"job": job_id, "agent_id": agent_id, "role": role, "tool": tool,
                     "tool_use_id": payload.get("tool_use_id") or payload.get("toolUseId"),
                     "input_sha256": jobs.call_sha256(tool_input), "rule": keys[0], "why": why,
                     "reason_sha256": jobs.text_sha256(text),
                     "_scope": scope, "_op": jev.get("op")}
            return Denial(text, event)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - fail-open: the judge alone decides
        return None


def _string_denial(text: str, rule: str, why: str, payload: dict, agent_id: str, job):
    """`text`, or a Denial of rule `rule` (one of jobs.DENY_EVENT_RULES) that writes a
    `research-guard-deny` event, for a plain-string denial of a call of a job whose role is not
    guarded (t-351 F48, in-session-runtime.md section 23): the audit then reads the call as one
    that did not run. `job` is the job the call resolved to (None: no event, the string stands);
    a verifier's or an auditor's string denial writes none (section 18.1). Any failure keeps the
    string."""
    try:
        from alpaca.research import jobs
        if not job or not job.get("job") or job.get("role") in jobs.GUARDED_ROLES \
                or rule not in jobs.DENY_EVENT_RULES:
            return text
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        event = {"job": job["job"], "agent_id": agent_id, "role": job.get("role"),
                 "tool": str(payload.get("tool_name") or ""),
                 "tool_use_id": payload.get("tool_use_id") or payload.get("toolUseId"),
                 "input_sha256": jobs.call_sha256(tool_input), "rule": rule,
                 "why": str(why).replace("\n", " ")[:400], "reason_sha256": jobs.text_sha256(text),
                 "_scope": job.get("scope"), "_op": None}
        return Denial(text, event)
    except Exception:  # noqa: BLE001 - fail-open: the string stands
        return text


def _resolved_job(root: str, payload: dict, agent_id: str):
    """The job of a subagent's call when the driver session has a live marker and the job is found
    (the checks that run before the marker lookup use it to word their event), else None."""
    try:
        marker = _marker(root, common.session_of(payload))
        return _job_of(root, marker, payload, agent_id) if marker is not None else None
    except Exception:  # noqa: BLE001
        return None


def _ended_denial(root: str, payload: dict, agent_id: str, job):
    """The denial of every call of a job that has ended (expired, refused or accepted), or None
    (t-351 F47, in-session-runtime.md section 10; t-357, section 28.5): the state is read from the
    record (`jobs.job_ended`), never from the agent cache. An expired job keeps the JOB-EXPIRED
    text; a refused or accepted job gets JOB-ENDED with its state. Both are rule `expired`: a
    non-guarded role gets a Denial that writes the event, so the audit reads the call as one that
    did not run; a verifier or an auditor gets the string. A read error is no denial (fail-open)."""
    try:
        if not job or not job.get("job") or not job.get("scope"):
            return None
        from alpaca import db
        from alpaca.research import jobs
        conn = db.connect_readonly(root)
        try:
            state = jobs.job_ended(conn, str(job["job"]), str(job["scope"]))
        finally:
            conn.close()
        if not state:
            return None
        if state == "expired":
            text = ("JOB-EXPIRED: job %s has expired and no longer runs; end your turn now with "
                    "no further call" % job["job"])
            why = "job %s has expired" % job["job"]
        else:
            text = ("JOB-ENDED: job %s has ended (%s) and no longer runs; end your turn now with "
                    "no further call" % (job["job"], state))
            why = "job %s has ended (%s)" % (job["job"], state)
        return _string_denial(text, "expired", why, payload, agent_id, job)
    except Exception:  # noqa: BLE001 - fail-open
        return None


def _live_markers(root: str) -> list:
    """The live markers of every driver session (live/<session>.json), parsed."""
    out = []
    try:
        base = os.path.join(root, ".alpaca", "research", "live")
        for name in sorted(os.listdir(base)):
            if not name.endswith(".json") or name.endswith(".denials.jsonl"):
                continue
            try:
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    got = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(got, dict):
                out.append(got)
    except OSError:
        pass
    return out


def _straggler_denial(root: str, payload: dict, agent_id: str):
    """A subagent's call whose session has no live marker (a run interrupted in session A and
    resumed in session B has a marker for B only; t-351 F47, section 10): the denial of an
    ended job (section 28.5), then the tool step of section 26 (a call of a tool the job is not given), or
    None. The job comes from `live/agents/<agent id>.json` when it exists, else, only when at least
    one live marker exists, from the job line of the agent's first user turn looked up in the
    scopes of every live marker. Every other case is no verdict, as before."""
    try:
        cache = os.path.join(root, ".alpaca", "research", "live", "agents", "%s.json" % agent_id)
        job = None
        try:
            with open(cache, encoding="utf-8") as fh:
                got = json.load(fh)
            job = got if isinstance(got, dict) else None
        except (OSError, ValueError):
            job = None
        if job is None:
            markers = _live_markers(root)
            if not markers:
                return None
            scopes = []
            for m in markers:
                for scope in m.get("scopes") or []:
                    if scope not in scopes:
                        scopes.append(scope)
            job = _job_of(root, {"scopes": scopes}, payload, agent_id,
                          handed_to=common.session_of(payload))
        gone = _ended_denial(root, payload, agent_id, job)
        if gone is not None or not job:
            return gone
        # a straggler's job resolved: the tool step of section 26, read from its event
        return _role_denial(payload, root, {}, agent_id, set(), tool_only=True)
    except Exception:  # noqa: BLE001 - fail-open
        return None


def _write_deny_event(root: str, payload: dict, denial: Denial, agent_id: str) -> Denial:
    """Write the `research-guard-deny` event of `denial` and return the denial to print. The
    count `n` of the job's events is read and the event written in one write transaction, so hooks
    that run at once cannot pass the cap; from the 65th denial of a job on, no event is written and
    the text says the pass is void (a verifier or auditor job), or is the denial's own text (any
    other role: its call is then read as one that ran, which fails closed). The write waits at
    most EVENT_LOCK_S for the record's lock; when it fails the call is still denied, and the
    judge then voids the pass for that call."""
    ev = denial.event
    if ev is None:
        return denial
    try:
        import sqlite3
        from alpaca import db, paths as alpaca_paths
        from alpaca.research import events as rev, jobs, paths as rpaths
        fields = {k: v for k, v in ev.items() if not k.startswith("_")}
        conn = sqlite3.connect(alpaca_paths.db_path(root), timeout=EVENT_LOCK_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            with db.transaction(conn):
                got = conn.execute("SELECT COUNT(*) FROM events WHERE kind=? AND ref=?",
                                   (rev.GUARD_DENY, ev["job"])).fetchone()[0]
                if got >= jobs.GUARD_DENY_CAP:
                    if ev["role"] not in jobs.GUARDED_ROLES:
                        return Denial(denial.text)
                    if ev["role"] == "verifier":
                        from alpaca.research import verifier as mod
                    else:
                        from alpaca.research import audit as mod
                    prefix = "%s-RULE" % ("VERIFY" if ev["role"] == "verifier" else "AUDIT")
                    return Denial("%s deny-cap: this job has been refused %d calls. This call did "
                                  "not run, and this pass is void. %s" % (
                                      prefix, jobs.GUARD_DENY_CAP, mod.RULE_WORDS["deny-cap"]))
                scope = ev.get("_scope")
                run = scope if scope and rpaths.RUN_ID.fullmatch(str(scope)) else None
                rev.emit(conn, rev.GUARD_DENY, run=run, ref=ev["job"], op=ev.get("_op"),
                         session=common.session_of(payload), actor=jobs.GUARD_ACTOR,
                         data=dict(fields, n=got + 1), in_txn=True)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - still denied; the judge voids the pass for the call
        pass
    return denial


def verdict(payload: dict, root: str) -> "str | Denial | None":
    """The reason the call is denied, or None. A Denial with no event for any job's call of a tool
    it is not given (design 26); a Denial for a verifier or auditor job's call that breaks the
    role's rules; for any other role a string denial is also returned through `_string_denial`,
    which records its research-guard-deny event (design 18.1, 23.2)."""
    agent_id = payload.get("agent_id") or payload.get("agentId")
    if not agent_id:
        return None
    hit = _driving_call(payload)
    if hit:
        return _string_denial(
            "RUN-DRIVER-IS-SUBAGENT: a subagent never drives a research run (%s); only the "
            "driving session runs that verb" % hit, "driver", hit, payload, str(agent_id),
            _resolved_job(root, payload, str(agent_id)))
    if _names_marker(payload):
        return _string_denial(
            "RUN-DRIVER-IS-SUBAGENT: a subagent's command names %s, the variable that marks a "
            "subagent's shell; the guard sets it and no command may touch it" % agent_marker.ENV,
            "driver", "names %s" % agent_marker.ENV, payload, str(agent_id),
            _resolved_job(root, payload, str(agent_id)))
    marker = _marker(root, common.session_of(payload))
    if marker is None:
        return _straggler_denial(root, payload, str(agent_id))
    from alpaca.research import session_engine as eng
    call = _call_of(str(payload.get("tool_name") or ""), payload.get("tool_input"))
    job = _job_of(root, marker, payload, str(agent_id))
    gone = _ended_denial(root, payload, str(agent_id), job)
    if gone is not None:
        return gone
    own = []
    if job:
        own = [job.get("cwd"), job.get("workdir"), job.get("prompt_path"), job.get("scratch")] \
            + list(job.get("read_paths") or []) + list(job.get("write_paths") or [])
    else:
        # with no job found only the rules that need none apply: the job decides what lies under
        # the research folders
        own = [os.path.join(root, ".alpaca", "research")]
    own = [p for p in own if p]
    # the stat pass of the engine pin at the start of every call of a subagent
    for scope, pin_file in (marker.get("pin_files") or {}).items():
        if not pin_file:
            continue
        got = eng.engine_unchanged(root, pin_file, driver_session=common.session_of(payload))
        if not got["ok"]:
            try:
                from alpaca.research import jobs
                jobs.write_marker(root, scope, kind="engine", changed=got["changed"],
                                  pin_event=_pin_event(root, scope), point="guard")
            except Exception:  # noqa: BLE001
                pass
            changed = ", ".join(c["path"] for c in got["changed"][:3])
            return _string_denial(
                "the engine, a hook script, an import root or a text source was touched (%s); "
                "the run is ending" % changed, "engine", "touched %s" % changed, payload,
                str(agent_id), job)
    exact = _exact_files(payload, str(agent_id), payload.get("tool_input"))
    # every role's call of a tool outside the event's allowed_tools and RUNTIME_TOOLS is denied
    # here first, with no event; K9 still refuses the job (section 26). Then a verifier's or
    # auditor's call is read against the role's rules before the background check and the control
    # plane: a trailing `&` is one of pathscan.FLAGS, and the role rule names it with the prompt's
    # words (section 18.1)
    role = _role_denial(payload, root, call, str(agent_id), exact)
    if role is not None:
        return role
    bg = eng.background_hit(call)
    if bg:
        return _string_denial(
            "starting a process that outlives the call (%s) is not allowed in a research job" % bg,
            "background", bg, payload, str(agent_id), job)
    plane = {"deny": marker.get("deny") or [], "write": marker.get("write") or [],
             "read_deny": marker.get("read_deny") or []}
    shell = payload.get("cwd") or root
    hits = eng.control_hits([call], here=shell, root=root, hook_scripts=marker.get("hook_scripts"),
                            cfg=marker.get("config_dir"), allowed=tuple(own), plane=plane,
                            exact_allowed=tuple(exact))
    if hits:
        why = hits[0]["why"]
        text = "this call %s; a research job never reaches the control plane" % why
        if job and job.get("role") not in _guarded_roles():
            # a relative write that the cd reader placed in the start folder: say where it was
            # read and how to name it so that it lands in the cd folder (the reader is not
            # widened: this only words the denial; t-351 F48). Only a folder the job may write
            # is offered, never its read-only cwd (t-351 M9, t-354 section 27.1)
            writable = tuple(p for p in [job.get("scratch")] + list(job.get("write_paths") or [])
                             if p)
            note = eng.cd_chain_note(call, here=shell, hit_why=why, own=writable)
            if note:
                text = "%s. %s" % (text, note)
            form = _rewrite_sentence(hits[0].get("rule"), writable)
            if form:
                text = "%s. %s" % (text, form)
        return _string_denial(text, "control-plane", why, payload, str(agent_id), job)
    return None


def _rewrite_sentence(rule, writable: tuple) -> str | None:
    """The form the guard accepts, for a hit of the deny-by-default rules of section 27.1: a write
    the reader cannot place, a name read from a command's words or code in the start folder, a
    text that does not parse. A pure function of the hit and the job's write folders."""
    if rule == "unplaced":
        return ("Name the file by its absolute path in your own folder, with no variable or "
                "substitution.")
    if rule == "words-here":
        folder = writable[0] if writable else "<your own folder>"
        return ("A command that may write reads its words and code as names in the folder it "
                "runs in; run it after `cd %s &&`." % folder)
    if rule == "parse":
        return "Write the call so that it parses: close every quote and every `$(`."
    return None


def _guarded_roles() -> tuple:
    from alpaca.research import jobs
    return jobs.GUARDED_ROLES


def _pin_event(root: str, scope: str):
    try:
        from alpaca import db
        from alpaca.research import jobs
        conn = db.connect_readonly(root)
        try:
            ev = jobs.newest_pin(conn, scope, scope if scope.startswith("rr-") else None)
            return ev["id"] if ev else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _marked(payload: dict) -> dict | None:
    """The allow decision for a Bash call of a subagent: `updatedInput` is the WHOLE tool input
    with its command behind the marker export. Only a Bash call of a subagent (a payload with an
    `agent_id`) gets one; the driver's calls and every other tool are left alone."""
    agent_id = payload.get("agent_id") or payload.get("agentId")
    tool_input = payload.get("tool_input")
    if not agent_id or str(payload.get("tool_name") or "") != "Bash" \
            or not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
        return None
    updated = dict(tool_input, command=agent_marker.mark(tool_input["command"], agent_id))
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "permissionDecisionReason": "research guard: the shell of a "
                                   "subagent carries %s" % agent_marker.ENV,
                                   "updatedInput": updated}}


def handle(payload: dict) -> dict | None:
    from alpaca import paths
    root = paths.root(payload.get("cwd"))
    why = verdict(payload, root)
    if isinstance(why, Denial):
        # the event first, the decision second (section 18.1)
        why = _write_deny_event(root, payload, why, str(payload.get("agent_id") or payload.get("agentId")))
    if why is None:
        # the marker goes only into the shells of a driver session that has a live run: every
        # session of the project shares this hook, and a marked shell makes every driving verb
        # (and the suite's research tests) refuse, so outside a run it would break the
        # subagents of every other session (hotfix after the rr-002 rehearsal)
        if _marker(root, common.session_of(payload)) is None:
            return None
        return _marked(payload)
    try:
        sid = common.session_of(payload)
        log = os.path.join(root, ".alpaca", "research", "live", "%s.denials.jsonl" % sid)
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"agent_id": payload.get("agent_id"),
                                 "tool": payload.get("tool_name"), "why": str(why)}) + "\n")
    except OSError:
        pass
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": str(why)}}


@common.fail_open(record_cost=False)
def main():
    out = handle(common.read_stdin())
    if out is not None:
        import sys
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
