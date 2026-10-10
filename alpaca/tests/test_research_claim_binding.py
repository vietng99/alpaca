"""t-296: every claim of a seal names what a receipt of the verifier's own try checks.

F43 (rr-014): claim s1 of an engine attempt bundled "the attempt was recorded once and the report
rendered from the record", which no verifier can check (the record is out of its reach by design),
so the two verifiers swapped between supported and cannot-verify every round and every s1 dispute
ran to the round cap. F50 (rr-023): the same glance said the gate chain and the accept rule ran for
an attempt that broke at schema, and both verifiers refuted it. The fail case of the contract: a
change that lets a verifier support a claim with no receipt of its own (`requires: any`, which C5
passed on the apply receipt every engine try writes first).

The rule pinned here (design in-session-runtime.md 25.2 and 25.3):

* the engine glances and Result lines are written from their rows and name each row as `event:N`,
  so s1 and the Result items are bound to sealed pointers (`rehash`) or to gate items (`rerun`);
* the extractor never writes `requires: any`; a claim with no binding stops the verification at
  open (VERIFY-NO-COMMAND), and C5 refuses an unknown class and an `any` claim (left only in a
  claims.json written before t-296) that cites the apply receipt alone;
* "recorded once" is checked by code: the render refuses a record with more than one attempt row;
* the done-bar-0 replay (synthetic, the shape of the rr-014 vf-t-294 rounds only): the old s1
  stays disputed, the new s1 settles in round 0, and the new s1 on the apply receipt is refused.

Synthetic data only: placeholder run rr-901, op op-001, a test project record, and the shipped
placeholder rr-004 kit of alpaca/tests (research_rr004_kit) for the judge cases.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading

import pytest

from alpaca import db, proof
from alpaca.gates import verdict as vc
from alpaca.research import claims, events as rev, proofs, rerun, verdicts, verifier
from alpaca.tests import research_rr004_kit as kit
from alpaca.tests.conftest import REPO

RUN, OP, SESSION = "rr-901", "op-001", "research-rr-901"
#: the clauses of the old attempt glance a verifier cannot check from its packet
RECORD_ONLY = ("recorded once", "rendered from the record")
CHAIN = ("build", "unit", "metric:total_bytes")


# ------------------------------------------------------------------ a synthetic record
def _task(conn, title, research):
    from alpaca import ops
    return ops.add_task(conn, OP, title, title, extra={"research": research})


def attempt(project, shape, *, key="rr-901:r1:s0", baseline=False):
    """One attempt task of run RUN in `shape` (schema, gate-failed, accepted, refused) and return
    (conn, task). `baseline` marks the round 0 baseline."""
    conn = db.connect(project)
    research = {"run": RUN, "role": "attempt", "attempt": key, "key": key}
    if baseline:
        research["baseline"] = True
    task = _task(conn, "attempt %s" % shape, research)

    def emit(kind, data, code=None):
        return rev.emit(conn, kind, run=RUN, ref=task, op=OP, session=SESSION, code=code, data=data)

    emit(rev.TAMPER, {"point": "open", "verdict": "PASS", "path": None})
    data = {"forked_from": "a" * 40, "head": "a" * 40, "accept_event": None, "audit_event": None}
    if shape == "schema":
        emit(rev.PROPOSAL, {"valid": False, "problems": [{"code": "P-SHAPE", "where": "plan"}]})
        data.update(outcome="broken", broken_class="schema", reason="the proposal fails its shape")
    elif shape == "refused":
        emit(rev.PROPOSAL, {"valid": True, "problems": []})
        emit(rev.DIFF_REFUSED, {"step": "capture", "found": 2})
        data.update(outcome="broken", broken_class="diff", reason="two diff events")
    elif shape == "gate-failed":
        if not baseline:
            emit(rev.PROPOSAL, {"valid": True, "problems": []})
        emit(rev.DIFF, {"path": None, "sha256": "b" * 64, "bytes": 10, "empty": False})
        emit(rev.GATE, {"stage": "build", "verdict": "PASS", "rc": 0, "side": "attempt", "log": None})
        emit(rev.GATE, {"stage": "unit", "verdict": "FAIL", "rc": 1, "side": "attempt", "log": None})
        data.update(outcome="gate-failed", broken_class=None, reason="unit failed",
                    diff={"path": "diff.patch", "sha256": "b" * 64, "bytes": 10, "empty": False})
    elif shape == "accepted":
        if not baseline:
            emit(rev.PROPOSAL, {"valid": True, "problems": []})
        emit(rev.DIFF, {"path": None, "sha256": "c" * 64, "bytes": 12, "empty": False})
        for stage in CHAIN:
            emit(rev.GATE, {"stage": stage, "verdict": "PASS", "rc": 0, "side": "attempt", "log": None})
        acc = emit(rev.ACCEPT, {"accepted": True, "outcome": "improvement", "phase": "strict",
                                "reason": "unit and build passed, 90 against 100", "score": 90.0,
                                "champion_score": 100.0}, code=vc.PASS)
        aud = emit(rev.AUDIT_VERDICT, {"flags": []}, code=vc.PASS)
        emit(rev.TAMPER, {"point": "close", "verdict": "PASS", "path": None})
        data.update(outcome="improvement", broken_class=None, reason=None, accept_event=acc["id"],
                    audit_event=aud["id"],
                    diff={"path": "diff.patch", "sha256": "c" * 64, "bytes": 12, "empty": False},
                    metrics={"total_bytes": {"repeats": [90.0, 90.0, 90.0], "median": 90.0, "n_ok": 3}},
                    champion_metrics={"total_bytes": {"median": 100.0}})
    else:
        raise ValueError(shape)
    emit(rev.ATTEMPT, data, code=vc.PASS if shape == "accepted" else vc.FAIL)
    return conn, task


def repair(project, shape):
    conn = db.connect(project)
    task = _task(conn, "repair %s" % shape, {"run": RUN, "role": "repair", "case": "c1", "key": "rr-901:c1"})

    def emit(kind, data):
        return rev.emit(conn, kind, run=RUN, ref=task, op=OP, session=SESSION, data=data)

    if shape == "reproducer-unproved":
        emit(rev.PREPARE, {"stage": "prepare", "ok": True, "head": "d" * 40})
        rep = emit(rev.REPRODUCER, {"proved": False, "origin": "given", "tests": ["t_a"]})
        emit(rev.REPAIR_RESULT, {"outcome": "reproducer-unproved", "fix_attempt": None,
                                 "reproducer_event": rep["id"]})
    elif shape == "prepare-failed":
        prep = emit(rev.PREPARE, {"stage": "prepare", "ok": False})
        emit(rev.REPAIR_RESULT, {"outcome": "prepare-failed", "fix_attempt": None,
                                 "prepare_event": prep["id"]})
    elif shape == "fixed":
        prep = emit(rev.PREPARE, {"stage": "prepare", "ok": True, "head": "d" * 40})
        rep = emit(rev.REPRODUCER, {"proved": True, "origin": "given", "tests": ["t_a"]})
        emit(rev.BASELINE, {"pass_to_pass": {"base_pass": ["t_b"]}})
        emit(rev.REPAIR_RESULT, {"outcome": "fixed", "fix_attempt": None,
                                 "reproducer_event": rep["id"], "prepare_event": prep["id"]})
    elif shape == "no-result":
        emit(rev.PREPARE, {"stage": "prepare", "ok": True, "head": "d" * 40})
    else:
        raise ValueError(shape)
    return conn, task


def report_task(project):
    conn = db.connect(project)
    task = _task(conn, "report", {"run": RUN, "role": "report", "key": RUN})
    rev.emit(conn, rev.FINALIZE, run=RUN, ref=RUN, op=OP, session=SESSION, data={"state": "done"})
    rev.emit(conn, rev.REPORT, run=RUN, ref=RUN, op=OP, session=SESSION, data={})
    return conn, task


@pytest.fixture
def contract(monkeypatch):
    """The synthetic run has no contract folder: the render gets a marker contract and the gate
    block of each item of CHAIN on side tree, in the shape proofs._gate_blocks writes."""
    monkeypatch.setattr(proofs, "_contract", lambda root, run: object())
    monkeypatch.setattr(proofs, "_gate_blocks", lambda c, repair_case=None, both=False: [
        "```\n# alpaca-gate %s side tree\nmake %s\n```" % (item, item.replace(":", "-")) for item in CHAIN])


def parts(conn, project, task):
    return proofs._attempt_sections(conn, project, task, proofs._research(conn, task))


def glance_of(text):
    return " ".join(proof.split_sections(text)[proof.SUMMARY].split())


def evidence_of(conn, text):
    """The seal's evidence items for the `event:` lines of the report's Evidence section, sealed by
    the row's chain hash as alpaca/proof.py seals an event pointer."""
    out = []
    for line in proof.split_sections(text).get(proof.EVIDENCE, "").splitlines():
        line = line.strip()
        if not line.startswith("- event:"):
            continue
        ptr = line[2:].split(" ", 1)[0]
        row = conn.execute("SELECT hash FROM events WHERE id=?", (int(ptr.split(":")[1]),)).fetchone()
        out.append({"pointer": ptr, "kind": "event", "note": None, "sha256": row["hash"],
                    "bytes": None, "verified": True})
    return out


