"""Plans express dependencies; neither list order nor a chat lease authorizes work."""
import copy
import json
from pathlib import Path

import pytest

from alpaca import cli, db, work_record
from alpaca.tests import proofkit


@pytest.fixture
def record(project):
    conn = db.connect(project)
    db.upsert(conn, 'ops', 'id', {'id': 'op-001', 'intent': 'Deliver a feature', 'status': 'open'})
    for n in range(1, 8):
        ident = 't-%03d' % n
        db.upsert(conn, 'tasks', 'id', {'id': ident, 'op': 'op-001', 'statement': 'Task ' + ident,
                                      'title': 'Task ' + ident, 'status': 'open'})
    yield Path(project), conn
    conn.close()


def plan():
    return {'version': 1, 'revision': 0, 'groups': [
        {'id': 'build', 'title': 'Implementation'}, {'id': 'docs', 'title': 'Documentation'}],
        'tasks': [entry('t-001'), entry('t-002', after=['t-001']),
                  entry('t-003', after=['t-002']), entry('t-004', after=['t-003']),
                  entry('t-005', group='docs'), entry('t-006', after=['t-004', 't-005'])]}


def entry(ident, group='build', after=(), **extra):
    return dict(id=ident, group=group, after=list(after), resources=[], requires_stages=[], hold=None, **extra)


def save(conn, data=None):
    from alpaca import taskplan
    return taskplan.record(conn, 'op-001', data or plan(), session='planner', actor='planner')


def projected(root, conn, stages=()):
    from alpaca import taskplan
    view = taskplan.project(conn, work_record.tasks(conn), root=root, stages=stages)
    op = next(o for o in view['operations'] if o['op'] == 'op-001')
    return op, {n['id']: n for g in op['groups'] for n in g['nodes']}


def finish(root, conn, ident):
    pointer = proofkit.seal_for(str(root), ident, conn=conn)
    db.patch(conn, 'tasks', 'id', ident, {'status': 'done', 'proof': pointer})
    return pointer


