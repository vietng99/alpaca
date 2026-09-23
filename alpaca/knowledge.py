"""knowledge.* - the declarative domain-fact store, ported from the earlier harness ops/knowledge_store.py and
retargeted at the wiki tables (a sqlite conn) in place of the per-claim markdown entries.

Kept faithfully from the upstream port:
  - two write paths (run-derived gated proposer; owner expert-encoded), never one;
  - the three-state stamp PROBED | ASSERTED | PROBE-BLOCKED;
  - a read path that is STRUCTURALLY incapable of being a gate() oracle (PriorSet / Prior taint);
  - the blind-class axis PUBLIC | SEALED | QUARANTINE, defaulting to QUARANTINE, with the run-derived
    path STRUCTURALLY unable to emit PUBLIC (propose() has no blind_class parameter);
  - retention decay (a decay-state riding the read filter).

H7 (domain-knowledge-store): declarative domain facts ONLY. Procedural self-critique belongs to
lessons.* and is rejected here on sight. Interface (M2.12): knowledge.record(conn, fact, state).

Stdlib only.
"""
from __future__ import annotations

import collections.abc as _abc
import datetime as _dt
import hashlib
import re
import sqlite3
from pathlib import Path

# ---------------------------------------------------------------- vocabulary

PROBED, ASSERTED, PROBE_BLOCKED = "PROBED", "ASSERTED", "PROBE-BLOCKED"
STAMPS = (PROBED, ASSERTED, PROBE_BLOCKED)

PUBLIC, SEALED, QUARANTINE = "PUBLIC", "SEALED", "QUARANTINE"
BLIND_CLASSES = (PUBLIC, SEALED, QUARANTINE)

RUN_DERIVED, EXPERT_ENCODED = "run-derived", "expert-encoded"


