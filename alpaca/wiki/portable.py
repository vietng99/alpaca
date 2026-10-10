"""Portable records: the `rune2-portable/1` export and import of a store's accepted state
(design/rune2-memory/architecture.md 4, 6.2, 10.4; FR-4, FR-23; OB-12, OB-19).

An export is JSON lines, one canonical JSON object per line (`determinism.canonical_json`), the
manifest first: the record cutoff (the record's last event when the store was read, and its hash),
whether private rows were asked for, and the row count of each kind. The rows are the accepted state
`digest.accepted_digest` covers, named by portable identities only (source refs, quote and body
sha256, fact identities, record event ids), never by a block, doc or edge id, which stay provenance
of one store (architecture.md 4.3): the source envelopes (`source_versions`, current and withdrawn,
never a replaced version), the facts with their anchor and support claims, the admitted (or since
retracted) candidates, the corrections, the exclusions, and the lessons and priors the admissions
wrote. Clock columns are left out, since knowledge time comes from the record (OB-19); a `wiki
ingest` document, which the record holds no copy of, keeps its text and the time the store recorded
it, so an import can land it again.

Redaction (architecture.md 6.2) is decided when the export is read, by the servable predicate
(`servable.servable_ref`): a quote whose source fails it, and a candidate's body when any of its
anchors fails it, are null with `redacted: true` beside their sha256; a fact anchored on such a
source is written without its subject, object and quote, and so is the fact a correction names
when the fact's source fails it (its subject and object are spans of that source's quote, K-3); a
correction whose own note fails it is written without its replacement and reason; a lesson or
prior whose candidate was withdrawn, or has an anchor that fails it, is written without its text
(OB-12). With `include_private` the rows of a private or quarantined source keep their text; a
withdrawn source is redacted always, since an exclusion is not a purge: its text stays in the
record, never in an export.

`import_store` pours a fresh store. The documents the export carries land first, each at the time
the exporting store recorded it; then the store is rebuilt from the record through the manifest
cutoff (`recovery.replay`, every event at its `ts`), so every accepted row it holds is one the record
backs: an admitted fact, lesson or prior only through an applied `wiki-admission` event at or below
the cutoff, a correction only through its `wiki-correction` event, a record fact only when its rule
reproduces it from the event's note. The export's rows are then compared with the rows of the new
store, read the same way: a row the store holds is imported, a row it does not hold is refused and
counted, and a row whose source the import cannot land (a document the export carries no text for,
a transcript whose file is gone) is counted as unrestorable. A row of the new store that the export
lacks (an event the exporting store had not captured yet, a private row a public export left out)
is counted as coming from the record. The store records in `meta` that its knowledge time comes from
the record (OB-19). Exporting the imported store gives the same bytes when nothing was refused,
unrestorable or added. Nothing here reads the network or calls a model, and nothing here writes the
record.
"""
from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from alpaca import db as record
from .config import Config
from .determinism import canonical_json, sha256_hex

