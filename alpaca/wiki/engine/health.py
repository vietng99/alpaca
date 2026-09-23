"""operating-surface.2 - deterministic 0-100 health score + ranked next-actions.

health = clamp(100 - Σ penalty_i, 0, 100). Every term is an INTEGER count × INTEGER weight
(hashable, replayable - no float ever enters the score). The penalty table is documented below and
frozen; changing a weight or the tie-break order flips the byte-pinned golden (os.2 mutation
target). Each penalty stores its contributing (code, count, penalty) tuple for audit/replay, and
the ≤6 ranked next-actions are single runnable commands ordered by penalty weight with a byte-order
(code-ascending) tie-break. Pending mirrors the top action.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..store.db import DB
from ..store.ledger import verify_chain
from ..store.query import all_events


@dataclass(frozen=True)
class Penalty:
    code: str
    weight: int
    cap: Optional[int]     # None == a cliff (fixed weight when triggered, no per-N scaling)
    cliff: bool
    label: str
    action: str            # a single runnable command the owner can execute


# ---------------------------------------------------------------- the frozen penalty table
# (research brief 3: per-code capped integer weights; H3/H8/H9 are cliffs.)
PENALTIES: tuple[Penalty, ...] = (
    Penalty("H1", 5, 20, False, "orphan edges (endpoint node missing)",
            "rune ingest --relink-orphans"),
    Penalty("H2", 5, 20, False, "edges with a non-live endpoint (superseded/merged node)",
            "rune refresh --superseded-endpoints"),
    Penalty("H3", 40, None, True, "broken ingest hash-chain",
            "rune verify --chain"),
    Penalty("H4", 2, 15, False, "uncorroborated CLASS-A (tier-1) facts",
            "rune lucid --corroborate-class-a"),
    Penalty("H5", 3, 15, False, "open contradictions",
            "rune lucid --contradictions"),
    Penalty("H6", 10, 30, False, "quarantined-yet-reachable edges",
            "rune lucid --quarantined"),
    Penalty("H7", 2, 10, False, "stale volatile claims served past expiry",
            "rune refresh --expired-volatile"),
    Penalty("H8", 25, None, True, "schema drift (missing required column)",
            "rune migrate --repair"),
    Penalty("H9", 20, None, True, "determinism-gate failures",
            "rune verify --determinism"),
    Penalty("H10", 1, 10, False, "VN-oracle-blind docs",
            "rune lucid --vn-oracle-blind"),
)

# required columns whose absence is schema drift (H8). Mirrors lint._REQUIRED_COLUMNS intent.
_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "edges": ("edge_id", "subj_node", "predicate", "source_block_id", "atom_type",
              "status", "corroboration_count", "edge_tier", "volatile", "expires_at"),
    "docs": ("doc_id", "kind", "domain", "private"),
    "nodes": ("node_id", "status", "page_band"),
    "blocks": ("block_id", "block_content_id", "status"),
}


@dataclass(frozen=True)
class HealthReport:
    score: int
    contributions: tuple[tuple[str, int, int], ...]   # (code, count, penalty) for ALL codes, code-sorted
    actions: tuple[str, ...]                           # ≤6 runnable commands, ranked
    pending: str                                       # mirrors the top action

    def audit(self) -> str:
        rows = [f"{c} count={n} penalty={p}" for c, n, p in self.contributions]
        return (f"health={self.score}\n" + "\n".join(rows)
                + "\npending=" + self.pending + "\n")


# ---------------------------------------------------------------- count queries
def _schema_drift(db: DB) -> int:
    for table, cols in _REQUIRED_COLUMNS.items():
        present = {r["name"] for r in db.conn.execute(f"PRAGMA table_info({table})")}
        if not set(cols).issubset(present):
            return 1
    return 0


def _counts(db: DB, now: str) -> dict[str, int]:
    c = db.conn
    orphan = c.execute(
        "SELECT COUNT(*) AS n FROM edges e WHERE e.status='active' AND ("
        "  NOT EXISTS (SELECT 1 FROM nodes n WHERE n.node_id=e.subj_node)"
        "  OR (e.obj_datatype='node' AND e.obj_node IS NOT NULL AND NOT EXISTS"
        "      (SELECT 1 FROM nodes n WHERE n.node_id=e.obj_node)))"
    ).fetchone()["n"]
    non_live = c.execute(
        "SELECT COUNT(*) AS n FROM edges e WHERE e.status='active' AND ("
        "  EXISTS (SELECT 1 FROM nodes n WHERE n.node_id=e.subj_node AND n.status!='active')"
        "  OR (e.obj_datatype='node' AND e.obj_node IS NOT NULL AND EXISTS"
        "      (SELECT 1 FROM nodes n WHERE n.node_id=e.obj_node AND n.status!='active')))"
    ).fetchone()["n"]
    ok_chain, _ = verify_chain(all_events(db))
    class_a = c.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE status='active' AND edge_tier=1 "
        "AND corroboration_count < 2"
    ).fetchone()["n"]
    contradictions = c.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE status='active' "
        "AND reconcile_verdict='contradicts' AND superseded_at IS NULL"
    ).fetchone()["n"]
    quarantined = c.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE status='quarantined'"
    ).fetchone()["n"]
    stale = c.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE status='active' AND volatile=1 "
        "AND expires_at IS NOT NULL AND expires_at < ?", (now,)
    ).fetchone()["n"]
    det_fail = int(db.get_meta("determinism_gate_failures") or 0)
    vn_blind = int(db.get_meta("vn_oracle_blind_count") or 0)
    return {
        "H1": orphan,
        "H2": non_live,
        "H3": 0 if ok_chain else 1,
        "H4": class_a,
        "H5": contradictions,
        "H6": quarantined,
        "H7": stale,
        "H8": _schema_drift(db),
        "H9": 1 if det_fail > 0 else 0,
        "H10": vn_blind,
    }


def _penalty_for(p: Penalty, count: int) -> int:
    if count <= 0:
        return 0
    if p.cliff:
        return p.weight
    return min(p.weight * count, p.cap if p.cap is not None else p.weight * count)


def health_score(db: DB, now: str = "9999-12-31T00:00:00+00:00") -> HealthReport:
    """Pure function of store state (+ an explicit `now` for the expiry comparison)."""
    counts = _counts(db, now)
    contributions: list[tuple[str, int, int]] = []
    ranked: list[tuple[int, str, str]] = []          # (penalty, code, action)
    total = 0
    for p in PENALTIES:
        n = counts[p.code]
        pen = _penalty_for(p, n)
        contributions.append((p.code, n, pen))
        total += pen
        if pen > 0:
            ranked.append((pen, p.code, p.action))
    score = max(0, min(100, 100 - total))
    # rank by penalty DESC, tie-break by code ASC (byte order) - the frozen ordering contract.
    ranked.sort(key=lambda t: (-t[0], t[1]))
    actions = tuple(a for _, _, a in ranked[:6])
    contributions.sort(key=lambda t: t[0])
    pending = actions[0] if actions else "none"
    return HealthReport(score, tuple(contributions), actions, pending)
