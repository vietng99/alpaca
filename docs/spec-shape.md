# Spec shape (minimal)

The spec is the one artifact the checklist engine consumes. It is a plain markdown file.
The synthesis instrument reads exactly one part of it: the acceptance table. Everything
else in the file is prose the engine reads only to prove that no obligation hides outside
the table.

A spec file carries, in order:

1. Numbered requirements, as prose.
2. An acceptance table (below).
3. A step-manifest line naming the phases used.
4. Open questions.

## Acceptance table

The acceptance table is a GitHub-flavoured markdown pipe table with a declared key column.
Alpaca's requirement phase produces this shape and nothing wider.

- Key column header: `item` (or `id`). Its cells are the acceptance keys, one per row,
  in the `AC-nn` family (for example `AC-01`, `AC-02`).
- Columns: `statement`, `oracle class`, `proof kind`.
- The `oracle class` cell names a class declared in `project.yaml`, never an ad-hoc check.
- The `proof kind` cell names how the row is discharged: `test`, `run`, or `review`.

Example of a clean table:

```
| item  | statement                        | oracle class | proof kind |
| ----- | -------------------------------- | ------------ | ---------- |
| AC-01 | the parser reads a clean table   | unit         | test       |
| AC-02 | residue outside the table BLOCKs | unit         | test       |
```

The register the closure check derives is exactly the set of key cells in this table.

## Residue is refused

An obligation that lives outside the acceptance table is derived from nothing, which is
worse than an obligation that is missing: it looks discharged while no row carries it. The
parser dispositions every line of the file into a ledger and refuses, with BLOCKED, any
line outside the item table that carries either a token in the key shape (an `AC-nn` in
prose) or a stray table delimiter. An empty table is BLOCKED too: an empty measured
population is never a pass.

## Step models per phase

Each phase named in `project.yaml` has a step model under `step-models/<phase>.json`. A
step model lists its steps, each carrying:

- `key`: the step's stable identifier.
- `name`: the human name of the step.
- `ordinal`: the step's position; ordinals form the partition `1..N` with no gap.
- `witness`: a pointer into the method that declares the step.
- `consumes`: the artifact kinds the step reads (for example `acceptance-table`).
- `obligation`: an item-bound template; it names its item with `{item}`, so one statement
  claimed N times is N obligations, not a vacuous universal.
- `report_back`: the four axes `proof`, `where`, `how`, `when`.

## Anchors used by the shipped step models

The shipped step models witness against sections of this file:

- `#acceptance-table` the acceptance table contract above.
- `#design-phase` design records an approach and fixes an interface per item.
- `#build-phase` build implements to the interface and wires an independent caller.
- `#verify-phase` verify discharges each item by its oracle class and holds the freeze.
- `#release-phase` release reconciles the store and records the human close.
