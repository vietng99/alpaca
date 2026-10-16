"""PreToolUse (Bash, Write, Edit, MultiEdit, NotebookEdit): refuse a write outside the op's confirmed sandbox.

An op's owner-confirmed path list (alpaca/sandbox.py, `alpaca sandbox set`; round 4: the free-text batch
answer is only the proposal and binds nothing by itself) names where the work may write. This hook
reads that list for the calling session and denies a Write or Edit whose file, or a Bash command
whose redirection, `tee`, `touch`, `mkdir`, `rm`, `cp`, `mv`, `git clone`, `git worktree add`,
`pytest --basetemp`, `flock FILE` and the like target, lies outside the allowed paths. Reads are never
checked. The paths always allowed inside a sandbox are the project root, the record area `.alpaca/`, the
session scratchpad, `~/.claude/`, `$CLAUDE_CONFIG_DIR`, the session temp root `/tmp/sbx<sid8>` and a few
device files (alpaca/sandbox.py `_always`); the list word `scratchpad` adds every session scratchpad of
the project.

While a sandbox binds the session this hook also keeps `.alpaca/sandbox/env/<sid>.sh` current, the
file SessionStart wired into the Claude env file, so TMPDIR and PYTEST_DEBUG_TEMPROOT point at the
session temp root and a pytest run without `--basetemp` leaves nothing else in /tmp.

HOW IT READS A COMMAND: the command word is found past shell keywords (`then`, `do`, `!`, `{`),
VAR=value and wrappers (`timeout`, `env`, `sudo`, `nohup`, ...) with their options and the values of
those options (`sudo -u root`, `env -u X`, `timeout -s KILL 5`) by one function shared with temp_guard
(`command_head`); `env -C DIR` and `sudo -D DIR` set the folder of the command; `bash -c`, `bash -lc`,
`( ... )` and `$( ... )` are opened up, and the working directory is a stack that a `cd` inside them does
not leak out of (`pushd` and `popd` keep their own list; a `cd` in a pipe, in the background or after
`coproc` moves nothing). The variables the command sets itself (`B=/x; rm -rf $B`, `export TMPDIR=/x`,
`TMPDIR=/x pytest`, `B=/x bash -c '...'`, `for B in /x; do`, `B=(/x)`, `HOME=/x` for `~`) are followed
into the targets that use them, and `${B:-/x}` of a name it does not know reads as its default. One pass
over the text (temp_guard.strip_heredocs) reads it as bash does: the quote state carries across lines, a
here-document body (any delimiter word, `<<\\EOF` and `<<END-OF` included) is data and ends only at its
exact line, no body starts in a comment, an arithmetic `$((1<<X))` or a here-string, a backslash at the
end of a line joins the next line except in a comment, and comments are dropped. A quoted or escaped lone
`>` is an argument. Wrapper values are skipped (`timeout 0.5`, `exec -a NAME`, `uv run --with X`, `flock
-w 5 FILE`). A pytest call that drops TMPDIR (`unset TMPDIR`, `env -u TMPDIR`, `env -i` without setting it
again; round 5: also `export -n TMPDIR`, `declare +x TMPDIR` and an empty `TMPDIR=`) is denied: pytest's
temp root and every `tempfile` call fall back to /tmp. A command over 64 KB (after the here-document bodies
are cut), or one the guard cannot read within its 2 s parsing budget, is denied with a clear message
("split it"), never skipped; round 5: the clock is read inside variable expansion too, a value over 64 KB
or more than 1 MB of values in one command is the same deny, and so is a MemoryError or RecursionError
while reading, in a session with an enforced list. A command the tokenizer cannot read (an unbalanced
quote, `"$(echo '"')"`, `$'it\\'s'`) is scanned conservatively for its write shapes instead (`scan_split`):
a write outside the list is denied, anything else is not. A copy, move or link into a folder is judged
where each source lands (`cp -r stage/sandbox .alpaca/` lands on the guard's folder), and an archive
extracted into `.alpaca` is denied. A record that is locked for more than half a second uses the last
cached answer when it is at most 200 s old and otherwise gives no decision, and any other error is "no
decision". The guard's own folder `.alpaca/sandbox/` (cache, env file, denial log) and the session
transcripts under each Claude config folder (`projects/*/*.jsonl`) cannot be written by name by a tool call (a hard link made first is not followed, a stated
limit); a cache entry dated in the future, or one whose session id or hash does not match, is ignored.

WHAT IT CANNOT SEE, and the door self-check stays the second line for all of it (tests pin each as
"no decision", tests/test_sandbox_guard_reading.py): writes made inside a program (a script file,
`make`, `npm`, `python -c "open(...)"`, a heredoc fed to an interpreter, `alpaca` verbs, git's own
files, release gates, which give each child a TMPDIR of their own under /tmp: "writes inside
programs"); targets built at run time (`$VAR` the command did not set, `$(...)`, `eval`), by `xargs` or
by `find -exec`; a bare `mktemp`; `cp -at DIR` (combined short flags ending in `t`); `tar -C<dir>` (a
value stuck to the flag); a symlink made earlier in the same command and written through (`ln -s OUT l
&& echo > l/f`); `sort -o FILE`; `env -S` (a split string hides the command); a process substitution;
a redirection glued to a separator (`)>f`, `;>f`, `&&>f`, `|>f`) and `<>`; a `#` inside a word, which
cuts the line like a comment; the `function` keyword; `$(...)` inside double quotes and backticks; a
third nested `bash -c`; a brace list (`{OUT,/var}/f`); `pytest -o cache_dir=` and `--junitxml=`; and
the other clauses of the answer (`sudo`, installs, network). Two more limits: the temp root name takes the
first 8 characters of the session id, so two ids that share them (Codex ids) share the folder name and are
told apart by the marker only; and a widen race: a hook that read the record just before `alpaca sandbox
set` or `widen` can rewrite the cache after the verb cleared it, for up to 20 s. Round 5 limits: an
archive extracted into the project root may hold `.alpaca/sandbox/` (only an extraction into `.alpaca`
itself is denied); a function body is read as if it ran (`f() { unset TMPDIR; }; pytest` is denied
though `f` is never called); the scan of an unreadable command does not look inside `$(...)` within double
quotes either; and a harness place (`~/.claude`, `$CLAUDE_CONFIG_DIR`, `.alpaca/`) replaced by a link to a
specific folder is followed (one to `/` or to a folder too broad for a list is dropped).

It writes one thing on stdout: a PreToolUse deny decision. Every other path, any exception included,
prints nothing, so the call goes ahead (fail open). A session with no claim, an op with no
owner-confirmed path list or an unreadable record is never blocked. A deny appends a line to `.alpaca/sandbox/denials.jsonl`
(best effort). The hook never writes the record.
"""
import json
import os
import re
import shlex
import time

