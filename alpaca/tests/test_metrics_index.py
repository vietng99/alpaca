"""Project and crew totals never count client snapshots as additional charges."""
import json
from pathlib import Path

import pytest

from alpaca import db, transcripts
from alpaca.analytics import metrics_index


def register(root, sid):
    conn = db.connect(root)
    db.upsert(conn, 'sessions', 'sid', {'sid': sid, 'started': '2026-09-22T00:00:00Z'})
    db.meta_set(conn, 'operator:' + sid, 'claude')
    conn.close()


def write(path, ident, cost):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'type': 'assistant', 'uuid': ident,
        'timestamp': '2026-09-22T00:00:00Z', 'message': {'id': ident, 'model': 'claude-opus-5',
        'content': [{'type': 'text', 'text': 'Completed a check'}],
        'usage': {'input_tokens': 100, 'output_tokens': 10, 'cache_read_input_tokens': 0,
                  'cache_creation_input_tokens': 0}}}) + '\n' +
        json.dumps({'type': 'cost-state', 'totalCostUSD': cost}) + '\n')


def test_index_keeps_empty_usage_unknown_and_excludes_probes(project):
    register(project, 'empty')
    result = metrics_index.index(project, [{'sid': 'empty', 'class': 'work'},
                                           {'sid': 'probe', 'class': 'probe'}])
    assert len(result['sessions']) == 1
    assert result['totals']['total_tokens'] is None
    assert result['totals']['estimated_cost_usd'] is None
    assert result['totals']['measured_sessions'] == 0


def test_index_yields_between_conversations_without_changing_totals(project):
    register(project, 'parent')
    write(transcripts.local_path(project, 'parent'), 'main-response', 999)
    write(Path(transcripts.subagents_dir(project, 'parent')) / 'agent-child.jsonl',
          'child-response', 777)
    sessions = [{'sid': 'parent', 'class': 'work'}]
    expected = metrics_index.index(project, sessions)
    boundaries = []
    result = metrics_index.index(project, sessions, yield_control=lambda: boundaries.append(1))
    assert result == expected
    assert len(boundaries) >= 2  # parent and child remain separate units of work


def test_crew_totals_include_each_conversation_once_without_client_cost(project):
    register(project, 'parent')
    write(transcripts.local_path(project, 'parent'), 'main-response', 999)
    write(Path(transcripts.subagents_dir(project, 'parent')) / 'agent-child.jsonl', 'child-response', 777)
    result = metrics_index.family(project, 'parent')
    assert len(result['conversations']) == 2
    assert result['totals']['responses'] == 2
    assert result['totals']['total_tokens'] == 220
    assert result['totals']['estimated_cost_usd'] == .0015
    assert result['complete'] is False  # discovered files do not establish a closed child inventory
    main = metrics_index.index(project, [{'sid': 'parent', 'class': 'work'}])
    # t-143: the index folds the child under its parent. The row's own summary stays the main
    # conversation; the row's family figures and the totals agree with the crew view.
    assert main['sessions'][0]['summary']['tokens']['total_tokens'] == 110
    assert main['totals']['main_total_tokens'] == 110
    assert main['totals']['total_tokens'] == main['sessions'][0]['family']['total_tokens'] == result['totals']['total_tokens']
    assert main['sessions'][0]['cost']['reported_usd'] == 999


def test_index_totals_report_pricing_gaps(project):
    register(project, 'parent')
    write(transcripts.local_path(project, 'parent'), 'main-response', 1)
    totals = metrics_index.index(project, [{'sid': 'parent', 'class': 'work'}])['totals']
    assert totals['unpriced_models'] == [] and totals['unpriced_other'] == 0
    path = Path(transcripts.local_path(project, 'parent'))
    path.write_text(path.read_text().replace('claude-opus-5', 'claude-opus-9'))
    totals = metrics_index.index(project, [{'sid': 'parent', 'class': 'work'}])['totals']
    assert totals['unpriced_models'] == [{'model': 'claude-opus-9', 'provider': 'anthropic', 'responses': 1,
                                          'sessions': 1,
                                          'fix': 'bin/alpaca analytics price-check --model claude-opus-9'}]
    assert totals['unpriced_other'] == 0