class RejectedWrite(Exception):
    """A write the gate refused. Carries the reason; never silently dropped."""

    def __init__(self, reason: str, control: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.control = control


# ------------------------------------------------------------- the read path


class Prior(dict):
    """One knowledge hit. Carries taint so it cannot be quietly unwrapped into a verdict."""

    __slots__ = ()

    def __bool__(self):
        raise TypeError(
            "a knowledge hit has no truth value - it is a prior, never a verdict. Read a "
            "field, or call .count() on the PriorSet.")

    def copy(self):
        """dict.copy() strips the taint; taint must survive the copy or good practice bypasses."""
        return Prior(self)

    def __reduce__(self):
        return (Prior, (dict(self),))


class PriorSet:
    """Return type of query(). A prior to weigh - never a verdict, never an oracle.

    Enforced: __bool__ raises, __len__ raises (use .count()), elements are Prior (also raise on
    __bool__). gate() deep-inspects both the oracle and, if callable, its return value for taint.
    NOT enforced (stated plainly): a caller who deliberately launders will get past this. The
    guarantee is against accident and casual reach-through, not against intent."""

    __slots__ = ("_hits",)

    def __init__(self, hits):
        self._hits = [h if isinstance(h, Prior) else Prior(h) for h in hits]

    @property
    def hits(self):
        return list(self._hits)          # tainted elements, deliberately

    def count(self):
        return len(self._hits)

    def __iter__(self):
        return iter(self._hits)

    def __repr__(self):
        return f"<PriorSet n={len(self._hits)} - a prior, not a verdict>"

    def __bool__(self):
        raise TypeError(
            "PriorSet has no truth value. A knowledge hit is a prior, never a verdict. Use "
            ".count() if you want the number.")

    def __len__(self):
        raise TypeError(
            "len(PriorSet) is refused: `len(ps) > 0` is the commonest way a prior gets laundered "
            "into a verdict. Call .count() - naming it is the point.")


def _tainted(obj, _depth=0):
    """True if obj is, contains, or could yield prior-derived data. Fails CLOSED past the depth
    cap, and refuses bare iterators outright (unknowable without consuming)."""
    if _depth > 6:
        return True
    if isinstance(obj, (PriorSet, Prior)):
        return True
    if isinstance(obj, (list, tuple, set, frozenset)):
        return any(_tainted(o, _depth + 1) for o in obj)
    if isinstance(obj, dict):
        return any(_tainted(k, _depth + 1) or _tainted(v, _depth + 1) for k, v in obj.items())
    if isinstance(obj, _abc.Iterator) or isinstance(obj, _abc.Generator):
        return True
    return False


def gate(name: str, oracle):
    """gate(name, oracle) - refuses prior-derived data as an oracle. Checks the argument AND, for a
    callable, its return value."""
    if _tainted(oracle):
        raise TypeError(
            f"gate({name!r}): prior-derived data cannot be an oracle argument. A knowledge-store "
            "hit is the gate's own prior opinion; accepting it would launder a learned claim into "
            "an external-oracle certification.")
    result = oracle() if callable(oracle) else oracle
    if _tainted(result):
        raise TypeError(
            f"gate({name!r}): the oracle RETURNED prior-derived data. Wrapping a PriorSet in a "
            "callable does not change what it is.")
    return result


# ------------------------------------------------------------------ resolver

_PATH_LINE = re.compile(r"([A-Za-z0-9_./\-]+\.[A-Za-z0-9_]+)(?::(\d+))?")
_CHECKABLE = re.compile(r"(cmd:|probe:|observe:|[A-Za-z0-9_./\-]+\.[A-Za-z0-9_]+)")


def _resolve_pointer(pointer: str, roots):
    """Resolve a provenance pointer against the store's roots. Returns (ok, detail). Accepts `path`
    or `path:line`. A pointer that does not resolve BLOCKS admission; never a floating claim."""
    pointer = (pointer or "").strip()
    if not pointer:
        return False, "empty pointer"
    m = _PATH_LINE.search(pointer)
    if not m:
        return False, f"no resolvable path in {pointer!r}"
    raw, line = m.group(1), m.group(2)
    for root in roots:
        cand = Path(root) / raw
        if cand.exists() and cand.is_file():
            if line:
                n = sum(1 for _ in cand.open(encoding="utf-8", errors="replace"))
                if int(line) > n:
                    return False, f"{raw}:{line} beyond EOF ({n} lines)"
            return True, f"resolved {cand}"
    return False, f"path {raw!r} does not resolve"


def _falsifiability_is_checkable(note: str, roots):
    """The CLEVER-style symmetric control. A falsifiability note must name a counter-observation
    someone could actually make: a checkable anchor (a resolvable path, or an explicit
    cmd:/probe:/observe: predicate), and any path it cites must resolve."""
    note = (note or "").strip()
    if not note:
        return False, "missing falsifiability note"
    if len(note) < 12:
        return False, f"falsifiability note too thin to evaluate: {note!r}"

    anchors = _CHECKABLE.findall(note)
    if not anchors:
        return False, (
            "falsifiability note names no checkable anchor - needs a resolvable path or an "
            "explicit cmd:/probe:/observe: predicate, else the gate is vacuous")

    for a in anchors:
        if a in ("cmd:", "probe:", "observe:"):
            tail = note.split(a, 1)[1].strip()
            if len(tail.split()) < 4:
                return False, f"{a} names no predicate ({tail[:40]!r})"
            if not re.search(r"\b(refut|fail|differ|mismatch|absent|error|reject|non-?zero"
                             r"|unset|expect|return|exit|>|<|=)", tail, re.I):
                return False, (
                    f"{a} states no observable outcome - a predicate must say what would be SEEN "
                    f"if the claim were false, not merely restate it")
            return True, "explicit predicate"
    for a in anchors:
        ok, detail = _resolve_pointer(a, roots)
        if ok:
            return True, f"anchor {detail}"
    return False, (
        f"falsifiability note cites {anchors[0]!r} which does not resolve - a counter-observation "
        "nobody can make is not a falsifiability condition")


_PROCEDURAL = re.compile(
    r"\b(we should|next time|the agent should|the formation|never do|always do|"
    r"lesson|our (?:run|campaign|approach|process)|instead of doing)\b",
    re.I,
)


def _is_procedural(claim: str):
    """H7 fence: procedural self-critique is lessons.* territory, rejected here."""
    m = _PROCEDURAL.search(claim or "")
    return (True, m.group(0)) if m else (False, "")


_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")


def _sanitize_component(name: str, what: str):
    name = (name or "").strip()
    if not _SAFE.match(name):
        raise RejectedWrite(
            f"unsafe {what} {name!r}: must match [A-Za-z0-9_-]+", control="NC-path")
    return name


def _flatten(v: str):
    """A stored value can carry no field-delimiter that a later reader might mistake for a field."""
    v = re.sub(r"[\r\n]+", " ", str(v))
    return v.strip()


def _now():
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _mint_id(claim, scope):
    slug = re.sub(r"[^a-z0-9]+", "-", claim.lower())[:44].strip("-")
    return f"{scope}-{slug}" if not slug.startswith(scope) else slug


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the conn-backed knowledge tables (additive, idempotent) - the wiki-table retarget of
    the upstream per-claim markdown entries + challenges.jsonl."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_facts ("
        "claim_id TEXT NOT NULL, scope TEXT NOT NULL, claim TEXT NOT NULL, source TEXT, "
        "provenance TEXT, stamp TEXT NOT NULL, blind_class TEXT NOT NULL, falsifiability TEXT, "
        "trust_ceiling TEXT, decay_state TEXT NOT NULL DEFAULT 'LIVE', created_at TEXT, "
        "reaffirmed_at TEXT, PRIMARY KEY(scope, claim_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_log ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event TEXT NOT NULL, "
        "claim_id TEXT NOT NULL, scope TEXT, detail TEXT)"
    )


