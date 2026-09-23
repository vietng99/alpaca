"""operating-surface.8 - the /lucid gap ledger with pinned Layer-1 deterministic detectors.

A continuously-growing memory rots silently unless something enumerates what it is MISSING. This
module is that enumerator: a set of pure, deterministic SQL detectors (Layer-1, no LLM) run every
build, each returning a `gap_id`-sorted candidate set, folded into the append-only `gaps` ledger.

Determinism contract (Fork-A / research brief 3): a gap's identity is CONTENT-DERIVED so the same
gap across runs is the same row (open/closed lifecycle, byte-reproducible opened/closed diff):

    gap_id = SHA256( GAP_TAG + JCS{type, subject_key, detail_key} )

hashed over NFC strings via determinism.sha256_hex/canonical_json - never a float, never builtin
hash(). Detectors are pure queries; `rank_gaps` orders the surfaced set; `expire_gaps` is the
category-aware anti-rot sweep (low-signal single-referrer gaps expire, high-distinct-referrer gaps
are curator-only and NEVER auto-expire); `close_gap_on_ingest` is the loop-closure that proposes
[x] with a gap->raw back-reference when freshly-ingested content resolves a gap.
"""
from __future__ import annotations

from typing import Optional

from ..clock import now_iso
from ..determinism import canonical_json, nfc, sha256_hex
from ..store.db import DB

# Frozen domain tag + NUL (research brief 3). NEVER change it - it repins every gap_id.
GAP_TAG = "rune2/gap/v1\x00"

# Fixed per-type severity (1..5). Data, never a float.
SEVERITY: dict[str, int] = {
    "OPEN_CONTRADICTION": 5,
    "STALE_HOT": 4,
    "UNANSWERED_QUERY": 4,
    "MISSING_INVERSE": 3,
    "RED_LINK": 3,
    "STUB": 2,
    "ORPHAN_NODE": 2,
}

STUB_EDGE_FLOOR = 2   # a node defined by fewer than K active subject-edges is a STUB


# ---------------------------------------------------------------- identity

def gap_id(gap_type: str, subject_key: str, detail_key: str) -> str:
    """Content-derived stable id. JCS = canonical_json over NFC strings (Fork-A)."""
    payload = canonical_json({
        "type": nfc(gap_type),
        "subject_key": nfc(subject_key or ""),
        "detail_key": nfc(detail_key or ""),
    })
    return "g_" + sha256_hex(GAP_TAG + payload)[:32]


def _gap(gap_type: str, subject_key: str, detail: str, distinct_referrers: int = 1,
         detail_key: str = "") -> dict:
    return {
        "gap_id": gap_id(gap_type, subject_key, detail_key),
        "gap_type": gap_type,
        "subject_key": subject_key,
        "detail": detail,
        "severity": SEVERITY.get(gap_type, 3),
        "distinct_referrers": max(1, int(distinct_referrers)),
    }


# ---------------------------------------------------------------- Layer-1 detectors
# Each is a PURE query returning a gap_id-sorted list. No writes, no LLM, no floats in identity.

def detect_red_links(db: DB) -> list[dict]:
    """RED_LINK: a node pointed AT by >=1 active edge (as object) but which asserts nothing itself
    (never an active subject). A dangling wikilink target - referenced, undefined."""
    rows = db.conn.execute(
        "SELECT n.node_id AS node_id, COUNT(DISTINCT e.edge_id) AS refs "
        "FROM nodes n JOIN edges e ON e.obj_node = n.node_id AND e.status='active' "
        "WHERE n.status='active' "
        "AND NOT EXISTS (SELECT 1 FROM edges s WHERE s.subj_node=n.node_id AND s.status='active') "
        "GROUP BY n.node_id"
    ).fetchall()
    out = [_gap("RED_LINK", r["node_id"], f"referenced by {r['refs']} edge(s), never defined",
               distinct_referrers=r["refs"]) for r in rows]
    return sorted(out, key=lambda g: g["gap_id"])


def detect_stubs(db: DB, k: int = STUB_EDGE_FLOOR) -> list[dict]:
    """STUB: a node with between 1 and K-1 active subject-edges (thinly defined)."""
    rows = db.conn.execute(
        "SELECT subj_node AS node_id, COUNT(DISTINCT edge_id) AS n "
        "FROM edges WHERE status='active' GROUP BY subj_node HAVING n < ? AND n >= 1",
        (k,),
    ).fetchall()
    out = [_gap("STUB", r["node_id"], f"only {r['n']} active edge(s) (< {k})",
               distinct_referrers=r["n"], detail_key=str(r["n"])) for r in rows]
    return sorted(out, key=lambda g: g["gap_id"])