from alpaca.hooks import common

FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}
PUNCT = ";&|()<>\n"
REDIRECTS = {">", ">>", ">|", "&>", "&>>", ">&"}
INPUTS = {"<", "<<", "<<-", "<<<", "<&"}
DEVICES = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/stdin")
DEVICE_DIRS = ("/dev/fd/", "/proc/self/fd/")
DENIALS = os.path.join(".alpaca", "sandbox", "denials.jsonl")
ALL_POSITIONAL = {"touch", "mkdir", "rm", "rmdir", "truncate", "shred", "unlink", "tee"}
VALUE_OPTS = {
    "touch": {"-d", "-t", "-r", "--date", "--reference"},
    "mkdir": {"-m", "--mode", "-Z", "--context"},
    "truncate": {"-s", "--size", "-r", "--reference"},
    "shred": {"-n", "--iterations", "-s", "--size"},
    "cp": {"-S", "--suffix", "--backup", "--preserve", "--sparse", "--reflink"},
    "mv": {"-S", "--suffix"},
    "install": {"-m", "--mode", "-o", "--owner", "-g", "--group", "-S", "--suffix", "-Z"},
    "ln": {"-S", "--suffix"},
    "rsync": {"-e", "--rsh", "--exclude", "--include", "--exclude-from", "--include-from", "-f",
              "--filter", "--port", "--bwlimit", "--chmod", "--partial-dir", "-M"},
}
CLONE_VALUE_OPTS = {"-b", "--branch", "--depth", "-o", "--origin", "--reference", "-c", "--config",
                    "--separate-git-dir", "--template", "-j", "--jobs", "--filter", "--shallow-since",
                    "-u", "--upload-pack", "--shallow-exclude", "--server-option", "--ref-format"}
INIT_VALUE_OPTS = {"--template", "-b", "--initial-branch", "--separate-git-dir", "--object-format",
                   "--ref-format"}
GLOB_CHARS = re.compile(r"[*?\[{]")
#: A command longer than this (after the here-document bodies are cut) is not read: shlex takes time that
#: grows with the square of one huge quoted word, and the hook has 10 s. The call is denied with a clear
#: message, never skipped: padding a command must not switch the guard off. None of 24,972 real commands
#: came near it (check-adversarial-r2.md part 3).
MAX_COMMAND_BYTES = 64 * 1024
#: The guard reads a command for at most this long (round 4). Past it the call is denied ("split it"):
#: none of the ~25,000 real commands of the transcript sweep took more than a few milliseconds, and a hook
#: that runs into its 10 s timeout gives no decision, so a slow command must not be a way around the guard.
PARSE_BUDGET_S = 2.0
#: The name a pytest call that drops TMPDIR (`unset TMPDIR`, `env -u TMPDIR`, `env -i`) is reported under;
#: its target is /tmp, where pytest and `tempfile` fall back to.
PYTEST_NO_TMPDIR = "pytest without TMPDIR"
#: A quoted word made only of operator characters (`'>'`, `">>"`) or an escaped one (`\>`) is an argument.
_LONE_OPERATOR = re.compile(r"""(['"])[<>&|]+\1|\\[<>&|]""")
#: pytest flags that print or collect and write nothing: the unwired-pytest rule leaves them alone.
PYTEST_QUIET = {"--version", "-V", "--help", "-h", "--collect-only", "--co", "--fixtures", "--markers",
                "--fixtures-per-test"}


# ------------------------------------------------------------------ reading a Bash command
#: A folder that cannot be told (`env -C $X cmd`): a relative target of that command is not judged.
NOWHERE = object()
#: $NAME, ${NAME} and ${NAME<op>WORD} with op one of :- - := = :+ + :? ? (the default, the alternative).
_VAR = re.compile(r"\$(?:\{(\w+)(?:(:?[-=+?])([^{}]*))?\}|(\w+))")
_ASSIGN_WORD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?)=(.*)$", re.S)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DECLARERS = {"export", "declare", "local", "readonly", "typeset"}
PYTEST_TEMP_VARS = ("TMPDIR", "PYTEST_DEBUG_TEMPROOT")
#: A value no variable lookup gives: the name is not known to the guard at all.
_UNKNOWN = object()


#: Round 5 (check-adversarial-r4.md B-5): a 40 KB command doubled a variable into gigabytes inside one
#: segment, past the budget and the hook's deadline. A value (or an expanded word) over MAX_VALUE_BYTES, or
#: more than MAX_VALUES_BYTES assigned in one command, is "too complex": a deny in an enforced session. A real
#: command is at most 64 KB, so only a value grown by expansion reaches either.
MAX_VALUE_BYTES = 64 * 1024
MAX_VALUES_BYTES = 1024 * 1024
#: Expansion steps between two looks at the clock.
_EXPAND_CLOCK = 64


