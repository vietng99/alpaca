"""Measured transcript analytics must not double-count provider mirror records."""
import json
from pathlib import Path

import pytest

from alpaca import db, pool, transcripts
from alpaca.analytics import detail


def write(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))
    return path


def register(root, path=None, operator='claude'):
    conn = db.connect(root)
    db.upsert(conn, 'sessions', 'sid', {'sid': 's1', 'transcript': str(path) if path else None,
                                      'started': '2026-09-22T00:00:00Z'})
    db.meta_set(conn, 'operator:s1', operator)
    conn.close()


def claude(ident='response1', uuid='block1', usage=None, content=None):
    return {'type': 'assistant', 'uuid': uuid, 'timestamp': '2026-09-22T01:02:03Z',
            'requestId': 'request-' + ident, 'message': {
                'id': ident, 'model': 'claude-haiku-4-5-20251001',
                'usage': usage if usage is not None else {'input_tokens': 10, 'output_tokens': 5,
                    'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0},
                'content': content or [{'type': 'text', 'text': 'Visible result'}]}}


def codex(kind, payload, ts='2026-09-22T02:00:00Z'):
    return {'type': kind, 'timestamp': ts, 'payload': payload}


def usage(input=100, output=10, cached=40, reasoning=3):
    return {'input_tokens': input, 'output_tokens': output, 'cached_input_tokens': cached,
            'cache_write_input_tokens': 0, 'reasoning_output_tokens': reasoning,
            'total_tokens': input + output}


def snapshot(total, last=None):
    return codex('event_msg', {'type': 'token_count', 'info': {
        'total_token_usage': total, 'last_token_usage': last,
        'model_context_window': 1000}})


def analyze(root, **kwargs):
    from alpaca.analytics import metrics
    return metrics.analyze(root, 's1', **kwargs)


def test_claude_unique_response_usage_merges_tools_and_preserves_cache_subsets(project):
    native = {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 100,
              'cache_creation_input_tokens': 50,
              'cache_creation': {'ephemeral_5m_input_tokens': 30, 'ephemeral_1h_input_tokens': 20},
              'output_tokens_details': {'thinking_tokens': 7}}
    records = [claude(usage=native), claude(uuid='block2', usage=native, content=[
        {'type': 'thinking', 'thinking': 'HIDDEN CHAIN'},
        {'type': 'tool_use', 'id': 'tool1', 'name': 'Read', 'input': {'api_key': 'SECRET'}}]),
        {'type': 'user', 'uuid': 'result1', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'tool1', 'content': 'ok'}]}}]
    write(transcripts.local_path(project, 's1'), records)
    register(project)
    result = analyze(project)
    assert result['summary']['responses'] == 1
    assert result['summary']['tokens']['input_tokens'] == 160
    assert result['summary']['tokens']['total_tokens'] == 180
    assert result['summary']['tokens']['reasoning_output_tokens'] == 7
    assert result['summary']['tokens']['cache_creation_5m_tokens'] == 30
    assert result['summary']['tokens']['cache_creation_1h_tokens'] == 20
    assert result['ledger'][0]['tools'][0]['status'] == 'completed'
    assert len(result['ledger'][0]['message_ids']) == 2
    assert result['context']['current_tokens'] == 160
    assert result['context']['capacity_tokens'] is None
    assert 'SECRET' not in json.dumps(result) and 'HIDDEN CHAIN' not in json.dumps(result)
    assert result['summary']['tool_calls'] == 1


