"""Configuration + meta defaults + the provider seam registry.

A shareable, content-neutral template (D-2): every path is a placeholder rooted at a vault dir
the instantiator supplies; no owner content is baked in. `rune.toml` overrides these defaults.
"""
from __future__ import annotations

import os
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


# meta.* defaults (mirrored into the DB `meta` table at pour; the deterministic knobs live here)
META_DEFAULTS: dict[str, Any] = {
    "schema_version": "3",
    "embed_model": "deterministic-hash-64",   # SEAM default: swap for a real CPU embedder
    "embed_dim": "64",
    "embed_version": "0",
    "rerank_model": "identity",               # SEAM default: swap for mxbai-rerank-v2 (0.5B CPU)
    "entail_models": "structural-only",       # SEAM default: swap for HHEM/MiniCheck/Paladin
    "ppr_alpha": "0.15",
    "ppr_iters": "40",
    "ppr_epsilon": "1e-6",
    "rrf_k": "60",
    "retrieval_profile": "narrow",
    "determinism_seed": "0",
    "sqlite_vec_version": "none",
}


@dataclass(frozen=True)
class Config:
    vault_dir: Path                     # holds raw/ (+ optional wiki/)
    db_path: Path                       # single-file rune.db
    ledger_path: Path                   # ledger/events.jsonl (co-authoritative)
    projection_dir: Path                # rendered checkable markdown projection
    # seam selection (provider names resolved by providers.registry)
    embedder: str = "deterministic"
    reranker: str = "identity"
    entailer: str = "structural"
    llm_extractor: str = "off"          # deterministic-first; 'off' keeps the template LLM-free
    # behaviour knobs
    dream_armed: bool = False           # Dream disarmed by default (ADR-043) until proven
    # op.9 Dream guardrail bounds - CONSERVATIVE DEFAULTS, owner-overridable via rune.toml. None of
    # these auto-arm anything: they only tighten/loosen the gates once an owner has set dream_armed.
    dream_blast_cap: int = 64           # HELD if a pass would change more than this many edges
    dream_min_interval_s: int = 3600    # PAUSED (throttle) if two passes run closer than this apart
    dream_kill_switch: bool = False     # force-disarm regardless of dream_armed (config-level e-stop)
    use_git: bool = False               # events.jsonl git-commit only if a repo + this flag
    refine_budget: int = 3
    meta: dict[str, Any] = field(default_factory=lambda: dict(META_DEFAULTS))

    def __post_init__(self) -> None:
        if not isinstance(self.meta, dict):
            raise TypeError("meta must be a table")
        for name in ("embedder", "reranker", "entailer", "llm_extractor"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        for name in ("dream_armed", "dream_kill_switch", "use_git"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be true or false")
        if self.use_git:
            raise ValueError("use_git is disabled: ledger and private derived state are local-only")
        _bounded_int("dream_blast_cap", self.dream_blast_cap, 0, 1_000_000)
        _bounded_int("dream_min_interval_s", self.dream_min_interval_s, 0, 31_536_000)
        _bounded_int("refine_budget", self.refine_budget, 0, 100)
        _bounded_int("meta.embed_dim", self.meta.get("embed_dim", 64), 1, 65_536)
        _bounded_int("meta.rrf_k", self.meta.get("rrf_k", 60), 1, 1_000_000)
        _bounded_int("meta.ppr_iters", self.meta.get("ppr_iters", 40), 1, 100_000)
        _bounded_float("meta.ppr_alpha", self.meta.get("ppr_alpha", 0.15), 0.0, 1.0,
                       lower_inclusive=False)
        _bounded_float("meta.ppr_epsilon", self.meta.get("ppr_epsilon", 1e-6), 0.0, 1.0,
                       lower_inclusive=False)
        if "authority_unit" in self.meta:
            _bounded_float("meta.authority_unit", self.meta["authority_unit"], 0.0, 1.0)
        profile = self.meta.get("retrieval_profile", "narrow")
        if profile not in ("narrow", "hybrid", "full"):
            raise ValueError("meta.retrieval_profile must be narrow, hybrid, or full")

    @staticmethod
    def for_vault(vault_dir: str | os.PathLike) -> "Config":
        v = Path(vault_dir).expanduser().resolve()
        return Config(
            vault_dir=v,
            db_path=v / "rune.db",
            ledger_path=v / "ledger" / "events.jsonl",
            projection_dir=v / "projection",
        )

    def providers(self):
        """The provider bundle for this vault, resolved lazily at the answer-door seam.

        M2.16 keeps every module under alpaca/wiki/engine BLIND to providers: no engine module imports a
        provider module. config.py is NOT an engine module, so it may reach the registry. The Engine
        is handed its bundle through this seam (`cfg.providers()`) from outside alpaca/wiki/engine, which
        is how M2.19 wires the answer door without breaking blindness. The import is lazy so a plain
        Config that never answers a question does not drag in the registry.
        """
        from .providers.registry import Providers
        return Providers(self)

    @staticmethod
    def load(vault_dir: str | os.PathLike, toml_path: str | os.PathLike | None = None) -> "Config":
        cfg = Config.for_vault(vault_dir)
        p = Path(toml_path) if toml_path else cfg.vault_dir / "rune.toml"
        if p.exists() and tomllib is not None:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            over: dict[str, Any] = {}
            for k in ("embedder", "reranker", "entailer", "llm_extractor",
                      "dream_armed", "dream_blast_cap", "dream_min_interval_s",
                      "dream_kill_switch", "use_git", "refine_budget"):
                if k in data:
                    over[k] = data[k]
            meta = dict(cfg.meta)
            meta.update({str(k): str(v) for k, v in data.get("meta", {}).items()})
            cfg = replace(cfg, meta=meta, **over)
        return cfg


def _bounded_int(name: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError(f"{name} must be an integer") from exc
    if str(value).strip() not in (str(parsed), f"+{parsed}"):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _bounded_float(name: str, value: Any, minimum: float, maximum: float,
                   lower_inclusive: bool = True) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError(f"{name} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    lower_ok = parsed >= minimum if lower_inclusive else parsed > minimum
    if not lower_ok or parsed > maximum:
        relation = "at least" if lower_inclusive else "greater than"
        raise ValueError(f"{name} must be {relation} {minimum} and at most {maximum}")
    return parsed
