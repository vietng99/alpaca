# Alpaca trace convention

Evidence and coverage contract for the bundled VSYS renderer. This adaptation uses native operator tools and has no inherited target data or verification history.

## 0. The shape of a run

A VSYS trace is a **bounded investigation with native operator tools**, not a single linear read. Five phases:

```
Scope  ->  Sweep (BFS)  ->  Verify (DFS)  ->  Cross-link  ->  Critic
           inherited model fleet      adversarial       one pass         miss-hunt (loop)
```

- **Scope** buckets every in-scope file into an area (`buckets.json`).
- **Sweep** is a wide inherited model fleet: one tracer per ~8-file chunk, over-capturing nodes+edges with
  evidence. BFS: breadth, cheap, exhaustive.
- **Verify** is a fresh adversarial wave, one per area: re-resolve every citation, downgrade every
  overclaimed state. DFS: depth, skeptical, provenance-or-die.
- **Cross-link** captures the inter-area wires once the full node set is known.
- **Critic** is an independent miss-hunt; its verdict gates the trace. Loop until `CLEAN`.

The tracer protocol (`tracer/PROTOCOL.md`) is the executable form of this shape.

## 1. Scope and boundary

Decide, in writing, what is in the system and what is not - before tracing.

- **In scope:** the real, authored surface of the target - source, config, docs, scripts, hooks,
  entry points, and the runtime state contracts they define. Enumerate with `git ls-files` (or a
  full walk for a non-git target).
- **Out of scope by default:** vendored/third-party code, generated/build output, caches, sandbox
  or worktree *copies* of the tree, test fixtures that duplicate real files, data blobs, and any
  **external package that is not present on disk** (an absent path can carry no evidence - exclude
  it, never node it). For Alpaca, exclude `.alpaca/`, `.venv/`, and generated caches.

The scope output is `buckets.json`: `{ "<area-id>": ["repo/rel/path", ...], ... }` - every in-scope
file assigned to exactly one area. This map is the anti-missing backbone: the sweep fans out over
its chunks, and the critic checks every path in it is dispositioned.

## 2. Areas and granularity

Areas are the subsystem blocks (the grid blocks in the render). For a known domain, start from the
Profile's `areas`; add or split as the tree demands, and log any area added beyond the Profile.

Nodes are tiered - capture the tree, choose the display level later:

- **System** - the whole target (one blueprint).
- **Area** - a subsystem block. Groups components that serve one purpose.
- **Component** - the default node. One coherent unit: a script, a doctrine/spec file, an agent
  brief, a gate/instrument, a runtime-state contract, a service, an endpoint.
- **Cluster** - when several files in a chunk are near-identical leaves (per-operation logs, a test
  directory, an RTL tree), fold them into ONE `cluster:true` node that lists them + a count in
  `notes`. A fold is logged (`folded:<node-id>`), never a silent drop.

Split when a file holds two clearly separate responsibilities; merge when several files are one
indivisible unit.

## 3. Capture schema (record the same fields every time)

**Per node** (the sweep emits these; the verify wave corrects them):

| field | meaning |
|---|---|
| `id` | stable, kebab-case, **area-prefixed**, globally unique (e.g. `gates-checklist-gate`) |
| `label` | short display name (<= 5 words) |
| `what` | one plain sentence, grounded in the file actually read (<= 300 chars) |
| `raw_type` | one of the Profile's node types (e.g. constitution/doctrine/gate/instrument/agent/ops/state/seam/owner/flow) |
| `shape` | `card`, or `diamond` **only** for a decision/gate |
| `state` | `specced` / `built` / `verified` / `overridden` - read honestly (sec 5) |
| `who` | `gear` (machine acts) or `person` (human acts/signs) |
| `file` | repo-relative path |
| `evidence` | a REAL `file:line` you read - the claim rests on it |
| `dims` | the Profile benchmark dimension number(s) this node is evidence for (empty if the Profile ships no benchmark) |
| `cluster` | `true` if this node folds several near-identical files |
| `notes` | anything raw worth keeping; correction notes from the verify wave; `UNRESOLVED` if no evidence supports it |

