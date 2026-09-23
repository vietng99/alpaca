"""The proof report: what a task hands in when it claims to be done (E4).

A proof is not a path. It is the report an engineer writes at the end of a piece of work: what
was done, how it was done, where it landed, what came out, what went wrong on the way, how to
run it again, and the pointers that back every line of it. This module owns the three steps and
the gate that reads them.

    alpaca proof new <id>     scaffold `.alpaca/proofs/<op>/<id>.md`. The header and Appendix A
                             are filled from the record and the tool pool; the seven sections a
                             person writes start with one `TODO(agent):` line each.
    alpaca proof seal <id>    read the report back, refuse a missing, placeholder or thin
                             section, resolve and hash every evidence pointer, keep a copy of
                             every `local:` evidence file beside the report, and append ONE
                             `proof-report` event carrying the report hash and the evidence
                             hashes. Sealing never edits the report bytes.
    alpaca proof check [<id>] re-hash the report and every sealed evidence file and print one
                             GATE line. PASS when the report still matches and every evidence
                             file either still matches or has its kept copy intact.

A report cites files that other work goes on editing. Hashing only the path the report named
would call the report false a week later, when what really happened is that the tree moved on.
So the seal keeps the attachment: every `local:` evidence file is copied into
`.alpaca/proofs/<op>/<id>.evidence/<nn>-<basename>` and its `kept` path is recorded beside its
hash. A later check reads the original first; when the original has changed or gone and the kept
copy still hashes to the sealed value, that is a note and not a problem. The proof still stands,
and the evidence as it was is still on disk.

`alpaca task move <id> done --proof local:<report>` then asks the whole question: does the latest
`proof-report` event for this id name that path, does the file still hash to the sealed value,
and does every sealed evidence file still hash to the value the seal recorded. A report that
still matches over evidence that is gone closes nothing. A `remote:` ref no longer closes a task
on its own; it belongs inside the report's
Evidence list, where it is recorded unverified and the report says what it stands for.

Re-sealing after an edit appends a new event; the latest one wins, so a correction is a new
record row, never a rewrite of an old one.

Interfaces:

    scaffold(conn, root, ident, *, force=False, session=None) -> dict
    seal(conn, root, ident, *, session=None) -> dict          (raises Refusal)
    fence_scan(text) -> [(line, inside_a_code_fence)]
    prose_len(text) -> int                                    the floor counts this, not bytes
    kept_dir(root, subj) -> str                               where the copies live
    keep_evidence(root, subj, evidence) -> dict               writes them, fills `kept`
    verify(conn, root, ident) -> (ok, problems)
    verify_detail(conn, root, ident) -> {"ok", "problems", "notes"}
    latest_seal(conn, ident) -> event | None
    done_gate(conn, root, ident, pointer) -> (ok, problems)
    next_steps(ident) -> [str, str, str]
    checks(root, conn) -> [(name, level, detail)]             the doctor form
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess

from alpaca import db, paths, util
from alpaca.gates import contract
from alpaca.gates import verdict as vc

#: the record event one seal appends.
KIND = "proof-report"

#: the placeholder line a scaffolded section carries until a person replaces it.
TODO_MARK = "TODO(agent):"

#: the six sections a person writes as prose, in the order the report carries them.
NARRATIVE = ("What I did", "How I did it", "Where", "Result", "Deviations and issues",
             "How to reproduce")
#: the pointer list, one pointer per line.
EVIDENCE = "Evidence"
#: the tool-filled section. A person does not write this one.
APPENDIX = "Appendix A. Mechanical extract"
#: every section a seal demands.
REQUIRED = NARRATIVE + (EVIDENCE,)
#: the whole heading order, as `proof new` writes it.
SECTIONS = NARRATIVE + (EVIDENCE, APPENDIX)

#: the floor for a narrative section, counted in non-space characters OUTSIDE code fences and
#: HTML comments. Below this the section is a gesture at an answer rather than one, and a pasted
#: log on its own never reaches it.
MIN_CHARS = 40

#: how many commands Appendix A prints before it says how many it left out.
COMMAND_CAP = 200
#: the same cap for edited files and for record event ids listed per kind.
FILE_CAP = 200
ID_CAP = 24

#: the pointer schemes an Evidence line may carry. Everything but `remote:` is checkable here.
#: `receipt:` and `job:` name a domain run; the domain profile resolves them to a file
#: (alpaca/profile.py `evidence_file`), and without a profile they resolve to nothing.
POINTER_KINDS = ("local", "event", "receipt", "job", "remote")
UNVERIFIABLE = ("remote",)

#: the directory that holds the kept copies for one report, beside the report itself.
KEPT_SUFFIX = ".evidence"
#: what one kept copy may weigh, and what all of them together may weigh for one report. A file
#: over either cap is hashed and recorded with `kept: null`, the way a seal behaved before the
#: copies existed: a proof directory is not a place to park a gigabyte of waveform.
KEPT_FILE_CAP = 8 * 1024 * 1024
KEPT_TOTAL_CAP = 64 * 1024 * 1024
#: the `kept_reason` recorded beside `kept: null` for a file the caps left out.
OVER_CAP = "over cap"

_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*$")
_TEST_CMD = re.compile(r"\b(pytest|unittest|make\s+test|bin/alpaca\s+verify|tox|nose)\b")
#: a fence line: up to three leading spaces, then three or more backticks or tildes.
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*(.*?)[ \t]*$")
#: an HTML comment, across lines. Pasted or commented-out text, not the writer's own words.
_COMMENT = re.compile(r"<!--.*?-->", re.S)

_GIT_CACHE = {}


class Refusal(Exception):
    """A seal or a scaffold that cannot proceed. `problems` names every reason, not the first.
    `blocked` marks a refusal the report cannot fix: the domain profile that must check the run
    ledger did not load or did not answer, so the seal waits (BLOCKED) rather than fails."""

    def __init__(self, problems, blocked=False):
        self.problems = list(problems)
        self.blocked = bool(blocked)
        super().__init__("; ".join(self.problems))


# --------------------------------------------------------------------------- the subject
def subject(conn, ident) -> dict:
    """Resolve `ident` to the thing the report is about: a tracker task or a checklist row.

    Returns {"kind", "id", "statement", "op", "done_when", "status"}. An id that is neither
    raises Refusal, so a typo never scaffolds an orphan report."""
    rows = db.rows(conn, "tasks", "id=?", (ident,))
    if rows:
        t = rows[0]
        return {"kind": "task", "id": t["id"], "statement": t.get("statement"),
                "op": t.get("op"), "done_when": _done_when(conn, t.get("op")),
                "status": t.get("status")}
    rows = db.rows(conn, "rows", "id=?", (ident,))
    if rows:
        r = rows[0]
        return {"kind": "row", "id": r["id"], "statement": r.get("statement"),
                "op": r.get("op"), "done_when": _done_when(conn, r.get("op")),
                "status": r.get("status")}
    raise Refusal(["no task and no checklist row with id %r" % ident])


def _done_when(conn, op):
    if not op:
        return None
    rows = db.rows(conn, "ops", "id=?", (op,))
    return rows[0].get("done_when") if rows else None


def proofs_dir(root) -> str:
    return os.path.join(paths.runtime_dir(root), "proofs")


def report_path(root, subj) -> str:
    """`.alpaca/proofs/<op>/<id>.md`. A task with no op lands under `unassigned`."""
    return os.path.join(proofs_dir(root), subj.get("op") or "unassigned", "%s.md" % subj["id"])


def report_rel(root, subj) -> str:
    return _rel(root, report_path(root, subj))


def kept_dir(root, subj) -> str:
    """`.alpaca/proofs/<op>/<id>.evidence/`: the copies this id's seal keeps, beside its report."""
    return os.path.join(proofs_dir(root), subj.get("op") or "unassigned",
                        "%s%s" % (subj["id"], KEPT_SUFFIX))


