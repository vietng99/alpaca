import json
from pathlib import Path

import pytest

from alpaca import db, observability as obs


def source(tmp_path, content):
    path = tmp_path / 'native.jsonl'
    path.write_text(content)
    spec = obs.expect_source(str(tmp_path), 's', 'codex', locator=str(path), native_id='n', closed=True)
    return path, spec['source_id']


def rows(root, table):
    conn = db.connect_readonly(str(root))
    try:
        return [dict(r) for r in conn.execute('SELECT * FROM ' + table)]
    finally:
        conn.close()


def test_partial_line_is_replayed_once_after_restart(tmp_path):
    path, sid = source(tmp_path, '{"type":"user"}\n{"type":')
    obs.run_once(str(tmp_path), consumers=False)
    assert len(rows(tmp_path, 'obs_observation')) == 1
    assert rows(tmp_path, 'obs_cursor')[0]['byte_offset'] == len('{"type":"user"}\n')
    with path.open('a') as f:
        f.write('"assistant"}\n')
    obs.run_once(str(tmp_path), consumers=False)
    obs.run_once(str(tmp_path), consumers=False)
    assert len(rows(tmp_path, 'obs_observation')) == 2
    assert rows(tmp_path, 'obs_cursor')[0]['byte_offset'] == path.stat().st_size


def test_early_same_size_rewrite_creates_generation(tmp_path):
    path, sid = source(tmp_path, '{"type":"user","x":"A"}\n' + '{"x":0}\n' * 1000)
    obs.run_once(str(tmp_path), max_records=2000, consumers=False)
    path.write_text(path.read_text().replace('"A"', '"B"'))
    obs.run_once(str(tmp_path), max_records=2000, consumers=False)
    assert len(rows(tmp_path, 'obs_generation')) == 2
    assert rows(tmp_path, 'obs_cursor')[0]['generation'] == 2
    assert len(rows(tmp_path, 'obs_observation')) == 2002


def test_malformed_and_private_reasoning_never_enter_facts(tmp_path):
    source(tmp_path, 'broken SECRET\n' + json.dumps({'type':'response_item', 'payload':{'type':'reasoning','text':'PRIVATE SECRET'}}) + '\n')
    obs.run_once(str(tmp_path), consumers=False)
    assert rows(tmp_path, 'obs_cursor')[0]['line_number'] == 2
    assert any(r['code'] == 'malformed-json' for r in rows(tmp_path, 'obs_issue'))
    assert 'SECRET' not in json.dumps(rows(tmp_path, 'obs_observation') + rows(tmp_path, 'obs_issue'))


def test_crash_before_cursor_commit_replays_without_loss(tmp_path, monkeypatch):
    source(tmp_path, '{"type":"user"}\n')
    def crash(*args):
        raise RuntimeError('power failure')
    monkeypatch.setattr(obs, '_before_commit', crash)
    result = obs.run_once(str(tmp_path), consumers=False)
    assert result['errors'] == 1
    assert rows(tmp_path, 'obs_cursor') == []
    assert rows(tmp_path, 'obs_observation') == []
    monkeypatch.setattr(obs, '_before_commit', lambda *_: None)
    conn = db.connect(str(tmp_path))
    conn.execute("DELETE FROM meta WHERE key LIKE 'obs:retry:%'")
    conn.close()
    obs.run_once(str(tmp_path), consumers=False)
    assert len(rows(tmp_path, 'obs_observation')) == 1


def test_unregistered_child_and_open_expectations_are_incomplete(tmp_path):
    source(tmp_path, '{"type":"user"}\n')
    obs.expect_source(str(tmp_path), 'child', 'codex', parent_session='s', native_id='child')
    coverage = obs.coverage(str(tmp_path), 's')
    assert not coverage['complete']
    assert coverage['missing_children'] == ['child']
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob('*')}
    assert obs.status(str(tmp_path))['coverage']['complete'] is False
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob('*')} == before


def test_bounded_batch_and_identical_native_id_conflict(tmp_path):
    source(tmp_path, '{"uuid":"1","type":"user"}\n{"uuid":"1","type":"changed"}\n')
    obs.run_once(str(tmp_path), max_records=1, consumers=False)
    assert len(rows(tmp_path, 'obs_observation')) == 1
    obs.run_once(str(tmp_path), max_records=1, consumers=False)
    assert any(r['code'] == 'native-id-conflict' for r in rows(tmp_path, 'obs_issue'))


def test_hook_outcome_spool_and_counter_commit_together(tmp_path):
    from alpaca.hooks.common import record_outcome
    (tmp_path/'ALPACA-MANIFEST').write_text('managed')
    record_outcome({'cwd':str(tmp_path),'session_id':'s'},'pre_tool','failed',12,'OSError')
    obs.run_once(str(tmp_path),consumers=False)
    obs.run_once(str(tmp_path),consumers=False)
    recorded = rows(tmp_path,'obs_hook_outcome')
    assert len(recorded) == 1 and recorded[0]['outcome'] == 'failed'
    conn = db.connect_readonly(str(tmp_path))
    assert json.loads(db.meta_get(conn,'hook-cost:s:pre_tool'))['runs'] == 1
    conn.close()


def test_wiki_failure_does_not_stop_other_consumer_or_advance_cursor(tmp_path,monkeypatch):
    from alpaca.observability import consumers
    conn = db.connect(str(tmp_path))
    db.append_event(conn,session='s',actor='a',kind='test')
    seen = []
    def fail(root,rows):
        raise OSError('secret path')
    monkeypatch.setattr(consumers,'CONSUMERS',{'wiki':fail,'other':lambda root,rows:seen.extend(rows)})
    consumers.consume(str(tmp_path),conn)
    assert len(seen) == 1
    checkpoints = {r['consumer']:r for r in rows(tmp_path,'obs_consumer_checkpoint')}
    assert checkpoints['wiki']['event_id'] == 0
    assert checkpoints['wiki']['failures'] == 1
    assert checkpoints['other']['event_id'] == 1
    assert 'secret' not in json.dumps(rows(tmp_path,'obs_issue'))
    consumers.consume(str(tmp_path),conn)
    assert len(seen) == 1
    conn.close()


