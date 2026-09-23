"""The tool capture pool (E6): one JSON object per tool call phase, under `.alpaca/pool/tools/`.

The record keeps a heartbeat per tool call: a name and a short ref, with a Bash command reduced to
its first word and a digest. That is enough to count activity and nothing like enough to read back
what a call actually did. The pool keeps the rest, beside the record rather than inside it:

    .alpaca/pool/tools/<sid>.jsonl

one line per call phase, written by the PreToolUse hook before the call and the PostToolUse hook
after it. The lines are raw capture, not record rows: they are not hash-chained and nothing else
reads them back into a session. Every line carries the same fifteen keys in the same order, so a
reader can stream the file without a schema.

Both halves of a call are capped, and a capped line still identifies the bytes it came from:

  * a response over `CAP` is stored as its first `CAP` bytes of text with `response_truncated`
    true, while `response_sha256` and `response_bytes` describe the whole value;
  * an input over `CAP` is stored the same way, under `input_truncated`, `input_sha256` and
    `input_bytes`;
  * an input over `POST_INPUT_CAP` is stored ONCE, on the `pre` line. The `post` line of the same
    call then carries `input: null` and `input_ref: "pre"`, keeping its digest and its size, so a
    10 MB Write costs the pool one copy rather than two.

Appending uses O_APPEND and one write call, so the PreToolUse hook of one tool and the PostToolUse
hook of another cannot interleave halves of two lines in the same file.
"""
from __future__ import annotations

import json
import os
import re

from alpaca import paths, util

#: the most response or input text one line stores. Above this the line is capped, never dropped.
CAP = 256 * 1024

#: above this an input is carried by the `pre` line alone; the `post` line points back at it.
POST_INPUT_CAP = 4 * 1024

#: the key order every pool line carries, checked by the tests that read the file back.
KEYS = ("ts", "sid", "phase", "tool", "tool_use_id", "input", "response",
        "response_truncated", "response_sha256", "response_bytes", "cwd",
        "input_truncated", "input_sha256", "input_bytes", "input_ref")

PHASES = ("pre", "post")

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def slug(value) -> str:
    """One path component from a caller-supplied id: every other character becomes a dash.

    A hook payload names its own session, so the id reaches a file path from outside the project.
    Slugging it keeps `../../escape` inside the directory it belongs to. This is the single rule;
    `alpaca.transcripts` imports it rather than spelling its own.
    """
    return _UNSAFE.sub("-", str(value))


def pool_dir(root) -> str:
    return os.path.join(paths.runtime_dir(root), "pool", "tools")


def path(root, sid) -> str:
    """The pool file for one session. A session id is slugged, so it can never leave the dir."""
    return os.path.join(pool_dir(root), "%s.jsonl" % slug(sid))


def _blob(value):
    """The bytes a value is measured and hashed by: its own text, or its util.canonical_json
    form, so the same value always gives the same digest."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.encode("utf-8")
    try:
        return util.canonical_json(value).encode("utf-8")
    except (TypeError, ValueError):
        return repr(value).encode("utf-8")


def _input_fields(phase, tool_input):
    """The five input fields of a line: what is stored, whether it was cut, its digest, the size
    of the whole value, and which line holds the whole value when this one does not.

    A `post` line over POST_INPUT_CAP stores nothing: the `pre` line of the same tool_use_id
    already holds those bytes and both lines carry the same digest, so nothing is lost.
    """
    blob = _blob(tool_input)
    nbytes = len(blob) if blob is not None else 0
    sha = util.sha256_hex(blob) if blob is not None else None
    if phase != "pre" and nbytes > POST_INPUT_CAP:
        return None, False, sha, nbytes, "pre"
    if nbytes > CAP:
        return blob[:CAP].decode("utf-8", "replace"), True, sha, nbytes, None
    return tool_input, False, sha, nbytes, None


def line(sid, phase, payload) -> dict:
    """Build one pool line from a hook payload. Pure: no file is touched.

    `phase` is "pre" (before the call, no response yet) or "post" (after it). A post payload with
    no tool_response reads like a pre one: a null response, no digest, zero bytes.
    """
    response_in = payload.get("tool_response") if phase != "pre" else None
    blob = _blob(response_in)
    response, truncated, sha, nbytes = None, False, None, 0
    if blob is not None:
        nbytes = len(blob)
        sha = util.sha256_hex(blob)
        if nbytes > CAP:
            response = blob[:CAP].decode("utf-8", "replace")
            truncated = True
        else:
            response = response_in
    tool_input = payload.get("tool_input")
    if tool_input is None:
        tool_input = {}
    stored_in, in_truncated, in_sha, in_bytes, in_ref = _input_fields(phase, tool_input)
    return {"ts": util.now_iso(),
            "sid": str(sid),
            "phase": phase,
            "tool": payload.get("tool_name") or "?",
            "tool_use_id": payload.get("tool_use_id") or payload.get("toolUseId") or None,
            "input": stored_in,
            "response": response,
            "response_truncated": truncated,
            "response_sha256": sha,
            "response_bytes": nbytes,
            "cwd": payload.get("cwd"),
            "input_truncated": in_truncated,
            "input_sha256": in_sha,
            "input_bytes": in_bytes,
            "input_ref": in_ref}


def _ends_with_newline(fd) -> bool:
    """True when the file is empty or its last byte is a newline.

    False means a previous write was cut off part way through its line -- a hook can be stopped at
    its deadline -- so the next append has to open a line of its own first.
    """
    try:
        size = os.fstat(fd).st_size
        if size == 0:
            return True
        return os.pread(fd, 1, size - 1) == b"\n"
    except OSError:
        return True


def append(root, sid, rec) -> str:
    """Serialize concurrent short-write retries under flock and fsync the completed line."""
    from alpaca.capture_io import append_json
    target = path(root, sid)
    append_json(target, rec, root=root)
    return target


def record(root, sid, phase, payload) -> dict:
    """Build and append one pool line. Returns the line as written."""
    rec = line(sid, phase, payload)
    append(root, sid, rec)
    return rec


class Lines(list):
    """The pool lines of one session, carrying the count of lines that did not read back.

    A pool file is raw capture written by a hook that can be stopped at its deadline, so a damaged
    line is possible. The reader skips it and counts it here instead of raising: one bad line must
    not hide the rest of a session.
    """

    def __init__(self, items=(), damaged=0):
        list.__init__(self, items)
        self.damaged = int(damaged)


def read(root, sid) -> Lines:
    """Every pool line for one session, in write order.

    A line that does not parse, or that parses to something other than an object, is skipped and
    counted in `.damaged`. Reading never raises on a damaged file.
    """
    p = path(root, sid)
    if not os.path.isfile(p):
        return Lines()
    out, damaged = [], 0
    with open(p, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                damaged += 1
                continue
            if not isinstance(obj, dict):
                damaged += 1
                continue
            out.append(obj)
    return Lines(out, damaged)
