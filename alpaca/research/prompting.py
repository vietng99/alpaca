"""Renders the role templates of prompts/ for the proposer, implementer, notes writer and the two
reproducer roles of repair mode; untrusted text goes between the markers of texts.py with the
fixed sentence.

Design: design/research-engine/architecture.md sections 3.6 and 6.8 (FR-003, FR-019, FR-043,
FR-044), gap map H17 and H19. A template is plain text with two kinds of placeholder:

  {{name}}             a trusted value from `context` (an id, a number, a path the engine made)
  {{untrusted:name}}   a value from `untrusted` (repository, issue, log or notes text), written
                       between texts.UNTRUSTED_BEGIN and texts.UNTRUSTED_END

A trailing `?` inside the braces ({{name?}}, {{untrusted:name?}}) makes a placeholder optional:
an absent value renders as "(none)". Any other absent value, and any context or untrusted key the
template does not use, raises ValueError, so no text is dropped or slipped in unseen.

The renderer, not the template, adds the fixed texts: texts.UNTRUSTED_SENTENCE once, just before
the first untrusted block; and for an attempt role texts.NO_INTERNET_PARAGRAPH and, as the last
line, one texts.ENGINE_MARKER line with the attempt id. Inside every inserted value the untrusted
markers and the engine marker are replaced by "[marker removed]", so a value can neither close
its own block early nor add a second marker line.
"""
from __future__ import annotations

import os
import re

from alpaca.research import paths, texts

_ATTEMPT_ROLES = ("proposer", "implementer", "notes", "reproducer-writer", "reproducer-reviewer")
_TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
_PLACEHOLDER = re.compile(r"\{\{(untrusted:)?([a-z][a-z0-9_]*)(\?)?\}\}")
_ROLE = re.compile(r"^[a-z][a-z-]{0,39}$")
_REMOVED = "[marker removed]"
_NONE = "(none)"


def _defang(value: str) -> str:
    text = str(value)
    for marker in (texts.UNTRUSTED_BEGIN, texts.UNTRUSTED_END, texts.ENGINE_MARKER):
        text = text.replace(marker, _REMOVED)
    return text


def _template(role: str) -> str:
    if not isinstance(role, str) or not _ROLE.fullmatch(role):
        raise ValueError("not a prompt role: %r" % (role,))
    path = os.path.join(_TEMPLATES, role + ".md")
    if not os.path.isfile(path):
        raise ValueError("no prompt template for role %r" % role)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _field_lines(contract, mode: str | None) -> str:
    """The proposal fields as the proposer must write them: the base fields of
    runbook-format-3.md section 5.1 for the mode, then the pack's own fields."""
    lines = ["- attempt (text): the attempt id on the last line of this prompt",
             "- title (text, one line, at most 120 characters)"]
    if tuple(getattr(contract, "categories", ()) or ()):
        lines.append("- category (text): the id of your hypothesis category, given above")
    lines.append("- hypothesis (text, at most 4000 characters): what the change is and why it "
                 "should help")
    lines.append("- files (list of paths, 1 to 50): the files you will change, each an editable "
                 "path")
    if mode == "repair":
        lines.append("- suspects (list of paths, 1 to 20): the files you believe own the bug")
        lines.append("- evidence (text, at most 4000 characters): the chain from the symptom "
                     "back to the owning file")
    else:
        lines.append("- expected (mapping of metric id to number): the value you expect for "
                     "each metric after the change")
    for field in tuple(getattr(contract, "proposal", ()) or ()):
        name = getattr(field, "field", None)
        if not name:
            continue
        kind = getattr(field, "type", "text")
        need = "required" if getattr(field, "required", True) else "optional"
        extra = ""
        values = tuple(getattr(field, "values", ()) or ())
        if values:
            extra = ", one of: " + ", ".join(str(v) for v in values)
        about = getattr(field, "description", None)
        lines.append("- %s (%s, %s%s)%s" % (name, kind, need, extra,
                                             ": " + str(about) if about else ""))
    return "\n".join(lines)


def _metric_lines(contract) -> str:
    rows = []
    for metric in tuple(getattr(contract, "metrics", ()) or ()):
        rows.append("- %s: %s is better (%s, unit %s)" % (
            getattr(metric, "id", "?"), getattr(metric, "direction", "?"),
            getattr(metric, "kind", "?"), getattr(metric, "unit", "?")))
    return "\n".join(rows) if rows else _NONE


def _list_lines(values) -> str:
    items = [str(v) for v in tuple(values or ())]
    return "\n".join("- " + v for v in items) if items else _NONE


