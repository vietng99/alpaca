"""The partner runbook kit: `alpaca runbook kit [--out DIR] [--zip]`.

A partner has none of Alpaca. They hand a folder to their own agent (Claude Code, Codex, Cursor,
any agent that reads AGENTS.md, or a chat model) and must get back a runbook.yaml that
`alpaca runbook check` and `alpaca intake` accept. This module builds that folder, named
alpaca-runbook-kit-v<format>, from the product's own files, so the kit cannot drift from what the
machine takes:

* check_runbook.py is carried from alpaca/runbook.py, with the verdict contract
  (alpaca/gates/verdict.py) and the OpenSpec change reader (alpaca/intake.py) inlined. The
  builder copies the definitions as they are, drops the imports of those modules and their
  `verdict.` / `runbook.` / `intake.` prefixes, and leaves out the parts that run a stage or a
  plugin script. It then refuses its own output when a carried definition names something the
  file does not define. Only the entry point `main` is written for the kit.
* runbook.schema.json is generated from the field tables of alpaca/runbook.py.
* FORMAT.md is docs/runbook-format.md rewritten by the rules in FORMAT_RULES; AGENTS.md is the
  body of the forge skill rewritten by AGENT_RULES, between kit-only parts. A rule that no longer
  matches stops the build and names itself, so a changed document never ships half rewritten.
* example/ is templates/runbook-example/ byte for byte, refused when it names a product-only
  path outside EXAMPLE_ALLOW; LICENSE is the product license.
* the parts written for the partner only live in templates/runbook-kit/ (see its ABOUT.md).

The build is deterministic: the same sources give the same files and the same zip bytes. It
writes only the kit folder (and the zip) under --out, and never the record.
"""
from __future__ import annotations

import ast
import builtins
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import symtable
import sys
import tokenize
import zipfile

from alpaca import runbook
from alpaca.gates import verdict

KIT_NAME = "alpaca-runbook-kit-v%d" % runbook.FORMAT
GATE = "alpaca-runbook-kit"
PARTS = "templates/runbook-kit"
FORMAT_DOC = "docs/runbook-format.md"
SKILL = ".claude/skills/alpaca-runbook-forge/SKILL.md"
EXAMPLE = "templates/runbook-example"

#: every product file or folder the kit is built from, relative to the product root
SOURCES = ("alpaca/runbook.py", "alpaca/gates/verdict.py", "alpaca/intake.py", FORMAT_DOC, SKILL,
           EXAMPLE, PARTS, "LICENSE")

#: what check_runbook.py carries from each module: `keep` names the top-level definitions taken,
#: `drop` the ones left out (everything else is taken)
CARRY = (
    ("alpaca/gates/verdict.py", {"keep": {
        "PASS", "USAGE", "VERDICT_BAND", "HARNESS_BAND", "_NAME", "_HARNESS_NAME", "ContractError",
        "name_of", "contract_map", "gate_line", "_BandParser", "make_parser"}}),
    ("alpaca/runbook.py", {"drop": {
        # the parts that give a check its meaning when a stage runs: a runner's, not a reader's
        "knob_values", "substitute", "_field", "_inside", "_compare", "evaluate", "_run_plugin",
        "next_attempt", "PLUGIN_TIMEOUT",
        # the product CLI glue; the kit has its own entry point
        "_from_caller", "USAGE_TEXT", "cmd_runbook", "_parser", "_register"}}),
    ("alpaca/intake.py", {"keep": {
        # the OpenSpec change reader `alpaca runbook check` uses for a change folder
        "_RENAME", "IntakeError", "_is_change", "_delta_sections", "_spec_files", "_capability",
        "_requirements", "effective_change"}}),
)
#: module names whose `name.` prefix is removed once everything sits in one file
PREFIXES = ("verdict", "runbook", "intake")
#: the only modules the kit checker may import
ALLOWED_IMPORTS = {"__future__", "argparse", "difflib", "json", "math", "os", "re", "sys", "yaml"}

EXECUTABLE = {"check_runbook.py", "example/checks/status_codes.py"}
ZIP_TIME = (1980, 1, 1, 0, 0, 0)

#: the example is the product's own, byte for byte; these phrases in it name the product side and
#: sit next to the kit's own equivalent, so they may stay (the build checks that they are there)
EXAMPLE_ALLOW = {
    "example/runbook.yaml": ("alpaca runbook check runbook.yaml", "docs/runbook-format.md"),
    "example/checks/status_codes.py": ("docs/runbook-format.md",),
}