def test_codex_native_usage_wins_over_snapshots_with_measured_context(project):
    records = [codex('turn_context', {'model': 'gpt-6-astra'}),
        codex('response_item', {'type': 'message', 'id': 'm1', 'role': 'assistant',
                              'content': [{'type': 'output_text', 'text': 'Answer'}]}),
        codex('response_item', {'type': 'function_call', 'call_id': 'c1', 'name': 'exec_command',
                              'arguments': '{"cmd":"pwd"}'}),
        codex('token_usage_record', {'response_id': 'r1', 'usage': usage()}),
        codex('token_usage_record', {'response_id': 'r1', 'usage': usage()}),
        snapshot(usage(), usage()), snapshot(usage(), usage()),
        codex('compacted', {'replacement_history': [{'private': 'HIDDEN SUMMARY'}]}),
        codex('response_item', {'type': 'message', 'id': 'm2', 'role': 'assistant',
                              'channel': 'analysis', 'content': [{'type': 'text', 'text': 'PRIVATE'}]}),
        codex('token_usage_record', {'response_id': 'r2', 'usage': usage(200, 20, 150, 6)}),
        snapshot(usage(300, 30, 190, 9), usage(200, 20, 150, 6))]
    write(transcripts.local_path(project, 's1'), records)
    register(project, operator='codex')
    result = analyze(project)
    assert result['summary']['responses'] == 2
    assert result['summary']['tokens']['input_tokens'] == 300
    assert result['summary']['tokens']['uncached_input_tokens'] == 110
    assert result['summary']['tokens']['total_tokens'] == 330
    assert result['coverage']['usage_method'] == 'native-response-records'
    assert result['context']['capacity_tokens'] == 1000
    assert result['context']['current_tokens'] == 200
    assert result['context']['current_percent'] == 20
    assert len(result['context']['compactions']) == 1
    assert result['ledger'][0]['excerpt'] == 'Answer'
    assert result['ledger'][1]['excerpt'] == ''
    assert 'PRIVATE' not in json.dumps(result) and 'HIDDEN SUMMARY' not in json.dumps(result)


def test_snapshot_fallback_deduplicates_cumulative_and_skips_resets(project):
    records = [snapshot(usage(), usage()), snapshot(usage(), usage()),
               snapshot(usage(300, 30, 190, 9), usage(200, 20, 150, 6)),
               snapshot(usage(20, 2, 0, 0), usage(20, 2, 0, 0)),
               snapshot(usage(20, 2, 0, 0), usage(20, 2, 0, 0)),
               snapshot(usage(50, 5, 0, 0), usage(30, 3, 0, 0))]
    write(transcripts.local_path(project, 's1'), records)
    register(project, operator='codex')
    result = analyze(project)
    assert result['summary']['responses'] == 3
    assert result['summary']['tokens']['input_tokens'] == 330
    assert result['summary']['tokens']['total_tokens'] == 363
    assert result['coverage']['snapshot_resets'] == 1
    assert result['coverage']['state'] == 'partial'
    assert result['coverage']['usage_method'] == 'deduplicated-last-usage-snapshots'


def test_first_snapshot_uses_last_response_not_entire_historical_total(project):
    write(transcripts.local_path(project, 's1'), [snapshot(usage(900, 90, 400, 15), usage())])
    register(project, operator='codex')
    result = analyze(project)
    assert result['summary']['tokens']['total_tokens'] == 110
    assert result['coverage']['state'] == 'partial'
    assert result['summary']['responses'] == 1


def test_unknown_model_retains_tokens_and_cost_denominator_excludes_unpriced(project):
    unknown = claude('unknown', 'u2')
    unknown['message']['model'] = 'unpublished-future-model'
    write(transcripts.local_path(project, 's1'), [claude(), unknown])
    register(project)
    result = analyze(project)
    assert result['summary']['tokens']['total_tokens'] == 30
    assert result['summary']['costed_responses'] == 1
    assert result['summary']['average_cost_usd'] == result['summary']['estimated_cost_usd']
    assert result['ledger'][1]['cost']['total_usd'] is None
    assert result['cost']['pricing_coverage'] == 0.5


