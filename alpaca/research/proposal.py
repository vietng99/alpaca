"""The proposal shape check by code. The shape, the codes and these names are t-088's
(runbook-format-3.md section 5.2); t-100 writes the bodies.

A proposal is the one document a proposer writes before any worktree exists (FR-028). It is YAML
(JSON is YAML), at most MAX_BYTES, one mapping. The base fields are the engine's (section 5.1 of
runbook-format-3.md): attempt, title, category (when the contract has categories), hypothesis,
files, and expected (improve) or suspects and evidence (repair). The pack adds its own fields
with research.proposal (types.ProposalField). check() returns every finding, each
{"code", "where", "message"} as the runbook checker writes them (alpaca/runbook.py:119-120); any
finding makes the attempt broken with class `schema`, recorded before any worktree is cut
(FR-028, EC-034).

Design: design/research-engine/architecture.md section 3.8 and runbook-format-3.md section 5."""

from __future__ import annotations

import hashlib
import math
import os

from alpaca.research.types import Proposal


CODES = ("PROPOSAL-UNREADABLE", "PROPOSAL-FIELD-MISSING", "PROPOSAL-FIELD-UNKNOWN",
         "PROPOSAL-FIELD-TYPE", "PROPOSAL-VALUE-UNKNOWN", "PROPOSAL-TOO-LONG",
         "PROPOSAL-ATTEMPT-MISMATCH", "PROPOSAL-CATEGORY-MISMATCH", "PROPOSAL-PATH-OUTSIDE")


MAX_BYTES = 64 * 1024

# the engine's limits for the base fields (runbook-format-3.md section 5.1) and the default
# `max` of a pack field by its type (runbook-format-3.md section 4, `proposal[].max`)
_TITLE_MAX = 120
_TEXT_MAX = 4000
_FILES_MAX = 50
_SUSPECTS_MAX = 20
_TYPE_MAX = {"str": 200, "text": 4000, "list": 50, "paths": 50}


def _finding(code: str, where: str, message: str) -> dict:
    assert code in CODES, code
    return {"code": code, "where": where, "message": message}


def load(path) -> tuple:
    """(doc or None, findings): the proposal file read and parsed. A missing file, one larger than
    MAX_BYTES, text that is not YAML (JSON is YAML) or a document that is not one mapping gives
    None and one PROPOSAL-UNREADABLE finding; so does text the parser cannot take at all (a
    nesting so deep it exhausts the recursion limit), never an exception."""
    try:
        size = os.path.getsize(path)
    except (OSError, TypeError, ValueError) as exc:
        return None, [_finding("PROPOSAL-UNREADABLE", "", "no proposal file: %s" % (exc,))]
    if size > MAX_BYTES:
        return None, [_finding("PROPOSAL-UNREADABLE", "", "the proposal holds %d bytes, over the "
                               "%d byte limit" % (size, MAX_BYTES))]
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [_finding("PROPOSAL-UNREADABLE", "", "the proposal cannot be read: %s" % exc)]
    import yaml
    try:
        doc = yaml.safe_load(text)
    except RecursionError:
        # deeply nested brackets exhaust the parser's recursion: an agent's reply never stops
        # the round, it is a broken (schema) proposal like any other unreadable one
        return None, [_finding("PROPOSAL-UNREADABLE", "", "the proposal nests too deeply to "
                               "parse")]
    except Exception as exc:  # noqa: BLE001 - yaml.YAMLError, or anything else safe_load raises
        why = (str(exc).splitlines() or [type(exc).__name__])[0]
        return None, [_finding("PROPOSAL-UNREADABLE", "", "the proposal is not YAML or JSON: %s"
                               % why)]
    if not isinstance(doc, dict):
        return None, [_finding("PROPOSAL-UNREADABLE", "", "a proposal is one mapping, not %s"
                               % type(doc).__name__)]
    return doc, []


def _is_text(value) -> bool:
    return isinstance(value, str)


