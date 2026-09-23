"""determinism-machine.8 - executed mechanical lint rules (exit 1 on any error).

Every rule here is a REAL executed query over the store (or the source tree), not an advisory
docstring. The forgeable-prose provenance guards are ported into checked gates: an edge without a
resolvable citation anchor, an alias surface bound to two canonical identities, a wikilink with no
node behind it, an enum value outside its declared domain, an edge_tier that disagrees with its
subject's page_band, a raw doc that left no ingest_event, dangling source references, raw files on
disk that were never tracked, and stamp-coverage holes. Each rule ships one violating fixture in
tests/test_dm8_lint.py; deleting a rule from RULES makes its fixture stop failing lint - the
mutation contract (dm.8 mutation target).

`cli lint` runs RULES alongside `guards.check_one_door`; any error-severity violation exits 1.
Output is byte-stable (sorted, no timestamps) so a re-run is diffable.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .guards import check_one_door
from .store.db import DB


@dataclass(frozen=True)
class Violation:
    rule: str
    severity: str          # 'error' (gating) | 'warn' (advisory, non-gating)
    target: str            # the offending id (edge_id, alias_id, doc_id, path, ...)
    message: str

    def line(self) -> str:
        return f"{self.severity.upper()} {self.rule} {self.target} :: {self.message}"


# ---------------------------------------------------------------- enum domains
_ENUM_CHECKS: list[tuple[str, str, str, tuple[str, ...], bool]] = [
    # (table, pk_column, column, allowed_values, nullable)
    ("edges", "edge_id", "atom_type", ("FACT", "TAKE", "ENTITY", "FULLTEXT"), False),
    ("edges", "edge_id", "obj_datatype", ("node", "date", "number", "string", "bool"), False),
    ("edges", "edge_id", "status", ("active", "invalidated", "quarantined", "retracted"), False),
    ("edges", "edge_id", "reconcile_verdict",
     ("novel", "refines", "contradicts", "duplicate", "matches"), True),
    ("docs", "doc_id", "domain", ("embedded", "career", "general", "personal"), False),
    ("docs", "doc_id", "kind", ("raw", "wiki"), False),
    ("blocks", "block_id", "status", ("active", "superseded"), False),
    ("nodes", "node_id", "status", ("active", "superseded", "merged"), False),
    ("aliases", "alias_id", "kind", ("exact", "wikilink", "declared", "near-advisory"), False),
    ("aliases", "alias_id", "status", ("bound", "candidate"), False),
]

# required columns per table - a missing one is schema drift (checked by rule + by health H8).
_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "edges": ("edge_id", "subj_node", "predicate", "source_block_id", "atom_type",
              "learned_at", "edge_tier", "status"),
    "docs": ("doc_id", "kind", "domain", "tier", "private", "sensitivity"),
    "blocks": ("block_id", "block_content_id", "status"),
    "nodes": ("node_id", "status", "page_band"),
}


# ---------------------------------------------------------------- rules
def rule_edge_source_block_unresolvable(db: DB, cfg: Optional[Config]) -> list[Violation]:
    rows = db.conn.execute(
        "SELECT e.edge_id, e.source_block_id FROM edges e "
        "WHERE e.source_block_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM blocks b WHERE b.block_id = e.source_block_id)"
    ).fetchall()
    return [Violation("edge-source-block-unresolvable", "error", r["edge_id"],
                      f"source_block_id {r['source_block_id']!r} resolves to no block")
            for r in rows]


def rule_edge_endpoint_node_missing(db: DB, cfg: Optional[Config]) -> list[Violation]:
    rows = db.conn.execute(
        "SELECT e.edge_id, e.subj_node, e.obj_node, e.obj_datatype FROM edges e "
        "WHERE NOT EXISTS (SELECT 1 FROM nodes n WHERE n.node_id = e.subj_node) "
        "   OR (e.obj_datatype='node' AND e.obj_node IS NOT NULL AND NOT EXISTS "
        "       (SELECT 1 FROM nodes n WHERE n.node_id = e.obj_node))"
    ).fetchall()
    return [Violation("edge-endpoint-node-missing", "error", r["edge_id"],
                      f"endpoint node missing (subj={r['subj_node']!r} obj={r['obj_node']!r})")
            for r in rows]


def rule_dangling_source_doc(db: DB, cfg: Optional[Config]) -> list[Violation]:
    rows = db.conn.execute(
        "SELECT e.edge_id, e.source_doc_id FROM edges e "
        "WHERE e.source_doc_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM docs d WHERE d.doc_id = e.source_doc_id)"
    ).fetchall()
    return [Violation("dangling-source-doc", "error", r["edge_id"],
                      f"source_doc_id {r['source_doc_id']!r} resolves to no doc")
            for r in rows]


def rule_node_block_source_dangling(db: DB, cfg: Optional[Config]) -> list[Violation]:
    """dangling-sources: a node_blocks provenance row pointing at a vanished block/node."""
    rows = db.conn.execute(
        "SELECT nb.node_id, nb.block_id FROM node_blocks nb "
        "WHERE NOT EXISTS (SELECT 1 FROM blocks b WHERE b.block_id = nb.block_id) "
        "   OR NOT EXISTS (SELECT 1 FROM nodes n WHERE n.node_id = nb.node_id)"
    ).fetchall()
    return [Violation("dangling-sources", "error", f"{r['node_id']}|{r['block_id']}",
                      "node_blocks source reference dangles")
            for r in rows]


def rule_alias_multi_canonical(db: DB, cfg: Optional[Config]) -> list[Violation]:
    """An alias surface bound to >1 canonical node - unless the ambiguity is already surfaced
    as a merge_candidate (a legitimate homonym stays two nodes; a silent double-binding does not).
    """
    def _canon(node_id: str) -> str:
        seen = set()
        cur = node_id
        while cur and cur not in seen:
            seen.add(cur)
            row = db.conn.execute(
                "SELECT canonical_node_id FROM nodes WHERE node_id=?", (cur,)).fetchone()
            if not row or not row["canonical_node_id"]:
                return cur
            cur = row["canonical_node_id"]
        return cur

    excused: set[str] = set()
    for r in db.conn.execute("SELECT node_a, node_b FROM merge_candidate"):
        excused.add(r["node_a"])
        excused.add(r["node_b"])

    surface_to_nodes: dict[str, set[str]] = {}
    for r in db.conn.execute(
            "SELECT norm_surface, node_id FROM aliases WHERE status='bound'"):
        surface_to_nodes.setdefault(r["norm_surface"], set()).add(r["node_id"])

    out: list[Violation] = []
    for surface, nodes in surface_to_nodes.items():
        canon = {_canon(n) for n in nodes}
        if len(canon) > 1 and not nodes.issubset(excused):
            out.append(Violation(
                "alias-multi-canonical", "error", surface,
                f"surface bound to {len(canon)} canonical nodes: "
                + ",".join(sorted(canon))))
    return out


def rule_wikilink_no_node(db: DB, cfg: Optional[Config]) -> list[Violation]:
    rows = db.conn.execute(
        "SELECT a.alias_id, a.node_id, a.surface FROM aliases a "
        "WHERE a.kind='wikilink' AND NOT EXISTS "
        "(SELECT 1 FROM nodes n WHERE n.node_id = a.node_id)"
    ).fetchall()
    return [Violation("wikilink-no-node", "error", str(r["alias_id"]),
                      f"wikilink {r['surface']!r} -> node {r['node_id']!r} which does not exist")
            for r in rows]


def rule_enum_domain_violations(db: DB, cfg: Optional[Config]) -> list[Violation]:
    out: list[Violation] = []
    for table, pk, col, allowed, nullable in _ENUM_CHECKS:
        placeholders = ",".join("?" * len(allowed))
        null_clause = f" AND {col} IS NOT NULL" if nullable else ""
        sql = (f"SELECT {pk} AS pk, {col} AS val FROM {table} "
               f"WHERE {col} NOT IN ({placeholders}){null_clause}")
        for r in db.conn.execute(sql, allowed).fetchall():
            out.append(Violation("enum-violation", "error", f"{table}.{col}:{r['pk']}",
                                 f"{col}={r['val']!r} outside {set(allowed)}"))
    return out


def rule_edge_tier_vs_page_band(db: DB, cfg: Optional[Config]) -> list[Violation]:
    rows = db.conn.execute(
        "SELECT e.edge_id, e.edge_tier, n.page_band FROM edges e "
        "JOIN nodes n ON n.node_id = e.subj_node "
        "WHERE e.edge_tier IS NOT NULL AND n.page_band IS NOT NULL "
        "  AND e.edge_tier != n.page_band"
    ).fetchall()
    return [Violation("edge-tier-vs-page-band", "error", r["edge_id"],
                      f"edge_tier={r['edge_tier']} != subj page_band={r['page_band']}")
            for r in rows]


def rule_raw_doc_no_ingest_event(db: DB, cfg: Optional[Config]) -> list[Violation]:
    rows = db.conn.execute(
        "SELECT d.doc_id FROM docs d WHERE d.kind='raw' AND NOT EXISTS "
        "(SELECT 1 FROM ingest_event ie WHERE ie.doc_id = d.doc_id)"
    ).fetchall()
    return [Violation("raw-doc-no-ingest-event", "error", r["doc_id"],
                      "raw doc has no ingest_event (untracked provenance)")
            for r in rows]


def rule_stamp_coverage(db: DB, cfg: Optional[Config]) -> list[Violation]:
    rows = db.conn.execute(
        "SELECT edge_id FROM edges WHERE status='active' "
        "AND (learned_at IS NULL OR learned_at='')"
    ).fetchall()
    return [Violation("stamp-coverage", "error", r["edge_id"],
                      "active edge missing learned_at stamp (km.4 coverage hole)")
            for r in rows]


def rule_block_missing_content_id(db: DB, cfg: Optional[Config]) -> list[Violation]:
    rows = db.conn.execute(
        "SELECT block_id FROM blocks WHERE block_content_id IS NULL OR block_content_id=''"
    ).fetchall()
    return [Violation("block-missing-content-id", "error", r["block_id"],
                      "block has no block_content_id (unhashable anchor)")
            for r in rows]


def rule_untracked_raws(db: DB, cfg: Optional[Config]) -> list[Violation]:
    """Raw markdown on disk under <vault>/raw that no docs row tracks."""
    if cfg is None:
        return []
    raw_dir = Path(cfg.vault_dir) / "raw"
    if not raw_dir.is_dir():
        return []
    tracked = {r["path"] for r in db.conn.execute("SELECT path FROM docs")}
    tracked_names = {Path(p).name for p in tracked}
    out: list[Violation] = []
    for f in sorted(raw_dir.rglob("*.md")):
        rel = f.relative_to(cfg.vault_dir).as_posix()
        if rel in tracked or f.name in tracked or f.name in tracked_names:
            continue
        out.append(Violation("untracked-raws", "error", rel,
                             "raw file on disk is not tracked in docs"))
    return out


def rule_one_door(db: DB, cfg: Optional[Config]) -> list[Violation]:
    ok, hits = check_one_door()
    if ok:
        return []
    return [Violation("one-door", "error", "source-tree", h) for h in hits]


# ---------------------------------------------------------------- registry
# ORDER matters only for readability; output is sorted. Deleting any entry removes a gate
# (dm.8 mutation target): its fixture then stops failing lint.
RULES: list[Callable[[DB, Optional[Config]], list[Violation]]] = [
    rule_edge_source_block_unresolvable,
    rule_edge_endpoint_node_missing,
    rule_dangling_source_doc,
    rule_node_block_source_dangling,
    rule_alias_multi_canonical,
    rule_wikilink_no_node,
    rule_enum_domain_violations,
    rule_edge_tier_vs_page_band,
    rule_raw_doc_no_ingest_event,
    rule_stamp_coverage,
    rule_block_missing_content_id,
    rule_untracked_raws,
    rule_one_door,
]


@dataclass(frozen=True)
class LintReport:
    violations: tuple[Violation, ...]

    @property
    def errors(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.severity == "error")

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0

    def text(self) -> str:
        if not self.violations:
            return "lint: 0 violations\n"
        lines = [v.line() for v in self.violations]
        lines.append(f"lint: {len(self.errors)} error(s), "
                     f"{len(self.violations) - len(self.errors)} warning(s)")
        return "\n".join(lines) + "\n"


def run_lint(db: DB, cfg: Optional[Config] = None) -> LintReport:
    found: list[Violation] = []
    for rule in RULES:
        found.extend(rule(db, cfg))
    found.sort(key=lambda v: (v.rule, v.target, v.message))
    return LintReport(tuple(found))


def cli_lint(cfg: Config) -> int:
    db = DB(cfg)
    try:
        report = run_lint(db, cfg)
        sys.stdout.write(report.text())
        return report.exit_code
    finally:
        db.close()


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    vault = argv[0] if argv else "."
    return cli_lint(Config.for_vault(vault))


if __name__ == "__main__":
    raise SystemExit(main())