class _Refresh:
    """A stand-in domain profile whose refresh answers from a script."""

    def __init__(self, answer):
        from alpaca import profile
        self.base, self.answer, self.calls = profile.Profile(), answer, []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def refresh(self, root, session, events=None):
        self.calls.append((session, [e['kind'] for e in events or []]))
        return self.answer


def _use(monkeypatch, fake):
    from alpaca import profile
    monkeypatch.setattr(profile, 'load', lambda root: fake)
    return fake


def test_profile_error_result_retains_checkpoint_until_successful_retry(tmp_path, monkeypatch):
    from alpaca.observability import consumers
    conn = db.connect(str(tmp_path))
    seen = []
    monkeypatch.setattr(consumers, 'CONSUMERS', {
        'profile': consumers._profile, 'other': lambda root, events: seen.extend(events)})
    fake = _use(monkeypatch, _Refresh(None))
    try:
        db.append_event(conn, session='s', actor='a', kind='test', op='domain-run')
        assert consumers.consume(str(tmp_path), conn)['profile']['through_event'] == 1
        fake.answer = {'status': 'error', 'error': 'private failure detail', 'recovery': 'domain sync'}
        db.append_event(conn, session='s', actor='a', kind='session-end')
        result = consumers.consume(str(tmp_path), conn)
        assert result['profile']['status'] == 'failed'
        assert result['profile']['through_event'] == 1
        assert result['other']['through_event'] == 2
        checkpoint = dict(conn.execute(
            "SELECT * FROM obs_consumer_checkpoint WHERE consumer='profile'").fetchone())
        assert checkpoint['event_id'] == 1 and checkpoint['failures'] == 1
        assert obs._time(checkpoint['next_attempt']) > obs._time()
        assert 'private failure detail' not in json.dumps(rows(tmp_path, 'obs_issue'))
        assert consumers.consume(str(tmp_path), conn)['profile']['status'] == 'backoff'
        assert len(seen) == 2
        assert fake.calls == [('observability-collector', ['test']),
                              ('observability-collector', ['session-end'])]

        fake.answer = {'status': 'ok'}
        conn.execute("UPDATE obs_consumer_checkpoint SET next_attempt=NULL WHERE consumer='profile'")
        result = consumers.consume(str(tmp_path), conn)
        assert result['profile'] == {'status': 'ok', 'through_event': 2}
        assert fake.calls[-1] == ('observability-collector', ['session-end'])
        checkpoint = dict(conn.execute(
            "SELECT * FROM obs_consumer_checkpoint WHERE consumer='profile'").fetchone())
        assert checkpoint['failures'] == 0 and checkpoint['next_attempt'] is None
        assert all(row['resolved_at'] for row in rows(tmp_path, 'obs_issue'))
    finally:
        conn.close()


def test_the_empty_profile_consumer_advances_without_work(tmp_path, monkeypatch):
    from alpaca.observability import consumers
    conn = db.connect(str(tmp_path))
    monkeypatch.setattr(consumers, 'CONSUMERS', {'profile': consumers._profile})
    try:
        db.append_event(conn, session='s', actor='a', kind='session-end')
        assert consumers.consume(str(tmp_path), conn)['profile'] == {'status': 'ok', 'through_event': 1}
    finally:
        conn.close()


def _no_hook_capture(monkeypatch):
    """Fail on any wiki capture a hook can reach: `service.capture` (what the hooks and
    `stop.capture_open_session` call since the op-008 merge), the drain beneath it, and the
    transcript snapshot. t-334, review2 m5: this guard patched only `drain.run`, which no hook calls
    any more, so it guarded nothing."""
    from alpaca import capture_runner, transcripts
    from alpaca.wiki import service
    from alpaca.wiki.ingest import drain
    for owner, name in ((service, 'capture'), (drain, 'capture'), (drain, 'run'),
                        (transcripts, 'snapshot'), (capture_runner, 'run_process')):
        monkeypatch.setattr(owner, name, lambda *_a, _n=name, **_kw: pytest.fail('synchronous ' + _n))


def test_enabled_checkpoint_queues_without_running_projection(project, monkeypatch):
    from alpaca.hooks import pre_compact
    folder = Path(project)/'.alpaca/observability'
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'enabled').touch()
    _no_hook_capture(monkeypatch)
    result = pre_compact.handle({'cwd':project,'session_id':'s','_strict':True})
    assert result['queued']
    # no collector is alive, so the hook notes once that nothing captures (A-8, t-070 RC4), as the
    # state kind capture-no-collector (t-070 round 2 C-D, I-5)
    assert [r['kind'] for r in rows(project,'events')][-2:] == ['pre-compact', 'capture-no-collector']
    note = json.loads(rows(project,'events')[-1]['data'])
    assert note['where'] == 'pre_compact'
    assert note['error'].startswith('observability enabled but no collector alive')
    # the Codex checkpoint verb, the whole Stop hook (its capture step runs in `main`, after
    # `handle`) and SessionEnd leave capture to the collector too
    assert pre_compact.checkpoint({'cwd':project,'session_id':'s','_strict':True})['queued']
    import io, sys
    from alpaca.hooks import common, session_end, stop
    monkeypatch.setattr(sys, 'stdout', io.StringIO())
    monkeypatch.setattr(common, 'read_stdin', lambda *_a, **_kw: {'cwd':project,'session_id':'s','_strict':True})
    stop.main.__wrapped__()
    assert session_end.handle({'cwd':project,'session_id':'s','_strict':True})['queued']
    assert [r['kind'] for r in rows(project,'events')][-3:] == ['session-checkpoint','turn-end','session-end']
    # the note is rate-limited: one for the four hook runs
    assert [r['kind'] for r in rows(project,'events')].count('capture-no-collector') == 1
    assert [r['kind'] for r in rows(project,'events')].count('drain-failed') == 0


