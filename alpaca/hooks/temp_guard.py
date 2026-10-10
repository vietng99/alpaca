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

from alpaca.hooks import common

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")"}
WRAPPERS = {"timeout", "env", "nice", "time", "stdbuf", "exec", "uv", "run", "poetry", "pipenv",
            "sudo", "command"}
SHELLS = {"bash", "sh", "zsh", "dash"}


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_heredocs(command):
    """The command without the bodies of its here-documents: a `<<EOF ... EOF` body is data fed
    to a program (a file being written, a script for python), not commands this shell runs."""
    out, ends = [], []
    for line in command.split("\n"):
        if ends:
            if line.strip() == ends[0]:
                ends.pop(0)
            continue
        out.append(line)
        ends.extend(m.group(2) for m in HEREDOC.finditer(line.replace("<<<", "")))
    return "\n".join(out)


def _split(command, depth=0):
    """[(separator_before, words)] for each simple command, in order. `bash -c '<script>'` is
    opened up so the commands inside it count too."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    out, seg, sep = [], [], ";"
    for tok in lexer:
        if tok in SEPARATORS or set(tok) <= set(";&|()\n"):
            if seg:
                out.append((sep, seg))
            seg, sep = [], tok
        else:
            seg.append(tok)
    if seg:
        out.append((sep, seg))
    if depth < 2:
        opened = []
        for sep, words in out:
            if words and os.path.basename(words[0]) in SHELLS and "-c" in words[:3]:
                i = words.index("-c")
                if i + 1 < len(words):
                    inner = _split(words[i + 1], depth + 1)
                    if inner:
                        opened.append((sep, inner[0][1]))
                        opened.extend(inner[1:])
                        continue
            opened.append((sep, words))
        out = opened
    return out


def _is_pytest(seg):
    """True when the command word runs pytest, past env assignments and wrappers such as
    `timeout 300` or `uv run`, and past interpreter flags (`python -X dev -m pytest`)."""
    i = 0
    while i < len(seg):
        w = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w) or os.path.basename(w) in WRAPPERS \
                or (w.startswith("-") and i > 0) or re.match(r"^\d+[smhd]?$", w):
            i += 1
            continue
        break
    if i >= len(seg):
        return False
    base = os.path.basename(seg[i])
    if base in ("pytest", "py.test"):
        return True
    if "python" not in base:
        return False
    j = i + 1
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
            return sep != "&&"
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
