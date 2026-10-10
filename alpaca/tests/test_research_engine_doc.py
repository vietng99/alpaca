"""docs/research-engine.md, the research engine's operator and engineer guide (op-009 t-116), is bound
to the code it describes.

Done bars: every verb and field is documented; the examples run as written; the guide credits FeSens
auto-arch-tournament and HWE-Bench with links, licenses and pinned commits and states that nothing is
copied. Fail case: a documented command that fails.

The bindings are two-way and per item, so a verb, a flag or a field that the code gains or loses
turns a test red and the message names it:

- the verbs: the choices of the `alpaca research` parser (what `bin/alpaca research --help` lists),
  which equal alpaca.research.cli.VERBS and _HANDLERS; one `#### alpaca research <verb>` heading each;
- the flags: every long option of every sub-parser, in the table of its own verb's section, with the
  choice words of a `choices=` option in its row; the MANUAL research paragraph names the same per
  verb;
- the fields: every path of runbook.research_fields(), with its improve and repair required
  columns, in the guide's field index;
- the commands: every alpaca line of the guide parses with the real parser (placeholders replaced by
  a value of the type their option takes), a command block without a run class is refused, and the
  `sh scratch` blocks run in a project made by `alpaca new`, each with the exit its note states;
- the grant rules: on a session run only `extend` and `budget` grants are recorded without being
  applied, a `stop` is applied at once, and `--no-apply` with a stop is refused (a scratch command);
- the credits: each reference's link, licence and pin sit in that reference's own bullet or clause;
- the runner sample prints a results block the marker parser accepts with a token from descriptor 3;
- the reason codes the guide names exist, a code row's exit word is the code's, and the exit table
  is alpaca/gates/verdict.py's;
- every path and relative link resolves in a shipped copy;
- the hub's Research paragraph states the view facts read from alpaca/research/view.py and
  alpaca/serve.py;
- the guide is ASCII, passes the plain-writing lint and names no host path or mailbox.

The parser and research_fields are read in process through module attributes (research_dockit), so
a test that grows them in process is seen.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from alpaca import runbook
from alpaca.gates import literal_guard
from alpaca.gates import verdict as vc
from alpaca.research import cli as rcli
from alpaca.research import errors, markers, view
from alpaca.research.errors import MarkerError
from alpaca.tests import research_dockit as kit
from alpaca.tests.conftest import REPO
from alpaca.tests.devfiles import names_as_data

# The guide test names the development-tree pack only to refuse it in a documented command.
names_as_data("packs/refpack")

GUIDE = "docs/research-engine.md"
GUIDE_PATH = os.path.join(REPO, GUIDE)
HEADING = re.compile(r"^####\s+`alpaca research ([a-z][a-z-]*)`\s*$")
FAMILY = re.compile(r"\b(?:RUN|RESEARCH|CONTRACT|JOB|REPAIR|GATE|ISOLATION|VERIFY|AUDIT)"
                    r"-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
FIELD_HEADER = "| field | required in improve | required in repair |"


def _read(rel: str) -> str:
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _guide() -> str:
    assert os.path.isfile(GUIDE_PATH), "%s is missing" % GUIDE
    return _read(GUIDE)


def _verb_sections(text: str) -> dict:
    """{verb: section text} for each `#### alpaca research <verb>` heading, to the next heading of
    level four or above."""
    lines = text.splitlines()
    out, cur, buf = {}, None, []
    for ln in lines:
        m = HEADING.match(ln)
        if m or re.match(r"^#{1,4}\s", ln):
            if cur is not None:
                out.setdefault(cur, []).append("\n".join(buf))
            cur, buf = (m.group(1) if m else None), []
            continue
        if cur is not None:
            buf.append(ln)
    if cur is not None:
        out.setdefault(cur, []).append("\n".join(buf))
    return {k: "\n".join(v) for k, v in out.items()}


def _table_rows(section: str) -> list:
    return [[c.strip() for c in ln.strip().strip("|").split("|")]
            for ln in section.splitlines() if ln.lstrip().startswith("|")]


def _manual_research() -> str:
    text = _read("MANUAL.md")
    m = re.search(r"^### Research\s*$(.*?)^### ", text, re.S | re.M)
    assert m, "MANUAL.md has no ### Research section"
    return m.group(1)


# ------------------------------------------------------------------ 1. verbs
def test_verbs_match_the_parser():
    parsers = kit.research_parsers()
    assert set(parsers) == set(rcli.VERBS) == set(rcli._HANDLERS), (sorted(parsers), rcli.VERBS)
    heads = [m.group(1) for m in (HEADING.match(ln) for ln in _guide().splitlines()) if m]
    missing = [v for v in parsers if heads.count(v) != 1]
    ghosts = sorted(set(heads) - set(parsers))
    assert missing == [], "verbs without exactly one `#### alpaca research <verb>` heading: %s" % missing
    assert ghosts == [], "headings for verbs the parser lacks: %s" % ghosts


# ------------------------------------------------------------------ 2. flags
def test_every_flag_is_in_its_verbs_section():
    sections = _verb_sections(_guide())
    wrong = []
    for verb, p in kit.research_parsers().items():
        rows = _table_rows(sections.get(verb, ""))
        documented = {}
        for row in rows:
            m = re.match(r"^`(--[a-z][a-z0-9-]*)`$", row[0]) if row else None
            if m:
                documented[m.group(1)] = row
        options = kit.long_options(p)
        for flag in options:
            if flag not in documented:
                wrong.append("%s %s: not in its table" % (verb, flag))
        for flag in documented:
            if flag not in options:
                wrong.append("%s %s: in its table, but the verb has no such flag" % (verb, flag))
        for action in p._actions:
            longs = [o for o in action.option_strings if o.startswith("--")]
            if action.choices and longs and longs[0] in documented:
                cell = " ".join(documented[longs[0]][1:])
                for choice in action.choices:
                    if "`%s`" % choice not in cell:
                        wrong.append("%s %s: choice %s not named in its row" % (verb, longs[0], choice))
    assert wrong == [], "flag tables out of step with the parser: %s" % wrong


# ------------------------------------------------------------------ 3. fields
def _field_index(text: str) -> list:
    lines = text.splitlines()
    assert FIELD_HEADER in lines, "the guide has no field index table (%s)" % FIELD_HEADER
    i = lines.index(FIELD_HEADER) + 2
    rows = []
    while i < len(lines) and lines[i].startswith("|"):
        cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        rows.append(cells)
        i += 1
    return rows


def test_the_field_index_matches_research_fields():
    text = _guide()
    fields = {f["path"]: f["required"] for f in runbook.research_fields()}
    rows = _field_index(text)
    wrong = []
    seen = set()
    for cells in rows:
        m = re.match(r"^`(research[^`]*)`$", cells[0])
        if not m or len(cells) != 3 or cells[1] not in ("yes", "no") or cells[2] not in ("yes", "no"):
            wrong.append("a row that is not `path` | yes/no | yes/no: %s" % cells)
            continue
        path = m.group(1)
        seen.add(path)
        if path not in fields:
            wrong.append("%s: in the index, not in research_fields()" % path)
            continue
        for mode, cell in (("improve", cells[1]), ("repair", cells[2])):
            if (cell == "yes") != fields[path][mode]:
                wrong.append("%s: required in %s is %s in the code" % (path, mode, fields[path][mode]))
    for path in fields:
        if path not in seen:
            wrong.append("%s: in research_fields(), not in the index" % path)
    named = set(re.findall(r"`(research(?:\.[a-z_]+(?:\[\])?)+)`", text))
    for path in sorted(named - set(fields)):
        wrong.append("%s: named in the guide, not a field" % path)
    assert wrong == [], "the field index is out of step with research_fields(): %s" % wrong


# ------------------------------------------------------------------ 4. commands parse
def _inline_spans(text: str) -> list:
    out = []
    for n, ln in enumerate(text.splitlines(), 1):
        if ln.lstrip().startswith(("```", "~~~")):
            continue
        for span in re.findall(r"`([^`]+)`", ln):
            out.append((n, span.strip()))
    return out


def _parse_span(top, span: str):
    words = kit.alpaca_words(kit.expand_synopsis(span))
    if words is None:
        return None, None
    argv = kit.typed_standin(top, words)
    err = kit.parse_error(top, argv)
    if err and "arguments are required" in err:
        err = None                       # an inline span may name a verb without its operands
    return argv, err


def test_every_command_parses():
    top = kit.parser()
    text = _guide()
    rows = kit.parse_lines(text, top)
    bad = ["line %d (%s): %s -> %s" % (n, cls, cmd, err) for n, cls, cmd, _argv, err in rows if err]
    classes = {cls for _n, cls, _c, _a, _e in rows}
    assert {"scratch", "driver", "owner-only", "synopsis"} <= classes, classes
    for n, span in _inline_spans(text):
        _argv, err = _parse_span(top, span)
        if err:
            bad.append("line %d: `%s` -> %s" % (n, span, err))
    verbs = set(kit.research_parsers())
    for span in re.findall(r"`([^`]+)`", _manual_research()):
        first = span.split()[0] if span.split() else ""
        if first in verbs:
            _argv, err = _parse_span(top, "alpaca research " + span)
            if err:
                bad.append("MANUAL: `%s` -> %s" % (span, err))
    assert len(rows) > 20, len(rows)
    assert bad == [], "documented commands the parser refuses:\n%s" % "\n".join(bad)


# ------------------------------------------------------------------ 5. credits
FESENS_URL = "https://github.com/FeSens/auto-arch-tournament"
HWE_URL = "https://github.com/pku-liang/hwe-bench"
PINS = {"auto-arch-tournament": "3a506537e7f323d56daf36990b8d7eafc87df129",
        "hwe-bench": "10c78a87e1f92695d78d15b1464a6107dcac8837"}
CARD_REV = "82a42e0a05719366a326e09ddc668ea0d46c91f6"


def _credits() -> str:
    text = _guide()
    m = re.search(r"^## Credits\s*$(.*)", text, re.S | re.M)
    assert m, "the guide has no ## Credits section"
    return m.group(1)


def _credit_parts() -> dict:
    """The Credits section cut into the parts each needle must sit in: the FeSens bullet, the
    HWE-Bench repository clause, its paper clause and its dataset clause, plus the whole section flat."""
    sec = _credits()
    bullets = [" ".join(b.split()) for b in re.findall(r"^- (.*?)(?=^- |^\s*$)", sec, re.S | re.M)]
    fes = [b for b in bullets if FESENS_URL in b]
    hwe = [b for b in bullets if HWE_URL in b]
    assert len(fes) == 1 and len(hwe) == 1 and len(bullets) == 2, bullets
    hwe = hwe[0]
    assert "Its paper" in hwe and "Its dataset" in hwe, hwe
    repo, rest = hwe.split("Its paper", 1)
    paper, data = rest.split("Its dataset", 1)
    return {"fesens": fes[0], "hwe": repo, "paper": paper, "dataset": data,
            "flat": " ".join(sec.split())}


# the part of the Credits section each fact belongs to: a fact moved to another part, or removed
# from its own, turns the test red (one Apache-2.0 per reference, so one cannot stand in for another)
CREDIT_FACTS = {
    "fesens": (FESENS_URL, "Apache-2.0", PINS["auto-arch-tournament"]),
    "hwe": (HWE_URL, "Apache-2.0", PINS["hwe-bench"]),
    "paper": ("https://arxiv.org/abs/2604.14709", "2604.14709", "CC BY 4.0"),
    "dataset": ("henryen/hwe-bench", "Apache-2.0", CARD_REV),
}


def test_credits():
    parts = _credit_parts()
    missing = ["%s: %s" % (part, needle) for part, needles in CREDIT_FACTS.items()
               for needle in needles if needle not in parts[part]]
    assert missing == [], "credit facts missing from their own reference:\n%s" % "\n".join(missing)
    # a pin or a licence of one reference must not sit in the other's bullet
    assert PINS["hwe-bench"] not in parts["fesens"] and PINS["auto-arch-tournament"] not in parts["hwe"]
    assert "CC BY 4.0" not in parts["hwe"] and "CC BY 4.0" not in parts["dataset"]
    flat = parts["flat"]
    for sentence in ("No reference code, prompt text or data is copied into Alpaca.",
                     "The engine re-implements ideas from two public projects.",
                     "The engine re-implements the ideas from written studies of the two projects"):
        assert sentence in flat, sentence
    for sentence in re.split(r"(?<=[.;])\s+", flat):
        low = sentence.lower()
        if "cc by 4.0" in low and ("dataset" in low or "henryen" in low) and "paper" not in low:
            raise AssertionError("a sentence gives the dataset CC BY 4.0: %r" % sentence)
        if "hwebench.com" in low:
            assert "fesens" in low, sentence


# ------------------------------------------------------------------ 5b. grant on a session run
# alpaca/research/cli.py `_grant`: only `extend` and `budget` on a session run are recorded without
# being applied (in-session-runtime.md section 3.4, "`grant RUN stop` stops as today"), `--no-apply`
# goes only with `RUN extend` or `RUN budget`, and a `stop` is applied at once on every run
GRANT_RULES = (
    "On a run a session drives, an `extend` or `budget` grant records the decision and starts nothing",
    "A `stop` grant stops the run at once, whether a session drives it or not.",
    "Two cases are recorded only: `extend` and `budget` on a session run, and `extend` and `budget`"
    " with `--no-apply`.",
    "A `stop` is applied at once on every run, a session run included; `--no-apply` with it is a"
    " usage error (exit 64).",
)
WITHHELD = re.compile(r"records the decision|recorded only|starts nothing|record the grant|unless")


def _prose_units(text: str) -> list:
    """The guide's sentences outside code fences, and each table row as one unit."""
    units, para, fenced = [], [], False
    for line in text.splitlines() + [""]:
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.startswith("|"):
            units.append(" ".join(line.split()))
        elif line.strip() and not line.startswith("#"):
            para.append(line.strip())
            continue
        if para:
            units += re.split(r"(?<=[.:;])\s+", " ".join(para))
            para = []
    return units


