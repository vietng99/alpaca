# Release a clean public copy

Alpaca can keep private development material in one repository and publish an allowlisted copy to another. The owner first approves one exact development commit. A release of that commit builds twice, scans for leaks with the private term policy, proves the files survive a fresh Git clone, and runs every configured project check. Publication updates a sync branch and opens or updates a pull request. The owner merges it.

The flow is: private development, owner approval of a commit, private checks, clean release, public pull request, owner merge. The real term list, the scan verdicts and the check logs stay on the owner's machine. No hosted job and no public event can start the real-term scan or choose its inputs.

## Configure the project

Add a `release` mapping to the private project's `project.yaml`. Keep public barrier rules and a nonempty private term list available even when the development repository uses a private tier. Release checks force the public policy and refuse optional or missing terms.

```yaml
release:
  allowlist: ALPACA-MANIFEST
  section: mechanism
  repository: example/project
  target_branch: main
  sync_branch: sync/main
  public_base: REPLACE_WITH_REVIEWED_PUBLIC_MAIN_40_CHARACTER_SHA
  overlays:
    project.yaml: templates/release/project.yaml
    intents/queue.md: templates/release/queue.md
  generated_manifest: [python, setup/gen_manifest.py, --write]
  gates:
    - name: development-tests
      scope: dev
      command: [python, -m, pytest, -m, dev_files or dev_tree, -n, '2', --dist, loadgroup]
      timeout: 900
    - name: development-preflight
      scope: dev
      command: [python, setup/boot-check.py, --full, --no-suite]
      timeout: 1800
    - name: clean-tests
      command: [python, -m, pytest, -n, '6', --dist, loadgroup]
      timeout: 1800
    - name: clean-manifest
      command: [python, setup/gen_manifest.py, --verify]
    - name: clean-install
      command: [python, setup/release_checks.py, smoke]
      timeout: 1800
    - name: documentation
      command: [python, setup/release_checks.py, docs]
```

The SHA placeholder deliberately fails validation until replaced. Read the current public main SHA from the repository and review its content before recording it. Unknown options, malformed paths and duplicate or reserved gate names are refused. At least one project gate is required. `section: null` accepts a plain file of relative paths instead of a sectioned manifest.

A gate command is an argument list, not an interpolated shell string. A non-Python project can use `[npm, test]` or another installed tool. Configure its dependencies on the runner before the check step. Python commands use the interpreter environment running Alpaca. Timeouts kill the command group and fail the gate. A gate that edits tracked inputs also fails.

A check runs the built-in checks first (reproducibility, leak scan, fresh clone), then the project gates by timeout, shortest first; gates with equal timeouts keep their configured order. A short timeout therefore marks a cheap gate, and a failure in one shows before the long gates start. By default a failure ends the run early: a failed built-in check skips every project gate, and the first failed project gate skips the gates after it. They are recorded as `SKIPPED` with the name of the gate that failed first, and the receipt is FAIL, as it would be after running them. A failed built-in check also skips the built-in checks after it: a failed reproducibility check skips the leak scan and the fresh clone, and a failed leak scan skips the fresh clone. The leak scan's detail then names any candidate member that the candidate's own ignore rules hide, which the fresh clone reports when it runs. The final comparison of the inputs with the build always runs. Set `fail_fast: false` in the `release` mapping to run every gate after a failure, for example to collect every failing gate of a candidate in one run.

Project gates run one at a time unless the `release` mapping sets `parallel: N` (1 to 16), the number of gates that may run at once. They still start in timeout order, and the receipt lists them in that order whatever order they finish in. With fail fast on, the first gate that fails interrupts the gates still running (SIGINT, then SIGKILL after 60 seconds); they are recorded as `STOPPED` and the gates not yet started as `SKIPPED`. Turn `parallel` on only for gates that can share the machine: each gate has its own checkout, but gates that bind a fixed port, write one shared folder outside their checkout or need the whole machine fail together when they run at once. One at a time is the default because a project's own gates may do any of that.

