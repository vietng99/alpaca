"""A fold over unchanged sessions reuses their rows; the result equals a cold fold over the same inputs."""
import json
import os
import threading
from pathlib import Path

from alpaca import db, transcripts, util
from alpaca.analytics import build_index, detail, metrics, pricing, ratecards


def register(root, sid):
    conn = db.connect(root)
    db.upsert(conn, 'sessions', 'sid', {'sid': sid, 'started': '2026-09-22T00:00:00Z'})
    db.meta_set(conn, 'operator:' + sid, 'claude')
    conn.close()


def write(root, sid, ident, model='claude-opus-5', out=10):
    path = Path(transcripts.local_path(root, sid)); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'type': 'user', 'uuid': 'u-' + ident, 'timestamp': '2026-09-22T00:00:00Z',
        'message': {'role': 'user', 'content': 'please ' + ident}}) + '\n' +
        json.dumps({'type': 'assistant', 'uuid': ident, 'timestamp': '2026-09-22T00:00:01Z',
        'message': {'id': ident, 'model': model, 'content': [{'type': 'text', 'text': 'done ' + ident}],
        'usage': {'input_tokens': 100, 'output_tokens': out, 'cache_read_input_tokens': 0,
                  'cache_creation_input_tokens': 0}}}) + '\n')


def fold(root):
    data = build_index._fold(root, build_index.collect(root))
    data['project'].pop('generated')
    return json.dumps(data, sort_keys=True)


def cold(root):
    """The fold with every in-process cache emptied: what a freshly started server computes."""
    with build_index._ROWS_LOCK:
        build_index._ROWS.clear()
        build_index._ROW_BYTES = 0
    with metrics._CACHE_LOCK:
        metrics._CACHE.clear()
    return fold(root)


def rows(root):
    return {sid for real, sid in build_index._ROWS if real == os.path.realpath(root)}


def test_incremental_fold_equals_cold_fold(project, monkeypatch):
    for sid in ('a', 'b', 'c'):
        register(project, sid)
        write(project, sid, sid + '-response', model='claude-opus-9' if sid == 'c' else 'claude-opus-5')
    analysed, parsed = [], []
    real_analyze, real_parse = metrics._analyze, build_index.ps.parse
    monkeypatch.setattr(metrics, '_analyze', lambda *args: analysed.append(args[1]) or real_analyze(*args))
    monkeypatch.setattr(build_index.ps, 'parse', lambda path, **k: parsed.append(Path(path).stem) or real_parse(path, **k))
    # a cache smaller than the session list must not matter
    monkeypatch.setattr(metrics, 'MAX_CACHE', 1)

    first = cold(project)
    assert sorted(analysed) == ['a', 'b', 'c'] and sorted(parsed) == ['a', 'b', 'c']
    assert rows(project) == {'a', 'b', 'c'}

    del analysed[:], parsed[:]
    assert fold(project) == first
    assert analysed == [] and parsed == []            # nothing moved: nothing analysed or parsed

    write(project, 'b', 'b-response', out=77)         # one transcript moves
    incremental = fold(project)
    assert analysed == ['b'] and parsed == ['b']
    assert incremental != first and incremental == cold(project)

    del analysed[:], parsed[:]
    Path(transcripts.local_path(project, 'zz')).write_text(Path(transcripts.local_path(project, 'a')).read_text())
    with_zz = fold(project)
    assert analysed == ['zz'] and parsed == ['zz'] and 'zz' in rows(project)
    del analysed[:], parsed[:]
    os.remove(transcripts.local_path(project, 'zz'))  # a session disappears
    removed = fold(project)
    assert analysed == [] and rows(project) == {'a', 'b', 'c'}
    assert removed == incremental == cold(project) and with_zz != removed

    record = db.connect(project)                      # a new rate card reprices claude-opus-9
    db.append_event(record, session='cli', actor='analytics', kind=ratecards.EVENT, ref='claude-opus-9',
                    data={'model': 'claude-opus-9', 'provider': 'anthropic',
                          'source_url': pricing.CLAUDE_PRICING, 'source_urls': [pricing.CLAUDE_PRICING],
                          'rates': ['5', '0.5', '6.25', '10', None, '25'], 'context_threshold_tokens': None,
                          'verified_at': '2026-09-25', 'fast_multiplier': None,
                          'label': {'method': 'agent-entered', 'verified_by': 'cli',
                                    'recorded_at': '2026-09-25T00:00:00+00:00'}})
    record.close()
    del analysed[:], parsed[:]
    repriced = fold(project)
    assert sorted(analysed) == ['a', 'b', 'c']
    by_sid = {s['sid']: s for s in json.loads(repriced)['sessions']}
    before = {s['sid']: s for s in json.loads(incremental)['sessions']}
    assert before['c']['cost_notional_usd'] is None and by_sid['c']['cost_notional_usd'] is not None
    assert repriced == cold(project)


