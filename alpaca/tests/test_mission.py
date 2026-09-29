"""Mission state must follow evidence and input identity, never task completion."""
import copy
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from alpaca import db


def definition():
    return {'version': 1, 'title': 'Example system', 'purpose': 'Trace work to evidence',
            'inventory': ['src'], 'groups': [{'id': 'core', 'title': 'Core'}],
            'nodes': [{'id': name, 'group': 'core', 'title': name.title(),
                       'purpose': 'Own ' + name, 'sources': ['src/' + name + '.txt'],
                       'checks': ['behavior'], 'tags': ['reliability'], 'lifecycle': 'current'}
                      for name in ('parser', 'usage', 'view')],
            'edges': [{'source': a, 'target': b, 'kind': 'feeds', 'confidence': 'confirmed',
                       'reason': b + ' consumes ' + a, 'evidence': ['src/' + b + '.txt']}
                      for a, b in [('parser', 'usage'), ('usage', 'view')]]}


@pytest.fixture
def mission():
    assert importlib.util.find_spec('alpaca.mission'), 'persistent mission model is missing'
    return importlib.import_module('alpaca.mission')


@pytest.fixture
def mapped(tmp_path, mission):
    root = tmp_path / 'project'; (root / 'src').mkdir(parents=True)
    for name in ('parser', 'usage', 'view'):
        (root / 'src' / (name + '.txt')).write_text(name)
    mission.define(root, definition(), session='owner', expected_revision=0)
    return root


def by_id(payload, ident):
    return next(n for n in payload['nodes'] if n['id'] == ident)


def test_development_plan_and_unmapped_work_are_captured_historically(mapped, mission):
    from alpaca import taskplan
    conn = db.connect(mapped)
    db.upsert(conn, 'ops', 'id', {'id': 'op-dev', 'intent': 'Develop memory', 'status': 'open'})
    for ident, title in [('t-a', 'Define quality bar'), ('t-b', 'Build retrieval')]:
        db.upsert(conn, 'tasks', 'id', {'id': ident, 'op': 'op-dev', 'statement': title, 'title': title, 'status': 'open'})
    plan = {'version': 1, 'revision': 0, 'groups': [{'id': 'memory', 'title': 'Memory development', 'category': 'Features'}],
            'tasks': [{'id': ident, 'group': 'memory', 'after': after, 'resources': [], 'requires_stages': [], 'hold': None}
                      for ident, after in [('t-a', []), ('t-b', ['t-a'])]]}
    taskplan.record(conn, 'op-dev', plan, session='owner', actor='owner')
    conn.close()
    mission.link(mapped, 'parser', 't-b', 'work', session='owner')
    snapshot = mission.scan(mapped, session='owner', force=True)
    current = mission.project(mapped)['development']
    nodes = current['operations'][0]['groups'][0]['nodes']
    assert [(n['id'], n['state']) for n in nodes] == [('t-a', 'ready'), ('t-b', 'waiting')]
    assert current['operations'][0]['edges'] == [{'from': 't-a', 'to': 't-b'}]
    assert {t['id'] for t in current['tasks']} == {'t-a', 't-b'}  # Unmapped work remains discoverable.
    conn = db.connect(mapped)
    conn.execute("UPDATE tasks SET status='blocked' WHERE id='t-a'")
    conn.close()
    assert mission.project(mapped)['development']['operations'][0]['groups'][0]['nodes'][0]['state'] == 'blocked'
    old = mission.project(mapped, at=snapshot['id'])['development']
    assert old['operations'][0]['groups'][0]['nodes'][0]['state'] == 'ready'
    assert next(t for t in old['tasks'] if t['id'] == 't-a')['status'] == 'open'



