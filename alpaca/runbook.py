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

`evaluate` gives each check type its meaning, `next_attempt` gives the retry rule its meaning and
`respond` the known failures of format 2, so a runner and intake share one reading of the file.
`bar_parts` renders what each check, fail case and owner gate asks, for intake's rows. `check`
reads files but writes nothing, and it never touches the record.

Format 2 (`runbook: 2`) adds numbered edge cases (EC-nnn) as spec items, known failures with a
detection and a response (`detect`, `then`, recovery stages) and provenance (`source`). A
`runbook: 1` file reads and checks as it always did.
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

#: the newest format this reader knows, and every format it reads
FORMAT = 2
FORMATS = (1, 2)
CHECK_TYPES = ("exit-code", "file-exists", "regex-in-file", "json-field", "plugin")
KNOB_TYPES = ("int", "float", "str", "bool", "enum")
OPS = ("==", "!=", "<", "<=", ">", ">=")
BUILTIN_VARS = ("RUNBOOK_DIR", "STAGE", "ATTEMPT", "EVIDENCE_DIR")
MAX_ATTEMPTS = 20
PLUGIN_TIMEOUT = 60
#: the allowed (lowest, highest) of a number field; the kit schema reads these too
EXPECT_RANGE = (0, 255)
PLUGIN_TIMEOUT_RANGE = (1, 24 * 3600)
STAGE_TIMEOUT_RANGE = (1, 7 * 24 * 3600)

# Every field, per block, and whether it is required. docs/runbook-format.md names each one and
# test_runbook.py fails when the two drift apart.
TOP_KEYS = {"runbook": True, "id": True, "title": True, "description": False, "spec": False,
            "owner": False, "knobs": False, "stages": True}
KNOB_KEYS = {"id": True, "description": True, "type": True, "default": True, "min": False,
             "max": False, "values": False, "owner_only": False, "source": False}
STAGE_KEYS = {"id": True, "title": False, "description": False, "needs": False, "run": False,
              "workdir": False, "timeout": False, "env": False, "inputs": False, "outputs": False,
              "checks": False, "retry": False, "fails": False, "owner_gate": False, "recovery": False}
CHECK_KEYS = {"id": True, "type": True, "description": False, "covers": False, "source": False}
#: a fail case's `detect` is a check without `id`, `covers` and `source`
DETECT_KEYS = {"type": True, "description": False}
TYPE_KEYS = {
    "exit-code": {"expect": False},
    "file-exists": {"path": True, "non_empty": False},
    "regex-in-file": {"path": True, "pattern": True, "absent": False, "ignore_case": False},
    "json-field": {"path": True, "field": True, "op": True, "value": True},
    "plugin": {"script": True, "args": False, "timeout": False},
}
RETRY_KEYS = {"max_attempts": True, "on_fail": False, "stop_on": False, "move": False}
MOVE_KEYS = {"knob": True, "by": True}
GATE_KEYS = {"approve": True, "evidence": False, "covers": False, "source": False}
OUTPUT_KEYS = {"path": True, "what": False}
FAIL_KEYS = {"id": True, "when": True, "detect": False, "then": False, "covers": False, "source": False}
#: the fields format 2 added, per table; in a `runbook: 1` file each is FIELD-UNKNOWN, as it was
SINCE_2 = {"knob": ("source",), "stage": ("recovery",), "check": ("source",), "gate": ("source",),
           "fail": ("detect", "then", "covers", "source")}
#: the answers a fail case gives besides `{run: <recovery stage id>}`
THEN_WORDS = ("retry", "stop", "ask-owner")

ERROR_CODES = (
    "FILE-UNREADABLE", "YAML-SYNTAX", "NOT-MAPPING", "KEY-DUPLICATE", "FIELD-MISSING",
    "FIELD-UNKNOWN", "FIELD-EMPTY", "FIELD-TYPE", "FIELD-NOT-LIST", "VERSION-UNSUPPORTED",
    "ID-INVALID", "ID-DUPLICATE", "NEEDS-UNKNOWN", "RUN-MISSING", "CHECKS-EMPTY",
    "CHECKS-WITHOUT-RUN", "CHECK-TYPE-UNKNOWN", "REGEX-INVALID", "OP-UNKNOWN", "VALUE-NOT-NUMBER", "RANGE",
    "PATH-ESCAPES", "PLUGIN-MISSING", "PLUGIN-NOT-EXECUTABLE", "PLUGIN-SCRIPT-VARIABLE",
    "KNOB-TYPE-UNKNOWN",
    "KNOB-DEFAULT-TYPE", "KNOB-DEFAULT-RANGE", "KNOB-UNKNOWN", "KNOB-OWNER-ONLY",
    "KNOB-NOT-NUMBER", "MOVE-NOT-INT", "RETRY-CHECK-UNKNOWN", "RETRY-OVERLAP", "VAR-UNKNOWN",
    "SPEC-MISSING", "SPEC-EMPTY", "SPEC-MIXED", "SPEC-UNCOVERED", "COVERS-UNKNOWN",
    "COVERS-AMBIGUOUS", "SPEC-DELTA",
    # format 2
    "EC-UNNUMBERED", "THEN-MISSING", "THEN-UNKNOWN", "RECOVERY-UNKNOWN", "RECOVERY-NOT-MARKED",
    "RECOVERY-NEEDED", "RECOVERY-SELF", "DETECT-INVALID",
)
WARNING_CODES = ("FR-UNCOVERED", "SPEC-CLARIFY", "COVERS-WITHOUT-SPEC", "SPEC-UNPARSED",
                 "EC-IGNORED", "SOURCE-SHAPE", "SOURCE-MISSING")