def test_missing_snapshot_retries_without_new_events_and_other_sessions_progress(tmp_path):
    from alpaca.observability import consumers
    conn = db.connect(str(tmp_path))
    missing = tmp_path/'missing.jsonl'
    ready = tmp_path/'ready.jsonl'; ready.write_text('{"type":"user"}\n')
    obs.expect_source(str(tmp_path),'missing','codex',locator=str(missing))
    obs.expect_source(str(tmp_path),'ready','codex',locator=str(ready))
    consumers.queue_snapshot(conn,'missing',event_id=1)
    consumers.queue_snapshot(conn,'ready',event_id=2)
    result = consumers.run_snapshot_jobs(str(tmp_path),conn)
    assert result == {'completed':1,'failed':1}
    missing.write_text('{"type":"user"}\n')
    conn.execute('UPDATE obs_projection_job SET next_attempt=NULL')
    result = consumers.run_snapshot_jobs(str(tmp_path),conn)
    assert result == {'completed':1,'failed':0}
    assert all(r['status']=='done' for r in rows(tmp_path,'obs_projection_job'))
    conn.close()


def test_preservation_batch_builds_one_wiki_index_and_keeps_failures_independent(tmp_path, monkeypatch):
    from alpaca.observability import consumers
    from alpaca.wiki.engine.echo import EchoIndex
    from alpaca.wiki.ingest.absorb import Absorber
    # Merge of op-008 into main (2026-10-06): op-008 (664ef5ba7) made the echo index lazy. An
    # absorber loads it on first use (`Absorber.echo_index`) and `_reload_echo_index` only drops
    # it, so the batch is measured by the absorbers it opens and the index loads it makes.
    absorbers = []
    loads = []
    scans = []
    original_init = Absorber.__init__
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        absorbers.append(self)
        self.db.conn.set_trace_callback(lambda sql: scans.append(sql) if "WHERE d.kind='raw' AND b.status='active' AND b.ordinal=0" in sql else None)
    original_load = EchoIndex.load
    def counted(self, rows):
        loads.append(self)
        return original_load(self, rows)
    monkeypatch.setattr(Absorber, '__init__', init)
    monkeypatch.setattr(EchoIndex, 'load', counted)
    conn = db.connect(str(tmp_path))
    for i in range(10):
        native = tmp_path / ('native-%d.jsonl' % i)
        native.write_text('{"type":"user","message":{"content":"batch %d"}}\n' % i)
        obs.expect_source(str(tmp_path), 's%d' % i, 'codex', locator=str(native))
        consumers.queue_snapshot(conn, 's%d' % i)
    obs.expect_source(str(tmp_path), 'missing', 'codex', locator=str(tmp_path/'absent.jsonl'))
    consumers.queue_snapshot(conn, 'missing')
    assert consumers.run_snapshot_jobs(str(tmp_path), conn, limit=20) == {'completed':10, 'failed':1}
    assert len(absorbers) == 1, 'opening the wiki store per source throttles catch-up'
    assert len(loads) == 1, 'rebuilding the complete wiki index per source throttles catch-up'
    assert len(scans) == 1, 'a shared batch should inventory prior projections once'
    jobs = rows(tmp_path, 'obs_projection_job')
    assert sum(job['status']=='done' for job in jobs) == 10
    assert next(job for job in jobs if job['session']=='missing')['status'] == 'retry'
    # No work must avoid rebuilding the index entirely.
    assert consumers.run_snapshot_jobs(str(tmp_path), conn) == {'completed':0, 'failed':0}
    assert len(absorbers) == 1 and len(loads) == 1
    conn.close()


def test_cached_wiki_capture_does_not_rebuild_corpus_for_empty_work(tmp_path, monkeypatch):
    from alpaca.observability import consumers
    from alpaca.wiki.ingest.absorb import Absorber
    path, _ = source(tmp_path, '{"type":"user"}\n')
    consumers._capture_wiki(str(tmp_path), [], ['s'])
    monkeypatch.setattr(Absorber, '_reload_echo_index', lambda *_: pytest.fail('empty capture loaded wiki corpus'))
    # Merge of op-008 into main (2026-10-06): the echo index is lazy since op-008 (664ef5ba7), so
    # an empty capture must not open the wiki store at all, which would be where a load starts.
    monkeypatch.setattr(Absorber, '__init__', lambda *_a, **_k: pytest.fail('empty capture opened the wiki store'))
    assert consumers._capture_wiki(str(tmp_path), [], ['s'])['notes'] == 0


