"""alpaca.wiki.extract - emit the project wiki as a portable, standalone Rune-2 vault (M2.15).

`emit(root, out_dir, clock=now_iso) -> manifest`

Opens the project wiki at `root` (its own rune.db + ledger) and writes a standalone vault under
`out_dir`. The emitted vault:

  * opens on its own - it has its OWN schema (a fresh `pour`) and its OWN re-chained ledger, and
    references nothing under `root`;
  * carries NO absolute path or machine identifier - every recorded path is vault-relative;
  * EXCLUDES every row whose visibility label is not public - the M2.12 compartment filter is
    applied ONCE at the DATA LAYER (the `is_pushable` predicate on docs: private=0 AND
    privacy_scanned=1), never after the fact;
  * is byte-identical across two emits under a fixed clock (the manifest, the ledger and the
    projection files);
  * carries the checkable markdown projection (project.compile_all + project.render) that travels
    with the vault - the read-time view the M2.17 store-versus-projection ruling names; and
  * ships a ParityReport (alpaca.wiki.migrate.content_port): a source page whose structural edge did
    NOT survive the emit is listed UNACCOUNTED, never dropped silently.

Direction. The extract LEAVES alpaca. Nothing outside alpaca writes back into the project record; the
emitted vault is a standalone read-only copy with its own ledger. `emit` never issues a raw mutation
of its own - it copies the public rows THROUGH the one write door (store.write.Writer), so the
standalone vault's ledger is produced by the writer that owns it (alpaca.wiki.guards one-write-door).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .clock import Clock, now_iso
from .config import Config
from .determinism import canonical_json, sha256_hex
from .engine.compartment import is_pushable, pushable_where_clause
from .migrate import content_port
from .project import artifacts as _artifacts
from .project import render as _render
from .store.db import DB
from .store.ledger import Ledger
from .store.write import Writer

MANIFEST_NAME = "manifest.json"
EXTRACT_SCHEMA = "alpaca-wiki-extract/1"

# tables serialized into the standalone-vault content digest, in a fixed order
_DIGEST_TABLES = (
    ("docs", "doc_id"),
    ("blocks", "block_id"),
    ("nodes", "node_id"),
    ("aliases", "alias_id"),
    ("edges", "edge_id"),
    ("node_blocks", "node_id, block_id, role"),
    ("edge_corroborations", "edge_id, source_block_id"),
)


def _rows(conn, sql: str, params: tuple = ()) -> list:
    return conn.execute(sql, params).fetchall()


def _placeholders(items) -> str:
    return ",".join("?" * len(items))


def _public_doc_ids(src: DB) -> list[str]:
    """The public docs - the M2.12 compartment filter, composed ONCE at the data layer."""
    where = pushable_where_clause()  # COALESCE(private,1)=0 AND COALESCE(privacy_scanned,0)=1
    return [r["doc_id"] for r in _rows(
        src.conn, f"SELECT doc_id FROM docs WHERE {where} ORDER BY doc_id")]


def _reconstruct_pages(src: DB, doc_ids: list[str]) -> dict[str, str]:
    """Rebuild each public page's text from its active blocks (ordered), keyed by doc_id."""
    pages: dict[str, str] = {}
    for doc_id in doc_ids:
        blocks = _rows(
            src.conn,
            "SELECT text FROM blocks WHERE doc_id=? AND status='active' ORDER BY ordinal",
            (doc_id,),
        )
        pages[doc_id] = "\n".join(b["text"] for b in blocks)
    return pages


