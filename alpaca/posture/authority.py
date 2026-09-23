"""Standing authority: the lifecycle of an authority to act with no one watching (M3.2).

Sources: D16 (spec:164), Q13 (spec:179), Q18 (spec:188), spec 5.6:389-401; section 6 DROP row 88
drops the grant files and re-homes their obligation here; absorb-gap AG-M4 (expiry, revocation,
supersession, re-verification, scope implies level), AG-A7, AG-A20 (authority bound to the unit
of work).

Name the two instruments apart. The autodrive LEVEL (M3.1, alpaca.posture.level) says how much the
agent decides in THIS session. An AUTHORITY here says whether a loop may act with no one watching,
and for how long, over how wide a scope. Alpaca keeps both; neither stands in for the other.

The obligation the dropped grant file used to carry is met by five properties:

  * Expiry. An authority carries a parseable expiry. Once `now` reaches it the authority stops
    authorizing, and it stops the moment the expiry passes, with no session boundary: `verify`
    re-reads the record every call, so a long-running loop that re-verifies each iteration sees
    the expiry bite mid-loop (Step 2).
  * Revocation. The owner appends a revocation to the revocation channel (a REVOKE_KIND event).
    A running loop that re-reads the record at its next iteration honours it at once (Step 5).
  * Supersession. A newer grant supersedes an older one; `current` returns the newest grant that
    has not been revoked (Step 1).
  * Re-verification. There is no cached decision that a boot alone refreshes. Every gate and the
    unattended loop call `verify` against the record, so an expiry or a revocation that only a
    boot would notice cannot reach a loop that never reboots (Step 2).
  * Scope implies level. A scope covering more than one op requires the top level (L6) and a
    parseable expiry. A wide scope at a lower level is refused; a wide open-ended scope at a lower
    level is refused as a de-facto top level under a safe label (Step 3).

File-free (Step 5). The authority is record rows (GRANT_KIND events) plus the owner-appendable
revocation channel (REVOKE_KIND events). There is no grant file; D16 and Q16 dropped it and it is
not reintroduced here.

Bound to the op (Step 4). When an op opens, `bind_op` stamps the authority id, level and scope
that op ran under onto the op forever: a BIND_KIND event on the append-only chain and a meta row
the op reads back through `for_op`. A closed op can always say what authorized it. The doctor
reconciliation of that stamp is task M4.13.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from alpaca import db, paths, util
from alpaca.gates import verdict as vc
from alpaca.phase import defaults

#: the two record channels this instrument writes. GRANT_KIND is the authority itself; REVOKE_KIND
#: is the owner-appendable revocation channel. BIND_KIND records the authority an op ran under.
GRANT_KIND = "authority-grant"
REVOKE_KIND = "authority-revoke"
BIND_KIND = "authority-bind"

#: the autodrive ladder band (L1-L6, spec 5.6:390). The top level is what a wide scope requires.
MIN_LEVEL, MAX_LEVEL = 1, 6
TOP_LEVEL = MAX_LEVEL

#: scope tokens that name more than one op on their own. A trailing '*' (a glob) or a list of two
#: or more op ids also spans more than one op.
_WIDE_SCOPE = frozenset({"*", "all", "any", "every", "campaign", "cross-op", "fleet"})

# reason tokens the callers bind to (grep ONE token, not prose).
R_OK = "AUTHORITY-OK"
R_NO_AUTHORITY = "AUTHORITY-NONE"
R_EXPIRED = "AUTHORITY-EXPIRED"
R_REVOKED = "AUTHORITY-REVOKED"
R_LEVEL_NOT_COVERED = "AUTHORITY-LEVEL-NOT-COVERED"
R_SCOPE_NOT_COVERED = "AUTHORITY-SCOPE-NOT-COVERED"
R_LEVEL_BAND = "AUTHORITY-LEVEL-OFF-BAND"
R_SCOPE_EMPTY = "AUTHORITY-SCOPE-EMPTY"
R_SCOPE_NEEDS_TOP = "AUTHORITY-WIDE-SCOPE-NEEDS-TOP-LEVEL"
R_SCOPE_NEEDS_EXPIRY = "AUTHORITY-WIDE-SCOPE-NEEDS-EXPIRY"
R_OPEN_ENDED_WIDE = "AUTHORITY-OPEN-ENDED-WIDE-SCOPE-IS-DE-FACTO-TOP"
R_REVOKE_NO_ID = "AUTHORITY-REVOKE-NEEDS-ID"
R_REVOKE_NO_REASON = "AUTHORITY-REVOKE-NEEDS-REASON"


class AuthorityRefusal(Exception):
    """A refusal carrying its integer verdict-band code and its EXACT reason token, exactly as
    alpaca.decisions.DecisionRefusal does. The verdict numbers are referenced from alpaca.gates.verdict,
    never restated here."""

    def __init__(self, verdict_code: int, reason: str, detail: str = ""):
        super().__init__("%s %s: %s" % (vc.name_of(verdict_code), reason, detail))
        if verdict_code not in vc.VERDICT_BAND:
            raise ValueError("a refusal needs a verdict-band code, got %r" % (verdict_code,))
        self.verdict = verdict_code
        self.reason = reason
        self.detail = detail


class Verdict:
    """The result of a `verify`: an `ok` flag, the exact reason token, and the authority row it
    read (or None). Truthy iff `ok`, so a caller may write `if authority.verify(...)`."""

    def __init__(self, ok: bool, reason: str, authority=None):
        self.ok = bool(ok)
        self.reason = reason
        self.authority = authority

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return "Verdict(ok=%r, reason=%r)" % (self.ok, self.reason)


# --------------------------------------------------------------------------- parsing helpers
def parse_dt(value):
    """Parse an ISO-8601 stamp, or None when it is empty or unparseable."""
    s = "" if value is None else str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def expiry_parseable(expiry) -> bool:
    """True iff `expiry` parses to a timestamp. An empty or malformed expiry is not parseable."""
    return parse_dt(expiry) is not None


def spans_multiple_ops(scope) -> bool:
    """True iff `scope` names more than one op: a wide token, a trailing-'*' glob, or a list of
    two or more op ids."""
    s = str(scope or "").strip().lower()
    if not s:
        return False
    if s in _WIDE_SCOPE or s.endswith("*"):
        return True
    parts = [p for p in re.split(r"[,\s]+", s) if p]
    return len(parts) > 1


def _norm_scope(scope) -> str:
    return str(scope or "").strip().lower()


def scope_covers(authority_scope, requested) -> bool:
    """Does an authority's scope cover a requested scope? A wide authority scope covers anything;
    a single-op authority scope covers only the same single op."""
    if requested is None:
        return True
    if spans_multiple_ops(authority_scope):
        return True
    return _norm_scope(authority_scope) == _norm_scope(requested)


# --------------------------------------------------------------------------- the record channels
def _grants(conn):
    """Every granted authority, oldest first (append order)."""
    return [e["data"] for e in db.events(conn, kind=GRANT_KIND, limit=1000000)]


def _revoked_ids(conn):
    out = set()
    for e in db.events(conn, kind=REVOKE_KIND, limit=1000000):
        d = e["data"] or {}
        out.add(d.get("id") or e.get("ref"))
    return out


def _next_id(conn) -> str:
    return "auth-%03d" % (len(_grants(conn)) + 1)


def latest_grant(conn):
    """The newest grant, revoked or not (supersession = newest wins), or None."""
    grants = _grants(conn)
    return grants[-1] if grants else None


def current(conn):
    """The standing authority in force: the newest grant that has not been revoked, or None.

    A newer grant supersedes an older one, so the newest wins; a revoked newest falls back to the
    newest grant still standing. This reads only, and reads the record every call, so a revocation
    or a supersession appended since the last call is seen at once.
    """
    grants = _grants(conn)
    if not grants:
        return None
    revoked = _revoked_ids(conn)
    for g in reversed(grants):
        if g.get("id") not in revoked:
            return g
    return None


# --------------------------------------------------------------------------- grant
def grant(conn, level, scope, expiry=None, *, actor="owner", pointer="", session="cli",
          supersedes=None) -> dict:
    """Append one standing-authority grant to the record, or refuse it.

    Enforces scope-implies-level (Step 3): a scope covering more than one op requires the top
    level and a parseable expiry. A wide scope at a lower level is refused; a wide open-ended scope
    at a lower level is refused as a de-facto top level under a safe label. Returns the grant row.
    """
    rank = defaults.level_num(level)
    if not (MIN_LEVEL <= rank <= MAX_LEVEL):
        raise AuthorityRefusal(vc.BLOCKED, R_LEVEL_BAND,
                               "an authority level is on the L1-L6 band, got %r" % (level,))
    label = "L%d" % rank
    scope_s = str(scope or "").strip()
    if not scope_s:
        raise AuthorityRefusal(vc.BLOCKED, R_SCOPE_EMPTY, "an authority needs a scope")

    if spans_multiple_ops(scope_s):
        has_expiry = expiry_parseable(expiry)
        if rank < TOP_LEVEL and not has_expiry:
            raise AuthorityRefusal(
                vc.BLOCKED, R_OPEN_ENDED_WIDE,
                "a wide open-ended scope (%s) at %s is a de-facto top-level authority; refusing it "
                "under a safe label. A scope over more than one op needs the top level L6 and a "
                "parseable expiry" % (scope_s, label))
        if rank < TOP_LEVEL:
            raise AuthorityRefusal(
                vc.BLOCKED, R_SCOPE_NEEDS_TOP,
                "scope %s covers more than one op, which needs the top level L6, not %s"
                % (scope_s, label))
        if not has_expiry:
            raise AuthorityRefusal(
                vc.BLOCKED, R_SCOPE_NEEDS_EXPIRY,
                "scope %s covers more than one op, which needs a parseable expiry; got %r"
                % (scope_s, expiry))

    expiry_s = "" if expiry is None else str(expiry).strip()
    aid = _next_id(conn)
    rec = {"id": aid, "level": label, "scope": scope_s, "expiry": expiry_s,
           "actor": actor or "owner", "pointer": str(pointer or ""),
           "supersedes": supersedes or ""}
    with db.transaction(conn):
        db.append_event(conn, session=session, actor=actor or "owner", kind=GRANT_KIND,
                        ref=aid, data=dict(rec), conn_in_txn=True)
    return rec


# --------------------------------------------------------------------------- revoke
def revoke(conn, id, actor, reason, *, session="cli") -> dict:
    """Append a revocation to the owner-appendable revocation channel. A revocation with no id or
    no reason is refused. A running loop honours it at its next iteration (it re-reads verify)."""
    rid = str(id or "").strip()
    if not rid:
        raise AuthorityRefusal(vc.BLOCKED, R_REVOKE_NO_ID, "a revocation names an authority id")
    reason_s = str(reason or "").strip()
    if not reason_s:
        raise AuthorityRefusal(vc.BLOCKED, R_REVOKE_NO_REASON,
                               "a revocation carries a reason; a bare revocation is refused")
    data = {"id": rid, "actor": actor or "owner", "reason": reason_s}
    db.append_event(conn, session=session, actor=actor or "owner", kind=REVOKE_KIND,
                    ref=rid, data=data)
    return dict(data)


# --------------------------------------------------------------------------- verify
def verify(conn, level=None, scope=None, now=None) -> Verdict:
    """Does the standing authority permit acting at `level` over `scope` at `now`?

    Reads the record every call (re-verification, Step 2): a loop that re-verifies each iteration
    sees an expiry or a revocation appended since the last call. Returns a Verdict; the reason
    token says why a refusal refused. `now` defaults to the process clock, so a FixedClock
    installed for a test drives it too.
    """
    now = now or util.now_iso()
    g = latest_grant(conn)
    if g is None:
        return Verdict(False, R_NO_AUTHORITY)
    if g.get("id") in _revoked_ids(conn):
        return Verdict(False, R_REVOKED, g)

    # a stored wide-scope authority still has to satisfy scope-implies-level; a record that no
    # longer does (a level lowered out of band elsewhere) does not authorize.
    if spans_multiple_ops(g.get("scope")):
        if defaults.level_num(g.get("level")) < TOP_LEVEL or not expiry_parseable(g.get("expiry")):
            return Verdict(False, R_SCOPE_NEEDS_TOP, g)

    exp = parse_dt(g.get("expiry"))
    if exp is not None:
        nd = parse_dt(now)
        if nd is None or nd >= exp:
            return Verdict(False, R_EXPIRED, g)

    if level is not None and defaults.level_num(g.get("level")) < defaults.level_num(level):
        return Verdict(False, R_LEVEL_NOT_COVERED, g)
    if scope is not None and not scope_covers(g.get("scope"), scope):
        return Verdict(False, R_SCOPE_NOT_COVERED, g)
    return Verdict(True, R_OK, g)


# --------------------------------------------------------------------------- bind to the op
def op_meta_key(op) -> str:
    return "authority:op:%s" % op


def bind_op(conn, op, *, session="cli", actor="alpaca") -> dict:
    """Stamp the standing authority an op ran under onto the op forever (Step 4): a BIND_KIND
    event on the append-only chain and a meta row `for_op` reads back. When no authority stands,
    the stamp records the absence (authority None), so a closed op always says what authorized it,
    or that nothing did."""
    auth = current(conn)
    stamp = {"authority": auth["id"] if auth else None,
             "level": auth["level"] if auth else None,
             "scope": auth["scope"] if auth else None}
    with db.transaction(conn):
        db.append_event(conn, session=session, actor=actor, kind=BIND_KIND, op=op,
                        ref=stamp["authority"], data=dict(stamp), conn_in_txn=True)
        db.meta_set(conn, op_meta_key(op), util.canonical_json(stamp))
    return stamp


def for_op(conn, op):
    """The authority stamp an op ran under, read back from the record, or None if never bound."""
    raw = db.meta_get(conn, op_meta_key(op))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
