"""Deterministic page-tiering (dm.10) and deterministic communities (km.7).

The deterministic middle assigns every entity node an INTEGER `page_band` (dm.10) and a
SNAPSHOTTED community membership (km.7). Both are computed here - never by the LLM - so a
re-run is byte-reproducible.

Fork-A discipline is load-bearing: the raw PPR float is ADVISORY ONLY and is projected to an
integer band (via `determinism.to_band`) BEFORE it can influence any ordering, and it is
EXCLUDED from every hash surface. `tier_hash`/`community_hash` fold only integers.

dm.10 `retier(db)`:
  - PPR over the full typed graph (active + superseded node-to-node edges), uniform seed.
  - Stage-A: percentile bands 7/25/55 over the INTEGER ranks (top 7% -> band 3, next to 25% ->
    band 2, next to 55% -> band 1, rest -> band 0), ordering by (-int_rank, node_id).
  - Stage-B: bounded bridge-in promotion - the BRIDGE_CAP nodes with the most distinct
    in-neighbours (and >= BRIDGE_MIN_INDEG) get a one-band promotion, capped.
  - Stage-C: a superseded node is force-demoted OUT of the top band (tier-1) UNLESS its
    supersession relationship is `predicate == 'updates'` (a version bump that keeps authority).
  - persists `nodes.page_band` (integer data) + `nodes.pagerank` (advisory float, never hashed).

km.7 `recompute_communities(db)`:
  - deterministic, pinned-seed label propagation over the undirected typed graph.
  - `community_id = min node_id in the community` (relabeling-invariant).
  - list-valued membership for straddlers (a node strongly tied to a second community).
  - persists the `communities` table; byte-identical across two runs on the same graph.
"""
from __future__ import annotations

from typing import Any

from ..clock import now_iso
from ..determinism import canonical_json, nfc, sha256_hex, to_band, ppr

# ---------------------------------------------------------------- dm.10 constants
TOP_BAND = 3                      # tier-1 == the highest integer band (stable_rank sorts page_band DESC)
BAND_CUTS = (0.07, 0.25, 0.55)    # cumulative top-fractions for bands 3 / 2 / 1 (rest -> 0)
BRIDGE_CAP = 2                    # Stage-B: at most this many bridge-in promotions
BRIDGE_MIN_INDEG = 2             # Stage-B: a bridge candidate needs >= this many distinct in-neighbours

# ---------------------------------------------------------------- km.7 constants
LPA_MAX_ITERS = 100              # fixed iteration budget for label propagation
STRADDLE_MIN = 2                 # a node joins a 2nd community if >= this many neighbours sit in it
COMMUNITY_ALGO = "lpa-min-label-v1"

# km.7 versioned-event / hub-map frozen domain tags (Fork-A: a hash surface is tag + JCS(fields)).
TIER_EVENT_TAG = "km7.tierevent\x00"
HUB_TAG = "km7.hubmap\x00"
EVENT_GENESIS = "km7.genesis"    # prev_hash anchor for the first event in the chain


# ================================================================ km.7 : versioned tier/community events

def _last_event(db) -> tuple[int, str]:
    """(seq, event_hash) of the newest tier_event, or (0, GENESIS) if the ledger is empty."""
    r = db.conn.execute(
        "SELECT event_seq, event_hash FROM tier_events ORDER BY event_seq DESC LIMIT 1"
    ).fetchone()
    if r is None:
        return 0, EVENT_GENESIS
    return int(r["event_seq"]), r["event_hash"]


def _emit_tier_event(db, node_id: str, kind: str, old: str | None, new: str | None) -> str:
    """Append ONE versioned, hash-chained change event. seq is monotonic; event_hash folds the
    prior hash (a chain) so the ledger is tamper-evident. recorded_at is advisory (never hashed)."""
    seq, prev = _last_event(db)
    seq += 1
    payload = canonical_json({"seq": seq, "node_id": nfc(node_id), "kind": kind,
                              "old": old, "new": new, "prev": prev})
    h = sha256_hex(TIER_EVENT_TAG + payload)
    db.conn.execute(
        "INSERT INTO tier_events(event_seq,node_id,kind,old_value,new_value,prev_hash,event_hash,recorded_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (seq, node_id, kind, old, new, prev, h, now_iso()),
    )
    return h


def read_tier_events(db, node_id: str | None = None) -> list[dict]:
    """Lineage reader: the append-only change history (optionally for one node), seq-ordered."""
    if node_id is None:
        rows = db.conn.execute(
            "SELECT event_seq, node_id, kind, old_value, new_value, prev_hash, event_hash "
            "FROM tier_events ORDER BY event_seq ASC").fetchall()
    else:
        rows = db.conn.execute(
            "SELECT event_seq, node_id, kind, old_value, new_value, prev_hash, event_hash "
            "FROM tier_events WHERE node_id=? ORDER BY event_seq ASC", (nfc(node_id),)).fetchall()
    return [dict(r) for r in rows]


# ================================================================ dm.10 : page-tiering

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