A long test suite is faster spread over processes inside one gate than run beside another suite. `-n 6 --dist loadgroup` hands the suite to 6 pytest-xdist workers; `alpaca/tests/conftest.py` keeps each test file on one worker (its module fixtures are built once), spreads a few slow files of independent tests test by test, keeps the tests that run research sessions against the live engine files on one worker, and compiles the tree before any worker starts. `setup/bootstrap.sh` installs pytest-xdist from `requirements.txt`. `boot-check.py --full --no-suite` runs every boot-check row except its own run of the pytest suite, for a gate list that runs that suite elsewhere.

Alpaca's own release runs the full suite once, in the clean tree. Its `dev-pytest` gate runs only the tests a release tree skips (`-m "dev_files or dev_tree"`: tests that read development-only files or the development tree itself), and `clean-motion` runs the Studio's browser timing checks alone in one Chrome run, because beside a loaded suite, or split into several runs, they fail. The gates run one at a time. Measured on 2026-10-13 on the shared host: the whole preflight took about 20 minutes, against about 2 hours 53 minutes for the 1.2.3 release check (two full suites and a full boot check, one process each). Four gates at once took 58 minutes and failed a timing check.

Each gate command runs with a temp folder of its own as `TMPDIR` (`/tmp/ag...`), removed when the command ends, so test runs of other sessions on the host cannot rotate away the folders a gate is using.

Every check and gate that ran records `started` (UTC) and `duration_s` in the check receipt, so a release's time can be read gate by gate.

A retry reuses the project gates that already passed. When a check names a build whose tree hash, source commit, source hash, approval receipt and leak policy hash equal those of an earlier recorded check, and the gate definitions equal too (each gate's command, scope and timeout, the gate list, and the interpreter and host that run the commands), every project gate that passed there is carried over and only the failed, stopped, skipped or missing gates run again. Any difference runs every gate. The three built-in checks, the final comparison of the inputs and the leak policy read are never carried; only project gates carry. The latest real run of a gate on the build decides, in whichever recorded check of that build it happened (a check whose built-in checks failed, or whose final comparison failed, cannot supply a pointer but its gate runs still count, and so do runs under another interpreter): a gate whose latest real run failed or was stopped runs again, and a gate carries only when that run passed in a check that fits the rules above. If any recorded check receipt cannot be loaded, nothing carries. A carried entry keeps `status` `PASS` and `returncode` `0` and adds a `carried` pointer to the receipt that ran the gate (its path, check attempt, receipt hash, the gate's start time there) and `failed_runs`, the number of times that gate failed (status `FAIL`; a `STOPPED` or `SKIPPED` run is not a failure) in the earlier checks of the build, so retrying until a flaky gate passes shows in the receipt. A carried entry has no `started` or `duration_s`, is never itself a source for a later carry, and takes no part in the progress percent or the time left. Earlier checks are found through the event record and loaded with their hash verified, so an edited or unrecorded receipt carries nothing; receipts recorded before this feature carry nothing. Validation (and so publish) re-reads each pointer and refuses a carried gate whose receipt is not a recorded check of the same build, approval and leak policy in which that gate passed by running, or whose gate ran again, and failed or was stopped, in a recorded check of the build before this one, or whose `failed_runs` differs from the record. The toolchain beyond the interpreter (node, browsers, system packages, environment variables) is not part of a gate's definition: approving the commit again, which makes a new approval receipt, runs every gate.

## Watch a running release

While `release build`, `preflight`, `check`, `publish` or `tag` runs, it keeps a progress file under `.alpaca/releases/running/`, rewrites it every 30 seconds and removes it when it ends. The hub shows it as a bar in the sidebar, under the health meter: the step and the gates running now, the percent done, how many steps are done, failed or skipped, and the time left. The bar appears only while a release step runs, and the hub reads it in the same 10-second poll as the health meter (`/hub/vitals.json`, field `release`). On a phone the sidebar is a drawer, so the bar shows when the menu is open.

