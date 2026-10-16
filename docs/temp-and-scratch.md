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

## Inside a sandbox

An op's batch has a `sandbox` item. The owner answers it in words ("Writes stay in this project root and
/path/to/studio plus the session scratchpad; no sudo"). The write guard `alpaca/hooks/sandbox_guard.py`
does not enforce those words: three adversarial rounds each found a wording that a parser read wrong, so
the guard enforces only an explicit path list the owner confirmed, for the session that holds a task of
the op or last showed the owner its batch:

- The owner confirms the LIST, not its parts: the owner pastes the exact command line the readback
  prints, `alpaca sandbox set <op> --path root --path /path/to/studio --path scratchpad`, as a message of
  the session, typed after the op's sandbox question was asked (the batch shown with `alpaca batch show`,
  or `alpaca sandbox ask <op> --path ...`, which records the question and prints the line). The agent
  then runs that line with `--by owner --quote '<the pasted line>'`. The line must start a line of the
  owner's message and name exactly the paths of the call (order aside); one owner message confirms once.
  Only the newest owner message of the session that holds an `alpaca sandbox set` or `widen` line for the
  op confirms, and never one typed before the message a recorded list of the op rests on, so an older,
  wider line cannot be replayed once the owner pasted a newer one (SANDBOX-NOT-NEWEST). A confirmation
  typed at a terminal (the owner-word terminal path) is not accepted for a sandbox list: it leaves nothing
  to re-read and an agent with a shell can fake a terminal, so `set` and `widen` refuse it
  (SANDBOX-NOT-SESSION) and a row that claims it is ignored. A Codex session, whose owner words exist only
  at the terminal, cannot confirm a list.
  An entry is an absolute or `~/` path, a glob (`/tmp/op61-*`), or one of two words: `root` (this project
  root) and `scratchpad` (this project's scratchpad root, the folder that holds the scratchpad of every
  session of the project, so a resumed or forked session that was told to use another session's scratchpad
  may write there; it includes the older session folders under `/tmp/claude-<uid>`, which is RAM). A later
  `set` replaces the list; `alpaca sandbox widen <op> --path DIR` (the same pasted-line proof, with
  `widen`) adds to it.
- Never confirmable, whatever the owner pastes: `/`, `/home`, `/tmp`, `/var`, `/etc`, `/usr`, `/var/tmp`,
  any entry with fewer than two folders (`/srv`), a folder that is only a wildcard (`/home/*`), and
  anything that covers the home folder or the folder that holds the project root (the sibling projects),
  and a glob over the children of one of those protected folders: one whose text before the first wildcard
  is a protected folder or names fewer than two folders (`~/[a-z]*`, `/var/tmp/*x`, `/t*/x`), one whose
  wildcard stands for a protected folder (`/home/<use>?/x`), and one whose wildcard sits directly in a
  protected folder after fewer than 3 characters, leading dots aside (`~/.*`, `<project parent>/Q*`). A
  specific glob such as `/tmp/op61-*` stays allowed.
  Each entry is resolved once, at set time: a path that is, or passes through, a symbolic link is refused
  (confirm the real folder), and the stored form is never resolved again, so a link made later at a
  confirmed path moves nothing; a write through it lands outside and is denied.
- A list row in the record is honored only with its proof: the stored entries, the pasted line in its
  quote, and the owner message at the stored place in the transcript while that transcript exists. While
  a list is enforced, a tool call may not write the session transcripts (`<config>/projects/*/*.jsonl`) by name; a hard link made first (`ln` without `-s`) is not followed, a stated limit of the same-user kind.