def _rel(root, path) -> str:
    return os.path.relpath(os.path.abspath(path), os.path.abspath(root)).replace(os.sep, "/")


def _norm(p) -> str:
    s = str(p or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s


# --------------------------------------------------------------------------- the window
def referencing_events(conn, ident) -> list:
    """Every record event that names `ident`: by `ref`, or by the row id a verdict binds."""
    out = []
    for e in db.events(conn, limit=10 ** 9):
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        binds = data.get("binds") if isinstance(data.get("binds"), dict) else {}
        if e.get("ref") == ident or binds.get("row_id") == ident:
            out.append(e)
    return out


def window(conn, ident) -> dict:
    """{"first", "now", "sessions"}: the first event that references the id, the clock now, and
    every session that wrote one of those events, in first-seen order."""
    evs = referencing_events(conn, ident)
    sessions = []
    for e in evs:
        sid = e.get("session")
        if sid and sid not in sessions:
            sessions.append(sid)
    return {"first": evs[0]["ts"] if evs else None, "now": util.now_iso(), "sessions": sessions,
            "events": evs}


def _git(root):
    """(head, branch, dirty_count). Empty strings and -1 when this root is not a git checkout.

    Only a `.git` directory in THIS root counts: walking up would report the enclosing
    repository, which is not the tree the report is about."""
    key = os.path.abspath(root)
    if key in _GIT_CACHE:
        return _GIT_CACHE[key]
    out = ("", "", -1)
    if os.path.isdir(os.path.join(root, ".git")):
        def run(*args):
            try:
                p = subprocess.run(("git",) + args, cwd=root, capture_output=True, text=True,
                                   encoding="utf-8", timeout=20)
            except (OSError, subprocess.SubprocessError):
                return None
            return p.stdout if p.returncode == 0 else None
        head = (run("rev-parse", "--short", "HEAD") or "").strip()
        branch = (run("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        status = run("status", "--porcelain")
        dirty = len([l for l in status.splitlines() if l.strip()]) if status is not None else -1
        out = (head, branch, dirty)
    _GIT_CACHE[key] = out
    return out


# --------------------------------------------------------------------------- the tool pool
def _pool_path(root, sid) -> str:
    return os.path.join(paths.runtime_dir(root), "pool", "tools", "%s.jsonl" % sid)


def _read_jsonl(path, cap=200000) -> list:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
                if len(out) >= cap:
                    break
    except OSError:
        return []
    return out


def _transcript_path(conn, root, sid):
    local = os.path.join(paths.transcript_dir(root), "%s.jsonl" % sid)
    if os.path.isfile(local):
        return local
    rows = db.rows(conn, "sessions", "sid=?", (sid,))
    registered = rows[0].get("transcript") if rows else None
    if registered and os.path.isfile(registered):
        return registered
    return None


_EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")


def _calls_from_pool(root, sid) -> list:
    """(tool, input) pairs from `.alpaca/pool/tools/<sid>.jsonl`, one per tool call.

    The pool writes a `pre` and a `post` line per call; both carry the same `tool_use_id`, so
    the pair folds to one call. A pool file that is not there yields nothing and the caller
    falls through to the transcript."""
    recs = _read_jsonl(_pool_path(root, sid))
    seen, out = {}, []
    for r in recs:
        tool = r.get("tool")
        if not tool:
            continue
        key = r.get("tool_use_id") or ("%s:%d" % (tool, len(out)))
        if key in seen:
            continue
        seen[key] = True
        out.append((str(tool), r.get("input") if isinstance(r.get("input"), dict) else {},
                    r.get("ts")))
    return out


def _calls_from_transcript(path) -> list:
    out = []
    for rec in _read_jsonl(path):
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        ts = rec.get("timestamp")
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                out.append((str(b.get("name") or "?"),
                            b.get("input") if isinstance(b.get("input"), dict) else {}, ts))
    return out


def _calls_from_record(events, sid) -> list:
    out = []
    for e in events:
        if e.get("kind") != "heartbeat" or e.get("session") != sid:
            continue
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        out.append((str(data.get("tool") or "?"), {"ref": data.get("ref")}, e.get("ts")))
    return out


def tool_calls(conn, root, sessions, events) -> dict:
    """{"source": str, "calls": [(tool, input, ts)]} over every session in the window.

    The pool is read first because it holds the full command and the tool response; then the
    transcript, which holds the tool input; then the record heartbeats, which hold a first word
    and a digest. The source is named in the report so a reader knows how much detail is behind
    each line."""
    used = []
    calls = []
    for sid in sessions:
        got = _calls_from_pool(root, sid)
        src = "pool"
        if not got:
            tp = _transcript_path(conn, root, sid)
            if tp:
                got = _calls_from_transcript(tp)
                src = "transcript"
        if not got:
            got = _calls_from_record(events, sid)
            src = "record heartbeats"
        if got:
            calls.extend(got)
            if src not in used:
                used.append(src)
    return {"source": ", ".join(used) if used else "none", "calls": calls}


def _commands_and_files(calls) -> tuple:
    commands, files = [], {}
    for tool, data, ts in calls:
        data = data or {}
        if tool == "Bash":
            cmd = str(data.get("command") or data.get("ref") or "").strip()
            if cmd:
                commands.append((cmd, ts))
        elif tool in _EDIT_TOOLS:
            fp = data.get("file_path") or data.get("notebook_path") or data.get("ref")
            if fp:
                files[str(fp)] = files.get(str(fp), 0) + 1
    return commands, files


# --------------------------------------------------------------------------- run evidence
def _run_file(root, kind, ident):
    """The file the profile names for a `receipt:` or `job:` pointer, kept inside the root, or
    None (alpaca/profile.py `evidence_file`)."""
    from alpaca import profile
    try:
        found = profile.load(root).evidence_file(root, kind, ident)
    except Exception:
        return None
    if not isinstance(found, str) or not found:
        return None
    full = found if os.path.isabs(found) else os.path.join(root, found)
    return full if _inside_root(root, os.path.realpath(full)) else None


# --------------------------------------------------------------------------- the scaffold
_PROMPT = {
    "What I did": "one paragraph, the change as a reader who was not here would need it",
    "How I did it": "the route you took: the commands, the edits, the order they ran in",
    "Where": "the files, paths, hosts and jobs this touched, named exactly",
    "Result": "the observed values against the done bar, numbers first",
    "Deviations and issues": "what did not go to plan, and what you did about it",
    "How to reproduce": "the shortest sequence another engineer can run to see the same thing",
    EVIDENCE: ("one pointer per line as a list item, each followed by ' - ' and what it shows: "
               "local:<path>, event:<id>, receipt:<id>, job:<id>, remote:<ref>"),
}


def _header(conn, root, subj, win) -> list:
    head, branch, dirty = _git(root)
    git_line = "not a git checkout"
    if head:
        git_line = "%s on %s, %s" % (head, branch or "(detached)",
                                     "%d dirty file(s)" % dirty if dirty >= 0 else "dirty count unavailable")
    return [
        "# Proof report %s" % subj["id"],
        "",
        "- id: %s (%s)" % (subj["id"], subj["kind"]),
        "- statement: %s" % (subj.get("statement") or "(none recorded)"),
        "- op: %s" % (subj.get("op") or "(none)"),
        "- done when: %s" % (subj.get("done_when") or "(the op records no done bar)"),
        "- sessions: %s" % (", ".join(win["sessions"]) if win["sessions"] else "(none on the record)"),
        "- window: %s .. %s" % (win["first"] or "(no event names this id yet)", win["now"]),
        "- host: %s" % (platform.node() or "(unknown)"),
        "- project: %s" % os.path.basename(os.path.abspath(root)),
        "- git: %s" % git_line,
        "",
        "Sections one to seven are written by the person or agent who did the work. Appendix A is "
        "filled by `alpaca proof new` and is not hand-edited. `alpaca proof seal %s` refuses this "
        "report while any section still carries its %s line, holds under %d non-space characters "
        "outside code fences and HTML comments (paste the log by all means, but write the section "
        "too), cites this report as its own evidence, or lists no evidence pointer that can be "
        "resolved." % (subj["id"], TODO_MARK, MIN_CHARS),
        "",
        "Sealing copies every `local:` evidence file into `%s/`, so the work can move on without "
        "the report losing what it stood on. A later check reads the file in the tree first and "
        "falls back to that copy." % _rel(root, kept_dir(root, subj)),
        "",
    ]


def appendix(conn, root, subj, win) -> list:
    """Appendix A, from the record and the pool. Deterministic given the record."""
    events = win["events"]
    sessions = win["sessions"]
    first = win["first"]
    scoped = [e for e in db.events(conn, limit=10 ** 9)
              if e.get("session") in sessions and (first is None or e["ts"] >= first)]
    lines = ["## %s" % APPENDIX, "",
             "Filled by `alpaca proof new` from the record and the tool pool, over the sessions and "
             "window in the header. Do not hand-edit: re-run the verb instead.", ""]

    lines += ["### Record events", ""]
    if not scoped:
        lines += ["No record event falls in this window.", ""]
    else:
        by_kind = {}
        for e in scoped:
            by_kind.setdefault(e["kind"], []).append(e["id"])
        lines += ["| kind | count | event ids |", "| --- | --- | --- |"]
        for kind in sorted(by_kind):
            ids = by_kind[kind]
            shown = ", ".join(str(i) for i in ids[:ID_CAP])
            if len(ids) > ID_CAP:
                shown += " (+%d more)" % (len(ids) - ID_CAP)
            lines.append("| %s | %d | %s |" % (kind, len(ids), shown))
        lines.append("")

    got = tool_calls(conn, root, sessions, scoped)
    commands, files = _commands_and_files(got["calls"])
    lines += ["### Commands run", "", "Source: %s." % got["source"], ""]
    if not commands:
        lines += ["No command was recorded for this window.", ""]
    else:
        for cmd, ts in commands[:COMMAND_CAP]:
            lines.append("- `%s`%s" % (_one_line(cmd), " (%s)" % ts if ts else ""))
        if len(commands) > COMMAND_CAP:
            lines.append("")
            lines.append("%d further command(s) ran and are not listed here; the pool and the "
                         "record hold all of them." % (len(commands) - COMMAND_CAP))
        lines.append("")

    lines += ["### Files edited", ""]
    if not files:
        lines += ["No file edit was recorded for this window.", ""]
    else:
        names = sorted(files)
        for name in names[:FILE_CAP]:
            lines.append("- %s (%d write(s))" % (_one_line(name), files[name]))
        if len(names) > FILE_CAP:
            lines.append("")
            lines.append("%d further file(s) were edited and are not listed here."
                         % (len(names) - FILE_CAP))
        lines.append("")

    # A domain profile names its runs in the window (alpaca/profile.py `proof_lines`); without one
    # the section is left out.
    from alpaca import profile
    found = profile.load(root).proof_lines(scoped)
    runs = [str(line) for line in found] if isinstance(found, (list, tuple)) else []
    if runs:
        lines += ["### Runs and receipts", ""] + runs + [""]

    tests = [c for c, _ts in commands if _TEST_CMD.search(c)]
    lines += ["### Test commands seen", ""]
    if not tests:
        lines += ["No test command was recorded for this window.", ""]
    else:
        for cmd in tests[:COMMAND_CAP]:
            lines.append("- `%s`" % _one_line(cmd))
        if len(tests) > COMMAND_CAP:
            lines.append("")
            lines.append("%d further test command(s) are not listed here."
                         % (len(tests) - COMMAND_CAP))
        lines.append("")
    return lines


def _one_line(text) -> str:
    return " ".join(str(text or "").split())[:400]


def render(conn, root, ident) -> str:
    """The scaffolded report as text. Pure: writes nothing."""
    subj = subject(conn, ident)
    win = window(conn, ident)
    lines = _header(conn, root, subj, win)
    for name in NARRATIVE + (EVIDENCE,):
        lines += ["## %s" % name, "", "%s %s" % (TODO_MARK, _PROMPT[name]), ""]
    lines += appendix(conn, root, subj, win)
    return "\n".join(lines).rstrip("\n") + "\n"


def scaffold(conn, root, ident, *, force=False, session=None) -> dict:
    """Write `.alpaca/proofs/<op>/<id>.md`. Refuses to overwrite an existing report unless
    `force`, so a written report is never lost to a second `proof new`."""
    subj = subject(conn, ident)
    path = report_path(root, subj)
    existed = os.path.isfile(path)
    if existed and not force:
        raise Refusal(["%s already exists; pass --force to overwrite it" % _rel(root, path)])
    util.write_text(path, render(conn, root, ident))
    return {"path": path, "rel": _rel(root, path), "op": subj.get("op"), "id": subj["id"],
            "overwrote": existed}


# --------------------------------------------------------------------------- reading it back
def fence_scan(text) -> list:
    """[(line, inside)] for every line of `text`: `inside` is True for a line that belongs to a
    fenced code block, both fence lines included.

    A fence opens on three or more backticks or tildes and closes only on a bare run of the SAME
    character at least as long, which is what CommonMark says. Anything between the two is code a
    person pasted, so a `## Result` line in there is body text and not a heading."""
    out = []
    char, size = None, 0
    for line in str(text or "").splitlines():
        m = _FENCE.match(line)
        if char is None:
            if m:
                char, size = m.group(1)[0], len(m.group(1))
                out.append((line, True))
                continue
            out.append((line, False))
            continue
        if m and m.group(1)[0] == char and len(m.group(1)) >= size and not m.group(2):
            char, size = None, 0
        out.append((line, True))
    return out


def split_sections(text) -> dict:
    """{heading: body} for every level-2 heading, in file order. The lines before the first
    heading are the header and are not a section. A heading inside a fenced code block is body
    text: it neither opens a section nor closes the one it sits in."""
    out = {}
    name, body = None, []
    for line, inside in fence_scan(text):
        m = None if inside else _HEADING.match(line)
        if m:
            if name is not None:
                out[name] = "\n".join(body)
            name, body = m.group(1).strip(), []
            continue
        if name is not None:
            body.append(line)
    if name is not None:
        out[name] = "\n".join(body)
    return out


def dense_len(text) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def prose_len(text) -> int:
    """The writer's own words in `text`, counted in non-space characters.

    A fenced code block and an HTML comment are material carried in from somewhere else, so they
    do not count towards the floor. A section holding one pasted log and nothing else therefore
    reads as zero, which is what it is."""
    kept = "\n".join(line for line, inside in fence_scan(text) if not inside)
    return dense_len(_COMMENT.sub(" ", kept))


def parse_evidence(body) -> list:
    """The Evidence section as a pointer list. One list item per pointer, an optional note after
    a plain hyphen. A line that is not a list item is ignored; a list item whose scheme is not a
    pointer scheme comes back with kind None so the seal can name it."""
    out = []
    for raw in str(body or "").splitlines():
        line = raw.strip()
        if not line or line[0] not in "-*":
            continue
        line = line[1:].strip()
        if not line:
            continue
        note = None
        if " - " in line:
            ptr, note = line.split(" - ", 1)
            ptr, note = ptr.strip(), note.strip()
        else:
            ptr = line
        ptr = ptr.strip().strip("`").strip()
        if not ptr:
            continue
        scheme = ptr.split(":", 1)[0].strip().lower()
        out.append({"pointer": ptr, "kind": scheme if scheme in POINTER_KINDS else None,
                    "note": note})
    return out


def _inside_root(root, full) -> bool:
    r = os.path.realpath(root) + os.sep
    return os.path.realpath(full).startswith(r)


def _blank_file(path) -> bool:
    """True when the file holds no byte that is not whitespace. Read in chunks and stopped at the
    first real byte, so a large file costs one read."""
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    return True
                if chunk.strip():
                    return False
    except OSError:
        return False


def _self_citation(root, ptr, real, report, ident):
    """The reason this `local:` pointer is the report citing itself, or None.

    A report that stands as its own evidence proves nothing: the seal would hash the same bytes
    twice and call it two sources. The report path, its `.seal.json` copy, any file under
    `.alpaca/proofs/` named for the SAME id, and anything inside this id's own `.evidence/`
    directory are all the report citing itself: the last of those is a copy this seal wrote from
    a pointer the report already carried. Another task's sealed report, and another task's kept
    copy, are evidence like any other file and pass."""
    if report:
        own = os.path.realpath(report)
        if real == own or real == os.path.realpath(report + ".seal.json"):
            return ("evidence %s is this report itself; a report cannot stand as its own "
                    "evidence" % ptr)
    if ident:
        pdir = os.path.realpath(proofs_dir(root))
        if real.startswith(pdir + os.sep):
            if os.path.basename(real) in ("%s.md" % ident, "%s.md.seal.json" % ident):
                return ("evidence %s is another copy of the report for %s; a report cannot stand "
                        "as its own evidence" % (ptr, ident))
            inside = real[len(pdir) + 1:].split(os.sep)[:-1]
            if ("%s%s" % (ident, KEPT_SUFFIX)) in inside:
                return ("evidence %s is a copy this seal kept for %s; a report cannot stand as "
                        "its own evidence" % (ptr, ident))
    return None


def resolve_evidence(conn, root, item, *, report=None, ident=None) -> dict:
    """Resolve one parsed pointer into its sealed shape, or raise Refusal naming the problem.

    local     through contract.resolve_pointer (relative, no escape, a real file), then it must
              stay inside the root once symlinks are followed, must not be the report itself,
              and must hold at least one byte that is not whitespace.
    event     the id exists in `events`; the sealed hash is the event's own chain hash.
    receipt   the domain profile names an existing file for it (alpaca/profile.py `evidence_file`).
    job       the same, for a job.
    remote    kept as written and recorded unverified: nothing here can reach it.
    """
    ptr, kind, note = item["pointer"], item["kind"], item.get("note")
    base = {"pointer": ptr, "kind": kind, "note": note}
    if kind is None:
        raise Refusal(["evidence %r names no pointer scheme; use one of %s"
                       % (ptr, ", ".join("%s:" % k for k in POINTER_KINDS))])
    body = ptr.split(":", 1)[1].strip() if ":" in ptr else ""
    if kind == "remote":
        if not body:
            raise Refusal(["evidence %r carries an empty remote ref" % ptr])
        base.update({"sha256": None, "bytes": None, "verified": False})
        return base
    if not body:
        raise Refusal(["evidence %r carries an empty body" % ptr])
    if kind == "local":
        try:
            full = contract.resolve_pointer(root, ptr)
        except contract.ContractError as e:
            raise Refusal(["evidence %s does not resolve (%s)" % (ptr, e.reason_code)])
        if not _inside_root(root, full):
            raise Refusal(["evidence %s resolves outside the project root" % ptr])
        mine = _self_citation(root, ptr, os.path.realpath(full), report, ident)
        if mine:
            raise Refusal([mine])
        size = os.path.getsize(full)
        if size == 0:
            raise Refusal(["evidence %s is an empty file; an empty file shows nothing" % ptr])
        if _blank_file(full):
            raise Refusal(["evidence %s holds only whitespace; an empty file shows nothing"
                           % ptr])
        base.update({"sha256": contract.sha256_bytes(full), "bytes": size, "verified": True})
        return base
    if kind == "event":
        try:
            eid = int(body)
        except ValueError:
            raise Refusal(["evidence %s does not name an integer event id" % ptr])
        row = conn.execute("SELECT id, hash FROM events WHERE id=?", (eid,)).fetchone()
        if row is None:
            raise Refusal(["evidence %s names no event on the record" % ptr])
        base.update({"sha256": row["hash"], "bytes": None, "verified": True})
        return base
    path = _run_file(root, kind, body)
    label = "run receipt" if kind == "receipt" else "run job"
    if path is None:
        from alpaca import profile
        why = profile.error(root)
        raise Refusal(["evidence %s names no %s: %s" % (
            ptr, label, why if why else "no domain profile resolves it")])
    if not os.path.isfile(path):
        raise Refusal(["evidence %s names no %s under %s"
                       % (ptr, label, _rel(root, os.path.dirname(path)))])
    size = os.path.getsize(path)
    if size == 0 or _blank_file(path):
        raise Refusal(["evidence %s names an empty %s file" % (ptr, label)])
    base.update({"sha256": contract.sha256_bytes(path), "bytes": size, "verified": True})
    return base


# --------------------------------------------------------------------------- keeping the files
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name) -> str:
    """The basename as a file name in the kept directory: every character outside
    `[A-Za-z0-9._-]` becomes a `_`, and a name that slugs away to nothing becomes `file`."""
    out = _SLUG.sub("_", str(name or "").strip()).strip(".")
    return (out or "file")[:120]


def _unlink(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _prune_kept(directory, keep_names) -> None:
    """Drop every name in the kept directory this seal did not write, a part file from an
    interrupted run included. Called only once the new copies are all in place, so a re-seal
    never leaves the directory empty."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if name not in keep_names:
            _unlink(os.path.join(directory, name))


def keep_evidence(root, subj, evidence) -> dict:
    """Copy every resolvable `local:` evidence file into `<id>.evidence/` and record where.

    Each entry gains `kept`: the copy's path relative to the project root, or None with a
    `kept_reason` when a cap or an unreadable file left it out. Position in the Evidence list
    leads the file name, so a reader matches a copy to the line that cited it without opening
    anything. Each copy is written under a part name and moved into place, so a half-written
    file is never mistaken for the evidence. Returns {"dir", "kept", "bytes"}."""
    directory = kept_dir(root, subj)
    written, total = [], 0
    for pos, item in enumerate(evidence, 1):
        if not isinstance(item, dict):
            continue
        if item.get("kind") != "local":
            item["kept"] = None                     # nothing here to copy: not a file in the tree
            continue
        target = _evidence_file(root, "local", item.get("pointer"))
        if target is None or not os.path.isfile(target):
            item["kept"] = None
            item["kept_reason"] = "the file went between resolving it and copying it"
            continue
        size = item.get("bytes") or os.path.getsize(target)
        if size > KEPT_FILE_CAP or total + size > KEPT_TOTAL_CAP:
            item["kept"] = None
            item["kept_reason"] = OVER_CAP
            continue
        name = "%02d-%s" % (pos, _slug(os.path.basename(target)))
        dest = os.path.join(directory, name)
        part = os.path.join(directory, ".%s.part" % name)
        try:
            os.makedirs(directory, exist_ok=True)
            shutil.copyfile(target, part)
            os.replace(part, dest)
        except OSError as e:
            _unlink(part)
            item["kept"] = None
            item["kept_reason"] = "the copy failed (%s)" % e.__class__.__name__
            continue
        item["kept"] = _rel(root, dest)
        written.append(name)
        total += size
    _prune_kept(directory, written)
    return {"dir": directory, "kept": written, "bytes": total}


def _kept_file(root, kept):
    """The kept copy on disk, or None when the seal recorded no usable path for it."""
    rel = str(kept or "").replace("\\", "/").strip()
    if not rel or os.path.isabs(rel) or ".." in rel.split("/"):
        return None
    return os.path.join(root, rel.replace("/", os.sep))


# --------------------------------------------------------------------------- the seal
def failed_jobs_in_window(root, first, now) -> list:
    """Every domain run whose verdict is not PASS and whose run overlaps [first, now], from the
    profile (alpaca/profile.py `failed_runs`). A report whose window contains one and does not name
    it is a run narrative true only by omission. Empty without a profile.

    The gate fails closed: when project.yaml names a profile that did not load, or the hook
    raises or answers with the wrong type, this raises alpaca.profile.ProfileError rather than
    answering "no failed run"."""
    from alpaca import profile
    out = []
    for job in profile.strict(root, "failed_runs", root, first, now):
        if isinstance(job, dict) and job.get("job_id"):
            out.append(dict(job, job_id=str(job["job_id"])))
    return out


def inspect(conn, root, ident) -> dict:
    """Read the report and collect every reason a seal would refuse it. Writes nothing.

    Returns {"path", "rel", "sections", "evidence", "problems"}; an empty `problems` is a
    sealable report."""
    subj = subject(conn, ident)
    path = report_path(root, subj)
    problems = []
    if not os.path.isfile(path):
        return {"path": path, "rel": _rel(root, path), "sections": {}, "evidence": [],
                "problems": ["no proof report at %s: run alpaca proof new %s"
                             % (_rel(root, path), ident)]}
    text = util.read_text(path)
    found = split_sections(text)
    sizes = {}
    for name in REQUIRED:
        if name not in found:
            problems.append("section %r is missing" % name)
            continue
        body = found[name]
        sizes[name] = prose_len(body)
        if TODO_MARK in body:
            problems.append("section %r still holds its %s line" % (name, TODO_MARK))
        if name in NARRATIVE and sizes[name] < MIN_CHARS:
            problems.append("section %r holds %d non-space characters outside code fences and "
                            "HTML comments, under the %d floor; a pasted log is not the section"
                            % (name, sizes[name], MIN_CHARS))
    for name in found:
        sizes.setdefault(name, prose_len(found[name]))

    evidence, items = [], parse_evidence(found.get(EVIDENCE, ""))
    if not items:
        problems.append("the %s section lists no pointer" % EVIDENCE)
    for item in items:
        try:
            evidence.append(resolve_evidence(conn, root, item, report=path, ident=subj["id"]))
        except Refusal as r:
            problems.extend(r.problems)
    if items and not any(e.get("kind") not in UNVERIFIABLE for e in evidence):
        problems.append("the %s section carries no verifiable pointer; a remote ref alone cannot "
                        "be checked here" % EVIDENCE)

    # A run narrative may not be true by omission: every job that failed inside this report's
    # window has to be named somewhere in the report. The gate reads the job files, not the
    # report's own claims, so a report cannot satisfy it by staying silent.
    span = window(conn, subj["id"])
    from alpaca import profile
    try:
        failed = failed_jobs_in_window(root, span.get("first"), span.get("now"))
    except profile.ProfileError as exc:
        failed, blocked = [], True
        problems.append("the failed-run check cannot run, so the seal waits: %s" % exc)
    else:
        blocked = False
    for job in failed:
        if job["job_id"] not in text and job["job_id"][:8] not in text:
            problems.append("run %s (%s) ended %s in this report's window and no section "
                            "names it: %s" % (job["job_id"], job.get("stage"), job["verdict"],
                                              str(job.get("reason"))[:80]))
    return {"path": path, "rel": _rel(root, path), "sections": sizes, "evidence": evidence,
            "problems": problems, "subject": subj, "blocked": blocked}


def seal(conn, root, ident, *, session=None) -> dict:
    """Seal the report: refuse it, or keep the evidence, append one `proof-report` event and
    write the seal copy.

    The report bytes are never edited here. A re-seal after an edit appends a NEW event and
    rebuilds the kept directory; the latest one wins, and both events stay on the record."""
    got = inspect(conn, root, ident)
    if got["problems"]:
        raise Refusal(got["problems"], blocked=got.get("blocked", False))
    path, subj = got["path"], got["subject"]
    keep_evidence(root, subj, got["evidence"])
    payload = {
        "path": _rel(root, path),
        "sha256": contract.sha256_bytes(path),
        "bytes": os.path.getsize(path),
        "sections": got["sections"],
        "evidence": got["evidence"],
    }
    db.append_event(conn, session=session or "cli", actor="agent", kind=KIND,
                    op=subj.get("op"), ref=subj["id"], data=payload)
    util.write_text(path + ".seal.json", json.dumps(payload, indent=1, sort_keys=True) + "\n")
    return payload


def seal_index(conn) -> dict:
    """id -> its LATEST `proof-report` event. One scan of the log, so a caller folding many ids
    does not re-read the log per id."""
    out = {}
    for e in db.events(conn, kind=KIND, limit=10 ** 9):
        ref = e.get("ref")
        if ref:
            out[ref] = e
    return out


def latest_seal(conn, ident, index=None):
    """The latest `proof-report` event for this id, or None. Latest wins: a re-seal supersedes."""
    if index is not None:
        return index.get(ident)
    return seal_index(conn).get(ident)


# --------------------------------------------------------------------------- re-checking
def verify(conn, root, ident, index=None) -> tuple:
    """(ok, problems): the two things every caller already reads, from `verify_detail`."""
    got = verify_detail(conn, root, ident, index)
    return got["ok"], got["problems"]


def verify_detail(conn, root, ident, index=None) -> dict:
    """{"ok", "problems", "notes"}: what a re-check of the seal for `ident` finds.

    A problem is a proof that no longer stands: the report changed or went, an event lost its
    chain hash, or an evidence file moved on and no kept copy holds what the seal recorded. A
    note is the ordinary life of a working tree: the original file changed or went, and the copy
    this seal kept still hashes to the sealed value, so the report still has its attachment and
    `ok` stays True. A seal written before the copies existed carries no `kept` key at all; for
    those a changed original is the problem it has always been, and the message says that
    re-sealing is what keeps the file from now on."""
    ev = latest_seal(conn, ident, index)
    if ev is None:
        return {"ok": False, "problems": ["%s carries no sealed proof report" % ident],
                "notes": []}
    data = ev["data"] if isinstance(ev.get("data"), dict) else {}
    problems, notes = [], []
    rel = data.get("path")
    full = os.path.join(root, str(rel or "").replace("/", os.sep))
    if not rel:
        problems.append("%s: the seal names no report path" % ident)
    elif not os.path.isfile(full):
        problems.append("%s: the sealed report %s is gone" % (ident, rel))
    elif contract.sha256_bytes(full) != data.get("sha256"):
        problems.append("%s: the report %s changed since it was sealed" % (ident, rel))
    for item in data.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        kind, ptr = item.get("kind"), item.get("pointer")
        if kind in UNVERIFIABLE or kind is None:
            continue
        if kind == "event":
            body = str(ptr or "").split(":", 1)[-1].strip()
            try:
                eid = int(body)
            except ValueError:
                problems.append("%s: evidence %s is malformed" % (ident, ptr))
                continue
            row = conn.execute("SELECT hash FROM events WHERE id=?", (eid,)).fetchone()
            if row is None:
                problems.append("%s: evidence %s is no longer on the record" % (ident, ptr))
            elif row["hash"] != item.get("sha256"):
                problems.append("%s: evidence %s no longer carries its sealed chain hash"
                                % (ident, ptr))
            continue
        target = _evidence_file(root, kind, ptr)
        gone = target is None or not os.path.isfile(target)
        if not gone and contract.sha256_bytes(target) == item.get("sha256"):
            continue
        moved = "%s: evidence %s %s" % (ident, ptr,
                                        "is gone" if gone else "changed since it was sealed")
        if "kept" not in item:
            # sealed before the copies existed: today's refusal, plus the one command that keeps
            # the file from here on. A file that is gone cannot be re-sealed, so it is not offered.
            problems.append(moved if gone else
                            "%s; re-run alpaca proof seal %s to keep a copy of the evidence as it "
                            "is now" % (moved, ident))
            continue
        kept = _kept_file(root, item.get("kept"))
        if kept is None:
            problems.append(moved)                  # a cap or a failed copy left nothing behind
        elif not os.path.isfile(kept):
            problems.append("%s, and the copy kept at %s is gone too" % (moved, item.get("kept")))
        elif contract.sha256_bytes(kept) != item.get("sha256"):
            problems.append("%s: the kept copy of %s was altered" % (ident, ptr))
        else:
            notes.append("evidence %s changed since sealing; the sealed copy is kept at %s"
                         % (ptr, item.get("kept")))
    return {"ok": not problems, "problems": problems, "notes": notes}


def _evidence_file(root, kind, pointer):
    body = str(pointer or "").split(":", 1)[-1].strip()
    if not body:
        return None
    if kind == "local":
        if os.path.isabs(body) or ".." in body.replace("\\", "/").split("/"):
            return None
        return os.path.join(root, body.replace("/", os.sep))
    if kind in ("receipt", "job"):
        return _run_file(root, kind, body)
    return None


def sealed_ids(conn) -> list:
    """Every id that carries at least one `proof-report` event, in first-seal order."""
    out = []
    for e in db.events(conn, kind=KIND, limit=10 ** 9):
        ref = e.get("ref")
        if ref and ref not in out:
            out.append(ref)
    return out


# --------------------------------------------------------------------------- the done gate
def next_steps(ident) -> list:
    """The three commands the gate prints when it refuses."""
    return ["alpaca proof new %s" % ident,
            "write the report: every section in your own words, real evidence pointers",
            "alpaca proof seal %s" % ident]


def done_gate(conn, root, ident, pointer) -> tuple:
    """(ok, problems). The one question a `done` move asks of a proof.

    `--proof` must be `local:<path>`, the pointer must resolve, the latest `proof-report` event
    for this id must name that path with a sha256 equal to the file right now, AND every sealed
    evidence pointer must still hash to what the seal recorded, in the tree or in the copy the
    seal kept. Hashing the report alone would close a task over a deleted or rewritten evidence
    file, so the gate asks `verify` the same question `alpaca proof check` asks and folds its
    problems into the refusal. An evidence file the work moved on from is not one of them: the
    kept copy still holds what the report stood on, so that task closes."""
    if not pointer:
        return False, ["done needs --proof local:<report>, the sealed proof report for %s" % ident]
    scheme = str(pointer).split(":", 1)[0].strip().lower()
    if scheme != "local":
        return False, ["--proof %s is not a local: pointer; a remote ref belongs inside the "
                       "report's %s list and does not close a task on its own"
                       % (pointer, EVIDENCE)]
    try:
        full = contract.resolve_pointer(root, pointer)
    except contract.ContractError as e:
        return False, ["--proof %s does not resolve (%s)" % (pointer, e.reason_code)]
    ev = latest_seal(conn, ident)
    if ev is None:
        return False, ["%s carries no proof-report event: the report is not sealed" % ident]
    data = ev["data"] if isinstance(ev.get("data"), dict) else {}
    want, sealed = _norm(_rel(root, full)), _norm(data.get("path"))
    if want != sealed:
        return False, ["the sealed report for %s is %s, and --proof names %s"
                       % (ident, data.get("path"), _rel(root, full))]
    now = contract.sha256_bytes(full)
    if now != data.get("sha256"):
        return False, ["%s changed since it was sealed (sealed %s, now %s)"
                       % (data.get("path"), str(data.get("sha256"))[:12], now[:12])]
    ok, problems = verify(conn, root, ident, {ident: ev})
    if not ok:
        return False, problems
    return True, []


def refuse_done(gate_name, ident, problems) -> None:
    """Print the refusal the way every `done` boundary prints it: the GATE line, then the three
    commands. One shape, so a reader meets the same message from task move and from board move."""
    print("GATE %s: FAIL (%s)" % (gate_name, problems[0] if problems else "no sealed proof report"))
    for extra in problems[1:]:
        print("  also: %s" % extra)
    for i, step in enumerate(next_steps(ident), 1):
        print("  %d. %s" % (i, step))


# --------------------------------------------------------------------------- the doctor form
def checks(root, conn) -> list:
    """Findings as (name, level, detail) tuples, the shape `alpaca doctor` folds in.

    One finding, `proof-integrity`. It WARNs naming each done task whose sealed report or sealed
    evidence changed or vanished with no kept copy behind it, and carries the count of done tasks
    still closed by an unsealed legacy proof string. An evidence file that moved on while its
    kept copy stayed intact is counted, not warned about: the work went on, which is what work
    does, and the proof still has what it stood on. A record with no done task yields nothing."""
    try:
        done = [t for t in db.rows(conn, "tasks", "status='done'")]
    except Exception:
        return []
    if not done:
        return []
    index = seal_index(conn)
    drifted, legacy, sealed, moved = [], [], 0, 0
    for t in sorted(done, key=lambda r: str(r.get("id"))):
        ident = t.get("id")
        if index.get(ident) is None:
            if t.get("proof"):
                legacy.append(str(ident))
            continue
        sealed += 1
        got = verify_detail(conn, root, ident, index)
        drifted.extend(got["problems"])
        moved += len(got["notes"])
    detail = "%d done task(s), %d sealed" % (len(done), sealed)
    kept = ("; %d evidence file(s) changed since sealing, sealed copies intact" % moved
            if moved else "")
    if drifted:
        return [("proof-integrity", "warn", "%s; %s%s%s"
                 % (detail, "; ".join(drifted),
                    "; %d unsealed legacy proof(s): %s" % (len(legacy), ", ".join(legacy))
                    if legacy else "", kept))]
    if legacy:
        return [("proof-integrity", "warn",
                 "%s; %d done task(s) carry an unsealed legacy proof: %s%s"
                 % (detail, len(legacy), ", ".join(legacy), kept))]
    if moved:
        return [("proof-integrity", "ok", "%s%s" % (detail, kept))]
    return [("proof-integrity", "ok", "%s; every sealed report and its evidence still match"
             % detail)]


# --------------------------------------------------------------------------- CLI boundary
def _cmd_proof(args):
    from alpaca import cli
    root = cli._root()
    conn = db.connect(root)
    sid = args.session or "cli"
    verb = getattr(args, "proof_verb", None)
    if verb == "new":
        try:
            res = scaffold(conn, root, args.id, force=args.force, session=sid)
        except Refusal as r:
            print("GATE alpaca-proof-new: FAIL (%s)" % "; ".join(r.problems))
            return cli.FAIL
        print("wrote %s" % res["rel"])
        print("next: write every section, then alpaca proof seal %s" % res["id"])
        return cli.PASS
    if verb == "seal":
        try:
            payload = seal(conn, root, args.id, session=sid)
        except Refusal as r:
            word = "BLOCKED" if r.blocked else "FAIL"
            print("GATE alpaca-proof-seal: %s (%d problem(s))" % (word, len(r.problems)))
            for p in r.problems:
                print("  - %s" % p)
            return cli.BLOCKED if r.blocked else cli.FAIL
        print("GATE alpaca-proof-seal: PASS (%s sealed, %d evidence pointer(s))"
              % (payload["path"], len(payload["evidence"])))
        print("close it with: alpaca task move %s done --proof local:%s"
              % (args.id, payload["path"]))
        return cli.PASS
    if verb == "check":
        ids = sealed_ids(conn) if (args.all or not args.id) else [args.id]
        if not ids:
            print("GATE alpaca-proof-check: BLOCKED (no sealed proof report on this record)")
            return cli.BLOCKED
        problems, notes, files = [], [], 0
        index = seal_index(conn)
        for ident in ids:
            got = verify_detail(conn, root, ident, index)
            problems.extend(got["problems"])
            notes.extend(got["notes"])
            ev = latest_seal(conn, ident, index)
            data = ev["data"] if ev and isinstance(ev.get("data"), dict) else {}
            files += 1 + len([e for e in (data.get("evidence") or [])
                              if isinstance(e, dict) and e.get("kind") not in UNVERIFIABLE])
        if problems:
            print("GATE alpaca-proof-check: FAIL (%d report(s), %d problem(s))"
                  % (len(ids), len(problems)))
            for p in problems:
                print("  - %s" % p)
            for n in notes:
                print("  %s" % n)
            return cli.FAIL
        print("GATE alpaca-proof-check: PASS (%d report(s), %d hash(es) still match)"
              % (len(ids), files))
        for n in notes:                     # the tree moved on; the proof kept what it stood on
            print("  %s" % n)
        return cli.PASS
    print("GATE alpaca-proof: BLOCKED (unknown proof verb; use new, seal or check)")
    return vc.BLOCKED


def _parser(sub):
    p = sub.add_parser("proof", help="the proof report a task hands in: new, seal, check")
    pv = p.add_subparsers(dest="proof_verb")
    n = pv.add_parser("new", help="scaffold the report for a task or a checklist row")
    n.add_argument("id")
    n.add_argument("--force", action="store_true",
                   help="overwrite an existing report (it is refused otherwise)")
    s = pv.add_parser("seal", help="resolve and hash the report's evidence, then record the seal")
    s.add_argument("id")
    c = pv.add_parser("check", help="re-hash a sealed report and its evidence")
    c.add_argument("id", nargs="?")
    c.add_argument("--all", action="store_true", help="check every sealed report on the record")


def _register():
    from alpaca import cli
    cli.command("proof")(_cmd_proof)
    cli.register_parser("proof", _parser)


_register()