class Ctx:
    """Where a command's relative targets resolve: the running cwd (None once it is unknown), the
    sandbox temp root that `$TMPDIR` stands for, the variables the command itself set so far (name to
    value, None when the value cannot be told) and the names it unset or unexported (`dropped`).
    `deadline` (time.monotonic) is read inside expansion too, and the value caps raise
    temp_guard.TooComplex."""

    def __init__(self, cwd, temp_root, deadline=None):
        self.cwd, self.temp_root = cwd, temp_root
        self.vars = {}
        self.dropped = set()
        self.deadline = deadline
        self.assigned = 0
        self.steps = 0

    def _tick(self):
        self.steps += 1
        if self.deadline is not None and self.steps % _EXPAND_CLOCK == 0 and time.monotonic() > self.deadline:
            from alpaca.hooks import temp_guard
            raise temp_guard.TooComplex("over the parsing budget")

    def lookup(self, name):
        """The value of $name: one the command set, else $TMPDIR as the sandbox temp root and $HOME as the
        home folder; None when it cannot be told, _UNKNOWN for a name the guard knows nothing about."""
        if name in self.vars:
            return self.vars[name]
        if name == "TMPDIR":
            return self.temp_root or None
        if name == "HOME":
            return os.path.expanduser("~")
        return _UNKNOWN

    def home(self):
        val = self.lookup("HOME")
        return None if val is _UNKNOWN else val

    def expand(self, word, depth=0):
        """`word` with every $NAME, ${NAME} and ${NAME:-default} replaced, or None when a value cannot be
        told or a `$`, `$(` or backtick is left. `${NAME:-default}` of a name the guard knows nothing about
        reads as its default: the value the command was written to use (round 4). The result is built
        piece by piece and raises TooComplex past MAX_VALUE_BYTES (round 5)."""
        from alpaca.hooks import temp_guard
        if depth > 8:
            return None
        out, pos, size = [], 0, 0
        for m in _VAR.finditer(word):
            self._tick()
            name = m.group(1) or m.group(4)
            op, alt = m.group(2), m.group(3)
            val = self.lookup(name)
            if op:
                is_set = val is not _UNKNOWN and val is not None
                empty = is_set and val == ""
                if val is None:
                    return None
                if op in (":-", ":=", "-", "="):
                    use_alt = (not is_set) or (op[0] == ":" and empty)
                    val = self.expand(alt, depth + 1) if use_alt else val
                elif op in (":+", "+"):
                    use_alt = is_set and not (op[0] == ":" and empty)
                    val = self.expand(alt, depth + 1) if use_alt else ("" if is_set else None)
                elif val is _UNKNOWN:                    # ${NAME:?message}: the value or an error
                    val = None
            if val is None or val is _UNKNOWN:
                return None
            size += (m.start() - pos) + len(val)
            if size > MAX_VALUE_BYTES:
                raise temp_guard.TooComplex("a value over %d bytes" % MAX_VALUE_BYTES)
            out += [word[pos:m.start()], val]
            pos = m.end()
        out.append(word[pos:])
        word = "".join(out)
        return None if "$" in word or "`" in word else word

    def tilde(self, w):
        """`~` and `~/x` with the home folder the command sees (a `HOME=` it set counts); None when it
        cannot be told."""
        if w == "~" or w.startswith("~/"):
            home = self.home()
            return None if home is None else home + w[1:]
        return w

    def assign(self, name, plus, raw):
        """Record `NAME=raw` set by this command (`+=` and a value that cannot be told give None)."""
        val = None
        if not plus:
            raw = self.tilde(raw)
            val = None if raw is None else self.expand(raw)
            if val is not None:
                self.assigned += len(val)
                if self.assigned > MAX_VALUES_BYTES:
                    from alpaca.hooks import temp_guard
                    raise temp_guard.TooComplex("over %d bytes of values" % MAX_VALUES_BYTES)
            if val is not None and re.search(r"\s", val):
                val = None                                  # word-split when used: not one path
        self.vars[name] = val
        self.dropped.discard(name)

    def unset(self, name):
        self.vars[name] = None
        self.dropped.add(name)

    def target(self, word, cwd=None):
        """(absolute path, is_glob_prefix) for one target word, or None when it cannot be read."""
        if not isinstance(word, str) or not word or word == "-":
            return None
        w = self.tilde(word)
        if w is None:
            return None
        w = self.expand(w)
        if w is None:
            return None
        if os.path.isabs(w):
            flat = os.path.normpath(w)                  # /dev/fd/../../tmp/x is /tmp/x, not a device
            if flat in DEVICES or flat.startswith(DEVICE_DIRS):
                return flat                             # /dev/stderr is a link to a terminal or pipe
        base = cwd if cwd is not None else self.cwd
        if base is NOWHERE:
            base = None
        glob = GLOB_CHARS.search(w)
        if glob:
            w = w[:glob.start()]
            w = w[:w.rfind("/") + 1] if "/" in w else ""
            if not w:
                w = "./"
        if not os.path.isabs(w):
            if base is None:
                return None
            w = os.path.join(base, w)
        return os.path.realpath(w)                      # a link before `..` is followed first, as the kernel does


def _strip_redirects(words):
    """(words without redirections and their operands, [redirect target words])."""
    out, targets, i = [], [], 0
    while i < len(words):
        tok = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None
        if tok in REDIRECTS and nxt is not None:
            if tok == ">&" and (nxt.isdigit() or nxt == "-"):
                pass                                     # >&2 duplicates a descriptor
            else:
                targets.append(nxt)
            if out and out[-1].isdigit():
                out.pop()                                # the N of N>file
            i += 2
            continue
        if tok in INPUTS and nxt is not None:
            if out and out[-1].isdigit():
                out.pop()
            i += 2
            continue
        out.append(tok)
        i += 1
    return out, targets


def _head(words):
    """The words from the command word on, past shell keywords, VAR=value, wrappers and their flags
    (one function for both write guards: alpaca/hooks/temp_guard.py `command_head`)."""
    from alpaca.hooks import temp_guard
    return temp_guard.command_head(words)


def _positional(args, value_opts=()):
    """The non-option words of args; `--` ends the options, a word in value_opts takes the next one."""
    out, i, opts = [], 0, True
    while i < len(args):
        a = args[i]
        if opts and a == "--":
            opts = False
        elif opts and a.startswith("-") and a != "-":
            if a in value_opts:
                i += 1
        else:
            out.append(a)
        i += 1
    return out