def test_preservation_batch_reloads_witnesses_after_external_wiki_commit(tmp_path, monkeypatch):
    from alpaca.observability import consumers
    from alpaca.wiki.ingest import drain
    from alpaca.wiki.ingest.absorb import Absorber
    # Merge of op-008 into main (2026-10-06): the echo index is lazy since op-008 (664ef5ba7), so
    # `_reload_echo_index` runs only when another connection committed (it drops the shared index
    # and the next use loads it again); the shared absorber is found by its construction.
    absorbers = []
    original_init = Absorber.__init__
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        absorbers.append(self)
    monkeypatch.setattr(Absorber, '__init__', init)
    from alpaca.wiki.engine.echo import EchoIndex
    built = []
    original_load = EchoIndex.load
    def load(self, rows):
        built.append(self)
        return original_load(self, rows)
    monkeypatch.setattr(EchoIndex, 'load', load)
    loads = []
    original_reload = Absorber._reload_echo_index
    def counted(self):
        loads.append(self)
        original_reload(self)
    monkeypatch.setattr(Absorber, '_reload_echo_index', counted)
    original_capture = consumers._capture_wiki
    calls = []
    def capture(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        calls.append(result)
        if len(calls) == 1:
            # Another native operator commits between two independent jobs.
            external = Absorber(absorbers[0].cfg)
            external.absorb_text('raw/external.md', 'An independent witness from another operator.')
            external.close()
        return result
    monkeypatch.setattr(consumers, '_capture_wiki', capture)
    conn = db.connect(str(tmp_path))
    for sid in ('first', 'second'):
        native = tmp_path / (sid+'.jsonl')
        native.write_text('{"type":"user"}\n')
        obs.expect_source(str(tmp_path), sid, 'codex', locator=str(native))
        consumers.queue_snapshot(conn, sid)
    assert consumers.run_snapshot_jobs(str(tmp_path), conn) == {'completed':2, 'failed':0}
    assert loads.count(absorbers[0]) == 1
    # job one built the shared index, the external operator its own, and job two built the
    # shared one again after the external commit
    assert len(built) == 3 and absorbers[0].echo_index is built[-1]
    assert 'raw/external.md' in {doc for doc, _ in absorbers[0].echo_index.entries().values()}
    conn.close()


def test_explicit_preservation_batch_drains_only_its_bounded_jobs(tmp_path):
    for i in range(12):
        native = tmp_path / ('native-%d.jsonl' % i)
        native.write_text('{"type":"user"}\n')
        obs.expect_source(str(tmp_path), 's%d' % i, 'codex', locator=str(native))
    result = obs.run_once(str(tmp_path), max_records=100, preservation_batch=5)
    assert result['preservation'] == {'completed':5, 'failed':0}
    assert obs.status(str(tmp_path))['collector']['preservation_progress'] == {'completed':5, 'failed':0, 'jobs':5}
    assert sum(row['status']=='pending' for row in rows(tmp_path, 'obs_projection_job')) == 7
    result = obs.run_once(str(tmp_path), max_records=100, preservation_batch=0)
    assert result['preservation'] == {'completed':1, 'failed':0}


def test_graceful_stop_finishes_current_job_and_leaves_remaining_jobs_pending(tmp_path):
    from alpaca.observability import consumers
    conn = db.connect(str(tmp_path))
    for sid in ('one', 'two'):
        native = tmp_path/(sid+'.jsonl'); native.write_text('{"type":"user"}\n')
        obs.expect_source(str(tmp_path), sid, 'codex', locator=str(native))
        consumers.queue_snapshot(conn, sid)
    def stop_requested():
        return conn.execute("SELECT count(*) FROM obs_projection_job WHERE status='done'").fetchone()[0] > 0
    assert consumers.run_snapshot_jobs(str(tmp_path), conn, limit=128, stop_requested=stop_requested) == {'completed':1, 'failed':0}
    assert sum(row['status']=='pending' for row in rows(tmp_path, 'obs_projection_job')) == 1
    conn.close()


def test_explicit_consumer_retry_preserves_other_backoff_and_real_failure(tmp_path, monkeypatch):
    import datetime as dt
    from alpaca.observability import consumers
    conn = db.connect(str(tmp_path))
    db.append_event(conn, session='s', actor='test', kind='note')
    for name in ('wiki', 'projection'):
        db.upsert(conn, 'obs_consumer_checkpoint', 'consumer', dict(
            consumer=name, event_id=0, observation_id=0, failures=3, error='OSError',
            updated_at=obs._time().isoformat(),
            next_attempt=(obs._time()+dt.timedelta(minutes=15)).isoformat()))
    calls = []
    def broken(*args):
        calls.append('wiki'); raise OSError('real failure')
    monkeypatch.setattr(consumers, 'CONSUMERS', {'wiki':broken, 'projection':lambda *_:calls.append('projection')})
    assert consumers.consume(str(tmp_path),conn)['wiki']['status'] == 'backoff'
    result = consumers.consume(str(tmp_path),conn,retry_consumers=['wiki'])
    assert result['wiki']['status'] == 'failed' and result['wiki']['through_event'] == 0
    assert result['projection']['status'] == 'backoff' and calls == ['wiki']
    monkeypatch.setitem(consumers.CONSUMERS,'wiki',lambda *_: calls.append('repaired'))
    result = consumers.consume(str(tmp_path),conn,retry_consumers=['wiki'])
    assert result['wiki']['status'] == 'ok' and result['wiki']['through_event'] == 1
    assert result['projection']['status'] == 'backoff'
    with pytest.raises(ValueError,match='unknown consumer'):
        consumers.consume(str(tmp_path),conn,retry_consumers=['typo'])
    conn.close()


def test_late_registration_and_recovered_readability(tmp_path):
    conn = db.connect(str(tmp_path))
    obs.expect_source(str(tmp_path),'s','codex')
    native = tmp_path/'late.jsonl'; native.write_text('{"type":"user"}\n')
    db.upsert(conn,'sessions','sid',{'sid':'s','transcript':str(native)})
    db.meta_set(conn,'operator:s','codex')
    obs.run_once(str(tmp_path),consumers=False)
    assert len(rows(tmp_path,'obs_observation')) == 1
    sid = rows(tmp_path,'obs_source')[0]['source_id']
    obs.issue(conn,'source-unreadable',source_id=sid,detail='PermissionError')
    obs.run_once(str(tmp_path),consumers=False)
    assert rows(tmp_path,'obs_issue')[0]['resolved_at']
    conn.close()


@pytest.mark.parametrize('legacy_session_locator', [False, True])
def test_restore_disabled_source_is_not_automatically_rebound(tmp_path, monkeypatch, legacy_session_locator):
    from alpaca import transcripts
    native = tmp_path / 'native.jsonl'
    native.write_text('{"type":"user"}\n')
    obs.expect_source(str(tmp_path), 's', 'codex', native_id='native-session',
                      capabilities={'restore_disabled': True, 'native_transcript': True})
    monkeypatch.setattr(transcripts, 'discover_codex', lambda *_: str(native))
    conn = db.connect(str(tmp_path))
    try:
        if legacy_session_locator:
            db.upsert(conn, 'sessions', 'sid', {'sid': 's', 'transcript': str(native)})
            db.meta_set(conn, 'operator:s', 'codex')
        obs._register_local(str(tmp_path), conn)
        source = dict(conn.execute('SELECT * FROM obs_source').fetchone())
        assert source['locator'] is None
        assert json.loads(source['capabilities'])['restore_disabled'] is True
    finally:
        conn.close()


def test_explicit_rebind_clears_restore_disable_and_preserves_capabilities(tmp_path):
    obs.expect_source(str(tmp_path), 's', 'codex', native_id='native-session',
                      capabilities={'restore_disabled': True, 'child_inventory_complete': False})
    changed = obs.expect_source(str(tmp_path), 's', 'codex',
                                capabilities={'native_transcript': True, 'restore_disabled': False})
    assert json.loads(changed['capabilities'])['restore_disabled'] is True
    native = tmp_path / 'approved.jsonl'
    native.write_text('{"type":"user"}\n')
    rebound = obs.expect_source(str(tmp_path), 's', 'codex', locator=str(native),
                               capabilities={'adapter': 'codex'})
    capabilities = json.loads(rebound['capabilities'])
    assert not capabilities.get('restore_disabled')
    assert capabilities['child_inventory_complete'] is False
    assert capabilities['native_transcript'] is True
    assert capabilities['adapter'] == 'codex'
    assert obs.run_once(str(tmp_path), consumers=False)['records'] == 1


def test_native_usage_identity_and_nonfinite_values_are_explicit(tmp_path):
    path, sid = source(tmp_path,json.dumps({'type':'token_usage_record','payload':{'response_id':'r1','usage':{'output_tokens':5,'input_tokens':float('inf')}}})+'\n'+json.dumps({'type':'hook_outcome','duration_ms':float('inf')})+'\n')
    obs.run_once(str(tmp_path),consumers=False)
    facts = rows(tmp_path,'obs_observation')
    assert facts[0]['native_id'] == 'r1'
    assert json.loads(facts[0]['facts'])['usage'] == {'output_tokens':5}
    assert facts[1]['kind'] == 'quarantined'


def test_snapshot_backoff_compares_utc_times(tmp_path,monkeypatch):
    import datetime as dt
    from alpaca.observability import consumers
    conn=db.connect(str(tmp_path))
    consumers.queue_snapshot(conn,'s')
    conn.execute('UPDATE obs_projection_job SET next_attempt=?',((obs._time()+dt.timedelta(minutes=15)).isoformat(),))
    monkeypatch.setattr(consumers.util,'now_iso',lambda: (obs._time().astimezone(dt.timezone(dt.timedelta(hours=7)))).isoformat())
    assert consumers.run_snapshot_jobs(str(tmp_path),conn)=={'completed':0,'failed':0}
    conn.close()


def test_scheduled_backup_budget_reports_failure_without_deleting(tmp_path):
    from alpaca.observability.maintenance import backup_once
    folder=tmp_path/'.alpaca/backups';folder.mkdir(parents=True)
    kept=folder/'previous';kept.write_text('retained')
    result=backup_once(str(tmp_path),budget_bytes=1)
    assert result['ok'] is False and kept.read_text()=='retained'
    from alpaca import backup
    assert backup.status(str(tmp_path))['status']=='error'


def test_spool_ancestor_symlink_is_never_registered_or_written(tmp_path):
    root=tmp_path/'project'; root.mkdir()
    outside=tmp_path/'outside'; outside.mkdir()
    (outside/'s.jsonl').write_text('{"type":"user"}\n')
    folder=root/'.alpaca/pool'; folder.mkdir(parents=True)
    (folder/'tools').symlink_to(outside,target_is_directory=True)
    result=obs.run_once(str(root),consumers=False)
    assert result['records']==0 and rows(root,'obs_source')==[]
    assert rows(root,'obs_issue')[0]['code']=='pool-path-refused'
    from alpaca import pool
    with pytest.raises(ValueError,match='symlink'):
        pool.record(str(root),'s','pre',{'tool_name':'read'})
    assert (outside/'s.jsonl').read_text()=='{"type":"user"}\n'


def test_health_reports_ingested_observations_not_yet_queued_for_preservation(tmp_path):
    path,sid=source(tmp_path,'{"type":"user"}\n')
    obs.run_once(str(tmp_path),consumers=False)
    state=obs.status(str(tmp_path))
    assert state['backlog']['bytes']==0
    assert state['backlog']['observations']==1
    assert state['status']!='ok'


def test_oversize_record_is_quarantined_without_losing_following_record(tmp_path,monkeypatch):
    monkeypatch.setattr(obs,'MAX_LINE',64)
    path,sid=source(tmp_path,json.dumps({'text':'x'*500})+'\n{"type":"user"}\n')
    obs.run_once(str(tmp_path),consumers=False)
    records=rows(tmp_path,'obs_observation')
    assert [r['kind'] for r in records]==['quarantined','user']
    assert rows(tmp_path,'obs_cursor')[0]['byte_offset']==path.stat().st_size
    assert json.loads(records[0]['facts'])['reason']=='oversize-line'


def test_the_profile_decides_whether_new_rows_concern_it(tmp_path, monkeypatch):
    from alpaca.observability import consumers
    conn = db.connect(tmp_path)
    db.append_event(conn, session="probe", actor="a", kind="session-end")
    fake = _use(monkeypatch, _Refresh(None))
    monkeypatch.setattr(consumers, "CONSUMERS", {"profile": consumers._profile})
    result = consumers.consume(tmp_path, conn)
    assert result["profile"]["status"] == "ok"
    assert result["profile"]["through_event"] == 1
    assert fake.calls == [("observability-collector", ["session-end"])]
    conn.close()


def test_verified_unchanged_source_does_not_commit_cursor_on_stat_change(tmp_path):
    import os
    path, sid = source(tmp_path, '{"type":"user"}\n')
    obs.run_once(tmp_path, consumers=False)
    conn = db.connect(tmp_path)
    spec = obs._sources(conn)[0]
    old = path.stat()
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000))
    before = conn.total_changes
    assert obs._collect_source(conn, spec, 200, tmp_path) == 0
    assert conn.total_changes == before
    # A real same-size content change must still produce a new generation.
    path.write_text('{"type":"xxxx"}\n')
    assert obs._collect_source(conn, spec, 200, tmp_path) == 1
    assert conn.execute('select max(generation) from obs_generation').fetchone()[0] == 2
    conn.close()


