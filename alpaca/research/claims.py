"""Claim extraction: the claims of a seal, from the report and, for an engine task, from the
record (design t-089, verifier.md section 3). Not the board's lease module, alpaca/claims.py.

A claim is one statement the proof makes that a verifier can check. The claims are extracted by
code, once per verification, from the latest seal, and both slots get the same claims file:

    s1       the At a glance section (format 2 reports)
    r1..rN   one per list item of the Result section (the whole section when it has none)
    h1       the How to reproduce section; `commands` holds one entry per fenced block
    e1..eN   one per sealed evidence item of kind local, event, receipt or job, numbered by its
             place in the seal's list
    d1..dN   the task's done bar, kept by the base-and-additions rule of `done_bar`
    x1..     the record claims of an attempt or repair task (section 3.3)

The builder's other prose (What I did, How I did it, Where, Deviations and issues, the context
header and Appendix A) is not a claim and never reaches a verifier.
"""

from __future__ import annotations

import json
import os
import re

from alpaca.research import events as rev_events, pathscan, verify_gate

#: section 3.5: the files that define the steering phrase lists and test them quote the phrases on
#: purpose, so a patch line in one of them is listed as exempt instead of stopping the driver.
STEERING_SOURCES = ("alpaca/research/audit.py", "alpaca/research/claims.py",
                    "alpaca/tests/test_research_audit.py", "alpaca/tests/test_research_verifier.py",
                    "design/research-engine/verifier.md")

#: the phrases of audit rule S12 (verifier.md section 9.3), read from `audit.STEERING` once t-095
#: defines it; until then this copy of the design's list stands in.
_STEERING_DEFAULT = ("note to the auditor", "note for the auditor", "note to the reviewer",
                     "note to the verifier", "dear reviewer", "dear auditor", "please approve",
                     "do not flag this", "ignore previous instructions",
                     "ignore all previous instructions", "disregard the rules above")

SCHEMA = "alpaca-verify-claims/1"
TEXT_CAP = 400
_LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*]|[0-9]+\.)\s+")
_HUNK = re.compile(r"^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,[0-9]+)? @@")
VERIFY_KINDS_PREFIX = ("research-verify-", "research-proof-")


def _collapse(text) -> str:
    return " ".join(str(text or "").lower().split())


def _self_line(line) -> bool:
    n = _collapse(line)
    if n.endswith("."):
        n = n[:-1]
    return n in verify_gate.SELF_LINES


# --------------------------------------------------------------------------- report reading
def _prose(body) -> str:
    from alpaca import proof
    return "\n".join(line for line, inside in proof.fence_scan(body) if not inside).strip()


def fenced_blocks(body) -> list:
    """The fenced code blocks of a section, each as the text between its fence lines."""
    from alpaca import proof
    out, cur, inside_prev = [], None, False
    for line, inside in proof.fence_scan(body):
        if inside and not inside_prev:
            cur = []                         # the opening fence line
        elif inside and inside_prev:
            cur.append(line)
        elif not inside and inside_prev:
            out.append(_close(cur))
            cur = None
        inside_prev = inside
    if inside_prev and cur is not None:
        out.append(_close(cur))
    return out


def _close(lines) -> str:
    """Drop the closing fence line from a block's lines."""
    from alpaca import proof
    if lines and proof._FENCE.match(lines[-1]) and not proof._FENCE.match(lines[-1]).group(2):
        lines = lines[:-1]
    return "\n".join(lines)


def _list_items(body) -> list:
    """The list items of a section outside code fences, each with its continuation lines."""
    from alpaca import proof
    items, cur = [], None
    for line, inside in proof.fence_scan(body):
        if inside:
            if cur is not None:
                cur.append(line)
            continue
        if _LIST_ITEM.match(line):
            if cur is not None:
                items.append("\n".join(cur).strip())
            cur = [_LIST_ITEM.sub("", line, count=1).strip()]
        elif cur is not None and line.strip():
            cur.append(line.strip())
        elif cur is not None and not line.strip():
            items.append("\n".join(cur).strip())
            cur = None
    if cur is not None:
        items.append("\n".join(cur).strip())
    return [i for i in items if i]