def _opt_values(args, short, long_):
    """Values of `-X V`, `-XV` (short), `--long V` and `--long=V` in args."""
    out = []
    for i, a in enumerate(args):
        if a in short or a in long_:
            if i + 1 < len(args):
                out.append(args[i + 1])
        elif a.startswith("--") and "=" in a and a.split("=", 1)[0] in long_:
            out.append(a.split("=", 1)[1])
        elif short and len(a) > 2 and a[0] == "-" and a[1] != "-" and a[:2] in short:
            out.append(a[2:])
    return out


def _git_targets(args, here, ctx):
    """[(word, base dir)] a git call writes: clone, worktree add, init."""
    from alpaca.hooks import git_guard
    out, target, i = [], here, 0
    while i < len(args) and args[i].startswith("-"):
        opt = args[i]
        if opt in git_guard.GIT_OPTS_WITH_VALUE and i + 1 < len(args):
            if opt == "-C":
                target = ctx.target(args[i + 1], target)
                if target is None:
                    return []
            i += 2
        else:
            i += 1
    if i >= len(args):
        return out
    sub, rest = args[i], args[i + 1:]
    if sub == "clone":
        pos = _positional(rest, CLONE_VALUE_OPTS)
        if len(pos) >= 2:
            out.append((pos[1], target))
        elif len(pos) == 1:
            name = re.split(r"[/:]", pos[0].rstrip("/"))[-1]
            name = name[:-4] if name.endswith(".git") else name
            if name:
                out.append((name, target))
    elif sub == "worktree" and rest[:1] == ["add"]:
        pos = _positional(rest[1:], {"-b", "-B", "--reason"})
        if pos:
            out.append((pos[0], target))
    elif sub == "init":
        pos = _positional(rest, INIT_VALUE_OPTS)
        out.append((pos[0] if pos else ".", target))
    return out


def _tar_targets(args):
    """The words a tar writes: the archive it creates (-f) and the folder it extracts into."""
    letters, files, dirs = "", [], []
    i = 0
    if args and not args[0].startswith("-") and args[0].isalpha():
        letters += args[0]
        i = 1
        operands = [a for a in args[1:] if not a.startswith("-")]
        if "f" in args[0] and operands:
            files.append(operands[0])
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a.startswith("--"):
            key, _eq, val = a.partition("=")
            if key in ("--create", "--extract", "--get"):
                letters += "c" if key == "--create" else "x"
            elif key in ("--file", "--directory"):
                if not _eq and nxt is not None:
                    val, i = nxt, i + 1
                (files if key == "--file" else dirs).append(val)
        elif a.startswith("-") and a[1:].isalpha():
            letters += a[1:]
            if a[-1] == "f" and nxt is not None:
                files.append(nxt)
                i += 1
            elif a[-1] == "C" and nxt is not None:
                dirs.append(nxt)
                i += 1
        i += 1
    out = []
    if "c" in letters:
        out += files
    if "x" in letters:
        out += dirs or ["."]
    return out


def _tar_extracts(args):
    letters = "".join(a.lstrip("-") for a in args if a.startswith("-") and not a.startswith("--") and a[1:].isalpha())
    if args and not args[0].startswith("-") and args[0].isalpha():
        letters += args[0]
    return "x" in letters or any(a in ("--extract", "--get") for a in args)


#: How many entries of a source folder copied by its contents (`rsync -a src/ dst`, `cp -a src/. dst`) are read.
_LANDING_MAX = 200


def _landings(name, sources, dest, base, ctx):
    """[(word, base)] for where each source of a copy, move or link lands inside the folder `dest`:
    `dest/<name of the source>`, or for a source copied by its contents (`rsync src/`, `cp src/.`) `dest/<each
    entry of it>`. A folder copied into `.alpaca/` lands on the guard's own folder when it is named `sandbox`
    (round 5, check-adversarial-r4.md MINOR 3): the landing is the target that is denied there. Elsewhere a
    landing lies inside `dest`, so it adds no deny that `dest` does not."""
    out = []
    for src in sources:
        if not isinstance(src, str) or not src or src == "-":
            continue
        contents = (name == "rsync" and src.endswith("/")) or src.endswith("/.")
        if contents:
            real = ctx.target(src, base)
            try:
                names = sorted(os.listdir(real))[:_LANDING_MAX] if isinstance(real, str) else []
            except OSError:
                names = []
            out += [(os.path.join(dest, n), base) for n in names]
        else:
            leaf = os.path.basename(src.rstrip("/"))
            if leaf and leaf not in (".", ".."):
                out.append((os.path.join(dest, leaf), base))
    return out