def test_cli_exposes_empty_plan_and_records_round_trip(record, capsys):
    root, conn = record
    assert cli.main(['task', 'plan', 'op-001', '--show']) == 0
    assert json.loads(capsys.readouterr().out)['revision'] == 0
    path = root / 'plan.json'
    path.write_text(json.dumps(plan()))
    assert cli.main(['task', 'plan', 'op-001', '--file', str(path)]) == 0
    capsys.readouterr()
    assert cli.main(['task', 'plan', 'op-001', '--show']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['tasks'][1]['after'] == ['t-001']
    assert result['revision'] > 0


def test_chain_parallel_branch_join_and_unplanned_task(record):
    root, conn = record
    save(conn)
    before = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    op, nodes = projected(root, conn)
    assert [nodes['t-%03d' % n]['depth'] for n in (1, 2, 3, 4, 5, 6)] == [0, 1, 2, 3, 0, 4]
    assert nodes['t-001']['state'] == nodes['t-005']['state'] == 'ready'
    assert nodes['t-002']['blockers'] == ['t-001']
    assert nodes['t-006']['blockers'] == ['t-004', 't-005']
    assert nodes['t-007']['state'] == 'unplanned'
    assert op['groups'][-1]['id'] == '__unplanned__'
    assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == before
    finish(root, conn, 't-004')
    assert projected(root, conn)[1]['t-006']['blockers'] == ['t-005']
    finish(root, conn, 't-005')
    assert projected(root, conn)[1]['t-006']['state'] == 'ready'


@pytest.mark.parametrize('change', ['cycle', 'self', 'unknown', 'foreign', 'duplicate', 'group', 'missing', 'typo'])
def test_invalid_plan_is_not_recorded(record, change):
    root, conn = record
    data = plan()
    if change == 'cycle': data['tasks'][0]['after'] = ['t-004']
    if change == 'self': data['tasks'][0]['after'] = ['t-001']
    if change == 'unknown': data['tasks'][0]['after'] = ['t-999']
    if change == 'foreign': db.patch(conn, 'tasks', 'id', 't-001', {'op': 'op-002'})
    if change == 'duplicate': data['tasks'].append(copy.deepcopy(data['tasks'][0]))
    if change == 'group': data['tasks'][0]['group'] = 'missing'
    if change == 'missing': del data['tasks'][0]['resources']
    if change == 'typo': data['tasks'][0]['depend_on'] = ['t-001']
    before = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    with pytest.raises(ValueError): save(conn, data)
    assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == before


def test_revision_prevents_lost_updates(record):
    root, conn = record
    first = save(conn)
    with pytest.raises(ValueError, match='revision'): save(conn)
    data = plan(); data['revision'] = first['id']; data['groups'][0]['title'] = 'Updated'
    second = save(conn, data)
    assert second['id'] > first['id']
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='task-plan'").fetchone()[0] == 2


def test_completion_without_seal_and_changed_report_do_not_unlock(record):
    root, conn = record
    save(conn)
    db.patch(conn, 'tasks', 'id', 't-001', {'status': 'done', 'proof': 'local:absent.md'})
    assert projected(root, conn)[1]['t-002']['state'] == 'waiting'
    pointer = finish(root, conn, 't-001')
    assert projected(root, conn)[1]['t-002']['state'] == 'ready'
    (root / pointer[6:]).write_text('Changed report after completion')
    nodes = projected(root, conn)[1]
    assert nodes['t-001']['state'] == 'done'
    assert nodes['t-001']['evidence'] == 'invalid'
    assert nodes['t-002']['state'] == 'waiting'


def test_current_stage_gate_and_explicit_hold(record):
    root, conn = record
    data = plan(); data['tasks'][0]['requires_stages'] = ['environment']
    data['tasks'][4]['hold'] = {'kind': 'approval', 'reason': 'Owner chooses the target'}
    save(conn, data)
    nodes = projected(root, conn)[1]
    assert nodes['t-001']['state'] == 'waiting'
    assert nodes['t-005']['state'] == 'approval'
    stages = [{'stage': 'environment', 'status': 'PASS'}]
    assert projected(root, conn, stages)[1]['t-001']['state'] == 'ready'
    stages[0]['status'] = 'STALE'
    assert projected(root, conn, stages)[1]['t-001']['state'] == 'waiting'


def test_resource_alternatives_and_expired_assignment_still_holds_resource(record):
    root, conn = record
    data = plan(); data['tasks'][0]['resources'] = data['tasks'][4]['resources'] = ['build-machine']
    save(conn, data)
    nodes = projected(root, conn)[1]
    assert nodes['t-001']['state'] == 'ready'
    assert nodes['t-001']['alternatives'] == ['t-005']
    db.patch(conn, 'tasks', 'id', 't-001', {'status': 'doing', 'claimant': 'worker', 'lease_until': '2001-01-01T00:00:00Z'})
    nodes = projected(root, conn)[1]
    assert nodes['t-001']['state'] == 'assignment_expired'
    assert nodes['t-005']['state'] == 'resources'
    assert nodes['t-005']['conflicts'] == ['t-001']


def test_unplanned_active_work_keeps_resources_unknown(record):
    root, conn = record
    save(conn)
    db.patch(conn, 'tasks', 'id', 't-007', {'status': 'doing'})
    nodes = projected(root, conn)[1]
    assert nodes['t-001']['state'] == 'resources'
    assert nodes['t-001']['conflicts'] == ['t-007']


def test_new_malformed_snapshot_is_visible_without_reviving_old_plan(record):
    root, conn = record
    save(conn)
    db.append_event(conn, session='legacy', actor='legacy', kind='task-plan', op='op-001', data={'broken': True})
    op, nodes = projected(root, conn)
    assert op['error']
    assert nodes['t-001']['state'] == 'unplanned'


def test_live_assignment_links_to_recorded_session(record):
    root, conn = record
    save(conn)
    db.patch(conn, 'tasks', 'id', 't-001', {'status': 'doing', 'claimant': 'worker', 'lease_until': '2999-01-01T00:00:00Z'})
    db.append_event(conn, session='chat-1', actor='worker', kind='claim', ref='t-001', data={'worker': 'worker'})
    db.meta_set(conn,'operator:chat-1','codex')
    node = projected(root, conn)[1]['t-001']
    assert node['operator'] == 'codex'
    assert node['state'] == 'working'
    assert node['session'] == 'chat-1'


def test_hub_and_generated_checklist_share_groups_and_dependencies(record):
    from alpaca import hub, hub_checklist
    root, conn = record
    save(conn)
    before = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    view = hub.overview(root)
    groups = view['roadmap']['operations'][0]['groups']
    assert [g['title'] for g in groups] == ['Implementation', 'Documentation', 'Needs organization']
    assert groups[0]['nodes'][1]['after'] == ['t-001']
    text = '\n'.join(hub_checklist.render(conn, []))
    assert 'Implementation' in text and 'Documentation' in text and 'Needs organization' in text
    assert 'Needs t-001 to finish' in text
    assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == before


def test_unopened_phase_is_not_ready(record):
    root, conn = record
    save(conn)
    db.patch(conn, 'tasks', 'id', 't-001', {'phase': 'build'})
    assert projected(root, conn)[1]['t-001']['state'] == 'waiting'
    db.append_event(conn, session='planner', actor='operator', kind='phase-advance', op='op-001',
                    data={'boundary': 'design->build', 'mode': 'human-go'})
    assert projected(root, conn)[1]['t-001']['state'] == 'ready'


def test_checklist_order_follows_dependencies_even_when_input_is_reversed(record):
    root, conn = record
    data = plan(); data['tasks'].reverse()
    save(conn, data)
    op, _ = projected(root, conn)
    assert [n['id'] for n in op['groups'][0]['nodes']] == ['t-001', 't-002', 't-003', 't-004', 't-006']


def test_completed_nodes_do_not_display_current_resource_waits(record):
    root, conn = record
    data = plan(); data['tasks'][0]['resources'] = data['tasks'][4]['resources'] = ['build-machine']
    save(conn, data)
    finish(root, conn, 't-001')
    db.patch(conn, 'tasks', 'id', 't-005', {'status': 'doing'})
    db.patch(conn, 'tasks', 'id', 't-007', {'status': 'doing'})
    node = projected(root, conn)[1]['t-001']
    assert node['state'] == 'done' and node['evidence'] == 'valid'
    assert node['reasons'] == [] and node['conflicts'] == [] and node['blockers'] == []


def test_display_metadata_survives_recording_and_projection(record):
    root, conn = record
    data = plan(); data.update(title='Release pipeline', summary='Move verified work into a public release.')
    data['groups'][0]['category'] = 'CI/CD'
    save(conn, data)
    op, _ = projected(root, conn)
    assert op['title'] == data['title'] and op['summary'] == data['summary']
    assert op['groups'][0]['category'] == 'CI/CD'


def test_group_and_mapped_task_creation_in_one_command(record, capsys):
    from alpaca import taskplan
    root, conn = record
    save(conn)
    assert cli.main(['task','group','op-001','release','--title','Release pipeline','--category','CI/CD']) == 0
    assert cli.main(['task','add','op-001','Ship the checked feature','--title','Ship feature',
                     '--group','release','--after','t-004','--no-resources']) == 0
    new = db.rows(conn,'tasks','id=?',('t-008',))[0]
    entry = next(t for t in taskplan.latest(conn,'op-001')['tasks'] if t['id']==new['id'])
    assert entry['group']=='release' and entry['after']==['t-004'] and entry['resources']==[]
    assert 't-008' in capsys.readouterr().out


def test_bad_mapping_rolls_back_task_and_all_events(record):
    from alpaca import ops
    root, conn = record
    save(conn)
    before = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    with pytest.raises(ValueError,match='group'):
        ops.add_task(conn,'op-001','New work','New work',mapping={'group':'missing','after':[],'resources':[]})
    assert not db.rows(conn,'tasks','id=?',('t-008',))
    assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]==before


