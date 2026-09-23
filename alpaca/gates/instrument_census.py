"""instrument_census.py - the reachability census (M1.17).

Ported from the earlier harness gates/instrument_census.py and re-based onto Alpaca. An instrument on disk
is a self-description: the file says "I am installed". WHO reaches it is a separate fact that
only a scan of the rest of the tree can establish. This census re-derives, from file CONTENT
(nothing describes itself), the population of instruments under `alpaca/gates/` and `alpaca/checklist/`
and, for each one, the callers that reference it. It is ADVISORY: it maps the wiring, it does
not gate (that is `wiring_audit`), and it PASSes as long as the population is non-empty.

A "caller" is CLASSIFIED, not merely counted, so a doc mention cannot masquerade as a call and
a unit test cannot masquerade as production wiring:

  * production caller - a NON-test module that imports the instrument's module or names its
    file path (how a door, a CLI or a sibling gate actually reaches it). This is the caller
    that satisfies the "present != wired" invariant (spec:300-307); `wiring_audit` folds it.
  * test caller       - a file under `tests/` (or a `test_*.py` / `conftest.py`) that imports
    the instrument. A unit test exercising a gate is NOT the same as the gate being wired into
    a production path, so it is listed separately and never counts as independent wiring.
  * doc mention       - a `.md` / `.html` / `.txt` / `.rst` file that only NAMES the instrument
    in prose. A mention is not a call.

HONEST LIMIT (do not soften): "called" is an import / path-reference test over file content, not
a live call-graph proof. A module that imports an instrument but never reaches it on any live
path still reads as a caller. The signal is the weaker but real one the rank-5 concern names:
"nothing in production even references this instrument".
"""
from __future__ import annotations

import os
import re
from collections import OrderedDict, namedtuple

from alpaca import manifest
from alpaca.gates import verdict as vc

INSTRUMENT = "instrument-census"

#: the two directories whose instruments this census counts (M1.17 Done-when). Declared once so
#: the population and the reported scope can never disagree.
INSTRUMENT_DIRS = ("alpaca/gates", "alpaca/checklist")

#: never an instrument: the package marker carries no boundary of its own.
_NOT_INSTRUMENTS = frozenset(("__init__.py",))

#: reason token the empty-population floor binds to.
R_NO_POP = "CENSUS-NO-INSTRUMENTS"

# Dirs pruned from the caller corpus: version-control / build / cache noise, the runtime record,
# and every dot-dir (so a `.claude/worktrees/` copy of the tree cannot forge a caller). Pruning
# by a leading dot covers `.git`, `.alpaca`, `.claude`, `.venv`, `.pytest_cache` in one rule.
_PRUNE_DIRS = frozenset(("__pycache__", "node_modules", "venv", "build", "dist",
                         ".git", ".alpaca", ".claude", ".venv", ".mypy_cache",
                         ".pytest_cache", ".ruff_cache", ".tox", ".eggs"))

# Text file kinds a caller could live in. A binary is never scanned.
_TEXT_EXT = frozenset((".py", ".md", ".html", ".htm", ".txt", ".rst", ".json", ".yaml",
                       ".yml", ".cfg", ".ini", ".toml", ".sh", ".cnf"))
_TEXT_NAMES = frozenset(("Makefile", "makefile"))

_DOC_EXT = frozenset((".md", ".html", ".htm", ".txt", ".rst"))

Instrument = namedtuple("Instrument", "treerel name module abspath")


