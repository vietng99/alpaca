"""Candidate intake (design/rune2-memory/architecture.md 7, FR-33): the model-free checks a proposal
from the live session agent passes before it becomes a `checked` candidate.

The live session agent does the reading and the judging; Rune2 only verifies. Six checks run
read-only against the store and the record, in this order, and every one of them runs even when an
earlier one failed, so the `wiki-candidate` event carries all six results (architecture.md 7.1):

| K-1 | the anchor's source has a current version with active blocks; an `event:` anchor names the record row's hash |
| K-2 | the normalized quote sits in an active block of that version |
| K-3 | fact: predicate `llm-allowed`; subject, object and dates each in a quote; the entailer calls the claim `supported`; entity unambiguous. observation and summary: every sentence has an anchor the entailer supports; `label` is a slug |
| K-4 | every anchor passes `servable_ref` and lies in the proposal's scope |
| K-5 | every anchor is `event:`, `proof:`, `contract:` or `decision:` (`INDEPENDENT_KINDS`), and neither its quote nor its block holds generated briefing text (`briefing.holds_generated`) |
| K-6 | at most 8 anchors, an observation of 400 characters, a summary of 2,000, 200 pending per proposer |

Generated briefing text never counts as support either (FR-20, HS-MM-11): capture leaves it out of
the notes it lands (`briefing.strip_generated`), and an anchor whose quote or resolved block still
holds a briefing's marker line, item line, left-out count or gap line is refused `not-independent`
under K-5, at proposal and again at admission (OB-15), so briefing text a record event or a proof
report carries can never anchor, support or corroborate a candidate.

Transcript text and `wiki ingest` documents never count as support (K-5): they go in `context`, are
stored as given only when they pass `servable_ref`, K-1 and K-2 in the source they name, and never
refuse the proposal (OB-22). No check, no field and no fact kind here yields `user-statement`
(dec-38940e1fb35f Q11). Nothing in this module calls a model or the network.

Redaction (architecture.md 6.2, OB-21, OB-22): a quote that fails K-4, and a context quote that fails
the OB-22 check, is kept as its sha256 only. When any anchor fails K-4 the proposal body is kept as
`body_sha256` only, since a fact's subject, object and dates sit in the quotes. Every string of a
proposal passes the secret screen first, when it is parsed (`screened`, t-070 RC2), so no quote,
body or context entry is kept with a secret the screen masks, whichever check it fails.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from .api import (Anchor, CandidateView, CheckResult, FactBody, ObservationBody, Proposal)
from .determinism import canonical_json, nfc, normalize_text, sha256_hex

#: K-5 (architecture.md 7.2): the only anchor kinds, the record and the `dec-5ea0e31009b7` source
#: list. Transcript role rules were cut on 2026-09-30: transcript text is never support.
INDEPENDENT_KINDS = ("event", "proof", "contract", "decision")

KINDS = ("fact", "observation", "summary")
MAX_ANCHORS = 8                      # K-6 (provisional until t-066 and t-067 measure them)
OBSERVATION_CHARS = 400
SUMMARY_CHARS = 2000
PENDING_LIMIT = 200
DATATYPES = ("node", "date", "number", "string", "bool")
_SLUG = re.compile(r"^[a-z0-9-]{1,64}$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# One spelling only: ASCII digits, no leading zeros. A second spelling of the same event would pass
# the exclusion and privacy checks, which compare the ref string (op-008 t-066 fix-r1 B1).
_EVENT_REF = re.compile(r"^event:(0|[1-9][0-9]*)$", re.ASCII)

_TOP_KEYS = {"kind", "anchors", "context", "fact", "observation", "text", "sentence_anchors", "scope"}
_ANCHOR_KEYS = {"source_ref", "quote", "hash", "event_hash", "doc_id", "block_content_id"}
_FACT_KEYS = {"subject", "predicate", "obj", "obj_datatype", "valid_from"}
_OBS_KEYS = {"label", "governs", "prescribes", "falsifiability"}


# ------------------------------------------------------------------ parsing

def _anchor(raw: Any, where: str) -> Anchor:
    if not isinstance(raw, dict):
        raise ValueError("%s must be an object {source_ref, quote}" % where)
    extra = set(raw) - _ANCHOR_KEYS
    if extra:
        raise ValueError("%s has unknown keys %s" % (where, sorted(extra)))
    for key in ("source_ref", "quote"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValueError("%s needs a non-empty string %s" % (where, key))
    for key in ("hash", "event_hash", "doc_id", "block_content_id"):
        if raw.get(key) is not None and not isinstance(raw[key], str):
            raise ValueError("%s: %s must be a string" % (where, key))
    return Anchor(source_ref=raw["source_ref"].strip(), quote=nfc(raw["quote"]),
                  event_hash=raw.get("hash") or raw.get("event_hash"),
                  doc_id=raw.get("doc_id"), block_content_id=raw.get("block_content_id"))


def screened(p: Proposal) -> Proposal:
    """The proposal with every string it carries through the secret screen (`drain.screen`, FR-26):
    each anchor's and context entry's quote, source_ref and hints, the fact, the text, the
    observation metadata and the scope. This is the door of candidate intake (t-070 RC2, B-2; owner
    decision dec-392bf1d64a89: every stored anchor quote is screened): the checks, the
    `wiki-candidate` event, the `candidates` and `candidate_anchors` rows, the candidates view, the
    admission and the edge an admission writes all hold the screened proposal, whether its anchors
    pass K-1 and K-2 or not. A quote an agent copied from a note as it read before the screen is
    screened as the note was, so it still sits in the note's stored text (K-2)."""
    from .ingest.drain import screen

    def s(value):
        return screen(value) if isinstance(value, str) else value

    def anchor(a: Anchor) -> Anchor:
        return Anchor(source_ref=s(a.source_ref), quote=s(a.quote), event_hash=s(a.event_hash),
                      doc_id=s(a.doc_id), block_content_id=s(a.block_content_id))
    fact = p.fact
    if fact is not None:
        fact = FactBody(subject=s(fact.subject), predicate=s(fact.predicate), obj=s(fact.obj),
                        obj_datatype=fact.obj_datatype, valid_from=s(fact.valid_from))
    o = p.observation
    if o is not None:
        o = ObservationBody(label=s(o.label), governs=tuple(s(g) for g in o.governs),
                            prescribes=s(o.prescribes), falsifiability=s(o.falsifiability))
    return Proposal(kind=p.kind, anchors=tuple(anchor(a) for a in p.anchors),
                    context=tuple(anchor(a) for a in p.context), fact=fact, observation=o,
                    text=s(p.text), sentence_anchors=p.sentence_anchors,
                    scope=None if p.scope is None else {k: s(v) for k, v in p.scope.items()})


