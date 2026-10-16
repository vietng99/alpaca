"""Native event and visible transcript capture through every supported entry point."""
import json
from pathlib import Path

import pytest

from alpaca import cli, db, decisions, messages, observability, ops, sort
from alpaca.observability import consumers
from alpaca.tests import proofkit
from alpaca.wiki.config import Config
from alpaca.wiki.ingest import drain
from alpaca.wiki.ingest.absorb import Absorber
from alpaca.wiki.store.db import open_db_readonly


def corpus(root):
    conn = open_db_readonly(Config.for_vault(drain.wiki_vault_dir(root)))
    try:
        return '\n'.join(row[0] for row in conn.execute("SELECT text FROM blocks WHERE status='active' ORDER BY doc_id,ordinal"))
    finally:
        conn.close()


def transcript(root, sid='s', assistant='VISIBLE_ASSISTANT'):
    path = Path(root, '.alpaca/transcripts', sid + '.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {'type': 'user', 'message': {'content': 'VISIBLE_USER asks for SQLite'}},
        {'type': 'assistant', 'message': {'content': [
            {'type': 'thinking', 'thinking': 'PRIVATE_THINKING'},
            {'type': 'text', 'text': assistant},
            {'type': 'tool_use', 'id': 'call1', 'name': 'check', 'input': {'query': 'VISIBLE_CALL'}}]}},
        {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'call1', 'content': 'VISIBLE_RESULT'}]}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
            'channel': 'analysis', 'content': [{'type': 'output_text', 'text': 'PRIVATE_ANALYSIS'}]}},
    ]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


def task(root):
    assert cli.main(['init']) == 0
    assert cli.main(['op', 'new', 'Repair capture', '--done-when', 'Visible evidence is searchable']) == 0
    conn = db.connect(root)
    tid = ops.add_task(conn, 'op-001', 'Capture the decision substance', 'Capture', phase='build', session='s')
    return conn, tid


def test_real_decision_retains_substance(project):
    conn = db.connect(project)
    decisions.record(conn, 'technical', 'CONTEXT_MARKER', ['OPTION_ONE', 'OPTION_TWO'],
                     'CHOICE_MARKER', 'CONSEQUENCE_MARKER', pointer='remote:decision-source',
                     session='s', root=project)
    drain.run(project, 's')
    text = corpus(project)
    for marker in ('CONTEXT_MARKER', 'OPTION_ONE', 'OPTION_TWO', 'CHOICE_MARKER',
                   'CONSEQUENCE_MARKER', 'remote:decision-source'):
        assert marker in text
    conn.close()


def test_real_task_completion_carries_result_and_proof(project):
    conn, tid = task(project)
    pointer = proofkit.seal_for(project, tid, conn=conn)
    assert cli.main(['--session', 's', 'task', 'move', tid, 'done', '--proof', pointer]) == 0
    drain.run(project, 's')
    text = corpus(project)
    assert pointer in text
    assert 'to: done' in text
    result = sort.run(project)
    assert result['paired'] == 1
    conn.close()


def test_native_result_message_resolves_task_operation(project):
    conn, tid = task(project)
    messages.post(conn, 'worker', tid, 'result', 'RESULT_MARKER', session='s', root=project)
    drain.run(project, 's')
    result = sort.run(project)
    assert result['paired'] == 1
    assert result['ops']['op-001']['paired'][0]['result_doc']
    conn.close()


def test_fresh_collector_captures_visible_transcript_and_matches_drain(project):
    conn = db.connect(project)
    path = transcript(project)
    observability.expect_source(project, 's', 'claude', locator=str(path))
    db.append_event(conn, session='s', actor='test', kind='session-checkpoint')
    result = consumers.consume(project, conn)
    assert result['wiki']['status'] == 'ok'
    text = corpus(project)
    for marker in ('VISIBLE_USER', 'VISIBLE_ASSISTANT', 'VISIBLE_CALL', 'VISIBLE_RESULT'):
        assert marker in text
    assert 'PRIVATE_THINKING' not in text and 'PRIVATE_ANALYSIS' not in text
    assert drain.run(project, 's')['ingested'] == 0
    assert corpus(project) == text
    conn.close()


