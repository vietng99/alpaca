"""project - read-time renderer of the human-readable, human-CHECKABLE markdown projection FROM
the authoritative graph (D-1 Pole-B-on-top).

The projection is a VIEW, never a content source. Each rendered assertion carries its edge_id so a
correction targets the ASSERTION (fix/retract the underlying edge), and the page is regenerated -
the markdown is never the edit target. SUPERSEDED / QUARANTINED / RETRACTED assertions are badged.

Vendored from rune2/project/render.py. One declared repoint: the upstream call `ensure_vec_tables(db)`
(rune2/store/vec.py) is DROPPED. `store.vec` is a guarded ranking primitive under the alpaca.wiki
one-door law (alpaca.wiki.guards), and this read-time renderer only SELECTs nodes / edges / aliases, so
it must not reach the vector surface. `db.pour()` already establishes the full schema. The badge
glyphs below are byte-faithful upstream non-ASCII (not em dashes).
"""
from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..store.db import DB
from .artifacts import (_PUBLIC_NODE_SQL, _PUBLIC_PRIMARY_EDGE_SQL,
                        _public_aliases, _write)

_BADGE = {"active": "", "invalidated": " `⟂ SUPERSEDED`", "quarantined": " `? QUARANTINED`",
          "retracted": " `✗ RETRACTED`"}


def render(cfg: Config) -> list[Path]:
    db = DB(cfg)
    db.pour()
    written: list[Path] = []
    nodes = db.conn.execute(
        "SELECT node_id, display_name FROM nodes n WHERE status='active' "
        f"AND {_PUBLIC_NODE_SQL} ORDER BY node_id"
    ).fetchall()
    for n in nodes:
        nid = n["node_id"]
        aliases = _public_aliases(db, nid)
        display_name = aliases[0] if aliases else nid
        lines = [f"# {display_name}", "",
                 f"> Projected FROM the graph. Corrections target the **assertion** (edge_id), not this page.",
                 ""]
        edges = db.conn.execute(
            "SELECT * FROM edges e WHERE subj_node=? AND "
            f"{_PUBLIC_PRIMARY_EDGE_SQL} ORDER BY predicate, edge_id", (nid,)
        ).fetchall()
        if not edges:
            lines.append("_No assertions._")
        for e in edges:
            obj = e["obj_node"] or e["obj_literal"]
            badge = _BADGE.get(e["status"], "")
            cite = f"[{e['source_block_id']}]"
            lines.append(f"- **{e['predicate']}** → {obj}{badge}  "
                         f"<sub>edge:`{e['edge_id']}` · src:{cite} · corrob:{e['corroboration_count']}</sub>")
            if e["source_quote"]:
                lines.append(f"  > {e['source_quote']}")
        p = _write(cfg, f"{nid}.md", "\n".join(lines) + "\n")
        written.append(p)
    db.close()
    return written