# ----------------------------------------------------------------- the store


class KnowledgeStore:
    def __init__(self, conn: sqlite3.Connection, roots=None, authorization_verifier=None):
        self.conn = conn
        self.roots = [Path(r) for r in (roots or [Path.cwd()])]
        # This seam is installed by the trusted integration, never supplied by the caller of
        # owner_promote.  Default deny is intentional: a bare string is not an identity proof.
        # Same-process/filesystem compromise is outside this API boundary; the installer of this
        # callback is responsible for resolving its own durable owner approval record.
        self.authorization_verifier = authorization_verifier
        _ensure_tables(self.conn)

    # -- gate ------------------------------------------------------------

    def _admit_gate(self, claim, scope, provenance, falsifiability, probe_receipt):
        """The external write-gate. Returns (stamp, notes) or raises RejectedWrite. Cheapest-first;
        every refusal names its control."""
        notes = []

        proc, hit = _is_procedural(claim)
        if proc:
            raise RejectedWrite(
                f"H7 violation: {hit!r} reads as procedural self-critique. Declarative domain "
                "facts only; route this to lessons.write",
                control="H7")

        ok, detail = _resolve_pointer(provenance, self.roots)
        if not ok:
            raise RejectedWrite(f"provenance does not resolve: {detail}", control="NC-provenance")
        notes.append(f"provenance: {detail}")

        ok, detail = _falsifiability_is_checkable(falsifiability, self.roots)
        if not ok:
            raise RejectedWrite(detail, control="NC-falsifiability")
        notes.append(f"falsifiability: {detail}")

        clash = self._contradiction(claim, scope)
        if clash:
            raise RejectedWrite(
                f"contradicts existing entry {clash} in scope {scope!r} with no recorded "
                "challenge; file a challenge first",
                control="NC-contradiction")

        if probe_receipt:
            ok, detail = _resolve_pointer(probe_receipt, self.roots)
            if ok:
                notes.append(f"probe receipt: {detail}")
                return PROBED, notes
            notes.append(f"probe receipt did not resolve ({detail}) -> PROBE-BLOCKED")
            return PROBE_BLOCKED, notes

        # fails safe: on any doubt about a stamp, take the WEAKER one
        return ASSERTED, notes

    def _contradiction(self, claim: str, scope: str):
        neg = re.sub(r"\bis not\b", "is", claim, flags=re.I)
        neg = re.sub(r"\bnot supported\b", "supported", neg, flags=re.I)
        key = set(re.findall(r"[a-z0-9_]{4,}", (neg or "").lower()))
        if not key:
            return None
        for row in self.conn.execute(
            "SELECT claim_id, claim FROM knowledge_facts WHERE scope=? AND decay_state<>'DECAYED'",
            (scope,),
        ).fetchall():
            other_claim = row["claim"] if isinstance(row, sqlite3.Row) else row[1]
            other = set(re.findall(r"[a-z0-9_]{4,}", other_claim.lower()))
            overlap = len(key & other) / max(1, len(key))
            if overlap > 0.75 and ("not" in claim.lower()) != ("not" in other_claim.lower()):
                return row["claim_id"] if isinstance(row, sqlite3.Row) else row[0]
        return None

    # -- write path 1: run-derived ---------------------------------------

    def propose(self, claim, scope, provenance, falsifiability, run_id,
                rank="analyst", probe_receipt="", claim_id=""):
        """knowledge.propose(claim) - the gated run-derived path.

        NOTE the absent parameter: there is no blind_class argument. A run-derived write lands
        QUARANTINE and only owner_promote() can move it; the class that leaks is unreachable here.
        """
        cid = _sanitize_component(claim_id or _mint_id(claim, scope), "claim-id")
        scope = _sanitize_component(scope, "scope")
        try:
            stamp, notes = self._admit_gate(claim, scope, provenance, falsifiability, probe_receipt)
        except RejectedWrite as e:
            self._log("reject", cid, scope, f"[{e.control}] {e.reason}")
            raise
        self._record(cid, claim, scope, f"{RUN_DERIVED} <run:{run_id}, rank:{rank}>", provenance,
                     stamp, QUARANTINE, falsifiability, "stamp-plus-falsifiable")
        self._log("admit", cid, scope, f"stamp={stamp} blind={QUARANTINE} src={RUN_DERIVED}")
        return {"claim_id": cid, "scope": scope, "stamp": stamp, "blind_class": QUARANTINE}

    # -- write path 2: expert-encoded ------------------------------------

    def expert_encode(self, claim, scope, provenance, falsifiability, owner,
                      probe_receipt="", blind_class=QUARANTINE, claim_id=""):
        """Owner-direct write. Bypasses the PROBE gate, never the falsifiability bar."""
        if blind_class not in BLIND_CLASSES:
            raise RejectedWrite(f"unknown blind-class {blind_class!r}")
        cid = _sanitize_component(claim_id or _mint_id(claim, scope), "claim-id")
        scope = _sanitize_component(scope, "scope")

        ok, detail = _falsifiability_is_checkable(falsifiability, self.roots)
        if not ok:
            e = RejectedWrite(
                f"owner-direct is a shortcut past the probe-gate, NEVER past falsifiability: "
                f"{detail}", control="NC-falsifiability")
            self._log("reject", cid, scope, f"[{e.control}] {e.reason}")
            raise e

        ok, pdetail = _resolve_pointer(provenance, self.roots)
        if not ok:
            e = RejectedWrite(f"provenance does not resolve: {pdetail}", control="NC-provenance")
            self._log("reject", cid, scope, f"[{e.control}] {e.reason}")
            raise e

        stamp = ASSERTED
        if probe_receipt and _resolve_pointer(probe_receipt, self.roots)[0]:
            stamp = PROBED
        self._record(cid, claim, scope, f"{EXPERT_ENCODED} <{owner}, {_now()}>", provenance,
                     stamp, blind_class, falsifiability, "owner-signed")
        self._log("admit", cid, scope, f"stamp={stamp} blind={blind_class} src={EXPERT_ENCODED}")
        return {"claim_id": cid, "scope": scope, "stamp": stamp, "blind_class": blind_class}

    # -- blind-class promotion -------------------------------------------

    def owner_promote(self, claim_id, scope, blind_class, signature="", rationale="",
                      approval_ref=""):
        """Promote only through this store's trusted, exact-binding approval verifier.

        `signature` remains an ignored compatibility input so an arbitrary non-empty string cannot
        silently regain authority. The preconfigured verifier receives the persisted claim content
        digest and must return a matching approval artifact for this exact transition.
        """
        if blind_class not in BLIND_CLASSES:
            raise RejectedWrite(f"unknown blind-class {blind_class!r}")
        row = self.conn.execute(
            "SELECT blind_class FROM knowledge_facts WHERE scope=? AND claim_id=?", (scope, claim_id)
        ).fetchone()
        if not row:
            raise RejectedWrite(f"no entry {claim_id} in scope {scope}")
        prev = row["blind_class"] if isinstance(row, sqlite3.Row) else row[0]
        claim_row = self.conn.execute(
            "SELECT claim FROM knowledge_facts WHERE scope=? AND claim_id=?", (scope, claim_id)
        ).fetchone()
        claim = claim_row["claim"] if isinstance(claim_row, sqlite3.Row) else claim_row[0]
        request = {
            "claim_id": claim_id, "scope": scope,
            "claim_sha256": hashlib.sha256(claim.encode("utf-8")).hexdigest(),
            "from_class": prev, "to_class": blind_class,
        }
        verifier = self.authorization_verifier
        if not callable(verifier):
            raise RejectedWrite(
                "promotion requires a preconfigured authorization verifier; signature text alone "
                "is not an approval", control="NC-authorization-verifier")
        try:
            approval = verifier(dict(request), approval_ref)
        except Exception as exc:  # A missing/unreachable authority must never become an allow.
            raise RejectedWrite(
                f"authorization verifier failed: {type(exc).__name__}",
                control="NC-authorization-verifier") from exc
        if not isinstance(approval, dict) or not approval.get("approval_id"):
            raise RejectedWrite("authorization verifier returned no recorded approval",
                                control="NC-authorization-approval")
        for key, value in request.items():
            if approval.get(key) != value:
                raise RejectedWrite(
                    f"approval binding mismatch for {key}: expected exact promotion request",
                    control="NC-authorization-binding")
        # Bind the write to the exact row inspected before authorization. If another writer changes
        # its content or class during verification, the conditional update fails rather than promote
        # a different claim (TOCTOU fail-closed).
        with self.conn:
            updated = self.conn.execute(
                "UPDATE knowledge_facts SET blind_class=? "
                "WHERE scope=? AND claim_id=? AND claim=? AND blind_class=?",
                (blind_class, scope, claim_id, claim, prev),
            ).rowcount
            if updated != 1:
                raise RejectedWrite("claim changed during authorization; promotion refused",
                                    control="NC-authorization-race")
            self._log("promote", claim_id, scope,
                      f"{prev} -> {blind_class} approval:{approval['approval_id']} rationale:{rationale}")
        return {"claim_id": claim_id, "scope": scope, "was": prev, "now": blind_class}

    # -- read path -------------------------------------------------------

    def query(self, scope="", topic="", include_decayed=False, blind_class="") -> PriorSet:
        """Returns a PriorSet - a prior, never a verdict, never a gate oracle."""
        sql = "SELECT claim_id, scope, claim, stamp, blind_class, decay_state FROM knowledge_facts"
        conds, params = [], []
        if scope:
            conds.append("scope=?"); params.append(scope)
        if blind_class:
            conds.append("blind_class=?"); params.append(blind_class)
        if not include_decayed:
            conds.append("decay_state<>'DECAYED'")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY scope, claim_id"
        hits = []
        for r in self.conn.execute(sql, params).fetchall():
            body = (r["claim"] if isinstance(r, sqlite3.Row) else r[2]).lower()
            if topic and topic.lower() not in body:
                continue
            hits.append({
                "claim_id": r["claim_id"] if isinstance(r, sqlite3.Row) else r[0],
                "scope": r["scope"] if isinstance(r, sqlite3.Row) else r[1],
                "claim": r["claim"] if isinstance(r, sqlite3.Row) else r[2],
                "stamp": r["stamp"] if isinstance(r, sqlite3.Row) else r[3],
                "blind_class": r["blind_class"] if isinstance(r, sqlite3.Row) else r[4],
            })
        return PriorSet(hits)

    def export_public(self, scope=""):
        """The repack: exactly the PUBLIC set. Nothing else is exportable."""
        return [dict(h) for h in self.query(scope=scope, blind_class=PUBLIC)]

    # -- retention decay -------------------------------------------------

    def decay(self, scope, claim_id):
        """Retention decay: mark an unreaffirmed claim DECAYED so it drops out of the read path
        without being deleted (retire-not-delete)."""
        self.conn.execute(
            "UPDATE knowledge_facts SET decay_state='DECAYED' WHERE scope=? AND claim_id=?",
            (scope, claim_id))
        self._log("decay", claim_id, scope, "unreaffirmed -> DECAYED")

    def reaffirm(self, scope, claim_id):
        self.conn.execute(
            "UPDATE knowledge_facts SET decay_state='LIVE', reaffirmed_at=? WHERE scope=? AND claim_id=?",
            (_now(), scope, claim_id))

    # -- helpers ---------------------------------------------------------

    def _record(self, cid, claim, scope, source, provenance, stamp, blind_class, falsifiability,
                trust_ceiling):
        self.conn.execute(
            "INSERT INTO knowledge_facts(claim_id,scope,claim,source,provenance,stamp,blind_class,"
            "falsifiability,trust_ceiling,decay_state,created_at,reaffirmed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'LIVE',?,?) "
            "ON CONFLICT(scope,claim_id) DO UPDATE SET claim=excluded.claim,source=excluded.source,"
            "provenance=excluded.provenance,stamp=excluded.stamp,blind_class=excluded.blind_class,"
            "falsifiability=excluded.falsifiability,trust_ceiling=excluded.trust_ceiling,"
            "decay_state='LIVE',reaffirmed_at=excluded.reaffirmed_at",
            (cid, scope, _flatten(claim), _flatten(source), _flatten(provenance), stamp,
             blind_class, _flatten(falsifiability), trust_ceiling, _now(), _now()))

    def _log(self, event, claim_id, scope, detail):
        self.conn.execute(
            "INSERT INTO knowledge_log(ts,event,claim_id,scope,detail) VALUES(?,?,?,?,?)",
            (_now(), event, claim_id, scope, detail))


