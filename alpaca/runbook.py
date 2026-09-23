"""The runbook: a domain's executable plan, and `alpaca runbook check`.

A runbook sits between a spec and intake. The spec (spec-kit `spec.md` with FR-nnn and SC-nnn
ids, or an OpenSpec spec with `### Requirement:` blocks and `#### Scenario:` children) says what
must hold. The runbook says how it is shown: the stages, the command each stage runs, its inputs
and outputs, the pass checks, the knobs a retry may move, the retry rules and the owner gates.
Intake turns the runbook into checklist rows and task contracts. The format is documented in
docs/runbook-format.md; this module is its one reader.

One runbook is one YAML file (`runbook.yaml`). `check(path, spec_path)` reads it, refuses every
malformed field at once with a code, a dotted location and a plain message, and, with a spec,
fails when a success criterion or scenario is covered by no check or owner gate. An empty
measured population is never a pass: a spec with nothing to cover is refused too.

The verdicts come from alpaca.gates.verdict (0 PASS, 1 FAIL, 2 BLOCKED, 3 PAUSED); they are
referenced, never restated. A plugin check reports through the same band, the contract
contracts/README.md sets for acceptance scripts.

`evaluate` gives each check type its meaning and `next_attempt` gives the retry rule its meaning,
so a runner and intake share one reading of the file. `check` reads files but writes nothing,
and it never touches the record.
"""
from __future__ import annotations

import difflib
import json
import math
import os
import re
import subprocess
import sys

from alpaca.gates import verdict

FORMAT = 1
CHECK_TYPES = ("exit-code", "file-exists", "regex-in-file", "json-field", "plugin")
KNOB_TYPES = ("int", "float", "str", "bool", "enum")
OPS = ("==", "!=", "<", "<=", ">", ">=")
BUILTIN_VARS = ("RUNBOOK_DIR", "STAGE", "ATTEMPT", "EVIDENCE_DIR")
MAX_ATTEMPTS = 20
PLUGIN_TIMEOUT = 60

# Every field, per block, and whether it is required. docs/runbook-format.md names each one and
# test_runbook.py fails when the two drift apart.
TOP_KEYS = {"runbook": True, "id": True, "title": True, "description": False, "spec": False,
            "owner": False, "knobs": False, "stages": True}
KNOB_KEYS = {"id": True, "description": True, "type": True, "default": True, "min": False,
             "max": False, "values": False, "owner_only": False}
STAGE_KEYS = {"id": True, "title": False, "description": False, "needs": False, "run": False,
              "workdir": False, "timeout": False, "env": False, "inputs": False, "outputs": False,
              "checks": False, "retry": False, "fails": False, "owner_gate": False}
CHECK_KEYS = {"id": True, "type": True, "description": False, "covers": False}
TYPE_KEYS = {
    "exit-code": {"expect": False},
    "file-exists": {"path": True, "non_empty": False},
    "regex-in-file": {"path": True, "pattern": True, "absent": False, "ignore_case": False},
    "json-field": {"path": True, "field": True, "op": True, "value": True},
    "plugin": {"script": True, "args": False, "timeout": False},
}
RETRY_KEYS = {"max_attempts": True, "on_fail": False, "stop_on": False, "move": False}
MOVE_KEYS = {"knob": True, "by": True}
GATE_KEYS = {"approve": True, "evidence": False, "covers": False}
OUTPUT_KEYS = {"path": True, "what": False}
FAIL_KEYS = {"id": True, "when": True}

ERROR_CODES = (
    "FILE-UNREADABLE", "YAML-SYNTAX", "NOT-MAPPING", "KEY-DUPLICATE", "FIELD-MISSING",
    "FIELD-UNKNOWN", "FIELD-EMPTY", "FIELD-TYPE", "FIELD-NOT-LIST", "VERSION-UNSUPPORTED",
    "ID-INVALID", "ID-DUPLICATE", "NEEDS-UNKNOWN", "RUN-MISSING", "CHECKS-EMPTY",
    "CHECK-TYPE-UNKNOWN", "REGEX-INVALID", "OP-UNKNOWN", "VALUE-NOT-NUMBER", "RANGE",
    "PATH-ESCAPES", "PLUGIN-MISSING", "PLUGIN-NOT-EXECUTABLE", "KNOB-TYPE-UNKNOWN",
    "KNOB-DEFAULT-TYPE", "KNOB-DEFAULT-RANGE", "KNOB-UNKNOWN", "KNOB-OWNER-ONLY",
    "KNOB-NOT-NUMBER", "MOVE-NOT-INT", "RETRY-CHECK-UNKNOWN", "RETRY-OVERLAP", "VAR-UNKNOWN",
    "SPEC-MISSING", "SPEC-EMPTY", "SPEC-MIXED", "SPEC-UNCOVERED", "COVERS-UNKNOWN",
    "COVERS-AMBIGUOUS",
)
WARNING_CODES = ("FR-UNCOVERED", "SPEC-CLARIFY", "COVERS-WITHOUT-SPEC")

#: words people reach for, mapped to the field that means it (used only for the "did you mean" hint)
ALIASES = {"command": "run", "cmd": "run", "depends_on": "needs", "depends": "needs",
           "after": "needs", "retries": "retry", "attempts": "max_attempts", "tries": "max_attempts",
           "gate": "owner_gate", "approval": "owner_gate", "threshold": "value", "file": "path",
           "regex": "pattern", "key": "field", "expected": "expect", "exit": "expect",
           "cwd": "workdir", "name": "title"}

SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
KNOB_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VAR = re.compile(r"\$\{([^}]*)\}")


# ----------------------------------------------------------------------------- findings
def _finding(code, where, message):
    return {"code": code, "where": where, "message": message}


class _Report:
    def __init__(self):
        self.errors, self.warnings = [], []

    def error(self, code, where, message):
        assert code in ERROR_CODES, code
        self.errors.append(_finding(code, where, message))

    def warn(self, code, where, message):
        assert code in WARNING_CODES, code
        self.warnings.append(_finding(code, where, message))


