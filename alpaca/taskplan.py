"""Recorded task relationships and read-only readiness, separate from execution gates."""
import json
import re
from contextlib import nullcontext
from datetime import datetime

from alpaca import db, util

KIND = 'task-plan'
MAX_TASKS, MAX_GROUPS = 500, 80
LABELS = {'unplanned': 'Needs organization', 'waiting': 'Waiting on prerequisites', 'approval': 'Needs approval',
          'resources': 'Waiting for resources', 'blocked': 'Blocked', 'ready': 'Ready',
          'working': 'In progress', 'assignment_expired': 'Assignment expired',
          'failed': 'Failed', 'skipped': 'Skipped', 'done': 'Done'}


def latest(conn, op):
    row = conn.execute('SELECT id,data FROM events WHERE kind=? AND op=? ORDER BY id DESC LIMIT 1',
                       (KIND, op)).fetchone()
    if row is None:
        return {'version': 1, 'revision': 0, 'groups': [], 'tasks': []}
    try:
        data = json.loads(row['data'])
        if not isinstance(data, dict):
            raise ValueError('plan must be an object')
    except (TypeError, ValueError) as exc:
        raise ValueError('The latest plan is unreadable') from exc
    return dict(data, revision=row['id'])


def _text(value, name, limit=120):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError('%s must be nonempty text of at most %d characters' % (name, limit))
    return value.strip()


def _fields(value, keys, name, optional=()):
    if not isinstance(value, dict) or not set(keys) <= set(value) or set(value) - set(keys) - set(optional):
        raise ValueError('%s needs fields %s; optional: %s' % (name, ', '.join(keys), ', '.join(optional)))


def _names(value, name, limit=MAX_TASKS):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError('%s must be an explicit list of at most %d names' % (name, limit))
    result = [_text(v, name) for v in value]
    if len(set(result)) != len(result):
        raise ValueError('%s contains duplicates' % name)
    return result


def validate(data, tasks):
    _fields(data, ('version', 'revision', 'groups', 'tasks'), 'plan', ('title', 'summary'))
    metadata = {key: _text(data[key], 'plan ' + key, 80 if key == 'title' else 240)
                for key in ('title', 'summary') if key in data}
    if type(data['version']) is not int or data['version'] != 1:
        raise ValueError('plan version must be 1')
    if type(data['revision']) is not int or data['revision'] < 0:
        raise ValueError('plan revision must be a nonnegative event ID')
    groups, entries = data['groups'], data['tasks']
    if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
        raise ValueError('plan allows at most %d groups' % MAX_GROUPS)
    if not isinstance(entries, list) or len(entries) > MAX_TASKS:
        raise ValueError('plan allows at most %d tasks' % MAX_TASKS)
    group_ids, clean_groups = set(), []
    for group in groups:
        _fields(group, ('id', 'title'), 'group', ('category',))
        ident = _text(group['id'], 'group id', 60)
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', ident) or ident in group_ids:
            raise ValueError('group IDs must be unique ASCII identifiers')
        group_ids.add(ident)
        clean_groups.append({'id': ident, 'title': _text(group['title'], 'group title'),
                             **({'category': _text(group['category'], 'category', 60)} if 'category' in group else {})})
    ids, clean = set(), []
    for entry in entries:
        _fields(entry, ('id', 'group', 'after', 'resources', 'requires_stages', 'hold'), 'task entry')
        ident = _text(entry['id'], 'task id')
        if ident not in tasks or ident in ids:
            raise ValueError('task %s is missing, duplicated or belongs to another operation' % ident)
        if not isinstance(entry['group'], str) or entry['group'] not in group_ids:
            raise ValueError('task %s names an unknown group' % ident)
        ids.add(ident)
        hold = entry['hold']
        if hold is not None:
            _fields(hold, ('kind', 'reason'), 'hold')
            if hold['kind'] not in ('approval', 'resources', 'blocked'):
                raise ValueError('hold kind must be approval, resources or blocked')
            hold = {'kind': hold['kind'], 'reason': _text(hold['reason'], 'hold reason', 600)}
        clean.append({'id': ident, 'group': entry['group'], 'after': _names(entry['after'], 'after'),
                      'resources': _names(entry['resources'], 'resources', 40),
                      'requires_stages': _names(entry['requires_stages'], 'requires_stages', 80), 'hold': hold})
    for entry in clean:
        if entry['id'] in entry['after'] or any(t not in ids for t in entry['after']):
            raise ValueError('dependencies must name another task included in this plan')
    depths(clean)  # Also rejects cycles without recursive traversal.
    return {'version': 1, 'revision': data['revision'], 'groups': clean_groups, 'tasks': clean, **metadata}


def depths(entries):
    pending = {t['id']: set(t['after']) for t in entries}
    result = {}
    while pending:
        ready = [ident for ident, needs in pending.items() if needs <= result.keys()]
        if not ready:
            raise ValueError('task dependencies contain a cycle')
        for ident in ready:
            result[ident] = max((result[t] + 1 for t in pending.pop(ident)), default=0)
    return result