def extract(conn, project, task, text=None):
    """claims.extract over the rendered report, written where proof.seal writes it and named by a
    hand-made seal payload whose evidence is the report's own event lines."""
    body = proofs.render_attempt(conn, project, task) if text is None else text
    rel = os.path.join(".alpaca", "proofs", OP, "%s.md" % task)
    full = os.path.join(project, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    data = body.encode("utf-8")
    with open(full, "wb") as fh:
        fh.write(data)
    seal = {"id": 10 ** 6, "data": {"path": rel, "sha256": hashlib.sha256(data).hexdigest(),
                                    "evidence": evidence_of(conn, body)}}
    return claims.extract(conn, project, task, seal)


ATTEMPT_SHAPES = [("schema", False), ("gate-failed", False), ("accepted", False), ("refused", False),
                  ("accepted", True), ("gate-failed", True)]


# ------------------------------------------------------------------ 1. the glance states what ran
@pytest.mark.parametrize("shape,baseline", ATTEMPT_SHAPES)
def test_the_attempt_glance_and_what_name_only_the_steps_whose_rows_exist(project, contract, shape, baseline):
    conn, task = attempt(project, shape, baseline=baseline)
    try:
        p = parts(conn, project, task)
        aid = conn.execute("SELECT id FROM events WHERE kind=? AND ref=?", (rev.ATTEMPT, task)).fetchone()["id"]
    finally:
        conn.close()
    g, what = p["glance"].lower(), " ".join(p["what"]).lower()
    for clause in ("gate chain", "accept rule", "audit", "fingerprint", "recorded once", "the record"):
        assert clause not in g, (clause, p["glance"])
    assert ("gate chain" in what) == bool(p["gates"]), p["what"]
    assert ("accept rule" in what) == (shape == "accepted"), p["what"]
    assert ("audit," in what or "audit and" in what) == (shape == "accepted"), p["what"]
    assert ("capture of one diff" in what) == (shape in ("gate-failed", "accepted")), p["what"]
    assert ("proposal check" in what) == (not baseline), p["what"]
    # the facts a verifier checks on the attempt row stay, and the glance names that row
    verdict = {"schema": "broken", "refused": "broken", "gate-failed": "rejected", "accepted": "accepted"}[shape]
    outcome = {"schema": "broken", "refused": "broken", "gate-failed": "gate-failed",
               "accepted": "improvement"}[shape]
    for fact in (RUN, task, "verdict %s" % verdict, "outcome %s" % outcome, "(event:%d)" % aid):
        assert fact in p["glance"], (fact, p["glance"])
    # the verdict line states what the row holds (the outcome) and names it, as the glance does
    assert "- the attempt event records outcome %s, which gives verdict %s (event:%d)" % (
        outcome, verdict, aid) in p["result"], p["result"]
    if shape == "schema":
        assert "class schema" in p["glance"]
        assert any("no gate item ran" in line for line in p["result"])
    assert 40 <= len(p["glance"]) <= 600


@pytest.mark.parametrize("shape", ["reproducer-unproved", "prepare-failed", "fixed"])
def test_the_repair_glance_follows_its_rows(project, contract, shape):
    conn, task = repair(project, shape)
    try:
        g = glance_of(proofs.render_attempt(conn, project, task))
        got = {c["id"]: c for c in extract(conn, project, task)["claims"]}
    finally:
        conn.close()
    assert "outcome %s" % shape in g and "case c1" in g, g
    assert ("reproducer is proved" in g) == (shape == "fixed"), g
    assert "base FAIL shown live" not in g and "verified and merged" not in g, g
    assert 40 <= len(g) <= 600
    if "y1" in got:
        assert ("was proved" in got["y1"]["text"]) == (shape == "fixed"), got["y1"]


@pytest.mark.parametrize("which", ["schema", "gate-failed", "accepted", "refused", "reproducer-unproved",
                                   "prepare-failed", "fixed", "report"])
def test_no_claim_holds_a_record_only_clause_and_every_claim_is_bound(project, contract, which):
    if which == "report":
        conn, task = report_task(project)
    elif which in ("reproducer-unproved", "prepare-failed", "fixed"):
        conn, task = repair(project, which)
    else:
        conn, task = attempt(project, which)
    try:
        got = extract(conn, project, task)
    finally:
        conn.close()
    for c in got["claims"]:
        text = " ".join(str(c["text"]).lower().split())
        assert not any(ph in text for ph in RECORD_ONLY), c
        assert claims.bound(c), c
        assert c["requires"] in claims.BOUND_CLASSES and c["requires"] != "any", c
        sealed = {s["pointer"]: s for s in c.get("sealed") or []}
        for ptr in c.get("pointers") or []:
            assert ptr in sealed and sealed[ptr]["sha256"], (c["id"], ptr)
            if ptr.startswith("event:"):
                assert sealed[ptr]["copy"] == "packet/events/%s.json" % ptr.split(":")[1], (c["id"], ptr)
                assert c["requires"] == "rehash" and not c["commands"], c
    s1 = [c for c in got["claims"] if c["id"] == "s1"]
    assert s1
    if which == "report":
        assert s1[0]["requires"] == "rerun" and s1[0]["commands"], s1[0]
    else:
        assert s1[0]["requires"] == "rehash" and s1[0]["pointers"], s1[0]


def test_the_metric_line_names_its_gate_and_the_record_claims_name_their_rows(project, contract):
    conn, task = attempt(project, "accepted")
    try:
        got = {c["id"]: c for c in extract(conn, project, task)["claims"]}
        acc = conn.execute("SELECT id FROM events WHERE kind=? AND ref=?", (rev.ACCEPT, task)).fetchone()["id"]
        tampers = [r["id"] for r in conn.execute("SELECT id FROM events WHERE kind=? AND ref=? ORDER BY id",
                                                 (rev.TAMPER, task))]
    finally:
        conn.close()
    metric = [c for c in got.values() if c["source"] == "result" and c["text"].startswith("metric total_bytes")]
    assert len(metric) == 1 and [e["gate"] for e in metric[0]["commands"]] == ["metric:total_bytes"]
    assert metric[0]["requires"] == "rerun"
    for stage in ("build", "unit"):
        line = [c for c in got.values() if c["source"] == "result" and c["text"].startswith("gate item %s " % stage)]
        assert line and [e["gate"] for e in line[0]["commands"]] == [stage]
    # the accept line's reason names gate items, but the line states the accept row: it is bound to
    # the row alone
    accept_line = [c for c in got.values() if c["source"] == "result" and "accept rule" in c["text"]]
    assert accept_line[0]["pointers"] == ["event:%d" % acc] and accept_line[0]["commands"] == []
    assert got["x3"]["pointers"] == ["event:%d" % acc] and got["x3"]["requires"] == "rehash"
    assert "event:%d" % acc in got["x3"]["text"]
    assert got["x5"]["pointers"] == ["event:%d" % t for t in tampers] and got["x5"]["requires"] == "rehash"


def test_a_pointer_is_named_only_at_a_token_end():
    assert claims._named("rows event:123 and event:7.", ["event:12", "event:123", "event:7"]) == [
        "event:123", "event:7"]
    assert claims._named("see logs/a.log.1", ["local:logs/a.log"]) == []
    assert claims._named("see logs/a.log here", ["local:logs/a.log"]) == ["local:logs/a.log"]


def test_bound_reads_the_binding_of_each_class():
    assert claims.bound({"requires": "rerun", "commands": [{"gate": "build", "cwd": "tree"}]})
    assert not claims.bound({"requires": "rerun", "commands": []})
    assert claims.bound({"requires": "rehash", "pointers": ["event:1"]})
    assert not claims.bound({"requires": "rehash", "pointers": []})
    assert claims.bound({"requires": "apply", "sealed": [{"copy": "packet/diff.patch"}]})
    assert not claims.bound({"requires": "apply", "sealed": []})
    for req in ("any", "unbound", None, "rerun-ish"):
        assert not claims.bound({"requires": req, "commands": [{"gate": "x"}], "pointers": ["event:1"]})


def test_y1_follows_the_reproducer_row(project):
    conn, task = repair(project, "reproducer-unproved")
    try:
        got = {c["id"]: c for c in claims.engine_claims(conn, task)}
    finally:
        conn.close()
    assert got["y1"]["text"].startswith("the reproducer was not proved (event:")


@pytest.mark.parametrize("shape", ["reproducer-unproved", "prepare-failed", "fixed", "no-result"])
def test_the_repair_result_and_deviations_state_only_their_rows(project, contract, shape):
    conn, task = repair(project, shape)
    try:
        text = proofs.render_attempt(conn, project, task)
        got = {c["id"]: c for c in extract(conn, project, task)["claims"]}
    finally:
        conn.close()
    sections = proof.split_sections(text)
    result = " ".join(sections["Result"].split())
    deviations = " ".join(sections["Deviations and issues"].split())
    # a task with no repair result row says so and names no row; it never states a result
    assert ("- no repair result is recorded" in result) == (shape == "no-result"), result
    assert "(event none)" not in result and "repair result is none" not in result, result
    # "proved once" only when the reproducer row says proved and supersedes nothing
    assert ("the reproducer was proved once" in deviations) == (shape == "fixed"), deviations
    assert "No test was left out of the base set" in deviations, deviations
    if shape == "no-result":
        line = [c for c in got.values() if c["source"] == "result" and "no repair result" in c["text"]]
        assert line and line[0]["requires"] == claims.UNBOUND and not claims.bound(line[0]), line


def test_a_claim_that_names_no_command_and_no_pointer_gets_the_unbound_class():
    assert claims._requires([], []) == claims.UNBOUND
    assert claims._requires([], None) == claims.UNBOUND
    assert claims._requires([{"gate": "build", "cwd": "tree"}], ["event:1"]) == "rerun"
    assert claims._requires([], ["event:1"]) == "rehash"
    assert claims.UNBOUND not in claims.BOUND_CLASSES and claims.UNBOUND != "any"


# ------------------------------------------------------------------ 2. "recorded once" is a code check
def test_a_record_with_two_attempt_rows_is_refused_at_the_seal(project, contract):
    conn, task = attempt(project, "accepted")
    try:
        last = conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id DESC",
                            (rev.ATTEMPT, task)).fetchone()
        rev.emit(conn, rev.ATTEMPT, run=RUN, ref=task, op=OP, session=SESSION, code=vc.PASS,
                 data={k: v for k, v in json.loads(last["data"]).items() if k not in ("run", "verdict", "exit")})
        path = proof.report_path(project, proof.subject(conn, task))
        before = conn.execute("SELECT COUNT(*) FROM events WHERE kind='proof-report'").fetchone()[0]
        with pytest.raises(proof.Refusal) as err:
            proofs.seal(conn, project, task, session=SESSION)
        assert "a report is rendered from exactly one" in str(err.value)
        after = conn.execute("SELECT COUNT(*) FROM events WHERE kind='proof-report'").fetchone()[0]
    finally:
        conn.close()
    assert not os.path.exists(path) and before == after


