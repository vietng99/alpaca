#!/bin/sh
# Example verify-phase contract: a BEHAVIORAL probe.
#
# A behavioral probe OBSERVES the running system's behavior, not just its source: it RUNS
# the thing and checks what it does. A static read of a file is a build-phase check, not a
# verify-phase probe. Copy this into contracts/verify/ and replace the body with a probe of
# THIS project's built artifact.
#
# Reports under the verdict contract (alpaca/gates/verdict.py):
#   exit 0  PASS     the observed behavior is correct
#   exit 1  FAIL     the observed behavior is wrong
#   exit 2  BLOCKED  the probe could not run the thing (missing input, broken tool)
#   exit 3  PAUSED   a decision is owed before this can pass
#
# The runner (alpaca/contracts_runner.py) executes every executable under contracts/<phase>/ and
# folds these codes with worst() (BLOCKED > FAIL > PAUSED > PASS). Keep every contract pure
# ASCII and exit only 0, 1, 2 or 3. No em dash anywhere.
set -eu

# Drive a real behavior and observe the bytes it prints. This generic stand-in runs a
# one-line program and checks its output; replace it with the project's own command.
if ! out=$(python3 -c 'print("greeting: hello")' 2>/dev/null); then
    echo "GATE example-probe: BLOCKED"
    echo "  reason: the probed command did not run"
    exit 2
fi

if [ "$out" = "greeting: hello" ]; then
    echo "GATE example-probe: PASS"
    exit 0
fi

echo "GATE example-probe: FAIL"
echo "  reason: observed unexpected output: $out"
exit 1
