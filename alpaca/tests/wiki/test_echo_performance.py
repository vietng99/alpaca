"""Large transcript comparisons retain verdicts without corpus-sized allocations."""
from alpaca.wiki.engine.echo import EchoIndex


def test_short_query_does_not_materialize_large_candidate_unions():
    index = EchoIndex()
    query = ' '.join('q%d' % i for i in range(100))
    for i in range(20):
        index.add(str(i), query + ' ' + ' '.join('extra%d-%d' % (i, j) for j in range(2000)))
    class LargeTokens(set):
        def __ror__(self, other):
            raise AssertionError('large candidate union allocated for a provably dissimilar transcript')
    index._tokens = {key:LargeTokens(value) for key, value in index._tokens.items()}
    assert index.detect(query) == ('original', None, None)


def test_size_bounds_keep_quotes_attribution_threshold_matches_and_ties():
    index = EchoIndex()
    # Quote lookup still runs before near-duplicate size bounds.
    quote = ' '.join('word%d' % i for i in range(30))
    index.add('quote', quote)
    assert index.detect(quote+' additional') == ('quotes', 'quote', None)
    assert index.detect('According to [[Alice]] the plan changed') == ('attributes', None, 'alice')
    index = EchoIndex()
    index.add('b', 'a b c d extra', 'second')
    index.add('a', 'a b c d extra', 'first')
    assert index.detect('d c b a', 0.8) == ('restates', 'a', None)
    assert index.detect('d c b a', 0.8, 'first') == ('restates', 'b', None)
    assert index.detect('d c b a', 0.81) == ('original', None, None)
    index.add('exact', 'd c b a')
    assert index.detect('d c b a') == ('quotes', 'exact', None)
    index.remove('exact')
    assert index.detect('d c b a') == ('restates', 'a', None)


def test_current_document_candidates_do_not_require_individual_metadata_scans():
    index = EchoIndex()
    query = 'common query marker token'
    for i in range(2000):
        index.add('self-%d' % i, query+' extra-%d' % i, 'current')
    class DocumentLookup(dict):
        def get(self, *args):
            raise AssertionError('per-block document lookup during current-document exclusion')
    index._doc = DocumentLookup(index._doc)
    assert index.detect(query, exclude_doc_id='current') == ('original', None, None)
    index.add('external', 'token marker query common extra', 'other')
    assert index.detect(query, exclude_doc_id='current') == ('restates', 'external', None)
    # Moving an existing block must remove its old document membership.
    index.add('external', 'token marker query common extra', 'current')
    assert index.detect(query, exclude_doc_id='current') == ('original', None, None)
    index.add('external', 'token marker query common extra', 'other')
    index.remove('external')
    assert index.detect(query, exclude_doc_id='current') == ('original', None, None)


def test_large_dissimilar_corpus_skips_individual_token_reads():
    index = EchoIndex()
    query = ' '.join('q%d' % i for i in range(30))
    extra = ' '.join('extra%d' % i for i in range(100))
    for i in range(3000):
        index.add('large-%d' % i, query+' '+extra+' marker-%d' % i)
    class Tokens(dict):
        def __getitem__(self, key):
            if key.startswith('large-'):
                raise AssertionError('provably dissimilar candidate token set was read')
            return super().__getitem__(key)
    index._tokens = Tokens(index._tokens)
    assert index.detect(query) == ('original', None, None)
    index.add('match', query+' extra', 'other')
    assert index.detect(query) == ('restates', 'match', None)
    index.add('match', 'different content', 'other')
    assert index.detect(query) == ('original', None, None)
    index.remove('match')
    assert index.detect(query) == ('original', None, None)


def test_bulk_load_builds_the_same_index_as_add():
    # Merge of op-008 into main (2026-10-06): op-008 loads the absorber's index with the bulk
    # `EchoIndex.load`; it must fill the by-document and by-size maps main's `detect` reads, or a
    # document would echo its own blocks and a large comparison would see no candidates.
    rows = [('b%d' % i, ' '.join('w%d' % j for j in range(i % 7 + 3)) + ' doc%d' % (i % 3), 'd%d' % (i % 3))
            for i in range(40)] + [('none', 'a block with no document', None)]
    added, loaded = EchoIndex(), EchoIndex()
    for block_id, text, doc_id in rows:
        added.add(block_id, text, doc_id)
    loaded.load(rows)
    for name in ('_doc', '_by_doc', '_by_size', '_norm', '_tokens', '_exact', '_postings'):
        assert dict(getattr(loaded, name)) == dict(getattr(added, name)), name
    text = rows[5][1]
    assert loaded.detect(text, 0.8, rows[5][2]) == added.detect(text, 0.8, rows[5][2])
    loaded.remove('b5'); added.remove('b5')
    assert dict(loaded._by_doc) == dict(added._by_doc) and dict(loaded._by_size) == dict(added._by_size)