def test_a_record_with_one_attempt_row_renders_and_the_appendix_says_so(project, contract):
    conn, task = attempt(project, "accepted")
    try:
        text = proofs.render_attempt(conn, project, task)
        assert proofs.render_attempt(conn, project, task) == text      # deterministic
    finally:
        conn.close()
    assert "must hold exactly one attempt row" in text


def test_the_render_check_taken_out_reports_a_doubled_record(project, contract, monkeypatch):
    # mutation: with the count check gone the doubled record renders as if it held one row
    conn, task = attempt(project, "accepted")
    try:
        last = conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id DESC",
                            (rev.ATTEMPT, task)).fetchone()
        rev.emit(conn, rev.ATTEMPT, run=RUN, ref=task, op=OP, session=SESSION, code=vc.PASS,
                 data={k: v for k, v in json.loads(last["data"]).items() if k not in ("run", "verdict", "exit")})
        real = proofs._rows
        monkeypatch.setattr(proofs, "_rows", lambda c, kind, ref: (
            real(c, kind, ref)[-1:] if kind == rev.ATTEMPT else real(c, kind, ref)))
        assert proofs.render_attempt(conn, project, task)
    finally:
        conn.close()


# ------------------------------------------------------------------ 3. the judge (C5) and the open stop
@pytest.fixture(scope="module")
def kit_try(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("rr004binding"))
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), root)
    return kit.Try("a660f66772906b0c2", root)


