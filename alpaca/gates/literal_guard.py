"""literal_guard.py - the "no forbidden literal in core" boot guard (M1.17).

Ported from the earlier harness gates/literal_guard.py and re-based onto Alpaca. The mechanism is generic: it
scans the core `*.py` files under the declared scan dirs for a set of FORBIDDEN literals and
FAILs, bound to the exact `<file>:<line>` and the literal, when one appears. What changes for Alpaca
is the SOURCE of the forbidden list: it is DATA, read from `project.yaml` and `style/banned.txt`,
NEVER hardcoded in code (there is no domain default). This is the M1 banned-words rule read as an
instrument (the ban list is a projection input), and the same generic seam the earlier harness built.

  * a forbidden literal in a core `*.py`   -> LITERAL-GUARD-HIT (FAIL, bound to file:line)
  * no core `*.py` to scan at all          -> LITERAL-GUARD-NO-POPULATION (BLOCKED; the floor)
  * no forbidden literal configured at all -> LITERAL-GUARD-NO-LITERALS (BLOCKED; a guard with
    nothing to look for is not a pass). `check(root)` treats an unconfigured ban list as an
    advisory PASS (nothing is claimed forbidden, so there is nothing to refute); the core `scan`
    keeps the honest floor so the selftest can prove it.

WHAT THE GUARD IGNORES (so it never flags itself):
  (1) ITS OWN file - matched by REAL self-path only (`os.path.realpath`), because this module's
      own selftest fixtures legitimately name the literals. A decoy that only SHARES the basename
      elsewhere is scanned like any other core file (excluding by basename would hide a leak).
  (2) any `--config`/`extra_excluded` declaration file (a declaration is not a leak).
  (3) a LINE carrying the visible allow marker `literal-guard:allow` - the loud, per-line,
      in-the-diff escape hatch for a line that must name a literal on purpose.

HONEST LIMIT: this is a byte-level substring scan of `*.py`, not a semantic analysis. A green is
"no forbidden literal string in core", never "core is provably clean by every route".
"""
from __future__ import annotations

import os
import re
from collections import namedtuple

from alpaca import project
from alpaca.gates import verdict as vc

INSTRUMENT = "literal-guard"

#: the core dirs scanned by default. Overridable via project.yaml (`literal_guard.scan_dirs`).
DEFAULT_SCAN_DIRS = ("alpaca/gates", "alpaca/checklist")

#: the visible, per-line escape hatch.
ALLOW_MARKER = "literal-guard:allow"

# reason tokens.
R_HIT = "LITERAL-GUARD-HIT"
R_NO_POP = "LITERAL-GUARD-NO-POPULATION"
R_NO_LITERALS = "LITERAL-GUARD-NO-LITERALS"

_PRUNE_DIRS = frozenset(("__pycache__", "node_modules", ".git", ".alpaca", ".claude", ".venv",
                         ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".eggs"))

Hit = namedtuple("Hit", "file line literal snippet")
Result = namedtuple("Result", "verdict reasons info")


def _self_path():
    return os.path.realpath(os.path.abspath(__file__))


