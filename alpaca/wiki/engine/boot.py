"""operating-surface.4 - Lean Ring-0 boot snapshot with a freshness assertion.

`boot_snapshot(db) -> BootReport` is the in-process face of `snapshot.md` (dm.6): every number is
derived directly from the store, never from a cached artifact. The freshness contract is a single
hash comparison - `store_content_hash(db)` (what the store *is* right now) vs the stamp the last
apply() wrote into `meta['snapshot_source_hash']` (what the store was when the snapshot was cut). A
mismatch is a HARD `stale=True`; a missing stamp is a full-read fallback that recommends a rebuild.
A stale or absent snapshot can therefore never silently masquerade as current.

Ownership boundary (Fork-C): boot only READS. The apply() write door (os.3) is responsible for
calling `stamp_snapshot(db)` on every committed mutation so the stamp tracks the store; that wiring
is described in shared_edits and is NOT exercised by this module's tests (they stamp directly).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..determinism import canonical_json, sha256_hex
from ..store.db import DB

# frozen domain tag for the boot freshness hash surface (Fork-A: SHA-256 over NFC/JCS strings)
_BOOT_HASH_TAG = "rune2/boot/v1"
_SNAPSHOT_STAMP_KEY = "snapshot_source_hash"

# token-budget ceiling on the serialized boot context (Ring-0 must stay lean).
BOOT_CONTEXT_CEILING = 2048


# ---------------------------------------------------------------- store content hash

def store_content_hash(db: DB) -> str:
    """A deterministic digest of the store's current material state.

    Folds the row-count of every authoritative table plus the last hash-chain checksum (which moves
    on every graph-mutating event) into one SHA-256 over a canonical-JSON object. NO float ever
    enters this surface (Fork-A) - every folded value is an integer count or an opaque hex string.
    Any insert/supersede/retract changes at least one of these, so the stamp goes stale on mutation.
    """
    counts = {
        "docs": _count(db, "docs"),
        "blocks": _count(db, "blocks"),
        "nodes": _count(db, "nodes"),
        "edges": _count(db, "edges"),
        "edges_active": _count(db, "edges", "status='active'"),
        "aliases": _count(db, "aliases"),
    }
    last = db.conn.execute(
        "SELECT seq, checksum FROM ingest_event ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    surface = {
        "counts": counts,
        "last_seq": int(last["seq"]) if last else 0,
        "last_checksum": last["checksum"] if last else "",
    }
    return sha256_hex(_BOOT_HASH_TAG + "\x00" + canonical_json(surface))


def stamp_snapshot(db: DB) -> str:
    """Record the current store hash as the snapshot's source-of-truth stamp (called by apply())."""
    h = store_content_hash(db)
    db.set_meta(_SNAPSHOT_STAMP_KEY, h)
    db.commit()
    return h


# ---------------------------------------------------------------- BootReport

@dataclass(frozen=True)
class BootReport:
    pages: int                 # wiki docs
    raw: int                   # raw docs
    nodes: int                 # active entities
    edges: int                 # active assertions
    last_activity: str         # last ingest_event ts ('' if none)
    health: int                # deterministic 0..100 health score
    next_action: str           # single ranked next action
    pending: str               # Pending mirror of the top action
    stale: bool                # HARD: snapshot stamp != current store hash (or absent)
    recommend_rebuild: bool    # missing stamp -> full-read fallback + rebuild advice
    snapshot_source_hash: str  # the stamp apply() last wrote ('' if never stamped)
    store_hash: str            # freshly computed store_content_hash
    penalties: tuple = field(default_factory=tuple)   # (code, count, penalty) audit tuples

    def serialize(self) -> str:
        """Compact, deterministic Ring-0 context string (kept under BOOT_CONTEXT_CEILING)."""
        return canonical_json({
            "pages": self.pages, "raw": self.raw, "nodes": self.nodes, "edges": self.edges,
            "last_activity": self.last_activity, "health": self.health,
            "next_action": self.next_action, "pending": self.pending,
            "stale": self.stale, "recommend_rebuild": self.recommend_rebuild,
        })

    def within_budget(self, ceiling: int = BOOT_CONTEXT_CEILING) -> bool:
        return len(self.serialize().encode("utf-8")) <= ceiling


# ---------------------------------------------------------------- health (self-contained, integer)

# Documented penalty table (integer weight x integer count) - Fork-A hashable, no float.
_HEALTH_PENALTIES = (
    ("QUARANTINED_EDGE", "SELECT COUNT(*) FROM edges WHERE status='quarantined'", 5, 30),
    ("ORPHAN_NODE",
     "SELECT COUNT(*) FROM nodes n WHERE n.status='active' "
     "AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.subj_node=n.node_id OR e.obj_node=n.node_id)",
     2, 20),
    ("OPEN_GAP", "SELECT COUNT(*) FROM gaps WHERE status='open'", 3, 15),
)


def _health(db: DB) -> tuple[int, tuple]:
    score = 100
    audit = []
    for code, sql, weight, cap in _HEALTH_PENALTIES:
        try:
            n = int(db.conn.execute(sql).fetchone()[0])
        except Exception:
            n = 0
        pen = min(weight * n, cap)
        if pen:
            audit.append((code, n, pen))
        score -= pen
    return max(0, min(100, score)), tuple(audit)


# ---------------------------------------------------------------- boot

def boot_snapshot(db: DB) -> BootReport:
    """Derive the Ring-0 report from the store and assert snapshot freshness (fail-closed on stale).

    `stale=True` iff the stamp is absent OR differs from the freshly computed store hash - the sole
    freshness decision, so a mutated-but-unre-stamped store is caught (`stale=True`, not served).
    """
    store_hash = store_content_hash(db)
    stored = db.get_meta(_SNAPSHOT_STAMP_KEY)

    stale = stored is None or stored != store_hash            # <-- the freshness assertion
    recommend_rebuild = stored is None                        # missing stamp -> full-read fallback

    pages = _count(db, "docs", "kind='wiki'")
    raw = _count(db, "docs", "kind='raw'")
    nodes = _count(db, "nodes", "status='active'")
    edges = _count(db, "edges", "status='active'")

    row = db.conn.execute(
        "SELECT ts FROM ingest_event ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    last_activity = row["ts"] if row and row["ts"] else ""

    health, penalties = _health(db)
    next_action = _next_action(stale, recommend_rebuild, penalties, raw, pages)
    pending = next_action

    return BootReport(
        pages=pages, raw=raw, nodes=nodes, edges=edges, last_activity=last_activity,
        health=health, next_action=next_action, pending=pending,
        stale=stale, recommend_rebuild=recommend_rebuild,
        snapshot_source_hash=(stored or ""), store_hash=store_hash, penalties=penalties,
    )


def _next_action(stale: bool, recommend_rebuild: bool, penalties: tuple,
                 raw: int, pages: int) -> str:
    if recommend_rebuild:
        return "rune2 compile   # no snapshot stamp - full rebuild recommended"
    if stale:
        return "rune2 refresh   # snapshot is stale - re-derive and re-stamp"
    if penalties:
        top = max(penalties, key=lambda p: (p[2], p[0]))
        return f"rune2 lucid     # {top[0]} x{top[1]} (penalty {top[2]})"
    if raw and not pages:
        return "rune2 ingest    # raw present, nothing compiled yet"
    return "rune2 status     # nominal"


# ---------------------------------------------------------------- helpers

def _count(db: DB, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(db.conn.execute(sql).fetchone()[0])


__all__ = ["BootReport", "boot_snapshot", "store_content_hash", "stamp_snapshot",
           "BOOT_CONTEXT_CEILING"]