def test_a_stop_grant_applies_on_a_session_run():
    text = _guide()
    flat = " ".join(text.split())
    missing = [rule for rule in GRANT_RULES if rule not in flat]
    assert missing == [], "the grant rules the guide must state:\n%s" % "\n".join(missing)
    wrong = []
    for unit in _prose_units(text):
        low = unit.lower()
        if not ("session" in low or "--no-apply" in low) or not WITHHELD.search(low):
            continue
        # a unit that withholds a grant on a session run, or under --no-apply, names extend and
        # budget, and a stop in it is one applied at once
        if not re.search(r"\bextend\b", low) or not re.search(r"\bbudget\b", low) or \
                ("stop" in low and "at once" not in low):
            wrong.append(unit)
    assert wrong == [], "units that withhold a grant other than extend or budget:\n%s" % \
        "\n".join(wrong)
    # the refusal of --no-apply on a stop is a scratch command, so the kit runs it against the code
    rows = kit.scratch_commands(text)
    assert any("grant rr-001 stop --no-apply" in cmd and want == 64 for _n, cmd, want in rows), rows


def _refs_dir():
    """research-refs/ of this checkout, else of the main checkout of a worktree; None if neither."""
    here = os.path.join(REPO, "research-refs")
    if os.path.isdir(here):
        return here
    got = subprocess.run(["git", "-C", REPO, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                         capture_output=True, text=True)
    common = got.stdout.strip()
    if got.returncode == 0 and common:
        main = os.path.join(os.path.dirname(common), "research-refs")
        if os.path.isdir(main):
            return main
    return None


def test_credit_pins_are_the_clones_heads():
    refs = _refs_dir()
    if refs is None:
        pytest.skip("no research-refs/ clones in this checkout or its main checkout")
    sec = _credits()
    for name, pin in PINS.items():
        path = os.path.join(refs, name)
        if not os.path.isdir(path):
            pytest.skip("research-refs/%s is not cloned here" % name)
        head = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        assert head == pin and pin in sec, (name, head, pin)


# ------------------------------------------------------------------ 6. codes and exits
def _known_codes() -> set:
    known = set(errors.CODES) | set(runbook.RESEARCH_ERROR_CODES)
    base = os.path.join(REPO, "alpaca")
    for dirpath, dirnames, files in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("tests", "__pycache__")]
        for f in files:
            if f.endswith(".py"):
                with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                    known |= set(re.findall(r"[\"']([A-Z]+-[A-Z0-9-]+)[\"']", fh.read()))
    return known


