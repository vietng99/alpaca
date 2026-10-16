"""PreToolUse (Bash only): warn, never block, when a pytest run would leave its whole temp tree.

pytest never deletes an explicit `--basetemp DIR`: it empties DIR at the start of a run and leaves
it afterwards. Parallel runs do need a private basetemp (runs sharing `/tmp/pytest-of-<user>`
rotate and delete each other's dirs; `.alpaca/research-engine/wt.sh` gives each run its own and
removes it after), so the leak is a private basetemp nobody removes. On 2026-10-02
`/tmp/t173-full`, `-full2` and `-full3` held 13G of the RAM-backed /tmp this way. The rule
(docs/temp-and-scratch.md): remove the basetemp in the same command (`; rm -rf DIR` or a trap).
A basetemp on the disk (under /var/tmp, /home or the scratchpad) is warned too: the same tests run
about 100 times slower there.

It only ever writes one thing on stdout: a PreToolUse additionalContext note for the agent. It
never denies, never opens the record, and prints nothing on any parse failure (fail open).
"""
import json
import os
import re
import shlex
import time

from alpaca.hooks import common

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")"}
WRAPPERS = {"timeout", "env", "nice", "time", "stdbuf", "exec", "uv", "run", "poetry", "pipenv",
            "sudo", "command", "nohup", "setsid", "ionice", "chrt", "flock"}