def test_reported_cost_state_keeps_separate_scope_and_safe_hook_metadata(project):
    records = [claude(), {'type': 'cost-state', 'totalCostUSD': 283.72,
        'modelUsage': {'claude-sonnet-4-5-20250929': {'costUSD': 283.72, 'inputTokens': 999}}},
        {'type': 'attachment', 'uuid': 'h1', 'attachment': {'type': 'hook_success',
            'hookName': 'SessionStart:startup', 'hookEvent': 'SessionStart', 'toolUseID': 'x',
            'content': 'API_KEY=secret-private-value'}},
        {'type': 'system', 'uuid': 'h2', 'subtype': 'stop_hook_summary', 'hookCount': 2,
            'hookInfos': [{'command': 'secret path', 'durationMs': 40}], 'hookErrors': ['private']},
        {'type': 'progress', 'uuid': 'h3', 'data': {'type': 'hook_progress', 'hookEvent': 'PreToolUse',
            'hookName': 'safe', 'command': 'PRIVATE COMMAND'}}]
    write(transcripts.local_path(project, 's1'), records)
    register(project)
    result = analyze(project)
    assert result['cost']['reported_usd'] == 283.72
    assert result['cost']['reported_kind'] == 'client-reported estimate'
    assert result['cost']['reported_scope'] == 'client session scope; child inclusion unspecified'
    assert result['summary']['tokens']['input_tokens'] == 10
    assert result['hooks']['total'] == 3
    assert result['hooks']['executions_reported'] == 2
    assert result['hooks']['events'][1]['duration_ms'] == 40
    assert all(s not in json.dumps(result) for s in ('secret-private-value', 'PRIVATE COMMAND', 'secret path'))


def test_parent_child_registered_scope_and_read_only_cache_invalidation(project, tmp_path):
    external = write(tmp_path / 'account' / 'parent.jsonl', [claude()])
    write(external.parent / 'other.jsonl', [claude('foreign')])
    write(external.parent / external.stem / 'subagents' / 'agent-a.jsonl', [claude('child')])
    register(project, path=external)
    before = Path(project, '.alpaca', 'alpaca.db').read_bytes()
    result = analyze(project)
    assert len(result['children']) == 1
    assert result['summary']['responses'] == 1
    assert result['coverage']['totals_include_children'] is False
    nested = analyze(project, child=result['children'][0]['id'])
    assert nested['session']['parent_sid'] == 's1'
    assert nested['ledger'][0]['id'] == 'child'
    assert before == Path(project, '.alpaca', 'alpaca.db').read_bytes()
    write(external, [claude(), claude('new', 'new-block')])
    again = analyze(project)
    assert again['summary']['responses'] == 2
    assert again['revision'] != result['revision']
    result['summary']['responses'] = 999
    assert analyze(project)['summary']['responses'] == 2
    with pytest.raises(ValueError):
        analyze(project, child='../../other')


def test_bounded_scan_and_bounded_ledger_explain_lower_bounds(project, monkeypatch):
    from alpaca.analytics import metrics
    write(transcripts.local_path(project, 's1'), [claude(str(n), str(n)) for n in range(4)])
    register(project)
    monkeypatch.setattr(detail, 'MAX_RECORDS', 3)
    monkeypatch.setattr(metrics, 'MAX_LEDGER', 2)
    result = analyze(project)
    assert result['summary']['responses'] == 3
    assert result['ledger_total'] == 3 and len(result['ledger']) == 2
    assert result['ledger_truncated'] is True
    assert result['coverage']['state'] == 'partial'
    assert result['coverage']['total_is_exact'] is False


def test_unavailable_usage_is_not_zero_and_no_account_scanning(project, tmp_path):
    register(project)
    write(tmp_path / 'unregistered.jsonl', [claude()])
    result = analyze(project)
    assert result['coverage']['state'] == 'unavailable'
    assert result['summary']['tokens'] is None
    assert result['summary']['estimated_cost_usd'] is None
    assert result['ledger'] == []


def test_native_usage_without_final_snapshot_is_current_context(project):
    write(transcripts.local_path(project, 's1'), [
        codex('turn_context', {'model': 'gpt-6-astra'}),
        codex('token_usage_record', {'response_id': 'r1', 'usage': usage()}),
        snapshot(usage(), usage()),
        codex('token_usage_record', {'response_id': 'r2', 'usage': usage(200, 20, 150, 6)})])
    register(project, operator='codex')
    result = analyze(project)
    assert result['context']['current_tokens'] == 200
    assert result['context']['capacity_tokens'] == 1000