def parse_proposal(raw: Any) -> Proposal:
    """A screened `Proposal` (`screened`) from its JSON object, or ValueError naming what is
    malformed. A malformed body is a caller error, not a candidate: it gets no receipt and no record
    event."""
    if isinstance(raw, Proposal):
        return screened(raw)
    if not isinstance(raw, dict):
        raise ValueError("a proposal is a JSON object")
    extra = set(raw) - _TOP_KEYS
    if extra:
        raise ValueError("unknown proposal keys %s (no field may name another candidate)" % sorted(extra))
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ValueError("kind must be one of %s" % (KINDS,))
    anchors = raw.get("anchors")
    if not isinstance(anchors, list):
        raise ValueError("anchors must be a list")
    context = raw.get("context") or []
    if not isinstance(context, list):
        raise ValueError("context must be a list")
    fact = observation = None
    if raw.get("fact") is not None:
        f = raw["fact"]
        if not isinstance(f, dict) or set(f) - _FACT_KEYS:
            raise ValueError("fact holds subject, predicate, obj, obj_datatype and valid_from")
        for key in ("subject", "predicate", "obj", "obj_datatype"):
            if not isinstance(f.get(key), str) or not f[key].strip():
                raise ValueError("fact needs a non-empty string %s" % key)
        if f["obj_datatype"] not in DATATYPES:
            raise ValueError("obj_datatype must be one of %s" % (DATATYPES,))
        if f.get("valid_from") is not None and not isinstance(f["valid_from"], str):
            raise ValueError("valid_from must be a string")
        fact = FactBody(subject=nfc(f["subject"].strip()), predicate=f["predicate"].strip(),
                        obj=nfc(f["obj"].strip()), obj_datatype=f["obj_datatype"],
                        valid_from=(f.get("valid_from") or "").strip() or None)
    if raw.get("observation") is not None:
        o = raw["observation"]
        if not isinstance(o, dict) or set(o) - _OBS_KEYS:
            raise ValueError("observation holds label, governs, prescribes and falsifiability")
        governs = o.get("governs") or []
        if not isinstance(governs, list) or not all(isinstance(g, str) for g in governs):
            raise ValueError("governs must be a list of strings")
        for key in ("label", "prescribes", "falsifiability"):
            if o.get(key) is not None and not isinstance(o[key], str):
                raise ValueError("%s must be a string" % key)
        observation = ObservationBody(label=o.get("label"), governs=tuple(governs),
                                      prescribes=o.get("prescribes"),
                                      falsifiability=o.get("falsifiability"))
    text = raw.get("text")
    if text is not None and not isinstance(text, str):
        raise ValueError("text must be a string")
    sentence_anchors = raw.get("sentence_anchors") or []
    if (not isinstance(sentence_anchors, list)
            or not all(isinstance(s, list) and all(type(i) is int for i in s) for s in sentence_anchors)):
        raise ValueError("sentence_anchors must be a list of lists of anchor indexes")
    scope = raw.get("scope")
    if scope is not None and (not isinstance(scope, dict) or set(scope) - {"op", "task"}
                              or not all(isinstance(v, str) for v in scope.values())):
        raise ValueError("scope is {\"op\": ...} or {\"task\": ...}")
    if kind == "fact" and fact is None:
        raise ValueError("a fact proposal needs a fact body")
    if kind != "fact" and (text is None or not text.strip()):
        raise ValueError("an observation or summary proposal needs text")
    if kind != "fact" and fact is not None:
        raise ValueError("only a fact proposal takes a fact body")
    if kind != "observation" and observation is not None:
        raise ValueError("only an observation proposal takes observation metadata")
    return screened(Proposal(
        kind=kind,
        anchors=tuple(_anchor(a, "anchors[%d]" % i) for i, a in enumerate(anchors)),
        context=tuple(_anchor(a, "context[%d]" % i) for i, a in enumerate(context)),
        fact=fact, observation=observation,
        text=nfc(text.strip()) if text is not None else None,
        sentence_anchors=tuple(tuple(s) for s in sentence_anchors), scope=scope or None))


def quote_sha256(quote: str) -> str:
    return sha256_hex(normalize_text(quote))