def test_marked_capture_routes_to_private_partition_and_remains_replayable(tmp_path):
    from alpaca.wiki.config import Config
    from alpaca.wiki.store.db import DB
    from alpaca.wiki.ingest import drain
    root = str(tmp_path)
    conn = db.connect(root)
    db.append_event(conn, session='s', actor='test', kind='note', data={'note': 'NOFORN synthetic routing fixture'})
    event = dict(conn.execute('SELECT * FROM events ORDER BY id DESC LIMIT 1').fetchone())
    conn.close()
    first = drain.capture(root, [event])
    assert first['docs'][0].startswith('wiki/private/raw/')
    assert (tmp_path/'.alpaca/wiki'/first['docs'][0]).is_file()
    assert not (tmp_path/'.alpaca/wiki/raw/unassigned'/('%06d-note.md' % event['id'])).exists()
    second = drain.capture(root, [event])
    assert second['skipped'] == 1
    vault = DB(Config.for_project(root))
    try:
        docs = [dict(x) for x in vault.conn.execute('SELECT path,private FROM docs')]
        assert docs and all(x['private'] == 1 and x['path'].startswith('wiki/private/') for x in docs)
    finally:
        vault.close()


def test_event_replay_does_not_rescan_unchanged_transcripts_but_native_growth_is_preserved(tmp_path, monkeypatch):
    from alpaca.wiki.ingest import drain
    native = tmp_path/'native.jsonl'
    native.write_text(json.dumps({'type':'user','message':{'content':'First visible message'}})+'\n')
    obs.expect_source(str(tmp_path),'s','claude',locator=str(native))
    assert obs.run_once(str(tmp_path),max_records=50)['preservation']['completed'] == 1
    calls = []
    real = drain._visible_transcript
    monkeypatch.setattr(drain,'_visible_transcript',lambda *a: calls.append(a[0]) or real(*a))
    conn = db.connect(str(tmp_path))
    db.append_event(conn,session='s',actor='test',kind='heartbeat',data={})
    conn.close()
    result = obs.run_once(str(tmp_path),max_records=50)
    assert calls == [], 'operation replay must not re-read unchanged native content'
    assert result['consumers']['wiki']['status'] == 'ok'
    with native.open('a') as out:
        out.write(json.dumps({'type':'user','message':{'content':'New visible message'}})+'\n')
    result = obs.run_once(str(tmp_path),max_records=50)
    assert calls == ['s'] and result['preservation']['completed'] == 1
    assert any('New visible message' in x.read_text() for x in (tmp_path/'.alpaca/wiki/raw/transcripts').rglob('*.md'))


