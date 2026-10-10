"""The runbook: a domain's executable plan, and `alpaca runbook check`.

A runbook sits between a spec and intake. The spec (spec-kit `spec.md` with FR-nnn and SC-nnn
ids, or an OpenSpec spec with `### Requirement:` blocks and `#### Scenario:` children) says what
must hold. The runbook says how it is shown: the stages, the command each stage runs, its inputs
and outputs, the pass checks, the knobs a retry may move, the retry rules and the owner gates.
Intake turns the runbook into checklist rows and task contracts. The format is documented in
docs/runbook-format.md; this module is its one reader.

One runbook is one YAML file (`runbook.yaml`). `check(path, spec_path)` reads it, refuses every
malformed field at once with a code, a dotted location and a plain message, and, with a spec,
fails when a success criterion, edge case (format 2) or scenario is covered by no check, fail
case or owner gate. An empty measured population is never a pass: a spec with nothing to cover is
refused too.

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

#: the newest plain format this reader knows, and every plain format it reads
FORMAT = 2
FORMATS = (1, 2)
#: format 3 is the research format: format 2 plus a required `research` block and `spec`
RESEARCH_FORMAT = 3
READ_FORMATS = FORMATS + (RESEARCH_FORMAT,)
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
            "owner": False, "knobs": False, "stages": True, "research": False}
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
#: the fields format 3 added, per table; below format 3 each is FIELD-UNKNOWN, as it was
SINCE_3 = {"top": ("research",)}
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
    "RECOVERY-NEEDED", "RECOVERY-SELF", "RECOVERY-CHAIN", "RECOVERY-NO-ATTEMPT", "DETECT-INVALID",
    "COVERS-NO-DETECT",
)
WARNING_CODES = ("FR-UNCOVERED", "SPEC-CLARIFY", "COVERS-WITHOUT-SPEC", "SPEC-UNPARSED",
                 "EC-IGNORED", "SOURCE-SHAPE", "SOURCE-MISSING")

# ----------------------------------------------------------------- format 3: the research block
# design/research-engine/runbook-format-3.md sections 4, 6.1, 7 and 14.1. Every rule below runs only
# when a file is read as format 3 (`runbook: 3` with a `research` key), so format 1 and 2 files
# check exactly as before.
MODES = ("improve", "repair")
DIRECTIONS = ("higher", "lower")
METRIC_KINDS = ("deterministic", "noisy")
MEDIANS = ("worse", "middle")
PARSERS = ("markers", "json-field")
ACCEPT_RULES = ("champion", "deficit-pareto", "repair")
AFTER_TARGETS = ("stop", "pareto")
PROPOSAL_TYPES = ("str", "text", "int", "float", "bool", "enum", "list", "paths")
PROPOSAL_BASE = {"improve": ("attempt", "title", "category", "hypothesis", "files", "expected"),
                 "repair": ("attempt", "title", "category", "hypothesis", "files", "suspects", "evidence")}
F2P_ORIGINS = ("store", "generate")
AGENT_ROLES = ("proposer", "implementer", "notes", "audit", "reproducer", "reviewer")
RESEARCH_VARS = ("WORKTREE", "CONTRACT_DIR")          # every command of a format 3 file
METRIC_VARS = ("REPEAT", "SEED")                     # metric commands and paths
F2P_VARS = ("REPRODUCER_DIR",)                       # fail-to-pass commands
PLACEHOLDER = re.compile(r"^<FILL:[^>]*>$")
HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")   # egress.allow
TOOL = re.compile(r"^[A-Za-z0-9._+-]+$")              # fingerprint.tools: a bare command name
COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")             # repair.cases[].base
PROPOSAL_FIELD = re.compile(r"^[a-z][a-z0-9_]*$")     # proposal[].field
PART_EXTRAS = {"covers": False, "source": False}      # covers: required in effect on a metric and a case
COVERS_REQUIRED = ("metrics", "repair.cases")         # COVERS-NONE; research_fields() reports their covers required

# the field tables, {key: required}; "required" is per mode where the key is mode-bound
RESEARCH_KEYS = {"mode": True, "defaults": False, "repo": False, "metrics": False, "accept": True,
                 "paths": True, "fingerprint": True, "gates": True, "stop": True, "budget": False,
                 "disk": True, "proposal": False, "categories": False, "lessons": False,
                 "controls": True, "holdout": False, "repair": False, "coupled": False,
                 "runtime": False, "verifier": False, "audit": False, "egress": False}
REPO_KEYS = {"path": False, "ref": False}
MODE_KEYS = {"improve": {"metrics": True, "categories": True, "repair": None},
             "repair": {"metrics": None, "categories": False, "repair": True}}   # None: not allowed
METRIC_KEYS = dict(PART_EXTRAS, id=True, description=False, command=True, workdir=False, parser=True,
                   path=False, field=False, unit=True, direction=True, kind=True, repeats=False,
                   min_ok=False, median=False, margin=False, seeds=False, timeout=True, target=False,
                   artifact=False)
ACCEPT_KEYS = dict(PART_EXTRAS, rule=True, after_targets=False)
PATHS_KEYS = dict(PART_EXTRAS, editable=True, protected=True, hidden=False, tests=False, read=False,
                  keep=False, outputs=False)
FINGERPRINT_KEYS = dict(PART_EXTRAS, tools=True, shared=False)
GATE_ENTRY_KEYS = {"stage": True, "heavy": False, "lock_timeout": False}
STOP_KEYS = dict(PART_EXTRAS, rounds=True, slots=True)
# every cap is optional: a cap left out is no cap, and a run with none is uncapped (t-297)
BUDGET_KEYS = dict(PART_EXTRAS, usd=False, tokens=False, wall=False)
PROPOSAL_FIELD_KEYS = {"field": True, "type": True, "required": False, "values": False, "max": False,
                       "description": False}
CATEGORY_KEYS = {"id": True, "text": True}
LESSONS_KEYS = {"file": False, "note_chars": False}
CONTROLS_KEYS = dict(PART_EXTRAS, noop_rounds=True, random=True, mutate=False, syntax=False, seed=False)
HOLDOUT_KEYS = dict(PART_EXTRAS, stages=False, paths=False)
REPAIR_KEYS = {"cases": True, "pass_to_pass": True, "prepare": False}
CASE_KEYS = dict(PART_EXTRAS, id=True, bug_report=True, base=False, fail_to_pass=True, suspect_files=False)
F2P_KEYS = {"origin": True, "command": False, "tests": False, "timeout": False, "repeats": False}
P2P_KEYS = dict(PART_EXTRAS, command=True, min_tests=True, timeout=True)
COUPLED_KEYS = {"id": True, "files": True, "note": False}
RUNTIME_KEYS = {"timeouts": False, "effort": False, "retries": False}
EFFORTS = ("low", "medium", "high")
VERIFIER_KEYS = {"timeout": False, "rerun_timeout": False}
AUDIT_KEYS = {"hints": False, "assert_tokens": False, "build_files": False, "steering": False}
EGRESS_KEYS = {"allow": False}
DEFAULTS_FILE_KEYS = {"pack": False, "research": True}
#: the engine layer: section 4's unconditional defaults. A key names a part of the block, or, with
#: `[]`, the defaults of every item of a keyed list. The conditional defaults (section 7.2) are
#: _engine_fill's: a deterministic metric's repeats, min_ok and margin, accept.after_targets when a
#: metric has a target, and a proposal field's max by its type.
RESEARCH_DEFAULTS = {
    "repo": {"path": ".", "ref": "HEAD"},
    "paths": {"hidden": [], "tests": [], "read": [], "keep": [], "outputs": []},
    "fingerprint": {"shared": []},
    "proposal": [],
    "lessons": {"note_chars": 300},
    "controls": {"seed": 0},
    "holdout": {"stages": [], "paths": []},
    "coupled": [],
    "runtime": {"timeouts": {"proposer": 1200, "implementer": 1800, "notes": 240, "audit": 900,
                             "reproducer": 1800, "reviewer": 900},
                "effort": {}, "retries": 2},
    "verifier": {"timeout": 1800, "rerun_timeout": 1200},
    "audit": {"assert_tokens": [], "build_files": [], "steering": []},
    "egress": {"allow": []},
    "metrics[]": {"median": "worse"},
    "gates[]": {"heavy": False, "lock_timeout": 14400},
    "proposal[]": {"required": True},
    "repair.cases[]": {"suspect_files": []},
    "repair.cases[].fail_to_pass": {"command": "sh ${REPRODUCER_DIR}/run.sh", "timeout": 1200, "repeats": 1},
}
#: the parts the engine layer makes when the block has none (the optional mappings and lists);
#: a required part it only completes
_ENGINE_MAKES = ("repo", "proposal", "lessons", "holdout", "coupled", "runtime", "verifier", "audit",
                 "egress")
#: a proposal field's `max` when the pack sets none, by its type (section 7.2)
PROPOSAL_MAX = {"str": 200, "text": 4000, "list": 50, "paths": 50}
RESEARCH_ERROR_CODES = (
    "FIELD-UNFILLED", "VALUE-UNKNOWN", "DEFAULTS-UNREADABLE", "TARGET-SOURCE", "ACCEPT-RULE-MISMATCH",
    "PATHS-RULE-INVALID", "PATHS-OVERLAP", "TOOL-INVALID", "HOST-INVALID", "STAGE-UNKNOWN", "STAGE-ROLE",
    "STAGE-UNUSED", "GATES-ORDER", "TIMEOUT-MISSING", "THEN-ASK-OWNER", "KNOB-MOVES-BAR",
    "PACK-OUTSIDE-REPO", "PROPOSAL-FIELD-RESERVED", "FILE-MISSING", "BASE-NOT-COMMIT", "COVERS-NONE",
)
#: the keyed lists of the block and the key each merges on (section 7.2); every other list is
#: replaced whole
KEYED_LISTS = {"metrics": "id", "categories": "id", "coupled": "id", "proposal": "field",
               "repair.cases": "id"}

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
        assert code in ERROR_CODES + RESEARCH_ERROR_CODES, code
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
    """The format a loaded runbook is read as: 3 for `runbook: 3` with a `research` key (the
    research format), else its `runbook:` value when it is a plain format this reader knows, else
    the newest plain format (such a file is refused with VERSION-UNSUPPORTED anyway)."""
    value = data.get("runbook")
    if value == RESEARCH_FORMAT and not isinstance(value, bool) and "research" in data:
        return RESEARCH_FORMAT
    return int(value) if value in FORMATS else FORMAT


class _Unread(object):
    """The value of a field that is present but unread: a `<FILL:...>` placeholder, or a mapping or
    list of the wrong kind, already reported. It counts as present (no FIELD-MISSING) and every
    rule that needs the value skips it (runbook-format-3.md section 4, rule 2)."""

    def __repr__(self):
        return "<unread>"


UNREAD = _Unread()


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
        """The field table as this file's format has it: format 1 has none of the SINCE_2 fields,
        and formats 1 and 2 have none of the SINCE_3 fields."""
        drop = ()
        if self.fmt < 2:
            drop += SINCE_2.get(name, ())
        if self.fmt < RESEARCH_FORMAT:
            drop += SINCE_3.get(name, ())
        if not drop:
            return table
        return {k: v for k, v in table.items() if k not in drop}

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
        if key not in block or block[key] is UNREAD:
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
        if key in block and block[key] is not UNREAD and not isinstance(block[key], bool):
            self.r.error("FIELD-TYPE", self._at(where, key), "`%s` must be true or false" % key)

    def whole(self, block, key, where, low, high):
        if key not in block or block[key] is UNREAD:
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
        if key not in block or block[key] is UNREAD:
            return []
        value = block[key]
        at = self._at(where, key)
        if not isinstance(value, list):
            self.r.error("FIELD-NOT-LIST", at, "`%s` must be a list (write `[%s]` for one item)" % (
                key, value if isinstance(value, str) else "..."))
            return []
        out = []
        for n, item in enumerate(value):
            if item is UNREAD:
                continue
            if kind is str and (not isinstance(item, str) or not item.strip()):
                self.r.error("FIELD-TYPE", "%s[%d]" % (at, n), "each `%s` item must be non-empty text" % key)
            else:
                out.append(item)
        return out

    def ident(self, block, where, pattern=SLUG, seen=None, kind="id"):
        value = block.get("id")
        at = self._at(where, "id")
        if "id" not in block or value is UNREAD:
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

    def variables(self, value, at, extra=()):
        """Every ${NAME} in value names a knob or a built-in variable (in format 3 also
        ${WORKTREE} and ${CONTRACT_DIR}, plus `extra`)."""
        texts = []
        if isinstance(value, str):
            texts = [value]
        elif isinstance(value, list):
            texts = [v for v in value if isinstance(v, str)]
        elif isinstance(value, dict):
            texts = [v for v in value.values() if isinstance(v, str)]
        allowed = BUILTIN_VARS
        if self.fmt == RESEARCH_FORMAT:
            allowed = BUILTIN_VARS + RESEARCH_VARS + tuple(extra)
        for text in texts:
            for name in VAR.findall(text):
                if name not in self.knobs and name not in allowed:
                    self.r.error("VAR-UNKNOWN", at, "${%s} names no knob and no built-in variable (%s)"
                                 % (name, ", ".join(allowed)))

    # -- blocks
    def run(self):
        d = self.data
        research = self.fmt == RESEARCH_FORMAT
        unfilled = self.unfilled(d) if research else []
        top = self.table(TOP_KEYS, "top")
        if research:
            # a research contract always names the spec it covers, so coverage is always checked
            top = dict(top, spec=True, research=True)
        self.keys(d, top, "")
        if "runbook" in d:
            value = d["runbook"]
            if value == RESEARCH_FORMAT and not isinstance(value, bool) and "research" not in d:
                self.r.error("VERSION-UNSUPPORTED", "runbook", "`runbook: 3` is the research format and needs "
                             "a `research` block; a runbook without one is `runbook: 2`")
            elif value not in FORMATS and not research:
                self.r.error("VERSION-UNSUPPORTED", "runbook", "`runbook: %r` is not a format this reader "
                             "knows (%s, or %d for a research runbook with a `research` block); write "
                             "`runbook: %d`" % (value, ", ".join(map(str, FORMATS)), RESEARCH_FORMAT, FORMAT))
        self.ident(d, "", kind="runbook id")
        self.text(d, "title", "")
        self.text(d, "description", "")
        self.text(d, "owner", "")
        spec = self.text(d, "spec", "")
        if spec:
            self.local_path(spec, "spec", "spec")
        self.knob_list()
        self.stage_list()
        if research:
            self.research()
        if unfilled:
            # a placeholder value gets FIELD-UNFILLED and no other code (section 4, rule 2)
            def under(where):
                return any(where == u or where.startswith(u + ".") or where.startswith(u + "[")
                           for u in unfilled)
            self.r.errors[:] = [e for e in self.r.errors if e["code"] == "FIELD-UNFILLED" or not under(e["where"])]
            self.r.warnings[:] = [w for w in self.r.warnings if not under(w["where"])]

    def unfilled(self, data):
        """Report FIELD-UNFILLED for every `<FILL:...>` placeholder outside the research block
        (the block's own are reported where its layers are read) and return their locations."""
        found = []

        def walk(value, where):
            if isinstance(value, str) and PLACEHOLDER.match(value):
                found.append(where)
                self.r.error("FIELD-UNFILLED", where, "%s is still the template placeholder %s; fill it in"
                             % (where, value))
            elif isinstance(value, dict):
                for key, item in value.items():
                    walk(item, self._at(where, key))
            elif isinstance(value, list):
                for n, item in enumerate(value):
                    walk(item, "%s[%d]" % (where, n))

        for key, value in data.items():
            if key != "research":
                walk(value, str(key))
        return found

    def research(self):
        """The research block (format 3): each layer's shape where it is written, then the merged
        block's required fields and the rules across fields, each finding pointed at the layer
        that wrote the value (runbook-format-3.md sections 6.1 and 7.1)."""
        raw = self.data.get("research")
        if not isinstance(raw, dict):
            if raw is not None:
                self.r.error("FIELD-TYPE", "research", "`research` must be a mapping (the research block), "
                             "found %s" % _name(raw))
            return
        layers, _pack = _research_layers(self.data, self.base, self.r)
        block = _engine_fill(_merge_layers(layers))
        located = _located(block, layers)
        errors, warnings = research_findings(self.data, block, self.base, self.check_files)
        for e in errors:
            self.r.error(e["code"], _relocate(e["where"], located), e["message"])
        for w in warnings:
            self.r.warn(w["code"], _relocate(w["where"], located), w["message"])

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
            covers = self.items(fail, "covers", at)
            self.source(fail, at)
            if covers and not isinstance(fail.get("detect"), dict):
                self.r.error("COVERS-NO-DETECT", at + ".covers", "only a fail case that detects the failure "
                             "and answers it covers a spec item; this one has no `detect`, so it is never "
                             "recognized. Give it `detect` and `then`, or list %s in the `covers` of the check "
                             "that shows it" % ", ".join(str(c) for c in covers))
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
            elif stage.get("recovery") is True:
                self.r.error("RECOVERY-CHAIN", at + ".run", "recovery stage `%s` sends its failure on to "
                             "recovery stage `%s`; a recovery stage that fails ends the failed stage with "
                             "FAIL, so its fail cases answer retry, stop or ask-owner"
                             % (stage.get("id"), target))
            else:
                retry = stage.get("retry")
                limit = retry.get("max_attempts", 1) if isinstance(retry, dict) else 1
                if isinstance(limit, int) and not isinstance(limit, bool) and limit < 2:
                    self.r.error("RECOVERY-NO-ATTEMPT", at + ".run", "after recovery stage `%s` passes, stage "
                                 "`%s` runs again as one more attempt, and it has %s; give it "
                                 "`retry.max_attempts` of 2 or more, or recovery stage `%s` never runs"
                                 % (target, stage.get("id"), "no `retry` block (one attempt)"
                                    if not isinstance(retry, dict) else "`max_attempts: %d`" % limit, target))
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


# ---------------------------------------------------------------- format 3: layers and the merge
#: the lists of mappings in the block (their items have a table of their own)
_LIST_OF_MAPS = ("metrics", "gates", "proposal", "categories", "repair.cases", "coupled")


def research_block(data):
    """The runbook's own research block when the file is read as format 3, else None."""
    if isinstance(data, dict) and runbook_format(data) == RESEARCH_FORMAT and isinstance(data.get("research"), dict):
        return data["research"]
    return None


def _research_tables(mode):
    """{table path: field table} for a block of `mode` (None: a block whose mode is unknown, where
    every mode-bound part is allowed and none is required). The path is dotted from the block,
    and `[]` marks the items of a list."""
    top = dict(RESEARCH_KEYS)
    for key in MODE_KEYS["improve"]:
        top[key] = False
    if mode in MODE_KEYS:
        for key, required in MODE_KEYS[mode].items():
            if required is None:
                top.pop(key, None)
            else:
                top[key] = required
    roles = dict((role, False) for role in AGENT_ROLES)
    return {"": top, "repo": REPO_KEYS, "metrics[]": METRIC_KEYS, "accept": ACCEPT_KEYS,
            "paths": PATHS_KEYS, "fingerprint": FINGERPRINT_KEYS, "gates[]": GATE_ENTRY_KEYS,
            "stop": STOP_KEYS, "budget": BUDGET_KEYS, "proposal[]": PROPOSAL_FIELD_KEYS,
            "categories[]": CATEGORY_KEYS, "lessons": LESSONS_KEYS, "controls": CONTROLS_KEYS,
            "holdout": HOLDOUT_KEYS, "repair": REPAIR_KEYS, "repair.cases[]": CASE_KEYS,
            "repair.cases[].fail_to_pass": F2P_KEYS, "repair.pass_to_pass": P2P_KEYS,
            "coupled[]": COUPLED_KEYS, "runtime": RUNTIME_KEYS, "runtime.timeouts": roles,
            "runtime.effort": roles, "verifier": VERIFIER_KEYS, "audit": AUDIT_KEYS,
            "egress": EGRESS_KEYS}


def _last(where):
    """The last field name of a dotted location."""
    tail = where.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
    return re.sub(r"\[\d+\]$", "", tail)


def _unfilled(value):
    return isinstance(value, str) and PLACEHOLDER.match(value) is not None


def _shape(value, tpath, where, tables, report, mode, defaults):
    """One layer of the block (or the merged block) with every key its table lacks reported
    (FIELD-UNKNOWN) and left out, every `<FILL:...>` placeholder reported (FIELD-UNFILLED) and a
    mapping or list of the wrong kind reported, each of the last two kept as UNREAD. `defaults` is
    the defaults file's path when the layer is that file, whose block may not set `mode`,
    `defaults`, `covers` or `source` (section 7.3)."""
    if value is UNREAD:
        return value
    if _unfilled(value):
        report.error("FIELD-UNFILLED", where, "%s is still the template placeholder %s; fill it in"
                     % (where, value))
        return UNREAD
    if tpath in tables:
        if not isinstance(value, dict):
            report.error("FIELD-TYPE", where, "`%s` must be a mapping, found %s" % (_last(where) or "research",
                                                                                   _name(value)))
            return UNREAD
        table, out = tables[tpath], {}
        for key, item in value.items():
            at = "%s.%s" % (where, key if not isinstance(key, bool) else str(key).lower())
            why = None
            if not isinstance(key, str):
                why = ("a key read as %s: YAML reads a bare on/off/yes/no key as true or false; quote it"
                       % _name(key) if isinstance(key, bool) else "a key must be text, found %s" % _name(key))
            elif key not in table:
                other = [m for m in MODES if m != mode and MODE_KEYS[m].get(key) is not None]
                if tpath == "" and mode in MODES and other and MODE_KEYS[mode].get(key, False) is None:
                    why = "%s is not a field of a %s block (it belongs to %s mode); allowed: %s" % (
                        key, mode, other[0], ", ".join(sorted(table)))
                else:
                    near = difflib.get_close_matches(key, list(table), n=1, cutoff=0.6)
                    why = "`%s` is not a field here; allowed: %s%s" % (
                        key, ", ".join(sorted(table)), "; did you mean `%s`?" % near[0] if near else "")
            elif defaults and ((tpath == "" and key in ("mode", "defaults")) or key in ("covers", "source")):
                why = ("`%s` is not a field of the defaults file %s: %s" % (
                    key, defaults, "each runbook states its own mode" if key == "mode" else
                    "a defaults file names no other defaults file" if key == "defaults" else
                    "coverage and provenance are written in the runbook itself, since two runbooks that "
                    "share the file cover two specs"))
            if why:
                report.error("FIELD-UNKNOWN", at, why)
                continue
            sub = "%s.%s" % (tpath, key) if tpath else key
            out[key] = _shape(item, sub, at, tables, report, mode, defaults)
        return out
    if tpath in _LIST_OF_MAPS:
        if not isinstance(value, list):
            report.error("FIELD-NOT-LIST", where, "`%s` must be a list of mappings" % _last(where))
            return UNREAD
        return [_shape(item, tpath + "[]", "%s[%d]" % (where, n), tables, report, mode, defaults)
                for n, item in enumerate(value)]
    if isinstance(value, list):
        out = []
        for n, item in enumerate(value):
            if _unfilled(item):
                at = "%s[%d]" % (where, n)
                report.error("FIELD-UNFILLED", at, "%s is still the template placeholder %s; fill it in"
                             % (at, item))
                out.append(UNREAD)
            else:
                out.append(item)
        return out
    return value


def _research_layers(data, base_dir, report):
    """([(layer, location prefix, part)], pack name): the defaults file's `research` mapping, when
    the block names one, and the runbook's own block, each with its shape checked where it is
    written (_shape) into `report`, in merge order (section 7.1). The defaults file is always read,
    `--no-files` or not, since it is part of the contract."""
    raw = data.get("research")
    mode = raw.get("mode") if isinstance(raw.get("mode"), str) and raw.get("mode") in MODES else None
    tables = _research_tables(mode)
    layers, pack = [], None
    name = raw.get("defaults")
    plain = VAR.sub("x", name) if isinstance(name, str) else ""
    if (isinstance(name, str) and name.strip() and not _unfilled(name) and not os.path.isabs(plain)
            and os.path.normpath(plain).split(os.sep)[0] != ".."):
        doc, found = load(os.path.join(base_dir, name))
        if doc is None:
            why = "; ".join(e["message"] for e in found.errors) or "it is empty"
            report.error("DEFAULTS-UNREADABLE", "research.defaults", "the defaults file %s cannot be read as a "
                         "YAML mapping: %s" % (name, why))
        elif not isinstance(doc.get("research"), dict):
            report.error("DEFAULTS-UNREADABLE", "research.defaults", "the defaults file %s has no `research` "
                         "mapping; it holds `pack` (optional) and `research`" % name)
        else:
            for key in doc:
                if key not in DEFAULTS_FILE_KEYS:
                    report.error("FIELD-UNKNOWN", "%s:%s" % (name, key), "`%s` is not a field of a defaults file; "
                                 "allowed: %s" % (key, ", ".join(sorted(DEFAULTS_FILE_KEYS))))
            if "pack" in doc:
                value = doc["pack"]
                if _unfilled(value):
                    report.error("FIELD-UNFILLED", "%s:pack" % name, "%s:pack is still the template placeholder "
                                 "%s; fill it in" % (name, value))
                elif not isinstance(value, str) or not SLUG.match(value):
                    report.error("ID-INVALID", "%s:pack" % name, "pack name %r must use lower-case letters, digits, "
                                 "'.', '_' and '-', starting with a letter or digit" % (value,))
                else:
                    pack = value
            prefix = "%s:research" % name
            layers.append(("defaults", prefix, _shape(doc["research"], "", prefix, tables, report, mode, name)))
    layers.append(("runbook", "research", _shape(raw, "", "research", tables, report, mode, None)))
    return layers, pack


def _copy(value):
    """A deep copy of plain block data (mappings, lists and scalars)."""
    if isinstance(value, dict):
        return dict((k, _copy(v)) for k, v in value.items())
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def _item_key(item, field):
    """The merge key of a keyed-list item, or None when it has none a merge can use."""
    if isinstance(item, dict):
        value = item.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def merge_research(base, over):
    """Layer `over` merged onto layer `base` (section 7.2), as a new mapping: a mapping merges key
    by key; a keyed list (KEYED_LISTS) merges item by item on its key, a new key appended in
    `over`'s order and no item ever dropped; every other list, and every scalar, is replaced."""
    return _merge_at("", base, over)


def _merge_at(path, base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict((k, _copy(v)) for k, v in base.items())
        for key, value in over.items():
            sub = "%s.%s" % (path, key) if path else str(key)
            out[key] = _merge_at(sub, base[key], value) if key in base else _copy(value)
        return out
    field = KEYED_LISTS.get(path)
    if field and isinstance(base, list) and isinstance(over, list):
        out = [_copy(item) for item in base]
        index = {}
        for n, item in enumerate(out):
            key = _item_key(item, field)
            if key is not None and key not in index:
                index[key] = n
        for item in over:
            key = _item_key(item, field)
            if key is not None and key in index:
                out[index[key]] = _merge_at(path + "[]", out[index[key]], item)
            else:
                out.append(_copy(item))
                if key is not None:
                    index[key] = len(out) - 1
        return out
    return _copy(over)


def _fill(target, defaults):
    if not isinstance(target, dict) or not isinstance(defaults, dict):
        return
    for key, value in defaults.items():
        if key not in target:
            target[key] = _copy(value)
        elif isinstance(value, dict):
            _fill(target[key], value)


def _items_at(block, key):
    """The mapping items of the keyed list a RESEARCH_DEFAULTS key such as
    `repair.cases[].fail_to_pass` names, followed to the mapping after `[]`."""
    head, _sep, rest = key.partition("[]")
    nodes = [block]
    for part in head.split("."):
        nodes = [n.get(part) for n in nodes if isinstance(n, dict)]
    items = [i for n in nodes if isinstance(n, list) for i in n if isinstance(i, dict)]
    for part in [p for p in rest.split(".") if p]:
        items = [i.get(part) for i in items]
        items = [i for i in items if isinstance(i, dict)]
    return items


def _is_whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _engine_fill(block):
    """The engine layer (section 7.1) over a merged block, as a new mapping: every unconditional
    default of RESEARCH_DEFAULTS where the field is absent (an optional part is made, a required
    one only completed), then the conditional ones of section 7.2. A value a layer set is never
    replaced, so a value the engine filled is derived again whenever a layer is added."""
    if not isinstance(block, dict):
        return block
    out = _copy(block)
    for key, value in RESEARCH_DEFAULTS.items():
        if "[]" in key:
            continue
        if key not in out:
            if key in _ENGINE_MAKES:
                out[key] = _copy(value)
            continue
        _fill(out[key], value)
    for key, value in RESEARCH_DEFAULTS.items():
        if "[]" in key:
            for item in _items_at(out, key):
                _fill(item, value)
    metrics = [m for m in out.get("metrics") or [] if isinstance(m, dict)] \
        if isinstance(out.get("metrics"), list) else []
    for metric in metrics:
        if metric.get("kind") == "deterministic":
            metric.setdefault("repeats", 1)
            if "min_ok" not in metric and _is_whole(metric["repeats"]):
                metric["min_ok"] = metric["repeats"]
            metric.setdefault("margin", 0)
    accept = out.get("accept")
    if isinstance(accept, dict) and "after_targets" not in accept and any(
            _is_number(m.get("target")) for m in metrics):
        accept["after_targets"] = "stop"
    if isinstance(out.get("proposal"), list):
        for field in out["proposal"]:
            if isinstance(field, dict) and "max" not in field and field.get("type") in PROPOSAL_MAX:
                field["max"] = PROPOSAL_MAX[field["type"]]
    return out


def _nodes(block):
    """[(location in the block, chain, table path, value)] for every node of a block. The chain
    names the node the way every layer can find it: ("k", key) for a mapping key and ("i", field,
    key) for the item of a keyed list."""
    out = []

    def walk(value, tpath, where, chain):
        out.append((where, chain, tpath, value))
        if isinstance(value, dict):
            for key, item in value.items():
                sub = "%s.%s" % (tpath, key) if tpath else str(key)
                walk(item, sub, "%s.%s" % (where, key), chain + (("k", key),))
        elif isinstance(value, list) and tpath in KEYED_LISTS:
            field = KEYED_LISTS[tpath]
            for n, item in enumerate(value):
                walk(item, tpath + "[]", "%s[%d]" % (where, n), chain + (("i", field, _item_key(item, field)),))

    walk(block, "", "research", ())
    return out


def _resolve(part, chain):
    """Where `chain` lies in one layer, as a location suffix (".stop.slots", ".metrics[1]"), or None
    when the layer does not hold that node."""
    node, where = part, ""
    for step in chain:
        if step[0] == "k":
            if not isinstance(node, dict) or step[1] not in node:
                return None
            node, where = node[step[1]], "%s.%s" % (where, step[1])
        else:
            if step[2] is None or not isinstance(node, list):
                return None
            hit = [n for n, item in enumerate(node) if _item_key(item, step[1]) == step[2]]
            if not hit:
                return None
            node, where = node[hit[0]], "%s[%d]" % (where, hit[0])
    return where


def _keyed(chain):
    """A chain as a field path of section 0: research.metrics[compares].target."""
    out = "research"
    for step in chain:
        out += ".%s" % step[1] if step[0] == "k" else "[%s]" % step[2]
    return out


def _is_leaf(tpath, value):
    return not isinstance(value, dict) and not (isinstance(value, list) and tpath in KEYED_LISTS)


def _layer_map(block, layers):
    """{leaf field path: the layer that set it}: the last layer holding the leaf, else `engine`."""
    out = {}
    for _where, chain, tpath, value in _nodes(block):
        if not chain or not _is_leaf(tpath, value):
            continue
        name = "engine"
        for layer, _prefix, part in reversed(layers):
            if _resolve(part, chain) is not None:
                name = layer
                break
        out[_keyed(chain)] = name
    return out


def _located(block, layers):
    """{location in the merged block: the location in the layer that wrote it}, so a finding on the
    merged block points at the runbook's dotted path or at `<defaults file>:research...`."""
    out = {}
    for where, chain, _tpath, _value in _nodes(block):
        for _layer, prefix, part in reversed(layers):
            suffix = _resolve(part, chain)
            if suffix is not None:
                out[where] = prefix + suffix
                break
    return out


def _relocate(where, located):
    """A merged-block location moved to the layer that wrote the node it lies in."""
    if where in located:
        return located[where]
    best = None
    for path in located:
        if (where.startswith(path + ".") or where.startswith(path + "[")) and (best is None or len(path) > len(best)):
            best = path
    return located[best] + where[len(best):] if best is not None else where


def _merge_layers(layers):
    merged = {}
    for _layer, _prefix, part in layers:
        merged = merge_research(merged, part if isinstance(part, dict) else {})
    return merged


def expand_research(data, base_dir):
    """(block, layers, report): the pack-level block of a format 3 runbook (the engine, defaults and
    runbook layers merged, section 7.1), a map from each leaf field path to the layer that set it,
    and the findings of reading the layers (the defaults file among them). (None, {}, report) for
    a file that is not format 3."""
    report = _Report()
    if research_block(data) is None:
        return None, {}, report
    layers, _pack = _research_layers(data, base_dir, report)
    block = _engine_fill(_merge_layers(layers))
    return block, _layer_map(block, layers), report


def _research_pack(data, base_dir):
    """What the contract loader needs besides expand_research: {"raw": the defaults and runbook
    layers merged without the engine layer (the loader adds the task layer to it and fills the
    engine layer again, section 7.2), "pack": the defaults file's `pack`, "report"}."""
    report = _Report()
    if research_block(data) is None:
        return {"raw": None, "pack": None, "report": report}
    layers, pack = _research_layers(data, base_dir, report)
    return {"raw": _merge_layers(layers), "pack": pack, "report": report}


def research_findings(data, block, base_dir, check_files=True, repo_root=None):
    """(errors, warnings) of every research rule on an expanded block: the keys of every part, the
    required fields of its mode, each field's value and the rules across fields and stages
    (sections 2.2, 4 and 6.1). `data` is the runbook (its stages and knobs), `base_dir` its folder;
    `repo_root` the root of repo.path when the caller resolved it (the loader always does), else it
    is resolved as section 2.2 says, or not at all. Locations are the block's own dotted paths."""
    report = _Report()
    _Research(data, block, base_dir, check_files, report, repo_root).check_block()
    return report.errors, report.warnings


def _rule_problem(rule, editable=False):
    """Why a root rule breaks the grammar of section 2.2, or None."""
    if not rule.strip():
        return "is empty"
    if rule.startswith("/"):
        return "is absolute; write it from the root of the repository the run works on (repo.path)"
    for bad, why in (("\\", "holds a backslash"), ("**", "holds `**`; `*` matches within one part of a path"),
                     ("~", "holds `~`"), ("${", "holds `${`; a rule is fixed text")):
        if bad in rule:
            return why
    body = rule
    while body.startswith("./"):
        body = body[2:]
    if ".." in body.split("/"):
        return "climbs out with `..`"
    if editable and rule in (".", "./", "*", "*/"):
        return "admits the whole tree; name the folders and files an attempt may change"
    return None


def _location_problem(value):
    """Why a location (a folder on the host) breaks the grammar of section 2.2, or None."""
    if not value.strip():
        return "is empty"
    if "~" in value:
        return "holds `~`; write the folder as an absolute path or from the project root"
    if "${" in value:
        return "holds `${`; a location is fixed text"
    return None


def _quiet_rules(value):
    """The rules of a list that pass the grammar, read without reporting."""
    if not isinstance(value, list):
        return []
    return [r for r in value if isinstance(r, str) and _rule_problem(r) is None]


def _overlap(rule, other):
    """How root rule `rule` relates to `other` as text (./ and case ignored): "names the same path
    as", "lies under", "holds", or None."""
    def norm(r):
        r = r.replace("\\", "/")
        while r.startswith("./"):
            r = r[2:]
        return r.lower()
    a, b = norm(rule), norm(other)
    ba, bb = a.rstrip("/"), b.rstrip("/")
    if ba == bb:
        return "names the same path as"
    if b.endswith("/") and ba.startswith(bb + "/"):
        return "lies under"
    if a.endswith("/") and bb.startswith(ba + "/"):
        return "holds"
    return None


def _within(path, folder):
    """True when `path` is `folder` or lies inside it (both absolute)."""
    path, folder = os.path.abspath(path), os.path.abspath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _project_root(base_dir):
    """The first folder from base_dir up that holds project.yaml, or None."""
    folder = os.path.abspath(base_dir)
    while True:
        if os.path.isfile(os.path.join(folder, "project.yaml")):
            return folder
        up = os.path.dirname(folder)
        if up == folder:
            return None
        folder = up


def _listed(node, key):
    value = node.get(key) if isinstance(node, dict) else None
    return value if isinstance(value, list) else []


class _Research(_Checker):
    """The rules research_findings applies to one expanded research block (section 4, section 6.1,
    and the format 3 stage rules of section 2.2)."""

    def __init__(self, data, block, base_dir, check_files, report, repo_root):
        _Checker.__init__(self, data, base_dir, check_files, report)
        self.fmt = RESEARCH_FORMAT
        self.given, self.repo_root, self.mode, self.b = block, repo_root, None, {}
        for knob in _listed(data, "knobs"):
            if isinstance(knob, dict) and isinstance(knob.get("id"), str):
                self.knobs.setdefault(knob["id"], knob)
        self.stages = {}
        for n, stage in enumerate(_listed(data, "stages")):
            if isinstance(stage, dict) and isinstance(stage.get("id"), str):
                self.stages.setdefault(stage["id"], (n, stage))

    # -- helpers
    def part(self, key):
        value = self.b.get(key)
        return value if isinstance(value, dict) else None

    def word(self, node, key, where, words):
        if key not in node or node[key] is UNREAD:
            return None
        value = node[key]
        if not isinstance(value, str) or value not in words:
            self.r.error("VALUE-UNKNOWN", self._at(where, key), "`%s` is %r; it is one of %s"
                         % (key, value, ", ".join(words)))
            return None
        return value

    def number(self, node, key, where, low=None, high=None, above=False):
        if key not in node or node[key] is UNREAD:
            return None
        value, at = node[key], self._at(where, key)
        if not _is_number(value):
            self.r.error("FIELD-TYPE", at, "`%s` must be a number, found %s" % (key, _name(value)))
            return None
        if low is not None and (value <= low if above else value < low):
            self.r.error("RANGE", at, "`%s` is %s; it must be %s %s" % (key, _bar_value(value),
                                                                       "above" if above else "at least", low))
            return None
        if high is not None and value > high:
            self.r.error("RANGE", at, "`%s` is %s; it must be at most %s" % (key, _bar_value(value), high))
            return None
        return value

    def key_id(self, node, key, where, seen, kind, pattern=SLUG):
        if key not in node or node[key] is UNREAD:
            return None
        value, at = node[key], self._at(where, key)
        if not isinstance(value, str) or not pattern.match(value):
            rule = ("a lower-case letter, then lower-case letters, digits and '_'" if pattern is PROPOSAL_FIELD
                    else "lower-case letters, digits, '.', '_' and '-', starting with a letter or digit")
            self.r.error("ID-INVALID", at, "%s %r must use %s" % (kind, value, rule))
            return None
        if value in seen:
            self.r.error("ID-DUPLICATE", at, "%s `%s` is already used at %s" % (kind, value, seen[value]))
            return None
        seen[value] = at
        return value

    def rules(self, node, key, where, required=False, editable=False):
        """[(position, rule)] of a list of root rules that pass the grammar."""
        if key not in node or node[key] is UNREAD:
            return []
        value, at = node[key], self._at(where, key)
        if not isinstance(value, list):
            self.r.error("FIELD-NOT-LIST", at, "`%s` must be a list of rules (write `[%s]` for one)"
                         % (key, value if isinstance(value, str) else "..."))
            return []
        if required and not value:
            self.r.error("FIELD-EMPTY", at, "`%s` needs at least one rule" % key)
        out = []
        for n, rule in enumerate(value):
            if rule is UNREAD:
                continue
            if not isinstance(rule, str):
                self.r.error("FIELD-TYPE", "%s[%d]" % (at, n), "each `%s` rule is text, found %s" % (key, _name(rule)))
                continue
            why = _rule_problem(rule, editable)
            if why:
                self.r.error("PATHS-RULE-INVALID", "%s[%d]" % (at, n), "%s rule %r %s" % (key, rule, why))
                continue
            out.append((n, rule))
        return out

    def location(self, node, key, where):
        value = self.text(node, key, where)
        if value is not None:
            why = _location_problem(value)
            if why:
                self.r.error("PATHS-RULE-INVALID", self._at(where, key), "%s %r %s" % (key, value, why))
                return None
        return value

    def locations(self, node, key, where):
        for n, value in enumerate(self.items(node, key, where)):
            why = _location_problem(value)
            if why:
                self.r.error("PATHS-RULE-INVALID", "%s[%d]" % (self._at(where, key), n), "%s %r %s" % (key, value, why))

    def extras(self, node, where):
        """`covers` and `source` of a part, a metric or a case."""
        self.items(node, "covers", where)
        self.source(node, where)

    def bar_vars(self, text, at, extra=()):
        """Every ${NAME} of a command the attempt is measured or tested with: a knob must be
        owner_only (KNOB-MOVES-BAR), any other name a variable of this field (VAR-UNKNOWN)."""
        allowed = BUILTIN_VARS + RESEARCH_VARS + tuple(extra)
        for name in VAR.findall(text):
            knob = self.knobs.get(name)
            if knob is not None:
                if knob.get("owner_only") is not True:
                    self.r.error("KNOB-MOVES-BAR", at, "knob %s is not owner_only, so a retry could move what "
                                 "the attempt is measured or tested with; mark it `owner_only: true`" % name)
            elif name not in allowed:
                self.r.error("VAR-UNKNOWN", at, "${%s} names no knob and no variable of this field (%s)"
                             % (name, ", ".join(allowed)))

    def resolved_repo(self):
        """The root of repo.path when it can be resolved (section 2.2), else None."""
        if self.repo_root is not None:
            return self.repo_root
        repo = self.part("repo") or {}
        path = repo.get("path", ".")
        if not isinstance(path, str) or _location_problem(path):
            return None
        if os.path.isabs(path):
            return path
        project = _project_root(self.base)
        return os.path.join(project, path) if project else None

    def agent_file(self, rel, at, what):
        """PATHS-OVERLAP for a pack file that reaches agent prompts and a hidden or holdout rule
        hits from the root of repo.path (FR-045)."""
        root = self.resolved_repo()
        if not root:
            return
        full, top = os.path.realpath(os.path.join(self.base, rel)), os.path.realpath(root)
        if not _within(full, top):
            return
        path = os.path.relpath(full, top).replace(os.sep, "/")
        hidden = _quiet_rules((self.part("paths") or {}).get("hidden"))
        holdout = _quiet_rules((self.part("holdout") or {}).get("paths"))
        hit = path_hit(path, hidden + holdout)
        if hit:
            self.r.error("PATHS-OVERLAP", at, "%s %s reaches agent prompts, and the %s rule %s hides it"
                         % (what, path, "hidden" if hit in hidden else "holdout", hit))

    def pack_file(self, node, key, where, what):
        value = self.text(node, key, where)
        at = self._at(where, key)
        if value and self.local_path(value, at, key):
            if self.check_files and not os.path.isfile(os.path.join(self.base, value)):
                self.r.error("FILE-MISSING", at, "%s %s does not exist (paths are relative to the runbook folder)"
                             % (what, value))
            self.agent_file(value, at, what)

    def required(self, node, tpath, where, tables):
        """FIELD-MISSING for every required field of every part, by the mode's tables."""
        if tpath in tables and isinstance(node, dict):
            for key, need in tables[tpath].items():
                if need and key not in node:
                    why = "`%s` is required here" % key
                    if tpath == "" and self.mode and key in MODE_KEYS[self.mode]:
                        why = "`%s` is required in a %s block" % (key, self.mode)
                    self.r.error("FIELD-MISSING", self._at(where, key), why)
            for key, value in node.items():
                self.required(value, "%s.%s" % (tpath, key) if tpath else key, "%s.%s" % (where, key), tables)
        elif tpath in _LIST_OF_MAPS and isinstance(node, list):
            for n, item in enumerate(node):
                self.required(item, tpath + "[]", "%s[%d]" % (where, n), tables)

    # -- the block
    def check_block(self):
        block = self.given
        if not isinstance(block, dict):
            if block is not UNREAD:
                self.r.error("FIELD-TYPE", "research", "`research` must be a mapping, found %s" % _name(block))
            return
        mode = block.get("mode")
        self.mode = mode if isinstance(mode, str) and mode in MODES else None
        tables = _research_tables(self.mode)
        self.b = _shape(block, "", "research", tables, self.r, self.mode, None)
        if self.b is UNREAD:
            return
        self.required(self.b, "", "research", tables)
        if self.mode is None:
            self.word(self.b, "mode", "research", MODES)
        name = self.text(self.b, "defaults", "research")
        if name:
            self.local_path(name, "research.defaults", "defaults")
        for step in (self.repo, self.metrics, self.accept, self.paths, self.fingerprint, self.gates, self.stop,
                     self.budget, self.proposal, self.categories, self.lessons, self.controls, self.holdout,
                     self.repair, self.coupled, self.runtime, self.verifier, self.audit, self.egress):
            step()
        self.number(self.b, "disk", "research", 0, 100000, above=True)
        self.stage_rules()

    def repo(self):
        repo, where = self.part("repo"), "research.repo"
        if repo is None:
            return
        path = self.location(repo, "path", where)
        self.text(repo, "ref", where)
        if path is None and "path" in repo:
            return
        root = self.resolved_repo()
        if root and not _within(os.path.realpath(self.base), os.path.realpath(root)):
            self.r.error("PACK-OUTSIDE-REPO", where + ".path", "the runbook folder %s does not lie inside "
                         "repo.path %s; a pack is committed inside the repository its runs work on"
                         % (self.base, root))

    def metrics(self):
        listed = self.b.get("metrics")
        if not isinstance(listed, list):
            return
        if not listed:
            self.r.error("FIELD-EMPTY", "research.metrics", "an improve block measures at least one metric")
        seen = {}
        for n, metric in enumerate(listed):
            where = "research.metrics[%d]" % n
            if not isinstance(metric, dict):
                continue
            ident = self.key_id(metric, "id", where, seen, "metric id")
            for key in ("description", "unit", "artifact"):
                self.text(metric, key, where)
            extra = METRIC_VARS if "seeds" in metric else ("REPEAT",)
            command = self.text(metric, "command", where)
            if command:
                self.bar_vars(command, where + ".command", extra)
            workdir = self.text(metric, "workdir", where)
            if workdir and self.local_path(workdir, where + ".workdir", "workdir"):
                self.bar_vars(workdir, where + ".workdir", extra)
            parser = self.word(metric, "parser", where, PARSERS)
            for key in ("path", "field"):
                if parser == "markers" and key in metric:
                    self.r.error("FIELD-UNKNOWN", "%s.%s" % (where, key), "`%s` belongs to a json-field metric; a "
                                 "markers metric reads its number from a METRIC: line" % key)
                elif parser == "json-field" and key not in metric:
                    self.r.error("FIELD-MISSING", "%s.%s" % (where, key), "a json-field metric names the `path` "
                                 "its command writes and the `field` that holds the number; `%s` is missing" % key)
            if parser != "markers":
                path = self.text(metric, "path", where)
                if path and self.local_path(path, where + ".path", "path"):
                    self.bar_vars(path, where + ".path", extra)
                self.text(metric, "field", where)
            self.word(metric, "direction", where, DIRECTIONS)
            kind = self.word(metric, "kind", where, METRIC_KINDS)
            self.word(metric, "median", where, MEDIANS)
            repeats = self.whole(metric, "repeats", where, 1, 50)
            min_ok = self.whole(metric, "min_ok", where, 1, 50)
            margin = self.number(metric, "margin", where, 0)
            if kind == "noisy":
                for key in ("repeats", "min_ok", "margin"):
                    if key not in metric:
                        self.r.error("FIELD-MISSING", "%s.%s" % (where, key), "a noisy metric declares repeats, "
                                     "min_ok and margin; %s is missing" % key)
                if repeats is not None and repeats < 2:
                    self.r.error("RANGE", where + ".repeats", "a noisy metric needs at least 2 repeats: one "
                                 "measurement gives no comparison")
                if min_ok is not None and min_ok < 2:
                    self.r.error("RANGE", where + ".min_ok", "a noisy metric needs min_ok of at least 2")
                if margin is not None and margin == 0:
                    self.r.error("RANGE", where + ".margin", "a noisy metric needs a margin above 0; an attempt "
                                 "must beat the champion by more than the noise")
            if min_ok is not None and repeats is not None and min_ok > repeats:
                self.r.error("RANGE", where + ".min_ok", "min_ok %d is above repeats %d" % (min_ok, repeats))
            if "seeds" in metric and metric["seeds"] is not UNREAD:
                seeds = metric["seeds"]
                if not isinstance(seeds, list):
                    self.r.error("FIELD-TYPE", where + ".seeds", "`seeds` is a list of whole numbers, one per repeat")
                elif not all(_is_whole(s) for s in seeds if s is not UNREAD):
                    self.r.error("FIELD-TYPE", where + ".seeds", "each seed is a whole number")
                elif repeats is not None and len(seeds) != repeats:
                    self.r.error("RANGE", where + ".seeds", "seeds lists %d seed(s); it needs one per repeat (%d)"
                                 % (len(seeds), repeats))
            self.whole(metric, "timeout", where, *STAGE_TIMEOUT_RANGE)
            target = self.number(metric, "target", where)
            self.extras(metric, where)
            source = metric.get("source")
            if target is not None and source is not UNREAD:
                kind_of = source.partition(":")[0] if isinstance(source, str) and source_shape(source) else None
                if kind_of not in ("spec", "owner"):
                    self.r.error("TARGET-SOURCE", where + ".source", "metric %s has target %s, and a target's source "
                                 "is spec:<item id> or owner:<decision ref>, never %s"
                                 % (ident or n, _bar_value(target), source if isinstance(source, str) else "missing"))

    def accept(self):
        accept, where = self.part("accept"), "research.accept"
        if accept is None:
            return
        rule = self.word(accept, "rule", where, ACCEPT_RULES)
        self.word(accept, "after_targets", where, AFTER_TARGETS)
        self.extras(accept, where)
        metrics = [m for m in _listed(self.b, "metrics") if isinstance(m, dict)]
        count, targets = len(metrics), len([m for m in metrics if _is_number(m.get("target"))])
        if rule and self.mode:
            fits = {"champion": self.mode == "improve" and count == 1 and targets == 0,
                    "deficit-pareto": self.mode == "improve" and targets >= 1,
                    "repair": self.mode == "repair"}
            if not fits[rule]:
                need = {"champion": "improve mode, exactly one metric and no target",
                        "deficit-pareto": "improve mode and at least one metric with a target",
                        "repair": "repair mode"}[rule]
                fit = [r for r in ACCEPT_RULES if fits[r]]
                self.r.error("ACCEPT-RULE-MISMATCH", where + ".rule", "accept rule %s needs %s; this %s block has %d "
                             "metric(s), %d with a target, which is %s" % (rule, need, self.mode, count, targets,
                                                                           fit[0] if fit else "no accept rule's shape"))
        if "after_targets" in accept and accept["after_targets"] is not UNREAD and not targets:
            self.r.error("ACCEPT-RULE-MISMATCH", where + ".after_targets", "after_targets says what a run does once "
                         "every target is met, and this block has no target")

    def paths(self):
        part, where = self.part("paths"), "research.paths"
        if part is None:
            return
        editable = self.rules(part, "editable", where, required=True, editable=True)
        protected = self.rules(part, "protected", where, required=True)
        hidden = self.rules(part, "hidden", where)
        for key in ("tests", "keep", "outputs"):
            self.rules(part, key, where)
        self.locations(part, "read", where)
        self.extras(part, where)
        holdout = list(enumerate(_quiet_rules((self.part("holdout") or {}).get("paths"))))
        for n, rule in editable:
            for kind, others in (("protected", protected), ("hidden", hidden), ("holdout", holdout)):
                found = [(other, _overlap(rule, other)) for _m, other in others if _overlap(rule, other)]
                if found:
                    self.r.error("PATHS-OVERLAP", "%s.editable[%d]" % (where, n), "editable rule %s %s %s rule %s"
                                 % (rule, found[0][1], kind, found[0][0]))
                    break

    def fingerprint(self):
        part, where = self.part("fingerprint"), "research.fingerprint"
        if part is None:
            return
        tools = part.get("tools")
        if "tools" in part and tools is not UNREAD:
            if not isinstance(tools, list):
                self.r.error("FIELD-NOT-LIST", where + ".tools",
                             "`tools` must be a list of command names (it may be [])")
            else:
                for n, tool in enumerate(tools):
                    if tool is not UNREAD and (not isinstance(tool, str) or not TOOL.match(tool)):
                        self.r.error("TOOL-INVALID", "%s.tools[%d]" % (where, n), "tool %r is not a bare command name "
                                     "(letters, digits, '.', '_', '+' and '-')" % (tool,))
        self.locations(part, "shared", where)
        self.extras(part, where)

    def gates(self):
        listed = self.b.get("gates")
        if not isinstance(listed, list):
            return
        if not listed:
            self.r.error("FIELD-EMPTY", "research.gates", "the gate chain needs at least one stage")
        for n, gate in enumerate(listed):
            where = "research.gates[%d]" % n
            if not isinstance(gate, dict):
                continue
            stage = self.text(gate, "stage", where)
            if stage and stage not in self.stages:
                self.r.error("STAGE-UNKNOWN", where + ".stage", "`%s` is not a stage of this runbook" % stage)
            self.flag(gate, "heavy", where)
            self.whole(gate, "lock_timeout", where, *STAGE_TIMEOUT_RANGE)

    def stop(self):
        part, where = self.part("stop"), "research.stop"
        if part is not None:
            self.whole(part, "rounds", where, 1, 100)
            self.whole(part, "slots", where, 1, 16)
            self.extras(part, where)

    def budget(self):
        part, where = self.part("budget"), "research.budget"
        if part is not None:
            self.number(part, "usd", where, 0, 1000000, above=True)
            self.whole(part, "tokens", where, 1000, 10 ** 12)
            self.whole(part, "wall", where, 60, 30 * 24 * 3600)
            self.extras(part, where)

    def proposal(self):
        listed = self.b.get("proposal")
        if not isinstance(listed, list):
            return
        seen = {}
        base = PROPOSAL_BASE.get(self.mode) or tuple(sorted(set(PROPOSAL_BASE["improve"] + PROPOSAL_BASE["repair"])))
        for n, field in enumerate(listed):
            where = "research.proposal[%d]" % n
            if not isinstance(field, dict):
                continue
            name = self.key_id(field, "field", where, seen, "proposal field", pattern=PROPOSAL_FIELD)
            if name and name in base:
                self.r.error("PROPOSAL-FIELD-RESERVED", where + ".field", "`%s` is a base field of the proposal (%s); "
                             "a pack field needs a name of its own" % (name, ", ".join(base)))
            kind = self.word(field, "type", where, PROPOSAL_TYPES)
            self.flag(field, "required", where)
            if kind == "enum":
                if "values" not in field:
                    self.r.error("FIELD-MISSING", where + ".values", "an enum field lists its `values`")
                elif field["values"] is not UNREAD:
                    if isinstance(field["values"], list) and not field["values"]:
                        self.r.error("FIELD-EMPTY", where + ".values", "`values` needs at least one word")
                    else:
                        self.items(field, "values", where)
            elif kind is not None and "values" in field:
                self.r.error("FIELD-UNKNOWN", where + ".values", "`values` belongs to an enum field")
            if "max" in field:
                if kind in PROPOSAL_MAX:
                    self.whole(field, "max", where, 1, 100000)
                elif kind is not None:
                    self.r.error("FIELD-UNKNOWN", where + ".max", "`max` applies to str, text, list and paths fields")
            self.text(field, "description", where)

    def categories(self):
        listed = self.b.get("categories")
        if not isinstance(listed, list):
            return
        if not listed:
            self.r.error("FIELD-EMPTY", "research.categories", "`categories` needs at least one hypothesis category")
        seen = {}
        for n, category in enumerate(listed):
            where = "research.categories[%d]" % n
            if isinstance(category, dict):
                self.key_id(category, "id", where, seen, "category id")
                self.text(category, "text", where)

    def lessons(self):
        part, where = self.part("lessons"), "research.lessons"
        if part is not None:
            self.pack_file(part, "file", where, "the lessons file")
            self.whole(part, "note_chars", where, 40, 2000)

    def gate_ids(self):
        return [g["stage"] for g in _listed(self.b, "gates") if isinstance(g, dict) and isinstance(g.get("stage"), str)]

    def controls(self):
        part, where = self.part("controls"), "research.controls"
        if part is None:
            return
        self.whole(part, "noop_rounds", where, 2, 10)
        self.flag(part, "random", where)
        if part.get("random") is True:
            for key in ("mutate", "syntax"):
                if key not in part:
                    self.r.error("FIELD-MISSING", "%s.%s" % (where, key), "the random-edit control (`random: true`) "
                                 "needs `mutate` and `syntax`; `%s` is missing" % key)
        script = self.text(part, "mutate", where)
        if script:
            if VAR.search(script):
                self.r.error("PLUGIN-SCRIPT-VARIABLE", where + ".mutate", "`mutate` is a fixed script path in the "
                             "runbook folder, found %r" % script)
            elif self.local_path(script, where + ".mutate", "mutate") and self.check_files:
                full = os.path.join(self.base, script)
                if not os.path.isfile(full):
                    self.r.error("PLUGIN-MISSING", where + ".mutate", "mutation script %s does not exist (paths are "
                                 "relative to the runbook folder)" % script)
                elif not os.access(full, os.X_OK):
                    self.r.error("PLUGIN-NOT-EXECUTABLE", where + ".mutate", "mutation script %s is not executable; "
                                 "run chmod +x %s" % (script, script))
        syntax = self.text(part, "syntax", where)
        if syntax:
            if syntax not in self.stages:
                self.r.error("STAGE-UNKNOWN", where + ".syntax", "`%s` is not a stage of this runbook" % syntax)
            elif syntax not in self.gate_ids():
                self.r.error("STAGE-ROLE", where + ".syntax", "controls.syntax names stage %s, which is not a gate "
                             "stage (research.gates)" % syntax)
        self.whole(part, "seed", where, 0, 2 ** 31)
        self.extras(part, where)

    def holdout(self):
        part, where = self.part("holdout"), "research.holdout"
        if part is None:
            return
        self.items(part, "stages", where)
        for n, stage in enumerate(_listed(part, "stages")):
            if isinstance(stage, str) and stage.strip() and stage not in self.stages:
                self.r.error("STAGE-UNKNOWN", "%s.stages[%d]" % (where, n),
                             "`%s` is not a stage of this runbook" % stage)
        self.rules(part, "paths", where)
        self.extras(part, where)

    def repair(self):
        part, where = self.part("repair"), "research.repair"
        if part is None:
            return
        prepare = self.text(part, "prepare", where)
        if prepare and prepare not in self.stages:
            self.r.error("STAGE-UNKNOWN", where + ".prepare", "`%s` is not a stage of this runbook" % prepare)
        p2p = part.get("pass_to_pass")
        if isinstance(p2p, dict):
            at = where + ".pass_to_pass"
            command = self.text(p2p, "command", at)
            if command:
                self.bar_vars(command, at + ".command")
            self.whole(p2p, "min_tests", at, 1, 100000)
            self.whole(p2p, "timeout", at, *STAGE_TIMEOUT_RANGE)
            self.extras(p2p, at)
        cases = part.get("cases")
        if not isinstance(cases, list):
            return
        if not cases:
            self.r.error("FIELD-EMPTY", where + ".cases", "a repair block fixes at least one case")
        seen = {}
        for n, case in enumerate(cases):
            at = "%s.cases[%d]" % (where, n)
            if not isinstance(case, dict):
                continue
            self.key_id(case, "id", at, seen, "case id")
            self.pack_file(case, "bug_report", at, "the bug report")
            base = case.get("base")
            if "base" in case and base is not UNREAD and not (isinstance(base, str) and COMMIT.match(base)):
                self.r.error("BASE-NOT-COMMIT", at + ".base", "base %s is not a 40-digit commit id; a branch or "
                             "tag can move after the contract is frozen" % json.dumps(base, default=str))
            f2p = case.get("fail_to_pass")
            if isinstance(f2p, dict):
                fat = at + ".fail_to_pass"
                origin = self.word(f2p, "origin", fat, F2P_ORIGINS)
                command = self.text(f2p, "command", fat)
                if command:
                    self.bar_vars(command, fat + ".command", F2P_VARS)
                if origin == "store" and "tests" not in f2p:
                    self.r.error("FIELD-MISSING", fat + ".tests", "a store case names the TEST names its reproducer "
                                 "turns from FAIL on the base to PASS on the fix")
                self.items(f2p, "tests", fat)
                self.whole(f2p, "timeout", fat, *STAGE_TIMEOUT_RANGE)
                self.whole(f2p, "repeats", fat, 1, 20)
            self.rules(case, "suspect_files", at)
            self.extras(case, at)

    def coupled(self):
        listed = self.b.get("coupled")
        if not isinstance(listed, list):
            return
        seen = {}
        for n, group in enumerate(listed):
            where = "research.coupled[%d]" % n
            if not isinstance(group, dict):
                continue
            self.key_id(group, "id", where, seen, "group id")
            self.rules(group, "files", where)
            if isinstance(group.get("files"), list) and len(group["files"]) < 2:
                self.r.error("RANGE", where + ".files", "a coupled group names at least two files that change together")
            self.text(group, "note", where)

    def runtime(self):
        part, where = self.part("runtime"), "research.runtime"
        if part is None:
            return
        if isinstance(part.get("timeouts"), dict):
            for role in part["timeouts"]:
                self.whole(part["timeouts"], role, where + ".timeouts", *STAGE_TIMEOUT_RANGE)
        if isinstance(part.get("effort"), dict):
            for role in part["effort"]:
                self.word(part["effort"], role, where + ".effort", EFFORTS)
        self.whole(part, "retries", where, 0, 10)

    def verifier(self):
        part, where = self.part("verifier"), "research.verifier"
        if part is not None:
            self.whole(part, "timeout", where, *STAGE_TIMEOUT_RANGE)
            self.whole(part, "rerun_timeout", where, *STAGE_TIMEOUT_RANGE)

    def audit(self):
        part, where = self.part("audit"), "research.audit"
        if part is None:
            return
        self.text(part, "hints", where)
        self.items(part, "assert_tokens", where)
        self.rules(part, "build_files", where)
        self.items(part, "steering", where)

    def egress(self):
        part, where = self.part("egress"), "research.egress"
        if part is None or "allow" not in part or part["allow"] is UNREAD:
            return
        allow = part["allow"]
        if not isinstance(allow, list):
            self.r.error("FIELD-NOT-LIST", where + ".allow", "`allow` must be a list of host names")
            return
        for n, host in enumerate(allow):
            if host is not UNREAD and (not isinstance(host, str) or not HOST.match(host)):
                self.r.error("HOST-INVALID", "%s.allow[%d]" % (where, n), "%r is not a host name: lower-case letters, "
                             "digits, '-' and '.', with no scheme, port or path" % (host,))

    # -- the stages of a research runbook (section 2.2)
    def stage_rules(self):
        gates = [(n, g["stage"]) for n, g in enumerate(_listed(self.b, "gates"))
                 if isinstance(g, dict) and isinstance(g.get("stage"), str)]
        holdout = [(n, s) for n, s in enumerate(_listed(self.part("holdout") or {}, "stages")) if isinstance(s, str)]
        prepare = (self.part("repair") or {}).get("prepare")
        prepare = prepare if isinstance(prepare, str) else None
        named = [("research.gates[%d].stage" % n, s, "gate") for n, s in gates]
        named += [("research.holdout.stages[%d]" % n, s, "holdout") for n, s in holdout]
        if prepare:
            named.append(("research.repair.prepare", prepare, "prepare"))
        role = {}
        for where, sid, kind in named:
            if sid not in self.stages:
                continue
            stage = self.stages[sid][1]
            if sid in role and role[sid][0] != kind:
                self.r.error("STAGE-ROLE", where, "stage %s is already a %s stage (%s); a stage has one research role"
                             % (sid, role[sid][0], role[sid][1]))
                continue
            role.setdefault(sid, (kind, where))
            if stage.get("recovery") is True:
                self.r.error("STAGE-ROLE", where, "stage %s is a recovery stage: it runs only when a fail case sends "
                             "to it, never as a research stage" % sid)
            elif "run" not in stage:
                self.r.error("STAGE-ROLE", where, "stage %s runs no command, so it cannot be a %s stage" % (sid, kind))
        for n, stage in enumerate(_listed(self.data, "stages")):
            if not isinstance(stage, dict):
                continue
            where, sid = "stages[%d]" % n, stage.get("id")
            mine = role.get(sid) if isinstance(sid, str) else None
            if "run" in stage and "timeout" not in stage:
                self.r.error("TIMEOUT-MISSING", where + ".timeout", "stage %s runs a command, and every command of a "
                             "research runbook has a timeout" % sid)
            if stage.get("owner_gate") is not None and ("run" in stage or mine):
                self.r.error("STAGE-ROLE", where + ".owner_gate", "an owner gate is a stage of its own, with no `run` "
                             "and no research role; the gate chain never waits for a person")
            if "run" in stage and stage.get("recovery") is not True and mine is None:
                self.r.error("STAGE-UNUSED", where, "stage %s runs a command that no research field names "
                             "(research.gates, research.holdout.stages, research.repair.prepare), so the engine "
                             "would never run it" % sid)
            for f, fail in enumerate(_listed(stage, "fails")):
                if isinstance(fail, dict) and fail.get("then") == "ask-owner":
                    self.r.error("THEN-ASK-OWNER", "%s.fails[%d].then" % (where, f), "the gate chain never waits for "
                                 "a person, so a fail case of a research runbook answers retry, stop or "
                                 "{run: <recovery stage id>}")
            if mine and mine[0] in ("gate", "holdout"):
                for c, chk in enumerate(_listed(stage, "checks")):
                    for key in ("value", "path"):
                        value = chk.get(key) if isinstance(chk, dict) else None
                        for name in VAR.findall(value) if isinstance(value, str) else []:
                            knob = self.knobs.get(name)
                            if knob is not None and knob.get("owner_only") is not True:
                                self.r.error("KNOB-MOVES-BAR", "%s.checks[%d].%s" % (where, c, key), "knob %s is not "
                                             "owner_only, so a retry could move the %s this %s check is held to; mark "
                                             "it `owner_only: true`" % (name, "threshold" if key == "value" else "file",
                                                                        mine[0]))
        gate_ids = [s for _n, s in gates]
        hold_ids = [s for _n, s in holdout]
        for kind, ids, allowed_before in (("gate", gate_ids, lambda i: gate_ids[:i]),
                                          ("holdout", hold_ids, lambda i: gate_ids + hold_ids[:i])):
            for i, sid in enumerate(ids):
                if sid not in self.stages:
                    continue
                n, stage = self.stages[sid]
                allowed = allowed_before(i) + ([prepare] if prepare else [])
                for k, need in enumerate(_listed(stage, "needs")):
                    if isinstance(need, str) and need not in allowed:
                        self.r.error("GATES-ORDER", "stages[%d].needs[%d]" % (n, k), "%s stage %s needs %s, which is "
                                     "not %s or the prepare stage" % (kind, sid, need,
                                                                     "an earlier gate stage" if kind == "gate" else
                                                                     "a gate stage, an earlier holdout stage"))
        if prepare in self.stages:
            n, stage = self.stages[prepare]
            if _listed(stage, "needs"):
                self.r.error("GATES-ORDER", "stages[%d].needs" % n, "the prepare stage %s runs first, on the case "
                             "base, so it needs no other stage" % prepare)


def _bar_value(value):
    """A number as a message writes it: a whole number without `.0`."""
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value)


def research_fields():
    """[{"path", "required": {"improve": bool, "repair": bool}}] for every field of the research
    block, read from the tables: a field is required in a mode when its own flag is and every part
    above it is required in that mode. The `covers` of each COVERS_REQUIRED item is reported
    required in that list's mode, since COVERS-NONE refuses a metric or a case without one."""
    order, required = [], {}

    def walk(tables, mode, tpath, prefix, up):
        for key, flag in tables[tpath].items():
            sub = "%s.%s" % (tpath, key) if tpath else key
            path = "%s.%s" % (prefix, key)
            need = bool(up and flag)
            if key == "covers" and tpath.endswith("[]") and tpath[:-2] in COVERS_REQUIRED:
                need = bool(up)
            if path not in required:
                order.append(path)
                required[path] = dict((m, False) for m in MODES)
            required[path][mode] = need
            if sub in tables:
                walk(tables, mode, sub, path, need)
            elif sub in _LIST_OF_MAPS:
                walk(tables, mode, sub + "[]", path + "[]", need)

    for mode in MODES:
        walk(_research_tables(mode), mode, "", "research", True)
    return [{"path": path, "required": dict(required[path])} for path in order]


# ------------------------------------------------------------------ the two path matchers
_GLOBS = {}


def _glob(pattern, one_part):
    """The regular expression of a shell pattern, read the way fnmatch.translate reads `*`, `?`
    and a `[...]` set; with one_part, `*` and `?` stay inside one part of a path."""
    star, one = ("[^/]*", "[^/]") if one_part else (".*", ".")
    i, n, out = 0, len(pattern), []
    while i < n:
        c = pattern[i]
        i += 1
        if c == "*":
            out.append(star)
            while i < n and pattern[i] == "*":
                i += 1
        elif c == "?":
            out.append(one)
        elif c == "[":
            j = i
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append("\\[")
            else:
                stuff = pattern[i:j].replace("\\", "\\\\")
                i = j + 1
                if stuff.startswith("!"):
                    stuff = "^" + stuff[1:]
                elif stuff[:1] in ("^", "["):
                    stuff = "\\" + stuff
                out.append("[%s]" % re.sub(r"([&~|])", r"\\\1", stuff))
        else:
            out.append(re.escape(c))
    return "(?s:%s)\\Z" % "".join(out)


def _fnmatch(name, pattern, one_part=False):
    key = (pattern, one_part)
    rx = _GLOBS.get(key)
    if rx is None:
        try:
            rx = re.compile(_glob(pattern, one_part))
        except re.error:
            rx = re.compile("(?!)")               # a set no character can match matches nothing
        _GLOBS[key] = rx
    return rx.match(name) is not None


def path_hit(path, rules):
    """The rule of `rules` that hits `path`, or None: barrier.path_is_protected's match
    (alpaca/barrier.py:126-154) built on re, so the partner kit can carry it. Case does not
    matter; a folder rule (`x/`) hits that folder from the root, and when it names one folder, a
    folder of that name at any depth; a bare name hits any part of the path; a rule with an inner
    slash is a path from the root, where `*` also spans a `/`."""
    p = path.replace(os.sep, "/")
    if p.startswith("./"):
        p = p[2:]
    low = p.lower()
    parts = low.split("/")
    for rule in rules:
        r = rule.replace(os.sep, "/")
        if r.startswith("./"):
            r = r[2:]
        rl = r.lower()
        if rl.endswith("/"):
            body = rl.rstrip("/")
            if low == body or low.startswith(body + "/") or _fnmatch(low, body):
                return rule
            if "/" not in body and any(_fnmatch(c, body) for c in parts[:-1]):
                return rule
        elif "/" in rl:
            if _fnmatch(low, rl):
                return rule
        elif any(_fnmatch(c, rl) for c in parts):
            return rule
    return None


def path_admitted(path, rules):
    """The editable rule of `rules` that admits `path`, or None: the strict matcher. Every rule is
    read from the root and keeps case, `*` and `?` match within one part, a folder rule (`src/`)
    admits the folder and everything under it, and any other rule admits exactly one path. So
    `src/` admits neither `tests/src/x` nor `SRC/x` (design/research-engine/gap-map.md:79)."""
    p = path.replace(os.sep, "/")
    while p.startswith("./"):
        p = p[2:]
    parts = p.split("/")
    for rule in rules:
        r = rule.replace(os.sep, "/")
        while r.startswith("./"):
            r = r[2:]
        body = r.rstrip("/")
        if not body:
            continue
        want = body.split("/")
        if len(parts) < len(want) or (not r.endswith("/") and len(parts) != len(want)):
            continue
        if all(_fnmatch(parts[k], want[k], one_part=True) for k in range(len(want))):
            return rule
    return None


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
#: from spec-kit to OpenSpec keeps its rows. SC-nnn and EC-nnn stay required items, FR-nnn an
#: optional one. An EC-nnn id is kept only in the shape the move writes (openspec_alias).
_ALIAS = re.compile(r"^((?:SC|FR|EC)-\d+)\b\s*[:.)-]?\s*(.*)$")
_REQUIRED_ALIAS = ("SC-", "EC-")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")


def scenario_alias(name):
    """(id, rest) when an OpenSpec scenario name starts with a spec-kit id, else (None, name)."""
    m = _ALIAS.match(name.strip())
    if m:
        return m.group(1), m.group(2).strip()
    return None, name.strip()


def openspec_alias(requirement, name):
    """The spec-kit id an OpenSpec scenario named `name` under `requirement` keeps, or None. An
    SC-nnn or FR-nnn at the start of the name is kept under any requirement. An EC-nnn is kept only
    in the shape the move from spec-kit writes, a requirement named by that same id (`### Requirement:
    EC-001` with `#### Scenario: EC-001`): OpenSpec reads the same in both formats, so a scenario
    that only starts with an EC id (`#### Scenario: EC-001 port taken` under `### Requirement:
    Startup`) stays the scenario `Startup/EC-001 port taken`."""
    alias, _rest = scenario_alias(name)
    if alias and alias.startswith("EC-") and _norm(str(requirement or "")) != _norm(alias):
        return None
    return alias


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
            alias = openspec_alias(requirement, name)
            removed = section.startswith("REMOVED")
            current = {"id": "%s/%s" % (capability, bare) if capability else bare, "bare": bare,
                       "capability": capability, "kind": alias[:2] if alias else "scenario",
                       "text": name, "required": not removed and (alias is None or alias.startswith(_REQUIRED_ALIAS)),
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
def _covering(data, block=None):
    """[(covers entry, who, where)] for every check, fail case (format 2) and owner gate. who is
    the check id, "fail:<stage id>/<fail id>" for a fail case, or "gate:<stage id>" for an owner
    gate. For a format 3 runbook, `block` is its expanded research block, and the research
    coverers of runbook-format-3.md section 9 follow: `metric:<id>`, `case:<id>`,
    `research:pass-to-pass` and `research:<part>`."""
    out = []
    fmt = runbook_format(data)

    def named(entry):
        # a format 3 file's <FILL:...> placeholder is FIELD-UNFILLED only (section 4 rule 2)
        return isinstance(entry, str) and entry.strip() and not (fmt == RESEARCH_FORMAT and _unfilled(entry))

    stages = data.get("stages") if isinstance(data.get("stages"), list) else []
    for n, stage in enumerate(stages):
        if not isinstance(stage, dict):
            continue
        checks = stage.get("checks") if isinstance(stage.get("checks"), list) else []
        for c, chk in enumerate(checks):
            if isinstance(chk, dict) and isinstance(chk.get("covers"), list):
                for k, entry in enumerate(chk["covers"]):
                    if named(entry):
                        out.append((entry, str(chk.get("id")), "stages[%d].checks[%d].covers[%d]" % (n, c, k)))
        fails = stage.get("fails") if fmt >= 2 and isinstance(stage.get("fails"), list) else []
        for f, fail in enumerate(fails):
            # only a fail case that detects the failure covers (COVERS-NO-DETECT otherwise)
            if isinstance(fail, dict) and isinstance(fail.get("covers"), list) and isinstance(fail.get("detect"), dict):
                for k, entry in enumerate(fail["covers"]):
                    if named(entry):
                        out.append((entry, "fail:%s/%s" % (stage.get("id"), fail.get("id")),
                                    "stages[%d].fails[%d].covers[%d]" % (n, f, k)))
        gate = stage.get("owner_gate")
        if isinstance(gate, dict) and isinstance(gate.get("covers"), list):
            for k, entry in enumerate(gate["covers"]):
                if named(entry):
                    out.append((entry, "gate:%s" % stage.get("id"), "stages[%d].owner_gate.covers[%d]" % (n, k)))
    if fmt == RESEARCH_FORMAT and isinstance(block, dict):
        out += _research_covering(data, block)
    return out


def _research_parts(data, block):
    """[(who, node, where)] for every research coverer of a format 3 runbook (section 9), read
    from its expanded block: each metric (`metric:<id>`), each repair case (`case:<id>`), the
    pass-to-pass suite (`research:pass-to-pass`) and the parts accept, stop, budget, paths,
    fingerprint, controls and holdout (`research:<part>`). `where` is the node's location where it
    is written: `covers` and `source` are written only in the runbook itself, so a metric or case
    the runbook holds is located at its position there, and one only the defaults file holds at
    its position in that file (`<file>:research.metrics[<n>]`); the defaults layer comes first in
    the merge, so that position is its position in the block."""
    out = []
    own = data.get("research") if isinstance(data.get("research"), dict) else {}
    defaults = block.get("defaults") if isinstance(block.get("defaults"), str) else "defaults"

    def position(items, key):
        for n, item in enumerate(items if isinstance(items, list) else []):
            if _item_key(item, "id") == key:
                return n
        return None

    def items(kind, listed, own_listed, path):
        for n, item in enumerate(listed):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                at = position(own_listed, item["id"])
                where = ("research.%s[%d]" % (path, at) if at is not None
                         else "%s:research.%s[%d]" % (defaults, path, n))
                out.append(("%s:%s" % (kind, item["id"]), item, where))

    items("metric", _listed(block, "metrics"), own.get("metrics"), "metrics")
    repair = block.get("repair") if isinstance(block.get("repair"), dict) else {}
    own_repair = own.get("repair") if isinstance(own.get("repair"), dict) else {}
    items("case", _listed(repair, "cases"), own_repair.get("cases"), "repair.cases")
    if isinstance(repair.get("pass_to_pass"), dict):
        out.append(("research:pass-to-pass", repair["pass_to_pass"], "research.repair.pass_to_pass"))
    for part in ("accept", "stop", "budget", "paths", "fingerprint", "controls", "holdout"):
        if isinstance(block.get(part), dict):
            out.append(("research:%s" % part, block[part], "research.%s" % part))
    return out


def _research_covering(data, block):
    """The research coverers of a format 3 runbook (section 9), read from its expanded block. A
    `<FILL:...>` placeholder entry is FIELD-UNFILLED only, so it covers nothing and names nothing."""
    out = []
    for who, node, where in _research_parts(data, block):
        covers = node.get("covers")
        if isinstance(covers, list):
            for k, entry in enumerate(covers):
                if isinstance(entry, str) and entry.strip() and not _unfilled(entry):
                    out.append((entry, who, "%s.covers[%d]" % (where, k)))
    return out


def coverage(data, spec, report, block=None):
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
    named, unresolved = {}, set()        # who: the items its entries name; who with an entry naming nothing
    for entry, who, where in _covering(data, block):
        found = index.get(_norm(entry), [])
        if not found:
            near = difflib.get_close_matches(_norm(entry), list(index), n=1, cutoff=0.6)
            hint = "; closest is %r" % next(i["id"] for i in index[near[0]]) if near else ""
            report.error("COVERS-UNKNOWN", where, "covers %r names nothing in %s%s" % (entry, spec["path"], hint))
            unresolved.add(who)
            continue
        if len({i["id"] for i in found}) > 1:
            report.error("COVERS-AMBIGUOUS", where, "covers %r matches %s; write the capability prefix"
                         % (entry, ", ".join(sorted(i["id"] for i in found))))
            unresolved.add(who)
            continue
        ident = found[0]["id"]
        named.setdefault(who, []).append(ident)
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
    lead = "no runbook check covers"
    if fmt == RESEARCH_FORMAT:
        # the research coverers of section 9 join format 2's
        lead = "no metric, repair case, research part, check, fail case or owner gate covers"
        fix = ("the metric that measures it, the repair case whose bug it describes, or the research part "
               "that shows it (accept, paths, fingerprint, stop, budget, controls, holdout or "
               "repair.pass_to_pass), of the check that shows it or of the fail case that detects and answers "
               "it, or to an owner gate when only a person can judge it")
    for item in required:
        if item["id"] not in covered:
            missing.append(item["id"])
            report.error("SPEC-UNCOVERED", item["id"], "%s %s (%s); add it to the `covers` list of %s"
                         % (lead, item["id"], item["text"][:120], fix))
    if fmt == RESEARCH_FORMAT and isinstance(block, dict):
        # every metric and every repair case traces to a required spec item (section 9, rule 4)
        need = {i["id"] for i in required}
        for who, node, where in _research_parts(data, block):
            kind = who.split(":", 1)[0]
            if kind not in ("metric", "case") or who in unresolved:
                continue                    # an entry naming nothing is COVERS-UNKNOWN already
            covers = node.get("covers")
            if covers is not None and not isinstance(covers, list):
                continue                    # FIELD-TYPE, FIELD-NOT-LIST or FIELD-UNFILLED already
            if isinstance(covers, list) and any(_unfilled(e) or not isinstance(e, str) or not e.strip()
                                                for e in covers):
                continue                    # a placeholder is FIELD-UNFILLED only (section 4 rule 2), a
                #                             blank or non-text entry has its own code already
            if any(ident in need for ident in named.get(who, [])):
                continue
            thing = "metric" if kind == "metric" else "repair case"
            shown = ", ".join(named.get(who, []))
            report.error("COVERS-NONE", where + ".covers", "%s %s covers no required spec item%s; list in its "
                         "`covers` the success criterion or edge case of %s it %s, so nothing the spec does not "
                         "ask for enters the accept rule" % (
                             thing, who.split(":", 1)[1],
                             " (it names only %s)" % shown if shown else
                             " (its `covers` is empty)" if isinstance(covers, list) else " (it has no `covers`)",
                             spec["path"], "measures" if kind == "metric" else "fixes"))
    for item in items:
        if not item["required"] and item["kind"] == "FR" and item["id"] not in covered:
            report.warn("FR-UNCOVERED", item["id"], "no check covers %s (a warning: only success criteria "
                        "%smust be covered)" % (item["id"], "and edge cases " if fmt >= 2 else ""))
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
    # a format 3 runbook's coverers include its research block, read after the merge
    block = expand_research(data, base)[0] if fmt == RESEARCH_FORMAT else None
    target = spec_path
    if target is None and isinstance(data.get("spec"), str) and data["spec"].strip():
        target = os.path.join(base, data["spec"])
    if spec is not None:
        items = spec_items(spec, fmt)
        result["spec"] = {"path": spec["path"], "format": spec["format"], "items": len(items),
                          "required": sum(1 for i in items if i["required"])}
        result["coverage"] = coverage(data, spec, report, block=block)
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
            result["coverage"] = coverage(data, spec, report, block=block)
    elif _covering(data, block):
        report.warn("COVERS-WITHOUT-SPEC", "spec", "checks name `covers` entries but no spec was given; "
                    "pass --spec or set `spec:` to check them")
    if result["coverage"] is not None and check_files:
        for where, note in _note_sources(data, block):
            if not _note_found(base, note):
                report.warn("SOURCE-MISSING", where, "source note:%s names a note that is not in the runbook "
                            "folder or a folder above it, up to the project root (the folder that holds "
                            "project.yaml)" % note)
    code = verdict.FAIL if report.errors else verdict.PASS
    result.update(verdict=verdict.name_of(code), code=code, errors=report.errors, warnings=report.warnings)
    return result


def _note_sources(data, block=None):
    """[(where, note path)] for every well-formed `source: note:<path>` of a format 2 runbook. For
    a format 3 runbook, `block` is its expanded research block, and the sources of its metrics,
    repair cases and parts follow, each located where the runbook writes it."""
    fmt = runbook_format(data)
    if fmt < 2:
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
    if fmt == RESEARCH_FORMAT and isinstance(block, dict):
        for _who, node, where in _research_parts(data, block):
            take(node, where)
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
    stage); on a recovery stage it ends FAIL, since a recovery stage that fails ends the failed
    stage with FAIL. With no fail case recognized, the retry rule decides exactly as next_attempt
    does."""
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
        if isinstance(then, dict) and isinstance(then.get("run"), str) and stage.get("recovery") is True:
            return {"retry": False, "knobs": dict(knobs), "reason": "%s, but %s is a recovery stage: a "
                    "recovery stage that fails ends the failed stage with FAIL and never runs another "
                    "recovery stage (%s)" % (why, stage.get("id"), then["run"]),
                    "run": None, "end": verdict.FAIL, "fail": ident}
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


def bar_parts(data, block=None):
    """What each part of a loaded runbook asks, written as intake writes the `bar` of a row: every
    check (`<stage>/<check>: <type> <what> <op> <value>`, a knob's value put in and the knob named,
    owner-only marked), every fail case (`fail <stage>/<id>: detect <...> then <...>`, or `when
    <text>` without a detect) and every owner gate (`owner approves: <approve text>`). For a format
    3 runbook, `block` is its expanded research block (the checker's, or the one the contract
    loader returned), and every research coverer of section 9 follows with the bar of
    runbook-format-3.md section 14.3 point 1: `metric:<id>` (kind "metric"), `case:<id>` (kind
    "case"), `research:pass-to-pass` and `research:<part>` (kind "research"), each with "stage"
    None, since the research engine runs it and no stage does. The bar reads the values from the
    block, so a value the defaults file or the engine layer set is shown too.

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
    if runbook_format(data) == RESEARCH_FORMAT and isinstance(block, dict):
        # the research parts live here, never in a top-level helper: the partner kit leaves the bar
        # out and carries every other top-level definition (runbook-format-3.md section 13.3)
        def shown(value):
            return _bar_shown(value, quote=False)

        def rules(value):
            listed = [shown(v) for v in value] if isinstance(value, list) else []
            return ", ".join(listed) if listed else "none"

        def got(node, key):
            return node.get(key) if isinstance(node, dict) else None

        metrics = _listed(block, "metrics")
        accept = block.get("accept") if isinstance(block.get("accept"), dict) else {}
        repair_mode = block.get("mode") == "repair"
        for who, node, _where in _research_parts(data, block):
            kind, name = who.split(":", 1)
            if kind == "metric":
                lower = node.get("direction") == "lower"
                bar = ("metric %s: %s is better, %s; %s, median (%s) of %s repeats, at least %s ok; beats the "
                       "champion by more than %s %s" % (
                           name, shown(node.get("direction")), shown(node.get("unit")), shown(node.get("kind")),
                           shown(node.get("median")), shown(node.get("repeats")), shown(node.get("min_ok")),
                           shown(node.get("margin")), shown(node.get("unit"))))
                if _is_number(node.get("target")):
                    bar += "; target %s %s (owner only)" % ("<=" if lower else ">=", shown(node["target"]))
            elif kind == "case":
                f2p = node.get("fail_to_pass") if isinstance(node.get("fail_to_pass"), dict) else {}
                tests = f2p.get("tests") if isinstance(f2p.get("tests"), list) and f2p.get("tests") else None
                base = node.get("base")
                bar = "repair case %s: %s FAIL on %s and PASS on the fix, %s run(s) each" % (
                    name, rules(tests) if tests else "the generated reproducer",
                    "base %s" % shown(base) if isinstance(base, str) and base else "the run base",
                    shown(f2p.get("repeats")))
            elif name == "pass-to-pass":
                bar = "pass-to-pass: every test that passes on the base still passes, at least %s tests" % (
                    shown(node.get("min_tests")))
                kind = "research"
            else:
                kind = "research"
                if name == "accept":
                    bar = "accept %s" % shown(node.get("rule"))
                    if node.get("after_targets") is not None:
                        bar += ", after every target %s" % shown(node["after_targets"])
                elif name == "stop":
                    if repair_mode:
                        when = "when every case is fixed or after"
                    elif accept.get("after_targets") == "stop" and any(_is_number(got(m, "target")) for m in metrics):
                        when = "when every target is met or after"
                    else:
                        when = "after"
                    bar = "stop %s %s rounds of %s slots; at %s the run halts for the owner" % (
                        when, shown(node.get("rounds")), shown(node.get("slots")), shown(node.get("rounds")))
                elif name == "budget":
                    caps = ["%s %s" % (shown(node[key]), unit) for key, unit in
                            (("usd", "USD"), ("tokens", "tokens"), ("wall", "s")) if node.get(key) is not None]
                    bar = ("budget %s; past it the run pauses for the owner" % ", ".join(caps) if caps
                           else "budget: no cap set, the run is uncapped")
                elif name == "paths":
                    bar = ("editable %s; protected %s; a change outside the editable rules or to a protected path "
                           "breaks the attempt" % (rules(node.get("editable")), rules(node.get("protected"))))
                elif name == "fingerprint":
                    bar = "fingerprint tools %s" % rules(node.get("tools"))
                    if isinstance(node.get("shared"), list) and node["shared"]:
                        bar += ", shared %s" % rules(node["shared"])
                    bar += " before and after each agent and the eval"
                elif name == "controls":
                    bar = "no-op control for %s rounds before a real run" % shown(node.get("noop_rounds"))
                    if node.get("random") is True:
                        bar += ", and the random-edit control"
                    bar += "; an accepted control attempt fails the control run"
                else:                                   # holdout
                    stages_named = node.get("stages") if isinstance(node.get("stages"), list) else []
                    bar = ("holdout stages %s run on the final champion only" % rules(stages_named)
                           if stages_named else "no holdout stage")
            parts[who] = {"kind": kind, "stage": None, "source": node.get("source"), "knobs": [], "bar": bar}
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
                                   "success criterion, edge case or scenario; read-only, writes no record")
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