#: words people reach for, mapped to the field that means it (used only for the "did you mean" hint)
ALIASES = {"command": "run", "cmd": "run", "depends_on": "needs", "depends": "needs",
           "after": "needs", "retries": "retry", "attempts": "max_attempts", "tries": "max_attempts",
           "gate": "owner_gate", "approval": "owner_gate", "threshold": "value", "file": "path",
           "regex": "pattern", "key": "field", "expected": "expect", "exit": "expect",
           "cwd": "workdir", "name": "title"}

SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
#: where `alpaca note add` keeps the raw notes, relative to the project root
NOTES_DIR = "input/notes"
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


def runbook_format(data):
    """The format a loaded runbook is read as: its `runbook:` value when this reader knows it,
    else the newest (such a file is refused with VERSION-UNSUPPORTED anyway)."""
    value = data.get("runbook")
    return int(value) if value in FORMATS else FORMAT


def source_shape(text):
    """True when `text` has one of the shapes of a `source` field: spec:<item id>,
    note:input/notes/<file>, interview:<slot id>, owner:<decision ref> or default."""
    if text == "default":
        return True
    kind, sep, rest = text.partition(":")
    if not sep or not rest.strip() or rest != rest.strip():
        return False
    if kind == "note":
        parts = rest.split("/")
        return (rest.startswith(NOTES_DIR + "/") and len(parts) > 2
                and all(part not in ("", ".", "..") for part in parts))
    if kind == "interview":
        return bool(SLUG.match(rest))
    return kind in ("spec", "owner")


