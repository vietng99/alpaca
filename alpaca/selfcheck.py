"""The door self-check (op-019 t-203, design section 5): a fresh agent checks the work against what the
owner signed, and the door does not open until it passes.

Alpaca cannot launch an agent (CLAUDE.md Operation), so this module reads a recorded result, the way
`runlog/reviewflow.py` does:

  * `alpaca selfcheck brief <op> <boundary> [--deliverable P]...` builds the PACKET under
    `.alpaca/selfcheck/<op>/<boundary-slug>-<n>/`: the op's signed brief copy, the project runbook
    when there is one, `owner.json` (the owner's decisions, batch answers, orders and chat messages),
    the deliverables, `prompt.md` (the checking agent's whole brief, with a nonce) and `packet.json`
    (sha256 of every file). The checker sees the work and what the owner covered, never the builder's
    account of the work: builder notes are left out of the packet.
  * the main session dispatches one fresh subagent with `prompt.md` as its whole prompt; it writes
    `result.json`.
  * `alpaca selfcheck submit <op> <boundary> <result.json>` checks every quote byte for byte against
    the packet, checks that the writer was a separate agent (its transcript), and records one
    `selfcheck` event and one `derived-choice` event per outside-brief finding.
  * the check binds as the door link `intent-selfcheck` (L5 and L6) and, through `task_gate`, at the
    phase transition (design D-12), so it holds whether or not anyone runs `alpaca phase gate`.

The rule at L6 (brief D1: nothing stops for the owner): a current PASS must exist, and no `mismatch` or
`dropped-order` finding of any self-check of (op, boundary) may still hold. A mismatch holds while its
deliverable quote is still there byte for byte, so a later lenient check cannot clear it; a dropped
order holds until the order is accounted for (alpaca.orders). `outside-brief` findings never fail at
L6: they are derived choices, listed for the owner to read later (`alpaca selfcheck derived`).

HONEST LIMIT: the checker's isolation is the packet and the prompt; the subagent can still open other
files. The same Unix user can write any transcript or record file; the separate-agent check proves the
transcript Claude Code wrote has the shape of a fresh agent that wrote these bytes.
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import re
import secrets
import subprocess

from alpaca import db, ownerword, paths, util
from alpaca.gates import verdict as vc
from alpaca.phase import defaults

KIND = "selfcheck"
DERIVED_KIND = "derived-choice"
CLAIM_KIND = "covered-by-claim"  # an owner.json item that is the agent's claim (alpaca.oob.owner_items)
BATCH_ITEM_KIND = "batch-item"
CHECK_NAME = "intent-selfcheck"

#: the finding kinds. The first two fail the door at L6; `outside-brief` never does (it is derived).
MISMATCH, OUTSIDE, DROPPED = "mismatch", "outside-brief", "dropped-order"
BLOCKING_KINDS = (MISMATCH, DROPPED)
FINDING_KINDS = (MISMATCH, OUTSIDE, DROPPED)

R_NO_BRIEF = "SELFCHECK-NO-BRIEF"
R_NOTES = "SELFCHECK-BUILDER-NOTES"
R_NO_PACKET = "SELFCHECK-NO-PACKET"
R_PACKET_CHANGED = "SELFCHECK-PACKET-CHANGED"
R_RESULT = "SELFCHECK-RESULT"
R_QUOTE = "SELFCHECK-QUOTE-NOT-FOUND"
R_BRIEF_SHA = "SELFCHECK-BRIEF-SHA"
R_PASS = "SELFCHECK-PASS-REFUSED"
R_NOT_SEPARATE = "SELFCHECK-NOT-SEPARATE"
R_MISSING = "SELFCHECK-MISSING"
R_HOLDING = "SELFCHECK-HOLDING"

#: a deliverable larger than this is left out of the packet (listed as dropped): the checker reads it.
MAX_DELIVERABLE_BYTES = 2 * 1024 * 1024
#: a directory named by a task's `where` field is read to at most this many files.
MAX_DIR_FILES = 200

#: base names (lower-cased) that are builder notes: the builder's own account of its work.
NOTE_PATTERNS = ("notes*.md", "*-notes.md", "build-r*.md", "fix-r*.md", "investigate-r*.md")
NOTE_WORDS = ("landing", "handoff")
#: path prefixes (repo-relative) that are builder notes.
NOTE_PREFIXES = (".alpaca/proofs/", ".alpaca/transcripts/")


class SelfcheckRefusal(ValueError):
    """A refused packet or result: `.reason` is the reason token, the message names what is wrong."""

    def __init__(self, reason, detail):
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail


# ------------------------------------------------------------------ small helpers
def slug(boundary) -> str:
    """`design->build` as a folder name: `design-build`."""
    return re.sub(r"[^A-Za-z0-9]+", "-", str(boundary)).strip("-")


def _check_boundary(boundary):
    if boundary not in defaults.BOUNDARY_PHASE:
        raise ValueError("%r is not a phase boundary (%s)" % (
            boundary, ", ".join(sorted(defaults.BOUNDARY_PHASE))))


def _sha(data) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _data(raw) -> dict:
    try:
        out = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _rel(root, path) -> str:
    """`path` as a normalized path under `root`, or ValueError when it points outside it."""
    full = path if os.path.isabs(path) else os.path.join(root, path)
    real_root = os.path.realpath(root)
    real = os.path.realpath(full)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise ValueError("%s is outside the project root" % path)
    return os.path.relpath(os.path.join(real_root, os.path.relpath(real, real_root)), real_root)


def is_builder_notes(rel) -> bool:
    """True when repo-relative path `rel` is the builder's account of the work: anything under
    `.alpaca/proofs/` or `.alpaca/transcripts/`, a base name containing `landing` or `handoff`, or one
    of the note file shapes. The checker sees the work, never the builder's account of it."""
    norm = rel.replace(os.sep, "/")
    while norm.startswith("./"):
        norm = norm[2:]
    if any(norm.startswith(p) for p in NOTE_PREFIXES):
        return True
    base = norm.rsplit("/", 1)[-1].lower()
    if any(word in base for word in NOTE_WORDS):
        return True
    return any(fnmatch.fnmatch(base, pat) for pat in NOTE_PATTERNS)