def retier(db, autocommit: bool = True) -> dict[str, int]:
    """Run PPR + Stage-A/B/C, PERSIST `nodes.page_band` (+ advisory `pagerank`). Returns node->band.

    Fires on full rebuild AND incrementally from absorb; page_band is what `stable_rank` and map.md
    read as a real tier.
    """
    node_ids, statuses, node_edges, updates_exempt = _graph(db)
    if not node_ids:
        return {}

    # in-neighbour sets (distinct sources) for Stage-B bridge detection.
    in_neighbors: dict[str, set] = {nid: set() for nid in node_ids}
    for s, o in node_edges:
        in_neighbors[o].add(s)

    # PPR: uniform restart over every node (seed = uniform), damping 0.85 == teleport 0.15.
    ppr_edges = [(s, o, 1.0) for (s, o) in node_edges]
    seed = {nid: 1.0 for nid in node_ids}
    scores = ppr(ppr_edges, seed)

    result = compute_tiers(node_ids, scores, statuses, in_neighbors, updates_exempt)
    band = result["band"]

    # prior persisted bands so a VERSIONED tier-change event fires only on an actual band change
    # (a no-op re-tier emits nothing; NULL prior == first assignment).
    prior_band = {
        nfc(r["node_id"]): (None if r["page_band"] is None else int(r["page_band"]))
        for r in db.conn.execute("SELECT node_id, page_band FROM nodes")
    }

    for nid in node_ids:
        old_band = prior_band.get(nid)
        new_band = band[nid]
        if old_band != new_band:
            _emit_tier_event(db, nid, "tier",
                             None if old_band is None else str(old_band), str(new_band))
        db.conn.execute(
            "UPDATE nodes SET page_band=?, pagerank=? WHERE node_id=?",
            (band[nid], float(scores.get(nid, 0.0)), nid),
        )
    if autocommit:
        db.commit()
    return band


def tier_hash(db) -> str:
    """Fold the PERSISTED integer tiers into one digest. Fork-A: ONLY page_band (never the float)."""
    rows = db.conn.execute(
        "SELECT node_id, page_band, pagerank FROM nodes "
        "WHERE status IN ('active','superseded') ORDER BY node_id ASC"
    ).fetchall()
    surface = {nfc(r["node_id"]): int(r["page_band"] if r["page_band"] is not None else -1)
               for r in rows}
    return sha256_hex("dm10.tier\n" + canonical_json(surface))


# ================================================================ km.7 : deterministic communities

def label_propagation(node_ids: list[str], adjacency: dict[str, set]) -> dict[str, str]:
    """Deterministic label propagation. `node_ids` canonical ASC; `adjacency` undirected neighbour sets.

    Async sweep in canonical node order; a node adopts the neighbour label with the highest count,
    ties broken by SMALLEST label (deterministic). After convergence each label is remapped to the
    MIN node_id among its members so `community_id` is relabeling-invariant.
    """
    labels: dict[str, str] = {nid: nid for nid in node_ids}
    for _ in range(LPA_MAX_ITERS):
        changed = False
        for nid in node_ids:                         # canonical order -> deterministic async sweep
            nbrs = adjacency.get(nid, set())
            if not nbrs:
                continue
            counts: dict[str, int] = {}
            for m in nbrs:
                counts[labels[m]] = counts.get(labels[m], 0) + 1
            best = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if labels[nid] != best:
                labels[nid] = best
                changed = True
        if not changed:
            break

    # remap: community_id = MIN node_id among members sharing a label (relabeling-invariant).
    rep: dict[str, str] = {}
    for nid in node_ids:
        lab = labels[nid]
        if lab not in rep or nid < rep[lab]:
            rep[lab] = nid
    return {nid: rep[labels[nid]] for nid in node_ids}


def _memberships(node_ids: list[str], adjacency: dict[str, set],
                 community: dict[str, str]) -> dict[str, list[str]]:
    """Primary community + straddler extras (list-valued): a node joins a 2nd community when
    >= STRADDLE_MIN of its neighbours live there."""
    out: dict[str, list[str]] = {}
    for nid in node_ids:
        mine = community.get(nid)
        joined = {mine} if mine is not None else set()
        other_counts: dict[str, int] = {}
        for m in adjacency.get(nid, set()):
            c = community.get(m)
            if c is not None and c != mine:
                other_counts[c] = other_counts.get(c, 0) + 1
        for c, cnt in other_counts.items():
            if cnt >= STRADDLE_MIN:
                joined.add(c)
        out[nid] = sorted(joined)
    return out


def _hub_summaries(node_ids: list[str], adjacency: dict[str, set],
                   community: dict[str, str]) -> dict[str, dict]:
    """Build the non-blank HUB-MAP summary for every community (keyed by min-node label).

    The hub is the member with the most in-community neighbours (INTEGER degree - never a float on
    a hash surface), tie-broken by node_id ASC so the choice is byte-deterministic. The summary is a
    human one-liner that is never blank for a real community.
    """
    members_by_comm: dict[str, list[str]] = {}
    for nid in node_ids:
        members_by_comm.setdefault(community[nid], []).append(nid)

    out: dict[str, dict] = {}
    for label, mem in members_by_comm.items():
        members = sorted(mem)
        # in-community degree only (neighbours that share this community) -> the hub is central HERE.
        comm_set = set(members)
        deg = {m: len(adjacency.get(m, set()) & comm_set) for m in members}
        hub = min(members, key=lambda m: (-deg[m], m))
        size = len(members)
        summary = f"hub={hub} · size={size} · members={', '.join(members)}"
        out[label] = {"hub": hub, "size": size, "members": members, "summary": summary}
    return out


