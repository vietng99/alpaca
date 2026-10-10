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


# DEMAND GATE (architecture.md 8.2). The PPR arm needs a graph to walk: with no edge the walk is the
# lexical seed with extra steps, so the arm is refused and retrieval stays lexical, with the trace
# note `graph-empty`. The count is the as-of active edges the walk would read (store/read.py
# `ppr_edge_count`: the four-axis clause, the compartment and the visible blocks of the question),
# so a store whose edges were all retired, retracted or quarantined counts zero. Deterministic and
# total (a hollowed gate that always returns True would let a walk run on an empty graph and flips
# the demand-gate test).
GRAPH_EVENT_FLOOR = 3


def graph_demand_met(active_edge_count: int, floor: int = GRAPH_EVENT_FLOOR) -> bool:
    """True iff the as-of graph holds at least `floor` active edges, enough for a walk to mean
    something. Below it the demand gate refuses the arm; retrieval keeps the lexical seed."""
    return int(active_edge_count) >= int(floor)
