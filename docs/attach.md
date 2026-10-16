# Attach mode

Attach mode runs Alpaca beside a repository that already exists and that other people keep committing to. The Alpaca
install (the studio) keeps the record, the doctrine, the proofs and the agent setup. The existing repository sits in a
folder of the studio (`app/` by default) as its own git clone and never receives an Alpaca file. Agents change it only on
work branches, and a change leaves the studio only as a branch and a pull request, after its checks pass and somebody
approved the exact commit.

A project is in attach mode when `project.yaml` has an `attach:` block. A project without the block behaves exactly as
before. Use attach when the repository stays shared (other committers, a pull request flow, or you want the repository
free of Alpaca files). An in-place mode, with Alpaca files inside an existing repository, is not built in this version, so an existing repository is always attached; the first chat says so when you allow Alpaca files in your repository.

Onboarding that picks the mode (who else commits, may Alpaca files live in the repository) is in `docs/onboarding.md`.
This guide follows one change from attach to sync. `design/attach-mode/definition.md` holds the definition and the
acceptance table, and `design/attach-mode/build-plan.md` the design choices (P1 to P24) that the sections below cite.

## The flow

```
alpaca new --attach <git-url|path>      make the studio, clone the repository into app/, write attach:
alpaca attach status                    the settings, the work folders, the handoffs
alpaca attach study                     scouts write the codebase map (design/attach/codebase-map.md)
alpaca task claim <task>                cuts alpaca/<task> from a fresh upstream base into its own work folder
  ... edit and commit in .alpaca/attach/worktrees/<task>
alpaca attach check <task>              the gates on the head and on a merge preview, judged against the baseline
alpaca attach approve <task> <sha>      the owner (or an agent under a delegation) approves the exact head
alpaca attach handoff <task>            dry run: branch, commits, files, gates, preview, behind count, text
alpaca attach handoff <task> --execute --expect <sha>     push the branch, open the pull request
alpaca attach merge <task> --execute --expect <sha>       merge it (only under an approval that covers merge)
alpaca attach sync                      fetch the upstream, report merged, closed and behind handoffs
```

`alpaca attach init <source>` attaches a repository to a studio that exists already (`alpaca new --attach` runs it for a
new one). Its options include `--base`, `--branch-prefix`, `--gates-file`, `--no-pr-footer` and
`--commit-name` with `--commit-email` (see "Known limits" for the last three). Every verb ends with one line, `GATE alpaca-attach-<verb>: PASS|FAIL|BLOCKED (reason)`, or with `--json` one
object with the keys `verdict`, `task` and `receipt`. Exit codes follow `alpaca/cli.py`: 0 PASS, 1 FAIL, 2 BLOCKED, 64 usage.

## Attach, status, doctor

Init clones the repository, senses the gates from files it finds (`Makefile`, `package.json`, `pyproject.toml`,
`CMakeLists.txt` and similar) or reads `--gates-file`, and refuses to write anything when it finds no gate. It sets the
commit identity of the clone to `attach.commit_name` and `attach.commit_email` (default `Alpaca agent <alpaca-agent@localhost>`, P13), adds the clone and the work folders to the
studio's `.git/info/exclude` (P2), and sets `remote.<upstream>.pushurl` to `alpaca-handoff-only:refused` (P6): a plain
`git push origin` from the clone fails, and only a handoff pushes. A push that names the remote is held by that trap. A push that spells the upstream's address out is held only
in part: init writes one `url.alpaca-handoff-only:refused.pushInsteadOf` entry for each of the listed spellings of the address
as it was configured (the address itself, with and without a trailing `.git`, with a trailing slash, and for a host address the
https, http, git, `ssh://` and `git@host:path` forms; for a GitHub address with capitals in owner or repo, also the lower-cased
form, because a prefix rule cannot match case-insensitively), and git rewrites a push to any of them into the same refusing
address. Any other spelling of the same repository still reaches it, because git compares the rewrite prefix as written. Examples
the check found: user information added (`https://user@host/...`), `//` or `/./` in the path, `file://localhost/...`, a relative
path, a port (`https://github.com:443/...`, `ssh://git@ssh.example.com:443/...`), a leading slash after the colon, a host in another
letter case, and a GitHub owner/repo in a case that is neither the configured one nor lower case. The list is not complete and is
not meant to be. The rewrite is a prefix match and git has no exact match, so a different repository whose name starts with the
upstream's (`.../widgetx.git`, `<dir>/upstream-work`) is refused too from the clone; that over-block is deliberate, because the
stem with no `.git` must be held. The same-user residual stands as before: a script run as the same user can rewrite any config.
The handoff push is not affected: it runs from a bare folder with a config of its own that does not read the clone's config.
`alpaca doctor` reports the entries next to the pushurl trap. A studio attached before these entries existed has none: doctor warns
(the pushurl trap still holds), and only a new studio gets them; adding them by hand changes the config digest. It records an `attach-init` event with the URL and a
digest of the clone's git config (P8). `alpaca attach status` and `alpaca doctor` report the mode, the clone, the upstream,
the config digest, the push checks and the work folders.

