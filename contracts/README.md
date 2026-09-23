# contracts/

Human-owned acceptance checks, one executable script per check, under the verdict
contract: exit 0 PASS, 1 FAIL, 2 BLOCKED, 3 PAUSED-FOR-DECISION. Agents read and run
these; they never write them. This is where the independence law lives in code.

## Layout

One directory per phase:

```
contracts/
  requirement/
  design/
  build/
  verify/
  release/
```

`alpaca/contracts_runner.py` runs one phase at a time: it executes every executable file
under `contracts/<phase>/` and folds their exit codes with the verdict fold `worst()`
(BLOCKED > FAIL > PAUSED > PASS). A file with no executable bit is not a contract and is
skipped. An empty or absent phase directory folds to BLOCKED: an empty universe is never a
pass. A contract that exits outside the verdict band (0..3) did not report a verdict, so it
folds as BLOCKED rather than laundering into a pass.

Run a phase's contracts:

```
python3 -m alpaca.contracts_runner build
```

## Writing a contract

Copy `example-build.sh` into the phase directory, make it executable
(`chmod +x`), and replace its body with the real check. Keep every script pure ASCII and
exit only 0, 1, 2 or 3. Below autodrive L5 an agent proposes changes here through a review
card; it does not edit these files directly (task M3.3).

Empty by default: this repo ships no committed contracts under a phase directory yet.