def segment_targets(words, ctx):
    """[(absolute target, command name)] one simple command writes."""
    words, redirs = _strip_redirects(words)
    found = [(w, None) for w in redirs]          # a redirection resolves against the running cwd
    from alpaca.hooks import temp_guard
    cmdline = _head(words)
    name = os.path.basename(cmdline[0]) if cmdline else ""
    args = cmdline[1:]
    base = ctx.cwd
    chdir = temp_guard.wrapper_chdir(words)      # env -C DIR, sudo -D DIR: the command runs there
    if chdir is not None:
        base = ctx.target(chdir)
        if base is None:
            base = NOWHERE
    cmd_found = []
    if name in ALL_POSITIONAL:
        cmd_found += [(p, base) for p in _positional(args, VALUE_OPTS.get(name, ()))]
    elif name in ("cp", "mv", "install", "ln", "rsync"):
        opts = VALUE_OPTS.get(name, ())
        tdir = _opt_values(args, {"-t"}, {"--target-directory"})
        pos = _positional(args, set(opts) | {"-t", "--target-directory"})
        if tdir:
            cmd_found += [(t, base) for t in tdir]
            cmd_found += _landings(name, pos, tdir[-1], base, ctx)
        elif name == "install" and ("-d" in args or "--directory" in args):
            cmd_found += [(p, base) for p in pos]
        elif name == "ln" and len(pos) == 1:
            cmd_found.append((".", base))
            cmd_found += _landings(name, pos, ".", base, ctx)
        elif len(pos) >= 2:
            last = pos[-1]
            if not (name == "rsync" and ":" in last and not last.startswith(("/", ".", "~"))):
                cmd_found.append((last, base))
                if "-T" not in args and "--no-target-directory" not in args:
                    cmd_found += _landings(name, pos[:-1], last, base, ctx)
    elif name == "dd":
        cmd_found += [(a[3:], base) for a in args if a.startswith("of=")]
    elif name == "sed":
        in_place = any((re.match(r"^-[A-Za-z]*i", a) and not a.startswith("--")) or
                       a.startswith("--in-place") for a in args)
        if in_place:
            scripted = any(a in ("-e", "-f", "--expression", "--file") or a.startswith(("--expression=", "--file="))
                           for a in args)
            pos = _positional(args, {"-e", "-f", "--expression", "--file", "-l", "--line-length"})
            cmd_found += [(p, base) for p in (pos if scripted else pos[1:])]
    elif name == "tar":
        for t in _tar_targets(args):
            cmd_found.append((t, base))
            if _tar_extracts(args):                      # an archive's entries cannot be read: what lands in a
                cmd_found.append((os.path.join(t, "sandbox"), base))   # `.alpaca` it extracts into is the guard's
    elif name == "unzip":
        d = _opt_values(args, {"-d"}, set())
        cmd_found += [(x, base) for x in d] + [(os.path.join(x, "sandbox"), base) for x in d]
    elif name == "curl":
        cmd_found += [(v, base) for v in _opt_values(args, {"-o"}, {"--output"})]
        cmd_found += [(v, base) for v in _opt_values(args, set(), {"--output-dir"})]
        for a in args:                                  # -sSo FILE
            if re.match(r"^-[A-Za-z]+o$", a):
                i = args.index(a)
                if i + 1 < len(args):
                    cmd_found.append((args[i + 1], base))
    elif name == "wget":
        cmd_found += [(v, base) for v in _opt_values(args, {"-O"}, {"--output-document"})]
        cmd_found += [(v, base) for v in _opt_values(args, {"-P"}, {"--directory-prefix"})]
    elif name == "mktemp":
        cmd_found += [(v, base) for v in _opt_values(args, {"-p"}, {"--tmpdir"})]
        cmd_found += [(p, base) for p in _positional(args, {"-p", "--suffix"}) if "/" in p]
    elif name.startswith("python") or name in ("pip", "pip3"):
        a = list(args)
        if name.startswith("python"):
            j = 0
            while j < len(a) and a[j] != "-m" and a[j].startswith("-"):
                j += 2 if a[j] in ("-X", "-W") else 1
            a = a[j:]
            if a[:2] == ["-m", "venv"]:
                pos = _positional(a[2:], {"--prompt"})
                cmd_found += [(p, base) for p in pos[-1:]]
            elif a[:2] == ["-m", "pip"]:
                a = a[2:]
            else:
                a = []
        if a[:1] == ["install"] or a[:1] == ["download"]:
            cmd_found += [(v, base) for v in _opt_values(a, {"-t", "-d"}, {"--target", "--prefix", "--dest"})]
    elif name == "git":
        cmd_found += _git_targets(args, base, ctx)
    for wrapper, opt, val in temp_guard.scan(words)[1]:
        if wrapper == "flock" and opt == temp_guard.FLOCK_FILE:
            found.append((val, None))                    # `flock FILE cmd` makes FILE when it is missing
    no_tmpdir = False
    if temp_guard._is_pytest(cmdline) or temp_guard._is_pytest(words):
        cmd_found += [(b, base) for b in temp_guard._basetemps_of(words)]
        # pytest puts its temp tree under TMPDIR / PYTEST_DEBUG_TEMPROOT: a prefix assignment of this
        # command (`TMPDIR=X pytest`) and one set earlier in the same command (`export TMPDIR=X; pytest`)
        for w in words[:temp_guard.command_index(words)]:
            m = _ASSIGN_WORD.match(w)
            if m and m.group(1) in PYTEST_TEMP_VARS and not m.group(2):
                cmd_found.append((m.group(3), base))
        cmd_found += [(ctx.vars[v], base) for v in PYTEST_TEMP_VARS if ctx.vars.get(v)]
        # round 4: TMPDIR dropped (`unset TMPDIR`, `env -u TMPDIR`, `env -i`) and not set again: pytest's
        # temp root and every `tempfile` call fall back to /tmp
        # round 5 (MINOR 1): unexported (`export -n`, `declare +x`) or set empty (`TMPDIR=`): tempfile skips
        # an empty TMPDIR, so the same fallback to /tmp
        front = _front_env(words).get("TMPDIR")
        no_tmpdir = front == "drop" or (front is None and ("TMPDIR" in ctx.dropped or ctx.vars.get("TMPDIR") == ""))
        if PYTEST_QUIET.intersection(words):
            no_tmpdir = False                            # `pytest --version` and the like write nothing
    out = []
    for word, b in found + cmd_found:
        got = ctx.target(word, b)
        if got is not None:
            out.append((got, name or "redirect"))
    if no_tmpdir:
        out.append(("/tmp", PYTEST_NO_TMPDIR))
    return out


def _front_env(words):
    """{NAME: "drop" | "set"} for what the front of one simple command does to the environment of the
    command it runs, in order: `NAME=x` sets, `env -u NAME` / `--unset=NAME` drops, `env -i` / `env -` /
    `--ignore-environment` drops every name (each of PYTEST_TEMP_VARS here)."""
    from alpaca.hooks import temp_guard
    state, cur, i = {}, None, 0
    end = temp_guard.command_index(words)
    while i < end:
        w = words[i]
        m = _ASSIGN_WORD.match(w)
        if os.path.basename(w) in temp_guard.WRAPPERS:
            cur = os.path.basename(w)
        elif m and not w.startswith("-"):
            state[m.group(1)] = "set" if m.group(3) != "" else "drop"      # `TMPDIR=`: empty is no TMPDIR
        elif cur == "env" and w.startswith("-"):
            if w in ("-", "-i", "--ignore-environment"):
                state.update({v: "drop" for v in PYTEST_TEMP_VARS})
            elif w.startswith("--unset"):
                key, eq, val = w.partition("=")
                if key == "--unset":
                    if not eq and i + 1 < end:
                        val, i = words[i + 1], i + 1
                    state[val] = "drop"
            elif not w.startswith("--"):
                for j in range(1, len(w)):
                    letter = w[j]
                    if letter == "i":
                        state.update({v: "drop" for v in PYTEST_TEMP_VARS})
                    elif letter in "uCS":                # these take a value: the rest, or the next word
                        val = w[j + 1:]
                        if not val and i + 1 < end:
                            val, i = words[i + 1], i + 1
                        if letter == "u":
                            state[val] = "drop"
                        break
        i += 1
    return state