#: words that name a product-only path or verb; no kit document may carry one
PRODUCT_ONLY = ("alpaca runbook", "alpaca intake", "alpaca/", "docs/", "contracts/", "bin/alpaca",
                "ALPACA_CALLER_CWD", ".claude/skills/alpaca", "templates/runbook-example", "autodrive",
                "project.yaml", "next_attempt", "{{")


class KitError(Exception):
    """The kit cannot be built from these sources (FAIL)."""


class KitBlocked(Exception):
    """The kit could be built, but the place it would go holds something else (BLOCKED)."""


def product_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(root, rel):
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise KitError("cannot read %s: %s" % (rel, exc))


def _fill(text):
    return text.replace("{{FORMAT}}", str(runbook.FORMAT)).replace("{{KIT}}", KIT_NAME)


# ----------------------------------------------------------------------------- the checker
def _names(node):
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        return {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    return set()


def _alpaca_import(node):
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] == "alpaca"
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] == "alpaca" for a in node.names)
    return False


def _strip_prefixes(text, where):
    """Remove `verdict.`, `runbook.` and `intake.` in front of a name (code tokens only, never in
    a string or a comment)."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def at(pos):
        return offsets[pos[0] - 1] + pos[1]

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError) as exc:
        raise KitError("%s: the carried code does not tokenize: %s" % (where, exc))
    cuts, prev = [], None
    for n, tok in enumerate(tokens):
        if tok.type == tokenize.NAME and tok.string in PREFIXES and n + 1 < len(tokens):
            nxt = tokens[n + 1]
            if nxt.type == tokenize.OP and nxt.string == "." and nxt.start == tok.end and not (
                    prev is not None and prev.type == tokenize.OP and prev.string == "."):
                cuts.append((at(tok.start), at(nxt.end)))
        if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT,
                            tokenize.DEDENT):
            prev = tok
    for start, end in reversed(cuts):
        text = text[:start] + text[end:]
    return text


def _carry(root, rel, keep=None, drop=()):
    """(text, names): the kept top-level definitions of one module, each with the comment lines
    above it, its alpaca imports removed."""
    source = _read(root, rel)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise KitError("%s does not parse: %s" % (rel, exc))
    lines = source.splitlines(keepends=True)
    pieces, names, prev_end = [], set(), 0
    for node in tree.body:
        start, end = prev_end, node.end_lineno
        prev_end = end
        own = _names(node)
        if not own:
            continue                    # the docstring, imports, `_register()`, `if __name__`
        if (keep is not None and not own & keep) or own & set(drop):
            continue
        cut = set()
        for sub in ast.walk(node):
            if _alpaca_import(sub):
                first = lines[sub.lineno - 1]
                last = lines[sub.end_lineno - 1]
                if first[:sub.col_offset].strip() or last[sub.end_col_offset:].strip():
                    raise KitError("%s line %d: an alpaca import shares its line with other code, so "
                                   "it cannot be dropped cleanly" % (rel, sub.lineno))
                cut.update(range(sub.lineno - 1, sub.end_lineno))
        text = "".join(line for n, line in enumerate(lines[start:end], start) if n not in cut)
        pieces.append(text)
        names |= own
    missing = (set(keep) - names) if keep is not None else set()
    if missing:
        raise KitError("%s no longer defines %s, which the kit checker carries"
                       % (rel, ", ".join(sorted(missing))))
    body = "".join(pieces).lstrip("\n")
    return _strip_prefixes(body, rel), names


CHECKER_HEAD = '''#!/usr/bin/env python3
"""check_runbook.py: the Alpaca runbook checker, format {format}, standalone.

    python3 check_runbook.py <runbook.yaml> [--spec <path>] [--no-files] [--json]

Built by `alpaca runbook kit` from the product's own checking code; do not edit it by hand. The
sections below are carried from these product files:

{sources}

Each definition is copied as it is; the imports of those modules and their `verdict.`,
`runbook.` and `intake.` prefixes are removed, because everything now sits in this one file. Left
out on purpose: the parts that run a stage command or a plugin script (the product's evaluate
and next_attempt), and everything that writes a record. This file only reads files. Only `main`
at the end is written for the kit.

Needs Python 3.9 or later and PyYAML. Exit status: 0 PASS, 1 FAIL, 2 BLOCKED, 64 usage error,
65 PyYAML is not installed.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import sys
'''

SECTION = '''

# ---------------------------------------------------------------------------------------------
# carried from {rel}
# ---------------------------------------------------------------------------------------------
'''

CHECKER_MAIN = '''

