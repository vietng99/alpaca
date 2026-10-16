"""engine.echo - echo-collapse to an independent-witness count or a hard NULL (oracle-safety.2).

The hole this closes: an echoed claim ("As [[X]] noted, …", a verbatim re-quote, a near-duplicate
restatement) inflated `corroboration_count` and then WON conflict arbitration on manufactured
weight. The fix collapses echo chains onto their provable primary witness and reports the count of
DISTINCT independent roots - or a **hard NULL** when independence cannot be proven. A fabricated
integer never leaks: ambiguity resolves to NULL, never a guess (there is no model in the loop).

Derivation is captured at absorb in `block_provenance(block_id, cites_block_id, cites_node_id,
relation, detector)` with `relation ∈ {quotes, attributes, restates, original}`:
  - `quotes`    - a verbatim span of a known block  -> provable primary (follow cites_block_id).
  - `attributes`- "As [[X]] noted" / VN "theo|dẫn lời|trích [[X]]" -> primary resolved IFF the cited
                  node has a UNIQUE original block, else NULL (unresolvable attribution).
  - `restates`  - near-duplicate claim w/o attribution -> conservatively collapsed to the earliest
                  asserter (undercount, never overcount).
  - `original`  - a root witness (records the node it defines in cites_node_id).

`independent_witness_count(db, edge_id)` walks every corroborating block's cites chain to a fixpoint
root and returns len(distinct roots); NULL on an unresolvable attribution or a cycle. The value is
persisted on `edges.independent_witness_count` (NULLABLE). `arbitrate_conflict` must consume it in
place of `corroboration_count` and DROP the corroboration term when it is NULL (authority + recency
still order) - see `corroboration_for_arbitration` and the shared_edits wiring note.

This module additively provisions its own schema (block_provenance table + three edges columns) at
runtime via the guarded `DB.migrate_column` / `CREATE TABLE IF NOT EXISTS` - no schema.sql edit.
"""
from __future__ import annotations

import re
from array import array
import math
import sys
from typing import Optional

from ..determinism import normalize_text

# --------------------------------------------------------------------------- schema (additive)

def ensure_schema(db) -> None:
    """Idempotently add the echo-provenance table + edges columns. Safe to call repeatedly."""
    db.conn.execute(
        "CREATE TABLE IF NOT EXISTS block_provenance ("
        " block_id       TEXT NOT NULL,"
        " cites_block_id TEXT,"
        " cites_node_id  TEXT,"
        " relation       TEXT NOT NULL CHECK (relation IN ('quotes','attributes','restates','original')),"
        " detector       TEXT,"
        " PRIMARY KEY (block_id, relation, cites_block_id, cites_node_id))"
    )
    db.migrate_column("edges", "derived", "INTEGER DEFAULT 0")
    db.migrate_column("edges", "echo_of_edge_id", "TEXT")
    db.migrate_column("edges", "independent_witness_count", "INTEGER")
    db.commit()


def record_provenance(db, block_id: str, relation: str, cites_block_id: Optional[str] = None,
                      cites_node_id: Optional[str] = None, detector: str = "det") -> None:
    """Persist one derivation row (called at absorb). `relation` governs how the chain is walked."""
    db.conn.execute(
        "INSERT OR IGNORE INTO block_provenance"
        "(block_id, cites_block_id, cites_node_id, relation, detector) VALUES(?,?,?,?,?)",
        (block_id, cites_block_id, cites_node_id, relation, detector),
    )


# --------------------------------------------------------------------------- deterministic detector

# EN: "As [[X]] noted/said/wrote/argued", "according to [[X]]", "per [[X]]".
# VN: "theo [[X]]", "dẫn lời [[X]]", "trích [[X]]".
_ATTRIB = re.compile(
    r"(?:\bAs\b|\baccording to\b|\bper\b|\btheo\b|\bdẫn lời\b|\btrích\b)\s*\[\[([^\]|]+)",
    re.IGNORECASE)


