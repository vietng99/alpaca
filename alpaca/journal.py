"""The verbatim journal (M3.6). Ported from the earlier harness capture.py, section 6 ADAPT row 94.

What this is
------------
An append-only, length-framed, hash-chained journal of RAW TURN BYTES. A turn is written to disk
and hashed BEFORE anything is derived from it. Interpretation is then performed on the bytes read
back FROM DISK, never on the live turn that is still in a variable.

Why the ordering is structural and not a convention
---------------------------------------------------
`append_turn()` returns a `Ref` -- a seq number and a hash. It does NOT return anything a reader
will accept. A reader accepts only a `Captured`, and a `Captured` can only be produced by
`load_record()`, which re-reads the journal file, re-derives the payload hash from the raw bytes on
disk and compares it to the recorded one. Constructing a `Captured` by hand raises. So "paraphrase
first, capture later" is not a discipline anyone has to remember: there is no call sequence that
expresses it.

What was dropped in the port
----------------------------
The earlier harness capture.py bound a captured turn to a SIGNER identity: `parse_identity`,
`parse_signature`, the whole confusable-identity comparator (`identity_key`, `identity_skeleton`,
`identity_conflict`, the projection tables) and their `--selftest` controls (K-01, K-13..K-15,
K-21..K-30). This harness has no signer surface: intent is the done-bar (alpaca.intent), authorship is a
self-asserted actor token on the append-only record, and a name is never compared for personhood
here. So the signature parsers are DROPPED. What remains is the verbatim journal core: capture the
bytes, hash-chain them, and let nothing be read except from disk.

LIMITS (do not soften)
----------------------
1. Append-only is enforced by this program, not by the filesystem.
2. The pin is written by the same writer as the journal. A tamperer with write access to both
   produces a consistent pair, and `verify_journal` says PASS. `expect_head` is the seam where a
   genuinely external anchor plugs in; the selftest demonstrates both directions.

Self-verification: `journal.selftest()` -- every control binds to a named bypass and asserts the
exact reason code the code emitted. `tests/test_intent_done_bar.py` wraps it and fails on any
control that did not fire.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import re
import tempfile

from alpaca import db, paths, util

#: the journal's own advisory verdict words. These are NOT exit codes: the journal is a byte-level
#: file mechanism with no CLI boundary of its own, so it carries reason words, not the exit-code
#: contract (alpaca.gates.verdict owns that for the CLI verbs that consume this module).
PASS_V, FAIL_V, BLOCKED_V = "PASS", "FAIL", "BLOCKED"

SCHEMA = 1
MAGIC = b"CAPTURE-JOURNAL/1 algo=sha256 payload=opaque-bytes\n"
CHAIN_DOMAIN = b"capture-journal/v1\n"
HEADER_RE = re.compile(
    rb"^REC seq=(\d+) utc=(\S+) len=(\d+) sha256=([0-9a-f]{64}) prev=([0-9a-f]{64}) "
    rb"role=(\S*) label=(\S*)$")
MAX_TURN_BYTES = 1 << 20

#: the record channel a captured turn is cited on, so the append-only record points at the disk
#: journal. The journal file is the verbatim truth; this event is the pointer back to it.
JOURNAL_KIND = "journal-capture"


class Refusal(Exception):
    """A refusal carrying its verdict WORD and its EXACT reason code. Controls bind to these."""

    def __init__(self, verdict_name, reason, detail=""):
        super().__init__("%s: %s%s" % (verdict_name, reason, (" -- " + detail) if detail else ""))
        self.verdict = verdict_name
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- raw-byte primitives
def sha256_hex(data):
    if not isinstance(data, (bytes, bytearray)):
        raise Refusal(BLOCKED_V, "HASH-NOT-BYTES", type(data).__name__)
    return hashlib.sha256(bytes(data)).hexdigest()


def read_raw(path, what="file"):
    """Binary read, existence-checked. Nothing is decoded and nothing is stripped here."""
    if not os.path.isfile(path):
        raise Refusal(BLOCKED_V, "POINTER-ABSENT", "%s: %s" % (what, path))
    with io.open(path, "rb") as fh:
        return fh.read()


def decode_strict(raw, what):
    """Strict utf-8. A foreign encoding BLOCKS; it is never repaired into a clean string."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refusal(BLOCKED_V, "DECODE-FAILED-NOT-UTF8", "%s: %s" % (what, exc))