def test_partial_native_usage_keeps_snapshot_gap_explicit_without_doublecount(project):
    write(transcripts.local_path(project, 's1'), [
        codex('token_usage_record', {'response_id': 'r1', 'usage': usage()}),
        snapshot(usage(300, 30, 190, 9), usage(200, 20, 150, 6))])
    register(project, operator='codex')
    result = analyze(project)
    assert result['summary']['tokens']['total_tokens'] == 110
    assert result['coverage']['state'] == 'partial'
    assert result['coverage']['native_snapshot_disagreement'] is True


def test_service_tier_reaches_cost_and_additional_context_is_not_execution(project):
    record = claude(usage={'input_tokens': 1000, 'output_tokens': 100,
                          'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0,
                          'service_tier': 'batch', 'speed': 'standard'})
    write(transcripts.local_path(project, 's1'), [record,
        {'type': 'attachment', 'uuid': 'injection', 'attachment': {
            'type': 'hook_additional_context', 'hookEvent': 'PreToolUse',
            'hookName': 'Safety', 'content': 'DO NOT EXPOSE'}}])
    register(project)
    result = analyze(project)
    assert result['summary']['estimated_cost_usd'] == pytest.approx(0.00075)
    assert result['hooks']['total'] == 1
    assert result['hooks']['executions_reported'] == 0
    assert result['hooks']['by_type'][0]['type'] == 'hook_additional_context'
    assert 'DO NOT EXPOSE' not in json.dumps(result)


def test_imported_reported_cost_keeps_event_provenance_separate(project, tmp_path):
    from alpaca.analytics import usage as imports
    register(project)
    source = tmp_path / 'cost-sample.json'
    source.write_text(json.dumps({'sample_id': 'one', 'model': 'measured-model',
        'provider': 'external', 'tokens': {'input': 100, 'output': 10}, 'reported_cost_usd': 1.25}))
    imports.ingest(project, 's1', str(source))
    result = analyze(project)
    assert result['cost']['imported_reported_usd'] == 1.25
    assert result['cost']['reported_usd'] is None
    assert result['summary']['tokens']['input_tokens'] == 100
    assert result['coverage']['usage_method'] == 'explicit-usage-samples'
    assert result['cost']['imported_sources'][0]['event_id'] > 0
    assert str(source) not in json.dumps(result)


def test_native_missing_usage_is_not_an_exact_zero(project):
    write(transcripts.local_path(project, 's1'), [claude(usage={'input_tokens': 1})])
    register(project)
    result = analyze(project)
    assert result['summary']['responses'] == 1
    assert result['summary']['usage_responses'] == 0
    assert result['summary']['tokens'] is None
    assert result['coverage']['usage_total_is_exact'] is False


def test_pool_only_tools_do_not_contaminate_registered_parent_scope(project):
    write(transcripts.local_path(project, 's1'), [claude(content=[
        {'type': 'tool_use', 'id': 'parent-tool', 'name': 'Read', 'input': {}}])])
    register(project)
    write(pool.path(project, 's1'), [
        {'sid': 's1', 'phase': 'post', 'tool_use_id': 'parent-tool', 'tool': 'Read', 'response': 'ok'},
        {'sid': 's1', 'phase': 'pre', 'tool_use_id': 'unlinked-tool', 'tool': 'Write', 'input': {}}])
    result = analyze(project)
    assert result['summary']['tool_calls'] == 1
    assert result['ledger'][0]['tools'][0]['status'] == 'completed'
    assert result['coverage']['excluded_unlinked_pool_calls'] == 1
    assert [tool['name'] for tool in result['tools']] == ['Read']


def test_legacy_codex_missing_cache_writes_stays_unknown_and_unpriced(project):
    old_usage = usage()
    del old_usage['cache_write_input_tokens']
    write(transcripts.local_path(project, 's1'), [
        codex('turn_context', {'model': 'gpt-6-astra'}),
        codex('token_usage_record', {'response_id': 'r1', 'usage': old_usage})])
    register(project, operator='codex')
    result = analyze(project)
    assert result['summary']['tokens']['total_tokens'] == 110
    assert result['summary']['tokens']['cache_write_input_tokens'] is None
    assert result['summary']['estimated_cost_usd'] is None
    assert result['session']['usage_available'] is True