def test_transcript_replacement_retires_old_searchable_version_and_survives_vault_replay(project):
    conn = db.connect(project)
    path = transcript(project, assistant='OBSOLETE_MARKER ' * 100)
    first = drain.run(project, 's', transcript_path=str(path))
    old = first['docs'][0]
    old_bytes = Path(drain.wiki_vault_dir(project), old).read_bytes()
    transcript(project, assistant='CURRENT_MARKER')
    second = drain.run(project, 's', transcript_path=str(path))
    assert second['docs'][0] != old
    assert 'OBSOLETE_MARKER' not in corpus(project)
    assert 'CURRENT_MARKER' in corpus(project)
    assert Path(drain.wiki_vault_dir(project), old).read_bytes() == old_bytes
    absorber = Absorber(Config.for_vault(drain.wiki_vault_dir(project)))
    try:
        absorber.absorb_vault()
    finally:
        absorber.close()
    assert 'OBSOLETE_MARKER' not in corpus(project)
    assert drain.run(project, 's')['ingested'] == 0
    conn.close()


def _gap_rows(root):
    conn = db.connect_readonly(root)
    try:
        issues = [dict(r) for r in conn.execute("SELECT * FROM obs_issue WHERE code='transcript-unavailable'")]
        jobs = {r['session']: dict(r) for r in conn.execute('SELECT * FROM obs_projection_job')}
        checkpoint = conn.execute("SELECT * FROM obs_consumer_checkpoint WHERE consumer='wiki'").fetchone()
        failed = [dict(r) for r in conn.execute("SELECT * FROM obs_issue WHERE code='consumer-failed' AND resolved_at IS NULL")]
        return issues, jobs, dict(checkpoint) if checkpoint else None, failed
    finally:
        conn.close()


def _transcript_docs(root, sid):
    import hashlib
    folder = Path(drain.wiki_vault_dir(root), 'raw/transcripts', hashlib.sha256(sid.encode()).hexdigest()[:16])
    return sorted(p.name for p in folder.glob('*.md')) if folder.is_dir() else []


def test_wiki_consumer_continues_past_missing_source_and_records_gap(project):
    conn = db.connect(project)
    transcript(project, 'first', assistant='FIRST_MARKER')
    transcript(project, 'last', assistant='LAST_MARKER')
    private = Path(project, 'PRIVATE_HOME_DIR', 'gone.jsonl')
    observability.expect_source(project, 'gone', 'claude', locator=str(private))
    db.append_event(conn, session='first', actor='test', kind='session-checkpoint')
    gone_event = db.append_event(conn, session='gone', actor='test', kind='heartbeat')
    last_event = db.append_event(conn, session='last', actor='test', kind='session-checkpoint')
    result = consumers.consume(project, conn)
    assert result['wiki'] == {'status': 'ok', 'through_event': last_event['id']}
    text = corpus(project)
    assert 'FIRST_MARKER' in text and 'LAST_MARKER' in text
    issues, jobs, checkpoint, failed = _gap_rows(project)
    assert checkpoint['event_id'] == last_event['id'] and failed == []
    assert [(i['code'], i['resolved_at']) for i in issues] == [('transcript-unavailable', None)]
    assert 'gone' in issues[0]['detail']
    assert jobs['gone']['status'] == 'pending' and jobs['gone']['event_id'] >= gone_event['id']
    assert _transcript_docs(project, 'gone') == []
    assert observability.coverage(project, 'gone')['complete'] is False
    # The gap never leaks the private locator, in the issue or the retry job.
    stored = json.dumps(issues) + json.dumps(list(jobs.values()))
    assert 'PRIVATE_HOME_DIR' not in stored and str(private) not in stored
    conn.close()


