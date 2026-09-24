## Step 7: deliver

Hand the person these files, in the folder layout the runbook expects (paths in a runbook are
relative to its folder), and say which each one is:

1. `runbook.yaml`;
2. the spec it covers;
3. every plugin check script the runbook names, executable (`chmod +x`);
4. the output of the last `python3 <kit>/check_runbook.py runbook.yaml --spec spec.md --json`.

Deliver only a runbook whose last check line is `GATE alpaca-runbook-check: PASS`. If you could
not get there, say which `ERROR` lines remain and what would clear them.