FORMAT = "rune2-portable/1"
#: The row kinds, in the order an export writes them after the manifest.
KINDS = ("source", "exclusion", "correction", "candidate", "edge", "lesson", "prior")
_LESSON_EVENTS = ("lesson-admit", "lesson-supersede", "lesson-withdraw")


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def _clean(value):
    """Every string of a row through the secret screen of the retain door (`drain.screen`, FR-26;
    PO-7: no secret in an export), so text a store kept from before the screen leaves masked. The
    screen is idempotent: text captured since passes unchanged, and an export stays byte-identical."""
    from .ingest.drain import screen
    if isinstance(value, str):
        return screen(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _hider(conn, include_private: bool):
    """`hidden(source_ref)`: whether text from that source may not leave in this export."""
    from .servable import excluded_source_refs, servable_ref
    if include_private:
        excluded = excluded_source_refs(conn)
        return lambda ref: not ref or ref in excluded
    view = SimpleNamespace(conn=conn)          # servable_ref caches its set on the view
    return lambda ref: not servable_ref(view, ref)


# ------------------------------------------------------------------ reading the accepted state

def _document_text(conn, vault: Path, doc_id: str, content_sha256: str) -> str | None:
    """The bytes of a `wiki ingest` document as the vault keeps them (`service.ingest` writes the
    screened text there), when they still hash to the version's `content_sha256` and the secret
    screen leaves them as they are. A document stored before the screen is not exported with its
    text: ingesting it again screens it (FR-26)."""
    from .ingest.drain import screen
    row = conn.execute("SELECT path FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
    key = PurePosixPath(row[0] if row else doc_id)
    if key.is_absolute() or ".." in key.parts:
        return None
    path = Path(vault, *key.parts)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if sha256_hex(text) == content_sha256 and screen(text) == text else None


def _sources(conn, hidden, vault: Path) -> list:
    out = []
    for r in conn.execute("SELECT * FROM source_versions WHERE status IN ('current','withdrawn') "
                          "ORDER BY source_ref, version_seq"):
        row = {"kind": "source", "source_ref": r["source_ref"],
               "content_sha256": r["content_sha256"], "origin_hash": r["origin_hash"],
               "operator": r["operator"], "session": r["session"], "op": r["op"], "task": r["task"],
               "source_time": r["source_time"], "private": r["private"], "status": r["status"]}
        if r["source_ref"].startswith("document:"):
            # not in the record: what an import needs to land it again, unless it may not leave
            text = None
            if r["status"] == "current" and not hidden(r["source_ref"]):
                text = _document_text(conn, vault, r["doc_id"], r["content_sha256"])
            row.update(observed_at=r["recorded_at"], text=text, redacted=hidden(r["source_ref"]))
        out.append(row)
    return out


def _exclusions(conn) -> list:
    return [{"kind": "exclusion", "source_ref": r[0], "reason": r[1], "requested_by": r[2],
             "record_event_id": r[3]}
            for r in conn.execute("SELECT source_ref, reason, requested_by, record_event_id "
                                  "FROM exclusions")]


def _corrections(conn, hidden) -> list:
    """The owner's corrections. A correction names its fact by identity (architecture.md 4.3),
    whose subject and object are spans of the fact's anchor quote (K-3): when that source is hidden,
    they are null with `redacted: true` beside their sha256, as the fact's own row is written
    (`_edges`). The replacement and the reason are the text of the correction's own note
    (`event:<record_event_id>`, which renders them): when that note is hidden, they go the same
    way. The predicate, the times and the refs are kept, which hold no source text."""
    out = []
    for r in conn.execute("SELECT * FROM corrections"):
        replacement = json.loads(r["replacement_json"]) if r["replacement_json"] else None
        if isinstance(replacement, dict):           # edge ids are provenance of one store
            replacement = {k: v for k, v in replacement.items() if not k.endswith("edge_id")}
        target = {"subject": r["target_subj"], "predicate": r["target_predicate"],
                  "object_key": r["target_obj_key"], "valid_from": r["target_valid_from"],
                  "source_ref": r["target_source_ref"], "quote_sha256": r["target_quote_sha256"]}
        row = {"kind": "correction", "action": r["action"], "replacement": replacement,
               "target": target, "effective_at": r["effective_at"], "reason": r["reason"],
               "requested_by": r["requested_by"], "record_event_id": r["record_event_id"],
               "status": r["status"], "redacted": False}
        if hidden(r["target_source_ref"]):
            target.update(subject=None, object_key=None,
                          subject_sha256=sha256_hex(r["target_subj"] or ""),
                          object_key_sha256=sha256_hex(r["target_obj_key"] or ""))
            row["redacted"] = True
        if hidden("event:%d" % r["record_event_id"]):
            row.update(replacement=None, reason=None, redacted=True,
                       replacement_sha256=sha256_hex(canonical_json(replacement))
                       if replacement is not None else None,
                       reason_sha256=sha256_hex(r["reason"] or ""))
        out.append(row)
    return out


def _anchor_rows(conn) -> dict:
    out: dict = {}
    for a in conn.execute("SELECT * FROM candidate_anchors WHERE ordinal>=0 "
                          "ORDER BY candidate_id, ordinal"):
        out.setdefault(a["candidate_id"], []).append(dict(a))
    return out


def _candidates(conn, hidden, anchors: dict) -> list:
    """The candidates with an applied admission (admitted, or retracted since), redacted as the
    candidates view redacts them (`intake.candidate_views`): a quote whose source is hidden is null,
    and the body is null when an anchor's source is. A retracted candidate keeps only its ids and
    hashes, like the lesson or prior it wrote (architecture.md 6.2): its body and quotes are the text
    of a claim no longer accepted."""
    out = []
    for r in conn.execute("SELECT * FROM candidates WHERE status IN ('admitted','retracted')"):
        rows, any_hidden = [], r["status"] != "admitted"
        for a in anchors.get(r["candidate_id"], ()):
            gone = hidden(a["source_ref"]) or r["status"] != "admitted"
            any_hidden = any_hidden or (gone and a["role"] == "anchor")
            quote = None if gone else a["quote"]
            rows.append({"source_ref": a["source_ref"], "role": a["role"], "quote": quote,
                         "quote_sha256": a["quote_sha256"], "redacted": quote is None})
        body = None if (any_hidden or r["body_json"] is None) else json.loads(r["body_json"])
        out.append({"kind": "candidate", "candidate_id": r["candidate_id"],
                    "candidate_kind": r["kind"], "status": r["status"], "verdict": r["verdict"],
                    "admission_effect": r["admission_effect"],
                    "record_event_id": r["record_event_id"],
                    "admission_event_id": r["admission_event_id"], "proposer": r["proposer"],
                    "proposer_operator": r["proposer_operator"], "body": body,
                    "body_sha256": r["body_sha256"], "redacted": body is None, "anchors": rows})
    return out


def _edges(conn, hidden) -> list:
    """Every fact, by its identity, with its anchor and support claims, in the shape of
    `accepted_digest` (a capture-time stand-in for an unknown valid time is null)."""
    from .digest import UNKNOWN_END, _is_proxy
    from .servable import source_refs_for_docs
    edges = [dict(r) for r in conn.execute("SELECT * FROM edges")]
    if not edges:
        return []
    claims: dict = {}
    if _has(conn, "edge_origin"):
        for c in conn.execute("SELECT * FROM edge_origin"):
            claims.setdefault(c["edge_id"], []).append(dict(c))
    corroborators: dict = {}
    for r in conn.execute("SELECT edge_id, source_block_id FROM edge_corroborations"):
        corroborators.setdefault(r[0], set()).add(r[1])
    blocks = {e["source_block_id"] for e in edges}.union(*corroborators.values())
    block_doc = {}
    for block_id in blocks:
        row = conn.execute("SELECT doc_id FROM blocks WHERE block_id=?", (block_id,)).fetchone()
        if row:
            block_doc[block_id] = row[0]
    refs = source_refs_for_docs(conn, set(block_doc.values()))

    def ref_of(block_id):
        return refs.get(block_doc.get(block_id)) if block_id in block_doc else None

    ruled = {k for k, v in claims.items() if any(c["origin"] == "record-rule" for c in v)}
    proxy_times = {e["valid_from"] for e in edges if _is_proxy(e, ruled)}
    out = []
    for e in edges:
        anchor_ref = ref_of(e["source_block_id"])
        gone = hidden(anchor_ref)
        obj = e["obj_node"] if e["obj_datatype"] == "node" else e["obj_literal"]
        until = e["valid_until"]
        row = {"kind": "edge", "predicate": e["predicate"], "object_type": e["obj_datatype"],
               "subject": None if gone else e["subj_node"], "object": None if gone else obj,
               "valid_from": None if _is_proxy(e, ruled) else e["valid_from"],
               "source_created_at": e["source_created_at"],
               "valid_until": UNKNOWN_END if until is not None and until in proxy_times else until,
               "status": e["status"], "extractor": e["extractor"],
               "extractor_version": e["extractor_version"], "redacted": gone,
               "anchor": {"source_ref": anchor_ref, "quote": None if gone else e["source_quote"],
                          "quote_sha256": sha256_hex(e["source_quote"] or "")},
               "corroborated_by": sorted({ref_of(b) or "" for b in corroborators.get(e["edge_id"], ())}),
               "claims": sorted((_claim(c, hidden) for c in claims.get(e["edge_id"], ())),
                                key=canonical_json)}
        if gone:
            row.update(subject_sha256=sha256_hex(e["subj_node"] or ""),
                       object_sha256=sha256_hex(str(obj or "")))
        out.append(row)
    return out


def _claim(c: dict, hidden) -> dict:
    quote = None if hidden(c["source_ref"]) else c["quote"]
    return {"claim_ref": c["claim_ref"], "origin": c["origin"], "rule_id": c["rule_id"],
            "source_event_id": c["source_event_id"], "candidate_id": c["candidate_id"],
            "source_ref": c["source_ref"], "quote": quote, "quote_sha256": c["quote_sha256"],
            "fact_kind": c["fact_kind"], "status": c["status"], "redacted": quote is None}


def _withheld(conn, hidden, anchors: dict) -> dict:
    """admission_ref -> True when the lesson or prior an observation admitted may not leave with its
    text: its candidate is no longer admitted (withdrawn, or retracted with its source), or an anchor
    of it fails the servable predicate now (OB-12)."""
    out = {}
    for r in conn.execute("SELECT candidate_id, status, admission_ref FROM candidates "
                          "WHERE kind='observation' AND admission_ref IS NOT NULL"):
        failing = any(hidden(a["source_ref"]) for a in anchors.get(r["candidate_id"], ())
                      if a["role"] == "anchor")
        out[r["admission_ref"]] = r["status"] != "admitted" or failing
    return out


def _lessons(conn, withheld: dict) -> list:
    if not _has(conn, "lessons_log"):
        return []
    rows = [dict(r) for r in conn.execute(
        "SELECT event, id, block_json, supersedes, run_id FROM lessons_log WHERE event IN (?,?,?)",
        _LESSON_EVENTS)]
    out = []
    for r in rows:
        # a `lesson-withdraw` row is written only where its candidate is retracted (withdrawal.py
        # `undo_derived`), so the candidate's status decides; a lesson of no candidate stays hidden
        gone = withheld.get(r["id"], True)
        out.append({"kind": "lesson", "event": r["event"], "id": r["id"],
                    "block": None if gone or r["block_json"] is None else json.loads(r["block_json"]),
                    "block_sha256": sha256_hex(r["block_json"]) if r["block_json"] else None,
                    "supersedes": r["supersedes"], "run_id": r["run_id"], "redacted": gone})
    return out


def _priors(conn, withheld: dict) -> list:
    if not _has(conn, "knowledge_facts"):
        return []
    out = []
    for r in conn.execute("SELECT * FROM knowledge_facts"):
        gone = withheld.get(r["claim_id"], True)
        out.append({"kind": "prior", "claim_id": r["claim_id"], "scope": r["scope"],
                    "claim": None if gone else r["claim"], "claim_sha256": sha256_hex(r["claim"]),
                    "falsifiability": None if gone else r["falsifiability"],
                    "stamp": r["stamp"], "blind_class": r["blind_class"],
                    "trust_ceiling": r["trust_ceiling"], "decay_state": r["decay_state"],
                    "redacted": gone})
    return out


def _rows(conn, vault: Path, include_private: bool) -> list:
    """The export rows of a store, in export order: by kind (KINDS), then by their canonical text."""
    hidden = _hider(conn, include_private)
    found = {kind: [] for kind in KINDS}
    if _has(conn, "source_versions"):
        found["source"] = _sources(conn, hidden, vault)
    if _has(conn, "exclusions"):
        found["exclusion"] = _exclusions(conn)
    if _has(conn, "corrections"):
        found["correction"] = _corrections(conn, hidden)
    anchors = _anchor_rows(conn) if _has(conn, "candidate_anchors") else {}
    if _has(conn, "candidates"):
        found["candidate"] = _candidates(conn, hidden, anchors)
        withheld = _withheld(conn, hidden, anchors)
    else:
        withheld = {}
    if _has(conn, "edges"):
        found["edge"] = _edges(conn, hidden)
    found["lesson"] = _lessons(conn, withheld)
    found["prior"] = _priors(conn, withheld)
    return [row for kind in KINDS for row in sorted((_clean(r) for r in found[kind]),
                                                    key=canonical_json)]


def _store_rows(cfg, include_private: bool) -> list:
    """The export rows of the project's store, read in one read transaction (one snapshot)."""
    from .store.db import open_db_readonly
    if not cfg.db_path.exists():
        return []
    conn = open_db_readonly(cfg)
    try:
        conn.execute("BEGIN")
        try:
            return _rows(conn, cfg.vault_dir, include_private)
        finally:
            conn.execute("ROLLBACK")
    finally:
        conn.close()


def _record_head(root) -> tuple:
    source = record.connect_readonly(str(root))
    try:
        row = source.execute("SELECT id, hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        source.close()
    return (int(row[0]), row[1]) if row else (0, None)


def _write_private(path: Path, text: str) -> None:
    """Publish the export atomically, readable by its owner only (it may hold private rows)."""
    path = Path(path)
    temporary = path.with_name(".%s.%s" % (path.name, uuid.uuid4().hex))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


# ------------------------------------------------------------------ export

def export_store(root, out, *, include_private: bool = False) -> dict:
    """Write the `rune2-portable/1` export of the project's store to `out` (FR-23) and return its
    manifest. The store is read in one read transaction, then the record's last event is named as
    the cutoff, so every row's backing event is at or below it. Reads only; private rows only on
    request (`include_private`). A store behind the record's removal acts is not exported: it raises
    `service.StoreBehindRecord` (`unavailable`, `store-behind-record`; t-070 round 2 C-E)."""
    from . import service
    service._serving_gate(root)
    cfg = Config.for_project(root)
    rows = _store_rows(cfg, include_private)
    cutoff, cutoff_hash = _record_head(root)
    manifest = {"kind": "manifest", "format": FORMAT, "cutoff": cutoff, "cutoff_hash": cutoff_hash,
                "private": bool(include_private),
                "rows": {kind: sum(1 for r in rows if r["kind"] == kind) for kind in KINDS}}
    _write_private(Path(out), "".join(canonical_json(r) + "\n" for r in [manifest] + rows))
    return manifest


# ------------------------------------------------------------------ import

def _read(path) -> tuple:
    lines = [line for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise ValueError("an empty file is not a %s export" % FORMAT)
    manifest = json.loads(lines[0])
    if not isinstance(manifest, dict) or manifest.get("kind") != "manifest" \
            or manifest.get("format") != FORMAT:
        raise ValueError("not a %s export: the first line is not its manifest" % FORMAT)
    if not isinstance(manifest.get("cutoff"), int) or manifest["cutoff"] < 0:
        raise ValueError("the manifest names no record cutoff")
    rows = [json.loads(line) for line in lines[1:]]
    if any(not isinstance(r, dict) or r.get("kind") not in KINDS for r in rows):
        raise ValueError("a %s row has an unknown kind" % FORMAT)
    return manifest, rows


def _record_backs(root, manifest: dict) -> str | None:
    """None when the record holds the manifest's cutoff event with its hash, else why not."""
    if manifest["cutoff"] == 0:
        return None
    source = record.connect_readonly(str(root))
    try:
        row = source.execute("SELECT hash FROM events WHERE id=?", (manifest["cutoff"],)).fetchone()
    finally:
        source.close()
    if row is None:
        return "record-behind-cutoff"
    return None if row[0] == manifest.get("cutoff_hash") else "record-mismatch"


def _record_chain(root) -> str | None:
    """None when the record's hash chain verifies (`alpaca.db.verify_chain`, what the replay checks
    first), else `record-chain: <why>`. Read-only."""
    source = record.connect_readonly(str(root))
    try:
        ok, why = record.verify_chain(source)
    finally:
        source.close()
    return None if ok else "record-chain: %s" % why


def _claim_store(cfg, cutoff: int) -> bool:
    """Claim the empty store for this import under the write lock (`Writer.claim_for_import`)."""
    from .store.db import DB
    from .store.ledger import Ledger
    from .store.write import Writer
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    db = DB(cfg)
    try:
        db.pour()
        writer = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir))
        with writer.transaction():
            return writer.claim_for_import(cutoff)
    finally:
        db.close()


def _document_id(source_ref: str) -> str | None:
    """The doc id of a `document:` ref, when it is a canonical `wiki/... .md` path (`service.ingest`)."""
    doc_id = source_ref[len("document:"):]
    key = PurePosixPath(doc_id)
    if (not doc_id or "\\" in doc_id or key.is_absolute() or ".." in key.parts
            or key.as_posix() != doc_id or len(key.parts) < 2 or key.parts[0] != "wiki"
            or key.suffix.lower() != ".md"):
        return None
    return doc_id


def _land_documents(cfg, rows: list) -> tuple:
    """Land the `wiki ingest` documents an export carries, through the write door, each with the
    Writer clock at the time the exporting store recorded it, and their vault copies, which hold the
    screened text the door stores. A document
    whose text does not hash to its version, whose doc id is not a wiki path, or that holds generated
    briefing text, is not landed. Returns (landed refs, refs not landed)."""
    from . import faults
    from .briefing import holds_generated
    from .clock import now_iso, to_clock_format
    from .ingest import drain
    from .ingest.absorb import Absorber
    landed, skipped = [], []
    wanted = [r for r in rows if r["kind"] == "source" and r["status"] == "current"
              and str(r.get("source_ref", "")).startswith("document:") and r.get("text") is not None]
    if not wanted:
        return landed, skipped
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg)
    try:
        for r in sorted(wanted, key=lambda r: (str(r.get("observed_at") or ""), r["source_ref"])):
            doc_id, text = _document_id(r["source_ref"]), r["text"]
            if (doc_id is None or not isinstance(text, str) or sha256_hex(text) != r["content_sha256"]
                    or holds_generated(text)):
                skipped.append(r["source_ref"])
                continue
            stamp = to_clock_format(r.get("observed_at")) or now_iso()
            absorber.writer.clock = lambda: stamp
            # screened once, before the vault copy: the vault file is the text the door stores
            # (t-070 RC2, carried m-inv-2: the row's text as given went to the vault); the marking
            # scanner reads the row's text before the screen, as for `wiki ingest`
            screened = drain.screen(text)
            faults.boundary("import-row")
            with drain._projection_target(str(cfg.vault_dir), doc_id) as parent_fd:
                def publish(conn, doc_id=doc_id, screened=screened, parent_fd=parent_fd):
                    """The vault copy before the store commit (t-070 round 2 C-E)."""
                    drain._write_if_changed(os.path.join(str(cfg.vault_dir), doc_id), screened,
                                            parent_fd=parent_fd)
                absorber.absorb_text(doc_id, screened, kind="wiki", path=doc_id,
                                     session=r.get("session"), unscreened=text,
                                     before_commit=publish)
            landed.append(r["source_ref"])
    finally:
        absorber.close()
    return landed, skipped


def _restore_envelopes(cfg, rows: list) -> int:
    """The envelopes of withdrawn `wiki ingest` documents (`Writer.import_source_version`)."""
    from .store.db import DB
    from .store.ledger import Ledger
    from .store.write import Writer
    wanted = [r for r in rows if r["kind"] == "source" and r["status"] == "withdrawn"
              and str(r.get("source_ref", "")).startswith("document:")]
    if not wanted:
        return 0
    db = DB(cfg)
    try:
        db.pour()
        writer = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir))
        with writer.transaction():
            return sum(1 for r in sorted(wanted, key=canonical_json)
                       if writer.import_source_version(r))
    finally:
        db.close()