def candidate_id(p: Proposal) -> str:
    """sha256 of kind, normalized content and the sorted (source_ref, quote) pairs of the anchors
    (architecture.md 7.1, interfaces CandidateReceipt). Context, scope and metadata are not part of
    the identity: a proposal that differs only there is the same candidate."""
    if p.kind == "fact":
        f = p.fact
        content = [normalize_text(x or "") for x in
                   (f.subject, f.predicate, f.obj, f.obj_datatype, f.valid_from)]
    else:
        content = [normalize_text(p.text or "")]
    pairs = sorted([a.source_ref, normalize_text(a.quote)] for a in p.anchors)
    return "c_" + sha256_hex(canonical_json([p.kind, content, pairs]))[:32]


def derive_fact_kind(source_refs) -> str:
    """The `fact_kind` of a claim an admitted candidate writes (architecture.md 7.2, dec-38940e1fb35f
    Q11): `agent-experience` when every anchor is a proof report, else `derived-hypothesis`. Record
    rules write `record-fact` themselves and no input yields `user-statement`."""
    refs = list(source_refs)
    if refs and all(source_kind(r) == "proof" for r in refs):
        return "agent-experience"
    return "derived-hypothesis"


def source_kind(source_ref: str) -> str:
    return source_ref.partition(":")[0]


# ------------------------------------------------------------------ the store, read-only

def _conn(db):
    return getattr(db, "conn", db)


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def current_version(db, source_ref: str) -> dict | None:
    """The current version of a source: {doc_id, version_seq, op, task}. A source with
    `source_versions` rows is read from them (a withdrawn one has no current version); an event note
    captured before schema 4 has none and is found by its doc id."""
    conn = _conn(db)
    if _has_table(conn, "source_versions"):
        rows = conn.execute("SELECT doc_id,version_seq,op,task,status FROM source_versions "
                            "WHERE source_ref=? ORDER BY version_seq DESC", (source_ref,)).fetchall()
        if rows:
            cur = next((r for r in rows if r["status"] == "current"), None)
            return None if cur is None else {k: cur[k] for k in ("doc_id", "version_seq", "op", "task")}
    m = _EVENT_REF.match(source_ref)
    if m:
        doc = conn.execute("SELECT doc_id FROM docs WHERE doc_id LIKE ? AND doc_id NOT LIKE 'raw/sources/%' "
                           "ORDER BY doc_id DESC LIMIT 1", ("raw/%%/%06d-%%.md" % int(m.group(1)),)).fetchone()
        if doc:
            return {"doc_id": doc[0], "version_seq": 0, "op": None, "task": None}
    return None


def _active_blocks(db, doc_id: str) -> list:
    return _conn(db).execute(
        "SELECT block_id,doc_id,ordinal,block_content_id,block_sha256,text,source_created_at,valid_from "
        "FROM blocks WHERE doc_id=? AND status='active' ORDER BY ordinal", (doc_id,)).fetchall()


def resolve_anchor(db, source_ref: str, quote: str) -> dict | None:
    """architecture.md 4.3. The first active block, by ordinal, of the current version of
    `source_ref` whose normalized text holds the normalized quote, as a dict; None when unresolved."""
    if not quote or not normalize_text(quote):
        return None
    version = current_version(db, source_ref)
    if version is None:
        return None
    needle = normalize_text(quote)
    for block in _active_blocks(db, version["doc_id"]):
        if needle in normalize_text(block["text"]):
            out = dict(block)
            out["version"] = version
            return out
    return None


# ------------------------------------------------------------------ the checks

@dataclass
class Context:
    """What the checks read. `store` is a read-only store view (an object with `.conn`) or None
    before the store exists; `event_hash(n)` is the record row's hash or None; `entailer` is the
    configured deterministic provider; `pending` is the proposer's count of `checked` candidates."""
    store: Any
    event_hash: Any
    entailer: Any
    pending: int = 0


@dataclass
class Checked:
    candidate_id: str
    kind: str
    status: str                                  # checked | refused
    checks: list = field(default_factory=list)   # CheckResult
    anchors: list = field(default_factory=list)  # row dicts, role anchor then context
    body: dict | None = None                     # None when any anchor failed K-4
    body_sha256: str = ""
    entailer: str | None = None
    verdict: str | None = None
    prior_edge_id: str | None = None
    k4_failed: bool = False

    def event(self) -> dict:
        """The `wiki-candidate` event payload (architecture.md 10.2): complete enough for a replay to
        write the same rows without running a check."""
        return {"candidate_id": self.candidate_id, "kind": self.kind, "body": self.body,
                "body_sha256": self.body_sha256, "anchors": [dict(a) for a in self.anchors],
                "checks": [_check_dict(c) for c in self.checks], "status": self.status,
                "entailer": self.entailer, "verdict": self.verdict,
                "prior_edge_id": self.prior_edge_id}


def _check_dict(c: CheckResult) -> dict:
    return {"check": c.check, "passed": c.passed, "reason": c.reason, "detail": c.detail}


def duplicate_event(p: Proposal, cid: str) -> dict:
    """The `wiki-candidate` event of a repeated proposal (OB-21): only the candidate id, kind,
    status and each anchor's and context entry's source_ref and quote sha256, never a quote or a body."""
    return {"candidate_id": cid, "kind": p.kind, "status": "duplicate",
            "anchors": [{"source_ref": a.source_ref, "quote_sha256": quote_sha256(a.quote)}
                        for a in (*p.anchors, *p.context)]}


