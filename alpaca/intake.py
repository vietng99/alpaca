"""alpaca intake: a spec and a runbook in; the op's checklist rows, task contracts and profile out.

Intake is a front door onto the checklist engine Alpaca already has, not a second engine:

  * rows: every success criterion (spec-kit `SC-nnn`), edge case (`EC-nnn`, under `runbook: 2`) or
    scenario (OpenSpec) becomes one item file, a one-row acceptance table written under
    `.alpaca/intake/<op>/<runbook id>/items/` and named by its own sha256. The row carries its bar
    (what each covering check, fail case and owner gate asks, from `runbook.bar_parts`), the stage
    that shows it first in run order, and the sources. Each file is read by the acceptance-table
    parser (`checklist.artifact`), turned into one obligation row by synthesis (`checklist.synthesis`,
    the step model below) and landed by the bridge (`checklist.bridge`). The row id digests the file
    bytes, so an unchanged item keeps its row id, its row and its verdicts across intakes.
  * a changed criterion, or a changed bar, gets a new row that cites the current one
    (`checklist.supersession.supersede`, cite-and-freeze), landed by the same bridge. A change of
    only the stage or the sources keeps the row. Its item file names the row it replaces, so a
    criterion that goes back to earlier text is a new row too, never a frozen row derived again; a
    removed one gets a withdrawal row that cites it and a waiver (`checklist.verdict_row.waive`) that
    says why. A new one is added. Nothing is edited in place and nothing is deleted. A row of item
    format 1 (no bar) is kept as it is: its bar is recorded as the baseline (BAR-BASELINE).
  * task contracts: one task per runbook stage and one per owner gate, added through
    `ops.add_task` and contracted through `taskcontract.record` (the newest contract is current).
  * profile: the runbook's stage ids become the project's profile stages. Unless the project names
    a profile of its own, intake writes `intake_profile.py` (a `RunbookProfile`) and sets
    `profile: intake_profile` in project.yaml; `alpaca doctor` then checks the runbooks and their
    plugin check scripts.

The runbook is checked against the spec first (`runbook.check`, the same check as
`alpaca runbook check`); a runbook that leaves a criterion uncovered is refused and nothing is
written. `--dry-run` prints the plan and writes nothing. One `intake` event records each run that
changed something; the latest one for (op, runbook id) is what the next run compares against.

The spec argument takes a spec-kit `spec.md` (or its feature folder), an OpenSpec `spec.md`, the
OpenSpec `specs` folder, or an OpenSpec change folder (`openspec/changes/<id>`). A change folder is
applied to the living specs the way OpenSpec's archive applies it (RENAMED, REMOVED, MODIFIED,
ADDED), so intake of a change and intake of the archived specs later give the same rows.

    alpaca intake <spec> <runbook> [--op <op>] [--dry-run] [--json]

docs/intake.md is the reference.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sys
import tempfile

from alpaca import db, util
from alpaca.profile import Profile

INSTRUMENT = "alpaca-intake"
EVENT = "intake"
KEY_COLUMN = "key"
PROFILE_MODULE = "intake_profile"
PROFILE_FILE = PROFILE_MODULE + ".py"
INTAKE_DIR = os.path.join(".alpaca", "intake")
STEP_MODEL_NAME = "step-model.json"

PASS, FAIL, BLOCKED, USAGE = 0, 1, 2, 64

#: the step model intake feeds synthesis. The row phase is the model id: intake rows are the
#: acceptance obligations of the op's verify phase. One step per way an item is shown.
STEP_MODEL = {
    "model_id": "verify",
    "steps": [
        {"key": "check", "name": "Shown by runbook checks", "ordinal": 1,
         "witness": "docs/intake.md", "consumes": ["check"],
         "obligation": "{item}: {cell:criterion} Bar: {cell:bar}.",
         "report_back": {"proof": "a sealed proof report citing the results of the checks named on this row",
                         "where": "the runbook stages that hold those checks",
                         "how": "run the stages, then alpaca proof check on the report",
                         "when": "before the op closes"}},
        {"key": "review", "name": "Judged at an owner gate", "ordinal": 2,
         "witness": "docs/intake.md", "consumes": ["review"],
         "obligation": "{item}: {cell:criterion} Judged by the owner at {cell:shown by}.",
         "report_back": {"proof": "the owner's recorded approval and the evidence the gate names",
                         "where": "the owner gate of the runbook stage named on this row",
                         "how": "the owner reads the evidence and records the decision",
                         "when": "before the op closes"}},
        {"key": "withdrawn", "name": "Withdrawn from the spec", "ordinal": 3,
         "witness": "docs/intake.md", "consumes": ["withdrawn"],
         "obligation": "{item} was removed from the spec, so this row withdraws it and needs no "
                       "discharge. It read: {cell:criterion}",
         "report_back": {"proof": "the intake event that saw the removal",
                         "where": "the spec change that removed the item",
                         "how": "alpaca intake compares the spec with the previous intake",
                         "when": "at the intake that saw the removal"}},
    ],
}

#: the layout of an intake item file. The file bytes are part of each row's identity (the row id
#: digests them), so a change to this layout, to `item_text`, `_cell` or `criterion` makes every
#: intake row look changed at the next intake. Such a change bumps this number and says so in
#: docs/intake.md; it is never made in passing. Format 2 added the bar, stage and source columns;
#: a row of format 1 is kept and never rewritten (BAR-BASELINE).
ITEM_FORMAT = 2
_HEADER = ("Intake item file, format %d. alpaca intake wrote this file from a spec and a runbook "
           "and never edits it; a later intake that sees a change writes a new file.")
_ITEM_HEADER = _HEADER % ITEM_FORMAT
_SLUG = re.compile(r"[^a-z0-9]+")
_OP_ID = re.compile(r"^op-\d+$")
_LIST_MARK = re.compile(r"^(?:[-*+]|\d{1,9}[.)])\s+")
_STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_EMPH = re.compile(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])")
_RENAME = re.compile(r"^\s*[-*]?\s*(FROM|TO)\s*:\s*`?\s*#{0,6}\s*(?:Requirement:\s*)?(.+?)\s*`?\s*$", re.I)


class IntakeError(Exception):
    """A refusal. `code` is FAIL (the inputs are wrong) or BLOCKED (the project state stops it)."""

    def __init__(self, message, code=FAIL, detail=None):
        super().__init__(message)
        self.code = code
        self.detail = list(detail or [])


# ------------------------------------------------------------------------------ the spec
def _is_change(path):
    return os.path.isdir(os.path.join(path, "specs")) and any(
        os.path.isfile(os.path.join(path, name)) for name in ("proposal.md", ".openspec.yaml", "tasks.md"))


def _canon(lines):
    """One line of text: list markers dropped, whitespace collapsed, empty lines left out."""
    out = []
    for line in lines:
        line = _LIST_MARK.sub("", line.strip())
        if line:
            out.append(line)
    return " ".join(" ".join(out).split())


def _delta_sections(lines):
    """{"ADDED": [names], "MODIFIED": [...], "REMOVED": [...], "RENAMED": [(from, to)]} of one
    delta spec, in file order."""
    from alpaca import runbook
    out = {"ADDED": [], "MODIFIED": [], "REMOVED": [], "RENAMED": []}
    section, pending = None, None
    for line, masked in zip(lines, runbook._fence_mask(lines)):
        if masked:
            continue
        m = runbook._SECTION.match(line)
        if m and not line.lstrip().startswith("###"):
            head = m.group(1).strip().upper()
            section = next((k for k in out if head.startswith(k)), None)
            pending = None
            continue
        if section is None:
            continue
        m = runbook._REQUIREMENT.match(line)
        if m and section != "RENAMED":
            out[section].append(m.group(1).strip())
            continue
        if section == "RENAMED":
            m = _RENAME.match(line)
            if m and m.group(1).upper() == "FROM":
                pending = m.group(2).strip()
            elif m and pending is not None:
                out["RENAMED"].append((pending, m.group(2).strip()))
                pending = None
    return out


def _spec_files(folder):
    found = []
    for base, dirs, names in os.walk(folder):
        dirs.sort()
        if "spec.md" in names:
            found.append(os.path.join(base, "spec.md"))
    return sorted(found)


def _capability(folder, full):
    cap = os.path.relpath(os.path.dirname(full), folder).replace(os.sep, "/")
    return None if cap == "." else cap


def _requirements(lines):
    from alpaca import runbook
    return [m.group(1).strip() for line, masked in zip(lines, runbook._fence_mask(lines))
            if not masked for m in [runbook._REQUIREMENT.match(line)] if m]


def effective_change(change):
    """The OpenSpec change folder `change` applied to the living specs next to it, as a spec in
    the shape runbook.parse_spec returns. The order is OpenSpec's archive order: RENAMED, REMOVED,
    MODIFIED, ADDED. A delta that names a requirement the living spec lacks (or ADDS one it has)
    is refused."""
    from alpaca import runbook
    change = os.path.abspath(change)
    parent = os.path.dirname(change)
    if os.path.basename(parent) == "archive":
        raise IntakeError("%s is an archived change; its delta is already in the living specs, so "
                          "run intake on openspec/specs instead" % change)
    living = os.path.join(os.path.dirname(parent), "specs") if os.path.basename(parent) == "changes" else None
    norm = runbook._norm
    caps = {}                       # capability -> {norm name: [name, [scenario items]]}
    if living and os.path.isdir(living):
        for full in _spec_files(living):
            lines = runbook._read_lines(full)
            cap = _capability(living, full)
            reqs = caps.setdefault(cap, {})
            for name in _requirements(lines):
                reqs.setdefault(norm(name), [name, []])
            for item in runbook._openspec_items(lines, cap):
                reqs.setdefault(norm(item["requirement"]), [item["requirement"], []])[1].append(item)
    delta_dir = os.path.join(change, "specs")
    problems = []
    for full in _spec_files(delta_dir):
        lines = runbook._read_lines(full)
        cap = _capability(delta_dir, full)
        where = os.path.relpath(full, change)
        sections = _delta_sections(lines)
        delta = {}
        for item in runbook._openspec_items(lines, cap):
            delta.setdefault(norm(item["requirement"]), []).append(item)
        reqs = caps.setdefault(cap, {})
        for old, new in sections["RENAMED"]:
            if norm(old) not in reqs:
                problems.append("%s renames %r, which %s does not have" % (where, old, cap or "the spec"))
                continue
            entry = reqs.pop(norm(old))
            entry[0] = new
            reqs[norm(new)] = entry
        for name in sections["REMOVED"]:
            if reqs.pop(norm(name), None) is None:
                problems.append("%s removes %r, which %s does not have" % (where, name, cap or "the spec"))
        for name in sections["MODIFIED"]:
            if norm(name) not in reqs:
                problems.append("%s modifies %r, which %s does not have" % (where, name, cap or "the spec"))
                continue
            reqs[norm(name)] = [name, delta.get(norm(name), [])]
        for name in sections["ADDED"]:
            if norm(name) in reqs:
                problems.append("%s adds %r, which %s already has" % (where, name, cap or "the spec"))
                continue
            reqs[norm(name)] = [name, delta.get(norm(name), [])]
    if problems:
        raise IntakeError("the change does not apply to the living specs", detail=problems)
    items = []
    for cap in sorted(caps, key=lambda c: c or ""):
        for name, scenarios in caps[cap].values():
            for item in scenarios:
                alias, _rest = runbook.scenario_alias(item["text"])
                bare = "%s/%s" % (name, item["text"])
                item = dict(item, requirement=name, bare=bare, capability=cap,
                            id="%s/%s" % (cap, bare) if cap else bare,
                            required=alias is None or alias.startswith(runbook._REQUIRED_ALIAS))
                items.append(item)
    return {"path": change, "format": "openspec", "items": items, "clarifications": 0}


def read_spec(path):
    """(spec, shape): the spec items of `path` and what it was (file, folder or change)."""
    from alpaca import runbook
    try:
        if os.path.isdir(path):
            if _is_change(path):
                return effective_change(path), "change"
            top = os.path.join(path, "spec.md")
            if os.path.isfile(top) and runbook._detect(runbook._read_lines(top)) == "spec-kit":
                return runbook.parse_spec(top), "file"
            return runbook.parse_spec(path), "folder"
        return runbook.parse_spec(path), "file"
    except (OSError, UnicodeDecodeError) as exc:
        raise IntakeError("cannot read the spec %s: %s" % (path, exc), code=BLOCKED)
    except ValueError as exc:
        raise IntakeError(str(exc))


def item_key(item):
    """The row key of a spec item: its spec-kit id (also when an OpenSpec scenario carries one),
    else the OpenSpec scenario id."""
    return item.get("alias") or item["id"]


def _plain(text):
    """`text` without markdown emphasis (`**x**`, `*x*`), ending with a sentence mark."""
    text = _EMPH.sub(r"\1", _STRONG.sub(r"\1", text)).strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


def criterion(item):
    """The text a row stands for, on one line, as plain text that ends with a period. spec-kit:
    the text after the id. OpenSpec: the scenario name (without a leading spec-kit id) and the
    scenario body."""
    from alpaca import runbook
    if "body" not in item:
        return _plain(_canon([item.get("text") or ""]))
    alias, rest = runbook.scenario_alias(item.get("text") or "")
    title = _canon([rest if alias else (item.get("text") or "")])
    body = _canon(item.get("body") or [])
    if title and body:
        return _plain("%s: %s" % (title, body))
    return _plain(title or body)


# ------------------------------------------------------------------------------ item files
def _slug(text):
    return (_SLUG.sub("-", str(text).lower()).strip("-") or "item")[:48]


def _cell(text):
    return " ".join(str(text).split()).replace("\\", "\\\\").replace("|", "\\|")


def item_text(key, crit, shown_by, bar, stage, source, kind, supersedes=None):
    """The bytes of one intake item file (format 2). `bar` is what the covering parts ask, `stage`
    the run-order position and id of the first covering stage (`3/5 load-test`), `source` their
    sources ("-" when none). `supersedes` is the row this item replaces ("-" for a first row): it
    keeps a superseding item apart from every earlier row of its key, so a criterion that goes
    back to earlier text (a revert, or a removed item restored) gets a new row that cites the
    current one instead of deriving a frozen row's id again."""
    return ("%s\n\n| key | criterion | shown by | bar | stage | source | kind | supersedes |\n"
            "|---|---|---|---|---|---|---|---|\n| %s | %s | %s | %s | %s | %s | %s | %s |\n"
            % (_ITEM_HEADER, _cell(key), _cell(crit), _cell(shown_by), _cell(bar), _cell(stage),
               _cell(source), kind, _cell(supersedes or "-")))