def _rel(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _module_of(treerel):
    """A tree-relative `.py` path -> its dotted import name (`a/b/c.py` -> `a.b.c`)."""
    return treerel[:-3].replace("/", ".") if treerel.endswith(".py") else treerel.replace("/", ".")


def _is_test(rel):
    """A file that is a unit test (not a production caller)."""
    base = rel.rsplit("/", 1)[-1]
    return (rel == "tests" or rel.startswith("tests/") or "/tests/" in rel
            or base.startswith("test_") or base == "conftest.py")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def enumerate_instruments(root, instrument_dirs=INSTRUMENT_DIRS):
    """Every `*.py` instrument under the given dirs (never `__init__.py`), sorted. Re-derived
    from disk, so the population is what the tree actually holds, never a hand-kept manifest."""
    out = []
    root_abs = os.path.abspath(root)
    for d in instrument_dirs:
        ddir = os.path.join(root_abs, d.replace("/", os.sep))
        if not os.path.isdir(ddir):
            continue
        for name in sorted(os.listdir(ddir)):
            if not name.endswith(".py") or name in _NOT_INSTRUMENTS:
                continue
            p = os.path.join(ddir, name)
            if not os.path.isfile(p):
                continue
            treerel = _rel(p, root_abs)
            out.append(Instrument(treerel, name, _module_of(treerel), p))
    out.sort()
    return out


def load_corpus(root):
    """{relpath -> text} for every text file under root, pruned of VC/cache/dot noise so a
    worktree copy or the runtime record can never forge a caller. Every memory path of the
    manifest is pruned too, nested ones included: a host-local tool install under a mechanism dir
    is runtime state, its files are not callers, and reading them costs minutes per census."""
    corpus = OrderedDict()
    root_abs = os.path.abspath(root)
    memory = manifest.memory_ignore(root_abs)
    for dirpath, dirnames, filenames in os.walk(root_abs):
        gone = set(memory(dirpath, dirnames + filenames))
        dirnames[:] = [d for d in sorted(dirnames)
                       if d not in _PRUNE_DIRS and not d.startswith(".") and d not in gone]
        for fn in sorted(filenames):
            if fn in gone:
                continue
            if fn not in _TEXT_NAMES and os.path.splitext(fn)[1].lower() not in _TEXT_EXT:
                continue
            fp = os.path.join(dirpath, fn)
            corpus[_rel(fp, root_abs)] = _read(fp)
    return corpus


# --------------------------------------------------------------------- reference matchers
_PATHY = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


def ref_present(text, token):
    """Is `token` present in `text` as a whole path-ish literal (not glued to a longer
    identifier on either side)? A basename matches its own path reference but not a longer
    identifier that merely ends with it (a trailing `.pyx`, a `re`-prefixed name)."""
    if not text or not token:
        return False
    n = len(token)
    start = 0
    while True:
        i = text.find(token, start)
        if i < 0:
            return False
        start = i + 1
        before = text[i - 1] if i > 0 else ""
        after = text[i + n] if i + n < len(text) else ""
        if before in _PATHY or after in _PATHY:
            continue
        return True


def _imports_module(text, module):
    """A python `import alpaca.gates.x` / `from alpaca.gates.x import ...` of the FULL dotted module."""
    if not text or not module:
        return False
    return re.search(r"(?:^|\n)\s*(?:import|from)\s+" + re.escape(module) + r"\b", text) is not None


def _imports_name_from_package(text, package, name):
    """A python `from alpaca.gates import ... name ...` of the bare instrument name out of its
    package. Handles a comma list and an `as` alias on one line (the form this tree uses)."""
    if not text:
        return False
    for m in re.finditer(r"(?:^|\n)\s*from\s+" + re.escape(package) + r"\s+import\s+([^\n#]+)",
                         text):
        names = m.group(1)
        names = names.replace("(", " ").replace(")", " ")
        for token in names.split(","):
            head = token.strip().split()[0] if token.strip().split() else ""
            if head == name:
                return True
    return False


def caller_kind(crel, ctext, instr):
    """How does `crel` witness `instr`? 'import' | 'invoke' | 'doc' | None. A doc/prose file can
    only ever be a 'doc' (an `import`-looking line in Markdown is prose, never a Python import)."""
    package, _, name = instr.module.rpartition(".")
    stem = instr.name[:-3]                       # bare module name without `.py`
    is_doc = os.path.splitext(crel)[1].lower() in _DOC_EXT
    if is_doc:
        if (_imports_module(ctext, instr.module)
                or _imports_name_from_package(ctext, package, stem)
                or ref_present(ctext, instr.treerel) or ref_present(ctext, instr.name)):
            return "doc"
        return None
    if _imports_module(ctext, instr.module) or _imports_name_from_package(ctext, package, stem):
        return "import"
    if ref_present(ctext, instr.treerel):
        return "invoke"                          # a path reference in code / a config
    return None


def classify_callers(instr, corpus):
    """(production, tests, docs) rel-path lists. production = a non-test import/invoke;
    tests = an import/invoke from a unit test; docs = a prose mention. A file is never its own
    caller (no self-witness)."""
    production, tests, docs = [], [], []
    for crel, ctext in corpus.items():
        if crel == instr.treerel:
            continue
        kind = caller_kind(crel, ctext, instr)
        if kind in ("import", "invoke"):
            (tests if _is_test(crel) else production).append(crel)
        elif kind == "doc":
            docs.append(crel)
    return production, tests, docs


def census(root, instrument_dirs=INSTRUMENT_DIRS):
    """For each instrument, the callers that reference it, re-derived from content. Returns a
    list of rows: {rel, name, module, callers, test_callers, doc_mentions}. `callers` is the
    production (independent) caller list `wiring_audit` folds."""
    instrs = enumerate_instruments(root, instrument_dirs)
    corpus = load_corpus(root)
    rows = []
    for i in instrs:
        production, tests, docs = classify_callers(i, corpus)
        rows.append({"rel": i.treerel, "name": i.name, "module": i.module,
                     "callers": production, "test_callers": tests, "doc_mentions": docs})
    return rows


def check(root) -> int:
    """Advisory verdict: PASS once the population is enumerated, BLOCKED when there is none (a
    census over an empty population is never a pass). Never FAILs - mapping the wiring is not
    gating it (that is `wiring_audit`)."""
    rows = census(root)
    return vc.PASS if rows else vc.BLOCKED


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="Reachability census of the instruments under alpaca/gates/ and alpaca/checklist/ "
                    "and their callers (advisory; PASS once the population is non-empty).")
    ap.add_argument("--root", default=None, help="tree to census (default: the discovered root)")
    ap.add_argument("--selftest", action="store_true", help="run the census's own controls")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    from alpaca import paths
    root = a.root or paths.root()
    rows = census(root)
    if not rows:
        return vc.emit_verdict(INSTRUMENT, vc.BLOCKED,
                               "%s: no instrument under alpaca/gates/ or alpaca/checklist/" % R_NO_POP)
    print("instrument-census - %d instrument(s) under alpaca/gates/ + alpaca/checklist/" % len(rows))
    for r in rows:
        mark = "ok " if r["callers"] else "XX "
        extra = ""
        if not r["callers"] and (r["test_callers"] or r["doc_mentions"]):
            extra = " (test-only/doc-only: %s)" % (r["test_callers"] + r["doc_mentions"])[:2]
        print("  %s %-40s callers=%s%s" % (mark, r["rel"], r["callers"][:3] or "-", extra))
    return vc.emit_verdict(INSTRUMENT, vc.PASS, "%d instrument(s) censused" % len(rows))


