"""Posture: how much the agent decides alone, mirrored into the record (spec 5.6:389-401).

M3 splits the session posture into instruments that this package builds one at a time:

  * `level` (M3.1) is the session-posture contract: the autodrive level in force for a session,
    mirrored into the record as a decision row so every gate reads it FROM THE RECORD, not from
    the environment. It is the first instrument.
  * the authority to act unattended (M3.2, standing authority) is a SEPARATE instrument. Neither
    stands in for the other: a level says how much the agent decides in one session; an authority
    says whether a loop may act with no one watching.
"""
