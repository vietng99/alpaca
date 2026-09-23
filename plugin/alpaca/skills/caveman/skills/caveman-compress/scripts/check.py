#!/usr/bin/env python3
"""Compare a source and candidate without modifying either file."""
import argparse
from pathlib import Path
import sys

from validate import validate

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", type=Path)
parser.add_argument("candidate", type=Path)
args = parser.parse_args()
result = validate(args.source, args.candidate)
for issue in result.errors + result.warnings:
    print(issue)
print("Structural preservation: " + ("PASS" if not result.errors and not result.warnings else "REVIEW"))
sys.exit(0 if not result.errors and not result.warnings else 1)
