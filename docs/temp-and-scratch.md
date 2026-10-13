# Temp and scratch files

Read this before a task that writes large output: tree copies, test suites, emulator or
simulator runs, extracted archives, build trees, long logs.

## Where things go

`/tmp` on the host is a RAM-backed tmpfs with a per-user quota (80% of its size). Filling it
stalls tools with `Disk quota exceeded` while the disk still has room.

1. Large scratch output (copies of the tree, extracted archives, build trees, big logs) goes under
   the session scratchpad path named in the system prompt, or under a gitignored dir in this
   project root. Both are disk-backed. Never put it under `/tmp`.
2. Small, commit-heavy temp stays on the default temp dir (RAM): pytest's basetemp and the sqlite
   stores the tests create. On the disk the same tests run about 100 times slower (one 16-test
   file: 0.3 s on RAM, 40.9 s on disk). Do not pass `--basetemp` under `/var/tmp` or the
   scratchpad, and do not export `TMPDIR` to a disk path.
3. Jobs that print a lot (emulators, full-suite logs) write their output to a file under the
   scratchpad. Bring only the tail or the matched lines into the chat.
4. Claude's own temp root is set at the account level, not here: `env.CLAUDE_CODE_TMPDIR` in the
   account `settings.json`, value `/var/tmp`. A project `.claude/settings.json` cannot set it;
   Claude Code drops that key from project env. Check: the scratchpad path in the system prompt
   starts with `/var/tmp/`. If it starts with `/tmp/`, tell the owner before writing anything large.

## Clean up what you create

When a task closes, delete its folders under the scratchpad and under `/tmp`. Keep only what the
proof report cites, and move that into `.alpaca/proofs/` first. On 2026-10-04 one long session
had left 30G across 1,542 scratchpad folders.

A sweep backs this up (`alpaca/disk.py`, rules under `disk:` in `project.yaml`). SessionStart
and SessionEnd start it in the background, at most every `sweep_every_hours` (6). It removes:

- the scratch folder of a session the record shows ended at least `ended_grace_hours` (12) ago,
  so a `claude --resume` within that window still finds its files;
- the scratch folder of a session the record shows open, or does not know, only after
  `open_session_days` (14) with nothing written anywhere in it: an open session may be idle,
  not dead;
- `/tmp` entries you own that match `tmp_globs` and have been quiet for `grace_hours` (48);
- with `alpaca retention gc --apply`, or in the same sweep, the `prune` rules for heavy run trees
  under `.alpaca/`: verifier trees of done tasks, rune2 run scratch of done tasks, and the tree
  copies of a release check attempt that no release receipt names (its check logs stay);
- each release gate command gets its own `TMPDIR` under `/tmp` (`ag...`), removed when the command ends;
- a release preflight removes its own candidate tree, seeds and gate checkouts when it ends and keeps
  only its gate logs; a release source checkout (`.alpaca/releases/source/<sha>`, about 100 MB) unused
  for 3 days is removed when the next one is made;
- linked worktrees under `.alpaca/worktrees/` named `op<nnn>-...` whose op is closed, whose HEAD
  is merged into main, that have no changes and have been quiet 3 days: `git worktree remove`
  (never `--force`) and `git branch -d` for the branch. Worktrees of open ops stay.

Nothing goes that a running process holds (cwd, executable, mapped or open file), that a sealed
proof cites or a proof report names (prune rules), or that has a `.keep` file at its top: put `.keep` in your scratchpad to keep it.
When the record cannot be read, no scratch folder is touched. Every removal is recorded as a
`disk-gc` event. `alpaca retention gc` shows the plan without removing anything. `alpaca doctor`
prints a `disk` line with the sizes and warns over `warn_gib`; there is no cap.

## pytest and `--basetemp`

pytest never deletes an explicit `--basetemp DIR`: it empties DIR when a run starts and leaves it
after the run, whatever `tmp_path_retention_*` in `pytest.ini` says. A new name per run leaks the
whole suite's temp each time (`/tmp/t173-full`, `-full2`, `-full3` held 13G of RAM).

- Running alone: leave `--basetemp` out. pytest's numbered dirs under `/tmp/pytest-of-<user>`
  follow `pytest.ini` and clean themselves.
- Running beside other pytest runs: give the run a private basetemp under `/tmp` (runs sharing the
  default dir rotate and delete each other's dirs), name it `/tmp/pytest-<task-id>-<n>`, and
  remove it in the same command: `trap 'rm -rf DIR' EXIT` or `...; rm -rf DIR`.
  `.alpaca/research-engine/wt.sh pytest <task>` does this for you.
- Never point `--basetemp` at the disk (`/var/tmp`, the scratchpad): those tests run about 100
  times slower there.

The `temp_guard` hook prints a note when a pytest command passes `--basetemp` and does not remove
it, and the sweep removes `/tmp/pytest-t-*` and `/tmp/op<nnn>-t-*` leftovers after 48 h.

## Why this exists

A Zebu emulator run once dumped nearly 100G of logs into `/tmp` and stalled the machine. No cap
is set on purpose, because long sessions must not stall waiting for quota. The protection is
where output goes, plus cleanup.