def _contains(haystack: str, needle: str) -> bool:
    """`needle` in `haystack`, casefolded, whole-word at each end that is a word character, so a
    short name never matches inside a longer one."""
    h, n = normalize_text(haystack).casefold(), normalize_text(needle).casefold()
    if not n:
        return False
    pattern = (r"(?<!\w)" if re.match(r"\w", n[0]) else "") + re.escape(n) + (
        r"(?!\w)" if re.match(r"\w", n[-1]) else "")
    return re.search(pattern, h) is not None


def _sentences(text: str) -> list:
    return [s for s in _SENTENCE_END.split(text.strip()) if s]


_EDGE_PUNCT = "\"'()[]{}<>,;:!?*`"
_IDENT = re.compile(r"[0-9_./-]")
_POSSESSIVE = re.compile(r"['\u2019]s$")
#: Inside a whitespace token, these join no identifier: they split it into words (a dash other than
#: `-`, an ellipsis, a quote mark, a zero-width or other format character), so a name glued to
#: another word by one of them is still a word of its own.
_KEEP_INSIDE = set("-_./'\u2019")


def _trim(tok: str) -> str:
    """`tok` without the characters at either end that are punctuation (Unicode category P*), a
    symbol (S*), a format character (Cf, such as a zero-width space) or one of `_EDGE_PUNCT`."""
    def edge(ch: str) -> bool:
        cat = unicodedata.category(ch)
        return ch in _EDGE_PUNCT or cat[0] in "PS" or cat == "Cf"
    start, end = 0, len(tok)
    while start < end and edge(tok[start]):
        start += 1
    while end > start and edge(tok[end - 1]):
        end -= 1
    return tok[start:end]


def _words(raw: str) -> list:
    """The words of one whitespace token: split at every punctuation, symbol or format character
    other than the identifier marks `-`, `_`, `.`, `/` and an apostrophe."""
    out, cur = [], []
    for ch in raw:
        cat = unicodedata.category(ch)
        if ch not in _KEEP_INSIDE and (cat[0] in "PSZ" or cat == "Cf"):
            if cur:
                out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _capitalized(tok: str) -> bool:
    """True when the first letter of `tok` (not its first character) is upper or title case."""
    first = next((ch for ch in tok if ch.isalpha()), "")
    return bool(first) and (first.isupper() or first.istitle())


def sentence_fields(sentence: str) -> list:
    """The tokens of a sentence that must each sit in one of its anchors (architecture.md 7.2 K-3,
    OB-13): every number, every identifier-like token (one holding a digit, `-`, `_`, `.` or `/`) and
    every capitalized word that does not start the sentence. Besides each whitespace token as it
    stands, its words count too: a token is split at the punctuation, symbol and format characters
    inside it and read without those around it, and a word is capitalized by its first letter, not
    its first character, so a name written as \u201cViet\u201d, \u00abViet\u00bb, after a zero-width
    space or glued to a word by a dash is a field like a bare one. Pure and model-free."""
    out = []
    for k, raw in enumerate(sentence.split()):
        tok = _POSSESSIVE.sub("", raw.strip(_EDGE_PUNCT).rstrip(".").strip(_EDGE_PUNCT))
        if tok and re.search(r"\w", tok) and (_IDENT.search(tok) or (k > 0 and tok[0].isupper())):
            out.append(tok)
        for j, part in enumerate(_words(raw)):
            word = _trim(_POSSESSIVE.sub("", _trim(part)))
            if not word or not re.search(r"\w", word) or word in out:
                continue
            if _IDENT.search(word) or ((k > 0 or j > 0) and _capitalized(word)):
                out.append(word)
    return out


def _hide_reason(ctx: Context, ref: str) -> str:
    """Why `servable_ref` is false: `excluded-anchor` when a withdrawal took the source out (an
    exclusions row names it, or it fell with one: `servable.excluded_source_refs`), else
    `private-anchor`."""
    from .servable import excluded_source_refs
    if ref in excluded_source_refs(ctx.store):
        return "excluded-anchor"
    return "private-anchor"