def _slug(surface: str) -> str:
    s = normalize_text(surface).lower()
    s = re.sub(r"[^a-z0-9à-ỹ]+", "-", s).strip("-")
    return s


class EchoIndex:
    """Incremental token index for deterministic echo detection, held compactly (t-685).

    Every block is a slot: its block id, its document, the length and the hash of its normalized
    text, and its distinct tokens as an array of 4-byte token ids. A token's postings are an array
    of slots (a token of one block holds that slot alone), and a removed block's slot stays in them, listed in `_dead`, until the index is
    compacted (`_compact`, once the removed slots outnumber a quarter of the live ones), so a
    removal costs its own tokens only and no slot is reused while a posting may still hold it.
    Before t-685 the index held each block's whole normalized text, a tuple of its tokens and a
    Python set per posting and per distinct text: 6.8 GB for a copy of the live store's 1,091,479
    active blocks (66.6 million postings over 3.2 million tokens), against 1.7 GB now.

    `fetch(block_id)`, when given, returns a block's text from the store (`load_echo_index` passes
    the absorber's own connection), and the normalized texts are not kept: the two checks that read
    a text, an exact match (by hash first) and a verbatim quote, read it from the store, the quote
    check only for a block that can be a substring of the query (no longer than it, and holding no
    more than two tokens the query lacks). Without `fetch`
    the normalized texts are kept, as before. Either way every verdict is the one the set-based
    index gave: the same candidates, read in block id order, with the same tests."""

    def __init__(self, fetch=None):
        self._fetch = fetch
        self._slot: dict[str, int] = {}              # block id -> slot
        self._ids: list = []                         # slot -> block id, None once removed
        self._docs: list = []                        # slot -> document id (interned)
        self._size = array("I")                      # slot -> distinct tokens
        self._length = array("I")                    # slot -> len(normalized text)
        self._hash = array("q")                      # slot -> hash(normalized text)
        self._text: list | None = [] if fetch is None else None
        self._tok: list = []                         # slot -> array of token ids, None once removed
        self._vocab: dict[str, int] = {}             # token -> token id
        self._words: list = []                       # token id -> token, None once free
        self._df = array("I")                        # token id -> live blocks holding it
        self._post: list = []                        # token id -> array of slots (removed ones too),
        #                                              or the slot alone for a token of one block
        self._free_tokens: list[int] = []
        self._exact: dict[int, object] = {}          # hash(normalized) -> slot, or a list of slots
        self._by_doc: dict = {}                      # document -> array of slots (removed ones too)
        self._dead: set[int] = set()

    def __len__(self) -> int:
        return len(self._slot)

    def __contains__(self, block_id) -> bool:
        return block_id in self._slot

    # ---- writes
    def add(self, block_id: str, text: str, doc_id: str | None = None) -> None:
        self.remove(block_id)
        self._put(block_id, normalize_text(text), doc_id)

    def load(self, rows) -> None:
        """Bulk `add` of `(block_id, text, doc_id)` rows into an empty index (the load of a whole
        store)."""
        put = self._put
        for block_id, text, doc_id in rows:
            put(block_id, normalize_text(text), doc_id)

    def _put(self, block_id, normalized, doc_id):
        if doc_id is not None:
            doc_id = sys.intern(doc_id)              # one string per document, not per block
        slot = len(self._ids)
        vocab, df, post = self._vocab, self._df, self._post
        tids = array("I")
        for word in dict.fromkeys(normalized.split()):
            tid = vocab.get(word)
            if tid is None:
                tid = self._new_token(word)
            df[tid] += 1
            held = post[tid]
            if held is None:
                post[tid] = slot                     # a token of one block: its slot, no array
            elif held.__class__ is int:
                post[tid] = array("I", (held, slot))
            else:
                held.append(slot)
            tids.append(tid)
        self._slot[block_id] = slot
        self._ids.append(block_id)
        self._docs.append(doc_id)
        self._size.append(len(tids))
        self._length.append(len(normalized))
        key = hash(normalized)
        self._hash.append(key)
        if self._text is not None:
            self._text.append(normalized)
        self._tok.append(tids)
        held = self._exact.get(key)
        if held is None:
            self._exact[key] = slot
        elif isinstance(held, list):
            held.append(slot)
        else:
            self._exact[key] = [held, slot]
        members = self._by_doc.get(doc_id)
        if members is None:
            members = self._by_doc[doc_id] = array("I")
        members.append(slot)

    def _new_token(self, word):
        if self._free_tokens:
            tid = self._free_tokens.pop()
            self._words[tid] = word
        else:
            tid = len(self._post)
            self._post.append(None)
            self._words.append(word)
            self._df.append(0)
        self._vocab[word] = tid
        return tid

    def remove(self, block_id: str) -> None:
        slot = self._slot.pop(block_id, None)
        if slot is None:
            return
        df, post = self._df, self._post
        for tid in self._tok[slot]:
            df[tid] -= 1
            if not df[tid]:                          # no live block holds the token any more
                del self._vocab[self._words[tid]]
                self._words[tid] = post[tid] = None
                self._free_tokens.append(tid)
        key = self._hash[slot]
        held = self._exact.get(key)
        if isinstance(held, list):
            held.remove(slot)
            if len(held) == 1:
                self._exact[key] = held[0]
        elif held == slot:
            del self._exact[key]
        self._ids[slot] = None
        self._docs[slot] = None
        self._tok[slot] = None
        if self._text is not None:
            self._text[slot] = None
        self._dead.add(slot)
        if len(self._dead) > max(4096, len(self._slot) // 4):
            self._compact()

    def _compact(self) -> None:
        """Number the live blocks again from 0 and rebuild every posting and document list from their
        token arrays, dropping the removed slots (no store read)."""
        live = [slot for slot, block_id in enumerate(self._ids) if block_id is not None]
        ids, docs, tok = self._ids, self._docs, self._tok
        text = self._text
        self._ids = [ids[s] for s in live]
        self._docs = [docs[s] for s in live]
        self._tok = [tok[s] for s in live]
        if text is not None:
            self._text = [text[s] for s in live]
        self._size = array("I", (self._size[s] for s in live))
        self._length = array("I", (self._length[s] for s in live))
        self._hash = array("q", (self._hash[s] for s in live))
        numbers = list(range(len(live)))             # one int object per slot, shared below
        self._slot = dict(zip(self._ids, numbers))
        post = [None] * len(self._post)
        for slot, tids in zip(numbers, self._tok):
            for tid in tids:
                held = post[tid]
                if held is None:
                    post[tid] = slot
                elif held.__class__ is int:
                    post[tid] = array("I", (held, slot))
                else:
                    held.append(slot)
        self._post = post
        self._exact, self._by_doc = {}, {}
        for slot in numbers:
            key = self._hash[slot]
            held = self._exact.get(key)
            if held is None:
                self._exact[key] = slot
            elif isinstance(held, list):
                held.append(slot)
            else:
                self._exact[key] = [held, slot]
            members = self._by_doc.get(self._docs[slot])
            if members is None:
                members = self._by_doc[self._docs[slot]] = array("I")
            members.append(slot)
        self._dead = set()

    # ---- reads
    def entries(self) -> dict:
        """Block id -> (document id, its distinct tokens in text order): what the index holds."""
        words, docs, tok = self._words, self._docs, self._tok
        return {block_id: (docs[slot], tuple(words[t] for t in tok[slot]))
                for block_id, slot in self._slot.items()}

    def _known(self, slot):
        """The normalized text of a slot: kept, or read from the store (`fetch`)."""
        if self._text is not None:
            return self._text[slot]
        text = self._fetch(self._ids[slot])
        return None if text is None else normalize_text(text)

    def detect(self, text: str, restate_threshold: float = 0.8,
               exclude_doc_id: str | None = None) -> tuple[str, Optional[str], Optional[str]]:
        normalized = normalize_text(text)
        tokens = set(normalized.split())
        ids, length, tok, size = self._ids, self._length, self._tok, self._size

        excluded = self._by_doc.get(exclude_doc_id, ()) if exclude_doc_id is not None else ()
        held = self._exact.get(hash(normalized))
        if held is not None:
            slots = held if isinstance(held, list) else [held]
            if excluded:
                slots = set(slots).difference(excluded)
            exact = sorted(ids[s] for s in slots
                           if length[s] == len(normalized) and self._known(s) == normalized)
            if exact:
                return "quotes", exact[0], None

        vocab, df, post = self._vocab, self._df, self._post
        query = {vocab[word] for word in tokens if word in vocab}   # the query's known tokens
        candidates: set[int] = set()
        # A Jaccard match at threshold t can omit at most floor((1-t)*|query|) query tokens.
        # Probe one more than that many least-common tokens: every qualifying candidate must share
        # at least one probe, while common prose tokens no longer turn each lookup into a full scan.
        probe_count = max(1, math.floor((1.0 - restate_threshold) * len(tokens)) + 1)
        probes = sorted(tokens, key=lambda word: (df[vocab[word]] if word in vocab else 0, word))[
            :probe_count
        ]
        for word in probes:
            tid = vocab.get(word)
            if tid is not None:
                held = post[tid]
                if held.__class__ is int:
                    candidates.add(held)
                else:
                    candidates.update(held)
        if self._dead:
            candidates.difference_update(self._dead)
        if excluded:
            candidates.difference_update(excluded)
        # A block that is a substring of the query is no longer than it, and each of its tokens but
        # the first and the last (which may be cut) is a whole token of the query. The blocks that
        # pass are tried in block id order, as the set index tried every candidate.
        query_length, most = len(normalized), len(query) + 2
        overlaps = {}                                # slot -> tokens shared with the query
        inside = []
        for slot in candidates:
            if 0 < length[slot] <= query_length and size[slot] <= most:
                overlap = overlaps[slot] = len(query.intersection(tok[slot]))
                if size[slot] - overlap <= 2:
                    inside.append(slot)
        inside.sort(key=ids.__getitem__)
        for slot in inside:
            known = self._known(slot)
            if known and known in normalized:
                return "quotes", ids[slot], None

        match = _ATTRIB.search(text)
        if match:
            return "attributes", None, _slug(match.group(1))

        # The best Jaccard score, the smallest block id among equal scores: the block the set index
        # found first in block id order, without sorting the candidates.
        best_id, best_score = None, 0.0
        query_size = len(tokens)
        for slot in candidates:
            known_size = size[slot]
            largest = max(query_size, known_size)
            # Jaccard cannot exceed the smaller set's size divided by the larger. Use the same ratio
            # comparison as before: products and rounded integer bounds differ at floating-point
            # thresholds. Quote and attribution precedence have already been evaluated.
            if largest and min(query_size, known_size) / largest < restate_threshold:
                continue
            overlap = overlaps.get(slot)
            if overlap is None:
                overlap = len(query.intersection(tok[slot]))  # a block's tokens hold no repeats
            union_size = query_size + known_size - overlap
            score = overlap / union_size if union_size else 0.0
            if score > best_score or (score == best_score and best_id is not None
                                      and ids[slot] < best_id):
                best_id, best_score = ids[slot], score
        if best_id is not None and best_score >= restate_threshold:
            return "restates", best_id, None
        return "original", None, None


def detect_echo(text: str, known_blocks: dict[str, str] | EchoIndex,
                restate_threshold: float = 0.8,
                exclude_doc_id: str | None = None) -> tuple[str, Optional[str], Optional[str]]:
    """Classify a block's derivation deterministically.

    `known_blocks`: block_id -> that block's text (the already-absorbed corpus to test echoes against).
    Returns (relation, cites_block_id, cites_node_id). Order: verbatim quote -> attribution ->
    near-duplicate restatement -> original. Never guesses across the primary; ambiguity is left for
    `independent_witness_count` to resolve to NULL.
    """
    if isinstance(known_blocks, EchoIndex):
        return known_blocks.detect(text, restate_threshold, exclude_doc_id)
    index = EchoIndex()
    for block_id, known_text in known_blocks.items():
        index.add(block_id, known_text)
    return index.detect(text, restate_threshold)


# --------------------------------------------------------------------------- independence walk

def _originals_for_node(db, node_id: str) -> list[str]:
    rows = db.conn.execute(
        "SELECT block_id FROM block_provenance WHERE relation='original' AND cites_node_id=? "
        "ORDER BY block_id", (node_id,),
    ).fetchall()
    return [r["block_id"] for r in rows]


def _prov_row(db, block_id: str) -> Optional[dict]:
    r = db.conn.execute(
        "SELECT block_id, cites_block_id, cites_node_id, relation FROM block_provenance "
        "WHERE block_id=? ORDER BY relation, cites_block_id, cites_node_id LIMIT 1", (block_id,),
    ).fetchone()
    return dict(r) if r else None


def _trace_root(db, block_id: str, visited: set) -> Optional[str]:
    """Follow a block's cites chain to a fixpoint primary root. NULL on cycle / unresolvable attrib."""
    if block_id in visited:
        return None                                   # cycle -> unprovable
    visited.add(block_id)
    row = _prov_row(db, block_id)
    if row is None or row["relation"] == "original":
        return block_id                               # a root witness
    rel = row["relation"]
    if rel in ("quotes", "restates"):
        nxt = row["cites_block_id"]
        return _trace_root(db, nxt, visited) if nxt else block_id
    if rel == "attributes":
        node = row["cites_node_id"]
        if not node:
            return None
        originals = _originals_for_node(db, node)
        if len(originals) != 1:
            return None                               # 0 or >1 primaries -> unresolvable attribution
        return _trace_root(db, originals[0], visited)
    return None


def _corroborating_blocks(db, edge_id: str) -> list[str]:
    rows = db.conn.execute(
        "SELECT source_block_id FROM edge_corroborations WHERE edge_id=? ORDER BY source_block_id",
        (edge_id,),
    ).fetchall()
    blocks = [r["source_block_id"] for r in rows]
    if not blocks:
        r = db.conn.execute("SELECT source_block_id FROM edges WHERE edge_id=?", (edge_id,)).fetchone()
        if r and r["source_block_id"]:
            blocks = [r["source_block_id"]]
    return blocks


def independent_witness_count(db, edge_id: str) -> Optional[int]:
    """Collapse echo chains onto primaries; return the count of DISTINCT independent roots, or NULL.

    Returns an int ONLY when every corroborating source resolves to a provable primary root; any
    unresolvable attribution or cycle yields a HARD NULL (flagged-uncomputed) - never a fabricated
    integer, and never the raw (echo-inflated) corroboration_count.
    """
    roots: set[str] = set()
    for b in _corroborating_blocks(db, edge_id):
        root = _trace_root(db, b, set())
        if root is None:
            return None
        roots.add(root)
    return len(roots)


def persist_independent_witness_count(db, edge_id: str) -> Optional[int]:
    """Compute and store the count on edges.independent_witness_count. Returns the value (int|None)."""
    ensure_schema(db)
    val = independent_witness_count(db, edge_id)
    db.conn.execute("UPDATE edges SET independent_witness_count=? WHERE edge_id=?", (val, edge_id))
    db.commit()
    return val


# --------------------------------------------------------------------------- arbitration hook

def corroboration_for_arbitration(db, edge: dict) -> Optional[int]:
    """The value `arbitrate_conflict` must use instead of raw corroboration_count.

    Prefer the persisted independent_witness_count; recompute if absent. Returns NULL when
    independence is unprovable - the caller MUST then DROP the corroboration term entirely (letting
    authority + recency order), never fall back to the inflated raw count.
    """
    if edge.get("independent_witness_count") is not None:
        return int(edge["independent_witness_count"])
    eid = edge.get("edge_id")
    if not eid:
        return None
    return independent_witness_count(db, eid)