def _with(tr, cid, **spec):
    brief = json.loads(json.dumps(tr.brief))
    for c in brief["claims"]:
        if c["id"] == cid:
            c.update(spec)
    return brief


def _verdict(tr, cid, receipts):
    raw = json.loads(json.dumps(tr.verdict()))
    for c in raw["claims"]:
        if c["id"] == cid:
            c.update(verdict="supported", conditions=[],
                     evidence=[{"receipt": r, "observed": "the receipt %s was read" % r} for r in receipts])
    return raw


def _problems(err, cid):
    return [(p["check"], p["detail"]) for p in err.value.problems if isinstance(p, dict) and p.get("claim") == cid]


@pytest.mark.parametrize("cid", ["s1", "r5", "r6", "r7", "r8", "x3", "x5"])
def test_c5_refuses_an_old_any_claim_on_the_apply_receipt_alone_and_keeps_the_kit(kit_try, cid):
    # the kit's claims.json was written before t-296: these claims are `any`
    assert [c["requires"] for c in kit_try.brief["claims"] if c["id"] == cid] == ["any"]
    assert kit_try.check()["slot_verdict"] == "PASS"            # the kit's any claims cite event receipts
    with pytest.raises(verdicts.VerifyError) as err:
        kit_try.check(raw=_verdict(kit_try, cid, ["apply"]))
    assert ("C5", "a supported claim cites only the apply receipt, which is the diff claim's own") in \
        _problems(err, cid)
    # an `any` claim citing an event receipt of the try still passes (a claims.json made before t-296)
    assert kit_try.check(raw=_verdict(kit_try, "s1", ["ev-83131"]))["slot_verdict"] == "PASS"