def _rel(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def load_forbidden(root):
    """Resolve the forbidden set from DATA: the union of `project.yaml`'s `literal_guard.forbidden`
    list and every non-blank line of `style/banned.txt`. Returns (tuple, source_label). Never a
    hardcoded domain default - an unconfigured project simply has an empty set."""
    forbidden = []
    source = []
    try:
        cfg = project.load(root)
    except Exception:
        cfg = {}
    block = (cfg or {}).get("literal_guard") or {}
    for lit in (block.get("forbidden") or []):
        s = str(lit).strip()
        if s:
            forbidden.append(s)
    if forbidden:
        source.append("project.yaml")
    banned = os.path.join(root, "style", "banned.txt")
    if os.path.isfile(banned):
        try:
            with open(banned, encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh.read().splitlines()]
        except OSError:
            lines = []
        added = [ln for ln in lines if ln and not ln.startswith("#")]
        if added:
            source.append("style/banned.txt")
        forbidden.extend(added)
    # de-duplicate, order-stable.
    seen = set()
    uniq = []
    for f in forbidden:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return tuple(uniq), "+".join(source) if source else "none"


def _scan_dirs(root):
    try:
        cfg = project.load(root)
    except Exception:
        cfg = {}
    block = (cfg or {}).get("literal_guard") or {}
    dirs = block.get("scan_dirs")
    if isinstance(dirs, list) and dirs:
        return tuple(str(d) for d in dirs)
    return DEFAULT_SCAN_DIRS


def _iter_py(scan_root):
    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in _PRUNE_DIRS]
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def scan(root, scan_dirs=DEFAULT_SCAN_DIRS, forbidden=(), extra_excluded=()) -> Result:
    """Scan the core scan_dirs under `root` for the forbidden literals. Returns Result."""
    root_abs = os.path.abspath(root)
    forbidden = tuple(f for f in forbidden if f)
    info = {"root": root_abs, "scan_dirs": list(scan_dirs), "forbidden": list(forbidden)}
    if not forbidden:
        return Result(vc.BLOCKED,
                      ["%s: no forbidden literal was supplied (project.yaml / style/banned.txt / "
                       "argv all empty); a guard with nothing to look for is not a pass"
                       % R_NO_LITERALS], info)

    excluded = {_self_path()} | {os.path.realpath(os.path.abspath(p)) for p in extra_excluded}
    scanned = []
    hits = []
    for d in scan_dirs:
        ddir = os.path.join(root_abs, d.replace("/", os.sep))
        if not os.path.isdir(ddir):
            continue
        for fp in _iter_py(ddir):
            if os.path.realpath(os.path.abspath(fp)) in excluded:
                continue
            rel = _rel(fp, root_abs)
            scanned.append(rel)
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                text = ""
            for lineno, raw in enumerate(text.splitlines(), start=1):
                if ALLOW_MARKER in raw:
                    continue
                for lit in forbidden:
                    if lit in raw:
                        hits.append(Hit(rel, lineno, lit, raw.strip()[:100]))
    info["scanned_files"] = scanned
    info["hit_count"] = len(hits)

    if not scanned:
        return Result(vc.BLOCKED,
                      ["%s: no core *.py file was found to scan under %s in %s; an empty "
                       "population is never a pass" % (R_NO_POP, root_abs, list(scan_dirs))], info)
    if not hits:
        return Result(vc.PASS, [], info)
    reasons = []
    for h in hits:
        reasons.append("%s: the forbidden literal %r appears in core at %s:%d - move it behind the "
                       "seam, or opt the line out loudly with %r if it is a legitimate mention"
                       % (R_HIT, h.literal, h.file, h.line, ALLOW_MARKER))
    return Result(vc.FAIL, reasons, info)


def check(root) -> int:
    """The uniform instrument entry. An unconfigured ban list is an advisory PASS (nothing is
    declared forbidden, so there is nothing to refute); a configured list scans and adjudicates."""
    forbidden, _src = load_forbidden(root)
    if not forbidden:
        return vc.PASS
    return scan(root, scan_dirs=_scan_dirs(root), forbidden=forbidden).verdict


# ========================================================================= M3.11 style.lint
# One lint implementation serves three callers: the Stop hook (alpaca/hooks/stop.py), the door
# check (lint_files, run at the phase doors) and `alpaca style humanize`. The deterministic tiers
# are DATA -- words, phrases, placeholder templates and expressible structures -- read from the
# user list (style/banned.txt) and the active presets (style/presets/<preset>-ban-list.md), the
# canonical shipped source being the vendored `sam` skill where it overlaps the preset (P-010).
# The model-side tiers (context-dependent words, response patterns) are NOT matched here; they
# are injected on the M1.1 re-arm cadence (see model_side_text). Both files empty => no rule =>
# every path is a no-op.

STYLE_TIERS = ("words", "phrases", "placeholders", "structures")

#: a single match: which tier fired, the normalised term, the matched span, and its offset.
StyleMatch = namedtuple("StyleMatch", "tier term matched start")

# A prose word is matched on whole-word boundaries (`\b`); an extra token-context filter then
# drops any candidate that is really part of an identifier/path/API field, so `align_columns`,
# `src/align/main.py` and `response.leverage_ratio` are never prose hits while a sentence-final
# `back.` still is.
_SEPS = "./-"


