"""The scripted driving session of the session runtime tests (in-session-runtime.md section 16).

A test module, not a conftest: import it. It plays both halves of a research run that has no real
Claude Code session in it:

  * `Session` is the DRIVER. It sets the environment a Claude Code session has (the runtime marker,
    the config folder, the level and the driver's own transcript), starts the runner in a thread of
    the test process, and drives it only through the verbs `alpaca research next-job` and
    `alpaca research submit` (cli.main), as the /autoresearch skill does.
  * `FakeAgent` is a SUBAGENT. For each job it writes a transcript in the Claude Code record shape
    (the spawn prompt as the first turn, a Read of the prompt file, the role's calls, a final
    message, the model and the usage), the matching pool lines with the agent id, and plays the
    runtime's SubagentStart and SubagentStop hooks by calling the real hook handlers, so the witness
    rows are written by the real hook code.

The scripted behavior of each role is the behavior of the existing scripted agents
(research_loopkit.Agents for the proposer, implementer and notes writer, test_research_audit.Auditor
for the auditor and test_research_verifier.Scripted for the two verifiers). Nothing here starts an
agent runtime or touches the network.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import threading
import time
import uuid

from alpaca import db, pool, util
from alpaca.hooks import subagent_start, subagent_stop
from alpaca.research import events, jobs, paths, runner, session_engine, session_runtime
from alpaca.tests import research_loopkit as loopkit

DRIVER = "drv-0000-1111-2222-333344445555"
MODELS = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-5", "opus": "claude-opus-5-5",
          "fable": "claude-fable-5-1"}


def _iso(offset: float = 0.0) -> str:
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ================================================================ the environment of a session
def drop_foreign_trees(monkeypatch):
    """A legacy alpaca-root.pth (earlier bootstraps wrote one; t-428 removes it) puts the whole main
    tree on sys.path.
    The pin lists every file of a root that is not the project (a test's own tree), so each pin
    check would walk it (hundreds of thousands of files) and a run's jobs would never be handed in
    time. Another tree that holds an `alpaca` package is not this test's engine: drop it from
    sys.path for the test."""
    install = os.path.realpath(session_engine.install_root())
    monkeypatch.setattr(sys, "path", [
        e for e in sys.path
        if os.path.realpath(e or os.getcwd()) == install
        or not os.path.isfile(os.path.join(os.path.realpath(e or os.getcwd()), "alpaca",
                                           "__init__.py"))])


def prepare(monkeypatch, project, *, sid: str = DRIVER, level: int = 6, slug: str = "-proj"):
    """Everything a `start` of a session run checks, under pytest: the runtime marker, a home and a
    config folder of the test's own, the driver's registered transcript and its level. The tests
    cannot be an isolated interpreter, so isolation_ok is true, and the slow parts of the pin (the
    byte-compile of the venv, the block on absent names) are off. Returns the driver's context."""
    import tempfile
    home = os.path.join(os.path.dirname(project), "home")
    cfg = os.path.join(os.path.dirname(project), "cfg")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    os.makedirs(cfg, exist_ok=True)
    # Bound account discovery to this case, retaining project and fixture-parent context.
    # Module seams avoid changing process-wide HOME and preserve explicit fixture overrides.
    boundary = os.path.realpath(os.path.dirname(project))

    def ancestors(root):
        cur = os.path.realpath(root)
        if os.path.commonpath((cur, boundary)) != boundary:
            raise ValueError("session fixture root outside its case")
        out = []
        while True:
            out.append(cur)
            if cur == boundary:
                return out
            cur = os.path.dirname(cur)

    monkeypatch.setattr(session_engine, "home", lambda: os.path.realpath(home))
    monkeypatch.setattr(session_engine, "_ancestors", ancestors)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", cfg)
    for name in ("CODEX_THREAD_ID", "CODEX_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    monkeypatch.setenv("ALPACA_SESSION_ID", sid)
    monkeypatch.setattr(session_runtime, "isolation_ok", lambda: True)
    monkeypatch.setattr(session_engine, "COMPILE_SITE_PACKAGES", False)
    monkeypatch.setattr(session_engine, "PRELOAD_BLOCK", False)
    monkeypatch.setattr(session_runtime, "POLL_S", 0.05)
    monkeypatch.setattr(jobs, "STRAGGLER_WAIT_S", 0.0)
    monkeypatch.setattr(runner, "_TMP_PREFIXES", ())
    drop_foreign_trees(monkeypatch)
    pdir = os.path.join(cfg, "projects", slug)
    os.makedirs(os.path.join(pdir, sid, "subagents"), exist_ok=True)
    transcript = os.path.join(pdir, "%s.jsonl" % sid)
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "sessionId": sid,
                             "message": {"role": "user", "content": "start"}}) + "\n")
    conn = db.connect(project)
    try:
        from alpaca.posture import level as posture_level
        posture_level.set(conn, sid, level, "test the session runtime", root=project)
        db.upsert(conn, "sessions", "sid", {"sid": sid, "started": util.now_iso(), "cwd": project,
                                            "transcript": transcript})
    finally:
        conn.close()
    return {"sid": sid, "cfg": cfg, "home": home, "transcript": transcript, "slug": slug,
            "subagents": os.path.join(pdir, sid, "subagents"), "root": project}


