# Deploy Alpaca to another machine

The harness ships as one tarball of its git-tracked tree. Nothing about this machine travels in it:
the record (`.alpaca/`), the rendered `RESUME.md` and `analytics/`, the board/data projections, the
agent worktrees and the local settings are all gitignored, so they are absent from the tracked set
the release is built from. On the target you extract, then run two commands, and the harness boots as
a fresh project with its own record and identity.

## 1. Build the release (on this machine)

```bash
python3 setup/make_release.py build
```

Writes `dist/<name>-release-template.tar.gz` and a `.sha256` receipt beside it. The build is
reproducible: the same tree always produces the same bytes, so the receipt is a stable fingerprint.

Two identity modes:

- **template (default)** leaves `project.yaml` out, so the target reads as first contact and
  onboarding authors a clean identity from the target's own name and path. Use this to stand up a
  new project on the harness.
- **`--keep-identity`** carries `project.yaml`, so the *same* project moves to a new machine and
  keeps its id. Onboarding will refuse on the target (it already has an identity); you just start
  work.

```bash
python3 setup/make_release.py build --keep-identity   # move the same project
python3 setup/make_release.py list                     # print the exact members first
python3 setup/make_release.py verify --archive dist/<name>-release-template.tar.gz
```

## 2. Move the tarball

Copy `dist/<name>-release-template.tar.gz` (and its `.sha256`) to the target machine by whatever
means you use. Moving the file off this machine is a human step; the builder never pushes anything.

## 3. Install on the target

```bash
mkdir my-project && tar xzf <name>-release-template.tar.gz -C my-project && cd my-project
python3 setup/make_release.py verify --archive ../<name>-release-template.tar.gz   # optional: check the bytes
```

Requirements on the target: Python 3.10 or newer and the `pyyaml` package. No other install step.

## 4. Boot it

```bash
python3 bin/alpaca init                                   # writes a fresh .alpaca/ record
python3 bin/alpaca onboard --name my-project --who you:owner --what "what this project is"
python3 bin/alpaca doctor                                 # OK, with a warn until the first session
```

`bin/alpaca` is `alpaca`. From here the record is live: `alpaca status` prints the resume pad, `alpaca op new` opens
work, `alpaca verify` recomputes the hash chain.

## What "clean" means here, and how it is checked

`alpaca/tests/test_release.py` builds a release on every test run and asserts:

- no member is under the memory class, version control, a byte cache, the projections, the worktrees
  dir or the output dir, and `project.yaml` is absent in template mode;
- every support tree the harness boots from is present (`bin/alpaca`, `ALPACA-MANIFEST`, `MAP.md`,
  `doctrine/`, `plugin/alpaca`, `.claude/skills/alpaca-*`, `setup/`, `contracts/`, `alpaca/`);
- the build is byte-for-byte reproducible and its receipt attests its bytes;
- a fresh extract into a differently named directory runs `alpaca init`, `alpaca onboard`, `alpaca doctor`
  (no ERROR) and `alpaca verify` (PASS), and the written `project.yaml` carries the new name and no em
  dash.

A heavier proof runs in `alpaca/tests/test_acceptance_m4.py`: a fresh install passes `boot-check --full`,
the whole worst-code fold at PASS.

## Upgrading an existing install in place

A release is a fresh install. To refresh only the mechanism class of a tree that already carries its
own record and identity, use the cutover path instead, which backs up every overwrite first:

```bash
python3 setup/gen_manifest.py --write        # on the source, record the mechanism digests
setup/cutover.sh --dry-run                    # show the exact delta, no writes
setup/cutover.sh --apply                      # back up, install, verify (auto-rolls back on failure)
```