def test_named_codes_are_real_and_rows_carry_their_exit():
    text = _guide()
    known = _known_codes()
    ghosts = sorted({c for c in FAMILY.findall(text) if c not in known})
    assert ghosts == [], "codes the guide names that no code raises: %s" % ghosts
    words = {"PASS": vc.PASS, "FAIL": vc.FAIL, "BLOCKED": vc.BLOCKED, "PAUSED": vc.PAUSED}
    wrong = []
    for ln in text.splitlines():
        for code in re.findall(r"`([A-Z][A-Z0-9-]+)`", ln):
            if code not in errors.CODES or not ln.lstrip().startswith("|"):
                continue
            said = {w for w in words if re.search(r"\b%s\b" % w, ln)}
            if len(said) == 1 and words[next(iter(said))] != errors.CODES[code]:
                wrong.append((code, said, errors.CODES[code]))
        for m in re.finditer(r"`([A-Z][A-Z0-9-]+)` \((PASS|FAIL|BLOCKED|PAUSED), exit (\d+)\)", ln):
            code, word, num = m.group(1), m.group(2), int(m.group(3))
            if code in errors.CODES and (errors.CODES[code] != words[word] or num != words[word]):
                wrong.append((code, word, num, errors.CODES[code]))
    assert wrong == [], "codes shown with another exit than the code's: %s" % wrong
    holds = {"RUN-ROUNDS-REACHED", "RUN-BUDGET-HOLD", "CONTRACT-LOOSEN-UNGRANTED", "RUN-SHIP-DECISION"}
    for code in holds:
        assert errors.CODES[code] == vc.PAUSED and "`%s`" % code in text, code
    table = {(int(a), b) for a, b in re.findall(r"^\| (\d+) \| (PASS|FAIL|BLOCKED|PAUSED|usage) \|",
                                                 text, re.M)}
    assert table == {(vc.PASS, "PASS"), (vc.FAIL, "FAIL"), (vc.BLOCKED, "BLOCKED"),
                     (vc.PAUSED, "PAUSED"), (vc.USAGE, "usage")}, table


