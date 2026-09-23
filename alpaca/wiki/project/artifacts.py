"""project.artifacts - the compiled, byte-diffable derived artifacts (dm.4/.5/.6/.7).

Every artifact here is a deterministic VIEW of the authoritative graph, regenerable byte-for-byte
so `freshness.check` can prove disk == DB. The rules that keep them reproducible:

  * lines are emitted in an EXPLICIT canonical sort (never SQLite insertion order),
  * every string is NFC-normalized before it is written (Fork-A: the persisted form is the hashed
    form), floats never enter the ordering - only integer tiers/bands do,
  * edge endpoints are resolved through the SAME query-time resolver (`store.query.resolve_canonical`)
    used by the answer path, so the compiled graph and the answered graph cannot diverge,
  * volatile lines (Generated:/Hotness:) are emitted for humans but stripped by
    `normalize_for_freshness` before any freshness byte-diff.

These functions are pure readers; they open their own connection and never mutate the graph.
`build_gaps`' detectors are the SHARED Layer-1 gap detectors (os.8 /lucid imports them from here so
there is one implementation).

Vendored from rune2/project/artifacts.py. One declared repoint: the upstream `compile_all` rebuilt
communities via `engine.tiering.recompute_communities`. `engine.tiering` is a guarded ranking
primitive under the alpaca.wiki one-door law (alpaca.wiki.guards), so a read-time projection must not reach
it. Communities are a DERIVED index maintained on the ingest/retier WRITE path; the projection reads
them as persisted (`build_map` does a plain SELECT) and never rebuilds them here.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from ..clock import now_iso
from ..config import Config
from ..determinism import canonical_json, nfc, sha256_hex
from ..store.db import DB
from ..store.query import resolve_canonical

# frozen domain-tag prefixes (Fork-A): a hash surface is tag + NUL + JCS(fields)
GAP_TAG = "rune2/gap/v1\x00"

MAP_NAME = "map.md"
EDGES_NAME = "edges.jsonl"
SNAPSHOT_NAME = "snapshot.md"
GAPS_NAME = "gaps-candidates.md"

# lines a freshness byte-diff must ignore (wall-clock / hotness churn)
_VOLATILE_PREFIXES = ("Generated:", "Hotness:")

# A row is public only when at least one provenance path reaches a doc explicitly marked private=0.
# Missing joins, NULL labels, and unscanned rows fold to private.
def _public_doc_sql(alias: str) -> str:
    return (
        f"COALESCE({alias}.private, 1)=0 "
        f"AND COALESCE({alias}.privacy_scanned, 0)=1"
    )


_PUBLIC_PRIMARY_EDGE_SQL = (
    "EXISTS (SELECT 1 FROM blocks psb JOIN docs psd ON psd.doc_id=psb.doc_id "
    "         WHERE psb.block_id=e.source_block_id AND psb.status='active' "
    "         AND " + _public_doc_sql("psd") + ")"
)
_PUBLIC_EDGE_SQL = (
    "(" + _PUBLIC_PRIMARY_EDGE_SQL +
    " OR EXISTS (SELECT 1 FROM edge_corroborations pec "
    "            JOIN blocks pcb ON pcb.block_id=pec.source_block_id "
    "            JOIN docs pcd ON pcd.doc_id=pcb.doc_id "
    "            WHERE pec.edge_id=e.edge_id AND pcb.status='active' "
    "            AND " + _public_doc_sql("pcd") + "))"
)
_PUBLIC_NODE_SQL = (
    "(EXISTS (SELECT 1 FROM node_blocks pnb "
    "         JOIN blocks pbb ON pbb.block_id=pnb.block_id "
    "         JOIN docs pbd ON pbd.doc_id=pbb.doc_id "
    "         WHERE pnb.node_id=n.node_id AND pbb.status='active' "
    "         AND " + _public_doc_sql("pbd") + ") "
    " OR EXISTS (SELECT 1 FROM edges e "
    "            WHERE (e.subj_node=n.node_id OR e.obj_node=n.node_id) "
    "            AND e.status='active' AND e.superseded_at IS NULL AND "
    + _PUBLIC_EDGE_SQL + "))"
)


class UnsafeProjectionPath(ValueError):
    """Projection output would leave the vault or follow a symlink."""


# ---------------------------------------------------------------- shared helpers

def _open(cfg: Config) -> DB:
    db = DB(cfg)
    db.pour()  # idempotent; a years-old file just gains nothing
    return db


def _cell(s: object) -> str:
    """One map.md field: NFC, and never a '|' or newline that would break the one-line-per-page grid."""
    t = nfc("" if s is None else str(s))
    return t.replace("|", " ").replace("\n", " ").replace("\r", " ").strip()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _write(cfg: Config, name: str, content: str) -> Path:
    vault = Path(cfg.vault_dir).resolve()
    configured = Path(cfg.projection_dir)
    out = configured if configured.is_absolute() else vault / configured
    if configured.is_symlink():
        raise UnsafeProjectionPath(f"symlinked projection directory refused: {configured}")
    resolved_out = out.resolve(strict=False)
    if not _is_within(resolved_out, vault):
        raise UnsafeProjectionPath(f"projection directory escapes vault: {configured}")
    out.mkdir(parents=True, exist_ok=True)
    if out.is_symlink() or out.resolve() != resolved_out:
        raise UnsafeProjectionPath(f"symlinked projection directory refused: {configured}")

    relative = Path(name)
    if relative.is_absolute() or relative.name != name:
        raise UnsafeProjectionPath(f"invalid projection artifact name: {name}")
    p = out / name
    if p.is_symlink() or not _is_within(p.resolve(strict=False), resolved_out):
        raise UnsafeProjectionPath(f"projection target escapes output directory: {p}")

    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(out, dir_flags)
    except OSError as exc:
        raise UnsafeProjectionPath(f"unsafe projection directory refused: {out}") from exc
    try:
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        raise UnsafeProjectionPath(f"unsafe projection target refused: {p}") from exc
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(content)
    return p


def _node_is_public(db: DB, node_id: str) -> bool:
    row = db.conn.execute(
        f"SELECT 1 FROM nodes n WHERE n.node_id=? AND {_PUBLIC_NODE_SQL} LIMIT 1",
        (node_id,),
    ).fetchone()
    return row is not None


def _edge_is_public(db: DB, edge_id: str) -> bool:
    row = db.conn.execute(
        f"SELECT 1 FROM edges e WHERE e.edge_id=? AND {_PUBLIC_EDGE_SQL} LIMIT 1",
        (edge_id,),
    ).fetchone()
    return row is not None


def _public_aliases(db: DB, node_id: str) -> list[str]:
    rows = db.conn.execute(
        "SELECT a.surface FROM aliases a "
        "JOIN blocks ab ON ab.block_id=a.source_block_id "
        "JOIN docs ad ON ad.doc_id=ab.doc_id "
        "WHERE a.node_id=? AND a.status='bound' AND ab.status='active' "
        f"AND {_public_doc_sql('ad')} ORDER BY a.surface",
        (node_id,),
    ).fetchall()
    return sorted({nfc(row["surface"]) for row in rows})


def _gap_is_public(db: DB, gap: dict) -> bool:
    kind = str(gap.get("gap_type") or "")
    subject = str(gap.get("subject_key") or "")
    if kind in {"ORPHAN_NODE", "RED_LINK", "STUB"}:
        return _node_is_public(db, subject)
    if kind in {"EXPIRED_CLAIM", "STALE_HOT", "MISSING_INVERSE"}:
        return _edge_is_public(db, subject)
    if kind == "OPEN_CONTRADICTION":
        if _edge_is_public(db, subject):
            return True
        match = re.search(r"\be_[0-9a-f]{32}\b", str(gap.get("detail") or ""))
        return bool(match and _edge_is_public(db, match.group(0)))
    return False


# ---------------------------------------------------------------- dm.4  map.md

def build_map(cfg: Config, write: bool = True) -> str:
    """One deterministic pipe-delimited line per active node, ORDER BY slug.

    slug|path|type|domain|tier|community|updated|name|aliases|tags|keywords|summary
    """
    db = _open(cfg)
    try:
        nodes = db.conn.execute(
            "SELECT node_id, node_type, display_name, page_band, recorded_at, attrs, summary_block_id "
            f"FROM nodes n WHERE status='active' AND {_PUBLIC_NODE_SQL} ORDER BY node_id"
        ).fetchall()
        lines: list[str] = []
        for n in nodes:
            nid = n["node_id"]
            # aliases: bound surfaces, EXPLICITLY sorted (never the DB's return order)
            aliases = _public_aliases(db, nid)
            # domain: propagated from the node's blocks (deterministic min)
            drow = db.conn.execute(
                "SELECT b.domain AS d FROM node_blocks nb JOIN blocks b ON b.block_id=nb.block_id "
                "JOIN docs d ON d.doc_id=b.doc_id "
                f"WHERE nb.node_id=? AND {_public_doc_sql('d')} "
                "ORDER BY b.domain LIMIT 1",
                (nid,),
            ).fetchone()
            domain = drow["d"] if drow else ""
            # community snapshot (km.7), tier band (dm.10) - read as persisted, never recomputed here
            crow = db.conn.execute(
                "SELECT comm_id FROM communities WHERE node_id=? ORDER BY comm_id LIMIT 1", (nid,)
            ).fetchone()
            community = crow["comm_id"] if crow and _node_is_public(db, crow["comm_id"]) else ""
            tier = n["page_band"] if n["page_band"] is not None else ""
            try:
                attrs = json.loads(n["attrs"]) if n["attrs"] else {}
            except (TypeError, ValueError):
                attrs = {}
            tags = ",".join(sorted(_cell(t) for t in attrs.get("tags", []) if _cell(t)))
            keywords = ",".join(sorted(_cell(k) for k in attrs.get("keywords", []) if _cell(k)))
            summary = ""
            if n["summary_block_id"]:
                srow = db.conn.execute(
                    "SELECT b.text FROM blocks b JOIN docs d ON d.doc_id=b.doc_id "
                    f"WHERE b.block_id=? AND {_public_doc_sql('d')}",
                    (n["summary_block_id"],),
                ).fetchone()
                if srow and srow["text"]:
                    summary = srow["text"].splitlines()[0]
            updated = (n["recorded_at"] or "")[:10]
            public_name = aliases[0] if aliases else nid
            fields = [nid, f"{nid}.md", n["node_type"] or "", domain, tier, community,
                      updated, public_name, ",".join(aliases), tags, keywords, summary]
            lines.append("|".join(_cell(f) for f in fields))
        content = "\n".join(lines) + ("\n" if lines else "")
        content = nfc(content)
        if write:
            _write(cfg, MAP_NAME, content)
        return content
    finally:
        db.close()


# ---------------------------------------------------------------- dm.5  edges.jsonl

def build_edges(cfg: Config, write: bool = True) -> str:
    """One canonical-JSON object per active edge {from,rel,tier,to}, sorted by (from,rel,to).

    Endpoints resolve through `store.query.resolve_canonical` - the SAME resolver the answer path
    uses - so a merged-away node never leaks into the compiled graph while surviving in the query.
    """
    db = _open(cfg)
    try:
        # Pre-order is intentionally the REVERSE of the emitted (from,rel,to) key: the emitted order
        # is established SOLELY by the explicit python sort below, so removing that sort is observable.
        rows = db.conn.execute(
            "SELECT subj_node, predicate, obj_node, obj_literal, obj_datatype, edge_tier "
            f"FROM edges e WHERE status='active' AND superseded_at IS NULL AND {_PUBLIC_EDGE_SQL} "
            "ORDER BY subj_node DESC, predicate DESC, obj_node DESC"
        ).fetchall()
        objs = []
        for e in rows:
            frm = resolve_canonical(db, e["subj_node"])
            if e["obj_datatype"] == "node" and e["obj_node"]:
                to = resolve_canonical(db, e["obj_node"])
            else:
                to = e["obj_literal"] or ""
            tier = e["edge_tier"] if e["edge_tier"] is not None else 0
            objs.append({"from": nfc(frm), "rel": nfc(e["predicate"]),
                         "tier": tier, "to": nfc(to)})
        ordered = sorted(objs, key=lambda o: (o["from"], o["rel"], str(o["to"])))
        content = "".join(canonical_json(o) + "\n" for o in ordered)
        if write:
            _write(cfg, EDGES_NAME, content)
        return content
    finally:
        db.close()


# ---------------------------------------------------------------- dm.6  snapshot.md

def build_snapshot(cfg: Config, now: Optional[str] = None, write: bool = True) -> str:
    """Ring-0 boot surface. The Generated:/Hotness: lines are volatile and are stripped by
    `normalize_for_freshness` before any freshness byte-diff; every other line is DB-derived state."""
    now = now or now_iso()
    db = _open(cfg)
    try:
        def one(sql: str, params: tuple = ()) -> int:
            r = db.conn.execute(sql, params).fetchone()
            return int(r[0]) if r and r[0] is not None else 0

        docs = one(f"SELECT COUNT(*) FROM docs d WHERE {_public_doc_sql('d')}")
        blocks = one(
            "SELECT COUNT(*) FROM blocks b JOIN docs d ON d.doc_id=b.doc_id "
            f"WHERE b.status='active' AND {_public_doc_sql('d')}")
        nodes = one(
            f"SELECT COUNT(*) FROM nodes n WHERE n.status='active' AND {_PUBLIC_NODE_SQL}")
        edges = one(
            "SELECT COUNT(*) FROM edges e WHERE e.status='active' "
            f"AND e.superseded_at IS NULL AND {_PUBLIC_EDGE_SQL}")
        contradictions = one(
            "SELECT COUNT(*) FROM edges e WHERE e.reconcile_verdict='contradicts' "
            "AND e.status='active' AND e.superseded_at IS NULL "
            f"AND {_PUBLIC_EDGE_SQL}")
        expired = one(
            "SELECT COUNT(*) FROM edges e WHERE e.expires_at IS NOT NULL AND e.expires_at < ? "
            "AND e.status='active' AND e.superseded_at IS NULL "
            f"AND {_PUBLIC_EDGE_SQL}", (now,))
        gap_rows = db.conn.execute(
            "SELECT gap_type, subject_key, detail FROM gaps WHERE status='open'"
        ).fetchall()
        open_gaps = sum(_gap_is_public(db, dict(g)) for g in gap_rows)
        pending = "yes" if (contradictions or expired or open_gaps) else "no"

        body = [
            "# Rune-2 Snapshot",
            f"Generated: {now}",
            f"Hotness: {edges + blocks}",
            f"Docs: {docs}",
            f"Blocks: {blocks}",
            f"Nodes: {nodes}",
            f"Edges: {edges}",
            f"Contradictions: {contradictions}",
            f"Expired: {expired}",
            f"OpenGaps: {open_gaps}",
            f"Pending: {pending}",
        ]
        content = nfc("\n".join(body) + "\n")
        if write:
            _write(cfg, SNAPSHOT_NAME, content)
        return content
    finally:
        db.close()


def normalize_for_freshness(text: str) -> str:
    """Strip volatile lines (Generated:/Hotness:) so a freshness byte-diff sees only real state.

    This is the function the freshness hash is computed over - its dm.6 mutation is a no-op body.
    """
    kept = [ln for ln in text.splitlines()
            if not any(ln.startswith(p) for p in _VOLATILE_PREFIXES)]
    return "\n".join(kept)


# ---------------------------------------------------------------- dm.7  gaps + shared detectors

from ..engine import lucid as _lucid

DETECTORS = _lucid.DETECTORS
gap_id_for = _lucid.gap_id


def detect_orphan_nodes(db: DB, as_of: Optional[str] = None) -> list[dict]:
    return _lucid.detect_orphan_nodes(db)


def detect_gaps(db: DB, as_of: Optional[str] = None) -> list[dict]:
    """Run the one canonical /lucid detector registry without writing its ledger."""
    return _lucid.rank_gaps(_lucid.scan_gaps(db, now=as_of))


def build_gaps(cfg: Config, as_of: Optional[str] = None, write: bool = True) -> str:
    """gaps-candidates.md - one ranked G-row per detected gap. Byte-identical across two builds."""
    db = _open(cfg)
    try:
        gaps = [g for g in detect_gaps(db, as_of) if _gap_is_public(db, g)]
        lines = ["|".join([f"G-{g['gap_type']}", str(g["severity"]),
                           _cell(g["subject_key"]), _cell(g.get("detail", "")), g["gap_id"]])
                 for g in gaps]
        content = nfc("\n".join(lines) + ("\n" if lines else ""))
        if write:
            _write(cfg, GAPS_NAME, content)
        return content
    finally:
        db.close()


def _slug_tokens(question: str) -> set[str]:
    import re
    return {t for t in re.split(r"[^\w]+", nfc(question).lower()) if t}


def gap_lookup(db: DB, question: str, as_of: Optional[str] = None) -> Optional[dict]:
    """G-miss -> abstain wiring: if the question names a known gap subject, return that gap so the
    oracle can abstain honestly ('not in the vault yet') instead of inventing. Else None."""
    toks = _slug_tokens(question)
    for g in detect_gaps(db, as_of):
        subj = nfc(str(g["subject_key"])).lower()
        # match a node-slug subject appearing as a token (orphan/red-link) in the question
        parts = {p for p in subj.replace("-", " ").split() if p}
        if parts and parts <= toks:
            return g
    return None


# ---------------------------------------------------------------- compile-all convenience

def compile_all(cfg: Config, now: Optional[str] = None, as_of: Optional[str] = None) -> dict[str, Path]:
    """Write every compiled artifact to disk; return name -> path.

    REPOINT (alpaca.wiki one-door): upstream rebuilt the km.7 communities index here via
    `engine.tiering.recompute_communities`. `engine.tiering` is a guarded ranking primitive under
    alpaca.wiki.guards, so this read-time projection may not reach it. Communities are a DERIVED index
    maintained on the ingest/retier WRITE path; `build_map` reads them as persisted (a plain SELECT)
    and this compile never rebuilds them.
    """
    return {
        MAP_NAME: _write(cfg, MAP_NAME, build_map(cfg, write=False)),
        EDGES_NAME: _write(cfg, EDGES_NAME, build_edges(cfg, write=False)),
        SNAPSHOT_NAME: _write(cfg, SNAPSHOT_NAME, build_snapshot(cfg, now=now, write=False)),
        GAPS_NAME: _write(cfg, GAPS_NAME, build_gaps(cfg, as_of=as_of, write=False)),
    }
