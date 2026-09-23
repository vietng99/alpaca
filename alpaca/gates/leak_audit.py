"""leak_audit.py - the mechanical half of the leak control, ported to REFUSE (M4.11).

Ported from the earlier harness ops/leak_audit.py and re-based onto Alpaca. The mechanism is generic and
the port keeps every defense the predecessor rebuilt after each was walked past by a blind
refuter; what changes for Alpaca is the SOURCE of the sealed-term list (it is DATA, read from
project.yaml, never a domain literal in code) and the contract (the one Alpaca verdict<->exit-code
mapping in alpaca/gates/verdict.py, imported, never restated).

Each defense answers a reproduced bypass of an earlier advisory:

  1. AN AUDIT NOTHING IS OBLIGED TO CONSUME IS NOT A CONTROL. The verdict is content-bound: a
     receipt carries a tree_digest re-derived from the RAW BYTES, and `verify_clearance` refuses
     unless a PASS receipt matches the bytes actually present. A producer-written label on the
     trusted path is worth nothing on its own.
  2. WORD-BOUNDARY MATCHING CANNOT SEE A COMPOUND IDENTIFIER. Matching is SUBSTRING, strictly
     broader, with a documented, counted allowlist for the false positives that buys.
  3. errors="replace" LAUNDERED AN UNDECODABLE FILE INTO CLEAN. Decoding uses an explicit STRICT
     codec ladder (BOM sniff then a bounded list); errors="replace" appears nowhere here. A file
     no declared codec decodes is BLOCKED, never CLEAN, and is still scanned as bytes.
  4. COMPILED/BINARY ARTEFACTS WERE SKIPPED BY SUFFIX. Every regular file is scanned as bytes;
     there is no skip-list and no --allow-skipped. A file that cannot be read is BLOCKED.
  5. A MECHANICAL CLEAN IS NOT CLEARANCE. A term list cannot see a paraphrase, so with no
     content-bound blind-read attestation the best verdict is PAUSED-FOR-DECISION, never PASS.
  6. "AUTHOR THE COMPLETE LIST" IS UNFALSIFIABLE. Completeness is a separate oracle. What is
     adjudicated every run is ABLE-TO-FAIL: each term is probed against a synthetic compound
     witness in both lanes before the target is scanned; a term the matcher cannot find is a
     dead entry that BLOCKS the run.

Honest limit: this is a byte-level substring scan, not a semantic analysis. A clean is "no
sealed term string found", never "provably clean by every route" - which is exactly why PASS
requires the blind adversarial-read attestation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import namedtuple
from datetime import datetime, timezone

from alpaca.gates import verdict as vc

INSTRUMENT = "leak-audit"

# ------------------------------------------------------------------ reason codes
R_TERMS_ABSENT = "TERMS-ABSENT"
R_TERMS_UNDECODABLE = "TERMS-UNDECODABLE"
R_TERMS_EMPTY = "TERMS-EMPTY"
R_TERMS_INSIDE_TARGET = "TERMS-INSIDE-TARGET"
R_ALLOW_NO_REASON = "ALLOW-RULE-WITHOUT-REASON"
R_ALLOW_BAD_REGEX = "ALLOW-RULE-MALFORMED"
R_TERM_TOO_SHORT = "TERM-BELOW-MIN-LENGTH"
R_MATCHER_DEAD_TERM = "MATCHER-SELFCHECK-FAILED"
R_TARGET_ABSENT = "TARGET-ABSENT"
R_TARGET_EMPTY = "TARGET-POPULATION-EMPTY"
R_FILE_UNREADABLE = "FILE-UNREADABLE"
R_FILE_UNDECODABLE = "FILE-UNDECODABLE"
R_CONTROL_ABSENT = "CONTROL-ABSENT"
R_CONTROL_NOISY = "CONTROL-NOISY"
R_ATTEST_ABSENT = "ADVERSARIAL-READ-UNATTESTED"
R_ATTEST_MALFORMED = "ATTESTATION-MALFORMED"
R_ATTEST_MISMATCH = "ATTESTATION-TREE-DIGEST-MISMATCH"
R_HITS = "LEAK-HITS"
R_SHAPE_DISABLED = "SHAPE-RULES-DISABLED-BY-OPERATOR"
R_RECEIPT_OCCUPIED = "RECEIPT-PATH-OCCUPIED-BY-FOREIGN-FILE"
R_CLEARANCE_ABSENT = "CLEARANCE-RECEIPT-ABSENT"
R_CLEARANCE_MALFORMED = "CLEARANCE-RECEIPT-MALFORMED"
R_CLEARANCE_NOT_PASS = "CLEARANCE-VERDICT-NOT-PASS"
R_CLEARANCE_MISMATCH = "CLEARANCE-TREE-DIGEST-MISMATCH"
R_CLEARANCE_TERMS_DRIFT = "CLEARANCE-TERMS-DIGEST-MISMATCH"

RECEIPT_MAGIC = "leak-audit-receipt/1"

# Substring matching fires inside ordinary prose for a short token; four characters is the floor
# at which a term carries enough information to match as a substring. Anything shorter is REFUSED
# and REPORTED, never silently dropped.
MIN_TERM_LEN = 4

# Ordered, explicit, STRICT codec ladder. cp1252 sits last because five byte values are undefined
# in it, so genuinely binary payloads still fall through to UNDECODABLE rather than being
# laundered into clean-looking text.
CODEC_LADDER = ("utf-8", "utf-16-le", "utf-16-be", "cp1252")
_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

TEXT_SCAN_MAX_BYTES = 16 * 1024 * 1024
BYTE_CHUNK = 1 << 20

# GENERIC syntactic shape rules - not campaign values. They are the only control that can catch an
# identity or a site path nobody enumerated, which a term list structurally cannot do. They are
# ADDITIVE to term matching: the control is the UNION.
SHAPE_RULES = (
    ("shape:mailbox",
     re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("shape:license-host-port",
     re.compile(r"\b\d{4,5}@[A-Za-z0-9][A-Za-z0-9._\-]{2,}\b")),
    ("shape:posix-home-abspath",
     re.compile(r"/(?:home|Users|users|export|nfs|proj|scratch)/[A-Za-z0-9._\-]{2,}"
                r"(?:/[^\s\"'<>|]*)?")),
    ("shape:windows-user-abspath",
     re.compile(r"\b[A-Za-z]:[\\/](?:Users|users|home)[\\/][A-Za-z0-9._\-]{2,}")),
)

_HOMOGLYPH = str.maketrans({
    "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C",
    "Х": "X", "У": "Y", "І": "I", "К": "K", "М": "M",
    "Н": "H", "В": "B", "Т": "T",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "х": "x", "у": "y", "і": "i",
})
_SEP_RUN = re.compile(r"[\-‐]?[\s ]+")

# ------------------------------------------------------------------ severity fold
# Alpaca verdicts are integer codes (alpaca/gates/verdict.py); this module never restates those numbers.
# The fold is a SEVERITY ranking, not a verdict->code map: least to most severe a refusal (BLOCKED)
# dominates a real leak (FAIL), which dominates a held decision (PAUSED), which dominates a clean
# (PASS). The order is written as a sequence of the imported codes, so nothing here re-encodes the
# contract; `_rank` reads a code's position in it.
_SEVERITY_ORDER = (vc.PASS, vc.PAUSED, vc.FAIL, vc.BLOCKED)


def _rank(code):
    return _SEVERITY_ORDER.index(code) if code in _SEVERITY_ORDER else len(_SEVERITY_ORDER)


def worst(codes):
    codes = [c for c in codes if c is not None]
    if not codes:
        return vc.PASS
    return max(codes, key=_rank)


Result = namedtuple("Result", "verdict reasons report")


# ------------------------------------------------------------------ small io helpers
class LeakError(Exception):
    """A refusal raised by an io helper; never swallowed into a clean."""


def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise LeakError(str(e))


def decode_utf8_strict(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise LeakError(str(e))


def read_text_utf8(path):
    return decode_utf8_strict(read_bytes(path))


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file_raw(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(BYTE_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def canon_path(p):
    return str(p).replace(os.sep, "/")


def write_json_atomic(path, payload):
    from alpaca import util
    util.write_text(path, json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False))


def defang(text: str) -> str:
    """Undo the cheap evasions before matching: compatibility normalisation, format characters
    (soft hyphen, zero-width joiners, BOM), and Cyrillic homoglyphs."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return text.translate(_HOMOGLYPH)


