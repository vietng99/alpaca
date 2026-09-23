"""STEP 2 - TRAVERSE. The ONLY module permitted to import `store.read` (R1 one-door).

Gathers the four arms - symbolic-exact floor · BM25 · vector · graph-PPR - and returns
RetrievalHits carrying each channel's raw score plus the exact-floor flag. Fusion, rerank, and the
tie-break happen in engine.fuse; verification in engine.oracle. Nothing here writes.

Block-level as-of (F4, Operation-4-Litmus): every arm's block results are intersected with the
as-of-visible block set, so a past-as-of read never surfaces a block whose domain window excludes T
(the currency guard now covers block reads, not only edge reads). The fast-path (F13) genuinely
SKIPS PPR when asked.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..store import read                      # <-- the single guarded retrieval import
from ..store.asof import AsOf
from ..store.db import DB
from ..types import RetrievalHit
from .compartment import domain_filter        # os.5: the ONE data-layer compartment predicate
from .recall_ppr import compute_passage_scores

# docs.tier semantics (r1.1): LOWER = more authoritative. Blocks whose doc tier is >= RAW_TIER are
# "raw" and are admitted ONLY as the last resort - when no higher-tier block produced a candidate.
RAW_TIER = 1

# r1.2 PER-MODE WALK SHAPE. The retrieval MODE classified by plan.classify_mode (GLOBAL/LOCAL/
# DESCENT) is threaded INTO gather so the three modes walk DISTINCT shapes - NOT the identical
# ladder. Each entry is (seed_mult, ppr_iters_mult, rung_tag):
#   GLOBAL  - thematic/summary: WIDEN the lexical seed to more communities/hubs (broader breadth).
#   LOCAL   - entity single/multi-hop: keep a TIGHT neighborhood (baseline breadth/depth).
#   DESCENT - as-of / currency / delta: walk DEEPER multi-hop (more PPR propagation iterations).
# The mode branch below is the whole mechanism: collapse it (ignore the mode) and every mode
# retrieves the identical candidate/ladder shape.
_WALK_SHAPES = {
    "GLOBAL":  (2.0, 1, "global-widen"),
    "LOCAL":   (1.0, 1, "local-tight"),
    "DESCENT": (1.0, 3, "descent-deep"),
}


def _walk_shape(mode: str) -> tuple:
    """r1.2 - map a retrieval MODE to its (seed_mult, ppr_iters_mult, rung_tag) walk shape
    (deterministic, total; unknown modes fall back to the tight LOCAL shape)."""
    return _WALK_SHAPES.get(mode, _WALK_SHAPES["LOCAL"])


@dataclass
class Retrieval:
    hits: list[RetrievalHit]
    floor_ids: list[str]
    linked_nodes: list[str]
    channel_lists: dict[str, list[str]]        # channel -> ordered block ids (for RRF ranks)
    skipped_ppr: bool = False
    raw_fallthrough: bool = False              # r1.1: raw-tier blocks were admitted (no higher-tier winner)
    tiers: dict = None                         # block_id -> docs.tier (for the raw-last-resort gate)
    authorities: dict = None                   # C12: block_id -> docs.source_authority (the fuse prior)
    ladder: list = None                        # r1.2: the ordered rungs actually walked (lexical seed first)
    mode: str = "LOCAL"                        # r1.2: the GLOBAL/LOCAL/DESCENT mode that shaped this walk


def _domain_visible_blocks(db: DB, domain: str | None, all_domains: bool) -> set | None:
    """os.5 - the set of block_ids inside the reader's AUTHORIZED compartments, or None (no filter).

    Per the os.5 research-fold the reader filter is `domain = ANY(:authorized_domains)` with the
    shared `general` compartment always authorized; only the OFF-target compartments (e.g. career
    for a personal query) are excluded. `all_domains`/no-domain => None (do not restrict).
    """
    if all_domains:
        return None
    domain = domain or "general"
    ok: set = set()
    # union of {target, general} - general is the shared, always-authorized compartment
    for d in sorted({domain, "general"}):
        sql, params = domain_filter("SELECT block_id, domain FROM blocks WHERE status='active'", d)
        ok |= {r["block_id"] for r in db.conn.execute(sql, params).fetchall()}
    return ok


def _doc_marks_for(db: DB, block_ids: list[str]) -> tuple[dict, dict]:
    """r1.1 + C12 - carry the two doc marks (docs.tier, docs.source_authority) onto each candidate
    block (joined blocks -> docs). Returns (tiers, authorities).

    ONLY the two STOCK docs columns are selected. A brain vault additively carries a symbolic
    Axis-A LABEL column too, but naming that column here would raise
    `sqlite3.OperationalError: no such column` on every stock vault - `schema/schema.sql` gives
    docs `tier` and `source_authority` and nothing else - which would break retrieval for every
    store that is not a brain vault. So the symbolic-label lookup lives in the brain's own read
    door, never on the engine's retrieval path. The engine treats source_authority as an OPAQUE
    integer: it never decodes a label from it (two distinct classes may share one number).

    Fail-closed in both directions, because a missing mark must never buy rank:
      * a NULL/absent tier folds to RAW_TIER, so an unmarked doc stays raw-last-resort behind the
        r1.1 gate instead of being silently promoted past it;
      * a NULL/absent authority folds to 0 - the identical fold `oracle._source_authority` already
        applies - so an unmarked doc carries no C12 prior mass and cannot out-rank an attested one.
    """
    if not block_ids:
        return {}, {}
    marks = ",".join("?" * len(block_ids))
    rows = db.conn.execute(
        f"SELECT b.block_id AS b, d.tier AS tier, d.source_authority AS sa "
        f"FROM blocks b JOIN docs d ON d.doc_id=b.doc_id "
        f"WHERE b.block_id IN ({marks})", tuple(block_ids)
    ).fetchall()
    tiers = {r["b"]: int(r["tier"] if r["tier"] is not None else RAW_TIER) for r in rows}
    authorities = {r["b"]: int(r["sa"] if r["sa"] is not None else 0) for r in rows}
    return tiers, authorities


def gather(db: DB, providers, question: str, asof: AsOf, k: int = 50,
           alpha: float | None = None, skip_ppr: bool = False,
           domain: str | None = None, all_domains: bool = False,
           mode: str = "LOCAL", wiki_blind: bool = False,
           use_vector: bool = True) -> Retrieval:
    # r1.2: the mode SHAPES the walk before any read - GLOBAL widens the lexical seed breadth,
    # DESCENT deepens the multi-hop PPR propagation, LOCAL stays tight. Collapsing this branch makes
    # every mode walk the identical shape.
    seed_mult, iters_mult, shape_tag = _walk_shape(mode)
    seed_k = max(1, int(round(k * seed_mult)))
    qvec = providers.embedder.embed(question) if use_vector else None
    visible = read.asof_visible_blocks(db, asof)          # F4: block-level currency guard
    dom_ok = _domain_visible_blocks(db, domain, all_domains)   # os.5: compartment gate (or None)
    if dom_ok is not None:
        visible = visible & dom_ok
    if wiki_blind:
        raw_blocks = {
            r["block_id"] for r in db.conn.execute(
                "SELECT b.block_id FROM blocks b JOIN docs d ON d.doc_id=b.doc_id "
                "WHERE b.status='active' AND d.kind='raw'"
            ).fetchall()
        }
        visible &= raw_blocks

    def _vis(pairs):
        return [(b, s) for b, s in pairs if b in visible]

    # r1.2 LADDER: the lexical seed (BM25 + vector + exact floor) is gathered BEFORE any PPR/graph
    # walk. PPR restarts only from that seed - graph propagation NEVER precedes the lexical seed.
    bm25_hits = _vis(read.bm25(db, question, seed_k))
    vec_hits = _vis(read.vector(db, qvec, seed_k)) if qvec is not None else []
    linked = read.link_entities(db, question, qvec)
    visible_nodes = {
        r["node_id"] for r in db.conn.execute(
            "SELECT node_id,block_id FROM node_blocks ORDER BY node_id,block_id"
        ).fetchall() if r["block_id"] in visible
    }
    linked = [node_id for node_id in linked if node_id in visible_nodes]
    floor = [b for b in read.exact_floor(db, question, linked) if b in visible]
    ladder = ["lexical-seed"]
    # r1.2: the shape rung records WHICH mode-shape drove the seed - the ladders differ per mode.
    ladder.append(shape_tag)

    candidate = []
    seen = set()
    for bid in [b for b, _ in bm25_hits] + [b for b, _ in vec_hits] + floor:
        if bid not in seen:
            seen.add(bid); candidate.append(bid)

    meta = db.cfg.meta
    if skip_ppr:
        ppr_scores: dict[str, float] = {}
    else:
        ladder.append("graph-ppr")
        seed = read.ppr_seed(db, bm25_hits, vec_hits, linked)
        authorized_domains = None if all_domains else {domain or "general", "general"}
        edges = read.ppr_edges(
            db, asof, authorized_domains=authorized_domains, visible_blocks=visible,
        )
        block_nodes = read.node_blocks_for(db, candidate)
        ppr_scores = compute_passage_scores(
            seed, edges, candidate, block_nodes,
            alpha=float(alpha if alpha is not None else meta.get("ppr_alpha", 0.15)),
            iters=int(meta.get("ppr_iters", 40)) * iters_mult,   # r1.2: DESCENT walks deeper
            epsilon=float(meta.get("ppr_epsilon", 1e-6)),
        )
        # PPR may surface as-of-visible blocks not already in candidate
        for bid in list(ppr_scores):
            if "#" in bid and bid in visible and bid not in seen:
                seen.add(bid); candidate.append(bid)

    # ---- r1.1 RAW-TIER GATE ------------------------------------------------------------------
    # Carry BOTH doc marks onto the candidates: docs.tier drives this gate and
    # docs.source_authority rides onto each hit for the C12 bounded prior in fuse (one join, not
    # two). If any HIGHER-tier (tier < RAW_TIER) block is present,
    # hold the raw-tier blocks back (they are the last lexical resort) - EXCEPT exact-floor members,
    # which are un-overridable. Only when the higher tiers produced no candidate do raw blocks
    # fall through, which we log as raw_fallthrough for the replay trace.
    tiers, authorities = _doc_marks_for(db, candidate)
    floor_set0 = set(floor)
    higher = [b for b in candidate if tiers.get(b, RAW_TIER) < RAW_TIER]
    raw_fallthrough = False
    if higher:
        candidate = [b for b in candidate
                     if tiers.get(b, RAW_TIER) < RAW_TIER or b in floor_set0]
        seen = set(candidate)
    else:
        raw_fallthrough = bool(candidate)     # only raw-tier blocks were available -> admitted
        if candidate:
            ladder.append("raw-fallthrough")

    rows = read.block_rows(db, candidate)
    bm25_map = dict(bm25_hits)
    vec_map = dict(vec_hits)
    floor_set = set(floor)

    hits: list[RetrievalHit] = []
    for bid in candidate:
        row = rows.get(bid)
        if not row:
            continue
        ch: dict[str, float] = {}
        if bid in bm25_map:
            ch["bm25"] = bm25_map[bid]
        if bid in vec_map:
            ch["vector"] = vec_map[bid]
        if bid in ppr_scores:
            ch["ppr"] = ppr_scores[bid]
        if bid in floor_set:
            ch["exact"] = 1.0
        hits.append(RetrievalHit(
            block_id=bid, text=row["text"], channels=ch,
            page_band=int(row.get("page_bandish") or 0),
            valid_at=row.get("valid_from") or "",
            exact_floor=bid in floor_set,
            # C12: re-read from the doc row on THIS pass, never inherited from a previous hit or
            # supplied by a caller - the prior must track what the store says right now.
            authority=authorities.get(bid, 0),
            # Ordinary retrieval returns evidence for citation, not authenticated machine policy.
            # A future independently verified policy-authorization path must label its own output.
            source_trust="evidence-only",
        ))

    channel_lists = {
        "bm25": [b for b, _ in bm25_hits],
    }
    if use_vector:
        channel_lists["vector"] = [b for b, _ in vec_hits]
    if not skip_ppr:
        channel_lists["ppr"] = [b for b, _ in sorted(ppr_scores.items(), key=lambda t: (-t[1], t[0]))]
    return Retrieval(hits=hits, floor_ids=floor, linked_nodes=linked,
                     channel_lists=channel_lists, skipped_ppr=skip_ppr,
                     raw_fallthrough=raw_fallthrough, tiers=tiers, authorities=authorities,
                     ladder=ladder, mode=mode)


# ---------------------------------------------------------------- Alpaca M2.11 door passthrough (R1)
# The answer-path controller (engine/loop.py) needs the vector arm's table/extension set up, but
# under the one-door law it may not reach store.vec directly - only the read door may. This thin
# passthrough carries that setup THROUGH the door, so the controller imports engine.retrieve (an
# engine ranking module it is allowed to orchestrate) and never the raw store.vec primitive. The
# NARROW profile ships the vector arm OFF, so the controller only calls this when a caller opts the
# vector/graph profile in.
def ensure_vector_index(db: DB) -> bool:
    """Create the vector arm's fallback tables and best-effort load sqlite-vec, through the door.
    Returns True iff the sqlite-vec extension is active (False keeps the pure-stdlib fallback)."""
    from ..store import vec                     # store.vec is guarded; only the door reaches it
    vec.ensure_vec_tables(db)
    return vec.try_load_sqlite_vec(db)
