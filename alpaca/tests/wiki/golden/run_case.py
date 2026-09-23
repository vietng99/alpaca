#!/usr/bin/env python3
"""determinism-machine.11 [upstream tests/golden/run_case.py] - the byte-pinned golden CLI case.

Ingests a FIXED synthetic vault under a FIXED clock, answers a FIXED question through the one door,
and prints ONLY the answer's `determinism_hash` on stdout. The whole point is that this line is
byte-identical across the environment matrix (PYTHONHASHSEED / LC_ALL / OMP_NUM_THREADS) and across
decades - hashing uses SHA-256 (never builtin hash()) and integer bands (never raw floats), so no
env knob can perturb it. tests/wiki/test_lint.py pins each case's line and runs the env matrix.

Adapted to the Alpaca tree: `rune2` -> `alpaca.wiki`, and the repo root is three levels up from here. The
write path `alpaca.wiki.ingest.absorb` is vendored in M2.14; until then this runner cannot execute and
the golden-pin test in tests/wiki/test_lint.py skips its cross-environment matrix.

Usage:  python3 tests/wiki/golden/run_case.py <case_name>
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from alpaca.wiki.clock import FixedClock          # noqa: E402
from alpaca.wiki.config import Config             # noqa: E402
from alpaca.wiki.engine.answer import answer      # noqa: E402
from alpaca.wiki.ingest.absorb import Absorber    # noqa: E402

# Frozen cases. Editing a question or a doc here re-pins the golden (a deliberate, reviewable act).
CASES: dict[str, dict] = {
    "alice_works": {
        "docs": [("alice.md", "[[Alice]] works at [[Acme]].\n")],
        "question": "Where does Alice work?",
    },
    "vn_binh_works": {
        "docs": [("binh.md", "[[Binh]] works at [[Fpt]].\n")],
        "question": "Where does Binh work?",
    },
    "unknown_abstains": {
        "docs": [("alice.md", "[[Alice]] works at [[Acme]].\n")],
        "question": "Where does Zeus work?",
    },
}


def run(case_name: str) -> str:
    case = CASES[case_name]
    d = Path(tempfile.mkdtemp(prefix="golden_"))
    try:
        cfg = Config.for_vault(d)
        a = Absorber(cfg, clock=FixedClock(start="2020-01-01T00:00:00+00:00"))
        try:
            for doc_id, text in case["docs"]:
                a.absorb_text(doc_id, text)
        finally:
            a.close()
        ans = answer(cfg, case["question"])
        return ans.extra["determinism_hash"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    print(run(sys.argv[1]))