def check_proposal(p: Proposal, ctx: Context) -> Checked:
    """Run K-1 to K-6 read-only and return the candidate's status, rows and event body."""
    from .servable import servable_ref
    cid = candidate_id(p)
    n = len(p.anchors)
    results: dict = {}

    def fail(check: str, reason: str, detail: str) -> None:
        results.setdefault(check, CheckResult(check, False, reason, detail))

    store = ctx.store
    resolved: list = [None] * n          # the block each anchor resolves to
    source_ok: list = [False] * n        # K-1 per anchor

    # K-1 and K-2
    for i, a in enumerate(p.anchors):
        version = current_version(store, a.source_ref) if store is not None else None
        blocks = _active_blocks(store, version["doc_id"]) if version else []
        m = _EVENT_REF.match(a.source_ref)
        hash_ok = True
        if m:
            row_hash = ctx.event_hash(int(m.group(1)))
            hash_ok = bool(a.event_hash) and row_hash is not None and a.event_hash == row_hash
        if not blocks or not hash_ok:
            fail("K-1", "anchor-unresolved", "anchor[%d]" % i)
            continue
        source_ok[i] = True
        found = resolve_anchor(store, a.source_ref, a.quote)
        if found is None:
            fail("K-2", "quote-not-in-span", "anchor[%d]" % i)
        else:
            resolved[i] = found

    # K-4
    failed_k4 = set()
    for i, a in enumerate(p.anchors):
        if store is None:
            break
        if not servable_ref(store, a.source_ref):
            failed_k4.add(i)
            fail("K-4", _hide_reason(ctx, a.source_ref), "anchor[%d]" % i)
        elif p.scope:
            version = current_version(store, a.source_ref)
            if version is not None and any(version.get(k) != v for k, v in p.scope.items()):
                failed_k4.add(i)
                fail("K-4", "out-of-scope", "anchor[%d]" % i)

    # K-5: an independent kind, and no generated briefing text in the quote or the block it names
    from .briefing import holds_generated
    for i, a in enumerate(p.anchors):
        if source_kind(a.source_ref) not in INDEPENDENT_KINDS or ":" not in a.source_ref:
            fail("K-5", "not-independent", "anchor[%d]" % i)
        elif holds_generated(a.quote) or (resolved[i] is not None
                                          and holds_generated(resolved[i]["text"])):
            fail("K-5", "not-independent", "anchor[%d] briefing-text" % i)

    # K-6
    if n < 1 or n > MAX_ANCHORS:
        fail("K-6", "limit", "anchors")
    if p.kind == "observation" and len(p.text or "") > OBSERVATION_CHARS:
        fail("K-6", "limit", "text")
    if p.kind == "summary" and len(p.text or "") > SUMMARY_CHARS:
        fail("K-6", "limit", "text")
    if ctx.pending >= PENDING_LIMIT:
        fail("K-6", "limit", "pending")

    # K-3
    verdict = prior = None
    if p.kind == "fact":
        verdict, prior = _check_fact(p, ctx, results, resolved)
    else:
        _check_text(p, ctx, results, resolved)

    for check in ("K-1", "K-2", "K-3", "K-4", "K-5", "K-6"):
        results.setdefault(check, CheckResult(check, True, None, None))
    checks = [results[c] for c in ("K-1", "K-2", "K-3", "K-4", "K-5", "K-6")]
    status = "refused" if any(not c.passed for c in checks) else "checked"
    if status == "refused":
        verdict = prior = None

    rows = []
    for i, a in enumerate(p.anchors):
        block = resolved[i]
        rows.append({
            "role": "anchor", "source_ref": a.source_ref,
            "quote": None if i in failed_k4 else a.quote, "quote_sha256": quote_sha256(a.quote),
            "doc_id": block["doc_id"] if block else a.doc_id,
            "block_content_id": block["block_content_id"] if block else a.block_content_id,
            "block_sha256": block["block_sha256"] if block else None})
    for a in p.context:
        rows.append(_context_row(a, ctx))

    body = _body(p, cid, rows)
    body_sha = sha256_hex(canonical_json(body))
    return Checked(candidate_id=cid, kind=p.kind, status=status, checks=checks, anchors=rows,
                   body=None if failed_k4 else body, body_sha256=body_sha,
                   entailer=getattr(ctx.entailer, "name", None), verdict=verdict,
                   prior_edge_id=prior, k4_failed=bool(failed_k4))


def _context_row(a: Anchor, ctx: Context) -> dict:
    """OB-22: the quote is kept only when the entry passes `servable_ref`, K-1 and K-2 in the source
    its `source_ref` names. These checks decide redaction only; the entry is never support."""
    from .servable import servable_ref
    keep = None
    store = ctx.store
    block = None
    if store is not None and servable_ref(store, a.source_ref):
        block = resolve_anchor(store, a.source_ref, a.quote)
        m = _EVENT_REF.match(a.source_ref)
        if block is not None and m and a.event_hash is not None:
            block = block if ctx.event_hash(int(m.group(1))) == a.event_hash else None
        if block is not None:
            keep = a.quote
    return {"role": "context", "source_ref": a.source_ref, "quote": keep,
            "quote_sha256": quote_sha256(a.quote),
            "doc_id": block["doc_id"] if keep is not None else None,
            "block_content_id": block["block_content_id"] if keep is not None else None,
            "block_sha256": block["block_sha256"] if keep is not None else None}


def _body(p: Proposal, cid: str, rows: list) -> dict:
    refs = [r["source_ref"] for r in rows if r["role"] == "anchor"]
    if p.kind == "fact":
        f = p.fact
        return {"fact": {"subject": f.subject, "predicate": f.predicate, "obj": f.obj,
                         "obj_datatype": f.obj_datatype, "valid_from": f.valid_from},
                "fact_kind": derive_fact_kind(refs), "scope": p.scope}
    body = {"text": p.text, "sentence_anchors": [list(s) for s in p.sentence_anchors],
            "scope": p.scope}
    if p.observation is not None:
        o = p.observation
        body["observation"] = {"label": o.label, "governs": list(o.governs),
                               "prescribes": o.prescribes, "falsifiability": o.falsifiability}
    return body


def _quotes(p: Proposal) -> list:
    return [a.quote for a in p.anchors]


def primary_anchor_index(p: Proposal) -> int | None:
    """The first anchor whose quote holds every field of the fact (subject, object unless bool,
    valid_from). The accepted edge is placed on this anchor, so no field of an accepted fact is
    missing from its cited span (Z10). None when no single anchor holds them all."""
    f = p.fact
    wanted = [f.subject]
    if f.obj_datatype != "bool":
        wanted.append(f.obj)
    if f.valid_from:
        wanted.append(f.valid_from)
    for i, a in enumerate(p.anchors):
        if all(_contains(a.quote, v) for v in wanted):
            return i
    return None


