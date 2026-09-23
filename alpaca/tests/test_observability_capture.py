"""Regression controls for audit F2/F3/F5/F6; synthetic provider data only."""
import json
import os
from pathlib import Path

import pytest

from alpaca import db, sessions_view, transcripts
from alpaca.analytics import build_index, detail, metrics, metrics_index, parse_session, usage


def write(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in records))
    return path


def register(root, path=None, sid='audit', operator='claude'):
    conn = db.connect(str(root))
    db.upsert(conn, 'sessions', 'sid', {'sid': sid, 'started': '2026-09-22T10:00:00Z',
                                     'transcript': str(path) if path else None})
    db.meta_set(conn, 'operator:' + sid, operator)
    conn.close()


def response(ident='r1', uuid='b1', output=10):
    return {'type': 'assistant', 'uuid': uuid, 'timestamp': '2026-09-22T10:00:00Z',
            'message': {'id': ident, 'model': 'claude-sonnet-5', 'usage': {
                'input_tokens': 20, 'output_tokens': output, 'cache_read_input_tokens': 5,
                'cache_creation_input_tokens': 0, 'cache_creation': {
                    'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0}},
                'content': [{'type': 'text', 'text': 'Visible answer'},
                            {'type': 'tool_use', 'id': 'c1', 'name': 'Read', 'input': {}}]}}


@pytest.mark.parametrize('append', [False, True])
def test_rewrite_outside_tail_never_mixes_generations(tmp_path, append):
    src, dst = tmp_path / 'source', tmp_path / 'copy'
    src.write_bytes(b'a' + b'x' * 9000)
    transcripts.copy_forward(src, dst)
    src.write_bytes(b'b' + b'x' * 9000 + (b'new' if append else b''))
    assert transcripts.copy_forward(src, dst) == 'rewrite'
    assert dst.read_bytes() == src.read_bytes()
    assert Path(str(dst) + '.1').read_bytes() == b'a' + b'x' * 9000


def test_append_publish_failure_preserves_original_and_retry(tmp_path, monkeypatch):
    src, dst = tmp_path / 'source', tmp_path / 'copy'
    src.write_bytes(b'first\n')
    transcripts.copy_forward(src, dst)
    src.write_bytes(b'first\nsecond\n')
    real = os.replace
    def fail(source, dest):
        if Path(dest) == dst:
            raise OSError('injected publication failure')
        return real(source, dest)
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError, match='publication'):
        transcripts.copy_forward(src, dst)
    assert dst.read_bytes() == b'first\n'
    monkeypatch.setattr(os, 'replace', real)
    assert transcripts.copy_forward(src, dst) == 'append'
    assert dst.read_bytes() == src.read_bytes()


def test_link_failure_does_not_remove_canonical_copy(tmp_path, monkeypatch):
    dst = tmp_path / 'copy'
    dst.write_bytes(b'preserve')
    monkeypatch.setattr(os, 'link', lambda *args: (_ for _ in ()).throw(OSError('no links')))
    kept = transcripts._keep(dst)
    assert dst.read_bytes() == b'preserve'
    assert Path(kept).read_bytes() == b'preserve'


def test_snapshot_filters_private_records_and_retries_partial_line(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    src = write(tmp_path / 'native.jsonl', [
        {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': ['PRIVATE']}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                                             'channel': 'analysis', 'content': 'PRIVATE'}},
        {'type': 'assistant', 'message': {'id': 'r1', 'content': [
            {'type': 'thinking', 'thinking': 'PRIVATE'}, {'type': 'text', 'text': 'Visible'}]}}])
    with src.open('ab') as out:
        out.write(b'{"type":"user","message":{"content":"unfinished')
    register(root, src)
    first = transcripts.snapshot(str(root), 'audit')
    raw = Path(transcripts.local_path(str(root), 'audit')).read_text()
    assert 'PRIVATE' not in raw and 'Visible' in raw and 'unfinished' not in raw
    assert first['partial_line'] is True
    with src.open('ab') as out:
        out.write(b'"}}\n')
    second = transcripts.snapshot(str(root), 'audit')
    assert second['generation'] == first['generation']
    assert 'unfinished' in Path(transcripts.local_path(str(root), 'audit')).read_text()


def test_scoped_codex_discovery_rejects_wrong_project_and_ambiguity(tmp_path):
    root, storage = tmp_path / 'project', tmp_path / 'sessions'
    root.mkdir()
    native = '01a0c976-06a2-7c03-8eef-96eed544e626'
    good = write(storage / '2026' / ('rollout-' + native + '.jsonl'), [
        {'type': 'session_meta', 'payload': {'id': native, 'cwd': str(root)}}])
    write(storage / ('foreign-' + native + '.jsonl'), [
        {'type': 'session_meta', 'payload': {'id': native, 'cwd': str(tmp_path / 'foreign')}}])
    assert transcripts.discover_codex(root, native, storage_root=storage) == str(good)
    assert transcripts.discover_codex(root, 'missing', storage_root=storage) is None
    with pytest.raises(ValueError):
        transcripts.discover_codex(root, '../escape', storage_root=storage)
    write(storage / ('duplicate-' + native + '.jsonl'), [
        {'type': 'session_meta', 'payload': {'id': native, 'cwd': str(root)}}])
    with pytest.raises(ValueError, match='ambiguous'):
        transcripts.discover_codex(root, native, storage_root=storage)


def test_canonical_legacy_response_tool_and_usage_agree(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    src = write(tmp_path / 'audit.jsonl', [response(), response(uuid='b2', output=15)])
    register(root, src)
    old = parse_session.parse(str(src))
    new = metrics.analyze(str(root), 'audit')
    assert old['turns'] == new['summary']['responses'] == 1
    assert old['tool_calls'] == new['summary']['tool_calls'] == 1
    assert old['tokens']['output'] == new['summary']['tokens']['output_tokens'] == 15
    assert old['tokens']['input'] == new['summary']['tokens']['input_tokens'] == 25
    assert old['cost_notional_usd'] == new['summary']['estimated_cost_usd'] == 0.000191


def test_imported_samples_do_not_replace_measured_native_usage(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    src = write(tmp_path / 'audit.jsonl', [response()])
    register(root, src)
    sample = tmp_path / 'usage.json'
    sample.write_text(json.dumps({'sample_id': 'manual', 'provider': 'anthropic', 'model': 'claude-sonnet-5',
        'tokens': {'input': 999, 'output': 888, 'cache_read': 0, 'cache_write_5m': 0, 'cache_write_1h': 0},
        'reported_cost_usd': 0.000123}))
    usage.ingest(str(root), 'audit', str(sample))
    old = build_index.collect(str(root), only='audit')[0]
    new = metrics.analyze(str(root), 'audit')
    assert old['tokens']['output'] == new['summary']['tokens']['output_tokens'] == 10
    assert old['cost_reported_usd'] == new['cost']['imported_reported_usd'] == 0.000123
    assert old['imported_usage']['tokens']['output'] == 888


def test_missing_sources_are_in_denominator_and_not_exact(tmp_path):
    register(tmp_path)
    result = metrics.analyze(str(tmp_path), 'audit')
    assert result['coverage']['total_is_exact'] is False
    value = metrics_index.index(str(tmp_path), [{'sid': 'audit', 'class': 'work'}])
    assert value['totals']['partial_sessions'] == 1
    assert value['totals']['listed_sessions'] == 1
    assert value['totals']['incomplete_project'] is True


def test_complete_file_does_not_imply_complete_session_or_children(tmp_path):
    src = write(tmp_path / 'audit.jsonl', [response()])
    register(tmp_path, src)
    result = metrics.analyze(str(tmp_path), 'audit')
    assert result['coverage']['file_complete'] is True
    assert result['coverage']['session_complete'] is False
    assert result['coverage']['child_coverage']['state'] == 'unknown'
    assert metrics_index.family(str(tmp_path), 'audit')['complete'] is False


def test_read_collection_does_not_create_record_or_cache(tmp_path):
    assert build_index.collect(str(tmp_path)) == []
    assert list(tmp_path.iterdir()) == []


def test_online_liveness_rejects_ended_frozen_and_future_heartbeats():
    info = {'class': 'work', 'last_beat': '2026-09-22T10:00:00Z', 'ended': '2026-09-22T10:00:01Z'}
    assert not sessions_view.is_active(info, '2026-09-22T10:00:02Z')
    info['ended'] = None
    assert not sessions_view.is_active(info, '2026-09-22T10:10:01Z')
    assert not sessions_view.is_active(info, '2026-09-22T09:59:00Z')
    assert sessions_view.is_active(info, '2026-09-22T10:00:02Z')
    assert isinstance(sessions_view.is_active(info), bool)


def test_conflicting_native_response_replay_is_visible_and_not_max_merged(tmp_path):
    def native(count):
        return {'type': 'token_usage_record', 'payload': {'response_id': 'r1', 'model': 'gpt-6-astra',
            'usage': {'input_tokens': count, 'output_tokens': 5, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0}}}
    src = write(tmp_path / 'audit.jsonl', [native(10), native(99), native(10)])
    register(tmp_path, src, operator='codex')
    result = metrics.analyze(str(tmp_path), 'audit')
    assert result['summary']['responses'] == 1
    assert result['summary']['tokens']['input_tokens'] == 10
    assert result['coverage']['conflicting_response_records'] == 1
    assert not result['coverage']['usage_total_is_exact']


def test_usage_reset_and_missing_prefix_make_lower_bound_explicit(tmp_path):
    def snap(count):
        tokens = {'input_tokens': count, 'output_tokens': count, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0}
        return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'total_token_usage': tokens, 'last_token_usage': tokens}}}
    src = write(tmp_path / 'audit.jsonl', [snap(10), snap(2)])
    register(tmp_path, src, operator='codex')
    result = metrics.analyze(str(tmp_path), 'audit')
    assert result['summary']['tokens']['output_tokens'] == 10
    assert result['coverage']['snapshot_resets'] == 1
    assert not result['coverage']['usage_total_is_exact']


def test_missing_cache_categories_remain_unknown_in_both_views(tmp_path):
    rec = response()
    rec['message']['usage'] = {'input_tokens': 10, 'output_tokens': 2}
    src = write(tmp_path / 'audit.jsonl', [rec])
    register(tmp_path, src)
    old, new = parse_session.parse(str(src)), metrics.analyze(str(tmp_path), 'audit')
    assert old['tokens']['input'] is new['summary']['tokens']['input_tokens'] is None
    assert old['tokens']['output'] == new['summary']['tokens']['output_tokens'] == 2
    assert old['cost_notional_usd'] is new['summary']['estimated_cost_usd'] is None


def test_declared_missing_child_cannot_report_complete_family(tmp_path):
    src = write(tmp_path / 'audit.jsonl', [response()])
    register(tmp_path, src)
    conn = db.connect(str(tmp_path))
    db.meta_set(conn, 'capture-manifest:audit', json.dumps({'session_complete': True,
        'children_closed': True, 'expected_children': ['not-yet-captured'],
        'capabilities': {'visible_messages': True, 'private_reasoning': False}}))
    conn.close()
    result = metrics.analyze(str(tmp_path), 'audit')
    assert result['coverage']['child_coverage']['missing'] == ['not-yet-captured']
    assert result['coverage']['capabilities']['private_reasoning'] is False
    assert not metrics_index.family(str(tmp_path), 'audit')['complete']


def test_unregistered_work_session_is_a_capture_finding(tmp_path):
    register(tmp_path)
    conn = db.connect(str(tmp_path))
    findings = transcripts.checks(str(tmp_path), conn)
    conn.close()
    assert any(level == 'warn' and 'unregistered' in message and 'audit' in message
               for _name, level, message in findings)


def test_same_size_rewrite_advances_generation_and_keeps_old_bytes(tmp_path):
    src = write(tmp_path / 'native.jsonl', [{'type': 'user', 'message': {'content': 'a' * 9000}}])
    register(tmp_path, src)
    first = transcripts.snapshot(str(tmp_path), 'audit')
    before = Path(transcripts.local_path(str(tmp_path), 'audit')).read_bytes()
    src.write_bytes(src.read_bytes().replace(b'aaaa', b'baaa', 1))
    after = transcripts.snapshot(str(tmp_path), 'audit')
    assert after['generation'] == first['generation'] + 1
    assert Path(transcripts.local_path(str(tmp_path), 'audit') + '.1').read_bytes() == before


def test_zero_progress_write_fails_without_publishing(tmp_path, monkeypatch):
    src, dst = tmp_path / 'source', tmp_path / 'copy'
    src.write_bytes(b'content')
    monkeypatch.setattr(os, 'write', lambda *args: 0)
    with pytest.raises(OSError, match='progress'):
        transcripts.copy_forward(src, dst)
    assert not dst.exists()


def test_snapshot_rejects_unavailable_lock(tmp_path, monkeypatch):
    src = write(tmp_path / 'native.jsonl', [response()])
    register(tmp_path, src)
    def fail(*args):
        raise OSError('unsupported locking')
    monkeypatch.setattr(transcripts.fcntl, 'flock', fail)
    with pytest.raises(OSError, match='locking'):
        transcripts.snapshot(str(tmp_path), 'audit')
    assert not Path(transcripts.local_path(str(tmp_path), 'audit')).exists()


def test_new_capture_excludes_codex_reasoning_event_shapes(tmp_path):
    src = write(tmp_path / 'native.jsonl', [
        {'type': 'event_msg', 'payload': {'type': 'agent_reasoning', 'text': 'PRIVATE'}},
        {'type': 'event_msg', 'payload': {'type': 'agent_reasoning_raw_content', 'text': 'PRIVATE'}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Visible'}}])
    register(tmp_path, src, operator='codex')
    transcripts.snapshot(str(tmp_path), 'audit')
    content = Path(transcripts.local_path(str(tmp_path), 'audit')).read_text()
    assert 'PRIVATE' not in content and 'Visible' in content


def test_symlink_child_is_not_authorized_by_registered_parent(tmp_path):
    src = write(tmp_path / 'source' / 'audit.jsonl', [response()])
    secret = write(tmp_path / 'foreign.jsonl', [{'type': 'user', 'message': {'content': 'OTHER PROJECT'}}])
    folder = src.parent / src.stem / 'subagents'
    folder.mkdir(parents=True)
    (folder / 'agent-linked.jsonl').symlink_to(secret)
    register(tmp_path, src)
    transcripts.snapshot(str(tmp_path), 'audit')
    assert transcripts.local_subagents(str(tmp_path), 'audit') == []


def test_failed_source_manifest_publication_recovers_generation(tmp_path, monkeypatch):
    src = write(tmp_path / 'native.jsonl', [{'type': 'user', 'message': {'content': 'before'}}])
    register(tmp_path, src)
    first = transcripts.snapshot(str(tmp_path), 'audit')
    src.write_text(src.read_text().replace('before', 'after!'))
    real = os.replace
    def fail(source, target):
        if str(target).endswith('.source.json'):
            raise OSError('manifest interrupted')
        return real(source, target)
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError, match='manifest'):
        transcripts.snapshot(str(tmp_path), 'audit')
    monkeypatch.setattr(os, 'replace', real)
    recovered = transcripts.snapshot(str(tmp_path), 'audit')
    assert recovered['generation'] == first['generation'] + 1
    assert 'after!' in Path(transcripts.local_path(str(tmp_path), 'audit')).read_text()


def test_registered_native_child_sources_use_provider_independent_registry(tmp_path):
    from alpaca import observability
    main = write(tmp_path / 'parent.jsonl', [response()])
    child = write(tmp_path / 'native' / 'codex-child.jsonl', [response('child-response')])
    register(tmp_path, main)
    observability.expect_source(str(tmp_path), 'audit', 'anthropic', locator=str(main), closed=True,
                                capabilities={'child_inventory_complete': True})
    observability.expect_source(str(tmp_path), 'native-child', 'openai', locator=str(child),
                                parent_session='audit', closed=True,
                                capabilities={'child_inventory_complete': True})
    result = metrics.analyze(str(tmp_path), 'audit')
    assert len(result['children']) == 1
    assert result['coverage']['child_coverage']['state'] == 'complete'
    assert result['coverage']['session_complete'] is True
    assert metrics_index.family(str(tmp_path), 'audit')['totals']['responses'] == 2
    assert 'codex-child.jsonl' not in json.dumps(result)
    child.unlink()
    revised = metrics.analyze(str(tmp_path), 'audit')
    assert revised['coverage']['child_coverage']['missing'] == ['native-child']
    assert not revised['coverage']['complete']


def test_registry_only_main_source_is_read_without_mirroring_session_metadata(tmp_path):
    from alpaca import observability
    main = write(tmp_path / 'external' / 'native.jsonl', [response()])
    register(tmp_path)
    observability.expect_source(str(tmp_path), 'audit', 'anthropic', locator=str(main), closed=True,
                                capabilities={'child_inventory_complete': True})
    result = metrics.analyze(str(tmp_path), 'audit')
    assert result['summary']['responses'] == 1
    assert build_index.collect(str(tmp_path))[0]['tokens']['output'] == 10


def test_snapshot_partial_source_stays_incomplete_after_native_source_disappears(tmp_path):
    src = write(tmp_path / 'native.jsonl', [response()])
    with src.open('ab') as out:
        out.write(b'{"type":"assistant"')
    register(tmp_path, src)
    transcripts.snapshot(str(tmp_path), 'audit')
    src.unlink()
    result = metrics.analyze(str(tmp_path), 'audit')
    assert result['coverage']['partial_lines'] == 1
    assert not result['coverage']['file_complete']
    assert not result['coverage']['usage_total_is_exact']


def test_declared_registry_readable_file_cannot_establish_usage_when_not_closed(tmp_path):
    from alpaca import observability
    main = write(tmp_path / 'native.jsonl', [response()])
    register(tmp_path, main)
    observability.expect_source(str(tmp_path), 'audit', 'anthropic', locator=str(main))
    result = metrics.analyze(str(tmp_path), 'audit')
    assert result['coverage']['file_complete'] is True
    assert result['coverage']['child_coverage']['state'] == 'unknown'
    assert result['coverage']['session_complete'] is False


def test_legacy_cost_entrypoint_uses_verified_exact_model_cards():
    raw = {'input_tokens': 1_000_000, 'output_tokens': 0,
           'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
    assert parse_session.notional_cost('claude-sonnet-5', raw) == 2.0
    assert parse_session.notional_cost('made-up-sonnet-model', raw) is None


def test_filtered_current_snapshot_is_not_reported_behind_by_raw_byte_size(tmp_path):
    rec = response()
    rec['message']['content'].append({'type': 'thinking', 'thinking': 'PRIVATE' * 2000})
    src = write(tmp_path / 'native.jsonl', [rec])
    register(tmp_path, src)
    conn = db.connect(str(tmp_path))
    db.patch(conn, 'sessions', 'sid', 'audit', {'ended': '2026-09-22T11:00:00Z'})
    transcripts.snapshot(str(tmp_path), 'audit', conn=conn)
    findings = transcripts.checks(str(tmp_path), conn)
    assert not any(level == 'warn' for _, level, _ in findings)
    src.write_bytes(src.read_bytes().replace(b'Visible answer', b'Changed answer'))
    assert any(level == 'warn' for _, level, _ in transcripts.checks(str(tmp_path), conn))
    conn.close()


def test_legacy_fold_exposes_incomplete_session_denominator(tmp_path):
    register(tmp_path)
    value = build_index._fold(str(tmp_path), build_index.collect(str(tmp_path)))
    assert value['project']['listed_sessions'] == 1
    assert value['project']['partial_sessions'] == 1
    assert value['project']['incomplete_project'] is True


def test_registry_child_snapshot_survives_native_source_removal(tmp_path):
    from alpaca import observability
    main = write(tmp_path / 'parent.jsonl', [response()])
    child = write(tmp_path / 'native' / 'child.jsonl', [response('native-child')])
    register(tmp_path, main)
    observability.expect_source(str(tmp_path), 'child-id', 'openai', locator=str(child), parent_session='audit')
    before = metrics.analyze(str(tmp_path), 'audit')
    ident = before['children'][0]['id']
    snap = transcripts.snapshot(str(tmp_path), 'audit')
    assert snap['registry_children'][0]['retained'] is True
    assert Path(transcripts.local_path(str(tmp_path), 'child-id')).is_file()
    # Standalone collector capture also resolves a registry-only child session.
    assert transcripts.snapshot(str(tmp_path), 'child-id')['mode'] in ('current', 'copy')
    child.unlink()
    after = metrics.analyze(str(tmp_path), 'audit')
    assert after['children'][0]['id'] == ident
    assert metrics.analyze(str(tmp_path), 'audit', child=ident)['ledger'][0]['id'] == 'native-child'


def test_project_reported_charge_total_requires_proven_nonoverlapping_import_scopes(tmp_path):
    for sid in ('parent', 'child'):
        register(tmp_path, sid=sid)
        source = tmp_path / (sid + '-usage.json')
        source.write_text(json.dumps({'sample_id': 'same-provider-request', 'provider': 'provider',
            'model': 'model', 'tokens': {'input': 10, 'output': 5, 'cache_read': 0,
                'cache_write_5m': 0, 'cache_write_1h': 0}, 'reported_cost_usd': 1.25}))
        usage.ingest(str(tmp_path), sid, str(source))
    sessions = build_index.collect(str(tmp_path))
    assert all(session['cost_reported_usd'] == 1.25 for session in sessions)
    combined = build_index._fold(str(tmp_path), sessions)['project']
    assert combined['cost_reported_usd'] is None
    assert combined['cost_available'] is False