def test_wiki_gap_replay_is_idempotent(project):
    conn = db.connect(project)
    observability.expect_source(project, 'gone', 'claude', locator=str(Path(project, 'absent.jsonl')))
    db.append_event(conn, session='gone', actor='test', kind='heartbeat')
    assert consumers.consume(project, conn)['wiki']['status'] == 'ok'
    db.append_event(conn, session='gone', actor='test', kind='heartbeat')
    second = db.append_event(conn, session='gone', actor='test', kind='heartbeat')
    assert consumers.consume(project, conn)['wiki'] == {'status': 'ok', 'through_event': second['id']}
    issues, jobs, checkpoint, failed = _gap_rows(project)
    assert len(issues) == 1 and issues[0]['occurrences'] == 2 and issues[0]['resolved_at'] is None
    assert list(jobs) == ['gone'] and jobs['gone']['event_id'] == second['id']
    assert failed == [] and _transcript_docs(project, 'gone') == []
    conn.close()


def test_capture_wiki_default_still_raises_for_missing_source(project):
    observability.expect_source(project, 'gone', 'claude', locator=str(Path(project, 'absent.jsonl')))
    with pytest.raises(drain.TranscriptUnavailable):
        consumers._capture_wiki(project, [], ['gone'])


@pytest.mark.parametrize('shape', ['directory', 'dangling-link', 'unreadable-file', 'unsearchable-parent'])
def test_present_but_broken_source_still_stops_wiki_consumer(project, shape):
    import os
    conn = db.connect(project)
    broken = Path(project, 'locked', 'broken.jsonl') if shape == 'unsearchable-parent' else Path(project, 'broken.jsonl')
    # Register first: registration stores the resolved locator, and the shape appears at it later.
    observability.expect_source(project, 'odd', 'claude', locator=str(broken))
    if shape == 'directory':
        broken.mkdir()
    elif shape == 'dangling-link':
        broken.symlink_to(Path(project, 'nowhere.jsonl'))
    elif shape == 'unsearchable-parent':
        # A real file in a folder the collector cannot search: lstat fails with EACCES, not
        # ENOENT, so the source is present but broken, never an absent gap.
        broken.parent.mkdir()
        broken.write_text('{"type":"user"}\n')
        broken.parent.chmod(0)
        if os.access(broken.parent, os.X_OK):
            broken.parent.chmod(0o755)
            pytest.skip('running with privileges that ignore file modes')
    else:
        broken.write_text('{"type":"user"}\n')
        broken.chmod(0)
        if os.access(broken, os.R_OK):
            pytest.skip('running with privileges that ignore file modes')
    db.append_event(conn, session='odd', actor='test', kind='session-checkpoint')
    try:
        result = consumers.consume(project, conn)
    finally:
        if shape == 'unsearchable-parent':
            broken.parent.chmod(0o755)
    assert result['wiki']['status'] == 'failed' and result['wiki']['through_event'] == 0
    issues, jobs, checkpoint, failed = _gap_rows(project)
    assert issues == [] and len(failed) == 1
    conn.close()


def test_registered_missing_transcript_is_a_retained_gap_cleared_by_restore(project):
    conn = db.connect(project)
    path = Path(project, 'missing.jsonl')
    observability.expect_source(project, 's', 'claude', locator=str(path))
    event = db.append_event(conn, session='s', actor='test', kind='session-checkpoint')
    result = consumers.consume(project, conn)
    assert result['wiki'] == {'status': 'ok', 'through_event': event['id']}
    assert consumers.run_snapshot_jobs(project, conn) == {'completed': 0, 'failed': 1}
    issues, jobs, checkpoint, failed = _gap_rows(project)
    assert issues[0]['resolved_at'] is None and jobs['s']['status'] == 'retry'
    path.write_text(json.dumps({'type': 'assistant', 'message': {'content': 'RECOVERED_MARKER'}}) + '\n')
    conn.execute('UPDATE obs_projection_job SET next_attempt=NULL')
    assert consumers.run_snapshot_jobs(project, conn) == {'completed': 1, 'failed': 0}
    assert 'RECOVERED_MARKER' in corpus(project)
    issues, jobs, checkpoint, failed = _gap_rows(project)
    assert issues[0]['resolved_at'] is not None and jobs['s']['status'] == 'done'
    assert _transcript_docs(project, 's') != []
    conn.close()


