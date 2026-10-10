"""Readers and a runner for the command examples of a Markdown guide (op-009 t-116). A test kit, not
a conftest and not a test module: import it, or run it.

A guide marks each fenced command block with a run class after the language word:

    ```sh scratch      runs as written, top to bottom, in a new project made by `alpaca new`
    ```sh driver       a driving verb: typed only in the session that drives a research run
    ```sh owner-only   a grant the owner types at their own terminal
    ```text synopsis   grammar: <placeholders>, [optional parts] and a|b choices

A `sh`, `bash`, `shell`, `console` or bare fence that holds an `alpaca` command and carries none of
these words is refused (`untagged`). Every command line of every class is parsed with the real
argument parser, with each placeholder replaced by a value of the type its option takes
(`typed_standin`); only `scratch` blocks run. A command's expected exit code is a trailing
`# exit: N` note, 0 when there is none.

The parser is built the way `alpaca.cli.main` builds it, and `alpaca.research.cli.parser` is looked
up as a module attribute at call time, so a test that grows the parser in process (a new flag) is
seen here.

Run it to keep the evidence of a guide:

    python3 -m alpaca.tests.research_dockit --emit DIR [--doc docs/research-engine.md]
        writes DIR/scratch.sh (every scratch block in order) and DIR/parse-only.txt (the driver,
        owner-only and synopsis lines with the argv each parses to)
    python3 -m alpaca.tests.research_dockit --run DIR --log FILE [--doc ...]
        makes a project with `alpaca new` in DIR, runs every scratch block there and writes one log
        line per command: the block's line, the command, the exit it expects and the exit it got
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
GUIDE = "docs/research-engine.md"

CLASSES = ("scratch", "driver", "owner-only", "synopsis")
COMMAND_INFOS = ("", "sh", "bash", "shell", "console")
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+-]*)(.*)$")
_EXIT = re.compile(r"\s#\s*exit:\s*(\d+)\s*$")
_COMMENT = re.compile(r"\s+#.*$")
PLACEHOLDER = re.compile(r"<[^<>\s]+>")
#: shell words that end the alpaca command of a line
_STOP_WORDS = ("|", "||", "&&", ";", ">", ">>", "2>", "<", "2>&1")
_ALPACA = re.compile(r"^(?:\./)?(?:bin/)?alpaca$")


# ------------------------------------------------------------------ reading the guide
def blocks(text: str) -> list:
    """Every fenced block: {"info", "cls", "line", "body"}. `info` is the language word, lowercased;
    `cls` the first run-class word after it, or None; `line` the fence's line number (1-based)."""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        fence, info, rest = m.group(1), m.group(2).lower(), m.group(3).split()
        j = i + 1
        body = []
        while j < len(lines) and not lines[j].strip().startswith(fence):
            body.append(lines[j])
            j += 1
        cls = next((w for w in rest if w in CLASSES), None)
        out.append({"info": info, "cls": cls, "line": i + 1, "body": body})
        i = j + 1
    return out


def commands(block: dict) -> list:
    """[(line number, command, expected exit)] of a command block: continuation lines joined, only
    `$ ` lines in a console block, comment-only lines dropped. A block that holds a here-document
    (`<<`) is one command, the whole body, whose expected exit is the last note in it."""
    if block["info"] not in COMMAND_INFOS:
        return []
    body = block["body"]
    first = block["line"] + 1
    if any("<<" in ln for ln in body):
        notes = [int(m.group(1)) for m in (_EXIT.search(ln) for ln in body) if m]
        return [(first, "\n".join(body), notes[-1] if notes else 0)]
    console = block["info"] == "console"
    out, cur, start = [], None, None
    for n, raw in enumerate(body, first):
        line = raw.rstrip()
        if cur is not None:
            cur += " " + line.strip()
            if cur.endswith("\\"):
                cur = cur[:-1].rstrip()
                continue
            out.append((start, cur))
            cur = None
            continue
        s = line.strip()
        if console:
            if not s.startswith("$ "):
                continue
            s = s[2:]
        if not s or s.startswith("#"):
            continue
        if s.endswith("\\"):
            cur, start = s[:-1].rstrip(), n
            continue
        out.append((n, s))
    if cur is not None:
        out.append((start, cur))
    res = []
    for n, cmd in out:
        m = _EXIT.search(cmd)
        res.append((n, cmd, int(m.group(1)) if m else 0))
    return res


def untagged(text: str) -> list:
    """[(line, command)] of every alpaca command in a command block that has no run class."""
    bad = []
    for b in blocks(text):
        if b["cls"] is None:
            for n, cmd, _want in commands(b):
                if alpaca_words(cmd) is not None:
                    bad.append((n, cmd))
    return bad


def alpaca_words(cmd: str):
    """The words after `alpaca` (or `bin/alpaca`) of one shell line, up to the first shell operator,
    or None when the line runs something else. Leading VAR=value words are skipped."""
    cmd = _COMMENT.sub("", cmd.strip())
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    while words and re.match(r"^[A-Z_][A-Z0-9_]*=", words[0]):
        words.pop(0)
    if not words or not _ALPACA.match(words[0]):
        return None
    out = []
    for w in words[1:]:
        if w in _STOP_WORDS:
            break
        out.append(w)
    return out