def _report_text(root, seal_data) -> str:
    rel = seal_data.get("path")
    full = os.path.join(root, str(rel or "").replace("/", os.sep))
    with open(full, "rb") as fh:
        raw = fh.read()
    import hashlib
    if hashlib.sha256(raw).hexdigest() != seal_data.get("sha256"):
        raise ValueError("the report %s no longer hashes to its seal" % rel)
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- the done bar
def _contracts(conn, task):
    return verify_gate.events(conn, "task-contract", task)


def _first_work_event(conn, task):
    claims = verify_gate.events(conn, "claim", task)
    if claims:
        return claims[0]["id"]
    for ev in verify_gate.events(conn, "task-move", task):
        if ev["data"].get("to") == "doing":
            return ev["id"]
    return None


def _base_and_additions(conn, task, events=None):
    """The contract events in the order the rule reads them: the base first, then every later
    one and every one a session of the work wrote. `events` limits the read to those ids."""
    evs = _contracts(conn, task)
    if events is not None:
        wanted = set(int(e) for e in events)
        evs = [e for e in evs if e["id"] in wanted]
    if not evs:
        return None, []
    work = verify_gate.work_identities(conn, task)["sessions"]
    first = _first_work_event(conn, task)
    base = None
    if first is None:
        base = evs[0]
    else:
        before = [e for e in evs if e["id"] < first and e["session"] not in work]
        base = before[-1] if before else None
    rest = [e for e in evs if base is None or e["id"] != base["id"]]
    if base is not None:
        rest = [e for e in rest if e["id"] > base["id"] or e["session"] in work]
    return base, rest


def _merge(base, rest, part) -> list:
    lines, seen = [], set()
    for ev in ([base] if base else []) + rest:
        for line in ev["data"].get(part) or []:
            key = _collapse(line)
            if key and key not in seen:
                seen.add(key)
                lines.append(str(line).strip())
    return lines


def done_bar(conn, task) -> dict:
    """{"lines", "contracts"}: the done bar the `d` claims come from (section 3.2). The base is
    the newest contract recorded before the task's first claim that no session of the work
    wrote; every later contract, and every one a session of the work wrote, adds the lines it
    holds that the set does not hold yet; no line ever leaves the set."""
    base, rest = _base_and_additions(conn, task)
    evs = ([base] if base else []) + rest
    return {"lines": _merge(base, rest, "done_bar"), "contracts": sorted(e["id"] for e in evs)}


def contract_view(conn, task, *, events: list | None = None) -> dict:
    """{"input", "expected", "done_bar", "fail_cases", "contracts"}: every part of the task
    contract under the same base-and-additions rule, read from exactly the contract events
    `events` names when given (the open event's `contracts`), so a contract recorded after the
    open never reaches a brief."""
    base, rest = _base_and_additions(conn, task, events)
    evs = ([base] if base else []) + rest
    out = {part: _merge(base, rest, part) for part in ("input", "expected", "done_bar", "fail_cases")}
    out["contracts"] = sorted(e["id"] for e in evs)
    return out


# --------------------------------------------------------------------------- evidence
def _forbidden_evidence(conn, root, task, item, traces=()) -> bool:
    """True when an evidence item points at a verification of this task (section 3.2), or is a
    byte copy of a kept review or of a trace an earlier verification left in its folders
    (`traces`, the hashes of verify_gate.earlier_traces; section 5.2)."""
    ptr = str(item.get("pointer") or "")
    kind = item.get("kind")
    body = ptr.split(":", 1)[1].strip() if ":" in ptr else ""
    if kind == "event":
        ev = verify_gate.event_by_id(conn, int(body)) if body.isdigit() else None
        if ev and ev["ref"] == task and str(ev["kind"]).startswith(VERIFY_KINDS_PREFIX):
            return True
    if kind == "local":
        prefix = ".alpaca/verify/%s/" % verify_gate.slug(task)
        if body.replace("\\", "/").lstrip("./").startswith(prefix):
            return True
    sha = item.get("sha256")
    if sha and sha in traces:
        return True
    if sha:
        for ev in verify_gate.events(conn, verify_gate.KIND_REVIEW, task):
            if ev["data"].get("sha256") == sha:
                return True
    return False