def test_repeated_issue_updates_its_detail(project):
    conn = db.connect(project)
    with db.transaction(conn):
        observability.issue(conn, 'consumer-failed', detail='wiki: PrivatePathViolation', identity='wiki')
    with db.transaction(conn):
        observability.issue(conn, 'consumer-failed', detail='wiki: TranscriptUnavailable', identity='wiki')
    row = conn.execute("SELECT detail, occurrences FROM obs_issue WHERE code='consumer-failed'").fetchone()
    assert tuple(row) == ('wiki: TranscriptUnavailable', 2)
    conn.close()


def test_collector_transcript_updates_without_new_events(project):
    conn = db.connect(project)
    path = transcript(project)
    observability.expect_source(project, 's', 'claude', locator=str(path))
    observability.run_once(project)
    transcript(project, assistant='LATE_ASSISTANT')
    observability.run_once(project)
    assert 'LATE_ASSISTANT' in corpus(project)
    assert 'VISIBLE_ASSISTANT' not in corpus(project)
    conn.close()


def test_recovery_replaces_legacy_native_result_projection(project):
    from alpaca.wiki import recovery
    conn, tid = task(project)
    messages.post(conn, 'worker', tid, 'result', 'RESULT_MARKER', pointer=tid, session='s', root=project)
    event = db.events(conn, session='s')[-1]
    vault = Path(drain.wiki_vault_dir(project))
    vault.mkdir(parents=True, exist_ok=True)
    legacy_id = 'raw/unassigned/%06d-note.md' % event['id']
    legacy_text = ('op: unassigned\nrole: note\nkey: %s\nkind: msg\nevent: %s\nts: %s\n\n'
                   'msg during session s. RESULT_MARKER\n') % (tid, event['id'], event['ts'])
    old = vault / legacy_id
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(legacy_text)
    absorber = Absorber(Config.for_vault(str(vault)))
    try:
        absorber.absorb_text(legacy_id, legacy_text, kind='raw')
    finally:
        absorber.close()
    recovery.run(project, session='repair')
    assert corpus(project).count('RESULT_MARKER') == 1
    assert sort.run(project)['paired'] == 1
    assert old.read_text() == legacy_text
    conn.close()


def test_recovery_verifies_transcript_bytes_and_searchable_projection(project, monkeypatch):
    from alpaca.wiki import recovery
    conn = db.connect(project)
    path = transcript(project)
    observability.expect_source(project, 's', 'claude', locator=str(path))
    original = drain.run

    def corrupt(root, sid, **kwargs):
        result = original(root, sid, **kwargs)
        for doc_id in result['docs']:
            if 'transcript-' in doc_id:
                kwargs['absorber'].db.conn.execute(
                    "UPDATE blocks SET text='damaged transcript' WHERE doc_id=?", (doc_id,))
                kwargs['absorber'].db.conn.commit()
        return result

    monkeypatch.setattr(drain, 'run', corrupt)
    with pytest.raises(RuntimeError, match='verification'):
        recovery.run(project, session='repair')
    assert db.events(conn, kind='wiki-recovered') == []
    conn.close()


