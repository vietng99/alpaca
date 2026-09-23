"""The FROZEN four-axis as-of predicate - the single builder of the currency clause (R1 + R6).

Every edge read on the answer path goes through here. The LLM cannot skip it; currency is
mechanical, not disciplinary. DOMAIN axis (valid_from/valid_until) and TRANSACTION axis
(recorded_at/superseded_at) are NEVER re-conflated. The default query fixes the transaction axis
at latest-knowledge (as-of on the domain axis only); a delta/audit query may pin both.
"""
from __future__ import annotations

from dataclasses import dataclass


# The one frozen clause. Any deviation is a currency bug; keep it in exactly one place.
# DEFAULT (K is None): domain-axis as-of T, transaction axis fixed at LATEST-KNOWLEDGE.
#   "latest knowledge" = the assertion is CURRENTLY believed => superseded_at IS NULL + active.
#   recorded_at is NOT tied to T (that would answer "what did the KB know at T", the audit path).
_ASOF_CLAUSE = (
    "{a}.valid_from <= :T "
    "AND ({a}.valid_until IS NULL OR :T < {a}.valid_until) "
    "AND {a}.superseded_at IS NULL "
    "AND {a}.status IN ('active')"
)

# AUDIT (K set): pin BOTH axes - domain window contains T, and the KB's belief AS-OF knowledge-time
#   K (recorded_at<=K<superseded_at). No current-status filter: an edge invalidated AFTER K was
#   still believed at K. Retracted poison is the one thing still excluded.
_ASOF_CLAUSE_TXN = (
    "{a}.valid_from <= :T "
    "AND ({a}.valid_until IS NULL OR :T < {a}.valid_until) "
    "AND {a}.recorded_at <= :K "
    "AND ({a}.superseded_at IS NULL OR :K < {a}.superseded_at) "
    "AND {a}.status <> 'retracted'"
)


@dataclass(frozen=True)
class AsOf:
    T: str                      # domain-axis as-of instant (ISO-8601)
    K: str | None = None        # transaction-axis knowledge instant; None => latest-knowledge

    def clause(self, alias: str = "edges") -> str:
        tmpl = _ASOF_CLAUSE_TXN if self.K is not None else _ASOF_CLAUSE
        return tmpl.format(a=alias)

    def params(self) -> dict:
        p = {"T": self.T}
        if self.K is not None:
            p["K"] = self.K
        return p


def asof_where(asof: AsOf, alias: str = "edges") -> tuple[str, dict]:
    """Return (sql_fragment, params) - append the fragment to any edge SELECT's WHERE."""
    return asof.clause(alias), asof.params()


# km.6 shelf-life on the ANSWER PATH. An edge past its expiry as-of T is NOT current knowledge:
# it is excluded from retrieval exactly like a superseded edge, not merely logged. Edges-only -
# blocks carry no expires_at, so this term is NEVER folded into the shared four-axis _ASOF_CLAUSE
# (read.py reuses that clause for the blocks table). Keep in exactly one place.
_EXPIRY_TERM = "({a}.expires_at IS NULL OR :T < {a}.expires_at)"


def asof_where_edges(asof: AsOf, alias: str = "edges") -> tuple[str, dict]:
    """Edge variant of `asof_where`: the frozen four-axis clause AND the km.6 shelf-life exclusion.

    An edge whose expires_at is at or before the as-of instant T is shelf-expired and is dropped
    from the result - an expired volatile claim is never served bare as current. The domain-axis
    instant :T (always bound) is the comparison point, mirroring valid_until (`:T < valid_until`).
    """
    clause, params = asof_where(asof, alias)
    return f"{clause} AND {_EXPIRY_TERM.format(a=alias)}", params
