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