# ------------------------------------------------------------------------------ loading
def _loader():
    import yaml

    class Loader(yaml.SafeLoader):
        """SafeLoader that records a mapping key given twice (PyYAML keeps the last silently)."""
        duplicates = None

    def construct_mapping(loader, node, deep=False):
        seen = set()
        for key_node, _value in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if isinstance(key, str) and key in seen:
                loader.duplicates.append((key, key_node.start_mark.line + 1))
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    return Loader


def load(path):
    """(data, report). data is None when the file cannot be read or parsed; the report then holds
    FILE-UNREADABLE (the caller answers BLOCKED) or YAML-SYNTAX / NOT-MAPPING (FAIL)."""
    import yaml
    report = _Report()
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        report.error("FILE-UNREADABLE", "", "cannot read %s: %s" % (path, exc))
        return None, report
    Loader = _loader()
    loader = Loader(text)
    loader.duplicates = []
    try:
        data = loader.get_single_data()
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = " at line %d, column %d" % (mark.line + 1, mark.column + 1) if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        report.error("YAML-SYNTAX", "", "the file is not valid YAML%s: %s" % (where, problem))
        return None, report
    finally:
        loader.dispose()
    for key, line in loader.duplicates:
        report.error("KEY-DUPLICATE", key, "key `%s` is given twice (again at line %d); YAML would keep "
                     "only the last one" % (key, line))
    if not isinstance(data, dict):
        report.error("NOT-MAPPING", "", "a runbook is a YAML mapping with `runbook`, `id`, `title` "
                     "and `stages` at the top, not a %s" % type(data).__name__)
        return None, report
    return data, report


