"""Explicit, idempotent replay of every recorded session to a fixed event cutoff.

This calls the normal capture/write path. It never deletes records, invents graph
assertions, or upgrades profile acceptance. A success event is appended only after
source notes, indexed documents, blocks, database integrity, and the ledger agree.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from alpaca import db
from . import withdrawal
from .config import Config
from .determinism import sha256_hex
from .ingest import drain
from .ingest.absorb import Absorber
from .ingest.segment import segment
from .servable import excluded_source_refs


def verify_documents(vault, conn, documents):
    """Verify exact raw bytes and active searchable blocks for each captured source."""
    for doc_id, text in documents:
        raw = Path(vault, doc_id)
        row = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        expected = [(b['ordinal'], b['text'], b['block_sha256']) for b in segment(doc_id, text)]
        actual = [tuple(r) for r in conn.execute(
            "SELECT ordinal,text,block_sha256 FROM blocks WHERE doc_id=? AND status='active' "
            "ORDER BY ordinal", (doc_id,))]
        if (not raw.is_file() or raw.read_bytes() != text.encode('utf-8')
                or row is None or row[0] != sha256_hex(text) or actual != expected):
            raise RuntimeError(f"wiki recovery verification failed: {doc_id}")
    return len(documents)


def verify_events(vault, conn, events, rerender_notes=False):
    """Verify both the raw bytes and the searchable projection for source events. A note the
    capture kept in an earlier rendering (no `rerender_notes`) is verified as it is stored."""
    prefixes = drain.observed_prefixes(str(Path(vault).resolve().parent.parent))
    # A document the marking scanner trips on lives under wiki/private/; the scanner reads the note
    # before the screen, as the drain placed it (drain.note_placement). A note whose source the
    # store holds private stays there, and a changed rendering lands at its `-render-<sha>` revision
    # (merge of op-008 into main, 2026-10-06): `drain.stored_note` finds it by capture's own rule.
    previous = drain._previous_projections(conn, [], [], full=True) if events else {}
    return verify_documents(vault, conn, [
        drain.stored_note(conn, vault, event, prefixes=prefixes, previous=previous,
                          rerender_notes=rerender_notes)
        for event in events])


ACT_KINDS = ("wiki-candidate", "wiki-admission", "wiki-candidate-withdrawn", "wiki-withdrawal",
             "wiki-correction")
#: Verdicts that differ only by the order two sources reached the store (architecture.md 10.2).
_DRIFT = {"novel", "matches", "duplicate"}


def _bump(table: dict, key: str) -> None:
    table[key] = table.get(key, 0) + 1


def replay_candidates(root: str, *, absorber, cutoff: int | None = None,
                      record_time: bool = False) -> dict:
    """Replay the candidate acts of the record in record id order (architecture.md 10.2), capturing
    every event before an act first, so each act meets the store it met live. The `Writer` clock is
    the act's `ts`, and with `record_time` (a rebuild) each captured event lands at its own `ts` too
    (`drain.capture`, OB-19). Before an act runs, record-only checks: the actor is `alpaca-wiki`; an admission
    or a withdrawal has an earlier `wiki-candidate` for the id (its row exists); then the one
    transition table the live doors ask (`lifecycle.admit`, `lifecycle.withdraw`, t-070 RC3) on the
    row as the replay holds it: a job never admits, the admitting session is not the row's proposer
    (the session of the event that wrote the row, never a later event's), only a `checked` row is
    admitted, the withdrawing session is the proposer or the event declares the owner. A failing
    event is skipped and counted under the reason the live door would have given (consistency, not
    authentication).

    * `wiki-candidate` writes its row from the recorded fields and runs no check again; a `duplicate`
      event, or an id that already has a row other than a `refused` one, writes none.
    * `wiki-admission` (result `applied`) re-applies when the verdict and the prior's identity still
      match. `novel` against `matches` or `duplicate` is applied under the verdict now found and
      reported as `order_drift`; any other difference, an anchor or prior that no longer resolves,
      marks the candidate `stale`.
    * `wiki-candidate-withdrawn` undoes the recorded net effect (`withdraw_candidate_effects`).

    Every capture renders from record events at or below the act it comes before (`drain.capture`
    `through`, t-070 round 2 C-B), as the live door met them, and a candidate row's operator is the
    one its session declared at or before its own act; the session loop of `replay` then renders at
    the cutoff, and a declaration captured late re-renders the notes it names. A transcript withdrawal lands the transcript version it names
    (`source_bytes`, `source_sha256`; B-4) and applies the act in one store transaction, so a crash
    leaves neither; when that version is gone nothing of the transcript lands, the act still
    applies, and `transcript_versions_unavailable` names it (the replay reports `incomplete`). After
    each content-removing act, applied or skipped, the store's watermark (`DB.REMOVAL_ACTS_KEY`)
    moves to it in the act's own transaction, so the serving doors know what the store has
    processed (C-E).

    Nothing here runs a check, reads the network or calls a model."""
    from . import faults, intake, lifecycle
    from .clock import to_clock_format
    import json
    report = {"acts": 0, "candidates_written": 0, "admissions_applied": 0, "withdrawals_applied": 0,
              "source_withdrawals_applied": 0, "corrections_applied": 0,
              "skipped": {}, "order_drift": [], "stale": [], "transcript_versions_unavailable": [],
              "transcript_versions_legacy": []}
    source = db.connect_readonly(str(root))
    try:
        head = drain.record_head(source)
        through = head if cutoff is None else min(int(cutoff), head)
        acts = [dict(r) for r in source.execute(
            "SELECT * FROM events WHERE kind IN (%s) AND id<=? ORDER BY id"
            % ",".join("?" * len(ACT_KINDS)), ACT_KINDS + (through,))]
        if not acts:
            return report
        writer, store = absorber.writer, absorber.db
        landed = 0
        for act in acts:
            report["acts"] += 1
            before = [dict(r) for r in source.execute(
                "SELECT * FROM events WHERE id>? AND id<? ORDER BY id", (landed, act["id"]))]
            landed = act["id"] - 1
            data = json.loads(act["data"]) if isinstance(act["data"], str) else (act["data"] or {})
            if act["kind"] == "wiki-correction" and drain.renders_as_source(act["kind"], data):
                # an applied owner correction is a source: its note lands with the events before it,
                # because the replacement is anchored on it (architecture.md 10.2)
                before.append(dict(act))
                landed = act["id"]
            if before:
                # rendered from the record at or below the act, the head the live door read before
                # it appended the act: a note withdrawn before its session declared an operator stays
                # as it was withdrawn, and a later declaration re-renders the others (`drain.capture`)
                drain.capture(str(root), before, absorber=absorber, record_time=record_time,
                              through=act["id"])
            faults.boundary("replay-act")
            talk = str(data.get("source_ref") or "")
            if act["kind"] == "wiki-withdrawal" and talk.startswith("transcript:"):
                # The transcript a withdrawal names lands before it, as it had live: the live door
                # refuses a transcript the store does not hold (`unknown-source`), and the session
                # loop of `replay` lands transcripts only after every act, when this one is excluded
                # and lands no version, so the rebuild and the import lacked the withdrawn source's
                # row (t-070 s3-e2e). It lands as the version the act names, never the file as it is
                # now (B-4), and in the act's own transaction (C-E). Its session's events before the
                # act give its record time; their notes are held already and land nothing again.
                _replay_transcript_withdrawal(report, root, absorber, source, act, data,
                                              talk[len("transcript:"):], act["id"], record_time)
                continue
            cid = data.get("candidate_id")
            if act["actor"] != "alpaca-wiki":
                _bump(report["skipped"], "actor")
                if act["kind"] in REMOVAL_KINDS:
                    with writer.transaction():
                        writer.advance_removal_watermark(act["id"])
                continue
            if act["kind"] in ("wiki-withdrawal", "wiki-correction"):
                _replay_source_act(report, writer, store, source, act, data)
                continue
            stamp = to_clock_format(act["ts"])
            lent, writer.clock = writer.clock, (lambda: stamp)
            try:
                if act["kind"] == "wiki-candidate":
                    # the operator the proposing session had declared when the live door read the
                    # record, before it appended this act (C-B, B-3)
                    made = intake.rows_from_event(
                        data, proposer=act["session"],
                        operator=drain.session_operator(source, act["session"], act["id"]),
                        event_id=act["id"], ts=act["ts"])
                    if made is not None:
                        with writer.transaction():
                            if writer.write_candidate(*made):
                                report["candidates_written"] += 1
                    continue
                # The proposer, the status and who may act are the row's and the table's
                # (lifecycle.py): the rule the live door asked, never a later event's session
                # (t-066 adv-r1-m2, adv-r1-m3; t-070 RC3). A row exists only once an earlier
                # `wiki-candidate` wrote it. The row is read, and the act decided and applied, in one
                # transaction (RC1): a `wiki recover` may run beside a live door.
                if act["kind"] == "wiki-admission" and data.get("result") != "applied":
                    _bump(report["skipped"], "not-applied")
                    continue
                with writer.transaction():
                    if act["kind"] == "wiki-candidate-withdrawn":
                        # processed here, applied or skipped, in this transaction (C-E)
                        writer.advance_removal_watermark(act["id"])
                    row = store.conn.execute("SELECT * FROM candidates WHERE candidate_id=?",
                                             (cid,)).fetchone()
                    if row is None:
                        _bump(report["skipped"], "no-earlier-candidate")
                        continue
                    if act["kind"] == "wiki-admission":
                        decided = lifecycle.admit(row, session=act["session"])
                        if not decided.go:
                            _bump(report["skipped"], decided.reason)
                            continue
                        _replay_admission(report, writer, store, row, act, data)
                        continue
                    decided = lifecycle.withdraw(row, session=act["session"], by=data.get("by"))
                    if not decided.go:
                        _bump(report["skipped"], decided.reason)
                        continue
                    prior = None
                    if decided.to == "retracted":
                        found = source.execute("SELECT data FROM events WHERE id=?",
                                               (row["admission_event_id"],)).fetchone()
                        prior = json.loads(found["data"]) if found else None
                        writer.withdraw_candidate_effects(cid, prior)
                        withdrawal.undo_derived(store, prior)
                    writer.set_candidate_status(cid, decided.to)
                    report["withdrawals_applied"] += 1
            finally:
                writer.clock = lent
    finally:
        source.close()
    return report


#: The record act kinds that take content out of what the store serves (`Writer.REMOVAL_ACT_KINDS`).
REMOVAL_KINDS = ("wiki-withdrawal", "wiki-candidate-withdrawn", "wiki-correction")


@contextmanager
def _act_transaction(writer):
    """The act's store transaction: the caller's open one when there is one (a transcript withdrawal
    lands its version and applies the act in one commit), else a new one."""
    if writer._tx_active:
        yield writer
    else:
        with writer.transaction():
            yield writer


def _snapshot_versions_at(source, through: int) -> dict:
    """The transcript version the newest `transcript-snapshot` of each session at or below
    `through` names (t-417's part of t-430, now the best-effort reading of a session no
    `transcript-version` event places at the cutoff): `{session: alternatives}` (the registered file
    cut at the snapshot's `source_bytes` and `source_sha256`, the bytes the hooks captured beside
    the snapshot, then the snapshot's copy cut at its `bytes` and `sha256`), and `{session: False}`
    for a session whose snapshots all come after the cutoff. A session with no snapshot is left out."""
    import json
    named, later = {}, set()
    for row in source.execute("SELECT id, session, data FROM events WHERE kind='transcript-snapshot' "
                              "ORDER BY id"):
        if row[0] > through:
            later.add(row[1])
            continue
        try:
            data = json.loads(row[2]) if isinstance(row[2], str) else (row[2] or {})
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        alternatives = []
        if data.get("source_sha256") and isinstance(data.get("source_bytes"), int):
            alternatives.append((None, int(data["source_bytes"]), str(data["source_sha256"])))
        if data.get("sha256") and isinstance(data.get("bytes"), int) and data.get("path"):
            alternatives.append((str(data["path"]), int(data["bytes"]), str(data["sha256"])))
        if alternatives:
            named[row[1]] = alternatives
    out = {session: False for session in later if session not in named}
    out.update(named)
    return out


def _transcript_versions_at(root, source, through: int) -> tuple:
    """`(versions, how)`: the transcript version a replay at a cutoff before the record head reads
    for each session (t-430, owner decision dec-f812470497fb), from the `transcript-version` events
    the captures append (`drain._record_transcript_version`).

    Exact, from the record alone: `recorded`, the version the session's newest version event at or
    below `through` names (the transcript's own resolved file cut at its `source_bytes` and
    `source_sha256`, then the project's retained copy cut the same way: whichever the capture read);
    `recorded-after-cutoff`, False (none lands), when the session's first version event comes later
    and says the store held no version of it before (`held_before` false).

    Best-effort, for a session the record cannot place at the cutoff (captured before t-430, or a
    first recorded landing that replaced a version no event names, or a rebuild that could not say):
    `snapshot` or `snapshot-after-cutoff` (`_snapshot_versions_at`), else
    `first-recorded-version`, the version its first event names (the nearest the record has, and
    the same however the file grows later); a session in none of these is read as its file is now
    (`current-file`, left out of `versions`)."""
    import json
    from alpaca import transcripts
    recorded, first = {}, {}
    for row in source.execute("SELECT id, ref, data FROM events WHERE kind=? ORDER BY id",
                              (drain.TRANSCRIPT_VERSION_KIND,)):
        ref = row[1] if isinstance(row[1], str) else ""
        if not ref.startswith("transcript:"):
            continue
        try:
            data = json.loads(row[2]) if isinstance(row[2], str) else (row[2] or {})
        except ValueError:
            continue
        if not (isinstance(data, dict) and isinstance(data.get("source_bytes"), int)
                and isinstance(data.get("source_sha256"), str)):
            continue
        sid = ref[len("transcript:"):]
        first.setdefault(sid, data)
        if row[0] <= through:
            recorded[sid] = data

    def cut(data, sid):
        size, sha = int(data["source_bytes"]), str(data["source_sha256"])
        return [(None, size, sha), (transcripts.local_path(str(root), sid), size, sha)]

    versions, how = {}, {}
    for sid, data in recorded.items():
        versions[sid], how[sid] = cut(data, sid), "recorded"
    for sid, data in first.items():
        if sid not in how and data.get("held_before") is False:
            versions[sid], how[sid] = False, "recorded-after-cutoff"
    for sid, alternatives in _snapshot_versions_at(source, through).items():
        if sid not in how:
            versions[sid] = alternatives
            how[sid] = "snapshot" if alternatives else "snapshot-after-cutoff"
    for sid, data in first.items():
        if sid not in how:
            versions[sid], how[sid] = cut(data, sid), "first-recorded-version"
    return versions, how


def _withdrawn_version(source, act: dict, data: dict, sid: str):
    """`(version, how)`: the transcript version a withdrawal names, as `(source_bytes,
    source_sha256)`. `recorded` when the act holds it (B-4, every withdrawal since t-070 round 2);
    for an older act, the newest `transcript-snapshot` of the session at or below the act
    (`snapshot`), else `(None, "current-file")`: the file as it is now, the old behaviour."""
    import json
    if data.get("source_sha256") and data.get("source_bytes") is not None:
        return (int(data["source_bytes"]), str(data["source_sha256"])), "recorded"
    row = source.execute("SELECT data FROM events WHERE session=? AND kind='transcript-snapshot' "
                         "AND id<? ORDER BY id DESC LIMIT 1", (sid, act["id"])).fetchone()
    if row is not None:
        try:
            snap = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
        except ValueError:
            snap = {}
        if isinstance(snap, dict) and snap.get("sha256") and snap.get("bytes") is not None:
            return (int(snap["bytes"]), str(snap["sha256"])), "snapshot"
    return None, "current-file"


def _replay_transcript_withdrawal(report: dict, root, absorber, source, act: dict, data: dict,
                                  sid: str, through: int, record_time: bool) -> None:
    """Land the transcript version a `wiki-withdrawal` names and replay the act, in one store
    transaction (see `replay_candidates`)."""
    from . import faults
    writer = absorber.writer
    version, how = _withdrawn_version(source, act, data, sid)
    if how != "recorded":
        report["transcript_versions_legacy"].append({"event": act["id"], "session": sid, "how": how})
    held = [dict(r) for r in source.execute(
        "SELECT * FROM events WHERE session=? AND id<? ORDER BY id", (sid, act["id"]))]
    try:
        with writer.transaction():
            with absorber.joined():
                out = drain.capture(str(root), held, sessions=[sid], absorber=absorber,
                                    record_time=record_time, through=through,
                                    transcript_versions={sid: version} if version else None)
            faults.boundary("replay-act-landed")
            for entry in out.get("missing_transcripts") or ():
                if entry.get("session") == sid and version is not None:
                    report["transcript_versions_unavailable"].append({
                        "event": act["id"], "session": sid,
                        "reason": entry.get("reason") or "transcript-unavailable",
                        "source_bytes": version[0], "source_sha256": version[1]})
            if act["actor"] != "alpaca-wiki":
                _bump(report["skipped"], "actor")
                writer.advance_removal_watermark(act["id"])
            else:
                _replay_source_act(report, writer, absorber.db, source, act, data)
    except BaseException:
        absorber._reload_echo_index()
        raise


def _replay_source_act(report: dict, writer, store, source, act: dict, data: dict) -> None:
    """Replay a `wiki-withdrawal` or an applied owner `wiki-correction` (architecture.md 10.2): the
    same code the live verb ran, with the `Writer` clock at the act's `ts`. A withdrawal of a source
    that already has an `exclusions` row is skipped. A correction is resolved by the identity it
    recorded and written `stale` when the store no longer holds one such edge; a refused correction
    changed nothing and is skipped. Each act moves the store's removal watermark in its own
    transaction (or the caller's open one), applied or skipped (t-070 round 2 C-E)."""
    import json
    from .clock import to_clock_format

    def admission_of(event_id):
        found = source.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()
        return json.loads(found["data"]) if found else None

    stamp = to_clock_format(act["ts"])
    lent, writer.clock = writer.clock, (lambda: stamp)
    try:
        if act["kind"] == "wiki-withdrawal":
            ref = data.get("source_ref")
            with _act_transaction(writer):       # read and act under one lock (t-070 RC1)
                writer.advance_removal_watermark(act["id"])
                if not ref or store.conn.execute("SELECT 1 FROM exclusions WHERE source_ref=?",
                                                 (ref,)).fetchone():
                    _bump(report["skipped"], "already-withdrawn")
                    return
                withdrawal.withdraw(writer, ref, reason=data.get("reason") or "", record_event_id=act["id"],
                                    requested_by=act["session"], admission_of=admission_of)
            report["source_withdrawals_applied"] += 1
            return
        if data.get("result") != "applied" or data.get("actor") != "owner":
            _bump(report["skipped"], "not-applied")
            if data.get("result") == "applied":       # a removal act the gate counts (C-E)
                with _act_transaction(writer):
                    writer.advance_removal_watermark(act["id"])
            return
        with _act_transaction(writer):
            writer.advance_removal_watermark(act["id"])
            # One act of the record that cannot be written must not stop the replay of the rest, and
            # the record cannot be edited: the act is kept as a `stale` correction with the reason.
            store.conn.execute("SAVEPOINT correction")
            try:
                out = writer.apply_correction(
                    data["target"], data["action"], data.get("replacement"), data.get("effective_at"),
                    reason=data.get("reason") or "", requested_by=act["session"],
                    record_event_id=act["id"], note_ref="event:%d" % act["id"],
                    statement=data.get("statement"))
                store.conn.execute("RELEASE correction")
            except Exception as exc:                 # noqa: BLE001 (any write failure of one act)
                store.conn.execute("ROLLBACK TO correction")
                store.conn.execute("RELEASE correction")
                why = "apply-failed:%s" % type(exc).__name__
                cid = writer.record_stale_correction(
                    data["target"], data["action"], data.get("replacement"), data.get("effective_at"),
                    reason=data.get("reason") or "", requested_by=act["session"],
                    record_event_id=act["id"])
                out = {"result": "stale", "reason": why, "correction_id": cid}
        if out["result"] == "applied":
            report["corrections_applied"] += 1
        elif out["result"] == "stale":
            report["stale"].append({"correction_id": out["correction_id"], "event": act["id"],
                                    "reason": out["reason"]})
        else:
            _bump(report["skipped"], "already-applied")
    finally:
        writer.clock = lent


def _replay_admission(report: dict, writer, store, row, act: dict, data: dict) -> None:
    """Re-apply one recorded admission (see `replay_candidates`) in the caller's transaction, which
    read the row and asked `lifecycle.admit`."""
    import json
    from . import intake, lifecycle
    from .api import FactBody
    cid = row["candidate_id"]

    def stale(reason: str) -> None:
        writer.set_candidate_status(cid, lifecycle.replay_stale(row).to)
        report["stale"].append({"candidate_id": cid, "event": act["id"], "reason": reason})

    if row["kind"] == "observation":
        return _replay_observation(report, writer, store, row, act, data, stale)
    edge = data.get("edge") or {}
    anchor = edge.get("anchor") or {}
    block = intake.resolve_anchor(store, anchor.get("source_ref", ""), anchor.get("quote", ""))
    if block is None or row["body_json"] is None:
        return stale("anchor-unresolved")
    f = json.loads(row["body_json"])["fact"]
    fact = FactBody(f["subject"], f["predicate"], f["obj"], f["obj_datatype"], f.get("valid_from"))
    plan = writer.plan_candidate_edge(fact, block, anchor["quote"])
    if plan["refused"]:
        return stale(plan["refused"])
    recorded, found = data.get("verdict"), plan["verdict"]
    drift = recorded != found and recorded in _DRIFT and found in _DRIFT and "novel" in (recorded, found)
    if recorded != found and not drift:
        return stale("verdict-changed")
    if found == "contradicts":
        ident = (edge.get("prior") or {}).get("identity")
        if ident is None or intake.resolve_fact_identity(store, ident) != [plan["prior_edge_id"]]:
            return stale("prior-unresolved")
    out = writer.apply_candidate_edge(
        plan, candidate_id=cid, anchor_ref=anchor["source_ref"], quote=anchor["quote"],
        fact_kind=(edge.get("claim") or {}).get("fact_kind", "derived-hypothesis"), block=block)
    writer.set_candidate_status(cid, "admitted", admission_ref=out["edge_id"],
                                admission_effect=out["effect"], admission_event_id=act["id"])
    report["admissions_applied"] += 1
    if drift:
        report["order_drift"].append({"candidate_id": cid, "event": act["id"],
                                      "recorded": recorded, "found": found})


def _replay_observation(report: dict, writer, store, row, act: dict, data: dict, stale) -> None:
    """Re-apply an admitted observation, in the caller's transaction: the lesson block or the prior
    row the admission recorded is
    written back as it was stamped then, without the probe corpus or the provenance file the gate used
    live (architecture.md 10.2). The first anchor must still resolve; otherwise the candidate is
    stale. The gate runs once, live, and its verdict is the record."""
    from alpaca.knowledge import KnowledgeStore
    from alpaca.lessons import LessonsStore
    from . import intake
    cid = row["candidate_id"]
    first = store.conn.execute(
        "SELECT source_ref,quote FROM candidate_anchors WHERE candidate_id=? AND role='anchor' "
        "AND ordinal=0", (cid,)).fetchone()
    lesson, prior = data.get("lesson"), data.get("knowledge")
    if (first is None or first["quote"] is None or not (lesson or prior)
            or intake.resolve_anchor(store, first["source_ref"], first["quote"]) is None):
        return stale("anchor-unresolved")
    if lesson:
        LessonsStore(store.conn).replay_admitted(lesson["block"], run_id=cid)
        ref = lesson["id"]
    else:
        KnowledgeStore(store.conn).replay_admitted(prior)
        ref = prior["claim_id"]
    writer.set_candidate_status(cid, "admitted", admission_ref=ref,
                                admission_effect=data.get("effect"), admission_event_id=act["id"])
    report["admissions_applied"] += 1


def store_is_empty(cfg) -> bool:
    """True when the project's store holds no document and no accepted row yet (or does not exist):
    what `wiki recover` builds then is a rebuild from the record, not a catch-up of a live store. A
    ledger left from an earlier store (`ledger/events.jsonl` with content) counts as something held:
    the Writer refuses a new store behind it. So does a store an import has claimed (t-070)."""
    from .store.db import holds_something, open_db_readonly
    if cfg.ledger_path.exists() and cfg.ledger_path.stat().st_size > 0:
        return False
    if not cfg.db_path.exists():
        return True
    conn = open_db_readonly(cfg)
    try:
        return not holds_something(conn)
    finally:
        conn.close()


def run(root: str, *, session: str, progress=None, rerender_notes: bool = False) -> dict:
    """`wiki recover`: `replay` through the record's last event, then the `wiki-recovered` (or
    `wiki-recovery-incomplete`) record event with the report. `rerender_notes` is the owner's
    re-render job (`wiki recover --rerender-notes`, see `replay`); `wiki context/query --refresh`
    never pass it. Each transcript version the replay lands that the record does not name yet gets
    its `transcript-version` event first, as a live capture's does (t-430)."""
    report = replay(root, progress=progress, rerender_notes=rerender_notes, record_versions=True)
    source = db.connect(str(root))
    try:
        event = db.append_event(source, session=session, actor='alpaca-wiki', kind=('wiki-recovered' if report['status'] == 'ok'
                                      else 'wiki-recovery-incomplete'), data=report)
        report['recovery_event'] = event['id']
    finally:
        source.close()
    return report


def replay(root: str, *, cutoff: int | None = None, progress=None,
           rebuild: bool | None = None, rerender_notes: bool = False,
           record_versions: bool = False) -> dict:
    """Replay every recorded session through `cutoff` (default the record's last event) into the
    project's store and verify it, writing nothing to the record (but see `record_versions`).

    Restart and catch-up (FR-18): on a live store the replay lands what capture missed and re-applies
    a record act whose store commit was lost (`replay_candidates`), a proposal of the consolidation
    job included, so the job's next step, which starts at its checkpoint, proposes none of them
    again; `jobs` reports where each job stands (`consolidate.queue_status`). On an empty store it is
    a rebuild: every source lands at the `ts` of its record event, and the store records in `meta`
    that its knowledge time comes from the record (OB-19, `knowledge_time` of the report). `rebuild`
    None decides by `store_is_empty`; an import, which lands its documents first, passes True.

    An event note the store already holds in another rendering of the same event (a note format
    change, the secret screen) is kept as stored and counted in `kept_renderings`; with
    `rerender_notes` it is rewritten in place, the owner's job (merge of op-008 into main, review
    B4: on the live store that is about 109,000 notes, hours of write-lock churn, so it is timed on
    a copy first). New events and new notes land either way.

    At a cutoff before the record head (an import of an older export, a rebuild at an earlier
    cutoff), each transcript is read at the version the record names there (t-430, owner decision
    dec-f812470497fb, `_transcript_versions_at`): the newest `transcript-version` event at or below
    the cutoff, which every capture appends for the version it lands; a session whose first such
    event comes later, with no version held before it, lands no transcript. A session the record
    cannot place at the cutoff (captured before t-430) is read best-effort, and the report's
    `transcript_cutoff` names each session: `recorded`, `recorded_after_cutoff`, and `best_effort`
    ({session, how}: `snapshot`, `snapshot-after-cutoff`, `first-recorded-version`, `current-file`).
    At the head every transcript is read as its file is now, as before.

    The replay writes nothing to the record, unless `record_versions` (only `run`, `wiki recover`):
    then each transcript version it lands that the record does not name yet gets its
    `transcript-version` event, as a live capture's does (`drain.capture`), counted in
    `transcript_versions_recorded`."""
    from . import consolidate
    source = db.connect(str(root))
    try:
        ok, why = db.verify_chain(source)
        if not ok:
            raise RuntimeError(f"source record verification failed: {why}")
        # One SELECT freezes the inventory; later arrivals are left for the next drain. Every note is
        # rendered from the record at or below the cutoff (t-070 round 2 C-B, B-3).
        head = drain.record_head(source)
        through = head if cutoff is None else min(int(cutoff), head)
        events = drain.normalize_events(source, [dict(row) for row in source.execute(
            "SELECT * FROM events WHERE id<=? ORDER BY id", (through,))], through)
        registered = [row[0] for row in source.execute("SELECT sid FROM sessions WHERE transcript IS NOT NULL AND transcript<>''")]
        registered += [row[0] for row in source.execute("SELECT DISTINCT session FROM obs_source WHERE kind='transcript' AND locator IS NOT NULL")]
        # t-430: before the record head, the transcript versions the record names at the cutoff
        versions, how = _transcript_versions_at(root, source, through) if through < head else (None, {})
    finally:
        source.close()
    cutoff = max((event['id'] for event in events), default=0)
    sessions = list(dict.fromkeys([event['session'] for event in events] + registered))
    cfg = Config.for_project(root)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    if rebuild is None:
        rebuild = store_is_empty(cfg)
    absorber = Absorber(cfg)
    report = {'status': 'ok', 'through_event': cutoff, 'sessions': len(sessions),
              'notes': 0, 'ingested': 0, 'skipped': 0, 'kept_renderings': 0,
              'rerender_notes': bool(rerender_notes),
              'knowledge_time': 'record' if rebuild else 'capture'}
    try:
        if rebuild:
            # flagged first, so a rebuild cut short is flagged too (OB-19)
            with absorber.writer.transaction():
                absorber.writer.mark_knowledge_time_from_record()
        elif absorber.db.get_meta(absorber.db.KNOWLEDGE_TIME_KEY) == 'record':
            report['knowledge_time'] = 'record'
        # Candidate acts replay in record id order, each after the events before it (ARCH 10.2); the
        # session loop below then lands what is left (later events, transcripts).
        candidates = replay_candidates(root, absorber=absorber, cutoff=cutoff, record_time=rebuild)
        if candidates['acts']:
            report['candidates'] = candidates
        if candidates['transcript_versions_unavailable']:
            # a withdrawn transcript version that is gone lands nothing, never another version (B-4)
            report['status'] = 'incomplete'
            report['transcript_versions_unavailable'] = candidates['transcript_versions_unavailable']
        report['verified_transcripts'] = 0
        report['transcript_sources'] = []
        report['missing_transcripts'] = []
        # A withdrawn transcript keeps its projection file (nothing is deleted) and no active block
        # (architecture.md 10.2 step 3), so only its raw bytes are verified, as for a withdrawn event.
        # What is withdrawn is the set the servable predicate and the absorb skip read.
        withdrawn_sources = {r for r in excluded_source_refs(absorber.db) if r.startswith('transcript:')}
        report['withdrawn_transcripts'] = []
        if versions is not None:
            report['transcript_cutoff'] = {'recorded': [], 'recorded_after_cutoff': [],
                                           'best_effort': []}
        if record_versions:
            report['transcript_versions_recorded'] = 0
        for index, sid in enumerate(sessions, 1):
            version = None if versions is None else versions.get(sid)
            result = drain.run(str(root), sid, through_event=cutoff, absorber=absorber,
                               record_time=rebuild, rerender_notes=rerender_notes,
                               transcript_version=version, record_versions=record_versions)
            if record_versions:
                report['transcript_versions_recorded'] += result.get('versions_recorded', 0)
            if versions is not None:
                read = how.get(sid, 'current-file')
                if read in ('recorded', 'recorded-after-cutoff'):
                    report['transcript_cutoff'][read.replace('-', '_')].append(sid)
                elif (read == 'snapshot-after-cutoff' or result['transcripts']
                      or result.get('missing_transcripts')):
                    # a session with a transcript to read that the record cannot place at the cutoff
                    report['transcript_cutoff']['best_effort'].append({'session': sid, 'how': read})
            # A transcript that is missing no longer stops the event notes landing (FR-8).
            for entry in result.get('missing_transcripts', ()):
                report['missing_transcripts'].append(entry)
                report['status'] = 'incomplete'
            for key in ('notes', 'ingested', 'skipped', 'kept_renderings'):
                report[key] += result[key]
            for transcript in result['transcripts']:
                if transcript.get('excluded'):
                    # a withdrawn transcript lands no new version and no vault bytes (C-E, A-6)
                    report['withdrawn_transcripts'].append(transcript['session'])
                    report['transcript_sources'].append(transcript)
                    continue
                # Verify the captured source cutoff, never reread a growing native
                # transcript. The manifest hash was computed before publication.
                raw = cfg.vault_dir / transcript['doc_id']
                if not raw.is_file():
                    raise RuntimeError("wiki recovery transcript verification failed: missing projection")
                text = raw.read_bytes().decode('utf-8')
                if sha256_hex(text) != transcript['content_sha256']:
                    raise RuntimeError("wiki recovery transcript verification failed: changed projection")
                if 'transcript:%s' % transcript['session'] in withdrawn_sources:
                    report['withdrawn_transcripts'].append(transcript['session'])
                else:
                    report['verified_transcripts'] += verify_documents(
                        cfg.vault_dir, absorber.db.conn, [(transcript['doc_id'], text)])
                report['transcript_sources'].append(transcript)
            if progress is not None:
                progress(index, len(sessions), report.copy())
        # OB-20: a block the schema-3 retire_block closed keeps `valid_until = superseded_at`, which
        # hides it from a read at an earlier knowledge time. Cleared once, here, never in a hook.
        with absorber.writer.transaction():
            report['legacy_block_validity_repaired'] = absorber.writer.clear_legacy_block_valid_until()
        # the records Rune2 appends about its own work (candidates, admissions) are not sources
        # and a withdrawn event keeps no active block to verify (architecture.md 10.2 step 3): one
        # an exclusions row names, and one that fell with a withdrawn source copy made from it,
        # which the absorb skip does not land again (check round 4 invariants B1)
        withdrawn = {r for r in excluded_source_refs(absorber.db) if r.startswith('event:')}
        report['verified_events'] = verify_events(cfg.vault_dir, absorber.db.conn, [
            e for e in events if drain.renders_as_source(e.get('kind'), e.get('data'))
            and 'event:%d' % e['id'] not in withdrawn], rerender_notes=rerender_notes)
        if [row[0] for row in absorber.db.conn.execute('PRAGMA integrity_check')] != ['ok']:
            raise RuntimeError('wiki database integrity verification failed')
        if absorber.db.conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('wiki database foreign key verification failed')
        absorber.writer.sync_ledger()
    finally:
        absorber.close()
    report['jobs'] = {consolidate.CONSUMER: consolidate.queue_status(root)}
    return report