def _derived(role: str, contract, context: dict) -> dict:
    """Values the renderer reads from the frozen contract when the caller did not pass them."""
    out: dict = {}
    mode = context.get("mode") or getattr(contract, "mode", None)
    if mode is not None:
        out["mode"] = mode
    out["note_chars"] = getattr(contract, "note_chars", None) or texts.NOTE_MAX_CHARS
    if contract is None:
        return out
    categories = tuple(getattr(contract, "categories", ()) or ())
    if categories and "slot" in context:
        slot = int(context["slot"])
        chosen = categories[slot % len(categories)]
        out["category_id"] = getattr(chosen, "id", str(chosen))
        out["category_text"] = getattr(chosen, "text", "")
    out["proposal_fields"] = _field_lines(contract, mode)
    out["metrics"] = _metric_lines(contract)
    out["editable"] = _list_lines(getattr(contract, "editable", ()))
    out["protected"] = _list_lines(getattr(contract, "protected", ()))
    return out


def render(role: str, *, contract, context: dict, untrusted: dict[str, str]) -> str:
    """prompts/<role>.md filled from context; every untrusted value wrapped in
    texts.UNTRUSTED_BEGIN/END, preceded once by texts.UNTRUSTED_SENTENCE; every attempt prompt
    carries texts.NO_INTERNET_PARAGRAPH and one texts.ENGINE_MARKER line with the attempt id last.

    An attempt prompt is one of _ATTEMPT_ROLES; its context must carry `attempt`, a safe task id
    (paths.check_name). The contract (a types.Contract, or None outside a run) supplies the
    slot's hypothesis category (slot modulo the number of categories, FR-043), the proposal
    fields, the metrics and the editable and protected paths, unless context names them.
    Raises ValueError for an unknown role, a missing value, an unused key or a bad attempt id.

    The keys each template reads (a caller passes exactly these; `?` marks an optional one,
    `*` one the renderer derives from the contract when context leaves it out; attempt is
    required for all three and read by the renderer, not the template):

      proposer     context: run, round, slot, mode*, cwd, category_id?*, category_text?*,
                   proposal_fields?*, metrics?*, editable?*, protected?*
                   untrusted: notes?, lessons?, bug_report?, hints?
      implementer  context: run, round, slot, mode*, cwd, editable?*, protected?*
                   untrusted: proposal, bug_report?, hints?
      notes        context: run, round, slot, cwd, note_chars*
                   untrusted: proposal?, verdict?

      reproducer-writer    context: run, case, cwd, draft, packet
                           untrusted: bug_report, hints?

      reproducer-reviewer  context: run, case, cwd, tests
                           untrusted: bug_report

    The two reproducer roles (t-101, repair.prove_reproducer) carry the repair task id as
    `attempt`. test_research_prompting.py checks this list against the templates."""
    template = _template(role)
    ctx = dict(context or {})
    raw_untrusted = dict(untrusted or {})
    values = _derived(role, contract, ctx)
    values.update(ctx)
    attempt = None
    if role in _ATTEMPT_ROLES:
        attempt = values.get("attempt")
        if attempt is None:
            raise ValueError("an attempt prompt needs context['attempt'] (%s)" % role)
        paths.check_name(str(attempt))
    if texts.ENGINE_MARKER in template:
        raise ValueError("template %s holds the engine marker" % role)
    used_context, used_untrusted = set(), set()
    blocks = []

    def fill(match):
        is_untrusted, name, optional = match.group(1), match.group(2), match.group(3)
        if is_untrusted:
            used_untrusted.add(name)
            if name not in raw_untrusted:
                if optional:
                    return _NONE
                raise ValueError("prompt %s needs untrusted[%r]" % (role, name))
            blocks.append(name)
            body = _defang(raw_untrusted[name]).rstrip("\n")
            return "%s\n%s\n%s" % (texts.UNTRUSTED_BEGIN, body, texts.UNTRUSTED_END)
        used_context.add(name)
        if name not in values or values[name] is None:
            if optional:
                return _NONE
            raise ValueError("prompt %s needs context[%r]" % (role, name))
        return _defang(values[name])

    body = _PLACEHOLDER.sub(fill, template)
    unused_context = set(ctx) - used_context - {"attempt", "mode", "slot"}
    if unused_context:
        raise ValueError("context keys the %s template does not use: %s"
                         % (role, ", ".join(sorted(unused_context))))
    unused_untrusted = set(raw_untrusted) - used_untrusted
    if unused_untrusted:
        raise ValueError("untrusted keys the %s template does not use: %s"
                         % (role, ", ".join(sorted(unused_untrusted))))
    if blocks:
        cut = body.index(texts.UNTRUSTED_BEGIN)
        line_start = body.rfind("\n", 0, cut) + 1
        body = body[:line_start] + texts.UNTRUSTED_SENTENCE + "\n\n" + body[line_start:]
    body = body.rstrip("\n")
    if attempt is not None:
        body += "\n\n" + texts.NO_INTERNET_PARAGRAPH + "\n\n%s %s" % (texts.ENGINE_MARKER, attempt)
    return body + "\n"