# ---------------------------------------------------------------------------- the schema
def _name(value):
    return {bool: "true/false", int: "a whole number", float: "a number", str: "text",
            list: "a list", dict: "a mapping"}.get(type(value), type(value).__name__)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class _Checker:
    def __init__(self, data, base_dir, check_files, report):
        self.data, self.base, self.check_files, self.r = data, base_dir, check_files, report
        self.knobs = {}

    # -- generic field helpers
    def keys(self, block, allowed, where):
        """Report unknown keys (with the nearest allowed key) and missing required ones."""
        for key in block:
            if isinstance(key, bool):
                self.r.error("FIELD-UNKNOWN", self._at(where, str(key).lower()), "a key read as %s: YAML "
                             "reads a bare on/off/yes/no key as true or false; the retry field is "
                             "`on_fail`, and any other such key must be quoted" % str(key).lower())
                continue
            if key not in allowed:
                words = list(allowed) + [a for a, target in ALIASES.items() if target in allowed]
                near = difflib.get_close_matches(str(key), words, n=1, cutoff=0.6)
                hint = "; did you mean `%s`?" % ALIASES.get(near[0], near[0]) if near else ""
                self.r.error("FIELD-UNKNOWN", self._at(where, key),
                             "`%s` is not a field here; allowed: %s%s" % (
                                 key, ", ".join(sorted(allowed)), hint))
        for key, required in allowed.items():
            if required and key not in block:
                self.r.error("FIELD-MISSING", self._at(where, key), "`%s` is required here" % key)

    @staticmethod
    def _at(where, key):
        return "%s.%s" % (where, key) if where else str(key)

    def text(self, block, key, where, required=False):
        if key not in block:
            return None
        value = block[key]
        at = self._at(where, key)
        if not isinstance(value, str):
            self.r.error("FIELD-TYPE", at, "`%s` must be text, found %s" % (key, _name(value)))
            return None
        if not value.strip():
            self.r.error("FIELD-EMPTY", at, "`%s` is empty" % key)
            return None
        return value

    def flag(self, block, key, where):
        if key in block and not isinstance(block[key], bool):
            self.r.error("FIELD-TYPE", self._at(where, key), "`%s` must be true or false" % key)

    def whole(self, block, key, where, low, high):
        if key not in block:
            return None
        value = block[key]
        at = self._at(where, key)
        if isinstance(value, bool) or not isinstance(value, int):
            self.r.error("FIELD-TYPE", at, "`%s` must be a whole number, found %s" % (key, _name(value)))
            return None
        if not low <= value <= high:
            self.r.error("RANGE", at, "`%s` is %d; it must be between %d and %d" % (key, value, low, high))
            return None
        return value

    def items(self, block, key, where, kind=str):
        """A list field: FIELD-NOT-LIST when it is not a list, FIELD-TYPE for a bad item."""
        if key not in block:
            return []
        value = block[key]
        at = self._at(where, key)
        if not isinstance(value, list):
            self.r.error("FIELD-NOT-LIST", at, "`%s` must be a list (write `[%s]` for one item)" % (
                key, value if isinstance(value, str) else "..."))
            return []
        out = []
        for n, item in enumerate(value):
            if kind is str and (not isinstance(item, str) or not item.strip()):
                self.r.error("FIELD-TYPE", "%s[%d]" % (at, n), "each `%s` item must be non-empty text" % key)
            else:
                out.append(item)
        return out

    def ident(self, block, where, pattern=SLUG, seen=None, kind="id"):
        value = block.get("id")
        at = self._at(where, "id")
        if "id" not in block:
            return None
        if not isinstance(value, str) or not pattern.match(value):
            rule = ("letters, digits and '_', not starting with a digit" if pattern is KNOB_ID
                    else "lower-case letters, digits, '.', '_' and '-', starting with a letter or digit")
            self.r.error("ID-INVALID", at, "%s %r must use %s" % (kind, value, rule))
            return None
        if seen is not None:
            if value in seen:
                self.r.error("ID-DUPLICATE", at, "%s `%s` is already used at %s" % (kind, value, seen[value]))
                return None
            seen[value] = at
        return value

    def local_path(self, value, at, key):
        """A path relative to the runbook folder that stays inside it (after ${...} is removed)."""
        plain = VAR.sub("x", value)
        if os.path.isabs(plain) or os.path.normpath(plain).split(os.sep)[0] == "..":
            self.r.error("PATH-ESCAPES", at, "`%s` must be a path inside the runbook folder, found %r"
                         % (key, value))
            return False
        return True

    def variables(self, value, at):
        """Every ${NAME} in value names a knob or a built-in variable."""
        texts = []
        if isinstance(value, str):
            texts = [value]
        elif isinstance(value, list):
            texts = [v for v in value if isinstance(v, str)]
        elif isinstance(value, dict):
            texts = [v for v in value.values() if isinstance(v, str)]
        for text in texts:
            for name in VAR.findall(text):
                if name not in self.knobs and name not in BUILTIN_VARS:
                    self.r.error("VAR-UNKNOWN", at, "${%s} names no knob and no built-in variable (%s)"
                                 % (name, ", ".join(BUILTIN_VARS)))

    # -- blocks
    def run(self):
        d = self.data
        self.keys(d, TOP_KEYS, "")
        if "runbook" in d and d["runbook"] != FORMAT:
            self.r.error("VERSION-UNSUPPORTED", "runbook", "`runbook: %r` is not a format this reader "
                         "knows; write `runbook: %d`" % (d["runbook"], FORMAT))
        self.ident(d, "", kind="runbook id")
        self.text(d, "title", "")
        self.text(d, "description", "")
        self.text(d, "owner", "")
        spec = self.text(d, "spec", "")
        if spec:
            self.local_path(spec, "spec", "spec")
        self.knob_list()
        self.stage_list()

    def knob_list(self):
        value = self.data.get("knobs", [])
        if not isinstance(value, list):
            self.r.error("FIELD-NOT-LIST", "knobs", "`knobs` must be a list of knob mappings")
            return
        seen = {}
        for n, knob in enumerate(value):
            where = "knobs[%d]" % n
            if not isinstance(knob, dict):
                self.r.error("FIELD-TYPE", where, "each knob must be a mapping with `id`, `description`, "
                             "`type` and `default`")
                continue
            self.keys(knob, KNOB_KEYS, where)
            ident = self.ident(knob, where, pattern=KNOB_ID, seen=seen, kind="knob id")
            self.text(knob, "description", where)
            self.flag(knob, "owner_only", where)
            kind = knob.get("type")
            if "type" in knob and kind not in KNOB_TYPES:
                self.r.error("KNOB-TYPE-UNKNOWN", where + ".type", "knob type %r is not one of %s"
                             % (kind, ", ".join(KNOB_TYPES)))
                continue
            self.knob_default(knob, where)
            if ident and kind is not None:
                # registered even when its default is wrong (already reported), so a reference to
                # it is not reported a second time as an unknown knob
                self.knobs[ident] = knob

    def knob_default(self, knob, where):
        kind, default = knob.get("type"), knob.get("default")
        if kind is None or "default" not in knob:
            return False
        ok = {"int": lambda v: isinstance(v, int) and not isinstance(v, bool),
              "float": _is_number, "str": lambda v: isinstance(v, str),
              "bool": lambda v: isinstance(v, bool), "enum": lambda v: True}[kind](default)
        good = True
        for key in ("min", "max"):
            if key in knob:
                if kind not in ("int", "float"):
                    self.r.error("FIELD-UNKNOWN", "%s.%s" % (where, key), "`%s` applies to int and float "
                                 "knobs only" % key)
                    good = False
                elif not _is_number(knob[key]):
                    self.r.error("FIELD-TYPE", "%s.%s" % (where, key), "`%s` must be a number" % key)
                    good = False
        if good and _is_number(knob.get("min")) and _is_number(knob.get("max")) and knob["min"] > knob["max"]:
            self.r.error("RANGE", where + ".min", "min %s is above max %s" % (knob["min"], knob["max"]))
            good = False
        if kind == "enum":
            values = knob.get("values")
            if "values" not in knob:
                self.r.error("FIELD-MISSING", where + ".values", "an enum knob lists its `values`")
                return False
            if not isinstance(values, list) or not values:
                self.r.error("FIELD-NOT-LIST", where + ".values", "`values` must be a non-empty list")
                return False
            if default not in values:
                self.r.error("KNOB-DEFAULT-RANGE", where + ".default", "default %r is not one of the "
                             "values %s" % (default, values))
                return False
        elif "values" in knob:
            self.r.error("FIELD-UNKNOWN", where + ".values", "`values` applies to enum knobs only")
            good = False
        if not ok:
            self.r.error("KNOB-DEFAULT-TYPE", where + ".default", "default %r is not a %s" % (default, kind))
            return False
        if good and kind in ("int", "float"):
            low, high = knob.get("min"), knob.get("max")
            if (low is not None and default < low) or (high is not None and default > high):
                self.r.error("KNOB-DEFAULT-RANGE", where + ".default", "default %s is outside %s..%s"
                             % (default, "" if low is None else low, "" if high is None else high))
                return False
        return good

    def stage_list(self):
        stages = self.data.get("stages")
        if "stages" not in self.data:
            return
        if not isinstance(stages, list):
            self.r.error("FIELD-NOT-LIST", "stages", "`stages` must be a list of stage mappings")
            return
        if not stages:
            self.r.error("FIELD-EMPTY", "stages", "a runbook needs at least one stage")
            return
        stage_ids, check_ids = {}, {}
        for n, stage in enumerate(stages):
            where = "stages[%d]" % n
            if not isinstance(stage, dict):
                self.r.error("FIELD-TYPE", where, "each stage must be a mapping with at least `id`")
                continue
            self.stage(stage, where, stage_ids, check_ids)

    def stage(self, stage, where, stage_ids, check_ids):
        self.keys(stage, STAGE_KEYS, where)
        earlier = dict(stage_ids)
        self.ident(stage, where, seen=stage_ids, kind="stage id")
        for key in ("title", "description"):
            self.text(stage, key, where)
        for n, need in enumerate(self.items(stage, "needs", where)):
            if need not in earlier:
                self.r.error("NEEDS-UNKNOWN", "%s.needs[%d]" % (where, n), "`%s` is not a stage declared "
                             "before this one; list stages in run order" % need)
        run = self.text(stage, "run", where)
        gate = stage.get("owner_gate")
        if "run" not in stage and gate is None:
            self.r.error("RUN-MISSING", where, "a stage runs a command (`run`) or is an owner gate "
                         "(`owner_gate`); this one has neither")
        if run:
            self.variables(run, where + ".run")
        workdir = self.text(stage, "workdir", where)
        if workdir and self.local_path(workdir, where + ".workdir", "workdir"):
            self.variables(workdir, where + ".workdir")
        self.whole(stage, "timeout", where, 1, 7 * 24 * 3600)
        env = stage.get("env")
        if env is not None:
            if not isinstance(env, dict) or not all(
                    isinstance(k, str) and isinstance(v, (str, int, float)) and not isinstance(v, bool)
                    for k, v in env.items()):
                self.r.error("FIELD-TYPE", where + ".env", "`env` maps variable names to text or numbers")
            else:
                self.variables(env, where + ".env")
        self.items(stage, "inputs", where)
        self.outputs(stage, where)
        self.fails(stage, where)
        own = self.checks(stage, where, check_ids)
        listed = stage.get("checks")
        if run and (listed is None or listed == []):
            self.r.error("CHECKS-EMPTY", where + ".checks", "a stage that runs a command needs at least "
                         "one check; an empty check list would pass anything")
        if gate is not None:
            self.owner_gate(gate, where + ".owner_gate")
        if "retry" in stage:
            self.retry(stage["retry"], where + ".retry", own)

    def outputs(self, stage, where):
        value = stage.get("outputs")
        if value is None:
            return
        if not isinstance(value, list):
            self.r.error("FIELD-NOT-LIST", where + ".outputs", "`outputs` must be a list")
            return
        for n, out in enumerate(value):
            at = "%s.outputs[%d]" % (where, n)
            if isinstance(out, str) and out.strip():
                continue
            if isinstance(out, dict):
                self.keys(out, OUTPUT_KEYS, at)
                self.text(out, "path", at)
                self.text(out, "what", at)
                continue
            self.r.error("FIELD-TYPE", at, "an output is a path, or a mapping with `path` and `what`")

    def fails(self, stage, where):
        value = stage.get("fails")
        if value is None:
            return
        if not isinstance(value, list):
            self.r.error("FIELD-NOT-LIST", where + ".fails", "`fails` must be a list")
            return
        seen = {}
        for n, fail in enumerate(value):
            at = "%s.fails[%d]" % (where, n)
            if not isinstance(fail, dict):
                self.r.error("FIELD-TYPE", at, "a known failure is a mapping with `id` and `when`")
                continue
            self.keys(fail, FAIL_KEYS, at)
            self.ident(fail, at, seen=seen, kind="failure id")
            self.text(fail, "when", at)

    def checks(self, stage, where, check_ids):
        value = stage.get("checks")
        if value is None:
            return []
        if not isinstance(value, list):
            self.r.error("FIELD-NOT-LIST", where + ".checks", "`checks` must be a list of check mappings")
            return []
        own = []
        for n, chk in enumerate(value):
            at = "%s.checks[%d]" % (where, n)
            if not isinstance(chk, dict):
                self.r.error("FIELD-TYPE", at, "each check must be a mapping with `id` and `type`")
                continue
            ident = self.ident(chk, at, seen=check_ids, kind="check id")
            if ident:
                own.append(ident)
            elif isinstance(chk.get("id"), str):
                own.append(chk["id"])
            self.check(chk, at)
        return own

    def check(self, chk, at):
        kind = chk.get("type")
        if "type" in chk and kind not in CHECK_TYPES:
            self.r.error("CHECK-TYPE-UNKNOWN", at + ".type", "check type %r is not one of %s"
                         % (kind, ", ".join(CHECK_TYPES)))
            self.keys({k: v for k, v in chk.items() if k in CHECK_KEYS}, CHECK_KEYS, at)
            return
        allowed = dict(CHECK_KEYS)
        allowed.update(TYPE_KEYS.get(kind, {}))
        self.keys(chk, allowed, at)
        self.text(chk, "description", at)
        self.items(chk, "covers", at)
        if kind == "exit-code":
            self.whole(chk, "expect", at, 0, 255)
        elif kind in ("file-exists", "regex-in-file", "json-field"):
            path = self.text(chk, "path", at)
            if path and self.local_path(path, at + ".path", "path"):
                self.variables(path, at + ".path")
            self.flag(chk, "non_empty", at)
            self.flag(chk, "absent", at)
            self.flag(chk, "ignore_case", at)
        if kind == "regex-in-file":
            pattern = self.text(chk, "pattern", at)
            if pattern is not None:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    self.r.error("REGEX-INVALID", at + ".pattern", "pattern %r does not compile: %s"
                                 % (pattern, exc))
        elif kind == "json-field":
            self.text(chk, "field", at)
            op = chk.get("op")
            if "op" in chk and op not in OPS:
                self.r.error("OP-UNKNOWN", at + ".op", "op %r is not one of %s" % (op, " ".join(OPS)))
            if "value" in chk:
                self.threshold(chk["value"], op, at + ".value")
        elif kind == "plugin":
            self.plugin(chk, at)

    def threshold(self, value, op, at):
        ref = VAR.fullmatch(value) if isinstance(value, str) else None
        if ref:
            self.variables(value, at)
            knob = self.knobs.get(ref.group(1))
            numeric = knob is not None and knob.get("type") in ("int", "float")
            if op in ("<", "<=", ">", ">=") and knob is not None and not numeric:
                self.r.error("VALUE-NOT-NUMBER", at, "op %s compares numbers, and knob %s is a %s knob"
                             % (op, ref.group(1), knob.get("type")))
            return
        if isinstance(value, (dict, list)) or value is None:
            self.r.error("FIELD-TYPE", at, "`value` is a number, text, true/false or a ${KNOB} reference")
        elif op in ("<", "<=", ">", ">=") and not _is_number(value):
            self.r.error("VALUE-NOT-NUMBER", at, "op %s compares numbers, and %r is not a number" % (op, value))

    def plugin(self, chk, at):
        script = self.text(chk, "script", at)
        args = self.items(chk, "args", at, kind=object)
        for n, arg in enumerate(args):
            if not isinstance(arg, (str, int, float)) or isinstance(arg, bool):
                self.r.error("FIELD-TYPE", "%s.args[%d]" % (at, n), "each argument is text or a number")
        self.variables([a for a in args if isinstance(a, str)], at + ".args")
        self.whole(chk, "timeout", at, 1, 24 * 3600)
        if not script or not self.local_path(script, at + ".script", "script"):
            return
        if self.check_files:
            full = os.path.join(self.base, script)
            if not os.path.isfile(full):
                self.r.error("PLUGIN-MISSING", at + ".script", "plugin script %s does not exist (paths are "
                             "relative to the runbook folder)" % script)
            elif not os.access(full, os.X_OK):
                self.r.error("PLUGIN-NOT-EXECUTABLE", at + ".script", "plugin script %s is not executable; "
                             "run chmod +x %s" % (script, script))

    def owner_gate(self, gate, at):
        if not isinstance(gate, dict):
            self.r.error("FIELD-TYPE", at, "`owner_gate` is a mapping with `approve`")
            return
        self.keys(gate, GATE_KEYS, at)
        self.text(gate, "approve", at)
        self.items(gate, "evidence", at)
        self.items(gate, "covers", at)

    def retry(self, retry, at, own):
        if not isinstance(retry, dict):
            self.r.error("FIELD-TYPE", at, "`retry` is a mapping with `max_attempts`")
            return
        self.keys(retry, RETRY_KEYS, at)
        self.whole(retry, "max_attempts", at, 1, MAX_ATTEMPTS)
        lists = {}
        for key in ("on_fail", "stop_on"):
            lists[key] = self.items(retry, key, at)
            for n, ident in enumerate(lists[key]):
                if ident not in own:
                    self.r.error("RETRY-CHECK-UNKNOWN", "%s.%s[%d]" % (at, key, n), "`%s` is not a check "
                                 "of this stage (%s)" % (ident, ", ".join(own) or "none"))
        for n, ident in enumerate(lists["stop_on"]):
            if ident in lists["on_fail"]:
                self.r.error("RETRY-OVERLAP", "%s.stop_on[%d]" % (at, n), "`%s` is in both `on_fail` and "
                             "`stop_on`; a failure either retries or stops" % ident)
        move = retry.get("move")
        if move is None:
            return
        where = at + ".move"
        if not isinstance(move, dict):
            self.r.error("FIELD-TYPE", where, "`move` is a mapping with `knob` and `by`")
            return
        self.keys(move, MOVE_KEYS, where)
        name = move.get("knob")
        knob = self.knobs.get(name) if isinstance(name, str) else None
        if "knob" in move and knob is None:
            self.r.error("KNOB-UNKNOWN", where + ".knob", "`%s` is not a declared knob" % name)
        elif knob is not None:
            if knob.get("owner_only"):
                self.r.error("KNOB-OWNER-ONLY", where + ".knob", "knob %s is owner_only: only the owner "
                             "changes it, so a retry may not move it" % name)
            if knob.get("type") not in ("int", "float"):
                self.r.error("KNOB-NOT-NUMBER", where + ".knob", "a retry moves a number knob by a step; "
                             "%s is a %s knob" % (name, knob.get("type")))
        if "by" in move:
            by = move["by"]
            if not _is_number(by):
                self.r.error("FIELD-TYPE", where + ".by", "`by` must be a number")
            elif by == 0:
                self.r.error("RANGE", where + ".by", "`by` is 0, so the retry would change nothing")
            elif knob is not None and knob.get("type") == "int" and not isinstance(by, int):
                self.r.error("MOVE-NOT-INT", where + ".by", "knob %s is an int knob; `by` must be a whole "
                             "number" % name)