def test_sort_replaces_assertions_for_retired_projection(project):
    conn, tid = task(project)
    messages.post(conn, 'worker', tid, 'claim', 'CLAIM_MARKER', pointer=tid, session='s', root=project)
    messages.post(conn, 'worker', tid, 'result', 'RESULT_MARKER', pointer=tid, session='s', root=project)
    drain.run(project, 's')
    sort.run(project)
    wiki = Absorber(Config.for_vault(drain.wiki_vault_dir(project)))
    try:
        wiki.db.conn.execute("INSERT INTO sort_assertion (assertion_id,op,intent_doc,status) VALUES ('obsolete','unassigned','raw/old-intent.md','unpaired')")
        wiki.db.conn.commit()
    finally:
        wiki.close()
    result = sort.run(project)
    wiki = open_db_readonly(Config.for_vault(drain.wiki_vault_dir(project)))
    try:
        assert wiki.execute('SELECT count(*) FROM sort_assertion').fetchone()[0] == len(result['assertions'])
    finally:
        wiki.close()
        conn.close()


def test_snapshot_job_retries_failed_wiki_projection_without_new_events(project, monkeypatch):
    conn = db.connect(project)
    path = transcript(project)
    observability.expect_source(project, 's', 'claude', locator=str(path))
    consumers.queue_snapshot(conn, 's')
    with monkeypatch.context() as patch:
        def unavailable(*args, **kwargs):
            raise OSError('temporary index failure')
        patch.setattr(drain, 'capture', unavailable)
        assert consumers.run_snapshot_jobs(project, conn) == {'completed': 0, 'failed': 1}
    conn.execute('UPDATE obs_projection_job SET next_attempt=NULL')
    assert consumers.run_snapshot_jobs(project, conn) == {'completed': 1, 'failed': 0}
    assert 'VISIBLE_ASSISTANT' in corpus(project)
    conn.close()


def test_malformed_registered_transcript_fails_before_capture_checkpoint(project):
    conn = db.connect(project)
    path = transcript(project)
    with path.open('a') as stream:
        stream.write('malformed record\n')
    observability.expect_source(project, 's', 'claude', locator=str(path))
    db.append_event(conn, session='s', actor='test', kind='session-checkpoint')
    result = consumers.consume(project, conn)
    assert result['wiki']['status'] == 'failed'
    assert result['wiki']['through_event'] == 0
    conn.close()


