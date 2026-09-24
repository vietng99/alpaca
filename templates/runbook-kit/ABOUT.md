# Sources of the partner runbook kit

`alpaca runbook kit [--out DIR] [--zip]` (alpaca/runbook_kit.py) builds the partner runbook kit,
a folder a partner hands to their own agent to write a runbook this product accepts. The files
here are the parts of the kit written for the partner only. Everything else in the kit is built
from the product's own files, so it follows them:

| kit file | built from |
|---|---|
| `check_runbook.py` | alpaca/runbook.py, alpaca/gates/verdict.py and the OpenSpec change reader of alpaca/intake.py, carried by the builder |
| `runbook.schema.json` | the field tables of alpaca/runbook.py |
| `FORMAT.md` | docs/runbook-format.md, rewritten by the builder's rules, plus `FORMAT-note.md` and `FORMAT-our-side.md` |
| `AGENTS.md` | `AGENTS-intro.md`, the body of .claude/skills/alpaca-runbook-forge/SKILL.md, `AGENTS-deliver.md`, `AGENTS-never.md` |
| `.claude/skills/runbook-forge/SKILL.md` | `SKILL-head.md` and the AGENTS.md body |
| `README.md`, `CLAUDE.md`, `templates/` | the files here |
| `example/` | templates/runbook-example/ |
| `LICENSE` | LICENSE |

`{{FORMAT}}` and `{{KIT}}` are replaced by the format version and the kit folder name. When the
format document or the forge skill changes so that a rule of the builder no longer fits, the
build stops and names the rule; fix the rule in alpaca/runbook_kit.py, never the kit output.