The URL, the config digest and the work folders are read from the record and from derived paths, never from a file under
`.alpaca/attach/` (those files are output for people). Every attach verb that talks to the upstream first checks that the
clone's git config still has the digest recorded at attach (`remote.*`, `url.*`, `core.hookspath`, `filter.*`, `merge.*`,
`credential.*`, `include*`, `user.*`, `core.worktree`); a changed config refuses the verb until it is restored.

## Work and check

`alpaca task claim` cuts the work branch `<branch_prefix><task>` (default `alpaca/t-123`) from a freshly fetched upstream
base into `.alpaca/attach/worktrees/<task>` and records the baseline of that base: the sha and the exit status of each gate.
Agents commit on the work branch in that folder; `app/` itself stays on the upstream base.

`alpaca attach check <task>` runs the gates twice in throwaway clones, once on the branch head and once on a merge preview
(the branch merged into the newest fetched upstream base), and compares both with the baseline. A gate that passed at its
base and fails at the head or in the preview is a regression and fails the check. A gate that was already red at its base
is marked red at base: the check passes, but the handoff then needs an owner approval and the pull request says so. A merge
conflict fails the check and names the files. v1 compares exit status only. The result is kept in the record as an
`attach-check` event, which `handoff` reads.

## Approve

```sh
bin/alpaca attach approve t-123 FULL_40_CHARACTER_SHA --by owner
bin/alpaca attach approve t-123 FULL_40_CHARACTER_SHA --by agent --delegation docs/delegations.md#Attach-trial
bin/alpaca attach approve t-123 FULL_40_CHARACTER_SHA --by owner --revoke
```

An approval is a private receipt of attach's own kind (`attach-approval`, event `release-attach-approval`) for one task and
one full head sha, which must be the head of the work branch now. It binds the repository (`attach.repository`), the
upstream URL, the work branch, the base branch and the digest of the gate set. A later change to any of them voids it, and
`approve` must be run again. The receipt kinds do not meet: a `release approve` of the same sha authorizes nothing here,
and an attach approval authorizes no release publication.

- `--by owner` is typed in an interactive terminal outside an agent session (the command asks for the first 12 characters
  of the sha). It covers `push` and `pr`. `--actions push,pr,merge` adds the merge.
- `--by agent` needs `--delegation PATH#Section`. PATH is a file of the project and Section a heading in it. The section must
  hold exactly one line:

  ```
  attach-delegation: <dec-id> <owner/name> <actions>
  ```

  `<dec-id>` is a decision of the record (`dec-` and 12 hex characters), `<owner/name>` must equal `attach.repository`, and
  `<actions>` is a comma list of `push`, `pr` and `merge`. The approval covers at most those actions (`--actions` can
  narrow them). The receipt keeps the file's hash. The section is read again at every use, so removing the line, or a
  line for another repository, withdraws the delegation and every approval made under it.
- A check that found a gate red at its base (or at the merge target) needs an owner approval. An agent approval of that head is
  refused when it is made, and a handoff refuses an agent approval when the check says so.
- A revocation of the same sha wins when it is later; a later approval wins over a revocation. An agent may revoke without a
  delegation. Lookup fails closed: it verifies the event chain, and a receipt that is missing or edited refuses every
  approval until it is restored, so deleting a revocation cannot bring an earlier approval back.

