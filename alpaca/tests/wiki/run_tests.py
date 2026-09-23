#!/usr/bin/env python3
"""Zero-install test runner [upstream tests/run_tests.py]: `python3 tests/wiki/run_tests.py`.

Uses stdlib unittest discovery so the whole vendored R1-R7 property suite under tests/wiki runs
without pip installing anything (pytest also discovers these files if you prefer it). Adapted to the
Alpaca tree: the suite lives at tests/wiki (not tests), and the repo root is two levels up from here.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def main() -> int:
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests" / "wiki"), pattern="test_*.py", top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
