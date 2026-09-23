"""operating-surface.7 - /heartbeat + /eod rituals with the D-MATURE taint-check.

Two guards protect the personal/identity compartment from self-reinforcing hallucination:

  * `mature_personal_claim` - a personal claim graduates to durable belief ONLY on repeated USER
    assertion across >=2 distinct sessions >=7 days apart. A claim supported only by Dream's own
    diary echo (source 'dream'/'reflection') can NEVER mature - otherwise the memory would launder
    its own guesses into "facts" by re-reading its diary. This is a taint-check: the SOURCE of the
    supporting assertions, not just their count, decides.

  * `write_eod_entry` - enforces one-way link direction. An /eod (end-of-day diary) entry may link
    OUT to canonical nodes, but may never create a backref edge whose SUBJECT is a canonical node
    (that would let a diary jotting mutate the canonical graph). Direction is one-way: diary -> world.

  * `write_heartbeat_stub` - /heartbeat appends interview stubs under wiki/personal/.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..store.db import DB

# a supporting assertion from these sources is a self-echo - it can never mature a claim.
TAINTED_SOURCES = frozenset({"dream", "reflection"})
USER_SOURCE = "user"

MIN_SESSIONS = 2
MIN_DAYS_APART = 7


class EodLinkDirectionError(Exception):
    """Raised when an /eod entry attempts a backref edge onto a canonical node (wrong direction)."""


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def mature_personal_claim(db: DB, claim: dict, now: str,
                          min_sessions: int = MIN_SESSIONS,
                          min_days_apart: int = MIN_DAYS_APART) -> dict:
    """Decide whether a personal claim matures to durable belief.

    `claim` carries its supporting assertion history:
        {"claim_id": str, "assertions": [{"source": str, "recorded_at": iso, "session_id": str}, ...]}

    Matures IFF the claim is USER-asserted across >= min_sessions distinct sessions whose recorded_at
    span is >= min_days_apart days. Dream/reflection echoes are stripped BEFORE counting, so a claim
    with only diary echoes can never mature no matter how many times the diary repeats it."""
    assertions = claim.get("assertions", []) or []

    # TAINT-CHECK: only genuine USER assertions count toward maturation. Strip self-echoes first.
    user_assertions = [a for a in assertions if a.get("source") == USER_SOURCE]
    if not user_assertions:
        return {"matured": False, "claim_id": claim.get("claim_id"),
                "reason": "unsupported by any user assertion (dream/reflection echo cannot mature)"}

    sessions = {a.get("session_id") for a in user_assertions if a.get("session_id") is not None}
    if len(sessions) < min_sessions:
        return {"matured": False, "claim_id": claim.get("claim_id"),
                "reason": f"asserted in only {len(sessions)} session(s) (need >= {min_sessions})"}

    times = sorted(_parse(a["recorded_at"]) for a in user_assertions)
    spread = times[-1] - times[0]
    if spread < timedelta(days=min_days_apart):
        return {"matured": False, "claim_id": claim.get("claim_id"),
                "reason": f"sessions only {spread.days}d apart (need >= {min_days_apart}d)"}

    return {"matured": True, "claim_id": claim.get("claim_id"),
            "sessions": len(sessions), "spread_days": spread.days,
            "reason": "repeated user assertion across >=2 sessions >=7d apart"}


# diary / interview node types live in the personal compartment - they are NOT canonical graph
# entities, so an /eod entry may of course be the subject of its own outbound links.
PERSONAL_NODE_TYPES = frozenset({"eod", "personal", "diary", "heartbeat"})


def _is_canonical_node(db: DB, node_id: str) -> bool:
    """A node is canonical if it exists, is active, is its own canonical target (not a merge
    redirect), and is not a personal/diary node. Writing a backref edge whose subject is such a
    node is the forbidden direction."""
    row = db.conn.execute(
        "SELECT status, canonical_node_id, node_type FROM nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    if not row:
        return False
    if (row["node_type"] or "") in PERSONAL_NODE_TYPES:
        return False
    return row["status"] == "active" and row["canonical_node_id"] is None


def write_eod_entry(db: DB, writer, eod_node_id: str, edges: list[dict], now: str) -> dict:
    """Write an /eod diary entry's outbound links through the Writer, enforcing one-way direction.

    Every edge must originate FROM the eod entry (or another personal node), never place a canonical
    node as its SUBJECT. A backref onto a canonical node raises EodLinkDirectionError and NOTHING is
    written (the whole entry rolls back)."""
    # Direction check runs BEFORE any write so a rejected entry leaves the store untouched.
    for e in edges:
        if _is_canonical_node(db, e.get("subj_node", "")):
            raise EodLinkDirectionError(
                f"/eod entry '{eod_node_id}' may not create a backref edge ON canonical node "
                f"'{e.get('subj_node')}' - link direction is one-way (diary -> world)"
            )
    written = []
    with writer.transaction():
        for e in edges:
            e.setdefault("extractor", "reflection")
            e.setdefault("recorded_at", now)
            e.setdefault("valid_from", now)
            written.append(writer.upsert_edge(e))
    return {"eod_node_id": eod_node_id, "written": written}


def write_heartbeat_stub(cfg, prompts: list[str], date: str) -> Path:
    """/heartbeat: append interview stubs under wiki/personal/ for the owner to answer later."""
    personal = Path(cfg.vault_dir) / "wiki" / "personal"
    personal.mkdir(parents=True, exist_ok=True)
    path = personal / f"heartbeat-{date}.md"
    lines = [f"# Heartbeat {date}", ""]
    for p in prompts:
        lines.append(f"- [ ] {p}")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path
