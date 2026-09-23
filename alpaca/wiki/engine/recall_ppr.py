"""The PPR recall arm (the grafted recall engine, §5 GRAPH).

Pure: receives the seed + as-of adjacency + block/entity bridges already fetched by retrieve (so
this module never touches the guarded store surface) and returns deterministic passage scores.
Personalized PageRank math lives in determinism.ppr (fixed order/dtype/cap/epsilon -> reproducible).
"""
from __future__ import annotations

from ..determinism import ppr


def compute_passage_scores(
    seed: dict[str, float],
    edges: list[tuple[str, str, float]],
    block_ids: list[str],
    block_nodes: dict[str, list[str]],
    alpha: float = 0.15,
    iters: int = 40,
    epsilon: float = 1e-6,
) -> dict[str, float]:
    """passage score = r(block) + Σ_entity r(entity)·node_blocks.weight (weight folded as 1.0)."""
    r = ppr(edges, seed, alpha=alpha, iters=iters, epsilon=epsilon)
    scores: dict[str, float] = {}
    candidate_blocks = set(block_ids) | {b for b in r if "#" in b}
    for b in candidate_blocks:
        s = r.get(b, 0.0)
        for n in block_nodes.get(b, []):
            s += r.get(n, 0.0)
        if s > 0:
            scores[b] = s
    return scores


# Alpaca M2.11 DEMAND GATE (spec 5.9:527-528). The PPR graph arm is OFF by default (the NARROW profile
# ships vector + graph disabled); even when a caller opts the graph in, a graph is NOT built below
# the EVENT FLOOR. Too few anchored blocks make a personalized-PageRank walk pure noise, so the
# gate refuses and retrieval stays on the lexical seed alone. The floor is a count of active,
# graph-eligible blocks; below it, no graph. Deterministic and total (a hollowed gate that always
# returns True would let a graph build on an empty store and flips the demand-gate test).
GRAPH_EVENT_FLOOR = 3


def graph_demand_met(active_block_count: int, floor: int = GRAPH_EVENT_FLOOR) -> bool:
    """True iff the store carries enough anchored blocks to make a graph walk meaningful. Below the
    event floor the demand gate refuses to build a graph; retrieval falls back to the lexical seed."""
    return int(active_block_count) >= int(floor)