def _parts(command, deadline=None):
    """[(separator, words)] of a Bash command: the here-document bodies cut (they are data) and the
    backslash-newlines joined, in one pass (temp_guard.strip_heredocs), a quoted or escaped lone operator
    made a plain word, the shell opened up (`bash -c`, `( ... )`)."""
    from alpaca.hooks import temp_guard
    joined = temp_guard.strip_heredocs(command, join=True, deadline=deadline, comments=False)
    text = _LONE_OPERATOR.sub("__OP__", joined)
    try:
        return text, temp_guard._split(text, punct=PUNCT, deadline=deadline)
    except ValueError:                                   # round 5 (B-4): the tokenizer gave up; bash may not
        return text, scan_split(text, deadline)


#: One token of the conservative scan: a word made of quoted parts (`'...'`, `"..."`, `$'...'`), escapes and
#: plain characters, else an operator, else a lone quote character that closes nothing (dropped).
_LOOSE = re.compile(r"""(?:\$'(?:\\.|[^'\\])*'|'[^']*'|"(?:\\.|[^"\\])*"|\\.|[^\s'"\\;&|()<>`])+"""
                    r"""|&>>|&>|&&|\|\||\|&|>>|>\||>&|<<<|<<-|<<|<&|[<>]|[;&|()\n`]|['"]""", re.S)
_LOOSE_PART = re.compile(r"""\$'((?:\\.|[^'\\])*)'|'([^']*)'|"((?:\\.|[^"\\])*)"|\\(.)|(.)""", re.S)


def _unquote(word):
    out = []
    for m in _LOOSE_PART.finditer(word):
        ansi, single, double, esc, plain = m.groups()
        if ansi is not None:
            out.append(re.sub(r"\\(.)", r"\1", ansi))
        elif single is not None:
            out.append(single)
        elif double is not None:
            out.append(re.sub(r'\\([\\"$`])', r"\1", double))
        else:
            out.append(esc if esc is not None else plain)
    return "".join(out)


def scan_split(text, deadline=None, depth=0):
    """[(separator, words)] of a command the tokenizer could not read (an unbalanced quote, `"$(echo '"')"`,
    `$'it\\'s'`): a conservative scan of the raw text (round 5, check-adversarial-r4.md B-4). Quoted parts are
    kept as in bash where they balance, a quote character that closes nothing is dropped, and every write
    shape the reader knows (redirections, tee, cp, mv, rm, mkdir, touch, ln, install, dd, sed -i and the
    rest) is then read from the words as for any command, so a write outside the list in front of the
    unreadable part is denied and a command that writes only inside, or nothing, is not."""
    from alpaca.hooks import temp_guard
    out, seg, pend = [], [], []
    for n, m in enumerate(_LOOSE.finditer(text)):
        if n % temp_guard._CLOCK_EVERY == 0:
            temp_guard._check(deadline)
        tok = m.group()
        if tok in ("'", '"'):
            continue
        if tok == "`" or tok in temp_guard.SEPARATORS or set(tok) <= temp_guard._PUNCT_ONLY:
            if seg:
                out.append((temp_guard._sep(pend), seg))
                seg, pend = [], []
            pend.append(";" if tok == "`" else tok)
        elif tok in REDIRECTS or tok in INPUTS:
            seg.append(tok)
        else:
            seg.append(_unquote(tok))
    if seg:
        out.append((temp_guard._sep(pend), seg))
    elif any(")" in t for t in pend):
        out.append((temp_guard._sep(pend), []))
    if depth >= 2:
        return out
    opened = []
    for sep, words in out:
        script = temp_guard._shell_script(words) if words else None
        inner = scan_split(script, deadline, depth + 1) if script else []
        if inner:
            opened.append((sep + "(", inner[0][1]))
            opened.extend(inner[1:])
            opened.append((")", []))
        else:
            opened.append((sep, words))
    return opened


def _cd_target(head, ctx):
    """The folder `cd`/`pushd` moves to, or None when it cannot be told (`cd -`, `cd $X`, a flag)."""
    args = head[1:]
    while args and args[0] in ("-P", "-L", "-e", "-@", "--"):
        args = args[1:]
    arg = args[0] if args else "~"
    if arg == "-" or arg.startswith("-"):
        return None
    return ctx.target(arg)


_OPS = re.compile(r"&&|\|\||\|&|;|&|\||\(|\)|\n")


def _runs_apart(parts, k):
    """True when the segment parts[k] runs in a pipe or in the background: after `|` or `|&`, or before
    `|`, `|&` or `&` (not `||`, `&&`, `;`). A `cd` there moves a child shell, not this one."""
    before = _OPS.findall(parts[k][0])
    after = _OPS.findall(parts[k + 1][0]) if k + 1 < len(parts) else []
    return bool(before and before[-1] in ("|", "|&")) or bool(after and after[0] in ("|", "|&", "&"))


