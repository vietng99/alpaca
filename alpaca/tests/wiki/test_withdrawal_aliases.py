"""op-008 t-067: a source withdrawn by any name the verb accepts is withdrawn whole, on every serve
path, at the next capture and in `wiki recover`, and nothing else is (design/rune2-memory/architecture.md
8.3, 10.2; FR-17, FR-22). Check rounds 1 to 4 of t-067 each found one name of a source that left another
name of it, or a copy, served or counted; this is the alias matrix behind those findings (its
development-only script sits with the t-067 execution notes) as tests.

The world: two sessions with registered transcripts; s-src holds a task-add (t-13 in op-3), a result
note, a decision, a task contract (t-13 in op-13) and a proof report, so event notes, their decision:,
contract: and proof: copies and their record rule edges exist; s-tx holds a result note; one document
comes in through `wiki ingest` and one through the evaluation fixtures' write path (`Absorber.absorb_text`
of kind wiki). Each event-kind source carries an admitted agent fact on one of its names, the note a
checked one too, and a checked candidate on the s-tx note keeps context quotes from the s-tx transcript
and both documents. Each case withdraws one source by one name, then reads the serve paths (query,
context, a read pinned to a knowledge time before the withdrawal, the candidates view, the record-facts
dry run, the rows `wiki extract` copies, status with the expected set and a question scoped to the
source's op) and the store, right after the withdrawal, after the next captures (the Stop-hook call, a
SessionEnd-style capture, the documents ingested again, the withdrawn one in new bytes too) and after
`wiki recover` into a fresh store; and it asks again by every other name on a copy of the project.

A source copy and the event note it was rendered from are one source, the record event both come from
(architecture.md 4.2 `origin_hash`, 8.3, 10.2 steps 1 and 7; `servable._source_copy_links`): withdrawing
either, by any name, withdraws both, and the note retired with its copy is not something retired that was
not withdrawn. A transcript is `transcript:<session>` and the notes of its session are other sources; an
ingested document is `document:<doc id>`. Real record, drain, store and recovery; nothing is mocked.
"""
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import at, store                              # noqa: E402

from alpaca import db as record                                   # noqa: E402
from alpaca.wiki import recovery, service                         # noqa: E402
from alpaca.wiki.clock import FixedClock, now_iso                 # noqa: E402

SESSIONS = ("s-src", "s-tx")
DEC_ID = "dec-a1b2c3d4e5f6"
PROOF_PATH = ".alpaca/proofs/op-14/t-14.md"
PROOF_TEXT = ("# Proof t-14\n\n## At a glance\n\nThe ember queue stalled because the cinder lock stayed "
              "held. Task t-14 is blocked on the cinder lock.\n")
PROOF_SHA = hashlib.sha256(PROOF_TEXT.encode("utf-8")).hexdigest()
NOTE_BODY = "The lantern rota moved to Tuesday. Task t-11 is blocked on the lantern review."
OSPREY_BODY = "The osprey rota moved to Friday. Task t-16 is blocked on the osprey count."
KESTREL = "The kestrel queue drains nightly at the zircon hour."
IBIS = "The ibis pump restarts weekly at the slate dock."
HERON_DOC, HERON = "wiki/notes/heron.md", "The heron gate opens at dawn behind the cobalt shed.\n"
TERN_DOC, TERN = "wiki/synthetic/tern-net.md", "The tern net is mended every Thursday at the amber pier.\n"
T_AFTER = "2026-03-01T23:00:00+00:00"
EVENT_KINDS = ("note", "decision", "contract", "proof")


def ts(h, m=0):
    return "2026-03-01T%02d:%02d:00+00:00" % (h, m)


def appender(project, session, when=None):
    def append_wiki(kind, data):
        conn = record.connect(str(project))
        try:
            return record.append_event(conn, session=session, actor="alpaca-wiki", kind=kind, data=data,
                                       clock=FixedClock(when) if when else None)["id"]
        finally:
            conn.close()
    return append_wiki


def record_hash(project, eid):
    conn = record.connect_readonly(str(project))
    try:
        return conn.execute("SELECT hash FROM events WHERE id=?", (eid,)).fetchone()[0]
    finally:
        conn.close()