**Per edge (relation or interaction):**

| field | meaning |
|---|---|
| `from`, `to` | node ids (must be real ids) |
| `what` | what connects them, in words (<= 4 words) |
| `raw_kind` | `flow` / `calls` / `signs` / `routes` / `reads` / `guards` / `feeds` |
| `evidence` | `file:line` where the link is visible (an import, a call site, a config ref) |
| `dash` | `true` for an override/bypass wire |

**Per coverage row:** `{ "file": path, "disposition": "node:<id>" | "folded:<id>" | "excluded:<reason>" }`.

Over-capture. It is cheaper to drop a captured node later than to discover a missing one.

## 4. Coverage guarantee (the anti-missing mechanism)

Do not *hope* nothing was missed - *prove* it.

1. **Enumerate everything:** `buckets.json` lists every in-scope path (sec 1).
2. **Account for each item:** every path in `buckets.json` ends as `node:` , `folded:` , or
   `excluded:` . Nothing is left unaccounted. Files not individually dispositioned by a tracer are
   **folded into their area** by the builder (logged, with per-area counts), never dropped.
3. **Ledger:** the enumeration + disposition is emitted as `data/<target>.coverage.md` (or the
   `coverage` rows inside the data JSON) so a reader can audit what was and was not drawn.
4. **Critic pass:** an independent agent hunts misses - unmapped files, empty areas, evidence-less
   edges/nodes, overclaimed states, and (if the Profile ships a benchmark) uncovered dimensions. It
   returns `CLEAN` or `NEEDS-ANOTHER-ROUND: why`. Feed its findings back into Sweep/Verify and
   repeat until it returns `CLEAN`.

## 5. Rigor and honesty

- **Read, do not guess.** A node's `what` comes from the file's content, not its name.
- **Existence-check floor.** An absent file supports nothing. Never node a path that is not on disk;
  never mark it `built`/`verified`. If a package is external and not checked out, exclude it.
- **Specced != Built != Verified.** A design/spec doc is `specced`; a script that imports and runs
  is `built`; reserve `verified` for a behavioral test/probe you can name in `notes`. `overridden`
  only if the file itself carries an OVERRIDDEN stamp. The verify wave only ever **downgrades**, it
  never upgrades.
- **Every claim carries evidence.** A node without a resolvable `file:line`, or an edge without a
  visible link, gets `notes:"UNRESOLVED"` and a conservative state - it is not asserted as fact.
- **Mark unknowns; never omit them.** An uncertain component is captured with a flagged `notes`.
- **No silent caps.** If a run bounds coverage (a cluster fold, a top-N, a skipped dir), it is
  logged in the ledger, not hidden.

## 6. Two-altitude output (the draw contract)

Every trace produces two artifacts from the same dataset:

- **`data/<target>.md`** - the EXHAUSTIVE source of truth. Every component, every file pointer,
  every evidence pointer, every state, the full coverage ledger, and the critic verdict. Nothing
  folded away.
- **`data/<target>.html`** - the drawable blueprint. It draws **every** node, wired inside its
  block, and uses **semantic zoom** for altitude: block-level names when zoomed out, components
  when you zoom in. No "+N more" truncation.

The Markdown is truth; the HTML is the view. They are regenerated together by
`engine/gen_blueprint.py` from `data/<target>.json` + the Profile.

## 7. Stop condition (when a trace is done)

A trace is done when: every in-scope path in `buckets.json` is accounted for (`node`/`folded`/
`excluded`); every area has at least one node; every edge and node has an evidence pointer or is
flagged `UNRESOLVED`; every benchmark dimension the Profile declares has at least one evidence node
(or its absence is logged); and the critic pass returns `CLEAN`. Only then are the MD + HTML
handed off as the finished blueprint.