#: the cells that say what a row stands for. The bar is compared on its own (a format 1 file has
#: none), and the stage and the sources are left out: a change of only those keeps the row.
_SAME_CELLS = ("key", "criterion", "shown by", "kind", "supersedes")


def _head_cells(root, head):
    """The cells of the item file the row `head` was made from, or None when the file is gone or
    is not the one the row id digests."""
    from alpaca.checklist import Halt, artifact, synthesis
    rel = _proof_path(head)
    full = os.path.join(root, rel) if rel else None
    if not full or not os.path.isfile(full):
        return None
    try:
        parsed = artifact.parse(full, KEY_COLUMN)
    except Halt:
        return None
    if len(parsed["items"]) != 1:
        return None
    item = parsed["items"][0]
    if synthesis.row_id(head["step"], rel, item["key"], parsed["sha256_raw"]) != head["id"]:
        return None
    return dict(item["cells"])


def _file_cells(full):
    from alpaca.checklist import artifact
    return dict(artifact.parse(full, KEY_COLUMN)["items"][0]["cells"])


def _step_model(scratch):
    """The loaded step model (through `step_model.load`, the loader every step model goes through)."""
    from alpaca.checklist import step_model
    path = os.path.join(scratch, STEP_MODEL_NAME)
    util.write_text(path, json.dumps(STEP_MODEL, indent=1, sort_keys=True) + "\n")
    return step_model.load(path)