def atomic_write_bytes(path, data):
    """Write-then-rename. open(...,'w') truncates before it writes, so a captured turn is never
    updated in place: a fresh temp file is fsynced and renamed over the target."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = os.path.join(directory, ".%s.part" % os.path.basename(path))
    n = 0
    while os.path.exists(tmp):
        n += 1
        tmp = os.path.join(directory, ".%s.part%d" % (os.path.basename(path), n))
    with io.open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def append_raw(path, data):
    """Binary append. One write, then fsync. No newline translation can reach these bytes."""
    with io.open(path, "ab") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def utc_now():
    """The stamp for a header, routed through util.now_iso so a FixedClock drives it under test."""
    return util.now_iso()


# --------------------------------------------------------------------------- the chain
def magic_hash_of(magic_bytes):
    return sha256_hex(CHAIN_DOMAIN + magic_bytes)


def chain_seed(magic_hash):
    return sha256_hex(CHAIN_DOMAIN + magic_hash.encode("ascii"))


def chain_step(prev_head, record_bytes):
    """The prior head is folded IN, so the head is cumulative over every preceding byte."""
    return sha256_hex(prev_head.encode("ascii") + b"\n" + record_bytes)


def _q(s):
    """Percent-encode a header value so a header line is always one ascii line."""
    import urllib.parse
    return urllib.parse.quote(("" if s is None else str(s)), safe="")


def _unq(b):
    import urllib.parse
    return urllib.parse.unquote(b.decode("ascii"))


def _header_bytes(seq, utc, length, sha, prev, role, label):
    return ("REC seq=%d utc=%s len=%d sha256=%s prev=%s role=%s label=%s\n"
            % (seq, utc, length, sha, prev, _q(role), _q(label))).encode("ascii")


# --------------------------------------------------------------- the Captured object (only reader)
_DISK_TOKEN = object()


class Ref(object):
    """What append_turn() hands back. Deliberately NOT a reader input: it holds no payload."""

    __slots__ = ("journal", "seq", "sha256", "utc", "head")

    def __init__(self, journal, seq, sha, utc, head):
        self.journal = journal
        self.seq = seq
        self.sha256 = sha
        self.utc = utc
        self.head = head

    def as_dict(self):
        return {"journal": self.journal, "seq": self.seq, "sha256": self.sha256,
                "utc": self.utc, "head": self.head}

    def __repr__(self):
        return "Ref(seq=%d, sha256=%s...)" % (self.seq, self.sha256[:12])


class Captured(object):
    """Raw bytes READ BACK FROM DISK, with the payload hash re-derived and matched.

    There is no public constructor. `load_record()` is the only producer, and it only produces one
    after the bytes on disk hashed to the recorded value. This is the structural half of "verbatim
    before interpretation": a reader cannot be handed anything else.
    """

    __slots__ = ("journal", "seq", "utc", "sha256", "role", "label", "_payload")

    def __init__(self, _token=None, journal=None, seq=None, utc=None, sha=None,
                 role=None, label=None, payload=None):
        if _token is not _DISK_TOKEN:
            raise Refusal(BLOCKED_V, "CAPTURE-CONSTRUCT-FORBIDDEN",
                          "a Captured is only obtainable from load_record(); building one by hand "
                          "would let interpretation precede capture")
        self.journal = journal
        self.seq = seq
        self.utc = utc
        self.sha256 = sha
        self.role = role
        self.label = label
        self._payload = payload

    def raw(self):
        """The exact bytes that were captured. No normalisation, ever."""
        return self._payload

    def text(self):
        """Strict utf-8 decode. A non-utf-8 turn BLOCKS -- it is never silently repaired."""
        return decode_strict(self._payload, "captured turn seq=%s" % self.seq)

    def __repr__(self):
        return "Captured(seq=%s, sha256=%s..., len=%d)" % (
            self.seq, self.sha256[:12], len(self._payload))


# ----------------------------------------------- journal reading: framing, chain, payload re-hash
def pin_path_for(journal_path):
    return journal_path + ".pin.json"


def _parse_records(raw, journal_path):
    if not raw.startswith(MAGIC):
        raise Refusal(BLOCKED_V, "JOURNAL-MAGIC-UNKNOWN",
                      "%s does not begin with this journal format's exact magic line"
                      % journal_path)
    mh = magic_hash_of(MAGIC)
    head = chain_seed(mh)
    pos = len(MAGIC)
    records = []
    while pos < len(raw):
        nl = raw.find(b"\n", pos)
        if nl < 0:
            raise Refusal(FAIL_V, "FRAMING-HEADER-UNTERMINATED", "at byte %d" % pos)
        header = raw[pos:nl]
        m = HEADER_RE.match(header)
        if not m:
            raise Refusal(FAIL_V, "FRAMING-HEADER-MALFORMED", "at byte %d" % pos)
        seq = int(m.group(1))
        utc = m.group(2).decode("ascii")
        length = int(m.group(3))
        rec_sha = m.group(4).decode("ascii")
        prev = m.group(5).decode("ascii")
        role = _unq(m.group(6))
        label = _unq(m.group(7))
        start = nl + 1
        end = start + length
        if end + 1 > len(raw):
            raise Refusal(FAIL_V, "FRAMING-PAYLOAD-TRUNCATED",
                          "record seq=%d declares len=%d, file ends early" % (seq, length))
        payload = raw[start:end]
        if raw[end:end + 1] != b"\n":
            raise Refusal(FAIL_V, "FRAMING-SEPARATOR-MISSING", "record seq=%d" % seq)
        record_bytes = raw[pos:end + 1]
        if seq != len(records) + 1:
            raise Refusal(FAIL_V, "SEQ-OUT-OF-ORDER",
                          "record %d carries seq=%d" % (len(records) + 1, seq))
        # The recorded sha is a PRODUCER TOKEN. Re-derive it from the bytes and compare.
        got = sha256_hex(payload)
        if got != rec_sha:
            raise Refusal(FAIL_V, "CAPTURE-HASH-MISMATCH",
                          "record seq=%d records sha256=%s; its bytes hash to %s"
                          % (seq, rec_sha[:16], got[:16]))
        if prev != head:
            raise Refusal(FAIL_V, "CHAIN-BREAK",
                          "record seq=%d prev=%s, expected %s" % (seq, prev[:16], head[:16]))
        head = chain_step(head, record_bytes)
        records.append({"seq": seq, "utc": utc, "sha256": got, "role": role, "label": label,
                        "payload": payload, "prev": prev, "head": head})
        pos = end + 1
    return {"journal": journal_path, "magic_hash": mh, "records": records,
            "record_count": len(records), "head": head}


def load_journal(journal_path):
    """Parse + re-derive. Raises Refusal on anything that is not a clean parse."""
    raw = read_raw(journal_path, "journal")
    return _parse_records(raw, journal_path)


def read_pin(pin_path):
    if not os.path.isfile(pin_path):
        raise Refusal(BLOCKED_V, "PIN-ABSENT",
                      "%s -- a journal that carries its own only witness is not evidence"
                      % pin_path)
    obj = json.loads(decode_strict(read_raw(pin_path, "pin"), pin_path))
    for k in ("schema", "algo", "magic_hash", "record_count", "head"):
        if k not in obj:
            raise Refusal(BLOCKED_V, "PIN-MALFORMED", "missing %s" % k)
    if int(obj["schema"]) != SCHEMA or obj["algo"] != "sha256":
        raise Refusal(BLOCKED_V, "PIN-SCHEMA-UNSUPPORTED", json.dumps(obj)[:120])
    return obj


def write_pin(journal_path, state, pin_path=None):
    pin = {"schema": SCHEMA, "algo": "sha256", "magic_hash": state["magic_hash"],
           "record_count": state["record_count"], "head": state["head"]}
    atomic_write_bytes(pin_path or pin_path_for(journal_path),
                       (json.dumps(pin, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return pin


def verify_journal(journal_path, pin_path=None, expect_head=None):
    """-> (verdict_word, [reason], info). Fail-fast: the FIRST refusal is the reason, so a control
    binds to an exact branch. Never PASS over an empty population."""
    info = {"journal": journal_path, "pin": pin_path or pin_path_for(journal_path)}
    try:
        state = load_journal(journal_path)          # framing + per-record hash + chain
        info["record_count"] = state["record_count"]
        info["head"] = state["head"]
        info["magic_hash"] = state["magic_hash"]
        if state["record_count"] == 0:
            raise Refusal(BLOCKED_V, "EMPTY-JOURNAL-NO-WITNESS",
                          "the journal carries no captured turns; a claim over an empty population "
                          "is not a pass")
        pin = read_pin(info["pin"])
        if pin["magic_hash"] != state["magic_hash"]:
            raise Refusal(FAIL_V, "PIN-MAGIC-MISMATCH", str(pin["magic_hash"])[:16])
        if int(pin["record_count"]) != state["record_count"]:
            raise Refusal(FAIL_V, "PIN-RECORD-COUNT-MISMATCH",
                          "pinned %s, found %d" % (pin["record_count"], state["record_count"]))
        if pin["head"] != state["head"]:
            raise Refusal(FAIL_V, "PIN-HEAD-MISMATCH",
                          "pinned %s, computed %s" % (str(pin["head"])[:16], state["head"][:16]))
        if expect_head is not None and str(expect_head) != state["head"]:
            raise Refusal(FAIL_V, "ANCHOR-MISMATCH",
                          "external anchor %s, computed %s"
                          % (str(expect_head)[:16], state["head"][:16]))
        return PASS_V, ["OK"], info
    except Refusal as r:
        info["detail"] = r.detail
        return r.verdict, [r.reason], info


# --------------------------------------------------------------------------- writing: capture FIRST
def init_journal(journal_path, pin_path=None):
    if os.path.exists(journal_path):
        raise Refusal(BLOCKED_V, "JOURNAL-EXISTS", journal_path)
    atomic_write_bytes(journal_path, MAGIC)
    state = load_journal(journal_path)
    write_pin(journal_path, state, pin_path)
    return state


def append_turn(journal_path, raw_bytes, role="", label="", pin_path=None, utc=None):
    """Write the RAW TURN BYTES + their hash to the journal before anything reads them.

    Returns a Ref. A Ref carries no payload on purpose: the only route from here to an
    interpretation is load_record(), which goes back to the bytes on disk.
    """
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise Refusal(BLOCKED_V, "CAPTURE-NOT-BYTES",
                      "a turn is captured as bytes, not as %s -- decoding before capture is "
                      "already an interpretation" % type(raw_bytes).__name__)
    if len(raw_bytes) == 0:
        raise Refusal(BLOCKED_V, "CAPTURE-EMPTY-TURN", "an empty turn is not a captured turn")
    if len(raw_bytes) > MAX_TURN_BYTES:
        raise Refusal(BLOCKED_V, "CAPTURE-TURN-TOO-LARGE", "%d bytes" % len(raw_bytes))
    state = load_journal(journal_path)              # existence-checked + verified as we go
    seq = state["record_count"] + 1
    payload = bytes(raw_bytes)
    sha = sha256_hex(payload)
    header = _header_bytes(seq, utc or utc_now(), len(payload), sha, state["head"], role, label)
    append_raw(journal_path, header + payload + b"\n")
    new_state = load_journal(journal_path)
    if new_state["record_count"] != seq:
        raise Refusal(BLOCKED_V, "APPEND-DID-NOT-LAND", "expected seq=%d" % seq)
    write_pin(journal_path, new_state, pin_path)
    rec = new_state["records"][-1]
    return Ref(journal_path, seq, rec["sha256"], rec["utc"], new_state["head"])


def load_record(journal_path, seq):
    """The ONLY producer of a Captured. Re-reads the file; re-derives the hash; then returns."""
    state = load_journal(journal_path)
    if state["record_count"] == 0:
        raise Refusal(BLOCKED_V, "EMPTY-JOURNAL-NO-WITNESS", journal_path)
    hits = [r for r in state["records"] if r["seq"] == int(seq)]
    if not hits:
        raise Refusal(BLOCKED_V, "RECORD-ABSENT", "seq=%s in %s" % (seq, journal_path))
    r = hits[0]
    return Captured(_token=_DISK_TOKEN, journal=journal_path, seq=r["seq"], utc=r["utc"],
                    sha=r["sha256"], role=r["role"], label=r["label"], payload=r["payload"])


def export_record(journal_path, seq, out_path):
    """Materialise a captured turn's RAW bytes at a fresh path, for a consumer that wants a file.
    Refuses to overwrite: an overwritten capture is a destroyed capture."""
    cap = load_record(journal_path, seq)
    if os.path.exists(out_path):
        raise Refusal(BLOCKED_V, "EXPORT-DEST-EXISTS", out_path)
    atomic_write_bytes(out_path, cap.raw())
    written = sha256_hex(read_raw(out_path, "export"))
    if written != cap.sha256:
        raise Refusal(BLOCKED_V, "EXPORT-HASH-MISMATCH", out_path)
    return out_path, written


# ------------------------------------------------------- the record-aware public API (conn-bound)
def _db_file(conn):
    """The path of the main SQLite file behind `conn`, so the journal sits beside the record."""
    for row in conn.execute("PRAGMA database_list"):
        # (seq, name, file)
        if row[1] == "main" and row[2]:
            return row[2]
    return None


def journal_dir_for(conn, root=None):
    dbf = _db_file(conn)
    base = os.path.dirname(dbf) if dbf else paths.runtime_dir(root or paths.root())
    return os.path.join(base, "journal")


def journal_path_for(conn, root=None):
    """The verbatim journal file for a record: `.alpaca/journal/capture.journal` beside alpaca.db."""
    return os.path.join(journal_dir_for(conn, root), "capture.journal")


def append(conn, raw_bytes, actor, *, role="", label="", root=None, utc=None, session=None):
    """Capture one raw turn to the verbatim journal beside the record, and cite it on the record.

    The journal file is the truth; the JOURNAL_KIND event is the append-only pointer back to the
    seq and hash on disk, so the record says a turn was captured without ever holding the bytes.
    Returns the Ref.
    """
    jp = journal_path_for(conn, root)
    if not os.path.exists(jp):
        init_journal(jp)
    ref = append_turn(jp, raw_bytes, role=role or (actor or ""), label=label, utc=utc)
    db.append_event(conn, session=session or "cli", actor=actor or "agent", kind=JOURNAL_KIND,
                    ref=str(ref.seq),
                    data={"seq": ref.seq, "sha256": ref.sha256, "head": ref.head, "journal": jp})
    return ref


def load(conn, seq, *, root=None):
    """Read a captured turn back from disk as a Captured (the only reader input)."""
    return load_record(journal_path_for(conn, root), seq)


def verify(conn, *, root=None):
    """Verify the verbatim journal beside the record. -> (verdict_word, [reason], info)."""
    jp = journal_path_for(conn, root)
    return verify_journal(jp)


# --------------------------------------------------------------------------- the selftest table
_BYPASS_NOTES = {
    "K-00": "positive control: an untampered journal still verifies",
    "K-02": "a Captured built by hand, letting interpretation precede capture",
    "K-03": "a single mutated payload byte still verifying",
    "K-04": "the header's own sha token believed instead of re-derived",
    "K-05": "a dropped last record with the pin left at the old count",
    "K-06": "a journal that carries its own only witness (pin absent)",
    "K-07": "a claim over a journal with zero records",
    "K-08": "a fully consistent rewrite, with and without an external anchor",
    "K-09": "a non-utf-8 turn quietly repaired into a clean string",
    "K-10": "a shortened declared length letting the tail escape the framing",
    "K-11": "a CRLF-translating checkout turning the journal into a false pass",
    "K-11b": "payload newlines translated: a mark quietly normalised under the hash",
    "K-12": "an empty turn captured as if it were a mark",
    "K-16": "append rewriting earlier bytes (not append-only)",
    "K-17": "export overwriting an existing capture",
    "K-19": "an unresolvable journal pointer treated as an empty journal",
    "K-20": "generated, not listed: any single-bit edit of the journal still verifying",
}


def _fresh_dir(base, stem):
    path = os.path.join(base, stem)
    n = 2
    while os.path.exists(path):
        path = os.path.join(base, "%s.%d" % (stem, n))
        n += 1
    os.makedirs(path)
    return path


def _mk_journal(directory, turns):
    j = os.path.join(directory, "capture.journal")
    init_journal(j)
    for i, t in enumerate(turns):
        append_turn(j, t, role="r%d" % i, label="L%d" % i, utc="2026-01-01T00:00:00+00:00")
    return j


def selftest(workdir=None):
    """Run the ported control table. -> (failures:int, rows:list). Every row that a control does
    not fire is a failure; K-00 is the positive control that catches a tautological refuser.

    The dropped-signature controls (K-01, K-13..K-15, K-21..K-30) are gone with their parsers.
    The verbatim-journal controls are kept and re-run from executed code, nothing transcribed.
    """
    base = workdir or _fresh_dir(os.path.join(tempfile.gettempdir(), "alpaca-journal-selftest"),
                                 "journal-%s-%d" % (
                                     datetime.datetime.now().strftime("%Y%m%dT%H%M%S"),
                                     os.getpid()))
    if not os.path.isdir(base):
        os.makedirs(base)
    rows = []
    failures = [0]

    def record(cid, expect_v, expect_r, got_v, got_r, detail=""):
        ok = (got_v == expect_v and got_r == expect_r)
        if not ok:
            failures[0] += 1
        rows.append((cid, _BYPASS_NOTES.get(cid, ""), "%s/%s" % (expect_v, expect_r),
                     "%s/%s" % (got_v, got_r), "FIRED" if ok else "DID-NOT-FIRE", detail))

    def refusal_case(cid, expect_v, expect_r, fn, detail=""):
        try:
            val = fn()
            record(cid, expect_v, expect_r, PASS_V, "NO-REFUSAL", detail + (" -> %.60r" % (val,)))
        except Refusal as exc:
            record(cid, expect_v, expect_r, exc.verdict, exc.reason, detail)

    d = _fresh_dir(base, "k")
    j = _mk_journal(d, [b"Name: A. Person\n", b"signed A. Person\n"])

    # K-02: a Captured built by hand is refused (interpretation cannot precede capture).
    refusal_case("K-02", BLOCKED_V, "CAPTURE-CONSTRUCT-FORBIDDEN",
                 lambda: Captured(journal=j, seq=1, sha="0" * 64, payload=b"x"))

    # K-03: mutate one payload byte in place, leave everything else alone.
    d3 = _fresh_dir(base, "k03")
    j3 = _mk_journal(d3, [b"Name: A. Person\n"])
    raw3 = bytearray(read_raw(j3))
    idx = raw3.rfind(b"Person")
    raw3[idx] = ord("p")
    atomic_write_bytes(j3, bytes(raw3))
    v, r, _ = verify_journal(j3)
    record("K-03", FAIL_V, "CAPTURE-HASH-MISMATCH", v, r[0])

    # K-04: mutate the payload AND re-pin the head, but leave the header's sha token alone.
    d4 = _fresh_dir(base, "k04")
    j4 = _mk_journal(d4, [b"Name: A. Person\n"])
    raw4 = bytearray(read_raw(j4))
    raw4[raw4.rfind(b"Person")] = ord("p")
    atomic_write_bytes(j4, bytes(raw4))
    try:
        st4 = load_journal(j4)
        write_pin(j4, st4)
    except Refusal:
        atomic_write_bytes(pin_path_for(j4), (json.dumps(
            {"schema": SCHEMA, "algo": "sha256", "magic_hash": magic_hash_of(MAGIC),
             "record_count": 1, "head": "0" * 64}, sort_keys=True) + "\n").encode("utf-8"))
    v, r, _ = verify_journal(j4)
    record("K-04", FAIL_V, "CAPTURE-HASH-MISMATCH", v, r[0],
           "the header's own sha256 field is re-derived, never believed")

    # K-05: drop the last record; leave the pin pinned at the old count.
    d5 = _fresh_dir(base, "k05")
    j5 = _mk_journal(d5, [b"one\n", b"two\n"])
    raw5 = read_raw(j5)
    cut = raw5.rfind(b"REC seq=2 ")
    atomic_write_bytes(j5, raw5[:cut])
    v, r, _ = verify_journal(j5)
    record("K-05", FAIL_V, "PIN-RECORD-COUNT-MISMATCH", v, r[0])

    # K-06: pin absent.
    d6 = _fresh_dir(base, "k06")
    j6 = _mk_journal(d6, [b"one\n"])
    os.replace(pin_path_for(j6), pin_path_for(j6) + ".stashed")
    v, r, _ = verify_journal(j6)
    record("K-06", BLOCKED_V, "PIN-ABSENT", v, r[0])

    # K-07: a journal with zero records.
    d7 = _fresh_dir(base, "k07")
    j7 = os.path.join(d7, "capture.journal")
    init_journal(j7)
    v, r, _ = verify_journal(j7)
    record("K-07", BLOCKED_V, "EMPTY-JOURNAL-NO-WITNESS", v, r[0])

    # K-08: full consistent rewrite -- passes with no anchor, fails against one. Both shown.
    d8 = _fresh_dir(base, "k08")
    j8 = _mk_journal(d8, [b"Name: A. Person\n"])
    anchor = load_journal(j8)["head"]
    os.replace(j8, j8 + ".orig")
    j8b = os.path.join(d8, "capture.journal")
    init_journal(j8b)
    append_turn(j8b, b"Name: B. Other\n", utc="2026-01-01T00:00:00+00:00")
    v_noanchor, r_noanchor, _ = verify_journal(j8b)
    v_anchor, r_anchor, _ = verify_journal(j8b, expect_head=anchor)
    record("K-08", FAIL_V, "ANCHOR-MISMATCH", v_anchor, r_anchor[0],
           "same rewrite with NO anchor: %s/%s" % (v_noanchor, r_noanchor[0]))

    # K-09: a non-utf-8 turn. Captured byte-exactly; refuses to become a clean string.
    d9 = _fresh_dir(base, "k09")
    j9 = _mk_journal(d9, [b"\xff\xfe not utf-8 \x80\n"])
    v9, r9, _ = verify_journal(j9)
    refusal_case("K-09", BLOCKED_V, "DECODE-FAILED-NOT-UTF8",
                 lambda: load_record(j9, 1).text(),
                 "raw bytes verify %s/%s; only the decode refuses" % (v9, r9[0]))

    # K-10: shorten a record's declared length so the tail escapes the framing.
    d10 = _fresh_dir(base, "k10")
    j10 = _mk_journal(d10, [b"abcdefgh\n"])
    raw10 = read_raw(j10).replace(b"len=9 ", b"len=4 ")
    atomic_write_bytes(j10, raw10)
    v, r, _ = verify_journal(j10)
    record("K-10", FAIL_V, "FRAMING-SEPARATOR-MISSING", v, r[0])

    # K-11: a CRLF-translating checkout of the whole file. Must never be a pass.
    d11 = _fresh_dir(base, "k11")
    j11 = _mk_journal(d11, [b"line one\nline two\n"])
    atomic_write_bytes(j11, read_raw(j11).replace(b"\n", b"\r\n"))
    v11, r11, _ = verify_journal(j11)
    record("K-11", BLOCKED_V, "JOURNAL-MAGIC-UNKNOWN", v11, r11[0])

    # K-11b: only the PAYLOAD newline-translated (magic and framing intact) -- the subtle half.
    d11b = _fresh_dir(base, "k11b")
    j11b = _mk_journal(d11b, [b"line one\nline two\n"])
    raw11 = read_raw(j11b)
    at = raw11.rfind(b"line one")
    body = raw11[at:at + len(b"line one\nline two\n")]
    atomic_write_bytes(j11b, raw11[:at] + body.replace(b"\n", b"\r\n")[:len(body)]
                       + raw11[at + len(body):])
    v11b, r11b, _ = verify_journal(j11b)
    record("K-11b", FAIL_V, "CAPTURE-HASH-MISMATCH", v11b, r11b[0],
           "a mark is opaque bytes: no newline normalisation is applied to it")

    # K-12: an empty turn.
    refusal_case("K-12", BLOCKED_V, "CAPTURE-EMPTY-TURN", lambda: append_turn(j, b""))

    # K-16: append-only -- earlier bytes are byte-identical after an append.
    d16 = _fresh_dir(base, "k16")
    j16 = _mk_journal(d16, [b"first\n"])
    before = read_raw(j16)
    append_turn(j16, b"second\n", utc="2026-01-01T00:00:00+00:00")
    after = read_raw(j16)
    ok16 = after.startswith(before) and len(after) > len(before)
    record("K-16", PASS_V, "PREFIX-STABLE", PASS_V if ok16 else FAIL_V,
           "PREFIX-STABLE" if ok16 else "PREFIX-REWRITTEN",
           "%d -> %d bytes" % (len(before), len(after)))

    # K-17: export refuses to overwrite an existing capture.
    d17 = _fresh_dir(base, "k17")
    j17 = _mk_journal(d17, [b"Name: A. Person\n"])
    out17 = os.path.join(d17, "cap-1.txt")
    export_record(j17, 1, out17)
    refusal_case("K-17", BLOCKED_V, "EXPORT-DEST-EXISTS",
                 lambda: export_record(j17, 1, out17))

    # K-19: an unresolvable journal pointer is BLOCKED, not "no records".
    refusal_case("K-19", BLOCKED_V, "POINTER-ABSENT",
                 lambda: load_journal(os.path.join(base, "no-such-journal")))

    # K-20: a GENERATED control. Every single-bit mutation of a verified journal, checked against
    # the pin taken before the mutation. No case here was thought of by hand.
    d20 = _fresh_dir(base, "k20")
    j20 = _mk_journal(d20, [b"Name: A. Person\n", b"signed A. Person\n"])
    pin20 = pin_path_for(j20)
    orig20 = read_raw(j20)
    mutant = os.path.join(d20, "mutant.journal")
    survivors, tried = [], 0
    for i in range(len(orig20)):
        mut = bytearray(orig20)
        mut[i] ^= 0x01
        atomic_write_bytes(mutant, bytes(mut))
        tried += 1
        mv, mr, _ = verify_journal(mutant, pin_path=pin20)
        if mv == PASS_V:
            survivors.append(i)
    atomic_write_bytes(mutant, orig20)
    rv, rr, _ = verify_journal(mutant, pin_path=pin20)
    ok20 = (not survivors and rv == PASS_V)
    if not ok20:
        failures[0] += 1
    rows.append(("K-20", _BYPASS_NOTES["K-20"], "0-survivors/restored-%s" % PASS_V,
                 "%d-survivors/restored-%s" % (len(survivors), rv),
                 "FIRED" if ok20 else "DID-NOT-FIRE",
                 "%d single-bit mutants enumerated from the file" % tried))

    # positive control: a clean journal still passes, or this is a tautological refuser.
    v_pos, r_pos, info_pos = verify_journal(j)
    pos_ok = (v_pos == PASS_V and info_pos["record_count"] == 2)
    if not pos_ok:
        failures[0] += 1
    rows.append(("K-00", _BYPASS_NOTES["K-00"], "%s/OK" % PASS_V, "%s/%s" % (v_pos, r_pos[0]),
                 "FIRED" if pos_ok else "DID-NOT-FIRE",
                 "records=%s" % info_pos.get("record_count")))

    return failures[0], rows