def _torn(project, sid):
    """A native transcript as the live store held it on 2026-10-11 (t-684): a record cut part way,
    the next record appended on the same line, and the session going on after it, so the torn line
    ends with a newline and no later read heals it."""
    path = Path(project, 'native', sid + '.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    whole = json.dumps({'type': 'assistant', 'message': {'content': 'TORN_' + 'x' * 200}})
    rows = [json.dumps({'type': 'user', 'message': {'content': 'BEFORE_TEAR'}}),
            whole[:90] + json.dumps({'type': 'user', 'message': {'content': 'GLUED'}}),
            json.dumps({'type': 'user', 'message': {'content': 'AFTER_TEAR'}})]
    path.write_text(''.join(row + '\n' for row in rows))
    observability.expect_source(project, sid, 'claude', locator=str(path))
    return path


def _open(root, code):
    conn = db.connect_readonly(root)
    try:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM obs_issue WHERE code=? AND resolved_at IS NULL', (code,))]
    finally:
        conn.close()


def _retry_now(conn, failures):
    conn.execute("UPDATE obs_consumer_checkpoint SET failures=?, next_attempt=NULL WHERE consumer='wiki'",
                 (failures,))


def test_a_torn_transcript_is_handed_to_its_snapshot_job_after_the_retries(project):
    # t-684: a torn record in the middle of a transcript never reads clean again. The consumer fails
    # on it, as merge decision D2 asks, for `MALFORMED_AFTER_FAILURES` tries; then the session goes
    # to its own snapshot job with an open `transcript-malformed` issue and the consumer advances,
    # where it stalled for 3 days (293 failures at event 238470) with five such transcripts.
    conn = db.connect(project)
    transcript(project, 'good', assistant='GOOD_MARKER')
    torn = _torn(project, 'torn')
    db.append_event(conn, session='torn', actor='test', kind='session-checkpoint')
    db.append_event(conn, session='good', actor='test', kind='session-checkpoint')
    first = consumers.consume(project, conn)
    assert first['wiki'] == {'status': 'failed', 'error': 'TranscriptMalformed', 'through_event': 0}
    assert 'GOOD_MARKER' in corpus(project)              # the rest of the batch landed all the same
    assert _open(project, 'transcript-malformed') == []
    for tries in range(2, consumers.MALFORMED_AFTER_FAILURES + 1):
        _retry_now(conn, tries - 1)
        assert consumers.consume(project, conn)['wiki']['status'] == 'failed'
    _retry_now(conn, consumers.MALFORMED_AFTER_FAILURES)
    last = db.append_event(conn, session='good', actor='test', kind='heartbeat')
    handed = consumers.consume(project, conn)
    assert handed['wiki'] == {'status': 'ok', 'through_event': last['id']}
    issue, = _open(project, 'transcript-malformed')
    assert 'torn' in issue['detail'] and str(torn) not in issue['detail']
    assert _open(project, 'consumer-failed') == []
    issues, jobs, checkpoint, failed = _gap_rows(project)
    assert jobs['torn']['status'] == 'pending' and checkpoint['failures'] == 0
    assert _transcript_docs(project, 'torn') == []       # nothing of a torn transcript lands
    # A later event of the session does not stop the consumer again: its snapshot job owns it.
    later = db.append_event(conn, session='torn', actor='test', kind='heartbeat')
    assert consumers.consume(project, conn)['wiki'] == {'status': 'ok', 'through_event': later['id']}
    # The job fails loudly on its own; a transcript that reads clean again lands and closes both.
    conn.execute('UPDATE obs_projection_job SET next_attempt=NULL')
    assert consumers.run_snapshot_jobs(project, conn)['failed'] == 1
    torn.write_text(json.dumps({'type': 'assistant', 'message': {'content': 'MENDED_MARKER'}}) + '\n')
    conn.execute('UPDATE obs_projection_job SET next_attempt=NULL')
    assert consumers.run_snapshot_jobs(project, conn)['completed'] == 1
    assert 'MENDED_MARKER' in corpus(project)
    assert _open(project, 'transcript-malformed') == [] and _open(project, 'snapshot-failed') == []
    conn.close()


def test_a_torn_transcript_is_not_handed_off_before_the_retries(project):
    conn = db.connect(project)
    _torn(project, 'torn')
    db.append_event(conn, session='torn', actor='test', kind='session-checkpoint')
    _retry_now(conn, consumers.MALFORMED_AFTER_FAILURES - 1)
    consumers.consume(project, conn)                     # seeds the checkpoint row on its first run
    _retry_now(conn, consumers.MALFORMED_AFTER_FAILURES - 2)
    result = consumers.consume(project, conn)
    assert result['wiki']['status'] == 'failed' and result['wiki']['through_event'] == 0
    assert _open(project, 'transcript-malformed') == []
    conn.close()


def test_a_broken_locator_still_stops_the_consumer_after_the_retries(project):
    # The hand-off is for a torn transcript only: a directory at the locator still fails the
    # consumer however many times it was tried (merge decision D2).
    conn = db.connect(project)
    broken = Path(project, 'broken.jsonl')
    observability.expect_source(project, 'odd', 'claude', locator=str(broken))
    broken.mkdir()
    db.append_event(conn, session='odd', actor='test', kind='session-checkpoint')
    assert consumers.consume(project, conn)['wiki']['status'] == 'failed'
    _retry_now(conn, consumers.MALFORMED_AFTER_FAILURES + 3)
    result = consumers.consume(project, conn)
    assert result['wiki'] == {'status': 'failed', 'error': 'TranscriptUnavailable', 'through_event': 0}
    assert _open(project, 'transcript-malformed') == [] and _open(project, 'transcript-unavailable') == []
    conn.close()


def test_a_transcript_gone_while_the_batch_reads_it_is_a_gap(project, monkeypatch):
    # A recorded gap never stops the consumer, also when the file goes between the consumer's check
    # and the read (subagent transcripts are pruned while the collector runs).
    conn = db.connect(project)
    native = Path(project, 'native', 'brief.jsonl')
    native.parent.mkdir()
    native.write_text(json.dumps({'type': 'user', 'message': {'content': 'BRIEF'}}) + '\n')
    observability.expect_source(project, 'brief', 'claude', locator=str(native))
    real = drain._visible_transcript

    def pruned(session, path, *args):
        if session == 'brief':
            native.unlink()
        return real(session, path, *args)
    monkeypatch.setattr(drain, '_visible_transcript', pruned)
    event = db.append_event(conn, session='brief', actor='test', kind='session-checkpoint')
    assert consumers.consume(project, conn)['wiki'] == {'status': 'ok', 'through_event': event['id']}
    gap, = _open(project, 'transcript-unavailable')
    assert 'brief' in gap['detail'] and str(native) not in gap['detail']
    conn.close()


def test_capture_preserves_large_visible_content_beyond_ui_budgets(project, monkeypatch):
    from alpaca.analytics import detail
    conn = db.connect(project)
    text = 'visible content ' * 20000 + ' END_OF_LARGE_VISIBLE_MESSAGE'
    path = transcript(project, assistant=text)
    # Both the real per-content cap and artificial small UI pagination/read limits
    # must be irrelevant to complete source capture.
    monkeypatch.setattr(detail, 'MAX_READ_BYTES', 1024)
    monkeypatch.setattr(detail, 'MAX_LINE_BYTES', 1024)
    monkeypatch.setattr(detail, 'MAX_RECORDS', 1)
    drain.run(project, 's', transcript_path=str(path))
    assert text in corpus(project)
    conn.close()


def test_registered_missing_source_uses_preserved_transcript(project):
    from alpaca import transcripts
    conn = db.connect(project)
    original = Path(project, 'external.jsonl')
    original.write_bytes(transcript(project).read_bytes())
    observability.expect_source(project, 's', 'claude', locator=str(original))
    transcripts.snapshot(project, 's')
    original.unlink()
    drain.run(project, 's')
    assert 'VISIBLE_ASSISTANT' in corpus(project)
    conn.close()


@pytest.mark.parametrize('target', ['directory', 'file'])
def test_capture_refuses_symlinked_projection_before_ingest(project, tmp_path, target):
    conn = db.connect(project)
    db.append_event(conn, session='s', actor='test', kind='result', data={'body': 'SYMLINK_MARKER'})
    vault = Path(drain.wiki_vault_dir(project))
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_text('preserved')
    if target == 'directory':
        vault.mkdir(parents=True, exist_ok=True)
        (vault / 'raw').symlink_to(outside, target_is_directory=True)
    else:
        (vault / 'raw/unassigned').mkdir(parents=True, exist_ok=True)
        (vault / 'raw/unassigned/000001-result.md').symlink_to(sentinel)
    with pytest.raises((ValueError, OSError)):
        drain.run(project, 's')
    assert sentinel.read_text() == 'preserved'
    assert list(outside.iterdir()) == [sentinel]
    assert 'SYMLINK_MARKER' not in corpus(project)
    conn.close()


def test_transcript_source_can_return_to_prior_content_without_duplicate_search_hits(project):
    conn = db.connect(project)
    path = transcript(project, assistant='FIRST_VERSION')
    drain.run(project, 's', transcript_path=str(path))
    transcript(project, assistant='SECOND_VERSION')
    drain.run(project, 's', transcript_path=str(path))
    transcript(project, assistant='FIRST_VERSION')
    drain.run(project, 's', transcript_path=str(path))
    text = corpus(project)
    assert text.count('FIRST_VERSION') == 1
    assert 'SECOND_VERSION' not in text
    assert drain.run(project, 's')['ingested'] == 0
    conn.close()


def test_recovery_verifies_captured_transcript_when_live_source_grows(project, monkeypatch):
    from alpaca.wiki import recovery
    conn = db.connect(project)
    path = transcript(project, assistant='BEFORE_CAPTURE')
    observability.expect_source(project, 's', 'claude', locator=str(path))
    original = drain.run

    def source_grows(root, sid, **kwargs):
        transcript(project, assistant='CAPTURED_VERSION')
        result = original(root, sid, **kwargs)
        transcript(project, assistant='AFTER_CAPTURE')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(drain, 'run', source_grows)
        result = recovery.run(project, session='repair')
    assert result['verified_transcripts'] == 1
    assert 'CAPTURED_VERSION' in corpus(project)
    assert 'AFTER_CAPTURE' not in corpus(project)
    assert result['transcript_sources'][0]['source_sha256']
    recovery.run(project, session='repair')
    assert 'AFTER_CAPTURE' in corpus(project)
    assert 'CAPTURED_VERSION' not in corpus(project)
    conn.close()


def test_visible_user_and_tool_secrets_are_redacted_before_indexing(project):
    conn = db.connect(project)
    path = transcript(project)
    rows = [
        {'type': 'custom-title', 'customTitle': 'Captured session title'},
        {'type': 'user', 'message': {'content': 'Use API_KEY=PRIVATE_USER_SECRET to check deployment'}},
        {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'secret-call', 'name': 'check',
             'input': {'api_key': 'PRIVATE_TOOL_SECRET', 'query': 'Visible check'}}]}},
    ]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = drain.run(project, 's', transcript_path=str(path))
    text = corpus(project)
    assert 'PRIVATE_USER_SECRET' not in text
    assert 'PRIVATE_TOOL_SECRET' not in text
    assert '[redacted]' in text
    assert 'Visible check' in text
    raw = Path(drain.wiki_vault_dir(project), result['docs'][0]).read_text()
    assert 'Captured session title' in raw
    conn.close()