def test_c5_refuses_a_class_it_does_not_know(kit_try):
    brief = _with(kit_try, "s1", requires=claims.UNBOUND)
    with pytest.raises(verdicts.VerifyError) as err:
        kit_try.check(brief=brief, commands=brief["claims"], raw=_verdict(kit_try, "s1", ["ev-83131"]))
    assert ("C5", "a supported claim of an unbound class cannot be supported") in _problems(err, "s1")


#: the glance of an accepted attempt before t-296 (alpaca/research/proofs.py on main): the shape
#: whose s1 the two verifiers of rr-014 vf-t-294 swapped on every round
OLD_GLANCE = ("Research run %s, attempt %s (key %s): verdict accepted, outcome improvement. The "
              "attempt was recorded once, after its fingerprint points, its gate chain and its accept "
              "rule ran, and this report was rendered from the record.")


def _fold(project, vf, slots):
    """Record one round 0 of verification `vf` over claim s1 (one PROOF_VERDICT per slot, as the
    driver writes them) and return verdicts.fold_round of it."""
    conn = db.connect(project)
    try:
        def record(kind, data):
            db.append_event(conn, session="driver", actor="research-verifier", kind=kind, op=OP,
                            ref="t-901", data=dict(data, verification=vf))

        record(verdicts.VERIFY_OPEN, {"claim_ids": ["s1"]})
        for slot, (eff, reason) in slots:
            record(verdicts.PROOF_VERDICT, {"round": 0, "verifier": slot, "claims": [
                {"id": "s1", "effective": eff, "reason": reason}]})
        return verdicts.fold_round(conn, vf, 0)
    finally:
        conn.close()