def _depends_on(row: dict) -> set:
    """The sources a row cannot be landed without."""
    if row["kind"] in ("source", "exclusion"):
        return {row.get("source_ref")}
    if row["kind"] == "edge":
        return {(row.get("anchor") or {}).get("source_ref")}
    if row["kind"] == "candidate":
        return {a.get("source_ref") for a in row.get("anchors") or () if a.get("role") == "anchor"}
    return set()


def _refusal(row: dict) -> str:
    """Why the record does not back a row the new store lacks."""
    kind = row["kind"]
    if kind == "source" and str(row.get("source_ref", "")).startswith("document:"):
        return "document-not-landed"
    if kind == "edge":
        if row.get("extractor") == "llm":
            return "no-applied-admission"
        if any(str(c.get("rule_id") or "").startswith("wiki-correction/") for c in row.get("claims") or ()):
            return "no-correction-event"
        return "rule-does-not-reproduce"
    return {"source": "not-in-record", "exclusion": "no-withdrawal-event",
            "correction": "no-correction-event"}.get(kind, "no-applied-admission")


def import_store(root, source) -> dict:
    """Pour a fresh store from a `rune2-portable/1` export (FR-23; architecture.md 10.4) and report
    what was imported, refused, unrestorable and added from the record (see the module text). The
    project's store must hold nothing yet, and its record must hold the manifest's cutoff event
    with the same hash; otherwise nothing is written and the report says why.

    Everything that can refuse the import is checked before the first document lands (t-070, finding
    m-adv-4): the export's rows (`_read`), the cutoff event, and the record's whole hash chain, which
    the replay would otherwise check only after the documents landed, leaving a store that refuses
    the retry. Then the empty store is claimed under the write lock (`Writer.claim_for_import`), so a
    second import at the same moment is refused `store-not-empty` (RC1)."""
    from . import recovery
    manifest, rows = _read(source)
    cfg = Config.for_project(root)
    report = {"format": FORMAT, "cutoff": manifest["cutoff"],
              "rows": dict(Counter(r["kind"] for r in rows))}
    if not recovery.store_is_empty(cfg):
        return dict(report, status="refused", reason="store-not-empty")
    why = _record_backs(root, manifest) or _record_chain(root)
    if why is not None:
        return dict(report, status="refused", reason=why)
    if not _claim_store(cfg, manifest["cutoff"]):
        return dict(report, status="refused", reason="store-not-empty")
    landed, skipped = _land_documents(cfg, rows)
    from . import faults
    faults.boundary("import-replay")
    replayed = recovery.replay(root, cutoff=manifest["cutoff"], rebuild=True)
    envelopes = _restore_envelopes(cfg, rows)
    held = Counter(canonical_json(r) for r in _store_rows(cfg, bool(manifest.get("private"))))
    lost = {r["source_ref"] for r in rows if r["kind"] == "source"
            and str(r.get("source_ref", "")).startswith("document:")
            and r["status"] == "current" and r.get("text") is None}
    lost |= {"transcript:%s" % m.get("session") for m in replayed.get("missing_transcripts") or ()}
    imported, unrestorable, refused = Counter(), Counter(), []
    for row in rows:
        line = canonical_json(row)
        if held[line] > 0:
            held[line] -= 1
            imported[row["kind"]] += 1
        elif _depends_on(row) & lost:
            unrestorable[row["kind"]] += 1
        else:
            refused.append({"kind": row["kind"], "reason": _refusal(row), "row_sha256": sha256_hex(line)})
    added = Counter(json.loads(line)["kind"] for line, n in held.items() for _ in range(n))
    report.update(status=replayed["status"], knowledge_time="record",
                  imported=dict(imported), refused=refused, unrestorable=dict(unrestorable),
                  from_record=dict(added), documents_landed=len(landed),
                  documents_not_landed=sorted(skipped), envelopes_restored=envelopes,
                  replay={k: replayed.get(k) for k in ("through_event", "sessions", "notes",
                                                       "missing_transcripts", "candidates")
                          if k in replayed})
    return report


__all__ = ["FORMAT", "KINDS", "export_store", "import_store"]