class _Checker:
    def __init__(self, data, base_dir, check_files, report):
        self.data, self.base, self.check_files, self.r = data, base_dir, check_files, report
        self.knobs = {}
        self.fmt = runbook_format(data)
        # every stage id, and the ids of the recovery stages (format 2), known before any stage
        # is read, since a fail case may send to a stage declared after it
        self.stage_ids, self.recovery = set(), set()

    def table(self, table, name):
        """The field table as this file's format has it: format 1 has none of the SINCE_2 fields."""
        if self.fmt >= 2:
            return table
        return {k: v for k, v in table.items() if k not in SINCE_2[name]}

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
        if "runbook" in d and d["runbook"] not in FORMATS:
            self.r.error("VERSION-UNSUPPORTED", "runbook", "`runbook: %r` is not a format this reader "
                         "knows (%s); write `runbook: %d`" % (d["runbook"], ", ".join(map(str, FORMATS)),
                                                            FORMAT))
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
            self.keys(knob, self.table(KNOB_KEYS, "knob"), where)
            ident = self.ident(knob, where, pattern=KNOB_ID, seen=seen, kind="knob id")
            self.text(knob, "description", where)
            self.flag(knob, "owner_only", where)
            if self.fmt >= 2:
                self.source(knob, where)
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
        for stage in stages:
            if isinstance(stage, dict) and isinstance(stage.get("id"), str):
                self.stage_ids.add(stage["id"])
                if self.fmt >= 2 and stage.get("recovery") is True:
                    self.recovery.add(stage["id"])
        for n, stage in enumerate(stages):
            where = "stages[%d]" % n
            if not isinstance(stage, dict):
                self.r.error("FIELD-TYPE", where, "each stage must be a mapping with at least `id`")
                continue
            self.stage(stage, where, stage_ids, check_ids)

    def stage(self, stage, where, stage_ids, check_ids):
        self.keys(stage, self.table(STAGE_KEYS, "stage"), where)
        earlier = dict(stage_ids)
        self.ident(stage, where, seen=stage_ids, kind="stage id")
        for key in ("title", "description"):
            self.text(stage, key, where)
        recovery = False
        if self.fmt >= 2:
            self.flag(stage, "recovery", where)
            recovery = stage.get("recovery") is True
        needs = self.items(stage, "needs", where)
        for n, need in enumerate(needs):
            if need in self.recovery:
                self.r.error("RECOVERY-NEEDED", "%s.needs[%d]" % (where, n), "`%s` is a recovery stage: it "
                             "runs only when a fail case sends to it, so no stage waits for it" % need)
            elif need not in earlier:
                self.r.error("NEEDS-UNKNOWN", "%s.needs[%d]" % (where, n), "`%s` is not a stage declared "
                             "before this one; list stages in run order" % need)
        if recovery and needs:
            self.r.error("RECOVERY-NEEDED", where + ".needs", "a recovery stage runs only when a fail case "
                         "sends to it, outside the run order, so it has no `needs`")
        run = self.text(stage, "run", where)
        gate = stage.get("owner_gate")
        if recovery and gate is not None:
            self.r.error("FIELD-UNKNOWN", where + ".owner_gate", "a recovery stage is never an owner gate: "
                         "it runs only when a fail case sends to it, with no one to wait for")
            gate = None
        if recovery and "run" not in stage:
            self.r.error("RUN-MISSING", where, "a recovery stage runs a command (`run`) and has checks; "
                         "this one has no `run`")
        elif "run" not in stage and gate is None:
            self.r.error("RUN-MISSING", where, "a stage runs a command (`run`) or is an owner gate "
                         "(`owner_gate`); this one has neither")
        if run:
            self.variables(run, where + ".run")
        workdir = self.text(stage, "workdir", where)
        if workdir and self.local_path(workdir, where + ".workdir", "workdir"):
            self.variables(workdir, where + ".workdir")
        self.whole(stage, "timeout", where, *STAGE_TIMEOUT_RANGE)
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
        if gate is not None and "run" not in stage and listed is not None:
            # nothing runs in a gate-only stage, so its checks would never be judged
            self.r.error("CHECKS-WITHOUT-RUN", where + ".checks", "this stage is only an owner gate and "
                         "has no `run`, so nothing would judge its checks; move them to a stage that "
                         "has a `run`, or give this stage the `run` they check")
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
            self.keys(fail, self.table(FAIL_KEYS, "fail"), at)
            self.ident(fail, at, seen=seen, kind="failure id")
            self.text(fail, "when", at)
            if self.fmt < 2:
                continue
            self.items(fail, "covers", at)
            self.source(fail, at)
            if "detect" in fail:
                self.detect(fail["detect"], at + ".detect")
                if "then" not in fail:
                    self.r.error("THEN-MISSING", at + ".then", "a fail case with `detect` says what to do "
                                 "when the failure is recognized: `then` is retry, stop, ask-owner or "
                                 "{run: <recovery stage id>}")
            if "then" in fail:
                self.then(fail["then"], at + ".then", stage)

    def detect(self, detect, at):
        """A `detect` is a check without `id`, `covers` and `source`: the check rules, each finding
        reported as DETECT-INVALID with the rule it broke."""
        if not isinstance(detect, dict):
            self.r.error("DETECT-INVALID", at, "`detect` is a check mapping with `type` and the fields of "
                         "that type, without `id` and `covers`")
            return
        outer, self.r = self.r, _Report()
        try:
            self.check(detect, at, base=DETECT_KEYS)
        finally:
            inner, self.r = self.r, outer
        for e in inner.errors:
            self.r.error("DETECT-INVALID", e["where"], "a detect follows the check rules: %s (%s)"
                         % (e["message"], e["code"]))

    def then(self, then, at, stage):
        if isinstance(then, str) and then in THEN_WORDS:
            return
        if (isinstance(then, dict) and list(then) == ["run"] and isinstance(then["run"], str)
                and then["run"].strip()):
            target = then["run"]
            if target not in self.stage_ids:
                self.r.error("RECOVERY-UNKNOWN", at + ".run", "`%s` is not a stage of this runbook" % target)
            elif target not in self.recovery:
                self.r.error("RECOVERY-NOT-MARKED", at + ".run", "stage `%s` is not marked `recovery: true`; "
                             "a fail case sends only to a recovery stage, which runs outside the run order"
                             % target)
            elif target == stage.get("id"):
                self.r.error("RECOVERY-SELF", at + ".run", "recovery stage `%s` would send its own failure "
                             "back to itself" % target)
            return
        self.r.error("THEN-UNKNOWN", at, "`then` is retry, stop, ask-owner or {run: <recovery stage id>}, "
                     "found %r" % (then,))

    def source(self, block, where):
        value = self.text(block, "source", where)
        if value is not None and not source_shape(value):
            self.r.warn("SOURCE-SHAPE", self._at(where, "source"), "source %r has none of the shapes "
                        "spec:<item id>, note:%s/<file>, interview:<slot id>, owner:<decision ref>, "
                        "default" % (value, NOTES_DIR))

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

    def check(self, chk, at, base=None):
        """One check; `base` is the table of the fields every type has (a detect has fewer)."""
        base = self.table(CHECK_KEYS, "check") if base is None else base
        kind = chk.get("type")
        if "type" in chk and kind not in CHECK_TYPES:
            self.r.error("CHECK-TYPE-UNKNOWN", at + ".type", "check type %r is not one of %s"
                         % (kind, ", ".join(CHECK_TYPES)))
            self.keys({k: v for k, v in chk.items() if k in base}, base, at)
            return
        allowed = dict(base)
        allowed.update(TYPE_KEYS.get(kind, {}))
        self.keys(chk, allowed, at)
        self.text(chk, "description", at)
        if "covers" in base:
            self.items(chk, "covers", at)
        if "source" in base:
            self.source(chk, at)
        if kind == "exit-code":
            self.whole(chk, "expect", at, *EXPECT_RANGE)
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
        if isinstance(value, str) and VAR.search(value):
            # text with a ${NAME} inside ("v${RELEASE}"): the name must exist; evaluate replaces it
            self.variables(value, at)
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
        self.whole(chk, "timeout", at, *PLUGIN_TIMEOUT_RANGE)
        if script and VAR.search(script):
            # evaluate runs the script path as written; only args get ${NAME} replaced
            self.r.error("PLUGIN-SCRIPT-VARIABLE", at + ".script", "a plugin `script` is a fixed path; "
                         "${...} is replaced in `args` only, found %r" % script)
            return
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
        self.keys(gate, self.table(GATE_KEYS, "gate"), at)
        self.text(gate, "approve", at)
        self.items(gate, "evidence", at)
        self.items(gate, "covers", at)
        if self.fmt >= 2:
            self.source(gate, at)

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
# spec-kit item lines. The template writes `- **SC-001**: ...`; people also write the colon inside
# the bold, a numbered list, no bullet, no bold (`SC-001: ...`), a heading, or a table row. Edge
# cases (EC-nnn) take the same shapes; they are items of a format 2 runbook only.
_KIT_LEAD = r"^\s*(?:[-*+]\s+|\d{1,9}[.)]\s+|#{1,6}\s+)?"
_KIT_ITEM = re.compile(_KIT_LEAD + r"(?:\*\*|__)((?:SC|FR|EC)-\d+)\s*:?\s*(?:\*\*|__)\s*:?\s*(.*)$")
_KIT_BARE = re.compile(_KIT_LEAD + r"((?:SC|FR|EC)-\d+)\s*:\s*(.*)$")
_KIT_ROW = re.compile(r"^\s*\|\s*(?:\*\*|__)?((?:SC|FR|EC)-\d+)(?:\*\*|__)?\s*\|(.*)$")
_KIT_ID = re.compile(r"\b(?:SC|FR)-\d+\b")
_EC_ID = re.compile(r"\bEC-\d+\b")
_INLINE_COMMENT = re.compile(r"<!--.*?-->")
_ANY_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*$")
# a top-level bullet (at most one space in front); a deeper one is a detail of the bullet above it
_EC_BULLET = re.compile(r"^ ?(?:[-*+]|\d{1,9}[.)])\s+(.*\S)\s*$")