def test_done_bar_0_replay_the_old_s1_stays_disputed_and_the_new_s1_settles_in_round_0(project, contract,
                                                                                       kit_try):
    """The shape of rr-014 vf-t-294 round 0 (events 104870 and 104872; no transcript text): v1
    supported s1 with no condition, v2 supported it with one condition about the record it could
    not run. Both s1 claims here come from the extractor (`claims.extract`) over a report of the
    same synthetic accepted attempt: (a) with the old glance put back, (b) as proofs.py renders it
    now. The judge (check_verdict over the rr-004 kit try) and the fold read the extracted class
    and pointer kind. The slot verdicts are written by this test (no live agent runs here); what
    the replay shows is which of them the judge can accept and how the round folds."""
    conn, task = attempt(project, "accepted")
    try:
        aid = conn.execute("SELECT id FROM events WHERE kind=? AND ref=?", (rev.ATTEMPT, task)).fetchone()["id"]
        text = proofs.render_attempt(conn, project, task)
        new = {c["id"]: c for c in extract(conn, project, task)["claims"]}["s1"]
        glance = proof.split_sections(text)[proof.SUMMARY].strip()
        assert glance and glance in text
        old_text = text.replace(glance, OLD_GLANCE % (RUN, task, "rr-901:r1:s0"), 1)
        old = {c["id"]: c for c in extract(conn, project, task, text=old_text)["claims"]}["s1"]
    finally:
        conn.close()

    # (a) the old s1: it states what only the record holds and names no row, so no receipt of a
    # verifier's own try can check it. The extractor leaves it unbound (the verification stops at
    # open); in rr-014 it was `any`, and the two slots' stances fold to a dispute every round.
    assert any(ph in " ".join(old["text"].lower().split()) for ph in RECORD_ONLY), old["text"]
    assert old["pointers"] == [] and not claims.bound(old), old
    cond = [{"id": "k1", "check": "the record holds exactly one attempt row", "ran": False}]
    stance = {"id": "s1", "verdict": "supported", "reason": None}
    e1 = verdicts.effective(dict(stance, conditions=[]), requires="any", apply_failed=False)
    e2 = verdicts.effective(dict(stance, conditions=cond), requires="any", apply_failed=False)
    assert e2 == ("cannot-verify", "conditional")
    assert _fold(project, "vf-t-901-1", (("v1", e1), ("v2", e2)))["disputed"] == ["s1"]

    # (b) the new s1, as the extractor writes it: bound to the attempt row alone, nothing about
    # the record, the gate chain or the accept rule
    flat = " ".join(new["text"].lower().split())
    for clause in RECORD_ONLY + ("gate chain", "accept rule"):
        assert clause not in flat, (clause, new["text"])
    assert new["requires"] == "rehash" and claims.bound(new) and new["commands"] == [], new
    assert new["pointers"] == ["event:%d" % aid], new
    assert [s["copy"] for s in new["sealed"]] == ["packet/events/%d.json" % aid], new
    # the kit's receipts check its own attempt row (event 83131): lay the extracted claim's class
    # and pointer kind on that row, sealed as the kit's packet seals it
    kind = new["pointers"][0].split(":", 1)[0]
    kit_ptr = "%s:83131" % kind
    kit_seal = [s for c in kit_try.brief["claims"] for s in c.get("sealed") or [] if s["pointer"] == kit_ptr]
    assert kit_seal, kit_ptr
    brief = _with(kit_try, "s1", text=new["text"].replace(new["pointers"][0], kit_ptr),
                  requires=new["requires"], pointers=[kit_ptr], sealed=kit_seal[:1], commands=new["commands"])
    got = kit_try.check(brief=brief, commands=brief["claims"], raw=_verdict(kit_try, "s1", ["ev-83131"]))
    s1 = [c for c in got["claims"] if c["id"] == "s1"][0]
    assert s1["effective"] == "supported" and got["slot_verdict"] == "PASS"
    fold = _fold(project, "vf-t-901-2", (("v1", (s1["effective"], s1["reason"])),
                                         ("v2", (s1["effective"], s1["reason"]))))
    assert fold["settled"] == {"s1": "supported"} and fold["disputed"] == []

    # (c) the new s1 supported on the apply receipt alone is refused by C5 and by C12
    with pytest.raises(verdicts.VerifyError) as err:
        kit_try.check(brief=brief, commands=brief["claims"], raw=_verdict(kit_try, "s1", ["apply"]))
    checks = _problems(err, "s1")
    assert ("C5", "a supported claim that needs a re-hash cites no re-hash receipt") in checks
    assert ("C12", "no matching re-hash of %s" % kit_ptr) in checks