# ------------------------------------------------------------------ term list
class TermList(object):
    def __init__(self):
        self.terms = []          # [(term, defanged_lower)]
        self.allow = []          # [(regex, reason, source_line)]
        self.shape_allow = []    # [(regex, reason, rule_number)] - shape hits only, whole value
        self.refused = []        # [(raw, why)]  - reported, never silent
        self.digest = ""
        self.path = ""


def _terms_from_lines(lines, source):
    """Parse sealed-term lines into a TermList. Returns (TermList | None, reasons, detail)."""
    reasons, detail = [], []
    tl = TermList()
    tl.path = source
    seen = set()
    for lineno, line in enumerate(lines, start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("!exempt:"):
            continue
        if s.startswith("!allow:"):
            body = s[len("!allow:"):]
            if "#" not in body:
                reasons.append(R_ALLOW_NO_REASON)
                detail.append("%s:%d: allow rule carries no '# reason'" % (source, lineno))
                continue
            pat, reason = body.split("#", 1)
            pat, reason = pat.strip(), reason.strip()
            if not pat or not reason:
                reasons.append(R_ALLOW_NO_REASON)
                detail.append("%s:%d: allow rule has an empty pattern or reason" % (source, lineno))
                continue
            try:
                rx = re.compile(pat, re.IGNORECASE)
            except re.error as e:
                reasons.append(R_ALLOW_BAD_REGEX)
                detail.append("%s:%d: %s" % (source, lineno, e))
                continue
            tl.allow.append((rx, reason, lineno))
            continue
        term = s.split("  #", 1)[0].strip()
        if not term:
            continue
        if len(term) < MIN_TERM_LEN:
            tl.refused.append((term, "shorter than %d chars" % MIN_TERM_LEN))
            reasons.append(R_TERM_TOO_SHORT)
            detail.append("%s:%d: refused %r (substring matching on a token this short fires on "
                          "ordinary prose)" % (source, lineno, term))
            continue
        key = defang(term).casefold()
        if key in seen:
            continue
        seen.add(key)
        tl.terms.append((term, key))
    if not tl.terms:
        reasons.append(R_TERMS_EMPTY)
        detail.append("term source %s yields zero usable terms - refusing to adjudicate a "
                      "universal claim against an empty witness set" % source)
        return None, reasons, detail
    return tl, reasons, detail


def parse_shape_allow(entries, source):
    """Parse shape allow rules: each entry is `<regex>  # <reason>`. A rule clears a SHAPE hit only
    (a mailbox, a home path) and only when the regex matches the WHOLE matched value, so it can never
    hide a sealed term, and never clears a longer value that merely contains an allowed one.
    Returns (rules, reasons, detail); a rule with no reason or a bad regex is reported, never used."""
    rules, reasons, detail = [], [], []
    for n, entry in enumerate(entries or [], start=1):
        body = str(entry)
        if "#" not in body:
            reasons.append(R_ALLOW_NO_REASON)
            detail.append("%s rule %d: allow rule carries no '# reason'" % (source, n))
            continue
        pat, reason = body.rsplit("#", 1)
        pat, reason = pat.strip(), reason.strip()
        if not pat or not reason:
            reasons.append(R_ALLOW_NO_REASON)
            detail.append("%s rule %d: allow rule has an empty pattern or reason" % (source, n))
            continue
        try:
            rx = re.compile(pat, re.IGNORECASE)
        except re.error as e:
            reasons.append(R_ALLOW_BAD_REGEX)
            detail.append("%s rule %d: %s" % (source, n, e))
            continue
        rules.append((rx, reason, n))
    return rules, reasons, detail


def _shape_allowed(tl, value):
    for rx, reason, n in getattr(tl, "shape_allow", ()):
        if rx.fullmatch(value):
            return reason, n
    return None, None


def load_terms(path):
    """Parse a sealed-term list file. Returns (TermList | None, reasons, detail)."""
    if not os.path.isfile(path):
        return None, [R_TERMS_ABSENT], ["term list does not exist: %s" % path]
    try:
        raw = read_bytes(path)
        text = decode_utf8_strict(raw)
    except LeakError as e:
        return None, [R_TERMS_UNDECODABLE], [str(e)]
    tl, reasons, detail = _terms_from_lines(text.splitlines(), path)
    if tl is not None:
        tl.digest = sha256_bytes(raw)
    return tl, reasons, detail


def load_terms_from_project(root):
    """Resolve the sealed-term source from DATA in project.yaml, never a code literal.

    Two shapes are honoured, in order: `barrier.terms` naming a file (a path relative to the
    root), or `barrier.sealed_terms` giving the terms inline as a list. This is the M4.11 Step 3
    seam: the term list is pointed at project.yaml, exactly as literal_guard points its forbidden
    list there. Returns (TermList | None, reasons, detail).
    """
    from alpaca import project
    try:
        cfg = project.load(root) or {}
    except Exception:
        cfg = {}
    block = (cfg.get("barrier") or {})
    terms_file = block.get("terms")
    inline = block.get("sealed_terms")
    if terms_file:
        p = terms_file if os.path.isabs(terms_file) else os.path.join(root, terms_file)
        tl, reasons, detail = load_terms(p)
    elif isinstance(inline, list) and inline:
        lines = [str(t) for t in inline]
        tl, reasons, detail = _terms_from_lines(lines, "project.yaml:barrier.sealed_terms")
        if tl is not None:
            tl.digest = sha256_bytes("\n".join(lines).encode("utf-8"))
    else:
        tl, reasons, detail = None, [R_TERMS_ABSENT], ["no barrier.terms or barrier.sealed_terms "
                                                       "in project.yaml"]
    # Shape allow rules (barrier.allow) are product DATA: they name the known false positives of the
    # generic shape rules in this tree, never a sealed term, so they live in project.yaml.
    rules, ar, ad = parse_shape_allow(block.get("allow"), "project.yaml:barrier.allow")
    reasons = list(reasons) + ar
    detail = list(detail) + ad
    if tl is not None:
        tl.shape_allow = rules
    return tl, reasons, detail


# ------------------------------------------------------------------ decoding
def decode_explicit(raw: bytes):
    """Decode with an explicit, ordered, STRICT codec ladder. Returns (text, codec) or
    (None, None). errors='replace' is never used: it converts an undecodable payload into a
    clean-looking string and returns CLEAN on a file that carries the term."""
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(codec), codec
            except UnicodeDecodeError:
                return None, None
    for codec in CODEC_LADDER:
        if codec.startswith("utf-16") and b"\x00" not in raw:
            continue
        try:
            return raw.decode(codec), codec
        except UnicodeDecodeError:
            continue
    return None, None


# ------------------------------------------------------------------ matching
def _allowed(tl: TermList, context: str):
    for rx, reason, lineno in tl.allow:
        if rx.search(context):
            return reason, lineno
    return None, None


def scan_text(tl: TermList, text: str, relpath: str, shape_on: bool):
    """Text lane: substring term matching over defanged text, plus generic shape rules.

    Three projections, each a demonstrated evasion: line (ordinary, carries a line number),
    joined (separator runs collapsed: a term split across a break or tab), nulstrip (NUL bytes
    removed: UTF-16-shaped text that decoded as something else)."""
    hits, suppressed = [], []
    d = defang(text)
    lanes = [("line", d)]
    joined = _SEP_RUN.sub("", d)
    if joined != d:
        lanes.append(("joined", joined))
    if "\x00" in d:
        lanes.append(("nulstrip", d.replace("\x00", "")))

    for lane, body in lanes:
        low = body.casefold()
        offsets = []
        if lane == "line":
            pos = 0
            for i, ln in enumerate(body.splitlines(True), start=1):
                offsets.append((pos, i, ln))
                pos += len(ln)
        for term, key in tl.terms:
            start = 0
            while True:
                idx = low.find(key, start)
                if idx < 0:
                    break
                start = idx + 1
                if lane == "line":
                    lineno, ctx = 0, ""
                    for off, i, ln in offsets:
                        if off <= idx < off + len(ln):
                            lineno, ctx = i, ln.strip()[:200]
                            break
                else:
                    lineno, ctx = 0, body[max(0, idx - 60):idx + len(key) + 60]
                why, at = _allowed(tl, ctx)
                rec = {"file": relpath, "line": lineno, "kind": "term", "rule": term,
                       "lane": lane, "context": ctx}
                if why:
                    rec["suppressed_by"] = why
                    rec["allow_rule_line"] = at
                    suppressed.append(rec)
                else:
                    hits.append(rec)
                if lane != "line":
                    break
        if not shape_on:
            continue
        for name, rx in SHAPE_RULES:
            for m in rx.finditer(body):
                if lane == "line":
                    lineno, ctx = 0, ""
                    for off, i, ln in offsets:
                        if off <= m.start() < off + len(ln):
                            lineno, ctx = i, ln.strip()[:200]
                            break
                else:
                    lineno, ctx = 0, m.group(0)
                why, at = _shape_allowed(tl, m.group(0))
                if not why:
                    why, at = _allowed(tl, ctx or m.group(0))
                rec = {"file": relpath, "line": lineno, "kind": "shape", "rule": name,
                       "lane": lane, "context": (ctx or m.group(0))[:200],
                       "matched": m.group(0)[:120]}
                if why:
                    rec["suppressed_by"] = why
                    rec["allow_rule_line"] = at
                    suppressed.append(rec)
                else:
                    hits.append(rec)
    return hits, suppressed


def scan_bytes_stream(tl: TermList, path: str, relpath: str, exclude=frozenset()):
    """Byte lane: every regular file, whatever its suffix, streamed. Two projections per chunk:
    the raw bytes (a term inside a compiled artefact or binary container) and the NUL-stripped
    bytes (UTF-16-encoded text without needing it to decode). It is the BACKSTOP: `exclude`
    carries the terms the text lane already accounted for, so it reports only what the text lane
    could not."""
    hits, suppressed = [], []
    keys = [(term, key.encode("utf-8")) for term, key in tl.terms if term not in exclude]
    if not keys:
        return [], [], ""
    maxlen = max((len(k) for _, k in keys), default=1)
    overlap = maxlen * 2 + 8
    found = set()
    tail = b""
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(BYTE_CHUNK)
                if not chunk:
                    break
                window = tail + chunk
                for proj in (window.lower(), window.replace(b"\x00", b"").lower()):
                    for term, kb in keys:
                        if term in found:
                            continue
                        if kb in proj:
                            found.add(term)
                tail = window[-overlap:] if len(window) > overlap else window
    except OSError as e:
        return None, None, str(e)
    for term in sorted(found):
        ctx = "(byte lane: term present in the raw bytes of this file)"
        why, at = _allowed(tl, relpath + " " + ctx)
        rec = {"file": relpath, "line": 0, "kind": "term", "rule": term,
               "lane": "bytes", "context": ctx}
        if why:
            rec["suppressed_by"] = why
            rec["allow_rule_line"] = at
            suppressed.append(rec)
        else:
            hits.append(rec)
    return hits, suppressed, ""


def scan_name(tl: TermList, relpath: str, shape_on: bool):
    hits, suppressed = scan_text(tl, relpath, relpath, shape_on)
    for h in hits + suppressed:
        h["lane"] = "filename"
        h["line"] = 0
    return hits, suppressed


# ------------------------------------------------------------------ able-to-fail
def matcher_selfcheck(tl: TermList):
    """Prove, every run, that the matcher can FIND each term - in a COMPOUND context, in both
    lanes. A guard that cannot fail is not verification, and a term the matcher can never hit is
    a dead entry that silently shrinks coverage."""
    dead = []
    for term, key in tl.terms:
        probe = "Zq" + term + "Wk"
        text_ok = key in defang(probe).casefold()
        byte_ok = key.encode("utf-8") in probe.encode("utf-8").lower()
        u16_ok = key.encode("utf-8") in probe.encode("utf-16-le").replace(b"\x00", b"").lower()
        if not (text_ok and byte_ok and u16_ok):
            dead.append((term, "text=%s bytes=%s utf16=%s" % (text_ok, byte_ok, u16_ok)))
    return dead


# ------------------------------------------------------------------ tree digest
def iter_files(root: str):
    if os.path.isfile(root):
        yield root, os.path.basename(root)
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                yield full, canon_path(os.path.relpath(full, root))
                continue
            if not os.path.isfile(full):
                continue
            yield full, canon_path(os.path.relpath(full, root))


def tree_digest(root: str):
    """sha256 over (relative path bytes, raw file bytes) for every file, sorted. Hashes the EXACT
    bytes on disk: no rstrip, no decode, no row filter - a digest over a projection can be made to
    agree while the shipped bytes differ."""
    h = hashlib.sha256()
    n = 0
    for full, rel in iter_files(root):
        try:
            fh = sha256_file_raw(full)
        except OSError:
            fh = "UNREADABLE"
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(fh.encode("ascii"))
        h.update(b"\n")
        n += 1
    return h.hexdigest(), n


# ------------------------------------------------------------------ attestation + receipt
def check_attestation(path, digest):
    """The blind adversarial read (the half a term list structurally cannot do). The attestation
    is content-bound: it must carry the tree_digest of the tree actually audited. A producer's
    'reviewed: yes' with no digest, or one recycled from another tree, is refused."""
    if path is None:
        return [R_ATTEST_ABSENT], ["no blind adversarial-read attestation supplied; a mechanical "
                                   "clean cannot reach PASS on its own because a paraphrase is "
                                   "invisible to it"]
    if not os.path.isfile(path):
        return [R_ATTEST_ABSENT], ["attestation path does not exist: %s" % path]
    try:
        obj = json.loads(read_text_utf8(path))
    except (LeakError, ValueError) as e:
        return [R_ATTEST_MALFORMED], [str(e)]
    if not isinstance(obj, dict):
        return [R_ATTEST_MALFORMED], ["attestation is not a json object"]
    for field in ("tree_digest", "attested_by", "utc", "statement"):
        if not obj.get(field):
            return [R_ATTEST_MALFORMED], ["attestation missing field %r" % field]
    if obj["tree_digest"] != digest:
        return [R_ATTEST_MISMATCH], [
            "attestation is for tree_digest %s but the tree on disk digests to %s"
            % (obj["tree_digest"][:16], digest[:16])]
    return [], []


def write_receipt(path, payload):
    """Atomic write-then-rename. Refuses to overwrite a file this tool did not produce."""
    if os.path.exists(path):
        try:
            prior = json.loads(read_text_utf8(path))
            if not (isinstance(prior, dict) and prior.get("magic") == RECEIPT_MAGIC):
                return [R_RECEIPT_OCCUPIED], ["%s exists and was not produced by this instrument; "
                                             "refusing to overwrite" % path]
        except (LeakError, ValueError):
            return [R_RECEIPT_OCCUPIED], ["%s exists and is not a readable receipt; refusing to "
                                          "overwrite" % path]
    write_json_atomic(path, payload)
    return [], []


# ------------------------------------------------------------------ the audit
def audit(target, terms_path=None, term_list=None, control=None, attestation=None, receipt=None,
          shape_on=True, require_control=True):
    """Run the mechanical leak audit. Returns Result(verdict, reasons, report).

    Exactly one of `terms_path` (a file) or `term_list` (a pre-parsed TermList, e.g. from
    project.yaml) supplies the sealed terms. Verdict is folded through `worst`; the precedence
    lives in this module and is not restated at call sites.
    """
    reasons, detail, verdicts = [], [], []
    report = {"instrument": INSTRUMENT, "magic": RECEIPT_MAGIC,
              "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "target": canon_path(os.path.abspath(target)),
              "terms": canon_path(os.path.abspath(terms_path)) if terms_path else "inline",
              "shape_rules_enabled": bool(shape_on)}

    if not shape_on:
        reasons.append(R_SHAPE_DISABLED)
        detail.append("generic shape rules were disabled by the operator; unenumerated identities "
                      "and site paths are NOT covered by this run")
        verdicts.append(vc.PAUSED)

    if not os.path.exists(target):
        reasons.append(R_TARGET_ABSENT)
        detail.append("target does not exist: %s" % target)
        report.update({"verdict": vc.BLOCKED, "reason_codes": reasons, "detail": detail})
        return Result(vc.BLOCKED, reasons, report)

    if term_list is not None:
        tl, tr, td = term_list, [], []
    else:
        tl, tr, td = load_terms(terms_path)
    reasons.extend(tr)
    detail.extend(td)
    if tl is None:
        report.update({"verdict": vc.BLOCKED, "reason_codes": reasons, "detail": detail})
        return Result(vc.BLOCKED, reasons, report)
    if R_ALLOW_NO_REASON in reasons or R_ALLOW_BAD_REGEX in reasons:
        verdicts.append(vc.BLOCKED)
    report["terms_digest"] = tl.digest
    report["terms_count"] = len(tl.terms)
    report["allow_rules"] = len(tl.allow)
    report["terms_refused"] = tl.refused

    # The answer key never ships with the thing it grades.
    if terms_path:
        ta = os.path.abspath(target)
        pa = os.path.abspath(terms_path)
        if os.path.isdir(ta) and (pa == ta or pa.startswith(ta + os.sep)):
            reasons.append(R_TERMS_INSIDE_TARGET)
            detail.append("the term list %s lives inside the audited tree %s" % (pa, ta))
            verdicts.append(vc.BLOCKED)

    dead = matcher_selfcheck(tl)
    if dead:
        reasons.append(R_MATCHER_DEAD_TERM)
        for term, why in dead[:10]:
            detail.append("the matcher cannot find its own term %r in a compound witness (%s); "
                          "this entry can never fire" % (term, why))
        verdicts.append(vc.BLOCKED)

    hits, suppressed, files = [], [], 0
    undecodable, unreadable, oversized = [], [], []
    for full, rel in iter_files(target):
        files += 1
        nh, ns = scan_name(tl, rel, shape_on)
        hits.extend(nh)
        suppressed.extend(ns)
        text, accounted = None, set()
        try:
            size = os.path.getsize(full)
        except OSError as e:
            unreadable.append((rel, str(e)))
            size = None
        if size is not None:
            if size > TEXT_SCAN_MAX_BYTES:
                oversized.append(rel)
            else:
                try:
                    raw = read_bytes(full)
                except LeakError as e:
                    unreadable.append((rel, str(e)))
                    raw = None
                if raw is not None:
                    text, _codec = decode_explicit(raw)
                    if text is None:
                        undecodable.append(rel)
                    else:
                        th, ts = scan_text(tl, text, rel, shape_on)
                        hits.extend(th)
                        suppressed.extend(ts)
                        accounted = {r["rule"] for r in th + ts if r["kind"] == "term"}
        bh, bs, err = scan_bytes_stream(tl, full, rel, exclude=accounted)
        if bh is None:
            unreadable.append((rel, err))
            continue
        hits.extend(bh)
        suppressed.extend(bs)

    report["files_scanned"] = files
    if files == 0:
        reasons.append(R_TARGET_EMPTY)
        detail.append("scanned ZERO files under %s - an empty measured population makes 'no "
                      "sealed term found' vacuously true" % target)
        verdicts.append(vc.BLOCKED)
    if unreadable:
        reasons.append(R_FILE_UNREADABLE)
        for rel, err in unreadable[:10]:
            detail.append("unreadable, therefore NOT cleared: %s (%s)" % (rel, err))
        verdicts.append(vc.BLOCKED)
    if undecodable:
        reasons.append(R_FILE_UNDECODABLE)
        for rel in undecodable[:10]:
            detail.append("no declared codec decodes %s; scanned as bytes only, and NOT reported "
                          "clean" % rel)
        verdicts.append(vc.BLOCKED)
    if oversized:
        reasons.append(R_FILE_UNDECODABLE)
        for rel in oversized[:10]:
            detail.append("%s exceeds the text-scan ceiling; byte lane ran, text lane did not, so "
                          "it is NOT reported clean" % rel)
        verdicts.append(vc.BLOCKED)

    control_state = "not-supplied"
    if control:
        if not os.path.exists(control):
            reasons.append(R_CONTROL_ABSENT)
            detail.append("control path does not exist: %s" % control)
            verdicts.append(vc.BLOCKED)
            control_state = "absent"
        else:
            chits = []
            for full, rel in iter_files(control):
                nh, _ = scan_name(tl, rel, shape_on)
                chits.extend(nh)
                accounted = set()
                try:
                    raw = read_bytes(full)
                except LeakError:
                    raw = None
                if raw is not None:
                    text, _codec = decode_explicit(raw)
                    if text is not None:
                        th, ts = scan_text(tl, text, rel, shape_on)
                        chits.extend(th)
                        accounted = {r["rule"] for r in th + ts if r["kind"] == "term"}
                bh, _bs, _err = scan_bytes_stream(tl, full, rel, exclude=accounted)
                if bh:
                    chits.extend(bh)
            if chits:
                reasons.append(R_CONTROL_NOISY)
                detail.append("the term list fires %d time(s) on the known-clean control %s; it "
                              "cannot come back clean, so a DIRTY verdict from it carries no "
                              "information" % (len(chits), control))
                verdicts.append(vc.BLOCKED)
                control_state = "noisy"
            else:
                control_state = "clean"
    elif require_control:
        reasons.append(R_CONTROL_ABSENT)
        detail.append("no known-clean control supplied; able-to-pass is unproven, so a clean "
                      "result cannot be distinguished from a broken matcher")
        verdicts.append(vc.BLOCKED)
    report["control"] = control_state

    report["hits"] = hits
    report["suppressed"] = suppressed
    report["hit_count"] = len(hits)
    report["suppressed_count"] = len(suppressed)
    if hits:
        reasons.append(R_HITS)
        by = {}
        for h in hits:
            by.setdefault(h["rule"], []).append(h)
        for rule in sorted(by, key=lambda k: -len(by[k])):
            ex = by[rule][0]
            detail.append("%d hit(s) on %s - e.g. %s:%s [%s]" % (len(by[rule]), rule, ex["file"],
                                                                 ex["line"], ex["lane"]))
        verdicts.append(vc.FAIL)

    digest, dn = tree_digest(target)
    report["tree_digest"] = digest
    report["tree_file_count"] = dn
    ar, ad = check_attestation(attestation, digest)
    if ar:
        reasons.extend(ar)
        detail.extend(ad)
        verdicts.append(vc.PAUSED if ar == [R_ATTEST_ABSENT] else vc.BLOCKED)

    if not verdicts:
        verdicts.append(vc.PASS)
    verdict = worst(verdicts)

    if receipt:
        rr, rd = write_receipt(receipt, dict(report, verdict=verdict, reason_codes=reasons,
                                             detail=detail))
        if rr:
            reasons.extend(rr)
            detail.extend(rd)
            verdict = worst(verdicts + [vc.BLOCKED])
        else:
            report["receipt"] = canon_path(os.path.abspath(receipt))

    report["verdict"] = verdict
    report["reason_codes"] = reasons
    report["detail"] = detail
    return Result(verdict, reasons, report)


# ------------------------------------------------------------------ clearance
def verify_clearance(target, receipt_path, terms_path=None):
    """Re-derive the audited tree's digest and refuse unless a PASS receipt matches it. This is the
    refusal seam: a caller is REFUSED on anything but a PASS receipt whose tree_digest re-derives
    from the bytes it is about to ship. The verdict string is never trusted on its own."""
    reasons, detail, verdicts = [], [], []
    if not os.path.exists(target):
        reasons.append(R_TARGET_ABSENT)
        detail.append("target does not exist: %s" % target)
        verdicts.append(vc.BLOCKED)
        digest = None
    else:
        digest, n = tree_digest(target)
        if n == 0:
            reasons.append(R_TARGET_EMPTY)
            detail.append("target holds zero files; there is nothing to clear")
            verdicts.append(vc.BLOCKED)

    obj = None
    if not receipt_path or not os.path.isfile(receipt_path):
        reasons.append(R_CLEARANCE_ABSENT)
        detail.append("no clearance receipt at %r - absence of a clearance is a refusal, never a "
                      "pass" % receipt_path)
        verdicts.append(vc.BLOCKED)
    else:
        try:
            obj = json.loads(read_text_utf8(receipt_path))
        except (LeakError, ValueError) as e:
            reasons.append(R_CLEARANCE_MALFORMED)
            detail.append(str(e))
            verdicts.append(vc.BLOCKED)
        else:
            if not isinstance(obj, dict) or obj.get("magic") != RECEIPT_MAGIC:
                reasons.append(R_CLEARANCE_MALFORMED)
                detail.append("receipt is not a %s document" % RECEIPT_MAGIC)
                verdicts.append(vc.BLOCKED)
                obj = None

    if obj is not None:
        if obj.get("tree_digest") != digest:
            reasons.append(R_CLEARANCE_MISMATCH)
            detail.append("receipt clears tree_digest %s but the tree on disk digests to %s - the "
                          "bytes changed after the audit"
                          % (str(obj.get("tree_digest"))[:16], str(digest)[:16]))
            verdicts.append(vc.BLOCKED)
        if obj.get("verdict") != vc.PASS:
            reasons.append(R_CLEARANCE_NOT_PASS)
            detail.append("receipt verdict is %r; only PASS clears a tree to ship"
                          % obj.get("verdict"))
            verdicts.append(vc.FAIL if obj.get("verdict") == vc.FAIL else vc.BLOCKED)
        if terms_path:
            if not os.path.isfile(terms_path):
                reasons.append(R_TERMS_ABSENT)
                detail.append("term list does not exist: %s" % terms_path)
                verdicts.append(vc.BLOCKED)
            else:
                td = sha256_bytes(read_bytes(terms_path))
                if obj.get("terms_digest") != td:
                    reasons.append(R_CLEARANCE_TERMS_DRIFT)
                    detail.append("receipt was produced against a different term list (%s vs %s on "
                                  "disk)" % (str(obj.get("terms_digest"))[:16], td[:16]))
                    verdicts.append(vc.BLOCKED)

    if not verdicts:
        verdicts.append(vc.PASS)
    return Result(worst(verdicts), reasons, {"tree_digest": digest, "detail": detail})


def require_clearance(target, receipt_path, terms_path=None):
    """Library form of the refusal. Raises LeakError on anything but PASS, so a caller that wraps
    its ship step in this cannot ship a DIRTY tree by ignoring an exit code."""
    res = verify_clearance(target, receipt_path, terms_path)
    if res.verdict != vc.PASS:
        raise LeakError("CLEARANCE-REFUSED: %s for %s" % (vc.name_of(res.verdict), target))
    return True


# ------------------------------------------------------------------ CLI boundary
def check(root) -> int:
    """The uniform instrument entry. Reads the sealed terms from project.yaml (Step 3). An
    unconfigured term list is an advisory PASS (nothing is claimed sealed, so there is nothing to
    refute); a configured list audits the product tree and adjudicates."""
    tl, _r, _d = load_terms_from_project(root)
    if tl is None:
        return vc.PASS
    from alpaca import project
    cfg = project.load(root) or {}
    tree = os.path.join(root, str((cfg.get("paths") or {}).get("product_tree") or "."))
    return audit(tree, term_list=tl, require_control=False).verdict


def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="Mechanical leak audit that REFUSES. Substring matching, binary aware, strict "
                    "decoding, and a content-bound clearance a caller cannot talk past.")
    ap.add_argument("target", nargs="?", help="file or directory to audit")
    ap.add_argument("--terms", help="sealed-term list (must live OUTSIDE the target)")
    ap.add_argument("--control", help="known-clean path proving the list can come back clean")
    ap.add_argument("--attestation", help="json attestation of the blind adversarial read")
    ap.add_argument("--receipt", help="write the clearance receipt here")
    ap.add_argument("--no-shape", action="store_true", help="disable the generic shape rules")
    ap.add_argument("--verify-clearance", action="store_true",
                    help="re-derive the tree digest and refuse unless a PASS receipt matches it")
    ap.add_argument("--selftest", action="store_true", help="run the negative-control selftest")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.target:
        return vc.emit_verdict(INSTRUMENT, vc.BLOCKED, "no target supplied")
    if a.verify_clearance:
        res = verify_clearance(a.target, a.receipt, a.terms)
        return vc.emit_verdict(INSTRUMENT + "/clearance", res.verdict, "; ".join(res.reasons))
    if not a.terms:
        return vc.emit_verdict(INSTRUMENT, vc.BLOCKED, "--terms is required (an audit with no "
                               "oracle is not an audit)")
    res = audit(a.target, terms_path=a.terms, control=a.control, attestation=a.attestation,
                receipt=a.receipt, shape_on=not a.no_shape)
    for d in res.report.get("detail", [])[:8]:
        print("  %s" % d)
    return vc.emit_verdict(INSTRUMENT, res.verdict, "; ".join(res.reasons[:6]))


