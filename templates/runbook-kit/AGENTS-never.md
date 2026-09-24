- Never edit `check_runbook.py`, `runbook.schema.json` or `FORMAT.md` to make a runbook pass;
  our machine checks with its own copy.
- Never run the runbook's stages or plugin scripts while forging; the checker does not need them
  to run.
