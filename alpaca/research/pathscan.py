"""The paths, addresses and shell commands a tool call names, shared by the verifier's breach check
and the audit (design t-089, verifier.md sections 3.2 and 10.2).

A tool call here is one entry of `launcher.tool_calls` (t-098): a dict with `tool`, `kind`
(`read`, `write`, `shell`, `web` or `other`), `input` (a shell call's full command text, heredoc
bodies included, or the tool's input mapping), `paths` (a write's target paths) and `output`.

The shell reader `parse` is small and strict. It splits a command text into simple commands on
`;`, `&&`, `||`, `|`, `&` and line breaks, keeps quoted text as one word, decodes `$'...'` and
`$"..."` quoting, expands unquoted brace lists and sequences into the words bash makes of them
(so the path scan sees `.{.,}/x` as `../x` and `./x`), reads heredoc bodies apart from the
words, and names every construct it does not follow (command substitution,
process substitution, a subshell or brace group, a background job, a heredoc whose delimiter is
not quoted, a leading environment assignment, a brace expansion too large to follow or one
that makes a redirect target ambiguous) in `flags`. A text it cannot read at all (an
unterminated quote or heredoc) comes back with `error` set. Every rule built on it is
default-deny: what the reader cannot follow counts as running something.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re

#: section 3.2: the read commands of the verifier's allow list, plus cd, pwd, echo, printf, true
#: and `:`. A How to reproduce section whose every command is one of these only reads or prints.
READ_COMMANDS = ("cat", "head", "tail", "wc", "grep", "rg", "ls", "stat", "file", "sha256sum",
                 "diff")
READ_ONLY = READ_COMMANDS + ("cd", "pwd", "echo", "printf", "true", ":")

#: section 3.2: the first line of a gate block, as architecture.md's proofs.GATE_BLOCK.
GATE_BLOCK = re.compile(r"^# alpaca-gate (\S+) side (tree|base)$")

#: constructs parse() names in `flags`; each one voids a verifier pass that uses it in its own
#: shell (section 5.3, item 4).
FLAGS = ("command-substitution", "process-substitution", "subshell", "brace-group",
         "background", "unquoted-heredoc", "assignment", "eval", "brace-expansion")

#: network clients whose arguments are read as targets (section 5.3, item 4).
NET_COMMANDS = ("curl", "wget", "nc", "ncat", "socat", "telnet", "ssh")

#: interpreters of a shell wrapper that Codex puts around a command (`bash -lc '<text>'`).
_WRAPPERS = ("bash", "sh", "/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/sh")

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: the character after a `$` that makes it an expansion the path scan cannot follow: a name or a
#: brace (`$?`, `$#` and the other special parameters name no path the scan reads)
_LIVE_NEXT = re.compile(r"[A-Za-z_{]")
_FD = re.compile(r"^[0-9]+$")

#: the most words one brace expansion may make before the scan gives up on it
MAX_BRACE_WORDS = 1024
_SEQ_NUM = re.compile(r"^([-+]?[0-9]+)\.\.([-+]?[0-9]+)(?:\.\.([-+]?[0-9]+))?$")
_SEQ_CHR = re.compile(r"^([A-Za-z])\.\.([A-Za-z])(?:\.\.([-+]?[0-9]+))?$")
_ANSI_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
                "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c(s: str, start: int) -> tuple:
    """(text, end) of a bash `$'...'` string whose body starts at `start`: the text with its
    backslash escapes decoded as bash decodes them (\\n, \\xHH, \\nnn octal, \\uHHHH,
    \\UHHHHHHHH, \\cX), cut at a NUL as bash cuts it, and the index of the closing quote
    (-1 when there is none)."""
    out = []
    j, n = start, len(s)
    while j < n:
        c = s[j]
        if c == "'":
            break
        if c != "\\" or j + 1 >= n:
            out.append(c)
            j += 1
            continue
        e = s[j + 1]
        j += 2
        if e in _ANSI_SIMPLE:
            out.append(_ANSI_SIMPLE[e])
        elif e in "01234567":
            k = j
            while k < n and k < j + 2 and s[k] in "01234567":
                k += 1
            out.append(chr(int(e + s[j:k], 8) & 0xFF))
            j = k
        elif e in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[e]
            k = j
            while k < n and k < j + width and s[k] in "0123456789abcdefABCDEF":
                k += 1
            if k == j:
                out.append("\\" + e)
            else:
                code = int(s[j:k], 16)
                out.append(chr(code) if code <= 0x10FFFF else "?")
            j = k
        elif e == "c" and j < n:
            out.append(chr(ord(s[j]) & 0x1F))
            j += 1
        else:
            out.append("\\" + e)
    else:
        j = -1
    text = "".join(out)
    return text.split("\x00", 1)[0], j


def _sequence(body: str):
    """The words of a bash sequence expression `{x..y[..step]}` (integers, or single letters
    walked through their character codes), or None when `body` is not one."""
    m = _SEQ_NUM.match(body)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        step = abs(int(m.group(3) or 1)) or 1
        if abs(b - a) // step + 1 > MAX_BRACE_WORDS:
            raise OverflowError
        width = 0
        for g in (m.group(1), m.group(2)):
            digits = g.lstrip("+-")
            if len(digits) > 1 and digits.startswith("0"):
                width = max(width, len(g))
        rng = range(a, b + 1, step) if a <= b else range(a, b - 1, -step)
        return [str(v).zfill(width) if width else str(v) for v in rng]
    m = _SEQ_CHR.match(body)
    if m:
        a, b = ord(m.group(1)), ord(m.group(2))
        step = abs(int(m.group(3) or 1)) or 1
        rng = range(a, b + 1, step) if a <= b else range(a, b - 1, -step)
        return [chr(v) for v in rng]
    return None


def _brace_expand(chars: list, bare: set, quoted: bool):
    """The words bash's brace expansion makes of one word, given its characters and the
    offsets of those read unquoted: only an unquoted `{`, `,`, `}` or sequence counts, and a
    `{` right after an unquoted `$` does not. An unquoted word that expands to nothing is
    dropped, as bash drops it. None when the expansion would make more than MAX_BRACE_WORDS
    words; the caller then names the word as one the scan cannot follow."""
    items = [(c, k in bare) for k, c in enumerate(chars)]
    out = []

    def expand(seq, start):
        i = start
        while i < len(seq):
            c, b = seq[i]
            if not (c == "{" and b) or (i > 0 and seq[i - 1] == ("$", True)):
                i += 1
                continue
            depth, commas, close = 0, [], None
            for j in range(i, len(seq)):
                cj, bj = seq[j]
                if not bj:
                    continue
                if cj == "{" and not (j > 0 and seq[j - 1] == ("$", True)):
                    depth += 1
                elif cj == "}":
                    depth -= 1
                    if depth == 0:
                        close = j
                        break
                elif cj == "," and depth == 1:
                    commas.append(j)
            if close is None:
                i += 1
                continue
            if commas:
                alts, begin = [], i + 1
                for k in commas + [close]:
                    alts.append(seq[begin:k])
                    begin = k + 1
            else:
                inner = seq[i + 1:close]
                words = _sequence("".join(ch for ch, _b in inner)) \
                    if all(bb for _c, bb in inner) else None
                if words is None:
                    i += 1
                    continue
                alts = [[(ch, False) for ch in w] for w in words]
            for alt in alts:
                expand(seq[:i] + alt + seq[close + 1:], i)
            return
        out.append("".join(ch for ch, _b in seq))
        if len(out) > MAX_BRACE_WORDS:
            raise OverflowError

    try:
        expand(items, 0)
    except (OverflowError, RecursionError):
        return None
    return [w for w in out if w or quoted]


# --------------------------------------------------------------------------- the shell reader
class _Cmd:
    __slots__ = ("argv", "redirects", "heredocs", "assign", "live", "rlive", "plain")

    def __init__(self):
        self.argv, self.redirects, self.heredocs, self.assign = [], [], [], False
        self.live, self.rlive, self.plain = [], [], []

    def as_dict(self):
        return {"argv": list(self.argv), "redirects": list(self.redirects),
                "heredocs": list(self.heredocs), "assign": self.assign,
                "live": [None if x is None else list(x) for x in self.live],
                "rlive": [None if x is None else list(x) for x in self.rlive],
                "plain": list(self.plain)}


def parse(text: str) -> dict:
    """{"commands", "heredocs", "flags", "error"} for one command text.

    `commands` lists the simple commands in order, each {"argv", "redirects" ([(op, target)]),
    "heredocs" (indexes into `heredocs`), "assign", "live", "rlive", "plain"}; `heredocs` lists
    {"delim", "quoted", "body"} with the body's lines joined by line breaks and no final one;
    `flags` is the sorted list of FLAGS met; `error` is None or why the text cannot be read.

    `live` (one entry per argv word) and `rlive` (one per redirect target) are the offsets in the
    word of each `$NAME`, `${`, `$(` and backquote the shell expands, read unquoted or inside
    double quotes; a `$` or backquote that was single-quoted or escaped is a plain character and
    has no offset. An entry is None when brace expansion made the word, whose offsets are then
    unknown. `plain` (one per argv word) is True when no character of the word was quoted, so a
    first word `for`, `do` or `done` is the shell's keyword (t-173 fix batch 3, F16)."""
    s = str(text or "")
    n = len(s)
    i = 0
    cmds, heredocs, flags = [], [], set()
    cur = _Cmd()
    word, quoted = None, False
    bare = set()                       # offsets in `word` of the characters read unquoted
    live = []                          # offsets in `word` of an expansion the shell makes
    pending_redirect = None
    pending_docs = []                  # (delim, quoted, strip_tabs, heredoc index)
    error = None

    def end_word():
        nonlocal word, quoted, pending_redirect
        if word is None:
            return
        w = "".join(word)
        words = [w]
        if any(word[k] == "{" for k in bare):
            got = _brace_expand(word, bare, quoted)
            if got is None:
                flags.add("brace-expansion")
            else:
                words = got
        # the offsets of the expansions hold for the word as read; a brace expansion that made
        # other words moves them, so those words have unknown offsets (None), unless the word held
        # no expansion at all (`{a,b}.txt`), whose words hold none either
        offsets = sorted(live) if (words == [w] or not live) else None
        if pending_redirect is not None:
            if len(words) != 1:
                # bash refuses an ambiguous redirect; the scan cannot place it either
                flags.add("brace-expansion")
                words = [w]
                offsets = sorted(live)
            cur.redirects.append((pending_redirect, words[0]))
            cur.rlive.append(offsets)
            pending_redirect = None
        else:
            if not cur.argv and not quoted and _ASSIGN.match(w):
                cur.assign = True
                flags.add("assignment")
                words = [w]
                offsets = sorted(live)
            elif not cur.argv and w in ("{", "}") and not quoted:
                flags.add("brace-group")
            plain = not quoted and not live and words == [w]
            cur.argv.extend(words)
            cur.live.extend([None if offsets is None else list(offsets) for _w in words])
            cur.plain.extend([plain for _w in words])
        word, quoted = None, False
        bare.clear()
        live.clear()

    def end_cmd():
        nonlocal cur
        end_word()
        if cur.argv or cur.redirects or cur.heredocs:
            cmds.append(cur)
        cur = _Cmd()

    def add(ch):
        """One character read outside any quote."""
        nonlocal word
        if word is None:
            word = []
        bare.add(len(word))
        word.append(ch)

    def read_bodies(start):
        """Read the pending heredoc bodies from `start`; return the index after the last one."""
        nonlocal error
        pos = start
        for delim, dq, strip, idx in pending_docs:
            lines = []
            found = False
            while pos <= n:
                end = s.find("\n", pos)
                line = s[pos:] if end < 0 else s[pos:end]
                pos = n + 1 if end < 0 else end + 1
                cmp = line.lstrip("\t") if strip else line
                if cmp == delim:
                    found = True
                    break
                lines.append(line)
                if end < 0:
                    break
            heredocs[idx]["body"] = "\n".join(lines)
            if not found:
                error = error or "unterminated heredoc %r" % delim
        pending_docs.clear()
        return min(pos, n)

    def balanced(start, opener, closer):
        """Skip a balanced (...) group from `start` (just past the opener); return the end."""
        depth, j = 1, start
        while j < n and depth:
            c = s[j]
            if c == "\\":
                j += 2
                continue
            if c == "'":
                k = s.find("'", j + 1)
                j = n if k < 0 else k + 1
                continue
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
            j += 1
        return j, depth == 0

    while i < n:
        c = s[i]
        if c in " \t\r":
            end_word()
            i += 1
            continue
        if c == "\n":
            end_cmd()
            i += 1
            if pending_docs:
                i = read_bodies(i)
            continue
        if c == "#" and word is None:
            k = s.find("\n", i)
            i = n if k < 0 else k
            continue
        if c == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                i += 2
                continue
            if i + 1 < n:
                if word is None:
                    word = []
                word.append(s[i + 1])          # an escaped character is quoted
                quoted = True
                i += 2
                continue
            i += 1
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "'":
            # bash's ANSI-C quoting: the word holds the decoded text, all of it quoted
            text, k = _ansi_c(s, i + 2)
            if k < 0:
                error = error or "unterminated $' quote"
                k = n - 1
            if word is None:
                word = []
            word.extend(text)
            quoted = True
            i = k + 1
            continue
        if c == "$" and i + 1 < n and s[i + 1] == '"':
            # a locale-translated string reads as the double-quoted text it holds
            i += 1
            continue
        if c == "'":
            k = s.find("'", i + 1)
            if k < 0:
                error = error or "unterminated single quote"
                add(s[i + 1:])
                i = n
                continue
            if word is None:
                word = []
            word.extend(s[i + 1:k])
            quoted = True
            i = k + 1
            continue
        if c == '"':
            j = i + 1
            if word is None:
                word = []
            closed = False
            while j < n:
                d = s[j]
                if d == "\\" and j + 1 < n and s[j + 1] in '"\\$`\n':
                    if s[j + 1] != "\n":
                        word.append(s[j + 1])
                    j += 2
                    continue
                if d == '"':
                    closed = True
                    break
                if d == "`" or (d == "$" and j + 1 < n and s[j + 1] == "("):
                    flags.add("command-substitution")
                    live.append(len(word))
                elif d == "$" and j + 1 < n and _LIVE_NEXT.match(s[j + 1]):
                    live.append(len(word))
                word.append(d)
                j += 1
            if not closed:
                error = error or "unterminated double quote"
            quoted = True
            i = j + 1
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            flags.add("command-substitution")
            j, ok = balanced(i + 2, "(", ")")
            if not ok:
                error = error or "unterminated command substitution"
            if word is None:
                word = []
            live.append(len(word))
            word.extend(s[i:j])
            i = j
            continue
        if c == "`":
            flags.add("command-substitution")
            k = s.find("`", i + 1)
            if k < 0:
                error = error or "unterminated backquote"
                k = n - 1
            if word is None:
                word = []
            live.append(len(word))
            word.extend(s[i:k + 1])
            i = k + 1
            continue
        if c in "<>" and i + 1 < n and s[i + 1] == "(" and word is None:
            flags.add("process-substitution")
            j, ok = balanced(i + 2, "(", ")")
            if not ok:
                error = error or "unterminated process substitution"
            if word is None:
                word = []
            live.append(len(word))
            word.extend(s[i:j])
            i = j
            continue
        if c == ";":
            end_cmd()
            i += 1
            continue
        if c == "&":
            if i + 1 < n and s[i + 1] == "&":
                end_cmd()
                i += 2
                continue
            if i + 1 < n and s[i + 1] == ">":
                end_word()
                op = "&>>" if s[i + 2:i + 3] == ">" else "&>"
                pending_redirect = op
                i += len(op)
                continue
            flags.add("background")
            end_cmd()
            i += 1
            continue
        if c == "|":
            end_cmd()
            i += 2 if (i + 1 < n and s[i + 1] in "|&") else 1
            continue
        if c in "()":
            flags.add("subshell")
            end_cmd()
            i += 1
            continue
        if c in "<>":
            fd = None
            if word is not None and not quoted and _FD.match("".join(word)):
                fd = "".join(word)
                word, quoted = None, False
                bare.clear()
                live.clear()
            else:
                end_word()
            if s.startswith("<<<", i):
                op = "<<<"
            elif s.startswith("<<-", i):
                op = "<<-"
            elif s.startswith("<<", i):
                op = "<<"
            elif s.startswith(">>", i):
                op = ">>"
            elif s.startswith(">&", i) or s.startswith("<&", i):
                op = s[i:i + 2]
            elif s.startswith(">|", i) or s.startswith("<>", i):
                op = s[i:i + 2]
            else:
                op = c
            i += len(op)
            if op in ("<<", "<<-"):
                while i < n and s[i] in " \t":
                    i += 1
                j = i
                dq = False
                delim = []
                while j < n and s[j] not in " \t\n;&|<>()":
                    ch = s[j]
                    if ch in "'\"":
                        k = s.find(ch, j + 1)
                        if k < 0:
                            error = error or "unterminated heredoc delimiter"
                            k = n
                        delim.extend(s[j + 1:k])
                        dq = True
                        j = k + 1
                        continue
                    if ch == "\\":
                        dq = True
                        if j + 1 < n:
                            delim.append(s[j + 1])
                        j += 2
                        continue
                    delim.append(ch)
                    j += 1
                i = j
                name = "".join(delim)
                if not name:
                    error = error or "a heredoc without a delimiter"
                    continue
                if not dq:
                    flags.add("unquoted-heredoc")
                heredocs.append({"delim": name, "quoted": dq, "body": ""})
                cur.heredocs.append(len(heredocs) - 1)
                pending_docs.append((name, dq, op == "<<-", len(heredocs) - 1))
                continue
            if op in (">&", "<&"):
                j = i
                while j < n and s[j] not in " \t\n;&|<>()":
                    j += 1
                target = s[i:j]
                if _FD.match(target) or target == "-":
                    cur.redirects.append(((fd or "") + op, "&" + target))
                    i = j
                    continue
                pending_redirect = (fd or "") + op
                continue
            pending_redirect = (fd or "") + op
            continue
        if c == "$" and i + 1 < n and _LIVE_NEXT.match(s[i + 1]):
            live.append(len(word) if word is not None else 0)
        add(c)
        i += 1
    end_cmd()
    if pending_docs:
        read_bodies(n)
    if pending_redirect is not None:
        error = error or "a redirect without a target"
    for cmd in cmds:
        if cmd.argv and cmd.argv[0] in ("eval", "exec", "source", "."):
            flags.add("eval")
    return {"commands": [c.as_dict() for c in cmds], "heredocs": heredocs,
            "flags": sorted(flags), "error": error}


