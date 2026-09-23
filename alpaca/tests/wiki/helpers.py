"""Vendored test helpers [upstream tests/helpers.py], adapted to the alpaca.wiki surface.

Upstream binds `Absorber` at import time from `rune2.ingest.absorb`. In Alpaca that write path is
vendored in a LATER task (M2.14: alpaca/wiki/ingest/absorb.py), so importing it at module load would
break every test that only needs `fresh_cfg`/`cleanup`. The Absorber import is therefore made lazy
inside `absorber`/`ingest_docs`; the two functions raise a clear error only if actually called
before the write path lands. Nothing in M2.13 calls them. Everything else is byte-faithful to
upstream with `rune2` rewritten to `alpaca.wiki`.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config


def fresh_cfg() -> Config:
    d = Path(tempfile.mkdtemp(prefix="rune2t_"))
    return Config.for_vault(d)


def cleanup(cfg: Config) -> None:
    shutil.rmtree(cfg.vault_dir, ignore_errors=True)


def _Absorber():
    # Lazy: the vendored write path (alpaca/wiki/ingest/absorb.py) lands in M2.14. Import here so a
    # caller gets a precise ImportError rather than a suite-wide collection failure.
    from alpaca.wiki.ingest.absorb import Absorber
    return Absorber


def absorber(cfg: Config, start: str = "2020-01-01T00:00:00+00:00", step: int = 0):
    return _Absorber()(cfg, clock=FixedClock(start=start, step=step))


def ingest_docs(cfg: Config, docs: list[tuple[str, str]], step: int = 0):
    """Ingest (doc_id, text) pairs with a fixed clock; returns the live Absorber (caller closes)."""
    a = absorber(cfg, step=step)
    for doc_id, text in docs:
        a.absorb_text(doc_id, text)
    return a