def record(conn, op, data, *, session, actor, in_transaction=False):
    if in_transaction and not conn.in_transaction:
        raise ValueError('plan append requires the caller transaction')
    with nullcontext() if in_transaction else db.transaction(conn):
        if not db.rows(conn, 'ops', 'id=?', (op,)):
            raise ValueError('no operation %s' % op)
        tasks = {t['id'] for t in db.rows(conn, 'tasks', 'op=?', (op,))}
        clean = validate(data, tasks)
        row = conn.execute('SELECT id FROM events WHERE kind=? AND op=? ORDER BY id DESC LIMIT 1', (KIND, op)).fetchone()
        revision = row['id'] if row else 0
        if clean['revision'] != revision:
            raise ValueError('plan revision changed to %d; read --show and reapply your changes' % revision)
        return db.append_event(conn, session=session, actor=actor, kind=KIND, op=op, ref=op,
                               data=clean, conn_in_txn=True)


def map_task(conn, op, ident, mapping, *, session, actor, in_transaction=False):
    """Edit one membership while preserving other entries and constraints atomically."""
    if in_transaction and not conn.in_transaction:
        raise ValueError('task mapping requires the caller transaction')
    with nullcontext() if in_transaction else db.transaction(conn):
        tasks = {t['id'] for t in db.rows(conn, 'tasks', 'op=?', (op,))}
        if ident not in tasks:
            raise ValueError('task must belong to the operation')
        _fields(mapping, (), 'mapping', ('group', 'inherit', 'after', 'resources', 'requires_stages', 'hold'))
        plan = validate(latest(conn, op), tasks)
        entries = {t['id']: t for t in plan['tasks']}
        old = entries.get(ident)
        changes = dict(mapping)
        if 'inherit' in changes:
            source = changes.pop('inherit')
            if 'group' in changes or source not in entries or source == ident:
                raise ValueError('inherit must name another mapped task in this operation, without --group')
            changes['group'] = entries[source]['group']
        if not old and not {'group', 'after', 'resources'} <= changes.keys():
            raise ValueError('new mapping needs --group or --inherit, --after or --independent, and --resource or --no-resources')
        entry = {**(old or {'id': ident, 'requires_stages': [], 'hold': None}), **changes}
        plan['tasks'] = [entry if t['id'] == ident else t for t in plan['tasks']] if old else plan['tasks'] + [entry]
        return record(conn, op, plan, session=session, actor=actor, in_transaction=True)


def group(conn, op, ident, title, category, *, session, actor):
    """Create or rename a workstream without replacing another planner's entries."""
    with db.transaction(conn):
        tasks = {t['id'] for t in db.rows(conn, 'tasks', 'op=?', (op,))}
        plan = validate(latest(conn, op), tasks)
        entry = {'id': ident, 'title': title, 'category': category}
        if any(g['id'] == ident for g in plan['groups']):
            plan['groups'] = [entry if g['id'] == ident else g for g in plan['groups']]
        else:
            plan['groups'].append(entry)
        return record(conn, op, plan, session=session, actor=actor, in_transaction=True)


def _lease_expired(task):
    lease = task.get('lease_until')
    if not task.get('claimant'):
        return False
    if not lease:
        return True
    try:
        return datetime.fromisoformat(lease) <= datetime.fromisoformat(util.now_iso())
    except (ValueError, TypeError):
        return True