def detect_orphan_nodes(db: DB) -> list[dict]:
    """ORPHAN_NODE: an active node in NO edge (subject or object) and with NO passage projection."""
    rows = db.conn.execute(
        "SELECT n.node_id AS node_id FROM nodes n WHERE n.status='active' "
        "AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.subj_node=n.node_id OR e.obj_node=n.node_id) "
        "AND NOT EXISTS (SELECT 1 FROM node_blocks nb WHERE nb.node_id=n.node_id)"
    ).fetchall()
    out = [_gap("ORPHAN_NODE", r["node_id"], "no edges, no passages") for r in rows]
    return sorted(out, key=lambda g: g["gap_id"])


def detect_expired_claims(db: DB, now: Optional[str] = None) -> list[dict]:
    """STALE_HOT: an active volatile edge past its expires_at - served bare it would be stale."""
    now = now or now_iso()
    rows = db.conn.execute(
        "SELECT edge_id, expires_at FROM edges "
        "WHERE status='active' AND volatile=1 AND expires_at IS NOT NULL AND expires_at < ? "
        "ORDER BY edge_id",
        (now,),
    ).fetchall()
    out = [_gap("STALE_HOT", r["edge_id"], f"expired {r['expires_at']}",
               detail_key=r["expires_at"] or "") for r in rows]
    return sorted(out, key=lambda g: g["gap_id"])


def detect_open_contradictions(db: DB) -> list[dict]:
    """OPEN_CONTRADICTION: an active edge marked 'contradicts' - a live contested assertion."""
    rows = db.conn.execute(
        "SELECT edge_id, subj_node, predicate FROM edges "
        "WHERE status='active' AND reconcile_verdict='contradicts' ORDER BY edge_id"
    ).fetchall()
    out = [_gap("OPEN_CONTRADICTION", f"{r['subj_node']}|{r['predicate']}",
               f"contested edge {r['edge_id']}", detail_key=r["edge_id"]) for r in rows]
    return sorted(out, key=lambda g: g["gap_id"])


def detect_unanswered_queries(db: DB) -> list[dict]:
    """UNANSWERED_QUERY: an eval_ledger row that abstained - a query the memory could not answer."""
    rows = db.conn.execute(
        "SELECT query_id, qtype FROM eval_ledger WHERE abstained=1 ORDER BY query_id"
    ).fetchall()
    out = [_gap("UNANSWERED_QUERY", r["query_id"], f"abstained ({r['qtype']})") for r in rows]
    return sorted(out, key=lambda g: g["gap_id"])


def detect_missing_inverse(db: DB) -> list[dict]:
    """MISSING_INVERSE: an active node-object edge whose predicate declares an inverse, but no
    active inverse edge (obj --inverse--> subj) exists. The graph is one-directional where it
    should be symmetric."""
    rows = db.conn.execute(
        "SELECT e.edge_id, e.subj_node, e.obj_node, e.predicate, p.inverse AS inv "
        "FROM edges e JOIN predicates p ON p.predicate = e.predicate "
        "WHERE e.status='active' AND e.obj_datatype='node' AND e.obj_node IS NOT NULL "
        "AND p.inverse IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM edges b WHERE b.status='active' "
        "                AND b.subj_node = e.obj_node AND b.obj_node = e.subj_node "
        "                AND b.predicate = p.inverse) "
        "ORDER BY e.edge_id"
    ).fetchall()
    out = [_gap("MISSING_INVERSE", r["edge_id"],
               f"{r['subj_node']} {r['predicate']} {r['obj_node']} lacks inverse {r['inv']}",
               detail_key=r["inv"] or "") for r in rows]
    return sorted(out, key=lambda g: g["gap_id"])


# canonical detector registry - order fixed for a byte-reproducible sweep.
DETECTORS = (
    detect_red_links,
    detect_stubs,
    detect_orphan_nodes,
    detect_expired_claims,
    detect_open_contradictions,
    detect_unanswered_queries,
    detect_missing_inverse,
)


# ---------------------------------------------------------------- ledger writes

def _current_seq(db: DB) -> int:
    row = db.conn.execute("SELECT MAX(seq) AS s FROM ingest_event").fetchone()
    return int(row["s"]) if row and row["s"] is not None else 0


