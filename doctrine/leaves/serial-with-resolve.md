# Serial with resolve, not a hard lock

This leaf states the rule that `alpaca/resolve.py` and the claim path in `alpaca/claims.py` enforce. The
code is the mechanism; this leaf is the reason, so the two never drift.

## The rule

Two writers are allowed to reach for one reference. The collision is not prevented; it is
recorded, and a later pass resolves it. This is serial-with-resolve: the writers are serialised
after the fact against the value they diverged from, rather than blocked before the fact by a
lock. The record is the named artifact for this: a resolve log of typed entries (`claim`,
`concurrent-detected`, `resolved`) on the append-only chain, and a resolve pass that consumes it.

## Claim and first beat land before the first mutation

A writer takes a claim and writes its first heartbeat BEFORE it makes the first mutation, never
after. The order is deliberate and is kept in code, not only here: `claims.take` appends the
`claim` event and the first `heartbeat`, and only then mutates the current-state row. The reason
is the gap. If the mutation landed first and the claim second, a second session peeking in the gap
between them would see a changed reference with no holder, and would take it as free. With the
claim first, a second session peeking in the same gap sees a live claim rather than nothing, and
records a `concurrent-detected` entry instead of clobbering. The claim is what makes the collision
visible; it must exist before there is anything to collide over.

## Why a hard lock is refused

A hard lock (an operating-system lock, an exclusive file handle, a lock column that blocks every
other writer) is refused, and the refusal is a decision on the record, not an omission:

- A held lock outlives the process that took it. A worker that crashes mid-hold leaves the
  reference wedged with no live holder to release it, and the next worker cannot tell a live hold
  from a dead one. A claim, by contrast, carries a lease that ages out on its own: a crashed
  worker's claim stops being live when its lease passes, with no lock to break.
- A lock cannot be reconstructed from the append-only record after a restart. It is state held in
  a process or a file handle, not an event on the chain. A claim IS an event on the chain, so a
  resumed run folds the true holder back from the record rather than trusting a stale flag.
- A lock stops honest work to prevent a collision. Serial-with-resolve lets the honest work
  proceed and records the collision to resolve afterwards, which is the proportionate response
  when collisions are rare and the record can always reconstruct what happened.

The softness is the decision: a claim is advisory, and the safety comes from recording every
collision and resolving it, not from forbidding the second writer.

## The resolve pass

`resolve.pass_` walks every `concurrent-detected` entry that no `resolved` entry yet answers, and
for each one reconstructs the divergence AGAINST THE SNAPSHOT: the base value the two writers
diverged from.

- Snapshot available: the pass reconstructs what each writer changed from the base and records a
  `resolved` entry that keeps every writer's divergence. The writers are ordered and the later one
  wins, but nothing is dropped in silence: the loser's divergence is on the record.
- No snapshot: the pass cannot know the common ancestor, so any merge would be a blind clobber. It
  refuses to merge and SURFACES the collision instead. A collision with no snapshot is never
  merged; it stays unresolved until a snapshot or a human decides it.

No collision is ever silently merged. The unresolved count is a read over the record, surfaced by
`alpaca doctor` and on the pad, so a collision that was logged and never resolved cannot hide.