def selftest() -> int:
    """Control cases drawn from the reproduced bypasses, not happy paths. The table is generated
    from the executed runs; nothing is transcribed."""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="leak-audit-selftest-")
    rows = []

    def mkdir(*parts):
        d = os.path.join(tmp, *parts)
        os.makedirs(d, exist_ok=True)
        return d

    def wf(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def case(name, want_verdict, want_reasons, fn):
        try:
            res = fn()
            got = list(res.reasons)
            ok = (res.verdict == want_verdict and all(r in got for r in want_reasons))
        except Exception as e:
            rows.append((name, "EXCEPTION", [repr(e)], False))
            return
        rows.append((name, vc.name_of(res.verdict), got, ok))

    TERM = "ZqSealedTokenAlpha"
    terms_dir = mkdir("terms")
    terms = wf(os.path.join(terms_dir, "t.txt"), (TERM + "\n").encode("utf-8"))
    control = mkdir("control")
    wf(os.path.join(control, "ok.md"), b"ordinary prose with nothing sealed in it\n")

    d = mkdir("compound")
    wf(os.path.join(d, "m.py"), ("class Prefix" + TERM + "Suffix: pass\n").encode("utf-8"))
    case("compound identifier is caught (word-boundary matcher misses it)",
         vc.FAIL, [R_HITS], lambda: audit(d, terms_path=terms, control=control))

    d = mkdir("utf16")
    wf(os.path.join(d, "n.txt"), ("prelude " + TERM + " coda\n").encode("utf-16"))
    case("UTF-16 file carrying the term is FAIL, not CLEAN",
         vc.FAIL, [R_HITS], lambda: audit(d, terms_path=terms, control=control))

    empty_terms = wf(os.path.join(terms_dir, "empty.txt"), b"# only comments\n\n   \n")
    d = mkdir("clean1")
    wf(os.path.join(d, "a.md"), b"nothing sealed here\n")
    case("empty term list is BLOCKED, never CLEAN",
         vc.BLOCKED, [R_TERMS_EMPTY], lambda: audit(d, terms_path=empty_terms, control=control))

    case("absent target is BLOCKED",
         vc.BLOCKED, [R_TARGET_ABSENT],
         lambda: audit(os.path.join(tmp, "no-such-tree"), terms_path=terms, control=control))

    d = mkdir("emptytree")
    case("zero files scanned is BLOCKED (empty measured population)",
         vc.BLOCKED, [R_TARGET_EMPTY], lambda: audit(d, terms_path=terms, control=control))

    d = mkdir("clean2")
    wf(os.path.join(d, "a.md"), b"ordinary prose\n")
    case("mechanical clean without a blind adversarial read is PAUSED, not PASS",
         vc.PAUSED, [R_ATTEST_ABSENT], lambda: audit(d, terms_path=terms, control=control))

    d = mkdir("cleanpass")
    wf(os.path.join(d, "a.md"), b"ordinary prose\n")
    digest, _n = tree_digest(d)
    att = os.path.join(tmp, "attest.json")
    write_json_atomic(att, {"tree_digest": digest, "attested_by": "control-case",
                            "utc": "1970-01-01T00:00:00Z", "statement": "synthetic control"})
    case("clean + content-bound attestation reaches PASS",
         vc.PASS, [], lambda: audit(d, terms_path=terms, control=control, attestation=att))

    bad = os.path.join(tmp, "attest-bad.json")
    write_json_atomic(bad, {"tree_digest": "0" * 64, "attested_by": "c", "utc": "x",
                            "statement": "recycled"})
    case("attestation for a different tree is refused (digest re-derived)",
         vc.BLOCKED, [R_ATTEST_MISMATCH],
         lambda: audit(d, terms_path=terms, control=control, attestation=bad))

    d = mkdir("nocontrol")
    wf(os.path.join(d, "a.md"), b"ordinary prose\n")
    case("no known-clean control means able-to-pass is unproven -> BLOCKED",
         vc.BLOCKED, [R_CONTROL_ABSENT], lambda: audit(d, terms_path=terms, control=None))

    d = mkdir("shape")
    wf(os.path.join(d, "a.md"), b"contact: someone.unenumerated@example.invalid\n")
    case("shape rule catches an identity absent from the term list (union, not swap)",
         vc.FAIL, [R_HITS], lambda: audit(d, terms_path=terms, control=control))

    print("CONTROL TABLE - %s" % INSTRUMENT)
    print("  " + "-" * 92)
    failures = 0
    for name, verdict, got, ok in rows:
        failures += 0 if ok else 1
        print("  %-4s %-72s" % ("PASS" if ok else "FAIL", name[:72]))
        print("       observed: verdict=%s reasons=%s" % (verdict, got))
    print("  " + "-" * 92)
    if failures:
        return vc.emit_verdict(INSTRUMENT + "/selftest", vc.FAIL,
                               "%d control(s) did not fire" % failures)
    return vc.emit_verdict(INSTRUMENT + "/selftest", vc.PASS, "every control fired")


if __name__ == "__main__":
    import sys
    sys.exit(main())