def _in_token(text: str, start: int, end: int) -> bool:
    """True when the [start:end) span is glued into a larger identifier/path/API-field token by
    a separator (`.` `/` `-`) that continues into an alphanumeric on the far side."""
    def word(ch):
        return ch.isalnum() or ch == "_"
    i = start - 1
    if i >= 0 and text[i] in _SEPS:
        while i >= 0 and text[i] in _SEPS:
            i -= 1
        if i >= 0 and word(text[i]):
            return True
    j = end
    if j < len(text) and text[j] in _SEPS:
        while j < len(text) and text[j] in _SEPS:
            j += 1
        if j < len(text) and word(text[j]):
            return True
    return False


def _escape_flex(seg: str) -> str:
    """Escape a literal segment; every run of whitespace becomes `\\s+`. re.escape itself
    escapes whitespace (it matters in VERBOSE mode), so split on whitespace and escape the
    tokens rather than post-processing the escaped string."""
    chunks = []
    for piece in re.split(r"(\s+)", seg):
        if piece == "":
            continue
        if piece.strip() == "":
            chunks.append(r"\s+")
        else:
            chunks.append(re.escape(piece))
    return "".join(chunks)


def _flex(literal: str) -> str:
    """A whole-phrase literal, whitespace-flexible."""
    return _escape_flex(literal.strip())


def _template_re(template: str) -> str:
    """Compile a placeholder/structure template: standalone X/Y/Z match any single token."""
    chunks = []
    for seg in re.split(r"\b([XYZ])\b", template.strip()):
        if seg in ("X", "Y", "Z"):
            chunks.append(r"\S+")
        elif seg:
            chunks.append(_escape_flex(seg))
    return "".join(chunks)


def _empty_rules(rules) -> bool:
    if not rules:
        return True
    return not any((rules.get(t) or []) for t in STYLE_TIERS)


def _compile(rules) -> dict:
    """Compile the non-empty tiers of a rules dict into one regex each. {} when all empty."""
    out = {}
    words = [w for w in (rules.get("words") or []) if w and str(w).strip()]
    if words:
        alt = "|".join(re.escape(str(w).strip()) for w in sorted(set(words), key=len, reverse=True))
        out["words"] = re.compile(r"\b(?:" + alt + r")\b", re.I)
    phrases = [p for p in (rules.get("phrases") or []) if p and str(p).strip()]
    if phrases:
        alt = "|".join(_flex(str(p).strip()) for p in sorted(set(phrases), key=len, reverse=True))
        out["phrases"] = re.compile(r"\b(?:" + alt + r")\b", re.I)
    for tier in ("placeholders", "structures"):
        items = [t for t in (rules.get(tier) or []) if t and str(t).strip()]
        if items:
            alt = "|".join(_template_re(str(t)) for t in sorted(set(items), key=len, reverse=True))
            out[tier] = re.compile("(?:" + alt + ")", re.I)
    return out