# ------------------------------------------------------------------ the parser
def parser():
    """alpaca.cli's parser, built the way alpaca.cli.main builds it. No handler runs."""
    from alpaca import cli
    top, sub = cli.build_parser()
    for mod in cli.VERB_MODULES:
        __import__("alpaca.%s" % mod)
    for _name, reg in cli._EXTRA_PARSERS:
        reg(sub)
    return top


def research_parsers() -> dict:
    """{verb: sub-parser} of `alpaca research`, from alpaca.research.cli.parser looked up now."""
    from alpaca.research import cli as rcli
    top = argparse.ArgumentParser(prog="alpaca")
    sub = top.add_subparsers(dest="verb")
    entry = rcli.parser(sub)
    action = [a for a in entry._actions if isinstance(a, argparse._SubParsersAction)][0]
    return dict(action.choices)


def long_options(p) -> list:
    """Every long option of one parser, `--help` left out, in the parser's order."""
    return [o for a in p._actions for o in a.option_strings if o.startswith("--") and o != "--help"]


def _subparsers(p):
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


def _standin_for(action, word: str) -> str:
    if action is not None and action.choices:
        return str(list(action.choices)[0])
    kind = getattr(action, "type", None)
    if kind is int:
        return "1"
    if kind is float:
        return "1.0"
    inner = word.strip("<>").lower()
    if inner in ("run", "run-id"):
        return "rr-001"
    return "x"


def typed_standin(top, argv: list) -> list:
    """argv with each `<placeholder>` word replaced by a value of the type its place takes: the
    option before it (int 1, float 1.0, a choice's first choice, else a word), or, in a sub-command
    position, the first sub-command."""
    cur, out, prev_action = top, [], None
    for word in argv:
        sub = _subparsers(cur)
        if PLACEHOLDER.fullmatch(word):
            if sub is not None and prev_action is None and _positionals(cur) == 0:
                word = list(sub.choices)[0]
            else:
                word = _standin_for(prev_action, word)
        elif PLACEHOLDER.search(word):
            word = PLACEHOLDER.sub(lambda m: _standin_for(None, m.group(0)), word)
        out.append(word)
        if sub is not None and word in sub.choices:
            cur, prev_action = sub.choices[word], None
            continue
        opt = word.split("=", 1)[0]
        act = cur._option_string_actions.get(opt) if opt.startswith("-") else None
        prev_action = act if act is not None and act.nargs != 0 and "=" not in word else None
    return out


def _positionals(p) -> int:
    """Positionals of `p` other than a sub-command slot (a placeholder then fills a positional)."""
    return sum(1 for a in p._actions if not a.option_strings
               and not isinstance(a, argparse._SubParsersAction))


def expand_synopsis(line: str) -> str:
    """A synopsis line as a command: `[` and `]` dropped (every optional part kept), `a|b` read as
    its first choice, a trailing `...` dropped."""
    line = line.replace("[", " ").replace("]", " ").replace("...", " ")
    words = []
    for w in line.split():
        if "|" in w and not PLACEHOLDER.fullmatch(w):
            w = w.split("|", 1)[0]
        words.append(w)
    return " ".join(words)


def parse_error(top, argv: list):
    """None when argv parses, else the parser's last error line."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            top.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            last = [ln for ln in err.getvalue().splitlines() if ln.strip()]
            return last[-1] if last else "exit %s" % exc.code
    return None


def parse_lines(text: str, top=None) -> list:
    """[(line, class, command, argv, error)] for every alpaca line of every classed block. A scratch
    line with a placeholder is an error by itself: it would fail as written."""
    top = top or parser()
    out = []
    for b in blocks(text):
        if b["cls"] is None:
            continue
        if b["cls"] == "synopsis":
            rows = [(n, ln.strip(), 0) for n, ln in enumerate(b["body"], b["line"] + 1)
                    if ln.strip() and not ln.strip().startswith("#")]
        else:
            rows = commands(b)
        for n, cmd, _want in rows:
            line = expand_synopsis(cmd) if b["cls"] == "synopsis" else cmd
            words = alpaca_words(line)
            if words is None:
                continue
            if b["cls"] == "scratch" and PLACEHOLDER.search(cmd):
                out.append((n, b["cls"], cmd, words, "a placeholder in a block that runs as written"))
                continue
            argv = typed_standin(top, words)
            out.append((n, b["cls"], cmd, argv, parse_error(top, argv)))
    return out


# ------------------------------------------------------------------ running the scratch blocks
def scratch_env(base: str, shim: str) -> dict:
    """The environment of a scratch run: the subagent marker set (a driving verb that slips through
    is refused before its handler), no disk sweep, the workspace registry and tunnel config under
    `base`, no session id, a synthetic git identity, and `shim` first on PATH."""
    env = dict(os.environ)
    env["PATH"] = "%s%s%s" % (shim, os.pathsep, env.get("PATH", ""))
    env["ALPACA_RESEARCH_AGENT"] = "research-dockit"
    env["ALPACA_DISK_SWEEP"] = "0"
    env["ALPACA_WORKSPACES"] = os.path.join(base, "workspaces.json")
    env["ALPACA_CLOUDFLARED_CONFIG"] = os.path.join(base, "cloudflared.yml")
    for key in ("CLAUDE_CODE_SESSION_ID", "ALPACA_SESSION_ID", "ALPACA_RECORD_SKIP_ROOT"):
        env.pop(key, None)
    env.update({"GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "tester@example.invalid",
                "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "tester@example.invalid"})
    return env


def scratch_project(dest: str, env: dict, shim: str, repo: str = REPO) -> None:
    """`alpaca new` into `dest` from `repo` (stdin closed), then an `alpaca` on PATH that runs the
    new project's own bin/alpaca. Raises RuntimeError when `alpaca new` fails."""
    made = subprocess.run([sys.executable, "-m", "alpaca", "new", "--name", "Scratch", "--dir", dest,
                           "--who", "tester:owner", "--yes", "--no-bootstrap", "--no-serve"],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=900,
                          stdin=subprocess.DEVNULL)
    if made.returncode != 0:
        raise RuntimeError("alpaca new failed (%d): %s%s" % (made.returncode, made.stdout[-2000:],
                                                            made.stderr[-2000:]))
    os.makedirs(shim, exist_ok=True)
    path = os.path.join(shim, "alpaca")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('#!/usr/bin/env bash\nexec "%s/bin/alpaca" "$@"\n' % dest)
    os.chmod(path, 0o755)