def validate(data, base_dir, check_files=True, report=None):
    """(errors, warnings) for a loaded runbook mapping. `base_dir` is the runbook's folder, the
    base of every relative path. check_files=False skips the plugin file look-ups."""
    report = report or _Report()
    _Checker(data, base_dir, check_files, report).run()
    return report.errors, report.warnings


# ------------------------------------------------------------------------------ the spec
def _fence_mask(lines):
    """True for every line inside a fenced code block (``` or ~~~) or an HTML comment."""
    mask, fence, comment = [], None, False
    for line in lines:
        s = line.strip()
        if comment:
            mask.append(True)
            if "-->" in s:
                comment = False
            continue
        if fence:
            mask.append(True)
            if s.startswith(fence):
                fence = None
            continue
        m = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if m:
            fence = m.group(1)
            mask.append(True)
            continue
        if s.startswith("<!--") and "-->" not in s[4:]:
            comment = True
            mask.append(True)
            continue
        mask.append(False)
    return mask


def _norm(text):
    text = re.sub(r"\s+", " ", text.strip()).casefold()
    return re.sub(r"\s*/\s*", "/", text)


_REQUIREMENT = re.compile(r"^\s{0,3}###\s+Requirement:\s*(.+?)\s*$", re.I)
_SCENARIO = re.compile(r"^\s{0,3}####\s+(.+?)\s*$")
_SECTION = re.compile(r"^\s{0,3}##\s+(.+?)\s*$")
_KIT_ITEM = re.compile(r"^\s*[-*]\s+\*\*((?:SC|FR)-\d+)\*\*\s*:?\s*(.*)$")


