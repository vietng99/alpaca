"""operating-surface.6 - CONTEXT.md glossary wired to the alias table.

`glossary_lint(cfg, db)` parses the glossary terms out of `CONTEXT.md` and asserts each maps to a
BOUND `aliases.norm_surface` row - the SAME alias table retrieval resolves mentions against (via
`store.textnorm.norm_surface`), so the glossary and the graph cannot silently drift. A term with no
bound alias is flagged `unbacked`; a bound entity alias absent from the glossary is flagged
`undocumented`; and the doc must stay under a 300-line budget.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Config
from ..store.db import DB
from ..store.textnorm import norm_surface

CONTEXT_FILE = "CONTEXT.md"
LINE_BUDGET = 300

# A glossary term is a markdown list item whose leading token is the term:
#   - **Term**: definition        (bolded)
#   - Term - definition           (em-dash)
#   - Term: definition            (colon)
_TERM_BOLD = re.compile(r"^\s*[-*]\s+\*\*(?P<term>[^*]+?)\*\*\s*[:-]")
_TERM_PLAIN = re.compile(r"^\s*[-*]\s+(?P<term>[^:-]+?)\s*[:-]")


@dataclass(frozen=True)
class GlossaryReport:
    terms: tuple = field(default_factory=tuple)          # every parsed term (in file order)
    backed: tuple = field(default_factory=tuple)         # terms with a bound alias
    unbacked: tuple = field(default_factory=tuple)       # terms with NO bound alias
    undocumented: tuple = field(default_factory=tuple)   # bound aliases absent from the glossary
    line_count: int = 0
    within_budget: bool = True

    @property
    def ok(self) -> bool:
        return not self.unbacked and not self.undocumented and self.within_budget


def parse_terms(text: str) -> list[str]:
    """Extract glossary terms from CONTEXT.md list items (order-preserving, de-duplicated)."""
    terms: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = _TERM_BOLD.match(line) or _TERM_PLAIN.match(line)
        if not m:
            continue
        term = m.group("term").strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return terms


def _alias_exists(db: DB, norm: str) -> bool:
    """True iff a BOUND alias row shares this normalized surface (the retrieval-side alias table)."""
    row = db.conn.execute(
        "SELECT 1 FROM aliases WHERE norm_surface=? AND status='bound' LIMIT 1", (norm,)
    ).fetchone()
    return row is not None


def _bound_alias_surfaces(db: DB) -> list[tuple[str, str]]:
    """(surface, norm_surface) for every bound alias - the common-alias inventory to document."""
    rows = db.conn.execute(
        "SELECT surface, norm_surface FROM aliases WHERE status='bound'"
    ).fetchall()
    return [(r["surface"], r["norm_surface"]) for r in rows]


def glossary_lint(cfg: Config, db: DB) -> GlossaryReport:
    path = cfg.vault_dir / CONTEXT_FILE
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line_count = len(text.splitlines())
    within_budget = line_count < LINE_BUDGET

    terms = parse_terms(text)
    documented_norms = {norm_surface(t) for t in terms}

    backed: list[str] = []
    unbacked: list[str] = []
    for term in terms:
        norm = norm_surface(term)
        if _alias_exists(db, norm):              # <-- the alias-existence check (mutation target)
            backed.append(term)
        else:
            unbacked.append(term)

    # entities carrying a bound alias that the glossary never documents -> flag undocumented.
    undocumented: list[str] = []
    seen_norm: set[str] = set()
    for surface, norm in _bound_alias_surfaces(db):
        if norm in documented_norms or norm in seen_norm:
            continue
        seen_norm.add(norm)
        undocumented.append(surface)

    return GlossaryReport(
        terms=tuple(terms), backed=tuple(backed), unbacked=tuple(unbacked),
        undocumented=tuple(sorted(undocumented)),
        line_count=line_count, within_budget=within_budget,
    )


__all__ = ["glossary_lint", "GlossaryReport", "parse_terms", "CONTEXT_FILE", "LINE_BUDGET"]