def _is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _path_findings(where: str, entry, contract, *, editable_needed: bool = True) -> list:
    """PROPOSAL-FIELD-TYPE for an entry that is not text; PROPOSAL-PATH-OUTSIDE for one that is
    absolute, holds `..`, is admitted by no editable rule or is hit by a protected rule."""
    from alpaca import barrier, runbook
    if not _is_text(entry) or not entry.strip():
        return [_finding("PROPOSAL-FIELD-TYPE", where, "a path entry is non-empty text")]
    p = entry.replace("\\", "/")
    if p.startswith("/") or ".." in p.split("/"):
        return [_finding("PROPOSAL-PATH-OUTSIDE", where, "%r is absolute or holds `..`" % entry)]
    editable = tuple(getattr(contract, "editable", ()) or ())
    protected = tuple(getattr(contract, "protected", ()) or ())
    if editable_needed and runbook.path_admitted(p, editable) is None:
        return [_finding("PROPOSAL-PATH-OUTSIDE", where, "%r is admitted by no editable rule (%s)"
                         % (entry, ", ".join(editable) or "none"))]
    hit = barrier.path_is_protected(p, protected)
    if hit is not None:
        return [_finding("PROPOSAL-PATH-OUTSIDE", where, "%r is hit by the protected rule %r"
                         % (entry, hit))]
    return []


def _check_text(where, value, *, most, one_line=False) -> list:
    if not _is_text(value):
        return [_finding("PROPOSAL-FIELD-TYPE", where, "the value is text, not %s"
                         % type(value).__name__)]
    if not value.strip():
        return [_finding("PROPOSAL-FIELD-TYPE", where, "the value is empty")]
    if one_line and ("\n" in value or "\r" in value):
        return [_finding("PROPOSAL-FIELD-TYPE", where, "the value is one line")]
    if most is not None and len(value) > most:
        return [_finding("PROPOSAL-TOO-LONG", where, "%d characters, over %d" % (len(value), most))]
    return []


def _check_paths(where, value, contract, *, most) -> list:
    if not isinstance(value, list):
        return [_finding("PROPOSAL-FIELD-TYPE", where, "the value is a list of paths, not %s"
                         % type(value).__name__)]
    if not value:
        return [_finding("PROPOSAL-FIELD-TYPE", where, "the list is empty; name at least one path")]
    out = []
    if most is not None and len(value) > most:
        out.append(_finding("PROPOSAL-TOO-LONG", where, "%d paths, over %d" % (len(value), most)))
    for i, entry in enumerate(value):
        out.extend(_path_findings("%s[%d]" % (where, i), entry, contract))
    return out


def _check_pack_field(field, value, contract) -> list:
    """One pack field of research.proposal against its type (runbook-format-3.md section 4)."""
    where, kind = field.field, field.type
    most = field.max if field.max is not None else _TYPE_MAX.get(kind)
    if kind in ("str", "text"):
        return _check_text(where, value, most=most, one_line=(kind == "str"))
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return [_finding("PROPOSAL-FIELD-TYPE", where, "the value is a whole number")]
        return []
    if kind == "float":
        if not _is_number(value):
            return [_finding("PROPOSAL-FIELD-TYPE", where, "the value is a finite number")]
        return []
    if kind == "bool":
        if not isinstance(value, bool):
            return [_finding("PROPOSAL-FIELD-TYPE", where, "the value is true or false")]
        return []
    if kind == "enum":
        if not _is_text(value):
            return [_finding("PROPOSAL-FIELD-TYPE", where, "the value is one word of %s"
                             % ", ".join(map(str, field.values)))]
        if value not in tuple(field.values):
            return [_finding("PROPOSAL-VALUE-UNKNOWN", where, "%r is not one of %s"
                             % (value, ", ".join(map(str, field.values))))]
        return []
    if kind == "list":
        if not isinstance(value, list) or not all(_is_text(v) for v in value):
            return [_finding("PROPOSAL-FIELD-TYPE", where, "the value is a list of text")]
        if most is not None and len(value) > most:
            return [_finding("PROPOSAL-TOO-LONG", where, "%d items, over %d" % (len(value), most))]
        return []
    if kind == "paths":
        return _check_paths(where, value, contract, most=most)
    return [_finding("PROPOSAL-FIELD-TYPE", where, "the pack field has an unknown type %r" % kind)]


