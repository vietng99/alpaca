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