def _copy_name(root, pos, item) -> str | None:
    """Where the brief places an evidence item's copy, relative to the try folder."""
    from alpaca import proof
    kind = item.get("kind")
    if kind == "event":
        body = str(item.get("pointer")).split(":", 1)[1].strip()
        return "packet/events/%s.json" % body
    if kind == "local":
        kept = item.get("kept")
        if kept:
            return "packet/evidence/%s" % os.path.basename(str(kept))
        body = str(item.get("pointer")).split(":", 1)[1].strip()
        return "packet/evidence/%02d-%s" % (pos, proof._slug(os.path.basename(body)))
    if kind in ("receipt", "job"):
        target = proof._evidence_file(root, kind, item.get("pointer"))
        name = os.path.basename(target) if target else "file"
        return "packet/evidence/%02d-%s" % (pos, proof._slug(name))
    return None


#: a pointer is named only where it ends at a token end, so `event:12` is not named inside
#: `event:123` and `local:a.log` not inside `local:a.log.1`; a `.` that ends a sentence is a token
#: end (t-296)
_TOKEN_END = r"(?![0-9A-Za-z_/-]|\.[0-9A-Za-z_/-])"


def _names(text, word) -> bool:
    return bool(word) and re.search(re.escape(word) + _TOKEN_END, text) is not None


def _named(text, pointers) -> list:
    """The sealed pointers a claim's text names: the whole pointer, or a local path body, each
    only where it ends at a token end."""
    out = []
    for ptr in pointers:
        body = ptr.split(":", 1)[1].strip() if ":" in ptr else ptr
        if _names(text, ptr) or (ptr.startswith(("local:", "receipt:", "job:")) and body
                                 and _names(text, body)):
            if ptr not in out:
                out.append(ptr)
    return out


#: the classes a claim the extractor writes can have (t-296, design 25.2); `any` is not one
BOUND_CLASSES = ("rerun", "rehash", "apply")
#: the class of a claim that names nothing a receipt checks: C5 refuses it and the verifier stops
#: at open on it (verifier.open_verification)
UNBOUND = "unbound"


def _requires(commands, pointers) -> str:
    """The class of a claim from its binding: `rerun` when it records commands, else `rehash`
    when it names sealed pointers, else UNBOUND."""
    if commands:
        return "rerun"
    if pointers:
        return "rehash"
    return UNBOUND


def bound(claim) -> bool:
    """True when a receipt of the verifier's own try can check the claim (t-296, design 25.2): a
    `rerun` claim that records commands, a `rehash` claim that names pointers, or the `apply`
    claim whose sealed copy is the packet's diff. A claim of any other class, `any` included,
    or with an empty binding, is not bound."""
    if not isinstance(claim, dict):
        return False
    req = claim.get("requires")
    if req == "apply":
        return any(isinstance(x, dict) and x.get("copy") == "packet/diff.patch"
                   for x in claim.get("sealed") or [])
    if req == "rerun":
        return bool(claim.get("commands"))
    if req == "rehash":
        return bool(claim.get("pointers"))
    return False


# --------------------------------------------------------------------------- engine claims
def _events_of(conn, kind, task):
    return verify_gate.events(conn, kind, task)


def record_source(conn, task):
    """The attempt event of an attempt task, or the `research-repair-result` of a repair task
    (section 3.3), or None."""
    role = verify_gate.role_of(conn, task)
    if role == "attempt":
        evs = _events_of(conn, rev_events.ATTEMPT, task)
        return evs[-1] if evs else None
    if role == "repair":
        evs = _events_of(conn, rev_events.REPAIR_RESULT, task)
        return evs[-1] if evs else None
    return None


def _accept_event(conn, task, attempt_data):
    """The `research-accept` event the attempt event names (`accept_event`), else the attempt's
    newest one; None when the attempt has none or the named event is not an accept row."""
    eid = attempt_data.get("accept_event")
    if isinstance(eid, int):
        ev = verify_gate.event_by_id(conn, eid)
        if ev is not None and ev.get("kind") == rev_events.ACCEPT and ev.get("ref") == task:
            return ev
    evs = _events_of(conn, rev_events.ACCEPT, task)
    return evs[-1] if evs else None