def check(doc, contract, *, attempt: str, category: str | None) -> list:
    """Every finding of the proposal `doc` against the contract's shape; [] when it is valid.
    `attempt` is the attempt task id the prompt gave; `category` the id of the slot's hypothesis
    category, or None when the contract has none (runbook-format-3.md section 5.2)."""
    if not isinstance(doc, dict):
        return [_finding("PROPOSAL-UNREADABLE", "", "a proposal is one mapping, not %s"
                         % type(doc).__name__)]
    mode = getattr(contract, "mode", "improve")
    has_categories = bool(tuple(getattr(contract, "categories", ()) or ()))
    base = ["attempt", "title", "hypothesis", "files"]
    if has_categories:
        base.append("category")
    base += ["suspects", "evidence"] if mode == "repair" else ["expected"]
    pack = {f.field: f for f in tuple(getattr(contract, "proposal", ()) or ())}
    out = []
    for key in doc:
        if not isinstance(key, str) or (key not in base and key not in pack):
            out.append(_finding("PROPOSAL-FIELD-UNKNOWN", str(key), "%r is neither a base field "
                                "of mode %s nor a field of the pack" % (key, mode)))
    for key in base:
        if key not in doc:
            out.append(_finding("PROPOSAL-FIELD-MISSING", key, "the base field %r is missing"
                                % key))
    for name, field in pack.items():
        if field.required and name not in doc:
            out.append(_finding("PROPOSAL-FIELD-MISSING", name, "the pack field %r is missing"
                                % name))
    if "attempt" in doc:
        value = doc["attempt"]
        if not _is_text(value):
            out.append(_finding("PROPOSAL-FIELD-TYPE", "attempt", "the attempt id is text"))
        elif value != attempt:
            out.append(_finding("PROPOSAL-ATTEMPT-MISMATCH", "attempt", "%r is not the attempt "
                                "id %r" % (value, attempt)))
    if "title" in doc:
        out.extend(_check_text("title", doc["title"], most=_TITLE_MAX, one_line=True))
    if "category" in doc and "category" in base:
        value = doc["category"]
        if not _is_text(value):
            out.append(_finding("PROPOSAL-FIELD-TYPE", "category", "the category id is text"))
        elif value != category:
            out.append(_finding("PROPOSAL-CATEGORY-MISMATCH", "category", "%r is not the slot's "
                                "category %r" % (value, category)))
    if "hypothesis" in doc:
        out.extend(_check_text("hypothesis", doc["hypothesis"], most=_TEXT_MAX))
    if "files" in doc:
        out.extend(_check_paths("files", doc["files"], contract, most=_FILES_MAX))
    if mode == "repair":
        if "suspects" in doc:
            out.extend(_check_paths("suspects", doc["suspects"], contract, most=_SUSPECTS_MAX))
        if "evidence" in doc:
            out.extend(_check_text("evidence", doc["evidence"], most=_TEXT_MAX))
    elif "expected" in doc:
        value = doc["expected"]
        metrics = {m.id for m in tuple(getattr(contract, "metrics", ()) or ())}
        if not isinstance(value, dict) or not value:
            out.append(_finding("PROPOSAL-FIELD-TYPE", "expected", "a mapping of metric id to "
                                "number with at least one entry"))
        else:
            for mid, number in value.items():
                where = "expected.%s" % (mid,)
                if mid not in metrics:
                    out.append(_finding("PROPOSAL-FIELD-UNKNOWN", where, "%r names no metric of "
                                        "the contract" % (mid,)))
                elif not _is_number(number):
                    out.append(_finding("PROPOSAL-FIELD-TYPE", where, "the expected value is a "
                                        "finite number"))
    for name, field in pack.items():
        if name in doc:
            out.extend(_check_pack_field(field, doc[name], contract))
    return out


def parse(final_message: str, contract, *, attempt: str, category: str | None,
          path: str) -> Proposal:
    """The proposer's final message written to `path` (attempts/<task>/proposal.json), then load
    and check; a Proposal when there is no finding, else ValueError with the findings as its
    second argument (exc.args[1])."""
    data = (final_message if isinstance(final_message, str) else "").encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    doc, findings = load(path)
    if not findings:
        try:
            findings = check(doc, contract, attempt=attempt, category=category)
        except RecursionError:
            findings = [_finding("PROPOSAL-UNREADABLE", "", "the proposal nests too deeply to "
                                 "check")]
    if findings:
        raise ValueError("the proposal of %s fails its shape: %s" % (
            attempt, "; ".join("%s %s" % (f["code"], f["where"]) for f in findings)), findings)
    return Proposal(attempt=attempt, category=category or "", fields=dict(doc),
                    sha256=hashlib.sha256(data).hexdigest(), path=path)