## Handoff

`alpaca attach handoff <task>` is the dry run. It refuses unless all of these hold, and pushes nothing:

- the work folder is sound, clean and at the head of its branch, and the clone's git config is as attach left it (every
  local key counts except the `core.*` keys git writes itself, `alias.*` and `branch.*`; an attach made before this rule
  has a digest that no longer matches, and there is no compatibility path: attach again);
- the latest check passed, is of this head and branch, used the gate set of now, and the upstream base is still where the
  check saw it (`git ls-remote` against the check's preview base; a moved base says "run the check again");
- the branch does not exist upstream (a second handoff of a pushed task is a new decision);
- an approval of exactly this sha covers `push` and `pr` under the project as it is now;
- the outgoing scan passes (see "What never leaves").

It prints the branch, the commits, the changed files, the result of each gate at the head and on the preview, the merge
preview (conflicts: none), how many commits the branch is behind the base, the pull request title and body, and whether the
repository has a pre-push hook. It records a `DRY_RUN` receipt (`attach-handoff`) with the whole plan. The title defaults to
the subject of the newest commit; `--title` and `--body-file` set them. The body is your text, then a generated section: the
task, the head, the gate results (exit status only), the red-at-base gates, and the commit identity. With
`attach.pr_footer: false` there is no generated section: the body is your text and, after it, a short list of the gates by
name and exit status ("Checks run on this branch and on a trial merge with the base branch"), with no tool name, path, task,
sha or identity in it. The setting is a line of the dry run's plan and part of the approval's binding, so changing it after
the approval or after the dry run refuses the push until the owner approves and reviews again.

`alpaca attach handoff <task> --execute --expect <sha>` pushes. `--expect` is required and must be the full head sha you
reviewed. The plan is computed again and must equal the reviewed dry run in every field (pass the same `--title` and
`--body-file`), or the command says which fields changed. Then:

1. the repository's own `pre-push` hook runs, as git would run it (`core.hooksPath` of the clone, else `.git/hooks`), in the
   work folder with the arguments `<upstream> <url>` and the push line on standard input, in an environment without any
   token (P7). Their hook decides: an exit other than 0 refuses the push. A `core.hooksPath` written to the clone's config
   after attach changes the config digest and refuses the verb until it is restored;
2. the approval, the config digest and the upstream base are checked once more;
3. the branch is pushed to the URL recorded at attach, never through the remote name. The push does not run in the clone: a
   fresh bare folder is made for it under `.alpaca/attach/push/` (after the hook has run) that has no refs, no config of the
   clone, no hook and no link to the clone's object folder. It holds a copy of exactly the objects that enter (reachable from
   the head and not from the base) plus the base side commits and trees git walks to find where the two meet, no blob of the
   base. Each object is read from the clone and checked against its name before it is written, so bytes changed under a name
   are refused, never copied. The copy's own walk must list exactly the objects the scan read, or nothing is pushed. The
   command names exactly one refspec, `<sha>:refs/heads/<branch>`, with `--force-with-lease=refs/heads/<branch>:` (an empty
   expected value: the branch must not exist upstream), `--porcelain`, `--no-verify`, `--no-follow-tags`, `--no-signed`,
   `--no-thin`, `--recurse-submodules=no` and the off value of `push.followTags`, `push.recurseSubmodules`,
   `submodule.recurse`, `push.gpgSign`, `push.pushOption`, `push.default`, `push.autoSetupRemote`, `push.negotiate`,
   `remote.pushDefault`, `core.commitGraph`, `core.multiPackIndex`, `pack.useBitmaps` and `pack.useSparse`, with no shallow
   file, replace ref or graft file read, for that process only. The folder is removed afterwards. A tag, a mirror refspec, a
   push option or a signing program planted in the clone's config adds nothing (`alpaca/tests/test_attach_handoff_push.py`
   plants each one between the last config look and the push, and reads the upstream afterwards), and neither does a forged
   commit-graph, a reachability bitmap, a store the clone's `objects/info/alternates` names, or an object whose bytes were
   changed (`alpaca/tests/test_attach_push_store.py`);
