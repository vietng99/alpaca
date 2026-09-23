"""alpaca.gates - the instrument spine.

The verdict/exit-code contract and its I/O primitives are defined once, here:
`verdict` (the map and its census) and `contract` (canonicalisation, hashing,
pointer resolution, the worst-of fold). Every later instrument imports them and
never restates the numbers. `rc_conformance` is the enforcer of that rule.
"""