The time left comes from the median duration of each step over its last five passing runs in the recorded check and preflight receipts, with the gates that run at once counted as running at once. A step with no recorded run yet makes the time left unknown, and the bar says so instead of guessing; the first check or preflight after an upgrade records the timings the next one uses. Only check and preflight receipts record step timings, so `release build`, `publish` and `tag` show their step and progress with no time estimate. If the process of a release step dies, its file stops being rewritten: two minutes later the bar says the step ended without a result, ten minutes after that the bar disappears, and the next release step removes the file.

Development gates run on a captured checkout of the source files, with original configuration and no private runtime state. Release gates each receive a fresh checkout of the built distribution. Caches may be written there. Installation smoke uses a further disposable copy so onboarding cannot modify the checked candidate. Projects whose tests require instance data must supply an explicit isolated test setup; the engine never copies the live record into test clones.

### The upgrade gate

Alpaca's own gate list has `upgrade-previous` (`python setup/release_checks.py upgrade`). It proves that a project made by the last public release moves to the candidate build without losing its record. It needs the network: it reads the tags of `https://github.com/vietng99/alpaca.git` (`ALPACA_PREVIOUS_RELEASE_REPO` or `--repo URL` names another repository) with an anonymous `git ls-remote`, picks the newest `vX.Y.Z` tag strictly below `VERSION`, and fails with a reason, naming the step, when the network is down or no such tag exists. `--previous vX.Y.Z` names the tag instead, and may equal `VERSION`: between a release and the next version bump the default tests the release before the last one, so run `python setup/release_checks.py upgrade --previous v<VERSION>` to test the one users hold.

The gate works in a scratch folder under `.alpaca/release-upgrade`, removed when it ends. It clones the tag, runs that release's `install.sh --from` into a scratch prefix, and makes a project with the installed `alpaca new`, as a user does. It checks the project holds the tag's files, then runs an op, a task and a sealed proof with a done move (the same lifecycle as the installation smoke) and records what must survive: the verify event count, the doctor check levels, every column of every event row, the task list, the sealed proof's sha256, and the digests of the proofs, `project.yaml`, `README.md`, `intents/` and the project's own lines of `CLAUDE.md`. It then copies the candidate, bootstraps it, and runs `alpaca upgrade --target` from it, first with `--plan` (which must change nothing), then for real.

The gate fails, naming the step (`upgrade-previous: <step> failed: <reason>`), unless: the apply prints `GATE alpaca-upgrade: PASS` and leaves no journal; every file of a refreshed path holds the candidate's bytes; every module imports in the project's own environment; the project's `--version` equals the candidate's; `alpaca verify` passes and reads at least the earlier number of events; `alpaca doctor` ends in its own PASS or WARN line, prints no ERROR line and no scan that crashed, reads OK on its core checks, and has no check worse than before, gone, or new and not OK (a check renamed in a release needs an entry in `RENAMED` in `setup/release_checks.py`; a doctor WARN exits 1, so the lines are read, not the exit code; the checks that read the host, `uncommitted`, `disk`, `transcripts` and `observability`, may go from OK to WARN and the log names each); the earlier event rows read back unchanged and in order; the task is still done and its proof still checks; and the preserved files are unchanged. Two things it only reports as `NOTE` lines: a pinned line of `requirements.txt` that the project's environment lacks (`alpaca upgrade` refreshes the file and installs nothing) and a project `MANIFEST.json` that is the previous release's, not the candidate's.

Measured on 2026-10-14 on the shared host: the run took 38 s from v1.2.3 and 43 s from v1.3.0 with pip's wheel cache warm, and about 350 MB of scratch at its peak (the gate refuses to start with less than 3 GB free below `.alpaca/release-upgrade`). The gate has a 1800 s timeout, kept far above that because a cold cache or a slow network stretches the three bootstraps.