# ------------------------------------------------------------------ 7. paths ship
_TOKEN = re.compile(r"`([^`\s<>]*/[^`\s<>]*)`")
_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
_NEVER = re.compile(r"(?<![\w./-])(?:design/|research-refs/|packs/refpack)")


def _mechanism() -> list:
    from alpaca import manifest
    return list(manifest.mechanism(REPO))


def _ships(rel: str, mech: list) -> bool:
    rel = rel.rstrip("/")
    for entry in mech:
        if entry.endswith("/"):
            if rel == entry.rstrip("/") or rel.startswith(entry):
                return True
        elif rel == entry:
            return True
    return False


def _bad_paths(text: str, base: str, made: str, mech: list) -> list:
    bad = []
    for n, ln in enumerate(text.splitlines(), 1):
        for hit in _NEVER.findall(ln):
            bad.append((n, hit))
        for tok in _TOKEN.findall(ln):
            tok = tok.rstrip(".,;:)")
            if tok.startswith(("/", "#", "http")) or tok.startswith(".alpaca/"):
                continue
            head = "/".join(tok.split("/")[:2])
            if _ships(tok, mech) or head in made:
                continue
            # a name such as a dataset id (owner/name) is not a path: only a token whose top
            # folder or file is in the tree reads as a path of this tree
            if not os.path.exists(os.path.join(REPO, tok.split("/")[0])):
                continue
            bad.append((n, tok))
        for link in _LINK.findall(ln):
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            rel = os.path.normpath(os.path.join(base, link)).replace(os.sep, "/")
            if not _ships(rel, mech):
                bad.append((n, "link " + link))
    return bad


