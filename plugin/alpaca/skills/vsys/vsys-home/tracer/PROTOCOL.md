# VSYS Tracer Protocol

The step-by-step agentic run that turns a target system into a Data JSON the Engine can render. It
executes the `TRACE-CONVENTION.md` contract; read that first. The Alpaca implementation executes these stages with the active operator native tools; the `/vsys` skill supplies the portable entry point.

## Inputs
- `repo` - the target path (the system to draw).
- `profile` - `profiles/<domain>.json` - pick an existing one or author it for the domain. Supplies
  the area set (`areas`), node types, and the optional benchmark axes (`bench`).
- `bucketsPath` (optional) - a pre-built `buckets.json`. If absent, phase 0 builds it.

## Phases

### 0. Scope -> buckets
Enumerate every in-scope path (`git ls-files`, or a full walk). Apply the exclusions in
convention sec 1 (vendored, generated, sandbox/worktree copies, caches, absent external packages),
logging each. Assign every remaining path to exactly one area from the Profile. Emit
`buckets.json`: `{ "<area-id>": [paths...] }`. This is the coverage backbone.

### 1. Sweep (BFS) - the inherited model fleet
Chunk each area's files into groups of ~8. Fan out **one inherited model tracer per chunk** (the inherited parent model and available native delegation), forced to the node schema. Each tracer opens every
file in its chunk and captures nodes+edges+coverage with real `file:line` evidence, tagging each
node with the benchmark dimension(s) it proves (convention sec 3). Over-capture. Merge chunk output
per area; dedup node ids (first wins).

### 2. Verify (DFS) - the adversarial wave
Fan out **one verifier per area** (effort `high`), given the merged area dataset. Each assumes the
sweep inflated something and, for every node/edge: re-opens the cited evidence and confirms it
resolves and supports `what`; downgrades any state that overclaims (the C3 honest-ledger floor -
`verified` only with a named behavioral probe); drops unearned dimension tags; dedups; fixes
mis-dispositions. It **only corrects** - never upgrades - and notes each change.

### 3. Cross-link
With the full node index known, one agent (effort `high`) captures the inter-area wires only (both
ends in different areas) - the load-bearing connections between subsystems. `from`/`to` must be
real ids; `dash:true` for override/bypass; every wire evidence-grounded.

### 4. Critic (loop)
One independent agent (effort `high`) hunts misses: unmapped files, empty areas, evidence-less
nodes/edges, overclaimed states, uncovered benchmark dims. It returns `{misses, uncovered_dims,
verdict}` where verdict is `CLEAN` or `NEEDS-ANOTHER-ROUND: why`. If not `CLEAN`, feed the misses
back into a targeted re-sweep/re-verify of the offending areas and re-run the critic. Repeat until
`CLEAN` (or the budget floor is hit, logged).

### 5. Emit Data
Write `data/<target>.json` (the workflow return: `areas`, `cross`, `critic`, `stats`, plus any
optional `dropin`/`dvflow` areas) and `data/<target>.coverage.md` (the ledger).

### 6. (Optional) Enrich
For a richer render, three optional passes proven on the earlier harness:
- **Deepen** - re-trace the most complex areas at higher fidelity, merge back.
- **Compress** - rewrite verbose node `what` into terse, verb-first lines.
- **Layout** - an agentic block placement + per-dimension recaps (`design2out.json`), and
  re-derived block-to-block connections (`blockconn.json`).
These are optional inputs to the builder; the render works without them.

### 7. Draw
Run the builder:

```
python3 engine/gen_blueprint.py \
    data/<target>.json profiles/<domain>.json data/ engine/blueprint-engine.html \
    data/buckets.json [blockconn.json] [design2out.json]
```

It emits `data/<target>.md` (exhaustive) + `data/<target>.html` (semantic-zoom blueprint).

## Agent tiers (proven on the earlier harness run)

| phase | model | effort | count |
|---|---|---|---|
| Scope | inherited model | low | 1 |
| Sweep | inherited model | low | one per ~8-file chunk (the earlier harness: ~64) |
| Verify | default | high | one per area |
| Cross-link | default | high | 1 |
| Critic | default | high | 1 per round |

## Notes
- Honesty rules (convention sec 5) apply at every phase: read don't guess, existence-check before
  use, Specced != Built != Verified, evidence or `UNRESOLVED`, no silent caps.
- The run is crash-safe by artifact: `buckets.json`, `data/<target>.json`, and the coverage ledger
  are the durable state; a re-run resumes from what is already accounted for (local operation checkpoints
  unchanged agent calls).