def _set_variables(head, stripped, ctx):
    """Apply a segment that only sets variables (`B=x`, `export TMPDIR=x`, `declare -x T=x`), drops
    them (`unset B`) or starts a loop over them (`for B in x; do`) to ctx. True when the segment was one
    of those. A prefix assignment (`B=x cmd`) is not: bash expands the command's words before it assigns.
    A loop over one word sets the name to that word; over more, or over "$@", the name cannot be told."""
    if stripped and all(_ASSIGN_WORD.match(w) for w in stripped):
        for w in stripped:
            m = _ASSIGN_WORD.match(w)
            ctx.assign(m.group(1), bool(m.group(2)), m.group(3))
        return True
    if head and head[0] in DECLARERS:
        # round 5 (MINOR 1): `export -n NAME` and `declare +x NAME` unexport a name: the commands that follow
        # do not get it, as after `unset`; a plain `export NAME` exports it again
        flags = [w for w in head[1:] if w[:1] in ("-", "+") and len(w) > 1 and not _ASSIGN_WORD.match(w)]
        unexport = (head[0] == "export" and any(f[0] == "-" and "n" in f[1:] for f in flags)) or \
            any(f[0] == "+" and "x" in f[1:] for f in flags)
        exports = head[0] == "export" or any(f[0] == "-" and "x" in f[1:] for f in flags)
        for w in head[1:]:
            m = _ASSIGN_WORD.match(w)
            if m:
                ctx.assign(m.group(1), bool(m.group(2)), m.group(3))
            name = m.group(1) if m else (w if _NAME.match(w) else None)
            if name is None:
                continue
            if unexport:
                ctx.dropped.add(name)
            elif exports and not m:
                ctx.dropped.discard(name)
        return True
    if head and head[0] == "unset":
        if "-f" in head[1:]:
            return True                                  # functions, not variables
        for w in head[1:]:
            if not w.startswith("-"):
                ctx.unset(w)
        return True
    if head and head[0] == "for" and len(head) >= 2 and _NAME.match(head[1]):
        values = head[3:] if head[2:3] == ["in"] else None
        if values is not None and len(values) == 1:
            ctx.assign(head[1], False, values[0])
        else:
            ctx.vars[head[1]] = None
            ctx.dropped.discard(head[1])
        return True
    return False


def bash_targets(command, cwd, temp_root, deadline=None):
    """[(absolute target, command name)] the Bash command writes, as far as it can be read. The working
    directory and the variables the command sets are a stack: `( ... )`, `$( ... )` and `bash -c '...'`
    each start with the ones outside and give them back; `pushd` and `popd` keep their own list. A `cd`
    in a pipe, in the background or after `coproc` moves nothing. `deadline` (time.monotonic) raises
    temp_guard.TooComplex once passed."""
    from alpaca.hooks import temp_guard
    _text, parts = _parts(command, deadline)
    ctx = Ctx(cwd if cwd and os.path.isabs(cwd) else None, temp_root, deadline)
    scopes, dirs = [], []
    out = []
    array_of = None                                      # the name `NAME=(...)` is filling, at its "("
    for k, (sep, words) in enumerate(parts):
        if deadline is not None and time.monotonic() > deadline:
            raise temp_guard.TooComplex("over the parsing budget")
        for ch in sep:
            if ch == "(":
                scopes.append((ctx.cwd, list(dirs), dict(ctx.vars), set(ctx.dropped)))
            elif ch == ")" and scopes:
                ctx.cwd, dirs, ctx.vars, ctx.dropped = scopes.pop()
        if array_of is not None:
            name, array_of = array_of, None              # `B=(x y)`: $B is the first element
            if words:
                ctx.assign(name, False, words[0])
                scopes[-1][2][name] = ctx.vars[name] if scopes else None
            continue
        if not words:
            continue
        stripped, redirs = _strip_redirects(words)
        head = _head(stripped)
        last = _ASSIGN_WORD.match(stripped[-1]) if stripped else None
        nxt = parts[k + 1][0] if k + 1 < len(parts) else ""
        if last and last.group(3) == "" and nxt.startswith("(") and (
                all(_ASSIGN_WORD.match(w) for w in stripped) or (head and head[0] in DECLARERS)):
            _set_variables(head, stripped, ctx)
            ctx.vars[last.group(1)] = None
            if ")" not in nxt:                           # `B=()` is empty: $B cannot be a path
                array_of = last.group(1)
            continue
        moves = bool(head) and head[0] in ("cd", "pushd", "popd") and stripped[0] != "coproc"
        if moves or _set_variables(head, stripped, ctx):
            if moves and not _runs_apart(parts, k):
                if head[0] == "popd":
                    ctx.cwd = dirs.pop() if dirs else None
                else:
                    if head[0] == "pushd":
                        dirs.append(ctx.cwd)
                    ctx.cwd = _cd_target(head, ctx)
            for w in redirs:
                t = ctx.target(w)
                if t:
                    out.append((t, "redirect"))
            continue
        out += segment_targets(words, ctx)
    return out


# ------------------------------------------------------------------ the decision
def _device(target):
    return target in DEVICES or target.startswith(DEVICE_DIRS)


def outside(targets, sb):
    """The first (target, command name) of targets that no allowed path covers, or None."""
    from alpaca import sandbox
    for target, name in targets:
        if _device(target) or sandbox.covers(target, sb):
            continue
        return target, name
    return None


def _unwired_pytest(command, sb, root, sid, deadline=None):
    """True for a pytest call that sets no basetemp and no TMPDIR in a session whose Bash calls do not
    source the sandbox env file (it started before the hook was deployed, or the temp root is unusable).
    A call that only prints or collects (`--version`, `--help`, `--collect-only`) writes nothing."""
    from alpaca import sandbox
    from alpaca.hooks import temp_guard
    if sandbox.is_wired(root, sid) or "TMPDIR=" in command or "PYTEST_DEBUG_TEMPROOT=" in command:
        return False
    for _sep, seg in _parts(command, deadline)[1]:
        if temp_guard._is_pytest(seg) and not temp_guard._basetemps_of(seg) \
                and not PYTEST_QUIET.intersection(seg):
            return True
    return False


def _allowed_text(sb):
    always = list(sb.get("always") or [])
    shown = [p for p in list(sb["roots"]) + list(sb["globs"]) if p not in always]
    first = always[:1]                                   # the project root
    named = ", ".join(shown + first)
    temp = ("the session temp root %s (harness, removed at session end)" % sb["temp_root"]
            if sb.get("temp_ok", True) else "no session temp root (%s is not this session's)" % sb["temp_root"])
    return "%s, the session scratchpad, %s (plus .alpaca/, ~/.claude/ and /dev/null)" % (named, temp)


def _owners(sb):
    return ", ".join("%s, owner-confirmed list event %s" % (op, sb["events"][op]) for op in sb["ops"])


