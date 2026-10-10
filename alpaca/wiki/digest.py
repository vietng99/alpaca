"""accepted_digest: one hash of the accepted state of a store (architecture.md 10.3; interfaces.py.txt
section 4).

The digest covers identity and record-derived fields only, rows sorted by them, so two stores built
from one record under different provider configurations (A: narrow, identity, structural; B: hybrid,
lexical, ensemble) or a store and its rebuild compare equal. It leaves out capture-clock columns,
block, doc and edge ids, replaced source versions, gate refusal rows, sort_assertion, indexes,
answers and answer_outcomes. A missing table counts as empty. A pure read: it writes nothing.

Unknown valid time. An edge whose source gives no date gets the Writer clock as its valid_from
(store/write.py upsert_edge: `valid_from = now`, the same value as its recorded_at), the capture-time
proxy of architecture.md 5.1. That value is recording time, not validity time, so it is hashed as
None (unknown start). A valid_until copied from such a proxy (ingest/reconcile.py sets a candidate's
valid_until to a later edge's valid_from) is hashed as UNKNOWN_END, which keeps "ended at an unknown
time" apart from an open window (None). An edge is a proxy when its valid_from equals its
recorded_at and no record rule wrote it, or when it is an agent fact (extractor `llm`) with a null
source_created_at: that fact takes its anchor block's capture time as valid_from (architecture.md
5.1), which a rebuild captures again at another instant. A dated fact keeps its own valid_from and
valid_until. A record rule (an edge with a `record-rule` claim, an owner correction's replacement
included) always gives the event's `ts` as valid_from, and a rebuild lands the event at that same
`ts` (OB-19), so there the two columns are equal without the value being a proxy.
"""
from __future__ import annotations

import json

from .determinism import canonical_json, sha256_hex
from .servable import source_refs_for_docs

_LESSON_EVENTS = ("lesson-admit", "lesson-supersede", "lesson-withdraw")
# A valid_until copied from a capture-time proxy: ended, at an unknown time.
UNKNOWN_END = "unknown-end"


def _conn(db):
    return getattr(db, "conn", db)


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _rows(conn, table: str, wanted: tuple[str, ...], where: str = "",
          params: tuple = ()) -> list[list]:
    have = _columns(conn, table)
    if not have:
        return []
    cols = [c for c in wanted if c in have]
    if not cols:
        return []
    rows = conn.execute(f"SELECT {','.join(cols)} FROM {table} {where}", params).fetchall()
    return sorted((list(r) for r in rows), key=canonical_json)


def _is_proxy(e: dict, ruled: frozenset | set = frozenset()) -> bool:
    """True when valid_from is the Writer clock standing in for an unknown time (store/write.py
    upsert_edge sets both valid_from and recorded_at to one `now` when the source gives no date).
    `ruled` holds the ids of the edges a record rule wrote, whose valid_from is the event's own time."""
    return bool(e["valid_from"]) and (
        (e["valid_from"] == e["recorded_at"] and e["edge_id"] not in ruled)
        or (e["extractor"] == "llm" and e["source_created_at"] is None))