def _copy_public(src: DB, writer: Writer) -> dict[str, Any]:
    """Copy the public rows from `src` into the standalone vault THROUGH the write door.

    Selection is data-layer, fail-closed: a doc is copied only when it is explicitly public and
    scanned; blocks/edges/nodes/aliases ride on their public provenance. Nothing private travels.
    """
    conn = src.conn
    public_docs = _public_doc_ids(src)
    doc_ids = sorted(set(public_docs))

    # blocks under public docs (active)
    public_blocks = _rows(
        conn,
        "SELECT * FROM blocks WHERE status='active' AND doc_id IN (%s) ORDER BY block_id"
        % _placeholders(doc_ids),
        tuple(doc_ids),
    ) if doc_ids else []
    block_set = {b["block_id"] for b in public_blocks}

    # public edges: the assertion is public when its source doc is public
    public_edges = _rows(
        conn,
        "SELECT * FROM edges WHERE status='active' AND source_doc_id IN (%s) ORDER BY edge_id"
        % _placeholders(doc_ids),
        tuple(doc_ids),
    ) if doc_ids else []

    # nodes reachable from public provenance: endpoints of public edges + node_blocks on public
    # blocks. Endpoint nodes of a public assertion are public by that assertion.
    node_set: set[str] = set()
    for e in public_edges:
        if e["subj_node"]:
            node_set.add(e["subj_node"])
        if e["obj_node"]:
            node_set.add(e["obj_node"])
    block_ids = sorted(block_set)
    nb_rows = _rows(
        conn,
        "SELECT nb.node_id, nb.block_id, nb.role, nb.weight FROM node_blocks nb "
        "WHERE nb.block_id IN (%s) ORDER BY nb.node_id, nb.block_id, nb.role"
        % _placeholders(block_ids),
        tuple(block_ids),
    ) if block_ids else []
    for r in nb_rows:
        node_set.add(r["node_id"])

    node_ids = sorted(node_set)
    node_rows = _rows(
        conn,
        "SELECT * FROM nodes WHERE status='active' AND node_id IN (%s) ORDER BY node_id"
        % _placeholders(node_ids),
        tuple(node_ids),
    ) if node_ids else []
    copied_nodes = {n["node_id"] for n in node_rows}

    node_id_list = sorted(copied_nodes)
    aliases = _rows(
        conn,
        "SELECT * FROM aliases WHERE status='bound' AND node_id IN (%s) "
        "ORDER BY node_id, surface, alias_id"
        % _placeholders(node_id_list),
        tuple(node_id_list),
    ) if node_id_list else []

    with writer.transaction():
        # docs
        for doc_id in public_docs:
            d = _rows(conn, "SELECT * FROM docs WHERE doc_id=?", (doc_id,))[0]
            writer.upsert_doc(
                d["doc_id"], d["path"], d["kind"], d["content_sha256"],
                byte_len=d["byte_len"] or 0, mtime=d["mtime"] or "",
                source_authority=d["source_authority"] or 0, domain=d["domain"],
                tier=d["tier"], source_created_at=d["source_created_at"],
                private=0, sensitivity=d["sensitivity"] or "none",
            )
        # blocks (before nodes: nodes.summary_block_id may reference one)
        for b in public_blocks:
            writer.upsert_block({
                "block_id": b["block_id"], "block_content_id": b["block_content_id"],
                "occurrence_index": b["occurrence_index"],
                "chunk_ruleset_version": b["chunk_ruleset_version"],
                "doc_id": b["doc_id"], "ordinal": b["ordinal"],
                "heading_path": b["heading_path"], "char_start": b["char_start"],
                "char_end": b["char_end"], "block_sha256": b["block_sha256"],
                "block_type": b["block_type"], "context_header": b["context_header"],
                "text": b["text"], "embedded_model": b["embedded_model"],
                "valid_from": b["valid_from"], "valid_until": b["valid_until"],
                "recorded_at": b["recorded_at"], "superseded_at": b["superseded_at"],
                "status": b["status"], "domain": b["domain"],
                "source_created_at": b["source_created_at"],
            })
        # nodes
        for n in node_rows:
            summary = n["summary_block_id"] if n["summary_block_id"] in block_set else None
            try:
                attrs = json.loads(n["attrs"]) if n["attrs"] else {}
            except (TypeError, ValueError):
                attrs = {}
            writer.upsert_node(n["node_id"], node_type=n["node_type"] or "",
                               display_name=n["display_name"] or n["node_id"],
                               summary_block_id=summary, attrs=attrs)
        # aliases (source_block must be copied, else drop the anchor to keep provenance honest)
        for a in aliases:
            sb = a["source_block_id"] if a["source_block_id"] in block_set else None
            writer.add_alias(a["node_id"], a["surface"], a["norm_surface"],
                             kind=a["kind"], status=a["status"],
                             confidence=a["confidence"] if a["confidence"] is not None else 1.0,
                             source_block_id=sb)
        # node_blocks
        for r in nb_rows:
            if r["node_id"] in copied_nodes and r["block_id"] in block_set:
                writer.add_node_block(r["node_id"], r["block_id"], role=r["role"],
                                      weight=r["weight"] if r["weight"] is not None else 1.0)
        # edges (endpoints + source block must have been copied)
        for e in public_edges:
            if e["subj_node"] not in copied_nodes:
                continue
            if e["obj_datatype"] == "node" and e["obj_node"] and e["obj_node"] not in copied_nodes:
                continue
            if e["source_block_id"] not in block_set:
                continue
            writer.upsert_edge({
                "edge_id": e["edge_id"], "subj_node": e["subj_node"], "predicate": e["predicate"],
                "obj_node": e["obj_node"], "obj_literal": e["obj_literal"],
                "obj_datatype": e["obj_datatype"], "source_block_id": e["source_block_id"],
                "source_doc_id": e["source_doc_id"], "source_quote": e["source_quote"],
                "extractor": e["extractor"], "extractor_version": e["extractor_version"],
                "confidence": e["confidence"] if e["confidence"] is not None else 1.0,
                "reconcile_verdict": e["reconcile_verdict"],
                "corroboration_count": e["corroboration_count"],
                "recorded_at": e["recorded_at"], "valid_from": e["valid_from"],
                "valid_until": e["valid_until"], "status": e["status"],
                "edge_tier": e["edge_tier"], "atom_type": e["atom_type"],
                "learned_at": e["learned_at"], "source_created_at": e["source_created_at"],
                "expires_at": e["expires_at"], "volatile": e["volatile"],
            })

    return {"docs": public_docs, "blocks": public_blocks, "pages_source": _reconstruct_pages(src, public_docs)}


