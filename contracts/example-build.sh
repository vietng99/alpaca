#!/bin/sh
# Example build-phase contract. Copy into contracts/build/ and replace the body with a
# real check. A contract is human-owned and reports under the verdict contract:
#   exit 0  PASS     the check holds
#   exit 1  FAIL     the check does not hold
#   exit 2  BLOCKED  the check could not adjudicate (missing input, broken tool)
#   exit 3  PAUSED   a decision is owed before this can pass
# The runner (alpaca/contracts_runner.py) executes every executable under contracts/<phase>/
# and folds these codes with worst() (BLOCKED > FAIL > PAUSED > PASS).
#
# This generic example asserts the package byte-compiles. Keep every contract pure ASCII.
set -eu

if python3 -m compileall -q alpaca >/dev/null 2>&1; then
    echo "GATE example-build: PASS"
    exit 0
fi

echo "GATE example-build: FAIL"
echo "  reason: alpaca/ did not byte-compile"
exit 1
