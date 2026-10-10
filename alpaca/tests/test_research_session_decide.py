"""Fail case 2 of the in-session runtime: a subagent result accepted without the gates,
fingerprints and verifier verdicts the runner records (in-session-runtime.md sections 12.4 and 16).

It is impossible by construction. `submit` writes a claim; the judge writes the result and the
launch end; whether an attempt is accepted, merged or verified is decided by other events that only
the runner's plan steps and the verifier driver write.
"""
from __future__ import annotations

import ast
import json
import os

import pytest

from alpaca import db
from alpaca.research import events, jail
from alpaca.tests import research_loopkit as kit
from alpaca.tests.conftest import REPO
from alpaca.tests.test_research_session_runtime import _new_run, _why, needs_jail

RESEARCH = os.path.join(REPO, "alpaca", "research")

#: the modules that decide an attempt: the accept rule, the gates, the fingerprints, the proof flow,
#: the verifier's verdict writers, the audit, the holdout stage and the plan steps
DECIDING = ("accept", "gates", "fingerprint", "proofs", "verify_gate", "verifier", "audit",
            "holdout", "lifecycle", "repair", "isolation", "merge")
#: the kinds the judge, the claim and the driver registration may write
ALLOWED_KINDS = {"LAUNCH", "LAUNCH_END", "INFRA", "JOB", "JOB_HANDED", "JOB_CLAIM", "JOB_RESULT",
                 "JOB_REFUSED", "JOB_EXPIRED", "JOB_LATE", "DRIVER", "DRIVER_AUDIT", "REPIN"}
DECIDING_KINDS = {"GATE", "TAMPER", "ACCEPT", "AUDIT_VERDICT", "PROOF_VERDICT", "PROOF_RULING",
                  "MERGE", "ROUND", "ATTEMPT", "VERIFY_END", "FINALIZE", "DIFF"}


def _imports(node):
    """The research modules an AST subtree imports, at any depth."""
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.ImportFrom):
            if sub.module == "alpaca.research":
                names |= {a.name for a in sub.names}
            elif sub.module and sub.module.startswith("alpaca.research."):
                names.add(sub.module.split(".")[2])
        elif isinstance(sub, ast.Import):
            for a in sub.names:
                if a.name.startswith("alpaca.research."):
                    names.add(a.name.split(".")[2])
    return names


def _emitted(node):
    """The names X of `events.X` passed as the second argument of an `emit` call."""
    kinds = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and getattr(sub.func, "attr", None) == "emit" and \
                len(sub.args) >= 2 and isinstance(sub.args[1], ast.Attribute) and \
                isinstance(sub.args[1].value, ast.Name) and sub.args[1].value.id in ("events", "rev"):
            kinds.add(sub.args[1].attr)
    return kinds