def _db_content_digest(db: DB) -> str:
    """A logical, container-independent digest of the standalone vault's public tables.

    The raw sqlite file bytes are an implementation detail (freelist, page order) and are NOT a
    stable identity; this digest is a canonical row-sorted serialization so two emits of the same
    logical content hash identically regardless of sqlite's physical layout.
    """
    parts: list[str] = []
    for table, order in _DIGEST_TABLES:
        rows = db.conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        serial = [{k: r[k] for k in r.keys()} for r in rows]
        parts.append(canonical_json({"table": table, "rows": serial}))
    return sha256_hex("\n".join(parts))


def _file_hashes(out_dir: Path) -> dict[str, str]:
    """Real byte hashes of every deterministic text file (ledger + projection), vault-relative.

    The sqlite container files (rune.db and its -wal/-shm sidecars) are excluded here - their
    logical content rides in `db_content_sha256` instead.
    """
    hashes: dict[str, str] = {}
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(out_dir).as_posix()
        if rel == MANIFEST_NAME:
            continue
        name = path.name
        if name == "rune.db" or name.startswith("rune.db-"):
            continue
        hashes[rel] = sha256_hex(path.read_bytes())
    return dict(sorted(hashes.items()))


def _actual_edge_tuples(db: DB) -> list[tuple]:
    rows = db.conn.execute(
        "SELECT subj_node, predicate, obj_node, source_doc_id FROM edges "
        "WHERE status='active' AND obj_node IS NOT NULL"
    ).fetchall()
    return [(r["subj_node"], r["predicate"], r["obj_node"], r["source_doc_id"]) for r in rows]


def emit(root, out_dir, clock: Clock = now_iso) -> dict:
    """Emit the wiki at `root` as a standalone portable vault under `out_dir`. Return the manifest.

    Deterministic under a fixed `clock`: the ledger, the projection files and the manifest are
    byte-identical across two emits with the same clock.
    """
    root = Path(root).expanduser().resolve()
    out_dir = Path(out_dir).expanduser().resolve()

    src_cfg = Config.for_vault(root)
    src = DB(src_cfg)
    try:
        # a fresh, empty standalone vault (own schema + own ledger)
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        out_cfg = Config.for_vault(out_dir)
        out_db = DB(out_cfg)
        try:
            out_db.pour()
            ledger = Ledger(out_cfg.ledger_path, vault_dir=out_cfg.vault_dir)
            writer = Writer(out_db, ledger, clock=clock)

            copied = _copy_public(src, writer)

            # the checkable markdown projection travels with the vault (fixed stamp for determinism)
            stamp = clock()
            _artifacts.compile_all(out_cfg, now=stamp, as_of=stamp)
            _render.render(out_cfg)

            # parity: reconcile source public pages against what survived (UNACCOUNTED, not dropped)
            actual_docs = {d for d in copied["docs"]}
            actual_nodes = {r["node_id"] for r in out_db.conn.execute(
                "SELECT node_id FROM nodes WHERE status='active'").fetchall()}
            report = content_port.reconcile(
                copied["pages_source"], actual_docs, actual_nodes,
                _actual_edge_tuples(out_db))

            counts = {
                "docs": out_db.conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
                "blocks": out_db.conn.execute(
                    "SELECT COUNT(*) FROM blocks WHERE status='active'").fetchone()[0],
                "nodes": out_db.conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE status='active'").fetchone()[0],
                "edges": out_db.conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE status='active'").fetchone()[0],
                "aliases": out_db.conn.execute(
                    "SELECT COUNT(*) FROM aliases WHERE status='bound'").fetchone()[0],
            }
            db_digest = _db_content_digest(out_db)
        finally:
            out_db.close()
    finally:
        src.close()

    manifest = {
        "schema": EXTRACT_SCHEMA,
        "schema_version": "3",
        "generated": stamp,
        "counts": counts,
        "db_content_sha256": db_digest,
        "files": _file_hashes(out_dir),
        "parity": report.as_dict(),
    }
    body = canonical_json(manifest)
    (out_dir / MANIFEST_NAME).write_text(body, encoding="utf-8")
    return manifest