def selftest() -> int:
    """In-memory controls on a synthetic tree: an instrument imported by a production module is
    a production caller; the same import from a tests/ file is a test caller, never production;
    a prose mention is a doc, never a call; __init__.py is never an instrument."""
    import shutil
    import tempfile

    controls = []

    def _check(cid, ok, detail):
        controls.append((cid, "FIRED" if ok else "DID-NOT-FIRE", detail))
        return 0 if ok else 1

    base = tempfile.mkdtemp(prefix="instrument-census-selftest-")
    failures = 0
    try:
        files = {
            "alpaca/gates/__init__.py": "",
            "alpaca/gates/alpha_gate.py": "def check(root):\n    return 0\n",
            "alpaca/gates/beta_gate.py": "def check(root):\n    return 0\n",
            "alpaca/gates/gamma_gate.py": "def check(root):\n    return 0\n",
            "alpaca/driver.py": "from alpaca.gates import alpha_gate\nalpha_gate.check('.')\n",
            "tests/test_beta.py": "from alpaca.gates import beta_gate\n",
            "notes/HOWTO.md": "The `gamma_gate.py` instrument is described here.\n",
        }
        for rel, body in files.items():
            p = os.path.join(base, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(body)
        rows = {r["name"]: r for r in census(base)}
        failures += _check("C-01", "__init__.py" not in rows, "__init__.py is never an instrument")
        failures += _check("C-02", bool(rows["alpha_gate.py"]["callers"]),
                           "a production import is a production caller")
        failures += _check("C-03", not rows["beta_gate.py"]["callers"]
                           and bool(rows["beta_gate.py"]["test_callers"]),
                           "a tests/ import is a test caller, never production")
        failures += _check("C-04", not rows["gamma_gate.py"]["callers"]
                           and bool(rows["gamma_gate.py"]["doc_mentions"]),
                           "a prose mention is a doc, never a call")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("CONTROL TABLE - %s" % INSTRUMENT)
    for cid, state, detail in controls:
        print("  %-5s %-12s %s" % (cid, state, detail))
    if failures:
        return vc.emit_verdict(INSTRUMENT + "-selftest", vc.FAIL,
                               "%d control(s) did not fire" % failures)
    return vc.emit_verdict(INSTRUMENT + "-selftest", vc.PASS, "every control fired")


if __name__ == "__main__":
    import sys
    sys.exit(main())