def test_development_readiness_uses_current_profile_evidence(mapped, mission, monkeypatch):
    from alpaca import profile, taskplan
    conn = db.connect(mapped)
    db.upsert(conn, 'ops', 'id', {'id': 'op-stage', 'intent': 'Stage work', 'status': 'open'})
    db.upsert(conn, 'tasks', 'id', {'id': 't-stage', 'op': 'op-stage', 'statement': 'Implement after lint', 'status': 'open'})
    taskplan.record(conn, 'op-stage', {'version': 1, 'revision': 0,
        'groups': [{'id': 'build', 'title': 'Build'}],
        'tasks': [{'id': 't-stage', 'group': 'build', 'after': [], 'resources': [], 'requires_stages': ['lint'], 'hold': None}]},
        session='owner', actor='owner')
    conn.close()
    monkeypatch.setattr(profile, 'strict', lambda *args: [{'stage': 'lint', 'verdict': 'PASS'}])
    snap = mission.scan(mapped, session='owner', force=True)
    def state(at=None):
        return mission.project(mapped, at=at)['development']['operations'][0]['groups'][0]['nodes'][0]['state']
    assert state() == 'ready'
    monkeypatch.setattr(profile, 'strict', lambda *args: [{'stage': 'lint', 'verdict': 'STALE'}])
    assert state() == 'waiting'
    assert state(snap['id']) == 'ready'
    def unavailable(*args):
        raise ValueError('Profile evidence unavailable')
    monkeypatch.setattr(profile, 'strict', unavailable)
    assert state() == 'waiting'

def check(mission, root, node, code='print("checked")'):
    return mission.run_check(root, node, 'behavior', [sys.executable, '-c', code], session='checker')


def test_unconfigured_project_has_honest_starter_and_read_does_not_write(tmp_path, mission):
    payload = mission.project(tmp_path)
    assert payload['configured'] is False
    assert payload['nodes'] and all(n['state'] == 'unknown' for n in payload['nodes'])
    assert not (tmp_path / '.alpaca').exists()


def test_checks_turn_green_only_for_current_transitive_inputs(mapped, mission):
    assert by_id(mission.project(mapped), 'view')['state'] == 'unknown'
    result = check(mission, mapped, 'view')
    assert result['result'] == 'PASS'
    assert by_id(mission.project(mapped), 'view')['state'] == 'verified'
    (mapped / 'src/parser.txt').write_text('changed contract')
    payload = mission.project(mapped)
    assert by_id(payload, 'view')['state'] == 'stale'
    assert by_id(payload, 'view')['state'] != 'failing'
    assert by_id(payload, 'view')['last_checked']['result'] == 'PASS'
    check(mission, mapped, 'view')
    assert by_id(mission.project(mapped), 'view')['state'] == 'verified'


def test_failed_run_and_source_drift_are_preserved(mapped, mission):
    assert check(mission, mapped, 'parser', 'raise SystemExit(7)')['result'] == 'FAIL'
    assert by_id(mission.project(mapped), 'parser')['state'] == 'failing'
    result = check(mission, mapped, 'parser', 'from pathlib import Path; Path("src/parser.txt").write_text("drift")')
    assert result['result'] == 'BLOCKED'
    node = by_id(mission.project(mapped), 'parser')
    assert node['state'] in ('blocked', 'stale')
    assert any(e['data'].get('result') == 'FAIL' for e in node['history'])


def test_evidence_tampering_revokes_current_green(mapped, mission):
    result = check(mission, mapped, 'parser')
    (mapped / result['evidence']).write_text('rewritten')
    node = by_id(mission.project(mapped), 'parser')
    assert node['state'] == 'blocked'
    assert 'evidence' in node['reason'].lower()


def test_scan_is_idempotent_and_unmapped_files_are_reported(mapped, mission):
    mission.scan(mapped, session='collector')
    assert mission.scan(mapped, session='collector') is None
    (mapped / 'src/new.txt').write_text('not mapped')
    assert mission.scan(mapped, session='collector')
    assert 'src/new.txt' in mission.project(mapped)['coverage']['unmapped']


