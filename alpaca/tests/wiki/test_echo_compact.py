"""t-685: the compact echo index gives every verdict the set-based index gave, and holds far less.

`_SetIndex` is the index as it was before t-685 (alpaca/wiki/engine/echo.py at 3fd28a0a0), kept here as
the reference the compact one is checked against."""
import math
import random
import sys
from collections import defaultdict
from typing import Optional

import pytest

from alpaca.wiki.determinism import normalize_text
from alpaca.wiki.engine.echo import EchoIndex, _ATTRIB, _slug


def _token_tuple(normalized: str) -> tuple[str, ...]:
    """The distinct tokens of a normalized text: the members of `set(normalized.split())`, held as a
    tuple of interned strings. An index of every active block holds tens of millions of token entries
    over a vocabulary of about two million (the live store, 2026-10-06): one shared string per token
    and a tuple per block instead of a set take about 3 GB less, and `detect` reads only the members
    and their count, so every verdict is the one the set gave."""
    return tuple(dict.fromkeys(map(sys.intern, normalized.split())))


class _SetIndex:
    """Incremental normalized/token index for deterministic echo detection."""

    def __init__(self):
        self._doc: dict[str, str | None] = {}
        self._by_doc: dict[str | None, set[str]] = defaultdict(set)
        self._by_size: dict[int, set[str]] = defaultdict(set)
        self._norm: dict[str, str] = {}
        self._tokens: dict[str, tuple[str, ...]] = {}     # distinct tokens (`_token_tuple`)
        self._exact: dict[str, set[str]] = defaultdict(set)
        self._postings: dict[str, set[str]] = defaultdict(set)

    def add(self, block_id: str, text: str, doc_id: str | None = None) -> None:
        self.remove(block_id)
        normalized = normalize_text(text)
        tokens = _token_tuple(normalized)
        self._doc[block_id] = doc_id
        self._by_doc[doc_id].add(block_id)
        self._norm[block_id] = normalized
        self._tokens[block_id] = tokens
        self._by_size[len(tokens)].add(block_id)
        self._exact[normalized].add(block_id)
        for token in tokens:
            self._postings[token].add(block_id)

    def load(self, rows) -> None:
        """Bulk `add` of `(block_id, text, doc_id)` rows into an empty index: the same index `add`
        builds, without the per-block remove and method calls (the load of a whole store). It fills
        the by-document and by-size maps `add` fills (main a24aef856, b70a1022f), which `detect`
        reads to exclude a document's own blocks and to bound large comparisons."""
        docs, norms, tokens_of = self._doc, self._norm, self._tokens
        by_doc, by_size = self._by_doc, self._by_size
        exact, postings = self._exact, self._postings
        for block_id, text, doc_id in rows:
            normalized = normalize_text(text)
            tokens = _token_tuple(normalized)
            if doc_id is not None:
                doc_id = sys.intern(doc_id)              # one string per document, not per block
            docs[block_id] = doc_id
            by_doc[doc_id].add(block_id)
            norms[block_id] = normalized
            tokens_of[block_id] = tokens
            by_size[len(tokens)].add(block_id)
            exact[normalized].add(block_id)
            for token in tokens:
                postings[token].add(block_id)

    def remove(self, block_id: str) -> None:
        normalized = self._norm.pop(block_id, None)
        tokens = self._tokens.pop(block_id, ())
        sized = self._by_size.get(len(tokens))
        if sized is not None:
            sized.discard(block_id)
            if not sized:
                self._by_size.pop(len(tokens), None)
        doc_id = self._doc.pop(block_id, None)
        members = self._by_doc.get(doc_id)
        if members is not None:
            members.discard(block_id)
            if not members:
                self._by_doc.pop(doc_id, None)
        if normalized is not None:
            ids = self._exact.get(normalized)
            if ids is not None:
                ids.discard(block_id)
                if not ids:
                    self._exact.pop(normalized, None)
        for token in tokens:
            ids = self._postings.get(token)
            if ids is not None:
                ids.discard(block_id)
                if not ids:
                    self._postings.pop(token, None)

    def detect(self, text: str, restate_threshold: float = 0.8,
               exclude_doc_id: str | None = None) -> tuple[str, Optional[str], Optional[str]]:
        normalized = normalize_text(text)
        tokens = set(normalized.split())

        excluded = self._by_doc.get(exclude_doc_id, ()) if exclude_doc_id is not None else ()
        exact_ids = self._exact.get(normalized, ())
        exact = sorted(set(exact_ids).difference(excluded)) if excluded else sorted(exact_ids)
        if exact:
            return "quotes", exact[0], None

        candidates: set[str] = set()
        # A Jaccard match at threshold t can omit at most floor((1-t)*|query|) query tokens.
        # Probe one more than that many least-common tokens: every qualifying candidate must share
        # at least one probe, while common prose tokens no longer turn each lookup into a full scan.
        probe_count = max(1, math.floor((1.0 - restate_threshold) * len(tokens)) + 1)
        probes = sorted(tokens, key=lambda token: (len(self._postings.get(token, ())), token))[
            :probe_count
        ]
        for token in probes:
            candidates.update(self._postings.get(token, ()))
        if excluded:
            candidates.difference_update(excluded)
        ordered = sorted(candidates)
        for block_id in ordered:
            known = self._norm[block_id]
            if known and known in normalized:
                return "quotes", block_id, None

        match = _ATTRIB.search(text)
        if match:
            return "attributes", None, _slug(match.group(1))

        best_id, best_score = None, 0.0
        query_size = len(tokens)
        if len(candidates) > 512:
            eligible = set()
            for known_size, members in self._by_size.items():
                largest = max(query_size, known_size)
                # Use the same ratio comparison as the per-block bound. Products
                # and rounded integer bounds differ at floating-point thresholds.
                if not largest or min(query_size, known_size) / largest >= restate_threshold:
                    eligible.update(candidates.intersection(members))
            ordered = sorted(eligible)
        for block_id in ordered:
            known_tokens = self._tokens[block_id]
            known_size = len(known_tokens)
            largest = max(query_size, known_size)
            # Jaccard cannot exceed the smaller set's size divided by the larger.
            # Quote and attribution precedence have already been evaluated.
            if largest and min(query_size, known_size) / largest < restate_threshold:
                continue
            overlap = len(tokens.intersection(known_tokens))   # known_tokens holds no repeats
            union_size = query_size + known_size - overlap
            score = overlap / union_size if union_size else 0.0
            if score > best_score:
                best_id, best_score = block_id, score
        if best_id is not None and best_score >= restate_threshold:
            return "restates", best_id, None
        return "original", None, None



