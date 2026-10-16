# First-chat rehearsal (AC-20)

Three newcomer personas talk to a host agent that follows `.claude/skills/alpaca-first-chat/SKILL.md`, in a real
studio, and a scorer and a fresh judge grade each run. The harness here is built and tested by t-656
(`alpaca/tests/test_rehearsal_harness.py`); the run itself is done by the main session after the merge.

| Piece | File |
|---|---|
| personas: a public part and a sealed part | `personas/P1.yaml` (take-over), `P2.yaml` (team, trap "yes, but use L3"), `P3.yaml` (non-programmer) |
| fixture repositories | `fixtures.py` (`build_team_repo`, `mirror_qtrm64`, `build_repo`) |
| driver: studio, session, transcript lines, snapshots, state | `driver.py` |
| scorer: the mechanical rows, the pass bar | `score.py` |
| the Workflow script | `run-workflow.js` |

## How to run

1. Merge t-656. Pick a NEW folder in the session scratchpad for `scratch` (a rehearsal always starts from a fresh studio).
2. P1 needs a local clone of QTRM64 (`git clone https://github.com/example-org/QTRM64 <probe>` or an existing clone).
3. Start the script with the Workflow tool: `{scriptPath: "alpaca/tests/rehearsal/run-workflow.js", args: {install:
   "<merged worktree>", scratch: "<new folder>", today: "YYYY-MM-DD", probe: "<QTRM64 clone>"}}`.
4. Each persona ends with `transcript.md`, `commands.log`, `snapshots.tsv`, `events.tsv`, `quiz.md`, `cards/`,
   `score.json` under `<scratch>/<id>/`. Copy them to `.alpaca/proofs/op-062/t-656.logs/rehearsal/` and delete the
   scratch studios.
5. Check that no host agent read the sealed sheets: grep the workflow journal for `rehearsal/personas` and pass that
   file as `--extra-log` to `python -m alpaca.tests.rehearsal.score <run dir>`.
6. Join the judge's R1, R6 and R9 with `score.finalize(mech, judge)`. The pass bar: R1, R2, R3 and R7 at 2 for every
   persona, and at least 18 of 22 each. A failed run goes back to the skill, the renderer or the round builder, and the
   persona runs again from a fresh studio. A sheet is never edited to make a run pass.

## What the rehearsal does not prove

The host and the persona are language models; the persona's typed lines are real transcript entries but not a
person's. The confirm in a rehearsal proves the code path and the owner-word rules, not owner consent (design
section 9.4): every event of a rehearsal carries `rehearsal: true`, and a trial that uses the driver must say its
confirm is simulated in origin.