4. the result is checked three ways: git's porcelain report must name exactly one ref, a new branch at the approved sha; the
   upstream's whole ref list (`git ls-remote -- <url>`) is read before and after and must differ only by that branch; and
   `git ls-remote` must show the sha. When the first two do not hold, the branch is already upstream, so the receipt is
   `PUSHED_UNVERIFIED` (the error names the refs that differ), no pull request is opened, and a rerun refuses until a person
   has looked. A busy upstream can trigger it by moving another branch during the few seconds of the push. The remedy is to
   read the refs and, if the branch is not wanted, delete it upstream and run the handoff again;
5. the approval is checked again and the pull request is opened with attach's own GitHub client (`alpaca/attach/github.py`);
   an open pull request for the same branch and head is reused.

The receipt `PUSHED` holds the pull request address and number. When the push worked and the pull request step failed, the
receipt is `PUSHED_NO_PR` and the same command, run again, opens the pull request without a second push. `PUSHED_UNVERIFIED`
is not retried: see step 4. The GitHub token
comes from `ALPACA_ATTACH_TOKEN`, else `gh auth token`; it is never printed, recorded or passed to a hook, and a missing token
is found before anything is pushed.

## Merge

`alpaca attach merge <task>` prints the plan. `--execute --expect <sha> [--method merge|squash|rebase]` merges the pull
request of the handoff. It needs an approval of the head that covers `merge` (an agent approval under a delegation that names
`merge`, or an owner approval typed with `--actions push,pr,merge`), the pull request this studio opened for this head,
the pull request still open, at the approved head and into the base branch, and the upstream base where the latest check saw it.
The call pins the head sha, so GitHub refuses the merge when the branch moved. The default method is `merge`. The receipt
`attach-merge` holds the merge commit.

## Sync

`alpaca attach sync` fetches the upstream base (the only ref it moves is `refs/remotes/<upstream>/<base>`) and reports:

- merged: the handed-off head is an ancestor of the fetched base, or GitHub reports `merged_at` for the pull request (a squash
  or rebase merge makes new commits, so ancestry cannot see it);
- closed: the pull request is closed and was not merged;
- open: everything else, with a note when the pull request could not be read (no token, no network);
- behind: every work branch not merged that is behind the base, with the number of commits.

It never merges, rebases, deletes or pushes. When the codebase map exists, it refreshes the study base and lists the map
areas the new upstream commits touched. It writes one `attach-sync` event.

## What never leaves

The outgoing scan (`alpaca/attach/scan.py`) judges the commits and objects that would enter the upstream (reachable from the
head and not from the base), the branch name, the pull request title and the body. It reads them from the same kind of
checked copy the push is made from, walked over each commit's real parents (no commit-graph, bitmap, multi-pack-index,
replace ref, graft or shallow file), so what it reads is what the push can send. It refuses:

- sealed terms and the studio's own strings (its real path, the home folder, session and scratch paths, session ids, op ids,
  the project id); task ids such as `t-123` stay allowed because the branch is named after one;
- a blob that is byte for byte a file of the studio under any file name: every regular file of the studio's working tree,
  tracked or not, ignored or not, `.alpaca/` and `RESUME.md` included (a studio made by `alpaca new` has no commit, so its
  git history cannot be the list), plus the blobs of the studio's HEAD, index and object store. Left out are the folders that
  hold the attached repository's own bytes (the clone, `.alpaca/attach/worktrees|runs|stages|study|push`,
  `.alpaca/worktrees`, a research run's frozen repository files), git's `.git` folders, `.venv`, `__pycache__`,
  `.pytest_cache` and `node_modules`. Files are compared only at the sizes of the blobs that enter, so there is no size cap;
- `.alpaca/`, `ALPACA-MANIFEST`, `RESUME.md` and `CHECKLIST.md`;
- research pack files, by content and by path: the studio's `research/` folder, the folder of each recorded research runbook,
  and the frozen copies a research run keeps (`RESEARCH-PACK-IN-PUSH`);
- a commit by anyone but the attach identity, unless a project rule allows the mailbox.

A refusal names reason codes and digests, never the text that matched. A studio whose term list is missing refuses everything.

## Records and files