def has_batch(conn, op) -> bool:
    """True when the op has a batch: any `batch-item` event for it. Ops opened before the batch
    existed, and ops a research run opens from code, have none and are not gated (design D-1)."""
    for row in conn.execute("SELECT op, data FROM events WHERE kind=?", (BATCH_ITEM_KIND,)):
        if row["op"] == op or _data(row["data"]).get("op") == op:
            return True
    return False


def packet_dir(root, op, boundary, n) -> str:
    return os.path.join(paths.runtime_dir(root), "selfcheck", op, "%s-%d" % (slug(boundary), n))


def packet_numbers(root, op, boundary) -> list:
    base = os.path.join(paths.runtime_dir(root), "selfcheck", op)
    out = []
    pattern = re.compile(r"^%s-(\d+)$" % re.escape(slug(boundary)))
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        m = pattern.match(name)
        if m and os.path.isfile(os.path.join(base, name, "packet.json")):
            out.append(int(m.group(1)))
    return sorted(out)


# ------------------------------------------------------------------ the brief and the runbook
def signed_brief(conn, root, op):
    """(bytes, sha256) of the newest signed copy of the op's brief whose bytes still match their
    sha256, or None. The signed copy, never the live file, is what the checker reads."""
    from alpaca import brief
    for ev in reversed(brief.signed_events(conn, op)):
        data = _read_bytes(os.path.join(root, ev.get("copy") or ""))
        if data is not None and _sha(data) == ev.get("sha256"):
            return data, ev["sha256"]
    return None


def runbook_file(conn, root, op):
    """The repo-relative path of the op's runbook (the newest `intake` event of the op names it), or
    None when the op has no runbook or the file is gone."""
    row = conn.execute("SELECT data FROM events WHERE kind='intake' AND op=? ORDER BY id DESC LIMIT 1",
                       (op,)).fetchone()
    rel = _data(row["data"]).get("runbook") if row else None
    if isinstance(rel, str) and rel:
        try:
            rel = _rel(root, rel)
        except ValueError:
            return None
        if os.path.isfile(os.path.join(root, rel)):
            return rel
    return None


# ------------------------------------------------------------------ owner.json
def _op_opened(conn, op):
    row = conn.execute("SELECT opened FROM ops WHERE id=?", (op,)).fetchone()
    if row and row["opened"]:
        return row["opened"]
    row = conn.execute("SELECT ts FROM events WHERE kind='op-new' AND (op=? OR ref=?) ORDER BY id "
                       "LIMIT 1", (op, op)).fetchone()
    return row["ts"] if row else None


def _decision_items(conn, root, op):
    out = []
    seen = set()
    for row in conn.execute("SELECT ref, data FROM events WHERE kind='decision' ORDER BY id"):
        ref = row["ref"]
        if ref in seen:
            continue
        data = _data(row["data"])
        if not re.search(r"(?<![A-Za-z0-9-])%s(?![0-9A-Za-z])" % re.escape(op),
                         json.dumps(data, ensure_ascii=False)):
            continue
        seen.add(ref)
        words = ownerword.decision_words(conn, ref, root)
        if words is None:
            continue
        out.append({"id": "decision:%s" % ref, "kind": "owner-decision", "decision": ref,
                    "text": words, "choice": data.get("choice") or "",
                    "consequence": data.get("consequence") or ""})
    return out


def _message_items(conn, root, op):
    """Every owner message (ownerword filter) of every session that wrote an event for the op, typed
    since the op opened: the packet is how a message that gave an order nobody recorded (R4) reaches
    the checker."""
    opened = _op_opened(conn, op)
    out = []
    sessions = [r["session"] for r in conn.execute(
        "SELECT DISTINCT session FROM events WHERE op=? ORDER BY session", (op,))]
    for sid in sessions:
        try:
            _path, messages = ownerword.session_messages(conn, sid, root)
        except ownerword.OwnerWordRefusal:
            continue
        for m in messages:
            if opened is not None and not ownerword._after(m, opened, None):
                continue
            out.append({"id": "msg:%s:%d" % (sid[:8], m["line"]), "kind": "owner-message",
                        "session": sid, "line": m["line"], "timestamp": m["timestamp"],
                        "text": m["text"]})
    return out


def owner_stable_sha(items) -> str:
    """sha256 of the owner items that are not chat messages: the decisions and what the sibling modules
    collect (batch answers, orders). A check is stale when one of these arrives after it. Chat messages
    are left out: a new message would stale every check, and orders are answered by the orders link."""
    stable = [i for i in items if i.get("kind") != "owner-message"]
    return _sha(util.canonical_json(stable).encode("utf-8"))


def collect_owner_items(conn, root, op) -> list:
    """The items of `owner.json`: owner-proved decisions, whatever the sibling modules collect through
    intentgate.owner_items (batch answers, owner orders), and the owner's chat messages. Every item
    has a unique `id`; the result must account for each one (coverage)."""
    from alpaca import intentgate
    items = _decision_items(conn, root, op)
    for raw in intentgate.owner_items(conn, op, root=root):
        if isinstance(raw, dict):
            items.append(dict(raw))
    items.extend(_message_items(conn, root, op))
    seen, out = set(), []
    for item in items:
        iid = str(item.get("id") or "")
        if not iid:
            iid = "item-" + _sha(util.canonical_json(item).encode("utf-8"))[:12]
        base, k = iid, 2
        while iid in seen:
            iid = "%s#%d" % (base, k)
            k += 1
        seen.add(iid)
        out.append(dict(item, id=iid))
    return out


def item_texts(item) -> list:
    """Every string a quote of this owner.json item may come from."""
    out = []

    def walk(value):
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(item)
    return out