def test_cost_allocation_is_equal_share_not_intrinsic_tool_price(project):
    write(transcripts.local_path(project, 's1'), [claude(content=[
        {'type': 'tool_use', 'id': 'tool-a', 'name': 'Read', 'input': {}},
        {'type': 'tool_use', 'id': 'tool-b', 'name': 'Write', 'input': {}}])])
    register(project)
    result = analyze(project)
    assert len(result['tools']) == 2
    assert result['tools'][0]['estimated_allocated_cost_usd'] == pytest.approx(0.0000175)
    assert result['tools'][1]['estimated_allocated_cost_usd'] == pytest.approx(0.0000175)
    assert 'equal share' in result['tools'][0]['allocation']


def test_large_compaction_counts_metadata_without_hidden_history_or_false_partial(project):
    hidden = 'HIDDEN ' * (detail.MAX_LINE_BYTES // 7 + 2)
    write(transcripts.local_path(project, 's1'), [
        codex('compacted', {'replacement_history': [{'type': 'message', 'content': hidden}]}),
        codex('compacted', {'replacement_history': []}),
        codex('token_usage_record', {'response_id': 'r1', 'usage': usage()})])
    register(project, operator='codex')
    result = analyze(project)
    assert result['context']['compactions_total'] == 2
    assert result['coverage']['state'] == 'captured'
    assert 'HIDDEN' not in json.dumps(result)


def test_analytics_read_budget_remains_bounded(project, monkeypatch):
    from alpaca.analytics import metrics
    path = write(transcripts.local_path(project, 's1'), [claude(str(n), str(n)) for n in range(4)])
    register(project)
    monkeypatch.setattr(metrics, 'MAX_READ_BYTES', path.stat().st_size // 2)
    result = analyze(project)
    assert result['summary']['responses'] == 2
    assert result['coverage']['state'] == 'partial'
    assert result['coverage']['total_is_exact'] is False


def test_hourly_bins_use_unambiguous_utc_instants(project):
    a = claude('a', 'a')
    b = claude('b', 'b')
    a['timestamp'] = '2026-09-22T09:30:00+07:00'
    b['timestamp'] = '2026-09-22T02:45:00Z'
    write(transcripts.local_path(project, 's1'), [a, b])
    register(project)
    result = analyze(project)
    assert len(result['hourly']) == 1
    assert result['hourly'][0]['hour'] == '2026-09-22T02:00:00Z'
    assert result['hourly'][0]['responses'] == 2


def test_intermediate_native_context_peak_survives_missing_mirror_snapshot(project):
    write(transcripts.local_path(project, 's1'), [
        codex('token_usage_record', {'response_id': 'r1', 'usage': usage()}),
        snapshot(usage(), usage()),
        codex('token_usage_record', {'response_id': 'r2', 'usage': usage(900, 10, 600)}),
        codex('token_usage_record', {'response_id': 'r3', 'usage': usage(200, 10, 150)}),
        snapshot(usage(1200, 30, 790, 9), usage(200, 10, 150))])
    register(project, operator='codex')
    result = analyze(project)
    assert result['context']['peak_tokens'] == 900
    assert result['context']['current_tokens'] == 200
    assert result['context']['peak_percent'] == 90
    assert len(result['context']['timeline']) == 3


def test_nested_command_failures_are_separate_from_outer_tool_flags_and_private_content(project):
    failed = codex('event_msg', {'type': 'item_completed', 'item': {
        'type': 'CommandExecution', 'id': 'cmd-1', 'status': 'failed', 'exit_code': 2,
        'command': 'PRIVATE COMMAND', 'stdout': 'PRIVATE OUTPUT',
        'duration': {'secs': 1, 'nanos': 500000000}}})
    hidden = codex('event_msg', {'type': 'item_completed', 'item': {
        'type': 'Reasoning', 'id': 'hidden', 'text': 'PRIVATE REASONING'}})
    write(transcripts.local_path(project, 's1'), [failed, failed, hidden])
    register(project, operator='codex')
    result = analyze(project)
    assert result['nested_actions']['total'] == 1
    assert result['nested_actions']['failed'] == 1
    assert result['nested_actions']['items'][0]['exit_code'] == 2
    assert result['nested_actions']['items'][0]['duration_ms'] == 1500
    assert result['summary']['tool_calls'] == 0
    assert 'PRIVATE' not in json.dumps(result)


def test_synthetic_claude_errors_are_not_provider_responses(project):
    synthetic = claude('fake', 'fake', usage={'input_tokens': 0, 'output_tokens': 0})
    synthetic['message']['model'] = '<synthetic>'
    write(transcripts.local_path(project, 's1'), [claude(), synthetic])
    register(project)
    result = analyze(project)
    assert result['summary']['responses'] == 1
    assert result['coverage']['synthetic_records_excluded'] == 1


def test_inconsistent_native_cache_subset_is_unknown_not_negative_uncached(project):
    invalid = usage(100, 10, 200)
    write(transcripts.local_path(project, 's1'), [
        codex('turn_context', {'model': 'gpt-6-astra'}),
        codex('token_usage_record', {'response_id': 'bad', 'usage': invalid})])
    register(project, operator='codex')
    result = analyze(project)
    assert result['summary']['tokens']['input_tokens'] == 100
    assert result['summary']['tokens']['cached_input_tokens'] is None
    assert result['summary']['tokens']['uncached_input_tokens'] is None
    assert result['summary']['estimated_cost_usd'] is None
    assert result['coverage']['usage_anomalies'] == 1
    assert result['coverage']['state'] == 'partial'


def test_blocking_hook_error_has_error_status_without_disclosing_content(project):
    write(transcripts.local_path(project, 's1'), [{'type': 'attachment', 'uuid': 'block',
        'attachment': {'type': 'hook_blocking_error', 'hookEvent': 'Stop',
                       'hookName': 'Guard', 'content': 'PRIVATE'}}])
    register(project)
    result = analyze(project)
    assert result['hooks']['events'][0]['status'] == 'error'
    assert 'PRIVATE' not in json.dumps(result)


def test_client_model_usage_keeps_token_counts_beside_cost(project):
    records = [claude(), {'type': 'cost-state', 'totalCostUSD': 1.5, 'modelUsage': {
        'claude-opus-5': {'inputTokens': 40, 'outputTokens': 17832, 'thinkingTokens': 9363,
                          'cacheReadInputTokens': 1919502, 'cacheCreationInputTokens': 83082,
                          'webSearchRequests': 0, 'costUSD': 1.25},
        'claude-haiku-4-5-20251001': {'costUSD': 0.25, 'inputTokens': 'many'}}}]
    write(transcripts.local_path(project, 's1'), records)
    register(project)
    models = analyze(project)['cost']['reported_models']
    assert models[0] == {'model': 'claude-opus-5', 'cost_usd': 1.25, 'input_tokens': 40,
                         'output_tokens': 17832, 'cache_read_tokens': 1919502,
                         'cache_write_tokens': 83082}
    assert models[1] == {'model': 'claude-haiku-4-5-20251001', 'cost_usd': 0.25, 'input_tokens': None,
                         'output_tokens': None, 'cache_read_tokens': None, 'cache_write_tokens': None}


def test_model_rollups_name_the_provider_and_why_responses_are_unpriced(project):
    unknown = claude('unknown', 'u2')
    unknown['message']['model'] = 'unpublished-future-model'
    write(transcripts.local_path(project, 's1'), [claude(), unknown])
    register(project)
    models = {row['model']: row for row in analyze(project)['models']}
    assert models['claude-haiku-4-5-20251001']['provider'] == 'anthropic'
    assert models['claude-haiku-4-5-20251001']['unpriced'] == {}
    assert models['unpublished-future-model']['unpriced'] == {'unverified_model': 1}