def _check_fact(p: Proposal, ctx: Context, results: dict, resolved: list):
    f = p.fact
    store = ctx.store

    def fail(reason: str, detail: str) -> None:
        results.setdefault("K-3", CheckResult("K-3", False, reason, detail))

    conn = _conn(store) if store is not None else None
    row = conn.execute("SELECT extraction,cardinality FROM predicates WHERE predicate=?",
                       (f.predicate,)).fetchone() if conn is not None and _has_table(
                           conn, "predicates") else None
    if row is None or row["extraction"] != "llm-allowed":
        fail("predicate-not-allowed", "field:predicate")
        return None, None
    quotes = _quotes(p)
    wanted = [("subject", f.subject)]
    if f.obj_datatype != "bool":
        wanted.append(("object", f.obj))
    if f.valid_from:
        wanted.append(("valid_from", f.valid_from))
    missing = [name for name, value in wanted if not any(_contains(q, value) for q in quotes)]
    if not missing and primary_anchor_index(p) is None:
        missing = ["single-anchor"]          # the fields are spread over several anchors (op-008 t-066 fix-r1 B2)
    if missing:
        fail("field-not-in-span", "field:" + ",".join(missing))
        return None, None
    from .ingest.resolve import resolve_surface
    ids = {"subject": resolve_surface(store, f.subject)}
    if f.obj_datatype == "node":
        ids["object"] = resolve_surface(store, f.obj)
    ambiguous = [name for name, nodes in ids.items() if len(nodes) > 1]
    if ambiguous:
        fail("ambiguous-entity", "field:" + ",".join(ambiguous))
        return None, None
    claim = " ".join(x for x in (f.subject, f.obj, f.valid_from) if x)
    label, _score = ctx.entailer.entail(claim, p.anchors[primary_anchor_index(p)].quote)
    if label != "supported":
        fail("sentence-unsupported", "claim")
        return None, None
    return _advisory_verdict(p, ctx, ids, resolved)


def _advisory_verdict(p: Proposal, ctx: Context, ids: dict, resolved: list):
    """The read-only `reconcile.classify` verdict against the active edges, written nowhere
    (architecture.md 7.3). None when an anchor did not resolve."""
    from .ingest import reconcile
    if not resolved or any(b is None for b in resolved):
        return None, None
    # the block admission places the edge on: the primary anchor's (t-070 B-5; before, anchor 0's
    # block with the primary anchor's quote, so the receipt could say `matches` where admission
    # found `duplicate`)
    primary = primary_anchor_index(p) or 0
    block = resolved[primary]
    f = p.fact
    cand = fact_edge(f, ids, block, p.anchors[primary].quote)
    if cand is None:
        return None, None
    v = reconcile.classify(ctx.store, cand, cand["edge_id"])
    return v.verdict, v.prior_edge_id


def admission_time(f: FactBody, block) -> tuple:
    """(valid_from, source_created_at) of an admitted agent fact (architecture.md 5.1): the agent's
    `valid_from` when given, which K-3 found in a quote; else the anchor block's own source time;
    else unknown, which is the block's capture-time `valid_from` with a null `source_created_at`,
    never the admission clock. `(None, None)` when the agent's `valid_from` is not a readable time."""
    from .clock import to_clock_format
    if f.valid_from:
        given = to_clock_format(f.valid_from)
        return (given, given) if given else (None, None)
    if block["source_created_at"]:
        return block["source_created_at"], block["source_created_at"]
    return block["valid_from"], None


def fact_edge(f: FactBody, ids: dict, block, quote: str) -> dict | None:
    """The edge an agent fact stands for, as `reconcile.classify` and `upsert_edge` take it, with its
    content-derived `edge_id`; None when the time is unreadable (see `admission_time`)."""
    from .determinism import edge_id as make_edge_id
    from .ingest.encode import obj_key
    valid_from, created = admission_time(f, block)
    if valid_from is None:
        return None
    subj = ids["subject"][0] if ids["subject"] else nfc(f.subject)
    cand = {"subj_node": subj, "predicate": f.predicate, "obj_datatype": f.obj_datatype,
            "obj_node": (ids["object"][0] if ids.get("object") else nfc(f.obj))
            if f.obj_datatype == "node" else None,
            "obj_literal": None if f.obj_datatype == "node" else f.obj,
            "valid_from": valid_from, "source_created_at": created, "extractor": "llm",
            "source_block_id": block["block_id"], "source_doc_id": block["doc_id"],
            "source_quote": quote}
    cand["edge_id"] = make_edge_id(subj, f.predicate, obj_key(cand), block["block_content_id"])
    return cand


def _check_text(p: Proposal, ctx: Context, results: dict, resolved: list) -> None:
    def fail(reason: str, detail: str) -> None:
        results.setdefault("K-3", CheckResult("K-3", False, reason, detail))

    o = p.observation
    if o is not None and ((o.label is not None and not _SLUG.match(o.label))
                          or ((o.governs or o.prescribes) and not o.label)):
        fail("label-not-slug", "field:label")
        return
    sentences = _sentences(p.text or "")
    cited = [list(s) for s in p.sentence_anchors]
    if (len(cited) != len(sentences) or any(not s for s in cited)
            or any(i < 0 or i >= len(p.anchors) for s in cited for i in s)):
        fail("sentence-without-anchor", "sentence_anchors")
        return
    for k, (sentence, idx) in enumerate(zip(sentences, cited)):
        label, _score = ctx.entailer.entail(sentence, " ".join(p.anchors[i].quote for i in idx))
        if label != "supported":
            fail("sentence-unsupported", "sentence:%d" % k)
            return
        # The entailer passes 80% token coverage, which lets one invented name through (Z10, OB-13).
        absent = [t for t in sentence_fields(sentence)
                  if not any(_contains(p.anchors[i].quote, t) for i in idx)]
        if absent:
            fail("field-not-in-span", "sentence:%d" % k)
            return


