"""The sealed-path predicate (op-009 t-166): the one place where the paths of the held-out data
are matched against what an implementer read, so that the audit can flag such a read (rule S3,
verifier.md section 9.3) while no held-out path or value reaches the audit packet or a prompt.

audit.py and the other prompt builders never name the held-out paths. The audit asks this module
for a closed predicate, `closed(contract, root, run)`, and only ever calls its five questions on
a path, a call's text or a folder: `covers`, `reaches` and `names_in` (rule S3), `generator_run`
(rule S16) and `copies_in` (rule S17). The rules, the recipe of a generated held-out set and the
digests of the sealed files stay inside the object; it has no attribute that returns them, it
prints as a fixed word, and it is not serialised.
alpaca/tests/test_research_controls.py (test_no_prompt_builder_reads_the_holdout) checks that no
prompt builder or the audit carries the held-out word and that none of the prompt builders
imports this module, and the t166 and t358 tests in alpaca/tests/test_research_audit.py check
that a read under a held-out path is still an S3 flag, that a run of a declared generator is an
S16 candidate and held-out bytes in an attempt's folders an S17 candidate, and that nothing of
the paths, the recipe or the digests reaches the packet.

Design: design/research-engine/architecture.md (controls and the holdout, FR-045),
design/research-engine/verifier.md section 9.3 (S3, S16, S17), in-session-runtime.md section 29
(the recipe file, S16 and S17), runbook-format-3.md section 7.9 (the authoring rule).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

from alpaca import runbook
from alpaca.research import paths, pathscan

_GLOB = re.compile(r"[*?\[]")
_DIR = "holdout"
#: rule S16 (in-session-runtime.md section 29.3): the file under the holdout paths that declares
#: the program cutting a generated held-out set; read only here, never by a checker
_RECIPE = "recipe.json"
_RECIPE_CAP = 64 * 1024
_MIN_STEM = 4
_WORD = "A-Za-z0-9_"
#: rule S17 (section 29.4): a sealed file shorter than this matches nothing, so an empty or
#: tiny file is no evidence; the caps bound the walks of one audit
_MIN_BYTES = 32
_SEALED_CAP = 4096
_WALK_CAP = 100000


def _rules(contract) -> tuple:
    return tuple(str(v) for v in (getattr(contract, "holdout_paths", ()) or ()))


def _files(top, cap, over=None):
    """The regular files under `top` (no symlink followed or listed, no .git folder), at most
    `cap` entries looked at; a missing or unreadable folder gives nothing. When the walk stops
    at the cap, True is appended to the list `over`, if one is given."""
    seen = 0
    for base, dirs, names in os.walk(top):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(names):
            seen += 1
            if seen > cap:
                if over is not None:
                    over.append(True)
                return
            full = os.path.join(base, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                yield full, st.st_size


def _recipe(data):
    """(stem pattern, option forms, visible seeds) of one recipe object, or None when it declares
    nothing: `generator` a path whose file stem has four or more characters, `option` absent
    (`--seed`), null (a positional seed) or a string starting with `-`, `visible` absent or a
    list of whole numbers. The forms and the seeds are None when the recipe has no quiet case."""
    if not isinstance(data, dict):
        return None
    gen = data.get("generator")
    if not isinstance(gen, str):
        return None
    stem = os.path.splitext(os.path.basename(gen.strip().rstrip("/")))[0]
    if len(stem) < _MIN_STEM:
        return None
    option = data.get("option", "--seed")
    if option is not None and not (isinstance(option, str) and len(option) > 1
                                   and option.startswith("-") and not re.search(r"\s", option)):
        return None
    visible = data.get("visible", [])
    if not isinstance(visible, list) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in visible):
        return None
    stem_re = re.compile(r"(?<![%s])%s(?![%s])" % (_WORD, re.escape(stem), _WORD))
    if option is None or not visible:
        return stem_re, None, None
    # argparse takes any unique prefix of a long option, so each prefix of three or more
    # characters counts as the seed option; a short option is itself only
    forms = tuple(option[:n] for n in range(3, len(option) + 1)) if option.startswith("--") \
        else (option,)
    return stem_re, forms, frozenset(str(v) for v in visible)


def _seed_words(argv, forms):
    """The values of the words of `argv` that set the seed option of `forms`: the next word
    after a bare form, the rest after `<form>=`, and for a short option the rest of the word
    (`-s5`); a word that may set it in a way not read here (a short option inside a cluster)
    gives None."""
    out = []
    short = not forms[0].startswith("--")
    for k, word in enumerate(argv):
        word = str(word)
        hit = next((f for f in forms if word == f or word.startswith(f + "=")), None)
        if hit is not None:
            if word == hit:
                out.append(str(argv[k + 1]) if k + 1 < len(argv) else None)
            else:
                out.append(word[len(hit) + 1:])
        elif short and word.startswith(forms[0]) and len(word) > len(forms[0]):
            out.append(word[len(forms[0]):])
        elif short and word.startswith("-") and not word.startswith("--") \
                and forms[0][1:] in word[1:]:
            out.append(None)
    return out


#: rule S16's quiet case (in-session-runtime.md section 29.3, check round 2): the closed grammar
#: of the one visible run. Interpreter words, the options that run no extra code and read no
#: stdin, the word shape of the interpreter and the script operand, a timeout's duration, a module
#: name, and the commands that may stand beside the run in the same call.
_PYTHON = re.compile(r"python(?:3(?:\.[0-9]+)?)?")
_PY_OPTIONS = frozenset(("-B", "-u", "-I", "-E", "-s", "-S", "-O", "-OO", "-q"))
_PATH_WORD = re.compile(r"[A-Za-z0-9_.+@/-]+")
_DURATION = re.compile(r"[0-9]+(?:\.[0-9]+)?[smhd]?")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_BESIDE_READS = ("cat", "head", "tail", "wc", "grep", "ls", "stat", "file", "sha256sum", "diff",
                 "pwd", "echo", "printf", "true", ":")
_CP_OPTION = re.compile(r"-[rRapf]+")
_FD_TARGET = re.compile(r"&?[0-9]+|&-")


def _python_run(argv, module_ok):
    """The index in `argv` of the operand of a plain Python run, or None. The closed grammar:
    an optional prefix `time` or `timeout <duration>`; the interpreter, a path word whose base
    name is python, python3 or python3.N; options of _PY_OPTIONS only (never -c, -i, -m with
    another meaning, -W or -X); then a script operand, a path word ending in `.py`, or, when
    `module_ok`, `-m` and a dotted module name. The words after the operand are the script's."""
    k = 0
    if argv[:1] == ["time"]:
        k = 1
    elif argv[:1] == ["timeout"] and len(argv) > 1 and _DURATION.fullmatch(argv[1]):
        k = 2
    if k >= len(argv) or not _PATH_WORD.fullmatch(argv[k]) \
            or not _PYTHON.fullmatch(os.path.basename(argv[k])):
        return None
    k += 1
    while k < len(argv) and argv[k] in _PY_OPTIONS:
        k += 1
    if k >= len(argv):
        return None
    if argv[k] == "-m":
        if module_ok and k + 1 < len(argv) and _MODULE.fullmatch(argv[k + 1]):
            return k + 1
        return None
    if argv[k].startswith("-") or not _PATH_WORD.fullmatch(argv[k]) \
            or not argv[k].endswith(".py"):
        return None
    return k


