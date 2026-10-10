"""The no-op and random-edit control agent. Standard library only: it is copied to
<home>/bin/control_agent.py and run in the jail, so it never imports alpaca.

    python3 <home>/bin/control_agent.py noop|random --role R --seed N [--mutate F]

It stands in for the proposer, implementer and notes roles of a control run (FR-044, gap map F19,
F20). It reads its prompt from stdin, as the launcher sends it, and prints JSON events on stdout:
one `system` event, then one `result` event whose `result` is the final message (the launcher
reads the final message of a control launch from that event, launcher._final_message).

- proposer: a proposal of the base fields of the prompt's mode and every required pack field the
  prompt lists, built from the trusted part of the prompt only (every untrusted block is cut out
  first). `attempt` is the id after the last engine marker; `category` the slot's category the
  prompt names; `files` (and `suspects` in repair mode) one editable, unprotected file of the
  read-only checkout in the working directory, picked with the attempt's seed. The engine checks
  it with proposal.check, like any proposal, and t-102's tests run proposal.check on what this
  script writes, so its shape and the engine's stay one.
- implementer: `noop` changes nothing. `random` picks the same file with the same seed and runs
  the pack's mutation script (research.controls.mutate, frozen, passed as --mutate) once in the
  working directory, the attempt's worktree: `<mutate> <file> <seed>` (the file relative to the
  worktree, the attempt's seed as a decimal integer), with the same two values in the environment
  variables ALPACA_CONTROL_FILE and ALPACA_CONTROL_SEED, and ALPACA_WORKTREE. This is the interface
  a pack's mutation script is written against (the sortbench fixture's bench/mutate.py reads its
  file and seed from argv). Without --mutate it refuses and changes nothing (F20: no mutation
  without the pack's operators). The mutation's syntax check (research.controls.syntax) is a gate
  stage, so the engine runs it in the gate chain after the implementer, never here.
- notes: one line on what the control attempt did.

The attempt's seed is the --seed value (research.controls.seed plus the slot) mixed with the
attempt id, so the slots of a round and the rounds of a slot try different files and mutations,
and a rerun of one attempt repeats them.

ENGINE_MARKER and parse_attempt_id restate texts.ENGINE_MARKER and controls.parse_attempt_id;
UNTRUSTED_BEGIN and UNTRUSTED_END restate texts.py's markers. t-102's tests compare them.

Design: design/research-engine/architecture.md sections 3.9 and 6.4; spec FR-044, EC-053."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import random
import re
import subprocess
import sys

ENGINE_MARKER = "ALPACA-RESEARCH-ATTEMPT:"            # restates texts.ENGINE_MARKER; a test compares
UNTRUSTED_BEGIN = "<<<ALPACA-UNTRUSTED-BEGIN>>>"      # restates texts.UNTRUSTED_BEGIN
UNTRUSTED_END = "<<<ALPACA-UNTRUSTED-END>>>"          # restates texts.UNTRUSTED_END
ATTEMPT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")   # restates controls.ATTEMPT_ID
ROLES = ("proposer", "implementer", "notes")
MODES = ("noop", "random")

_MAX_FILES = 20000           # files of the working directory looked at when picking one
_MUTATE_TIMEOUT_S = 600      # the mutation script's own limit; the launch timeout stops it first
_NOTE_CHARS = 300            # texts.NOTE_MAX_CHARS, when the notes prompt names no limit
_BASE_FIELDS = ("attempt", "title", "category", "hypothesis", "files", "suspects", "evidence",
                "expected")
_FIELD_LINE = re.compile(r"^- ([a-z][a-z0-9_]*) \(([a-z]+), (required|optional)"
                         r"(?:, one of: ([^)]*))?\)")
_USAGE = 64


class _Usage(Exception):
    """A command line the script does not take."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Usage(message)


