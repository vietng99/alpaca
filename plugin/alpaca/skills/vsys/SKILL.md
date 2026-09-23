---
name: vsys
description: Trace a local system from file evidence and render a standalone interactive architecture blueprint with the bundled Python renderer.
---

# Alpaca system blueprint

Read `vsys-home/TRACE-CONVENTION.md` and `vsys-home/tracer/PROTOCOL.md` beside this file.
The portable contract uses the active operator's native read and delegation tools. No Workflow
plugin or additional account installation is required. When independent review cannot run,
record that limitation in the coverage verdict.

1. Define the target and enumerate in-scope files. Keep inputs and outputs under
   `.alpaca/vsys/<name>/` within the project. Create `buckets.json` mapping areas to source paths.
2. Copy `vsys-home/profiles/alpaca.json` into that output directory. Set `target` to a simple
   filename stem, `title` to the system name, and `areas` to observed subsystems. Keep `repoRoot`
   blank and use relative evidence paths so shared output carries no host path.
3. Trace each area using native agents where useful, otherwise sequentially. Every node needs
   an observed component and a file:line pointer. Write `traced.json` using the schema in the
   protocol. Distinguish specced, built, and verified states; existence alone proves only built.
4. Review nodes and wiring against the source. Account for every bucket entry as a node, a
   documented fold, or an explicit exclusion. Record missing evidence in `critic.misses`.
5. Render with the bundled dependency-free builder, using actual absolute paths for its inputs:

```bash
bin/alpaca-python plugin/alpaca/skills/vsys/vsys-home/engine/gen_blueprint.py \
  .alpaca/vsys/NAME/traced.json .alpaca/vsys/NAME/profile.json .alpaca/vsys/NAME \
  plugin/alpaca/skills/vsys/vsys-home/engine/blueprint-engine.html .alpaca/vsys/NAME/buckets.json
```

The outputs are NAME.html, NAME.md, and a coverage ledger when buckets are supplied. Check
standalone HTML for ASCII bytes, inspect the rendered diagram, and link the output. Report
actual coverage and unresolved evidence; a rendered page alone does not verify the system.
