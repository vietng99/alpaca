# Operate in place: the store is history, not a promote source

This leaf states the rule that `alpaca/retention.py` enforces. The code is the mechanism; this leaf is
the reason, so the two never drift.

## The rule

The harness operates on the tree in place. A non-idempotent act mutates a file where it lives; it
does not fork a working copy, act on the copy, and promote the copy over the original. Before it
mutates, it snapshots the file's pre-image into the store (`.alpaca/store/`), named
`<file>.pre-<change-tag>.<date>`, with a mandatory note stating why the past is being mutated.

## The store is history the operator is mutating past, not a clone to promote from

This is the clarification the whole leaf exists for. The store looks like a set of file copies, and
a copy invites a wrong instinct: treat it as a candidate tree, keep working on it, and promote it
back over the original when it looks good. That instinct is refused here.

- The store is a record of the past, kept so a change is reconstructable. Its one forward use is as
  the diff reference the resolve pass reads to reconstruct a divergence against the value two
  writers started from. It is read, never promoted.
- Nothing is ever restored FROM the store as the new live tree. There is no promote path, no
  cutover, no swap. A snapshot is a pre-image and an audit trail, not a staging area.
- The live tree is the only tree. Work happens on it directly, and the snapshot is the safety net
  behind that work, not a parallel line of development that competes with it.

Promoting from the store would mean two live trees drifting apart with no single truth, which is
exactly the fork the record exists to prevent. The store stays behind the work, as history.

## The mandatory note

`snapshot(root, path, change_tag, note)` refuses without a note. A pre-image with no stated reason
is an unexplained mutation of the past: the store would fill with anonymous copies no one can read
back. The note is what turns a copy into a record. The refusal is in the code, not only here.

## Compaction under a declared policy

`compact(root, policy)` reads a size ceiling and an age ceiling from `project.yaml` (data, never
code) and removes old snapshots under them. Two rules bound it:

- It never removes a snapshot a live token or an unresolved collision still references. Those
  snapshots are the diff reference something still needs; dropping one would strand a mid-verb
  resume or the resolve pass with no base to reconstruct against.
- It refuses a zero-retention policy (a zero ceiling) while unresolved collisions exist, for the
  same reason: a policy that keeps nothing would delete the very diff reference an unresolved
  collision is waiting on. The refusal is a decision on the record, not an omission.