# ------------------------------------------------------------------ admission (architecture.md 7.4)

def proposal_from_row(row: dict, anchor_rows: list, event_hash) -> Proposal | None:
    """The `Proposal` a stored candidate stands for, so admission can run the checks again on the
    current store. None when the row holds no body or an anchor quote (a refusal that hid them is
    never admitted). An `event:` anchor takes the record row's hash as it is now: the record is
    append-only and the hash was matched at proposal time."""
    if row.get("body_json") is None:
        return None
    body = json.loads(row["body_json"])
    rows = [a for a in anchor_rows if a["role"] == "anchor" and a["ordinal"] >= 0]
    if not rows or any(a["quote"] is None for a in rows):
        return None
    anchors = []
    for a in sorted(rows, key=lambda r: r["ordinal"]):
        m = _EVENT_REF.match(a["source_ref"])
        anchors.append(Anchor(source_ref=a["source_ref"], quote=a["quote"],
                              event_hash=event_hash(int(m.group(1))) if m else None))
    if row["kind"] == "fact":
        f = body["fact"]
        return Proposal(kind="fact", anchors=tuple(anchors),
                        fact=FactBody(f["subject"], f["predicate"], f["obj"], f["obj_datatype"],
                                      f.get("valid_from")), scope=body.get("scope"))
    o = body.get("observation")
    observation = None if o is None else ObservationBody(
        label=o.get("label"), governs=tuple(o.get("governs") or ()),
        prescribes=o.get("prescribes"), falsifiability=o.get("falsifiability"))
    return Proposal(kind=row["kind"], anchors=tuple(anchors), text=body.get("text"),
                    sentence_anchors=tuple(tuple(s) for s in body.get("sentence_anchors") or ()),
                    observation=observation, scope=body.get("scope") or None)


fact_proposal_from_row = proposal_from_row


def recheck(row: dict, anchor_rows: list, ctx: Context) -> tuple:
    """K-1 to K-5 again, on the current store, for a candidate of any kind (architecture.md 7.4,
    OB-15): returns (Proposal, Checked), or (None, reason) when the candidate cannot be rebuilt. An
    anchor that stopped resolving or turned non-servable between `propose` and `admit` fails here, so
    no kind admits on a stale check. The pending limit (K-6) is not asked again at admission."""
    p = proposal_from_row(row, anchor_rows, ctx.event_hash)
    if p is None:
        return None, "body-redacted"
    ctx = Context(store=ctx.store, event_hash=ctx.event_hash, entailer=ctx.entailer, pending=0)
    return p, check_proposal(p, ctx)


recheck_fact = recheck


# ------------------------------------------------------------------ observation doors (architecture.md 7.4)

def observation_door(o: ObservationBody | None) -> tuple:
    """(door, reason): `procedural` (the lesson gate) when the body names a label, probe tags and a
    prescription; `declarative` (a QUARANTINE prior) when it names a falsifiability pointer. A body
    that is missing, mixes both, or leaves a procedural field out has no door: the live agent
    judges the kind and supplies the gate metadata, Rune2 derives the rest (ARCH 7.4)."""
    if o is None:
        return None, "observation-body-missing"
    procedural = bool(o.label or o.governs or o.prescribes)
    declarative = bool(o.falsifiability and o.falsifiability.strip())
    if procedural and declarative:
        return None, "observation-body-ambiguous"
    if declarative:
        return "declarative", None
    if procedural:
        if not (o.label and o.governs and o.prescribes):
            return None, "observation-body-incomplete"
        return "procedural", None
    return None, "observation-body-missing"


def lesson_id_for(cid: str) -> str:
    """The lesson id of an admitted procedural observation: derived from its candidate id, never from
    the label, so two observations with one label slug never share an id (OB-06)."""
    return "L-" + cid[2:] if cid.startswith("c_") else "L-" + cid


def claim_id_for(cid: str) -> str:
    """The prior `claim_id` of an admitted declarative observation: its candidate id (OB-06)."""
    return cid


_SCOPE_COMPONENT = re.compile(r"[^A-Za-z0-9_-]+")


def prior_scope(scope: dict | None) -> str:
    """The knowledge scope of a declarative prior: the proposal's op or task, else `project`,
    reduced to the characters the gate accepts."""
    for key in ("op", "task"):
        value = (scope or {}).get(key)
        if value:
            return _SCOPE_COMPONENT.sub("-", value).strip("-") or "project"
    return "project"


_CASE_ID = re.compile(r"^C\d{2}-\d{3}$")


def probe_eval_items(corpus: list) -> list:
    """The ids of the probe corpus items that are held-out evaluation items (FR-16): an id in the
    case form of the question set (`C07-001`), or an item carrying a `case` or `question` field. A
    lesson is never probed on an item the evaluation scores (architecture.md 7.4). Production code
    reads nothing of the development-only evaluation tooling (FR-13, FR-27; t-070 S-1): the id form
    and the two fields are the whole check."""
    hits = []
    for item in corpus:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("id", ""))
        if _CASE_ID.match(ident) or "case" in item or "question" in item:
            hits.append(ident)
    return hits


