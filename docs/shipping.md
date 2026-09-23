# Shipping Alpaca to another machine

The distribution contains the Alpaca runtime, operator instructions, vendored skills, and the
`project.yaml` template. Each machine creates its own `.alpaca/` record. The template's
`name` and `project_id` identify the distribution; they are not a copied project's runtime ID.

## Build a clean archive

Use the distribution template with `template: true`, an empty `people` list, an empty
`cwd_history`, and an empty intent queue. From the distribution root:

```bash
python3 setup/ship.py build --archive ../alpaca-clean.tar.gz
python3 setup/ship.py verify ../alpaca-clean.tar.gz
```

The builder creates `alpaca-clean.tar.gz` and `alpaca-clean.tar.gz.sha256`. It stages only the
`[mechanism]` entries in `ALPACA-MANIFEST`, excludes `[memory]`, and writes nothing into the
source tree. An existing output name is refused. Identical source bytes and executable modes
produce an identical archive. The archive includes a generated `MANIFEST.json` with per-file
SHA256 hashes and file modes.

The builder refuses missing mechanism paths, symbolic links, populated project identity
fields, or an intent queue with project tasks. Runtime state, local environments, worktrees,
tool installations, domain outputs, and predecessor operation history do not travel. Adding a
new mechanism file beneath an owned directory includes it automatically; adding a new root
directory requires an explicit manifest entry. Review the resulting file inventory before
sharing it.

`setup/gen_manifest.py` and the older `setup/package_candidate.py` remain low-level integrity
and cutover helpers. Use `setup/ship.py` for a fresh distribution: it also checks cleanliness
and allowlist completeness.

## Verify and unpack

Copy both the archive and its `.sha256` receipt to the destination. With an existing trusted
Alpaca distribution available, verification and extraction use only its local code:

```bash
python3 /path/to/trusted/alpaca/setup/ship.py verify alpaca-clean.tar.gz
python3 /path/to/trusted/alpaca/setup/ship.py extract alpaca-clean.tar.gz "Alpaca workspace"
```

Extraction requires a new destination directory and checks the archive's complete member set,
hashes, modes, fixed product identity, fresh project template, and shipment allowlist. The
co-located receipt detects transfer corruption; verify its hash through the sender if you need
to establish who supplied the archive.

For a first installation without a trusted local verifier, verify the receipt before unpacking
the archive you received from the owner:

```bash
sha256sum -c alpaca-clean.tar.gz.sha256
mkdir "Alpaca workspace"
tar -xzf alpaca-clean.tar.gz -C "Alpaca workspace"
cd "Alpaca workspace"
python3 setup/gen_manifest.py --verify
```

On systems with `shasum`, use `shasum -a 256 -c alpaca-clean.tar.gz.sha256` for the first command.
Distribution identity stays fixed when the folder is renamed or moved. Setup root discovery
starts beside the setup script or uses an explicit `--root`; ambient Claude project settings
do not redirect it.

## Bootstrap and start

Python 3.10 or newer with `venv` and `pip` is required. Install this copy's pinned dependencies:

```bash
bash setup/bootstrap.sh
bash setup/bootstrap.sh --check
bin/alpaca --help
```

Bootstrap creates only `.venv/` inside this copy. It does not install domain tools, edit global
configuration, or initialize a project record. `--python /path/to/python3` selects an
interpreter. For a prepared wheel directory on an offline machine:

```bash
bash setup/bootstrap.sh --offline --wheelhouse /path/to/wheels
```

Supply wheels matching the target Python and operating system for every pinned dependency
and its dependencies. A virtual environment is machine-specific and is never shipped.

Read [operator setup](operators.md), then onboard the project (`bin/alpaca onboard ...`, then
`bin/alpaca doctor`). A domain profile named in `project.yaml` documents its own tool setup; a
successful Python bootstrap does not establish that any domain tools are installed.

For full runtime verification in a disposable extracted copy:

```bash
.venv/bin/python -m pytest
.venv/bin/python setup/boot-check.py --full
```

Keep the archive and receipt as the clean distribution. Run project work in the extracted copy;
the `.alpaca/` record, generated resume files, local settings, and domain outputs belong to that
copy. To preserve a project's runtime history, use a separate explicitly chosen backup process.

## Push barrier

Before a push to a shared remote, install the pre-push barrier in the repository you push from:

```bash
bin/alpaca barrier install
```

It writes `.git/hooks/pre-push` (it refuses to replace a hook it did not write). Git runs the hook
before any object leaves, for every remote, and the hook runs `bin/alpaca-python -m alpaca.barrier
prepush` (plain `python3` when the launcher is missing). The barrier scans every commit that would
enter the remote, not the working tree, and refuses the push when it finds:

- a protected path (`barrier.protected_paths` in `project.yaml`: `.alpaca/`, `.venv/`, `.env`);
- a sealed term, case-insensitive, in any file, path name, author or committer, commit message,
  pushed ref name or annotated tag;
- a shape the term list cannot enumerate (an e-mail address, a `/home/<user>` path) in a file or a
  path name, unless an allow rule clears that exact value.

Its report names digests, never the paths, refs or terms it refused. Any error while scanning, an
unreadable `project.yaml`, an unusable term list or an allow rule without a reason refuses the push.
`bin/alpaca barrier scan <rev> [--since <sha>]` runs the same scan by hand. Deciding to push at all
stays a human decision; the barrier only stops what must not leave.

### The term list stays outside the published tree

`barrier.terms` names `.alpaca/sealed-terms.txt`. The `.alpaca/` folder is gitignored and a protected
path, so the list is never committed: listing the names inside the published tree would publish
them. Each clone supplies its own list there, in the leak-audit format (one term per line, at least
four characters, `#` starts a comment). When the file is absent the push is not blocked, but the
report says `TERMS-NOT-CHECKED`: only protected paths were checked.

The maintainers keep the forbidden-name list of this project outside the repository, in the
workspace that holds it, at `tools/forge/forbidden.txt`, and link it into place:

```bash
mkdir -p .alpaca
ln -s ../../tools/forge/forbidden.txt .alpaca/sealed-terms.txt   # from the repository root
```

The same list feeds the full leak audit over the tree and the git history before a first publish.

### Allow rules

The shape rules also fire on a few known values in this tree. `barrier.allow` in `project.yaml`
clears them. Each rule is `<regex>  # <reason>`; the regex must match the WHOLE value the shape rule
found (case-insensitive), and a rule clears shape hits only, never a sealed term. A rule with no
reason, or a regex that does not compile, refuses the push. The shipped rules:

| Rule clears | Reason |
|---|---|
| addresses at `example.com`, `example.org`, `example.net`, `*.example`, `*.invalid`, `*.test`, `*.localhost` | reserved placeholder domains (RFC 2606, RFC 6761) used by fixtures and docs |
| `<text>@pytest.fixture`, `@pytest.mark.<name>`, `@cli.command`, `@common.fail`, `@contextlib.contextmanager` | the joined lane (whitespace removed, to catch a term split across a line break) glues the end of one line to a Python decorator on the next |
| `<text>@reboot...` | the same gluing with the crontab keyword `@reboot` in prose |
| `/home/owner`, `/home/someone`, `/home/secret` and paths below them | generic home folders in test fixtures; no real user |

The joined lane removes whitespace, so keep a placeholder address apart from the next word with a
quote or a bracket (`<t@example.invalid>`), as the fixtures here do. Font files and compressed
archives do not decode as text; the barrier scans their raw bytes for the sealed terms only, so
they need no rule.

### Check that it refuses

In a scratch clone with a scratch bare repository as its remote, install the barrier the same way,
commit a file that holds one sealed term and push: the push must be refused. Remove the term, commit
a clean change, push again: it must pass. Never add the scratch remote to the real repository.