#: an OpenSpec scenario whose name starts with a spec-kit id (`#### Scenario: SC-002 ...`) keeps that
#: id: a runbook `covers: [SC-002]` finds it, and intake keys its row by the id, so a spec that moves
#: from spec-kit to OpenSpec keeps its rows. SC-nnn stays a required item, FR-nnn an optional one.
_ALIAS = re.compile(r"^((?:SC|FR)-\d+)\b\s*[:.)-]?\s*(.*)$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")


def scenario_alias(name):
    """(id, rest) when an OpenSpec scenario name starts with a spec-kit id, else (None, name)."""
    m = _ALIAS.match(name.strip())
    if m:
        return m.group(1), m.group(2).strip()
    return None, name.strip()


def _openspec_items(lines, capability=None):
    """Scenario items with the requirement and section they sit under, their line and their body
    (the lines under the scenario heading up to the next heading)."""
    items, requirement, section = [], None, ""
    mask = _fence_mask(lines)
    current = None
    for n, (line, masked) in enumerate(zip(lines, mask), 1):
        if masked:
            continue
        if _HEADING.match(line):
            current = None
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
            alias, _rest = scenario_alias(name)
            removed = section.startswith("REMOVED")
            current = {"id": "%s/%s" % (capability, bare) if capability else bare, "bare": bare,
                       "capability": capability, "kind": alias[:2] if alias else "scenario",
                       "text": name, "required": not removed and (alias is None or alias.startswith("SC-")),
                       "requirement": requirement, "section": section, "line": n, "body": []}
            if alias:
                current["alias"] = alias
            items.append(current)
            continue
        if current is not None:
            current["body"].append(line)
    return items


def _kit_item(line):
    """(id, text) when the line states a spec-kit item in a shape this reader knows, else None."""
    m = _KIT_ROW.match(line)
    if m:
        cells = [c.strip() for c in m.group(2).split("|")]
        return m.group(1), " ".join(c for c in cells if c)
    m = _KIT_ITEM.match(line) or _KIT_BARE.match(line)
    if m:
        return m.group(1), m.group(2).strip()
    return None


def _edge_heading(text):
    """True for the heading text of an Edge Cases section (`Edge Cases`, `Edge cases *(optional)*`)."""
    name = re.sub(r"\(.*?\)", " ", re.sub(r"[*_`]", "", text))
    name = re.sub(r"\s#+$", "", name).replace(":", " ")
    return " ".join(name.split()).casefold() == "edge cases"


def _speckit_items(lines):
    """Items from the known line shapes first; then any SC-nnn, FR-nnn or EC-nnn id that appears
    only in some other line is still an item (an SC or EC is still required), marked `loose` with
    its line number, so a criterion written in an odd shape can never drop out of coverage unseen.

    Edge cases (EC-nnn) are kept apart in `edge_cases`: they are items of a format 2 runbook only
    (spec_items). `unnumbered` holds the top-level bullets under an Edge Cases heading that carry
    no EC id, and `edge_case_bullets` counts every such bullet."""
    items, edge, unnumbered, clarify, seen = [], [], [], 0, set()
    bullets, section = 0, None             # section: the level of the Edge Cases heading we are under
    open_lines = []
    for n, (line, masked) in enumerate(zip(lines, _fence_mask(lines)), 1):
        if masked:
            continue
        line = _INLINE_COMMENT.sub(" ", line)
        open_lines.append((n, line))
        clarify += line.count("[NEEDS CLARIFICATION")
        head = _ANY_HEADING.match(line)
        if head:
            level = len(head.group(1))
            if _edge_heading(head.group(2)):
                section = level
            elif section is not None and level <= section:
                section = None
        elif section is not None:
            bullet = _EC_BULLET.match(line)
            if bullet:
                bullets += 1
                if not _EC_ID.search(line):
                    unnumbered.append({"line": n, "text": bullet.group(1).strip()})
        found = _kit_item(line)
        if found and found[0] not in seen:
            ident = found[0]
            seen.add(ident)
            (edge if ident.startswith("EC-") else items).append(
                {"id": ident, "kind": ident[:2], "text": found[1], "line": n,
                 "required": ident[:2] in ("SC", "EC")})
    for n, line in open_lines:
        for ident in _KIT_ID.findall(line) + _EC_ID.findall(line):
            if ident not in seen:
                seen.add(ident)
                (edge if ident.startswith("EC-") else items).append(
                    {"id": ident, "kind": ident[:2], "text": line.strip(), "line": n,
                     "required": ident[:2] in ("SC", "EC"), "loose": True})
    items.sort(key=lambda i: (not i["required"], i["id"]))
    edge.sort(key=lambda i: i["id"])
    return {"items": items, "clarifications": clarify, "edge_cases": edge, "unnumbered": unnumbered,
            "edge_case_bullets": bullets}


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
    spec-kit items are SC-nnn (required) and FR-nnn (not required); a spec-kit spec also gives
    "edge_cases" (the EC-nnn items, which spec_items adds for a format 2 runbook), "unnumbered"
    and "edge_case_bullets" (see _speckit_items). OpenSpec items are
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
    found = _speckit_items(lines)
    return {"path": path, "format": fmt, "items": found["items"], "clarifications": found["clarifications"],
            "edge_cases": found["edge_cases"], "unnumbered": found["unnumbered"],
            "edge_case_bullets": found["edge_case_bullets"]}


