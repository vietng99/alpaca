"""rc_conformance.py - THE verdict/exit-code conformance enforcer.

`alpaca/gates/verdict.py` defines the verdict<->exit-code contract once and states that any other
file which restates the map, squats a reserved verdict code, or speaks a private verdict
vocabulary is a defect. This tool is what reports it.

Ported from the earlier harness gates/rc_conformance.py and adapted for Alpaca: it points at the `alpaca/`
tree (not `gates/`) and imports the one contract from `alpaca.gates`. It NEVER executes a
candidate; a planted non-conforming module must not buy code execution inside the enforcer, so
every check is AST-only.

What it checks for the process-boundary population it governs (a *.py with an
`if __name__ == "__main__"` guard, under the scanned tree, excluding tests and scratch roots):

  RC-RAW-ARGPARSE       the boundary constructs a raw argparse.ArgumentParser(...). argparse
                        exits 2 on a usage error (BLOCKED squat) and 0 on --help (PASS squat);
                        the cure is verdict.make_parser(...).
  RC-CODE-SQUAT         the boundary imports no contract yet terminates with a literal
                        verdict-band code {0,1,2,3}: the numbers are restated, not referenced.
  RC-PRIVATE-VOCAB      the boundary imports no contract yet emits a `VERDICT: <word>` line
                        whose word is not one of the canonical four.
  RC-CONTRACT-DIVERGED  a file DEFINES a verdict->code map whose semantics diverge from the
                        running contract.

Able-to-fail in both directions: a squatter or a restated map is flagged (FAIL); a boundary
that delegates cleanly to the contract clears (PASS).
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

from alpaca.gates import contract as _c  # noqa: F401  (kept as the sibling primitive module)
from alpaca.gates import verdict as _v

INSTRUMENT = "rc-conformance"

_SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", ".mypy_cache",
                        ".pytest_cache", ".ruff_cache", "node_modules", ".tox", ".eggs",
                        "nuclear", "napalm", "work", "tests", "proposed", "fixtures"})

# A whole-token verdict member: the name ends, or continues only by `_`/`-`, so
# PAUSED_FOR_DECISION and PAUSED-FOR-DECISION normalise to PAUSED. It must NOT match
# `failures`, `passed`, `password`: a prefix match there turns this into a gate that cannot pass.
_MEMBER_RE = re.compile(r"^(PASS|FAIL|BLOCKED|PAUSED)([_-]|$)")

_VERDICT_RE = re.compile(r"\bVERDICT\b\s*[:\-]\s*([A-Za-z][A-Za-z0-9_\-]*)")
_CANON_WORDS = frozenset({"PASS", "FAIL", "BLOCKED", "PAUSED"})
_BAND_LITERALS = frozenset({0, 1, 2, 3})


# --------------------------------------------------------------- contract extraction (AST)
def _norm_member(name) -> str:
    s = str(name).upper().strip()
    m = _MEMBER_RE.match(s)
    return m.group(1) if m else s


def _int_of(node) -> Optional[int]:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _int_of(node.operand)
        return -inner if inner is not None else None
    return None


def _key_member(node) -> Optional[str]:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _map_from_dict(dnode) -> Optional[Dict[str, int]]:
    """{norm-member: int} for a verdict->code (or inverse code->verdict) dict literal; {} if it
    is not a verdict map; None if it is definer-shaped but not statically resolvable."""
    if any(k is None for k in dnode.keys):  # {**other}: cannot resolve statically
        return None
    out: Dict[str, int] = {}
    for k, val in zip(dnode.keys, dnode.values):  # forward: member -> int
        i = _int_of(val)
        km = _key_member(k)
        if km is not None and _MEMBER_RE.match(str(km).upper()) and i is not None:
            out[_norm_member(km)] = i
    if out:
        return out
    inv: Dict[str, int] = {}  # inverse: int -> member string
    for k, val in zip(dnode.keys, dnode.values):
        i = _int_of(k)
        s = val.value if isinstance(val, ast.Constant) and isinstance(val.value, str) else None
        if i is not None and s is not None and _MEMBER_RE.match(str(s).upper()):
            inv[_norm_member(s)] = i
    return inv


def _map_from_tuple_assign(node) -> Dict[str, int]:
    """{norm-member: int} for `PASS, FAIL, BLOCKED, PAUSED = 0, 1, 2, 3`."""
    out: Dict[str, int] = {}
    if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Tuple):
        return out
    for tgt in node.targets:
        if isinstance(tgt, ast.Tuple) and len(tgt.elts) == len(node.value.elts):
            for t, val in zip(tgt.elts, node.value.elts):
                i = _int_of(val)
                if isinstance(t, ast.Name) and i is not None \
                        and _MEMBER_RE.match((t.id or "").upper()):
                    out[_norm_member(t.id)] = i
    return out


def extract_contract(src_text) -> Optional[Dict[str, int]]:
    """AST-only. Return the verdict->code map a source statically DEFINES, or None when none is
    statically resolvable. Conflict detection, not last-wins: a member bound twice to different
    codes in one source raises (fail-closed) so a canonical decoy dict cannot launder a
    divergent map back to the running fingerprint."""
    try:
        tree = ast.parse(src_text or "")
    except SyntaxError:
        return None
    best: Optional[Dict[str, int]] = None

    def _merge(m):
        nonlocal best
        if best is None:
            best = dict(m)
            return
        for member, code in m.items():
            if member in best and best[member] != code:
                raise ValueError("CONTRACT-DEFINER-CONFLICT: %s bound to %r and %r"
                                 % (member, best[member], code))
            best[member] = code

    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            v = n.value
            if isinstance(v, ast.Dict):
                m = _map_from_dict(v)
                if m is None:
                    continue
                if len(m) >= 2:
                    _merge(m)
            elif isinstance(n, ast.Assign) and isinstance(v, ast.Tuple):
                m = _map_from_tuple_assign(n)
                if m and len(m) >= 2:
                    _merge(m)
    return best


def is_contract_definer(src_text) -> Tuple[bool, list]:
    """True when a source DEFINES the map (a `class Verdict` with the members, a verdict<->code
    dict literal, or a tuple-unpacked verdict-code assignment). Returns (is_definer, evidence)."""
    try:
        tree = ast.parse(src_text or "")
    except SyntaxError:
        return False, []
    evidence: List[Tuple[int, str]] = []
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == "Verdict":
            members = set()
            for s in n.body:
                tgts = []
                if isinstance(s, ast.Assign):
                    tgts = [t for t in s.targets if isinstance(t, ast.Name)]
                elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
                    tgts = [s.target]
                for t in tgts:
                    if _MEMBER_RE.match((t.id or "").upper()):
                        members.add(_norm_member(t.id))
            if len(members) >= 3:
                evidence.append((n.lineno, "class Verdict with members %s" % sorted(members)))
    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            v = n.value
            if isinstance(v, ast.Dict):
                m = _map_from_dict(v)
                if m and len(m) >= 2:
                    evidence.append((n.lineno, "verdict<->code mapping literal"))
            elif isinstance(n, ast.Assign) and isinstance(v, ast.Tuple):
                tm = _map_from_tuple_assign(n)
                if tm and len(tm) >= 2:
                    evidence.append((n.lineno, "tuple-unpacked verdict codes"))
    return (bool(evidence), evidence)


def _fingerprint(vmap) -> Optional[str]:
    if not vmap:
        return None
    doc = {"verdicts": {k: vmap[k] for k in sorted(vmap)}}
    return hashlib.sha256(_c.canonical(doc).encode("utf-8")).hexdigest()


def running_fingerprint() -> str:
    return _fingerprint({_norm_member(k): v for k, v in _v.contract_map().items()})


# --------------------------------------------------------------------- boundary analysis
def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _is_boundary_text(text):
    return "__name__" in text and "__main__" in text


def _imports_contract(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in ("verdict", "contract") or a.name.endswith(".verdict") \
                        or a.name.endswith(".contract"):
                    return True
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if mod in ("verdict", "contract") or mod.endswith(".verdict") \
                    or mod.endswith(".contract") or mod.endswith("gates"):
                for a in n.names:
                    if a.name in ("verdict", "contract"):
                        return True
            for a in n.names:
                if a.name in ("verdict", "contract"):
                    return True
    return False


def _raw_argparse_calls(tree):
    lines = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr == "ArgumentParser" \
                    and isinstance(f.value, ast.Name) and f.value.id == "argparse":
                lines.append(n.lineno)
    return lines


def _literal_band_exits(tree):
    lines = []

    def _lit(node):
        return (isinstance(node, ast.Constant) and isinstance(node.value, int)
                and not isinstance(node.value, bool) and node.value in _BAND_LITERALS)

    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            is_exit = (isinstance(f, ast.Name) and f.id in ("exit", "SystemExit")) or \
                      (isinstance(f, ast.Attribute) and f.attr == "exit"
                       and isinstance(f.value, ast.Name) and f.value.id == "sys")
            if is_exit and len(n.args) == 1 and _lit(n.args[0]):
                lines.append(n.lineno)
        elif isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call):
            f = n.exc.func
            if isinstance(f, ast.Name) and f.id == "SystemExit" and len(n.exc.args) == 1 \
                    and _lit(n.exc.args[0]):
                lines.append(n.lineno)
    return lines


def _private_vocab(tree):
    lines = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            m = _VERDICT_RE.search(n.value)
            if m and m.group(1).upper() not in _CANON_WORDS:
                lines.append(n.lineno)
    return lines


def analyze(path, *, force_boundary=False):
    """Return a list of (rule, line, detail) defects for one file. `force_boundary` treats the
    file as a boundary even without an `if __name__` guard (explicit-path mode)."""
    text = _read(path)
    if text is None:
        return [("RC-UNREADABLE", 0, "file is not valid UTF-8 / cannot be read")]
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [("RC-SYNTAX", getattr(e, "lineno", 0) or 0, "not parseable: %s" % e)]

    if not (force_boundary or _is_boundary_text(text)):
        return []  # not a process boundary; nothing to govern here

    is_def, _ev = is_contract_definer(text)

    defects = []
    for ln in _raw_argparse_calls(tree):
        defects.append((
            "RC-RAW-ARGPARSE", ln,
            "raw argparse.ArgumentParser(...) -- usage error exits 2 (BLOCKED squat), --help "
            "exits 0 (PASS squat); build it through verdict.make_parser()"))

    imports = _imports_contract(tree)
    if not imports and not is_def:
        for ln in _literal_band_exits(tree):
            defects.append((
                "RC-CODE-SQUAT", ln,
                "terminates with a literal verdict-band code {0,1,2,3} while importing no "
                "contract -- the verdict numbers are restated here, not referenced"))
        for ln in _private_vocab(tree):
            defects.append((
                "RC-PRIVATE-VOCAB", ln,
                "emits a private verdict vocabulary while importing no contract -- a caller has "
                "no projection onto the canonical four"))

    if is_def:
        try:
            m = extract_contract(text)
        except ValueError as exc:
            defects.append(("RC-CONTRACT-CONFLICT", 0, str(exc)))
            m = None
            return defects
        if m is None:
            defects.append((
                "RC-CONTRACT-UNRESOLVED", 0,
                "defines the contract but its map is not statically resolvable (never cleared)"))
        elif _fingerprint({_norm_member(k): v for k, v in m.items()}) != running_fingerprint():
            defects.append((
                "RC-CONTRACT-DIVERGED", 0,
                "restates the verdict->code map with DIFFERENT semantics %r than the running "
                "contract -- a caller decoding through the running map mis-reads it" % (m,)))
    return defects


# ------------------------------------------------------------------- population walks
def scan_paths(paths):
    """Check exactly these files (explicit-path mode). Returns {path: defects}."""
    defect_files = {}
    for p in paths:
        if not os.path.isfile(p):
            defect_files[p] = [("RC-ABSENT", 0, "path does not resolve")]
            continue
        d = analyze(p, force_boundary=True)
        if d:
            defect_files[p] = d
    return defect_files


def scan_tree(root):
    """Scan the boundary population under `root`; return (defect_files, checked_count)."""
    defect_files = {}
    checked = 0
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            text = _read(path)
            if text is None or not _is_boundary_text(text):
                continue
            checked += 1
            d = analyze(path)
            if d:
                defect_files[os.path.relpath(path, root)] = d
    return defect_files, checked


def check_paths(paths) -> int:
    """Verdict code for an explicit set of files: FAIL on any defect, PASS otherwise."""
    return _v.FAIL if scan_paths(paths) else _v.PASS


def _report(defect_files):
    for rel in sorted(defect_files):
        for rule, ln, detail in defect_files[rel]:
            print("  DEFECT %-22s %s:%d  %s" % (rule, rel, ln, detail))


def main(argv=None) -> int:
    ap = _v.make_parser(
        name=INSTRUMENT,
        description="verdict/exit-code conformance enforcer (import the ONE contract; do not "
                    "restate it, squat a reserved code, or speak a private vocabulary)")
    ap.add_argument("paths", nargs="*",
                    help="files to check; default is the alpaca/ boundary population")
    ap.add_argument("--root", default=None, help="tree to scan (default: the alpaca/ package)")
    a = ap.parse_args(argv)

    if a.paths:
        defect_files = scan_paths(a.paths)
        checked = len(a.paths)
    else:
        root = a.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        defect_files, checked = scan_tree(root)

    print("  rc_conformance -- %d boundary/boundaries checked" % checked)
    if defect_files:
        _report(defect_files)
        n = sum(len(v) for v in defect_files.values())
        return _v.emit_verdict(INSTRUMENT, _v.FAIL,
                               "%d defect(s) across %d file(s)" % (n, len(defect_files)))
    return _v.emit_verdict(INSTRUMENT, _v.PASS, "no verdict/exit-code conformance defects")


if __name__ == "__main__":
    sys.exit(main())