def parse_attempt_id(prompt: str) -> str:
    """The attempt task id after the last ENGINE_MARKER in prompt: the first word on the rest of
    that marker's line. Raises ValueError when the prompt is not text, holds no marker, or the
    word after the last marker is not an attempt id. The same rule as controls.parse_attempt_id."""
    if not isinstance(prompt, str):
        raise ValueError("a prompt is text, not %s" % type(prompt).__name__)
    at = prompt.rfind(ENGINE_MARKER)
    if at < 0:
        raise ValueError("the prompt holds no engine marker")
    rest = prompt[at + len(ENGINE_MARKER):]
    line = rest.splitlines()[0] if rest else ""
    words = line.split()
    if not words or not ATTEMPT_ID.fullmatch(words[0]):
        raise ValueError("the last engine marker names no attempt id: %r" % line[:80])
    return words[0]


# ------------------------------------------------------------------------------ the prompt
def trusted_text(prompt: str) -> str:
    """The prompt with every untrusted block (UNTRUSTED_BEGIN up to its UNTRUSTED_END, both
    included) cut out; an unclosed block is cut to the end. Only this text is read for the
    category, the paths, the metrics and the fields, so no repository or notes text can steer
    them."""
    out, at = [], 0
    while True:
        start = prompt.find(UNTRUSTED_BEGIN, at)
        if start < 0:
            out.append(prompt[at:])
            break
        out.append(prompt[at:start])
        end = prompt.find(UNTRUSTED_END, start + len(UNTRUSTED_BEGIN))
        if end < 0:
            break
        at = end + len(UNTRUSTED_END)
    return "".join(out)


