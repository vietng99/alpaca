"""The proof reports of attempt, repair and report tasks, rendered from the record, sealed and
closed; the report task's event export and its record-free replay. Lease calls use the board's
module as `from alpaca import claims as leases`, never alpaca/research/claims.py.

A report is rendered only from recorded values (the task's research object, its verdict events,
the frozen contract), so rendering it again after a crash gives the same bytes and the `seal`
step can tell a finished seal from one it must make again. The report is a format 2 proof
(alpaca/proof.py SECTIONS) whose How to reproduce section holds one gate block per chain item
and side, which the verifiers' claims extraction binds to a `gate` re-run (verifier.md sections
3.2 and 4.3). An attempt task closes through `task move ... done`, so proof.done_gate and its
verify_gate.check always run.

Design: design/research-engine/architecture.md sections 3.8 and 8.2."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading

from alpaca import claims as leases
from alpaca import db, proof, util
from alpaca.gates import verdict as vc
from alpaca.research import events, paths

ACTOR = "research-runner"
_CLOSE_LOCK = threading.Lock()          # cli.main is not written for concurrent calls

_REPRODUCE_GATES = ("Each block below names one item of the frozen gate chain and the tree side "
                    "it runs on; its other lines are the item's command as the frozen contract "
                    "holds it, unexpanded, for the reader. A verifier re-runs an item with "
                    "`bin/rerun --gate <item> --side <side>`, which runs the frozen command in "
                    "an offline gate jail.")
_REPRODUCE_REPLAY = ("The block rebuilds report.md and report.json from the sealed export of "
                     "the run's events with no record, prints the sha256 of each and exits 1 when "
                     "either differs from the sha256 the export names.")


# ------------------------------------------------------------------ record reads
def _rows(conn, kind: str, ref: str) -> list:
    out = []
    for row in conn.execute("SELECT id, ts, ref, data FROM events WHERE kind=? AND ref=? "
                            "ORDER BY id", (kind, ref)).fetchall():
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        out.append({"id": row["id"], "ts": row["ts"], "ref": row["ref"], "data": data})
    return out


def _research(conn, task: str) -> dict:
    adds = _rows(conn, "task-add", task)
    got = adds[0]["data"].get("research") if adds else None
    return got if isinstance(got, dict) else {}


def _rel(root: str, path) -> str | None:
    if not path:
        return None
    full = path if os.path.isabs(path) else os.path.join(root, path)
    rel = os.path.relpath(os.path.realpath(full), os.path.realpath(root))
    if rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/")


def _local(root: str, path, note: str) -> str | None:
    """An Evidence line for a non-empty regular file inside the root, else None."""
    rel = _rel(root, path)
    if rel is None:
        return None
    full = os.path.join(root, rel)
    if os.path.islink(full) or not os.path.isfile(full) or os.path.getsize(full) == 0:
        return None
    return "- local:%s - %s" % (rel, note)


def _contract(root: str, run: str | None):
    if not run:
        return None
    from alpaca.research import contract as contract_mod
    try:
        return contract_mod.read_frozen(root, run)
    except Exception:  # noqa: BLE001 - a report is still rendered; its blocks then say so
        return None


def _item_command(contract, item: str) -> str:
    """The frozen command of one chain item, as the contract holds it."""
    if item.startswith("metric:"):
        for m in contract.metrics:
            if m.id == item[len("metric:"):]:
                return m.command
    elif item.startswith("fail-to-pass:"):
        for case in contract.repair:
            if case.case_id == item[len("fail-to-pass:"):]:
                return case.fail_to_pass.command
    elif item == "pass-to-pass":
        if contract.pass_to_pass is not None:
            return contract.pass_to_pass.command
    else:
        for stage in contract.stages:
            if isinstance(stage, dict) and stage.get("id") == item:
                return str(stage.get("run") or "")
    return ""


def _gate_blocks(contract, *, repair_case: str | None, both: bool) -> list:
    """One fenced block per chain item and side: side tree for every item, and side base too for
    the fail-to-pass and pass-to-pass items of a fix attempt or a repair task."""
    from alpaca.research import gates
    out = []
    for item in gates.chain(contract, repair_case=repair_case):
        sides = ["tree"]
        if both and (item.startswith("fail-to-pass:") or item == "pass-to-pass"):
            sides.append("base")
        command = _item_command(contract, item).strip() or "(no command in the frozen contract)"
        for side in sides:
            out.append("```\n# alpaca-gate %s side %s\n%s\n```" % (item, side, command))
    return out


def _fmt(value) -> str:
    if isinstance(value, float):
        return ("%.6f" % value).rstrip("0").rstrip(".")
    return str(value)


# ------------------------------------------------------------------ render
def _attempt_sections(conn, root: str, task: str, research: dict) -> dict:
    run = research.get("run")
    attempt = _rows(conn, events.ATTEMPT, task)
    d = attempt[-1]["data"] if attempt else {}
    from alpaca.research import state
    verdict = state.attempt_verdict(conn, task)
    gates_ = [e for e in _rows(conn, events.GATE, task) if e["data"].get("side") in (None, "attempt")]
    tampers = _rows(conn, events.TAMPER, task)
    diffs = _rows(conn, events.DIFF, task)
    accepts = _rows(conn, events.ACCEPT, task)
    refused = _rows(conn, events.DIFF_REFUSED, task)
    audits = _rows(conn, events.AUDIT_VERDICT, task)
    proposals = _rows(conn, events.PROPOSAL, task)
    launches = _rows(conn, events.LAUNCH, task)
    notes = conn.execute("SELECT body FROM messages WHERE kind='note' AND ref=? ORDER BY id",
                         (task,)).fetchall()
    baseline = bool(research.get("baseline"))
    key = research.get("key") or task
    outcome = d.get("outcome") or verdict.get("outcome") or "none"
    word = verdict.get("verdict") or "none"
    ran = [(e["data"].get("stage"), e["data"].get("verdict")) for e in gates_]
    seen, ran_once = set(), []
    for stage, v in ran:
        if stage not in seen:
            seen.add(stage)
            ran_once.append((stage, v))
    first_fail = next((s for s, v in ran_once if v not in ("PASS", None)), None)
    metrics = d.get("metrics") if isinstance(d.get("metrics"), dict) else {}
    diff = d.get("diff") if isinstance(d.get("diff"), dict) else {}
    kind = "the round 0 baseline" if baseline else "attempt"
    klass = ", class %s" % verdict.get("broken_class") if verdict.get("broken_class") else ""
    rec = attempt[-1]["id"] if attempt else None
    tag = " (event:%d)" % rec if rec is not None else ""
    # the glance states only the facts of the attempt row and names it, so claim s1 is bound to
    # that row and a verifier re-hashes its export (F43, F50; design in-session-runtime.md 25.3):
    # no clause about steps the row does not hold, and none about the record itself
    if rec is not None:
        glance = ("Research run %s, %s %s (key %s): its attempt event (event:%d) records outcome "
                  "%s%s, which gives verdict %s." % (run, kind, task, key, rec, outcome, klass,
                                                     word))
    else:
        glance = "Research run %s, %s %s (key %s): verdict %s, outcome %s%s." % (
            run, kind, task, key, word, outcome, klass)
    # "What I did" lists a step only when the record holds its row (t-296)
    closed = any(t["data"].get("point") == "close" for t in tampers)
    steps = ["claim", "fingerprint window"]
    if proposals and not baseline:
        steps += ["proposal", "proposal check"]
    if diffs or gates_:
        steps += ["worktree"] + ([] if baseline else ["implementation", "allowlist check"]) + \
            ["capture of one diff", "purge"]
    if gates_:
        steps.append("gate chain")
    if accepts:
        steps.append("accept rule")
    if audits:
        steps.append("audit")
    if notes:
        steps.append("notes")
    if closed:
        steps.append("the closing fingerprint point")
    if attempt:
        steps.append("the attempt record")
    what = ["The loop runner drove %s through these steps of the attempt plan of the research "
            "engine, each of which left its row in the record: %s." % (
                task, ", ".join(steps[:-1]) + " and " + steps[-1])]
    if proposals:
        p = proposals[-1]["data"]
        what.append("The proposal was %s (%s)." % (
            "valid" if p.get("valid") else "refused before any worktree existed",
            ", ".join("%s %s" % (f.get("code"), f.get("where")) for f in p.get("problems") or [])
            or "no finding"))
    how = ["Each agent was a subagent of the driving session, named in the launch rows by its "
           "agent id and the hash of its kept transcript (%d launch(es) recorded); %s; the "
           "verdict events hold the inputs of each decision." % (
               len(launches), "every gate item ran from the frozen contract copy in an offline "
               "gate jail" if gates_ else "no gate item ran"),
           "Fingerprint points recorded: %s." % (", ".join(
               "%s %s" % (t["data"].get("point"), t["data"].get("verdict")) for t in tampers)
               or "none")]
    where = ["Run folder .alpaca/research/%s/attempts/%s/ (diff, fingerprints, gate logs); "
             "worktree .alpaca/worktrees/research/%s/%s/ while it lived; the run's clone "
             "branch research/%s." % (run, task, run, task, run)]
    # each Result line names what checks it (t-296, design 25.3): a gate line its gate item, a
    # metric line its `metric:<id>` item, every other line the row it states as `event:N`; no
    # line names both a gate item and a row
    result = []
    for stage, v in ran_once:
        result.append("- gate item %s ended %s on the attempt tree" % (stage, v))
    for mid in sorted(metrics):
        m = metrics[mid] if isinstance(metrics[mid], dict) else {}
        result.append("- metric %s (gate item metric:%s): median value %s from repeats %s (%s "
                      "successful)" % (mid, mid, _fmt(m.get("median")), json.dumps(m.get("repeats")),
                                       m.get("n_ok")))
    if accepts:
        a = accepts[-1]["data"]
        result.append("- the accept rule (phase %s) gave %s: %s (event:%d)" % (
            a.get("phase"), a.get("outcome"), a.get("reason"), accepts[-1]["id"]))
    if refused:
        r = refused[-1]["data"]
        result.append("- the FR-018 diff check refused the attempt at step %s: %s diff event(s) "
                      "found (event:%d)" % (r.get("step"), r.get("found"), refused[-1]["id"]))
    if diff:
        result.append("- the recorded diff is %s (%s bytes%s)%s" % (
            diff.get("path"), diff.get("bytes"), ", empty" if diff.get("empty") else "", tag))
    if rec is not None:
        # the row holds the outcome (its `verdict` field is the band word), so the line states
        # the outcome it records and the verdict the engine reads from it, as the glance does
        result.append("- the attempt event records outcome %s, which gives verdict %s%s" % (
            outcome, word, tag))
    else:
        result.append("- the attempt verdict is %s with outcome %s" % (word, outcome))
    if not ran_once:
        result.append("- no gate item ran on the attempt tree: an earlier step set the verdict%s"
                      % tag)
    deviations = []
    if first_fail:
        deviations.append("The first failing gate item is %s; the chain stopped there." % first_fail)
    if verdict.get("reason"):
        deviations.append("Verdict reason: %s." % verdict["reason"])
    if audits:
        deviations.append("The audit recorded verdict %s." % audits[-1]["data"].get("verdict"))
    else:
        deviations.append("No audit verdict is recorded for this attempt.")
    if notes:
        deviations.append("Notes line: %s" % " ".join(str(notes[-1]["body"]).split()))
    return {"glance": glance, "what": what, "how": how, "where": where, "result": result,
            "deviations": deviations, "diffs": diffs, "gates": gates_, "tampers": tampers,
            "verdict_events": [e["id"] for e in attempt + accepts + refused + audits + proposals],
            "launches": launches}


def _evidence_lines(conn, root: str, task: str, parts: dict) -> list:
    lines = []
    for line in parts.get("extra") or []:
        if line not in lines:
            lines.append(line)
    for ev in parts["diffs"]:
        line = _local(root, ev["data"].get("path"), "the attempt diff")
        if line and line not in lines:
            lines.append(line)
    for ev in parts["gates"]:
        line = _local(root, ev["data"].get("log"), "gate log of %s" % ev["data"].get("stage"))
        if line and line not in lines:
            lines.append(line)
    for ev in parts["tampers"]:
        line = _local(root, ev["data"].get("path"), "fingerprint point %s" % ev["data"].get("point"))
        if line and line not in lines:
            lines.append(line)
    for ev in parts["launches"]:
        line = _local(root, ev["data"].get("prompt"), "the %s prompt" % ev["data"].get("role"))
        if line and line not in lines:
            lines.append(line)
    for eid in parts["verdict_events"]:
        line = "- event:%d - a verdict event of %s" % (eid, task)
        if line not in lines:
            lines.append(line)
    return lines


def render_attempt(conn, root: str, task: str) -> str:
    """The proof report of an attempt, repair or report task, deterministic from the record, in
    the format 2 shape the seal checks (alpaca/proof.py:74-95, :894-905), so t-094's seal never
    refuses it (verifier.md section 8.1, item 5): At a glance, Report context (with
    proof.FORMAT_LINE), the six narrative sections, Evidence and Appendix A; under How to
    reproduce, one gate block per chain item and side (an attempt or a repair task) or the replay
    block (the report task), and nothing else fenced; the Evidence list names the diff, the gate
    logs, the fingerprints and the prompts as local: paths and the verdict events as event:
    pointers (architecture.md section 3.8)."""
    research = _research(conn, task)
    role = research.get("role") or "attempt"
    run = research.get("run")
    if role == "attempt":
        # the report states the facts of the one attempt row (s1 binds it); a record that holds
        # more than one is refused here, the render the seal makes, rather than reported as if
        # it held one (F43; design in-session-runtime.md 25.3)
        rows = [e["id"] for e in _rows(conn, events.ATTEMPT, task)]
        if len(rows) > 1:
            raise proof.Refusal(["research-attempt rows for %s: %s; a report is rendered from "
                                 "exactly one" % (task, ", ".join(str(r) for r in rows))])
    subj = proof.subject(conn, task)
    contract = _contract(root, run)
    if role == "report":
        parts = _report_sections(conn, root, task, research)
        blocks = ["```\npython3 -m alpaca.research.proofs replay-report --run %s --events "
                  ".alpaca/reports/research/%s/events.json --out replay\n```" % (run, run)]
        lead = _REPRODUCE_REPLAY
    else:
        parts = _attempt_sections(conn, root, task, research)
        if role == "repair":
            _repair_parts(conn, root, task, research, parts)
        case = research.get("case") if role == "repair" else None
        if role == "attempt" and research.get("repair_task"):
            case = _research(conn, research["repair_task"]).get("case")
        blocks = (_gate_blocks(contract, repair_case=case, both=case is not None)
                  if contract is not None else [])
        lead = _REPRODUCE_GATES
        if not blocks:
            lead += " The frozen contract could not be read when this report was rendered."
    evidence = parts.get("evidence") or _evidence_lines(conn, root, task, parts)
    if not evidence:
        evidence = ["- event:%d - the task-add event of %s" % (_rows(conn, "task-add", task)[0]["id"],
                                                               task)]
    body = ["# Proof report %s" % task, "",
            "## %s" % proof.SUMMARY, "", parts["glance"], "",
            "## %s" % proof.CONTEXT, "", proof.FORMAT_LINE,
            "- task: %s (%s of research run %s, key %s)" % (task, role, run, research.get("key")),
            "- op: %s" % subj.get("op"), "",
            "## What I did", ""] + parts["what"] + ["",
            "## How I did it", ""] + parts["how"] + ["",
            "## Where", ""] + parts["where"] + ["",
            "## Result", ""] + parts["result"] + ["",
            "## Deviations and issues", ""] + parts["deviations"] + ["",
            "## How to reproduce", "", lead, ""]
    for block in blocks:
        body += [block, ""]
    body += ["## %s" % proof.EVIDENCE, ""] + evidence + ["",
             "## %s" % proof.APPENDIX, "",
             "Rendered by alpaca.research.proofs from the record, which must hold exactly one "
             "attempt row for an attempt task; the loop runner writes no tool extract here.", ""]
    return "\n".join(body)


def _repair_parts(conn, root: str, task: str, research: dict, parts: dict) -> None:
    """The repair task's own sections (t-101): its prepare, reproducer, base measurement and
    result events stand where an attempt's attempt event stands, and its gate lines are the
    fix attempt's (the fix the result names), whose gates the verifiers re-run over trees at the
    case base (architecture.md sections 8.2 and 11.2). The Evidence list adds the prepare diff,
    the reproducer's frozen files, the fix's diff and those events."""
    run = research.get("run")
    case = research.get("case")
    prep = (_rows(conn, events.PREPARE, task) or [None])[-1]
    rep = (_rows(conn, events.REPRODUCER, task) or [None])[-1]
    base = (_rows(conn, events.BASELINE, task) or [None])[-1]
    res = (_rows(conn, events.REPAIR_RESULT, task) or [None])[-1]
    rd = res["data"] if res else {}
    pd = rep["data"] if rep else {}
    bd = base["data"] if base else {}
    fix = rd.get("fix_attempt")
    outcome = rd.get("outcome") or "none"
    p2p = bd.get("pass_to_pass") if isinstance(bd.get("pass_to_pass"), dict) else {}
    base_pass = list(p2p.get("base_pass") or [])
    branch = paths.research_branch(run, task) if run else "research/<run>/%s" % task
    # the glance states the rows only, each named as `event:N`, so claim s1 is bound to them
    # (t-296, design in-session-runtime.md 25.3): no clause about a step the rows do not hold
    glance = ["Research run %s, repair task %s (case %s): outcome %s%s." % (
        run, task, case, outcome, " (event:%d)" % res["id"] if res else "")]
    if rep is not None:
        glance.append("The reproducer is %s (event:%d)." % (
            "proved" if pd.get("proved") else "not proved", rep["id"]))
    if base is not None:
        glance.append("The base pass-to-pass set holds %d test(s) (event:%d)." % (
            len(base_pass), base["id"]))
    if fix:
        glance.append("The result names fix attempt %s." % fix)
    parts["glance"] = " ".join(glance)
    parts["what"] = [
        "The loop runner drove %s through the repair plan: claim, the repair repository at the "
        "case base, the prepare stage, the reproducer proof, the base pass-to-pass measurement, "
        "the fix rounds and the result the verifiers read." % task]
    parts["how"] = [
        "The reproducer (origin %s, tests %s) ran once on a fresh tree at the case base in an "
        "offline gate jail and every named test failed there; the base set comes from the "
        "pass-to-pass suite run on the same base. Each fix attempt ran the attempt plan and was "
        "judged by the repair rule against that set." % (
            pd.get("origin"), ", ".join(pd.get("tests") or []) or "none")]
    parts["where"] = [
        "Run folder .alpaca/research/%s/repair/%s/ (packets, draft, prepare.diff); the repair "
        "repository .alpaca/research/%s/clones/%s/ and its branch %s; the fix attempt's folder "
        ".alpaca/research/%s/attempts/%s/." % (run, task, run, task, branch, run, fix or "-")]
    result = []
    if prep is not None:
        result.append("- the prepare stage %s ended %s; the case base is %s (event:%d)" % (
            prep["data"].get("stage"), "PASS" if prep["data"].get("ok") else "not PASS",
            prep["data"].get("head") or prep["data"].get("parent"), prep["id"]))
    if rep is not None:
        result.append("- the reproducer is %s (event:%d)" % (
            "proved" if pd.get("proved") else "not proved", rep["id"]))
    if base is not None:
        result.append("- the base pass-to-pass set holds %d test(s) (event:%d)" % (
            len(base_pass), base["id"]))
    if fix:
        seen = []
        for ev in _rows(conn, events.GATE, fix):
            d = ev["data"]
            if d.get("side") not in (None, "attempt") or d.get("stage") in seen:
                continue
            seen.append(d.get("stage"))
            result.append("- gate item %s ended %s on the fix attempt's tree" % (
                d.get("stage"), d.get("verdict")))
    if res is not None:
        result.append("- the repair result is %s (event:%d)" % (outcome, res["id"]))
    else:
        result.append("- no repair result is recorded")
    parts["result"] = result
    deviations = []
    if bd.get("left_out"):
        deviations.append("Tests that already failed on the case base were recorded and left "
                          "out of the base set: %s." % ", ".join(bd["left_out"]))
    if pd.get("supersedes"):
        deviations.append("The reproducer changed after an earlier proof (event %s) and was "
                          "proved again." % pd["supersedes"])
    if rd.get("code"):
        deviations.append("The repair task ended early with code %s." % rd["code"])
    if not deviations:
        if pd.get("proved") and not pd.get("supersedes"):
            deviations.append("No test was left out of the base set and the reproducer was proved "
                              "once, before the first fix round.")
        else:
            deviations.append("No test was left out of the base set.")
    parts["deviations"] = deviations
    # the repair task's own gate logs: the live base FAIL run and the base measurement
    parts["gates"] = _rows(conn, events.GATE, task)
    for kind, what in ((events.REPRODUCER, "reproducer"), (events.PREPARE, "prepare stage"),
                       (events.BASELINE, "base measurement"),
                       (events.REPAIR_RESULT, "repair result")):
        for ev in _rows(conn, kind, task):
            parts["verdict_events"].append(ev["id"])
    extra = []
    if prep is not None:
        diff = prep["data"].get("diff") or {}
        if isinstance(diff, dict) and diff.get("path"):
            extra.append(_local(root, diff.get("path"), "the prepare diff of the case base"))
    for f in pd.get("files") or []:
        if isinstance(f, dict) and pd.get("dir") and f.get("path"):
            extra.append(_local(root, os.path.join(pd["dir"], f["path"]),
                                "a frozen reproducer file"))
    diff = rd.get("diff") if isinstance(rd.get("diff"), dict) else {}
    if diff.get("path"):
        extra.append(_local(root, diff.get("path"), "the fix attempt's diff"))
    if fix:
        for ev in _rows(conn, events.GATE, fix):
            if ev["data"].get("side") in (None, "attempt"):
                extra.append(_local(root, ev["data"].get("log"),
                                    "gate log of %s on the fix" % ev["data"].get("stage")))
    parts["extra"] = [line for line in extra if line]


def _report_sections(conn, root: str, task: str, research: dict) -> dict:
    run = research.get("run")
    folder = paths.reports_dir(root, run)
    finals = _rows(conn, events.FINALIZE, run)
    reports = _rows(conn, events.REPORT, run)
    state = finals[-1]["data"].get("state") if finals else "unknown"
    glance = ("The report of research run %s: report.md and report.json built from the run's "
              "events, which ended in state %s; the replay block rebuilds both from the sealed "
              "event export with no record." % (run, state))
    evidence = []
    for name, note in (("report.md", "the run report"), ("report.json", "the run report data"),
                       ("events.json", "the sealed export of the run's events")):
        line = _local(root, os.path.join(folder, name), note)
        if line:
            evidence.append(line)
    for ev in reports[-1:] + finals[-1:]:
        evidence.append("- event:%d - the run's %s event" % (ev["id"], "report"
                                                             if ev in reports else "finalize"))
    return {"glance": glance,
            "what": ["The loop runner finalized run %s and built its report from the run's "
                     "events at finalize (architecture.md section 4.8)." % run],
            "how": ["report.write built the report; proofs.export_report_events wrote the rows "
                    "it read to events.json, the file the replay block reads."],
            "where": [".alpaca/reports/research/%s/ holds report.md, report.json and "
                      "events.json." % run],
            "result": ["- the run ended in state %s, recorded by its finalize event" % state],
            "deviations": ["None recorded beyond the finalize event's own reason."],
            "evidence": evidence}


GATE_BLOCK = re.compile(r"^# alpaca-gate (\S+) side (tree|base)$")   # first line of a gate block


# ------------------------------------------------------------------ seal and close
def seal(conn, root: str, task: str, *, session: str) -> dict:
    """Render the report, write it to the task's proof path and seal it with proof.seal. Skips
    (returns the latest seal's payload) when the latest proof-report's sha256 equals the report
    rendered again from the record, so a resumed `seal` step never seals twice for nothing. A
    report task whose newest seal holds the run's frozen export (`_sealed_export`) is never sealed
    again by this call: it returns that seal's payload with skipped and frozen true and the
    research-report the export covers as report_event."""
    latest = proof.latest_seal(conn, task)
    research = _research(conn, task)
    if latest is not None and research.get("role") == "report" and research.get("run"):
        # the report task's proof is sealed once with its export (in-session-runtime.md section
        # 20): a later research-report rebuilds report.md in place, so a new seal would pair it
        # with the frozen export of the earlier report and the replay block would rebuild a
        # report that is not the sealed one (t-288 check s1, case C)
        frozen = _sealed_export(conn, root, research["run"])
        if frozen is not None:
            ld = latest["data"] if isinstance(latest.get("data"), dict) else json.loads(latest["data"])
            return dict(ld, event=latest["id"], skipped=True, frozen=True,
                        report_event=frozen["report_event"])
    text = render_attempt(conn, root, task)
    data = text.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    subj = proof.subject(conn, task)
    path = proof.report_path(root, subj)
    if latest is not None:
        ld = latest["data"] if isinstance(latest.get("data"), dict) else json.loads(latest["data"])
        if ld.get("sha256") == sha and os.path.isfile(path):
            with open(path, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() == sha:
                    return dict(ld, event=latest["id"], skipped=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    payload = proof.seal(conn, root, task, session=session)
    newest = proof.latest_seal(conn, task)
    return dict(payload, event=newest["id"] if newest else None, skipped=False)


def close(conn, root: str, task: str, *, session: str, to: str = "done",
          reason: str | None = None) -> int:
    """The lease calls here are alpaca/claims.py's, the board's lease module, which proofs.py
    imports as `from alpaca import claims as leases`; `from . import claims` would import
    alpaca/research/claims.py, t-089's claim extraction (section 2.1), which has no leases.
    First claims.release(conn, task, <run>, session=session) when claims.live(conn, task)
    names worker <run>: `task move` writes no claim event (alpaca/ops.py:380-386), so a lease the
    fold still holds would be set back to doing by the next heartbeat's claims.renew and to open
    by a later claims.release (alpaca/claims.py:106-118, alpaca/claims.py:157-181,
    alpaca/claims.py:239-249; .alpaca/proofs/op-009/t-087.logs/check-buildable-r2-lease-probe.txt:3-5).
    Then cli.main(["--session", session, "task", "move", task, "done", "--proof",
    "local:<report>", "--by", "research-runner"]): --session is an option of the top-level
    parser only (alpaca/cli.py:284), so it comes before the verb. Going through the verb runs
    proof.done_gate and its verify_gate.check, so the gate cannot be skipped. to="blocked" runs
    ["--session", session, "task", "move", task, "blocked", "--reason", reason, "--by",
    "research-runner"] instead (alpaca/ops.py:536-537). When the verb refuses the move, the
    lease is taken back (claims.take, worker <run>) and the verb's code is returned. A task
    already done or blocked is left as it is (PASS)."""
    from alpaca import cli, paths as alpaca_paths
    if to not in ("done", "blocked"):
        raise ValueError("a research task closes to done or blocked, not %r" % (to,))
    try:
        here = os.path.realpath(alpaca_paths.root())
    except Exception:  # noqa: BLE001 - no project above the cwd
        here = None
    if here != os.path.realpath(root):
        # the verb finds its record from the working folder; never move a task in another one
        raise RuntimeError("the working folder is in %s, not in the run's root %s" % (here, root))
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
    if row is not None and row["status"] in ("done", "blocked"):
        return vc.PASS
    research = _research(conn, task)
    worker = research.get("run")
    held = leases.live(conn, task)
    if worker and held is not None and held.get("worker") == worker:
        leases.release(conn, task, worker, session=session)
    if to == "done":
        subj = proof.subject(conn, task)
        argv = ["--session", session, "task", "move", task, "done", "--proof",
                "local:%s" % proof.report_rel(root, subj), "--by", ACTOR]
    else:
        argv = ["--session", session, "task", "move", task, "blocked", "--reason",
                str(reason or "research run stopped"), "--by", ACTOR]
    with _CLOSE_LOCK:
        code = cli.main(argv)
    if code != vc.PASS and worker:
        leases.take(conn, task, worker, session=session)
    return code


# ------------------------------------------------------------------ the report task's export
_COLUMNS = ("id", "ts", "session", "actor", "kind", "op", "ref", "data", "content_hash",
            "prev_hash", "hash", "where_")


def export_report_events(conn, root: str, run: str) -> dict:
    """Before the report task's seal: write .alpaca/reports/research/<run>/events.json, the rows
    report.build reads for the run (operator-surface section 4.3.6: the research events whose ref
    is the run or one of its tasks, those tasks' task events, the run's ship card events, minus
    research-report and research-status) up to the newest research-report's through_event, each
    row as the events table holds it, with every column (id, ts, session, actor, kind, op, ref,
    data, content_hash, prev_hash, hash, where_; alpaca/schema.sql:17-21), plus {"run", "through_event", "report_md_sha256", "report_json_sha256"} from that
    research-report event. Returns {path, sha256, rows, frozen: False}.

    The export is written once (in-session-runtime.md section 20, t-288). It is frozen when the
    newest seal of the run's report task names events.json with the sha256 of the export the
    record gives for a research-report recorded before that seal that covers the run's finished
    finalize (`_sealed_export`); a seal of any other file (a forged or stale events.json sealed
    early, or a mid-run report's export sealed before the finalize) freezes nothing, and the call
    writes the record's export for the newest research-report. Frozen, the call writes nothing,
    whatever run events came after (a second research-report, the verification's events, the
    driver audit), and returns {path, sha256 (of the file as it is, None when it is gone),
    rows: None, frozen: True, seal_event, report_event (the research-report the sealed export
    covers), sealed_sha256, matches}; a file that no longer hashes to its sealed value is left as
    it is for the verifier to refuse, never rewritten to look sealed."""
    folder = paths.reports_dir(root, run)
    path = os.path.join(folder, "events.json")
    sealed = _sealed_export(conn, root, run)
    if sealed is not None:
        have = None
        if os.path.isfile(path) and not os.path.islink(path):
            with open(path, "rb") as fh:
                have = hashlib.sha256(fh.read()).hexdigest()
        return {"path": path, "sha256": have, "rows": None, "frozen": True,
                "seal_event": sealed["event"], "report_event": sealed["report_event"],
                "sealed_sha256": sealed.get("sha256"),
                "matches": have is not None and have == sealed.get("sha256")}
    reports = _rows(conn, events.REPORT, run)
    if not reports:
        raise ValueError("run %s has no research-report event to export for" % run)
    text, count = _export_text(conn, run, reports[-1]["data"])
    os.makedirs(folder, exist_ok=True)
    util.write_text(path, text)
    return {"path": path, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "rows": count, "frozen": False}


def _export_text(conn, run: str, rep: dict) -> tuple:
    """(text, row count) of the export for the research-report whose data is `rep`: the run's
    events through its through_event, deterministic from the append-only record, so the same
    report gives the same bytes on every call."""
    from alpaca.research import state
    through = rep.get("through_event")
    tasks = {a["ref"] for a in state._run_tasks(conn, run)}
    keep = {ev["id"] for ev in state._run_events(conn, run, tasks)}
    have = [c for c in _COLUMNS if c in _event_columns(conn)]
    rows = []
    for row in conn.execute("SELECT %s FROM events ORDER BY id" % ", ".join(have)).fetchall():
        if row["id"] not in keep or (isinstance(through, int) and row["id"] > through):
            continue
        rows.append({c: row[c] for c in have})
    body = {"run": run, "through_event": through, "report_md_sha256": rep.get("sha256"),
            "report_json_sha256": rep.get("json_sha256"), "rows": rows}
    return json.dumps(body, indent=1, sort_keys=True, ensure_ascii=True) + "\n", len(rows)


def _report_task(conn, run: str) -> str | None:
    """The run's report task: the run start's report_task, else the run task whose research
    role is report."""
    from alpaca.research import state
    for ev in _rows(conn, events.RUN_START, run)[:1]:
        task = ev["data"].get("report_task")
        if isinstance(task, str) and task:
            return task
    for ev in state._run_tasks(conn, run):
        research = ev["data"].get("research") if isinstance(ev.get("data"), dict) else None
        if isinstance(research, dict) and research.get("role") == "report":
            return ev["ref"]
    return None


def _sealed_export(conn, root: str, run: str) -> dict | None:
    """The evidence item, with its proof-report id as `event` and the research-report it covers
    as `report_event`, by which the newest seal of the run's report task names
    .alpaca/reports/research/<run>/events.json, when that item's sha256 is the sha256 of the
    record's export for a research-report of the run recorded before the seal (newest first)
    whose through_event is at or after a research-finalize of the run with state finished,
    itself recorded before the seal. None when the report task has no seal, its newest seal does
    not name the export, the run has no finished finalize before the seal, or the sealed file is
    not the record's export for any such research-report (a forged or stale file sealed early:
    t-288 check s1, case D; the genuine export of a mid-run report sealed before the finalize:
    check s2, case S2-5). The match is computed from the record, not
    from the file, so a file changed after a true seal stays frozen for the verifier to refuse."""
    task = _report_task(conn, run)
    if task is None:
        return None
    seal_ev = proof.latest_seal(conn, task)
    if seal_ev is None:
        return None
    data = seal_ev["data"] if isinstance(seal_ev.get("data"), dict) else json.loads(seal_ev["data"])
    want = _rel(root, os.path.join(paths.reports_dir(root, run), "events.json"))
    found = None
    for item in data.get("evidence") or []:
        if not isinstance(item, dict) or item.get("kind") != "local":
            continue
        body = str(item.get("pointer") or "").split(":", 1)[-1].strip()
        if body == want:
            found = item
            break
    if found is None or not isinstance(found.get("sha256"), str):
        return None
    # only the export of a report that covers the run's finished finalize is the run proof: a
    # seal of a mid-run report's export, made before the finalize, freezes nothing, so the close
    # still exports and seals the rows through the finalize (t-288 check s2, case S2-5)
    finals = [f["id"] for f in _rows(conn, events.FINALIZE, run)
              if f["id"] < seal_ev["id"] and f["data"].get("state") == "finished"]
    if not finals:
        return None
    for rep in reversed([r for r in _rows(conn, events.REPORT, run) if r["id"] < seal_ev["id"]]):
        through = rep["data"].get("through_event")
        if not isinstance(through, int) or through < finals[0]:
            continue
        text, _count = _export_text(conn, run, rep["data"])
        if hashlib.sha256(text.encode("utf-8")).hexdigest() == found["sha256"]:
            return dict(found, event=seal_ev["id"], report_event=rep["id"])
    return None


def _event_columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}


def replay_report(events_path: str, run: str, out_dir: str) -> dict:
    """`python3 -m alpaca.research.proofs replay-report`: create an in-memory database from
    alpaca/schema.sql, insert the rows of events_path, each row inserted with every column the
    export holds (so the NOT NULL content_hash is filled), call report.build(conn, out_dir, run) and
    report.render, write report.md and report.json to out_dir (made under the cwd), and compare
    their sha256 with the export's; returns {md_sha256, json_sha256, match}, and main exits 0 on a
    match, 1 otherwise. Reads no file outside events_path and the alpaca package, and no record."""
    from alpaca import migrate
    from alpaca.research import report
    with open(events_path, encoding="utf-8") as fh:
        body = json.load(fh)
    if body.get("run") != run:
        raise ValueError("the export names run %r, not %r" % (body.get("run"), run))
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(migrate.schema_sql())
    cols = _event_columns(conn)
    for row in body.get("rows") or []:
        use = [c for c in _COLUMNS if c in row and c in cols]
        conn.execute("INSERT INTO events (%s) VALUES (%s)" % (", ".join(use), ", ".join("?" * len(use))),
                     [row[c] for c in use])
    out = os.path.abspath(out_dir)
    os.makedirs(out, exist_ok=True)
    built = report.build(conn, out, run)
    md = report.render(built)
    md_bytes = md.encode("utf-8")
    js = (json.dumps(built, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    with open(os.path.join(out, "report.md"), "wb") as fh:
        fh.write(md_bytes)
    with open(os.path.join(out, "report.json"), "wb") as fh:
        fh.write(js)
    md_sha, js_sha = hashlib.sha256(md_bytes).hexdigest(), hashlib.sha256(js).hexdigest()
    return {"md_sha256": md_sha, "json_sha256": js_sha,
            "match": md_sha == body.get("report_md_sha256") and js_sha == body.get("report_json_sha256")}


def main(argv: list[str] | None = None) -> int:
    """replay-report --run R --events F --out D: exit 0 when both rebuilt files have the sha256
    the export names, 1 otherwise; 64 on a usage error."""
    import sys
    parser = vc.make_parser(prog="alpaca.research.proofs", name="alpaca-research-proofs")
    sub = parser.add_subparsers(dest="verb")
    rp = sub.add_parser("replay-report")
    rp.add_argument("--run", required=True)
    rp.add_argument("--events", required=True)
    rp.add_argument("--out", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else vc.USAGE
    if args.verb != "replay-report":
        return vc.USAGE
    try:
        got = replay_report(args.events, args.run, args.out)
    except Exception as exc:  # noqa: BLE001 - any failure to rebuild is a failed replay
        sys.stdout.write("replay-report: FAIL %s: %s\n" % (type(exc).__name__, exc))
        return vc.FAIL
    sys.stdout.write("report.md %s\nreport.json %s\n" % (got["md_sha256"], got["json_sha256"]))
    return vc.PASS if got["match"] else vc.FAIL


if __name__ == "__main__":
    raise SystemExit(main())