def test_first_observation_is_distinct_from_a_recorded_change(mapped, mission):
    assert by_id(mission.project(mapped), 'parser')['last_changed']['baseline'] is True
    (mapped / 'src/parser.txt').write_text('changed')
    mission.scan(mapped, session='collector')
    assert by_id(mission.project(mapped), 'parser')['last_changed']['baseline'] is False


def test_history_uses_snapshot_bytes_instead_of_current_files(mapped, mission):
    check(mission, mapped, 'view')
    snapshot = mission.scan(mapped, session='collector', force=True)
    (mapped / 'src/parser.txt').write_text('later')
    historical = mission.project(mapped, at=snapshot['id'])
    assert historical['historical'] is True
    assert by_id(historical, 'view')['state'] == 'verified'
    assert by_id(mission.project(mapped), 'view')['state'] == 'stale'


def test_nonpropagating_and_inferred_edges_do_not_invalidate_inputs(mapped, mission):
    spec = definition()
    spec['edges'][0]['kind'] = 'documents'
    spec['edges'][1]['confidence'] = 'inferred'
    mission.define(mapped, spec, session='owner')
    check(mission, mapped, 'view')
    (mapped / 'src/parser.txt').write_text('new parser')
    (mapped / 'src/usage.txt').write_text('new usage')
    assert by_id(mission.project(mapped), 'view')['state'] == 'verified'


def test_dependency_cycles_are_finite_and_still_invalidate(mapped, mission):
    spec = definition()
    spec['edges'].append(dict(spec['edges'][0], source='view', target='parser'))
    mission.define(mapped, spec, session='owner')
    check(mission, mapped, 'parser')
    (mapped / 'src/view.txt').write_text('changed')
    assert by_id(mission.project(mapped), 'parser')['state'] == 'stale'


@pytest.mark.parametrize('mutation', ['duplicate', 'unknown-edge', 'escape', 'absolute', 'group'])
def test_definition_rejects_ambiguous_or_unsafe_inputs(tmp_path, mission, mutation):
    spec = definition()
    if mutation == 'duplicate': spec['nodes'].append(copy.deepcopy(spec['nodes'][0]))
    if mutation == 'unknown-edge': spec['edges'][0]['target'] = 'missing'
    if mutation == 'escape': spec['nodes'][0]['sources'] = ['../outside']
    if mutation == 'absolute': spec['inventory'] = ['/etc']
    if mutation == 'group': spec['nodes'][0]['group'] = 'missing'
    with pytest.raises(ValueError):
        mission.define(tmp_path, spec, session='owner')


def test_symlinks_are_not_read_and_cannot_certify_a_node(mapped, mission, tmp_path):
    outside = tmp_path / 'secret'; outside.write_text('DO NOT READ')
    p = mapped / 'src/parser.txt'; p.unlink(); p.symlink_to(outside)
    assert check(mission, mapped, 'parser')['result'] == 'BLOCKED'
    data = mission.project(mapped)
    assert by_id(data, 'parser')['state'] != 'verified'
    assert 'DO NOT READ' not in json.dumps(data)


def test_missing_declared_input_cannot_pass(mapped, mission):
    (mapped / 'src/parser.txt').unlink()
    assert check(mission, mapped, 'parser')['result'] == 'BLOCKED'


def test_definition_updates_require_current_revision_when_supplied(mapped, mission):
    with pytest.raises(ValueError, match='revision'):
        mission.define(mapped, definition(), session='late-writer', expected_revision=0)


def test_task_completion_does_not_create_verification_or_a_fix(mapped, mission):
    conn = db.connect(mapped)
    db.upsert(conn, 'ops', 'id', {'id': 'op-1', 'intent': 'Fix', 'status': 'open'})
    db.upsert(conn, 'tasks', 'id', {'id': 't-1', 'op': 'op-1', 'title': 'Repair', 'statement': 'Repair', 'status': 'done'})
    conn.close()
    mission.link(mapped, 'parser', 't-1', 'fix', session='owner')
    node = by_id(mission.project(mapped), 'parser')
    assert node['state'] == 'unknown'
    assert node['last_fix'] is None
    assert node['tasks'][0]['id'] == 't-1'