def _openspec_items(lines, capability=None):
    items, requirement, section = [], None, ""
    mask = _fence_mask(lines)
    for line, masked in zip(lines, mask):
        if masked:
            continue
        m = _SECTION.match(line)
        if m and not line.lstrip().startswith("###"):
            section, requirement = m.group(1).strip().upper(), None
            continue
        m = _REQUIREMENT.match(line)
        if m:
            requirement = m.group(1).strip()
            continue
        if line.lstrip().startswith("### "):
            requirement = None
            continue
        m = _SCENARIO.match(line)
        if m and requirement:
            name = re.sub(r"[ \t]+#+[ \t]*$", "", m.group(1))
            name = re.sub(r"^Scenario:\s*", "", name, flags=re.I).strip()
            bare = "%s/%s" % (requirement, name)
            items.append({"id": "%s/%s" % (capability, bare) if capability else bare, "bare": bare,
                          "capability": capability, "kind": "scenario", "text": name,
                          "required": not section.startswith("REMOVED")})
    return items


def _speckit_items(lines):
    items, clarify = [], 0
    for line, masked in zip(lines, _fence_mask(lines)):
        if masked:
            continue
        clarify += line.count("[NEEDS CLARIFICATION")
        m = _KIT_ITEM.match(line)
        if m:
            ident = m.group(1).upper()
            items.append({"id": ident, "kind": ident[:2], "text": m.group(2).strip(),
                          "required": ident.startswith("SC-")})
    items.sort(key=lambda i: (not i["required"], i["id"]))
    return items, clarify