def _section(text: str, heading: str) -> list:
    """The `- ` lines right after the line that ends with heading, up to the first other line."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.rstrip().endswith(heading):
            out = []
            for item in lines[i + 1:]:
                if not item.startswith("- "):
                    break
                out.append(item)
            return out
    return []


def read_prompt(prompt: str) -> dict:
    """What the control agent takes from the trusted part of a proposer, implementer or notes
    prompt: mode, category, metrics, editable and protected paths, the field lines and the note
    limit."""
    text = trusted_text(prompt)
    mode = re.search(r"\bmode (improve|repair)\b", text)
    category = re.search(r"^Your hypothesis category for this slot is (\S+?):", text, re.M)
    cat = category.group(1) if category else None
    metrics = []
    for line in _section(text, "The metrics the run measures:"):
        found = re.match(r"^- (\S+?): ", line)
        if found:
            metrics.append(found.group(1))
    limit = re.search(r"at most (\d+)\s+characters", text)
    return {"mode": mode.group(1) if mode else "improve",
            "category": None if cat in (None, "(none)") else cat,
            "metrics": metrics,
            "editable": [line[2:].strip() for line in _section(text, "editable paths:")
                         if line[2:].strip() != "(none)"],
            "protected": [line[2:].strip() for line in _section(text, "protected paths:")
                          if line[2:].strip() != "(none)"],
            "fields": _section(text, "with these fields:"),
            "note_chars": int(limit.group(1)) if limit else _NOTE_CHARS}


# ------------------------------------------------------------------------------ paths
def admitted(path: str, rules) -> str | None:
    """The editable rule that admits path, read the way runbook.path_admitted reads it: from the
    root, case kept, `*` and `?` within one part, a folder rule admits everything under it, any
    other rule exactly one path."""
    parts = path.split("/")
    for rule in rules:
        r = rule.replace("\\", "/")
        while r.startswith("./"):
            r = r[2:]
        body = r.rstrip("/")
        if not body:
            continue
        want = body.split("/")
        if len(parts) < len(want) or (not r.endswith("/") and len(parts) != len(want)):
            continue
        if all(fnmatch.fnmatchcase(parts[k], want[k]) for k in range(len(want))):
            return rule
    return None


def protected(path: str, rules) -> str | None:
    """The protected rule that hits path, read the way barrier.path_is_protected reads it: case
    does not matter, a folder rule hits that folder at the root and, when it names one folder, a
    folder of that name at any depth, a bare name hits any part, a rule with an inner slash is a
    path from the root."""
    low = path.lower()
    parts = low.split("/")
    for rule in rules:
        r = rule.replace("\\", "/")
        if r.startswith("./"):
            r = r[2:]
        rl = r.lower()
        if rl.endswith("/"):
            body = rl.rstrip("/")
            if low == body or low.startswith(body + "/") or fnmatch.fnmatchcase(low, body):
                return rule
            if "/" not in body and any(fnmatch.fnmatchcase(c, body) for c in parts[:-1]):
                return rule
        elif "/" in rl:
            if fnmatch.fnmatchcase(low, rl):
                return rule
        elif any(fnmatch.fnmatchcase(c, rl) for c in parts):
            return rule
    return None


def candidates(cwd: str, editable, protect) -> list:
    """The regular files under cwd (no link followed, `.git` skipped), relative with `/`, that an
    editable rule admits and no protected rule hits, sorted; at most _MAX_FILES are looked at."""
    out, seen = [], 0
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = sorted(d for d in dirnames if d != ".git"
                             and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            seen += 1
            if seen > _MAX_FILES:
                return sorted(out)
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, cwd).replace(os.sep, "/")
            if admitted(rel, editable) and not protected(rel, protect):
                out.append(rel)
    return sorted(out)


def _from_rule(rule: str) -> str:
    """A path a rule admits, made from the rule itself: a glob character becomes `x`, a folder
    rule gets the file `control.txt`."""
    body = re.sub(r"\[[^\]]*\]", "x", rule.replace("\\", "/"))
    body = body.replace("*", "x").replace("?", "x")
    while body.startswith("./"):
        body = body[2:]
    if body.endswith("/"):
        return body.rstrip("/") + "/control.txt"
    return body


def pick(cwd: str, editable, protect, seed: int) -> str | None:
    """One editable, unprotected path: a file of cwd chosen with seed, else a path made from an
    editable rule; None when no rule gives one."""
    found = candidates(cwd, editable, protect)
    if found:
        return random.Random(seed).choice(found)
    for rule in editable:
        path = _from_rule(rule)
        if path and admitted(path, editable) and not protected(path, protect):
            return path
    return None


def attempt_seed(seed: int, attempt: str) -> int:
    """The attempt's seed: --seed mixed with the attempt id."""
    digest = hashlib.sha256(("%d:%s" % (int(seed), attempt)).encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


# ------------------------------------------------------------------------------ the roles
def _pack_value(kind: str, values: str | None, path: str | None):
    if kind == "enum":
        first = (values or "").split(", ")[0].strip()
        return first or "control"
    if kind == "int":
        return 0
    if kind == "float":
        return 0.0
    if kind == "bool":
        return False
    if kind == "list":
        return []
    if kind == "paths":
        return [path] if path else []
    if kind == "str":
        return "control"
    return "control attempt"


def proposal(prompt: str, *, mode: str, cwd: str, seed: int) -> dict:
    """The control proposal for a proposer prompt (see the module docstring)."""
    attempt = parse_attempt_id(prompt)
    seen = read_prompt(prompt)
    path = pick(cwd, seen["editable"], seen["protected"], attempt_seed(seed, attempt))
    if mode == "noop":
        title = "no-op control: change nothing"
        why = ("Control attempt: the no-op control changes nothing, so any gain the gates "
               "report for it is noise or a broken gate.")
    else:
        title = "random-edit control: mutate %s" % (path or "an editable file")
        why = ("Control attempt: the random-edit control applies a seeded mutation from the "
               "pack's operators, so an accepted result means a gate or the accept rule is "
               "broken.")
    doc = {"attempt": attempt, "title": title[:120], "hypothesis": why,
           "files": [path] if path else []}
    names = set()
    for line in seen["fields"]:
        found = re.match(r"^- ([a-z][a-z0-9_]*) \(", line)
        if found:
            names.add(found.group(1))
    if "category" in names and seen["category"]:
        doc["category"] = seen["category"]
    if seen["mode"] == "repair":
        doc["suspects"] = [path] if path else []
        doc["evidence"] = "Control attempt: no localization; the file is a seeded pick."
    else:
        doc["expected"] = {m: 0 for m in seen["metrics"]}
    for line in seen["fields"]:
        found = _FIELD_LINE.match(line)
        if not found or found.group(1) in _BASE_FIELDS or found.group(3) != "required":
            continue
        doc[found.group(1)] = _pack_value(found.group(2), found.group(4), path)
    return doc


def implement(prompt: str, *, mode: str, cwd: str, seed: int, mutate: str | None) -> tuple:
    """(exit code, final message) of the implementer role (see the module docstring)."""
    attempt = parse_attempt_id(prompt)
    if mode == "noop":
        return 0, "no-op control: changed nothing in %s" % attempt
    if not mutate:
        return 2, "random-edit control: no mutation script was given, so nothing was mutated"
    seen = read_prompt(prompt)
    own = attempt_seed(seed, attempt)
    path = pick(cwd, seen["editable"], seen["protected"], own)
    if path is None:
        return 2, "random-edit control: no editable file to mutate"
    env = dict(os.environ)
    env.update(ALPACA_CONTROL_SEED=str(own), ALPACA_CONTROL_FILE=path, ALPACA_WORKTREE=cwd)
    try:
        done = subprocess.run([mutate, path, str(own)], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=_MUTATE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "random-edit control: the mutation script did not run: %s" % exc
    if done.returncode != 0:
        return 1, "random-edit control: %s exited %d on %s" % (
            os.path.basename(mutate), done.returncode, path)
    return 0, "random-edit control: mutated %s with %s, seed %d" % (
        path, os.path.basename(mutate), own)


def note(prompt: str, *, mode: str) -> str:
    """The one notes line."""
    attempt = parse_attempt_id(prompt)
    limit = read_prompt(prompt)["note_chars"]
    what = "changed nothing" if mode == "noop" else "applied one seeded mutation"
    line = "control (%s) attempt %s %s" % (mode, attempt, what)
    return line[:max(1, limit)]


# ------------------------------------------------------------------------------ main
def _emit(final: str, *, error: bool, mode: str) -> None:
    out = sys.stdout
    out.write(json.dumps({"type": "system", "subtype": "init",
                          "model": "alpaca-control-%s" % mode}, sort_keys=True) + "\n")
    out.write(json.dumps({"type": "result", "subtype": "error" if error else "success",
                          "is_error": bool(error), "result": final,
                          "usage": {"input_tokens": 0, "output_tokens": 0},
                          "total_cost_usd": 0}, sort_keys=True) + "\n")
    out.flush()


def main(argv: list[str] | None = None) -> int:
    """control_agent.py noop|random --role R --seed N [--mutate F]: read the prompt from stdin,
    act for the role in the working directory and print the events. Returns 0, 1 when the role
    could not do its work (the result says why), 2 for a random implementer with no mutation
    script or no file, and 64 for a command line it does not take."""
    parser = _Parser(prog="control_agent.py", add_help=False)
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("--role", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mutate", default=None)
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    except _Usage as exc:
        sys.stderr.write("control_agent.py: %s\n" % exc)
        return _USAGE
    if args.role not in ROLES:
        _emit("the control agent stands in for %s only, not %r" % (", ".join(ROLES), args.role),
              error=True, mode=args.mode)
        return _USAGE
    prompt = sys.stdin.read()
    cwd = os.getcwd()
    try:
        if args.role == "proposer":
            doc = proposal(prompt, mode=args.mode, cwd=cwd, seed=args.seed)
            _emit(json.dumps(doc, sort_keys=True), error=False, mode=args.mode)
            return 0
        if args.role == "implementer":
            code, final = implement(prompt, mode=args.mode, cwd=cwd, seed=args.seed,
                                    mutate=args.mutate)
            _emit(final, error=False, mode=args.mode)
            return code
        _emit(note(prompt, mode=args.mode), error=False, mode=args.mode)
        return 0
    except ValueError as exc:
        _emit("control agent: %s" % exc, error=True, mode=args.mode)
        return 1


if __name__ == "__main__":
    sys.exit(main())