def engine_claims(conn, task) -> list:
    """The record claims of an attempt or repair task (section 3.3): `x1` (the diff applies to the
    commit the attempt forked from), `x2` (the gate chain as recorded), `m1..mN` (each metric's
    repeats and median), `x3` (the accept rule's outcome), `x4` (what the audit found), `x5` (the
    fingerprint points) for an attempt; `x1`, `x2`, `y1` and `y2` for a repair task. A claim whose
    record event is missing is left out."""
    role = verify_gate.role_of(conn, task)
    src = record_source(conn, task)
    if src is None or role not in ("attempt", "repair"):
        return []
    d = src["data"]
    out = []
    diff = d.get("diff") if isinstance(d.get("diff"), dict) else {}
    forked = d.get("forked_from")
    if diff:
        if diff.get("empty"):
            text = ("the attempt made no change on %s: its recorded diff %s is the zero-byte file, "
                    "and it applies there with no change" % (forked, diff.get("path")))
        else:
            text = ("the attempt diff %s (sha256 %s) applies to the commit %s the attempt forked "
                    "from" % (diff.get("path"), diff.get("sha256"), forked))
        out.append({"id": "x1", "source": "record", "text": text, "pointers": [],
                    "sealed": [{"pointer": "local:%s" % diff.get("path"), "sha256": diff.get("sha256"),
                                "copy": "packet/diff.patch"}],
                    "commands": [], "requires": "apply"})
    gate_task = task
    if role == "repair" and d.get("fix_attempt"):
        gate_task = d.get("fix_attempt")
    gates = [e for e in _events_of(conn, rev_events.GATE, gate_task)
             if e["data"].get("side") in (None, "attempt")]
    if gates:
        seen, parts, first_fail = [], [], None
        for e in gates:
            g = e["data"]
            st = g.get("stage")
            if st in seen:
                continue
            seen.append(st)
            parts.append("%s %s rc %s" % (st, g.get("verdict"), g.get("rc")))
            if first_fail is None and g.get("verdict") not in ("PASS", None):
                first_fail = st
        text = "the gate chain ended as recorded: %s%s" % (
            "; ".join(parts), "; the first failing stage is %s" % first_fail if first_fail else "")
        out.append({"id": "x2", "source": "record", "text": text, "pointers": [], "sealed": [],
                    "commands": [{"gate": s, "cwd": "tree"} for s in seen], "requires": "rerun"})
    if role == "attempt":
        from alpaca.research import gates as _gates
        metrics = d.get("metrics") if isinstance(d.get("metrics"), dict) else {}
        for k, mid in enumerate(sorted(metrics), 1):
            m = metrics[mid] if isinstance(metrics[mid], dict) else {}
            # the gate item of a metric is `metric:<id>` in the frozen chain (gates.chain), so a
            # verifier's `bin/rerun --gate` names an item the chain holds (t-173 fix batch 3, F17)
            out.append({"id": "m%d" % k, "source": "record",
                        "text": "metric %s had repeats %s and median %s" % (
                            mid, m.get("repeats"), m.get("median")),
                        "pointers": [], "sealed": [],
                        "commands": [{"gate": _gates._METRIC + str(mid), "cwd": "tree"}],
                        "requires": "rerun"})
        # x3 states the accept rule's own result: the outcome of the attempt's `research-accept`
        # event. The attempt event's `outcome` is the outcome of the whole attempt (an audit that
        # broke it makes it `broken` after the rule said `improvement`), so it is not the rule's
        # result (t-191 F27). The accept event is the one the attempt event names, else the
        # attempt's newest; an attempt that never reached the rule has no x3.
        accept = _accept_event(conn, task, d)
        if accept is not None:
            ad = accept["data"]
            # bound to the accept row itself (t-296): the verifier checks the exported row by
            # its chain hash with `--event`
            out.append({"id": "x3", "source": "record",
                        "text": "the accept rule (event:%d) gives %s, accepted %s, for medians %s "
                                "against the champion's %s"
                                % (accept["id"], ad.get("outcome"),
                                   json.dumps(bool(ad.get("accepted"))),
                                   json.dumps(metrics, sort_keys=True),
                                   json.dumps(d.get("champion_metrics"), sort_keys=True)),
                        "pointers": ["event:%d" % accept["id"]],
                        "sealed": [_event_seal(conn, accept["id"])],
                        "commands": [], "requires": "rehash"})
        audit_id = d.get("audit_event")
        audit = verify_gate.event_by_id(conn, audit_id) if isinstance(audit_id, int) else None
        if audit is not None:
            a = audit["data"]
            out.append({"id": "x4", "source": "record",
                        "text": "the attempt audit found what its event says: verdict %s, flags %s"
                                % (a.get("verdict"), json.dumps(a.get("flags"), sort_keys=True)),
                        "pointers": ["event:%d" % audit["id"]],
                        "sealed": [{"pointer": "event:%d" % audit["id"], "sha256": _chain_hash(conn, audit["id"]),
                                    "copy": "packet/events/%d.json" % audit["id"]}],
                        "commands": [], "requires": "rehash"})
        tamper = _events_of(conn, rev_events.TAMPER, task)
        if tamper:
            # one sealed pointer per fingerprint row (t-296)
            out.append({"id": "x5", "source": "record",
                        "text": "every fingerprint point of the attempt recorded what its event "
                                "says: %s" % ", ".join("%s %s (event:%d)" % (
                                    t["data"].get("point"), t["data"].get("verdict"), t["id"])
                                    for t in tamper),
                        "pointers": ["event:%d" % t["id"] for t in tamper],
                        "sealed": [_event_seal(conn, t["id"]) for t in tamper],
                        "commands": [], "requires": "rehash"})
    else:
        rep_ev = d.get("reproducer_event")
        rep_row = verify_gate.event_by_id(conn, rep_ev) if isinstance(rep_ev, int) else None
        # y1 states what the reproducer row says, never "proved" for a row that says not (t-296)
        proved = bool(rep_row is not None and (rep_row.get("data") or {}).get("proved"))
        for cid, key, what in (("y1", "reproducer_event",
                                "the reproducer was proved: it failed on the case base before any "
                                "fix ran" if proved else "the reproducer was not proved"),
                               ("y2", "prepare_event",
                                "the case base is the commit the prepare stage made")):
            eid = d.get(key)
            if isinstance(eid, int) and verify_gate.event_by_id(conn, eid) is not None:
                out.append({"id": cid, "source": "record", "text": "%s (event:%d)" % (what, eid),
                            "pointers": ["event:%d" % eid],
                            "sealed": [{"pointer": "event:%d" % eid, "sha256": _chain_hash(conn, eid),
                                        "copy": "packet/events/%d.json" % eid}],
                            "commands": [], "requires": "rehash"})
    return out