def spec_items(spec, fmt):
    """The items a runbook of format `fmt` covers: a format 2 runbook also covers the edge cases
    (EC-nnn) of a spec-kit spec, as required items. OpenSpec items are the same in both."""
    items = list(spec["items"])
    if fmt >= 2:
        have = {i["id"] for i in items}
        more = [i for i in spec.get("edge_cases") or [] if i["id"] not in have]
        if more:
            items = sorted(items + more, key=lambda i: (not i["required"], i["id"]))
    return items


# ----------------------------------------------------------------------------- coverage
def _covering(data):
    """[(covers entry, who, where)] for every check, fail case (format 2) and owner gate. who is
    the check id, "fail:<stage id>/<fail id>" for a fail case, or "gate:<stage id>" for an owner
    gate."""
    out = []
    fmt = runbook_format(data)
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
        fails = stage.get("fails") if fmt >= 2 and isinstance(stage.get("fails"), list) else []
        for f, fail in enumerate(fails):
            if isinstance(fail, dict) and isinstance(fail.get("covers"), list):
                for k, entry in enumerate(fail["covers"]):
                    if isinstance(entry, str) and entry.strip():
                        out.append((entry, "fail:%s/%s" % (stage.get("id"), fail.get("id")),
                                    "stages[%d].fails[%d].covers[%d]" % (n, f, k)))
        gate = stage.get("owner_gate")
        if isinstance(gate, dict) and isinstance(gate.get("covers"), list):
            for k, entry in enumerate(gate["covers"]):
                if isinstance(entry, str) and entry.strip():
                    out.append((entry, "gate:%s" % stage.get("id"), "stages[%d].owner_gate.covers[%d]" % (n, k)))
    return out


def coverage(data, spec, report):
    """Map every covers entry to a spec item and report what stays uncovered.

    Returns {"required": count, "covered": {required item: [who...]}, "optional": {other covered
    item (FR-nnn, a removed scenario): [who...]}, "missing": [required item...]}. A format 2
    runbook also covers the edge cases of a spec-kit spec (spec_items)."""
    fmt = runbook_format(data)
    items = spec_items(spec, fmt)
    index = {}
    for item in items:
        keys = {_norm(item["id"])}
        if item.get("capability"):
            # a folder spec: "<cap>/<req>/<scenario>" also answers to "<req>/<scenario>" when unique
            keys.add(_norm(item["bare"]))
        if item.get("alias"):
            keys.add(_norm(item["alias"]))
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
    required = [i for i in items if i["required"]]
    if not required:
        report.error("SPEC-EMPTY", spec["path"], "the spec has no success criterion (SC-nnn) and no "
                     "OpenSpec scenario to cover; an empty list is never a pass")
    missing = []
    fix = ("the check that shows it, or to an owner gate when only a person can judge it" if fmt < 2 else
           "the check that shows it or of the fail case that detects and answers it, or to an owner gate "
           "when only a person can judge it")
    for item in required:
        if item["id"] not in covered:
            missing.append(item["id"])
            report.error("SPEC-UNCOVERED", item["id"], "no runbook check covers %s (%s); add it to the "
                         "`covers` list of %s" % (item["id"], item["text"][:120], fix))
    for item in items:
        if not item["required"] and item["kind"] == "FR" and item["id"] not in covered:
            report.warn("FR-UNCOVERED", item["id"], "no check covers %s (a warning: only success criteria "
                        "must be covered)" % item["id"])
    for item in items:
        if item.get("loose"):
            report.warn("SPEC-UNPARSED", item["id"], "%s appears at line %d of %s in a shape this reader "
                        "does not know (%r); it is counted as %s anyway. Write it as `- **%s**: ...`"
                        % (item["id"], item["line"], spec["path"], item["text"][:80],
                           {"SC": "a required success criterion", "EC": "a required edge case"}.get(
                               item["kind"], "a functional requirement"), item["id"]))
    if fmt >= 2:
        for bullet in spec.get("unnumbered") or []:
            report.error("EC-UNNUMBERED", "%s:%d" % (spec["path"], bullet["line"]), "the edge case at line "
                         "%d of %s has no EC-nnn id (%r); number it `- **EC-001**: ...`. Ids stay stable, "
                         "so the reader never numbers an edge case by its position"
                         % (bullet["line"], spec["path"], bullet["text"][:120]))
    elif spec.get("edge_case_bullets"):
        report.warn("EC-IGNORED", spec["path"], "format 1 does not cover edge cases; move to runbook: 2 "
                    "(%s lists %d under an Edge Cases heading)" % (spec["path"], spec["edge_case_bullets"]))
    if spec.get("clarifications"):
        report.warn("SPEC-CLARIFY", spec["path"], "the spec still has %d [NEEDS CLARIFICATION] marker(s)"
                    % spec["clarifications"])
    need = {i["id"] for i in required}
    return {"required": len(required),
            "covered": {k: v for k, v in covered.items() if k in need},
            "optional": {k: v for k, v in covered.items() if k not in need},
            "missing": missing}


# ----------------------------------------------------------------------------- the check
class _DeltaError(ValueError):
    """An OpenSpec change that does not apply to the living specs next to it."""


def _read_target(target):
    """parse_spec, except that an OpenSpec change folder (openspec/changes/<id>) is read as the
    living specs with the change applied, the way `alpaca intake` reads it: the runbook covers the
    whole spec after the change, not only the delta."""
    if os.path.isdir(target):
        from alpaca import intake
        if intake._is_change(target):
            try:
                return intake.effective_change(target)
            except intake.IntakeError as exc:
                raise _DeltaError("; ".join([str(exc)] + exc.detail))
    return parse_spec(target)


