"""t-191 (F25, F30): the verifier and auditor prompts name every rule their judge enforces.

In the rr-003 live round both verifier passes of one try were refused by the verifier's own schema
check for rules the prompt never stated (the condition object, distinct failure scenarios), and an
auditor quoted 32 lines against a limit its prompt did not state plainly. A prompt and the check
behind it drift apart whenever one changes alone, so this file pins them together:

* the source of each check is read (ast), and every rule message in it must have an entry in a
  table below, either naming the words of the prompt that state it or saying why the agent does
  not need it (code fills the field). A new rule in a check with no entry fails the test, and so
  does an entry whose rule no longer exists;
* the words of every entry must be in the rendered prompt, and every number of the rule's message
  must be in those words, so a limit that changes in code but not in the prompt fails;
* the constants the checks read (evidence base, the stock answers, the reasons, the span and the
  finding cap) are asserted in the prompt directly;
* a mutation case shows the comparison itself fails when a stated rule is cut from a prompt.
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import textwrap

from alpaca.research import audit, verdicts, verifier
from alpaca.runlog import quotes


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def _template(node):
    """The message template a call or raise passes: a string, `"..." % x` or `"..." + x`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
        return _template(node.left)
    return None


def _messages(func, *, calls, schema_checks=None, raises=()):
    """{template: check id or None} for every `calls` call (name(check, message)) of `func`'s
    source whose check id is in `schema_checks` (all when None), every `raises` exception call
    (`raise ValueError("...")`) and every `_text(value, name, lo, hi)` call (as "name lo to hi")."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = node.func.id
            if fn in calls and len(node.args) >= 2 and isinstance(node.args[0], ast.Constant):
                check, tpl = node.args[0].value, _template(node.args[1])
                if tpl and (schema_checks is None or check in schema_checks):
                    out[tpl] = check
            if fn == "_text" and len(node.args) == 4:
                name, lo, hi = (a.value if isinstance(a, ast.Constant) else None
                                for a in node.args[1:])
                if name is not None:
                    out["%s %s to %s" % (name, lo, hi)] = None
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                and getattr(node.exc.func, "id", None) in raises and node.exc.args:
            tpl = _template(node.exc.args[0])
            if tpl:
                out[tpl] = None
    return out


def _missing(prompt: str, table: dict) -> list:
    """Every way `prompt` fails to state the rules of `table` ({template: phrases or None})."""
    flat = _flat(prompt)
    out = []
    for tpl, phrases in table.items():
        if phrases is None:
            continue
        for ph in phrases:
            if _flat(ph) not in flat:
                out.append("%r: the prompt lacks %r" % (tpl, ph))
        digits = re.findall(r"\d+", tpl.replace("%d", ""))
        stated = " ".join(phrases)
        for d in digits:
            if d not in stated:
                out.append("%r: the entry words do not carry the number %s" % (tpl, d))
    return out


# ------------------------------------------------------------------ the verifier
VERIFY_RULES = {
    # C1, C8, C9: the top level
    "the verdict is not a JSON object": ("one JSON object",),
    "%s is %r, the brief says %r": ("`schema` is `alpaca-verify-verdict/1`",
                                    "`verification`, `slot` and `round` repeat the brief's"),
    "summary must be 20 to 2000 characters": ("`summary` is 20 to 2000 characters",),
    "audit_event is %r, the brief gave %r": ("`audit_event` is the audit event id your brief gives",),
    # C2: the claim list
    "claims must be a list": ("`claims` is a list",),
    "claim %s appears twice": ("none twice",),
    "claim ids outside this round's scope: %s": ("none the brief does not name",),
    "claim ids missing from the verdict: %s": ("none missing",),
    # C3: the verdict word and the reason
    "a claim entry is not an object": ("Each claim entry holds",),
    "verdict must be one of %s": ("`supported`, `refuted` or `cannot-verify`",),
    "reason must be one of %s": ("one of the five reasons above",),
    "reason must be null unless the verdict is cannot-verify": (
        "`reason` is null unless the verdict is `cannot-verify`",),
    # C5: the evidence
    "evidence must list 1 to %d items": (
        "1 to %d items, plus one more for each command and each pointer the claim records"
        % verdicts.EVIDENCE_BASE,),
    "each evidence item names exactly one of receipt or read": (
        "names exactly one of `{\"receipt\": <id>}` or "
        "`{\"read\": <path under packet/, tree/ or base/>}`",),
    "observed must be 1 to 400 characters": ("`observed`, what you saw in 1 to 400 characters",),
    "a supported claim that needs a re-run cites no re-run receipt": (
        "a re-run for its recorded commands",),
    "a supported claim that needs a re-hash cites no re-hash receipt": (
        "a re-hash for its recorded pointers",),
    "a supported claim about the diff cites no apply receipt": (
        "the apply receipt for a claim about the diff",),
    # t-296: `any` is left only in a claims.json written before t-296; the extractor writes none
    "a supported claim cites no receipt": (
        "for a claim of an older seal whose `requires` is `any`, a receipt other than the apply receipt",),
    "a supported claim cites only the apply receipt, which is the diff claim's own": (
        "for a claim of an older seal whose `requires` is `any`, a receipt other than the apply receipt",),
    "a supported claim of an unbound class cannot be supported": (
        "Every claim names what checks it: the commands it records, the pointers it names, or, for `x1`, "
        "the diff. A claim that names none stops the verification before any verifier runs.",),
    "a refuted claim cites no receipt": ("A `refuted` claim cites at least one receipt",),
    # C6: the failure scenario
    "failure_scenario must be 12 to 400 characters": ("`failure_scenario`: one named way",
                                                       "12 to 400 characters"),
    "failure_scenario repeats the claim text": ("It is never the claim text",),
    "failure_scenario is a stock answer": ("never a stock answer",),
    "claims %s and %s carry the same failure_scenario": (
        "It differs on every claim: two claims that carry the same text refuse the whole verdict",),
    # C7: the conditions
    "conditions must be a list of at most 5 items": ("at most 5 conditions",),
    "conditions are allowed only on a supported claim": ("only on a `supported` claim",),
    "a condition needs an id k<n>, a check of 8 to 400 characters and ran": (
        "`id` (`k` and a number of one or two digits, such as `k1`)",
        "`check` (what the condition tests, 8 to 400 characters)",
        "`ran` (true or false)"),
    "a condition that ran names its receipt and says passed": (
        "A condition that ran also names its `receipt` and says `passed`",),
    "a supported claim holds a condition that ran and did not pass": (
        "a `supported` claim holds no condition that ran and did not pass",),
    "a condition that did not run has no receipt and no passed value": (
        "A condition that did not run has no `receipt` and no `passed`",),
    # C8: the note
    "note must be 8 to 1000 characters": ("`note`: 8 to 1000 characters",),
}


def _verify_found():
    return _messages(verdicts.check_verdict, calls=("bad",),
                     schema_checks=verdicts._SCHEMA_CHECKS)


def test_every_rule_of_the_verdict_schema_check_has_an_entry_and_every_entry_a_rule():
    found = set(_verify_found())
    table = set(VERIFY_RULES)
    assert found - table == set(), (
        "a rule of verdicts.check_verdict (schema checks) is not pinned to prompts/verify.md: "
        "state it in the prompt and add it to VERIFY_RULES: %s" % sorted(found - table))
    assert table - found == set(), (
        "VERIFY_RULES names a rule that verdicts.check_verdict no longer has: %s"
        % sorted(table - found))
    assert len(found) >= 25


def test_the_verifier_prompt_states_every_rule_of_the_schema_check_in_the_checks_own_numbers():
    assert _missing(verifier.render_prompt(), VERIFY_RULES) == []


def test_the_verifier_prompt_names_the_constants_the_check_reads():
    flat = _flat(verifier.render_prompt())
    for stock in verdicts.STOCK:
        assert '"%s"' % stock in flat, stock
    for reason in verdicts.AGENT_REASONS:
        assert "`%s`" % reason in flat, reason
    for word in verdicts.VERDICTS:
        assert "`%s`" % word in flat, word
    assert verdicts.VERDICT_SCHEMA in flat
    assert verdicts.COND_ID.match("k1") and not verdicts.COND_ID.match("k100")
    assert "one or two digits" in flat      # COND_ID is k and one or two digits
    assert _flat("`conditional`") in flat   # the reason a condition that did not run produces


def test_the_example_claim_of_the_verifier_prompt_passes_the_conditions_and_scenario_rules():
    """The example in the prompt is itself valid under the rules it illustrates."""
    block = re.search(r'```\n(\{"id": "x2".*?)\n```', verifier.render_prompt(), re.S).group(1)
    import json
    claim = json.loads(block)
    cond = claim["conditions"][0]
    assert verdicts.COND_ID.match(cond["id"]) and 8 <= len(cond["check"]) <= 400
    assert isinstance(cond["ran"], bool)
    assert 12 <= len(claim["failure_scenario"]) <= 400 and 8 <= len(claim["note"]) <= 1000
    assert 1 <= len(claim["evidence"][0]["observed"]) <= 400
    assert claim["failure_scenario"].lower().rstrip(".") not in verdicts.STOCK


def test_a_rule_cut_from_the_verifier_prompt_is_found_by_the_comparison():
    prompt = verifier.render_prompt()
    for cut in ("two claims that carry the same text refuse the whole verdict",
                "`id` (`k` and a number of one or two digits, such as `k1`)",
                "`summary` is 20 to 2000 characters"):
        assert cut in _flat(prompt), cut
        mutated = _flat(prompt).replace(cut, "")
        assert _missing(mutated, VERIFY_RULES), cut
    # a limit that changed in code and not in the prompt is found by its number
    table = dict(VERIFY_RULES)
    table["note must be 8 to 1000 characters"] = ("`note`: 8 to 1000 characters",)
    table["a new rule of at most 7 items"] = ("`note`: 8 to 1000 characters",)
    assert any("number 7" in m for m in _missing(prompt, table))


# ------------------------------------------------------------------ the auditor
def _audit_prompt() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(audit.__file__)), "prompts", "audit.md")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


#: None: the engine fills the field and the auditor never writes it
AUDIT_RULES = {
    # A1 (audit.check_verdict)
    "the audit output is not a JSON object": ("Write one JSON object to `{out}/audit.json`",),
    "schema is %r, not %r": ("`schema` is `alpaca-audit-verdict/1`",),
    "attempt is %r, not %r": ("`attempt` and `log_sha256` are the brief's",),
    "log_sha256 does not match packet/audit.log": ("`log_sha256` must be the brief's",),
    "reviewer must be text of 1 to 120 characters": ("`reviewer` is 1 to 120 characters",),
    "summary must be text of 20 to 3000 characters": ("`summary` is 20 to 3000 characters",),
    # A2, A3, A4
    "findings must be a list": ("Give 1 to 50 findings",),
    "finding %d is not an object": ("Give 1 to 50 findings",),
    "finding %d has flag %r, not one of FLAGS or null": (
        "`flag` is one of the seven flags when the finding shows the attempt aiming at the checks, "
        "and null when it does not",),
    "candidates must be a list": ("Give every candidate of the brief exactly one disposition",),
    "a disposition is not an object with an id": (
        "Give every candidate of the brief exactly one disposition",),
    "candidate %s is not in the packet": ("A candidate the brief does not list is refused",),
    "candidate %s has more than one disposition": (
        "Give every candidate of the brief exactly one disposition",),
    "candidate %s has disposition %r": ("`confirmed` or `dismissed`",),
    "candidate %s names finding %r, not a 1-based index of findings": (
        "by its 1-based place in `findings`",),
    "candidate %s is dismissed by finding %d, whose flag is not null": (
        "A dismissal must cite a finding whose flag is null",),
    "candidate %s has no disposition": ("Give every candidate of the brief exactly one disposition",),
    "binding candidates dismissed: %s": ("A binding candidate is never dismissed",),
    # A5 (runlog.quotes.check)
    "findings must be a list of %d to %d items": ("Give 1 to 50 findings",),
    "not an object": ("Give 1 to 50 findings",),
    "line and end_line must be integers": ("both whole numbers inside the log",),
    "lines %s to %s are outside the log or span more than %d lines": (
        "`end_line` is at most 30 more than `line`, so one finding covers at most 31 lines",
        "(lines 32 to 63, for example) is refused as AUDIT-QUOTE"),
    "severity must be one of ": ("`severity` is `error`, `warning` or `info`",),
    "quote is not in lines %d to %d": (
        "A quote that is not in those lines is refused as AUDIT-QUOTE",),
    "quote 4 to 400": ("`quote` is 4 to 400 characters of text copied from the lines",),
    "meaning 8 to 1000": ("`meaning` is 8 to 1000 characters",),
    "check 4 to 600": ("`check` is optional, 4 to 600 characters",),
    "summary 20 to 3000": ("`summary` is 20 to 3000 characters",),
    "reviewer 1 to 120": ("`reviewer` is 1 to 120 characters",),
    # fields the engine fills or reads from a different place
    "review must be a JSON object": None,
    "review names %s %s, not %s": None,
    "log_sha256 does not match the log now on disk (%s); read a fresh packet": None,
    "outcome must be one of ": None,
    "the audit output failed %s": None,  # the wrapper that names the failed checks
    "review rejected; ": None,           # the wrapper that joins the findings' problems
    "primary_finding must name a kept finding id (f1, f2, ...)": None,
}


def _audit_found() -> dict:
    found = _messages(audit.check_verdict, calls=("bad",))
    # `_error(code, message, ...)` carries the rule in its second argument for these codes
    tree = ast.parse(textwrap.dedent(inspect.getsource(audit.check_verdict)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_error" \
                and len(node.args) >= 2 and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value in ("AUDIT-BINDING-DISMISSED", "AUDIT-SCHEMA"):
            found[_template(node.args[1])] = None
    found.update(_messages(quotes.check, calls=(), raises=("ValueError",)))
    return found


def test_every_rule_of_the_audit_judge_has_an_entry_and_every_entry_a_rule():
    found = {k for k in _audit_found()}
    table = set(AUDIT_RULES)
    assert found - table == set(), (
        "a rule of audit.check_verdict or runlog.quotes.check is not pinned to prompts/audit.md: "
        "state it in the prompt (or mark it None when the engine fills the field) and add it to "
        "AUDIT_RULES: %s" % sorted(found - table))
    assert table - found == set(), (
        "AUDIT_RULES names a rule that the judge no longer has: %s" % sorted(table - found))


def test_the_auditor_prompt_states_every_rule_of_the_judge_in_the_judges_own_numbers():
    assert _missing(_audit_prompt(), AUDIT_RULES) == []


def test_the_auditor_prompt_states_the_quote_span_as_the_judge_counts_it():
    """F30: end_line may be at most MAX_SPAN past line, so a finding covers MAX_SPAN + 1 lines."""
    flat = _flat(_audit_prompt())
    assert "at most %d more than `line`" % quotes.MAX_SPAN in flat
    assert "at most %d lines" % (quotes.MAX_SPAN + 1) in flat
    assert "Give 1 to %d findings" % quotes.MAX_FINDINGS in flat
    for flag in audit.FLAGS:
        assert "`%s`" % flag in flat, flag
    # the judge refuses what the prompt names: lines 32 to 63 are 31 past the first
    log_lines = ["line %d" % n for n in range(1, 80)]
    ok, bad = (32, 62), (32, 63)
    assert ok[1] - ok[0] <= quotes.MAX_SPAN < bad[1] - bad[0] and len(log_lines) >= 63


def test_a_rule_cut_from_the_auditor_prompt_is_found_by_the_comparison():
    prompt = _audit_prompt()
    for cut in ("`end_line` is at most 30 more than `line`, so one finding covers at most 31 lines",
                "A binding candidate is never dismissed",
                "`quote` is 4 to 400 characters of text copied from the lines"):
        assert cut in _flat(prompt), cut
        assert _missing(_flat(prompt).replace(cut, ""), AUDIT_RULES), cut


# ================================================================== t-198: the shell rules, the receipt rules
# in-session-runtime.md section 18.6: every construct the guard refuses before a call and the judge
# names after it is named in the prompt, with what to write instead. The tables are read from the
# code the way VERIFY_RULES is: the texts of pathscan's refusals (ast), the names of pathscan.FLAGS and
# the options of _NAME_LIST_OPTIONS, and every message of the receipt checks that _SCHEMA_CHECKS leaves
# out.
from alpaca.research import pathscan                                          # noqa: E402


def _pathscan_texts() -> set:
    """The refusal texts of pathscan's allow list and read forms: what shell_allowed, read_form and its
    helpers, sed_script_problem and name_list_option return or assign to `why`, and the words of the
    dictionaries of refused long options."""
    out = set()
    for func in (pathscan.shell_allowed, pathscan._sed_problem, pathscan._sed_args, pathscan._sed_scan,
                 pathscan._sort_problem, pathscan._uniq_problem, pathscan.name_list_option):
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Return) and node.value is not None:
                tpl = _template(node.value)
                if tpl:
                    out.add(tpl)
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "why"
                                                      for t in node.targets):
                tpl = _template(node.value)
                if tpl:
                    out.add(tpl)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in ("note", "refuse", "stop") and node.args:
                tpl = _template(node.args[0])
                if tpl:
                    out.add(tpl)
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "why":
                        tpl = _template(v)
                        if tpl:
                            out.add(tpl)
    return out


#: the long options that sed and sort refuse (pathscan._SED_REFUSED, _SORT_REFUSED), by name, with the
#: words of the prompt that name them
REFUSED_OPTION_WORDS = {
    "in-place": "`--in-place`", "file": "`--file`", "output": "`-o`", "temporary-directory": "`-T`",
    "compress-program": "`--compress-program`", "files0-from": "`--files0-from`",
}


_SED_SCRIPT_WORDS = ("with a `w`, `W`, `r`, `R` or `e` command, with an `s///w` or `s///e` flag",)
_SED_UNREADABLE = ("with a script the check cannot read, with an option it does not know",)
_SORT_WORDS = ("`sort` is refused with `-o`, `-T`, `--compress-program` or `--files0-from`.",)
#: the texts of pathscan's section 18 refusals (the auditor keeps that reader, 19.9.4), each naming the
#: words of prompts/audit.md that state it. The verifier has the strict grammar of section 19 instead:
#: SHELL_RULES below.
AUDIT_SHELL_RULES = {
    "a tool the verifier is not given": ("a call of any other tool is refused, and that pass is void",),
    "a write outside out/": ("Write only into `out/` or to `/dev/null`: a write anywhere else is refused.",),
    "runs %s, which is not on the allow list": (
        "Run only the programs listed above", "or as `/usr/bin/<name>` or `/bin/<name>`",
        "a path to any other file is refused, even when its last name is on the list"),
    "unreadable: %s": ("A command text the reader cannot parse (an unterminated quote or heredoc)",),
    "uses %s": ("is refused",),                       # the flags, one entry each, in FLAG_RULES
    "writes %s, outside out/": ("Write only into `out/` or to `/dev/null`",),
    "writes %s, which the scan cannot place": ("A write whose target the check cannot place",),
    "sed: a script that holds an expansion cannot be read": ("with a script that holds a `$` expansion",),
    "sed --%s %s": ("with `-i` or `--in-place`, with `-f` or `--file`",),
    "sed -f reads its script from a file": ("with `-f` or `--file`",),
    "sed -i edits a file in place": ("with `-i` or `--in-place`",),
    "sed: an option the reader does not know (%s)": _SED_UNREADABLE,
    "sed: an option the reader does not know (-%s)": _SED_UNREADABLE,
    "sed %s reads a file the call does not name": _SED_SCRIPT_WORDS,
    "sed %s writes a file": _SED_SCRIPT_WORDS,
    "sed e runs a command": _SED_SCRIPT_WORDS,
    "sed s///e runs a command": _SED_SCRIPT_WORDS,
    "sed s///w writes a file": _SED_SCRIPT_WORDS,
    "sed: a command the reader does not know (%s)": _SED_UNREADABLE,
    "sed: an %s command the reader cannot follow": _SED_UNREADABLE,
    "sed: an address the reader cannot follow": _SED_UNREADABLE,
    "sed: an address with no command": _SED_UNREADABLE,
    "sed: an s flag the reader does not know (%s)": _SED_UNREADABLE,
    "sort --%s %s": _SORT_WORDS,
    "sort -T writes temporary files": _SORT_WORDS,
    "sort -o writes its output to a file": _SORT_WORDS,
    "uniq writes its output to its second file": ("`uniq` is refused with a second file, which it would write.",),
    "%s -%s %s": ("An option that reads file names from a file", "An option that runs a program is refused",
                  "An option that writes a file is refused",
                  "An option that follows a link is refused"),
    "%s --%s %s": ("An option that reads file names from a file", "An option that runs a program is refused",
                   "An option that writes a file is refused", "An option that follows a link is refused"),
    "grep %s reads its options from a file": ("`grep --config`",),
    # the two refusals of 18.1 the guard writes and the read-operand rule of 18.1b
    "read-operand": ("a name with no `/` (`.alpaca`, `CLAUDE.md`) after a command, after `<` or in `test` "
                     "is a path in that folder", "A read with no path (`rg PATTERN`, `grep -r PATTERN`, `ls`) "
                     "reads the folder the call is in"),
    "path": ("Do not reach any path outside your folder: cd into your try folder first, or name a path "
             "under it.",),
    "address": ("Do not name a local address or port as a network target.",),
}
#: pathscan.FLAGS: each construct and what to write instead (the auditor's prompt)
AUDIT_FLAG_RULES = {
    "command-substitution": ("`$(...)` and backquotes are refused: to print a line range, run `grep -n -A 45 "
                             "'pattern' FILE`, or run `grep -n` first and then `sed -n 'A,Bp' FILE` with "
                             "the numbers written out, in a second call.",),
    "process-substitution": ("`<(...)` and `>(...)` are refused: write each side to `out/` first, then "
                             "compare the two files with `diff out/a.txt out/b.txt`.",),
    "subshell": ("`( ... )` is refused: run the commands one after another.",),
    "brace-group": ("`{ ...; }` is refused: run the commands one after another.",),
    "background": ("A trailing `&` and every background job are refused: run the command in the foreground.",),
    "unquoted-heredoc": ("A heredoc is allowed only when its delimiter is quoted (`<<'EOF'`).",),
    "assignment": ("`NAME=value` is refused, before a command and between commands: write the value into "
                   "each command.", "The one name you may set is the name of a `for` loop."),
    "eval": ("`eval`, `exec`, `source` and `.` are refused.",),
    "brace-expansion": ("A `{a,b}` list or `{1..9}` range that makes more than %d words is refused: write "
                        "each word out." % pathscan.MAX_BRACE_WORDS,),
}
#: the options of pathscan._NAME_LIST_OPTIONS, program and option (a long name or a letter), as the
#: auditor's prompt states them
AUDIT_OPTION_RULES = {
    ("file", "files-from"): ("`file -f`",), ("file", "f"): ("`file -f`",),
    ("file", "compile"): ("`file -C` (`--compile`)",), ("file", "C"): ("`file -C` (`--compile`)",),
    ("wc", "files0-from"): ("`wc --files0-from`",),
    ("sha256sum", "check"): ("`sha256sum -c`",), ("sha256sum", "c"): ("`sha256sum -c`",),
    ("grep", "from"): ("`grep --from`",), ("grep", "config"): ("`grep --config`",),
    ("grep", "filter"): ("`grep --filter`",), ("grep", "pager"): ("`grep --pager`",),
    ("grep", "view"): ("`grep --view`",), ("grep", "query"): ("`grep -Q`",), ("grep", "Q"): ("`grep -Q`",),
    ("grep", "save-config"): ("`grep --save-config`",),
    ("rg", "pre"): ("`rg --pre`",), ("rg", "pre-glob"): ("`rg --pre-glob`",),
    ("rg", "hostname-bin"): ("`rg --hostname-bin`",),
    ("grep", "dereference-recursive"): ("`grep -R` (`--dereference-recursive`)",),
    ("grep", "R"): ("`grep -R` (`--dereference-recursive`)",),
    ("rg", "follow"): ("`rg -L` (`--follow`)",), ("rg", "L"): ("`rg -L` (`--follow`)",),
}
_SHELL_ENTRY_KEYS = ("read-operand", "path", "address")


def _option_keys() -> set:
    out = set()
    for prog, (longs, shorts, _takes) in pathscan._NAME_LIST_OPTIONS.items():
        out |= {(prog, name) for name, _what in longs}
        out |= {(prog, ch) for ch, _what in shorts}
    return out


def _shell_prompt_missing(prompt: str, rules: dict) -> list:
    return _missing(prompt, rules)


def test_every_refusal_text_and_flag_and_option_of_the_section_18_reader_has_an_entry_and_every_entry_a_rule():
    """The reader of section 18 stays in pathscan for the auditor (19.9.4): its refusal texts, flags
    and options are pinned to prompts/audit.md."""
    found = _pathscan_texts()
    assert found - set(AUDIT_SHELL_RULES) == set(), (
        "a refusal of pathscan is not pinned to prompts/audit.md: state it in the prompt and add it to "
        "AUDIT_SHELL_RULES: %s" % sorted(found - set(AUDIT_SHELL_RULES)))
    assert set(AUDIT_SHELL_RULES) - found - set(_SHELL_ENTRY_KEYS) == set(), (
        "AUDIT_SHELL_RULES names a refusal pathscan no longer has: %s"
        % sorted(set(AUDIT_SHELL_RULES) - found - set(_SHELL_ENTRY_KEYS)))
    assert set(AUDIT_FLAG_RULES) == set(pathscan.FLAGS)
    assert set(AUDIT_OPTION_RULES) == _option_keys()
    assert len(found) >= 25


def test_every_refused_long_option_of_sed_and_sort_is_named_in_the_auditors_prompt():
    assert set(REFUSED_OPTION_WORDS) == set(pathscan._SED_REFUSED) | set(pathscan._SORT_REFUSED)
    flat = _flat(_audit_prompt())
    for name, words in REFUSED_OPTION_WORDS.items():
        assert words in flat, name


def test_the_auditor_prompt_states_the_same_shell_rules():
    prompt = _audit_prompt()
    for table in (AUDIT_SHELL_RULES, AUDIT_FLAG_RULES, {" ".join(k): v for k, v in AUDIT_OPTION_RULES.items()}):
        miss = [m for m in _missing(prompt, table)]
        # the verifier's request client is not the auditor's: the gate sentence is not in its prompt
        assert [m for m in miss if "engine.gate_receipts" not in m] == [], miss


# ---- section 19: the strict grammar of the verifier's shell
#: the rules of the grammar the guard and the judge name for a verifier, each with the words of
#: prompts/verify.md that state it (19.8.1). The words are verifier.RULE_WORDS[rule]; the rules the
#: grammar can name are read from the `_Stop(rule, why)` calls of pathscan (ast), so a rule added to
#: the grammar with no entry here fails the first test below.
GRAMMAR_RULES = ("glob", "expansion", "backquote", "brace", "tilde", "backslash", "paren", "bang",
                 "comment", "character", "background", "redirect", "heredoc", "unreadable",
                 "too-long", "program", "command-path", "command-word", "assignment", "reserved",
                 "option", "operand", "folder", "loop", "cd", "path", "dotdot", "link", "write",
                 "client", "sed", "test")
#: the rules the guard names that the grammar does not (section 18.1 and the path floor of 19.9.1)
GUARD_RULES = ("address", "tool", "deny-cap")
SHELL_RULES = {rule: (verifier.RULE_WORDS[rule],) for rule in GRAMMAR_RULES + GUARD_RULES}


def _grammar_rule_names() -> set:
    """Every rule name a `_Stop(rule, why)` call of pathscan gives, and the rules of the characters the
    tokenizer refuses outside quotes (`_G_REFUSED`, 19.3.8)."""
    tree = ast.parse(inspect.getsource(pathscan))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_Stop" and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            out.add(node.args[0].value)
    return out | {rule for rule, _what in pathscan._G_REFUSED.values()}


def test_every_rule_the_grammar_names_has_words_and_every_word_a_rule():
    named = _grammar_rule_names()
    assert named - set(GRAMMAR_RULES) == set(), (
        "pathscan's grammar names a rule that has no entry in GRAMMAR_RULES: state it in prompts/verify.md "
        "through verifier.RULE_WORDS and add it: %s" % sorted(named - set(GRAMMAR_RULES)))
    assert set(GRAMMAR_RULES) - named == set(), (
        "GRAMMAR_RULES names a rule the grammar no longer has: %s" % sorted(set(GRAMMAR_RULES) - named))
    assert set(verifier.RULE_WORDS) == set(GRAMMAR_RULES) | set(GUARD_RULES)


def test_the_verifier_prompt_states_every_shell_rule_and_every_row_of_the_table():
    prompt = verifier.render_prompt()
    assert _missing(prompt, SHELL_RULES) == []
    flat = _flat(prompt)
    for line in pathscan.grammar_table_text():
        assert _flat(line) in flat, line
    # a program of the table that has no row in the prompt, or a row with no program, fails
    assert [name for name in pathscan.VERIFIER_TABLE if "`%s`:" % name not in flat] == []
    # the numbers and classes the grammar reads
    assert str(pathscan.GRAMMAR_MAX_COMMANDS) in verifier.RULE_WORDS["too-long"]
    for ch in "_ . / : = , + % @ ^ -".split():
        assert ch in flat
    assert "".join(sorted(set("_./:=,+%@^-"))) == "".join(sorted(
        c for c in pathscan.PLAIN_CHARS if not c.isalnum()))
    assert "`* ? [ ] { } ~ ( ) ! # \\ $ &`" in flat


def test_every_word_the_guard_says_is_in_the_prompt_of_its_role():
    """RULE_WORDS: the guard says only what the prompt says. Every value is a sentence of its prompt,
    each table has the entries `tool` and `deny-cap`, and every key a refusal can give has words."""
    flat = _flat(verifier.render_prompt())
    for key, words in verifier.RULE_WORDS.items():
        assert _flat(words) in flat, (key, words)
    assert "tool" in verifier.RULE_WORDS and "deny-cap" in verifier.RULE_WORDS
    # the keys a hit of the verifier's rules can give, with and without the grammar's rule
    for hit in ({"why": "a tool the verifier is not given"}, {"why": "a write outside out/"},
                {"why": "x", "rule": "glob"}, {"why": "runs x, which is not on the allow list"},
                {"why": "other"}):
        for key in verifier._verifier_keys(hit):
            assert key in verifier.RULE_WORDS, (hit, key)
    assert verifier.rule_keys("path", "x") == ["path"] and verifier.rule_keys("address", "x") == ["address"]
    # the auditor keeps the section 18 words against its own prompt
    aflat = _flat(_audit_prompt())
    for key, words in audit.RULE_WORDS.items():
        assert _flat(words) in aflat, (key, words)
    assert "tool" in audit.RULE_WORDS and "deny-cap" in audit.RULE_WORDS
    for flag in pathscan.FLAGS:
        assert flag in audit.RULE_WORDS
    for key in ("program-off-list", "write-outside-out", "write-unplaced", "unreadable", "path",
                "address", "option-names", "option-runs", "option-writes", "option-follows", "sed", "sort",
                "uniq"):
        assert key in audit.RULE_WORDS, key
    for why in ("unreadable: x", "uses command-substitution, assignment", "runs x, which is not on the allow list",
                "writes x, which the scan cannot place", "writes x, outside out/", "a write outside out/",
                "a tool the verifier is not given", "sed -i edits a file in place", "sort -o writes its output to a file",
                "uniq writes its output to its second file", "rg --pre runs a program",
                "file --compile writes a file", "wc --files0-from reads file names from a file",
                "grep -R follows links", "rg --follow follows links", "other"):
        for key in verifier.rule_keys("allow-list", why):
            assert key in audit.RULE_WORDS, (why, key)


def test_a_shell_rule_cut_from_the_verifier_prompt_is_found_by_the_comparison():
    prompt = _flat(verifier.render_prompt())
    for cut in (verifier.RULE_WORDS["paren"], verifier.RULE_WORDS["glob"], verifier.RULE_WORDS["cd"],
                "After 64 refused calls in one pass, the next refused call voids the pass: send the rewrite",
                "A heredoc is only for the submission"):
        assert _flat(cut) in prompt, cut
        mutated = prompt.replace(_flat(cut), "")
        broken = _missing(mutated, SHELL_RULES) + [
            w for w in verifier.RULE_WORDS.values() if _flat(w) not in mutated]
        assert broken, cut
    # a row of the table cut from the prompt is found too
    row = _flat(pathscan.grammar_table_text()[4])
    assert row in prompt and row not in prompt.replace(row, "")


# ---- the receipt checks that _SCHEMA_CHECKS leaves out: C4, C10, C11, C12, C13 and C14
_RECEIPT_STAND = ("A receipt counts only when you made it in this try",)
_DRIVER_WRITES = ("Only the driver writes `receipts/` and `requests/`.",)
_CHANGED = ("a receipt whose file changed after the call printed it",)
_LAST_SUBMISSION = ("The last submission of your pass is your verdict",)
#: t-296 (F42, rr-013 event 101063): an `event:` pointer is sealed by its row's chain hash, so a file
#: hash of its export never matches; the prompt says which request checks it
_EVENT_POINTER = ("An `event:` pointer is sealed by its chain hash: check it with `--event "
                  "packet/events/<id>.json`, never with `--hash`.")
#: t-296: C12 refuses a supported claim that cites a non-matching receipt even beside a matching one
_NO_MISMATCH = ("Leave a receipt that did not match out of a supported claim's evidence: a supported claim "
                "that cites one is refused, even beside a matching receipt.")
RECEIPT_RULES = {
    "receipt %r is not a receipt id": ("Receipt ids are lower case letters, digits and hyphens",),
    "receipt %s is not in this try's receipts/": _RECEIPT_STAND,
    "receipt %s is not a well-formed receipt": _DRIVER_WRITES,
    "receipt %s names %s %r, not this try's": _RECEIPT_STAND,
    "receipt %s is not printed by a bin/rerun call in the stream": (
        "a `bin/rerun` call of your own pass asked for its id and printed its line `rerun: receipt <id> "
        "<sha256>`, once or inside a `for` loop",),
    "receipt %s is not one the driver served in this try": _DRIVER_WRITES,
    "receipt %s no longer hashes to the value the stream shows": _CHANGED,
    "receipt %s no longer hashes to the value the driver kept": _CHANGED,
    "read %s names no file inside packet/, tree/ or base/": (
        "A `read` item names a regular file under `packet/`, `tree/` or `base/` of your try folder",),
    "the packet holds diff.patch and no apply receipt exists": (
        "# first, when the packet holds diff.patch",
        "Every try starts with `python3 packet/bin/rerun apply --apply` when the packet holds `diff.patch`, "
        "in a later round too"),
    "the apply receipt was not written for rerun apply --apply": ("python3 packet/bin/rerun apply --apply",),
    "no gate receipt re-runs stage %s on %s": (
        "For a gate entry `{\"gate\": ..., \"cwd\": ...}` use `--gate` with that item and side, never `--shell`",),
    "no re-run receipt of a recorded command at cwd %s": (
        "a re-run of every command the claim records, in the place it records (`commands`)",),
    "no matching re-hash of %s": ("a matching re-hash of every pointer it names (`pointers`)",
                                  _EVENT_POINTER),
    "a supported claim cites a re-hash that did not match": (
        "A re-hash that did not match cannot support a claim.", _NO_MISMATCH),
    "the stream holds no bin/rerun --submit - call": (
        "Submit once, when you have finished, as the body of a heredoc with a quoted delimiter",),
    "the last submission is not a heredoc with a quoted delimiter": (
        "as the body of a heredoc with a quoted delimiter",),
    "the driver holds no copy of the submission": _LAST_SUBMISSION,        # the driver writes the copy
    "the driver's copy differs from the heredoc body in the stream": _LAST_SUBMISSION,
    "the copy no longer hashes to the value the call printed": _DRIVER_WRITES,
    "the copy no longer hashes to the value the driver kept": _DRIVER_WRITES,
    "the driver never served the last submission": _LAST_SUBMISSION,
    "the served submission differs from the heredoc body in the stream": _LAST_SUBMISSION,
    "served request %s has no bin/rerun call in the stream that made it": _DRIVER_WRITES,
}


def _receipt_found() -> set:
    return set(_messages(verdicts.check_verdict, calls=("bad",),
                         schema_checks=("C4", "C10", "C11", "C12", "C13", "C14")))


def test_every_receipt_check_message_has_an_entry_and_every_entry_a_message():
    found = _receipt_found()
    assert found - set(RECEIPT_RULES) == set(), (
        "a message of C4, C10, C11, C12, C13 or C14 is not pinned to prompts/verify.md: %s"
        % sorted(found - set(RECEIPT_RULES)))
    assert set(RECEIPT_RULES) - found == set(), sorted(set(RECEIPT_RULES) - found)
    assert len(found) == 24


def test_the_verifier_prompt_states_every_receipt_rule():
    assert _missing(verifier.render_prompt(), RECEIPT_RULES) == []


def test_a_receipt_rule_cut_from_the_prompt_is_found_by_the_comparison():
    prompt = _flat(verifier.render_prompt())
    for cut in ("Only the driver writes `receipts/` and `requests/`.",
                "once or inside a `for` loop",
                "A re-hash that did not match cannot support a claim.", _EVENT_POINTER, _NO_MISMATCH,
                "Every try starts with `python3 packet/bin/rerun apply --apply` when the packet holds "
                "`diff.patch`, in a later round too"):
        assert cut in prompt, cut
        assert _missing(prompt.replace(cut, ""), RECEIPT_RULES), cut


def test_the_sentence_that_every_try_starts_with_the_apply_receipt_is_in_the_section_a_later_round():
    """F38 (19.10): in rr-005 and rr-011 a verifier of a later round made no apply receipt in its new try
    folder and lost its pass to C4 and C11. The sentence is in the section "A later round" itself, so it
    cannot move out of it, and it names the first `bin/rerun` call of the try."""
    text = verifier.render_prompt()
    head = "## A later round"
    assert text.count(head) == 1
    section = text.split(head, 1)[1]
    assert "\n## " not in section                     # it is the last section of the prompt
    sentence = ("Every try starts with `python3 packet/bin/rerun apply --apply` when the packet holds "
                "`diff.patch`, in a later round too: each try has its own folder and its own `tree/`, and a "
                "receipt of an earlier try or round does not count in it. Make it your first `bin/rerun` "
                "call of the try.")
    assert _flat(sentence) in _flat(section)
    # the same sentence anywhere else in the prompt does not count: cutting it from the section fails
    moved = text.replace(sentence, "").replace("## What you judge", sentence + "\n\n## What you judge")
    assert _flat(sentence) not in _flat(moved.split(head, 1)[1])
    assert _flat(sentence) in _flat(moved)


# ================================================================ t-351 F45: the header names only the tools a job has
#
# rr-021: two proposers called Write (their tools: Read, Bash) and lost the job to K9, because the
# header said "give Read, Write and Edit absolute paths" to every role and proposer.md said "Write
# every file". The Shell rule is now built from `allowed_tools`, and a job with a scratch folder and
# no Write tool is told how to make a file with Bash.
import pytest                                                                 # noqa: E402

from alpaca.research import session_runtime, types                            # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(audit.__file__))))
_PROMPTS = os.path.join(_ROOT, "alpaca", "research", "prompts")
_PROMPT_FILE = {"auditor": "audit.md", "verifier": "verify.md"}
_FORMATION = {"proposer": "research-improve", "implementer": "research-improve",
              "notes": "research-improve", "reproducer-writer": "research-repair",
              "reproducer-reviewer": "research-repair", "verifier": "builder-verifier"}
_TOOL_WORDS = [r"\bWrite every file\b", r"\bRead, Write and Edit\b", r"\bthe (Write|Edit) tool\b",
               r"\bWrite or Edit\b", r"\bgive (Read, )?Write\b"]
_HOW_TO_WRITE = r"cat >|heredoc|<<|\bcp\b|redirect|with Bash|by Bash|through Bash"


def _role_tools(role):
    if role == "auditor":
        return list(audit._tools(_ROOT))
    return list(verifier.role_tools(_ROOT, _FORMATION[role], role))


def _role_prompt(role, tools):
    scratch = None if role in ("auditor", "verifier") else "/s/%s/scratch" % role
    job = {"job": "t-900-%s-t1" % role, "nonce": "n", "role": role, "cwd": "/w",
           "read_paths": [], "write_paths": ["/w"] if "Write" in tools else [],
           "scratch": scratch, "allowed_tools": tools, "shell_cwd": "/r"}
    text = open(os.path.join(_PROMPTS, _PROMPT_FILE.get(role, role + ".md")),
                encoding="utf-8").read()
    return session_runtime.build_prompt(job, text), scratch


def _shell_rule(head):
    return re.search(r"- Shell rule\.(.*?)\n- ", head, re.S).group(1)


@pytest.mark.parametrize("role", types.ROLES)
def test_a_role_prompt_names_only_the_file_tools_the_job_has(role):
    tools = _role_tools(role)
    full, scratch = _role_prompt(role, tools)
    head = full.split("----- job text -----")[0]
    m = re.search(r"^Allowed tools: (.*)$", head, re.M)
    assert m and [t.strip() for t in m.group(1).split(",")] == tools
    if "Write" in tools and "Edit" in tools:
        assert "give Read, Write and Edit absolute paths." in _flat(_shell_rule(head))
        return
    problems = []
    if re.search(r"\b(Write|Edit)\b", _shell_rule(head)):
        problems.append("header Shell rule names Write/Edit")
    for pat in _TOOL_WORDS:
        for hit in re.finditer(pat, full):
            problems.append("prompt says %r" % hit.group(0))
    if scratch:
        rules = re.search(r"- Shell rule\.(.*?)- Tool rule\.", head, re.S).group(1)
        if not re.search(_HOW_TO_WRITE, rules):
            problems.append("no sentence says how to write the scratch folder without Write")
    assert not problems, "%s (tools %s): %s" % (role, tools, problems)


def _shell_rule_of(tools):
    job = {"job": "j", "nonce": "n", "role": "x", "cwd": "/w", "read_paths": [], "write_paths": [],
           "scratch": "/s", "allowed_tools": tools, "shell_cwd": "/r"}
    head = session_runtime.build_prompt(job, "").split("----- job text -----")[0]
    return _flat(_shell_rule(head)), _flat(head)


def test_the_shell_rule_lists_the_file_tools_of_the_job_in_order():
    rule, head = _shell_rule_of(["Read", "Bash"])
    assert "give Read absolute paths." in rule and "Write" not in rule and "Edit" not in rule
    assert "Make a file there only with Bash" in head
    rule, head = _shell_rule_of(["Read", "Bash", "Write", "Edit"])
    assert "give Read, Write and Edit absolute paths." in rule
    assert "Make a file there only with Bash" not in head
    rule, _ = _shell_rule_of(["Bash", "Read", "Edit"])
    assert "give Read and Edit absolute paths." in rule
    # no tool list: the runtime's own tools, so today's sentence stays and nothing is added
    rule, head = _shell_rule_of([])
    assert "give Read, Write and Edit absolute paths." in rule
    assert "Make a file there only with Bash" not in head


def test_no_prompt_of_a_job_without_write_says_write_every_file():
    for name in ("proposer.md", "notes.md"):
        text = open(os.path.join(_PROMPTS, name), encoding="utf-8").read()
        assert "Write every file" not in text, name
    text = open(os.path.join(_PROMPTS, "proposer.md"), encoding="utf-8").read()
    assert "Make every file with Bash, by its absolute path in your scratch folder." in _flat(text)
    sentence = ("A `cp`, `mv`, `rm` or interpreter call earlier in a `cd X &&` chain makes the audit "
                "read a later relative name in the folder the call started in; name the file by its "
                "absolute path.")
    assert _flat(sentence) in _flat(text)
    impl = open(os.path.join(_PROMPTS, "implementer.md"), encoding="utf-8").read()
    assert "name the file by its absolute path" in _flat(impl) and "interpreter call" in impl


# ================================================================ t-296: one allowed call per rule word
#
# t-228 minor 15b: seven rule words carried no allowed form, and four carried a fragment the grammar
# itself refuses (`grep -v`, `--include='*.py'`, `for f in ...`, `cd <try folder> &&`). Every rule now
# names one full call in verifier.RULE_EXAMPLES, which stands backquoted in its words and which the
# guard admits; the design table of in-session-runtime.md 19.8.1 is pinned word for word.
from alpaca.tests.conftest import REPO                                       # noqa: E402
from alpaca.tests.devfiles import needs_dev_files                            # noqa: E402

_EXAMPLE_FILES = ("packet/brief.json", "packet/a.json", "packet/evidence/01-a.log", "packet/evidence/02-b.log",
                  "packet/bin/rerun", "a.txt", "b.txt", "out/a.txt", "out/b.txt", "tree/x/f.txt", "base/x/f.txt")


def _example_folder(project):
    from alpaca.tests.test_research_calltime import Place
    pl = Place(project)
    for rel in _EXAMPLE_FILES:
        full = os.path.join(pl.folder, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write("x\n")
    return pl


def _filled(pl, example: str) -> str:
    """The call the guard reads for an example: placeholders filled, `cd <try folder> &&` in front
    unless the example starts with it, a heredoc closed with one body line."""
    text = example
    for a, b in (("FILE", "packet/brief.json"), ("<item id>", "build"), ("<id>", "h-a")):
        text = text.replace(a, b)
    if text.startswith("cd <try folder>"):
        text = text.replace("<try folder>", pl.folder, 1)
    else:
        text = "cd %s && %s" % (pl.folder, text)
    m = re.search(r"<<'([A-Z]+)'\s*$", text)
    if m:
        text = "%s\n{}\n%s" % (text, m.group(1))
    return text


def _example_problems(pl, words: dict, examples: dict) -> list:
    from alpaca.tests.test_research_calltime import sh
    out = []
    if set(examples) != set(words):
        out.append("RULE_EXAMPLES and RULE_WORDS name different rules: %s"
                   % sorted(set(examples) ^ set(words)))
    for rule, example in sorted(examples.items()):
        if "`%s`" % example not in words.get(rule, ""):
            out.append("%s: the example %r is not backquoted in its words" % (rule, example))
        hits = pl.rules(sh(_filled(pl, example)))
        if hits:
            out.append("%s: the guard refuses the example %r: %s" % (rule, example, hits[0]["why"]))
    return out


def test_every_rule_names_one_allowed_call_the_guard_admits(project):
    pl = _example_folder(project)
    assert _example_problems(pl, verifier.RULE_WORDS, verifier.RULE_EXAMPLES) == []
    assert "test" in verifier.RULE_EXAMPLES and "test -e" in verifier.RULE_WORDS["test"]


def test_an_example_cut_from_its_words_or_refused_by_the_guard_is_found(project):
    pl = _example_folder(project)
    words = dict(verifier.RULE_WORDS)
    words["test"] = words["test"].replace("`test -e packet/brief.json`", "the `-e` form")
    assert any(p.startswith("test:") for p in _example_problems(pl, words, verifier.RULE_EXAMPLES))
    examples = dict(verifier.RULE_EXAMPLES, bang="grep -v")      # the fragment main carried
    words = dict(verifier.RULE_WORDS, bang="invert a match with `grep -v`.")
    assert any(p.startswith("bang: the guard refuses") for p in _example_problems(pl, words, examples))
    examples = dict(verifier.RULE_EXAMPLES)
    del examples["comment"]
    assert _example_problems(pl, verifier.RULE_WORDS, examples)


_DESIGN = os.path.join(REPO, "design", "research-engine", "in-session-runtime.md")


def _design_rows(text: str) -> dict:
    """{rule: words} of the table of design 19.8.1: a `|` inside a cell is written `\\|`."""
    start = text.index("#### 19.8.1 The text")
    end = text.index("#### 19.8.2", start)
    out = {}
    for line in text[start:end].splitlines():
        if not line.startswith("| `"):
            continue
        cells = re.split(r"(?<!\\)\|", line)
        if len(cells) < 4:
            continue
        words = cells[2].strip().replace("\\|", "|")
        for r in re.findall(r"`([^`]+)`", cells[1]):
            out[r] = words
    return out


@needs_dev_files("design/research-engine/in-session-runtime.md")
def test_the_design_table_holds_each_rule_word():
    with open(_DESIGN, encoding="utf-8") as fh:
        text = fh.read()
    rows = _design_rows(text)
    assert set(rows) == set(verifier.RULE_WORDS), sorted(set(rows) ^ set(verifier.RULE_WORDS))
    for rule, words in verifier.RULE_WORDS.items():
        assert _flat(rows[rule]) == _flat(words), (rule, rows[rule])
    # a row that drifts from its words is found
    row = next(line for line in text.splitlines() if line.startswith("| `bang` |"))
    drifted = text.replace(row, row.replace("grep -v 'x' packet/brief.json", "grep -v"))
    assert _flat(_design_rows(drifted)["bang"]) != _flat(verifier.RULE_WORDS["bang"])


# ---- t-296 (F42, F43): the request forms and the record
_RECORD_SENTENCE = ("No claim states a fact that only the record holds: the code refuses to render a report from a "
                    "record with more than one attempt row, and renders the report itself. Write no condition "
                    "about the record; a condition you cannot run keeps the claim open.")
_HASH_KINDS = ("`--hash` takes a `local:`, `receipt:` or `job:` pointer that a claim or the seal names; the "
               "driver refuses any other request.")


def _t296_missing(flat: str) -> list:
    return [x for x in (_EVENT_POINTER, _HASH_KINDS, _NO_MISMATCH, _RECORD_SENTENCE) if _flat(x) not in flat]


def test_the_verifier_prompt_says_how_an_event_pointer_is_checked_and_what_the_record_holds():
    flat = _flat(verifier.render_prompt())
    assert _t296_missing(flat) == []
    # the event sentence follows the request block, which keeps both forms
    assert flat.index("python3 packet/bin/rerun <id> --event packet/events/<id>.json") < flat.index(
        _flat(_EVENT_POINTER))
    assert "python3 packet/bin/rerun <id> --hash <packet path> --pointer <claim pointer>" in flat


def test_a_t296_sentence_cut_from_the_prompt_is_found():
    flat = _flat(verifier.render_prompt())
    for cut in (_EVENT_POINTER, _RECORD_SENTENCE, _HASH_KINDS, _NO_MISMATCH):
        assert _t296_missing(flat.replace(_flat(cut), "")) == [cut]
    assert _missing(flat.replace(_flat(_EVENT_POINTER), ""), RECEIPT_RULES)
    assert _missing(flat.replace(_flat(_NO_MISMATCH), ""), RECEIPT_RULES)