def test_paths_and_links_ship():
    text = _guide()
    mech = _mechanism()
    assert GUIDE in mech, "ALPACA-MANIFEST [mechanism] does not list %s" % GUIDE
    made = " ".join(cmd for _n, cmd, _w in kit.scratch_commands(text))
    bad = _bad_paths(text, "docs", made, mech)
    bad += [("MANUAL",) + b for b in _bad_paths(_manual_research(), ".", "", mech)]
    hub = "\n".join(ln for ln in _read("docs/operations-hub.md").splitlines() if ln.startswith("- Research "))
    assert hub
    bad += [("hub",) + b for b in _bad_paths(hub, "docs", "", mech)]
    assert bad == [], "paths or links a shipped copy does not hold: %s" % bad


# ------------------------------------------------------------------ 8. the runner sample
def test_the_runner_sample_passes_the_gate(tmp_path):
    text = _guide()
    runners = [b for b in kit.blocks(text) if b["info"] == "python"
               and any("ALPACA-RESULTS-END" in ln for ln in b["body"])]
    assert len(runners) == 1, "the guide has %d python runner samples" % len(runners)
    script = tmp_path / "runner.py"
    script.write_text("\n".join(runners[0]["body"]) + "\n", encoding="utf-8")
    token = "f00d" * 8
    tok = tmp_path / "token"
    tok.write_text(token + "\n", encoding="ascii")
    env = dict(os.environ, ALPACA_RESULTS_FD="3")
    res = subprocess.run(["bash", "-c", 'exec 3<"$1"; exec "$2" "$3"', "runner", str(tok),
                          sys.executable, str(script)], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr
    clean = markers._with_token(res.stdout, token)
    report = markers.parse_tests(clean)
    assert report.tests and set(report.tests.values()) == {"PASS"}, report
    metrics = re.findall(r"^METRIC:\s+(\S+)", clean, re.M)
    assert metrics and all(markers.parse_metric(clean, m) is not None for m in metrics)
    with pytest.raises(MarkerError):        # the same output without its token is refused
        markers._with_token(res.stdout.replace(" " + token, ""), token)
    samples = [b for b in kit.blocks(text) if b["info"] == "text"
               and any("ALPACA-RESULTS-BEGIN" in ln for ln in b["body"])]
    assert samples, "the guide shows no sample results block"
    for b in samples:
        log = "\n".join(b["body"]).replace("<token>", token) + "\n"
        markers.parse_tests(markers._with_token(log, token))
    assert not re.search(r"ALPACA-RESULTS-END(?![^\n]{0,3}<token>|`| line| with)", text.replace(
        "ALPACA-RESULTS-END {token}", "")), "a bare ALPACA-RESULTS-END in the guide"


# ------------------------------------------------------------------ 9. MANUAL per verb
def test_manual_research_section_per_verb():
    sec = _manual_research()
    spans = re.findall(r"`([^`]+)`", sec)
    wrong = []
    for verb, p in kit.research_parsers().items():
        led = [s for s in spans if s.split()[:1] == [verb]]
        if not led:
            wrong.append("%s: no span starts with the verb" % verb)
            continue
        named = set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", " ".join(led)))
        want = set(kit.long_options(p)) - {"--json"}
        for flag in sorted(want - named):
            wrong.append("%s %s: missing" % (verb, flag))
        for flag in sorted(named - want):
            wrong.append("%s %s: the verb has no such flag" % (verb, flag))
    assert wrong == [], "MANUAL ### Research out of step with the parser: %s" % wrong
    assert "Every sub-verb takes `--json`" in sec
    assert "`%s`" % GUIDE in sec


# ------------------------------------------------------------------ 10. the hub paragraph
def test_hub_research_bullet():
    hub = [ln for ln in _read("docs/operations-hub.md").splitlines() if ln.startswith("- Research ")]
    assert len(hub) == 1
    line = hub[0]
    for needle in ("/hub/research.json", "/hub/research.json?run=", '"schema": "%s"' % view.SCHEMA,
                   "more than %d attempts" % view.MAX_ATTEMPTS, "`attempts_total`", "`sources`",
                   "answers 400", "answers 404", "`bin/alpaca op pin <op>`", "`%s`" % GUIDE):
        assert needle in line, needle
    serve = _read("alpaca/serve.py")
    assert '"/hub/research.json"' in serve and "Research run not found in this project" in serve
    assert "Research run not found in this project" in line


# ------------------------------------------------------------------ 11. untagged blocks
def test_untagged_command_block_is_refused():
    sample = "text\n\n```sh\nbin/alpaca research status\n```\n\n```sh scratch\nls\n```\n"
    assert kit.untagged(sample) == [(4, "bin/alpaca research status")]
    assert kit.untagged(_guide()) == []


# ------------------------------------------------------------------ 12. plain text
def test_plain_text():
    with open(GUIDE_PATH, "rb") as fh:
        raw = fh.read()
    bad = [i for i, b in enumerate(raw) if b > 127]
    assert bad == [], "non-ASCII bytes at offsets %s" % bad[:10]
    text = raw.decode("ascii")
    matches = literal_guard.lint(text, literal_guard.load_rules(REPO))
    assert matches == [], [m.matched for m in matches]
    assert not re.search(r"/home/|/Users/|/var/tmp/", text), "a host path"
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}", text), "a mailbox"


