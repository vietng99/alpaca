"""RESOLVE (§4.5, ADR-029 set-returning cannot-merge) + advisory near-match surfacing.

Exact/declared-alias match returns a SET of node_ids: |set|==1 -> link, ==0 -> mint, >1 -> keep
distinct (the two-Dats invariant - two people, one first name, stay separate). Near matches
(same normalized surface across DIFFERENT ids, or vec cosine) are written as advisory
merge_candidate rows, SURFACED to review-by-exception, NEVER auto-bound (R5).

The name-alike guard (FR-15, HS-EXT-07, dec-804f2708474c Q-F) decides which near matches may be
surfaced at all: `name_verdict` returns "same" (equal normalized surface), "review" (the names are
similar and agree word by word and number by number) or "distinct". An exact identifier (a token
holding a digit, such as TR-9 or 4471) is never "review": it is "same" or "distinct". The guard
helpers are adapted from the Hindsight entity resolver (see THIRD-PARTY-NOTICES.md); the database
resolver and spaCy are not ported. No path here calls a model.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from functools import lru_cache

from ..store.db import DB
from ..store.textnorm import norm_surface
from ..store.write import Writer


# ---- name-alike guard (adapted: Hindsight hindsight-api-slim entity_resolver.py at ccfe85b4, MIT) ----

# A trigram "word" is a maximal run of letters and digits (underscore excluded); everything else is
# a separator, so "Wren <emoji>" and "wren" share one trigram set.
_TRGM_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# Trigram similarity from which two names are surfaced for review. Calibrated on name-alike pairs
# that share one word (a first name 0.29, a trailing noun 0.28, a leading noun 0.23; the synthetic
# pairs of alpaca/tests/wiki/test_entity_guards.py score 0.22 to 0.29) against spelling variants
# (Harbor / Harbour ledger 0.71).
MIN_NAME_SIMILARITY = 0.5

# Sequence ratio from which two words count as the same word ("John" / "Jane" is 0.50 and fails).
_MIN_TOKEN_SIMILARITY = 0.6

_DIGIT_RUN = re.compile(r"\d+(?:\.\d+)*")


def _trigram_set(text: str) -> set[str]:
    """pg_trgm trigrams: lowercase, split into words, pad each with two leading and one trailing
    blank, take every 3-char window."""
    trigrams: set[str] = set()
    for word in _TRGM_WORD.findall(text.lower()):
        padded = "  %s " % word
        for i in range(len(padded) - 2):
            trigrams.add(padded[i:i + 3])
    return trigrams


def trigram_similarity(a: str, b: str) -> float:
    """Jaccard index of the two trigram sets (0.0 when both are empty)."""
    ta, tb = _trigram_set(a), _trigram_set(b)
    both = len(ta & tb)
    union = len(ta) + len(tb) - both
    return both / union if union else 0.0


@lru_cache(maxsize=4096)
def _tokens_match(a: str, b: str) -> bool:
    """Two words are plausibly the same: equal, one a prefix of the other, or a near miss."""
    return (a == b or a.startswith(b) or b.startswith(a)
            or SequenceMatcher(None, a, b).ratio() >= _MIN_TOKEN_SIMILARITY)


@lru_cache(maxsize=4096)
def _numbers_in(name: str) -> frozenset:
    """The numbers in a name ("UA0123" gives {"123"}, "4.0" gives {"4"}, "3.10" stays apart from "3.1")."""
    numbers = []
    for run in _DIGIT_RUN.findall(name):
        parts = run.split(".")
        parts[0] = parts[0].lstrip("0") or "0"
        if len(parts) == 2 and not parts[1].strip("0"):
            parts.pop()
        numbers.append(".".join(parts))
    return frozenset(numbers)


def _tokens_are_compatible(a: str, b: str) -> bool:
    """Whether two names agree word by word. Each name has a number the other lacks: two things
    ("Room 101" / "Room 102"). Otherwise every word of the shorter name needs a counterpart in the
    longer one, so one long shared word cannot hide a different short one ("John Smith" / "Jane
    Smith"). Single-word names are exempt from the word rule, not from the number rule."""
    numbers_a, numbers_b = _numbers_in(a), _numbers_in(b)
    if numbers_a - numbers_b and numbers_b - numbers_a:
        return False
    ta, tb = _TRGM_WORD.findall(a.lower()), _TRGM_WORD.findall(b.lower())
    if len(ta) < 2 and len(tb) < 2:
        return True
    short, rest = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return all(any(_tokens_match(w, o) for o in rest) for w in short)


def is_identifier(surface: str) -> bool:
    """An exact identifier: one token (no blank) that holds a digit, such as TR-9, t-066, 4471 or
    v1.2. It names one specific thing, so it only ever equals itself."""
    s = surface.strip()
    return bool(s) and not re.search(r"\s", s) and any(c.isdigit() for c in s)


def name_verdict(a: str, b: str) -> str:
    """"same", "review" or "distinct" for two entity names. Never "merge": a "review" pair is surfaced
    to a person and bound by no code path. Decided by the strings alone."""
    na, nb = norm_surface(a), norm_surface(b)
    if na == nb:
        return "same"
    if is_identifier(a) or is_identifier(b):
        return "distinct"
    if trigram_similarity(a, b) < MIN_NAME_SIMILARITY:
        return "distinct"
    return "review" if _tokens_are_compatible(a, b) else "distinct"


def resolve_surface(db: DB, surface: str) -> list[str]:
    """Return the SET of bound node_ids an exact/declared alias surface maps to (canonical)."""
    ns = norm_surface(surface)
    rows = db.conn.execute(
        "SELECT DISTINCT node_id FROM aliases WHERE norm_surface=? AND status='bound'", (ns,)
    ).fetchall()
    out = []
    for r in rows:
        cr = db.conn.execute(
            "SELECT canonical_node_id FROM nodes WHERE node_id=?", (r["node_id"],)
        ).fetchone()
        out.append(cr["canonical_node_id"] if cr and cr["canonical_node_id"] else r["node_id"])
    return sorted(set(out))


def ensure_node(db: DB, writer: Writer, node_id: str, display: str, block_id: str) -> str:
    """Mint-or-link the node for a wikilink target (the slug is its stable identity), attach the
    mention, and surface - never bind - any distinct node sharing this normalized surface."""
    existing = db.conn.execute("SELECT node_id, canonical_node_id FROM nodes WHERE node_id=?",
                               (node_id,)).fetchone()
    if existing is None:
        writer.upsert_node(node_id, display_name=display)
        writer.add_alias(node_id, display, norm_surface(display), kind="wikilink",
                         status="bound", source_block_id=block_id)
        canonical = node_id
    else:
        canonical = existing["canonical_node_id"] or existing["node_id"]
    writer.add_node_block(canonical, block_id, role="mention")

    # advisory near-match: a DIFFERENT existing node with the same normalized surface
    ns = norm_surface(display)
    dupes = db.conn.execute(
        "SELECT DISTINCT a.node_id FROM aliases a WHERE a.norm_surface=? AND a.node_id<>?",
        (ns, canonical),
    ).fetchall()
    for d in dupes:
        _surface_once(db, writer, canonical, d["node_id"])
    surfaced = {d["node_id"] for d in dupes}
    for other, surface in _name_alike_neighbours(db, canonical, display):
        if other not in surfaced and name_verdict(display, surface) == "review":
            _surface_once(db, writer, canonical, other, method="name-guard",
                          score=trigram_similarity(display, surface))
    return canonical


def _name_alike_neighbours(db: DB, canonical: str, display: str, limit: int = 60) -> list:
    """(node_id, surface) of other nodes whose bound alias shares a word with `display`: the
    bounded pool the guard looks at, so one new node costs a few rows, not a scan of the store."""
    seen, out = set(), []
    for word in sorted(set(_TRGM_WORD.findall(display.lower())), key=len, reverse=True)[:3]:
        if len(word) < 3:
            continue
        for r in db.conn.execute(
                "SELECT node_id, surface FROM aliases WHERE status='bound' AND node_id<>? "
                "AND (norm_surface LIKE ? OR norm_surface LIKE ? OR norm_surface LIKE ? OR norm_surface=?) LIMIT ?",
                (canonical, word + " %", "% " + word + " %", "% " + word, word, limit)):
            if r["node_id"] not in seen:
                seen.add(r["node_id"])
                out.append((r["node_id"], r["surface"]))
    return out


def _surface_once(db: DB, writer: Writer, a: str, b: str, method: str = "norm-surface",
                  score: float = 1.0) -> None:
    lo, hi = sorted((a, b))
    exists = db.conn.execute(
        "SELECT 1 FROM merge_candidate WHERE node_a=? AND node_b=?", (lo, hi)
    ).fetchone()
    if not exists:
        writer.surface_merge_candidate(lo, hi, method=method, score=score)