# ------------------------------------------------------------------ deliverables
def _task_where(conn, root, op):
    """(path, origin) for every path in the `where` field of every task of the op. A directory is
    read as its files (at most MAX_DIR_FILES)."""
    out = []
    for row in conn.execute("SELECT id, where_ FROM tasks WHERE op=? ORDER BY id", (op,)):
        for part in re.split(r"[,;\s]+", row["where_"] or ""):
            part = part.strip()
            if not part:
                continue
            try:
                rel = _rel(root, part)
            except ValueError:
                continue
            full = os.path.join(root, rel)
            if os.path.isdir(full):
                count = 0
                for directory, dirs, files in os.walk(full):
                    dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                    for name in sorted(files):
                        if count >= MAX_DIR_FILES:
                            break
                        out.append((os.path.relpath(os.path.join(directory, name), root),
                                    "task-where:%s" % row["id"]))
                        count += 1
            else:
                out.append((rel, "task-where:%s" % row["id"]))
    return out


def _commit_files(conn, root, op):
    """(path, origin) for every file changed by a commit whose message names one of the op's task
    ids (at a token boundary: t-20 never matches t-203). Empty when the root is not a git tree."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM tasks WHERE op=?", (op,))]
    if not ids:
        return []
    cmd = ["git", "-C", root, "log", "--name-only", "--no-renames", "--format=%x01%H%x02%B%x03"]
    for tid in ids:
        cmd.append("--grep=%s" % tid)
    cmd.append("--fixed-strings")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    named = re.compile(r"(?<![A-Za-z0-9-])(%s)(?![0-9A-Za-z])" % "|".join(re.escape(t) for t in ids))
    out = []
    for chunk in proc.stdout.split("\x01"):
        if "\x03" not in chunk:
            continue
        head, _, tail = chunk.partition("\x03")
        sha, _, message = head.partition("\x02")
        found = named.search(message)
        if not found:
            continue
        for line in tail.splitlines():
            line = line.strip()
            if line:
                out.append((line, "commit:%s:%s" % (sha[:12], found.group(1))))
    return out


def collect_deliverables(conn, root, op, extra=()):
    """(kept, dropped): kept is [{path, origin, bytes}], dropped is [{path, why}].

    Kept: every path in the `where` of the op's tasks, every file changed by a commit naming one of the
    op's tasks, and each `extra` path (the builder adds, never removes). Dropped from the record-derived
    set: builder notes, the record's own folders under `.alpaca/`, files that are gone and files over
    the size cap. The builder-notes and record-folder tests run on the path and on where a symlink
    resolves to, so a link cannot carry a proof report into the packet. An `extra` path that is builder notes is refused (SELFCHECK-BUILDER-NOTES)."""
    extras = []
    for path in extra:
        rel = _rel(root, path)
        if is_builder_notes(rel):
            raise SelfcheckRefusal(R_NOTES, "%s is builder notes; the checker sees the work, never the "
                                   "builder's account of it" % rel)
        extras.append((rel, "flag"))
    kept, dropped, seen = [], [], set()
    derived = _task_where(conn, root, op) + _commit_files(conn, root, op)
    for rel, origin in derived + extras:
        norm = rel.replace(os.sep, "/")
        if norm in seen:
            continue
        seen.add(norm)
        try:
            resolved = _rel(root, norm).replace(os.sep, "/")
        except ValueError:
            dropped.append({"path": norm, "why": "outside-root"})
            continue
        if is_builder_notes(norm) or is_builder_notes(resolved):
            dropped.append({"path": norm, "why": "builder-notes"})
            continue
        if (norm.startswith(".alpaca/") or resolved.startswith(".alpaca/")) and origin != "flag":
            dropped.append({"path": norm, "why": "record-folder"})
            continue
        full = os.path.join(root, norm)
        if not os.path.isfile(full):
            dropped.append({"path": norm, "why": "missing"})
            continue
        data = _read_bytes(full)
        if data is None:
            dropped.append({"path": norm, "why": "unreadable"})
        elif len(data) > MAX_DELIVERABLE_BYTES:
            dropped.append({"path": norm, "why": "too-large"})
        else:
            kept.append({"path": norm, "origin": origin, "bytes": data})
    return kept, dropped


# ------------------------------------------------------------------ the prompt
def prompt_text(op, boundary, folder, files, brief_sha, nonce, has_runbook) -> str:
    """The checking agent's whole brief. It tells the checker to judge against the brief, the runbook
    and owner.json only, names the finding kinds, the coverage rule, the output file and its schema."""
    listing = "\n".join("  - %s" % name for name in files)
    runbook = ("  - runbook: the project runbook; it is covered ground like the brief.\n"
               if has_runbook else "")
    return (
        "You are the door self-check for %(op)s, boundary %(boundary)s. You are a fresh checker: you "
        "took no part in the build and you are given no builder notes.\n"
        "\n"
        "Judge the deliverables against the brief, the runbook and owner.json ONLY. A thing is "
        "covered when the brief, the runbook, an owner decision, a batch answer or an owner message "
        "says it. Nothing else counts as covered: not what the builder meant, not what seems "
        "reasonable.\n"
        "\n"
        "Packet folder: %(folder)s\n"
        "Files in it:\n%(listing)s\n"
        "  - brief.md: the brief the owner signed.\n%(runbook)s"
        "  - owner.json: a JSON list of the owner's words, each item with an `id`. An item of kind "
        "covered-by-claim is NOT the owner's word: it is the builder's claim that a choice is "
        "covered. Re-read it against the brief, the runbook and the owner's own items; it is never "
        "a source a `meets` quote may come from, and it covers nothing by being listed.\n"
        "  - deliverables/<path>: the work, as it stands now.\n"
        "Read brief.md, the runbook if present, owner.json and every deliverable.\n"
        "\n"
        "Report findings of three kinds:\n"
        "  - mismatch: a deliverable contradicts something covered (the brief, the runbook, an owner "
        "decision, a batch answer, an owner message).\n"
        "  - outside-brief: a deliverable makes a choice nothing covered settles.\n"
        "  - dropped-order: an owner message in owner.json gives an order for %(op)s that no recorded "
        "order or owner item accounts for.\n"
        "\n"
        "Coverage rule: for EVERY item of owner.json, give a quote from a deliverable that agrees with "
        "it, or the word not-touched when no deliverable touches it. Do not skip an item. For a "
        "covered-by-claim item whose deliverable quote you give, add `meets` {\"source\", \"quote\"}: "
        "the brief, runbook or owner text that really covers that choice; with none, report the "
        "choice as outside-brief.\n"
        "\n"
        "Output: use the Write tool to write exactly one file, %(folder)s/result.json, a JSON object:\n"
        "  {\"brief_sha256\": \"<sha256 of brief.md; it must be %(sha)s, check it with sha256sum>\",\n"
        "   \"findings\": [\n"
        "     {\"kind\": \"mismatch\", \"path\": \"<deliverable path>\", \"quote\": \"<exact text from "
        "that deliverable>\",\n"
        "      \"meets\": {\"source\": \"brief.md|runbook|owner.json\", \"quote\": \"<exact text from "
        "that source>\"}, \"reason\": \"<one line>\"},\n"
        "     {\"kind\": \"outside-brief\", \"path\": \"<deliverable path>\", \"quote\": \"<exact text>\", "
        "\"reason\": \"<one line: what was chosen and what covers nothing>\"},\n"
        "     {\"kind\": \"dropped-order\", \"item\": \"<owner.json item id>\", \"quote\": \"<exact text "
        "from that message>\", \"reason\": \"<one line>\"}],\n"
        "   \"coverage\": [{\"item\": \"<owner.json item id>\", \"path\": \"<deliverable path>\", "
        "\"quote\": \"<exact text>\"}, {\"item\": \"<id>\", \"status\": \"not-touched\"}],\n"
        "   \"verdict\": \"FAIL if any finding is a mismatch or a dropped-order, else PASS\"}\n"
        "Every quote must be copied byte for byte; a quote that is not in the file is refused. Write "
        "the file once, with the final content, and do nothing after it.\n"
        "\n"
        "Nonce (do not change it, do not repeat it anywhere else): %(nonce)s"
    ) % {"op": op, "boundary": boundary, "folder": folder, "listing": listing, "runbook": runbook,
         "sha": brief_sha, "nonce": nonce}


# ------------------------------------------------------------------ the packet
def build_packet(conn, root, session, op, boundary, deliverables=(), *, level=None):
    """Write the packet of (op, boundary) and return its packet.json as a dict.

    Refused: SELFCHECK-NO-BRIEF when the op has no signed brief copy, SELFCHECK-BUILDER-NOTES when a
    `--deliverable` is builder notes."""
    _check_boundary(boundary)
    if not db.rows(conn, "ops", "id=?", (op,)):
        raise ValueError("no op %s" % op)
    signed = signed_brief(conn, root, op)
    if signed is None:
        raise SelfcheckRefusal(R_NO_BRIEF, "%s has no signed brief copy; present and sign it first "
                               "(`alpaca brief present|sign %s <file>`)" % (op, op))
    brief_bytes, brief_sha = signed
    kept, dropped = collect_deliverables(conn, root, op, deliverables)
    existing = packet_numbers(root, op, boundary)
    n = (existing[-1] + 1) if existing else 1
    folder = packet_dir(root, op, boundary, n)
    if level is None:
        from alpaca import intentgate
        level = intentgate.level(conn, session, root=root)

    files = {}
    _write_bytes(os.path.join(folder, "brief.md"), brief_bytes)
    files["brief.md"] = _sha(brief_bytes)
    runbook = None
    rel_rb = runbook_file(conn, root, op)
    if rel_rb:
        rb_bytes = _read_bytes(os.path.join(root, rel_rb))
        if rb_bytes is not None:
            name = "runbook" + (os.path.splitext(rel_rb)[1] or ".md")
            _write_bytes(os.path.join(folder, name), rb_bytes)
            files[name] = _sha(rb_bytes)
            runbook = {"path": rel_rb, "name": name, "sha256": files[name]}
    items = collect_owner_items(conn, root, op)
    owner_bytes = (json.dumps(items, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    _write_bytes(os.path.join(folder, "owner.json"), owner_bytes)
    files["owner.json"] = _sha(owner_bytes)
    listed = []
    for d in kept:
        name = "deliverables/" + d["path"]
        _write_bytes(os.path.join(folder, name), d["bytes"])
        files[name] = _sha(d["bytes"])
        listed.append({"path": d["path"], "sha256": files[name], "origin": d["origin"]})
    nonce = secrets.token_hex(16)
    prompt = prompt_text(op, boundary, folder, sorted(list(files) + ["prompt.md", "packet.json"]),
                         brief_sha, nonce, runbook is not None)
    prompt_bytes = prompt.encode("utf-8")
    _write_bytes(os.path.join(folder, "prompt.md"), prompt_bytes)
    files["prompt.md"] = _sha(prompt_bytes)
    packet = {"op": op, "boundary": boundary, "n": n, "nonce": nonce, "level": level,
              "created": util.now_iso(), "brief_sha256": brief_sha, "files": files,
              "runbook": runbook, "deliverables": listed, "dropped": dropped,
              "owner_items": [i["id"] for i in items], "owner_sha256": owner_stable_sha(items)}
    _write_bytes(os.path.join(folder, "packet.json"),
                 (json.dumps(packet, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return packet


def load_packet(root, op, boundary, n=None):
    """(packet.json dict, folder) of packet `n` of (op, boundary), the newest when `n` is None."""
    nums = packet_numbers(root, op, boundary)
    if not nums or (n is not None and n not in nums):
        raise SelfcheckRefusal(R_NO_PACKET, "no packet for %s %s; run `alpaca selfcheck brief %s %s`"
                               % (op, boundary, op, boundary))
    folder = packet_dir(root, op, boundary, n if n is not None else nums[-1])
    data = _read_bytes(os.path.join(folder, "packet.json"))
    packet = _data(data.decode("utf-8", errors="replace")) if data is not None else {}
    if not packet:
        raise SelfcheckRefusal(R_NO_PACKET, "%s is unreadable" % os.path.join(folder, "packet.json"))
    return packet, folder


def _packet_intact(packet, folder):
    """None when every file of the packet still has the sha256 packet.json recorded, else a detail."""
    for name, sha in sorted((packet.get("files") or {}).items()):
        data = _read_bytes(os.path.join(folder, name))
        if data is None or _sha(data) != sha:
            return "%s changed after the packet was built" % name
    return None


# ------------------------------------------------------------------ result checks
def _quote_in(quote, data) -> bool:
    return isinstance(quote, str) and quote != "" and quote.encode("utf-8") in data


def _check_meets(k, meets, packet, folder, items_by_id):
    """Verify one `meets` {source, quote} byte for byte; returns (source, quote). An owner.json quote
    must come from an owner item: a covered-by-claim item is the agent's own claim, and citing it as
    what a deliverable meets would launder the claim into owner authority (brief P2)."""
    src, mq = str(meets.get("source") or ""), meets.get("quote")
    if src == "owner.json":
        ok = any(_quote_in(mq, t.encode("utf-8")) for item in items_by_id.values()
                 if item.get("kind") != CLAIM_KIND for t in item_texts(item))
    else:
        name = src if src in (packet.get("files") or {}) else None
        if name is None and src == "runbook" and packet.get("runbook"):
            name = packet["runbook"]["name"]
        ok = name is not None and name != "owner.json" and \
            _quote_in(mq, _read_bytes(os.path.join(folder, name)) or b"")
    if not ok:
        raise SelfcheckRefusal(R_QUOTE, "finding %d: the `meets` quote is not in %r byte "
                               "for byte" % (k, src))
    return src, mq


def _verify_findings(result, packet, folder, items_by_id):
    out = []
    listed = {d["path"] for d in packet.get("deliverables") or []}
    findings = result.get("findings")
    if not isinstance(findings, list):
        raise SelfcheckRefusal(R_RESULT, "`findings` must be a list")
    for k, f in enumerate(findings):
        if not isinstance(f, dict) or f.get("kind") not in FINDING_KINDS:
            raise SelfcheckRefusal(R_RESULT, "finding %d: `kind` must be one of %s" % (
                k, ", ".join(FINDING_KINDS)))
        kind = f["kind"]
        reason = f.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SelfcheckRefusal(R_RESULT, "finding %d: a one-line `reason` is required" % k)
        entry = {"kind": kind, "reason": reason.strip(), "quote": f.get("quote")}
        if kind == DROPPED:
            item = items_by_id.get(f.get("item")) if isinstance(f.get("item"), str) else None
            if item is None or not any(_quote_in(f.get("quote"), t.encode("utf-8"))
                                       for t in item_texts(item)):
                raise SelfcheckRefusal(R_QUOTE, "finding %d: the quote is not in owner.json item %r"
                                       % (k, f.get("item")))
            entry["item"] = item["id"]
            entry["text"] = str(item.get("text") or "")
        else:
            path = str(f.get("path") or "")
            path = path[len("deliverables/"):] if path.startswith("deliverables/") else path
            if path not in listed:
                raise SelfcheckRefusal(R_QUOTE, "finding %d: %r is not a deliverable of this packet"
                                       % (k, path))
            data = _read_bytes(os.path.join(folder, "deliverables", path)) or b""
            if not _quote_in(f.get("quote"), data):
                raise SelfcheckRefusal(R_QUOTE, "finding %d: the quote is not in %s byte for byte"
                                       % (k, path))
            entry["path"] = path
            meets = f.get("meets")
            if kind == MISMATCH or meets is not None:
                if not isinstance(meets, dict):
                    raise SelfcheckRefusal(R_RESULT, "finding %d: a mismatch names what it meets: "
                                           "{\"source\", \"quote\"}" % k)
                src, mq = _check_meets(k, meets, packet, folder, items_by_id)
                entry["meets"] = {"source": src, "quote": mq}
        out.append(entry)
    return out


def _verify_coverage(result, packet, folder, items_by_id):
    item_ids = set(items_by_id)
    cov = result.get("coverage")
    if not isinstance(cov, list):
        raise SelfcheckRefusal(R_RESULT, "`coverage` must be a list")
    listed = {d["path"] for d in packet.get("deliverables") or []}
    covered = set()
    for k, c in enumerate(cov):
        if not isinstance(c, dict) or not isinstance(c.get("item"), str) or c.get("item") not in item_ids:
            raise SelfcheckRefusal(R_RESULT, "coverage %d: `item` must be an owner.json item id" % k)
        if c.get("status") == "not-touched":
            covered.add(c["item"])
            continue
        path = str(c.get("path") or "")
        path = path[len("deliverables/"):] if path.startswith("deliverables/") else path
        data = _read_bytes(os.path.join(folder, "deliverables", path)) if path in listed else None
        if data is None or not _quote_in(c.get("quote"), data):
            raise SelfcheckRefusal(R_QUOTE, "coverage %d: the quote is not in deliverable %r byte for "
                                   "byte (use status not-touched when none touches the item)" % (k, path))
        if items_by_id[c["item"]].get("kind") == CLAIM_KIND:
            # the agent's claim is covered only by the brief, the runbook or an owner item that says
            # the choice, never by the deliverable that makes it
            if not isinstance(c.get("meets"), dict):
                raise SelfcheckRefusal(R_RESULT, "coverage %d: a covered-by-claim item needs `meets` "
                                       "(the brief, runbook or owner text that covers the choice), or "
                                       "report the choice as outside-brief" % k)
            _check_meets(k, c["meets"], packet, folder, items_by_id)
        covered.add(c["item"])
    return covered


# ------------------------------------------------------------------ the separate-agent check
def _blocks(entry):
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content


def _agent_wrote(path, prompt, result_text):
    """True when the subagent transcript at `path` is a fresh agent that wrote `result_text`: its
    first user message is exactly the prompt, every other user entry is a tool_result (no later
    message from the coordinator), there is no queued_command attachment of any origin, and a Write
    tool call that did not fail wrote the result bytes."""
    first_seen = False
    writes = {}          # tool_use id -> True when a Write carried the result bytes
    errors = set()       # tool_use ids whose tool_result was an error
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return False
    with fh:
        for raw in fh:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if kind == "attachment":
                att = entry.get("attachment")
                if isinstance(att, dict) and att.get("type") == "queued_command":
                    return False
            elif kind == "user":
                content = _blocks(entry)
                if not first_seen:
                    first_seen = True
                    if entry.get("isMeta") or entry.get("isCompactSummary"):
                        return False
                    if isinstance(content, list) and content and all(
                            isinstance(b, dict) and b.get("type") == "text" for b in content):
                        content = "\n".join(str(b.get("text")) for b in content)
                    if content != prompt:
                        return False
                    continue
                if entry.get("isMeta") or not isinstance(content, list) or not content or not all(
                        isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    return False
                for b in content:
                    if b.get("is_error") and b.get("tool_use_id"):
                        errors.add(b["tool_use_id"])
            elif kind == "assistant":
                content = _blocks(entry)
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Write":
                        inp = b.get("input")
                        if isinstance(inp, dict) and inp.get("content") == result_text:
                            writes[b.get("id")] = True
    if not first_seen:
        return False
    return any(tid not in errors for tid in writes)


def separate_agent(conn, root, session, prompt, result_bytes):
    """The path of a subagent transcript of the session that proves the result was written by a
    separate agent given exactly `prompt`, or SELFCHECK-NOT-SEPARATE."""
    try:
        result_text = result_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise SelfcheckRefusal(R_RESULT, "result.json is not UTF-8")
    seen = []
    for sid in [session] + ownerword.real_sessions():
        if not sid or sid in seen:
            continue
        seen.append(sid)
        try:
            tpath = ownerword.transcript_path(conn, sid, root)
        except ownerword.OwnerWordRefusal:
            continue
        folder = os.path.join(os.path.splitext(tpath)[0], "subagents")
        for cand in sorted(glob.glob(os.path.join(folder, "agent-*.jsonl"))):
            if _agent_wrote(cand, prompt, result_text):
                return cand
    raise SelfcheckRefusal(R_NOT_SEPARATE, "no subagent transcript under the session's folder "
                           "(<sid>/subagents/agent-*.jsonl) shows a fresh agent given exactly "
                           "prompt.md that wrote these result.json bytes")


# ------------------------------------------------------------------ submit
def submit(conn, root, session, op, boundary, result_file):
    """Check the result of the newest packet of (op, boundary) and record it. Returns the selfcheck
    event. Refusals are SelfcheckRefusal; nothing is recorded on a refusal."""
    _check_boundary(boundary)
    packet, folder = load_packet(root, op, boundary)
    changed = _packet_intact(packet, folder)
    if changed:
        raise SelfcheckRefusal(R_PACKET_CHANGED, changed)
    result_path = result_file if os.path.isabs(result_file) else os.path.join(root, result_file)
    result_bytes = _read_bytes(result_path)
    if result_bytes is None:
        raise SelfcheckRefusal(R_RESULT, "cannot read %s" % result_file)
    try:
        result = json.loads(result_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SelfcheckRefusal(R_RESULT, "%s is not JSON" % result_file)
    if not isinstance(result, dict):
        raise SelfcheckRefusal(R_RESULT, "result.json must be a JSON object")
    verdict = result.get("verdict")
    if verdict not in ("PASS", "FAIL"):
        raise SelfcheckRefusal(R_RESULT, "`verdict` must be PASS or FAIL")
    if result.get("brief_sha256") != packet["brief_sha256"]:
        raise SelfcheckRefusal(R_BRIEF_SHA, "brief_sha256 %r is not the packet's %s: the checker did "
                               "not read the brief of this packet" % (result.get("brief_sha256"),
                                                                       packet["brief_sha256"]))
    items = json.loads(_read_bytes(os.path.join(folder, "owner.json")).decode("utf-8"))
    items_by_id = {i["id"]: i for i in items}
    findings = _verify_findings(result, packet, folder, items_by_id)
    covered = _verify_coverage(result, packet, folder, items_by_id)
    if verdict == "PASS":
        blocking = [f for f in findings if f["kind"] in BLOCKING_KINDS]
        if blocking:
            raise SelfcheckRefusal(R_PASS, "PASS with a %s finding; the verdict is FAIL" %
                                   blocking[0]["kind"])
        missing = sorted(set(items_by_id) - covered)
        if missing:
            raise SelfcheckRefusal(R_PASS, "PASS with owner.json items missing from coverage: %s" %
                                   ", ".join(missing[:8]))
    prompt = _read_bytes(os.path.join(folder, "prompt.md")).decode("utf-8")
    agent = separate_agent(conn, root, session, prompt, result_bytes)
    from alpaca import intentgate
    level = intentgate.level(conn, session, root=root)
    data = {"op": op, "boundary": boundary, "packet": os.path.relpath(folder, root), "n": packet["n"],
            "verdict": verdict, "findings": findings, "brief_sha256": packet["brief_sha256"],
            "packet_sha256": dict(packet["files"]), "deliverables": packet["deliverables"],
            "runbook": packet.get("runbook"), "owner_sha256": packet.get("owner_sha256"), "level": level, "agent_transcript": agent,
            "result_sha256": _sha(result_bytes)}
    actor = ownerword.session_actor(session)
    with db.transaction(conn):
        ev = db.append_event(conn, session=session, actor=actor, kind=KIND, op=op,
                             ref="%s:%s" % (op, boundary), data=data, conn_in_txn=True)
        for k, f in enumerate(findings):
            if f["kind"] == OUTSIDE:
                db.append_event(conn, session=session, actor=actor, kind=DERIVED_KIND, op=op,
                                ref="%s:%s:%d:%d" % (op, boundary, ev["id"], k),
                                data={"op": op, "boundary": boundary, "selfcheck": ev["id"],
                                      "index": k, "path": f.get("path"), "quote": f.get("quote"),
                                      "reason": f["reason"], "level": level},
                                conn_in_txn=True)
    if level == 5:
        # Brief D1/P3: at L5 a finding stops for the owner, one card per finding of any kind; at L6
        # nothing stops (the outside-brief ones are the derived choices above).
        from alpaca import oob
        for k, f in enumerate(findings):
            oob.raise_door_card(conn, op, boundary, ev["id"], k, f, session=session)
    return ev


# ------------------------------------------------------------------ the check
def selfcheck_events(conn, op, boundary) -> list:
    """Every selfcheck event of (op, boundary) as a dict (data merged with `event_id`), oldest first."""
    out = []
    for row in conn.execute("SELECT id, ts, data FROM events WHERE kind=? AND op=? ORDER BY id",
                            (KIND, op)):
        data = _data(row["data"])
        if data.get("boundary") == boundary:
            out.append(dict(data, event_id=row["id"], ts=row["ts"]))
    return out


def _is_current(conn, root, op, ev) -> bool:
    """True when everything the check read is still as it was: the signed brief, the runbook, the
    decisions and provider items of owner.json, and each deliverable have the sha256 the packet
    recorded, and no record-derived deliverable exists that the check did not see. A changed or added
    deliverable, or a new owner answer, needs a new check. (Chat messages are not compared: a new
    message would stale every check; orders have their own link.)"""
    signed = signed_brief(conn, root, op)
    if signed is None or signed[1] != ev.get("brief_sha256"):
        return False
    rb = ev.get("runbook")
    if rb:
        data = _read_bytes(os.path.join(root, rb["path"]))
        if data is None or _sha(data) != rb["sha256"]:
            return False
    seen = set()
    for d in ev.get("deliverables") or []:
        seen.add(d["path"])
        data = _read_bytes(os.path.join(root, d["path"]))
        if data is None or _sha(data) != d["sha256"]:
            return False
    if ev.get("owner_sha256") != owner_stable_sha(collect_owner_items(conn, root, op)):
        return False
    kept, _dropped = collect_deliverables(conn, root, op)
    return all(d["path"] in seen for d in kept)


def _accounted(conn, op, text) -> bool:
    """True when the dropped order whose message is `text` is accounted for (alpaca.orders, section 4).
    With no orders module, or one that fails, it is not: the finding holds."""
    try:
        from alpaca import orders
        fn = getattr(orders, "accounts_for", None)
        return bool(fn(conn, op, text)) if fn is not None else False
    except Exception:
        return False


def holding_findings(conn, op, boundary, *, root) -> list:
    """The `mismatch` and `dropped-order` findings of every self-check of (op, boundary) that still
    hold, as dicts with `event_id` and `index`. A mismatch holds while its deliverable quote is still
    present byte for byte in the file it named or in any current deliverable (a rename moves the bytes,
    it does not remove them), so a later lenient check cannot clear it. A
    dropped order holds until it is accounted for. An `outside-brief` finding holds only when the owner
    rejected its L5 card (alpaca.oob) and its quote is still there, as a mismatch does."""
    out = []
    current = None
    rejected = None
    for ev in selfcheck_events(conn, op, boundary):
        for k, f in enumerate(ev.get("findings") or []):
            kind = f.get("kind")
            if kind == OUTSIDE:
                # an outside-brief finding holds only once the owner rejected its L5 card (t-202)
                if rejected is None:
                    from alpaca import oob
                    rejected = oob.rejected_door_findings(conn, op, boundary)
                if (ev["event_id"], k) not in rejected:
                    continue
            if kind in (MISMATCH, OUTSIDE):
                if current is None:
                    current = [d["bytes"] for d in collect_deliverables(conn, root, op)[0]]
                data = _read_bytes(os.path.join(root, f.get("path") or ""))
                if any(_quote_in(f.get("quote"), blob) for blob in
                       ([data] if data is not None else []) + current):
                    out.append(dict(f, event_id=ev["event_id"], index=k))
            elif kind == DROPPED:
                if not _accounted(conn, op, f.get("text") or f.get("quote") or ""):
                    out.append(dict(f, event_id=ev["event_id"], index=k))
    return out


def current_pass(conn, op, boundary, *, root):
    """The newest PASS self-check of (op, boundary) whose packet still equals the files, or None."""
    for ev in reversed(selfcheck_events(conn, op, boundary)):
        if ev.get("verdict") == "PASS" and _is_current(conn, root, op, ev):
            return ev
    return None


def check(conn, op, boundary, *, level, root):
    """None when the check passes, else (verdict code, reason, detail). One rule for the door link and
    the task gate. Below L5 it is not asked."""
    holding = holding_findings(conn, op, boundary, root=root)
    if holding:
        f = holding[0]
        what = f.get("path") or f.get("item") or "?"
        return (vc.FAIL, R_HOLDING, "%d %s finding(s) of the %s self-check still hold, first: %s %r; "
                "rework and check again" % (len(holding), "/".join(sorted({h["kind"] for h in holding})),
                                           boundary, what, str(f.get("quote"))[:80]))
    if level == 5:
        # L5 (design 5.3, 6): an open out-of-brief card of this door, from any self-check, pauses it
        # and the transition behind it until the owner answers. A rejected card whose finding still
        # holds failed above (holding_findings); once every card is moved and none holds, it passes.
        from alpaca import oob
        opened = oob.open_door_cards(conn, op, boundary)
        if opened:
            return (vc.PAUSED, oob.R_OPEN, "%d out-of-brief card(s) of the %s door wait for the "
                    "owner, first: %s (%s)" % (len(opened), boundary, opened[0]["id"],
                                               opened[0].get("reason")))
    if current_pass(conn, op, boundary, root=root) is None:
        # A missing check is BLOCKED at L5; at L6 the run reworks and checks again: the owner is not
        # asked (brief D1).
        code = vc.BLOCKED if level == 5 else vc.FAIL
        return (code, R_MISSING, "no current PASS self-check of %s %s: run `alpaca selfcheck brief %s %s`, "
                "dispatch a fresh agent with its prompt.md, then `alpaca selfcheck submit`"
                % (op, boundary, op, boundary))
    return None


# ------------------------------------------------------------------ binding (design 5.3)
def door_link(conn, op, boundary, *, level, session, root):
    """The `intent-selfcheck` link of the phase door: composed at L5 and L6, for an op with a batch."""
    if level < 5 or not has_batch(conn, op):
        return None
    problem = check(conn, op, boundary, level=level, root=root)
    return (CHECK_NAME, vc.PASS if problem is None else problem[0])


def boundaries_before(conn, op, phase) -> list:
    """Every boundary a task of `phase` comes after whose closing phase has tasks in the op, nearest
    first. A task with no phase, or a phase off the ladder, counts as after every boundary. All of
    them are asked: a finding that holds at an earlier boundary is not cleared by a later pass."""
    have = {r["phase"] for r in conn.execute("SELECT DISTINCT phase FROM tasks WHERE op=?", (op,))}
    idx = defaults.LADDER.index(phase) if phase in defaults.LADDER else len(defaults.LADDER)
    found = [(defaults.LADDER.index(closing), boundary)
             for boundary, closing in defaults.BOUNDARY_PHASE.items()
             if closing in have and defaults.LADDER.index(closing) < idx]
    return [b for _i, b in sorted(found, reverse=True)]


def task_gate(conn, task, action, *, level, session, root):
    """Refuse `task claim` and `task move` to doing, review or done of a task that comes after a
    boundary whose self-check does not pass (the same check as the door link), for every such boundary."""
    if level < 5 or not task.get("op") or not has_batch(conn, task["op"]):
        return
    for boundary in boundaries_before(conn, task["op"], task.get("phase")):
        problem = check(conn, task["op"], boundary, level=level, root=root)
        if problem is not None:
            from alpaca.intentgate import IntentGateRefusal
            raise IntentGateRefusal(problem[1], "%s of %s comes after the %s door: %s" % (
                action, task.get("id"), boundary, problem[2]))


# ------------------------------------------------------------------ derived choices
def derived_choices(conn, op=None) -> list:
    """Every derived-choice event (an outside-brief finding the agent decided for itself), oldest
    first, as dicts. The owner reads these later (brief D1)."""
    q, p = "SELECT id, ts, op, data FROM events WHERE kind=?", [DERIVED_KIND]
    if op:
        q += " AND op=?"
        p.append(op)
    out = []
    for row in conn.execute(q + " ORDER BY id", p):
        out.append(dict(_data(row["data"]), event_id=row["id"], ts=row["ts"], op=row["op"]))
    return out


def derived_count(conn, op) -> int:
    return conn.execute("SELECT COUNT(*) FROM events WHERE kind=? AND op=?",
                        (DERIVED_KIND, op)).fetchone()[0]


# ------------------------------------------------------------------ CLI: alpaca selfcheck
def _cmd_selfcheck(args):
    from alpaca import cli
    root = cli._root()
    conn = db.connect(root)
    sid = args.session or "cli"
    verb = getattr(args, "selfcheck_verb", None)
    try:
        if verb == "brief":
            p = build_packet(conn, root, sid, args.op, args.boundary, args.deliverable or [])
            print("%s %s packet %d: %s" % (args.op, args.boundary, p["n"],
                                           os.path.relpath(packet_dir(root, args.op, args.boundary,
                                                                      p["n"]), root)))
            print("brief sha256 %s; %d deliverable(s), %d owner item(s), %d dropped"
                  % (p["brief_sha256"], len(p["deliverables"]), len(p["owner_items"]),
                     len(p["dropped"])))
            print("Dispatch one fresh subagent with prompt.md as its whole prompt; it writes "
                  "result.json. Then: alpaca selfcheck submit %s %s <result.json>"
                  % (args.op, args.boundary))
        elif verb == "submit":
            ev = submit(conn, root, sid, args.op, args.boundary, args.result)
            row = conn.execute("SELECT data FROM events WHERE id=?", (ev["id"],)).fetchone()
            data = _data(row["data"])
            print("%s %s self-check %s (event %s, brief sha256 %s, %d finding(s))" % (
                args.op, args.boundary, data["verdict"], ev["id"], data["brief_sha256"][:12],
                len(data["findings"])))
        elif verb == "derived":
            rows = derived_choices(conn, args.op)
            for d in rows:
                answer = d.get("answer")
                print("%s %s %s: %s%s [%s %s]" % (
                    d["op"], d.get("boundary") or d.get("kind") or "-",
                    d.get("path") or d.get("item") or d.get("task") or d.get("order") or "-",
                    d.get("reason"), (" (answer: %s)" % answer) if answer else "",
                    d["event_id"], d["ts"]))
            if not rows:
                print("no derived choices%s" % ((" for %s" % args.op) if args.op else ""))
        elif verb == "status":
            bounds = [args.boundary] if args.boundary else list(defaults.BOUNDARY_PHASE)
            for b in bounds:
                evs = selfcheck_events(conn, args.op, b)
                if not evs and not packet_numbers(root, args.op, b):
                    continue
                cur = current_pass(conn, args.op, b, root=root)
                hold = holding_findings(conn, args.op, b, root=root)
                print("%s %s: %d packet(s), %d check(s), latest %s, current PASS %s, %d holding" % (
                    args.op, b, len(packet_numbers(root, args.op, b)), len(evs),
                    evs[-1]["verdict"] if evs else "-", "yes" if cur else "no", len(hold)))
        else:
            return cli.USAGE
    except SelfcheckRefusal as exc:
        print("GATE alpaca-selfcheck: FAIL (%s: %s)" % (exc.reason, exc.detail))
        return cli.FAIL
    except ValueError as exc:
        print("GATE alpaca-selfcheck: FAIL (%s)" % exc)
        return cli.FAIL
    return cli.PASS


def _parser(sub):
    s = sub.add_parser("selfcheck", help="the door self-check: packet, submit, derived choices")
    sv = s.add_subparsers(dest="selfcheck_verb")
    b = sv.add_parser("brief", help="write the packet a fresh checking agent reads")
    b.add_argument("op"); b.add_argument("boundary")
    b.add_argument("--deliverable", action="append", help="one more file to check; repeatable")
    r = sv.add_parser("submit", help="check and record the checking agent's result.json")
    r.add_argument("op"); r.add_argument("boundary"); r.add_argument("result")
    d = sv.add_parser("derived", help="list the derived choices for the owner to read")
    d.add_argument("--op")
    t = sv.add_parser("status", help="show the self-checks of an op")
    t.add_argument("op"); t.add_argument("boundary", nargs="?")


def _register():
    from alpaca import cli
    cli.command("selfcheck")(_cmd_selfcheck)
    cli.register_parser("selfcheck", _parser)


_register()
