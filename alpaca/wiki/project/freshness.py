"""project.freshness - the FRESH/STALE gate (dm.9).

`check` regenerates every compiled artifact IN MEMORY and byte-diffs it against what is on disk,
modulo the dm.6 volatile-line normalizer. Any divergence (or a missing artifact) is STALE, and
STALE is an ERROR: `check(...).exit_code == 1` for the CLI, and `assert_fresh` FAIL-CLOSES on the
answer path so a hand-edited or drifted artifact can never silently back a served answer.

The regeneration is the trusted source; disk is the untrusted copy. We never trust disk to self-
report freshness - we recompute and compare, so the gate cannot rot by discipline.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from . import artifacts as A


class StaleArtifactError(RuntimeError):
    """Raised on the answer/boot path when a compiled artifact does not match the DB (fail-closed)."""


class StaleSnapshotError(StaleArtifactError):
    """Ring-0 boot gate: the snapshot is absent or does not match current DB state."""


# name -> in-memory regenerator (build with write=False so disk is untouched during a check)
_ARTIFACTS = {
    A.MAP_NAME: lambda cfg: A.build_map(cfg, write=False),
    A.EDGES_NAME: lambda cfg: A.build_edges(cfg, write=False),
    A.SNAPSHOT_NAME: lambda cfg: A.build_snapshot(cfg, now=None, write=False),
    A.GAPS_NAME: lambda cfg: A.build_gaps(cfg, write=False),
}


@dataclass
class FreshnessResult:
    fresh: bool
    stale: list[str] = field(default_factory=list)   # artifact names that diverged / are missing

    @property
    def exit_code(self) -> int:
        return 0 if self.fresh else 1


def _disk(cfg: Config, name: str) -> str | None:
    p = cfg.projection_dir / name
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def check(cfg: Config) -> FreshnessResult:
    """Regenerate each artifact in memory and byte-diff (normalized) against disk. STALE => error."""
    stale: list[str] = []
    for name, regen in sorted(_ARTIFACTS.items()):
        on_disk = _disk(cfg, name)
        in_mem = regen(cfg)
        if on_disk is None or A.normalize_for_freshness(on_disk) != A.normalize_for_freshness(in_mem):
            stale.append(name)
    return FreshnessResult(fresh=(not stale), stale=stale)


def is_fresh(cfg: Config) -> bool:
    return check(cfg).fresh


def assert_fresh(cfg: Config) -> None:
    """FAIL-CLOSED guard for Engine.answer: raise unless every artifact matches the DB."""
    res = check(cfg)
    if not res.fresh:
        raise StaleArtifactError("stale compiled artifacts: " + ", ".join(res.stale))


def snapshot_current(cfg: Config) -> bool:
    """dm.6 Ring-0 test: does snapshot.md on disk match a freshly-derived snapshot (modulo volatile)?"""
    on_disk = _disk(cfg, A.SNAPSHOT_NAME)
    if on_disk is None:
        return False
    in_mem = A.build_snapshot(cfg, now=None, write=False)
    return A.normalize_for_freshness(on_disk) == A.normalize_for_freshness(in_mem)


def assert_current_snapshot(cfg: Config) -> None:
    """dm.6 boot gate: a stale/absent snapshot cannot silently serve - fail-closed."""
    if not snapshot_current(cfg):
        raise StaleSnapshotError("snapshot.md is absent or stale vs current DB state")