def test_explicit_catchup_batch_reads_available_sources_without_duplicates(tmp_path):
    for sid in ('first','second'):
        p=tmp_path/(sid+'.jsonl')
        p.write_text(''.join(json.dumps({'type':'user','id':sid+'-'+str(i)})+'\n' for i in range(1200)))
        obs.expect_source(str(tmp_path),sid,'codex',locator=str(p))
    result=obs.run_once(str(tmp_path),max_records=5000,source_batch=2000,consumers=False)
    assert result['records']==2400 and result['errors']==0
    assert len(rows(tmp_path,'obs_observation'))==2400
    result=obs.run_once(str(tmp_path),max_records=5000,source_batch=2000,consumers=False)
    assert result['records']==0 and len(rows(tmp_path,'obs_observation'))==2400


def test_marked_transcript_snapshot_completes_in_private_partition(tmp_path):
    p=tmp_path/'native.jsonl'
    p.write_text(json.dumps({'type':'user','message':{'content':'NOFORN synthetic transcript fixture'}})+'\n')
    obs.expect_source(str(tmp_path),'s','claude',locator=str(p))
    result=obs.run_once(str(tmp_path),max_records=50)
    assert result['preservation']=={'completed':1,'failed':0}
    copies=list((tmp_path/'.alpaca/wiki/wiki/private/raw/transcripts').rglob('*.md'))
    assert len(copies)==1 and 'synthetic transcript fixture' in copies[0].read_text()
    assert not list((tmp_path/'.alpaca/wiki/raw/transcripts').rglob('*.md'))


@pytest.mark.parametrize('text',['Normal visible fixture','NOFORN synthetic private fixture'])
def test_cached_projection_is_repaired_if_retained_file_disappears(tmp_path,monkeypatch,text):
    from alpaca.observability import consumers
    from alpaca.wiki.ingest import drain
    p=tmp_path/'native.jsonl';p.write_text(json.dumps({'type':'user','message':{'content':text}})+'\n')
    obs.expect_source(str(tmp_path),'s','claude',locator=str(p))
    first=consumers._capture_wiki(str(tmp_path),[],['s'])
    target=tmp_path/'.alpaca/wiki'/first['docs'][0]
    assert target.is_file()
    target.unlink()
    calls=[];real=drain._visible_transcript
    monkeypatch.setattr(drain,'_visible_transcript',lambda *a:calls.append(a[0]) or real(*a))
    consumers._capture_wiki(str(tmp_path),[],['s'])
    assert calls==['s'] and target.is_file()


def test_cached_capture_cannot_hide_new_malformed_source(tmp_path):
    from alpaca.observability import consumers
    p=tmp_path/'native.jsonl';p.write_text('{"type":"user"}\n')
    obs.expect_source(str(tmp_path),'s','claude',locator=str(p))
    conn=db.connect(str(tmp_path))
    db.append_event(conn,session='s',actor='test',kind='heartbeat',data={})
    first=consumers.consume(str(tmp_path),conn)
    assert first['wiki']['status']=='ok'
    with p.open('a') as out:out.write('malformed\n')
    db.append_event(conn,session='s',actor='test',kind='heartbeat',data={})
    failed=consumers.consume(str(tmp_path),conn)
    assert failed['wiki']['status']=='failed'
    assert failed['wiki']['through_event']==first['wiki']['through_event']
    conn.close()


def test_summary_counts_what_status_lists(tmp_path):
    """t-590: the hub's vitals poll reads summary(); its counts and overall status match status()."""
    path, _ = source(tmp_path, '{"type":"user"}\n')
    child = tmp_path / 'child.jsonl'; child.write_text('{"type":"user"}\n' * 3)
    obs.expect_source(str(tmp_path), 'kid', 'codex', locator=str(child), parent_session='s', native_id='kid',
                      capabilities={'child_inventory_complete': True}, closed=True)
    obs.expect_source(str(tmp_path), 'lost', 'codex', parent_session='s', native_id='lost')
    obs.expect_source(str(tmp_path), 'gone', 'codex', locator=str(tmp_path / 'gone.jsonl'), parent_session='s')
    obs.expect_source(str(tmp_path), 'extra', 'codex', locator=str(path), required=False)
    conn = db.connect(str(tmp_path))
    obs.issue(conn, 'source-unreadable', source_id='x', detail='PermissionError')
    conn.close()
    full, slim = obs.status(str(tmp_path)), obs.summary(str(tmp_path))
    cov = full['coverage']
    assert slim['coverage'] == {'expected': cov['expected'], 'registered': cov['registered'], 'readable': cov['readable'],
                                'missing_children': len(cov['missing_children']),
                                'unknown_children': len(cov['unknown_children']), 'complete': cov['complete']}
    assert slim['coverage']['missing_children'] == 2 and slim['coverage']['readable'] == 2
    assert slim['backlog'] == full['backlog'] and slim['backlog']['bytes'] > 0
    assert slim['status'] == full['status'] and slim['collector'] == full['collector']
    assert slim['issues'] == {'open': 1, 'last_seen': [i['last_seen'] for i in full['issues']]}
    assert 'sources' not in slim