def unwrap(parsed: dict) -> dict:
    """A Codex `bash -lc '<text>'` call read as the text it runs: a parse whose one command is a
    shell wrapper with `-c` or `-lc` and one script argument is replaced by the parse of that
    script. Anything else comes back unchanged."""
    cmds = parsed.get("commands") or []
    if len(cmds) == 1 and not cmds[0]["redirects"] and not cmds[0]["heredocs"]:
        argv = cmds[0]["argv"]
        if len(argv) == 3 and argv[0] in _WRAPPERS and argv[1] in ("-c", "-lc", "-lic", "-ic"):
            return unwrap(parse(argv[2]))
    return parsed


def _literal_script(parsed: dict, cmd: dict) -> bool:
    """True when the script word of a wrapper call reaches the inner shell as written: the outer
    parse has no flag and no live position (a `$` expansion, a backquote or a `$(`) in any word.
    Inside outer double quotes the outer shell expands those before the inner shell reads the
    script, so a wrapper such as `bash -c "cd T && echo '$(id)'"` runs `id` although the inner
    text shows it in single quotes (section 19.9.1 unwraps the single-quoted Codex form only)."""
    if parsed.get("flags") or parsed.get("error"):
        return False
    return not any(x for x in cmd.get("live") or [])


def unwrap_literal(parsed: dict) -> dict:
    """`unwrap`, except that a wrapper whose script word holds a live position of the outer
    shell is left as it is, so the reader sees the wrapper as an off-table command with the
    outer flags (the single-quoted `bash -lc '<text>'` of Codex is unwrapped as in `unwrap`)."""
    cmds = parsed.get("commands") or []
    if len(cmds) == 1 and not cmds[0]["redirects"] and not cmds[0]["heredocs"]:
        argv = cmds[0]["argv"]
        if len(argv) == 3 and argv[0] in _WRAPPERS and argv[1] in ("-c", "-lc", "-lic", "-ic"):
            if not _literal_script(parsed, cmds[0]):
                return parsed
            return unwrap_literal(parse(argv[2]))
    return parsed


def shell_text(call: dict) -> str | None:
    """The command text of a shell call, or None for any other call."""
    if call.get("kind") != "shell":
        return None
    value = call.get("input")
    if isinstance(value, dict):
        value = value.get("command") or value.get("cmd")
        if isinstance(value, list):
            value = " ".join(str(v) for v in value)
    return value if isinstance(value, str) else None


def commands(calls: list) -> list:
    """Every simple command of every shell call, in order: {"line" (the call's 1-based
    position), "tool", "argv", "redirects", "heredocs", "flags", "error", "text"}."""
    out = []
    for pos, call in enumerate(calls or [], 1):
        text = shell_text(call)
        if text is None:
            continue
        got = unwrap(parse(text))
        for cmd in got["commands"]:
            out.append({"line": pos, "tool": call.get("tool"), "argv": cmd["argv"],
                        "redirects": cmd["redirects"],
                        "heredocs": [got["heredocs"][k] for k in cmd["heredocs"]],
                        "flags": got["flags"], "error": got["error"], "text": text})
    return out


# --------------------------------------------------------------------------- for loops
#: the most simple commands one call's for loops may unroll to before the scan gives up on them
MAX_LOOP_COMMANDS = 4096
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NAME_AT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class _LoopError(ValueError):
    pass


def _keyword(cmd: dict):
    """The shell keyword a simple command starts with (`for`, `do`, `done`): its first word, read
    with no quoting at all; else None."""
    argv = cmd.get("argv") or []
    plain = cmd.get("plain") or []
    if argv and plain and plain[0] and argv[0] in ("for", "do", "done"):
        return argv[0]
    return None


def _simple(argv, live, redirects=(), rlive=(), heredocs=(), plain=None) -> dict:
    return {"argv": list(argv), "redirects": list(redirects), "heredocs": list(heredocs),
            "assign": False, "live": [None if x is None else list(x) for x in live],
            "rlive": [None if x is None else list(x) for x in rlive],
            "plain": list(plain) if plain is not None else [False] * len(argv)}


def _split_do(cmds: list) -> list:
    """`do X ...` as two commands, `do` and `X ...` (the body's first command keeps the
    redirects and heredocs the reader put on the line)."""
    out = []
    for c in cmds:
        if _keyword(c) == "do" and len(c["argv"]) > 1:
            out.append(_simple(["do"], [[]], plain=[True]))
            out.append(dict(c, argv=c["argv"][1:], live=(c.get("live") or [None] * len(c["argv"]))[1:],
                            plain=(c.get("plain") or [False] * len(c["argv"]))[1:], assign=False))
        else:
            out.append(c)
    return out


def _put(word: str, offsets, name: str, value):
    """(word, offsets) with each expansion of `name` at a live offset (`$name`, `${name}`) put as
    `value`; the other expansions stay where they are. `offsets` None (unknown) or `value` None
    (a value the scan does not know) leave the word as it is."""
    if offsets is None or value is None or not offsets:
        return word, offsets
    out, new, last = [], [], 0
    for p in sorted(offsets):
        if p < last:
            continue
        out.append(word[last:p])
        here = len("".join(out))
        end = None
        if word.startswith("${", p):
            close = word.find("}", p + 2)
            if close > 0 and word[p + 2:close] == name:
                end = close + 1
        elif word.startswith("$", p):
            m = _NAME_AT.match(word, p + 1)
            if m and m.group(0) == name:
                end = m.end()
        if end is None:
            new.append(here)
            out.append(word[p])
            last = p + 1
            continue
        out.append(value)
        last = end
    out.append(word[last:])
    return "".join(out), new


def _loop_header(cmd: dict) -> tuple:
    """(name, values) of `for NAME in WORD ...`; a value the scan cannot know (a word with an
    expansion, or one that holds a blank, which the shell would split) is None, and `for NAME`
    with no `in` (the positional parameters) is one unknown value."""
    argv = cmd.get("argv") or []
    live = cmd.get("live") or [None] * len(argv)
    plain = cmd.get("plain") or [False] * len(argv)
    if cmd.get("redirects") or cmd.get("heredocs") or len(argv) < 2:
        raise _LoopError("a for loop without a name")
    name = argv[1]
    if not plain[1] or not _NAME.match(name):
        raise _LoopError("for %s: not a name" % name[:40])
    if len(argv) == 2:
        return name, [None]
    if argv[2] != "in" or not plain[2]:
        raise _LoopError("for %s: no `in` before its words" % name)
    values = []
    for w, lv in zip(argv[3:], live[3:]):
        values.append(None if (lv is None or lv or re.search(r"\s", w)) else w)
    return name, values


def _block(cmds: list, i: int, inner: bool, budget: list) -> tuple:
    out = []
    while i < len(cmds):
        c = cmds[i]
        k = _keyword(c)
        if k == "done":
            if not inner:
                raise _LoopError("done outside a for loop")
            return out, i
        if k == "do":
            raise _LoopError("do outside a for loop")
        if k == "for":
            name, values = _loop_header(c)
            nxt = cmds[i + 1] if i + 1 < len(cmds) else None
            if nxt is None or _keyword(nxt) != "do" or len(nxt["argv"]) != 1 or nxt["redirects"]:
                raise _LoopError("for %s: no do after its words" % name)
            body, j = _block(cmds, i + 2, True, budget)
            done = cmds[j]
            if len(done["argv"]) != 1:
                raise _LoopError("done takes no words")
            budget[0] -= len(body) * len(values)
            if budget[0] < 0:
                raise _LoopError("for loops too large to follow")
            for v in values:
                for b in body:
                    argv, live = [], []
                    for w, lv in zip(b["argv"], b.get("live") or [None] * len(b["argv"])):
                        w2, l2 = _put(w, lv, name, v)
                        argv.append(w2)
                        live.append(l2)
                    reds, rlive = [], []
                    for (op, t), rl in zip(b["redirects"], b.get("rlive") or [None] * len(b["redirects"])):
                        t2, r2 = _put(str(t), rl, name, v) if not str(t).startswith("&") else (t, rl)
                        reds.append((op, t2))
                        rlive.append(r2)
                    plain = [p and w2 == w for p, w, w2 in zip(
                        b.get("plain") or [False] * len(argv), b["argv"], argv)]
                    out.append(dict(b, argv=argv, live=live, redirects=reds, rlive=rlive,
                                    plain=plain))
            if done["redirects"] or done["heredocs"]:
                # the loop's own redirects (`done > out/x`) are the loop's writes and reads
                out.append(_simple([], [], done["redirects"],
                                   done.get("rlive") or [None] * len(done["redirects"]),
                                   done["heredocs"]))
            i = j + 1
            continue
        out.append(c)
        i += 1
    if inner:
        raise _LoopError("a for loop without done")
    return out, i


def unroll(parsed: dict) -> dict:
    """parse()'s result with each `for NAME in WORD ...; do BODY; done` read as BODY once for each
    WORD, the loop's name put in each `$NAME` and `${NAME}` the shell expands (t-173 fix batch 3,
    F16): `for f in a b; do cat "$f"; done` reads as `cat a` and `cat b`, so the path scan places
    `a` and `b` and the allow list judges the body's own commands. A loop nests; its `done` keeps
    the loop's redirects (`done > out/list`), and a value the scan cannot know (a word with an
    expansion or a blank, `for NAME` alone) leaves `$NAME` as an expansion it cannot place. A
    `do` or `done` outside a loop, a loop without its `do` or `done`, a name that is not one and a
    loop over more than MAX_LOOP_COMMANDS commands set `error` (the commands are left as they
    were), so the default-deny rules refuse what the reader cannot follow. Other compound
    commands (`while`, `if`, `case`) are not read: their words stay commands the allow list
    refuses."""
    cmds = parsed.get("commands") or []
    if not any(_keyword(c) for c in cmds):
        return parsed
    try:
        out, _i = _block(_split_do(cmds), 0, False, [MAX_LOOP_COMMANDS])
    except _LoopError as exc:
        return dict(parsed, error=parsed.get("error") or "unreadable loop: %s" % exc)
    return dict(parsed, commands=out)


#: the commands whose operands that read `NAME=value` are assignments, not paths (t-351 F48)
_ASSIGN_OPERANDS = ("export", "declare", "typeset", "readonly", "local")


def assign_start(argv, k: int) -> int | None:
    """The offset of the value in word `k` of `argv` when it is an assignment word, else None: a
    leading `NAME=value` word (every word before it is one too, as `FOO=1 BAR=2 cmd`), or an
    operand of export, declare, typeset, readonly or local. The path scan reads such a word by
    its value, as it does `--opt=value`: `S=/abs` names `/abs`, not `<folder>/S=/abs`. Any other
    word that holds an `=` is a plain word (t-351 F48, in-session-runtime.md section 23)."""
    if not 0 <= k < len(argv):
        return None
    m = _ASSIGN.match(str(argv[k]))
    if not m:
        return None
    if all(_ASSIGN.match(str(a)) for a in argv[:k]):
        return m.end()
    if os.path.basename(str(argv[0])) in _ASSIGN_OPERANDS:
        return m.end()
    return None


def _live_from(word: str, offsets, start: int = 0) -> bool:
    """True when `word[start:]` holds an expansion the scan cannot follow: one of its live offsets
    (parse's `live`), or, with no offsets known, the text of one (_EXPANSION)."""
    if offsets is None:
        return bool(_EXPANSION.search(word[start:]))
    return any(p >= start for p in offsets)


# --------------------------------------------------------------------------- paths
#: a word that expands a shell variable, or a backquote: the scan cannot know what it names
_EXPANSION = re.compile(r"\$[A-Za-z_{]|`")
#: a glob or a brace list in a path component: it may name any entry there
_PATTERN = re.compile(r"[*?\[]|\{[^{}]*(?:,|\.\.)[^{}]*\}")
_BRACES = re.compile(r"\{[^{}]*\}")
#: the folders that reach other folders through a process: /proc/self/cwd, /proc/<pid>/root,
#: /proc/thread-self/fd/<n>, /dev/fd/<n>. The kernel resolves them where the lexical scan
#: cannot, so a path that passes through one is never placed.
_PROCESS_DIRS = (("proc",), ("dev", "fd"))
#: what marks an option word as holding a path attached to its letters (`-f../x`, `-m/abs`)
_ATTACHED = re.compile(r"/|\.\.|~")
#: the options `cd` takes before its directory
_CD_OPTION = re.compile(r"^-[LPe@]+$")


def _looks_like_path(token: str) -> bool:
    if not token or token.startswith("-") and "=" not in token:
        return False
    if "://" in token:
        return False
    return "/" in token or token in (".", "..") or token.startswith("~")


def _component_matches(name: str, component: str) -> bool:
    """True when one path component as written (a name, a glob or a brace list) can name
    `name`."""
    if component == name:
        return True
    if not _PATTERN.search(component):
        return False
    return fnmatch.fnmatchcase(name, _BRACES.sub("*", component))


def _through_process(path: str) -> bool:
    """True when the absolute `path`, walked one component at a time as written, passes through
    /proc or /dev/fd, where a name like `cwd` or `3` leads to a folder the lexical walk does not
    see (`/proc/self/cwd/../../x` folds to `/x`, while the kernel reads it from the process's
    working folder)."""
    stack = []
    for part in str(path).split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if stack:
                stack.pop()
            continue
        stack.append(part)
        for magic in _PROCESS_DIRS:
            if len(stack) == len(magic) and all(
                    _component_matches(name, comp) for name, comp in zip(magic, stack)):
                return True
    return False


def _dot_glob(value: str) -> bool:
    """True when a component of the path word `value` is a pattern that starts with a literal
    `.` and can name `..` (`.?`, `.*`, `.[.]`): a shell without bash 5.2's globskipdots expands
    it to the parent folder, which the lexical walk cannot see. A pattern that does not start
    with `.` never names a dot entry, so `logs/*.txt` stays placed."""
    return any(c.startswith(".") and c not in (".", "..") and _PATTERN.search(c)
               and _component_matches("..", c) for c in value.split("/"))