def thread_runner(monkeypatch, root):
    """runner.launch for a session run becomes a thread of the test process: the detached process
    is a subprocess a test never starts. The runner thread runs the foreground runner."""
    threads = []

    def launch(root_, run, *, foreground, session):
        def body():
            runner.main(["--run", run, "--root", root_, "--foreground"])
        th = threading.Thread(target=body, name="runner-main-%s" % run, daemon=True)
        th.start()
        threads.append(th)
        return {"pid": os.getpid(), "host": "test", "log": paths.runner_log(root_, run)}
    monkeypatch.setattr(runner, "launch", launch)
    # `research stop` signals the runner's pid, which here is the test process itself: the signal
    # becomes the runner's own handler, called on the instance of the thread
    instances = {}
    real_init = runner._Runner.__init__

    def init(self, root_, run, **kw):
        real_init(self, root_, run, **kw)
        instances[run] = self

    def request_stop(root_, run):
        inst = instances.get(run)
        if inst is None or inst.stop.is_set():
            return False
        inst._on_signal(15, None)
        return True
    monkeypatch.setattr(runner._Runner, "__init__", init)
    monkeypatch.setattr(runner, "request_stop", request_stop)
    return threads


# ================================================================ a fake subagent
class FakeAgent:
    """One subagent doing one job: writes its transcript and pool lines and plays its hooks.
    `edit(records, ctx)` may rewrite the records before they are written (a test's way to craft a
    bad claim); `skip_hooks` leaves out the SubagentStop (or Start) event."""

    def __init__(self, session: dict, job: dict, root: str, *, behavior=None, agent_id=None,
                 model=None, edit=None, skip_stop=False, skip_start=False, agent_type="general-purpose"):
        self.s, self.job, self.root = session, job, root
        self.behavior = behavior or (lambda role, jobd: {})
        self.agent_id = agent_id or "a%s" % uuid.uuid4().hex[:16]
        self.model = model
        self.edit = edit
        self.skip_stop, self.skip_start, self.agent_type = skip_stop, skip_start, agent_type
        self.path = None
        self.t0 = None

    # ---- the hooks the runtime fires
    def start(self) -> None:
        if not self.skip_start:
            subagent_start.handle({"session_id": self.s["sid"], "agent_id": self.agent_id,
                                   "agent_type": self.agent_type, "cwd": self.root})

    def stop(self) -> None:
        if not self.skip_stop:
            subagent_stop.handle({"session_id": self.s["sid"], "agent_id": self.agent_id,
                                  "agent_type": self.agent_type, "cwd": self.root,
                                  "agent_transcript_path": self.path})

    # ---- the work
    def work(self) -> dict:
        jobd = self.job
        role = jobd["role"]
        act = dict(self.behavior(role, jobd) or {})
        calls = []          # (tool, input, output)
        prompt_path = jobd["prompt_path"]
        prompt_text = open(prompt_path, encoding="utf-8").read()
        numbered = "".join("%6d\t%s\n" % (i + 1, ln) for i, ln in enumerate(prompt_text.split("\n")))
        if not act.get("skip_prompt_read"):
            calls.append(("Read", {"file_path": act.get("read_path", prompt_path)}, numbered))
        final = act.get("final")
        if act.get("calls") is not None:
            role_calls, produced = list(act["calls"]), "the work is done"
        else:
            role_calls, produced = self.role_steps(role, jobd, act)
        calls += role_calls
        if final is None:
            final = produced
        self.records = self.build(calls, final, act)
        return {"agent_id": self.agent_id, "final": final}

    def role_steps(self, role: str, jobd: dict, act: dict) -> tuple:
        cwd = jobd["cwd"]
        shell = "cd %s && " % cwd
        extra = list(act.get("extra_calls") or ())
        conn = db.connect(self.root)
        try:
            if role in ("proposer", "implementer", "notes"):
                ag = loopkit.Agents(self.root)
                research = ag._research(conn, jobd["attempt"])
                value = act["value"] if "value" in act else ag.value(research)
                spec = {"role": role, "attempt": jobd["attempt"], "cwd": cwd}
                files = act.get("files") if role == "implementer" else None
                final = ag.answer(spec, research, {"value": value, "files": files})   # writes the file
                steps = []
                if role != "notes":
                    steps.append(("Bash", {"command": shell + "cat src/value.txt"}, "100\n"))
                if role == "implementer":
                    for rel, text in (files or {"src/value.txt": "%s\n" % value}).items():
                        steps.append(("Write", {"file_path": os.path.join(cwd, rel),
                                                "content": text}, "ok"))
                return steps + extra, final
            if role == "auditor":
                return self.auditor_steps(jobd, act, extra)
            if role == "verifier":
                return self.verifier_steps(jobd, act, extra)
        finally:
            conn.close()
        return extra, "done"

    def auditor_steps(self, jobd: dict, act: dict, extra: list) -> tuple:
        from alpaca.tests import test_research_audit as ta
        folder = jobd["cwd"]
        shell = "cd %s && " % folder
        with open(os.path.join(folder, "packet", "brief.json"), encoding="utf-8") as fh:
            brief = json.load(fh)
        with open(os.path.join(folder, "packet", "audit.log"), encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        body = act.get("body") or ta.default_verdict(brief, lines, act.get("dispose"))
        text = json.dumps(body, sort_keys=True)
        # the try's own output folder, out/t<n>/ (in-session-runtime.md section 28.1)
        out = (jobd.get("write_paths") or [os.path.join(folder, "out")])[0]
        rel = os.path.relpath(out, folder)
        os.makedirs(out, exist_ok=True)
        util.write_text(os.path.join(out, "audit.json"), text + "\n")
        steps = [("Bash", {"command": shell + "cat packet/brief.json"}, json.dumps(brief)),
                 ("Read", {"file_path": os.path.join(folder, "packet", "audit.log")}, "log")]
        steps += extra
        steps.append(("Bash", {"command": shell + "cat > %s/audit.json <<'AUDIT'\n%s\nAUDIT"
                               % (rel, text)}, "ok"))
        return steps, "audit written"

    def verifier_steps(self, jobd: dict, act: dict, extra: list) -> tuple:
        from alpaca.tests import test_research_verifier as tv
        folder = jobd["cwd"]
        plan = act.get("plan") or {}
        plan = dict(plan, no_end=True)
        s = tv.Scripted(self.root, plan=lambda slot, rnd, try_: plan)
        spec = {"workdir": folder, "cwd": folder, "try_n": jobd["try"], "key": jobd["key"],
                "session": jobd["launch_session"], "run": jobd.get("run"), "runtime": "claude",
                "role": "verifier", "attempt": jobd.get("attempt")}
        s(spec)
        stream = os.path.join(folder, "launch", "stream.jsonl")
        steps = []
        pending = {}
        with open(stream, encoding="utf-8") as fh:
            for raw in fh:
                rec = json.loads(raw)
                content = (rec.get("message") or {}).get("content")
                if rec.get("type") == "assistant" and content:
                    for item in content:
                        if item.get("type") == "tool_use":
                            cmd = item["input"].get("command", "")
                            item["input"]["command"] = "cd %s && %s" % (folder, cmd)
                            pending[item["id"]] = item
                elif rec.get("type") == "user" and content:
                    for item in content:
                        if item.get("type") == "tool_result":
                            tu = pending.pop(item["tool_use_id"])
                            steps.append(("Bash", tu["input"], item.get("content")))
        steps += extra
        return steps, "verified the claims"

    # ---- the transcript
    def build(self, calls: list, final: str, act: dict) -> list:
        s, jobd = self.s, self.job
        model = act.get("model") or self.model or MODELS.get(jobd.get("model"), "claude-sonnet-5")
        base = {"sessionId": s["sid"], "agentId": self.agent_id, "isSidechain": True,
                "cwd": s["root"], "version": "2.1.287", "userType": "external",
                "entrypoint": "cli", "gitBranch": "main"}
        records, parent = [], None
        t = [time.time()]

        def stamp():
            t[0] += 0.5
            import datetime
            return datetime.datetime.fromtimestamp(t[0], datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")

        def add(rec):
            nonlocal parent
            u = str(uuid.uuid4())
            body = dict(base, uuid=u, parentUuid=parent, timestamp=stamp())
            body.update(rec)
            records.append(body)
            parent = u

        add({"type": "user", "message": {"role": "user", "content": jobd["spawn_prompt"]}})
        add({"type": "attachment", "attachment": {"type": "model", "identity": {"modelId": model}}})
        n = 0
        for name, inp, out in calls:
            n += 1
            tid = "toolu_%s_%d" % (self.agent_id[:8], n)
            add({"type": "assistant", "message": {
                "id": "msg_%s_%d" % (self.agent_id[:8], n), "role": "assistant", "model": model,
                "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
                "usage": {"input_tokens": 200, "output_tokens": 40, "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0}}})
            add({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": out}]}})
            self._pool(name, inp, tid)
            if act.get("late_pool_gap") and n == act["late_pool_gap"]:
                pass
        add({"type": "assistant", "message": {
            "id": "msg_%s_final" % self.agent_id[:8], "role": "assistant", "model": model,
            "content": [{"type": "text", "text": final}],
            "usage": {"input_tokens": 300, "output_tokens": 60, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}}})
        if self.edit:
            self.edit(records, self)
        return records

    def _pool(self, name, inp, tid) -> None:
        pool.record(self.root, self.s["sid"], "pre", {
            "tool_name": name, "tool_input": inp, "tool_use_id": tid, "cwd": self.s["root"],
            "agent_id": self.agent_id, "agent_type": self.agent_type})

    def write(self) -> str:
        folder = self.s["subagents"]
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "agent-%s.jsonl" % self.agent_id)
        with open(self.path, "w", encoding="utf-8") as fh:
            for rec in self.records:
                fh.write(json.dumps(rec) + "\n")
        with open(self.path[:-6] + ".meta.json", "w", encoding="utf-8") as fh:
            json.dump({"agentType": self.agent_type, "description": self.job.get("description"),
                       "spawnDepth": 1}, fh)
        return self.path


# ================================================================ the driving session
class Session:
    """The driving session of one scripted run."""

    def __init__(self, monkeypatch, project, *, sid: str = DRIVER, level: int = 6,
                 behavior=None, edit=None, model_of=None):
        self.root = project
        self.mp = monkeypatch
        self.ctx = prepare(monkeypatch, project, sid=sid, level=level)
        self.sid = sid
        self.behavior = behavior or (lambda role, jobd: {})
        self.edit = edit
        self.model_of = model_of
        self.agents: list = []
        self.submits: list = []
        self.threads = thread_runner(monkeypatch, project)
        self.submitted: set = set()
        self.log: list = []

    # ---- the verbs
    def verb(self, *argv) -> tuple:
        from alpaca import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--session", self.sid, "research"] + list(argv) + ["--json"])
        text = buf.getvalue().strip().splitlines()
        body = None
        for line in reversed(text):
            try:
                body = json.loads(line)
                break
            except ValueError:
                continue
        return rc, body

    def start(self, runbook=loopkit.RUNBOOK, *extra) -> dict:
        rc, body = self.verb("start", "--runbook", runbook, "--via", "autoresearch", *extra)
        assert rc == 0, body
        return body

    def next_jobs(self, run: str, wait: float = 0.5):
        return self.verb("next-job", run, "--wait", str(wait), "--with-prompt")

    # ---- spawn and submit
    def spawn(self, entries: list, run: str) -> list:
        """One FakeAgent per job entry of a next-job response, all started before any stops (a
        group of verifiers runs at the same time), then run, stopped and returned."""
        agents = []
        for e in entries:
            jobd = jobs.read_job(self.root, run, e["job"])
            jobd = dict(jobd, spawn_prompt=e["spawn_prompt"], prompt_path=e["prompt_path"],
                        description=e.get("description"))
            agents.append(FakeAgent(self.ctx, jobd, self.root, behavior=self.behavior,
                                    edit=self.edit,
                                    model=self.model_of(jobd["role"], jobd) if self.model_of else None))
        for a in agents:
            a.start()
        threads, errors = [], []
        for a in agents:
            def go(a=a):
                try:
                    a.work()
                    a.write()
                except BaseException as exc:      # noqa: BLE001 - re-raised in the driver's thread
                    errors.append(exc)
            th = threading.Thread(target=go)
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
        if errors:
            # a fake agent that broke would leave its claim waiting for a transcript for ever
            raise AssertionError("a fake subagent failed: %r" % (errors[0],))
        for a in agents:
            a.stop()
        self.agents += agents
        return agents

    def submit(self, agents: list, *, wait: float = 30.0, **over) -> list:
        """submit for each agent, in threads: the judge of a verifier pair waits for both claims."""
        from alpaca import cli
        results = []
        lock = threading.Lock()

        def go(a):
            args = ["--session", self.sid, "research", "submit", a.job["job"], "--nonce",
                    a.job["nonce"], "--agent-id", a.agent_id, "--wait", str(wait), "--json"]
            # no redirect here: redirect_stdout swaps the process-wide sys.stdout, and threads that
            # enter and leave it out of order leave a stale buffer behind for the rest of the test
            rc = cli.main(args)
            with lock:
                results.append((a.job["job"], rc))
        ths = [threading.Thread(target=go, args=(a,)) for a in agents]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        self.submits += results
        return results

    def drive(self, run: str, *, timeout: float = 600.0, until=None) -> dict:
        """The skill's step 5: next-job, spawn the jobs, submit them; until the verb says report
        (or hold), or `until(body)` is true."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc, body = self.next_jobs(run)
            self.log.append((rc, body.get("action") if body else None))
            assert body is not None, "next-job printed no JSON"
            if until is not None and until(body):
                return body
            action = body["action"]
            if action == "spawn":
                agents = self.spawn(body["jobs"], run)
                self.submit(agents)
                continue
            if action in ("report", "hold", "resume"):
                return body
        raise AssertionError("the drive loop did not finish in %s seconds; log %r" % (timeout,
                                                                                    self.log[-10:]))


def events_of(root, kind, ref=None):
    return loopkit.events_of(root, kind, ref)


def install_hook_data(cfg: str, sid: str) -> None:
    """A PostToolUse hook whose script prints the driver's autodrive context file, as the account's
    autodrive-inject.sh does, with the three files it reads: the level, the context and the tick
    file (which changes on every call)."""
    hooks = os.path.join(cfg, "hooks")
    os.makedirs(hooks, exist_ok=True)
    script = os.path.join(hooks, "inject.sh")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write('CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"\n'
                 'LEVEL_FILE="$CLAUDE_DIR/.autodrive-level.$SID"\n'
                 'CONTEXT_FILE="$CLAUDE_DIR/.autodrive-context.$SID"\n'
                 'TICK_FILE="$CLAUDE_DIR/.autodrive-tick.$SID"\n'
                 'cat "$LEVEL_FILE" "$CONTEXT_FILE"\n')
    with open(os.path.join(cfg, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump({"hooks": {"PostToolUse": [{"hooks": [
            {"type": "command", "command": "bash %s PostToolUse" % script}]}]}}, fh)
    for name, text in ((".autodrive-level.%s" % sid, "AUTODRIVE L6\n"),
                       (".autodrive-context.%s" % sid, "Declared scope/goal: the test\n"),
                       (".autodrive-tick.%s" % sid, "1")):
        with open(os.path.join(cfg, name), "w", encoding="utf-8") as fh:
            fh.write(text)


# ================================================================ a world for the judge's checks
class JudgeWorld:
    """A scope with a registered driver and a job at a time, without a runner: the tests of the
    judge's checks issue a job, hand it out, let a FakeAgent do it and then ask `jobs.run_checks`
    about the claim, with the table of checks as it is or with one check turned into a swallow
    (the mutation loop of in-session-runtime.md section 16, test 1). The driver event is the one
    `register_driver` writes, less the pin: the pin has its own tests."""

    SCOPE = "rr-001"

    def __init__(self, monkeypatch, project, *, ledger: bool = False, baseline: bool = False,
                 hook_commands=(), pinned: bool = False, hook_data: bool = False):
        from alpaca.research import launcher
        self.root = project
        self.mp = monkeypatch
        self.ctx = prepare(monkeypatch, project)
        self.sid = self.ctx["sid"]
        if hook_data:
            install_hook_data(self.ctx["cfg"], self.sid)
        self.conn = db.connect(project)
        self.attempt = "t-900"
        self.cwd = os.path.join(project, ".alpaca", "worktrees", "research", self.SCOPE, self.attempt)
        os.makedirs(os.path.join(self.cwd, "src"), exist_ok=True)
        self.tries = {}
        self.entries = {}
        self.agents = {}
        cfg = self.ctx["cfg"]
        data = {"scope": self.SCOPE, "session": self.sid, "runtime": "claude", "witness": "hook",
                "cause": "start", "level": 6, "session_cwd": project, "pool_offset": 0,
                "hook_commands": list(hook_commands), "hook_scripts": [], "config_dir": cfg,
                "pin_file": None}
        if baseline:
            data["config_base_sha256"] = session_engine.config_hash(project, self.sid, cfg,
                                                                    scripts=[])
        if ledger:
            fp = session_engine.context_fingerprint(project, cfg, self.sid, runtime="claude",
                                                    transcript=self.ctx["transcript"])
            data["context"] = session_engine.take_ledger(
                jobs.scope_dir(project, self.SCOPE), fp)
        if pinned:
            # the real registration: the pin of the engine, the text sources and the ledger
            self.driver = session_runtime.register_driver(
                self.conn, project, self.SCOPE, self.sid, cause="start", level=6, runtime="claude",
                run=self.SCOPE, op=None)
        else:
            self.driver = events.emit(self.conn, events.DRIVER, run=None, ref=self.SCOPE, op=None,
                                      session=self.sid, actor="operator", data=data)

    # ---- issue and hand
    def make_spec(self, role: str = "implementer", *, key=None, try_n: int = 1, write_paths=None,
                  read_paths=(), scanned_cwd=None, prompt: str = "Do the job.\n", group=None,
                  group_size=None, verifier_slot=None, first_pass=None, tools=None, slot=0,
                  timeout_s: int = 600):
        from alpaca.research.types import LaunchSpec
        key = key or "%s/%s/t%d" % (self.attempt, role, try_n)
        jid = session_runtime._job_id(key)
        cwd = scanned_cwd or self.cwd
        evidence = os.path.join(self.root, ".alpaca", "research", self.SCOPE, "attempts",
                                self.attempt, "launches", jid)
        tools = tools if tools is not None else (("Read", "Bash") if role in ("verifier", "auditor")
                                                 else ("Read", "Write", "Edit", "Bash"))
        spec = LaunchSpec(role=role, runtime="claude", session="%s-%s" % (self.SCOPE, jid),
                          workdir=cwd, cwd=cwd, prompt=prompt, timeout_s=timeout_s, key=key,
                          run=None, attempt=self.attempt, read_paths=tuple(read_paths),
                          write_paths=tuple(write_paths if write_paths is not None else
                                            ((cwd,) if role in ("implementer", "proposer") else ())),
                          tools=tuple(tools), op=None, worker=None, try_n=try_n, round=1, slot=slot,
                          evidence_dir=evidence, group=group, group_size=group_size,
                          first_pass=first_pass, verifier_slot=verifier_slot)
        return spec

    def issue(self, role: str = "implementer", *, override=None, **kw) -> str:
        """Issue one job; `override` is a run's model override (start --model), which
        session_runtime.issue reads from the run's start and passes to resolve_model."""
        spec = self.make_spec(role, **kw)
        jid = session_runtime._job_id(spec.key)
        slot, verifier_slot = spec.slot, spec.verifier_slot
        tier, model = session_runtime.resolve_model(self.root, "claude", role, verifier_slot or slot,
                                                    override=override)
        drv = jobs.newest_driver(self.conn, self.SCOPE)
        cfg_hash = ""
        job = session_runtime._build(self.conn, self.root, spec, self.SCOPE, drv, tier, model,
                                     cfg_hash, None)
        got = session_runtime.commit_issue(self.conn, self.root, spec, self.SCOPE, drv, job, tier,
                                           model, cfg_hash, None)
        assert got["refused"] is None, got
        return jid

    def hand(self) -> list:
        got = jobs.hand_off(self.conn, self.root, self.SCOPE, self.sid, limit=20, run=None)
        for e in got["jobs"]:
            self.entries[e["job"]] = e
        return got["jobs"]

    def job_data(self, jid: str) -> dict:
        e = self.entries[jid]
        jobd = jobs.read_job(self.root, self.SCOPE, jid)
        return dict(jobd, spawn_prompt=e["spawn_prompt"], prompt_path=e["prompt_path"],
                    description=e.get("description"))

    # ---- the agent
    def agent(self, jid: str, **kw) -> "FakeAgent":
        a = FakeAgent(self.ctx, self.job_data(jid), self.root, **kw)
        self.agents[jid] = a
        return a

    def run_agent(self, jid: str, *, stop: bool = True, **kw) -> "FakeAgent":
        a = self.agent(jid, **kw)
        a.start()
        a.work()
        a.write()
        if stop:
            a.stop()
        return a

    # ---- the claim and the judge
    def claim(self, jid: str, a: "FakeAgent", **over) -> int:
        args = {"nonce": self.entries[jid]["nonce"], "agent_id": a.agent_id, "transcript": None,
                "final": None, "usage": None, "tool": None, "tool_use_id": None}
        args.update(over)
        got = jobs.claim(self.conn, self.root, jid, args, caller_session=self.sid,
                         in_subagent=False, session=self.sid)
        return got["n"]

    def ctx_for(self, jid: str, n: int, *, conn=None, **kw) -> "jobs.Ctx":
        folder = paths.job_dir(self.root, self.SCOPE, jid)
        with open(os.path.join(folder, "claims", "%d.json" % n), encoding="utf-8") as fh:
            claim = json.load(fh)
        return jobs.Ctx(conn or self.conn, self.root, self.SCOPE, jid, claim, **kw)

    def judge(self, jid: str, n: int, *, checks=None, conn=None, **kw) -> tuple:
        """(ctx, None) when every check passed, else (ctx, the Problem)."""
        c = self.ctx_for(jid, n, conn=conn, **kw)
        try:
            jobs.run_checks(c, checks)
        except jobs.Problem as exc:
            return c, exc
        return c, None

    def accept(self, c) -> dict:
        return jobs.commit_accept(self.conn, self.root, c, session=self.SCOPE)

    def close(self) -> None:
        self.conn.close()


def swallow(name: str):
    """The table of checks with one check turned into a swallow: it still runs (so its side
    effects, the copy of the transcript among them, still happen) but its refusal is dropped."""
    out = []
    for n, fn in jobs.CHECKS:
        if n == name:
            def quiet(c, fn=fn):
                try:
                    fn(c)
                except jobs.Problem:
                    return None
            out.append((n, quiet))
        else:
            out.append((n, fn))
    return tuple(out)