def scan_gaps(db: DB, now: Optional[str] = None) -> list[dict]:
    """Run the canonical detector registry without writing the gap ledger."""
    now = now or now_iso()
    detected: list[dict] = []
    for fn in DETECTORS:
        detected.extend(fn(db, now) if fn is detect_expired_claims else fn(db))
    detected.sort(key=lambda g: g["gap_id"])
    return detected


def detect_gaps(db: DB, seq: Optional[int] = None, now: Optional[str] = None) -> list[dict]:
    """Run every Layer-1 detector, fold results into the append-only gaps ledger, return the
    gap_id-sorted detected set. Re-detection updates last_seen_seq + distinct_referrers only -
    a gap's status (open/closed) is a lifecycle decision made elsewhere, never clobbered here."""
    now = now or now_iso()
    seq = seq if seq is not None else _current_seq(db)
    detected = scan_gaps(db, now)
    db.conn.execute("BEGIN")
    try:
        for g in detected:
            db.conn.execute(
                "INSERT INTO gaps(gap_id,gap_type,subject_key,detail,severity,distinct_referrers,"
                "first_seen_seq,last_seen_seq,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'open', ?) "
                "ON CONFLICT(gap_id) DO UPDATE SET last_seen_seq=excluded.last_seen_seq, "
                "distinct_referrers=excluded.distinct_referrers, "
                "severity=excluded.severity, detail=excluded.detail",
                (g["gap_id"], g["gap_type"], g["subject_key"], g["detail"], g["severity"],
                 g["distinct_referrers"], seq, seq, now),
            )
    except Exception:
        db.conn.execute("ROLLBACK")
        raise
    else:
        db.commit()
    return detected


def rank_gaps(gaps: list[dict]) -> list[dict]:
    """Priority order = severity x distinct_referrers (DESC), gap_id ASC as the deterministic
    tie-break. Both terms are integers - no float ever orders a gap."""
    return sorted(
        gaps,
        key=lambda g: (-(int(g["severity"]) * int(g["distinct_referrers"])), g["gap_id"]),
    )


def expire_gaps(db: DB, now: Optional[str] = None, min_referrers_to_retain: int = STUB_EDGE_FLOOR,
                now_seq: Optional[int] = None) -> list[str]:
    """Category-aware anti-rot sweep. A LOW-SIGNAL gap (distinct_referrers < the retain floor)
    ages out to status='closed' with an 'expired' evidence stamp. A HIGH-distinct-referrer gap is
    curator-only and is NEVER auto-expired - expiring it would rot away exactly the signal that
    matters. Returns the list of expired gap_ids (gap_id-sorted)."""
    now = now or now_iso()
    rows = db.conn.execute(
        "SELECT gap_id FROM gaps WHERE status='open' AND distinct_referrers < ? ORDER BY gap_id",
        (min_referrers_to_retain,),
    ).fetchall()
    expired = [r["gap_id"] for r in rows]
    db.conn.execute("BEGIN")
    try:
        for gid in expired:
            db.conn.execute(
                "UPDATE gaps SET status='closed', evidence_ref=? WHERE gap_id=?",
                (f"expired:low-signal@{now}", gid),
            )
    except Exception:
        db.conn.execute("ROLLBACK")
        raise
    else:
        db.commit()
    return expired


def close_gap_on_ingest(db: DB, resolving_ref: str, now: Optional[str] = None,
                        seq: Optional[int] = None) -> list[str]:
    """Loop-closure: after fresh raw content is absorbed, any OPEN gap that the detectors no longer
    report has been resolved by that ingest. Close it and stamp a gap->raw back-reference
    (`resolving_ref`) so the closed entry reads as a proposed [x] pointing at the raw that filled
    it. Returns the closed gap_ids (gap_id-sorted)."""
    now = now or now_iso()
    still_open: set[str] = set()
    for fn in DETECTORS:
        for g in fn(db):
            still_open.add(g["gap_id"])
    rows = db.conn.execute(
        "SELECT gap_id FROM gaps WHERE status='open' ORDER BY gap_id"
    ).fetchall()
    closed = [r["gap_id"] for r in rows if r["gap_id"] not in still_open]
    db.conn.execute("BEGIN")
    try:
        for gid in closed:
            db.conn.execute(
                "UPDATE gaps SET status='closed', evidence_ref=?, last_seen_seq=COALESCE(?,last_seen_seq) "
                "WHERE gap_id=?",
                (f"closed-by-ingest:{resolving_ref}", seq, gid),
            )
    except Exception:
        db.conn.execute("ROLLBACK")
        raise
    else:
        db.commit()
    return closed