def check(path, spec_path=None, check_files=True, spec=None):
    """Check one runbook file, and its coverage of a spec when one is given (`spec_path`, else the
    runbook's own `spec:` field, relative to the runbook folder). `spec` takes a spec already read
    (the shape `parse_spec` returns), for a caller that builds one, such as intake applying an
    OpenSpec change to the living specs; it wins over `spec_path`.

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
    fmt = runbook_format(data)
    target = spec_path
    if target is None and isinstance(data.get("spec"), str) and data["spec"].strip():
        target = os.path.join(base, data["spec"])
    if spec is not None:
        items = spec_items(spec, fmt)
        result["spec"] = {"path": spec["path"], "format": spec["format"], "items": len(items),
                          "required": sum(1 for i in items if i["required"])}
        result["coverage"] = coverage(data, spec, report)
    elif target is not None:
        try:
            spec = _read_target(target)
        except (OSError, UnicodeDecodeError) as exc:
            report.error("SPEC-MISSING", "spec", "cannot read the spec %s: %s" % (target, exc))
        except _DeltaError as exc:
            report.error("SPEC-DELTA", "spec", str(exc))
        except ValueError as exc:
            report.error("SPEC-MIXED", "spec", str(exc))
        else:
            items = spec_items(spec, fmt)
            result["spec"] = {"path": target, "format": spec["format"],
                              "items": len(items),
                              "required": sum(1 for i in items if i["required"])}
            result["coverage"] = coverage(data, spec, report)
    elif _covering(data):
        report.warn("COVERS-WITHOUT-SPEC", "spec", "checks name `covers` entries but no spec was given; "
                    "pass --spec or set `spec:` to check them")
    if result["coverage"] is not None and check_files:
        for where, note in _note_sources(data):
            if not _note_found(base, note):
                report.warn("SOURCE-MISSING", where, "source note:%s names a note that is not in the runbook "
                            "folder or a folder above it, up to the project root (the folder that holds "
                            "project.yaml)" % note)
    code = verdict.FAIL if report.errors else verdict.PASS
    result.update(verdict=verdict.name_of(code), code=code, errors=report.errors, warnings=report.warnings)
    return result


def _note_sources(data):
    """[(where, note path)] for every well-formed `source: note:<path>` of a format 2 runbook."""
    if runbook_format(data) < 2:
        return []
    out = []

    def take(block, where):
        value = block.get("source") if isinstance(block, dict) else None
        if isinstance(value, str) and value.startswith("note:") and source_shape(value):
            out.append((where + ".source", value[len("note:"):]))

    def listed(block, key):
        value = block.get(key)
        return value if isinstance(value, list) else []

    for n, knob in enumerate(listed(data, "knobs")):
        take(knob, "knobs[%d]" % n)
    for n, stage in enumerate(listed(data, "stages")):
        if not isinstance(stage, dict):
            continue
        where = "stages[%d]" % n
        for c, chk in enumerate(listed(stage, "checks")):
            take(chk, "%s.checks[%d]" % (where, c))
        for f, fail in enumerate(listed(stage, "fails")):
            take(fail, "%s.fails[%d]" % (where, f))
        take(stage.get("owner_gate"), where + ".owner_gate")
    return out


def _note_found(base, note):
    """True when `note` (input/notes/...) is under the runbook folder or a folder above it, up to
    the project root: the first folder that holds project.yaml."""
    folder = os.path.abspath(base)
    while True:
        if os.path.isfile(os.path.join(folder, note)):
            return True
        if os.path.isfile(os.path.join(folder, "project.yaml")):
            return False
        up = os.path.dirname(folder)
        if up == folder:
            return False
        folder = up


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


def _inside(path, folder):
    path, folder = os.path.abspath(path), os.path.abspath(folder)
    return os.path.commonpath([path, folder]) == folder


def _compare(left, op, right):
    if op in ("==", "!="):
        if isinstance(left, bool) or isinstance(right, bool):
            # Python reads true as 1 and false as 0; a check does not: a bool equals only a bool
            same = isinstance(left, bool) and isinstance(right, bool) and left == right
        elif _is_number(left) and _is_number(right):
            same = float(left) == float(right)
        else:
            same = left == right
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
        path = os.path.normpath(os.path.join(workdir, rel))
        # the schema checks the path as written; a ${NAME} is known only now, so check again
        if not (_inside(path, runbook_dir) or (
                evidence_dir and chk["path"].startswith("${EVIDENCE_DIR}") and _inside(path, evidence_dir))):
            return verdict.BLOCKED, ("%s resolves to %s, outside the runbook folder%s" % (
                chk["path"], path, " and the evidence folder" if evidence_dir else ""))
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
        if isinstance(want, str) and VAR.search(want):
            whole = VAR.fullmatch(want)
            # a whole ${KNOB} keeps the knob's type; a ${NAME} inside text is replaced as text
            want = values.get(whole.group(1), want) if whole else substitute(want, values)
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


def _step_end(results, step):
    """The verdict code a stage ends with after `step` (a next_attempt answer); None while it
    goes on."""
    if step["retry"]:
        return None
    if all(code == verdict.PASS for code in results.values()):
        return verdict.PASS
    for code in results.values():
        if code in (verdict.BLOCKED, verdict.PAUSED):
            return code
    return verdict.FAIL


def respond(data, stage, results, attempt, knobs, detected):
    """The step after attempt number `attempt` of a stage, with its known failures (format 2).

    results maps each check id of the stage to its verdict code, as for next_attempt; detected
    maps a fail case id of the stage to the verdict code of its `detect` on this attempt. Returns
    next_attempt's {"retry", "knobs", "reason"} and "run" (the recovery stage to run before the
    stage runs again, else None), "end" (the verdict code the stage ends with; None while it goes
    on) and "fail" (the id of the fail case that decided, else None).

    When every check passed the stage ends PASS, whatever was detected. Otherwise the first fail
    case, in file order, that has a `detect` and whose detect passed decides: `stop` ends FAIL;
    `ask-owner` ends PAUSED-FOR-DECISION; `retry` goes through the retry rule, its stop_on, its
    attempts and its knob range, but not its on_fail (the fail case names this failure as one a
    new attempt may fix); `{run: <id>}` returns the recovery stage, unless a stop_on check failed
    or no attempt is left for the rerun that follows it (the rerun counts as an attempt of this
    stage). With no fail case recognized, the retry rule decides exactly as next_attempt does."""
    retry = stage.get("retry") or {}
    failing = not all(code == verdict.PASS for code in results.values())
    for fail in (stage.get("fails") or []) if failing else []:
        if not isinstance(fail, dict) or not isinstance(fail.get("detect"), dict):
            continue
        ident, then = fail.get("id"), fail.get("then")
        if detected.get(ident) != verdict.PASS:
            continue
        why = "fail case %s was recognized" % ident
        if then in ("stop", "ask-owner"):
            end = verdict.FAIL if then == "stop" else verdict.PAUSED
            return {"retry": False, "knobs": dict(knobs), "reason": "%s; its answer is %s" % (why, then),
                    "run": None, "end": end, "fail": ident}
        if then == "retry":
            step = next_attempt(data, dict(stage, retry=dict(retry, on_fail=None)), results, attempt, knobs)
            return dict(step, reason="%s; %s" % (why, step["reason"]), run=None,
                        end=_step_end(results, step), fail=ident)
        if isinstance(then, dict) and isinstance(then.get("run"), str):
            limit = retry.get("max_attempts", 1)
            stopped = [c for c, code in results.items() if code == verdict.FAIL and c in (retry.get("stop_on") or [])]
            if stopped:
                reason = "%s, but check %s failed and is listed in stop_on" % (why, stopped[0])
            elif attempt >= limit:
                reason = ("%s, but attempt %d of %d is used, and the rerun after recovery stage %s would be "
                          "one more" % (why, attempt, limit, then["run"]))
            else:
                return {"retry": False, "knobs": dict(knobs), "reason": "%s; run recovery stage %s, then "
                        "attempt %d of %d" % (why, then["run"], attempt + 1, limit),
                        "run": then["run"], "end": None, "fail": ident}
            return {"retry": False, "knobs": dict(knobs), "reason": reason, "run": None,
                    "end": verdict.FAIL, "fail": ident}
    step = next_attempt(data, stage, results, attempt, knobs)
    return dict(step, run=None, end=_step_end(results, step), fail=None)


# ------------------------------------------------------------------------ the bar of a row
def _bar_shown(value, quote=True):
    """A value as a bar writes it: a whole number without `.0` (a knob default of 50 and of 50.0
    give one bar), true/false in lower case, text in double quotes (or as it is, quote=False)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value) if quote else value
    return json.dumps(value, sort_keys=True, default=str)