def scratch_commands(text: str) -> list:
    """[(line, command, expected exit)] of every scratch block, in document order."""
    out = []
    for b in blocks(text):
        if b["cls"] == "scratch":
            out += commands(b)
    return out


def run_scratch(text: str, dest: str, env: dict) -> list:
    """Run every scratch command in `dest`, in order, each with `bash -c` and stdin closed.
    [{"line", "command", "want", "got", "gate", "tail"}]."""
    rows = []
    for n, cmd, want in scratch_commands(text):
        res = subprocess.run(["bash", "-c", cmd], cwd=dest, env=env, capture_output=True, text=True,
                             timeout=600, stdin=subprocess.DEVNULL)
        out = (res.stdout + res.stderr).strip().splitlines()
        gate = next((ln.strip() for ln in reversed(out) if ln.strip().startswith("GATE ")), "")
        rows.append({"line": n, "command": cmd, "want": want, "got": res.returncode, "gate": gate,
                     "tail": out[-3:]})
    return rows


def log_lines(rows: list) -> list:
    return ["line %d  want %d  got %d  %s  %s  %s" % (r["line"], r["want"], r["got"],
                                                     "ok" if r["want"] == r["got"] else "WRONG",
                                                     r["command"], r["gate"]) for r in rows]


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m alpaca.tests.research_dockit")
    ap.add_argument("--doc", default=GUIDE, help="the guide, relative to the repository")
    ap.add_argument("--emit", metavar="DIR", help="write scratch.sh and parse-only.txt into DIR")
    ap.add_argument("--run", metavar="DIR", help="make a project in DIR and run the scratch blocks")
    ap.add_argument("--log", metavar="FILE", help="with --run: where the log goes")
    a = ap.parse_args(argv)
    with open(os.path.join(REPO, a.doc), encoding="utf-8") as fh:
        text = fh.read()
    if a.emit:
        os.makedirs(a.emit, exist_ok=True)
        with open(os.path.join(a.emit, "scratch.sh"), "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\n# the scratch blocks of %s, in order\n" % a.doc)
            for n, cmd, want in scratch_commands(text):
                fh.write("# %s line %d, exit %d\n%s\n" % (a.doc, n, want, cmd))
        with open(os.path.join(a.emit, "parse-only.txt"), "w", encoding="utf-8") as fh:
            for n, cls, cmd, args, err in parse_lines(text):
                if cls != "scratch":
                    fh.write("line %d  %-10s %s  argv=%s  %s\n" % (n, cls, cmd, args,
                                                                 "parses" if err is None else err))
        print("wrote %s and %s" % (os.path.join(a.emit, "scratch.sh"),
                                   os.path.join(a.emit, "parse-only.txt")))
    if a.run:
        base = os.path.abspath(a.run)
        dest, shim = os.path.join(base, "project"), os.path.join(base, "shim")
        os.makedirs(base, exist_ok=True)
        env = scratch_env(base, shim)
        scratch_project(dest, env, shim)
        rows = run_scratch(text, dest, env)
        lines = log_lines(rows)
        wrong = [r for r in rows if r["want"] != r["got"]]
        lines.append("%d command(s), %d as documented, %d not" % (len(rows), len(rows) - len(wrong),
                                                                  len(wrong)))
        if a.log:
            with open(a.log, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        print("\n".join(lines))
        return 1 if wrong else 0
    return 0


if __name__ == "__main__":
    sys.exit(_main())