def _derive(model, scratch, rel, text, kind, op, session, operator):
    """One obligation row from one item file: parse the file (written under `scratch` at `rel`),
    stage it through synthesis, refuse anything but a clean one-row PASS."""
    from alpaca.checklist import Halt, artifact, synthesis
    from alpaca.gates import verdict
    full = os.path.join(scratch, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    util.write_text(full, text)
    try:
        art = artifact.parse(full, KEY_COLUMN)
        art["path"] = rel.replace(os.sep, "/")
        art["kind"] = kind
        staged = synthesis.stage(model, art, op=op, session=session, operator=operator)
    except Halt as h:
        raise IntakeError("intake item %s was refused: %s %s" % (rel, h.code, h.detail))
    if staged["verdict"] != verdict.PASS or len(staged["rows"]) != 1:
        raise IntakeError("intake item %s did not stage as one row: %s" % (rel, staged["findings"]))
    return staged["rows"][0]


# ------------------------------------------------------------------------------ the record
def baseline(conn, op, runbook_id):
    """The data of the latest intake event for (op, runbook id), or None."""
    for e in reversed(db.events(conn, kind=EVENT, limit=10 ** 9)):
        d = e["data"]
        if d.get("op") == op and d.get("runbook_id") == runbook_id:
            return d
    return None


def _held_by_other_runbooks(conn, op, runbook_id, keys, store):
    """{spec key: (runbook id, live row id)} for the keys of this intake that another runbook id
    already holds in `op` with a live row (the head of its chain, not withdrawn): taking the runbook
    in under a new id, in a new or moved file, would leave two live rows for each of them."""
    from alpaca.checklist import supersession
    latest = {}
    for e in db.events(conn, kind=EVENT, limit=10 ** 9):
        d = e["data"]
        if d.get("op") == op and d.get("runbook_id") and d.get("runbook_id") != runbook_id:
            latest[d["runbook_id"]] = d
    held = {}
    for other, d in sorted(latest.items()):
        for key, entry in sorted((d.get("items") or {}).items()):
            if key not in keys or key in held:
                continue
            head = supersession.head(store, entry.get("row"))
            if head is None or head.get("step") == "withdrawn":
                continue
            held[key] = (other, head["id"])
    return held


def _renamed_from(conn, op, rel_runbook, runbook_id):
    """The runbook id the latest intake of this runbook file into `op` used, when it differs from
    `runbook_id`; else None."""
    for e in reversed(db.events(conn, kind=EVENT, limit=10 ** 9)):
        d = e["data"]
        if d.get("op") == op and d.get("runbook") == rel_runbook:
            return d.get("runbook_id") if d.get("runbook_id") != runbook_id else None
    return None


def _proof_path(row):
    proof = str(row.get("proof") or "")
    if not proof.startswith("local:") or ":" not in proof[6:]:
        return None
    return proof[6:].rsplit(":", 1)[0]


def _lineage(store, head, key):
    """The rows from the first one for `key` to `head`, each rebuilt from the store with the fields
    it was frozen with (plus its item key, the item file its proof names, and the tag and status it
    landed with; the tag column is derived later), so supersession can verify the chain before it
    appends to it."""
    from alpaca.checklist import supersession, synthesis
    by_id = {r["id"]: r for r in store}
    chain, seen = [head], {head["id"]}
    while chain[0].get("supersedes"):
        prev = by_id.get(chain[0]["supersedes"])
        if prev is None or prev["id"] in seen:
            break
        chain.insert(0, prev)
        seen.add(prev["id"])
    out = []
    for r in chain:
        rebuilt = {"id": r["id"], "kind": r["kind"], "op": r["op"], "session": r["session"],
                   "operator": r["operator"], "phase": r["phase"], "step": r["step"],
                   "item": key, "artifact": _proof_path(r), "statement": r["statement"],
                   "proof": r["proof"], "where": r["where_"], "how": r["how"], "when": r["when_"],
                   "why": r["why"], "tag": synthesis.SPECCED, "status": "open",
                   "supersedes": r["supersedes"], "content_hash": r["content_hash"],
                   "prev_hash": r["prev_hash"]}
        if not supersession.is_frozen(rebuilt):
            raise IntakeError("row %s does not match the intake item it names (%s); it was not written "
                              "by intake, or it was changed, so intake will not supersede it"
                              % (r["id"], rebuilt["artifact"]), code=BLOCKED)
        out.append(rebuilt)
    return out


def _intake_tasks(conn, op, runbook_id):
    """{task key: task id} for the tasks earlier intakes added for (op, runbook id)."""
    out = {}
    for e in db.events(conn, kind="task-add", limit=10 ** 9):
        mark = e["data"].get("intake") if isinstance(e["data"], dict) else None
        if isinstance(mark, dict) and mark.get("op") == op and mark.get("runbook") == runbook_id:
            out[mark.get("key")] = e["ref"]
    return out


# ------------------------------------------------------------------------------ contracts
def _check_what(chk, knobs):
    """What a check (or the detect of a fail case) asks, in words: `out/load.json field p95 <= 50`."""
    kind = chk.get("type")
    if kind == "exit-code":
        what = "the command exits %s" % chk.get("expect", 0)
    elif kind == "file-exists":
        what = "%s exists%s" % (chk.get("path"), "" if chk.get("non_empty") is False else " and is not empty")
    elif kind == "regex-in-file":
        what = "%s %s /%s/" % (chk.get("path"), "does not match" if chk.get("absent") else "matches",
                               chk.get("pattern"))
    elif kind == "json-field":
        value = chk.get("value")
        shown = value
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", str(value)) if isinstance(value, str) else None
        if m and m.group(1) in knobs:
            shown = "%s (%s)" % (value, knobs[m.group(1)])
        what = "%s field %s %s %s" % (chk.get("path"), chk.get("field"), chk.get("op"), shown)
    elif kind == "plugin":
        args = " ".join(str(a) for a in (chk.get("args") or []))
        what = "plugin %s%s passes" % (chk.get("script"), (" " + args) if args else "")
    else:
        what = kind
    return what


def _check_line(chk, knobs, stage_id):
    covers = chk.get("covers") or []
    tail = " (shows %s)" % ", ".join(str(c) for c in covers) if covers else ""
    return ("%s/%s (%s): %s%s" % (stage_id, chk.get("id"), chk.get("type"), _check_what(chk, knobs), tail))[:600]


#: what each `then` of a fail case does, as a task contract says it (docs/runbook-format.md)
_THEN = {"stop": "then stop: the stage ends FAIL without another attempt",
         "retry": "then retry: another attempt through the retry rule of the stage",
         "ask-owner": "then ask the owner: the stage pauses for a decision"}


def _fail_line(fail, knobs, stages):
    """One fail case line of a task contract. A fail case of format 1 (no detect, no then) is
    `<id>: <when>`. With a detect and a then, the line says how the failure is recognized and what
    happens then; a then that runs a recovery stage names that stage, its command and its checks,
    since the recovery stage has no task of its own."""
    when = " ".join(str(fail.get("when")).split())
    detect, then = fail.get("detect"), fail.get("then")
    if not isinstance(detect, dict) and then is None:
        return "%s: %s" % (fail.get("id"), when)
    pieces = ["%s: %s" % (fail.get("id"), when.rstrip("."))]
    if isinstance(detect, dict):
        pieces.append("detect (%s): %s" % (detect.get("type"), _check_what(detect, knobs)))
    if isinstance(then, dict):
        rid = str(then.get("run"))
        rec = stages.get(rid) or {}
        checks = ", ".join(_check_line(c, knobs, rid) for c in (rec.get("checks") or []) if isinstance(c, dict))
        pieces.append("then run the recovery stage %s: %s, checks %s; after it passes, then this stage runs "
                      "again as one more attempt, and when it fails this stage ends FAIL"
                      % (rid, " ".join(str(rec.get("run")).split()), checks or "none"))
    elif then is not None:
        pieces.append(_THEN.get(str(then), "then %s" % then))
    return "; ".join(pieces)


def _fit(lines, limit=20):
    """At most `limit` lines: the rest fold into the last one, never dropped silently."""
    from alpaca import taskcontract
    lines = [l[:taskcontract.MAX_TEXT] for l in lines if l]
    if len(lines) <= limit:
        return lines
    rest = lines[limit - 1:]
    return lines[:limit - 1] + [("and %d more: %s" % (len(rest), "; ".join(rest)))[:taskcontract.MAX_TEXT]]


def _task_plan(data, rel_runbook):
    """[(key, title, statement, contract)] for every stage and every owner gate, in run order.
    A recovery stage (`recovery: true`) gets no task: it runs only when a fail case sends to it,
    so the fail case line of the stage that sends names it, its command and its checks. It stays
    a profile stage."""
    from alpaca import runbook
    knobs = runbook.knob_values(data)
    rb_id = data.get("id")
    by_id = {s.get("id"): s for s in data.get("stages") or [] if isinstance(s, dict)}
    out = []
    for n, stage in enumerate(data.get("stages") or []):
        sid = stage.get("id")
        if stage.get("recovery") is True:
            continue
        title = str(stage.get("title") or sid)
        source = "%s stages[%d] (%s)" % (rel_runbook, n, sid)
        gate = stage.get("owner_gate") if isinstance(stage.get("owner_gate"), dict) else None
        if gate:
            covers = gate.get("covers") or []
            contract = {
                "input": ["evidence: %s" % p for p in (gate.get("evidence") or [])],
                "expected": ["the owner's recorded decision on stage %s" % sid],
                "done_bar": ["the owner approves: %s" % " ".join(str(gate.get("approve")).split())]
                            + (["the approval judges %s" % ", ".join(str(c) for c in covers)] if covers else []),
                "fail_cases": ["the owner does not approve; stage %s does not run" % sid],
                "stage": sid, "source": source + " owner_gate"}
            out.append(("gate:%s" % sid, ("Owner approval: %s" % title)[:60],
                        "Owner gate of stage %s in runbook %s: %s" % (sid, rb_id, " ".join(str(gate.get("approve")).split())),
                        contract))
        if stage.get("run") is None:
            continue
        outputs = []
        for o in stage.get("outputs") or []:
            if isinstance(o, dict):
                outputs.append("%s: %s" % (o.get("path"), o.get("what")) if o.get("what") else str(o.get("path")))
            else:
                outputs.append(str(o))
        inputs = ["%s" % i for i in (stage.get("inputs") or [])]
        inputs += ["stage %s passed" % need for need in (stage.get("needs") or [])]
        if gate:
            inputs.append("the owner approved stage %s" % sid)
        done = [_check_line(c, knobs, sid) for c in (stage.get("checks") or []) if isinstance(c, dict)]
        fails = [_fail_line(f, knobs, by_id) for f in (stage.get("fails") or []) if isinstance(f, dict)]
        retry = stage.get("retry") if isinstance(stage.get("retry"), dict) else None
        if retry:
            for chk in retry.get("stop_on") or []:
                fails.append("check %s fails: the stage stops without another attempt" % chk)
            fails.append("the checks still fail after %s attempt(s)" % retry.get("max_attempts"))
        contract = {"input": _fit(inputs), "expected": _fit(outputs), "done_bar": _fit(done),
                    "fail_cases": _fit(fails), "stage": sid, "source": source}
        desc = " ".join(str(stage.get("description") or title).split()).rstrip(".")
        statement = "Run stage %s of runbook %s: %s. Command: %s" % (sid, rb_id, desc, " ".join(str(stage.get("run")).split()))
        out.append(("stage:%s" % sid, title[:60], statement, contract))
    return out


# ------------------------------------------------------------------------------ the profile
def _read_project(root):
    from alpaca import spec_kits
    try:
        return spec_kits._read_yaml(root)
    except spec_kits.KitError as exc:
        raise IntakeError(str(exc), code=BLOCKED)


def project_edit(root, key, value):
    """The project.yaml text with the top-level `key: value` set, keeping comments and every other
    key (the same text edit `alpaca spec init` uses for its block), or None when it already says so.
    A file the text edit cannot keep whole (a flow mapping, say) is refused, never rewritten
    through a full dump that would drop its comments."""
    import yaml
    from alpaca import spec_kits
    path = os.path.join(root, "project.yaml")
    text = util.read_text(path) if os.path.isfile(path) else ""
    try:
        before = spec_kits._parse_yaml(text)
    except spec_kits.KitError as exc:
        raise IntakeError(str(exc), code=BLOCKED)
    if before.get(key) == value:
        return None
    new = spec_kits._replace_top_block(text, key, yaml.safe_dump({key: value}, sort_keys=False),
                                       keep_comments=True)
    want = dict(before)
    want[key] = value
    try:
        after = yaml.safe_load(new) or {}
    except yaml.YAMLError:
        after = None
    if after != want:
        raise IntakeError("project.yaml cannot take `%s: %s` by a line edit (its layout is not one "
                          "top-level key per line), and intake will not rewrite the whole file and drop "
                          "its comments; add the line `%s: %s` by hand and run intake again"
                          % (key, value, key, value), code=BLOCKED)
    return new


def set_project_key(root, key, value):
    """Write the top-level `key: value` into project.yaml through `project_edit`. Returns whether
    it wrote."""
    new = project_edit(root, key, value)
    if new is None:
        return False
    util.write_text(os.path.join(root, "project.yaml"), new)
    return True


def _profile_runbooks(root):
    """The runbook list of an existing intake_profile.py, or []."""
    path = os.path.join(root, PROFILE_FILE)
    if not os.path.isfile(path):
        return []
    try:
        tree = ast.parse(util.read_text(path))
    except SyntaxError:
        return []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "RUNBOOKS" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return []
            return [str(v) for v in value] if isinstance(value, list) else []
    return []


#: the first line of every intake_profile.py intake writes; a file without it is someone else's.
_PROFILE_MARK = '"""The domain profile `alpaca intake` writes (docs/intake.md).\n'


def profile_text(runbooks):
    lines = ",\n".join("    %s" % json.dumps(r) for r in runbooks)
    return (_PROFILE_MARK + '\n'
            "It names the runbooks intake has read. Their stage ids are this project's profile stages,\n"
            "so a task contract can name one, and `alpaca doctor` checks each runbook and its plugin\n"
            "check scripts. alpaca intake rewrites this file. For hooks of your own, write your own\n"
            "profile, name it in project.yaml `profile:`, and give it the same stages.\n"
            '"""\n'
            "from alpaca.intake import RunbookProfile\n\n"
            "RUNBOOKS = [\n%s,\n]\n\n"
            "PROFILE = RunbookProfile(__file__, RUNBOOKS)\n" % lines)


class RunbookProfile(Profile):
    """A profile read from runbooks: their stage ids are the stages, and doctor checks each
    runbook and its plugin check scripts. `anchor` is the file the runbook paths are relative to
    the folder of."""

    def __init__(self, anchor, runbooks):
        self._base = os.path.dirname(os.path.abspath(anchor))
        self._runbooks = [str(r) for r in runbooks]
        self.name = ", ".join(self._ids()) or "runbook"

    def _loaded(self):
        from alpaca import runbook
        out = []
        for rel in self._runbooks:
            data, _report = runbook.load(os.path.join(self._base, rel))
            out.append((rel, data))
        return out

    def _ids(self):
        return [str(d.get("id")) for _rel, d in self._loaded() if isinstance(d, dict) and d.get("id")]

    def stages(self):
        seen = []
        for _rel, data in self._loaded():
            for stage in (data or {}).get("stages") or []:
                sid = stage.get("id") if isinstance(stage, dict) else None
                if isinstance(sid, str) and sid not in seen:
                    seen.append(sid)
        return seen

    def doctor_checks(self, root, conn):
        from alpaca import runbook
        out = []
        for rel, data in self._loaded():
            full = os.path.join(self._base, rel)
            result = runbook.check(full)
            level = "ok" if result["code"] == 0 else "error"
            detail = "%s: %s" % (rel, result["verdict"])
            if result["errors"]:
                detail += " (%s)" % "; ".join("%s %s" % (e["code"], e["where"]) for e in result["errors"][:3])
            out.append(("intake runbook", level, detail))
            for stage in (data or {}).get("stages") or []:
                for chk in (stage.get("checks") or []) if isinstance(stage, dict) else []:
                    if isinstance(chk, dict) and chk.get("type") == "plugin":
                        script = os.path.join(os.path.dirname(full), str(chk.get("script")))
                        ok = os.path.isfile(script) and os.access(script, os.X_OK)
                        out.append(("plugin check", "ok" if ok else "error",
                                    "%s/%s: %s%s" % (stage.get("id"), chk.get("id"), chk.get("script"),
                                                     "" if ok else " is missing or not executable")))
        return out

    def paths(self):
        dirs = sorted({os.path.dirname(r) or "." for r in self._runbooks})
        return {"documents": dirs}


def _plugin_checks(data):
    out = []
    for stage in data.get("stages") or []:
        for chk in stage.get("checks") or []:
            if isinstance(chk, dict) and chk.get("type") == "plugin":
                out.append({"stage": stage.get("id"), "check": chk.get("id"), "script": chk.get("script")})
    return out


def _profile_plan(root, rel_runbook, stages):
    cfg = _read_project(root)
    named = cfg.get("profile")
    named = named.strip() if isinstance(named, str) and named.strip() else None
    if named and named != PROFILE_MODULE:
        from alpaca import profile
        prof = profile.load(root)
        problem = profile.error(root)
        have = [str(s) for s in prof.stages()]
        missing = [s for s in stages if s not in have]
        if problem or missing:
            raise IntakeError("project.yaml names the profile %s, and it %s; add the runbook stages to "
                              "that profile, or remove `profile:` and intake writes %s"
                              % (named, problem or "does not declare the stages %s" % ", ".join(missing),
                                 PROFILE_FILE), code=BLOCKED)
        return {"module": named, "action": "kept (the project's own profile has every stage)",
                "writes": {}, "stages": have}
    runbooks = _profile_runbooks(root)
    if rel_runbook not in runbooks:
        runbooks = runbooks + [rel_runbook]
    text = profile_text(runbooks)
    path = os.path.join(root, PROFILE_FILE)
    writes = {}
    if os.path.isfile(path) and not util.read_text(path).startswith(_PROFILE_MARK):
        raise IntakeError("%s exists and intake did not write it; intake will not write over it. Move "
                          "it, or name it in project.yaml `profile:` with the runbook stages"
                          % PROFILE_FILE, code=BLOCKED)
    if not os.path.isfile(path) or util.read_text(path) != text:
        writes["file"] = text
    if named != PROFILE_MODULE:
        project_edit(root, "profile", PROFILE_MODULE)       # refuses now, not halfway through apply
        writes["project.yaml"] = PROFILE_MODULE
    return {"module": PROFILE_MODULE, "action": "write" if writes else "kept", "writes": writes,
            "runbooks": runbooks}


# ------------------------------------------------------------------------------ plan and apply
def _rel(root, path):
    full = os.path.realpath(path)
    real_root = os.path.realpath(root)
    if full == real_root or full.startswith(real_root + os.sep):
        return os.path.relpath(full, real_root).replace(os.sep, "/")
    return None


def _open_op(conn, op):
    if op:
        rows = db.rows(conn, "ops", "id=?", (op,))
        if not rows:
            raise IntakeError("no op %s; open one with alpaca op new" % op, code=BLOCKED)
    else:
        rows = [r for r in db.rows(conn, "ops", "status='open' AND id != 'op-0' ORDER BY opened DESC")][:1]
        if not rows:
            raise IntakeError("no open op; open one with alpaca op new \"<intent>\" or pass --op",
                              code=BLOCKED)
    if rows[0]["status"] != "open":
        raise IntakeError("op %s is %s; intake adds rows only to an open op" % (rows[0]["id"], rows[0]["status"]),
                          code=BLOCKED)
    if not _OP_ID.match(rows[0]["id"]):
        raise IntakeError("op id %r is not of the form op-NNN" % rows[0]["id"], code=BLOCKED)
    return rows[0]["id"]


def _shown(who, part):
    """How the `shown by` cell names one covering part."""
    if part["kind"] == "gate":
        return "the owner gate of stage %s" % part["stage"]
    if part["kind"] == "fail":
        return "the fail case %s of stage %s" % (who.split("/", 1)[1], part["stage"])
    return "%s/%s" % (part["stage"], who)


def plan(root, conn, spec_path, runbook_path, *, op=None, session="cli", actor=INSTRUMENT):
    """Everything intake would do, and nothing done. Raises IntakeError on a refusal."""
    from alpaca import runbook, taskcontract
    from alpaca.checklist import Halt, supersession
    op = _open_op(conn, op)
    rel_runbook = _rel(root, runbook_path)
    if rel_runbook is None:
        raise IntakeError("the runbook %s is outside the project %s; keep it in the project so the "
                          "profile can name it" % (runbook_path, root), code=BLOCKED)
    spec, shape = read_spec(spec_path)
    checked = runbook.check(runbook_path, spec=spec)
    if checked["code"] != 0:
        raise IntakeError("the runbook does not pass alpaca runbook check against the spec",
                          code=checked["code"],
                          detail=["%s %s: %s" % (e["code"], e["where"] or "-", e["message"])
                                  for e in checked["errors"]])
    data, _report = runbook.load(runbook_path)
    rb_id = data["id"]
    old_id = _renamed_from(conn, op, rel_runbook, rb_id)
    if old_id:
        raise IntakeError("the runbook %s was taken into %s with the id %s and now has the id %s. Intake "
                          "keys rows and tasks by the runbook id, so a new id would add a second set "
                          "next to the first. Put the id back to %s. A new id in a new runbook file "
                          "is taken in as a second runbook: the rows and tasks of %s stay open, and "
                          "you close or withdraw them by hand" % (rel_runbook, op, old_id, rb_id, old_id,
                                                                  old_id),
                          code=BLOCKED)
    stages = [s["id"] for s in data["stages"]]
    covered = checked["coverage"]["covered"]
    bars = runbook.bar_parts(data)
    run_order = {s["id"]: {"position": bars["stages"][s["id"]]["position"],
                           "needs": [str(n) for n in (s.get("needs") or [])]} for s in data["stages"]}

    required = [i for i in runbook.spec_items(spec, runbook.runbook_format(data)) if i["required"]]
    keys = {}
    for item in required:
        k = item_key(item)
        if k in keys:
            raise IntakeError("two spec items share the key %s (%s and %s); give each its own id"
                              % (k, keys[k]["id"], item["id"]))
        keys[k] = item

    base = baseline(conn, op, rb_id) or {}
    base_items = base.get("items") or {}
    store = db.rows(conn, "rows", "1=1")
    by_id = {r["id"]: r for r in store}
    item_dir = "/".join((INTAKE_DIR.replace(os.sep, "/"), op, rb_id, "items"))
    scratch = tempfile.mkdtemp(prefix="alpaca-intake-")
    try:
        model = _step_model(scratch)
        rows = {"added": [], "kept": [], "superseded": [], "withdrawn": []}
        files, batch, waive, items_after, baselined, order = {}, [], [], {}, [], {}

        def build(key, crit, shown, bar, stage, source, kind, supersedes=None):
            text = item_text(key, crit, shown, bar, stage, source, kind, supersedes)
            rel = "%s/%s.%s.md" % (item_dir, _slug(key), util.sha256_hex(text)[:12])
            row = _derive(model, scratch, rel, text, kind, op, session, actor)
            return rel, text, row

        def same_item(key, head, rel, bar):
            """Whether `head` stands for the item written at `rel` (in scratch) with only its stage
            or sources changed. A head of item format 1 has no bar: it is the same when its other
            cells are, and the bar the previous intake recorded (if any) is the current one; with
            none recorded, the current bar becomes the baseline (BAR-BASELINE)."""
            if head.get("step") == "withdrawn":
                return False
            old = _head_cells(root, head)
            if old is None:
                return False
            new = _file_cells(os.path.join(scratch, rel))
            if any(old.get(c) != new.get(c) for c in _SAME_CELLS):
                return False
            if "bar" in old:
                return old["bar"] == new["bar"]
            recorded = (base_items.get(key) or {}).get("bar")
            if recorded is None:
                baselined.append(key)
                return True
            return recorded == bar

        def head_of(key):
            entry = base_items.get(key)
            if not entry:
                return None
            return supersession.head(store, entry.get("row"))

        def supersede(key, head, rel, text, row):
            """`row` (built citing `head`) frozen onto the lineage of `head`, or a refusal."""
            if row["id"] in by_id:
                raise IntakeError("the row %s that would supersede %s for %s is already in the record; "
                                  "intake will not land it twice" % (row["id"], head["id"], key), code=BLOCKED)
            try:
                chain = supersession.supersede(_lineage(store, head, key), head["id"],
                                               dict(row, supersedes=head["id"]))
            except Halt as h:
                raise IntakeError("supersession refused the row for %s: %s %s" % (key, h.code, h.detail),
                                  code=BLOCKED)
            batch.append(chain[-1])
            files[rel] = text
            return chain[-1]

        for key, item in keys.items():
            who = covered.get(item["id"]) or []
            parts = [bars["parts"][w] for w in who]
            shown = ", ".join(_shown(w, p) for w, p in zip(who, parts))
            kind = "check" if [p for p in parts if p["kind"] != "gate"] else "review"
            bar = "; ".join(p["bar"] for p in parts)
            first = min((bars["stages"][p["stage"]] for p in parts), default={"position": None, "label": "-"},
                        key=lambda st: (st["position"] is None, st["position"] or 0))
            sources = []
            for p in parts:
                if p["source"] and str(p["source"]) not in sources:
                    sources.append(str(p["source"]))
            stage, source = first["label"], ", ".join(sources) or "-"
            order[key] = (first["position"] is None, first["position"] or 0, key)
            head = head_of(key)
            # the item as the current head would hold it: same text, same predecessor
            rel, text, row = build(key, criterion(item), shown, bar, stage, source, kind,
                                   head and head.get("supersedes"))
            entry = {"key": key, "row": row["id"], "step": kind, "shown_by": shown, "bar": bar,
                     "stage": stage, "source": source}
            if head is not None and (head["id"] == row["id"] or same_item(key, head, rel, bar)):
                from alpaca.checklist import verdict_row
                entry["row"] = head["id"]
                rows["kept"].append(dict(entry, status=verdict_row.status_fold(conn, head["id"])))
            elif head is not None:
                rel, text, row = build(key, criterion(item), shown, bar, stage, source, kind, head["id"])
                new = supersede(key, head, rel, text, row)
                rows["superseded"].append(dict(entry, old=head["id"], new=new["id"]))
                entry["row"] = new["id"]
            elif row["id"] in by_id:
                rows["kept"].append(dict(entry, status="landed earlier"))
            else:
                batch.append(row)
                files[rel] = text
                rows["added"].append(entry)
            items_after[key] = {"row": entry["row"], "step": kind, "bar": bar, "stage": stage, "source": source}

        for key, entry in base_items.items():
            if key in keys:
                continue
            head = head_of(key)
            if head is None:
                continue
            if head.get("step") == "withdrawn":
                items_after[key] = {"row": head["id"], "step": "withdrawn"}
                if entry.get("step") != "withdrawn":
                    rows["withdrawn"].append({"key": key, "old": entry.get("row"), "new": head["id"],
                                              "row": head["id"], "step": "withdrawn"})
                    waive.append(head["id"])
                continue
            old_file = _proof_path(head)
            old_crit = ""
            if old_file and os.path.isfile(os.path.join(root, old_file)):
                from alpaca.checklist import artifact
                try:
                    old = artifact.parse(os.path.join(root, old_file), KEY_COLUMN)
                    old_crit = (old["items"][0]["cells"].get("criterion") or "")
                except Halt:
                    old_crit = ""
            rel, text, row = build(key, old_crit or "(the text is not on file)", "-", "-", "-", "-", "withdrawn",
                                   head["id"])
            new = supersede(key, head, rel, text, row)
            waive.append(new["id"])
            rows["withdrawn"].append({"key": key, "old": head["id"], "new": new["id"],
                                      "row": new["id"], "step": "withdrawn"})
            items_after[key] = {"row": new["id"], "step": "withdrawn"}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    for group in rows.values():                 # run order: stage position, then key
        group.sort(key=lambda r: order.get(r["key"], (True, 0, r["key"])))

    held = _held_by_other_runbooks(conn, op, rb_id, keys, store)
    lvl = _level(conn, session, root) if waive else None    # a dry run refuses what the run would

    prof = _profile_plan(root, rel_runbook, stages)
    existing = _intake_tasks(conn, op, rb_id)
    current = taskcontract.latest(conn)
    tasks = {"added": [], "contracts": [], "kept": [], "orphaned": []}
    task_writes = []
    planned_keys = []
    for key, title, statement, contract in _task_plan(data, rel_runbook):
        planned_keys.append(key)
        try:
            normal = taskcontract.check(contract, tuple(stages))
        except ValueError as exc:
            raise IntakeError("the contract for %s is not valid: %s" % (key, exc))
        tid = existing.get(key)
        if tid is None:
            tasks["added"].append({"key": key, "title": title, "stage": contract["stage"]})
            task_writes.append(("add", key, title, statement, contract))
            continue
        have = {k: v for k, v in (current.get(tid) or {}).items() if k not in ("event_id", "at", "by")}
        if have == normal:
            tasks["kept"].append({"key": key, "task": tid})
        else:
            tasks["contracts"].append({"key": key, "task": tid})
            task_writes.append(("contract", key, tid, None, contract))
    for key, tid in sorted(existing.items()):
        if key not in planned_keys:
            tasks["orphaned"].append({"key": key, "task": tid})

    return {"op": op, "root": root, "spec": _rel(root, spec_path) or os.path.abspath(spec_path),
            "spec_shape": shape, "format": spec["format"], "runbook": rel_runbook, "runbook_id": rb_id,
            "stages": stages, "required": len(required), "rows": rows, "tasks": tasks,
            "profile": {"module": prof["module"], "action": prof["action"], "stages": stages,
                        "plugin_checks": _plugin_checks(data)},
            "warnings": ["%s %s: %s" % (w["code"], w["where"] or "-", w["message"]) for w in checked["warnings"]]
                        + ["KEY-HELD-BY-ANOTHER-RUNBOOK %s: the runbook id %s holds the live row %s for "
                           "this spec item in %s, so the op carries two rows for it. If %s is the old id "
                           "of this runbook, its rows and tasks stay open until you close or withdraw "
                           "them by hand (docs/intake.md)" % (key, other, row, op, other)
                           for key, (other, row) in sorted(held.items())]
                        + ["BAR-BASELINE %s: the previous intake of %s stored no bar (item format 1), so "
                           "intake kept the row %s and recorded its bar as the baseline: %s. From now on "
                           "a change of the bar supersedes the row" % (key, key, items_after[key]["row"],
                                                                       items_after[key]["bar"])
                           for key in sorted(baselined, key=lambda k: order[k])],
            "_files": files, "_batch": batch, "_waive": waive, "_items": items_after, "_level": lvl,
            "_base_items": base_items, "_run_order": run_order,
            "_profile": prof, "_task_writes": task_writes, "_existing_tasks": existing}


def _changed(p):
    """Whether apply would write anything. A kept row whose stage, sources or baseline bar changed
    changes the entry the intake event records, so that is a change too."""
    r, t = p["rows"], p["tasks"]
    return bool(r["added"] or r["superseded"] or r["withdrawn"] or t["added"] or t["contracts"]
                or p["_profile"]["writes"] or p["_waive"] or p["_items"] != p["_base_items"])


def apply(root, conn, p, *, session="cli", actor=INSTRUMENT):
    """Write what `plan` planned: item files, the profile, the rows (bridge), the waivers of
    withdrawn rows, the tasks and their contracts, then one intake event. Returns the plan with
    the task ids filled in."""
    from alpaca import migrate, ops, taskcontract
    from alpaca.checklist import Halt, artifact, bridge, verdict_row
    from alpaca.gates import verdict
    if not _changed(p):
        return p
    lvl = p.get("_level") if p["_waive"] else None             # read by plan, before any write
    if p["_waive"] and lvl is None:
        lvl = _level(conn, session, root)
    for rel, text in p["_files"].items():
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            if util.read_text(full) != text:
                raise IntakeError("%s exists with other content; an intake item file is never "
                                  "rewritten" % rel, code=BLOCKED)
            continue
        util.write_text(full, text)
        try:
            parsed = artifact.parse(full, KEY_COLUMN)
        except Halt as h:
            raise IntakeError("%s did not parse after writing: %s" % (rel, h.detail), code=BLOCKED)
        if parsed["sha256_raw"] != util.sha256_hex(text):
            raise IntakeError("%s changed while it was written" % rel, code=BLOCKED)
    model_path = os.path.join(root, INTAKE_DIR, STEP_MODEL_NAME)
    model_text = json.dumps(STEP_MODEL, indent=1, sort_keys=True) + "\n"
    if not os.path.isfile(model_path) or util.read_text(model_path) != model_text:
        util.write_text(model_path, model_text)

    writes = p["_profile"]["writes"]
    if "file" in writes:
        util.write_text(os.path.join(root, PROFILE_FILE), writes["file"])
    if "project.yaml" in writes:
        set_project_key(root, "profile", writes["project.yaml"])
    if p["_profile"]["module"] == PROFILE_MODULE:
        from alpaca import profile
        have = [str(s) for s in profile.load(root).stages()]
        if profile.error(root) or [s for s in p["stages"] if s not in have]:
            raise IntakeError("the profile %s did not load with the runbook stages: %s"
                              % (PROFILE_MODULE, profile.error(root) or have), code=BLOCKED)

    if p["_batch"]:
        landed = bridge.apply(conn, p["_batch"], session=session, actor=INSTRUMENT, op=p["op"])
        if landed["verdict"] != verdict.PASS:
            raise IntakeError("the bridge refused the rows", code=BLOCKED,
                              detail=["%s: %s" % (f["code"], f["detail"]) for f in landed["findings"]])
        if any(r.get("supersedes") for r in p["_batch"]):
            with db.transaction(conn):
                migrate._backfill_superseded_by(conn)
    if p["_waive"]:
        store = db.rows(conn, "rows", "1=1")
        by_id = {r["id"]: r for r in store}
        for rid in p["_waive"]:
            if verdict_row.status_fold(conn, rid) == verdict_row.WAIVED_STATUS:
                continue
            try:
                verdict_row.waive(conn, rid, by_id[rid]["content_hash"],
                                  "the spec no longer has this item; alpaca intake withdrew it", lvl, session)
            except Halt as h:
                raise IntakeError("cannot waive withdrawn row %s: %s %s" % (rid, h.code, h.detail), code=BLOCKED)

    ids = dict(p["_existing_tasks"])
    for write in p["_task_writes"]:
        if write[0] == "add":
            _kind, key, title, statement, contract = write
            tid = ops.add_task(conn, p["op"], statement, title, session=session, actor=actor,
                               extra={"intake": {"op": p["op"], "runbook": p["runbook_id"], "key": key}})
            ids[key] = tid
        else:
            _kind, key, tid, _s, contract = write
        try:
            taskcontract.record(conn, ids[key], contract, session=session, actor=actor)
        except ValueError as exc:
            raise IntakeError("the contract for %s was refused: %s" % (key, exc), code=BLOCKED)
    for entry in p["tasks"]["added"]:
        entry["task"] = ids.get(entry["key"])

    summary = {"added": [r["key"] for r in p["rows"]["added"]],
               "kept": [r["key"] for r in p["rows"]["kept"]],
               "superseded": [[r["key"], r["old"], r["new"]] for r in p["rows"]["superseded"]],
               "withdrawn": [[r["key"], r["old"], r["new"]] for r in p["rows"]["withdrawn"]],
               "tasks_added": [t["key"] for t in p["tasks"]["added"]],
               "contracts": [t["key"] for t in p["tasks"]["contracts"]]}
    db.append_event(conn, session=session, actor=actor, kind=EVENT, op=p["op"], ref=p["runbook_id"],
                    data={"op": p["op"], "runbook": p["runbook"], "runbook_id": p["runbook_id"],
                          "spec": p["spec"], "spec_shape": p["spec_shape"], "format": p["format"],
                          "item_format": ITEM_FORMAT, "items": p["_items"], "run_order": p["_run_order"],
                          "tasks": ids, "profile": p["profile"]["module"],
                          "changes": summary})
    return p


def _level(conn, session, root):
    """The level in force, as "L<n>", for the waiver of a withdrawn row. A level that cannot be read
    is a refusal: a waiver is never recorded at a level nobody set."""
    import yaml
    from alpaca.posture import level
    try:
        n = level.in_force(conn, session, root=root)
    except (ValueError, TypeError, KeyError, OSError, yaml.YAMLError) as exc:
        raise IntakeError("the level in force cannot be read (%s: %s), so intake will not waive the "
                          "withdrawn rows; fix default_level in project.yaml or set the level, then run "
                          "intake again" % (type(exc).__name__, exc), code=BLOCKED)
    if not (level.MIN_LEVEL <= n <= level.MAX_LEVEL):
        raise IntakeError("the level in force reads as %r, outside L%d-L%d, so intake will not waive the "
                          "withdrawn rows" % (n, level.MIN_LEVEL, level.MAX_LEVEL), code=BLOCKED)
    return "L%d" % n


def public(p, dry_run):
    """The plan without its private parts, as printed with --json."""
    out = {k: v for k, v in p.items() if not k.startswith("_") and k != "root"}
    out["dry_run"] = dry_run
    out["changed"] = _changed(p)
    return out


def run(root, spec_path, runbook_path, *, op=None, dry_run=False, session="cli", actor=INSTRUMENT):
    conn = db.connect(root)
    p = plan(root, conn, spec_path, runbook_path, op=op, session=session, actor=actor)
    if not dry_run:
        apply(root, conn, p, session=session, actor=actor)
    return public(p, dry_run)


# ------------------------------------------------------------------------------ the verb
def _run_key(row):
    """Run order of a listed row: the position of its stage (`3/5 load-test`), then its key. A row
    without a position (withdrawn, or first shown by a recovery stage) comes after the others."""
    head = str(row.get("stage") or "").split(" ", 1)[0]
    number = head.split("/", 1)[0]
    return (not number.isdigit(), int(number) if number.isdigit() else 0, row["key"])


def _print(result):
    print("intake: %s (%s %s, %d required item(s)) with runbook %s (%s, %d stage(s)) into %s%s"
          % (result["spec"], result["format"], result["spec_shape"], result["required"], result["runbook"],
             result["runbook_id"], len(result["stages"]), result["op"],
             " -- dry run, nothing written" if result["dry_run"] else ""))
    rows = result["rows"]
    print("rows: %d added, %d kept, %d superseded, %d withdrawn"
          % (len(rows["added"]), len(rows["kept"]), len(rows["superseded"]), len(rows["withdrawn"])))
    # one list in run order (each group is in run order already; a withdrawn row has no stage)
    listed = []
    for mark in ("+", "~", "=", "-"):
        group = rows[{"+": "added", "~": "superseded", "=": "kept", "-": "withdrawn"}[mark]]
        listed += [(mark, r) for r in group]
    position = {r["key"]: n for n, r in enumerate(sorted(
        (r for _m, r in listed), key=lambda r: _run_key(r)))}
    for mark, r in sorted(listed, key=lambda mr: position[mr[1]["key"]]):
        stage = "  [%s]" % r["stage"] if r.get("stage") else ""
        if mark == "+":
            print("  + %s  %s%s  (%s: %s)" % (r["key"], r["row"], stage, r["step"], r["shown_by"]))
        elif mark == "~":
            print("  ~ %s  %s supersedes %s%s" % (r["key"], r["new"], r["old"], stage))
        elif mark == "=":
            print("  = %s  %s%s  [%s]" % (r["key"], r["row"], stage, r["status"]))
        else:
            print("  - %s  %s withdraws %s (waived)" % (r["key"], r["new"], r["old"]))
        if r.get("bar"):
            print("      bar: %s" % r["bar"])
    t = result["tasks"]
    print("tasks: %d added, %d contract(s) updated, %d kept, %d no longer in the runbook"
          % (len(t["added"]), len(t["contracts"]), len(t["kept"]), len(t["orphaned"])))
    for x in t["added"]:
        print("  + %s  %s  (stage %s)" % (x.get("task") or "(new)", x["title"], x["stage"]))
    for x in t["contracts"]:
        print("  ~ %s  contract of %s updated" % (x["task"], x["key"]))
    for x in t["orphaned"]:
        print("  ! %s  %s is no longer in the runbook; close or block the task by hand" % (x["task"], x["key"]))
    prof = result["profile"]
    print("profile: %s (%s); stages %s" % (prof["module"], prof["action"], ", ".join(prof["stages"])))
    for c in prof["plugin_checks"]:
        print("  plugin check %s/%s: %s" % (c["stage"], c["check"], c["script"]))
    for w in result["warnings"]:
        print("WARN %s" % w)
    if not result["changed"]:
        print("nothing to change: the record already matches the spec and the runbook")


def _from_caller(path):
    from alpaca import runbook
    return runbook._from_caller(path)


def cmd_intake(args):
    from alpaca import cli
    from alpaca.gates import verdict
    if not getattr(args, "spec", None) or not getattr(args, "runbook", None):
        print("usage: alpaca intake <spec> <runbook> [--op <op>] [--dry-run] [--json]", file=sys.stderr)
        return USAGE
    root = cli._root()
    try:
        result = run(root, _from_caller(args.spec), _from_caller(args.runbook), op=args.op,
                     dry_run=args.dry_run, session=args.session or "cli")
    except IntakeError as exc:
        if args.json:
            print(json.dumps({"verdict": verdict.name_of(exc.code), "reason": str(exc),
                              "detail": exc.detail}, indent=1))
        else:
            for line in exc.detail:
                print("ERROR %s" % line)
            print("GATE %s: %s (%s)" % (INSTRUMENT, verdict.name_of(exc.code), exc))
        return exc.code
    result["verdict"] = "PASS"
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True))
    else:
        _print(result)
        print(verdict.gate_line(INSTRUMENT, PASS))
    return PASS


def _parser(sub):
    p = sub.add_parser("intake", help="turn a spec and its runbook into the op's checklist rows, task "
                                      "contracts and profile; a spec change supersedes the rows it touches")
    p.add_argument("spec", nargs="?", help="a spec-kit spec.md (or its folder), an OpenSpec spec.md, "
                                           "openspec/specs, or an OpenSpec change folder")
    p.add_argument("runbook", nargs="?", help="the runbook.yaml that covers the spec")
    p.add_argument("--op", default=None, help="the op to fill (default: the open op opened last)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="print the plan, write nothing")
    p.add_argument("--json", action="store_true")


def _register():
    from alpaca import cli
    cli.command("intake")(cmd_intake)
    cli.register_parser("intake", _parser)


_register()