def _expands(cmd) -> bool:
    return bool(cmd["heredocs"]) or any(x is None or x for x in cmd["live"]) \
        or any(x is None or x for x in cmd["rlive"])


def _beside(cmd) -> bool:
    """True when a simple command may stand beside the visible run in one call: it expands
    nothing, redirects only to a file descriptor or /dev/null, and is a read or print of
    _BESIDE_READS, `cd` with at most one operand, `mkdir` with -p only, `cp` with -r, -R, -a, -p
    or -f only, or a plain Python run of a script by the grammar of `_python_run` (no -m).
    Anything else (export, env, an assignment, a keyword, a shell, eval or source) changes how
    the run executes or is not read here, so the call is a candidate."""
    argv = [str(a) for a in cmd["argv"]]
    if not argv or _expands(cmd):
        return False
    if any(t != "/dev/null" and not _FD_TARGET.fullmatch(str(t)) for _, t in cmd["redirects"]):
        return False
    name, rest = argv[0], argv[1:]
    if name in _BESIDE_READS:
        return True
    if name == "cd":
        return len(rest) <= 1 and not any(a.startswith("-") for a in rest)
    if name == "mkdir":
        return bool(rest) and all(a == "-p" or not a.startswith("-") for a in rest)
    if name == "cp":
        ops = [a for a in rest if not _CP_OPTION.fullmatch(a)]
        return len(ops) >= 2 and not any(a.startswith("-") for a in ops)
    return _python_run(argv, False) is not None


