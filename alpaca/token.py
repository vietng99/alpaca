"""Completion tokens, the write-ahead pair, and the resume-time BLOCK.

The reproduced defect: a non-idempotent verb is killed after it has changed the
world but before it has recorded that it did, or after it recorded the intent but
before it acted. On resume the record cannot tell "done" from "never ran", so a
naive retry either double-applies or silently drops the act. This module makes the
gap decidable.

A verb writes a PRE-ACT intent note (`issue`) into the record and carries the
returned token to its target, so the target itself dedupes on it. It then acts,
then writes a POST-ACT result note (`settle`). The two notes are the write-ahead
pair. A process killed between them leaves an UNMATCHED token: an intent with no
result and no human void.

The war-log is the events table, one definition only (no new table): kind
`token-issue` is the intent note, `token-settle` the result note, `token-void`
the human discard. Tokens live in the append-only hash chain like every other
fact.

Three-step resume resolution (`resume_check`):
  1. token settled  -> done  (never surfaces; `unmatched` already excludes it).
  2. no settle, but a foreign act carried a tree witness and the witness is
     present in the tree  -> resolved (the act completed even with no result note).
  3. anything else   -> BLOCK: a pad row and a review card, never a default and
     never a silent re-apply.

`resume_check` runs at pickup (SessionStart), not in a later pass. `unmatched`
stays available to the historian sort pass (M4.6), which reports rather than
resolves.
"""
from __future__ import annotations

import os
import uuid

from alpaca import db

KIND_ISSUE = "token-issue"
KIND_SETTLE = "token-settle"
KIND_VOID = "token-void"

_SCAN_LIMIT = 100000


def _new_token() -> str:
    return "tok-" + uuid.uuid4().hex[:12]


def _token_events(conn):
    """All token notes in chronological order. db.events returns oldest-first."""
    return [e for e in db.events(conn, limit=_SCAN_LIMIT)
            if e["kind"] in (KIND_ISSUE, KIND_SETTLE, KIND_VOID)]


def _issue_event(conn, token):
    for e in _token_events(conn):
        if e["kind"] == KIND_ISSUE and e["data"].get("token") == token:
            return e
    return None


def _resolutions(token: str) -> list:
    """The two candidate resolutions offered for an unmatched token. A BLOCK is a
    choice put to a human, never auto-taken: either the act finished before the
    kill (settle it, keep the change) or it never ran (void it, then re-run)."""
    return [
        "completed: the act finished before the kill; settle the token and keep the "
        "change (bin/alpaca token settle %s)" % token,
        "incomplete: the act never ran; void the token and re-run the verb "
        "(bin/alpaca token void %s)" % token,
    ]


def issue(conn, session, ref, witness=None):
    """Write the pre-act intent note and return the token the verb carries to its
    target. Committed on its own so it is durable before the act: a kill after this
    point leaves the intent in the record. `witness` is for a foreign target that
    cannot hold a token; it names something the tree can be asked for afterwards,
    for example {"path": "out/artifact"}."""
    token = _new_token()
    db.append_event(conn, session=session, actor="alpaca", kind=KIND_ISSUE, ref=ref,
                    data={"token": token, "target": ref, "state": "issued",
                          "witness": witness})
    return token


def settle(conn, token, result_pointer):
    """Write the post-act result note. This is what makes a token matched; after it
    the resume check reports the act as done."""
    iss = _issue_event(conn, token)
    ref = iss["ref"] if iss else None
    session = iss["session"] if iss else "alpaca"
    db.append_event(conn, session=session, actor="alpaca", kind=KIND_SETTLE, ref=ref,
                    data={"token": token, "target": ref, "result": result_pointer,
                          "state": "settled"})


def void(conn, token, reason=""):
    """Human discard: the operator confirmed the act never ran. Resolves an
    unmatched token without applying anything, so the verb may be re-run safely."""
    iss = _issue_event(conn, token)
    ref = iss["ref"] if iss else None
    session = iss["session"] if iss else "alpaca"
    db.append_event(conn, session=session, actor="human", kind=KIND_VOID, ref=ref,
                    data={"token": token, "target": ref, "reason": reason,
                          "state": "void"})


def unmatched(conn) -> list:
    """Every issued token with no result note and no human void, oldest first. Each
    row carries the token, its target, the session and timestamp of the intent, any
    tree witness, and the two candidate resolutions."""
    issued, order = {}, []
    resolved = set()
    for e in _token_events(conn):
        tok = e["data"].get("token")
        if not tok:
            continue
        if e["kind"] == KIND_ISSUE:
            if tok not in issued:
                order.append(tok)
            issued[tok] = e
        else:                       # settle or void both close a token
            resolved.add(tok)
    out = []
    for tok in order:
        if tok in resolved:
            continue
        e = issued[tok]
        out.append({"token": tok,
                    "target": e["data"].get("target") or e["ref"],
                    "session": e["session"],
                    "ts": e["ts"],
                    "witness": e["data"].get("witness"),
                    "resolutions": _resolutions(tok)})
    return out


def _witness_present(root, witness) -> bool:
    """True when a foreign act's declared tree witness is present, proving the act
    completed. Only a relative path witness under the root is honoured."""
    if not root or not isinstance(witness, dict):
        return False
    rel = witness.get("path")
    if not rel or os.path.isabs(rel) or ".." in str(rel).split("/"):
        return False
    return os.path.exists(os.path.join(root, str(rel).replace("/", os.sep)))


def guard(conn, target):
    """Refuse-if-in-doubt for a non-idempotent verb: return the unmatched token that
    already targets `target`, or None when it is safe to act. A verb that finds a
    token here must not act; the resume must be resolved first."""
    for u in unmatched(conn):
        if u.get("target") == target:
            return u
    return None


def resume_check(conn, root=None):
    """The three-step resolution, run at pickup. Returns one of:
      ("clear", detail)     no unmatched token.
      ("resolved", detail)  every unmatched token has a present tree witness.
      ("BLOCK", detail)     at least one token is undecidable; detail['blocked']
                            lists each with its token, target and two resolutions.
    A BLOCK is a pad row and a review card, never a default."""
    un = unmatched(conn)
    if not un:
        return ("clear", {"blocked": [], "resolved": []})
    blocked, resolved = [], []
    for u in un:
        if _witness_present(root, u.get("witness")):
            resolved.append(u)
        else:
            blocked.append(u)
    if blocked:
        return ("BLOCK", {"blocked": blocked, "resolved": resolved})
    return ("resolved", {"blocked": [], "resolved": resolved})


def pad_lines(conn, root=None) -> list:
    """The BLOCK section for the pad, empty when the resume is clear or resolved.
    Shared by the pad renderer and the SessionStart boot block so both name the same
    token, target and two resolutions."""
    verdict_word, detail = resume_check(conn, root)
    if verdict_word != "BLOCK":
        return []
    lines = ["## BLOCKED: undecidable resume", "",
             "A verb was killed between its intent note and its result note. "
             "Resolve each before acting; do not re-run.", ""]
    for b in detail["blocked"]:
        lines.append("- BLOCK token %s target %s (opened %s)" %
                     (b["token"], b["target"], b["ts"]))
        for r in b["resolutions"]:
            lines.append("    - resolution: %s" % r)
    return lines