def _resolve(value: str, here, prev) -> tuple:
    """(path, placed): the path word `value` made absolute against `here` (normpath, no link
    followed). `placed` is False, and `path` is the word as written, when the scan cannot know
    where it points: a variable expansion, a `~` form it cannot expand, a relative path while
    the shell's folder is unknown (`here` None), a path through /proc or /dev/fd, or a glob
    component that can name `..`. parse() has already expanded brace lists and sequences and
    decoded `$'...'` quoting, so each word here is one the shell hands the command."""
    raw, placed = _place(value, here, prev)
    return (os.path.normpath(raw), True) if placed else (raw, False)


def _real(raw: str) -> str:
    """The path the kernel reaches for the absolute path `raw` as written, taken now: every
    link on the host followed, a `..` after a link applied to the link's target (realpath)."""
    try:
        return os.path.realpath(raw)
    except (OSError, ValueError):
        return raw


def _place(value: str, here, prev, *, literal: bool = False) -> tuple:
    """(raw, placed): as _resolve, with `raw` the absolute path as written (no normpath), so
    _real can follow a link before a `..` the way the kernel does. `literal` says the reader
    knows the word holds no expansion (parse's `live`), so a quoted `$` or backquote in it is a
    plain character of the path."""
    if (not literal and _EXPANSION.search(value)) or _dot_glob(value):
        return value, False
    word = value
    if value.startswith("~"):
        head, _sep, rest = value.partition("/")
        if head == "~+":
            base = here
        elif head == "~-":
            base = prev
        else:
            base = os.path.expanduser(head)
            if base == head:
                base = None
        if base is None:
            return word, False
        value = os.path.join(base, rest) if rest else base
    if not os.path.isabs(value):
        if here is None:
            return word, False
        value = os.path.join(here, value)
    if _through_process(value):
        return word, False
    return value, True


def _cd_glob(word: str, here, prev) -> list | None:
    """The folders on disk a cd word that is a glob (`t*s`, `te?ts`, `[t]ests`) expands to, sorted
    as the shell hands them to cd; None when the word holds no glob character or cannot be placed.
    The shell expands the word before cd runs, so the scan takes the first match, as cd does, and
    an empty list leaves the folder unknown: the glob matched nothing on disk now, so the scan
    cannot tell where it led when the command ran. An extglob word (`@(tests)`, `+(x|y)`, which
    bash reads once `shopt -s extglob` ran) is left unknown too: Python's glob cannot expand it."""
    if not re.search(r"[*?\[]|[@!+]\(", str(word)):
        return None
    raw, placed = _place(str(word), here, prev)
    if not placed:
        return None
    if re.search(r"[@!+*?]\(", str(word)):
        return []
    try:
        return sorted(glob.glob(raw))
    except (OSError, ValueError, re.error):
        return []


def _cd(argv: list, here, prev) -> tuple:
    """(here, prev, target, placed) after one `cd` command, as a shell moves: a bare `cd` and
    `cd ~` go to HOME, `cd -` to the previous folder, and the options -L, -P, -e and -@ and a
    `--` are skipped. A glob word goes to its first match on disk (_cd_glob). A target the scan
    cannot place, or a glob that matches nothing, leaves the folder unknown (here None)."""
    args = list(argv[1:])
    while args and _CD_OPTION.match(args[0]):
        args.pop(0)
    if args and args[0] == "--":
        args.pop(0)
    word = args[0] if args else "~"
    found = _cd_glob(word, here, prev) if word != "-" else None
    if found is not None:
        if not found:
            return None, here, word, False
        return os.path.normpath(found[0]), here, os.path.normpath(found[0]), True
    if word == "-":
        target, placed = (prev, True) if prev is not None else ("-", False)
    else:
        target, placed = _resolve(word, here, prev)
    return (target if placed else None), here, target, placed


def _walk(command: str, here, prev, *, exact: bool = False) -> tuple:
    """([(path, placed, raw)], here, prev): the paths one command text names (`raw` is the
    placed path as written, before normpath, for _real), in order, with the
    shell's folder followed through every `cd` in it. A word counts when it holds a `/`, is `.`
    or `..`, starts with `~` or is a glob that can name `..`; an `--opt=value` counts for its
    value; redirect targets count too; a `cd` names the folder it moves to (HOME for a bare
    `cd`). A word with a variable expansion, and an option word that holds a `/`, a `..` or a
    `~` before any `=` (a path attached to an option letter, `-f../x`), count as paths the scan
    cannot place. The arguments handed to the request
    client (`bin/rerun`) are left out: that text runs in a rerun jail, not here.

    `exact` (the verifier's breach scan, t-173 fix batch 3, F16) reads a `for` loop over written
    words as its body once for each word (unroll), so `cat "$f"` in the loop names the paths the
    loop gives `f`, and tells a quoted `$` or backquote, a plain character, from an expansion by
    parse's `live` offsets; without it every `$NAME` and backquote is an expansion, as before.
    It also places every operand of a read (`operands`: a file word of cat, head, grep, rg, ls
    and the rest, an option value that names a file to read, and the `.` that `rg`, a recursive
    `grep` and `ls` read when they name no file) and the target of every redirect, in the folder
    the shell is in at that command, whether or not the word holds a `/` (in-session-runtime.md
    section 18.1b): a subagent's call starts in the project root, where `cat CLAUDE.md` and
    `rg x .alpaca` name the record's store with no `/` in them."""
    out = []
    got = unwrap_literal(parse(command))
    if exact:
        rolled = unroll(got)
        if not (rolled.get("error") and not got.get("error")):
            got = rolled
    for ci, cmd in enumerate(got["commands"]):
        argv = list(cmd["argv"])
        if exact and argv and (argv[0] not in VERIFIER_TABLE or (argv[0] == "cd" and ci > 0)):
            here, prev = None, None          # W2 (19.9.3): the folder is unknown from here on
        live = list(cmd.get("live") or [None] * len(argv)) if exact else [None] * len(argv)
        if rerun_args(argv) is not None:
            argv, live = argv[:2], live[:2]
        is_cd = bool(argv) and argv[0] == "cd"
        rlive = list(cmd.get("rlive") or [None] * len(cmd["redirects"])) if exact else \
            [None] * len(cmd["redirects"])
        # with `exact`, the words of a command that opens no file it is given (echo, printf, ...)
        # name no path: `echo "--- $f"` in a loop prints the path and reaches nothing
        quiet = exact and bool(argv) and os.path.basename(argv[0]) in NAMES_NOTHING \
            and argv[0] == os.path.basename(argv[0])
        forced, implicit = {}, False
        if exact and argv and not is_cd and not quiet:
            for idx, start in operands(argv):
                if idx < 0:
                    implicit = True
                else:
                    forced[idx] = min(start, forced.get(idx, start))
        items = [] if (is_cd or quiet) else [(w, wl, k, None) for k, (w, wl) in enumerate(
            zip(argv, live))]
        items += [(t, rl, None, op) for (op, t), rl in zip(cmd["redirects"], rlive)
                  if not str(t).startswith("&")]
        for w, wl, k, redirect in items:
            if k is not None and k in forced:
                start = forced[k]
                value = w[start:]
                if w.startswith("-") and _ATTACHED.search(w.lstrip("-").partition("=")[0]):
                    # a path glued to its option letter: the word is one the scan cannot place
                    out.append((w, False, w))
                elif _live_from(w, wl, start):
                    out.append((value, False, value))
                else:
                    raw, placed = _place(value, here, prev, literal=wl is not None)
                    out.append((os.path.normpath(raw) if placed else raw, placed, raw))
                continue
            if exact and redirect is not None:
                if "<<<" in redirect:
                    continue                # the word of a here-string is input text
                if _live_from(w, wl, 0):
                    out.append((w, False, w))
                else:
                    raw, placed = _place(w, here, prev, literal=wl is not None)
                    out.append((os.path.normpath(raw) if placed else raw, placed, raw))
                continue
            value, start = w, 0
            if w.startswith("-"):
                key, eq, rest = w.lstrip("-").partition("=")
                if _ATTACHED.search(key):
                    # a path glued to an option (`file -m../x`, `grep -f/abs`, `-fout/=/../x`):
                    # the scan does not guess where the option letters end, so the word is a
                    # path it cannot place (check round 4, adversarial B1)
                    out.append((w, False, w))
                    continue
                value = rest if eq else w
                start = len(w) - len(value)
            elif k is not None and not exact:
                # a `NAME=value` assignment word is read by its value, as `--opt=value` is
                at = assign_start(argv, k)
                if at is not None:
                    value, start = w[at:], at
                    if ":" in value and "://" not in value:
                        # U10 (t-354, section 27.1): a `:` list names each of its parts, so
                        # `PYTHONPATH=<scratch>:<champion>` names two folders, not one path
                        for part in value.split(":"):
                            if _EXPANSION.search(part):
                                out.append((part, False, part))
                            elif part and (_looks_like_path(part) or _dot_glob(part)):
                                raw, placed = _place(part, here, prev)
                                out.append((os.path.normpath(raw) if placed else raw, placed,
                                            raw))
                        continue
            if _live_from(w, wl, start):
                out.append((value, False, value))
            elif _looks_like_path(value) or _dot_glob(value):
                raw, placed = _place(value, here, prev, literal=wl is not None)
                out.append((os.path.normpath(raw) if placed else raw, placed, raw))
        if implicit:
            raw, placed = _place(".", here, prev, literal=True)
            out.append((os.path.normpath(raw) if placed else raw, placed, raw))
        if is_cd:
            here, prev, target, placed = _cd(argv, here, prev)
            out.append((target, placed, target))
    return out, here, prev


# --------------------------------------------------------------------------- read operands
#: each program's options that take a value (in-session-runtime.md section 18.1b): {program:
#: (short letters, long names)}, read from the `--help` text of the versions on this host (ripgrep
#: 14.1.1, ugrep 7.8.4 as `grep`, uutils coreutils 0.8.0, GNU diffutils 3.12, file 5.46; the
#: texts are saved under alpaca/tests/fixtures/research/help/ and a test checks every entry
#: against them). An option the table does not know takes no value, so the word after it counts
#: as an operand and is placed: the safe side. An entry for an option that in fact takes no value
#: would hide the operand after it, so an option whose value is optional (`--color[=WHEN]`) is
#: left out: it takes a value only after `=`, which is one word.
OPTION_VALUES = {
    "cat": ("", ()),
    "head": ("nc", ("bytes", "lines")),
    "tail": ("nc", ("bytes", "lines")),
    "wc": ("", ("files0-from", "total")),
    "grep": ("ABCDdefgJKMmNOt",
             ("after-context", "before-context", "context", "binary-files", "devices",
              "directories", "regexp", "file", "glob", "iglob", "include", "include-dir",
              "exclude", "exclude-dir", "include-from", "exclude-from", "max-count",
              "file-type", "file-extension", "file-magic", "label", "jobs", "encoding", "format",
              "replace", "neg-regexp", "colors", "colours", "max-files", "max-size", "min-size",
              "delay")),
    "rg": ("ABCEMTdefgjmrt",
           ("regexp", "file", "pre", "pre-glob", "dfa-size-limit", "encoding", "engine",
            "max-count", "regex-size-limit", "threads", "glob", "iglob", "ignore-file",
            "max-depth", "max-filesize", "type", "type-not", "type-add", "type-clear", "color",
            "colors", "context-separator", "field-context-separator", "field-match-separator",
            "hostname-bin", "hyperlink-format", "max-columns", "path-separator", "replace",
            "sort", "sortr", "generate", "after-context", "before-context", "context")),
    "ls": ("ITw", ("ignore", "hide", "tabsize", "width", "format", "time", "sort", "block-size",
                   "time-style", "quoting-style", "indicator-style")),
    "stat": ("c", ("format", "printf")),
    "file": ("eFmf", ("exclude", "exclude-quiet", "files-from", "magic-file", "separator")),
    "sha256sum": ("", ()),
    "diff": ("CUWFxXSID", ("width", "show-function-line", "label", "tabsize", "exclude",
                           "exclude-from", "starting-file", "from-file", "to-file",
                           "ignore-matching-lines", "ifdef", "horizon-lines", "palette",
                           "line-format")),
    "cmp": ("in", ("ignore-initial", "bytes")),
    "sort": ("ktS", ("key", "field-separator", "buffer-size", "sort", "parallel", "batch-size",
                     "random-source")),
    "uniq": ("fsw", ("skip-fields", "skip-chars", "check-chars")),
    "cut": ("bcdf", ("bytes", "characters", "delimiter", "fields", "output-delimiter")),
    "nl": ("bdfhilnsvw", ("body-numbering", "section-delimiter", "footer-numbering",
                          "header-numbering", "line-increment", "join-blank-lines",
                          "number-format", "number-separator", "starting-line-number",
                          "number-width")),
    "tac": ("s", ("separator",)),
    "comm": ("", ("output-delimiter",)),
    "paste": ("d", ("delimiters",)),
}
#: the options of OPTION_VALUES whose value names a file the program reads: {program: (short
#: letters, long names)}. Such a value is a path, placed like an operand.
FILE_VALUES = {
    "grep": ("f", ("file", "include-from", "exclude-from")),
    "rg": ("f", ("file", "ignore-file")),
    "file": ("mf", ("magic-file", "files-from")),
    "diff": ("XS", ("exclude-from", "starting-file", "from-file", "to-file")),
    "sort": ("", ("random-source", "files0-from")),
    "wc": ("", ("files0-from",)),
}
#: the programs whose words after the options are files they read
_OPERAND_COMMANDS = ("cat", "head", "tail", "wc", "grep", "rg", "ls", "stat", "file", "sha256sum",
                     "diff", "cmp", "sed", "sort", "uniq", "cut", "nl", "tac", "comm", "paste")
#: the grammar of `test` and `[` (bash 5.x `help test`), a closed set: the unary operators (file
#: tests, `-o OPTION`, `-v VAR`, `-z`/`-n STRING`; `-a` is both a file test and the binary and),
#: the binary operators (string, number and file comparisons, `-a` and `-o`), and `!`, `(`, `)`
_TEST_UNARY = ("-a", "-b", "-c", "-d", "-e", "-f", "-g", "-G", "-h", "-k", "-L", "-N", "-O", "-p",
               "-r", "-R", "-s", "-S", "-t", "-u", "-v", "-w", "-x", "-o", "-z", "-n")
_TEST_BINARY = ("=", "==", "!=", "<", ">", "-eq", "-ne", "-lt", "-le", "-gt", "-ge", "-nt", "-ot",
                "-ef", "-a", "-o")
_TEST_OPERATORS = frozenset(_TEST_UNARY + _TEST_BINARY + ("!", "(", ")"))


def _test_operands(argv: list) -> list:
    """T11 (t-354, section 27.1): every word of a `test` or `[` expression that is not an operator
    of the grammar (_TEST_OPERATORS) is an operand and is placed, a string operand too: the reader
    does not decide which words name files, so `test -a F`, `[ ! -r F ]`, `test -zz F` and
    `test -n F` all place F (the strict side; the closing `]` of `[` is no operand)."""
    words = list(argv)
    end = len(words)
    if command_name(words[0]) == "[" and words[-1] == "]":
        end -= 1
    return [(i, 0) for i in range(1, end) if words[i] not in _TEST_OPERATORS]


