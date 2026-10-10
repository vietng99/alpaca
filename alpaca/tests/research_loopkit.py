"""A small research project and scripted agents for the loop runner tests (t-100). A test module,
not a conftest: import it.

The project is a git repository with one number in src/value.txt, a frozen measuring script that
prints that number as the metric `value` (lower is better, target 10) inside the gate chain's
token-closed result block, and one build gate. The scripted agents stand in for the headless
launcher: they never start a runtime or touch the network. Each records the research-launch and
research-launch-end events the launcher records, keyed the same way, so a resumed step finds its
launch on the record; a proposer answers with a proposal, an implementer writes a number into its
worktree, a notes writer answers one line. A `script(role, research, spec)` function may change
what one launch does: return {"final": text} for another final message, {"value": n} for another
number, {"outcome": "quota", "reset_at": iso} or {"outcome": "start-failed"} for an
infrastructure failure, or {"kill": True} to SIGKILL the process that launched it after the
launch event is recorded. The verifiers are the scripted verifiers of test_research_verifier.py,
and the auditor is the scripted auditor of test_research_audit.py. Each scripted launch saves a
Claude stream-json stream (the implementer's shows the files it wrote) and records its path and
sha256 in its research-launch-end, as the launcher does, so the attempt audit (verifier.md section
9.5: a launch with no saved stream blocks the audit) finds the transcript of every launch.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import unittest.mock

from alpaca import db
from alpaca.gates import verdict as vc
from alpaca.research import events, launcher, paths, spend

RUNBOOK = "pack/rb.yaml"

SPEC = """# Feature Specification: a smaller value

## User Scenarios & Testing

### Edge Cases

- **EC-001**: A change outside src/ is refused.

## Requirements

- **FR-001**: The value file holds one whole number.

## Success Criteria

- **SC-001**: The value is at most 10.
- **SC-002**: The value file is present.
"""

MEASURE = """read -r T <&3; exec 3<&-
echo ALPACA-RESULTS-BEGIN
echo "METRIC: value $(cat "$1"/src/value.txt) $T"
echo "ALPACA-RESULTS-END $T"
"""

#: a measuring script that, for the value SLOW, prints MEASURING. In the first run of its gate
#: item (the jail's HOME is <stage>/home/<side>-1-1, gates.py _claim_tag and paths.stage_home)
#: it then waits, at most 50 s (under the metric's 60 s timeout), for the runner's stop to kill
#: it, so a test's signal always lands while the gate runs; a later run of the item (the resume,
#: HOME <side>-2-1) goes on at once
SLOW_MEASURE = """read -r T <&3; exec 3<&-
V=$(cat "$1"/src/value.txt)
if [ "$V" = "SLOW" ]; then
  echo MEASURING
  case "$HOME" in
    */attempt-1-1) i=0; while [ $i -lt 50 ]; do sleep 1; i=$((i+1)); done ;;
  esac
fi
echo ALPACA-RESULTS-BEGIN
echo "METRIC: value $V $T"
echo "ALPACA-RESULTS-END $T"
"""

RB = """runbook: 3
id: loop-improve
title: A smaller value
description: Agents lower the number in src/value.txt.
spec: spec.md
owner: the test

stages:
  - id: build
    title: the value file is present
    run: test -f ${WORKTREE}/src/value.txt
    timeout: 60
    checks:
      - {id: build-exit, type: exit-code, covers: [SC-002], source: spec:SC-002}

research:
  mode: improve
  metrics:
    - id: value
      description: the number in the value file
      command: sh ${CONTRACT_DIR}/pack/measure.sh ${WORKTREE}
      parser: markers
      unit: count
      direction: lower
      kind: deterministic
      timeout: 60
      margin: 0
      target: 10
      covers: [SC-001, FR-001]
      source: spec:SC-001
  accept:
    rule: deficit-pareto
    after_targets: stop
  paths:
    editable: [src/]
    protected: [pack/]
    covers: [EC-001]
    source: spec:EC-001
  gates:
    - {stage: build}
  stop:
    rounds: ROUNDS
    slots: SLOTS
  budget:
    usd: USD
    tokens: 1000000
  disk: DISK
  runtime:
    retries: RETRIES
  categories:
    - {id: small, text: make the number smaller}
    - {id: tidy, text: tidy the file}
  controls:
    noop_rounds: 2
    random: false
  fingerprint:
    tools: [sh]
