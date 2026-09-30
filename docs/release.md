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
      command: [python, -m, pytest]
      timeout: 1800
    - name: development-preflight
      scope: dev
      command: [python, setup/boot-check.py, --full]
      timeout: 3600
    - name: clean-tests
      command: [python, -m, pytest]
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

Development gates run on a captured checkout of the source files, with original configuration and no private runtime state. Release gates each receive a fresh checkout of the built distribution. Caches may be written there. Installation smoke uses a further disposable copy so onboarding cannot modify the checked candidate. Projects whose tests require instance data must supply an explicit isolated test setup; the engine never copies the live record into test clones.

The allowlist is the shipment boundary. Missing paths, traversal, symlinks, special files, submodules and runtime paths are refused or excluded as documented by the manifest. The manifest's memory class is excluded even inside a selected directory. Overlays explicitly replace existing allowlisted files before manifest generation. Keep distribution templates clean and commit them with the private source. The generator may create its manifest; its output is checked for forbidden runtime material.

## Approve one development commit

```sh
bin/alpaca release approve FULL_40_CHARACTER_SHA --by owner
bin/alpaca release approve FULL_40_CHARACTER_SHA --by agent --delegation PATH#section
bin/alpaca release approve FULL_40_CHARACTER_SHA --by owner --revoke
```

An approval names a full commit SHA that exists in the development repository. It is a private receipt in the release record; its event row names the commit, the decision and the receipt's exact byte hash. An owner decision is typed in an interactive terminal outside an agent session: the command asks for the first 12 characters of the SHA and refuses when standard input is not a terminal or an agent session is detected. An agent approves only with `--by agent` and `--delegation PATH[#section]`, where PATH is a project file holding the owner's delegation; the receipt records the pointer and the file's hash at that moment. A later revocation of the same SHA wins.

Approval lookup fails closed. It verifies the event hash chain first, and any missing or edited decision receipt for that commit refuses the commit, so deleting a revocation cannot restore an earlier approval. Revoke and approve again to recover.

`release check` refuses to run anything until the checked-out commit is the approved one and every release input is committed. Without that, the check receipt is BLOCKED with a single `approval` entry: no gate runs and the private term policy is not read. A new commit, an amended commit or an uncommitted edit needs its own approval. Checks re-read the approval when they finish; validation re-reads it before it reads the term policy; publication re-reads it before it pushes and again before it opens the pull request. A revocation, or a re-approval that creates a new receipt, makes older checks stale.

`release build` does not need an approval. It runs the configured manifest generator on the owner's machine with publication credentials removed, as any local development command does; it reads no term policy itself.

## Local sequence

```sh
bin/alpaca release init
bin/alpaca release approve FULL_40_CHARACTER_SHA --by owner
bin/alpaca release build
bin/alpaca release check BUILD_RECEIPT_PATH
bin/alpaca release publish CHECK_RECEIPT_PATH --dry-run
bin/alpaca release publish CHECK_RECEIPT_PATH --execute --expect DRY_RUN_RECEIPT_PATH
```

Build and check print JSON with the receipt path for the next command. Publish defaults to a dry run. It writes a report with file changes, new top-level paths, large files and the source commit. Review that report before executing. Actual publication requires committed source files, a current approval and the `PUBLIC_REPO_TOKEN` environment variable. Credentials are never part of project configuration or receipts, and project gate/generator commands do not inherit publication credentials.

Receipts live under local release state and have an exact byte hash in the shared event record. They bind source files, the source Git commit, the approval, the built tree, private policy and every gate. Editing a source file, candidate file or term list invalidates old checks. After any such change, approve, build and check again. Exit 0 means success or a successful dry run, 1 means failed checks, 2 means blocked, and command usage errors are reported by the CLI parser.

The publisher clones public history separately and creates a generated commit whose parents are public commits. It never copies private commit messages or ancestry. It compares the actual staged Git blobs and modes with the checked inventory, then scans the outgoing objects, the generated commit metadata, the PR title and the PR report before pushing. The publish receipt freezes what goes out: the sync ref, the public base, the commit, its parents, its tree, its message hash, the title, the report hash and the approval id. `--execute` requires `--expect` naming the reviewed dry-run receipt for the same checks, and refuses unless everything except the commit id (which carries a timestamp) matches it. A changed public base or report therefore needs a new dry run and review. The pull request receives exactly the scanned title and report. Public main is never updated by this command. Sync updates use an exact expected-old-ref lease; a concurrent change blocks publication.

The first release records the source tip as its baseline. Later releases list every development commit since the previous published source in the report, generated commit and receipt. API retries preserve that list. The candidate must descend from the previous source; older retries, unrelated history and missing history are blocked. Use a full development clone when publishing.

A previous normal merge is recognized only when ancestry and tree content agree. An unexpected public commit, edited sync branch or unsupported squash/rebase history stops publication for owner review. Do not reset public history to make the check pass. Resolve the discrepancy and explicitly review the next base. If a push succeeds but the PR API fails, the receipt keeps the pushed commit; retrying unchanged checked inputs under the same approval reuses that commit and updates the existing open PR.

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

Publication runs on the owner's machine. Create a fine-grained GitHub token limited to the one public repository with Contents and Pull requests read and write access. Add Workflows write only if the release must change files under `.github/workflows/`; GitHub refuses such pushes without it. Keep the token in a file outside the project that only the owner can read (mode 600), and pass it for one command:

```sh
PUBLIC_REPO_TOKEN="$(cat PATH_TO_TOKEN_FILE)" bin/alpaca release publish CHECK_RECEIPT_PATH --execute --expect DRY_RUN_RECEIPT_PATH
```

Do not put the token in workflow files, project configuration, receipts, logs or PR text. Do not reuse a broad personal login token.

## Alpaca's own configuration

The example gates cover full pytest, full boot checks, manifest integrity, a fresh bootstrap/onboard/doctor/operation/task/proof cycle, regenerated manual HTML, ASCII shipped HTML and README local links. The built-in gates always cover two-build equality, leak checks and a fresh clone. The clean distribution templates under `templates/release` must remain instance-free. Hosted test results and local receipts are separate evidence: a local PASS does not claim a GitHub run occurred, and a hosted test run does not claim a release check.

## Version numbers

`bin/alpaca --version` (and the `version` and `channel` keys of `alpaca status --json`) tell a development tree from a release tree:

| Tree | Reports | Example |
|---|---|---|
| Release (built by `alpaca release build`) | the bare number | `1.0.0` |
| Development checkout, harness code committed | pre-release plus the commit | `1.0.0-dev+g03ae15d0f9fc` |
| Development checkout, uncommitted changes under `alpaca/` or `bin/` | the same, marked dirty | `1.0.0-dev+g03ae15d0f9fc.dirty` |
| Development tree with no git | pre-release only | `1.0.0-dev` |

The number lives in one place, `VERSION` in `alpaca/__init__.py`. It names the release the development tree is heading to, so the dev form sorts before it under SemVer. The channel lives in `alpaca/_channel.py` (`CHANNEL = "dev"`); the release overlay `alpaca/_channel.py: templates/release/channel.py` in `project.yaml` swaps in `CHANNEL = "release"` at build time, so no one edits the channel by hand. To cut a new release, bump `VERSION` in a development commit (for example `1.0.0` to `1.1.0`), approve that commit, then build, check and publish as above. Changes to the record under `.alpaca/` never mark a version dirty.