def _detect(lines):
    mask = _fence_mask(lines)
    if any(_REQUIREMENT.match(l) for l, m in zip(lines, mask) if not m):
        return "openspec"
    return "spec-kit"


def _read_lines(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n").replace("\r", "\n").split("\n")


def parse_spec(path):
    """The items a runbook must cover, read from a spec-kit spec.md, an OpenSpec spec or delta
    spec, or an OpenSpec folder of <capability>/spec.md files.

    Returns {"path", "format", "items": [{"id", "kind", "text", "required"}], "clarifications"}.
    spec-kit items are SC-nnn (required) and FR-nnn (not required); OpenSpec items are
    "<requirement>/<scenario>" (under a folder, "<capability>/<requirement>/<scenario>"), required
    unless they sit under a `## REMOVED Requirements` section. Raises OSError when unreadable and
    ValueError for a folder that mixes formats."""
    if os.path.isdir(path):
        files = []
        for base, dirs, names in os.walk(path):
            dirs.sort()
            if "spec.md" in names:
                files.append(os.path.join(base, "spec.md"))
        items = []
        for full in sorted(files):
            lines = _read_lines(full)
            if _detect(lines) != "openspec":
                raise ValueError("%s is not an OpenSpec spec; a folder is read only as OpenSpec "
                                 "capabilities; pass a spec-kit spec.md as a file" % full)
            cap = os.path.relpath(os.path.dirname(full), path).replace(os.sep, "/")
            items += _openspec_items(lines, None if cap == "." else cap)
        return {"path": path, "format": "openspec", "items": items, "clarifications": 0}
    lines = _read_lines(path)
    fmt = _detect(lines)
    if fmt == "openspec":
        return {"path": path, "format": fmt, "items": _openspec_items(lines), "clarifications": 0}
    items, clarify = _speckit_items(lines)
    return {"path": path, "format": fmt, "items": items, "clarifications": clarify}


# ----------------------------------------------------------------------------- coverage
def _covering(data):
    """[(covers entry, who, where)] for every check and owner gate. who is the check id, or
    "gate:<stage id>" for an owner gate."""
    out = []
    stages = data.get("stages") if isinstance(data.get("stages"), list) else []
    for n, stage in enumerate(stages):
        if not isinstance(stage, dict):
            continue
        checks = stage.get("checks") if isinstance(stage.get("checks"), list) else []
        for c, chk in enumerate(checks):
            if isinstance(chk, dict) and isinstance(chk.get("covers"), list):
                for k, entry in enumerate(chk["covers"]):
                    if isinstance(entry, str) and entry.strip():
                        out.append((entry, str(chk.get("id")), "stages[%d].checks[%d].covers[%d]" % (n, c, k)))
        gate = stage.get("owner_gate")
        if isinstance(gate, dict) and isinstance(gate.get("covers"), list):
            for k, entry in enumerate(gate["covers"]):
                if isinstance(entry, str) and entry.strip():
                    out.append((entry, "gate:%s" % stage.get("id"), "stages[%d].owner_gate.covers[%d]" % (n, k)))
    return out


def coverage(data, spec, report):
    """Map every covers entry to a spec item and report what stays uncovered.

    Returns {"required": count, "covered": {required item: [who...]}, "optional": {other covered
    item (FR-nnn, a removed scenario): [who...]}, "missing": [required item...]}."""
    index = {}
    for item in spec["items"]:
        keys = {_norm(item["id"])}
        if item.get("capability"):
            # a folder spec: "<cap>/<req>/<scenario>" also answers to "<req>/<scenario>" when unique
            keys.add(_norm(item["bare"]))
        for key in keys:
            index.setdefault(key, []).append(item)
    covered = {}
    for entry, who, where in _covering(data):
        found = index.get(_norm(entry), [])
        if not found:
            near = difflib.get_close_matches(_norm(entry), list(index), n=1, cutoff=0.6)
            hint = "; closest is %r" % next(i["id"] for i in index[near[0]]) if near else ""
            report.error("COVERS-UNKNOWN", where, "covers %r names nothing in %s%s" % (entry, spec["path"], hint))
            continue
        if len({i["id"] for i in found}) > 1:
            report.error("COVERS-AMBIGUOUS", where, "covers %r matches %s; write the capability prefix"
                         % (entry, ", ".join(sorted(i["id"] for i in found))))
            continue
        ident = found[0]["id"]
        if who not in covered.setdefault(ident, []):
            covered[ident].append(who)
    required = [i for i in spec["items"] if i["required"]]
    if not required:
        report.error("SPEC-EMPTY", spec["path"], "the spec has no success criterion (SC-nnn) and no "
                     "OpenSpec scenario to cover; an empty list is never a pass")
    missing = []
    for item in required:
        if item["id"] not in covered:
            missing.append(item["id"])
            report.error("SPEC-UNCOVERED", item["id"], "no runbook check covers %s (%s); add it to the "
                         "`covers` list of the check that shows it, or to an owner gate when only a person "
                         "can judge it" % (item["id"], item["text"][:120]))
    for item in spec["items"]:
        if not item["required"] and item["kind"] == "FR" and item["id"] not in covered:
            report.warn("FR-UNCOVERED", item["id"], "no check covers %s (a warning: only success criteria "
                        "must be covered)" % item["id"])
    if spec.get("clarifications"):
        report.warn("SPEC-CLARIFY", spec["path"], "the spec still has %d [NEEDS CLARIFICATION] marker(s)"
                    % spec["clarifications"])
    need = {i["id"] for i in required}
    return {"required": len(required),
            "covered": {k: v for k, v in covered.items() if k in need},
            "optional": {k: v for k, v in covered.items() if k not in need},
            "missing": missing}


# ----------------------------------------------------------------------------- the check
def check(path, spec_path=None, check_files=True):
    """Check one runbook file, and its coverage of a spec when one is given (`spec_path`, else the
    runbook's own `spec:` field, relative to the runbook folder).

    Returns {"verdict", "code", "runbook", "spec", "errors", "warnings", "coverage"}: PASS with no
    error, FAIL for a malformed runbook or an uncovered item, BLOCKED when the file cannot be read."""
    result = {"runbook": path, "spec": None, "coverage": None}
    data, report = load(path)
    if data is None:
        code = verdict.BLOCKED if any(e["code"] == "FILE-UNREADABLE" for e in report.errors) else verdict.FAIL
        result.update(verdict=verdict.name_of(code), code=code, errors=report.errors, warnings=report.warnings)
        return result
    base = os.path.dirname(os.path.abspath(path))
    validate(data, base, check_files=check_files, report=report)
    target = spec_path
    if target is None and isinstance(data.get("spec"), str) and data["spec"].strip():
        target = os.path.join(base, data["spec"])
    if target is not None:
        try:
            spec = parse_spec(target)
        except (OSError, UnicodeDecodeError) as exc:
            report.error("SPEC-MISSING", "spec", "cannot read the spec %s: %s" % (target, exc))
        except ValueError as exc:
            report.error("SPEC-MIXED", "spec", str(exc))
        else:
            result["spec"] = {"path": target, "format": spec["format"],
                              "items": len(spec["items"]),
                              "required": sum(1 for i in spec["items"] if i["required"])}
            result["coverage"] = coverage(data, spec, report)
    elif _covering(data):
        report.warn("COVERS-WITHOUT-SPEC", "spec", "checks name `covers` entries but no spec was given; "
                    "pass --spec or set `spec:` to check them")
    code = verdict.FAIL if report.errors else verdict.PASS
    result.update(verdict=verdict.name_of(code), code=code, errors=report.errors, warnings=report.warnings)
    return result


# ------------------------------------------------------------------------ check meaning
def knob_values(data):
    """{knob id: default} for every knob of a loaded runbook."""
    knobs = data.get("knobs") if isinstance(data.get("knobs"), list) else []
    return {k["id"]: k.get("default") for k in knobs if isinstance(k, dict) and isinstance(k.get("id"), str)}


def substitute(text, values):
    """Replace ${NAME} with values[NAME]; an unknown name is left as written."""
    def one(m):
        value = values.get(m.group(1))
        return m.group(0) if value is None else str(value)
    return VAR.sub(one, text)


def _field(doc, dotted):
    cur = doc
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and re.fullmatch(r"\d+", part) and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise KeyError(part)
    return cur


def _compare(left, op, right):
    if op in ("==", "!="):
        same = left == right if not (_is_number(left) and _is_number(right)) else float(left) == float(right)
        return same if op == "==" else not same
    return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]