def _visible_run(text, stem_re, forms, visible) -> bool:
    """True when the shell reads `text` as one run of the generator `stem_re` names whose one
    seed option carries a visible seed (rule S16's quiet case). The decision is made on the
    shell's own words (pathscan.parse, quotes and escapes removed, a Codex `bash -lc` wrapper
    unwrapped), so a quoted, escaped or split option counts as an option: the text must parse
    with no flag and no error and name the stem once in the raw text and in one word of one
    simple command; that command must expand nothing, have no heredoc and be a plain Python run
    (`_python_run`) whose operand is that one word, the script `<stem>.py` or the module
    `<...>.<stem>`; every other command must be one `_beside` allows; and the seed option must be
    set once in the whole text, in the script's own words, to a visible seed (leading zeros
    allowed), or the script's one word is `--help`. A code string, a shell or env command line,
    xargs or find carrying the stem is not the operand, so it is a candidate (check round 2,
    adversarial B1)."""
    if len(stem_re.findall(text)) != 1:
        return False
    got = pathscan.unwrap_literal(pathscan.parse(text))
    if got.get("error") or got.get("flags"):
        return False
    hits = [c for c in got["commands"]
            if any(stem_re.search(str(w)) for w in c["argv"])]
    named = sum(len(stem_re.findall(str(w))) for c in got["commands"]
                for w in list(c["argv"]) + [t for _, t in c["redirects"]])
    if len(hits) != 1 or named != 1:
        return False
    cmd = hits[0]
    if _expands(cmd):
        return False
    argv = [str(a) for a in cmd["argv"]]
    op = _python_run(argv, True)
    if op is None:
        return False
    word = argv[op]
    base = word.rsplit(".", 1)[-1] if argv[op - 1] == "-m" else os.path.basename(word)[:-3]
    if not stem_re.fullmatch(base):
        return False
    if not all(_beside(c) for c in got["commands"] if c is not cmd):
        return False
    if any(_seed_words(c["argv"], forms) for c in got["commands"] if c is not cmd):
        return False
    if argv[op + 1:] == ["--help"]:
        return True
    values = _seed_words(argv[op + 1:], forms)
    if len(values) != 1 or values[0] is None:
        return False
    return re.fullmatch(r"0*([0-9]+)", values[0]) is not None \
        and str(int(values[0])) in visible