def _bar_check(chk, knobs):
    """(text, knob names): `<type> <what>` for one check or detect, every ${KNOB} replaced by the
    knob's default. Built-in variables stay as written."""
    names = []

    def put(text):
        def one(m):
            name = m.group(1)
            if name not in knobs:
                return m.group(0)
            if name not in names:
                names.append(name)
            return _bar_shown(knobs[name]["default"], quote=False)
        return VAR.sub(one, str(text))

    kind = chk.get("type")
    path = put(chk.get("path"))
    if kind == "exit-code":
        text = "exit == %s" % _bar_shown(chk.get("expect", 0))
    elif kind == "file-exists":
        text = "%s exists%s" % (path, "" if chk.get("non_empty") is False else ", non-empty")
    elif kind == "regex-in-file":
        text = "%s %s /%s/%s" % (path, "does not match" if chk.get("absent") else "matches", chk.get("pattern"),
                                 "i" if chk.get("ignore_case") else "")
    elif kind == "json-field":
        value = chk.get("value")
        whole = VAR.fullmatch(value) if isinstance(value, str) else None
        if whole and whole.group(1) in knobs:
            put(value)                                  # names the knob; its value keeps its type
            shown = _bar_shown(knobs[whole.group(1)]["default"])
        elif isinstance(value, str):
            shown = _bar_shown(put(value))
        else:
            shown = _bar_shown(value)
        text = "%s %s %s %s" % (path, chk.get("field"), chk.get("op"), shown)
    elif kind == "plugin":
        args = [put(a) if isinstance(a, str) else _bar_shown(a) for a in chk.get("args") or []]
        text = "%s exits 0" % " ".join([str(chk.get("script"))] + args)
    else:
        text = ""
    return ("%s %s" % (kind, text)).strip(), names


def _bar_note(names, knobs):
    """` (knob A, owner only; knob B)` for the knobs a bar put in, or nothing."""
    if not names:
        return ""
    return " (%s)" % "; ".join("knob %s%s" % (n, ", owner only" if knobs[n]["owner_only"] else "")
                               for n in names)


