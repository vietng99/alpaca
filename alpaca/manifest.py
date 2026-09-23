"""The harness manifest: which paths inside a project root the harness owns, and the two classes
the packaging protocol splits them into.

Two classes, stated in the `ALPACA-MANIFEST` header and parsed here:

  * [mechanism]  committed with the project. The harness code, its boot file, its contracts, its
                 self-tests (under the harness package), `pytest.ini` and `project.yaml`. A copy
                 or an upgrade refreshes these; they are the same in every copied project.
  * [memory]     runtime state, gitignored. The record (`.alpaca/`), the rendered `RESUME.md`, the
                 `analytics/` projection. This is one project's own state.

The contamination rule the two classes exist for: the memory class must never travel to another
project. It is this project's runtime state; a used copy is never the copy source, and the
collision pass and the copy step below only ever touch the mechanism class.

Interfaces (consumed by `alpaca init`, `alpaca upgrade`, `alpaca doctor`):

  classes(root)            -> {"mechanism": [...], "memory": [...]}
  mechanism(root) / memory(root)
  collisions(root, target) -> [colliding mechanism relpath, ...]   read-only, zero writes
  gitignore_from_memory(root)
  merge_boot_block(existing, incoming)
"""
import os

from alpaca import paths

MANIFEST = paths.MANIFEST

# The markers that fence the harness boot block inside `CLAUDE.md`. Merging is by these markers,
# never a clobber of the whole file, so a target repo's own house rules survive.
BOOT_BEGIN = "<!-- ALPACA:BOOT:BEGIN -->"
BOOT_END = "<!-- ALPACA:BOOT:END -->"

# The contamination rule, stated once and written into the manifest header. See module docstring.
RULE = ("The memory class must never travel to another project: it is this project's own runtime "
        "state, which is why a used copy is never the copy source.")

_SECTIONS = ("mechanism", "memory")


def _manifest_path(root):
    return paths.manifest_path(root)


def classes(root):
    """Parse `<root>/ALPACA-MANIFEST` into the two classes, one path per line. Comment and blank
    lines are ignored. An absent or unreadable manifest yields two empty lists."""
    out = {name: [] for name in _SECTIONS}
    try:
        with open(_manifest_path(root), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return out
    section = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            section = name if name in out else None
            continue
        if section:
            out[section].append(line)
    return out


def mechanism(root):
    return classes(root)["mechanism"]


def memory(root):
    return classes(root)["memory"]


def memory_ignore(root):
    """A `shutil.copytree` ignore callable that leaves every memory path of `root` behind.

    Each memory path is anchored at `root`, so a nested entry such as `pkg/cache/` drops that one
    directory while a directory that shares its basename elsewhere in the tree still travels. A
    copy made through this callable obeys RULE for nested memory paths as well as the top-level
    ones. The callable answers for any directory under `root`, so it also serves a walk that
    starts below the root (one mechanism path at a time)."""
    root_abs = os.path.abspath(root)
    drop = {}
    for entry in memory(root):
        rel = entry.strip("/")
        if not rel:
            continue
        parent, name = os.path.split(rel)
        drop.setdefault(os.path.normpath(os.path.join(root_abs, parent)), set()).add(name)

    def _ignore(dirpath, names):
        here = drop.get(os.path.abspath(dirpath))
        return [n for n in names if n in here] if here else []

    return _ignore


def collisions(root, target):
    """Every mechanism path that already exists under `target`, sorted. Read-only: it writes
    nothing to `target`. The memory class is excluded by construction (it never travels), so a
    stale `.alpaca/` or `RESUME.md` in the target is never reported and never a copy source."""
    out = []
    for rel in mechanism(root):
        probe = os.path.join(target, rel.rstrip("/"))
        if os.path.lexists(probe):
            out.append(rel)
    return sorted(out)


def gitignore_from_memory(root):
    """The `.gitignore` entries derived from the memory class, so the ignore list and the class
    never drift. One entry per memory path, verbatim."""
    return list(memory(root))


def _boot_block(text):
    """The boot block of `text` including its markers, or None when the markers are absent."""
    begin = text.find(BOOT_BEGIN)
    if begin == -1:
        return None
    end = text.find(BOOT_END, begin)
    if end == -1:
        return None
    return text[begin:end + len(BOOT_END)]


def merge_boot_block(existing, incoming):
    """Merge the harness boot block from `incoming` into `existing` by markers, never by clobber.

    The block is the marked region of `incoming`. When `existing` already carries the markers,
    only that region is replaced in place; otherwise the block is appended and the rest of
    `existing` is kept untouched. Idempotent: merging twice leaves exactly one block.
    """
    block = _boot_block(incoming)
    if block is None:
        # nothing marked to merge; leave the target as it stands.
        return existing
    begin = existing.find(BOOT_BEGIN)
    end = existing.find(BOOT_END, begin) if begin != -1 else -1
    if begin != -1 and end != -1:
        return existing[:begin] + block + existing[end + len(BOOT_END):]
    sep = "" if existing.endswith("\n") or existing == "" else "\n"
    tail = "" if block.endswith("\n") else "\n"
    return existing + sep + block + tail