def test_index_reuses_rows_for_unchanged_sessions_beyond_the_analysis_cache(project, monkeypatch):
    # The record holds more work sessions than metrics.MAX_CACHE; shrink the cache to show it.
    from alpaca import db as record
    from alpaca.analytics import metrics, pricing, ratecards
    monkeypatch.setattr(metrics, 'MAX_CACHE', 1)
    for sid in ('a', 'b', 'c'):
        register(project, sid)
        write(transcripts.local_path(project, sid), sid + '-response', 1)
    calls = []
    real = metrics._analyze
    monkeypatch.setattr(metrics, '_analyze', lambda *args: calls.append(args[1]) or real(*args))
    sessions = [{'sid': sid, 'class': 'work'} for sid in ('a', 'b', 'c')]
    first = metrics_index.index(project, sessions)
    assert sorted(calls) == ['a', 'b', 'c']
    second = metrics_index.index(project, sessions)
    assert sorted(calls) == ['a', 'b', 'c']          # no session analysed again
    assert second == first
    second['sessions'][0]['summary']['responses'] = 999
    assert metrics_index.index(project, sessions)['sessions'][0]['summary']['responses'] == 1
    assert metrics.analysis_key(project, 'a') == metrics.analysis_key(project, 'a')
    path = Path(transcripts.local_path(project, 'b'))
    path.write_text(path.read_text().replace('claude-opus-5', 'claude-opus-9'))
    del calls[:]
    third = metrics_index.index(project, sessions)
    assert calls == ['b']                            # only the changed transcript
    assert [row['model'] for row in third['totals']['unpriced_models']] == ['claude-opus-9']
    del calls[:]
    conn = record.connect(project)
    record.append_event(conn, session='cli', actor='analytics', kind=ratecards.EVENT, ref='claude-opus-9',
                        data={'model': 'claude-opus-9', 'provider': 'anthropic',
                              'source_url': pricing.CLAUDE_PRICING, 'source_urls': [pricing.CLAUDE_PRICING],
                              'rates': ['5', '0.5', '6.25', '10', None, '25'], 'context_threshold_tokens': None,
                              'verified_at': '2026-09-25', 'fast_multiplier': None,
                              'label': {'method': 'agent-entered', 'verified_by': 'cli',
                                        'recorded_at': '2026-09-25T00:00:00+00:00'}})
    conn.close()
    fourth = metrics_index.index(project, sessions)
    assert sorted(calls) == ['a', 'b', 'c']          # a new card reprices every session
    assert fourth['totals']['unpriced_models'] == []


# The live project had 2741 children, then 9314 under 1333 parents (t-680): a full sequential
# rebuild used to evict the next child's compact row from the 2048-entry cache, then from the
# 8192-entry one, on every pass. Only the byte budget may bound the row count.
@pytest.mark.parametrize('count', [2741, 12000])
def test_large_family_rebuild_does_not_reanalyse_unchanged_children(project, monkeypatch, count):
    from collections import OrderedDict
    from alpaca.analytics import metrics
    monkeypatch.setattr(metrics_index, '_ROWS', OrderedDict())     # rows of another case share ids
    monkeypatch.setattr(metrics_index, '_ROW_BYTES', 0)
    keys = {}
    calls = []
    def key(root, sid):
        return (str(root), sid, None, keys.get(sid, 'v1'))
    def analyze(root, sid):
        calls.append(sid)
        return {'summary': {'responses': 1, 'usage_responses': 1,
            'costed_responses': 1, 'estimated_cost_usd': .001,
            'tokens': {'total_tokens': 10}}, 'coverage': {'complete': True},
            'cost': {}, 'session': {'operator': 'claude'}, 'models': []}
    monkeypatch.setattr(metrics, 'analysis_key', key)
    monkeypatch.setattr(metrics, 'analyze', analyze)
    kids = {'child-%s' % i: {} for i in range(count)}
    sessions = [{'sid': 'parent', 'class': 'work'}]
    first = metrics_index.index(project, sessions, families={'parent': kids})
    assert len(calls) == count + 1
    assert metrics_index.index(project, sessions, families={'parent': kids}) == first
    assert len(calls) == count + 1, 'unchanged children must not restart full analysis'
    keys['child-100'] = 'v2'
    metrics_index.index(project, sessions, families={'parent': kids})
    assert calls[count+1:] == ['child-100']


