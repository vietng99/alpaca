"""Formations: more than one agent over one op, without a new protocol per formation (M3.7).

A formation is a FILE against a fixed interface, not code. `alpaca.formation.manifest` loads one
manifest file and scans the `formations/` directory into a registry; the dispatch protocol
(M3.8) and the doctrine registry (M4.14) consume that registry. Keeping the protocol ahead of
the catalogue is D7 (spec:156): adding a formation is dropping a file, never a code change.
"""