def _chain_hash(conn, eid):
    row = conn.execute("SELECT hash FROM events WHERE id=?", (eid,)).fetchone()
    return row["hash"] if row else None


def _event_seal(conn, eid) -> dict:
    """The sealed entry of a record claim's `event:` pointer: the row's chain hash and its export
    under packet/events/, the shape x4 and y1 use."""
    return {"pointer": "event:%d" % eid, "sha256": _chain_hash(conn, eid),
            "copy": "packet/events/%d.json" % eid}


# --------------------------------------------------------------------------- extraction
def extract(conn, root, task, seal_event: dict) -> dict:
    """The claims.json object of the latest seal (section 3.1). Raises ValueError when the
    sealed report no longer hashes to its seal."""
    from alpaca import proof
    seal_data = verify_gate._load(seal_event.get("data"))
    text = _report_text(root, seal_data)
    sections = proof.split_sections(text)
    role = verify_gate.role_of(conn, task)
    engine = verify_gate.research_of(conn, task) is not None
    attempt_like = role in ("attempt", "repair")
    fmt2 = proof.report_format(conn, task, text) >= 2
    vf = verify_gate.verification_id(task, seal_event["id"])

    evidence = [e for e in (seal_data.get("evidence") or []) if isinstance(e, dict)]
    traces = set(verify_gate.earlier_traces(conn, root, task, before=seal_event["id"])["hashes"])
    claims, skipped, sealed_of = [], [], {}
    for pos, item in enumerate(evidence, 1):
        kind = item.get("kind")
        if kind == "remote":
            skipped.append({"index": pos, "pointer": item.get("pointer"), "code": "remote",
                            "why": "remote pointer; the seal records it unverified"})
            continue
        if kind not in ("local", "event", "receipt", "job"):
            continue
        if _forbidden_evidence(conn, root, task, item, traces):
            skipped.append({"index": pos, "pointer": None, "code": "VERIFY-EVIDENCE-FORBIDDEN",
                            "why": "an earlier verification of this task is withheld from the "
                                   "verifiers"})
            continue
        sealed_of[item["pointer"]] = {"pointer": item["pointer"], "sha256": item.get("sha256"),
                                      "copy": _copy_name(root, pos, item), "index": pos}
    pointers = list(sealed_of)

    def sealed_for(ptrs):
        return [{k: sealed_of[p][k] for k in ("pointer", "sha256", "copy")} for p in ptrs]

    # h1 first: its blocks are the commands other claims carry
    how = sections.get("How to reproduce", "")
    blocks = fenced_blocks(how)
    h_cmds, gate_ids = [], []
    for block in blocks:
        first = block.split("\n", 1)[0].strip()
        m = pathscan.GATE_BLOCK.match(first)
        if attempt_like and m:
            h_cmds.append({"gate": m.group(1), "cwd": m.group(2)})
            gate_ids.append(m.group(1))
        else:
            h_cmds.append({"cwd": "tree", "text": block})

    def rerun_all():
        return [dict(c) for c in h_cmds]

    if fmt2 and proof.SUMMARY in sections:
        body = _prose(sections[proof.SUMMARY])
        if body:
            named = _named(body, pointers)
            # s1 of an attempt or repair task is bound to the rows its glance names as `event:N`
            # (F43, F50; design 25.2): it states only the record source's facts. The report task's
            # glance describes the replay block, so its s1 re-runs it. A glance that names no row
            # is unbound, and the verification stops at open (verifier.open_verification).
            if engine and attempt_like:
                cmds = []
            else:
                cmds = rerun_all()
            claims.append({"id": "s1", "source": "summary", "text": body, "pointers": named,
                           "sealed": sealed_for(named), "commands": cmds,
                           "requires": _requires(cmds, named) if engine else "rerun"})
    result = sections.get("Result", "")
    items = _list_items(result) or ([_prose(result)] if _prose(result) else [])
    for k, item in enumerate(items, 1):
        named = _named(item, pointers)
        if attempt_like:
            # a line names its gate item (re-run) or the rows it states as `event:N` (re-hash);
            # a line that names a row is bound to the row alone, so a gate id inside its free
            # text (an accept reason) adds no re-run; a line that names neither is unbound
            # (t-296: no `any`)
            cmds = [c for c in h_cmds if "gate" in c and re.search(
                r"(?<![A-Za-z0-9_.-])%s(?![A-Za-z0-9_-])" % re.escape(c["gate"]), item)]
            if any(p.startswith("event:") for p in named):
                cmds = []
            req = _requires(cmds, named)
        else:
            cmds, req = rerun_all(), "rerun"
        claims.append({"id": "r%d" % k, "source": "result", "text": item, "pointers": named,
                       "sealed": sealed_for(named), "commands": cmds, "requires": req})
    if how.strip():
        named = _named(how, pointers)
        claims.append({"id": "h1", "source": "reproduce", "text": how.strip(), "pointers": named,
                       "sealed": sealed_for(named), "commands": rerun_all(), "requires": "rerun"})
    for ptr in pointers:
        pos = sealed_of[ptr]["index"]
        item = evidence[pos - 1]
        note = item.get("note")
        claims.append({"id": "e%d" % pos, "source": "evidence",
                       "text": "%s - %s" % (ptr, note) if note else ptr,
                       "pointers": [ptr], "sealed": sealed_for([ptr]), "commands": [],
                       "requires": "rehash"})
    contracts = []
    bar = done_bar(conn, task)
    contracts = bar["contracts"]
    if not attempt_like:
        k = 0
        for line in bar["lines"]:
            if _self_line(line):
                skipped.append({"index": None, "pointer": None, "code": "self", "text": line,
                                "why": "a done-bar line in verify_gate.SELF_LINES"})
                continue
            k += 1
            claims.append({"id": "d%d" % k, "source": "done-bar",
                           "text": "the work meets: %s" % line, "pointers": [], "sealed": [],
                           "commands": rerun_all(), "requires": "rerun"})
    else:
        for line in bar["lines"]:
            skipped.append({"index": None, "pointer": None, "code": "self", "text": line,
                            "why": "a done-bar line of an attempt or repair task; the record "
                                   "claims test it"})
        claims.extend(engine_claims(conn, task))
    return {"schema": SCHEMA, "verification": vf, "task": task, "seal_event": seal_event["id"],
            "report_sha256": seal_data.get("sha256"), "engine": engine, "role": role,
            "claims": claims, "contracts": contracts, "skipped": skipped,
            "blocks": blocks}


