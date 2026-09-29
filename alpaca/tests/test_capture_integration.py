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
    tid = ops.add_task(conn, 'op-001', 'Capture the decision substance', 'Capture', session='s')
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


def test_registered_unreadable_transcript_keeps_consumer_checkpoint_for_retry(project):
    conn = db.connect(project)
    path = Path(project, 'missing.jsonl')
    observability.expect_source(project, 's', 'claude', locator=str(path))
    db.append_event(conn, session='s', actor='test', kind='session-checkpoint')
    result = consumers.consume(project, conn)
    assert result['wiki']['status'] == 'failed'
    assert result['wiki']['through_event'] == 0
    path.write_text(json.dumps({'type': 'assistant', 'message': {'content': 'RECOVERED_MARKER'}}) + '\n')
    conn.execute("UPDATE obs_consumer_checkpoint SET next_attempt=NULL WHERE consumer='wiki'")
    result = consumers.consume(project, conn)
    assert result['wiki']['status'] == 'ok'
    assert 'RECOVERED_MARKER' in corpus(project)
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
