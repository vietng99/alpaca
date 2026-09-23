"""The phase ladder (M1.15): enter a phase by level, close it with sign_out, and open one
composed door per phase boundary.

Ported from the earlier harness gates/phase_gate.py and steps/spec-to-step1.py and de-signed for Alpaca
per spec 5.2 item 8 and item 12, the 5.3 defaults table and the 7.4 human-decision table:

  * `phase_gate.enter(conn, op, phase, level)` is a LEVEL COMPARISON -- level in force >= the
    phase's declared level AND the previous phase discharged -- replacing the earlier harness L6 grant
    check. There is no grant file and no grant minting (D1: no signing anywhere).
  * `phase_gate.sign_out` keeps the ported content guards 0-3 (canonical state, every
    metric-closing step sampled from a real run, every step has exit evidence, no open
    flow-break / fundamental / owner-choice question) and loses the boundary-signature branch.
  * `doors.run(conn, op, boundary)` composes one door per boundary: `workspace_guard` first,
    then the phase's generic-defaults instrument set, then the project's own checks under
    `contracts/<phase>/`, folded with `worst()`. The door OPENs only if every link PASSes; no
    flag opens it past a failing link. Whether an open door advances automatically or pauses
    for a recorded human go is read off the 7.4 table by the level in force.

The generic phase defaults live in `alpaca.phase.defaults`, shipped exactly as the 5.3 spec table
states them.
"""