def _recipes(dirs) -> tuple:
    """The recipes of every `recipe.json` under the sealed folders; a file that is too big, not
    JSON or not a recipe declares nothing and raises nothing."""
    out = []
    for top in dirs:
        for full, size in _files(top, _SEALED_CAP):
            if os.path.basename(full) != _RECIPE or size > _RECIPE_CAP:
                continue
            try:
                with open(full, encoding="utf-8") as fh:
                    got = _recipe(json.load(fh))
            except (OSError, ValueError, RecursionError):
                continue
            if got:
                out.append(got)
    return tuple(out)


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Closed:
    """Answers whether a path, a glob or a call's text reaches a sealed path, whether a call's
    text runs a declared generator of held-out data, and whether a folder holds a copy of a
    sealed file; holds the rules, the sealed folder of the run, its recipes and its digests
    privately."""

    __slots__ = ("_rules", "_dirs", "_text", "_gen", "_sums")

    def __init__(self, rules, dirs):
        self._rules = tuple(rules)
        self._dirs = tuple(dirs)
        # a rule of two or more parts written in a call's text (inline code, a heredoc) names it
        self._text = tuple(
            re.compile(r"(?<![A-Za-z0-9_.-])" + re.escape(r.strip("/").lstrip("./")))
            for r in self._rules if "/" in r.strip("/") and not _GLOB.search(r))
        # rule S16: the generators the sealed folder declares; rule S17: the sealed files'
        # digests, taken on the first `copies_in` so a predicate stays cheap to build
        self._gen = _recipes(self._dirs)
        self._sums = None

    def __repr__(self) -> str:
        return "<closed predicate>"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._rules or self._dirs)

    def __reduce__(self):
        raise TypeError("the closed predicate is not serialised")

    def covers(self, path, rels) -> bool:
        """True when `path` lies under the sealed folder of the run, or any of its
        tree-relative forms `rels` hits a sealed rule."""
        p = os.path.normpath(str(path))
        if any(p == d or p.startswith(d.rstrip("/") + "/") for d in self._dirs):
            return True
        return bool(self._rules) and any(runbook.path_hit(rel, self._rules) for rel in rels)

    def reaches(self, path, rels, glob_reaches) -> bool:
        """True when the glob `path` can match the sealed folder, or a path in it, or a sealed
        rule (`glob_reaches(glob, prefix)` is the caller's part-by-part glob test)."""
        p = os.path.normpath(str(path))
        if not _GLOB.search(p):
            return False
        if any(glob_reaches(p, d) for d in self._dirs):
            return True
        return any(glob_reaches(rel, r.strip("/").lstrip("./"))
                   for rel in rels for r in self._rules if r.strip("/").lstrip("./"))

    def names_in(self, text) -> bool:
        """True when a call's text names the sealed folder or a sealed rule of two or more
        parts."""
        return any(d in text for d in self._dirs) or any(r.search(text) for r in self._text)

    def generator_run(self, text) -> bool:
        """True when a call's text names the file stem of a generator a sealed `recipe.json`
        declares, in the raw text or in a word the shell makes of it, anywhere other than in one
        run whose one seed option carries a visible seed (rule S16). The quiet case is a closed
        grammar read on the shell's words (`_visible_run`), so no spelling of a seed is listed: a
        computed seed, another number, an import, a copy, a second run, a seed option the shell
        gets from quotes, escapes or a substitution, and the stem inside a code string or a
        command line another program runs (python3 -c, bash -c, env -S) are candidates. A recipe
        with a positional seed or no visible seed has no quiet case. The quiet case trusts the
        program at the operand path to be the declared generator: a program written under that
        name by a call that named it by pieces is the auditor's (section 29.6)."""
        text = str(text or "")
        words = None
        for stem_re, forms, visible in self._gen:
            if not stem_re.search(text):
                if words is None:
                    got = pathscan.unwrap_literal(pathscan.parse(text))
                    words = [str(w) for c in got["commands"] for w in c["argv"]] + \
                        [str(t) for c in got["commands"] for _, t in c["redirects"]]
                if not any(stem_re.search(w) for w in words):
                    continue
            if forms is not None and _visible_run(text, stem_re, forms, visible):
                continue
            return True
        return False

    def copies_in(self, folders) -> bool:
        """True when a regular file under one of `folders` equals a sealed file byte for byte
        (size, then sha256; rule S17). A sealed `recipe.json`, a sealed file under 32 bytes and a
        symlink on either side are not compared; a missing folder is skipped and no error of the
        walk escapes. A folder with more entries than the walk reads is also True: the files
        after the cap are not seen, so the auditor reads the launch (check round 1, M2)."""
        if self._sums is None:
            sums = {}
            for top in self._dirs:
                for full, size in _files(top, _SEALED_CAP):
                    if size < _MIN_BYTES or os.path.basename(full) == _RECIPE:
                        continue
                    try:
                        sums.setdefault(size, set()).add(_sha256(full))
                    except OSError:
                        continue
            self._sums = sums
        if not self._sums:
            return False
        for top in folders or ():
            if not top or not os.path.isdir(str(top)):
                continue
            over = []
            for full, size in _files(str(top), _WALK_CAP, over):
                if size not in self._sums:
                    continue
                try:
                    if _sha256(full) in self._sums[size]:
                        return True
                except OSError:
                    continue
            if over:
                return True
        return False


def closed(contract, root, run) -> Closed:
    """The closed predicate of a frozen contract: its held-out rules and the sealed folder of
    the run (contract/holdout/)."""
    return Closed(_rules(contract), (os.path.join(paths.contract_dir(root, run), _DIR),))