GRACE_NOTE = ("A leftover in /tmp from before the guard is not yours to remove: the disk sweep removes the "
              "ones project.yaml `disk: tmp_globs` names after its grace; do not ask to widen the sandbox for it.")


def deny_reason(sb, tool, target, name=None):
    temp = ("Temp files belong in $TMPDIR (%s); for pytest leave --basetemp out or use "
            "--basetemp=$TMPDIR/<name>. " % sb["temp_root"]) if sb.get("temp_ok", True) else \
        "Temp files belong in the session scratchpad. "
    why = ("This pytest call drops TMPDIR (unset, env -u or env -i), so pytest's temp root and every tempfile "
           "call fall back to /tmp; keep TMPDIR as the session sets it. " if name == PYTEST_NO_TMPDIR else "")
    return ("Alpaca sandbox (%s): %s would write %s, outside the paths the owner allowed: %s. "
            "%s%sIf the work needs %s, stop and ask the owner; only the owner "
            "widens the sandbox, by pasting `alpaca sandbox widen %s --path <dir>` as a message of this session "
            "(a confirmation typed at a terminal is not accepted); then run that line with `--by owner --quote "
            "'<the pasted line>'`. Writing it another way (a script, python -c) is the same breach and the door "
            "self-check reports it. %s" % (_owners(sb), tool, target, _allowed_text(sb), why, temp, target,
                                           sb["ops"][0], GRACE_NOTE))


def deny_too_long(sb, size):
    return ("Alpaca sandbox (%s): this command is %d bytes after here-document bodies, too long for the "
            "sandbox guard to read (the limit is %d), so it cannot be checked against the paths the owner "
            "allowed; split it, or put the long data in a here-document or a file."
            % (_owners(sb), size, MAX_COMMAND_BYTES))


def deny_too_complex(sb):
    return ("Alpaca sandbox (%s): command too long or complex for the sandbox guard; split it. The guard "
            "reads a command for at most %g s and follows variables up to %d KB each, and this one went past "
            "that, so it cannot be checked against the paths the owner allowed."
            % (_owners(sb), PARSE_BUDGET_S, MAX_VALUE_BYTES // 1024))


def deny_unwired(sb, root):
    if not sb.get("temp_ok", True):
        return ("Alpaca sandbox (%s): the session temp root %s could not be made safely (a folder that is "
                "not this session's is already there), so pytest would put its default basetemp in /tmp. "
                "Pass --basetemp=<a folder inside the allowed paths, such as the session scratchpad>/"
                "<task>-<role>." % (_owners(sb), sb["temp_root"]))
    return ("Alpaca sandbox (%s): this session started before the sandbox env was wired, so pytest "
            "would put its default basetemp in /tmp. Pass --basetemp=%s/<task>-<role> (a folder inside "
            "the allowed temp root), or start the command with TMPDIR=%s. A resumed or compacted "
            "session gets the wiring at its next SessionStart." % (_owners(sb), sb["temp_root"], sb["temp_root"]))


def note_denial(root, payload, sb, tool, target):
    try:
        path = os.path.join(root, DENIALS)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 3), "sid": common.session_of(payload),
                                 "agent_id": payload.get("agent_id"), "tool": tool,
                                 "target": str(target)[:300], "ops": sb["ops"],
                                 "event": [sb["events"][o] for o in sb["ops"]]}) + "\n")
    except (OSError, ValueError, TypeError):
        pass


def _deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def decide(payload, root=None):
    """The deny decision for one PreToolUse payload, or None to let the call run. Any error in
    reading the record or the command is no decision (fail open)."""
    try:
        return _decide(payload, root)
    except Exception:
        return None


def _decide(payload, root):
    tool = payload.get("tool_name")
    if tool not in FILE_TOOLS and tool != "Bash":
        return None
    sid = common.session_of(payload)
    if sid == "unknown":
        return None
    from alpaca import sandbox
    if root is None:
        root = sandbox.hook_root()
    sb = sandbox.resolve(root, sid)
    if sb is None:
        return None
    tin = payload.get("tool_input") or {}
    cwd = payload.get("cwd")
    if tool in FILE_TOOLS:
        path = tin.get(FILE_TOOLS[tool])
        if not isinstance(path, str) or not path:
            return None
        path = os.path.expanduser(path)                     # ~/x is the home folder, not <cwd>/~/x
        if not os.path.isabs(path):
            if not (cwd and os.path.isabs(cwd)):
                return None
            path = os.path.join(cwd, path)
        found = outside([(os.path.realpath(path), tool)], sb)
    else:
        command = tin.get("command")
        if not isinstance(command, str):
            return None
        from alpaca.hooks import temp_guard
        deadline = time.monotonic() + PARSE_BUDGET_S          # parsing time only, the record lookup is before
        try:
            size = len(temp_guard.strip_heredocs(command, deadline=deadline).encode("utf-8", "ignore"))
            if size > MAX_COMMAND_BYTES:                    # too long to read in the hook's 10 s: denied, not skipped
                note_denial(root, payload, sb, tool, "command over 64 KB (%d bytes)" % size)
                return _deny(deny_too_long(sb, size))
            found = outside(bash_targets(command, cwd, sb["temp_root"], deadline), sb)
            if found is None and _unwired_pytest(command, sb, root, sid, deadline):
                note_denial(root, payload, sb, tool, "pytest without --basetemp")
                return _deny(deny_unwired(sb, root))
        except (temp_guard.TooComplex, MemoryError, RecursionError) as exc:
            # over the 2 s budget, over a value cap, out of memory or too deep (round 5, B-5): denied, never
            # skipped, in a session with an enforced list; a session with none never got here
            note_denial(root, payload, sb, tool, "command too complex (%s)" % type(exc).__name__)
            return _deny(deny_too_complex(sb))
    if found is None:
        return None
    target, name = found
    note_denial(root, payload, sb, tool, target)
    return _deny(deny_reason(sb, tool, target, name))


@common.fail_open(record_cost=False)
def main():
    decision = decide(common.read_stdin())
    if decision:
        print(json.dumps(decision))


if __name__ == "__main__":
    main()