def test_cached_rows_are_independent_copies(project):
    register(project, 'a')
    write(project, 'a', 'a-response')
    first = build_index.collect(project)
    first[0]['coverage']['state'] = 'mutated'
    first[0]['tokens']['output'] = -1
    first[0]['human_prompts'].append('x')
    again = build_index.collect(project)
    assert again[0]['coverage']['state'] != 'mutated' and again[0]['tokens']['output'] == 10
    assert [p['text'] for p in again[0]['human_prompts']] == ['please a-response']


def test_collect_only_keeps_other_rows(project):
    for sid in ('a', 'b'):
        register(project, sid)
        write(project, sid, sid + '-response')
    build_index.collect(project)
    build_index.collect(project, only='a')
    assert {'a', 'b'} <= rows(project)


def test_revision_batch_matches_per_session_revision(project):
    for sid in ('a', 'b'):
        register(project, sid)
        write(project, sid, sid + '-response')
    conn = db.connect(project)
    conn.execute('INSERT INTO obs_source VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 ('src-1', 'child-x', 'a', 'claude', 'transcript', None, 'child-x',
                  json.dumps({}), 1, 0, util.now_iso(), util.now_iso()))
    conn.commit(); conn.close()
    alone = {sid: metrics.analysis_key(project, sid) for sid in ('a', 'b')}
    with detail.revision_batch(project):
        batched = {sid: metrics.analysis_key(project, sid) for sid in ('a', 'b')}
    assert batched == alone
    assert detail.source_revision(project, 'a')['capability_revision'] != detail.source_revision(project, 'b')['capability_revision']


# ---- cache-key coverage past the child cap, and the minors of the round 1 check ---------------