def register_transcript(project, sid, text, when):
    folder = Path(project, ".alpaca", "transcripts")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (sid + ".jsonl")
    path.write_text(json.dumps({"type": "user", "timestamp": when, "message": {"content": text}}) + "\n",
                    encoding="utf-8")
    conn = record.connect(str(project))
    try:
        record.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-03-01T00:00:00+00:00",
                                                "transcript": str(path)})
    finally:
        conn.close()


def ingest_plain(project, text=HERON):
    page = Path(project, "heron-source.md")
    page.write_text(text, encoding="utf-8")
    return service.ingest(str(project), page, doc_id=HERON_DOC, session="s-wiki")


def ingest_synthetic(project, text=TERN):
    """The evaluation fixtures' ingest act (eval/rebuild.py `_apply_fixture`)."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    cfg = Config.for_project(str(project))
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg)
    try:
        target = cfg.vault_dir / TERN_DOC
        target.parent.mkdir(parents=True, exist_ok=True)
        absorber.absorb_text(TERN_DOC, text, kind="wiki", path=TERN_DOC,
                             source_created_at="2026-03-01T07:00:00+00:00")
        target.write_text(text, encoding="utf-8")
    finally:
        absorber.close()


def full_capture(project):
    return service.capture(str(project), sessions=list(SESSIONS), session_events=True, transcripts=True,
                           deadline_s=None)


def fact(subject, quote, ref, *, h=None, context=()):
    anchor = {"source_ref": ref, "quote": quote}
    if h:
        anchor["hash"] = h
    return {"kind": "fact", "fact": {"subject": subject, "predicate": "status", "obj": "blocked",
                                      "obj_datatype": "string"}, "anchors": [anchor], "context": list(context)}


def propose(project, body):
    receipt = service.propose(str(project), body, session="s-agent", record=appender(project, "s-agent"))
    assert receipt.status == "checked", receipt
    return receipt.candidate_id


def build_world(project):
    register_transcript(project, "s-src", IBIS, "2026-03-01T08:00:00Z")
    register_transcript(project, "s-tx", KESTREL, "2026-03-01T11:30:00Z")
    e_add = at(project, "task-add", {"title": "Build the gravel sorter"}, ts(9), session="s-src", op="op-3",
               ref="t-13")
    e_note = at(project, "result", {"body": NOTE_BODY}, ts(9, 10), session="s-src", op="op-11", ref="t-11")
    e_dec = at(project, "decision",
               {"id": DEC_ID, "kind": "design",
                "context": "Which store holds the quokka facts? Task t-12 is blocked on the quokka audit.",
                "options": ["a ledger", "a table"], "choice": "keep the quokka facts in the marble table",
                "consequence": "Replay rebuilds the marble table.", "pointer": "", "ts": ts(9, 20)},
               ts(9, 20), session="s-src", op="op-12", ref=DEC_ID)
    e_con = at(project, "task-contract",
               {"input": ["the walrus feed"], "expected": ["the walrus feed lands in the saffron bucket"],
                "done_bar": ["Task t-13 is blocked on the walrus intake."],
                "fail_cases": ["the saffron bucket overflows"], "stage": "build", "source": DEC_ID},
               ts(10), session="s-src", op="op-13", ref="t-13")
    proof = Path(project, PROOF_PATH)
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_bytes(PROOF_TEXT.encode("utf-8"))
    e_proof = at(project, "proof-report",
                 {"path": PROOF_PATH, "sha256": PROOF_SHA, "bytes": len(PROOF_TEXT.encode()),
                  "sections": ["At a glance"], "evidence": []},
                 ts(10, 30), session="s-src", op="op-14", ref="t-14")
    e_osprey = at(project, "result", {"body": OSPREY_BODY}, ts(11), session="s-tx", op="op-15", ref="t-16")
    full_capture(project)
    ingest_plain(project)
    ingest_synthetic(project)
    for subject, ref, h in (("t-11", "event:%d" % e_note, record_hash(project, e_note)),
                            ("t-12", "decision:" + DEC_ID, None), ("t-13", "contract:t-13", None),
                            ("t-14", "proof:" + PROOF_PATH, None)):
        cid = propose(project, fact(subject, "Task %s is blocked" % subject, ref, h=h))
        assert service.admit(str(project), cid, session="s-admin",
                             record=appender(project, "s-admin")).status == "applied"
    propose(project, fact("t-11", "Task t-11 is blocked on the lantern review", "event:%d" % e_note,
                          h=record_hash(project, e_note)))
    propose(project, fact("t-16", "Task t-16 is blocked", "event:%d" % e_osprey, h=record_hash(project, e_osprey),
                          context=[{"source_ref": "transcript:s-tx", "quote": KESTREL},
                                   {"source_ref": "document:" + HERON_DOC, "quote": HERON.strip()},
                                   {"source_ref": "document:" + TERN_DOC, "quote": TERN.strip()}]))
    sources = {
        "taskadd": dict(refs=["event:%d" % e_add], events=[e_add], question="What does task t-13 build?",
                        markers=["gravel"]),
        "note": dict(refs=["event:%d" % e_note], events=[e_note], op="op-11",
                     question="When did the lantern rota move?",
                     markers=["Tuesday", "Task t-11 is blocked", "lantern review"]),
        "decision": dict(refs=["event:%d" % e_dec, "decision:" + DEC_ID], events=[e_dec], op="op-12",
                         question="Which store holds the quokka facts?",
                         markers=["marble", "Task t-12 is blocked", "quokka audit"]),
        "contract": dict(refs=["event:%d" % e_con, "contract:t-13"], events=[e_con], op="op-13",
                         question="Which bucket does the walrus feed land in?",
                         markers=["saffron", "Task t-13 is blocked", "walrus intake", "source: " + DEC_ID]),
        "proof": dict(refs=["event:%d" % e_proof, "proof:" + PROOF_PATH], events=[e_proof], op="op-14",
                      question="Why did the ember queue stall?",
                      markers=["cinder", "Task t-14 is blocked", PROOF_SHA[:16]]),
        "osprey": dict(refs=["event:%d" % e_osprey], events=[e_osprey], question="When did the osprey rota move?",
                       markers=["Friday", "osprey count"]),
        "transcript": dict(refs=["transcript:s-tx"], events=[], question="When does the kestrel queue drain?",
                           markers=["zircon", "s-tx.jsonl"]),
        "ibis": dict(refs=["transcript:s-src"], events=[], question="When does the ibis pump restart?",
                     markers=["slate dock"]),
        "wiki-plain": dict(refs=["document:" + HERON_DOC], events=[], question="When does the heron gate open?",
                           markers=["cobalt"]),
        "wiki-synthetic": dict(refs=["document:" + TERN_DOC], events=[], question="When is the tern net mended?",
                               markers=["amber pier"]),
    }
    with store(project) as conn:
        for s in sources.values():
            s["docs"] = sorted({d for ref in s["refs"] for (d,) in conn.execute(
                "SELECT DISTINCT doc_id FROM source_versions WHERE source_ref=?", (ref,))})
    return {"sources": sources, "events": [e_add, e_note, e_dec, e_con, e_proof, e_osprey]}


COPY = {"decision": "decision:" + DEC_ID, "contract": "contract:t-13", "proof": "proof:" + PROOF_PATH}


def names_of(world, kind):
    """Every name of the source the verb accepts (`service._SOURCE_REF`), as {label: ref}."""
    s = world["sources"][kind]
    if kind in EVENT_KINDS:
        note = next(d for d in s["docs"] if not d.startswith("raw/sources/"))
        out = {"event:N": s["refs"][0], "document:<note>": "document:" + note, "wiki:<note>": "wiki:" + note}
        if kind in COPY:
            copy = next(d for d in s["docs"] if d.startswith("raw/sources/"))
            out.update({"copy": COPY[kind], "document:<copy>": "document:" + copy, "wiki:<copy>": "wiki:" + copy})
        return out
    if kind == "transcript":
        return {"transcript:<sid>": "transcript:s-tx", "document:<doc>": "document:" + s["docs"][0],
                "wiki:<doc>": "wiki:" + s["docs"][0]}
    return {"document:<doc>": s["refs"][0], "wiki:<doc>": "wiki:" + s["refs"][0][len("document:"):]}


CASES = ([("note", n) for n in ("event:N", "document:<note>", "wiki:<note>")]
         + [(k, n) for k in COPY for n in ("event:N", "document:<note>", "wiki:<note>", "copy",
                                           "document:<copy>", "wiki:<copy>")]
         + [("transcript", n) for n in ("transcript:<sid>", "document:<doc>", "wiki:<doc>")]
         + [(k, n) for k in ("wiki-plain", "wiki-synthetic") for n in ("document:<doc>", "wiki:<doc>")])


def has(text, needle):
    return needle.lower() in text.lower()


def strings(value, out):
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            if k != "question":
                strings(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            strings(v, out)
    elif hasattr(value, "__dict__"):
        strings(vars(value), out)
    return out


def extract_rows(project):
    """The rows `wiki extract` copies out of the store (`extract._copy_public`), written into a fresh
    vault, every byte of it as text. `extract.emit` itself also compiles a projection, which refuses the
    `proof:` node of this world as an artifact name before any withdrawal."""
    from alpaca.wiki import extract
    from alpaca.wiki.config import Config
    from alpaca.wiki.store.db import DB
    from alpaca.wiki.store.ledger import Ledger
    from alpaca.wiki.store.write import Writer
    dest = Path(project).parent / "extract-out"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir()
    src = DB(Config.for_vault(Config.for_project(str(project)).vault_dir))
    out_cfg = Config.for_vault(dest)
    out = DB(out_cfg)
    try:
        out.pour()
        extract._copy_public(src, Writer(out, Ledger(out_cfg.ledger_path, vault_dir=out_cfg.vault_dir),
                                         clock=lambda: "2026-03-02T00:00:00+00:00"))
    finally:
        out.close()
        src.close()
    text = "\n".join(p.read_bytes().decode("utf-8", errors="replace") for p in dest.rglob("*") if p.is_file())
    shutil.rmtree(dest, ignore_errors=True)
    return text


def snapshot(project, world):
    """The store: active blocks per doc, versions, exclusions, edges, claims, candidates, servable refs."""
    from alpaca.wiki.servable import servable_ref
    from alpaca.wiki.store.asof import AsOf, asof_where_edges
    refs = sorted({r for s in world["sources"].values() for r in s["refs"]})
    with store(project) as conn:
        clause, params = asof_where_edges(AsOf(T=T_AFTER), "edges")
        anchors = {}
        for cid, ref in conn.execute("SELECT candidate_id, source_ref FROM candidate_anchors "
                                     "WHERE role='anchor' AND ordinal>=0"):
            anchors.setdefault(cid, set()).add(ref)
        versions = {}
        for ref, status in conn.execute("SELECT source_ref, status FROM source_versions"):
            versions.setdefault(ref, set()).add(status)
        return {
            "blocks": {d: n for d, n in conn.execute("SELECT doc_id, SUM(status='active') FROM blocks "
                                                     "GROUP BY doc_id")},
            "versions": versions,
            "exclusions": {r[0] for r in conn.execute("SELECT source_ref FROM exclusions")},
            "edges": [dict(r) for r in conn.execute(
                "SELECT edge_id, subj_node, predicate, COALESCE(obj_node, obj_literal) AS obj, status, "
                "source_doc_id FROM edges")],
            "claims": [dict(r) for r in conn.execute("SELECT edge_id, claim_ref, source_ref, status "
                                                     "FROM edge_origin")],
            "candidates": {r[0]: r[1] for r in conn.execute("SELECT candidate_id, status FROM candidates")},
            "anchors": anchors,
            "servable": {r: servable_ref(conn, r) for r in refs},
            "in_op": sorted(r[0] for r in conn.execute(
                "SELECT COALESCE(obj_node, obj_literal) FROM edges WHERE subj_node='t-13' AND "
                "predicate='in_op' AND " + clause, params)),
        }


def served_paths(project, world, kind, k_before):
    """The serve paths of the target and every other source, as text."""
    from alpaca.wiki.config import Config
    t = world["sources"][kind]
    out = {"query": json.dumps(strings(service.query(project, t["question"]), [])),
           "context": service.context(project, t["question"]),
           "pinned": json.dumps(strings(service.query(project, t["question"], knowledge_as_of=k_before), [])),
           "candidates": json.dumps(strings([v.to_dict() for v in service.candidates(project)], [])),
           "extract": extract_rows(project)}
    rows = service.record_facts_preview(project, from_event=1, through_event=max(world["events"]))
    out["dry run"] = json.dumps(strings([vars(r) if hasattr(r, "__dict__") else r._asdict() for r in rows], []))
    out["_dry_visible"] = {r.event_id for r in rows if not r.redacted}
    for key, s in world["sources"].items():
        if key != kind:
            out["context:" + key] = service.context(project, s["question"])
    capture = service.status(project)["capture"]
    out["_withdrawn_events"] = capture["withdrawn_events"]
    out["_uncaptured"] = sorted(set(service._uncaptured_events(project, Config.for_project(project)))
                                & set(world["events"]))
    if t.get("op"):
        out["_scoped"] = service.query(project, "What happened in %s?" % t["op"])["coverage"][
            "missing_event_ids_in_scope"]
    return out


def findings(project, world, kind, base, k_before):
    """What of the target is still served or standing, and what else is no longer (empty when right)."""
    t = world["sources"][kind]
    names, docs = set(t["refs"]), set(t["docs"])
    paths = served_paths(project, world, kind, k_before)
    now = snapshot(project, world)
    bad = []
    for path, text in paths.items():
        if path.startswith("_"):
            continue
        hit = [m for m in t["markers"] if has(text, m)]
        if hit:
            bad.append("%s serves %r" % (path, hit[0]))
    bad += ["dry run shows event %d" % e for e in sorted(paths["_dry_visible"] & set(t["events"]))]
    bad += ["%d active blocks in %s" % (now["blocks"][d], d) for d in sorted(docs) if now["blocks"].get(d)]
    bad += ["version of %s current" % r for r in sorted(names) if "current" in now["versions"].get(r, ())]
    live = {c["edge_id"]: c["claim_ref"] for c in now["claims"] if c["source_ref"] in names and c["status"] == "live"}
    for e in now["edges"]:
        if e["status"] == "active" and (e["edge_id"] in live or e["source_doc_id"] in docs):
            bad.append("edge %s %s %s active (%s)" % (e["subj_node"], e["predicate"], e["obj"],
                                                       live.get(e["edge_id"], e["source_doc_id"])))
    for cid, refs in now["anchors"].items():
        if refs & names and now["candidates"][cid] not in ("stale", "retracted"):
            bad.append("candidate %s on %s is %s" % (cid, sorted(refs & names), now["candidates"][cid]))
    if (paths["_withdrawn_events"] or 0) != len(t["events"]):
        bad.append("status counts %s withdrawn events, not %d" % (paths["_withdrawn_events"], len(t["events"])))
    if paths["_uncaptured"]:
        bad.append("status and the expected set count %s missing" % paths["_uncaptured"])
    if paths.get("_scoped"):
        bad.append("a question scoped to %s has capture gaps %s" % (t["op"], paths["_scoped"]))
    # nothing else is retired
    for key, s in world["sources"].items():
        if key == kind:
            continue
        if not any(has(paths["context:" + key], m) for m in s["markers"]):
            bad.append("context no longer serves %s" % key)
        bad += ["%s (%s) retired" % (d, key) for d in s["docs"] if not now["blocks"].get(d)]
        for ref in s["refs"]:
            if ref in names:
                continue
            if not now["servable"][ref]:
                bad.append("%s (%s) is not servable" % (ref, key))
            if "current" not in now["versions"].get(ref, ()):
                bad.append("version of %s (%s) is not current" % (ref, key))
    bad += ["exclusion names %s" % r for r in sorted(now["exclusions"] - names)]
    tedges = {c["edge_id"] for c in base["claims"] if c["source_ref"] in names}
    tedges |= {e["edge_id"] for e in base["edges"] if e["source_doc_id"] in docs}
    kept = {(e["subj_node"], e["predicate"], e["obj"]) for e in base["edges"]
            if e["status"] == "active" and e["edge_id"] not in tedges}
    active = {(e["subj_node"], e["predicate"], e["obj"]) for e in now["edges"] if e["status"] == "active"}
    bad += ["edge %s %s %s no longer active" % e for e in sorted(kept - active)]
    tcands = {cid for cid, refs in base["anchors"].items() if refs & names}
    bad += ["candidate %s %s -> %s" % (cid, status, now["candidates"].get(cid))
            for cid, status in sorted(base["candidates"].items())
            if cid not in tcands and now["candidates"].get(cid) != status]
    want = ["op-3"] if kind == "contract" else ["op-13"]
    if now["in_op"] != want:
        bad.append("the current op of t-13 is %s, not %s" % (now["in_op"], want))
    return bad


def test_the_world_serves_every_source_before_any_withdrawal(project):
    """The control: before any withdrawal each source is served by its question, the pinned read, the
    candidates view and the extract rows hold what the cases look for, and nothing is missing."""
    world = build_world(project)
    k_now = now_iso()
    for kind, s in world["sources"].items():
        assert any(has(service.context(project, s["question"]), m) for m in s["markers"]), kind
        if kind in dict(CASES):
            pinned = json.dumps(strings(service.query(project, s["question"], knowledge_as_of=k_now), []))
            assert any(has(pinned, m) for m in s["markers"]), kind
    view = json.dumps(strings([v.to_dict() for v in service.candidates(project)], []))
    rows = extract_rows(project)
    for marker in ("Task t-11 is blocked", "Task t-12 is blocked", "Task t-13 is blocked", "Task t-14 is blocked",
                   "zircon", "cobalt", "amber pier"):
        assert has(view, marker), marker
    for marker in ("Tuesday", "marble", "saffron", "cinder", "zircon", "cobalt", "amber pier", "Friday", "slate dock"):
        assert has(rows, marker), marker
    assert service.status(project)["capture"]["missing_events"] == 0
    for kind in ("note", "decision", "contract", "proof", "transcript", "wiki-plain", "wiki-synthetic"):
        assert names_of(world, kind)


@pytest.mark.parametrize("kind,label", CASES, ids=["%s-%s" % c for c in CASES])
def test_a_withdrawal_by_any_name_takes_the_whole_source_and_nothing_else(project, kind, label):
    world = build_world(project)
    names = names_of(world, kind)
    ref = names[label]
    base = snapshot(project, world)
    k_before = now_iso()
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    receipt = service.withdraw_source(project, ref, session="s-owner", reason="alias matrix",
                                      record=appender(project, "s-owner", later))
    assert (receipt.status, receipt.reason) == ("applied", None)
    found = []
    # the same source asked for again by each of its other names, on a copy taken now: a duplicate
    twin = Path(project).parent / "twin"
    shutil.copytree(project, twin, symlinks=True)
    for other_label, other in names.items():
        if other != ref:
            again = service.withdraw_source(str(twin), other, session="s-owner", reason="again",
                                            record=appender(twin, "s-owner", later))
            if again.status != "duplicate":
                found.append("asked again as %s: %s %s" % (other_label, again.status, again.reason))
    shutil.rmtree(twin)
    found += ["withdrawn: " + f for f in findings(project, world, kind, base, k_before)]
    # the next captures: the Stop hook, a SessionEnd-style capture with transcripts, the documents again
    service.capture(project, sessions=list(SESSIONS), session_events=True, uncaptured_only=True,
                    transcripts=False, deadline_s=None)
    full_capture(project)
    ingest_plain(project)
    ingest_synthetic(project)
    if kind == "wiki-plain":
        ingest_plain(project, HERON + "The cobalt shed is painted twice a year.\n")
    if kind == "wiki-synthetic":
        ingest_synthetic(project, TERN + "The amber pier lamp is lit at dusk.\n")
    found += ["recapture: " + f for f in findings(project, world, kind, base, k_before)]
    # wiki recover into a fresh store; the documents come back by their own paths (no record event)
    shutil.rmtree(Path(project, ".alpaca", "wiki"))
    report = recovery.run(project, session="s-recover")
    assert report["status"] == "ok" and report["candidates"]["source_withdrawals_applied"] == 1
    ingest_plain(project)
    ingest_synthetic(project)
    found += ["recover: " + f for f in findings(project, world, kind, base, k_before)]
    assert not found, "\n".join(found)