| What | Where |
| --- | --- |
| approval, handoff and merge receipts | `.alpaca/releases/receipts/<attempt>-attach-approval.json`, `-attach-handoff.json`, `-attach-merge.json`; events `release-attach-approval`, `release-attach-handoff`, `release-attach-merge` |
| checks | `attach-check` events; files under `.alpaca/attach/checks/<task>/` for people |
| sync | `attach-sync` events on op-0 |
| the URL, the config digest, the work branches | `attach-init` and `attach-branch` events |
| locks | `.alpaca/attach/locks/handoff-<task>.lock` |

## When a command refuses

| Message | What to do |
| --- | --- |
| no current approval of <sha> | `alpaca attach approve <task> <sha> --by owner` (the head may have moved) |
| the latest check ... judged <sha>, not the head | run `alpaca attach check <task>` again |
| the upstream base ... moved since the check | run the check again, then the dry run |
| the approval ... was given for another <field> | the URL, branch, base, repository or gates changed; approve again |
| the branch ... already exists upstream | hand-off is a new decision; rename or remove the branch upstream yourself |
| app git config changed since attach | restore `app/.git/config`; `alpaca doctor` shows the row |
| the branch ... was pushed, but the upstream's refs differ from the approved push (status `PUSHED_UNVERIFIED`) | read the upstream's refs; no pull request was opened and a rerun refuses while the branch is there |
| the outgoing scan refuses this branch (`STUDIO-BLOB-IN-PUSH`) | a file's bytes equal a studio file, wherever the studio keeps it; change the file's bytes or remove it |
| the plan changed since the dry run | run the dry run again and review it |
| the bytes the clone holds under <sha> are not that object | an object in `app/.git/objects` was changed or replaced; run `git -C app fsck`, restore the object, run the check again |
| the objects the push would send are not the objects the scan read | the clone's history changed after the scan; run the check and the dry run again; nothing was pushed |
| the push folder changed while git read it (status `PUSHED_UNVERIFIED`) | something wrote into `.alpaca/attach/push/` during the push; read the upstream's objects and refs before anything else |
| the repository's own pre-push hook refused the push | fix what the hook reports; nothing was pushed |
| the outgoing scan refuses this branch | the reason codes name the class; remove the file or text |

## Known limits

Attach mode v1 is built for an honest repository and an agent that follows its rules; it is not a sandbox. The residuals
below are declared decisions, repeated here so no reader finds them by accident.

- `dec-b94b827b8fa8` (t-649): hostile gate code. Gates are the owner's own commands for their own repository, chosen when the
  gates are confirmed. A gate, a test or a conftest that runs as the same Unix user as the check can make its own check pass,
  write the check's memory or seeds, or put a program first on PATH. The check does not defend against it. The fix is gate
  confinement (another Unix user, a container or a sandbox), a separate mechanism that does not exist in v1. The
  same holds for the repository's `pre-push` hook (and a hook path inside the work branch such as `.husky/`): it runs as the
  same user, in an environment without the token, and is trusted as the repository's own code. GitHub's mergeability check and
  branch protection on the pull request are the backstop.
- `dec-1e72e0111133` (t-676): the git text guard. It denies the git forms it can read, including every mid-word substitution
  form, but shell expansion it cannot predict (IFS splitting, `for`/`read`/`printf -v` variables, `$_`, programs that build a
  git call) is a declared residual. The block on pushes to the upstream rests on the push trap (`remote.<upstream>.pushurl`
  and the `url.<trap>.pushInsteadOf` entries for the listed spellings of the upstream's address, both set at attach), the approval of the exact commit and the handoff. An agent that holds a GitHub credential and a shell can
  still push to the upstream with its own commands. Credential isolation for the agent (a token the agent session cannot read)
  is the future fix; until then keep the token out of the agent's environment and `gh` login, and use the owner path.
- `dec-3148632eb3ef` (t-656): the owner-only guard. `approve --by owner` asks for the typed confirmation of
  `alpaca.release.cli.owner_confirmation`, which refuses an agent session and a non-terminal by looking at process
  properties (agent environment markers, a terminal on standard input). An agent that runs as the same Unix user can set
  those properties (clear the markers, run under a pseudo-terminal), so the guard is one place with the strongest
  same-user checks, not a proof that a person typed. A stronger confirm (another Unix user, or a secret outside the agent's
  reach) is a separate future mechanism for every owner write. Until then the owner approval of a handoff is as strong as the
  separation between the agent's session and the owner's terminal.

Three settings keep the tool's name out of the attached repository. Their defaults keep the behavior of the first version.
The first chat shows all three on its setup card, defaults included, and an owner who answers "no" to "May Alpaca files
live in your repo?" is shown a proposed trace-free set (commit identity `Maintenance bot <maintenance@localhost>`, footer off,
branch prefix `fix/`) that `onboard apply` writes as shown; an owner who attaches by hand sets them:

- `attach.pr_footer` (default `true`). The first chat's `pr-says-agent` answer `b` writes `false`, and `alpaca attach init
  --no-pr-footer` does the same. With `false` the pull request body is the approved body file plus a neutral gate summary
  (see "Handoff"); the dry run prints which of the two it will send. Residuals that remain: the work branch name
  (`attach.branch_prefix`, default `alpaca/`) is the pull request's head ref and the text of the owner's own body file is
  sent as written, so a body file that names the tool names it.
- `attach.commit_name` and `attach.commit_email` (default `Alpaca agent` and `alpaca-agent@localhost`; set both or neither).
  `alpaca attach init --commit-name N --commit-email E`, or a `commit-identity` answer on the first chat card, chooses them.
  They are written to the clone's git config at attach and recorded on the `attach-init` event, and every commit attach makes
  (a landed patch, the identity of a work-branch commit) and the outgoing author check use them. The identity is fixed at
  attach: editing it in `project.yaml` later is refused (the record is the authority, as for the URL), and so is a change of
  the clone's `user.*`, which the config digest covers. To change it, attach again in a new studio. Commits made before the
  choice, and the merge preview commit in the throwaway seed (never sent), keep the default identity.
- `attach.branch_prefix` (default `alpaca/`). The branch name is the pull request's head ref, so it is the third place
  the tool's name can show. The first chat card takes the repo's own rule from `CONTRIBUTING.md` when there is one, the
  owner's `branch-prefix` answer over that, and `fix/` as a proposal when the owner wants no trace and gave neither.

Smaller limits of this part:

- The push folder is checked, not sealed. A process of the same Unix user that writes into the fresh push folder between
  its proof and git's read of it (a new pack or an alternates line) is not stopped: git runs with the commit-graph, bitmaps
  and multi-pack-index off, and the folder is compared with its proof before and after the push, so a change is reported
  (`PUSHED_UNVERIFIED`, no pull request), but an object it adds could already have been sent. The same holds for the scan's
  copy while the scan reads it (a change refuses the branch).

- A small generic file whose bytes equal a studio file (a version string, a one-line config) is refused as
  `STUDIO-BLOB-IN-PUSH`. There is no size floor, because the rule is that the repository never receives an Alpaca file;
  the remedy is to change the file's bytes.
- Folders left out of the studio-file comparison (the list under "What never leaves") are trusted to hold the repository's
  or third parties' bytes. A studio file copied into `.venv` or `node_modules` of the studio is not seen by that rule (the
  sealed-term and path rules still apply).
- An agent approval is as strong as its delegation line. Attach checks that the file is a file of the project, that the
  section holds one well-formed line, that the repository matches and that the decision it names exists in the record. It does
  not prove the owner wrote the line: `ownerword.decision_is_owner` is false for the trial's own decision `dec-049934c5931f`
  at the time of writing, so requiring it would block the trial. The line is as strong as the write protection of the
  delegation file (the authority surface, `contracts/`). Autodrive levels grant no authority by themselves; an unattended
  session pushes, opens and merges only under an approval of the exact commit.
- The record and the receipt files are append-only by convention and hash-checked, not a security boundary against a process
  that can write the studio's files and database.
- A merge does not read the pull request's CI status. Required checks and branch protection are the upstream's to enforce.
- The dry run needs an approval (a person reviews the check and the work folder before approving). The owner who wants the full
  listing before approving can read `git log` and `git diff` in the work folder, which is what the dry run prints.
- `sync` reads the state of a pull request only with a token and network; without them it reports by ancestry and says so.
- The task `done` move is not tied to the pushed head yet (the done gate of build plan 6.8 is a later task).
