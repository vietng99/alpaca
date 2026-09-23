# Checklist example (engineering)

This file is a teaching example, not a live store. It shows the acceptance-table shape a
real spec carries, and it is refused BLOCKED on purpose: the note below names an obligation
(AC-04) that no row in the table carries, so the residue scan of the parser refuses the
file. That refusal is the point. A worker who copies this template must delete the residue
note and add the missing row before the file becomes a real spec the engine will accept.

## Requirements

1. The harness parses an acceptance table into rows.
2. It refuses any obligation that lives outside the table.

## Acceptance

| item  | statement                                            | oracle class | proof kind |
| ----- | ---------------------------------------------------- | ------------ | ---------- |
| AC-01 | a clean acceptance table yields one row per key      | unit         | test       |
| AC-02 | residue outside the table is refused BLOCKED         | unit         | test       |
| AC-03 | an empty table is BLOCKED, never a pass              | unit         | test       |

Note (delete before use): the release step still owes AC-04, which nobody has written a
row for yet. While this line stands, the file is refused as a store by design.

## Step manifest

- phases: requirement, design, build, verify, release

## Open questions

None.
