"""STEP 4 - REFINE / ABSTAIN escalation schedule (CRAG-grader shape; vault-escalation, not web).

On a non-GROUNDED verdict with budget remaining, escalate DEEPER each round: widen retrieval,
raise the PPR restart weight toward the seed, and finally pull the raw block LAST. Exhausted ->
ABSTAIN with the specific unmet predicate.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RefineStep:
    k: int              # retrieval breadth
    alpha: float        # PPR restart weight (higher hugs the seed)
    pull_raw: bool      # pull the raw cited block directly as a last resort


def schedule(budget: int) -> list[RefineStep]:
    steps = [RefineStep(k=50, alpha=0.15, pull_raw=False),
             RefineStep(k=100, alpha=0.25, pull_raw=False),
             RefineStep(k=200, alpha=0.40, pull_raw=True)]
    return steps[: max(1, budget)]
