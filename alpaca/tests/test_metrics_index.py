"""Project and crew totals never count client snapshots as additional charges."""
import json
from pathlib import Path
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
    assert main['totals']['total_tokens'] == 110
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
