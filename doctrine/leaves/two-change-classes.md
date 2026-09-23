# Two change classes, the floor, and de-escalation

This leaf states the rule that `alpaca/posture/floor.py` enforces. The code is the mechanism; this
leaf is the reason, so the two never drift.

## Two change classes

Every write falls into exactly one of two classes, decided by path:

- **agent-writable**: the product tree, the `.alpaca/` runtime, the wiki, and the record rows. An
  agent writes these at any level.
- **human-owned** (the authority surface): `contracts/`, `project.yaml`, `doctrine/`, and the
  boot block (the marked region of `CLAUDE.md`). This is what the agent must not quietly rewrite,
  because it is the surface that says what the agent is allowed to do.

The split is read by path FROM THE MANIFEST (`ALPACA-MANIFEST`), so which paths the harness owns is
the shipped tree's own answer. The classification cannot drift from the tree, because it is read
off the tree. `doctrine/` is an authority surface by the rule below whether or not the manifest
lists it yet.

A human-owned write:

- up to L4 becomes a **review card**: the agent proposes the change, a human decides.
- at L5 and above is an **agent write with the diff recorded**: the agent writes it, and the
  event carries the diff, visible on the board forever.

## The floor: default-deny at every level

The floor is a set of actions refused at every level, not a boundary that a high level unlocks.
The named categories are:

1. an irreversible external action (push to a shared remote, deploy, delete outside the root,
   spend money);
2. leaving the project root;
3. an unsanctioned campaign;
4. editing a frozen record;
5. self-escalation (an agent raising its own level);
6. writing a global user surface;
7. writing the agent's own authority surface (this one is the human-owned change class above:
   a review card up to L4, an agent write with a recorded diff at L5+).

The named categories are examples, not an allow-list. Anything not recognised as explicitly safe
is refused. An unnamed irreversible action is still refused. The floor is a floor.

## De-escalation

Judgment runs downward only. Dropping to a safer, lower level is always permitted, at every
level. A de-escalation is ONE event. It discharges nothing already owed: an open obligation
(a pending review card, a ship decision, a human go still required) stays open after the drop.
Lowering the level narrows what the agent may decide next; it never clears what the agent already
owes.

Self-escalation is the opposite and is refused at every level. An agent never raises its own
level; that is a human decision recorded as one event, one name, one time.