def _strip_excluded(text: str) -> str:
    """Blank out the declared exclusions before matching: fenced and inline code, double-quoted
    quotations and blockquote lines. Identifiers, paths and API fields fall to the word boundary
    (they carry `_`, `/` or `.`); official names and commands live in the code/quote spans."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r'"[^"]*"', " ", text)
    lines = ["" if ln.lstrip().startswith(">") else ln for ln in text.splitlines()]
    return "\n".join(lines)


def lint(text, rules):
    """The one lint. Return a list of StyleMatch for every banned tier that fires in `text`,
    after the declared exclusions are removed. Empty rules or empty text => [] (a no-op)."""
    if not text or _empty_rules(rules):
        return []
    compiled = _compile(rules)
    if not compiled:
        return []
    scan = _strip_excluded(text)
    matches = []
    for tier in STYLE_TIERS:
        rx = compiled.get(tier)
        if rx is None:
            continue
        for m in rx.finditer(scan):
            if tier in ("words", "phrases") and _in_token(scan, m.start(), m.end()):
                continue
            term = re.sub(r"\s+", " ", m.group(0)).strip().lower()
            matches.append(StyleMatch(tier, term, m.group(0), m.start()))
    return matches


def rewrite_instruction(matches) -> str:
    """The single block message: the matches plus one rewrite instruction (curation by hit)."""
    terms = []
    seen = set()
    for m in matches:
        if m.term not in seen:
            seen.add(m.term)
            terms.append(m.term)
    return ("[alpaca style] this text uses banned wording: %s. Rewrite it in plain language without "
            "those; keep code, quotations, identifiers, paths, API fields and official names as "
            "they are." % ", ".join(terms))


# ------------------------------------------------------------------- ban-list parsing (DATA)
def parse_ban_list(path) -> dict:
    """Parse a plain-writing ban list (the sam/preset format) into the four deterministic tiers.

    Section `## 1. Words` -> words (a plain `- token` bullet), skipping the model-side
    `Context-dependent` subsection. Section `## 2. Phrases` -> phrases, or placeholders when the
    bullet carries a standalone X/Y/Z. Section `## 3. Sentence structures` -> structures, taken
    from each `- Avoid:` line's backtick templates. Later sections are model-side and ignored."""
    tiers = {"words": [], "phrases": [], "placeholders": [], "structures": []}
    if not os.path.isfile(path):
        return tiers
    section = 0
    context_dep = False
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            s = line.strip()
            if s.startswith("## "):
                head = s[3:].strip()
                m = re.match(r"(\d+)\.", head)
                section = int(m.group(1)) if m else 0
                context_dep = False
                continue
            if s.startswith("### "):
                context_dep = "context-dependent" in s.lower()
                continue
            if not s.startswith("- "):
                continue
            body = s[2:].strip()
            if section == 1:
                if context_dep or body.startswith("`") or ":" in body:
                    continue
                tiers["words"].append(body.lower())
            elif section == 2:
                if body.startswith("`"):
                    continue
                if re.search(r"\b[XYZ]\b", body):
                    tiers["placeholders"].append(body)
                else:
                    tiers["phrases"].append(body)
            elif section == 3:
                if not body.lower().startswith("avoid:"):
                    continue
                for tmpl in re.findall(r"`([^`]+)`", body):
                    tmpl = tmpl.strip()
                    if tmpl:
                        tiers["structures"].append(tmpl)
    return tiers


def shipped_preset_path(preset):
    """The canonical shipped source for a preset. `plain-writing` overlaps the vendored `sam`
    skill (P-010), so its source is the sam ban list; any other preset falls back to a same-named
    file under the harness `style/presets/` tree. Returns a path (which may not exist)."""
    here = os.path.dirname(os.path.abspath(__file__))          # alpaca/gates
    repo = os.path.dirname(os.path.dirname(here))              # repo root (holds alpaca/ and plugin/)
    if preset == "plain-writing":
        return os.path.join(repo, "plugin", "alpaca", "skills", "sam", "plain-writing-ban-list.md")
    return os.path.join(repo, "style", "presets", "%s-ban-list.md" % preset)


def _active_presets(root):
    try:
        cfg = project.load(root)
    except Exception:
        cfg = {}
    return list((cfg.get("style", {}) or {}).get("presets", []) or [])


def load_rules(root) -> dict:
    """Build the deterministic rules for a project: the user list (style/banned.txt) merged with
    every active preset (style/presets/<preset>-ban-list.md). Both empty => empty rules."""
    tiers = {"words": [], "phrases": [], "placeholders": [], "structures": []}
    banned = os.path.join(root, "style", "banned.txt")
    if os.path.isfile(banned):
        try:
            with open(banned, encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh.read().splitlines()]
        except OSError:
            lines = []
        for ln in lines:
            if not ln or ln.startswith("#"):
                continue
            if re.search(r"\b[XYZ]\b", ln):
                tiers["placeholders"].append(ln)
            elif " " in ln:
                tiers["phrases"].append(ln)
            else:
                tiers["words"].append(ln.lower())
    for preset in _active_presets(root):
        pth = os.path.join(root, "style", "presets", "%s-ban-list.md" % preset)
        parsed = parse_ban_list(pth)
        for t in STYLE_TIERS:
            tiers[t].extend(parsed[t])
    for t in STYLE_TIERS:
        seen = set()
        uniq = []
        for v in tiers[t]:
            if v not in seen:
                seen.add(v)
                uniq.append(v)
        tiers[t] = uniq
    return tiers


