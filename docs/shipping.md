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

It writes `.git/hooks/pre-push` (it refuses to replace a hook it did not write) and pins a copy of
the barrier in the git directory (`.git/alpaca-barrier/`: the `alpaca` package code, the `barrier`
and `tier` settings of `project.yaml`, and a stamp with their digests). Git runs the hook before any
object leaves, for every remote and from every worktree, and the hook runs the pinned copy
(`bin/alpaca-python` when the checkout has it, plain `python3` otherwise), never the checked-out
code. So checking out an older commit, or a `project.yaml` without the barrier block, does not
change what is checked; the report notes when the checkout differs from the pinned copy. Run the
install again after you change the barrier settings or update Alpaca. A pinned copy that was
edited after the install refuses the push. A hook written before the barrier was pinned (it runs
`python -m alpaca.barrier`) refuses every push and says `the hook predates the pinned barrier; run
bin/alpaca barrier install`, so a stale hook is never silent.

The barrier reads every object the push would send (`git rev-list --objects <local> --not
<remote>`), not the working tree, and refuses the push when it finds:

- a protected path (`barrier.protected_paths` in `project.yaml`: `.alpaca/`, `.venv/`, `.env`,
  `.env.local`, `.env.*.local`). Case does not matter. A folder rule (`.alpaca/`) also matches a
  folder of that name deeper in the tree, a name rule (`.env`) matches that name anywhere
  (`sub/.env`), and `*` or `?` match within one path part. `.env.example` is not protected;
- a sealed term, case-insensitive, in any file, in a member of a compressed file, in any path name
  (files, links, folders and submodule entries), in an author, committer or tagger, in a commit or
  tag message (every tag of a tag chain), or in a pushed ref name (a ref the push deletes
  included: its name reaches the server too);
- a shape the term list cannot enumerate (an e-mail address, a `/home/<user>` path) in a file or a
  path name, unless an allow rule clears that exact value; an author, committer or tagger e-mail
  that has the shape of a real address, unless an allow rule names it;
- a file it cannot read in full: a compressed file it cannot open, or a file larger than 16 MB (it
  scans the raw bytes of such a file for the terms, but not its text), unless
  `barrier.allow_blobs` names the file's blob digest with a reason.