def operands(argv: list) -> list:
    """[(index into argv, start of the path in that word)]: the words of a read command that are
    paths it opens, with or without a `/` (in-session-runtime.md section 18.1b). An index of -1
    stands for the `.` the program reads when it names no file: `rg`, `grep` with `-r`, `-R`,
    `--recursive`, `--dereference-recursive`, `-d recurse` or `--directories=recurse`, and `ls`.
    The programs are those of _OPERAND_COMMANDS and `test` and `[`, by command_name. The words
    that are not operands are the options, the values of options that take one (OPTION_VALUES), the
    pattern of `grep` and `rg` (the first word that is not an option, unless `-e`, `--regexp`,
    `-f` or `--file` gives the pattern, or `rg --files` or `--type-list` asks for none) and the
    script of `sed`. A `-` alone is the input and not a path; after `--` every word is an operand.
    The value of an option that names a file to read (FILE_VALUES) is placed with its start in
    the word, so `--ignore-file=x` and `-fx` give `x`."""
    if not argv:
        return []
    name = command_name(argv[0])
    if name in ("test", "["):
        return _test_operands(argv)
    if name not in _OPERAND_COMMANDS:
        return []
    if name == "sed":
        return [(i, 0) for i in sed_parts(argv)["files"]]
    shorts, longs = OPTION_VALUES.get(name, ("", ()))
    fshorts, flongs = FILE_VALUES.get(name, ("", ()))
    out, plain = [], []
    recursive = pattern_given = no_pattern = False
    i, opts = 1, True
    while i < len(argv):
        w = argv[i]
        if opts and w == "--":
            opts = False
        elif opts and w.startswith("--"):
            nm, eq, val = w[2:].partition("=")
            if nm in longs:
                at, start = i, len(nm) + 3
                if not eq:
                    i += 1
                    at, start = i, 0
                if at < len(argv):
                    value = argv[at][start:]
                    if nm in flongs:
                        out.append((at, start))
                    if nm == "directories" and name == "grep" and value == "recurse":
                        recursive = True
                if nm in ("regexp", "file") and name in ("grep", "rg"):
                    pattern_given = True
            elif nm in ("recursive", "dereference-recursive") and name == "grep":
                recursive = True
            elif nm in ("files", "type-list") and name == "rg":
                no_pattern = True
        elif opts and w.startswith("-") and len(w) > 1:
            for k in range(1, len(w)):
                ch = w[k]
                if ch in shorts:
                    at, start = i, k + 1
                    if not w[k + 1:]:
                        i += 1
                        at, start = i, 0
                    if at < len(argv):
                        value = argv[at][start:]
                        if ch in fshorts:
                            out.append((at, start))
                        if ch == "d" and name == "grep" and value == "recurse":
                            recursive = True
                    if ch in "ef" and name in ("grep", "rg"):
                        pattern_given = True
                    break
                if name == "grep" and ch in "rR":
                    recursive = True
        else:
            plain.append(i)
        i += 1
    if name in ("grep", "rg") and not pattern_given and not (name == "rg" and no_pattern):
        plain = plain[1:]
    for i in plain:
        if argv[i] != "-":
            out.append((i, 0))
    if not plain and (name in ("rg", "ls") or (name == "grep" and recursive)):
        out.append((-1, 0))
    return out


#: commands that write the paths they are given (the write part of the verifier's group check).
#: The one write table: K16 (shell_writes) reads it here, and the guard, S15 and K15 read it as
#: session_engine.WRITE_COMMANDS, the same tuple (t-354, in-session-runtime.md section 27.1)
WRITE_WORDS = ("tee", "cp", "mv", "rm", "install", "chmod", "chown", "chgrp", "ln", "touch",
               "mkdir", "rmdir", "truncate", "dd", "patch", "rsync", "unlink", "shred", "split",
               "csplit", "mkfifo", "mknod", "tar", "unzip")
#: rule U6 of section 27.1: the programs that write names the call does not show (the members of
#: an archive, the files a diff names, a counter's output names). Such a program writes into the
#: folder it runs in and into each existing folder its words name. This is a list because the
#: rule without one ("any program the reader does not model may not run where the control plane
#: is") denied 102 of 645 honest job calls (t-354 think stage, approach0/feature-cost.txt row C3)
UNSEEN_WRITERS = ("tar", "unzip", "cpio", "patch", "split", "csplit")


#: the unplaced entries shell_writes gives for a write it cannot place (in-session-runtime.md
#: section 18.2): none of them is a path, and none holds the text of the call
UNPLACED_SED_SCRIPT = "<sed script>"
UNPLACED_SED_BACKUP = "<sed backup>"
UNPLACED_OPTION_VALUE = "<option value>"
UNPLACED_PROGRAM = "<runs a program>"
UNPLACED_FILE_OUTPUT = "<file -C output>"
UNPLACED = (UNPLACED_SED_SCRIPT, UNPLACED_SED_BACKUP, UNPLACED_OPTION_VALUE, UNPLACED_PROGRAM,
            UNPLACED_FILE_OUTPUT)


def file_parts(argv: list) -> dict:
    """What a `file` call writes, read with file 5.46's options: {"compile" (`-C` or `--compile`),
    "magic" ([(index, start)] of each `-m` and `--magic-file` value: in its own word, attached to
    its letter or after `=`)}. `file -C` writes `<name of the -m file>.mgc` in the current
    folder."""
    out = {"compile": False, "magic": []}
    i, opts = 1, True
    while i < len(argv):
        w = argv[i]
        if opts and w == "--":
            opts = False
        elif opts and w.startswith("--"):
            nm, eq, _v = w[2:].partition("=")
            if nm and "compile".startswith(nm):
                out["compile"] = True
            elif len(nm) >= 2 and "magic-file".startswith(nm):
                if eq:
                    out["magic"].append((i, len(nm) + 3))
                else:
                    i += 1
                    if i < len(argv):
                        out["magic"].append((i, 0))
            elif nm and any(v.startswith(nm) for v in ("exclude", "exclude-quiet", "files-from",
                                                       "separator", "parameter")) and not eq:
                i += 1
        elif opts and w.startswith("-") and len(w) > 1:
            for k in range(1, len(w)):
                ch = w[k]
                if ch == "C":
                    out["compile"] = True
                elif ch in "eFmfP":
                    if w[k + 1:]:
                        if ch == "m":
                            out["magic"].append((i, k + 1))
                    else:
                        i += 1
                        if ch == "m" and i < len(argv):
                            out["magic"].append((i, 0))
                    break
        i += 1
    return out


def _substitution_bodies(word: str, offsets) -> list:
    """The text inside each `$(...)` or backquote pair that starts at one of `offsets` (parse's
    live positions) in `word`; a pair that does not close runs to the end of the word."""
    out = []
    for off in offsets or ():
        if word[off:off + 2] == "$(":
            depth, i = 1, off + 2
            while i < len(word) and depth:
                depth += {"(": 1, ")": -1}.get(word[i], 0)
                i += 1
            out.append(word[off + 2:i - 1 if depth == 0 else i])
        elif word[off:off + 1] == "`":
            end = word.find("`", off + 1)
            out.append(word[off + 1:end if end >= 0 else len(word)])
    return out


def shell_writes(command: str, here, *, _depth: int = 0) -> list:
    """[(path, placed)]: what one shell command text may write, read as the verifier's exact scan
    reads it (a `for` loop unrolled, quoting by parse's `live`): the target of every output
    redirect other than /dev/null and a file descriptor, every path word of a command of
    WRITE_WORDS (an option value attached to its letter among them, and every word after `--`),
    the file operands of `sed -i`, the backup of each under each suffix the call gives, and the
    `w` files of a sed script it can read, the `<name>.mgc` of `file -C`, and every path word of
    a sort or uniq in a form read_form refuses as a write. A target the scan cannot place comes
    back as written with placed False; a write it cannot place at all comes back as one of the
    UNPLACED entries, never as the text of the call: a sed script it cannot read, a sed backup
    with an expansion, an option value attached to its letter that holds a `/`, a `..` or an
    expansion, a command word that runs a program the allow list does not name, an option that
    runs a program, and a `file -C` with no magic file it can place. A text it cannot read at all
    comes back as one unplaced entry (in-session-runtime.md section 18.2, Code; t-173 fix batch 3,
    F19: the group check keeps the write part of section 6.3 item 7 and leaves the allow list to
    the breach check).

    Section 19.9.3 adds three reading rules, none of which models what bash would do: W1, a
    written word that holds a character outside PLAIN_CHARS (a glob, a brace, an expansion) is a
    write the scan cannot place; W2, the folder is unknown from the first command whose command
    word is not a program of VERIFIER_TABLE and from every `cd` that is not the first command;
    W3, every word after the command word of such a command is read again as call text with the
    folder unknown, to a depth of four, and the writes it gives are writes of the call (a word
    that does not parse gives none)."""
    got = unwrap_literal(parse(command))
    if got.get("error"):
        return [(str(command)[:200], False)]
    rolled = unroll(got)
    if not rolled.get("error"):
        # a loop the reader cannot unroll (`while`, a `do` alone) is read command by command: its
        # loop variable stays an expansion, so a redirect to it is a write the scan cannot place
        got = rolled
    out, prev = [], None

    def emit(word, raw, placed):
        if placed and not _plain(word):             # W1
            out.append((str(word), False))
        else:
            out.append((os.path.normpath(raw) if placed else raw, placed))

    def put(value, wl, keyed=False):
        if _live_from(value, wl if not keyed else None):
            out.append((value, False))
            return
        raw, placed = _place(value, here, prev, literal=wl is not None)
        emit(value, raw, placed)

    for ci, cmd in enumerate(got["commands"]):
        argv = list(cmd["argv"])
        live = list(cmd.get("live") or [None] * len(argv))
        rlive = list(cmd.get("rlive") or [None] * len(cmd["redirects"]))
        if argv and (argv[0] not in VERIFIER_TABLE or (argv[0] == "cd" and ci > 0)):
            here, prev = None, None                  # W2: the folder is unknown from here on
        if _depth < 4:
            for k, w in enumerate(argv):             # a word the outer shell expands: its bodies run
                for body in _substitution_bodies(w, live[k] if k < len(live) else None):
                    out.extend(e for e in shell_writes(body, None, _depth=_depth + 1)
                               if e != (UNPLACED_PROGRAM, False))
        if argv and argv[0] not in VERIFIER_TABLE and _depth < 4:
            for w in argv[1:]:                       # W3: a word of an off-table command is text
                if not parse(w).get("error"):
                    # a program run with a path word is not a write whose target is written in
                    # the text, so the entry that says only that is left out
                    out.extend(e for e in shell_writes(w, None, _depth=_depth + 1)
                               if e != (UNPLACED_PROGRAM, False))
        for (op, target), rl in zip(cmd["redirects"], rlive):
            t = str(target)
            if ">" not in op or t.startswith("&") or t == "/dev/null":
                continue
            raw, placed = _place(t, here, prev, literal=not _live_from(t, rl))
            emit(t, raw, placed)
        if not argv:
            continue
        name = command_name(argv[0])
        if name is None:
            # a word with a `/` that command_name refuses runs a file of the caller's choosing
            out.append((UNPLACED_PROGRAM, False))
            continue
        why_list = name_list_option(argv)
        if why_list and "runs a program" in why_list:
            out.append((UNPLACED_PROGRAM, False))
        if name == "sed":
            parts = sed_parts(argv, live)
            if parts["unreadable"]:
                out.append((UNPLACED_SED_SCRIPT, False))
            if parts["runs"]:
                out.append((UNPLACED_PROGRAM, False))
            for wname in parts["wfiles"]:
                raw, placed = _place(wname, here, prev, literal=True)
                emit(wname, raw, placed)
            if parts["inplace"]:
                for fi in parts["files"]:
                    opn = argv[fi]
                    put(opn, live[fi])
                    for si, st, text in parts["suffixes"]:
                        if not text:
                            continue
                        if _live_from(argv[si], live[si], st) or _live_from(opn, live[fi]):
                            out.append((UNPLACED_SED_BACKUP, False))
                            continue
                        bk = text.replace("*", opn) if "*" in text else opn + text
                        raw, placed = _place(bk, here, prev, literal=True)
                        emit(bk, raw, placed)
        elif name == "file":
            parts = file_parts(argv)
            if parts["compile"]:
                found = False
                for mi, st in parts["magic"]:
                    if _live_from(argv[mi], live[mi], st):
                        out.append((UNPLACED_FILE_OUTPUT, False))
                        continue
                    for one in argv[mi][st:].split(":"):
                        if not one:
                            continue
                        found = True
                        raw, placed = _place(os.path.basename(one) + ".mgc", here, prev,
                                             literal=True)
                        emit(os.path.basename(one) + ".mgc", raw, placed)
                if not found and not parts["magic"]:
                    out.append((UNPLACED_FILE_OUTPUT, False))
        else:
            sort_why = read_form(argv, live) if name in ("sort", "uniq") else None
            if sort_why and "runs a program" in sort_why:
                out.append((UNPLACED_PROGRAM, False))
            if name in WRITE_WORDS or sort_why:
                after_dashes = False
                for k in range(1, len(argv)):
                    w, wl = argv[k], live[k]
                    if after_dashes:
                        put(w, wl)
                        continue
                    if w == "--":
                        after_dashes = True
                        continue
                    if w.startswith("--"):
                        if "=" not in w:
                            continue
                        put(w.partition("=")[2], None, keyed=True)
                        continue
                    if w.startswith("-") and len(w) > 1:
                        if "=" in w:
                            put(w.partition("=")[2], None, keyed=True)
                            continue
                        rest = w[2:]
                        if not rest:
                            continue
                        # a short option that takes a value can carry it in its own word, and the
                        # scan cannot tell a run of flags from a letter and its value without the
                        # program's option table (in-session-runtime.md section 18.2)
                        if "/" in rest or ".." in rest or _live_from(w, wl, 2):
                            out.append((UNPLACED_OPTION_VALUE, False))
                        else:
                            raw, placed = _place(rest, here, prev, literal=True)
                            emit(rest, raw, placed)
                        continue
                    if w == "-":
                        continue
                    keyed = name == "dd" and "=" in w
                    put(w.partition("=")[2] if keyed else w, None if keyed else wl, keyed=keyed)
        if argv[0] == "cd":
            here, prev, _t, _p = _cd(argv, here, prev)
    return out


def _plain(word) -> bool:
    """True when every character of `word` is one of PLAIN_CHARS (19.3.2) or a path separator."""
    return all(ch in PLAIN_CHARS for ch in str(word))


def paths_in(command: str, cwd: str) -> list:
    """The paths one command text names, resolved against `cwd` and every `cd` in the text
    (normpath, no link is followed: the paths are read inside a jail). See `_walk` for what
    counts. A path the scan cannot place (a variable expansion, a path through /proc or /dev/fd,
    a relative path after a `cd` it cannot follow) comes back as written."""
    return [p for p, _placed, _raw in _walk(command, cwd, None)[0]]


def _under(path: str, prefix: str) -> bool:
    p, q = os.path.normpath(path), os.path.normpath(prefix)
    return p == q or p.startswith(q.rstrip(os.sep) + os.sep)


def _overlaps(path: str, prefix: str) -> bool:
    """True when `path` lies under `prefix` or holds it (a recursive read of an ancestor
    reaches it), a glob or brace list in a component of `path` matching any name there."""
    pc = [c for c in os.path.normpath(path).split(os.sep) if c]
    qc = [c for c in os.path.normpath(prefix).split(os.sep) if c]
    return all(_component_matches(q, p) for p, q in zip(pc, qc))


def _tool_paths(call: dict, cwd) -> list:
    """(path, access, placed, raw) tuples a read or write tool's own path fields and a write's
    target paths name (`raw` as in _walk)."""
    out = []
    kind = call.get("kind")
    value = call.get("input")
    fields = []
    if isinstance(value, dict):
        for key in ("file_path", "path", "notebook_path", "filePath"):
            if isinstance(value.get(key), str):
                fields.append(value[key])
    for p in call.get("paths") or ():
        if isinstance(p, str):
            fields.append(p)
    access = "write" if kind == "write" else "read"
    for p in fields:
        joined = p if os.path.isabs(p) else (None if cwd is None else os.path.join(cwd, p))
        if joined is None or _through_process(joined):
            out.append((p, access, False, p))
        else:
            out.append((os.path.normpath(joined), access, True, joined))
    return out


def _named(path: str, allowed: tuple, ignore_prefixes: tuple, watched, exact_allowed: tuple = ()) -> bool:
    if exact_allowed and os.path.normpath(path) in exact_allowed:
        return False
    if any(_under(path, a) for a in allowed):
        return False
    if any(_under(path, p) for p in ignore_prefixes):
        return False
    if watched is not None and not any(_overlaps(path, w) for w in watched if w):
        return False
    return True


def _with_real(prefixes) -> tuple:
    """Each prefix and its realpath, so a real path is compared against real prefixes."""
    out = []
    for p in prefixes or ():
        if p:
            out.append(p)
            real = _real(p)
            if real != p:
                out.append(real)
    return tuple(out)