def _assistant(ident):
    return json.dumps({'type': 'assistant', 'uuid': ident, 'timestamp': '2026-09-22T00:00:01Z',
                       'message': {'id': ident, 'model': 'claude-opus-5',
                                   'content': [{'type': 'text', 'text': 'done ' + ident}],
                                   'usage': {'input_tokens': 100, 'output_tokens': 10,
                                             'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}}})


def _obs(root, source_id, session, parent=None, locator=None):
    conn = db.connect(root)
    conn.execute('INSERT INTO obs_source VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 (source_id, session, parent, 'claude', 'transcript', locator, None, json.dumps({}), 1, 0,
                  util.now_iso(), util.now_iso()))
    conn.commit()
    conn.close()


def _rows_by_sid(root):
    data = build_index._fold(root, build_index.collect(root))
    return {s['sid']: s for s in data['sessions']}


def _fillers(root, count=40):
    # More sessions than the 32-entry analysis cache, so only the per-session rows can answer.
    for n in range(count):
        register(root, 'f%02d' % n)
        write(root, 'f%02d' % n, 'f%02dx' % n)


def test_subagent_file_past_the_child_cap_moves_the_row(project):
    _fillers(project)
    register(project, 'a')
    write(project, 'a', 'a1')
    sub = Path(transcripts.subagents_dir(project, 'a'))
    sub.mkdir(parents=True)
    for n in range(detail.MAX_CHILDREN + 1):
        (sub / ('agent-%03d.jsonl' % n)).write_text(_assistant('s%d' % n) + '\n')
    cold(project)
    nested = sub / 'workflows' / 'wf_1'                # reached by the scan after the capped files
    nested.mkdir(parents=True)
    (nested / 'agent-999.jsonl').write_text(_assistant('s999') + '\n')
    warm = fold(project)
    assert next(s for s in json.loads(warm)['sessions'] if s['sid'] == 'a')['subagents'] == detail.MAX_CHILDREN + 2
    assert warm == cold(project)


def test_subagent_file_edited_past_the_cap_moves_the_revision(project):
    register(project, 'a')
    write(project, 'a', 'a1')
    sub = Path(transcripts.subagents_dir(project, 'a'))
    sub.mkdir(parents=True)
    for n in range(detail.MAX_CHILDREN + 1):
        (sub / ('agent-%03d.jsonl' % n)).write_text(_assistant('s%d' % n) + '\n')
    cold(project)
    before = detail.source_revision(project, 'a')
    (sub / 'agent-040.jsonl').write_text(_assistant('s40') + _assistant('s40b') + '\n')
    assert detail.source_revision(project, 'a') != before


def test_child_locator_vanishing_past_the_cap_moves_coverage(project, tmp_path):
    _fillers(project)
    register(project, 'a')
    write(project, 'a', 'a1')
    ext = tmp_path / 'ext'
    ext.mkdir()
    for n in range(detail.MAX_CHILDREN + 5):
        path = ext / ('agent-%03d.jsonl' % n)
        path.write_text(_assistant('k%d' % n) + '\n')
        _obs(project, 'child-src-%03d' % n, 'kid-%03d' % n, parent='a', locator=str(path))
    cold(project)
    (ext / ('agent-%03d.jsonl' % (detail.MAX_CHILDREN + 4))).unlink()
    warm = fold(project)
    coverage = next(s for s in json.loads(warm)['sessions'] if s['sid'] == 'a')['coverage']
    assert coverage['child_coverage']['missing'] == ['kid-%03d' % (detail.MAX_CHILDREN + 4)]
    assert warm == cold(project)


def test_non_child_locator_vanishing_moves_coverage(project, tmp_path):
    register(project, 'a')
    write(project, 'a', 'a1')
    extra = tmp_path / 'hooks-a.jsonl'
    extra.write_text('{}\n')
    _obs(project, 'src-hooks', 'a', locator=str(extra))
    cold(project)
    first = _rows_by_sid(project)['a']['coverage']['source_registry']
    extra.unlink()
    warm = _rows_by_sid(project)['a']['coverage']['source_registry']
    assert warm != first and warm['readable'] == 0


def test_revision_batch_answers_only_for_its_own_root(project, tmp_path):
    import shutil
    register(project, 'a')
    write(project, 'a', 'a1')
    other = tmp_path / 'proj2'
    shutil.copytree(project, other)
    _obs(str(other), 'only-in-other', 'a')
    alone = detail.source_revision(str(other), 'a')
    with detail.revision_batch(project):
        assert detail.source_revision(str(other), 'a') == alone


def test_a_fold_running_past_the_stall_bound_makes_health_stale(project, monkeypatch):
    from alpaca import serve
    conn = db.connect(project)
    live = serve.Live(project)
    live.refresh()
    assert json.loads(live.health_json())['projection']['state'] == 'fresh'
    gate, entered = threading.Event(), threading.Event()
    real = serve.build_index._fold

    def hung(root, sessions):
        entered.set()
        gate.wait(60)
        return real(root, sessions)
    monkeypatch.setattr(serve.build_index, '_fold', hung)
    db.append_event(conn, session='s1', actor='agent', kind='heartbeat', data={})
    live._fold_at = 0
    live.refresh(throttle=True)
    assert entered.wait(10)
    live._fold_at = serve.time.time()
    assert json.loads(live.health_json())['projection']['state'] == 'fresh'   # still within the bound
    live._fold_at = serve.time.time() - serve.FOLD_STALL_S - 1
    assert json.loads(live.health_json())['projection']['state'] == 'stale'
    gate.set()
    live._fold_thread.join(30)
    assert json.loads(live.health_json())['projection']['state'] == 'fresh'
    conn.close()


def test_concurrent_collects_keep_the_byte_accounting(project, monkeypatch):
    register(project, 'a')
    write(project, 'a', 'a1')
    cold(project)
    barrier = threading.Barrier(2, timeout=10)
    real = build_index.ps.parse

    def parse(path, **kwargs):
        barrier.wait()
        return real(path, **kwargs)
    monkeypatch.setattr(build_index.ps, 'parse', parse)
    threads = [threading.Thread(target=build_index.collect, args=(project,)) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert build_index._ROW_BYTES == sum(entry['size'] for entry in build_index._ROWS.values())
    monkeypatch.setattr(build_index, 'MAX_ROW_JSON_BYTES', -1)   # an inconsistent bound empties the table, no KeyError
    with build_index._ROWS_LOCK:
        build_index._trim()
    assert not build_index._ROWS


def test_project_marker_appearing_moves_the_attribution(project, tmp_path):
    # parse attributes an edited file to the project whose marker sits above it; the marker is
    # outside the session, so the cached parse must not freeze that answer.
    other = tmp_path / 'other'
    other.mkdir()
    register(project, 'a')
    path = Path(transcripts.local_path(project, 'a'))
    path.parent.mkdir(parents=True, exist_ok=True)
    edit = {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Edit',
            'input': {'file_path': str(other / 'x.py'), 'old_string': 'a', 'new_string': 'b'}}
    path.write_text(json.dumps({'type': 'assistant', 'uuid': 'e1', 'timestamp': '2026-09-22T00:00:01Z',
        'message': {'id': 'e1', 'model': 'claude-opus-5', 'content': [edit],
                    'usage': {'input_tokens': 100, 'output_tokens': 10, 'cache_read_input_tokens': 0,
                              'cache_creation_input_tokens': 0}}}) + '\n')
    cold(project)
    assert _rows_by_sid(project)['a']['projects'] == {}
    (other / 'ALPACA-MANIFEST').write_text('x\n')
    warm = fold(project)
    assert next(s for s in json.loads(warm)['sessions'] if s['sid'] == 'a')['projects'] == {str(other): 1}
    assert warm == cold(project)