def test_compact_rows_have_a_byte_budget(project, monkeypatch):
    from alpaca.analytics import metrics
    monkeypatch.setattr(metrics_index, 'MAX_ROW_BYTES', 1024)
    monkeypatch.setattr(metrics, 'analysis_key', lambda root, sid: (str(root), sid, None, 'v1'))
    monkeypatch.setattr(metrics, 'analyze', lambda root, sid: {
        'summary': {'padding': 'x' * 2048}, 'coverage': {}, 'cost': {},
        'session': {'operator': 'claude'}, 'models': []})
    metrics_index._analysis_row(project, 'oversized')
    assert not any(k[:2] == (str(project), 'oversized') for k in metrics_index._ROWS)


def test_index_rows_are_identical_with_and_without_the_per_run_scope(project, monkeypatch):
    """t-577: one connection, one obs_source read and one rate-card read for a whole run, same rows."""
    import contextlib
    import sqlite3
    from alpaca.analytics import detail, metrics
    register(project, 'parent')
    write(transcripts.local_path(project, 'parent'), 'main-response', 999)
    write(Path(transcripts.subagents_dir(project, 'parent')) / 'agent-child.jsonl', 'child-response', 777)
    register(project, 'other')
    write(transcripts.local_path(project, 'other'), 'other-response', 5)
    sessions = [{'sid': 'parent', 'class': 'work'}, {'sid': 'other', 'class': 'work'}]
    connects = []
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: connects.append(a) or real(*a, **k))

    def run(scoped):
        metrics._CACHE.clear()
        metrics_index._ROWS.clear()
        connects.clear()
        if not scoped:
            monkeypatch.setattr(detail, 'revision_scope', lambda root: contextlib.nullcontext())
            monkeypatch.setattr(detail, 'revision_batch', lambda root: contextlib.nullcontext())
        return metrics_index.index(project, sessions), len(connects)

    with_scope, scoped_connects = run(True)
    without, plain_connects = run(False)
    assert with_scope == without
    assert scoped_connects < plain_connects
    assert detail._SCOPE.state is None


def test_a_child_the_record_does_not_know_is_not_analysed_again_while_its_key_holds(project, monkeypatch):
    """t-680: a subagent file found under a parent, with no session row, no project copy of its own
    and no pool file, makes metrics.analyze raise KeyError('unknown session'). The index kept no
    row for that outcome, so every index run paid the child's source revision twice plus the failed
    analysis (5979 of the 10598 rows of the live record, every fold). The miss is kept under the
    analysis key like a row: the next run with the same key answers it from the cache, and a key
    that moves analyses it again."""
    from alpaca.analytics import metrics
    register(project, 'parent')
    write(transcripts.local_path(project, 'parent'), 'main-response', 999)
    child = Path(transcripts.subagents_dir(project, 'parent')) / 'agent-child.jsonl'
    write(child, 'child-response', 777)
    calls = []
    real = metrics.analyze
    monkeypatch.setattr(metrics, 'analyze', lambda root, sid, *a: calls.append(sid) or real(root, sid, *a))
    sessions = [{'sid': 'parent', 'class': 'work'}]
    first = metrics_index.index(project, sessions)
    assert 'child' in calls, 'the fixture child goes through the analysis first'
    assert first['sessions'][0]['subagents']['measured'] == 1, 'and is measured by the light reader'
    del calls[:]
    assert metrics_index.index(project, sessions) == first
    assert calls == [], 'an unchanged child the record does not know is not analysed again'
    register(project, 'child')                          # the record learns the child: its key moves
    del calls[:]
    metrics_index.index(project, sessions)
    assert calls == ['child']