def outside(calls: list, *, cwd: str, allowed: list, ignore_prefixes: tuple,
            watched: tuple | None = None, reset_cwd: str | None = None,
            exact: bool = False, exact_allowed: tuple = ()) -> list:
    """[{"line", "tool", "path", "access"}]: every path a call names that lies under none of
    `allowed` and none of `ignore_prefixes` and, when `watched` is given, under or above one of
    `watched` (a recursive read of an ancestor reaches it; a glob component matches any name).
    The verifier's driver passes its try folder as `allowed` and the project root, the
    operator's home and the other slot's launch home as `watched`, so a path under the project
    root outside the try folder, under the operator's home or under the other slot's launch
    home is named (section 5.3, item 4). A path the scan cannot place is always named: a word
    with a variable expansion, a path attached to an option letter (`-f../x`), a path through
    /proc or /dev/fd, and a relative path after a `cd` it cannot follow. A shell `cd` moves the
    folder for the later commands of the same call and of later calls, as a persistent shell
    does, and names the folder it moves to.

    A placed path is tested twice: as written (normpath) and as the kernel reaches it now
    (realpath of the path as written, every link followed), so a link inside the try folder
    that points at another slot's folder is named although the path is lexically inside.

    `reset_cwd` is for a session launch (in-session-runtime.md section 7): a subagent's Bash call
    starts in the driving session's folder and the shell is not kept between calls, so every call
    starts again in `reset_cwd` and a `cd` moves only inside one call. A call that forgets its
    `cd` then reads as a path under the project root, outside the allowed folders.

    `exact` reads `for` loops, quoting and read operands as _walk does with it (the verifier's
    breach scan). `exact_allowed` lists files that are allowed as exactly that file, never as a
    folder prefix: the persisted outputs of the agent's own tool results (in-session-runtime.md
    section 18.1a), compared for equality as written and as the kernel reaches them."""
    out = []
    here, prev = cwd, None
    lex = (tuple(allowed or ()), tuple(ignore_prefixes or ()),
           None if watched is None else tuple(watched),
           tuple(os.path.normpath(e) for e in exact_allowed or () if e))
    real = (_with_real(allowed), _with_real(ignore_prefixes),
            None if watched is None else _with_real(watched),
            tuple(os.path.normpath(r) for e in exact_allowed or () if e
                  for r in {e, _real(e)}))
    for pos, call in enumerate(calls or [], 1):
        if call.get("kind") == "denied":
            continue                    # a call the guard refused never ran
        if reset_cwd is not None:
            here, prev = reset_cwd, None
        if call.get("kind") == "shell":
            found, here, prev = _walk(shell_text(call) or "", here, prev, exact=exact)
            quads = [(p, "shell", placed, raw) for p, placed, raw in found]
        else:
            quads = _tool_paths(call, here)
        for path, access, placed, raw in quads:
            if placed and not _named(path, *lex) and not _named(_real(raw), *real):
                continue
            out.append({"line": pos, "tool": call.get("tool"), "path": path, "access": access})
    return out


# --------------------------------------------------------------------------- addresses
_LOOPBACK = re.compile(r"^(127(?:\.[0-9]{1,3}){3}|localhost|::1|\[::1\]|0\.0\.0\.0)$", re.I)
_URL = re.compile(r"[a-z][a-z0-9+.-]*://(\[[^\]]+\]|[^/:?#\s'\"]+)(?::([0-9]+))?", re.I)
_HOSTPORT = re.compile(r"(?<![A-Za-z0-9_.-])(\[[0-9a-f:]+\]|[A-Za-z0-9_.-]+):([0-9]{1,5})\b", re.I)
_DEVTCP = re.compile(r"/dev/(?:tcp|udp)/([^/\s]+)/([0-9]+)")


def _is_host(name: str, host_names: tuple) -> bool:
    n = str(name or "").strip("[]").lower()
    if _LOOPBACK.match(n) or _LOOPBACK.match("[%s]" % n):
        return True
    return n in {h.lower() for h in host_names or () if h}


def addresses(calls: list, *, host_names: tuple, ports: tuple, skip_rerun: bool = True) -> list:
    """[{"line", "tool", "target"}]: every loopback or host address, and every port of `ports`,
    that a call names as a network target: in a URL, as `<address>:<port>`, in
    `/dev/tcp/<address>/<port>`, or as an argument of a network client (NET_COMMANDS). With
    `skip_rerun`, the text a call hands to the request client is left out: it runs in a rerun
    jail with no network at all (section 5.3, item 4)."""
    out = []
    wanted = {str(p) for p in ports or () if p}

    def scan(pos, tool, text):
        for m in _URL.finditer(text):
            host, port = m.group(1), m.group(2)
            if _is_host(host, host_names) or (port and port in wanted):
                out.append({"line": pos, "tool": tool, "target": m.group(0)})
        for m in _DEVTCP.finditer(text):
            if _is_host(m.group(1), host_names) or m.group(2) in wanted:
                out.append({"line": pos, "tool": tool, "target": m.group(0)})
        for m in _HOSTPORT.finditer(text):
            host, port = m.group(1), m.group(2)
            if "://" in text[max(0, m.start() - 3):m.start() + 3]:
                continue
            if _is_host(host, host_names) or port in wanted:
                out.append({"line": pos, "tool": tool, "target": m.group(0)})

    for pos, call in enumerate(calls or [], 1):
        kind = call.get("kind")
        if kind == "web":
            value = call.get("input")
            text = (" ".join(str(v) for v in value.values()) if isinstance(value, dict)
                    else str(value))
            scan(pos, call.get("tool"), text)
            continue
        text = shell_text(call)
        if text is None:
            continue
        got = unwrap(parse(text))
        for cmd in got["commands"]:
            argv = list(cmd["argv"])
            if skip_rerun and rerun_args(argv) is not None:
                continue
            words = argv + [t for _o, t in cmd["redirects"]]
            joined = " ".join(words)
            before = len(out)
            scan(pos, call.get("tool"), joined)
            if len(out) == before and argv and os.path.basename(argv[0]) in NET_COMMANDS:
                for w in argv[1:]:
                    if _is_host(w, host_names) or w in wanted:
                        out.append({"line": pos, "tool": call.get("tool"), "target": w})
            for k in cmd["heredocs"]:
                scan(pos, call.get("tool"), got["heredocs"][k].get("body") or "")
    return out


# --------------------------------------------------------------------------- the allow list
def command_name(word: str) -> str | None:
    """The program a command word names for every test that lets a call through by its name
    (in-session-runtime.md section 18.6, the command word): the word itself when it holds no `/`;
    its last name when that name is not empty and the text before it is exactly `/usr/bin` or
    `/bin`, read on the raw text (a doubled `/` before the name is the same folder); else None,
    which matches no name. A `.` or `..` part, a doubled leading `/` and any other folder give
    None. The test is on the raw text and not on a normalized path, because the kernel follows a
    link before it reads `..` and normpath does not."""
    word = str(word)
    if "/" not in word:
        return word
    head, name = os.path.split(word)
    if name and head in ("/usr/bin", "/bin"):
        return name
    return None


def rerun_args(argv: list) -> list | None:
    """The arguments handed to the request client when `argv` runs it (`python3
    <...>/packet/bin/rerun ...`), else None. The program is `python3` or `python`, bare or under
    /usr/bin or /bin (command_name): a file of another folder that is named python3 is no client."""
    if len(argv) >= 2 and command_name(argv[0]) in ("python3", "python") \
            and argv[1].replace("\\", "/").endswith("packet/bin/rerun"):
        return list(argv[2:])
    return None


#: allow-listed commands with an option that opens paths the call text does not name (a list of
#: file names read from a file or from stdin), reads its options from a file, or runs a
#: program: {command: (long options, short options, short letters that take the rest of their
#: word as an argument)}, each option as (name, what it does). The scan names only paths
#: written in the call, so such an option is refused outright, and the list file is never
#: read: its content can change after the pass (check round 5, adversarial B1). GNU
#: getopt_long takes any unambiguous prefix of a long name (`wc --f=`, `file --fi`,
#: `sha256sum --c`), so a prefix is refused too. `grep` covers GNU grep and ugrep, whose grep
#: mode takes --from and -Q.
_NAMES = "reads file names from a file"
_RUNS = "runs a program"
_WRITES = "writes a file"
#: the options that make a recursive read follow a link found inside a folder: GNU `grep -R` and
#: ripgrep's `-L`. A verifier that could make a link in its own `tree/` (`bin/rerun --shell 'ln -s
#: ...'`) would read another slot's folder through it with a path the scan places inside its own
#: (section 18.7, check round s3 B1). `diff -r` follows a link too, but real verifiers use `diff
#: -rq tree base` (rr-004), so it stays allowed and the driver's removal of the link
#: (verifier._drop_escaping_links) is its protection.
_FOLLOWS = "follows links"
_NAME_LIST_OPTIONS = {
    "file": ((("files-from", _NAMES), ("compile", _WRITES)), (("f", _NAMES), ("C", _WRITES)),
             "eFmP"),
    "rg": ((("pre", _RUNS), ("pre-glob", _RUNS), ("hostname-bin", _RUNS), ("follow", _FOLLOWS)),
           (("L", _FOLLOWS),), "AdBCeEfgjmMrtT"),
    "wc": ((("files0-from", _NAMES),), (), ""),
    "sha256sum": ((("check", _NAMES),), (("c", _NAMES),), ""),
    "grep": ((("from", _NAMES), ("config", "reads its options from a file"), ("filter", _RUNS),
              ("pager", _RUNS), ("view", _RUNS), ("query", _RUNS),
              ("save-config", "writes a file"), ("dereference-recursive", _FOLLOWS)),
             (("Q", _RUNS), ("R", _FOLLOWS)), "efmABCdDgOtM"),
}


def name_list_option(argv: list) -> str | None:
    """Why `argv` is refused for an option of _NAME_LIST_OPTIONS (`file -f`, `wc --files0-from`,
    `sha256sum -c`, `grep --from`, ugrep's `---FILE` config form and the like), else None.
    Every word after the command counts, `--` included, so an operand spelled like one of these
    options is refused too: the list is default-deny."""
    if not argv:
        return None
    name = os.path.basename(argv[0])
    rule = _NAME_LIST_OPTIONS.get(name)
    if rule is None:
        return None
    longs, shorts, takes_arg = rule
    shorts = dict(shorts)
    for word in argv[1:]:
        if name == "grep" and word.startswith("---"):
            return "grep %s reads its options from a file" % word.split("=", 1)[0]
        if word.startswith("--"):
            opt = word[2:].split("=", 1)[0]
            for full, what in longs:
                if opt and full.startswith(opt):
                    return "%s --%s %s" % (name, full, what)
        elif word.startswith("-") and len(word) > 1:
            for ch in word[1:]:
                if ch in shorts:
                    return "%s -%s %s" % (name, ch, shorts[ch])
                if ch in takes_arg:
                    break
    return None


# --------------------------------------------------------------------------- the read forms
#: the read-only text tools the verifier's allow list adds (t-173 fix batch 3, F16: the real
#: verifier agents of rr-002 used sed -n, sort, uniq, tr and cut in their own shell). Each only
#: reads the files it names and prints; the three below with an option or an operand that writes,
#: runs a program or opens a path the call text does not name are refused by `read_form`.
READ_FORMS = ("sed", "sort", "uniq", "tr", "cut", "nl", "tac", "comm", "paste", "cmp", "basename",
              "dirname", "seq", "test", "[", "true", "false")
#: commands that open no file named by their words (they print them, or compute on the text): the
#: exact path scan of the verifier reads only their redirects (_walk)
NAMES_NOTHING = ("echo", "printf", "basename", "dirname", "seq", "true", "false", ":", "pwd")
#: sed's long options: what each takes (None: a flag, "value": a value, "script": a script) and
#: the two that are refused (in place editing, a script read from a file the scan cannot read)
_SED_LONG = {"binary": None, "debug": None, "expression": "script", "follow-symlinks": None,
             "line-length": "value", "null-data": None, "posix": None, "quiet": None,
             "regexp-extended": None, "sandbox": None, "separate": None, "silent": None,
             "unbuffered": None, "zero-terminated": None, "help": None, "version": None}
_SED_REFUSED = {"in-place": "edits a file in place", "file": "reads its script from a file"}
_SED_SIMPLE = set("pdDnNPhHgGxzF=")
_SORT_REFUSED = {"output": "writes its output to a file",
                 "temporary-directory": "writes temporary files",
                 "compress-program": "runs a program",
                 "files0-from": "reads file names from a file"}
_SORT_ARG = set("ktS")
_UNIQ_VALUE = set("fsw")
_UNIQ_LONG_VALUE = ("skip-fields", "skip-chars", "check-chars")


def _sed_regex_end(s: str, i: int, delim: str, regex: bool) -> int:
    """The index of the `delim` that closes a sed regex (or replacement) starting at `i`, or -1
    when the text cannot be read for sure: an unescaped line break, the end of the script, or,
    in a regex, a bracket expression that holds the delimiter (sed versions read it apart)."""
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 < n and s[i + 1] == "\n" and regex:
                return -1
            i += 2
            continue
        if c == "\n":
            return -1
        if c == delim:
            return i
        if regex and c == "[":
            j = i + 1
            if j < n and s[j] == "^":
                j += 1
            if j < n and s[j] == "]":
                j += 1
            while j < n and s[j] != "]":
                if s[j] == delim or s[j] == "\n":
                    return -1
                j += 1
            if j >= n:
                return -1
            i = j + 1
            continue
        i += 1
    return -1


def _sed_scan(script: str) -> tuple:
    """(problem, wfiles, runs, readable) for one sed script. `problem` is the first refusal
    sed_script_problem gives, or None. `wfiles` lists the file names the script's `w` and `W`
    commands and `s///w` flags name (GNU sed takes the rest of the line as the name). `runs` is
    True when the script holds an `e` command or an `s///e` flag. `readable` is False when the
    reader stopped at a form it cannot follow, so the names it found may not be all of them."""
    s = str(script)
    n = len(s)
    i = 0
    first = [None]
    wfiles: list = []
    runs = [False]

    def refuse(text):
        if first[0] is None:
            first[0] = text

    def ws(k):
        while k < n and s[k] in " \t":
            k += 1
        return k

    def fname(k):
        """The file name that starts at k (after blanks) and runs to the end of the line, and the
        index after it."""
        k = ws(k)
        end = s.find("\n", k)
        end = n if end < 0 else end
        name = s[k:end]
        if name:
            wfiles.append(name)
        return end

    def address(k):
        """The index after one address at k, or k when there is none, or -1 when unreadable."""
        if k < n and s[k].isdigit():
            while k < n and s[k].isdigit():
                k += 1
            if k < n and s[k] == "~":
                k += 1
                while k < n and s[k].isdigit():
                    k += 1
            return k
        if k < n and s[k] == "$":
            return k + 1
        if k < n and s[k] in "/\\":
            delim = "/"
            if s[k] == "\\":
                if k + 1 >= n or s[k + 1] in "\n\\":
                    return -1
                delim, k = s[k + 1], k + 1
            end = _sed_regex_end(s, k + 1, delim, True)
            if end < 0:
                return -1
            k = end + 1
            while k < n and s[k] in "IM":
                k += 1
            return k
        return k

    def stop(text):
        refuse(text)
        return first[0], wfiles, runs[0], False

    while i < n:
        i = ws(i)
        if i >= n:
            break
        if s[i] in ";\n":
            i += 1
            continue
        if s[i] == "#":
            end = s.find("\n", i)
            i = n if end < 0 else end + 1
            continue
        k = address(i)
        if k < 0:
            return stop("sed: an address the reader cannot follow")
        if k != i:
            k = ws(k)
            if k < n and s[k] == ",":
                k = ws(k + 1)
                if k < n and s[k] in "+~":
                    k += 1
                    while k < n and s[k].isdigit():
                        k += 1
                else:
                    k2 = address(k)
                    if k2 < 0 or k2 == k:
                        return stop("sed: an address the reader cannot follow")
                    k = k2
        i = ws(k)
        while i < n and s[i] == "!":
            i = ws(i + 1)
        if i >= n:
            return stop("sed: an address with no command")
        c = s[i]
        if c in _SED_SIMPLE or c in "{}":
            i += 1
            continue
        if c in "qQlL":
            i += 1
            while i < n and s[i].isdigit():
                i += 1
            continue
        if c in ":btT":
            end = i + 1
            while end < n and s[end] not in ";\n":
                end += 1
            i = end
            continue
        if c in "aic":
            # a, i and c print the rest of the line (a backslash goes on to the next line)
            j = i + 1
            while j < n and s[j] != "\n":
                if s[j] == "\\":
                    j += 1
                j += 1
            i = j
            continue
        if c in "sy":
            if i + 1 >= n or s[i + 1] in "\n\\":
                return stop("sed: an %s command the reader cannot follow" % c)
            delim = s[i + 1]
            end1 = _sed_regex_end(s, i + 2, delim, True)
            end2 = _sed_regex_end(s, end1 + 1, delim, False) if end1 >= 0 else -1
            if end2 < 0:
                return stop("sed: an %s command the reader cannot follow" % c)
            i = end2 + 1
            if c == "s":
                while i < n and s[i] not in ";\n} \t#":
                    f = s[i]
                    if f in "wW":
                        refuse("sed s///w writes a file")
                        i = fname(i + 1)
                        break
                    if f == "e":
                        refuse("sed s///e runs a command")
                        runs[0] = True
                    elif f not in "gpiImM0123456789":
                        return stop("sed: an s flag the reader does not know (%s)" % f)
                    i += 1
            continue
        if c in "wW":
            refuse("sed %s writes a file" % c)
            i = fname(i + 1)
            continue
        if c in "rR":
            refuse("sed %s reads a file the call does not name" % c)
            end = s.find("\n", i)
            i = n if end < 0 else end
            continue
        if c == "e":
            refuse("sed e runs a command")
            runs[0] = True
            end = s.find("\n", i)
            i = n if end < 0 else end
            continue
        return stop("sed: a command the reader does not know (%s)" % c)
    return first[0], wfiles, runs[0], True


