# Sample spec: greet CLI

A tiny sample used by the M1 acceptance test (tests/test_acceptance_m1.py). The one
region below carrying the item keys is the acceptance table; everything else on this page
is prose or a step manifest line, and the parser refuses an obligation-shaped token that
lives outside the table.

## Acceptance

| item  | statement                                            | oracle-class | proof-kind |
| ----- | ---------------------------------------------------- | ------------ | ---------- |
| AC-01 | the greet command prints a greeting to stdout        | unit         | test       |
| AC-02 | an empty name is refused with a nonzero exit         | unit         | test       |
| AC-03 | the greeting is read back from the record            | integration  | test       |
| AC-04 | a second run greets without duplicating the record   | behavioral   | run        |
| AC-05 | the help text lists every flag the command accepts   | review       | review     |

## Step manifest

One step-manifest line per phase names, in engineering vocabulary, what each phase owes.

step requirement: state each acceptance item, name its oracle class, name its proof kind
step design: cite each acceptance item from a design section with a recorded why
step build: the build command exits clean, the commit is recorded, the lint is clean
step verify: run the probe, one proof pointer per item, a blind verifier verdict recorded
step release: changelog from discharged rows, manifest verified, ship decision recorded
