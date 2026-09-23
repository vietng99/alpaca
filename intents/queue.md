# Intent queue - Alpaca

Open tasks the people on this project want done. Edit freely. Each line becomes an op when it is picked up.

Format note (M3.6). One intent per checkbox line:

    - [ ] <label> - done when <bar>, proof local:<path>

The label names the work; the bar (the text after " - done when ") says when the work is done.
This file is the owner artifact: when `alpaca op new --from-intent` picks the first unchecked line it
records an intent whose bar traces back here, then opens the op from it. A line with no bar cannot
open an op. The queue is the only source an unattended loop opens the next op from.

- [x] Confirm spec patches P-001..P-010 (all ACCEPTED by owner 2026-09-16)
- [ ] M1 de-sign the checklist engine and gate the phase ladder - done when a sample spec becomes rows, instruments discharge them, and one phase boundary advances at L5 and pauses at L2, proof local:tests/test_acceptance_m1.py
- [ ] M2 build the board and vendor the wiki engine behind export and the page - done when two agents work one op through the board, the page shows it, an expired lease returns its row, and the wiki answers or abstains with pointers, proof local:tests/test_acceptance_m2.py
- [ ] M3 ship posture, formations and the skills plugin - done when an L5 op runs unattended to the ship card with zero other cards and the same op at L2 pauses at every boundary, proof local:tests/test_acceptance_m3.py
- [ ] M4 package the harness with its doctrine, rails and manual - done when a fresh copy and an existing repo both pass boot-check --full, the manifest verifies, the manual gate passes on all seven clauses, and the owner countersign row recording the guide opened on a phone is on the record, proof local:tests/test_acceptance_m4.py