# --------------------------------------------------------------------------- steering
def steering(claims: dict, patch: str | None, extra: tuple = ()) -> dict:
    """{"hits", "exempt"}: the claim texts and the added lines of the sealed patch that hold a
    phrase addressed to a reviewer (section 3.5). A patch line in STEERING_SOURCES is exempt."""
    try:
        from alpaca.research import audit
        phrases = tuple(getattr(audit, "STEERING", ()) or ()) or _STEERING_DEFAULT
    except Exception:
        phrases = _STEERING_DEFAULT
    phrases = tuple(_collapse(p) for p in phrases + tuple(extra or ()) if _collapse(p))
    hits, exempt = [], []
    for c in (claims or {}).get("claims") or []:
        n = _collapse(c.get("text"))
        for p in phrases:
            if p in n:
                hits.append({"claim": c.get("id"), "file": None, "line": None, "phrase": p})
                break
    current, line_no = None, 0
    for raw in str(patch or "").splitlines():
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            if name.startswith("b/"):
                name = name[2:]
            current = None if name == "/dev/null" else name
            continue
        if raw.startswith("--- "):
            continue
        m = _HUNK.match(raw)
        if m:
            line_no = int(m.group(1))
            continue
        if raw.startswith("+"):
            n = _collapse(raw[1:])
            for p in phrases:
                if p in n:
                    entry = {"claim": None, "file": current, "line": line_no, "phrase": p}
                    if current in STEERING_SOURCES:
                        exempt.append({"file": current, "line": line_no})
                    else:
                        hits.append(entry)
                    break
            line_no += 1
        elif raw.startswith(" "):
            line_no += 1
    return {"hits": hits, "exempt": exempt}


# --------------------------------------------------------------------------- the files
def lines(claims: dict) -> list:
    """The lines of claims.txt: a header, then one line per claim in file order."""
    out = ["# claims of %s" % claims.get("verification")]
    for c in claims.get("claims") or []:
        body = " ".join(str(c.get("text") or "").splitlines())[:TEXT_CAP]
        out.append("%s\t%s" % (c.get("id"), body))
    return out


def write(root, verification, claims: dict) -> dict:
    """Write claims.json and claims.txt into the verification folder: {"json", "txt",
    "sha256"}; `sha256` is claims.json's, which the open event records."""
    import hashlib
    folder = verify_gate.verification_dir(root, claims.get("task"), verification)
    os.makedirs(folder, exist_ok=True)
    body = (json.dumps(claims, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode()
    jpath = os.path.join(folder, "claims.json")
    tpath = os.path.join(folder, "claims.txt")
    for path, data in ((jpath, body), (tpath, ("\n".join(lines(claims)) + "\n").encode())):
        part = path + ".part"
        with open(part, "wb") as fh:
            fh.write(data)
        os.replace(part, path)
    return {"json": jpath, "txt": tpath, "sha256": hashlib.sha256(body).hexdigest()}