def model_side_text(root) -> str:
    """The model-side tiers to inject on the re-arm cadence -- NOT the deterministic tiers.

    The deterministic tiers (words, phrases, placeholders and the expressible `Avoid:` structure
    templates) are matched by lint and are not re-stated here. What IS injected is the judgement
    the lint cannot make: the context-dependent word bans (a word banned only in a vague or
    metaphorical use) and the response-pattern guidance (the section-3 pattern names and their
    `Instead:` direction). Empty string when no preset is active, so injection stays silent."""
    blocks = []
    for preset in _active_presets(root):
        pth = os.path.join(root, "style", "presets", "%s-ban-list.md" % preset)
        if not os.path.isfile(pth):
            continue
        ctx = []
        patterns = []
        section = 0
        grab_ctx = False
        with open(pth, encoding="utf-8") as fh:
            for raw in fh:
                s = raw.strip()
                if s.startswith("## "):
                    m = re.match(r"(\d+)\.", s[3:].strip())
                    section = int(m.group(1)) if m else 0
                    grab_ctx = False
                    continue
                if s.startswith("### "):
                    grab_ctx = section == 1 and "context-dependent" in s.lower()
                    if section == 3:
                        name = re.sub(r"^\d+\.\s*", "", s[4:].strip())
                        if name:
                            patterns.append(name)
                    continue
                if section == 1 and grab_ctx and s.startswith("- "):
                    ctx.append(s[2:].strip())
                elif section == 3 and s.lower().startswith("- instead:"):
                    patterns.append(s[len("- Instead:"):].strip())
        if ctx or patterns:
            lines = ["[alpaca style: model-side] judgement the deterministic lint cannot make -- avoid "
                     "these context-dependent uses and response patterns in prose (code, quotes "
                     "and identifiers exempt):"]
            lines += ["  - %s" % c for c in ctx[:60]]
            lines += ["  - pattern: %s" % p for p in patterns[:60]]
            blocks.append("\n".join(lines))
    return "\n".join(blocks)


def lint_files(root, files, rules=None):
    """The door check: lint each persisted file with the project rules. Returns a list of
    (file, StyleMatch). Binary or unreadable files are skipped. Empty rules => []."""
    if rules is None:
        rules = load_rules(root)
    if _empty_rules(rules):
        return []
    out = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        for m in lint(text, rules):
            out.append((f, m))
    return out


def style_selftest() -> int:
    """A ported control table: every control must FIRE on a bad input and stay silent on a good
    one. A control that does neither fails the table. Returns a verdict code (PASS on all-fire)."""
    R = {"words": ["delve"], "phrases": ["circle back"], "placeholders": ["Not X, but Y."],
         "structures": []}
    rows = []
    failures = 0

    def control(cid, desc, bad, good, tier):
        nonlocal failures
        bad_fires = any(m.tier == tier for m in lint(bad, R))
        good_silent = lint(good, R) == []
        ok = bad_fires and good_silent
        if not ok:
            failures += 1
        rows.append((cid, desc, "FIRED" if ok else "DID-NOT-FIRE"))

    control("SL-1", "a banned word fires; clean prose does not",
            "We delve into it.", "We study it.", "words")
    control("SL-2", "a banned phrase fires; a paraphrase does not",
            "Let us circle back.", "Let us return later.", "phrases")
    control("SL-3", "a placeholder template fires; an unrelated line does not",
            "Not speed, but clarity.", "Speed and clarity both matter here.", "placeholders")
    # exclusion controls: the bad path (bare prose) fires, the good path (excluded) does not.
    control("SL-4", "inline code is excluded",
            "Please delve now.", "Call `delve()` now.", "words")
    control("SL-5", "a quotation is excluded",
            "We delve here.", 'The note said "we delve" once.', "words")
    control("SL-6", "an identifier is excluded",
            "We delve here.", "The delve_into helper ran.", "words")
    # no-op control: empty rules never fire.
    if lint("delve circle back not a, but b", {}) != []:
        failures += 1
    rows.append(("SL-7", "empty rules are a no-op", "FIRED" if lint("delve", {}) == [] else "DID-NOT-FIRE"))

    print("CONTROL TABLE - %s-style" % INSTRUMENT)
    for cid, desc, state in rows:
        print("  %-5s %-48s %s" % (cid, desc, state))
    if failures:
        return vc.emit_verdict(INSTRUMENT + "-style-selftest", vc.FAIL,
                               "%d control(s) did not fire" % failures)
    return vc.emit_verdict(INSTRUMENT + "-style-selftest", vc.PASS, "every control fired")


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="'no forbidden literal in core' guard: FAIL when a literal from project.yaml "
                    "or style/banned.txt appears in alpaca/gates/ or alpaca/checklist/ *.py.")
    ap.add_argument("--root", default=None, help="tree to scan (default: the discovered root)")
    ap.add_argument("--selftest", action="store_true", help="run the negative-control selftest")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    from alpaca import paths
    root = a.root or paths.root()
    forbidden, source = load_forbidden(root)
    if not forbidden:
        return vc.emit_verdict(INSTRUMENT, vc.PASS, "no forbidden literal configured (advisory)")
    res = scan(root, scan_dirs=_scan_dirs(root), forbidden=forbidden)
    for r in res.reasons:
        print("  %s" % r)
    return vc.emit_verdict(INSTRUMENT, res.verdict,
                           "forbidden source: %s" % source, evidence=res.reasons[:5])


