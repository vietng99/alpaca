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

Format {{FORMAT}} in short (`<kit>/FORMAT.md` has it in full):

- Edge cases are items. In a spec-kit spec, number each edge case `EC-001`, `EC-002`, ... under
  its Edge Cases heading, and cover each one like a success criterion. An edge case bullet with
  no id is `EC-UNNUMBERED`. Never change the id of an edge case that has one.
- A known failure can say how it is recognized and what to do then: `detect` (a check that
  passes when the failure happened) and `then` (`retry`, `stop`, `ask-owner`, or
  `{run: <stage id>}`, which sends to a recovery stage: a stage with `recovery: true` that runs
  only then). A fail case that answers an edge case lists it in its `covers`.
- Give a knob, a check, a fail case and an owner gate a `source` when you know where it comes
  from: `spec:SC-002` for a spec item, `default` for a default you chose.

The steps below are the ones our own agents follow.