def evaluate(chk, *, workdir, runbook_dir=None, exit_code=None, knobs=None, stage="", attempt=1,
             evidence_dir=None):
    """(verdict code, reason) for one check after its stage ran.

    workdir is the stage's working folder (relative check paths start there); runbook_dir is the
    runbook's folder (a plugin script path starts there; default workdir); exit_code is the stage
    command's exit status, None when it did not run; knobs holds the knob values of this attempt."""
    runbook_dir = os.path.abspath(runbook_dir or workdir)
    values = dict(knobs or {})
    values.update(RUNBOOK_DIR=runbook_dir, STAGE=stage, ATTEMPT=attempt, EVIDENCE_DIR=evidence_dir or "")
    kind = chk.get("type")
    if kind == "exit-code":
        want = chk.get("expect", 0)
        if exit_code is None:
            return verdict.BLOCKED, "the stage command did not run"
        if exit_code == want:
            return verdict.PASS, "exit %d" % exit_code
        return verdict.FAIL, "exit %d, expected %d" % (exit_code, want)
    if kind in ("file-exists", "regex-in-file", "json-field"):
        rel = substitute(chk["path"], values)
        path = os.path.join(workdir, rel)
    if kind == "file-exists":
        if not os.path.isfile(path):
            return verdict.FAIL, "%s does not exist" % rel
        if chk.get("non_empty", True) and os.path.getsize(path) == 0:
            return verdict.FAIL, "%s is empty" % rel
        return verdict.PASS, "%s exists" % rel
    if kind == "regex-in-file":
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            return verdict.FAIL, "cannot read %s: %s" % (rel, exc.strerror or exc)
        found = re.search(chk["pattern"], text, re.I | re.M if chk.get("ignore_case") else re.M)
        if chk.get("absent"):
            return ((verdict.FAIL, "%s matches %r at %r" % (rel, chk["pattern"], found.group(0)[:80]))
                    if found else (verdict.PASS, "%s has no match for %r" % (rel, chk["pattern"])))
        return ((verdict.PASS, "%s matches %r" % (rel, chk["pattern"])) if found
                else (verdict.FAIL, "%s has no match for %r" % (rel, chk["pattern"])))
    if kind == "json-field":
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            return verdict.FAIL, "cannot read %s as JSON: %s" % (rel, exc)
        try:
            got = _field(doc, chk["field"])
        except KeyError:
            return verdict.FAIL, "%s has no field %s" % (rel, chk["field"])
        want, op = chk["value"], chk["op"]
        if isinstance(want, str) and VAR.fullmatch(want):
            want = values.get(VAR.fullmatch(want).group(1), want)
        if op in ("<", "<=", ">", ">="):
            if not _is_number(got):
                return verdict.FAIL, "%s %s is %r, not a finite number" % (rel, chk["field"], got)
            if not _is_number(want):
                return verdict.FAIL, "threshold %r is not a number" % (want,)
        elif isinstance(got, float) and not math.isfinite(got):
            return verdict.FAIL, "%s %s is not finite" % (rel, chk["field"])
        ok = _compare(got, op, want)
        return (verdict.PASS if ok else verdict.FAIL), "%s = %r, needs %s %r" % (chk["field"], got, op, want)
    if kind == "plugin":
        return _run_plugin(chk, workdir, runbook_dir, values, stage, attempt, evidence_dir, knobs or {})
    return verdict.BLOCKED, "unknown check type %r" % (kind,)