WORDS = ['alpha', 'beta', 'gamma', 'delta', 'eps', 'zeta', 'eta', 'theta', 'iota', 'kappa', 'lam',
         'mu', 'nu', 'xi', 'pi', 'rho', 'As', '[[Ada]]', 'noted', 'theo', '\n', '  ', 'b\u0301']


def _text(rng, low=1, high=14):
    return ' '.join(rng.choice(WORDS) for _ in range(rng.randint(low, high)))


def _queries(rng, texts):
    """Queries that hit every branch: exact copies, texts holding a known text, cut and padded
    copies, near duplicates, attributions and random ones."""
    out = []
    for _ in range(60):
        base = rng.choice(texts) if texts else _text(rng)
        kind = rng.randrange(6)
        if kind == 0:
            out.append(base)
        elif kind == 1:
            out.append(_text(rng, 0, 4) + ' ' + base + ' ' + _text(rng, 0, 4))
        elif kind == 2:
            out.append(base[rng.randrange(max(1, len(base))):] + ' ' + _text(rng, 0, 3))
        elif kind == 3:
            words = base.split()
            if words:
                words[rng.randrange(len(words))] = rng.choice(WORDS)
            out.append(' '.join(words))
        elif kind == 4:
            out.append('As [[' + rng.choice(WORDS) + ']] noted ' + base)
        else:
            out.append(_text(rng))
    return out