def _mk(base, files):
    for rel, body in files.items():
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
    return base


def selftest() -> int:
    import shutil
    import tempfile

    base = tempfile.mkdtemp(prefix="literal-guard-selftest-")
    rows = []
    failures = 0
    seq = [0]

    def fresh():
        seq[0] += 1
        d = os.path.join(base, "t-%02d" % seq[0])
        os.makedirs(d, exist_ok=True)
        return d

    def scan_of(files, **kw):
        return scan(_mk(fresh(), files), **kw)

    def has(res, token):
        return any(token in r for r in res.reasons)

    def control(cid, desc, bad_res, bad_want, token, good_res):
        nonlocal failures
        bad_fires = bad_res.verdict == bad_want and has(bad_res, token)
        good_passes = good_res.verdict == vc.PASS and not has(good_res, token)
        ok = bad_fires and good_passes
        if not ok:
            failures += 1
        rows.append((cid, desc, "FIRED" if ok else "DID-NOT-FIRE",
                     "bad=%s good=%s" % ("FIRED" if bad_fires else "MISS",
                                         "PASS" if good_passes else "MISS")))

    LIT = ("FORBIDDEN_LIT_XYZ",)
    CLEAN = {"alpaca/gates/clean.py": "print('generic core')\n"}

    planted = {"alpaca/gates/leaky.py": "REF = 'FORBIDDEN_LIT_XYZ'\n"}
    control("LG-1", "a forbidden literal in core -> FAIL bound to file:line",
            scan_of(planted, forbidden=LIT), vc.FAIL, R_HIT, scan_of(dict(CLEAN), forbidden=LIT))

    control("LG-2", "no core *.py to scan -> BLOCKED",
            scan_of({"alpaca/gates/notes.md": "no python\n"}, forbidden=LIT), vc.BLOCKED, R_NO_POP,
            scan_of(dict(CLEAN), forbidden=LIT))

    marked = {"alpaca/gates/doc.py": "# names FORBIDDEN_LIT_XYZ  " + ALLOW_MARKER + "\nprint('ok')\n"}
    unmarked = {"alpaca/gates/doc.py": "# names FORBIDDEN_LIT_XYZ with no opt-out\nprint('ok')\n"}
    control("LG-3", "a line carrying the allow marker is ignored (scoped opt-out)",
            scan_of(unmarked, forbidden=LIT), vc.FAIL, R_HIT, scan_of(marked, forbidden=LIT))

    control("LG-4", "no forbidden literal configured -> BLOCKED (nothing to look for)",
            scan_of(dict(CLEAN), forbidden=()), vc.BLOCKED, R_NO_LITERALS,
            scan_of(dict(CLEAN), forbidden=LIT))

    shutil.rmtree(base, ignore_errors=True)
    print("CONTROL TABLE - %s" % INSTRUMENT)
    for cid, desc, state, obs in rows:
        print("  %-5s %-52s %-12s %s" % (cid, desc, state, obs))
    if failures:
        return vc.emit_verdict(INSTRUMENT + "-selftest", vc.FAIL,
                               "%d control(s) did not fire" % failures)
    return vc.emit_verdict(INSTRUMENT + "-selftest", vc.PASS, "every control fired")


if __name__ == "__main__":
    import sys
    sys.exit(main())