def test_repeated_visible_user_messages_keep_each_occurrence(project):
    conn = db.connect(project)
    path = transcript(project)
    row = {'type': 'user', 'message': {'content': 'REPEATED_VISIBLE_PROMPT'}}
    path.write_text((json.dumps(row) + '\n') * 2)
    drain.run(project, 's', transcript_path=str(path))
    assert corpus(project).count('REPEATED_VISIBLE_PROMPT') == 2
    conn.close()


def test_recovery_reports_missing_transcripts_and_still_captures_remaining_sources(project):
    from alpaca.wiki import recovery
    conn = db.connect(project)
    for sid in ('first', 'missing', 'last'):
        db.append_event(conn, session=sid, actor='test', kind='result', data={'body': 'EVENT_' + sid})
        path = Path(project, '.alpaca/transcripts', sid + '.jsonl')
        if sid != 'missing':
            path = transcript(project, sid=sid, assistant='TRANSCRIPT_' + sid)
        observability.expect_source(project, sid, 'claude', locator=str(path))
    result = recovery.run(project, session='repair')
    assert result['status'] == 'incomplete'
    assert result['verified_events'] == 3
    assert result['verified_transcripts'] == 2
    assert [row['session'] for row in result['missing_transcripts']] == ['missing']
    text = corpus(project)
    for marker in ('EVENT_first', 'EVENT_missing', 'EVENT_last', 'TRANSCRIPT_first', 'TRANSCRIPT_last'):
        assert marker in text
    assert db.events(conn, kind='wiki-recovered') == []
    assert len(db.events(conn, kind='wiki-recovery-incomplete')) == 1
    conn.close()


def test_recovery_preserves_embedded_carriage_returns(project):
    from alpaca.wiki import recovery
    path = Path(project, 'windows-tool-output.jsonl')
    path.write_text(json.dumps({'type': 'assistant', 'message': {'content': [
        {'type': 'text', 'text': 'The tool returned line one\r\nline two.'}
    ]}}) + '\n')
    observability.expect_source(project, 'windows', 'claude', locator=str(path))
    result = recovery.run(project, session='recovery')
    assert result['status'] == 'ok'
    assert result['verified_transcripts'] == 1