def test_all_declared_checks_are_required_and_empty_checks_do_not_pass(mapped, mission):
    spec = definition(); spec['nodes'][0]['checks'] = ['behavior', 'integration']
    mission.define(mapped, spec, session='owner')
    check(mission, mapped, 'parser')
    assert by_id(mission.project(mapped), 'parser')['state'] != 'verified'
    mission.run_check(mapped, 'parser', 'integration', [sys.executable, '-c', 'print(1)'], session='checker')
    assert by_id(mission.project(mapped), 'parser')['state'] == 'verified'
    spec['nodes'][0]['checks'] = []
    mission.define(mapped, spec, session='owner')
    assert by_id(mission.project(mapped), 'parser')['state'] == 'unknown'


def test_malformed_record_definition_fails_visible_and_readonly(mapped, mission):
    conn = db.connect(mapped)
    db.append_event(conn, session='bad', actor='fixture', kind='mission-definition', data={'definition': {'nodes': 'wrong'}})
    before = conn.execute('SELECT count(*) FROM events').fetchone()[0]
    payload = mission.project(mapped)
    assert payload['errors'] and not payload['configured']
    assert conn.execute('SELECT count(*) FROM events').fetchone()[0] == before
    conn.close()


def test_cli_records_an_executed_check(mapped, mission, monkeypatch, capsys):
    from alpaca import cli
    monkeypatch.setattr(cli, '_root', lambda: str(mapped))
    assert cli.main(['mission', 'check', '--check', 'behavior', 'parser', '--', sys.executable, '-c', 'print("real check")']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['result'] == 'PASS'
    assert 'real check' in (mapped / output['evidence']).read_text()


def test_seed_inventory_has_ten_areas_and_resolvable_dependency_evidence(mission):
    from alpaca import mission_model
    root = Path(__file__).resolve().parents[2]
    data = mission_model.validate(json.loads((root / 'alpaca/mission-presets/alpaca.json').read_text()))
    assert len(data['groups']) == 10
    for edge in data['edges']:
        for name in edge['evidence']:
            # UI module is supplied by the next implementation task.
            if name != 'alpaca/web/mission.js': assert (root / name).is_file(), name


def test_collector_captures_changes_without_recursive_snapshot_events(mapped, mission):
    from alpaca.observability import consumers
    assert 'mission' in consumers.CONSUMERS
    callback = consumers.CONSUMERS['mission']
    (mapped / 'src/parser.txt').write_text('updated')
    callback(mapped, [])
    conn = db.connect_readonly(mapped)
    before = conn.execute("SELECT count(*) FROM events WHERE kind='mission-snapshot'").fetchone()[0]
    callback(mapped, [])
    assert conn.execute("SELECT count(*) FROM events WHERE kind='mission-snapshot'").fetchone()[0] == before
    assert by_id(mission.project(mapped), 'parser')['last_changed']['source'] == 'record'
    conn.close()


def test_latest_check_timestamp_uses_run_order_not_declaration_order(mapped, mission):
    spec = definition(); spec['nodes'][0]['checks'] = ['behavior', 'integration']
    mission.define(mapped, spec, session='owner')
    mission.run_check(mapped, 'parser', 'integration', [sys.executable, '-c', 'print(1)'], session='checker')
    latest = check(mission, mapped, 'parser')
    assert by_id(mission.project(mapped), 'parser')['last_checked']['event_id'] == latest['event_id']


def test_deleted_explicit_input_cannot_remain_green_when_other_sources_exist(mapped, mission):
    spec = definition(); spec['nodes'][0]['sources'].append('src/usage.txt')
    mission.define(mapped, spec, session='owner')
    (mapped / 'src/parser.txt').unlink()
    assert check(mission, mapped, 'parser')['result'] == 'BLOCKED'


def test_unrelated_map_label_does_not_invalidate_a_capability(mapped, mission):
    check(mission, mapped, 'parser')
    spec = definition(); spec['title'] = 'Renamed map'
    mission.define(mapped, spec, session='owner')
    assert by_id(mission.project(mapped), 'parser')['state'] == 'verified'


def test_verified_fix_requires_sealed_completion_and_survives_later_drift(mapped, mission, monkeypatch):
    from alpaca import cli, ops
    from alpaca.tests import proofkit
    monkeypatch.setattr(cli, '_root', lambda: str(mapped))
    conn = db.connect(mapped)
    db.upsert(conn, 'ops', 'id', {'id': 'op-1', 'intent': 'Repair parser', 'status': 'open'})
    task = ops.add_task(conn, 'op-1', 'Repair parser', 'Repair parser', session='worker')
    conn.close()
    mission.link(mapped, 'parser', task, 'fix', session='owner')
    pointer = proofkit.seal_for(str(mapped), task)
    assert cli.main(['task', 'move', task, 'done', '--proof', pointer]) == 0
    assert by_id(mission.project(mapped), 'parser')['last_fix'] is None
    check(mission, mapped, 'parser')
    node = by_id(mission.project(mapped), 'parser')
    assert node['last_fix']['task'] == task
    (mapped / 'src/parser.txt').write_text('later work')
    later = by_id(mission.project(mapped), 'parser')
    assert later['state'] == 'stale'
    assert later['last_fix'] == node['last_fix']


def test_check_evidence_reader_returns_only_recorded_untampered_logs(mapped, mission):
    result = check(mission, mapped, 'parser', 'print("known evidence")')
    assert 'known evidence' in mission.evidence(mapped, result['event_id'])['text']
    with pytest.raises(ValueError): mission.evidence(mapped, 1)
    (mapped / result['evidence']).write_text('changed')
    with pytest.raises(ValueError, match='evidence'): mission.evidence(mapped, result['event_id'])


def test_new_historical_snapshot_preserves_evidence_failure(mapped, mission):
    result = check(mission, mapped, 'parser')
    good = mission.scan(mapped, session='collector', force=True)
    (mapped / result['evidence']).write_text('tampered')
    bad = mission.scan(mapped, session='collector', force=True)
    assert by_id(mission.project(mapped, at=good['id']), 'parser')['state'] == 'verified'
    assert by_id(mission.project(mapped, at=bad['id']), 'parser')['state'] == 'blocked'


def test_unknown_check_result_cannot_become_green(mapped, mission):
    result = check(mission, mapped, 'parser')
    result['result'] = 'SKIPPED'
    conn = db.connect(mapped)
    db.append_event(conn, session='fixture', actor='fixture', kind='mission-check', ref='parser', data=result)
    conn.close()
    assert by_id(mission.project(mapped), 'parser')['state'] == 'blocked'


def test_wildcard_input_with_excluded_symlink_subtree_cannot_pass(mapped, mission, tmp_path):
    spec = definition(); spec['nodes'][0]['sources'] = ['src/**/*.txt']
    (mapped / 'src/real').mkdir(); (mapped / 'src/real/a.txt').write_text('real')
    outside = tmp_path / 'external'; outside.mkdir(); (outside / 'a.txt').write_text('external')
    (mapped / 'src/linked').symlink_to(outside, target_is_directory=True)
    mission.define(mapped, spec, session='owner')
    assert check(mission, mapped, 'parser')['result'] == 'BLOCKED'


def test_history_payload_does_not_multiply_whole_inventory_per_node(mapped, mission):
    for i in range(150):
        (mapped / 'src' / ('unmapped-' + str(i) + '-' + 'x' * 90)).write_text('inventory')
    for i in range(5):
        (mapped / 'src/parser.txt').write_text(str(i))
        mission.scan(mapped, session='collector')
    # Five changes should produce small history entries, not fifteen copies of the inventory.
    assert len(json.dumps(mission.project(mapped))) < 100000