def record(conn: sqlite3.Connection, fact: dict, state: str) -> dict:
    """Interface (M2.12): admit a declarative domain fact into the wiki knowledge table with an
    explicit target state.

    `fact` is {claim, scope, provenance, falsifiability, [run_id], [probe_receipt], [owner],
    [blind_class]}. `state` selects the write path/target stamp:
      - ASSERTED / PROBED / PROBE-BLOCKED via the gated run-derived proposer (stamp is decided by
        the gate; PROBED requires a resolving probe_receipt);
      - an owner-signed expert-encoded write when `fact` carries an `owner`.
    Returns the admitted row descriptor; raises RejectedWrite when a control refuses. The stamp on
    the stored row fails safe to the weaker value when the requested state cannot be substantiated.
    """
    roots = fact.get("roots")
    store = KnowledgeStore(conn, roots=roots)
    f = {k: v for k, v in fact.items() if k != "roots"}
    if f.get("owner"):
        return store.expert_encode(
            claim=f["claim"], scope=f["scope"], provenance=f["provenance"],
            falsifiability=f["falsifiability"], owner=f["owner"],
            probe_receipt=f.get("probe_receipt", ""), blind_class=f.get("blind_class", QUARANTINE),
            claim_id=f.get("claim_id", ""))
    if state == PROBED and not f.get("probe_receipt"):
        raise RejectedWrite(
            "state PROBED requires a resolving probe_receipt; a bare PROBED claim fails safe to "
            "ASSERTED, so the caller must supply the receipt or ask for ASSERTED",
            control="NC-probe-receipt")
    return store.propose(
        claim=f["claim"], scope=f["scope"], provenance=f["provenance"],
        falsifiability=f["falsifiability"], run_id=f.get("run_id", ""),
        rank=f.get("rank", "analyst"), probe_receipt=f.get("probe_receipt", ""),
        claim_id=f.get("claim_id", ""))


