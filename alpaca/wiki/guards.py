"""R1 enforcement helpers - the 'one door' as a regression-tested property, not a claim.

Vendored from Rune-2 rune2/guards.py (Operation-4-Litmus F1: the dynamic-import bypass fix and the
whole-surface guard). Two repoints, in ONE declared place each, carry it into alpaca.wiki:

  * READ door: the guarded ranking surface and its two allowed importers are named as alpaca.wiki.*
    modules (the block marked REPOINT below). Upstream named the rune2.* store.read/fts/vec/
    adjacency surface; Alpaca guards the whole ranking module set - engine.plan, engine.retrieve,
    engine.refine, engine.recall_ppr, engine.fuse, engine.tiering, store.fts, store.vec - and lets
    only engine/retrieve.py (the door) and the aggregator store/read.py reach it.
  * WRITE door: store/write.py is the one write door; the derived-data writers already in (and
    coming to) the alpaca.wiki tree are carved out BY NAME, not by silence (the WRITE_DOOR /
    KNOWN_WRITERS block below).

`check_one_door` greps the package's import graph (AST-level) and fails if any module other than
engine/retrieve.py or store/read.py imports a ranking primitive - by relative import, by absolute
import, by the `from ..engine import <submodule>` form, or by a dynamic
`importlib.import_module("...")` by name. It matches the WHOLE module path (or its dotted tail),
never a bare prefix, so engine.planner never collides with engine.plan and a same-named ranking
module rooted elsewhere cannot slip through.

`check_one_write_door` greps the same graph and fails if a raw mutating `conn.execute*` whose SQL
literal begins with INSERT / UPDATE / DELETE / REPLACE appears outside the door + the named
writers. It watches the SQL VERB, not the Writer method name, so a renamed method can never open a
second write door (the R1-F1 scar, kept out of the write surface too).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent          # .../alpaca/wiki

# ---------------------------------------------------------------- REPOINT (read door): alpaca.wiki.*
# The two ALLOWED importers (package-relative file paths) and the guarded ranking surface
# (package-root-relative dotted module names). This is the ONE declared place the read door is
# repointed from rune2.* onto alpaca.wiki.*.
ALLOWED: set[str] = {"engine/retrieve.py", "store/read.py"}   # the door + its aggregator
GUARDED_MODULES: tuple[str, ...] = (
    "engine.plan", "engine.retrieve", "engine.refine", "engine.recall_ppr",
    "engine.fuse", "engine.tiering", "store.fts", "store.vec",
)

# ---------------------------------------------------------------- M2.11 ORCHESTRATOR carve-out
# engine/loop.py is the answer-path ORCHESTRATOR: it wires plan -> retrieve -> fuse -> refine and
# so must reach the ENGINE ranking modules. It reaches them THROUGH the read door (it imports the
# door engine/retrieve.py and the engine-level ranking modules, never the raw store primitives).
# The STORE primitives (store.fts, store.vec) stay guarded against it too - a controller that pulled
# a raw arm directly would be the R1-F1 second door on the store surface. So an orchestrator is held
# to the STORE_PRIMITIVES sub-set of the guarded surface, not the whole set; every other module is
# still held to the whole GUARDED_MODULES set. ALLOWED stays exactly the two declared doors.
ORCHESTRATORS: set[str] = {"engine/loop.py"}
STORE_PRIMITIVES: tuple[str, ...] = ("store.fts", "store.vec")


def _rel_package(py: Path, package_root: Path) -> str:
    """The package-root-relative dotted package of a file (its containing dir); '' at the root."""
    rel = py.parent.relative_to(package_root).as_posix()
    return "" if rel in (".", "") else rel.replace("/", ".")


def _resolve_relative(module: str, level: int, pkg: str) -> str:
    """Resolve a relative import to a package-root-relative dotted name, as CPython would.

    level 1 is the file's own package, level 2 its parent, and so on.
    """
    parts = pkg.split(".") if pkg else []
    drop = level - 1
    if drop > 0:
        parts = parts[:-drop] if drop <= len(parts) else []
    base = ".".join(parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


def _matches(candidate: str, guarded: str) -> bool:
    """Whole module path (or its dotted tail), never a bare prefix.

    `engine.plan` matches `engine.plan`, `alpaca.wiki.engine.plan` and `x.engine.plan`, but not
    `engine.planner`. A same-named module rooted elsewhere is therefore caught, not let through.
    """
    return candidate == guarded or candidate.endswith("." + guarded)


def _dynamic_hit(literal: str, guarded: str) -> bool:
    """A guarded module named as a whole dotted segment run inside an import_module() literal."""
    return (
        literal == guarded
        or literal.endswith("." + guarded)
        or literal.startswith(guarded + ".")
        or ("." + guarded + ".") in literal
    )


def _read_violations(tree: ast.AST, pkg: str,
                     guarded: tuple[str, ...] = GUARDED_MODULES) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            candidates: list[str] = []
            if node.level:
                base = _resolve_relative(node.module or "", node.level, pkg)
            else:
                base = node.module or ""
            if base:
                candidates.append(base)
            # `from <pkg> import <submodule>` - the submodule pulled in by name.
            for alias in node.names:
                candidates.append(f"{base}.{alias.name}" if base else alias.name)
            for cand in candidates:
                for g in guarded:
                    if _matches(cand, g):
                        hits.append(f"from {node.module or ''} import "
                                    + ",".join(a.name for a in node.names))
                        break
                else:
                    continue
                break
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(_matches(alias.name, g) for g in guarded):
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.Call):
            fn = node.func
            is_import_module = (
                (isinstance(fn, ast.Attribute) and fn.attr == "import_module")
                or (isinstance(fn, ast.Name) and fn.id == "import_module")
            )
            if is_import_module and node.args and isinstance(node.args[0], ast.Constant):
                val = node.args[0].value
                if isinstance(val, str) and any(_dynamic_hit(val, g) for g in guarded):
                    hits.append(f"importlib.import_module({val!r})")
    return hits


def check_one_door(package_root: Path = PACKAGE_ROOT) -> tuple[bool, list[str]]:
    """R1 read-door lint: only engine/retrieve.py + store/read.py reach the ranking surface. The
    answer-path orchestrator (ORCHESTRATORS) may reach the ENGINE ranking modules through the door,
    but the STORE primitives (STORE_PRIMITIVES) stay guarded against it as against everyone else."""
    violations: list[str] = []
    for py in sorted(package_root.rglob("*.py")):
        rel = py.relative_to(package_root).as_posix()
        if rel in ALLOWED:
            continue
        guarded = STORE_PRIMITIVES if rel in ORCHESTRATORS else GUARDED_MODULES
        pkg = _rel_package(py, package_root)
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError as exc:                       # a syntax error is itself a lint failure
            violations.append(f"{rel}: syntax error {exc}")
            continue
        for hit in _read_violations(tree, pkg, guarded):
            violations.append(f"{rel}: reaches the guarded ranking surface ({hit}) - R1 second-door")
    return (not violations), violations


# ============================================================ REPOINT (write door): alpaca.wiki.*
# store/write.py is the one write door. The derived-data writers the alpaca.wiki tree carries (and
# will carry as later milestones land) are carved out BY NAME - the R1-F1 lesson applied to the
# write surface: watch the SQL verb, name every exception, never silence one.
WRITE_DOOR: set[str] = {"store/write.py"}                # the one write door
KNOWN_WRITERS: set[str] = {
    "store/db.py",        # schema pour + FTS('rebuild') + meta / privacy backfill (derived data)
    "store/vec.py",       # vector-index derived writes
    "store/ledger.py",    # append-log writer
    "engine/ledger.py",   # answer-ledger append
    "engine/echo.py", "engine/lucid.py", "engine/tiering.py",   # derived-data writers
    "ingest/shelf_life.py",                                     # shelf-life sweep
    "reflect/cure.py", "reflect/dream.py", "reflect/metamemory.py",   # reflect writers (disarmed)
}
_MUTATING_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)


def _raw_mutations(tree: ast.AST) -> list[str]:
    """Return hits for any conn.execute*(<mutating SQL literal>, ...) in this module."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("execute", "executemany", "executescript"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            if _MUTATING_SQL.match(node.args[0].value):
                verb = node.args[0].value.split()[0].upper()
                hits.append(f"{node.func.attr}({verb} ...)")
    return hits


def check_one_write_door(package_root: Path = PACKAGE_ROOT) -> tuple[bool, list[str]]:
    """Write-door lint: no raw mutating conn.execute outside the door + the NAMED known writers."""
    allowed = WRITE_DOOR | KNOWN_WRITERS
    violations: list[str] = []
    for py in sorted(package_root.rglob("*.py")):
        rel = py.relative_to(package_root).as_posix()
        if rel in allowed:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            violations.append(f"{rel}: syntax error {exc}")
            continue
        for hit in _raw_mutations(tree):
            violations.append(f"{rel}: raw mutating conn.execute outside the write door ({hit})")
    return (not violations), violations
