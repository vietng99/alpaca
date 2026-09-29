"""Indexes follow source and provider changes without changing evidence history."""
from dataclasses import replace
from unittest.mock import patch

import pytest

from alpaca.wiki.config import Config
from alpaca.wiki.ingest.absorb import Absorber
from alpaca.wiki.engine.loop import Engine
from alpaca.wiki.store import vec

TEXT = '[[Ada]] works at [[Acme]].'


def config(tmp_path, profile='full', **meta):
    cfg = Config.for_vault(tmp_path)
    return replace(cfg, meta={**cfg.meta, 'retrieval_profile': profile, **meta})


def vector_ids(db, kind='blocks'):
    table = f'vec_{kind}_fallback'
    if not db.conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
        return set()
    key = 'block_id' if kind == 'blocks' else 'node_id'
    return {r[0] for r in db.conn.execute(f'SELECT {key} FROM {table}')}


@pytest.mark.parametrize('profile', ['full', 'hybrid'])
def test_enabled_before_ingest_populates_both_indexes(tmp_path, profile):
    a = Absorber(config(tmp_path, profile))
    try:
        a.absorb_text('raw/a.md', TEXT)
        active = {r[0] for r in a.db.conn.execute("SELECT block_id FROM blocks WHERE status='active'")}
        assert vector_ids(a.db) == active
        assert vector_ids(a.db, 'nodes') == {'ada', 'acme'}
        assert vec.knn_blocks(a.db, a.providers.embedder.embed('Ada'), 10)
    finally:
        a.close()


def test_narrow_to_full_backfills_without_new_source_events(tmp_path):
    cfg = config(tmp_path, 'narrow')
    a = Absorber(cfg)
    a.absorb_text('raw/a.md', TEXT)
    assert not vector_ids(a.db)
    count = a.db.conn.execute('SELECT COUNT(*) FROM ingest_event').fetchone()[0]
    a.close()
    e = Engine(config(tmp_path))
    try:
        assert vector_ids(e.db)
        assert vector_ids(e.db, 'nodes')
        assert e.db.conn.execute('SELECT COUNT(*) FROM ingest_event').fetchone()[0] == count
    finally:
        e.close()


@pytest.mark.parametrize('meta', [{'embed_dim': '32'}, {'embed_version': '2'}, {'embed_model': 'new-model'}])
def test_provider_changes_reembed_unchanged_sources(tmp_path, meta):
    a = Absorber(config(tmp_path))
    a.absorb_text('raw/a.md', TEXT)
    a.close()
    cfg = config(tmp_path, **meta)
    providers = cfg.providers()
    with patch.object(providers.embedder, 'embed', wraps=providers.embedder.embed) as embed:
        with patch.object(Config, 'providers', return_value=providers):
            e = Engine(cfg)
        try:
            assert embed.call_count >= 3
            sizes = {len(r[0]) for r in e.db.conn.execute('SELECT embedding FROM vec_blocks_fallback')}
            assert sizes == {int(cfg.meta['embed_dim']) * 8}
            assert e.db.get_meta('embed_version') == cfg.meta['embed_version']
        finally:
            e.close()


def test_retirement_and_replacement_remove_vectors(tmp_path):
    a = Absorber(config(tmp_path))
    try:
        a.absorb_text('raw/a.md', TEXT)
        old = vector_ids(a.db)
        a.absorb_text('raw/a.md', '[[Grace]] works at [[Beta]].')
        assert old.isdisjoint(vector_ids(a.db))
        assert vector_ids(a.db, 'nodes') == {'grace', 'beta'}
        a.retire_documents(['raw/a.md'])
        assert not vector_ids(a.db)
        assert not vector_ids(a.db, 'nodes')
    finally:
        a.close()


def test_retier_after_defers_then_updates_even_for_unchanged_text(tmp_path):
    a = Absorber(config(tmp_path, 'narrow'))
    try:
        a.absorb_text('raw/a.md', TEXT, retier_after=False)
        assert all(r[0] is None for r in a.db.conn.execute('SELECT page_band FROM nodes'))
        a.absorb_text('raw/a.md', TEXT, retier_after=True)
        assert all(r[0] is not None for r in a.db.conn.execute('SELECT page_band FROM nodes'))
    finally:
        a.close()


def test_embedding_failure_rolls_back_source_and_ledger(tmp_path):
    a = Absorber(config(tmp_path))
    try:
        before = a.cfg.ledger_path.read_bytes() if a.cfg.ledger_path.exists() else b''
        with patch.object(a.providers.embedder, 'embed', side_effect=ValueError('provider failed')):
            with pytest.raises(ValueError, match='provider failed'):
                a.absorb_text('raw/a.md', TEXT)
        assert a.db.conn.execute('SELECT COUNT(*) FROM docs').fetchone()[0] == 0
        assert a.db.conn.execute('SELECT COUNT(*) FROM ingest_event').fetchone()[0] == 0
        assert (a.cfg.ledger_path.read_bytes() if a.cfg.ledger_path.exists() else b'') == before
    finally:
        a.close()