"""

GIT = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c",
       "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null")


def write(root, rel, text, mode=None):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def git(root, *args):
    return subprocess.run(("git", "-C", root) + GIT + args, check=True, capture_output=True,
                          text=True).stdout


def make_project(root, *, rounds=2, slots=2, retries=2, disk="0.001", value=100, usd=10,
                 slow=None, budget=True):
    """The research project in the `project` fixture's root, committed on main. With `slow`,
    the measuring script counts for a few seconds when the value is that number. With `budget`
    False the runbook sets no research.budget, so the run is uncapped (t-297)."""
    write(root, "project.yaml", "name: loop-test\n")
    write(root, ".gitignore", ".alpaca/\ncontracts/research/\n")
    write(root, "src/value.txt", "%d\n" % value)
    write(root, "pack/spec.md", SPEC)
    measure = MEASURE if slow is None else SLOW_MEASURE.replace("SLOW", str(slow))
    write(root, "pack/measure.sh", measure, 0o755)
    text = (RB.replace("ROUNDS", str(rounds)).replace("SLOTS", str(slots))
            .replace("RETRIES", str(retries)).replace("DISK", str(disk)).replace("USD", str(usd)))
    if not budget:
        text = text.replace("  budget:\n    usd: %s\n    tokens: 1000000\n" % usd, "")
        assert "budget" not in text
    write(root, RUNBOOK, text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def default_value(research):
    """The number an implementer writes: round 1 gives 60 and 80, round 2 gives 40 and 70, and
    so on, so slot 0 wins every round and the target (10) is never met in a few rounds."""
    r, s = int(research.get("round") or 0), int(research.get("slot") or 0)
    return max(11, 100 - 20 * r - 20 * (1 - s))


class Agents:
    """The scripted stand-in for launcher.launch (lifecycle._hooks["launch"]). Each launch spends
    `tokens` tokens and `cost_usd` dollars; with `priced` False it books no dollars and its end
    reads `priced: false` (a launch no rate card priced)."""

    tokens = 100
    cost_usd = 0.01
    priced = True

    def __init__(self, root, script=None, *, value=default_value):
        self.root = root
        self.script = script or (lambda role, research, spec: {})
        self.value = value
        self.calls = []
        self.lock = threading.Lock()

    def _research(self, conn, task):
        row = conn.execute("SELECT data FROM events WHERE kind='task-add' AND ref=? LIMIT 1",
                           (task,)).fetchone()
        return (json.loads(row["data"]).get("research") or {}) if row else {}

    def _find(self, conn, kind, key):
        row = conn.execute("SELECT id, data FROM events WHERE kind=? AND json_extract(data, "
                           "'$.key')=? ORDER BY id DESC LIMIT 1", (kind, key)).fetchone()
        return None if row is None else {"id": row["id"], "data": json.loads(row["data"])}

    def _result(self, spec, end_id, d):
        return {"session": spec["session"], "rc": d.get("rc"), "timed_out": False, "held": False,
                "launch_id": paths.launch_id(spec["key"]), "key": spec["key"],
                "role": spec["role"], "try_n": spec["try_n"], "outcome": d.get("outcome"),
                "launch_end": end_id, "infra": d.get("infra"), "reason": d.get("reason") or "",
                "final_message": d.get("final_message"), "tokens": d.get("tokens"),
                "cost_usd": d.get("cost_usd"), "priced": bool(d.get("priced")),
                "reset_at": d.get("reset_at"), "attempt": spec.get("attempt")}

    def __call__(self, spec, *, conn=None, root=None):
        own = conn is None
        conn = conn or db.connect(root or self.root)
        try:
            return self._launch(dict(spec), conn)
        finally:
            if own:
                conn.close()

    def _launch(self, spec, conn):
        key, task, run = spec["key"], spec["attempt"], spec["run"]
        ended = self._find(conn, events.LAUNCH_END, key)
        if ended is not None:
            return self._result(spec, ended["id"], ended["data"])
        started = self._find(conn, events.LAUNCH, key)
        if started is not None:
            if self._find(conn, events.INFRA, key) is None:
                events.emit(conn, events.INFRA, run=run, ref=task, op=spec.get("op"),
                            session=spec["session"], code=vc.BLOCKED,
                            data={"attempt": task, "role": spec["role"], "try": spec["try_n"],
                                  "class": "lost", "detail": "found on resume", "wait_until": None,
                                  "key": key, "launch": paths.launch_id(key)})
            return {"session": spec["session"], "held": False, "outcome": "lost",
                    "infra": "lost", "key": key, "role": spec["role"], "try_n": spec["try_n"]}
        research = self._research(conn, task)
        act = dict(self.script(spec["role"], research, spec) or {})
        with self.lock:
            self.calls.append((spec["role"], task, spec["try_n"], dict(act)))
        launch = paths.launch_id(key)
        folder = os.path.join(paths.attempt_dir(self.root, run, task), "launches", launch)
        os.makedirs(folder, exist_ok=True)
        prompt = os.path.join(folder, "prompt.txt")
        with open(prompt, "w", encoding="utf-8") as fh:
            fh.write(spec["prompt"])
        start = events.emit(conn, events.LAUNCH, run=run, ref=task, op=spec.get("op"),
                            session=spec["session"],
                            data={"key": key, "round": spec.get("round"), "slot": spec.get("slot"),
                                  "role": spec["role"], "attempt": task,
                                  "worker": spec.get("worker"), "session": spec["session"],
                                  "try": spec["try_n"], "launch": launch,
                                  "runtime": spec["runtime"], "cwd": spec["cwd"],
                                  "prompt": os.path.relpath(prompt, self.root),
                                  "prompt_sha256": hashlib.sha256(
                                      spec["prompt"].encode("utf-8")).hexdigest(),
                                  "max_usd": spec.get("max_usd")})
        if act.get("kill"):
            os.kill(os.getpid(), signal.SIGKILL)
        outcome = act.get("outcome", "ok")
        final = act.get("final")
        if outcome == "ok" and final is None:
            final = self.answer(spec, research, act)
        stream = os.path.join(folder, "stream.jsonl")
        stream_text = self.stream_text(spec, final, act)
        with open(stream, "w", encoding="utf-8") as fh:
            fh.write(stream_text)
        infra = outcome if outcome in ("start-failed", "quota", "auth", "crash") else None
        data = {"key": key, "attempt": task, "role": spec["role"], "session": spec["session"],
                "try": spec["try_n"], "launch": launch, "outcome": outcome,
                "rc": 0 if outcome == "ok" else 1, "timed_out": outcome == "timeout",
                "tokens": self.tokens, "cost_usd": self.cost_usd if self.priced else None,
                "priced": bool(self.priced), "final_message": final,
                "infra": infra, "reason": act.get("reason") or "",
                "reset_at": act.get("reset_at"), "launch_event": start["id"],
                "stream": os.path.relpath(stream, self.root),
                "stream_sha256": hashlib.sha256(stream_text.encode("utf-8")).hexdigest()}
        band = vc.PASS if outcome == "ok" else vc.BLOCKED if infra else vc.FAIL
        with db.transaction(conn):
            end = events.emit(conn, events.LAUNCH_END, run=run, ref=task, op=spec.get("op"),
                              session=spec["session"], code=band, data=data, in_txn=True)
            if infra:
                events.emit(conn, events.INFRA, run=run, ref=task, op=spec.get("op"),
                            session=spec["session"], code=vc.BLOCKED, in_txn=True,
                            data={"attempt": task, "role": spec["role"], "try": spec["try_n"],
                                  "class": infra, "detail": "scripted %s" % infra,
                                  "wait_until": (launcher._wait_until(act.get("reset_at"),
                                                                      spec["try_n"])
                                                 if infra == "quota" else None),
                                  "key": key, "launch": launch})
            spend.book(conn, run, task, cost_usd=self.cost_usd if self.priced else None,
                       tokens=self.tokens, session=spec["session"], in_txn=True)
        return self._result(spec, end["id"], data)

    def stream_text(self, spec, final, act) -> str:
        """The Claude stream-json stream the launch would have saved: an implementer's shows one
        Write per file it put into its worktree, then the final message."""
        lines = [{"type": "system", "subtype": "init", "session_id": "scripted"}]
        if spec["role"] == "implementer" and final is not None and "files" not in act:
            targets = {"src/value.txt": None}
        else:
            targets = dict.fromkeys(act.get("files") or (), None)
        for n, rel in enumerate(targets):
            lines.append({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "tu%d" % n, "name": "Write",
                 "input": {"file_path": os.path.join(spec["cwd"], rel), "content": ""}}]}})
            lines.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu%d" % n, "content": "ok"}]}})
        if final:
            lines.append({"type": "assistant", "message": {"content": [
                {"type": "text", "text": final}]}})
        lines.append({"type": "result", "subtype": "success", "is_error": False})
        return "\n".join(json.dumps(x) for x in lines) + "\n"

    def answer(self, spec, research, act):
        role = spec["role"]
        if role == "proposer":
            value = act.get("value", self.value(research))
            body = {"attempt": spec["attempt"], "title": "set the value to %d" % value,
                    "hypothesis": "A smaller number in the value file lowers the metric.",
                    "files": ["src/value.txt"], "expected": {"value": value}}
            if research.get("category"):
                body["category"] = research["category"]
            return json.dumps(body)
        if role == "implementer":
            value = act.get("value", self.value(research))
            for rel, text in (act.get("files") or {"src/value.txt": "%d\n" % value}).items():
                write(spec["cwd"], rel, text)
            return "wrote %d into src/value.txt" % value
        if role == "notes":
            return "round %s slot %s tried value %s" % (research.get("round"), research.get("slot"),
                                                        self.value(research))
        return "done"


def verifier_launch(root):
    """The scripted verifier of test_research_verifier.py: it re-runs every claim's receipts
    through the real verifier.serve and submits its verdicts."""
    from alpaca.tests import test_research_verifier as tv
    return tv.Scripted(root)


def audit_launch(root):
    """The scripted auditor of test_research_audit.py: it reads the packet the audit built and
    writes a verdict that confirms each candidate (the kit's attempts raise none)."""
    from types import SimpleNamespace
    from alpaca.tests import test_research_audit as ta
    return ta.Auditor(SimpleNamespace(root=root))


def install(monkeypatch, root, agents, *, verify=None, auditor=None):
    """Point the lifecycle at the scripted agents, verifiers and auditor, and let the controller
    run plans in a project under the temp root."""
    from alpaca.research import lifecycle, runner
    monkeypatch.setitem(lifecycle._hooks, "launch", agents)
    monkeypatch.setitem(lifecycle._hooks, "verify_launch", verify or verifier_launch(root))
    monkeypatch.setitem(lifecycle._hooks, "audit_launch", auditor or audit_launch(root))
    monkeypatch.setattr(runner, "_TMP_PREFIXES", ())


def start(root, *, burn_in=False, **kw):
    """runner.open_run with the loop's usual arguments. A real run waits for the no-op burn-in
    (FR-044), which these loop tests do not exercise, so by default the burn-in reads done; pass
    burn_in=True to run the real check (test_research_controls.py does)."""
    from alpaca.research import controls, runner
    args = dict(runbook=RUNBOOK, task=None, op=None, control=None, via="test", level="L6",
                session="test", runtime="control")
    args.update(kw)
    replaced = getattr(controls.burn_in, "__module__", "") != controls.__name__   # a test's own
    if burn_in or args["control"] is not None or replaced:
        return runner.open_run(root, **args)
    done = {"state": "done", "run": "burn-in-fixture", "rounds": 2, "of": 2}
    with unittest.mock.patch.object(controls, "burn_in", lambda conn, sha: dict(done)):
        return runner.open_run(root, **args)


def events_of(root, kind, ref=None):
    conn = db.connect(root)
    try:
        sql, params = "SELECT id, ref, data FROM events WHERE kind=?", [kind]
        if ref is not None:
            sql += " AND ref=?"
            params.append(ref)
        return [{"id": r["id"], "ref": r["ref"], "data": json.loads(r["data"])}
                for r in conn.execute(sql + " ORDER BY id", params).fetchall()]
    finally:
        conn.close()


def tasks_of(root, run):
    """key -> task id of every task the run added."""
    from alpaca.research import lifecycle
    conn = db.connect(root)
    try:
        return lifecycle.run_keys(conn, run)
    finally:
        conn.close()


def clean(path):
    shutil.rmtree(path, ignore_errors=True)


def kill_at(role, round_n, slot, try_n=1):
    """A script that SIGKILLs the launching process at one launch, after its research-launch."""
    def script(r, research, spec):
        if r == role and research.get("round") == round_n and research.get("slot") == slot \
                and spec.get("try_n") == try_n:
            return {"kill": True}
        return {}
    return script


def child_main(root, run, role, round_n, slot):
    """The body of a runner process that kills itself at one launch: the hooks installed without
    pytest's monkeypatch, then runner.main in the foreground."""
    from alpaca.research import lifecycle, runner
    lifecycle._hooks["launch"] = Agents(root, kill_at(role, round_n, slot))
    lifecycle._hooks["verify_launch"] = verifier_launch(root)
    lifecycle._hooks["audit_launch"] = audit_launch(root)
    runner._TMP_PREFIXES = ()
    return runner.main(["--run", run, "--root", root, "--foreground"])