def sed_script_problem(script: str) -> str | None:
    """Why a sed script is refused, or None: it may print, delete, substitute, transliterate,
    branch and add text, and nothing else. A `w` or `W` command or an `s///w` flag writes a
    file, an `r` or `R` command reads one the call text does not name, an `e` command or an
    `s///e` flag runs a command, and a command or a form the reader does not know is refused:
    the rule is default-deny."""
    return _sed_scan(script)[0]


def _long_match(word: str, table) -> tuple:
    """(name, value or None) of a `--name[=value]` word, the name taken as a prefix the way GNU
    getopt_long takes it; the name is None when it names no entry of `table`."""
    body = word[2:]
    name, eq, value = body.partition("=")
    if name in table:
        return name, (value if eq else None)
    got = [k for k in table if name and k.startswith(name)]
    return (got[0] if len(got) == 1 else None), (value if eq else None)


def _sed_args(args: list) -> dict:
    """The words of a sed call read with the option reader of GNU sed 4.9: {"problem" (the first
    refusal text of an option, as `_sed_problem` always gave it, or None), "scripts" ([(index in
    `args`, start of the script in that word)]), "files" (the operand indexes), "inplace",
    "suffixes" ([(index, start, text)] for each `-i[SUFFIX]` and `--in-place[=SUFFIX]` word that
    gives one), "fscript" (a `-f` or `--file` script, which the scan cannot read)}."""
    out = {"problem": None, "scripts": [], "files": [], "inplace": False, "suffixes": [],
           "fscript": False}
    operands, i, opts = [], 0, True

    def note(text):
        if out["problem"] is None:
            out["problem"] = text

    while i < len(args):
        a = args[i]
        if opts and a == "--":
            opts = False
        elif opts and a.startswith("--"):
            name = a[2:].partition("=")[0]
            handled = False
            for full, why in _SED_REFUSED.items():
                if name and full.startswith(name):
                    note("sed --%s %s" % (full, why))
                    if full == "in-place":
                        out["inplace"] = True
                        if "=" in a:
                            out["suffixes"].append((i, a.index("=") + 1, a.partition("=")[2]))
                    else:
                        out["fscript"] = True
                        if "=" not in a:
                            i += 1
                    handled = True
                    break
            if not handled:
                full, value = _long_match(a, _SED_LONG)
                if full is None:
                    note("sed: an option the reader does not know (%s)" % a[:40])
                else:
                    start = a.index("=") + 1 if "=" in a else 0
                    if _SED_LONG[full] is not None and value is None:
                        i += 1
                        value = args[i] if i < len(args) else ""
                        start = 0
                        idx = i
                    else:
                        idx = i
                    if _SED_LONG[full] == "script":
                        out["scripts"].append((idx, start))
        elif opts and a.startswith("-") and len(a) > 1:
            k = 1
            while k < len(a):
                ch = a[k]
                if ch == "i":
                    note("sed -i edits a file in place")
                    out["inplace"] = True
                    if a[k + 1:]:
                        out["suffixes"].append((i, k + 1, a[k + 1:]))
                    break
                if ch == "f":
                    note("sed -f reads its script from a file")
                    out["fscript"] = True
                    if not a[k + 1:]:
                        i += 1
                    break
                if ch in "el":
                    value = a[k + 1:]
                    idx, start = i, k + 1
                    if not value:
                        i += 1
                        value = args[i] if i < len(args) else ""
                        idx, start = i, 0
                    if ch == "e":
                        out["scripts"].append((idx, start))
                    break
                if ch not in "nsuzErb":
                    note("sed: an option the reader does not know (-%s)" % ch)
                k += 1
        else:
            operands.append(i)
        i += 1
    if not out["scripts"] and not out["fscript"] and operands:
        out["scripts"].append((operands[0], 0))
        operands = operands[1:]
    out["files"] = operands
    return out


def _sed_problem(args: list, live: list | None = None) -> str | None:
    """Why a sed call is refused, or None. `live` (one entry per word of `args`, parse's offsets
    of an expansion) lets a script word that holds an expansion be refused: the text before the
    shell expands it says nothing about the script sed gets."""
    got = _sed_args(args)
    if got["problem"]:
        return got["problem"]
    for idx, start in got["scripts"]:
        if live is not None and idx < len(live) and _live_from(args[idx], live[idx], start):
            return "sed: a script that holds an expansion cannot be read"
        why = sed_script_problem(args[idx][start:])
        if why:
            return why
    return None


def sed_parts(argv: list, live: list | None = None) -> dict:
    """What a sed call writes, from the option reader of GNU sed 4.9 (in-session-runtime.md
    section 18.2, Code): {"scripts" ([indexes into argv]), "files" ([indexes into argv] of the
    file operands), "inplace", "suffixes" ([(index, start, text)], every `-i` and `--in-place`
    word that gives one, in order), "wfiles" (the file names the `w`, `W` and `s///w` of each
    readable script name), "runs" (an `e` command or `s///e` flag), "unreadable" (a script the
    scan cannot read: one given by `-f` or `--file`, one with an expansion in it when `live` says
    so, one the reader cannot follow, or an option it does not know)}. Indexes count the command
    word as 0."""
    args = list(argv[1:])
    got = _sed_args(args)
    out = {"scripts": [i + 1 for i, _s in got["scripts"]], "files": [i + 1 for i in got["files"]],
           "inplace": got["inplace"],
           "suffixes": [(i + 1, st, text) for i, st, text in got["suffixes"]],
           "wfiles": [], "runs": False, "unreadable": bool(got["fscript"])}
    if got["problem"] and got["problem"].startswith("sed: an option the reader does not know"):
        out["unreadable"] = True
    for idx, start in got["scripts"]:
        if live is not None and idx + 1 < len(live) and _live_from(args[idx], live[idx + 1], start):
            out["unreadable"] = True
            continue
        _problem, wfiles, runs, readable = _sed_scan(args[idx][start:])
        out["wfiles"].extend(wfiles)
        out["runs"] = out["runs"] or runs
        if not readable:
            out["unreadable"] = True
    return out


def _sort_problem(args: list) -> str | None:
    i, opts = 0, True
    while i < len(args):
        a = args[i]
        if opts and a == "--":
            opts = False
        elif opts and a.startswith("--"):
            name = a[2:].partition("=")[0]
            for full, why in _SORT_REFUSED.items():
                if name and full.startswith(name):
                    return "sort --%s %s" % (full, why)
        elif opts and a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if ch == "o":
                    return "sort -o writes its output to a file"
                if ch == "T":
                    return "sort -T writes temporary files"
                if ch in _SORT_ARG:
                    if k == len(a) - 1:
                        i += 1
                    break
        i += 1
    return None


def _uniq_problem(args: list) -> str | None:
    operands, i, opts = 0, 0, True
    while i < len(args):
        a = args[i]
        if opts and a == "--":
            opts = False
        elif opts and a.startswith("--"):
            name = a[2:].partition("=")[0]
            if name in _UNIQ_LONG_VALUE and "=" not in a:
                i += 1
        elif opts and a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if ch in _UNIQ_VALUE:
                    if k == len(a) - 1:
                        i += 1
                    break
        else:
            operands += 1
        i += 1
    if operands > 1:
        return "uniq writes its output to its second file"
    return None


def read_form(argv: list, live: list | None = None) -> str | None:
    """Why a command of READ_FORMS is refused in the form it is written, or None: `sed` with
    `-i`, `-f` or a script that writes, reads or runs (sed_script_problem) or that holds an
    expansion (`live`, parse's offsets for each word of `argv`, says so), `sort` with `-o`, `-T`,
    `--compress-program` or `--files0-from`, `uniq` with a second file (its output). The other
    words of READ_FORMS only read and print in every form."""
    if not argv:
        return None
    name = os.path.basename(argv[0])
    if name == "sed":
        return _sed_problem(list(argv[1:]), None if live is None else list(live)[1:])
    if name == "sort":
        return _sort_problem(list(argv[1:]))
    if name == "uniq":
        return _uniq_problem(list(argv[1:]))
    return None


def _client_ok(argv: list, cwd: str, client: str | None) -> bool:
    if client is None or rerun_args(argv) is None:
        return False
    if command_name(argv[0]) != "python3":
        return False
    full, placed = _resolve(argv[1], cwd, None)
    return placed and full == os.path.normpath(client)


def _in_out(path: str, raw: str, out_dir: str) -> bool:
    """True when `path` lies under `out_dir` both as written and as the kernel reaches it now
    (a link under out/ that points elsewhere is a write outside out/)."""
    return _under(path, out_dir) and any(_under(_real(raw), d) for d in _with_real((out_dir,)))


def shell_allowed(calls: list, *, allow: tuple, try_folder: str, client: str | None,
                  reset_cwd: str | None = None, runtime_tools: tuple = (),
                  exact_allowed: tuple = ()) -> list:
    """[{"line", "tool", "command", "why"}]: every call the allow list of section 5.3, item 4
    refuses. With an `allow` that is a Grammar (the verifier's, section 19) a shell call passes
    only when `grammar_check` accepts it, a `bash -lc` wrapper taken off first; the hit then also
    holds the grammar's `rule`, `exact_allowed` are the files a path may name exactly (the prompt
    file and the persisted outputs, 18.1a), and every call starts in `reset_cwd` (the shell is not
    kept between calls) or the try folder. With a plain tuple the call is read as follows. A shell call passes only when every simple command in it (a `bash -lc` wrapper
    unwrapped) is the request client `client` (None for the auditor, which has none), or a
    command of `allow`, and the call holds no FLAGS construct and no output redirect outside
    `<try_folder>/out/` other than /dev/null. `rg` with `--pre` or `--pre-glob` runs a program
    and is refused, and so is an option of _NAME_LIST_OPTIONS (name_list_option), which opens
    paths the call text does not name or runs a program. A write tool call passes only when
    every path it names is under `out/`. A read tool runs nothing and passes. Any other tool is
    refused. A text the reader cannot parse is refused: the list is default-deny.

    `reset_cwd` starts every call in that folder (a session launch, see `outside`), and a call of
    kind `other` whose tool is in `runtime_tools` (SubagentHandback, ToolSearch: the runtime's own
    tools) is skipped instead of refused.

    A `for` loop over written words is read as its body once for each word (unroll), so its
    commands are judged as the commands they are, and a command of READ_FORMS on the allow list
    is refused in a form that writes, runs a program or reads a path the call does not name
    (read_form; t-173 fix batch 3, F16)."""
    out = []
    out_dir = os.path.join(try_folder, "out")
    here, prev = try_folder, None
    names = tuple(allow or ())
    for pos, call in enumerate(calls or [], 1):
        if reset_cwd is not None:
            here, prev = reset_cwd, None
        kind = call.get("kind")
        tool = call.get("tool")
        if kind in ("read", "denied"):
            continue
        if kind == "other" and tool in tuple(runtime_tools or ()):
            continue
        if kind == "write":
            found = _tool_paths(call, here)
            targets = [p for p, _a, _placed, _raw in found]
            if not targets or not all(placed and _in_out(p, raw, out_dir)
                                      for p, _a, placed, raw in found):
                out.append({"line": pos, "tool": tool, "command": ", ".join(targets),
                            "why": "a write outside out/"})
            continue
        if kind != "shell":
            out.append({"line": pos, "tool": tool, "command": str(call.get("input"))[:200],
                        "why": "a tool the verifier is not given"})
            continue
        text = shell_text(call) or ""
        if isinstance(allow, Grammar):
            hit = grammar_check(_unwrapped_text(text), table=allow.table, try_folder=try_folder,
                                start=reset_cwd if reset_cwd is not None else try_folder,
                                exact_files=tuple(exact_allowed or ()))
            if hit:
                out.append({"line": pos, "tool": tool, "command": text[:400], "why": hit["why"],
                            "rule": hit["rule"]})
            continue
        got = unroll(unwrap(parse(text)))
        if got["error"]:
            out.append({"line": pos, "tool": tool, "command": text[:400],
                        "why": "unreadable: %s" % got["error"]})
            continue
        if got["flags"]:
            out.append({"line": pos, "tool": tool, "command": text[:400],
                        "why": "uses %s" % ", ".join(got["flags"])})
            continue
        for cmd in got["commands"]:
            argv = cmd["argv"]
            shown = " ".join(argv)[:400]
            if not argv:
                why = None
            elif _client_ok(argv, here, client):
                why = None
            elif command_name(argv[0]) not in names:
                why = "runs %s, which is not on the allow list" % argv[0]
            else:
                why = name_list_option(argv) or read_form(argv, cmd.get("live"))
            if why is None:
                rlive = cmd.get("rlive") or [None] * len(cmd["redirects"])
                for (op, target), rl in zip(cmd["redirects"], rlive):
                    if ">" not in op or str(target).startswith("&"):
                        continue
                    raw, placed = _place(str(target), here, prev,
                                         literal=not _live_from(str(target), rl))
                    full = os.path.normpath(raw) if placed else raw
                    if not placed:
                        why = "writes %s, which the scan cannot place" % target
                        break
                    if full != "/dev/null" and not _in_out(full, raw, out_dir):
                        why = "writes %s, outside out/" % target
                        break
            if why:
                out.append({"line": pos, "tool": tool, "command": shown, "why": why})
            if argv and argv[0] == "cd":
                here, prev, _target, _placed = _cd(argv, here, prev)
    return out


# --------------------------------------------------------------------------- reproduce blocks
def reads_only(blocks: list, *, gates: bool = False) -> bool:
    """True when every command of every block is in READ_ONLY, so the blocks re-create nothing
    (section 3.2). A block the reader cannot parse, or one using a construct it does not follow,
    counts as running something; with `gates`, so does a block whose first line matches
    GATE_BLOCK, since the gate runs the item's frozen command."""
    for block in blocks or []:
        text = str(block or "")
        first = text.split("\n", 1)[0].strip()
        if gates and GATE_BLOCK.match(first):
            return False
        got = unwrap(parse(text))
        if got["error"] or set(got["flags"]) - {"unquoted-heredoc"}:
            return False
        for cmd in got["commands"]:
            argv = cmd["argv"]
            if not argv:
                continue
            if command_name(argv[0]) not in READ_ONLY:
                return False
    return True