- With no confirmed list nothing is enforced for the op, as before the guard existed. A session that
  works on several ops is held to the union of the lists of those that have one: claiming a task of an op
  with no list adds nothing and switches nothing off. The answer's reading is a PROPOSAL: under the
  `sandbox` item of the batch, after the answer is recorded and in `alpaca sandbox show <op>` ("not
  enforced: no confirmed path list"), the owner sees the proposed list, the entries left out as too
  broad, the clauses that proposed nothing with their reason, and the exact line to paste. A wrong
  reading grants nothing: the owner reads the list in the line before pasting it.
- A Write, Edit or Bash target outside the confirmed list is denied. The deny names the op, the event
  of the confirmed list and the path. Reads are never checked.
- How the proposal is read: by proof of permission, not by a list of prohibitions. The answer is cut
  into sentences and units (at `;`, a full stop, a comma, a colon, a parenthesis, and, or, plus, and
  before but, except, without, other than, with the exception of). A place is proposed only when its
  unit is made only of words from a closed vocabulary and says so ("writes stay in", "write only
  inside", "read and write only inside", "go to", "is allowed"). Any other word, a negation, a read
  word, an exclusion or a question proposes nothing. The reader knows English only; a confirmed list
  only adds, so a refusal inside a proposed folder ("but not /srv/studio/secrets") is shown, and the
  owner confirms a narrower folder to keep it out.
- Always allowed, whatever the list says, and named in every deny, in `alpaca sandbox show` and in the
  sandbox question the owner is asked: the project root, `.alpaca/`, the session scratchpad,
  `~/.claude/`, `$CLAUDE_CONFIG_DIR`, `/dev/null` and the other device files, and the session temp root.
- The session temp root is `/tmp/sbx<first 8 characters of the session id>`: 16 bytes, one folder per
  session, made mode 0700 by the harness (a folder another user or session made there first is refused
  and never allowed), with a marker file `.alpaca-sandbox` holding the full session id. It is never inside a
  project tree (a temp root under the project made the tests find the project and write into it) and it
  is short on purpose: Chrome's singleton socket and the research egress socket must stay under the kernel's
  107 bytes (`/tmp/sbx<sid8>/pytest-of-<user>/pytest-<N>/popen-gwN/tempfile/com.google.Chrome.XXXXXX/SingletonSocket`
  is 106 bytes with an 8-character user name; each extra character of the user name counts).
  TMPDIR and `PYTEST_DEBUG_TEMPROOT` point at it, through a line SessionStart puts in the Claude env file,
  so a pytest run with no `--basetemp` leaves nothing else in `/tmp`. It is on RAM like the rest of `/tmp`.
  SessionEnd removes it, and the disk sweep removes one left by a dead session, both by the marker
  (never by the 8-character name alone, which two sessions can share). Everything else in `/tmp` stays
  outside the sandbox; a leftover from before the guard is not yours to remove.
- A session started before this was deployed has no env wiring until its next SessionStart; the guard
  denies a `pytest` call there that names no `--basetemp` and no `TMPDIR=` (but not `--version`, `--help`
  or `--collect-only`, which write nothing).

The guard also reads what a command sets for itself: a variable (`B=/x; rm -rf $B`, `export TMPDIR=/x;
touch $TMPDIR/f`, a `TMPDIR=/x pytest` prefix, `B=/x bash -c '...'`, `for B in /x; do`, `B=(/x)`,
`HOME=/x` before a `~`, and `${B:-/x}`, which reads as its default when the command did not set `B`), a
wrapper's option values (`sudo -u root`, `env -u X`, `timeout -s KILL 5`, `timeout 0.5`, `stdbuf -o L`,
`nice -n 5`, `ionice -c 3`, `exec -a NAME`, `uv run --with X`, `flock -w 5 FILE`, whose FILE is a write
too), the folder `env -C DIR`, `sudo -D DIR` and `uv run --directory DIR` set, and a line that ends in a
backslash. It reads the text in one pass as bash does: the quote state carries across lines (a commit
message over several lines, then `cat > f <<'EOF'`), a here-document ends only at its exact delimiter
line (`<<END-OF` included), and no here-document starts in a comment, an arithmetic `$((1<<X))` or a
here-string. A comment that ends in a backslash does not continue. A `cd` in a pipe, in the background
or after `coproc` moves nothing. The guard's own folder `.alpaca/sandbox/` (cache, env file, denial log)
cannot be written by name by a tool call (a hard link made first is a stated limit), also not by a copy, move or link whose source lands there (`cp -r
stage/sandbox .alpaca/`, `rsync -a stage/ .alpaca/`) or by an archive extracted into `.alpaca`; a cache
entry whose session id or hash does not match is ignored. A command over 64 KB (after here-document
bodies are cut), or one the guard cannot read within its 2 s parsing budget ("command too long or complex
for the sandbox guard; split it"), is denied; it is never skipped. The clock is read inside variable
expansion too, and a variable value over 64 KB, more than 1 MB of values in one command, or running out of
memory or recursion depth while reading gives the same deny. A command the tokenizer cannot read (an
unbalanced quote, `"$(echo '"')"`, `$'it\'s'`) is scanned conservatively for its write shapes
(redirections, `tee`, `cp`, `mv`, `rm`, `mkdir`, `touch`, `ln`, `install`, `dd`, `sed -i`): a target
outside the list is denied, anything else is not. No real command of the ~25,000 in the transcript sweep
took more than 7 ms, and none of the 10 the tokenizer cannot read is denied.

pytest inside a sandbox falls back to `/tmp` when TMPDIR is gone, so a pytest call that drops it
(`unset TMPDIR`, `env -u TMPDIR`, `env -i` or `env -` without setting TMPDIR again, `export -n TMPDIR`,
`declare +x TMPDIR`, or an empty `TMPDIR=`, which `tempfile` skips) is denied, not warned: pytest's temp
root and every `tempfile` call of the tests would land in `/tmp`. Dropping only `PYTEST_DEBUG_TEMPROOT`
is fine (pytest then uses TMPDIR, the session temp root).

KNOWN LIMITS. The guard reads the command text; it cannot see:

- a write made inside a program: a script, `make`, `npm`, `python -c`, an `alpaca` verb, git's own files,
  a release gate (each child gets its own TMPDIR under `/tmp`);
- a target built at run time (`$VAR` the command did not set, `$(...)`, `eval`), or by a command that runs
  other commands (`xargs`, `find -exec`); `sort -o FILE`; `env -S` (a split string hides the command); a
  process substitution;
- a symlink made earlier in the same command and written through (`ln -s OUT l && echo > l/f`): links
  that already exist are resolved, one made by the same command is not;
- `cp -at DIR` (combined short flags that end in `t`) and `tar -C<dir>` (a value stuck to the flag);
- a redirection glued to a separator (`)>f`, `;>f`, `&&>f`, `|>f`) and `<>`; a `#` inside a word, which
  cuts the line like a comment; the `function` keyword; `$(...)` inside double quotes and backticks; a
  third nested `bash -c`; a brace list (`{OUT,/var}/f`); `pytest -o cache_dir=` and `--junitxml=`;
- a bare `mktemp`, which uses TMPDIR, or `/tmp` in a session whose Bash calls are not wired;
- the research egress folder `/tmp/alpaca-egress-<uid>` (`alpaca/research/paths.py` `egress_dir`): it
  uses `XDG_RUNTIME_DIR` when set, else `/tmp`, and ignores TMPDIR (its socket path must stay short), so a
  sandboxed session or test run that starts the research engine can still leave that folder in `/tmp`;
- the other clauses of the answer (no installs, no sudo, network);
- the free-text answer itself: it is only the proposal (English only, a closed vocabulary); what binds is
  the confirmed list, which only adds paths, so an exclusion inside a confirmed folder or the project root
  is not enforced.

Two more limits: the session temp root takes the first 8 characters of the session id, so two ids that
share them (Codex ids) share the folder name and are told apart by the marker only; and a widen race: a
hook that read the record just before `alpaca sandbox set` or `widen` can rewrite the cache after the verb
cleared it, for up to 20 s.

Round 5 limits: the same Unix user can write the record and the transcripts with a program (`sqlite3`,
`python -c`), so a list row that names a transcript that is gone is checked for its own consistency only
(a row that claims the terminal path is never honored); an archive extracted into the project root may hold `.alpaca/sandbox/`; a
function body is read as if it ran (`f() { unset TMPDIR; }; pytest` is denied though `f` never runs);
the scan of an unreadable command does not look inside `$(...)` within double quotes either; and a harness
place (`~/.claude`, `$CLAUDE_CONFIG_DIR`, `.alpaca/`) replaced by a link to a specific folder is followed
(one to `/` or to a folder too broad for a list is dropped).

The door self-check at the op's phase transitions stays the second line for all of these. A record that
cannot be read within half a second uses the last cached answer when it is at most 200 s old and gives no
decision otherwise, and any error in the guard is "no decision": the call runs.

## Why this exists

A Zebu emulator run once dumped nearly 100G of logs into `/tmp` and stalled the machine. No cap
is set on purpose, because long sessions must not stall waiting for quota. The protection is
where output goes, plus cleanup.