def test_summary_of_an_empty_record(tmp_path):
    db.connect(str(tmp_path)).close()
    slim = obs.summary(str(tmp_path))
    assert slim['status'] == 'degraded' and slim['issues'] == {'open': 0, 'last_seen': []}
    assert slim['coverage'] == {'expected': 0, 'registered': 0, 'readable': 0, 'missing_children': 0,
                                'unknown_children': 0, 'complete': False}


def test_snapshot_job_for_a_gone_transcript_closes_without_new_events(tmp_path):
    # t-606: 976 jobs for pruned subagent files retried every 15 minutes for days, each retry
    # appending a drain-failed. After GONE_AFTER_FAILURES tries the job closes, quietly.
    from alpaca.observability import consumers
    conn = db.connect(str(tmp_path))
    gone = tmp_path/'gone.jsonl'
    obs.expect_source(str(tmp_path),'gone','codex',locator=str(gone))
    consumers.queue_snapshot(conn,'gone',event_id=1)
    for _ in range(consumers.GONE_AFTER_FAILURES):
        conn.execute('UPDATE obs_projection_job SET next_attempt=NULL')
        assert consumers.run_snapshot_jobs(str(tmp_path),conn) == {'completed':0,'failed':1}
    events = len(rows(tmp_path,'events'))
    conn.execute('UPDATE obs_projection_job SET next_attempt=NULL')
    assert consumers.run_snapshot_jobs(str(tmp_path),conn) == {'completed':0,'failed':0,'gone':1}
    job, = rows(tmp_path,'obs_projection_job')
    assert job['status'] == 'done' and job['error'] == 'TranscriptGone'
    assert consumers.run_snapshot_jobs(str(tmp_path),conn) == {'completed':0,'failed':0}
    assert len(rows(tmp_path,'events')) == events
    assert [i['code'] for i in rows(tmp_path,'obs_issue') if i['resolved_at'] is None] == ['transcript-gone']
    # a source that comes back is queued again and preserved
    gone.write_text('{"type":"user"}\n')
    consumers.queue_snapshot(conn,'gone',event_id=2)
    assert consumers.run_snapshot_jobs(str(tmp_path),conn) == {'completed':1,'failed':0}
    # the snapshot taken again closes the transcript-gone gap (t-606)
    job, = rows(tmp_path,'obs_projection_job')
    assert job['status'] == 'done' and job['failures'] == 0 and job['error'] is None
    assert [i['code'] for i in rows(tmp_path,'obs_issue') if i['resolved_at'] is None] == []
    conn.close()


def _writes(conn):
    """The write statements `conn` runs from now on (sqlite3 trace callback)."""
    seen = []
    conn.set_trace_callback(lambda sql: seen.append(sql) if sql.lstrip().split(None, 1)[0].upper()
                            in ('INSERT', 'UPDATE', 'DELETE', 'REPLACE') else None)
    return seen


def test_open_issue_lookup_has_an_index_in_new_and_existing_records(tmp_path):
    """t-679: every poll of every source resolves its open issues by (source_id, code). Without an
    index each poll scanned the whole obs_issue table twice; on the live record (10,000 sources and
    10,600 issues every 2 s) that was two thirds of the collector's CPU."""
    from alpaca import migrate
    conn = db.connect(str(tmp_path))
    plan = lambda: ' '.join(str(r[3]) for r in conn.execute(
        'EXPLAIN QUERY PLAN SELECT 1 FROM obs_issue WHERE source_id=? AND code IN (?,?) '
        'AND resolved_at IS NULL LIMIT 1', ('s', 'a', 'b')))
    assert 'idx_obs_issue_open' in plan(), plan()
    conn.execute('DROP INDEX idx_obs_issue_open')
    conn.close()
    # An existing record at the current version gets it on its next open for write.
    conn = db.connect(str(tmp_path))
    assert 'idx_obs_issue_open' in plan(), plan()
    before = conn.total_changes
    migrate.apply(conn)
    assert conn.total_changes == before
    conn.close()


def test_a_poll_with_no_open_issue_writes_nothing_to_the_issue_table(tmp_path):
    """t-679: an unchanged source is polled every pass. With no open issue to close, the poll runs
    no UPDATE (each one was a write transaction of its own, 8,000 per pass on the live record)."""
    path, sid = source(tmp_path, '{"type":"user"}\n')
    obs.run_once(str(tmp_path), consumers=False)
    conn = db.connect(str(tmp_path))
    spec = obs._sources(conn)[0]
    writes = _writes(conn)
    assert obs._collect_source(conn, spec, 200, str(tmp_path)) == 0
    assert writes == []
    # New lines: the batch is written, but no issue row is touched.
    with path.open('a') as f:
        f.write('{"type":"assistant"}\n')
    assert obs._collect_source(conn, spec, 200, str(tmp_path)) == 1
    assert not [sql for sql in writes if 'obs_issue' in sql], writes
    conn.close()


def test_a_poll_closes_only_its_own_open_issues(tmp_path):
    """t-679: the cheaper check still closes an open issue of the polled source on both paths
    (unchanged and new lines), and leaves another source's issue and other codes open."""
    path, sid = source(tmp_path, '{"type":"user"}\n')
    obs.run_once(str(tmp_path), consumers=False)
    conn = db.connect(str(tmp_path))
    spec = obs._sources(conn)[0]
    obs.issue(conn, 'source-unreadable', source_id=sid, detail='PermissionError')
    obs.issue(conn, 'collector-error', source_id='other', detail='ValueError')
    obs.issue(conn, 'malformed-json', source_id=sid, detail='line=1', identity=1)
    assert obs._collect_source(conn, spec, 200, str(tmp_path)) == 0
    open_ = lambda: {(i['source_id'], i['code']) for i in rows(tmp_path, 'obs_issue') if i['resolved_at'] is None}
    assert open_() == {('other', 'collector-error'), (sid, 'malformed-json')}
    obs.issue(conn, 'collector-error', source_id=sid, detail='ValueError')
    with path.open('a') as f:
        f.write('{"type":"assistant"}\n')
    assert obs._collect_source(conn, spec, 200, str(tmp_path)) == 1
    assert open_() == {('other', 'collector-error'), (sid, 'malformed-json')}
    conn.close()