def test_the_judge_is_not_loosened():
    entry = {"id": "s1", "verdict": "supported", "reason": None,
             "conditions": [{"id": "k1", "check": "the record holds one row", "ran": False}]}
    assert verdicts.effective(entry, requires="rehash", apply_failed=False) == ("cannot-verify", "conditional")
    assert "conditional" in verdicts.RETRYABLE
    assert verdicts.slot_verdict({"s1": ("cannot-verify", "out-of-reach")}) == "BLOCKED"


# ------------------------------------------------------------------ 4. the request forms (F42)
@pytest.mark.parametrize("path", ["packet/events/777.json", "packet/evidence/01-a.log"])
def test_the_client_refuses_hash_on_an_event_pointer_with_the_event_rewrite(path):
    with pytest.raises(ValueError) as err:
        rerun.parse_request(["h-x4", "--hash", path, "--pointer", "event:777"])
    assert "--event" in str(err.value) and "never --hash" in str(err.value)


@pytest.mark.parametrize("ptr", ["foo:x", "p", "remote:x", "Event:7"])
def test_the_client_refuses_a_pointer_kind_it_does_not_list(ptr):
    with pytest.raises(ValueError) as err:
        rerun.parse_request(["h-1", "--hash", "packet/evidence/01-a.log", "--pointer", ptr])
    assert "local:, receipt: or job:" in str(err.value)