# ---------------------------------------------------------------------------------------------
# the kit's entry point, written for the kit; the check itself is run_check above
# ---------------------------------------------------------------------------------------------
def main(argv=None):
    try:
        import yaml  # noqa: F401  (load() reads the runbook with it)
    except ImportError:
        sys.stderr.write("check_runbook.py needs PyYAML, which is not installed for this Python. "
                         "Install it with\\n    python3 -m pip install pyyaml\\nand run the check again.\\n")
        return ABSENT
    parser = make_parser(prog="check_runbook.py", name="check_runbook",
                         description="Check a runbook against the Alpaca runbook format (FORMAT.md) "
                                     "and, with a spec, its coverage of the spec. Reads files only.")
    band_error = parser.error

    def error(message):
        # the usage line first, for the person; the HARNESS-ERROR line stays last
        sys.stderr.write(parser.format_usage())
        band_error(message)

    parser.error = error
    add_check_arguments(parser)
    args = parser.parse_args(argv)
    return run_check(args.file, args.spec, args.no_files, args.json)


if __name__ == "__main__":
    sys.exit(main())
'''


def _undefined(src):
    """Names the file reads as globals but defines nowhere (and that are not builtins)."""
    top = symtable.symtable(src, "check_runbook.py", "exec")
    defined = {s.get_name() for s in top.get_symbols()
               if s.is_assigned() or s.is_imported() or s.is_namespace()}
    missing = set()

    def walk(table):
        for sym in table.get_symbols():
            if not sym.is_referenced():
                continue
            if table is top or sym.is_global():
                name = sym.get_name()
                if name not in defined and not hasattr(builtins, name):
                    missing.add(name)
        for child in table.get_children():
            walk(child)

    walk(top)
    return missing


def build_checker(root):
    """The text of check_runbook.py."""
    parts, seen = [], {}
    for rel, how in CARRY:
        text, names = _carry(root, rel, keep=how.get("keep"), drop=how.get("drop", ()))
        for name in names:
            if name in seen:
                raise KitError("%s and %s both define %s" % (seen[name], rel, name))
            seen[name] = rel
        parts.append(SECTION.format(rel=rel) + text.rstrip("\n") + "\n")
    head = CHECKER_HEAD.format(format=runbook.FORMAT, sources="\n".join("    " + rel for rel, _how in CARRY))
    src = head + "".join(parts) + CHECKER_MAIN
    try:
        tree = ast.parse(src, feature_version=(3, 9))
    except SyntaxError as exc:
        raise KitError("the carried checker is not Python 3.9 code: %s" % exc)
    missing = _undefined(src)
    if missing:
        raise KitError("the carried checker uses %s, which it does not carry; add the definition to "
                       "CARRY in alpaca/runbook_kit.py or remove the use" % ", ".join(sorted(missing)))
    for node in ast.walk(tree):
        mod = None
        if isinstance(node, ast.Import):
            mod = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mod = {(node.module or "").split(".")[0]}
        if mod and not mod <= ALLOWED_IMPORTS:
            raise KitError("the carried checker imports %s; it may import only the standard "
                           "library modules %s and yaml" % (", ".join(sorted(mod - ALLOWED_IMPORTS)),
                                                            ", ".join(sorted(ALLOWED_IMPORTS - {"yaml"}))))
    return src


# ------------------------------------------------------------------------------ the schema
def schema():
    """runbook.schema.json (draft 2020-12), generated from the checker's field tables."""
    text = {"type": "string", "pattern": "\\S"}
    texts = {"type": "array", "items": {"$ref": "#/$defs/text"}}
    ref = lambda name: {"$ref": "#/$defs/%s" % name}          # noqa: E731
    slug = {"type": "string", "pattern": runbook.SLUG.pattern}

    def block(table, props, extra=None):
        missing = set(table) ^ set(props)
        if missing:
            raise KitError("the schema and the checker tables differ on %s" % ", ".join(sorted(missing)))
        out = {"type": "object", "properties": {k: props[k] for k in table},
               "required": [k for k, req in table.items() if req], "additionalProperties": False}
        out.update(extra or {})
        return out

    knob_kind = []
    for kind, value in (("int", {"type": "integer"}), ("float", {"type": "number"}),
                        ("str", {"type": "string"}), ("bool", {"type": "boolean"})):
        knob_kind.append({"if": {"required": ["type"], "properties": {"type": {"const": kind}}},
                          "then": {"properties": {"default": value}}})
    knob_kind.append({"if": {"required": ["type"], "properties": {"type": {"const": "enum"}}},
                      "then": {"required": ["values"]},
                      "else": {"not": {"required": ["values"]}}})
    knob_kind.append({"if": {"required": ["type"], "properties": {"type": {"enum": ["str", "bool", "enum"]}}},
                      "then": {"not": {"anyOf": [{"required": ["min"]}, {"required": ["max"]}]}}})
    knob = block(runbook.KNOB_KEYS, {
        "id": {"type": "string", "pattern": runbook.KNOB_ID.pattern}, "description": ref("text"),
        "type": {"enum": list(runbook.KNOB_TYPES)}, "default": {"description": "fits the type (and "
                                                                  "min..max, or one of values)"},
        "min": {"type": "number"}, "max": {"type": "number"},
        "values": {"type": "array", "minItems": 1}, "owner_only": {"type": "boolean"}},
        {"allOf": knob_kind})

    check_base = dict(runbook.CHECK_KEYS)
    common = {"id": slug, "type": None, "description": ref("text"), "covers": ref("texts")}
    number_ops = [op for op in runbook.OPS if op not in ("==", "!=")]

    def whole(bounds):
        return {"type": "integer", "minimum": bounds[0], "maximum": bounds[1]}

    type_props = {
        "exit-code": {"expect": whole(runbook.EXPECT_RANGE)},
        "file-exists": {"path": ref("path"), "non_empty": {"type": "boolean"}},
        "regex-in-file": {"path": ref("path"), "pattern": ref("text"), "absent": {"type": "boolean"},
                          "ignore_case": {"type": "boolean"}},
        "json-field": {"path": ref("path"), "field": ref("text"), "op": {"enum": list(runbook.OPS)},
                       "value": {"type": ["string", "number", "boolean"]}},
        "plugin": {"script": {"allOf": [ref("path"), {"not": {"pattern": runbook.VAR.pattern}}]},
                   "args": {"type": "array", "items": {"type": ["string", "number"]}},
                   "timeout": whole(runbook.PLUGIN_TIMEOUT_RANGE)},
    }
    defs = {"text": text, "texts": texts,
            "path": {"type": "string", "pattern": "\\S", "not": {"pattern": "^(?:/|\\.\\.(?:/|$))"},
                     "description": "relative to the runbook folder, never climbing out of it"},
            "knob": knob}
    branches = []
    for kind in runbook.CHECK_TYPES:
        name = "check_" + kind.replace("-", "_")
        table = dict(check_base)
        table.update(runbook.TYPE_KEYS[kind])
        props = dict(common, type={"const": kind})
        props.update(type_props[kind])
        extra = None
        if kind == "json-field":
            extra = {"if": {"required": ["op"], "properties": {"op": {"enum": number_ops}}},
                     "then": {"properties": {"value": {"anyOf": [
                         {"type": "number"}, {"type": "string", "pattern": "^\\$\\{[^}]*\\}$"}]}}}}
        defs[name] = block(table, props, extra)
        branches.append({"if": {"required": ["type"], "properties": {"type": {"const": kind}}},
                         "then": ref(name)})
    defs["check"] = {"type": "object", "required": [k for k, r in check_base.items() if r],
                     "properties": {"id": slug, "type": {"enum": list(runbook.CHECK_TYPES)}},
                     "allOf": branches}
    defs["move"] = block(runbook.MOVE_KEYS, {
        "knob": {"type": "string", "pattern": runbook.KNOB_ID.pattern},
        "by": {"type": "number", "not": {"const": 0}}})
    defs["retry"] = block(runbook.RETRY_KEYS, {
        "max_attempts": {"type": "integer", "minimum": 1, "maximum": runbook.MAX_ATTEMPTS},
        "on_fail": ref("texts"), "stop_on": ref("texts"), "move": ref("move")})
    defs["owner_gate"] = block(runbook.GATE_KEYS, {"approve": ref("text"), "evidence": ref("texts"),
                                                   "covers": ref("texts")})
    defs["output"] = {"anyOf": [ref("text"), block(runbook.OUTPUT_KEYS, {"path": ref("text"),
                                                                         "what": ref("text")})]}
    defs["fail"] = block(runbook.FAIL_KEYS, {"id": slug, "when": ref("text")})
    defs["stage"] = block(runbook.STAGE_KEYS, {
        "id": slug, "title": ref("text"), "description": ref("text"), "needs": ref("texts"),
        "run": ref("text"), "workdir": ref("path"),
        "timeout": whole(runbook.STAGE_TIMEOUT_RANGE),
        "env": {"type": "object", "additionalProperties": {"type": ["string", "number"]}},
        "inputs": ref("texts"), "outputs": {"type": "array", "items": ref("output")},
        "checks": {"type": "array", "items": ref("check")}, "retry": ref("retry"),
        "fails": {"type": "array", "items": ref("fail")}, "owner_gate": ref("owner_gate")},
        {"anyOf": [{"required": ["run"]}, {"required": ["owner_gate"]}],
         "if": {"required": ["run"]},
         "then": {"required": ["checks"], "properties": {"checks": {"minItems": 1}}},
         "else": {"not": {"required": ["checks"]}}})
    top = block(runbook.TOP_KEYS, {
        "runbook": {"const": runbook.FORMAT}, "id": slug, "title": ref("text"),
        "description": ref("text"), "spec": ref("path"), "owner": ref("text"),
        "knobs": {"type": "array", "items": ref("knob")},
        "stages": {"type": "array", "minItems": 1, "items": ref("stage")}})
    out = {"$schema": "https://json-schema.org/draft/2020-12/schema",
           "title": "Alpaca runbook, format %d" % runbook.FORMAT,
           "description": "The shape of runbook.yaml (FORMAT.md). check_runbook.py is the authority: "
                          "a file this schema accepts can still fail the checker.",
           "$comment": "Generated by the kit build from the checker's field tables. A JSON Schema "
                       "cannot state these checker rules: ids unique in the runbook, needs naming "
                       "an earlier stage, ${NAME} naming a knob or built-in variable, retry lists "
                       "naming checks of the same stage, a moved knob being a declared number knob "
                       "that is not owner_only, a knob default inside min..max or among values, a "
                       "pattern that compiles, a plugin script that exists and is executable, a "
                       "path that leaves the folder only after normalising, a whole number written "
                       "as 2.0, keys given twice, and coverage of the spec."}
    out.update(top)
    out["$defs"] = defs
    return out


# ------------------------------------------------------------------------------ the texts
def _loose(literal):
    """A regex for `literal` where any run of white space (a line break too) matches any other."""
    return r"\s+".join(re.escape(part) for part in literal.split())


#: docs/runbook-format.md -> FORMAT.md, in order. (label, find, replace, how many at least)
FORMAT_RULES = (
    ("checker in the intro", "`alpaca runbook check <file> [--spec <path>]` reads a runbook",
     "`check_runbook.py <file> [--spec <path>]` reads a runbook", 1),
    ("the forge skill", "The skill `/alpaca-runbook-forge` (`.claude/skills/alpaca-runbook-forge/SKILL.md`) "
     "writes a runbook from a spec and asks the person only for what the spec leaves out.",
     "`AGENTS.md` (for Claude Code, the skill `/runbook-forge`) tells an agent how to write a\n"
     "runbook from a spec and ask the person only for what the spec leaves out.", 1),
    ("example folder", "A worked example lives in `templates/runbook-example/`:",
     "A worked example lives in `example/`:", 1),
    ("example check", "`alpaca runbook check templates/runbook-example/runbook.yaml` passes on it.",
     "`python3 check_runbook.py example/runbook.yaml`\npasses on it.", 1),
    ("intake in the intro", "Intake reads the runbook and turns it into checklist rows and task contracts.",
     "On our side, intake reads the\nrunbook and turns it into checklist rows and task contracts.", 1),
    ("PyYAML reason", "- Alpaca already reads YAML (`project.yaml`) and ships PyYAML, so the format adds "
     "no dependency.", "- The checker needs only Python and PyYAML.", 1),
    ("verdict module", "the verdict contract (`alpaca/gates/verdict.py`):", "the verdict contract:", 1),
    ("plugin contract source", "The contract is the one `contracts/README.md` sets for acceptance scripts:",
     "The contract:", 1),
    ("example plugin", "`templates/runbook-example/checks/status_codes.py` is a complete plugin check.",
     "`example/checks/status_codes.py` is a complete plugin check.", 1),
    ("change folder reader", "the way `alpaca intake` reads it (`docs/intake.md`):",
     "the way our intake reads it:", 1),
    ("spec-kit to OpenSpec", "keeps its runbook (see `docs/intake.md`).", "keeps its runbook.", 1),
    ("retry rule owner", "`alpaca.runbook.next_attempt` implements this rule.", "Our runner applies this rule.", 1),
    ("owner gate levels", "Nothing in Alpaca moves it on its own, at any autodrive level.",
     "Nothing on our side moves it on its own.", 1),
    ("usage line", "alpaca runbook check <file> [--spec <path>] [--no-files] [--json]",
     "python3 check_runbook.py <file> [--spec <path>] [--no-files] [--json]", 1),
    ("relative paths", re.compile(r"- A relative `<file>` or `--spec` path is read from the folder.*?holds both files\.",
                                  re.S),
     "- A relative `<file>` or `--spec` path is read from the folder you run the command in, so\n"
     "  `python3 <kit>/check_runbook.py runbook.yaml --spec spec.md` works from the folder that holds\n"
     "  both files.", 1),
    ("exit status", "cannot be read), 64 usage error.",
     "cannot be read), 64 usage error, and 65 when PyYAML is not installed (the kit's checker only).", 1),
    ("read-only", "The verb reads files and writes nothing, not even to the record.",
     "The checker reads files and writes nothing. It never runs a stage command or a plugin script;\n"
     "those run only on our side, when the runbook is executed. It is the same code as the check our\n"
     "machine runs, so a runbook that passes here passes there.", 1),
    ("the verb name", "`alpaca runbook check`", "`check_runbook.py`", 1),
)

#: whole sections of docs/runbook-format.md: dropped (None) or replaced by a part file
FORMAT_SECTIONS = {"## The partner kit: `alpaca runbook kit`": None,
                   "## What intake takes from a runbook": "FORMAT-our-side.md",
                   "## Where the pieces come from": None}

#: the forge skill body -> AGENTS.md, in order
AGENT_RULES = (
    ("read-only verb", "Forging writes a plan. Do not run the stages, and do not run any `alpaca` verb that "
     "writes the record. The only verb this skill runs is `alpaca runbook check`, which is read-only.",
     "Forging writes a plan. Do not run the stages. The only command you run is\n"
     "  `check_runbook.py`, which only reads files.", 1),
    ("check command", "alpaca runbook check runbook.yaml --spec spec.md",
     "python3 <kit>/check_runbook.py runbook.yaml --spec spec.md", 2),
    ("format document", "`docs/runbook-format.md`", "`<kit>/FORMAT.md`", 2),
    ("example folder", "`templates/runbook-example/`", "`<kit>/example/`", 1),
    ("the verb name", "`alpaca runbook check`", "`check_runbook.py`", 0),
)


def _apply(text, rules, source):
    for label, find, repl, least in rules:
        pattern = find if hasattr(find, "sub") else re.compile(_loose(find))
        text, count = pattern.subn(lambda _m, r=repl: r, text)
        if count < least:
            raise KitError("%s changed: the kit rule %r no longer matches (found %d, needs %d); update "
                           "the rule in alpaca/runbook_kit.py" % (source, label, count, least))
    return text


def _guard(text, name, source, allow=()):
    scan = text
    for phrase in allow:
        if phrase not in scan:
            raise KitError("%s no longer holds %r, which EXAMPLE_ALLOW in alpaca/runbook_kit.py lets "
                           "it keep; update the list" % (source, phrase))
        scan = scan.replace(phrase, "")
    found = [w for w in PRODUCT_ONLY if w in scan]
    if found:
        raise KitError("%s would carry product-only text %s (from %s); add a rule in "
                       "alpaca/runbook_kit.py" % (name, ", ".join(repr(w) for w in found), source))
    return text


def _sections(text):
    """[(heading or "", lines)] split at level-two headings outside code fences."""
    out, fence = [("", [])], False
    for line in text.split("\n"):
        if line.startswith("```"):
            fence = not fence
        if not fence and line.startswith("## "):
            out.append((line.rstrip(), []))
        out[-1][1].append(line)
    return out


def build_format(root):
    doc = _read(root, FORMAT_DOC)
    parts = []
    found = set()
    for heading, lines in _sections(doc):
        if heading in FORMAT_SECTIONS:
            found.add(heading)
            part = FORMAT_SECTIONS[heading]
            if part:
                parts.append(_read(root, os.path.join(PARTS, part)).rstrip("\n") + "\n")
            continue
        parts.append("\n".join(lines).rstrip("\n") + "\n")
    missing = set(FORMAT_SECTIONS) - found
    if missing:
        raise KitError("%s changed: the kit expects the section(s) %s" % (FORMAT_DOC, ", ".join(sorted(missing))))
    text = "\n".join(parts)
    title, rest = text.split("\n", 1)
    if not title.startswith("# "):
        raise KitError("%s changed: it no longer starts with a title" % FORMAT_DOC)
    note = _read(root, os.path.join(PARTS, "FORMAT-note.md")).strip("\n")
    text = title + "\n\n" + note + "\n" + rest
    text = _apply(text, FORMAT_RULES, FORMAT_DOC)
    return _guard(_fill(text).rstrip("\n") + "\n", "FORMAT.md", FORMAT_DOC)


def build_agents(root):
    skill = _read(root, SKILL)
    m = re.search(r"^# .*\n", skill, re.M)
    if not m or not skill.startswith("---"):
        raise KitError("%s changed: the kit expects front matter and a title" % SKILL)
    body = skill[m.end():]
    if "\n## Never\n" not in body:
        raise KitError("%s changed: the kit expects a `## Never` section" % SKILL)
    body = _apply(body, AGENT_RULES, SKILL)
    steps, never = body.split("\n## Never\n", 1)
    text = "\n".join([
        _read(root, os.path.join(PARTS, "AGENTS-intro.md")).rstrip("\n") + "\n",
        steps.strip("\n") + "\n",
        _read(root, os.path.join(PARTS, "AGENTS-deliver.md")).rstrip("\n") + "\n",
        "## Never\n" + never.rstrip("\n") + "\n" + _read(root, os.path.join(PARTS, "AGENTS-never.md")).rstrip("\n"),
    ]) + "\n"
    return _guard(_fill(text), "AGENTS.md", SKILL)


def _part(root, rel, name):
    return _guard(_fill(_read(root, os.path.join(PARTS, rel))), name, os.path.join(PARTS, rel))


# ------------------------------------------------------------------------------ the build
def _source_files(root):
    out = []
    for rel in SOURCES:
        full = os.path.join(root, rel)
        if os.path.isdir(full):
            for base, dirs, names in os.walk(full):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for name in sorted(names):
                    out.append(os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/"))
        elif os.path.isfile(full):
            out.append(rel)
        else:
            raise KitError("the kit source %s is missing" % rel)
    return sorted(out)


def _sources_digest(root):
    h = hashlib.sha256()
    for rel in _source_files(root):
        with open(os.path.join(root, rel), "rb") as fh:
            h.update(("%s\0%s\n" % (rel, hashlib.sha256(fh.read()).hexdigest())).encode("ascii"))
    return h.hexdigest()


def _commit(root):
    def git(*args):
        return subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=True,
                              timeout=60, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    try:
        head = git("rev-parse", "--verify", "-q", "HEAD")
        if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip()):
            return "unknown"
        top = git("rev-parse", "--show-toplevel").stdout.strip()
        if not top or os.path.realpath(top) != os.path.realpath(root):
            return "unknown"
        state = git("status", "--porcelain", "--", *SOURCES)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = head.stdout.strip()
    if state.returncode != 0 or state.stdout.strip():
        return "%s (the kit sources differ from this commit)" % sha
    return sha


def compose(root):
    """{kit path: bytes} for every file of the kit, SHA256SUMS last."""
    files = {}
    files["README.md"] = _part(root, "README.md", "README.md")
    files["AGENTS.md"] = build_agents(root)
    files["CLAUDE.md"] = _part(root, "CLAUDE.md", "CLAUDE.md")
    files[".claude/skills/runbook-forge/SKILL.md"] = (
        _part(root, "SKILL-head.md", "SKILL.md").rstrip("\n") + "\n\n" + files["AGENTS.md"])
    files["FORMAT.md"] = build_format(root)
    files["runbook.schema.json"] = json.dumps(schema(), indent=2) + "\n"
    files["check_runbook.py"] = build_checker(root)
    for rel in ("runbook.yaml", "spec.md", "OPENSPEC.md"):
        files["templates/" + rel] = _part(root, "templates/" + rel, "templates/" + rel)
    for rel in _source_files(root):
        if rel.startswith(EXAMPLE + "/"):
            name = "example/" + rel[len(EXAMPLE) + 1:]
            files[name] = _guard(_read(root, rel), name, rel, EXAMPLE_ALLOW.get(name, ()))
    files["LICENSE"] = _read(root, "LICENSE")
    files["VERSION"] = _guard("format: %d\nkit: %s\nproduct_commit: %s\nsources_sha256: %s\n" % (
        runbook.FORMAT, KIT_NAME, _commit(root), _sources_digest(root)), "VERSION", "the build")
    data = {}
    for rel, text in files.items():
        raw = text.encode("utf-8")
        bad = sum(1 for b in raw if b > 0x7F)
        if bad or b"\r" in raw:
            raise KitError("%s would hold %d non-ASCII byte(s) or a carriage return; the kit is pure "
                           "ASCII text" % (rel, bad))
        data[rel] = raw
    sums = "".join("%s  %s\n" % (hashlib.sha256(data[rel]).hexdigest(), rel) for rel in sorted(data))
    data["SHA256SUMS"] = sums.encode("ascii")
    return data


def _is_kit(folder):
    try:
        with open(os.path.join(folder, "VERSION"), encoding="ascii") as fh:
            return ("kit: %s" % KIT_NAME) in fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return False


def _write(folder, data):
    for rel in sorted(data):
        full = os.path.join(folder, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data[rel])
        os.chmod(full, 0o755 if rel in EXECUTABLE else 0o644)
    for base, dirs, _names in os.walk(folder):
        for d in dirs:
            os.chmod(os.path.join(base, d), 0o755)
    os.chmod(folder, 0o755)


def _self_check(folder):
    """The kit's own checker, run as a partner runs it, must pass the example and the template.
    -E keeps PYTHONPATH out, and a script's own folder (the kit) is the first path entry, so the
    run sees no alpaca; the user site stays on, where a partner's PyYAML may live."""
    for target in ("example/runbook.yaml", "templates/runbook.yaml"):
        proc = subprocess.run([sys.executable, "-E", os.path.join(folder, "check_runbook.py"), target],
                              cwd=folder, capture_output=True, text=True, timeout=300,
                              env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"})
        last = (proc.stdout.strip().splitlines() or [""])[-1]
        if proc.returncode != verdict.PASS or last != verdict.gate_line("alpaca-runbook-check", verdict.PASS):
            raise KitError("the built checker does not pass %s (exit %d): %s" % (
                target, proc.returncode, (proc.stdout + proc.stderr).strip()[-800:]))


def _zip(folder_data, path):
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w") as zf:
        for rel in sorted(folder_data):
            info = zipfile.ZipInfo("%s/%s" % (KIT_NAME, rel), date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = ((0o100755 if rel in EXECUTABLE else 0o100644) << 16)
            zf.writestr(info, folder_data[rel], compresslevel=9)
    os.replace(tmp, path)
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def build(root, out_dir, make_zip=False):
    """Build the kit folder <out_dir>/<KIT_NAME> (and <KIT_NAME>.zip). Returns {"kit", "zip",
    "zip_sha256", "files"}. Raises KitError when the sources do not give a kit, KitBlocked when the
    kit folder's place holds something that is not an earlier kit."""
    root = os.path.abspath(root)
    out_dir = os.path.abspath(out_dir)
    dest = os.path.join(out_dir, KIT_NAME)
    if os.path.lexists(dest) and not (os.path.isdir(dest) and not os.path.islink(dest) and
                                      (_is_kit(dest) or not os.listdir(dest))):
        raise KitBlocked("%s exists and is not a kit this verb built; move it away or choose "
                         "another --out" % dest)
    data = compose(root)
    os.makedirs(out_dir, exist_ok=True)
    stage = os.path.join(out_dir, ".%s.building-%d" % (KIT_NAME, os.getpid()))
    shutil.rmtree(stage, ignore_errors=True)
    try:
        os.makedirs(stage)
        _write(stage, data)
        _self_check(stage)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.rename(stage, dest)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    result = {"kit": dest, "zip": None, "zip_sha256": None, "files": sorted(data)}
    if make_zip:
        result["zip"] = os.path.join(out_dir, KIT_NAME + ".zip")
        result["zip_sha256"] = _zip(data, result["zip"])
    return result


# ------------------------------------------------------------------------------- the verb
def cmd_kit(args):
    out = runbook._from_caller(getattr(args, "out", None) or ".")
    try:
        result = build(product_root(), out, make_zip=bool(getattr(args, "zip", False)))
    except KitBlocked as exc:
        print("refused: %s" % exc)
        print(verdict.gate_line(GATE, verdict.BLOCKED))
        return verdict.BLOCKED
    except KitError as exc:
        print("the kit was not built: %s" % exc)
        print(verdict.gate_line(GATE, verdict.FAIL))
        return verdict.FAIL
    print("kit: %s (%d files)" % (result["kit"], len(result["files"])))
    if result["zip"]:
        print("zip: %s" % result["zip"])
        print("zip sha256: %s" % result["zip_sha256"])
    print(verdict.gate_line(GATE, verdict.PASS))
    return verdict.PASS