# ------------------------------------------------------------------ 13. the examples run
@pytest.fixture(scope="module")
def scratch(tmp_path_factory):
    base = str(tmp_path_factory.mktemp("research-guide"))
    dest, shim = os.path.join(base, "project"), os.path.join(base, "shim")
    env = kit.scratch_env(base, shim)
    kit.scratch_project(dest, env, shim)
    return dest, env


def test_scratch_examples_run_as_written(scratch):
    text = _guide()
    cmds = kit.scratch_commands(text)
    assert len(cmds) >= 10, cmds
    driving = re.compile(r"research\s+(?:%s)(?![\w-])" % "|".join(map(re.escape, rcli.DRIVING)))
    for n, cmd, _want in cmds:
        assert not driving.search(cmd), "line %d runs a driving verb: %s" % (n, cmd)
        assert "packs/refpack" not in cmd, "line %d names the development-tree pack" % n
    dest, env = scratch
    assert os.path.isfile(os.path.join(dest, GUIDE)), "an `alpaca new` project lacks %s" % GUIDE
    rows = kit.run_scratch(text, dest, env)
    wrong = [r for r in rows if r["got"] != r["want"]]
    assert wrong == [], "documented commands that do not exit as documented:\n%s" % "\n".join(
        "line %d: %s (want %d, got %d) %s" % (r["line"], r["command"], r["want"], r["got"],
                                             " / ".join(r["tail"])) for r in wrong)
