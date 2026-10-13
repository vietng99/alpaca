"""(class A, field-copy drift): mission.py:316 counts a deferred linked task as active
work ('assigned'), and mission._tasks copies a fixed field tuple into taskplan, so any deferral
data the builder adds to work_record rows does not reach the mission's development roadmap: the
deferred node must still not be ready there, from the status alone."""
import importlib

import pytest

from alpaca.tests.not_now_kit import raw_defer
from alpaca import db, taskplan


def _definition():
    return {'version': 1, 'title': 'Example system', 'purpose': 'Trace work to evidence',
            'inventory': ['src'], 'groups': [{'id': 'core', 'title': 'Core'}],
            'nodes': [{'id': name, 'group': 'core', 'title': name.title(),
                       'purpose': 'Own ' + name, 'sources': ['src/' + name + '.txt'],
                       'checks': ['behavior'], 'tags': ['reliability'], 'lifecycle': 'current'}
                      for name in ('parser', 'usage')],
            'edges': [{'source': 'parser', 'target': 'usage', 'kind': 'feeds', 'confidence': 'confirmed',
                       'reason': 'usage consumes parser', 'evidence': ['src/usage.txt']}]}


@pytest.fixture
def mapped(tmp_path):
    mission = importlib.import_module('alpaca.mission')
    root = tmp_path / 'project'; (root / 'src').mkdir(parents=True)
    for name in ('parser', 'usage'):
        (root / 'src' / (name + '.txt')).write_text(name)
    mission.define(root, _definition(), session='owner', expected_revision=0)
    conn = db.connect(root)
    db.upsert(conn, 'ops', 'id', {'id': 'op-dev', 'intent': 'Develop', 'status': 'open'})
    for ident in ('t-a', 't-b'):
        db.upsert(conn, 'tasks', 'id', {'id': ident, 'op': 'op-dev', 'statement': ident,
                                        'title': ident, 'status': 'open'})
    plan = {'version': 1, 'revision': 0, 'groups': [{'id': 'g', 'title': 'Work'}],
            'tasks': [{'id': i, 'group': 'g', 'after': [], 'resources': [], 'requires_stages': [], 'hold': None}
                      for i in ('t-a', 't-b')]}
    taskplan.record(conn, 'op-dev', plan, session='owner', actor='owner')
    mission.link(root, 'parser', 't-a', 'work', session='owner')
    return mission, root, conn


def test_node_with_only_deferred_work_is_not_assigned(mapped):
    mission, root, conn = mapped
    node = next(n for n in mission.project(root)['nodes'] if n['id'] == 'parser')
    assert node['work'] == 'assigned', 'setup: an open linked task is assigned work'
    raw_defer(conn, 't-a')
    node = next(n for n in mission.project(root)['nodes'] if n['id'] == 'parser')
    assert node['work'] not in ('active', 'waiting', 'assigned'), node['work']


def test_mission_development_roadmap_never_shows_deferred_ready(mapped):
    mission, root, conn = mapped
    raw_defer(conn, 't-a')
    dev = mission.project(root)['development']
    got = {n['id']: n for op in dev['operations'] for g in op['groups'] for n in g['nodes']}
    assert got['t-a']['state'] != 'ready' and 'not now' in got['t-a']['label'].lower(), got['t-a']
    assert got['t-b']['state'] == 'ready'
