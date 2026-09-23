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