def bar_parts(data):
    """What each part of a loaded runbook asks, written as intake writes the `bar` of a row: every
    check (`<stage>/<check>: <type> <what> <op> <value>`, a knob's value put in and the knob named,
    owner-only marked), every fail case (`fail <stage>/<id>: detect <...> then <...>`, or `when
    <text>` without a detect) and every owner gate (`owner approves: <approve text>`).

    Returns {"parts": {who: {"kind", "stage", "bar", "source", "knobs"}}, "stages": {stage id:
    {"position", "of", "label", "recovery"}}, "knobs": {knob id: {"default", "owner_only",
    "source"}}}. `who` is the name coverage gives a covering part (a check id, `fail:<stage>/<id>`,
    `gate:<stage>`), so an item's bar is the bars of coverage["covered"][item] joined with "; ".
    A stage's position counts the stages in run order (`3/5 load-test`); a recovery stage runs
    outside that order and has none. Read only a runbook that passed the check."""
    knobs = {}
    for knob in data.get("knobs") or []:
        if isinstance(knob, dict) and isinstance(knob.get("id"), str):
            knobs[knob["id"]] = {"default": knob.get("default"), "owner_only": bool(knob.get("owner_only")),
                                 "source": knob.get("source")}
    stages = [s for s in data.get("stages") or [] if isinstance(s, dict)]
    order = [s.get("id") for s in stages if s.get("recovery") is not True]
    positions, parts = {}, {}
    for stage in stages:
        sid = stage.get("id")
        if stage.get("recovery") is True:
            positions[sid] = {"position": None, "of": len(order), "label": "recovery %s" % sid, "recovery": True}
        else:
            n = order.index(sid) + 1
            positions[sid] = {"position": n, "of": len(order), "label": "%d/%d %s" % (n, len(order), sid),
                              "recovery": False}
        for chk in stage.get("checks") or []:
            if isinstance(chk, dict):
                text, names = _bar_check(chk, knobs)
                parts[str(chk.get("id"))] = {"kind": "check", "stage": sid, "source": chk.get("source"),
                                             "knobs": names, "bar": "%s/%s: %s%s" % (
                                                 sid, chk.get("id"), text, _bar_note(names, knobs))}
        for fail in stage.get("fails") or []:
            if not isinstance(fail, dict):
                continue
            pieces, names = [], []
            if isinstance(fail.get("detect"), dict):
                text, names = _bar_check(fail["detect"], knobs)
                pieces.append("detect %s%s" % (text, _bar_note(names, knobs)))
            then = fail.get("then")
            if then is not None:
                pieces.append("then %s" % ("run %s" % then["run"] if isinstance(then, dict) else then))
            if not pieces:
                pieces.append("when %s" % " ".join(str(fail.get("when") or "").split()))
            parts["fail:%s/%s" % (sid, fail.get("id"))] = {
                "kind": "fail", "stage": sid, "source": fail.get("source"), "knobs": names,
                "bar": "fail %s/%s: %s" % (sid, fail.get("id"), " ".join(pieces))}
        gate = stage.get("owner_gate")
        if isinstance(gate, dict):
            parts["gate:%s" % sid] = {"kind": "gate", "stage": sid, "source": gate.get("source"), "knobs": [],
                                      "bar": "owner approves: %s" % " ".join(str(gate.get("approve") or "").split())}
    return {"parts": parts, "stages": positions, "knobs": knobs}


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


def _from_caller(path):
    """A command-line path, read from the folder the command was run in (alpaca/util.py
    from_caller)."""
    from alpaca import util
    return util.from_caller(path)


def add_check_arguments(parser):
    """The arguments of `alpaca runbook check`. The partner kit's check_runbook.py carries this
    function, so both take the same options."""
    parser.add_argument("file", help="the runbook.yaml to check")
    parser.add_argument("--spec", default=None, help="a spec-kit spec.md, an OpenSpec spec.md, or an "
                                                     "OpenSpec folder of <capability>/spec.md (default: "
                                                     "the runbook's `spec:` field)")
    parser.add_argument("--no-files", action="store_true", help="skip the plugin script look-ups")
    parser.add_argument("--json", action="store_true")


def run_check(file, spec=None, no_files=False, as_json=False, resolve=None):
    """Check one runbook, print the result and return its verdict code. This is the body of
    `alpaca runbook check`, and the partner kit's check_runbook.py carries it unchanged, so both
    print the same lines and exit with the same code. `resolve` maps a command-line path to the
    path to read (the verb passes _from_caller; the kit reads paths as given)."""
    resolve = resolve or (lambda path: path)
    result = check(resolve(file), spec_path=resolve(spec), check_files=not no_files)
    # show the paths as the person typed them; messages keep the full path that was read
    result["runbook"] = file
    if spec and result.get("spec"):
        result["spec"]["path"] = spec
    if as_json:
        print(json.dumps({k: result[k] for k in ("verdict", "runbook", "spec", "errors", "warnings",
                                                  "coverage")}, indent=2, sort_keys=True))
    else:
        _print(result)
    return result["code"]


USAGE_TEXT = ("usage: alpaca runbook check <file> [--spec <path>] [--no-files] [--json]\n"
              "       alpaca runbook kit [--out <dir>] [--zip]")


def cmd_runbook(args):
    verb = getattr(args, "runbook_verb", None)
    if verb == "kit":
        from alpaca import runbook_kit
        return runbook_kit.cmd_kit(args)
    if verb != "check" or not getattr(args, "file", None):
        print(USAGE_TEXT, file=sys.stderr)
        return verdict.USAGE
    return run_check(args.file, args.spec, args.no_files, args.json, resolve=_from_caller)


def _parser(sub):
    p = sub.add_parser("runbook", help="check a runbook file against the format and its spec "
                                       "(docs/runbook-format.md); build the partner kit")
    v = p.add_subparsers(dest="runbook_verb")
    c = v.add_parser("check", help="refuse a malformed runbook; with a spec, fail on any uncovered "
                                   "success criterion or scenario; read-only, writes no record")
    add_check_arguments(c)
    k = v.add_parser("kit", help="build the partner runbook kit (format, checker, schema, agent "
                                 "instructions, templates, example) from this product's own files")
    k.add_argument("--out", default=".", help="the folder the kit folder is written into "
                                              "(default: the current folder)")
    k.add_argument("--zip", action="store_true", help="also write <kit>.zip next to the kit folder")


def _register():
    from alpaca import cli
    cli.command("runbook")(cmd_runbook)
    cli.register_parser("runbook", _parser)


_register()