def test_a_verified_stat_change_is_not_verified_again_on_every_poll(tmp_path, monkeypatch):
    """t-679: a source whose stat changed with no new content (a new link, a touch, or lines that
    landed between the fstat and the read of the pass that took them) was read and hashed whole on
    every poll, since the stored stat hint is not rewritten for a poll that found nothing (no write
    per poll). On the live record 714 such sources, 755 MB, were hashed every 2 s. The watcher now
    remembers the verified stat in memory: the next poll of the same stat takes the fast path, still
    without a write, and a real change after it is still found."""
    import os
    path, sid = source(tmp_path, '{"type":"user"}\n')
    obs.run_once(tmp_path, consumers=False)
    conn = db.connect(tmp_path)
    spec = obs._sources(conn)[0]
    old = path.stat()
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000))
    reads = []
    real_prefix = obs._prefix
    monkeypatch.setattr(obs, '_prefix', lambda fh, length: reads.append(length) or real_prefix(fh, length))
    writes = _writes(conn)
    assert obs._collect_source(conn, spec, 200, tmp_path) == 0
    assert reads                                   # the first poll after the change verifies
    reads.clear()
    assert obs._collect_source(conn, spec, 200, tmp_path) == 0
    assert obs._collect_source(conn, spec, 200, tmp_path) == 0
    assert reads == [] and writes == []
    # A same-size content change has a new stat: verified again, and a new generation.
    path.write_text('{"type":"xxxx"}\n')
    assert obs._collect_source(conn, spec, 200, tmp_path) == 1
    assert conn.execute('select max(generation) from obs_generation').fetchone()[0] == 2
    conn.close()


def test_a_pass_reads_backoffs_and_the_source_list_once(tmp_path, monkeypatch):
    """t-679: a pass over the live record (10,000 sources, 5,850 of them in a retry backoff for a
    missing file) read each source's backoff with a query of its own and parsed the whole source
    list three times. The backoffs and the list are read once per pass; a source in backoff is
    still skipped, and one past its backoff still retried."""
    import datetime as dt
    present, sid = source(tmp_path, '{"type":"user"}\n')
    for n in range(40):
        obs.expect_source(str(tmp_path), 'gone%d' % n, 'codex', locator=str(tmp_path / ('gone%d.jsonl' % n)))
    first = obs.run_once(str(tmp_path), consumers=False)
    assert first['errors'] == 40 and first['records'] == 1
    statements = []
    real_connect = obs.db.connect

    def traced(root):
        conn = real_connect(root)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(obs.db, 'connect', traced)
    second = obs.run_once(str(tmp_path), consumers=False)
    assert second['errors'] == 0 and second['sources'] == 1          # all 40 still in backoff
    assert len([s for s in statements if 'obs:retry:' in s]) <= 1, statements
    assert len([s for s in statements if 'FROM obs_source' in s and 'SELECT *' in s]) <= 1, statements
    # One whose backoff ran out is tried again.
    conn = real_connect(str(tmp_path))
    gone = obs.expect_source(str(tmp_path), 'gone0', 'codex')['source_id']
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
    db.meta_set(conn, 'obs:retry:' + gone, json.dumps({'failures': 1, 'after': past}))
    conn.close()
    third = obs.run_once(str(tmp_path), consumers=False)
    assert third['errors'] == 1 and third['sources'] == 2


def test_a_verified_unchanged_source_is_polled_with_one_stat(tmp_path):
    """t-679: once this process verified a source at its cursor, a poll of the same stat runs no
    query at all; new lines, a failed poll or another record file bring the full poll back."""
    path, sid = source(tmp_path, '{"type":"user"}\n')
    obs.run_once(str(tmp_path), consumers=False)
    conn = db.connect(str(tmp_path))
    spec = obs._sources(conn)[0]
    assert obs._collect_source(conn, spec, 200, str(tmp_path)) == 0      # verifies and remembers
    statements = []
    conn.set_trace_callback(statements.append)
    assert obs._collect_source(conn, spec, 200, str(tmp_path)) == 0
    assert statements == []
    with path.open('a') as f:
        f.write('{"type":"assistant"}\n')
    assert obs._collect_source(conn, spec, 200, str(tmp_path)) == 1
    conn.set_trace_callback(None)
    conn.close()
    # A failed poll forgets the source: its next good poll runs the full check and closes the issue.
    obs._collect_source(db.connect(str(tmp_path)), spec, 200, str(tmp_path))
    assert (str(tmp_path), sid) in obs._VERIFIED
    path.rename(tmp_path / 'away.jsonl')
    assert obs.run_once(str(tmp_path), consumers=False)['errors'] == 1
    assert (str(tmp_path), sid) not in obs._VERIFIED
    (tmp_path / 'away.jsonl').rename(path)
    conn = db.connect(str(tmp_path))
    conn.execute("DELETE FROM meta WHERE key LIKE 'obs:retry:%'")
    conn.close()
    assert obs.run_once(str(tmp_path), consumers=False)['errors'] == 0
    assert [i['resolved_at'] is not None for i in rows(tmp_path, 'obs_issue')] == [True]


def test_another_record_file_drops_the_verified_stats_of_its_root(tmp_path):
    """t-679: a record restored or replaced under a running collector may hold other cursors."""
    path, sid = source(tmp_path, '{"type":"user"}\n')
    obs.run_once(str(tmp_path), consumers=False)
    obs.run_once(str(tmp_path), consumers=False)
    assert (str(tmp_path), sid) in obs._VERIFIED
    record = tmp_path / '.alpaca' / 'alpaca.db'
    src = db.connect_readonly(str(tmp_path))
    import sqlite3
    copy = sqlite3.connect(str(tmp_path / 'copy.db'))
    src.backup(copy)
    copy.close(); src.close()
    for suffix in ('-wal', '-shm'):
        Path(str(record) + suffix).unlink(missing_ok=True)
    (tmp_path / 'copy.db').replace(record)
    obs._record_identity(str(tmp_path))
    assert (str(tmp_path), sid) not in obs._VERIFIED