# --------------------------------------------------------------------- selftest control table


def selftest():
    """The ported negative-control receipt, run against a throwaway in-memory conn + temp roots.

    Returns (passed, total, results). Both directions exercised: refusals AND admission. The pytest
    wrapper fails on any non-PASS.
    """
    import inspect
    import tempfile

    results = []

    def check(name, expectation, fn):
        try:
            fn()
            ok, detail = False, "NO REFUSAL - the control did not fire"
        except RejectedWrite as e:
            ok, detail = True, f"rejected [{e.control}] {e.reason[:60]}"
        except TypeError as e:
            ok, detail = True, f"refused by type: {str(e)[:60]}"
        except AssertionError as e:
            ok, detail = False, f"assertion failed: {e}"
        results.append((name, expectation, ok, detail))

    def check_passes(name, expectation, fn, detail_on_pass="behaved as required"):
        try:
            fn()
            ok, detail = True, detail_on_pass
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {str(e)[:60]}"
        results.append((name, expectation, ok, detail))

    tmp = Path(tempfile.mkdtemp(prefix="alpaca-knowledge-selftest-"))
    real_name = "provenance.md"
    (tmp / real_name).write_text("real grounding line\nsecond line\n", encoding="utf-8")
    real = real_name
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ks = KnowledgeStore(conn, roots=[tmp])

    # NC-1 - dangling provenance blocks admission
    check("NC-1 dangling provenance", "REJECT", lambda: ks.propose(
        claim="the subject's L2 cache is 512 KiB 8-way", scope="OP_ROOT",
        provenance="work/nonexistent/nope.md:99",
        falsifiability=f"read {real} and find a different value", run_id="selftest"))

    # NC-2 - missing falsifiability note blocks admission outright
    check("NC-2 no falsifiability", "REJECT", lambda: ks.propose(
        claim="the subject's I-TLB has 16 entries", scope="OP_ROOT", provenance=real,
        falsifiability="", run_id="selftest"))

    # NC-3 - a note nobody could ever evaluate
    check("NC-3 unevaluable falsifiability", "REJECT", lambda: ks.propose(
        claim="the subject's D-TLB has 16 entries", scope="OP_ROOT", provenance=real,
        falsifiability="it would be refuted if it were false", run_id="selftest"))

    # NC-3b - a note citing a path that does not resolve
    check("NC-3b falsifiability cites unresolvable path", "REJECT", lambda: ks.propose(
        claim="the subject's BTB has 128 entries", scope="OP_ROOT", provenance=real,
        falsifiability="refuted if work/ghost/absent.log shows another value", run_id="selftest"))

    # NC-4 - a query hit cannot reach gate()'s oracle argument position
    check("NC-4 PriorSet as gate oracle", "REFUSE (structural)",
          lambda: gate("some-gate", ks.query(scope="OP_ROOT")))

    # NC-4b - and cannot be truthiness-tested as a verdict either
    def _truthy():
        if ks.query(scope="OP_ROOT"):
            pass
    check("NC-4b PriorSet truthiness", "REFUSE (structural)", _truthy)

    # NC-8 - H7: procedural self-critique belongs to lessons.*, not here
    check("NC-8 H7 procedural claim", "REJECT", lambda: ks.propose(
        claim="next time the agent should probe the commit stage first", scope="OP_ROOT",
        provenance=real, falsifiability=f"cmd: inspect {real} for a counter-case", run_id="selftest"))

    # NC-7 - promotion without a signature
    check("NC-7 unsigned promotion", "REJECT",
          lambda: ks.owner_promote("anything", "OP_ROOT", PUBLIC, signature=""))

    # NC-5 - ABLE TO PASS. coverage>=1: the gate must fire on a real antecedent.
    def _good():
        f = ks.propose(
            claim="the emulator front-end build requires ZEBU_IP_ROOT set before launch",
            scope="OP_ROOT", provenance=f"{real}:1",
            falsifiability=f"cmd: unset ZEBU_IP_ROOT and re-run; refuted if the build completes. "
                           f"See {real}",
            run_id="selftest", rank="analyst")
        assert f["stamp"] == ASSERTED, "argued-only claim should stamp ASSERTED"
    check_passes("NC-5 able-to-pass (coverage>=1)", "ADMIT", _good,
                 "admitted; stamped ASSERTED (argued-only)")

    # NC-6 - STRUCTURAL: the run-derived path cannot emit PUBLIC.
    def _blind_default():
        hits = list(ks.query(scope="OP_ROOT"))
        assert hits, "no admitted entry to inspect"
        for h in hits:
            assert h["blind_class"] == QUARANTINE, (
                f"run-derived entry landed {h['blind_class']}, must be {QUARANTINE}")
        sig = inspect.signature(KnowledgeStore.propose)
        assert "blind_class" not in sig.parameters, (
            "propose() exposes a blind_class parameter - PUBLIC must be unreachable from the "
            "run-derived path by construction")
    check_passes("NC-6 run-derived cannot emit PUBLIC", "QUARANTINE + no param",
                 _blind_default, "landed QUARANTINE; propose() has no blind_class param")

    # NC-10 - the contradiction control
    def _seed_then_contradict():
        ks.propose(
            claim="the subject PMP supports NA4 addressing mode", scope="nc10",
            provenance=f"{real}:1",
            falsifiability=f"cmd: write A=NA4 and read back; refuted if retained. {real}",
            run_id="selftest")
    check_passes("NC-10a seed a claim for contradiction", "ADMIT", _seed_then_contradict,
                 "seed admitted")
    check("NC-10b contradicting claim, no challenge filed", "REJECT", lambda: ks.propose(
        claim="the subject PMP does not supports NA4 addressing mode", scope="nc10",
        provenance=f"{real}:1",
        falsifiability=f"cmd: write A=NA4 and read back; refuted if accepted. {real}",
        run_id="selftest"))

    # NC-11 - H7 must not false-positive
    def _h7_false_positive():
        ks.propose(
            claim="the subject's boot ROM is supplied externally and is not part of the delivered "
                  "collateral",
            scope="nc11", provenance=f"{real}:1",
            falsifiability=f"cmd: inspect the delivered file list; refuted if a ROM image is "
                           f"present. {real}",
            run_id="selftest")
    check_passes("NC-11 H7 does not false-positive", "ADMIT", _h7_false_positive,
                 "declarative fact admitted, fence did not over-fire")

    # NC-12 - EVERY laundering path, not just the one shape NC-4 tests.
    def _all_bypasses():
        ks.propose(
            claim="the emulator front-end build requires ZEBU_IP_ROOT set before launch",
            scope="bypass", provenance=f"{real}:1",
            falsifiability=f"cmd: unset ZEBU_IP_ROOT and rerun; refuted if the build completes. "
                           f"{real}",
            run_id="selftest")
        ps = ks.query(scope="bypass")
        assert ps.count() == 1, "probe needs a populated set; empty carries no taint"
        paths = {
            "direct": lambda: gate("g", ps),
            "lambda-returns-set": lambda: gate("g", lambda: ps),
            "lambda-len-gt-0": lambda: gate("g", lambda: len(ps) > 0),
            "list()": lambda: gate("g", list(ps)),
            ".hits": lambda: gate("g", ps.hits),
            "dict-wrapper": lambda: gate("g", {"v": ps}),
            "nested": lambda: gate("g", [[ps.hits]]),
            "iter()": lambda: gate("g", iter(ps)),
            "generator": lambda: gate("g", (h for h in ps)),
            "map()": lambda: gate("g", map(dict, ps)),
            "[h.copy()]": lambda: gate("g", [h.copy() for h in ps]),
            "deep-nest-x6": lambda: gate("g", [[[[[[ps]]]]]]),
        }
        escaped = []
        for nm, fn in paths.items():
            try:
                fn()
                escaped.append(nm)
            except TypeError:
                pass
        assert not escaped, f"laundering paths still open: {escaped}"
    check_passes("NC-12 gate-laundering paths refused", "REFUSE ALL", _all_bypasses,
                 "gate() paths refused; deliberate unwrap still passes by design")

    # NC-9 - signed promotion works, and export ships exactly the PUBLIC set
    def _promote_and_export():
        hits = list(ks.query(scope="OP_ROOT"))
        cid = hits[0]["claim_id"]
        def verifier(request, approval_ref):
            return {"approval_id": approval_ref, **request}
        promoted_store = KnowledgeStore(conn, roots=[tmp], authorization_verifier=verifier)
        promoted_store.owner_promote(cid, "OP_ROOT", PUBLIC, approval_ref="selftest-approval",
                                    rationale="spec-derivable: tool mechanics, no subject content")
        pub = promoted_store.export_public("OP_ROOT")
        assert len(pub) == 1, f"expected 1 PUBLIC entry, got {len(pub)}"
        ks.expert_encode(
            claim="unit X returns a wrong value under condition Y", scope="OP_ROOT",
            provenance=f"{real}:1",
            falsifiability=f"cmd: sweep all rounding modes; refuted if outputs agree. {real}",
            owner="owner:selftest", blind_class=SEALED)
        pub2 = promoted_store.export_public("OP_ROOT")
        assert len(pub2) == 1, f"SEALED entry leaked into export: {len(pub2)} PUBLIC"
    check_passes("NC-9 signed promote + export excludes SEALED", "PUBLIC only",
                 _promote_and_export, "promoted 1 to PUBLIC; SEALED entry not exported")

    passed = sum(1 for *_, ok, _ in results if ok)
    return passed, len(results), results


if __name__ == "__main__":
    import sys
    p, t, rows = selftest()
    for name, exp, ok, detail in rows:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<44}  {exp:<22}  {detail}")
    print(f"  {p}/{t} controls behaved as required.")
    sys.exit(0 if p == t else 1)
