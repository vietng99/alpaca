# Alpaca runbook kit, format {{FORMAT}}

This folder lets you, and the coding agent you work with, write a runbook that our machine
accepts as it is. Everything the machine checks is in here, including the checker itself.

## What a runbook is

A runbook is one YAML file, `runbook.yaml`. It says how your work is shown to be done:

- the stages, in the order they run, and the command each stage runs;
- the checks that decide whether a stage passed (an exit status, a file, a line in a log, a
  number in a JSON report, or a small script of yours);
- the settings a retry may change, and how many attempts a stage gets;
- the known ways a stage fails, how each is recognized, and what to do then (retry, stop, ask a
  person, or run a recovery stage);
- the steps only a person may approve (owner gates), such as a release.

Each check names the items of your spec it shows (success criteria `SC-001`, edge cases
`EC-001`, ... or OpenSpec scenarios). The checker fails while any success criterion or edge
case has no check, so nothing in the spec is left unshown.

## What is in the kit

| file | for |
|---|---|
| `README.md` | you: this page |
| `AGENTS.md` | your agent: the steps to write the runbook |
| `CLAUDE.md`, `.claude/skills/runbook-forge/SKILL.md` | Claude Code: the same steps, and a `/runbook-forge` skill |
| `FORMAT.md` | the format reference: every field, check type and error code |
| `check_runbook.py` | the checker: the same code our machine runs |
| `runbook.schema.json` | a JSON Schema of the file, for editors and other tools |
| `templates/` | an empty runbook, a spec template, and a note on OpenSpec specs |
| `example/` | a finished example: a spec, its runbook and one plugin check script |
| `VERSION`, `SHA256SUMS`, `LICENSE` | what the kit was built from, file checksums, the MIT license |

The `.claude` folder is hidden in most file browsers; `ls -a` shows it. `unzip` keeps the file
modes of the zip; a tool that drops them leaves `example/checks/status_codes.py` not executable,
and the checker then reports `PLUGIN-NOT-EXECUTABLE` on the example: run
`chmod +x check_runbook.py example/checks/status_codes.py` in the kit folder.

## Before you start

- Python 3.9 or later, with PyYAML: `python3 -m pip install pyyaml`.
- A spec. Either a spec-kit `spec.md` with `FR-001`, `SC-001` and `EC-001` ids, or an OpenSpec spec
  (`### Requirement:` blocks with `#### Scenario:` children). If you have none yet, start from
  `templates/spec.md`.

## Hand it to your agent

1. Copy this folder, with its name `{{KIT}}`, into the root of your repository. The agent
   needs to see your code to find the build and test commands.
2. Put your spec in the repository, for example `spec.md` in the folder where the runbook should
   live.
3. Give your agent the task:
   - Codex, Cursor and other agents that read `AGENTS.md`: "Read `{{KIT}}/AGENTS.md` and follow
     it to write `runbook.yaml` for `spec.md`."
   - Claude Code: copy `{{KIT}}/.claude/skills/runbook-forge` into your repository's
     `.claude/skills/` folder, then run `/runbook-forge spec.md`. Or give it the sentence above.
   - A chat model with no access to your files: paste `AGENTS.md`, `FORMAT.md` and your spec
     into the chat, then run the checker yourself on the file it gives back.
4. Answer its questions. It asks only what the spec and the repository do not say: missing
   commands, thresholds given in words instead of numbers, which settings a retry may change,
   how many attempts, and who approves the owner gates.

## Check it yourself

From the folder that holds `runbook.yaml` and the spec, where `<kit>` is the path to this kit
folder from there:

```
python3 <kit>/check_runbook.py runbook.yaml --spec spec.md
```

With the runbook at the repository root, `<kit>` is `{{KIT}}`; with the runbook one folder down,
it is `../{{KIT}}` (`python3 ../{{KIT}}/check_runbook.py runbook.yaml --spec spec.md`).

The last line `GATE alpaca-runbook-check: PASS` means our machine will take the file. Any
`ERROR` line names the field and the problem; `FORMAT.md` explains every code. The exit status
is 0 PASS, 1 FAIL, 2 BLOCKED (the file cannot be read), 64 for a wrong command line and 65 when
PyYAML is not installed. `--help` prints the options and also exits 64, so that 0 always means
PASS; the line `HARNESS-ERROR check_runbook [USAGE]` after it is expected. `--json` prints the result as JSON, with the checks that cover each spec
item.

The checker only reads files. It never runs the commands in your runbook or your plugin check
scripts; our machine runs those later, on our side.

A runbook may also be written as JSON (`runbook.json`): JSON is YAML, so the same checker and the
same rules apply. `runbook.schema.json` gives editors completion and early warnings; with the
YAML language server, make the first line of the runbook
`# yaml-language-server: $schema=<kit>/runbook.schema.json`, with `<kit>` the path from the
runbook's folder as above. The checker has the last word:
some rules (unique ids, spec coverage) are beyond a schema.

## What to send back

Send these files, in the folder layout the runbook expects (paths in a runbook are relative to
the runbook's folder):

1. `runbook.yaml`;
2. the spec it covers (`spec.md`, or the OpenSpec folder);
3. every plugin check script the runbook names, still executable (`chmod +x`);
4. optional: the output of `python3 <kit>/check_runbook.py runbook.yaml --spec spec.md --json`.

Do not send the kit itself back.

## What happens on our side

We run the same check on what you send. Our intake then turns each stage into a task and each
covered spec item into a checklist row, and our runner runs the stages with the checks and retry
rules you wrote. Owner gates wait for a person; no agent approves them.

## Versions and checksums

`VERSION` names the format version and the product commit this kit was built from. A runbook
starts with `runbook: {{FORMAT}}`. A kit for a later format comes in a folder with a new name.
To check that a download is complete, run `sha256sum -c SHA256SUMS` inside the kit folder.