def test_the_forms_that_can_match_stay_accepted():
    assert rerun.parse_request(["v-x4", "--event", "packet/events/777.json"])["kind"] == "event"
    for ptr in ("local:logs/gate.log", "receipt:r1", "job:j1"):
        got = rerun.parse_request(["h-e1", "--hash", "packet/evidence/01-a.log", "--pointer", ptr])
        assert got["kind"] == "hash" and got["pointer"] == ptr
    # request_id gives no id for a refused request, so C10 credits no receipt for it
    assert rerun.request_id(["h-1", "--hash", "packet/events/7.json", "--pointer", "event:7"]) is None


def _serve(root, folder, brief, argvs):
    os.makedirs(os.path.join(folder, "requests"), exist_ok=True)
    os.makedirs(os.path.join(folder, "receipts"), exist_ok=True)
    stop, led = threading.Event(), {}
    th = threading.Thread(target=verifier.serve, args=(None, root, "vf-1", "v1", 0, 0), kwargs={
        "trees": {"folder": folder, "tree": os.path.join(folder, "tree"), "base": os.path.join(folder, "base")},
        "stop": stop, "ledger": led, "brief": brief, "task": "t-1"}, daemon=True)
    th.start()
    for n, argv in enumerate(argvs):
        part = os.path.join(folder, "requests", "%d.part" % n)
        with open(part, "w", encoding="utf-8") as fh:
            json.dump({"schema": rerun.REQUEST_SCHEMA, "argv": argv}, fh)
        os.replace(part, os.path.join(folder, "requests", "%d.json" % n))
        for _ in range(400):
            if os.path.isfile(os.path.join(folder, "receipts", "%d.answer" % n)):
                break
            threading.Event().wait(0.05)
    stop.set()
    th.join(30)
    return led


def test_the_driver_refuses_hash_requests_that_cannot_match_and_serves_event(project):
    conn = db.connect(project)
    try:
        ev = rev.emit(conn, rev.TAMPER, run=RUN, ref="t-1", op=OP, session=SESSION,
                      data={"point": "open", "verdict": "PASS", "path": None})
        row = verifier._export_event(conn, ev["id"])
    finally:
        conn.close()
    folder = os.path.join(project, ".alpaca", "verify", "t-1", "vf-1", "v1", "r0", "t0")
    os.makedirs(os.path.join(folder, "packet", "events"), exist_ok=True)
    os.makedirs(os.path.join(folder, "packet", "evidence"), exist_ok=True)
    with open(os.path.join(folder, "packet", "events", "%d.json" % ev["id"]), "w", encoding="utf-8") as fh:
        json.dump(row, fh)
    with open(os.path.join(folder, "packet", "evidence", "01-a.log"), "w", encoding="utf-8") as fh:
        fh.write("x\n")
    sha = rerun.sha256_file(os.path.join(folder, "packet", "evidence", "01-a.log"))
    ptr = "event:%d" % ev["id"]
    brief = {"claims": [{"id": "x5", "pointers": [ptr], "sealed": [
        {"pointer": ptr, "sha256": row["hash"], "copy": "packet/events/%d.json" % ev["id"]}]},
        {"id": "e1", "pointers": ["local:a.log"], "sealed": [
            {"pointer": "local:a.log", "sha256": sha, "copy": "packet/evidence/01-a.log"}]}]}
    led = _serve(project, folder, brief, [
        ["h-1", "--hash", "packet/events/%d.json" % ev["id"], "--pointer", ptr],
        ["h-2", "--hash", "packet/evidence/01-a.log", "--pointer", "local:not-sealed.log"],
        ["v-1", "--event", "packet/events/%d.json" % ev["id"]],
        ["h-3", "--hash", "packet/evidence/01-a.log", "--pointer", "local:a.log"]])
    assert led["requests"][0]["kind"] == "refused" and "--event" in led["requests"][0]["line"]
    assert led["requests"][1]["kind"] == "refused" and "no sealed value" in led["requests"][1]["line"]
    for rid in ("h-1", "h-2"):
        assert not os.path.exists(os.path.join(folder, "receipts", "%s.json" % rid))
        assert rid not in led["receipts"]
    with open(os.path.join(folder, "receipts", "v-1.json"), encoding="utf-8") as fh:
        assert json.load(fh)["match"] is True
    with open(os.path.join(folder, "receipts", "h-3.json"), encoding="utf-8") as fh:
        assert json.load(fh)["match"] is True