def _tree(name):
    with open(os.path.join(RESEARCH, name), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _handler(tree, *names):
    return [fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and fn.name in names]


def test_verbs_import_no_deciding_module():
    for module in ("jobs.py", "session_runtime.py", "session_engine.py", "agent_transcript.py"):
        got = _imports(_tree(module)) & set(DECIDING)
        # session_runtime reaches the verifier only to read its allow list (SHELL_ALLOW), which
        # decides nothing
        if module == "session_runtime.py":
            got -= {"verifier"}
        assert got == set(), (module, got)
    cli = _tree("cli.py")
    handlers = _handler(cli, "_next_job", "_submit", "_job_refusal", "_session_check",
                        "_session_status", "_session_status_lines")
    assert len(handlers) == 6
    for fn in handlers:
        assert _imports(fn) & set(DECIDING) == set(), fn.name


def test_the_judge_the_claim_and_the_driver_write_only_the_listed_kinds():
    for module in ("jobs.py", "session_runtime.py"):
        kinds = _emitted(_tree(module))
        assert kinds, module
        assert kinds <= ALLOWED_KINDS, (module, sorted(kinds - ALLOWED_KINDS))
        assert not kinds & DECIDING_KINDS, module
    cli = _tree("cli.py")
    for fn in _handler(cli, "_next_job", "_submit"):
        assert not _emitted(fn) & DECIDING_KINDS, fn.name


def test_the_ast_checks_see_a_planted_import_and_a_planted_write():
    bad = ast.parse("def f(conn):\n    from alpaca.research import accept\n"
                    "    events.emit(conn, events.ACCEPT, run='r')\n")
    assert _imports(bad) & set(DECIDING) == {"accept"}
    assert _emitted(bad) == {"ACCEPT"} and _emitted(bad) & DECIDING_KINDS


def _flawed(role, jobd):
    """Slot 0 of round 1 reaches the target. Slot 1's implementer writes a value the measuring
    gate cannot read, in a file the allow list lets it change, and its claim is flawless."""
    if role in ("proposer", "implementer") and jobd.get("round") == 1:
        if jobd.get("slot") == 0:
            return {"value": 10}
        if role == "implementer":
            return {"value": 80, "files": {"src/value.txt": "banana\n"}}
        return {"value": 80}
    return {}


@needs_jail
def test_flawless_result_with_failing_gates_is_rejected(project, monkeypatch):
    root, session = _new_run(project, monkeypatch, behavior=_flawed)
    run = session.start()["run"]
    done = session.drive(run, timeout=900)
    assert done["action"] == "report", _why(root, run, done)
    conn = db.connect(root)
    try:
        rows = conn.execute("SELECT id, kind, ref, data FROM events ORDER BY id").fetchall()
    finally:
        conn.close()
    data = lambda r: json.loads(r["data"])          # noqa: E731
    impl = [r for r in rows if r["kind"] == events.JOB and data(r)["role"] == "implementer"]
    flawed = [r for r in impl if data(r).get("slot") == 1]
    assert len(flawed) == 1
    attempt = flawed[0]["ref"]
    # the claim passed every check: the judge wrote the result for the job, a flawless one ...
    results = {data(r)["job"]: r for r in rows if r["kind"] == events.JOB_RESULT}
    assert data(flawed[0])["job"] in results
    assert data(results[data(flawed[0])["job"]])["outcome"] == "ok"
    assert not [r for r in rows if r["kind"] == events.JOB_REFUSED and r["ref"] == attempt]
    # ... and still no accept, no merge and no champion came of it: the gate decided
    gates = [data(r) for r in rows if r["kind"] == events.GATE and r["ref"] == attempt]
    assert gates and any(g.get("verdict") != "PASS" for g in gates), gates
    accepts = [data(r) for r in rows if r["kind"] == events.ACCEPT and r["ref"] == attempt]
    assert not any(a.get("accepted") or a.get("verdict") == "accepted" for a in accepts), accepts
    merges = [data(r) for r in rows if r["kind"] == events.MERGE]
    assert merges and all(m.get("attempt") != attempt for m in merges)
    # control: the attempt of slot 0 went the whole way
    good = [r for r in impl if data(r).get("slot") == 0][0]["ref"]
    assert any(data(r).get("attempt") == good for r in rows if r["kind"] == events.MERGE)


@needs_jail
def test_submit_writes_no_verdict(project, monkeypatch):
    root, session = _new_run(project, monkeypatch, behavior=lambda role, jobd: {})
    run = session.start()["run"]
    session.drive(run, timeout=900)
    conn = db.connect(root)
    try:
        rows = conn.execute("SELECT id, kind, actor, session FROM events WHERE kind IN (?, ?, ?)",
                            (events.PROOF_VERDICT, events.JOB_CLAIM, events.JOB_RESULT)).fetchall()
    finally:
        conn.close()
    verdicts = [r for r in rows if r["kind"] == events.PROOF_VERDICT]
    claims = [r for r in rows if r["kind"] == events.JOB_CLAIM]
    assert verdicts and claims
    # every verdict was written by the runner's verifier driver, under the runner's own session,
    # never by the session that typed `submit`; the claims are the driver's own rows
    assert {r["session"] for r in verdicts} == {"research-%s" % run}
    assert {r["session"] for r in claims} == {session.sid}
    assert {r["actor"] for r in verdicts}.isdisjoint({r["actor"] for r in claims})
    # and no verdict row came into the record between a claim and the judge's own end rows
    assert not {r["session"] for r in verdicts} & {session.sid}