@pytest.mark.parametrize('stored', [False, True], ids=['texts-kept', 'texts-from-store'])
@pytest.mark.parametrize('seed', range(12))
def test_the_compact_index_gives_every_verdict_the_set_index_gave(seed, stored):
    rng = random.Random(seed)
    store = {}
    reference = _SetIndex()
    compact = EchoIndex(fetch=store.get) if stored else EchoIndex()
    rows = [('b%03d' % i, _text(rng), 'd%d' % rng.randrange(6)) for i in range(rng.randint(5, 120))]
    store.update((block_id, text) for block_id, text, _ in rows)
    reference.load(rows)
    if seed % 2:
        compact.load(rows)
    else:
        for row in rows:
            compact.add(*row)
    live = {block_id: text for block_id, text, _ in rows}
    for round_ in range(6):
        for query in _queries(rng, list(live.values())):
            for threshold in (0.8, 0.5, 1.0):
                for exclude in (None, 'd%d' % rng.randrange(6), 'nowhere'):
                    assert compact.detect(query, threshold, exclude) == \
                        reference.detect(query, threshold, exclude), (seed, round_, query, threshold, exclude)
        # removals, moves to another document and new blocks between the rounds
        for block_id in rng.sample(sorted(live), k=min(len(live), rng.randint(0, 30))):
            reference.remove(block_id)
            compact.remove(block_id)
            del live[block_id]
        for _ in range(rng.randint(0, 25)):
            block_id = 'n%d-%d' % (round_, rng.randrange(1000))
            text, doc = _text(rng), 'd%d' % rng.randrange(6)
            store[block_id] = text
            reference.add(block_id, text, doc)
            compact.add(block_id, text, doc)
            live[block_id] = text
        assert len(compact) == len(live)


def test_removed_blocks_leave_no_candidates_and_compaction_keeps_the_verdicts():
    rng = random.Random(7)
    reference, compact = _SetIndex(), EchoIndex()
    rows = [('b%05d' % i, _text(rng, 3, 20), 'd%d' % (i % 50)) for i in range(12000)]
    reference.load(rows)
    compact.load(rows)
    for block_id, _, _ in rows[:9000]:
        reference.remove(block_id)
        compact.remove(block_id)
    assert len(compact) == 3000 and len(compact._ids) < 12000      # compacted on the way
    assert len(compact._dead) <= max(4096, len(compact) // 4)
    for query in _queries(rng, [text for _, text, _ in rows]):
        assert compact.detect(query) == reference.detect(query), query
    for block_id, _, _ in rows[9000:]:
        compact.remove(block_id)
    assert len(compact) == 0 and not compact._vocab and not compact._exact
    assert compact.detect(rows[-1][1]) == ('original', None, None)


def test_a_token_no_block_holds_any_more_is_forgotten_and_its_id_used_again():
    index = EchoIndex()
    index.add('a', 'rare common', 'd')
    index.add('b', 'common other', 'd')
    rare = index._vocab['rare']
    index.remove('a')
    assert 'rare' not in index._vocab and index._post[rare] is None
    index.add('c', 'fresh common', 'd')
    assert index._vocab['fresh'] == rare and index._post[rare] == index._slot['c']
    assert index.detect('fresh common') == ('quotes', 'c', None)


def test_the_store_text_is_read_only_for_a_block_that_can_be_inside_the_query():
    reads = []
    texts = {'short': 'alpha beta', 'long': 'alpha beta gamma delta eps zeta eta theta',
             'other': 'alpha mu nu xi'}

    def fetch(block_id):
        reads.append(block_id)
        return texts[block_id]
    index = EchoIndex(fetch=fetch)
    for block_id, text in texts.items():
        index.add(block_id, text, 'doc-' + block_id)
    assert index._text is None
    # the probe 'beta' finds 'short' and 'long'; 'long' is longer than the query, so it is not read
    assert index.detect('beta alpha beta') == ('quotes', 'short', None)
    assert reads == ['short']
    reads.clear()
    # the probe 'mu' finds 'other', longer than the query: nothing is read
    assert index.detect('alpha beta mu') == ('original', None, None)
    assert reads == []
    reads.clear()
    assert index.detect('alpha beta') == ('quotes', 'short', None)  # the exact match, checked once
    assert reads == ['short']