def test_retired_projection_stays_retired_on_vault_replay(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    (raw / 'old.md').write_text(TEXT)
    (raw / 'new.md').write_text('[[Grace]] works at [[Beta]].')
    a = Absorber(config(tmp_path))
    try:
        a.absorb_vault()
        a.retire_documents(['raw/old.md'], superseded_by='raw/new.md')
        a.absorb_vault()
        assert not a.db.conn.execute("SELECT 1 FROM blocks WHERE doc_id='raw/old.md' AND status='active'").fetchone()
        assert (raw / 'old.md').read_text() == TEXT
        a.absorb_text('raw/old.md', TEXT)
        assert a.db.conn.execute("SELECT 1 FROM blocks WHERE doc_id='raw/old.md' AND status='active'").fetchone()
    finally:
        a.close()


def test_unchanged_inputs_are_not_reembedded(tmp_path):
    a = Absorber(config(tmp_path))
    try:
        a.absorb_text('raw/a.md', TEXT)
        with patch.object(a.providers.embedder, 'embed', wraps=a.providers.embedder.embed) as embed:
            a.absorb_text('raw/a.md', TEXT)
            assert embed.call_count == 0
            a.absorb_text('raw/b.md', 'PostgreSQL is a database.')
            assert embed.call_count == 1
    finally:
        a.close()


def test_deleted_source_is_removed_from_indexes(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    page = raw / 'a.md'
    page.write_text(TEXT)
    a = Absorber(config(tmp_path))
    try:
        a.absorb_vault()
        assert vector_ids(a.db)
        assert a.db.conn.execute('SELECT COUNT(*) FROM tier_events').fetchone()[0] > 0
        page.unlink()
        a.absorb_vault()
        assert not vector_ids(a.db)
        assert not vector_ids(a.db, 'nodes')
    finally:
        a.close()


def test_failed_rebuild_preserves_previous_vectors_and_signature(tmp_path):
    a = Absorber(config(tmp_path))
    a.absorb_text('raw/a.md', TEXT)
    signature = a.db.get_meta('vector_cache_signature')
    vectors = [tuple(r) for r in a.db.conn.execute('SELECT * FROM vec_blocks_fallback')]
    a.close()
    cfg = config(tmp_path, embed_dim='32')
    providers = cfg.providers()
    with patch.object(providers.embedder, 'embed', side_effect=ValueError('provider failed')):
        with patch.object(Config, 'providers', return_value=providers):
            with pytest.raises(ValueError, match='provider failed'):
                Engine(cfg)
    a = Absorber(config(tmp_path))
    try:
        assert a.db.get_meta('vector_cache_signature') == signature
        assert a.db.get_meta('embed_dim') == '64'
        assert [tuple(r) for r in a.db.conn.execute('SELECT * FROM vec_blocks_fallback')] == vectors
    finally:
        a.close()


def test_shared_vector_codec_preserves_dimension_and_rejects_nonfinite():
    from alpaca.wiki.store import vector_codec
    values = [0.5, -0.25, 0.0]
    assert vector_codec._unpack(vector_codec._pack(values, 3), 3) == values
    with pytest.raises(vector_codec.VectorValidationError):
        vector_codec._pack([float('nan')], 1)
    with pytest.raises(vector_codec.VectorValidationError):
        vector_codec._unpack(vector_codec._pack(values), 2)


def test_shared_tier_policy_keeps_deterministic_ties_and_supersession():
    from alpaca.wiki.store import tier_policy
    result = tier_policy.compute_tiers(
        ['a', 'b', 'c'], {'c': 1.0, 'a': 1.0, 'b': 1.0},
        {'a': 'superseded', 'b': 'active', 'c': 'active'}, {}, set(),
    )
    assert result['order'] == ['a', 'b', 'c']
    assert result['band']['a'] == tier_policy.TOP_BAND - 1
    assert result['demoted'] == {'a'}


def test_embedding_failure_restores_in_memory_echo_index(tmp_path):
    a = Absorber(config(tmp_path))
    try:
        with patch.object(a.providers.embedder, 'embed', side_effect=ValueError('provider failed')):
            with pytest.raises(ValueError):
                a.absorb_text('raw/a.md', TEXT)
        assert a.echo_index.detect(TEXT) == ('original', None, None)
    finally:
        a.close()