def test_mapping_edits_preserve_unrelated_constraints_and_metadata(record):
    from alpaca import taskplan
    root, conn = record
    data=plan();data['title']='Feature delivery';data['tasks'][1]['resources']=['machine']
    data['tasks'][1]['hold']={'kind':'approval','reason':'Owner chooses target'}
    save(conn,data)
    taskplan.map_task(conn,'op-001','t-002',{'group':'docs'},session='planner',actor='planner')
    result=taskplan.latest(conn,'op-001')
    changed=next(t for t in result['tasks'] if t['id']=='t-002')
    assert changed=={**data['tasks'][1],'group':'docs'}
    assert result['title']=='Feature delivery'
    assert result['tasks'][0]==data['tasks'][0]
    taskplan.group(conn,'op-001','docs','Guides','Documentation',session='planner',actor='planner')
    assert taskplan.latest(conn,'op-001')['tasks']==result['tasks']


def test_new_mapping_requires_explicit_constraints_and_same_operation_inheritance(record):
    from alpaca import ops
    root, conn = record
    save(conn)
    for mapping in [{'group':'build'}, {'group':'build','after':[]}, {'inherit':'t-999','after':[],'resources':[]}]:
        with pytest.raises(ValueError):ops.add_task(conn,'op-001','New work','New work',mapping=mapping)
    assert len(db.rows(conn,'tasks'))==7
    ident=ops.add_task(conn,'op-001','Follow up','Follow up',mapping={'inherit':'t-002','after':['t-002'],'resources':[]})
    _,nodes=projected(root,conn)
    assert nodes[ident]['after']==['t-002']


def test_mapping_cycle_and_foreign_inheritance_preserve_latest_revision(record):
    from alpaca import taskplan,ops
    root,conn=record;save(conn)
    revision=taskplan.latest(conn,'op-001')['revision']
    with pytest.raises(ValueError,match='cycle'):
        taskplan.map_task(conn,'op-001','t-001',{'after':['t-004']},session='planner',actor='planner')
    db.upsert(conn,'tasks','id',{'id':'t-099','op':'op-002','statement':'Other operation','title':'Other','status':'open'})
    with pytest.raises(ValueError,match='inherit'):
        ops.add_task(conn,'op-001','Follow up','Follow up',mapping={'inherit':'t-099','after':[],'resources':[]})
    assert taskplan.latest(conn,'op-001')['revision']==revision


def test_required_planning_policy_prevents_unmapped_cli_creation(record,capsys):
    root,conn=record;save(conn)
    assert cli.main(['task','planning','--require'])==0
    before=len(db.rows(conn,'tasks'))
    assert cli.main(['task','add','op-001','Work','--title','Work'])!=0
    message=capsys.readouterr().out
    assert '--group' in message and '--independent' in message and '--no-resources' in message
    assert len(db.rows(conn,'tasks'))==before
    assert cli.main(['task','add','op-001','Work','--title','Work','--group','build','--independent','--no-resources'])==0