The allowlist is the shipment boundary. Missing paths, traversal, symlinks, special files, submodules and runtime paths are refused or excluded as documented by the manifest. The manifest's memory class is excluded even inside a selected directory. Overlays explicitly replace existing allowlisted files before manifest generation. Keep distribution templates clean and commit them with the private source. The generator may create its manifest; its output is checked for forbidden runtime material.

## Approve one development commit

```sh
bin/alpaca release approve FULL_40_CHARACTER_SHA --by owner
bin/alpaca release approve FULL_40_CHARACTER_SHA --by agent --delegation PATH#section
bin/alpaca release approve FULL_40_CHARACTER_SHA --by owner --revoke
```

An approval names a full commit SHA that exists in the development repository. It is a private receipt in the release record; its event row names the commit, the decision and the receipt's exact byte hash. An owner decision is typed in an interactive terminal outside an agent session: the command asks for the first 12 characters of the SHA and refuses when standard input is not a terminal or an agent session is detected. An agent approves only with `--by agent` and `--delegation PATH[#section]`, where PATH is a project file holding the owner's delegation; the receipt records the pointer and the file's hash at that moment. A later revocation of the same SHA wins.

Approval lookup fails closed. It verifies the event hash chain first, and any missing or edited decision receipt for that commit refuses the commit, so deleting a revocation cannot restore an earlier approval. Revoke and approve again to recover.

`release check` refuses to run anything until the commit it checks is the approved one and every release input is committed. Without that, the check receipt is BLOCKED with a single `approval` entry: no gate runs and the private term policy is not read. A new commit, an amended commit or an uncommitted edit needs its own approval. Checks re-read the approval when they finish; validation re-reads it before it reads the term policy; publication re-reads it before it pushes and again before it opens the pull request. A revocation, or a re-approval that creates a new receipt, makes older checks stale. It also stops their passed gates from carrying over to a later check.

`release build` does not need an approval. It runs the configured manifest generator on the owner's machine with publication credentials removed, as any local development command does; it reads no term policy itself.

## Preflight a commit before approving it