def _old_memberships(db) -> dict[str, str]:
    """Prior persisted membership per node as canonical-JSON of its sorted community list."""
    grouped: dict[str, list[str]] = {}
    for r in db.conn.execute("SELECT node_id, label FROM communities"):
        grouped.setdefault(nfc(r["node_id"]), []).append(r["label"])
    return {nid: canonical_json(sorted(labs)) for nid, labs in grouped.items()}


def read_hub_summaries(db) -> dict[str, dict]:
    """Lineage/answer-path reader: persisted HUB-MAP summaries keyed by community label."""
    rows = db.conn.execute(
        "SELECT label, hub_node, size, summary FROM community_hubs ORDER BY label ASC").fetchall()
    return {r["label"]: {"hub": r["hub_node"], "size": int(r["size"]), "summary": r["summary"]}
            for r in rows}


def hub_hash(db) -> str:
    """Fold the persisted HUB-MAP into one digest (integers + NFC strings only; recorded_at excluded)
    so a hub rebuild is provably byte-identical across two runs."""
    rows = db.conn.execute(
        "SELECT label, hub_node, size, summary FROM community_hubs ORDER BY label ASC").fetchall()
    surface = [{"label": nfc(r["label"]), "hub": nfc(r["hub_node"] or ""),
                "size": int(r["size"]), "summary": nfc(r["summary"])} for r in rows]
    return sha256_hex(HUB_TAG + canonical_json(surface))


def recompute_communities(db) -> dict[str, list[str]]:
    """Deterministic communities over the typed graph; PERSIST the `communities` snapshot.

    Returns node_id -> sorted list of community_ids (>1 == straddler). Byte-identical across two
    runs on the same graph.
    """
    node_ids: list[str] = [nfc(r["node_id"]) for r in db.conn.execute(
        "SELECT node_id FROM nodes WHERE status='active' ORDER BY node_id ASC")]
    known = set(node_ids)
    adjacency: dict[str, set] = {nid: set() for nid in node_ids}
    for r in db.conn.execute(
        "SELECT subj_node, obj_node FROM edges "
        "WHERE status='active' AND obj_datatype='node' AND obj_node IS NOT NULL"
    ):
        s, o = nfc(r["subj_node"]), nfc(r["obj_node"])
        if s in known and o in known and s != o:
            adjacency[s].add(o)
            adjacency[o].add(s)                       # undirected

    community = label_propagation(node_ids, adjacency)
    memberships = _memberships(node_ids, adjacency, community)
    hubs = _hub_summaries(node_ids, adjacency, community)

    # VERSIONED community-change events: read the PRIOR membership BEFORE we overwrite it, then emit
    # one event per node whose membership actually changed (a no-op rebuild emits nothing).
    prior = _old_memberships(db)
    for nid in node_ids:
        new_val = canonical_json(memberships[nid])
        old_val = prior.get(nid)
        if old_val != new_val:
            _emit_tier_event(db, nid, "community", old_val, new_val)

    # snapshot into the communities table (comm_id = "<community>::<member>" so per-node rows are
    # unique under the comm_id PRIMARY KEY; label = community_id, node_id = member).
    db.conn.execute("DELETE FROM communities")
    for nid in node_ids:
        for cid in memberships[nid]:
            db.conn.execute(
                "INSERT OR REPLACE INTO communities(comm_id, node_id, label, level) VALUES(?,?,?,?)",
                (f"{cid}::{nid}", nid, cid, 0),
            )

    # snapshot the non-blank HUB-MAP summary per community (rebuilt byte-deterministically).
    now = now_iso()
    db.conn.execute("DELETE FROM community_hubs")
    for label in sorted(hubs):
        h = hubs[label]
        db.conn.execute(
            "INSERT OR REPLACE INTO community_hubs(label, hub_node, size, summary, recorded_at) "
            "VALUES(?,?,?,?,?)",
            (label, h["hub"], h["size"], h["summary"], now),
        )
    db.set_meta("community_algo_version",
                canonical_json({"algo": COMMUNITY_ALGO, "max_iters": LPA_MAX_ITERS,
                                "straddle_min": STRADDLE_MIN}))
    db.commit()
    return memberships


def community_hash(db) -> str:
    """Fold the PERSISTED partition into one digest: SHA256(tag + JCS of {member -> [community_ids]})."""
    rows = db.conn.execute(
        "SELECT node_id, label FROM communities ORDER BY node_id ASC, label ASC"
    ).fetchall()
    surface: dict[str, list[str]] = {}
    for r in rows:
        surface.setdefault(nfc(r["node_id"]), []).append(r["label"])
    for k in surface:
        surface[k].sort()
    return sha256_hex("km7.communities\n" + canonical_json(surface))
