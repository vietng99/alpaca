"""Reader of the saved `--help` texts that pathscan.OPTION_VALUES is checked against
(in-session-runtime.md section 18.1b).

The texts under alpaca/tests/fixtures/research/help/ were saved from the versions on the host that
built the table: ripgrep 14.1.1 (`rg -h`, with its bullet characters not part of the option lines),
ugrep 7.8.4 (`grep --help`, the `grep` of a Claude Code shell), uutils coreutils 0.8.0, GNU diffutils
3.12 and file 5.46. Three places differ from what the programs print, so that no shipped file holds a mailbox or a text the release barrier reads as one: the bug report line of cmp and diff names no address, the author line of rg has an example.org address, and the ugrep example of `-@` is reworded (the barrier's joined-line scan reads `-@` and the words after it as an address). No test reads them. `value_options` gives, for one program, which short letters and long names the
text says take a value:

* `-A NUM`, `--after-context=NUM`, `--lines <lines>` and `--magic-file LIST` take one;
* `--color[=WHEN]` and `-Q[=DELAY]` take one only after `=`, which is one word, so the table leaves
  them out, and here they are False;
* a short letter that shares its line with a long name (`-A, --after-context=NUM`) takes the value
  the last name of the line takes when that is required.
"""
from __future__ import annotations

import os
import re

HELP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "research", "help")
_LINE = re.compile(r"^\s{1,8}(-.*?)(?:\s{2,}.*)?$")
_ITEM = re.compile(r"^(--?[A-Za-z0-9][A-Za-z0-9-]*)(.*)$")


def _kind(rest: str) -> str | None:
    """None: no value; "optional": `[=X]`; "required": `=X`, ` X`, ` <x>`."""
    if not rest:
        return None
    if rest.startswith("["):
        return "optional"
    if rest.startswith("=") or rest.startswith(" "):
        return "required" if rest.strip(" =") else None
    return None


def value_options(prog: str) -> dict:
    """{"short": {letter: True or False}, "long": {name: True or False}} for every option the
    saved help text of `prog` lists: True when it takes a value."""
    with open(os.path.join(HELP, prog + ".txt"), encoding="utf-8") as fh:
        text = fh.read()
    short, long_ = {}, {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        items = []
        for part in re.split(r",\s+(?=-)", m.group(1).strip()):
            mi = _ITEM.match(part.strip())
            if mi:
                items.append((mi.group(1), _kind(mi.group(2))))
        if not items:
            continue
        shared = items[-1][1] == "required"
        for name, kind in items:
            takes = kind == "required" or (kind is None and shared)
            if name.startswith("--"):
                long_[name[2:]] = takes
            elif len(name) == 2:
                short[name[1]] = takes
    return {"short": short, "long": long_}