# --------------------------------------------------------------------------- the strict grammar
# in-session-runtime.md section 19 (t-228). The verifier's shell is not read the way bash reads
# it; it is a small language every word of which the guard reads exactly, and a call outside the
# language is refused where it stands. Inside the language no word is expanded by the shell (no
# glob, no variable other than the name of a `for` loop around the command, no command
# substitution, no option the table does not list), so the guard and bash agree on what an
# accepted call reads and writes. A spelling the guard does not know is not a hole to patch: it
# is outside the language.

#: the most simple commands one call holds, a loop body counted once for each loop word
GRAMMAR_MAX_COMMANDS = 256
#: 19.3.2: the characters a word may hold outside quotes. None of them starts an expansion, a
#: pattern or an operator in bash.
PLAIN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:=,+%@^-")
_G_NUMBER = "number"
_G_PLUS = "plus-number"
_G_TEXT = "text"
_G_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_G_LOOP_NAME = re.compile(r"[a-z][a-z0-9]{0,15}\Z")
_G_DELIM = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_G_REDIR = re.compile(r"([0-9]*)(<<<|<<-|<<|<>|<&|<|>>|>\||>&|>)")
_G_FD_REDIR = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}[<>]")
_G_ENDS = " \t\n;&|<>"
_G_RESERVED = ("if", "then", "else", "elif", "fi", "while", "until", "do", "done", "case", "esac",
               "select", "function", "time", "coproc", "!", "[[", "]]", "{", "}")
#: the reasons a character outside quotes refuses the call (19.3.8): (rule, what bash would do)
_G_REFUSED = {
    "*": ("glob", "a glob character"), "?": ("glob", "a glob character"),
    "[": ("glob", "a glob character"), "]": ("glob", "a glob character"),
    "{": ("brace", "a brace"), "}": ("brace", "a brace"), "~": ("tilde", "a tilde"),
    "`": ("backquote", "a backquote"), "\\": ("backslash", "a backslash"),
    "(": ("paren", "a parenthesis"), ")": ("paren", "a parenthesis"), "!": ("bang", "a !"),
    "#": ("comment", "a #"),
}
#: the programs of the table whose operands the guard places as paths, and what each one reads
_G_TEST_UNARY = ("-b", "-c", "-d", "-e", "-f", "-g", "-G", "-h", "-k", "-L", "-N", "-O", "-p",
                 "-r", "-s", "-S", "-u", "-w", "-x")
_G_TEST_BINARY = ("-nt", "-ot", "-ef")
#: the operators of the two shapes of `test` the grammar reads (t-296, design 25.4) that place no
#: path: a string check of one word, and a comparison of two words. `test` takes one check of two
#: or three words; any other operator or word count is refused by default (dec-6939efd4f97c).
_G_TEST_STRING = ("-n", "-z")
_G_TEST_COMPARE = ("=", "==", "!=", "-eq", "-ne", "-lt", "-le", "-gt", "-ge")


def _row(flags="", vals=None, longs=None, operands="files", words="", numopt=False):
    return {"flags": flags, "vals": dict(vals or {}), "longs": dict(longs or {}),
            "operands": operands, "words": words or operands, "numopt": numopt}


_G_CONTEXT = {"A": _G_NUMBER, "B": _G_NUMBER, "C": _G_NUMBER, "m": _G_NUMBER}
#: 19.5, the table: each program of the verifier's shell, the flags that may be joined in one
#: word, the options with a value (short letter or exact long name, and the kind of the value),
#: and what its operands are. A long option is written in full: no prefix of it is the option.
VERIFIER_TABLE = {
    "cat": _row("nAbEsTv", words="files"),
    "head": _row("qv", {"n": _G_NUMBER, "c": _G_NUMBER}, words="files", numopt=True),
    "tail": _row("qv", {"n": _G_PLUS, "c": _G_PLUS}, words="files", numopt=True),
    "wc": _row("lcwmL", words="files"),
    "grep": _row("cHhilLnoqsvwxEFrIa", dict(_G_CONTEXT, e=_G_TEXT),
                 {"--include": _G_TEXT, "--exclude": _G_TEXT, "--exclude-dir": _G_TEXT},
                 operands="search",
                 words="a pattern unless -e is given, then files or folders (a folder with -r)"),
    "rg": _row("nNiSlcovwxFHIu", dict(_G_CONTEXT, e=_G_TEXT, g=_G_TEXT, t=_G_TEXT),
               {"--no-ignore": None, "--hidden": None, "--files": None, "--glob": _G_TEXT},
               operands="search",
               words="a pattern unless -e or --files is given, then files or folders"),
    "ls": _row("laAdR1hSrt", operands="paths", words="files or folders (none means the current folder)"),
    "stat": _row("", {"c": _G_TEXT}, operands="folders", words="files or folders"),
    "file": _row("bi", operands="folders", words="files or folders"),
    "sha256sum": _row("", words="files"),
    "diff": _row("quiwbBsNat", {"U": _G_NUMBER}, words="two files, no folder"),
    "cmp": _row("ls", words="two files"),
    "comm": _row("123", words="two files"),
    "sed": _row("nEs", {"e": "script"}, operands="sed",
                words="a script unless -e is given, then files"),
    "sort": _row("rnufbhVsc", {"k": _G_TEXT, "t": _G_TEXT}, words="files"),
    "uniq": _row("cdui", {"f": _G_NUMBER, "s": _G_NUMBER, "w": _G_NUMBER}, operands="uniq",
                 words="at most one file"),
    "tr": _row("dscC", operands="text", words="sets, not files"),
    "cut": _row("s", {"c": _G_TEXT, "f": _G_TEXT, "b": _G_TEXT, "d": _G_TEXT}, words="files"),
    "paste": _row("s", {"d": _G_TEXT}, words="files"),
    "nl": _row(words="files"),
    "tac": _row(words="files"),
    "basename": _row(operands="text", words="text"),
    "dirname": _row(operands="text", words="text"),
    "seq": _row(operands="text", words="text"),
    "test": _row(operands="test", words="one check: a file operator such as `-e`, `-f`, `-d` or `-s` "
                 "and one path of your try folder, `-n` or `-z` and one word, or two words around one "
                 "comparison"),
    "echo": _row(operands="echo", words="text, with `-n`, `-e` or `-E` first as its options"),
    "printf": _row(operands="printf", words="a format, then text (no -v)"),
    "true": _row(operands="none", words="none"),
    "false": _row(operands="none", words="none"),
    "pwd": _row(operands="none", words="none"),
    "cd": _row(operands="cd", words="your try folder"),
    "python3": _row(operands="client", words="packet/bin/rerun and its arguments"),
}
#: 19.3.7: the one command that may carry a heredoc, by the arguments after the client
GRAMMAR_SUBMISSION = ("--submit", "-")


class Grammar(tuple):
    """The verifier's allow list read as the grammar of section 19: a tuple of the names the
    launch spec's `bash_allow` holds, which carries the table of 19.5 and the submission form of
    19.3.7. `shell_allowed` runs `grammar_check` for a Grammar and reads a plain tuple as section
    18 does, so the auditor keeps its rules."""

    table = VERIFIER_TABLE
    submission = GRAMMAR_SUBMISSION


class _Stop(Exception):
    """A refusal of the grammar: the rule's name and what the scan found."""

    def __init__(self, rule: str, why: str):
        super().__init__(why)
        self.rule, self.why = rule, why


# ---- 19.3: the tokens
def _g_dquote(text: str, i: int, parts: list) -> int:
    """The double-quoted piece that starts at text[i] (a `"`), added to `parts`; the index after
    its closing quote. Inside it `$`, a backquote and a backslash are read as 19.3.4 says."""
    n, j, buf, before = len(text), i + 1, [], len(parts)

    def flush():
        if buf:
            parts.append(("dq", "".join(buf)))
            del buf[:]

    while True:
        if j >= n:
            raise _Stop("unreadable", "a double quote that is never closed")
        c = text[j]
        if c == '"':
            break
        if c == "`":
            raise _Stop("backquote", "a backquote")
        if c == "\\":
            nxt = text[j + 1:j + 2]
            if nxt == "\n":
                raise _Stop("backslash", "a backslash at the end of a line inside double quotes")
            if nxt in ("$", "`", '"', "\\") and nxt != "":
                buf.append(nxt)
                j += 2
                continue
        elif c == "$":
            if text[j + 1:j + 2] == "?":
                flush()
                parts.append(("status", "?"))
                j += 2
                continue
            m = _G_NAME.match(text, j + 1)
            if m:
                flush()
                parts.append(("var", m.group(0)))
                j = m.end()
                continue
            raise _Stop("expansion", "a $ that is not $NAME of a for loop or $? in echo or printf")
        buf.append(c)
        j += 1
    flush()
    if len(parts) == before:
        parts.append(("dq", ""))                      # "" is one empty word
    return j + 1


def _g_word(text: str, i: int) -> tuple:
    """(parts, index after the word): one word read from text[i], each piece a plain run
    ("lit"), single-quoted text ("sq"), double-quoted text ("dq"), `$NAME` ("var") or `$?`
    ("status"). The word ends at a blank or an operator."""
    n, parts = len(text), []
    while i < n:
        ch = text[i]
        if ch in PLAIN_CHARS:
            j = i + 1
            while j < n and text[j] in PLAIN_CHARS:
                j += 1
            parts.append(("lit", text[i:j]))
            i = j
        elif ch == "'":
            j = text.find("'", i + 1)
            if j < 0:
                raise _Stop("unreadable", "a single quote that is never closed")
            parts.append(("sq", text[i + 1:j]))
            i = j + 1
        elif ch == '"':
            i = _g_dquote(text, i, parts)
        elif ch == "$":
            if text[i + 1:i + 2] == "?":
                parts.append(("status", "?"))
                i += 2
                continue
            m = _G_NAME.match(text, i + 1)
            if not m:
                raise _Stop("expansion", "a $ that is not $NAME of a for loop or $? in echo or printf")
            parts.append(("var", m.group(0)))
            i = m.end()
        elif ch in _G_ENDS:
            break
        elif ch in _G_REFUSED:
            if ch == "{" and _G_FD_REDIR.match(text, i):
                raise _Stop("redirect", "a {fd} redirect")
            rule, what = _G_REFUSED[ch]
            raise _Stop(rule, "an unquoted %s (%s)" % (ch, what))
        else:
            raise _Stop("character", "the character %r, which is not plain text, a quote or a blank" % ch)
    return parts, i


def _g_tokens(text: str) -> list:
    """The tokens of a call, left to right: ("word", parts), ("op", "&&" | "||" | "|" | ";",
    newline), ("redir", fd, op, parts) for `<`, `>` and `>>`, ("dup", fd, op, digits) for `N>&M`
    and `N<&M`, and ("heredoc", delimiter) for the one heredoc of the submission."""
    toks, i, n = [], 0, len(text)
    pending, seen = None, False
    while i < n:
        ch = text[i]
        if ch in " \t":
            i += 1
            continue
        if ch == "\n":
            toks.append(("op", ";", True))
            i += 1
            if pending is not None:
                k = i
                while True:
                    e = text.find("\n", k)
                    line = text[k:] if e < 0 else text[k:e]
                    if line == pending:
                        i = n if e < 0 else e + 1
                        break
                    if e < 0:
                        raise _Stop("unreadable", "a heredoc with no closing line")
                    k = e + 1
                pending = None
            continue
        if ch == "&":
            if text.startswith("&&", i):
                toks.append(("op", "&&", False))
                i += 2
                continue
            if text.startswith("&>", i):
                raise _Stop("redirect", "a &> redirect")
            raise _Stop("background", "a & that starts a background job")
        if ch == "|":
            if text.startswith("||", i):
                toks.append(("op", "||", False))
                i += 2
                continue
            if text.startswith("|&", i):
                raise _Stop("redirect", "a |& pipe")
            toks.append(("op", "|", False))
            i += 1
            continue
        if ch == ";":
            if text.startswith(";;", i) or text.startswith(";&", i):
                raise _Stop("character", "a case terminator")
            toks.append(("op", ";", False))
            i += 1
            continue
        m = _G_REDIR.match(text, i)
        if m:
            fd, op = m.group(1), m.group(2)
            i = m.end()
            if op in ("<<<", "<<-", "<>", ">|"):
                raise _Stop("redirect", "the redirect %s" % op)
            if op in ("<&", ">&"):
                m2 = re.match(r"[0-9]+(?=[ \t\n;&|<>]|\Z)", text[i:])
                if not m2:
                    raise _Stop("redirect", "a %s that names anything but a descriptor number" % op)
                toks.append(("dup", fd, op, m2.group(0)))
                i += m2.end()
                continue
            while i < n and text[i] in " \t":
                i += 1
            if op == "<<":
                if fd:
                    raise _Stop("heredoc", "a heredoc on a descriptor")
                if not text.startswith("'", i):
                    raise _Stop("heredoc", "a heredoc whose delimiter is not in single quotes")
                j = text.find("'", i + 1)
                if j < 0:
                    raise _Stop("unreadable", "a single quote that is never closed")
                delim = text[i + 1:j]
                if not _G_DELIM.match(delim):
                    raise _Stop("heredoc", "a heredoc delimiter that is not capital letters, digits and _")
                i = j + 1
                if i < n and text[i] not in _G_ENDS:
                    raise _Stop("heredoc", "text written onto the heredoc delimiter")
                if seen:
                    raise _Stop("heredoc", "a second heredoc")
                seen = True
                pending = delim
                toks.append(("heredoc", delim))
                continue
            parts, i = _g_word(text, i)
            if not parts:
                raise _Stop("unreadable", "a redirect with no target")
            toks.append(("redir", fd, op, parts))
            continue
        parts, j = _g_word(text, i)
        if not parts:
            raise _Stop("character", "the character %r" % ch)
        toks.append(("word", parts))
        i = j
    if pending is not None:
        raise _Stop("unreadable", "a heredoc with no closing line")
    return toks


# ---- 19.4: the shape of a call
def _g_is_word(tok, name: str) -> bool:
    return tok[0] == "word" and tok[1] == [("lit", name)]


def _g_simple(toks: list, pos: int) -> tuple:
    cmd = {"words": [], "redirs": [], "heredoc": False}
    while pos < len(toks) and toks[pos][0] != "op":
        t = toks[pos]
        if t[0] == "word":
            cmd["words"].append(t[1])
        elif t[0] in ("redir", "dup"):
            cmd["redirs"].append(t)
        elif t[0] == "heredoc":
            cmd["heredoc"] = True
        pos += 1
    if not cmd["words"]:
        raise _Stop("unreadable", "an operator or a redirect with no command")
    return cmd, pos


def _g_pipeline(toks: list, pos: int) -> tuple:
    cmd, pos = _g_simple(toks, pos)
    cmds = [cmd]
    while pos < len(toks) and toks[pos][0] == "op" and toks[pos][1] == "|":
        pos += 1
        while pos < len(toks) and toks[pos][0] == "op" and toks[pos][2]:
            pos += 1                                    # a newline after a | is a line join
        if pos >= len(toks) or toks[pos][0] == "op":
            raise _Stop("unreadable", "a | with no command after it")
        cmd, pos = _g_simple(toks, pos)
        cmds.append(cmd)
    return ("pipe", cmds), pos


def _g_loop(toks: list, pos: int) -> tuple:
    """The `for NAME in WORD ...; do COMMANDS; done` that starts at toks[pos] (the word `for`):
    (("loop", name, words, body), index after `done`)."""
    n = len(toks)
    pos += 1
    if pos >= n or toks[pos][0] != "word":
        raise _Stop("loop", "a for with no loop name")
    parts = toks[pos][1]
    pos += 1
    if len(parts) != 1 or parts[0][0] != "lit" or not _G_LOOP_NAME.match(parts[0][1]):
        raise _Stop("loop", "a loop name that is not one to sixteen lowercase letters and digits "
                    "starting with a letter")
    name = parts[0][1]
    if pos >= n or not _g_is_word(toks[pos], "in"):
        raise _Stop("loop", "a for loop with no `in`")
    pos += 1
    words = []
    while pos < n and toks[pos][0] == "word":
        words.append(toks[pos][1])
        pos += 1
    if pos >= n or toks[pos][0] != "op" or toks[pos][1] != ";":
        raise _Stop("loop", "a for loop header that does not end with ; or a newline")
    pos += 1
    while pos < n and toks[pos][0] == "op" and toks[pos][2]:
        pos += 1
    if pos >= n or not _g_is_word(toks[pos], "do"):
        raise _Stop("loop", "a for loop with no `do`")
    pos += 1
    body, pos = _g_list(toks, pos, True)
    if pos >= n:
        raise _Stop("unreadable", "a for loop with no `done`")
    pos += 1                                            # the `done`
    if pos < n and (toks[pos][0] != "op" or toks[pos][1] == "|"):
        raise _Stop("loop", "a redirect, a pipe or a word after `done`")
    return ("loop", name, words, body), pos