def _edges(conn) -> list:
    if not _columns(conn, "edges"):
        return []
    cur = conn.execute(
        "SELECT edge_id, subj_node, predicate, obj_node, obj_literal, obj_datatype, valid_from, "
        "recorded_at, source_created_at, valid_until, status, source_block_id, source_quote, "
        "extractor FROM edges")
    names = [d[0] for d in cur.description]
    edges = [dict(zip(names, r)) for r in cur.fetchall()]
    if not edges:
        return []
    corrob: dict[str, set[str]] = {}
    for r in conn.execute("SELECT edge_id, source_block_id FROM edge_corroborations"):
        corrob.setdefault(r[0], set()).add(r[1])
    blocks = {e["source_block_id"] for e in edges} | set().union(*corrob.values()) \
        if corrob else {e["source_block_id"] for e in edges}
    block_doc = {}
    for block_id in blocks:
        row = conn.execute("SELECT doc_id FROM blocks WHERE block_id=?", (block_id,)).fetchone()
        if row:
            block_doc[block_id] = row[0]
    refs = source_refs_for_docs(conn, set(block_doc.values()))

    def ref_of(block_id):
        doc = block_doc.get(block_id)
        return refs.get(doc) if doc else None

    origins: dict[str, list] = {}
    if _columns(conn, "edge_origin"):
        for r in conn.execute(
                "SELECT edge_id, claim_ref, origin, rule_id, source_event_id, candidate_id, "
                "source_ref, quote_sha256, fact_kind, status FROM edge_origin"):
            origins.setdefault(r[0], []).append(list(r[1:]))
    ruled = {edge_id for edge_id, claims in origins.items()
             if any(claim[1] == "record-rule" for claim in claims)}
    proxy_times = {e["valid_from"] for e in edges if _is_proxy(e, ruled)}
    out = []
    for e in edges:
        obj = e["obj_node"] if e["obj_datatype"] == "node" else e["obj_literal"]
        valid_from = None if _is_proxy(e, ruled) else e["valid_from"]
        valid_until = e["valid_until"]
        if valid_until is not None and valid_until in proxy_times:
            valid_until = UNKNOWN_END
        out.append([e["subj_node"], e["predicate"], obj, e["obj_datatype"], valid_from,
                    e["source_created_at"], valid_until, e["status"],
                    ref_of(e["source_block_id"]), sha256_hex(e["source_quote"] or ""),
                    sorted({ref_of(b) or "" for b in corrob.get(e["edge_id"], ())}),
                    sorted(origins.get(e["edge_id"], []), key=canonical_json)])
    return sorted(out, key=canonical_json)


def _candidates(conn) -> list:
    if not _columns(conn, "candidates"):
        return []
    rows = conn.execute(
        "SELECT candidate_id, kind, status, verdict, admission_effect, record_event_id "
        "FROM candidates WHERE status IN ('admitted','retracted')").fetchall()
    out = []
    for r in rows:
        anchors = sorted([list(a) for a in conn.execute(
            "SELECT source_ref, quote_sha256 FROM candidate_anchors WHERE candidate_id=?",
            (r[0],))], key=canonical_json)
        out.append(list(r) + [anchors])
    return sorted(out, key=canonical_json)


def _corrections(conn) -> list:
    rows = _rows(conn, "corrections", (
        "target_subj", "target_predicate", "target_obj_key", "target_valid_from",
        "target_source_ref", "target_quote_sha256", "action", "replacement_json", "effective_at",
        "reason", "requested_by", "record_event_id", "status"))
    out = []
    for row in rows:
        replacement = row[7]
        if replacement:
            data = json.loads(replacement)
            if isinstance(data, dict):
                data = {k: v for k, v in data.items() if not k.endswith("edge_id")}
            row = row[:7] + [canonical_json(data)] + row[8:]
        out.append(row)
    return sorted(out, key=canonical_json)


def accepted_digest(db) -> str:
    conn = _conn(db)
    state = {
        "source_versions": _rows(conn, "source_versions", (
            "source_ref", "content_sha256", "origin_hash", "source_time", "private", "status"),
            "WHERE status IN ('current','withdrawn')"),
        "edges": _edges(conn),
        "candidates": _candidates(conn),
        "corrections": _corrections(conn),
        "exclusions": _rows(conn, "exclusions", (
            "source_ref", "reason", "requested_by", "record_event_id")),
        "lessons_log": _rows(conn, "lessons_log", ("event", "id", "block_json", "supersedes",
                                                   "run_id"),
                             "WHERE event IN (?,?,?)", _LESSON_EVENTS)
        if "event" in _columns(conn, "lessons_log") else [],
        "knowledge_facts": _rows(conn, "knowledge_facts", (
            "claim_id", "scope", "claim", "stamp", "blind_class", "decay_state")),
    }
    return sha256_hex("rune2-accepted/1\n" + canonical_json(state))


__all__ = ["accepted_digest"]
