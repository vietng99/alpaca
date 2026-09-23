<!-- ALPACA:BOOT:BEGIN -->
# Alpaca boot

This project runs under Alpaca, the engineering harness. The record of every session, op, task,
and message is `.alpaca/alpaca.db`; `alpaca` is its only writer. Read `RESUME.md` first on any start or
resume: the SessionStart hook prints it into this chat.

alpaca means bin/alpaca from the project root (or python3 -m alpaca).

Rules that hold in every session:
1. The record is append-only. A task is done only with a sealed proof report: `alpaca proof new <id>`,
   write the report the way an engineer documents work (what was done, how, where, the result,
   deviations, how to reproduce, the evidence list), `alpaca proof seal <id>`, then
   `alpaca task move <id> done --proof local:<report>`. A `remote:` ref belongs inside the
   report's Evidence list; alone it does not close a task.
2. Every claim you make carries a pointer to a file, a command output, or a record row.
3. Ship and irreversible external actions (push to a shared remote, deploy, delete outside this
   root, spend money) are human decisions at every autodrive level.
4. Keep operational writes in this project root. Never edit `RESUME.md` or `analytics/` by hand; run `alpaca status`.
5. Not onboarded yet? Ask who is working on this, what is being worked on, the open tasks, and
   whether to enable the plain-writing preset. Then run `alpaca onboard ...` once, then `alpaca doctor`.
   When `project.yaml` names a domain profile with a `profile:` key, that profile supplies the
   domain's stages and acceptance cards; with no profile, Alpaca runs as the generic harness.
6. The authority surface (`contracts/`, `project.yaml`, `doctrine/`, this boot block) is
   human-owned: below L5 an agent write there is a review card, at L5+ it writes with the diff
   recorded. Dropping to a safer level is always allowed and clears nothing you already owe;
   raising your own level is not.

Verbs: `alpaca status | doctor | verify | onboard | op new/close/list | task add/claim/move/list |
proof new/seal/check | msg post/read | analytics build | recall <sid>`. Autodrive level comes from the project-local session record.

Boot read-order (the core set loads unconditionally, for every role and phase, before the router
narrows anything): 1) pad; 2) `ALPACA-MANIFEST`; 3) the core set (`doctrine/CORE-CARD.md` and the
CORE leaves in `MAP.md` section 2); 4) the role-by-phase router in `MAP.md` section 3; 5) the
level; 6) the modes. The router narrows only after the core set is loaded. Every doctrine leaf
lives under `doctrine/leaves/` and is registered in `doctrine/INDEX.md`; the registration is
re-derived from the leaf bytes by `alpaca/gates/doctrine_registration_check.py`.
<!-- ALPACA:BOOT:END -->

## Temp and scratch files

`/tmp` on the host is a RAM-backed tmpfs with a per-user quota (80% of its size). Filling it
stalls tools with `Disk quota exceeded` while the disk still has room. Rules:
1. Large scratch output (copies of the tree, extracted archives, build trees, big logs) goes under
   the session scratchpad path named in the system prompt, or under a gitignored dir in this
   project root. Both are disk-backed. Never put it under `/tmp`.
2. Small, commit-heavy temp stays on the default temp dir (RAM): pytest's basetemp and the sqlite
   stores the tests create. On the disk the same tests run about 100 times slower (one 16-test
   file: 0.3 s on RAM, 40.9 s on disk). Do not pass `--basetemp` under `/var/tmp` or the
   scratchpad, and do not export `TMPDIR` to a disk path.
3. Claude's own temp root is set at the account level, not here: `env.CLAUDE_CODE_TMPDIR` in the
   account `settings.json`, value `/var/tmp`. A project `.claude/settings.json` cannot set it;
   Claude Code drops that key from project env. Check: the scratchpad path in the system prompt
   starts with `/var/tmp/`. If it starts with `/tmp/`, tell the owner before writing anything large.

## Operation

The Claude Code hooks in `.claude/settings.json` record lifecycle events through `bin/alpaca-python`, the local interpreter. Bootstrap with `bash setup/bootstrap.sh` before opening a fresh installation. Hook failures are fail-open for chat availability; they never stand in for evidence, and an explicit `alpaca session checkpoint` is the way to require a visible capture result.

Domain work plugs in as a profile named in `project.yaml`. Changing a profile's acceptance criteria or its protected inputs requires owner review. Poll a running job before launching another; a disconnected chat does not mean the job stopped.

Claude and Codex use the same record. Native delegation tools may differ; the formation library records work assignments and does not itself launch agents. Do not import another project's transcript or runtime state. See `docs/operators.md` for both adapters and `docs/shipping.md` for clean distribution.
