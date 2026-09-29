"""Deterministic tier policy and graph inputs; no ranking query or writes."""
from __future__ import annotations
from typing import Any
from ..determinism import nfc, to_band
TOP_BAND = 3
BAND_CUTS = (0.07, 0.25, 0.55)
BRIDGE_CAP = 2
BRIDGE_MIN_INDEG = 2

def _band_for_position(pos: int, n: int) -> int:
    """Stage-A percentile band for rank position `pos` (0 = top) out of `n` nodes."""
    frac = pos / n
    if frac < BAND_CUTS[0]:
        return TOP_BAND            # 3
    if frac < BAND_CUTS[1]:
        return TOP_BAND - 1        # 2
    if frac < BAND_CUTS[2]:
        return TOP_BAND - 2        # 1
    return TOP_BAND - 3            # 0


def compute_tiers(
    node_ids: list[str],
    scores: dict[str, float],
    statuses: dict[str, str],
    in_neighbors: dict[str, set],
    updates_exempt: set,
) -> dict[str, Any]:
    """Pure tiering core (no DB). Returns {'band', 'promoted', 'demoted', 'order'}.

    `node_ids` MUST already be canonical node_id-ASC (NFC). `scores` are advisory floats; they are
    projected to INTEGER ranks via `to_band` BEFORE ordering so a 1-ULP float cannot flip a band.
    """
    n = len(node_ids)
    if n == 0:
        return {"band": {}, "promoted": set(), "demoted": set(), "order": []}

    int_rank = {nid: to_band(scores.get(nid, 0.0)) for nid in node_ids}
    # Stage-A: order by (-integer rank, node_id) BEFORE quantiling; band by percentile.
    order = sorted(node_ids, key=lambda nid: (-int_rank[nid], nid))
    band = {nid: _band_for_position(i, n) for i, nid in enumerate(order)}

    # Stage-B: bounded bridge-in promotion. Candidates are sub-top nodes with enough distinct
    # in-neighbours; promote the strongest BRIDGE_CAP of them by exactly one band.
    candidates = [nid for nid in order
                  if band[nid] < TOP_BAND and len(in_neighbors.get(nid, set())) >= BRIDGE_MIN_INDEG]
    candidates.sort(key=lambda nid: (-len(in_neighbors.get(nid, set())), nid))
    promoted: set = set()
    for nid in candidates[:BRIDGE_CAP]:
        band[nid] = min(band[nid] + 1, TOP_BAND)
        promoted.add(nid)

    # Stage-C: force-demote a superseded node OUT of tier-1 UNLESS rel == 'updates'.
    demoted: set = set()
    for nid in node_ids:
        if statuses.get(nid) != "superseded":
            continue
        if nid in updates_exempt:
            continue
        if band[nid] == TOP_BAND:
            band[nid] = TOP_BAND - 1
            demoted.add(nid)

    return {"band": band, "promoted": promoted, "demoted": demoted, "order": order}


def _graph(db) -> tuple[list[str], dict[str, str], list[tuple[str, str]], set]:
    """Read the full typed graph: (node_ids ASC, statuses, node->node active edges, updates-exempt)."""
    node_ids: list[str] = []
    statuses: dict[str, str] = {}
    for r in db.conn.execute(
        "SELECT node_id, status FROM nodes WHERE status IN ('active','superseded') ORDER BY node_id ASC"
    ):
        nid = nfc(r["node_id"])
        node_ids.append(nid)
        statuses[nid] = r["status"]
    known = set(node_ids)
    node_edges: list[tuple[str, str]] = []
    updates_exempt: set = set()
    for r in db.conn.execute(
        "SELECT subj_node, obj_node, predicate FROM edges "
        "WHERE status='active' AND obj_datatype='node' AND obj_node IS NOT NULL"
    ):
        s, o = nfc(r["subj_node"]), nfc(r["obj_node"])
        if s in known and o in known:
            node_edges.append((s, o))
            if r["predicate"] == "updates":
                updates_exempt.add(s)
                updates_exempt.add(o)
    return node_ids, statuses, node_edges, updates_exempt


