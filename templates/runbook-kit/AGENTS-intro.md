# Runbook forge: instructions for the agent

You are writing a runbook in the Alpaca runbook format, version {{FORMAT}}, from a spec. The
person who gave you this task will send your runbook to a machine that checks it with the same
checker you have here. Your work is done when that checker passes and the files are delivered.

In these steps, `<kit>` is the kit folder, named `{{KIT}}`, which holds `check_runbook.py` and
`FORMAT.md` (look for it at the root of the repository). Run
the checker as `python3 <kit>/check_runbook.py`. It needs Python 3.9 or later and PyYAML; when
PyYAML is missing it says so and exits 65 (install it with `python3 -m pip install pyyaml`, or
ask the person to).

What the kit holds for you:

- `<kit>/FORMAT.md`: every field, check type, variable and error code of the format;
- `<kit>/example/`: a finished runbook with its spec and one plugin check script;
- `<kit>/templates/runbook.yaml`: an empty runbook to start from, with comments;
- `<kit>/runbook.schema.json`: a JSON Schema of the file, if your tools use one.

Where a value comes from, in this order: the spec first, then the person's answers. Never
choose a threshold yourself (a number a check compares against), a command or an approver. If
you cannot ask the person (you run unattended, or they do not answer), leave such an item to a
person: cover it with an owner gate whose `approve` says what must be decided, and list it under
"open questions" in your report. You may choose a default only for a setting that is not a pass
bar: a timeout, a port, an attempt count, a knob's starting value or range. Put a comment on
every value you choose, on its line (`# default chosen by the agent: <why>`), and list it in the
report.

The steps below are the ones our own agents follow.