Compressed files are recognised by their first bytes, not their names, and opened with the Python
standard library: gzip, tar, zip, xz, bzip2, zstd, zlib streams and WOFF fonts, nested up to four
levels, at most 64 MB per member and 256 MB per file. A zip is also recognised by its end record,
so a zip behind a prefix (a Python zipapp `.pyz`, a self-extracting file) is opened. The caps are
charged before a member is read and while a stream is read, and one member is held at a time, so
an archive that would open to gigabytes is refused without using that memory. Inside them only the
sealed terms are checked, not the shape rules (vendored packages carry their authors' addresses).

These are recognised but not opened, so they refuse unless allowed: 7z, rar, ar (a `.deb`), lzip,
lz4, compress (`.Z`), lzop, rpm, cab, xar, squashfs, cpio and ISO images; a zip with a local entry
its central directory does not list (a zip reader would never show that entry); a WOFF table that
opens to more or less than its declared length; an encrypted zip member; a member over 64 MB. When
one member of an archive cannot be read, its siblings are still read, and a `barrier.allow_blobs`
entry waives only the parts that were not read: the report lists them by digest, with the reason,
even when the entry lets the file pass. A WOFF2 font needs the `brotli` module (1.1 or later, which
can cap its output); without it the three fonts under `alpaca/web/vendor/` are passed by their
`barrier.allow_blobs` entries, which name each file's digest and why.

The scan has a time budget: 300 seconds, or `barrier.time_budget_seconds`. A scan that runs past
it refuses the push and says so (`SCAN-TIME-BUDGET-REFUSES`); the usual cause is a `re:` line with
nested repeats, such as `(a+)+`, that backtracks without end on some text.

Git runs the barrier with replace refs switched off (`GIT_NO_REPLACE_OBJECTS`), since a push sends
the real objects, not a local replacement. A grafts file (`.git/info/grafts`) or a shallow clone
refuses the push: to cut old history before a first publish, rewrite it for real (a new root
commit), not with `git replace --graft`.

Its report names digests and line numbers, never the paths, refs or terms it refused. Any error
while scanning, an unreadable `project.yaml`, an unusable term list, an allow rule without a reason
or an absent term list refuses the push. `bin/alpaca barrier scan <rev> [--since <sha>]` runs the
same scan by hand, with the checked-out settings. Deciding to push at all stays a human decision;
the barrier only stops what must not leave.

### The term list stays outside the published tree

`barrier.terms` names `.alpaca/sealed-terms.txt`. The `.alpaca/` folder is gitignored and a protected
path, so the list is never committed: listing the names inside the published tree would publish
them. Each clone supplies its own list there, in the main worktree (a linked worktree reads the
main worktree's list). When the configured list is absent, or a link that points nowhere, the
push is refused (`TERM-LIST-MISSING-REFUSES`). A public project (`tier: public`, the default) with
no list configured at all (`barrier.terms` empty or missing) is refused the same way. To push
without a list, set `barrier.terms_optional: true`: the push then passes with protected paths and
shape rules checked, and the report and the gate line say the sealed terms were NOT checked.

The list format, one entry per line:

| Line | Meaning |
|---|---|
| `<term>` | a case-insensitive substring, at least four characters; `  # note` after it is ignored |
| `re: <regex>` | a case-insensitive regular expression, for a whole word (`\bname\b`) or a path prefix too short or too common for a substring; `#re: <regex>` is the older spelling |
| `!include: <file>` | another list, relative to this list's own folder (up to four levels) |
| `# ...` | a comment (a `#` followed by a space, or alone) |

A line the barrier cannot use refuses the push: a term shorter than four characters, a regex that
does not compile or matches an empty string, an include that is absent or loops, a line that looks
like a directive the barrier does not know (`#word:` or `!word:`). The refusal names the line
number only. A byte order mark at the start of the file is dropped, so a list saved with one keeps
its first line. A regex that can never match (`(?!)x`) is not detected: check a new `re:` line
against a sample that must hit it.

The maintainers keep the lists of this project outside the repository, in the workspace that
holds it: `tools/forge/forbidden.txt` (the forbidden names) and `tools/forge/host-terms.txt` (the
host name, user names, tunnel and registry ids, addresses and account terms of the machine the
project is built on), both included by `tools/forge/sealed-terms.txt`, which is linked into place:

```bash
mkdir -p .alpaca
ln -s ../../tools/forge/sealed-terms.txt .alpaca/sealed-terms.txt   # from the repository root
```

The same lists feed the full leak audit over the tree and the git history before a first publish.

### Allow rules

The shape rules also fire on a few known values in this tree. `barrier.allow` in `project.yaml`
clears them. Each rule is `<regex>  # <reason>`; the regex must match the WHOLE value the shape rule
found (case-insensitive), and a rule clears shape hits only, never a sealed term. A rule with no
reason, or a regex that does not compile, refuses the push. The shipped rules:

| Rule clears | Reason |
|---|---|
| addresses at `example.com`, `example.org`, `example.net`, `*.example`, `*.invalid`, `*.test`, `*.localhost` | reserved placeholder domains (RFC 2606, RFC 6761) used by fixtures and docs |
| `<text>@pytest.fixture`, `@pytest.mark.<name>`, `@cli.command`, `@common.fail`, `@contextlib.contextmanager`, each optionally followed by `def<name>` | the joined lane (whitespace removed, to catch a term split across a line break) glues the end of one line to a Python decorator on the next, and to the `def` after it |
| `<text>@rebootweb-upscriptunder.alpaca` | the same gluing, in one docstring that names the crontab keyword `@reboot`; this one value only |
| `/home/owner`, `/home/someone`, `/home/secret` and paths below them | generic home folders in test fixtures; no real user |

The joined lane removes whitespace, so keep a placeholder address apart from the next word with a
quote or a bracket (`<t@example.invalid>`), as the fixtures here do.

The public identity chosen for the published history (the author and committer e-mail) needs an
allow rule of its own when it has the shape of a real address: a rule whose regex matches that
one address exactly (with each `.` and `+` escaped), and a reason such as `the public identity of
this repository`.

`barrier.allow_blobs` lists blobs the barrier may pass although it cannot read them in full, as
`<blob sha>  # <reason>` (`git rev-parse <rev>:<path>` prints the digest). The raw bytes of such a
blob are still scanned for the terms.

### What a pre-push hook cannot stop

The barrier is a pre-push hook, so it stops `git push` and nothing else:

- `git push --no-verify` skips every pre-push hook, and `core.hooksPath` pointed elsewhere skips
  this one. Hooks are not cloned: each clone installs its own.
- Other ways out are not pushes: a bundle, `git format-patch`, `git archive`, an upload through a
  hosting site's web page or API, a release file. Run the leak audit on what those carry.
- Push options (`git push -o`) go to the server and are never shown to the hook.
- Submodules: the hook sees a submodule only as its entry (a path and a commit id). `git push
  --recurse-submodules=on-demand` (or `push.recurseSubmodules`) also pushes the submodule's own
  commits to the submodule's remote, and no barrier runs there unless that submodule has its own
  hook: its files, messages and names are not checked. Install the barrier inside each submodule
  you push from, or push submodules on their own.
- A term the text cannot show as a string: split by code (`"na" + "me"`), split across comment
  markers, written as an HTML entity, percent-encoded or in base64, or inside a compressed stream
  with no magic bytes at its start (raw deflate, raw brotli, raw LZMA, PNG image data, PDF streams,
  an archive inside a disk image). The look-alike folding covers Cyrillic and Greek letters and the
  invisible characters, not the whole Unicode confusable table.

For the first publish, run the full leak audit over the tree and the history as well; the barrier
is the last guard, not the only one.

### Check that it refuses

In a scratch clone with a scratch bare repository as its remote, install the barrier the same way,
commit a file that holds one sealed term and push: the push must be refused. Remove the term, commit
a clean change, push again: it must pass. Never add the scratch remote to the real repository.