def _g_list(toks: list, pos: int, in_loop: bool) -> tuple:
    """(items, index): the commands, pipelines and loops joined by `&&`, `||`, `;` and newlines
    from toks[pos]; in a loop body it stops at the `done` that starts a command."""
    items, last, sep, n = [], "start", None, len(toks)
    while pos < n:
        t = toks[pos]
        if t[0] == "op":
            if t[2]:
                if last == "cmd":
                    last, sep = "semi", ";"
                pos += 1
                continue
            if t[1] == ";" and last == "cmd":
                last, sep = "semi", ";"
            elif t[1] in ("&&", "||") and last == "cmd":
                last, sep = "amp", t[1]
            else:
                raise _Stop("unreadable", "a %s with no command before it" % t[1])
            pos += 1
            continue
        if in_loop and _g_is_word(t, "done"):
            if last in ("amp", "start"):
                raise _Stop("unreadable", "a loop body that ends with an operator or holds no command")
            return items, pos
        if _g_is_word(t, "for"):
            if in_loop:
                raise _Stop("loop", "a loop inside a loop")
            node, pos = _g_loop(toks, pos)
        else:
            node, pos = _g_pipeline(toks, pos)
        items.append((sep, node))
        last, sep = "cmd", None
    if last in ("amp",):
        raise _Stop("unreadable", "a call that ends with && or ||")
    if in_loop:
        return items, pos
    if not items:
        raise _Stop("unreadable", "a call that holds no command")
    return items, pos


def _g_row_options(row: dict) -> str:
    """The options of one row of the table as the prompt and the refusal write them."""
    kinds = {_G_NUMBER: "NUMBER", _G_PLUS: "NUMBER", _G_TEXT: "TEXT", "script": "SCRIPT"}
    parts = ["-" + c for c in row["flags"]]
    parts += ["-%s %s" % (c, kinds[k]) for c, k in row["vals"].items()]
    parts += [k if v is None else "%s=%s" % (k, kinds[v]) for k, v in row["longs"].items()]
    if row["numopt"]:
        parts.append("-NUMBER")
    return " ".join(parts)


def _g_row_text(name: str, row: dict) -> str:
    return "%s takes only these options, written in full: %s" % (name, _g_row_options(row) or "none")


def grammar_table_text(table: dict | None = None) -> list:
    """One line for each program of the table of 19.5, as prompts/verify.md states it: the
    options, then the operands. test_research_prompt_rules.py pins every line against the prompt."""
    out = []
    for name, row in (VERIFIER_TABLE if table is None else table).items():
        opts = _g_row_options(row)
        out.append("`%s`: %s; operands: %s." % (
            name, "options `%s`" % opts if opts else "no options", row["words"]))
    return out


def _g_options(name: str, args: list, row: dict) -> dict:
    """The words of `args` split by the row of the table (19.5) into {"flags" (letters), "vals"
    (letter: values), "longs" (name: values), "operands"}. A word that starts with `-` before `--`
    is an option wherever it stands; a lone `-` and every word after `--` is an operand."""
    flags, vals, longs, ops = set(), {}, {}, []
    i, n = 0, len(args)

    def refuse(what: str):
        raise _Stop("option", "%s: %s" % (what, _g_row_text(name, row)))

    while i < n:
        w = args[i]
        if w == "--":
            ops.extend(args[i + 1:])
            break
        if w == "-" or not w.startswith("-"):
            ops.append(w)
            i += 1
            continue
        if w.startswith("--"):
            key, eq, val = w.partition("=")
            if key not in row["longs"]:
                refuse("%s is not an option of %s" % (key, name))
            kind = row["longs"][key]
            if kind is None:
                if eq:
                    refuse("%s takes no value" % key)
                longs.setdefault(key, [])
            else:
                if not eq:
                    i += 1
                    if i >= n:
                        refuse("%s has no value" % key)
                    val = args[i]
                longs.setdefault(key, []).append(val)
            i += 1
            continue
        body = w[1:]
        if row["numopt"] and re.fullmatch(r"[0-9]+", body):
            i += 1
            continue
        j = 0
        while j < len(body):
            ch = body[j]
            if ch in row["flags"]:
                flags.add(ch)
                j += 1
                continue
            if ch in row["vals"]:
                rest = body[j + 1:]
                if rest == "":
                    i += 1
                    if i >= n:
                        refuse("-%s has no value" % ch)
                    rest = args[i]
                kind = row["vals"][ch]
                if (kind == _G_NUMBER and not re.fullmatch(r"[0-9]+", rest)) or \
                        (kind == _G_PLUS and not re.fullmatch(r"\+?[0-9]+", rest)):
                    refuse("-%s takes a number, not %r" % (ch, rest[:40]))
                vals.setdefault(ch, []).append(rest)
                break
            refuse("-%s is not an option of %s" % (ch, name))
        i += 1
    return {"flags": flags, "vals": vals, "longs": longs, "operands": ops}


# ---- 19.5 to 19.7: the commands, the paths and the redirects
class _GrammarRun:
    """One run of the grammar over one call: the current folder, the try folder and what a path
    may be (19.6), read against the disk for links and folders."""

    def __init__(self, table, try_folder, start, exact_files, sed_problem):
        self.table = table
        self.given = str(try_folder)
        self.tf = os.path.normpath(self.given)
        self.tf_real = _real(self.tf)
        self.out_real = os.path.join(self.tf_real, "out")
        self.client = os.path.join(self.tf, "packet", "bin", "rerun")
        self.cwd = os.path.normpath(str(start if start is not None else try_folder))
        self.exact = set()
        for e in exact_files or ():
            if e:
                self.exact.add(os.path.normpath(str(e)))
                self.exact.add(_real(str(e)))
        self.sed_problem = sed_problem or sed_script_problem
        self.at = 0

    # -- words
    @staticmethod
    def text(parts: list, env: dict, status_ok: bool) -> str:
        out = []
        for kind, v in parts:
            if kind in ("lit", "sq", "dq"):
                out.append(v)
            elif kind == "var":
                if v not in env:
                    raise _Stop("expansion", "$%s, which is not the name of a for loop around the "
                                "command" % v)
                out.append(env[v])
            else:
                if not status_ok:
                    raise _Stop("expansion", "$? outside the words of echo and printf")
                out.append("0")
        return "".join(out)

    # -- paths (19.6.2)
    def place(self, word: str) -> str:
        if word == "/dev/null":
            return word
        if word == "" or "\0" in word:
            raise _Stop("path", "an empty path word")
        if ".." in word.split("/"):
            raise _Stop("dotdot", "the path %s holds a .. part" % word[:120])
        joined = word if os.path.isabs(word) else os.path.join(self.cwd, word)
        lexical = os.path.normpath(joined)
        real = _real(joined)
        if lexical in self.exact and real in self.exact:
            return lexical
        if not (_under(lexical, self.tf) or _under(lexical, self.tf_real)):
            raise _Stop("path", "%s is outside your try folder" % word[:120])
        if not _under(real, self.tf_real):
            raise _Stop("link", "%s leads through a link out of your try folder" % word[:120])
        return lexical

    def files(self, name: str, words: list, *, folders: bool):
        for w in words:
            if w == "-":
                continue
            p = self.place(w)
            if not folders and p != "/dev/null" and os.path.isdir(_real(p)):
                raise _Stop("folder", "%s %s is a folder" % (name, w[:120]))

    # -- one command
    def command(self, cmd: dict, env: dict, *, first: bool, pipe: bool, loop: bool):
        self.at += 1
        words = cmd["words"]
        w0parts = words[0]
        if w0parts[0][0] == "lit" and _ASSIGN.match(w0parts[0][1]):
            raise _Stop("assignment", "%s sets a variable" % w0parts[0][1].split("=")[0])
        if len(w0parts) != 1 or w0parts[0][0] != "lit":
            raise _Stop("command-word", "a command word that holds a quote or an expansion")
        w0 = w0parts[0][1]
        if "/" in w0:
            raise _Stop("command-path", "%s, a program named by its path" % w0[:120])
        if w0 in _G_RESERVED:
            raise _Stop("reserved", "the shell word %s" % w0)
        if w0 not in self.table:
            raise _Stop("program", "%s, which is not a program of the list" % w0[:120])
        row = self.table[w0]
        kind = row["operands"]
        status_ok = w0 in ("echo", "printf")
        args = [self.text(p, env, status_ok) for p in words[1:]]
        submission = w0 == "python3" and args[1:] == list(GRAMMAR_SUBMISSION)
        if cmd["heredoc"] and (not submission or loop):
            raise _Stop("heredoc", "a heredoc on %s, which is not the verdict submission" % w0)
        cwd_after = None
        if kind == "cd":
            if not first or pipe or loop:
                raise _Stop("cd", "a cd that is not the first command of the call, or a cd in a "
                            "pipeline or a loop")
            if len(args) != 1 or args[0].rstrip("/") != self.given.rstrip("/"):
                raise _Stop("cd", "cd %s, which is not your try folder as the brief gives it"
                            % " ".join(args)[:120])
            cwd_after = self.tf
        elif kind == "client":
            full = None
            if args and ".." not in args[0].split("/"):
                full = os.path.normpath(args[0] if os.path.isabs(args[0])
                                        else os.path.join(self.cwd, args[0]))
            if full != self.client:
                raise _Stop("client", "python3 runs only packet/bin/rerun of your try folder")
        elif kind == "none":
            if args:
                raise _Stop("operand", "%s takes no word" % w0)
        elif kind == "echo":
            pass
        elif kind == "printf":
            if args and args[0].startswith("-") and args[0] != "--":
                raise _Stop("option", "printf takes no option, and -v sets a variable")
        elif kind == "test":
            self.test(args)
        else:
            self.program(w0, row, kind, args)
        for r in cmd["redirs"]:
            if r[0] == "dup":
                continue
            _t, _fd, op, parts = r
            target = self.text(parts, env, False)
            if op == "<":
                self.place(target)
                continue
            if target == "/dev/null":
                continue
            p = self.place(target)
            if not _real(p).startswith(self.out_real + os.sep):
                raise _Stop("write", "a write to %s, which is not under out/" % target[:120])
        if cwd_after is not None:
            self.cwd = cwd_after

    def test(self, args: list):
        """`test` read by its word count, as POSIX `test` reads two and three arguments (t-296,
        design 25.4): `OP X` with a file operator places X, with `-n` or `-z` places nothing;
        `X OP Y` with `-nt`, `-ot` or `-ef` places X and Y, with a comparison places nothing. Any
        other operator or count (`-a`, `-o`, `!`, an unknown dash word, one word, four or more) is
        refused with the rule `test`, whose words give the `-e` form and the `&&` join."""
        n = len(args)
        if n == 2:
            op, word = args
            if op in _G_TEST_UNARY:
                self.place(word)
            elif op not in _G_TEST_STRING:
                raise _Stop("test", "%s is not an operator of the test row" % op[:40])
        elif n == 3:
            left, op, right = args
            if op in _G_TEST_BINARY:
                self.place(left)
                self.place(right)
            elif op not in _G_TEST_COMPARE:
                raise _Stop("test", "%s is not an operator of the test row" % op[:40])
        else:
            raise _Stop("test", "test of %d words; one check of two or three words" % n)

    def program(self, name: str, row: dict, kind: str, args: list):
        if name == "diff":
            for a in args:
                if a == "--":
                    break
                if a == "--recursive" or (a.startswith("-") and not a.startswith("--")
                                          and "r" in a[1:].split("U")[0]):
                    raise _Stop("folder", "diff %s compares two folders" % a[:40])
        opt = _g_options(name, args, row)
        ops = opt["operands"]
        if kind == "text":
            return
        if kind == "files":
            self.files(name, ops, folders=False)
        elif kind == "folders":
            self.files(name, ops, folders=True)
        elif kind == "paths":
            self.files(name, ops, folders=True)
            if not ops:
                self.place(".")
        elif kind == "uniq":
            if len(ops) > 1:
                raise _Stop("operand", "uniq takes at most one file; its second file is written")
            self.files(name, ops, folders=False)
        elif kind == "sed":
            scripts = opt["vals"].get("e", [])
            if not scripts:
                if not ops:
                    raise _Stop("operand", "sed with no script")
                scripts, ops = [ops[0]], ops[1:]
            for s in scripts:
                why = self.sed_problem(s)
                if why:
                    raise _Stop("sed", why)
            self.files(name, ops, folders=False)
        elif kind == "search":
            pats = opt["vals"].get("e", [])
            if pats or "--files" in opt["longs"]:
                paths = ops
            else:
                if not ops:
                    raise _Stop("operand", "%s with no pattern" % name)
                paths = ops[1:]
            recursive = name == "rg" or "r" in opt["flags"]
            self.files(name, paths, folders=recursive)
            if recursive and not [p for p in paths if p != "-"]:
                self.place(".")

    # -- the call
    def run(self, items: list):
        total = 0
        for _sep, node in items:
            if node[0] == "pipe":
                total += len(node[1])
            else:
                total += max(len(node[2]), 1) * sum(len(n[1]) for _s, n in node[3])
        if total > GRAMMAR_MAX_COMMANDS:
            raise _Stop("too-long", "%d commands, more than the %d a call may hold"
                        % (total, GRAMMAR_MAX_COMMANDS))
        first = True
        for _sep, node in items:
            if node[0] == "pipe":
                for ci, cmd in enumerate(node[1]):
                    self.command(cmd, {}, first=first and ci == 0, pipe=len(node[1]) > 1,
                                 loop=False)
                    first = False
                continue
            _k, name, words, body = node
            values = []
            for w in words:
                if any(k not in ("lit", "sq", "dq") for k, _v in w):
                    raise _Stop("loop", "a loop word with a $ in it")
                v = "".join(t for _k2, t in w)
                if v == "" or any(c not in PLAIN_CHARS for c in v):
                    raise _Stop("loop", "the loop word %r, which is not plain characters" % v[:80])
                values.append(v)
            if not values:
                raise _Stop("loop", "a for loop with no words")
            for v in values:
                for _s, n in body:
                    for cmd in n[1]:
                        self.command(cmd, {name: v}, first=False, pipe=len(n[1]) > 1, loop=True)
            first = False


def _unwrapped_text(text: str) -> str:
    """A Codex `bash -lc '<text>'` call as the text it runs (`unwrap_literal`, on the text)."""
    for _ in range(8):
        got = parse(text)
        cmds = got.get("commands") or []
        if len(cmds) == 1 and not cmds[0]["redirects"] and not cmds[0]["heredocs"]:
            argv = cmds[0]["argv"]
            if (len(argv) == 3 and argv[0] in _WRAPPERS and argv[1] in ("-c", "-lc", "-lic", "-ic")
                    and _literal_script(got, cmds[0])):
                text = argv[2]
                continue
        break
    return text


def grammar_check(text: str, *, table: dict | None = None, try_folder: str, start: str | None = None,
                  exact_files: tuple = (), sed_problem=None) -> dict | None:
    """None for a call in the grammar of section 19, else the first refusal as {"rule", "why",
    "at"}, `at` the index of the simple command it stopped at (a loop body counted once for each
    loop word). It reads only the text, the try folder, the folder the call starts in and, for
    the link, folder and existence tests of 19.6, the disk. `exact_files` are files allowed as
    exactly that file (the prompt file and the persisted outputs, 18.1a)."""
    run = _GrammarRun(VERIFIER_TABLE if table is None else table, try_folder, start, exact_files,
                      sed_problem)
    try:
        items, _pos = _g_list(_g_tokens(str(text)), 0, False)
        run.run(items)
    except _Stop as stop:
        return {"rule": stop.rule, "why": stop.why, "at": max(run.at - 1, 0)}
    return None