#: Shell grammar words that stand where a command name stands: `then touch x`, `do pytest t`, `! cmd`.
KEYWORDS = {"if", "then", "elif", "else", "while", "until", "do", "!", "{", "time", "coproc"}
SHELLS = {"bash", "sh", "zsh", "dash"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_SHELL_C = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
_PUNCT_ONLY = set(";&|()\n")


class TooComplex(Exception):
    """The command could not be read before its deadline (the sandbox guard's 2 s parsing budget)."""


#: Where a word ends in the shell's grammar (the delimiter word of a here-document stops here).
_METACHARS = frozenset(" \t\n;&|()<>")
#: The next character that can change the scanner's state, outside any quote.
_NORMAL_STOP = re.compile(r"[\\'\"#$(<\n]")
_DOUBLE_STOP = re.compile(r"[\\\"]")
_ANSI_STOP = re.compile(r"[\\']")
_ARITH_STOP = re.compile(r"[()]")
#: How many scanner steps run between two looks at the clock.
_CLOCK_EVERY = 256


def _check(deadline):
    if deadline is not None and time.monotonic() > deadline:
        raise TooComplex("over the parsing budget")


def _delimiter(command, i):
    """(the delimiter of a here-document whose word starts at command[i], the index after that word),
    or (None, i) when there is no word. Quotes and backslashes are removed, as bash does: `'EOF'`,
    `"EOF"`, `\\EOF`, `E'O'F` and `END-OF` give EOF, EOF, EOF, EOF and END-OF."""
    n, word = len(command), []
    while i < n and command[i] not in _METACHARS:
        c = command[i]
        if c == "'":
            j = command.find("'", i + 1)
            j = n if j < 0 else j
            word.append(command[i + 1:j])
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and command[j] != '"':
                j += 2 if command[j] == "\\" else 1
            word.append(command[i + 1:min(j, n)].replace("\\", ""))
            i = j + 1
        elif c == "\\":
            word.append(command[i + 1:i + 2])
            i += 2
        else:
            word.append(c)
            i += 1
    text = "".join(word)
    return (text or None), min(i, n)


def _word_start(command, i):
    return i == 0 or command[i - 1] in _METACHARS


def strip_heredocs(command, join=False, deadline=None, comments=True):
    """The command without the bodies of its here-documents: a `<<EOF ... EOF` body is data fed to a
    program (a file being written, a script for python), not commands this shell runs. One pass over the
    whole text, in bash's terms (round 4, check-adversarial-r3.md MINOR 5, 9, 10 and 11):

      * the quote state carries across lines, so a "..." that closes on the line of the opener (a commit
        message over several lines, then `cat > f <<'EOF'`) leaves the opener seen;
      * the delimiter is the whole word, quotes removed (`<<'EOF'`, `<<\\EOF`, `<<END-OF`, `<<E'O'F`), and a
        body ends only at a line that is exactly the delimiter (`<<-`: after its leading tabs);
      * a body starts after the line that holds its opener ends, a backslash-newline does not end it;
      * no body starts inside quotes, after a backslash, in a comment (`true # <<EOF`), in an arithmetic
        `$((1<<X))` or `(( ... ))`, or at a here-string `<<<`.

    `join=True` also removes each backslash-newline outside single quotes and comments (bash joins those
    lines; a comment that ends in a backslash does not continue). `comments=False` drops the text of each
    comment (a `#` that starts a word, to the end of its line; the newline stays): shlex would otherwise
    eat the newline with the comment and run the next line into the commented one. `deadline`
    (time.monotonic) raises TooComplex once passed."""
    n = len(command)
    out = []
    pending = []                                # [(delimiter, strip tabs)] waiting for the end of the line
    i, last, steps = 0, 0, 0
    state, depth = "normal", 0                  # normal, single, ansi, double, comment, arith

    def end_of_line(at):
        """Skip the bodies that start after the newline at command[at]; the index to go on from."""
        pos = at + 1
        while pending and pos <= n:
            delim, tabs = pending.pop(0)
            while pos <= n:
                nl = command.find("\n", pos)
                nl = n if nl < 0 else nl
                line = command[pos:nl]
                pos = nl + 1
                if (line.lstrip("\t") if tabs else line) == delim:
                    break
        pending.clear()
        return min(pos, n)

    _check(deadline)
    while i < n:
        steps += 1
        if steps % _CLOCK_EVERY == 0:
            _check(deadline)
        if state == "single":
            j = command.find("'", i)
            i = n if j < 0 else j + 1
            state = "normal" if j >= 0 else state
            continue
        if state == "ansi":                     # $'...': a backslash escapes, a ' ends it
            m = _ANSI_STOP.search(command, i)
            if m is None:
                i = n
            elif m.group() == "\\":
                i = m.start() + 2
            else:
                i, state = m.start() + 1, "normal"
            continue
        if state == "double":
            m = _DOUBLE_STOP.search(command, i)
            if m is None:
                i = n
            elif m.group() == '"':
                i, state = m.start() + 1, "normal"
            elif command[m.start() + 1:m.start() + 2] == "\n" and join:
                out.append(command[last:m.start()])
                i = last = m.start() + 2
            else:
                i = m.start() + 2
            continue
        if state == "comment":
            j = command.find("\n", i)
            if j < 0:
                i = n
                if not comments:
                    last = n
                continue
            state = "normal"
            if not comments:
                last = j
            if pending:
                out.append(command[last:j + 1])
                i = last = end_of_line(j)
            else:
                i = j + 1
            continue
        if state == "arith":
            m = _ARITH_STOP.search(command, i)
            if m is None:
                i = n
                continue
            depth += 1 if m.group() == "(" else -1
            i = m.start() + 1
            if depth <= 0:
                state = "normal"
            continue
        m = _NORMAL_STOP.search(command, i)
        if m is None:
            break
        k, c = m.start(), m.group()
        if c == "\\":
            if command[k + 1:k + 2] == "\n" and join:
                out.append(command[last:k])
                i = last = k + 2
            else:
                i = k + 2
        elif c == "'":
            i, state = k + 1, "single"
        elif c == '"':
            i, state = k + 1, "double"
        elif c == "#":
            i = k + 1
            if _word_start(command, k):
                state = "comment"
                if not comments:
                    out.append(command[last:k])
                    last = k
        elif c == "$":
            if command.startswith("$((", k):
                i, state, depth = k + 3, "arith", 2
            elif command.startswith("$'", k):
                i, state = k + 2, "ansi"
            else:
                i = k + 1
        elif c == "(":
            if command.startswith("((", k) and _word_start(command, k):
                i, state, depth = k + 2, "arith", 2
            else:
                i = k + 1
        elif c == "<":
            if command.startswith("<<<", k):
                i = k + 3
            elif command.startswith("<<", k):
                j = k + 2
                tabs = command.startswith("-", j)
                j += 1 if tabs else 0
                while j < n and command[j] in " \t":
                    j += 1
                delim, after = _delimiter(command, j)
                if delim is not None:
                    pending.append((delim, tabs))
                    i = after
                else:
                    i = k + 2
            else:
                i = k + 1
        else:                                   # a newline that ends a line
            if pending:
                out.append(command[last:k + 1])
                i = last = end_of_line(k)
            else:
                i = k + 1
    if last < n:
        out.append(command[last:])
    text = "".join(out)
    return text[:-1] if text.endswith("\n") and not command.endswith("\n") else text


#: Option letters (and words) that take a value, per wrapper: `sudo -u root cmd` is `cmd`, not `root`.
WRAPPER_VALUE_OPTS = {
    "sudo": frozenset({"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U", "-D", "--user", "--group",
                       "--chdir"}),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "-t"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "exec": frozenset({"-a"}),
    "flock": frozenset({"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"}),
}
#: `uv run` (and `uv` before it): the options that take a value (round 4, check-adversarial-r3.md MINOR 8).
_UV_VALUE_OPTS = frozenset({
    "--with", "--with-editable", "--with-requirements", "--python", "-p", "--project", "--directory",
    "--package", "--extra", "--group", "--only-group", "--no-group", "--env-file", "--index", "--default-index",
    "--index-url", "-i", "--extra-index-url", "--find-links", "-f", "--cache-dir", "--config-file",
    "--exclude-newer", "--refresh-package", "--reinstall-package", "--upgrade-package", "-P",
    "--index-strategy", "--keyring-provider", "--resolution", "--prerelease", "--link-mode", "--color",
    "--python-preference", "--allow-insecure-host", "--config-setting", "-C", "--no-build-package",
    "--no-binary-package", "--only-binary"})
WRAPPER_VALUE_OPTS["uv"] = WRAPPER_VALUE_OPTS["run"] = _UV_VALUE_OPTS
#: The options that move the working folder of the command a wrapper runs.
CHDIR_OPTS = {"sudo": frozenset({"-D", "--chdir"}), "env": frozenset({"-C", "--chdir"}),
              "uv": frozenset({"--directory"}), "run": frozenset({"--directory"})}
#: The option name `scan` gives the file `flock FILE cmd` locks (and makes when it is missing).
FLOCK_FILE = "<file>"


def scan(words):
    """(index of the command word, [(wrapper, option, value) the wrappers took]) of one simple
    command. Only the front is skipped: shell keywords, VAR=value, wrappers with their options and the
    values of those options, durations, and the file of `flock FILE cmd` (taken as (flock, FLOCK_FILE,
    FILE)). `env -S` hides the command in a string: the index is len(words)."""
    i, cur, taken = 0, None, []
    while i < len(words):
        w = words[i]
        base = os.path.basename(w)
        if cur == "flock" and not w.startswith("-") and not any(t[1] == FLOCK_FILE for t in taken):
            taken.append(("flock", FLOCK_FILE, w))
            i += 1
        elif base in WRAPPERS:
            cur = base
            i += 1
        elif w in KEYWORDS or _ASSIGN.match(w) or _DURATION.match(w):
            i += 1
        elif w.startswith("-") and i > 0:
            vals = WRAPPER_VALUE_OPTS.get(cur, ())
            if w.startswith("--"):
                key, eq, val = w.partition("=")
                if key in vals:
                    if cur == "env" and key == "--split-string":
                        return len(words), taken
                    if eq:
                        taken.append((cur, key, val))
                        i += 1
                    else:
                        if i + 1 < len(words):
                            taken.append((cur, key, words[i + 1]))
                        i += 2
                    continue
                i += 1
                continue
            for j in range(1, len(w)):                       # -abc: the first letter that takes a value
                opt = "-" + w[j]
                if opt in vals:
                    if cur == "env" and opt == "-S":
                        return len(words), taken
                    rest = w[j + 1:]
                    if rest:
                        taken.append((cur, opt, rest))
                        i += 1
                    else:
                        if i + 1 < len(words):
                            taken.append((cur, opt, words[i + 1]))
                        i += 2
                    break
            else:
                i += 1
        else:
            break
    return min(i, len(words)), taken


def command_index(words):
    """Index of the command word of one simple command: past leading shell keywords (`then`, `do`, `!`,
    `{`), VAR=value, wrappers (`timeout 300`, `env`, `nohup`, `uv run`) and their flags, the values of
    those flags (`sudo -u root`, `env -u X`, `timeout -s KILL 5`) and durations. Only the front is
    skipped: `echo then touch x` has the command word `echo`."""
    return scan(words)[0]


def command_head(words):
    """The words from the command word on. The one function both write guards read a command with."""
    return list(words[command_index(words):])


def wrapper_chdir(words):
    """The folder word a wrapper moves the command to (`env -C DIR`, `sudo -D DIR`, `--chdir`), the
    last one given, or None."""
    found = None
    for wrapper, opt, val in scan(words)[1]:
        if opt in CHDIR_OPTS.get(wrapper, ()):
            found = val
    return found


def _shell_script(words):
    """The script text of `bash -c '<script>'` (also `-lc`, `-ec`, `--norc -c`, after `timeout 60`, `env`,
    `nohup`), or None when the command is not a shell given a script."""
    i = command_index(words)
    if i >= len(words) or os.path.basename(words[i]) not in SHELLS:
        return None
    j = i + 1
    while j < len(words) and words[j].startswith(("-", "+")):
        if _SHELL_C.match(words[j]):
            return words[j + 1] if j + 1 < len(words) else None
        j += 2 if words[j] in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file") else 1
    return None


def _sep(pending):
    return "".join(pending) or ";"


def _split(command, depth=0, punct=";&|()\n", deadline=None):
    """[(separator_before, words)] for each simple command, in order. The separator is every
    punctuation token since the previous command (`&&`, `;`, `(`, `);`), so a caller can follow where a
    subshell opens and closes: a closing `)` with no command after it is a final entry with no words.
    `bash -c '<script>'` is opened up so the commands inside it count too; its first command's separator
    gets a `(` and a final entry with a `)` follows, as for `( ... )`; the variables assigned in front of
    the shell (`B=x bash -c ...`, `env B=x bash -c ...`) are in its environment, so they come first inside
    as an entry of their own. `punct` is what the lexer cuts into tokens of their own; the sandbox guard
    adds `<>` so a redirection is a word it can read. `deadline` (time.monotonic) raises TooComplex."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=punct)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    out, seg, pend = [], [], []
    for n_tok, tok in enumerate(lexer):
        if n_tok % _CLOCK_EVERY == 0:
            _check(deadline)
        if tok in SEPARATORS or set(tok) <= _PUNCT_ONLY:
            if seg:
                out.append((_sep(pend), seg))
                seg, pend = [], []
            pend.append(tok)
        else:
            seg.append(tok)
    if seg:
        out.append((_sep(pend), seg))
    elif any(")" in t for t in pend):
        out.append((_sep(pend), []))
    if depth < 2:
        opened = []
        for sep, words in out:
            script = _shell_script(words) if words else None
            if script is not None:
                inner = _split(script, depth + 1, punct, deadline)
                if inner:
                    prefix = [w for w in words[:command_index(words)] if _ASSIGN.match(w)]
                    if prefix:
                        opened.append((sep + "(", prefix))
                        opened.extend(inner)
                    else:
                        lead = inner[0][0] if inner[0][0] != ";" else ""
                        opened.append((sep + "(" + lead, inner[0][1]))
                        opened.extend(inner[1:])
                    opened.append((")", []))
                    continue
            opened.append((sep, words))
        out = opened
    return out


def _is_pytest(seg):
    """True when the command word runs pytest, past shell keywords, env assignments and wrappers such as
    `timeout 300` or `uv run`, and past interpreter flags (`python -X dev -m pytest`)."""
    seg = command_head(seg)
    if not seg:
        return False
    base = os.path.basename(seg[0])
    if base in ("pytest", "py.test"):
        return True
    if "python" not in base:
        return False
    j = 1
    while j < len(seg) and seg[j] != "-m" and seg[j].startswith("-"):
        j += 2 if seg[j] in ("-X", "-W") else 1
    return seg[j:j + 2] == ["-m", "pytest"]


def _basetemps_of(seg):
    out = []
    for i, tok in enumerate(seg):
        if tok == "--basetemp" and i + 1 < len(seg):
            out.append(seg[i + 1])
        elif tok.startswith("--basetemp="):
            out.append(tok.split("=", 1)[1])
    return out


def basetemps(command):
    """Every `--basetemp` value given to a pytest invocation in command."""
    return [b for _sep, seg in _split(strip_heredocs(command)) if _is_pytest(seg)
            for b in _basetemps_of(seg)]


def _names(words, norm):
    return any(t.rstrip("/") == norm for t in words)


def removed_after(parts, k, path):
    """True when path is removed whatever the run at parts[k] returns: a `trap '... rm ... path'`
    set before it, or an `rm ... path` after it that does not hang on `&&` (which skips the
    removal when the suite fails)."""
    norm = path.rstrip("/")
    for sep, words in parts[:k]:
        if words and words[0] == "trap" and len(words) > 1 and "rm " in words[1] \
                and norm in words[1]:
            return True
    for sep, words in parts[k + 1:]:
        if words and os.path.basename(words[0]) == "rm" and _names(words[1:], norm):
            return "&&" not in sep
    return False


def notes(command):
    """One warning per pytest --basetemp in command that breaks the rule."""
    parts = _split(strip_heredocs(command))
    out = []
    for k, (_sep, seg) in enumerate(parts):
        if not _is_pytest(seg):
            continue
        for path in _basetemps_of(seg):
            note = note_for(path, parts, k)
            if note:
                out.append(note)
    return out


def note_for(path, parts=(), k=0):
    """The warning for one basetemp value, or None when it follows the rule."""
    if not path.startswith(("/tmp/", "$")) or path.startswith(("$HOME", "${HOME")):
        return ("pytest --basetemp %s is not under /tmp: on the disk the same tests run about 100 "
                "times slower; use a private dir under /tmp and remove it after the run." % path)
    if parts and removed_after(list(parts), k, path):
        return None
    return ("pytest --basetemp %s: pytest never deletes an explicit basetemp, so it stays in "
            "RAM-backed /tmp after the run. Remove it whatever the run returns (`trap 'rm -rf %s' "
            "EXIT` first, or `; rm -rf %s` after, not `&& rm`), or leave --basetemp out when no "
            "other pytest runs at the same time. See docs/temp-and-scratch.md." % (path, path, path))


def decide(payload):
    """The PreToolUse output for one payload, or None to say nothing."""
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or "--basetemp" not in command:
        return None
    found = notes(command)
    if not found:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "additionalContext": "Alpaca temp guard: " + " ".join(found)}}


@common.fail_open(record_cost=False)
def main():
    out = decide(common.read_stdin())
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
