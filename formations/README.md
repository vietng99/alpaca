# formations/

A formation is more than one agent working one op, with independence where it matters, without a
new protocol per formation. Each formation is ONE file in this directory. Adding a formation is
dropping a file here; there is no code change (D7: the protocol stays ahead of the catalogue).

## The manifest

A formation file carries a YAML front matter block, delimited by two `---` lines, declaring five
fields (the loader in `alpaca/formation/manifest.py` refuses a file missing any of them):

- `name` - the formation's id, unique across this directory.
- `roles` - the members. Each role is a mapping with an `id`; a role may set `blind: true` to
  mark a verifier that must not receive the builder's narrative.
- `phases` - the ordered phases this formation MAY run.
- `gate_map` - each phase boundary bound to the gate that decides it.
- `budget_class` - the resource class the formation declares up front.
- `output` - the deliverable shape.

Example:

```
---
name: builder-verifier
roles:
  - id: builder
    archetype: eng
    produces: the change and its narrative
  - id: verifier
    archetype: red
    blind: true
    receives: [claim, pointer]
phases: [build, verify]
gate_map:
  build->verify: gate(verify, blind-verifier)
budget_class: standard
output: a verified change carrying an independent verdict
---

# builder + blind verifier

Prose after the front matter is documentation, not part of the manifest.
```

## Tiers and escalation (shield)

Every spawned role runs on a model chosen by `formations/shield.yaml`, never on the session's model
by default. A role's tier is its own optional `tier:` (`low`, `mid`, `high`, `top`, or `session` for
the main chat), else the `tier:` its archetype brief declares in `agents/<archetype>.md`. The policy
maps each tier to a model for Claude and Codex. A role with no tier anywhere or a runtime with no
model table is refused. Resolve the current values with `--runtime claude` or `--runtime codex`.

A formation may also carry an optional `escalation:` block holding only `round_cap`; it may lower
the policy cap (5), never raise it, and never to the class check's `after_failed_rounds` (3) or
below, where the class check could never run. The rest of the ladder (the think stage before
round 1, one high investigator per failed round that reports before the next fix, the
cause-to-action map, the class check that sends a non-converging task back through the think stage
once, the stop at the cap) and the verifier rules live in `shield.yaml` alone. `alpaca/formation/shield.py` resolves them; print the resolved table with
`bin/alpaca-python -m alpaca.formation.shield [formation ...] --runtime codex --json` for Codex
(the CLI default remains Claude). The shared Shield skill is how an orchestrator applies it;
Codex discovers its project entry under `.agents/skills/shield/` and uses native subagents.

## Registered in both directions

`manifest.registry(root)` scans this directory and registers every manifest both ways: name to
formation (`registry.get`, `registry.names`) for the dispatch protocol, and formation file back
to name (`registry.name_of`) for the doctrine registry. A file with no front matter, such as this
README, is skipped, not refused; a file that HAS front matter but is missing a field is refused,
so a broken formation never registers silently.

## Blind pairs

The blind-pair rule is engineered here, not hoped for. When a formation pairs a builder with a
`blind` verifier, whatever the orchestrator delivers to the verifier passes through
`manifest.payload_for`, which strips the narrative fields so the verifier sees only the bare claim
and its pointer. Two minds anchored to one narrative are one mind; blindness makes "CONFIRMED"
mean someone actually tried to break the claim without inheriting the builder's confidence.

## The roles

The role briefs live under `agents/`: `_common.md` (the shared contract), `orchestrator.md` (the
sole serial writer that routes and never judges), `scout.md`, `tracer.md`, `red.md`, `judge.md`,
`eng.md`, and `autodrive.md` (the loop brief). A role names its base archetype; a manifest refers
to these roles, it does not re-author them.