`release preflight` runs the project gates on a commit before anyone approves it: the checked-out HEAD, or the commit named with `--commit`. It builds the commit's candidate from its own checkout, then runs the gates in the order and with the fail-fast rule a release check uses. It prints JSON with the status, the first failing gate, the first failed test line found in that gate's log (pytest's `FAILED` and `ERROR` lines) and the log path, and exits 0 on PASS and 1 on FAIL. Its receipt is recorded with kind `preflight`.

A preflight needs no approval and reads no term policy. Like `release build` it runs the named commit's own commands (its manifest generator and its gates, as its `project.yaml` lists them) with your account and with publication credentials removed, so preflight only commits whose code you would run on this machine anyway. The leak scan, the reproducibility build and the fresh clone wait for the release check, and a preflight receipt is never evidence for publication. Run it on the commit you mean to approve: a commit that passes its preflight rarely fails its release check, and a commit that fails it costs no approval, no build and no full check.

## Run the door self-checks before approving

A release carries the work of one or more ops. Before `release approve`, run the door self-checks of each op whose work the candidate commit carries, for every door its tasks have reached (design->build, build->verify and, once the op has a release task, verify->release):

```sh
bin/alpaca selfcheck brief OP BOUNDARY        # writes the packet and its prompt.md
# a fresh checking agent reads prompt.md and writes result.json
bin/alpaca selfcheck submit OP BOUNDARY RESULT_JSON
bin/alpaca selfcheck status OP                # each door needs a current PASS
```

Approve only when every door of those ops shows a current PASS. A self-check reads the op's deliverables as they stand, so run it on the tree of the commit you mean to approve; a finding that changes code needs a new commit, a new preflight and a new self-check. The order matters because a self-check after the check costs a whole check: in 1.2.3 the verify->release self-check found a product bug after the first passing check and forced a rebuild and a full re-check, and in 1.3.0 op-056's build->verify self-check failed during the release, so its fixes reached main only after 1.3.0 shipped. See [intent fidelity](intent-fidelity.md) for how the self-check and its findings work.

## The release reads its own checkout of the commit

`release build` builds one commit: the checked-out HEAD, or the commit named with `--commit FULL_40_CHARACTER_SHA`. It first makes a private checkout of that commit at `.alpaca/releases/source/<sha>` and builds from there. `release check` and `release publish` read the commit's files, its release configuration and its history from the same checkout. The project root keeps the record, the receipts, the private term list and the scratch of each step.

Other work can therefore go on in the project root while a release runs: an edit, a commit or a merge there does not change what the release reads and does not fail its checks. An uncommitted edit in the project root is not part of the release either; commit it, approve the new commit and build again to ship it. An edit inside the checkout itself still fails the check and blocks publication.

The checkout shares Git objects with the project repository and leaves out `.alpaca/`, which holds no release input. A checkout unused for three days is removed when another one is made; a later step on the same commit makes it again byte for byte.

## Local sequence

```sh
bin/alpaca release init
bin/alpaca release preflight                  # or: release preflight --commit FULL_40_CHARACTER_SHA
bin/alpaca selfcheck status OP                # every door of each op in the release: current PASS
bin/alpaca release approve FULL_40_CHARACTER_SHA --by owner
bin/alpaca release build                      # or: release build --commit FULL_40_CHARACTER_SHA
bin/alpaca release check BUILD_RECEIPT_PATH
bin/alpaca release publish CHECK_RECEIPT_PATH --dry-run
bin/alpaca release publish CHECK_RECEIPT_PATH --execute --expect DRY_RUN_RECEIPT_PATH
# after the owner merges the sync pull request:
bin/alpaca release tag PUBLISH_RECEIPT_PATH --version X.Y.Z --notes NOTES.md --dry-run
bin/alpaca release tag PUBLISH_RECEIPT_PATH --version X.Y.Z --notes NOTES.md --execute --expect DRY_RUN_TAG_RECEIPT_PATH
```

Build and check print JSON with the receipt path for the next command. Publish defaults to a dry run. It writes a report with file changes, new top-level paths, large files and the source commit. Review that report before executing. Actual publication requires committed source files, a current approval and the `PUBLIC_REPO_TOKEN` environment variable. Credentials are never part of project configuration or receipts, and project gate/generator commands do not inherit publication credentials.

Receipts live under local release state and have an exact byte hash in the shared event record. They bind source files, the source Git commit, the approval, the built tree, private policy and every gate. Editing a file in the release checkout, the candidate tree or the term list invalidates old checks. After any such change, approve, build and check again. Exit 0 means success or a successful dry run, 1 means failed checks, 2 means blocked, and command usage errors are reported by the CLI parser.

The publisher clones public history separately and creates a generated commit whose parents are public commits. It never copies private commit messages or ancestry. It compares the actual staged Git blobs and modes with the checked inventory, then scans the outgoing objects, the generated commit metadata, the PR title and the PR report before pushing. The publish receipt freezes what goes out: the sync ref, the public base, the commit, its parents, its tree, its message hash, the title, the report hash and the approval id. `--execute` requires `--expect` naming the reviewed dry-run receipt for the same checks, and refuses unless everything except the commit id (which carries a timestamp) matches it. A changed public base or report therefore needs a new dry run and review. The pull request receives exactly the scanned title and report. Public main is never updated by this command. Sync updates use an exact expected-old-ref lease; a concurrent change blocks publication.

The first release records the source tip as its baseline. Later releases list every development commit since the previous published source in the report, generated commit and receipt. API retries preserve that list. The candidate must descend from the previous source; older retries, unrelated history and missing history are blocked. Use a full development clone when publishing.

A previous normal merge is recognized only when ancestry and tree content agree. An unexpected public commit, edited sync branch or unsupported squash/rebase history stops publication for owner review. Do not reset public history to make the check pass. Resolve the discrepancy and explicitly review the next base. If a push succeeds but the PR API fails, the receipt keeps the pushed commit; retrying unchanged checked inputs under the same approval reuses that commit and updates the existing open PR.

## Watch tasks after a publish

After `release publish --execute` passes and opens a pull request, Alpaca files two open tasks, due 1h and 24h after the release went public: the time of the first passing publish receipt for these checks, written in UTC in each task's contract. The tasks join the `release-watch` group, with no prerequisites and no resources.

To name the op and the ship runbook yourself, add the two flags to the executed publish, which stays the owner's step:

```sh
bin/alpaca release publish CHECK_RECEIPT_PATH --execute --expect DRY_RUN_RECEIPT_PATH --watch-op OP --watch-runbook SHIP_RUNBOOK_PATH
```

The op is the one named by `--watch-op OP`; a closed or unknown op named there files nothing. Without it, it is the one op whose tasks this session holds with a live claim (a task of a standing op does not count). When there is no such op (the shared `cli` session holds none, and a session with tasks in two ops has no single one), or that op has a batch (its tasks need a phase) or no room in its plan, the tasks go to the standing Release watch op: opened at the first such publish, found again by its `release-watch-op` marker event (never by its title), left open when its plan is full and replaced by a new one, and, like the After-action items op, never the current op.

The checks come from the 1h and 24h watch stages of the ship runbook given with `--watch-runbook PATH` (60m counts as 1h, 1d as 24h; only the checks a watch looks at). Without it they are: release check receipt, hub reachable, install from the published tag. A runbook that does not check, or is not `kind: ship`, files nothing. Publishing ends at a pull request, so the tag check can pass only after `release tag`.

A rerun of publish for the same check receipt files nothing new and keeps the due times. A new check receipt for the same commit is a new release to watch. When nothing is filed (an op named by `--watch-op` that is closed, unknown or needs a phase, a runbook that does not check, a record error), the `watch` field of the output says why and gives the `alpaca task add` command; the publish result and the exit code do not change.

## Tag the release and write its release page

Publishing ends at the sync pull request; it creates no tag and no GitHub release page. After the owner merges that pull request, `release tag` gives the merge commit the tag `vX.Y.Z` and creates the release page titled `Alpaca X.Y.Z` with the owner-reviewed notes, marked as the latest release unless `--not-latest` is given. It reads the publish receipt and refuses unless:

- that receipt is an executed publish (status PASS) whose pull request is on the configured repository;
- the pull request is merged into the target branch, and its head is the commit that receipt pushed;
- the merged tree's `VERSION` in `alpaca/__init__.py`, when the file exists, equals `--version`;
- the notes are nonempty, under 100,000 characters and pass the same leak policy as the PR report;
- no tag of that name points at another commit.

A dry run (the default) records the plan: repository, pull request, tag, target commit, title, latest flag and the notes hash, and keeps a copy of the notes beside the receipt. `--execute` requires `--expect` naming that reviewed dry-run receipt and refuses if anything in the plan changed. If the tag and its release page already exist on the same commit, the command records PASS and writes nothing. Write the notes for readers of the public repository: what is new, what changed and what was fixed, with no development task ids, host paths or private names.

## What the public side sees

The public copy carries only a minimal statement of success. The sync commit message has the trailers `Alpaca-Base`, `Alpaca-Source`, `Alpaca-Tree`, one `Alpaca-Covered` per development commit and `Alpaca-Approval`, a random id of the private approval receipt. The PR report states that all checks passed and lists the file changes. Neither names a gate, a log, a policy digest, a term hash, the approver or the delegation. Development SHAs are opaque hashes of private commits; they reveal the number of private commits in a release and nothing about their content.

A public reviewer checks the issuer and the bytes:

1. The pull request comes from the `sync/main` branch of the public repository itself. Only accounts with write access to that repository can push there, and GitHub shows the account that opened the pull request.
2. `git cat-file -p COMMIT` shows a tree equal to the `Alpaca-Tree` trailer, and the commit's parents are public commits.
3. The public test workflow passed on that commit.

Failed scans are never sent anywhere. A leak finding stops the command locally with a generic message; the detail stays in the local receipt.

## GitHub workflows

Initialization writes the development workflow to `.github/workflows/alpaca-dev.yml` and a public workflow draft under local release state. `--dev-output` and `--public-output` select different relative paths. Repeating initialization is safe for identical files; any edited destination is refused before writing either file. Templates are rendered by `alpaca/release/workflows.py` and pin Actions to full reviewed commits.

Both workflows only run the project's `commands.test` on Python 3.10 and 3.12 through `bin/alpaca-python -m alpaca.release.ci test`. They request read-only contents permission and reference no secrets. The development workflow runs on pushes to the private repository; it builds no release, runs no real-term scan and holds no publication credential. The public workflow runs on `pull_request` only. It has no `pull_request_target` trigger, so a public contributor's pull request, comment or label cannot start a job with secrets, and no public job knows the private terms.

Install the public draft as `.github/workflows/alpaca-public.yml` in the public distribution and add that exact file to its allowlist. Keep the development workflow out of the public allowlist. Do not copy the whole private `.github` directory. Do not store the private term list in any repository secret.

## Credential

Publication runs on the owner's machine. Create a fine-grained GitHub token limited to the one public repository with Contents and Pull requests read and write access (Contents write also covers tags and release pages). Add Workflows write only if the release must change files under `.github/workflows/`; GitHub refuses such pushes without it. Keep the token in a file outside the project that only the owner can read (mode 600), and pass it for one command:

```sh
PUBLIC_REPO_TOKEN="$(cat PATH_TO_TOKEN_FILE)" bin/alpaca release publish CHECK_RECEIPT_PATH --execute --expect DRY_RUN_RECEIPT_PATH
```

Do not put the token in workflow files, project configuration, receipts, logs or PR text. Do not reuse a broad personal login token.

## Alpaca's own configuration

The example gates cover full pytest, full boot checks, manifest integrity, a fresh bootstrap/onboard/doctor/operation/task/proof cycle, an upgrade of a project made by the previous public release (`upgrade-previous`), regenerated manual HTML, ASCII shipped HTML and README local links. The built-in gates always cover two-build equality, leak checks and a fresh clone. The clean distribution templates under `templates/release` must remain instance-free. Hosted test results and local receipts are separate evidence: a local PASS does not claim a GitHub run occurred, and a hosted test run does not claim a release check.

## Version numbers

`bin/alpaca --version` prints `Alpaca 1.3.1`, and the hub header shows the same. The number lives in one place, `VERSION` in `alpaca/__init__.py`, and names the release the development tree is heading to.

Whether a tree is a development checkout or a release is kept beside the number: the hub tooltip says "Development build at commit ..." or "Release build", and `alpaca status --json` carries `channel` (`dev` or `release`). For a bug report, `alpaca.version.info()["build"]` gives one SemVer string such as `1.2.0-dev+g03ae15d0f9fc.dirty` (dirty means uncommitted changes under `alpaca/` or `bin/`); a release build's is the bare number. The channel lives in `alpaca/_channel.py` (`CHANNEL = "dev"`); the release overlay `alpaca/_channel.py: templates/release/channel.py` in `project.yaml` swaps in `CHANNEL = "release"` at build time, so no one edits the channel by hand.

To cut a release, bump `VERSION` in a development commit, approve that commit, then build, check and publish as above. After the owner merges the sync pull request, tag it and write its release page with `release tag`. Then bump `main` to the next number.