def first_failure(checked: Checked) -> CheckResult | None:
    return next((c for c in checked.checks if not c.passed), None)


def fact_identity(db, edge) -> dict:
    """The portable name of an edge (interfaces `FactIdentity`, architecture.md 10.2): its subject,
    predicate, object key and valid_from, and the source and quote hash of its anchor. An edge of
    unknown time (a null `source_created_at`) has a capture-time `valid_from` that a rebuild would
    not reproduce, so its identity carries a null `valid_from` and matches by that."""
    from .ingest.encode import obj_key
    from .servable import source_refs_for_docs
    edge = dict(edge)
    refs = source_refs_for_docs(db, [edge["source_doc_id"]])
    return {"subj_node": edge["subj_node"], "predicate": edge["predicate"],
            "obj_key": obj_key(edge),
            "valid_from": edge["valid_from"] if edge["source_created_at"] else None,
            "anchor_source_ref": refs.get(edge["source_doc_id"]) or "document:%s" % edge["source_doc_id"],
            "anchor_quote_sha256": sha256_hex(edge["source_quote"])}


def resolve_fact_identity(db, ident: dict) -> list:
    """The edge ids a `FactIdentity` names whose anchor block is in the current version of its
    source: one is a match, none or several mean the act cannot be re-applied (stale). An event-note
    anchor ignores the quote hash, so a note re-render keeps the identity (OB-14)."""
    from .ingest.encode import obj_key
    version = current_version(db, ident["anchor_source_ref"])
    if version is None:
        return []
    out = []
    for r in _conn(db).execute(
            "SELECT * FROM edges WHERE subj_node=? AND predicate=? "
            "AND status IN ('active','invalidated') ORDER BY edge_id",
            (ident["subj_node"], ident["predicate"])):
        same_time = (r["source_created_at"] is None if ident["valid_from"] is None
                     else r["valid_from"] == ident["valid_from"])
        if (not same_time or obj_key(dict(r)) != ident["obj_key"]
                or r["source_doc_id"] != version["doc_id"]):
            continue
        if (source_kind(ident["anchor_source_ref"]) != "event"
                and sha256_hex(r["source_quote"]) != ident["anchor_quote_sha256"]):
            continue
        out.append(r["edge_id"])
    return out


# ------------------------------------------------------------------ rows and views

def rows_from_event(data: dict, *, proposer: str, operator: str | None, event_id: int,
                    ts: str) -> tuple | None:
    """The `candidates` row and `candidate_anchors` rows a `wiki-candidate` event writes, from its
    recorded fields alone (no check runs again, so a rebuild writes the row the live call wrote);
    None for a `duplicate` event, which writes no row (architecture.md 7.1, 10.2)."""
    if data.get("status") == "duplicate":
        return None
    body = data.get("body")
    row = {"candidate_id": data["candidate_id"], "kind": data["kind"],
           "body_json": canonical_json(body) if body is not None else None,
           "body_sha256": data["body_sha256"], "proposer": proposer,
           "proposer_kind": "job" if proposer.startswith("job:") else "session",
           "proposer_operator": operator, "record_event_id": event_id,
           "checks_json": canonical_json(data["checks"]), "status": data["status"],
           "verdict": data.get("verdict"), "prior_edge_id": data.get("prior_edge_id"),
           "created_at": ts, "updated_at": ts}
    return row, [dict(a) for a in data["anchors"]]


def candidate_views(store, rows: list, anchor_rows: dict) -> list:
    """`CandidateView`s of stored rows. Redaction is decided now, not at proposal time: a failing
    anchor's quote, and the body when an anchor fails, become null (`redacted`) with their sha256;
    a context entry shows its quote only when it is stored and its source is servable (OB-22), and is
    labelled `checked: False` whatever it holds (OA-5)."""
    from .servable import servable_ref
    out = []
    for r in rows:
        anchors, fresh, any_fail = [], True, False
        for a in anchor_rows.get(r["candidate_id"], ()):
            ok = servable_ref(store, a["source_ref"])
            is_anchor = a["role"] == "anchor"
            if is_anchor:
                any_fail = any_fail or not ok
                version = current_version(store, a["source_ref"])
                live = bool(version and a["block_content_id"] and _conn(store).execute(
                    "SELECT 1 FROM blocks WHERE doc_id=? AND status='active' AND block_content_id=?",
                    (version["doc_id"], a["block_content_id"])).fetchone())
                fresh = fresh and live
            quote = a["quote"] if (ok and a["quote"] is not None) else None
            anchors.append({"source_ref": a["source_ref"], "quote": quote,
                            "quote_sha256": a["quote_sha256"], "redacted": quote is None,
                            "role": a["role"], "checked": is_anchor})
        body = None if (any_fail or r["body_json"] is None) else json.loads(r["body_json"])
        out.append(CandidateView(
            candidate_id=r["candidate_id"], kind=r["kind"], status=r["status"], fresh=fresh,
            proposer=r["proposer"], proposer_kind=r["proposer_kind"],
            proposer_operator=r["proposer_operator"], body=body, body_sha256=r["body_sha256"],
            body_redacted=body is None, anchors=tuple(anchors), admission_ref=r["admission_ref"],
            admission_effect=r["admission_effect"], checks=tuple(json.loads(r["checks_json"]))))
    return out


def store_view(conn) -> SimpleNamespace:
    """A read-only store connection in the shape the shared readers take (`.conn`)."""
    return SimpleNamespace(conn=conn)