def _run_plugin(chk, workdir, runbook_dir, values, stage, attempt, evidence_dir, knobs):
    script = os.path.join(runbook_dir, chk["script"])
    args = [substitute(str(a), values) for a in chk.get("args", [])]
    env = dict(os.environ)
    env.update(ALPACA_RUNBOOK_DIR=runbook_dir, ALPACA_STAGE=str(stage), ALPACA_CHECK=str(chk.get("id")),
               ALPACA_ATTEMPT=str(attempt), ALPACA_EVIDENCE_DIR=evidence_dir or "")
    for name, value in knobs.items():
        env["ALPACA_KNOB_%s" % name] = str(value)
    timeout = chk.get("timeout", PLUGIN_TIMEOUT)
    try:
        proc = subprocess.run([script] + args, cwd=workdir, env=env, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return verdict.BLOCKED, "plugin %s timed out after %ss" % (chk["script"], timeout)
    except OSError as exc:
        return verdict.BLOCKED, "plugin %s did not start: %s" % (chk["script"], exc)
    lines = [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]
    reason = lines[-1][:300] if lines else "no reason printed"
    if proc.returncode in verdict.VERDICT_BAND:
        return proc.returncode, reason
    return verdict.BLOCKED, "plugin exit %d is outside the verdict band: %s" % (proc.returncode, reason)


def next_attempt(data, stage, results, attempt, knobs):
    """The retry rule of one stage, applied after attempt number `attempt` (1 for the first run).

    results maps each check id of the stage to its verdict code. Returns {"retry": bool, "knobs":
    the knob values for the next attempt, "reason": why}. The stage stops when every check
    passed, when a check is BLOCKED or PAUSED (a rerun does not supply a missing input or a
    decision), when a `stop_on` check failed, when a check outside `on_fail` failed, when the attempts
    are used, or when the move would take the knob outside its range."""
    knobs = dict(knobs)
    if all(code == verdict.PASS for code in results.values()):
        return {"retry": False, "knobs": knobs, "reason": "every check passed"}
    for ident, code in results.items():
        if code in (verdict.BLOCKED, verdict.PAUSED):
            return {"retry": False, "knobs": knobs, "reason": "check %s is %s; a rerun does not fix that"
                    % (ident, verdict.name_of(code))}
    retry = stage.get("retry") or {}
    failed = [ident for ident, code in results.items() if code == verdict.FAIL]
    for ident in failed:
        if ident in (retry.get("stop_on") or []):
            return {"retry": False, "knobs": knobs, "reason": "check %s failed and is listed in stop_on" % ident}
    allowed = retry.get("on_fail") or [c.get("id") for c in stage.get("checks") or []]
    for ident in failed:
        if ident not in allowed:
            return {"retry": False, "knobs": knobs, "reason": "check %s failed and retry.on_fail does not list it"
                    % ident}
    limit = retry.get("max_attempts", 1)
    if attempt >= limit:
        return {"retry": False, "knobs": knobs, "reason": "attempt %d of %d used" % (attempt, limit)}
    move = retry.get("move")
    if move:
        name, by = move["knob"], move["by"]
        spec = next((k for k in data.get("knobs") or [] if k.get("id") == name), {})
        value = knobs.get(name, spec.get("default")) + by
        if isinstance(value, float):
            value = round(value, 9)
        low, high = spec.get("min"), spec.get("max")
        if (low is not None and value < low) or (high is not None and value > high):
            return {"retry": False, "knobs": knobs, "reason": "%s would move to %s, outside its range %s..%s"
                    % (name, value, "" if low is None else low, "" if high is None else high)}
        knobs[name] = value
        return {"retry": True, "knobs": knobs, "reason": "attempt %d of %d with %s=%s"
                % (attempt + 1, limit, name, value)}
    return {"retry": True, "knobs": knobs, "reason": "attempt %d of %d, inputs unchanged" % (attempt + 1, limit)}


# ------------------------------------------------------------------------------- the verb
def _print(result):
    print("runbook: %s" % result["runbook"])
    spec = result.get("spec")
    if spec:
        cov = result.get("coverage") or {}
        print("spec: %s (%s, %d item(s), %d required)" % (spec["path"], spec["format"], spec["items"],
                                                        spec["required"]))
        print("coverage: %d of %d required item(s) covered" % (
            cov.get("required", 0) - len(cov.get("missing", [])), cov.get("required", 0)))
    for e in result["errors"]:
        print("ERROR %s %s: %s" % (e["code"], e["where"] or "-", e["message"]))
    for w in result["warnings"]:
        print("WARN %s %s: %s" % (w["code"], w["where"] or "-", w["message"]))
    print(verdict.gate_line("alpaca-runbook-check", result["code"]))


def cmd_runbook(args):
    if getattr(args, "runbook_verb", None) != "check" or not getattr(args, "file", None):
        print("usage: alpaca runbook check <file> [--spec <path>] [--json]", file=sys.stderr)
        return verdict.USAGE
    result = check(args.file, spec_path=args.spec, check_files=not args.no_files)
    if args.json:
        print(json.dumps({k: result[k] for k in ("verdict", "runbook", "spec", "errors", "warnings",
                                                  "coverage")}, indent=2, sort_keys=True))
    else:
        _print(result)
    return result["code"]


def _parser(sub):
    p = sub.add_parser("runbook", help="check a runbook file against the format and its spec "
                                       "(docs/runbook-format.md); read-only, writes no record")
    v = p.add_subparsers(dest="runbook_verb")
    c = v.add_parser("check", help="refuse a malformed runbook; with a spec, fail on any uncovered "
                                   "success criterion or scenario")
    c.add_argument("file", help="the runbook.yaml to check")
    c.add_argument("--spec", default=None, help="a spec-kit spec.md, an OpenSpec spec.md, or an OpenSpec "
                                                "folder of <capability>/spec.md (default: the runbook's "
                                                "`spec:` field)")
    c.add_argument("--no-files", action="store_true", help="skip the plugin script look-ups")
    c.add_argument("--json", action="store_true")


def _register():
    from alpaca import cli
    cli.command("runbook")(cmd_runbook)
    cli.register_parser("runbook", _parser)


_register()