def project(conn, tasks, *, root=None, stages=()):
    """Project relationships, not permission to launch. No writes or profile execution."""
    operators = {r['key'][9:]: r['value'] for r in conn.execute("SELECT key,value FROM meta WHERE key LIKE 'operator:%'")}
    by_id = {t['id']: t for t in tasks}
    plans, errors, entries = {}, {}, {}
    op_ids = list(dict.fromkeys(t.get('op') for t in tasks))
    for op in op_ids:
        try:
            snapshot = latest(conn, op)
            plan = validate(snapshot, {t['id'] for t in tasks if t.get('op') == op})
        except (ValueError, TypeError, KeyError) as exc:
            errors[op] = str(exc)
            plan = {'version': 1, 'revision': 0, 'groups': [], 'tasks': []}
        plans[op] = plan
        entries.update({t['id']: t for t in plan['tasks']})
    active = [t for t in tasks if t['status'] == 'doing']
    unknown_active = [t['id'] for t in active if t['id'] not in entries]
    stage_status = {s.get('stage'): str(s.get('status', s.get('verdict', 'UNKNOWN'))).upper() for s in stages}
    phase_open = set()
    for row in conn.execute("SELECT op,kind,data FROM events WHERE kind IN ('phase-enter','phase-advance') ORDER BY id"):
        try:
            data = json.loads(row['data'])
            phase = data.get('phase') if row['kind'] == 'phase-enter' else data.get('boundary', '').split('->')[-1]
            if phase:
                phase_open.add((row['op'], phase))
        except (ValueError, TypeError, AttributeError):
            continue
    evidence = {}
    for ident, task in by_id.items():
        if task['status'] != 'done' or ident not in entries:
            continue
        if root is None:
            evidence[ident] = 'unknown'
        else:
            from alpaca import proof
            try:
                ok, _ = proof.done_gate(conn, str(root), ident, task.get('proof'))
                evidence[ident] = 'valid' if ok else 'invalid'
            except (ValueError, OSError, TypeError, KeyError):
                evidence[ident] = 'unknown'
    nodes = {}
    for ident, task in by_id.items():
        entry = entries.get(ident)
        node = {'id': ident, 'state': 'unplanned', 'label': LABELS['unplanned'], 'after': [],
                'depth': 0, 'reasons': [], 'blockers': [], 'conflicts': [], 'alternatives': [],
                'resources': [], 'session': task.get('assigned_session'),
                'operator': operators.get(task.get('assigned_session')),
                'evidence': evidence.get(ident, 'not_completed')}
        nodes[ident] = node
        if entry is None:
            node['reasons'] = ['No task relationships or resource constraints are recorded.']
            continue
        node['after'], node['resources'] = entry['after'], entry['resources']
        node['blockers'] = [p for p in entry['after'] if evidence.get(p) != 'valid']
        for p in node['blockers']:
            node['reasons'].append('Needs %s%s' % (p, ' with valid completion evidence' if by_id[p]['status'] == 'done' else ' to finish'))
        for stage in entry['requires_stages']:
            if stage_status.get(stage) != 'PASS':
                node['reasons'].append('Needs current PASS evidence for stage %s' % stage)
        if task.get('phase') and (task.get('op'), task['phase']) not in phase_open:
            node['reasons'].append('Phase %s has not been opened' % task['phase'])
        state = 'waiting' if node['reasons'] else 'ready'
        conflicts = [t['id'] for t in active if t['id'] != ident and t['id'] in entries
                     and set(entry['resources']).intersection(entries[t['id']]['resources'])]
        unknown = [t for t in unknown_active if t != ident]
        node['conflicts'] = conflicts + unknown
        if conflicts:
            node['reasons'].append('Resources in use by ' + ', '.join(conflicts))
        if unknown:
            node['reasons'].append('Resource constraints not recorded for active work: ' + ', '.join(unknown))
        if node['conflicts'] and state == 'ready':
            state = 'resources'
        if entry['hold']:
            state = entry['hold']['kind']
            node['reasons'].append(entry['hold']['reason'])
        status = task['status']
        if status == 'blocked':
            state = 'blocked'
            node['reasons'].append(task.get('reason') or 'Task is recorded as blocked')
        elif status == 'doing':
            state = 'assignment_expired' if _lease_expired(task) else 'working'
            if state == 'assignment_expired':
                node['reasons'].append('Chat assignment expired; work may still be running')
        elif status in ('done', 'failed', 'skipped', 'cancelled', 'canceled'):
            state = status if status in LABELS else 'skipped'
            node['reasons'], node['blockers'], node['conflicts'] = [], [], []
            if status != 'done' and task.get('reason'):
                node['reasons'].append(task['reason'])
            if status == 'done' and node['evidence'] != 'valid':
                node['reasons'].append('Historical completion; proof is not currently verified')
        node['state'], node['label'] = state, LABELS[state]
    ready = [n for n in nodes.values() if n['state'] == 'ready']
    for node in ready:
        node['alternatives'] = [n['id'] for n in ready if n['id'] != node['id'] and set(n['resources']).intersection(node['resources'])]
        if node['alternatives']:
            node['reasons'].append('Shares exclusive resources with ' + ', '.join(node['alternatives']))
    operations = []
    for op, plan in plans.items():
        levels = depths(plan['tasks'])
        for ident, depth in levels.items():
            nodes[ident]['depth'] = depth
        ordered = sorted(plan['tasks'], key=lambda t: levels[t['id']])
        groups = [{**group, 'nodes': [nodes[t['id']] for t in ordered if t['group'] == group['id']]}
                  for group in plan['groups']]
        unplanned = [nodes[t['id']] for t in sorted(tasks, key=lambda t: t['id']) if t.get('op') == op and t['id'] not in entries]
        if unplanned:
            groups.append({'id': '__unplanned__', 'title': 'Needs organization', 'category': 'Needs organization', 'nodes': unplanned})
        operations.append({'op': op, 'revision': plan['revision'], 'error': errors.get(op), 'groups': groups,
                           'title': plan.get('title'), 'summary': plan.get('summary'),
                           'edges': [{'from': p, 'to': t['id']} for t in plan['tasks'] for p in t['after']]})
    return {'operations': operations}
