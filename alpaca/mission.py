"""Mission-map events and honest, read-only projections over the shared record."""
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

from alpaca import db, util
from alpaca import mission_model as model

PREFIX = 'mission-'
MAX_EVENTS = 10000


def _event(row):
    if row is None: return None
    return {**dict(row), 'data': json.loads(row['data'])}


def _latest(conn, kind, at=None):
    sql = 'SELECT * FROM events WHERE kind=?'
    args = [PREFIX + kind]
    if at is not None: sql += ' AND id<=?'; args.append(at)
    return _event(conn.execute(sql + ' ORDER BY id DESC LIMIT 1', args).fetchone())


def _definition(conn, at=None):
    event = _latest(conn, 'definition', at)
    return (model.validate(event['data']['definition']), event) if event else (model.starter(), None)


def _append(conn, kind, data, session, ref=None):
    return db.append_event(conn, session=session, actor=session, kind=PREFIX + kind, ref=ref, data=data)


def _tasks(conn):
    from alpaca import work_record
    fields = ('id', 'op', 'title', 'status', 'claimant', 'assigned_session', 'completed_at',
              'completed_session', 'proof', 'updated_at', 'lease_until', 'phase', 'reason', 'statement')
    return [{key: row.get(key) for key in fields} for row in work_record.tasks(conn)]


def _development(root, conn, tasks):
    from alpaca import profile, taskplan
    error = None
    try:
        stages = profile.strict(root, 'check', root)
    except Exception:
        stages = []
        error = 'Profile evidence is unavailable. Stage-gated tasks must wait for a current check.'
    return {'tasks': tasks, 'stage_error': error, **taskplan.project(conn, tasks, root=root, stages=stages)}


def define(root, data, *, session, expected_revision=None):
    definition = model.validate(data)
    conn = db.connect(root)
    try:
        with db.transaction(conn):
            previous = _latest(conn, 'definition')
            revision = previous['id'] if previous else 0
            if expected_revision is not None and revision != expected_revision:
                raise ValueError('map revision changed to %s; reload before editing' % revision)
            event = _append(conn, 'definition', {'definition': definition}, session)
    finally:
        conn.close()
    scan(root, session=session)
    return event


def scan(root, *, session='observability-collector', force=False):
    """Capture source changes once. No definition means no runtime state is created."""
    conn = db.connect_readonly(root)
    try:
        definition, event = _definition(conn)
        if event is None: return None
        previous = _latest(conn, 'snapshot')
        tasks = _tasks(conn)
        development = _development(root, conn, tasks)
        validity, seen = {}, set()
        for row in conn.execute("SELECT * FROM events WHERE kind='mission-check' ORDER BY id DESC LIMIT ?", (MAX_EVENTS,)):
            recorded = _event(row)
            key = (recorded['data'].get('node'), recorded['data'].get('check'))
            if key not in seen:
                seen.add(key)
                validity[str(recorded['id'])] = _evidence_ok(root, recorded['data'])
    finally:
        conn.close()
    observation = model.observe(root, definition)
    observation['evidence_validity'] = validity
    signature = model.digest({'definition': event['id'], 'observation': observation, 'tasks': tasks,
                              'development': development})
    if not force and previous and previous['data'].get('signature') == signature: return None
    old = previous['data'].get('observation', {}).get('nodes', {}) if previous else {}
    changed = [ident for ident, value in observation['nodes'].items()
               if old.get(ident, {}).get('own_digest') != value['own_digest']]
    affected = [ident for ident, value in observation['nodes'].items()
                if old.get(ident, {}).get('digest') != value['digest'] and ident not in changed]
    conn = db.connect(root)
    try:
        with db.transaction(conn):
            current = _latest(conn, 'definition')
            if current['id'] != event['id']: raise ValueError('definition changed during scan; retry')
            latest = _latest(conn, 'snapshot')
            if not force and latest and latest['data'].get('signature') == signature: return None
            return _append(conn, 'snapshot', {'definition_revision': event['id'], 'observation': observation,
                                             'signature': signature, 'changed': changed, 'affected': affected,
                                             'initialized': [ident for ident in observation['nodes'] if ident not in old],
                                             'tasks': tasks, 'development': development}, session)
    finally:
        conn.close()


def link(root, node_id, task_id, kind, *, session):
    if kind not in ('work', 'fix'): raise ValueError('link kind must be work or fix')
    conn = db.connect(root)
    try:
        definition, _ = _definition(conn)
        if node_id not in {n['id'] for n in definition['nodes']}: raise ValueError('unknown capability')
        if not db.rows(conn, 'tasks', 'id=?', (task_id,)): raise ValueError('unknown task')
        event = _append(conn, 'link', {'node': node_id, 'task': task_id, 'kind': kind}, session, node_id)
    finally:
        conn.close()
    scan(root, session=session, force=True)
    return event


def _evidence_ok(root, data):
    name = data.get('evidence', '')
    if not isinstance(name, str) or not name.startswith('.alpaca/mission/checks/') or '..' in Path(name).parts:
        return False
    path = model.safe_path(root, name)
    try:
        return path is not None and path.stat().st_size <= 16 * 1024 * 1024 and hashlib.sha256(path.read_bytes()).hexdigest() == data.get('evidence_sha256')
    except OSError:
        return False


def evidence(root, event_id):
    conn = db.connect_readonly(root)
    try:
        row = conn.execute("SELECT * FROM events WHERE id=? AND kind='mission-check'", (int(event_id),)).fetchone()
        event = _event(row)
    finally:
        conn.close()
    if not event: raise ValueError('no recorded mission check at this event')
    if not _evidence_ok(root, event['data']): raise ValueError('check evidence is missing or changed')
    path = model.safe_path(root, event['data']['evidence'])
    with path.open('rb') as stream:
        content = stream.read(65537)
    return {'event_id': event['id'], 'text': content[:65536].decode('utf-8', 'replace'), 'truncated': len(content) > 65536}


def run_check(root, node_id, check_id, command, *, session, timeout=300):
    root = Path(root).resolve()
    if not isinstance(command, list) or not command or not all(isinstance(c, str) and c for c in command):
        raise ValueError('a check requires an explicit command argument list')
    if not 1 <= timeout <= 3600: raise ValueError('timeout must be between 1 and 3600 seconds')
    conn = db.connect_readonly(root)
    try:
        definition, revision = _definition(conn)
    finally:
        conn.close()
    node = next((n for n in definition['nodes'] if n['id'] == node_id), None)
    if not revision or not node or check_id not in node['checks']:
        raise ValueError('check must name a declared capability and check ID')
    before = model.observe(root, definition)['nodes'][node_id]
    log = root / '.alpaca' / 'mission' / 'checks' / (uuid.uuid4().hex + '.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    started = util.now_iso()
    result, reason, returncode = 'BLOCKED', '', None
    with log.open('wb') as output:
        output.write(('Command: ' + json.dumps(command) + '\n').encode()); output.flush()
        if before['errors']:
            reason = '; '.join(before['errors'])
            output.write(reason.encode())
        else:
            try:
                completed = subprocess.run(command, cwd=root, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
                returncode = completed.returncode
                result = 'PASS' if returncode == 0 else 'FAIL'
                reason = 'Command exited %s' % returncode
            except subprocess.TimeoutExpired:
                reason = 'Check timed out'; output.write(b'\nCheck timed out\n')
            except OSError as exc:
                reason = str(exc); output.write(reason.encode())
    after = model.observe(root, definition)['nodes'][node_id]
    conn = db.connect(root)
    try:
        with db.transaction(conn):
            current = _latest(conn, 'definition')
            if current['id'] != revision['id'] or after['digest'] != before['digest'] or after['errors']:
                result, reason = 'BLOCKED', 'Inputs or map definition changed during the check'
            if log.stat().st_size > 16 * 1024 * 1024:
                result, reason = 'BLOCKED', 'Check log exceeds the 16 MiB verification limit'
            data = {'node': node_id, 'check': check_id, 'command': command, 'result': result, 'reason': reason,
                    'returncode': returncode, 'started_at': started, 'input_digest': before['digest'],
                    'definition_revision': revision['id'], 'evidence': log.relative_to(root).as_posix(),
                    'evidence_sha256': hashlib.sha256(log.read_bytes()).hexdigest()}
            event = _append(conn, 'check', data, session, node_id)
    finally:
        conn.close()
    scan(root, session=session, force=True)
    if result == 'PASS': _record_fixes(root, node_id, session)
    return {**data, 'event_id': event['id']}


def _record_fixes(root, node_id, session):
    payload = project(root)
    node = next(n for n in payload['nodes'] if n['id'] == node_id)
    if node['state'] != 'verified': return
    from alpaca import proof
    conn = db.connect(root)
    try:
        for task in node['tasks']:
            if task['link_kind'] != 'fix' or task['status'] != 'done' or not task.get('completed_at') or not task.get('proof'): continue
            if any(c['ts'] < task['completed_at'] for c in node['checks']): continue
            if not proof.verify(conn, root, task['id'])[0]: continue
            prior = conn.execute("SELECT id FROM events WHERE kind='mission-fix' AND ref=? AND json_extract(data,'$.task')=?",
                                 (node_id, task['id'])).fetchone()
            if prior: continue
            _append(conn, 'fix', {'node': node_id, 'task': task['id'], 'proof': task['proof'],
                                   'check_events': [c['id'] for c in node['checks']], 'input_digest': node['input_digest']}, session, node_id)
    finally:
        conn.close()


def _state(root, node, observation, checks, historical, evidence_validity):
    if observation.get('errors'): return 'blocked', '; '.join(observation['errors'])
    if not node['checks']: return 'unknown', 'No checks declared for this capability'
    if not checks: return 'unknown', 'No verification recorded'
    if any(e['data'].get('result') not in ('PASS', 'FAIL', 'BLOCKED') for e in checks):
        return 'blocked', 'A recorded check has an unsupported result'
    if historical and any(evidence_validity.get(str(e['id'])) is not True for e in checks):
        return 'blocked', 'Evidence was unavailable or unverified at this snapshot'
    if not historical and any(not _evidence_ok(root, e['data']) for e in checks):
        return 'blocked', 'Recorded check evidence is missing or changed'
    current = [e for e in checks if e['data'].get('input_digest') == observation.get('digest')]
    if any(e['data']['result'] == 'FAIL' for e in current): return 'failing', 'A check fails for the current inputs'
    if any(e['data']['result'] == 'BLOCKED' for e in current): return 'blocked', 'A check could not complete: ' + next(e['data'].get('reason', '') for e in current if e['data']['result'] == 'BLOCKED')
    if len(current) != len(checks): return 'stale', 'Source or dependency inputs changed since verification'
    if len(checks) != len(node['checks']): return 'stale', 'Some declared checks have not run'
    return 'verified', 'All declared checks pass for these inputs'


def project(root, tasks=(), at=None):
    """Fold a map without writing. Historical views use only recorded observations."""
    if at is not None:
        if isinstance(at, bool) or int(at) < 1: raise ValueError('snapshot must be a positive event ID')
        at = int(at)
    conn = db.connect_readonly(root)
    errors = []
    try:
        try:
            definition, revision = _definition(conn, at)
        except (ValueError, TypeError, KeyError) as exc:
            definition, revision = model.starter(), None
            errors.append('Map definition unavailable: ' + str(exc))
        snapshot = _latest(conn, 'snapshot', at)
        if at is not None:
            if not snapshot or not revision or snapshot['data'].get('definition_revision') != revision['id']:
                raise ValueError('no source snapshot for that map revision')
            observation = snapshot['data']['observation']
            tasks = snapshot['data'].get('tasks', [])
            development = snapshot['data'].get('development', {'tasks': tasks, 'operations': [], 'unavailable': True})
        else:
            observation = model.observe(root, definition)
            tasks = list(tasks) if tasks else _tasks(conn)
            development = _development(root, conn, tasks)
        rows = conn.execute("SELECT * FROM events WHERE kind LIKE 'mission-%' AND id<=? ORDER BY id DESC LIMIT ?",
                            (at if at is not None else 2**63-1, MAX_EVENTS + 1)).fetchall()
        if len(rows) > MAX_EVENTS: errors.append('Map history exceeds the loaded event limit; verification is incomplete')
        events = []
        for row in reversed(rows[:MAX_EVENTS]):
            try: events.append(_event(row))
            except (ValueError, TypeError): errors.append('Unreadable mission event %s' % row['id'])
        available = conn.execute("SELECT id,ts,data FROM events WHERE kind='mission-snapshot' ORDER BY id DESC LIMIT 50").fetchall()
        snapshots = [{'id': r['id'], 'ts': r['ts']} for r in available]
    finally:
        conn.close()
    links, latest_checks, fixes = {}, {}, {}
    for e in events:
        d = e['data']
        if not isinstance(d, dict): errors.append('Invalid mission event %s' % e['id']); continue
        if e['kind'] == 'mission-link': links[(d.get('node'), d.get('task'))] = d.get('kind')
        if e['kind'] == 'mission-check': latest_checks[(d.get('node'), d.get('check'))] = e
        if e['kind'] == 'mission-fix': fixes[d.get('node')] = {**d, 'ts': e['ts'], 'event_id': e['id']}
    nodes = []
    task_by_id = {t['id']: t for t in tasks}
    for node in definition['nodes']:
        nid = node['id']; obs = observation['nodes'].get(nid, {'errors': ['No source observation']})
        checks = sorted([latest_checks[(nid, c)] for c in node['checks'] if (nid, c) in latest_checks], key=lambda e: e['id'])
        state, reason = _state(root, node, obs, checks, at is not None, observation.get('evidence_validity', {}))
        if revision is None: state, reason = 'unknown', 'Configure this starter map for your project'
        elif errors: state, reason = 'blocked', '; '.join(errors)
        linked = [{**task_by_id[tid], 'link_kind': kind} for (n, tid), kind in links.items() if n == nid and tid in task_by_id]
        active = [t for t in linked if t['status'] not in ('done', 'cancelled', 'canceled')]
        work = 'active' if any(t['status'] == 'doing' for t in active) else 'waiting' if any(t['status'] == 'blocked' for t in active) else 'assigned' if active else 'idle'
        history = [e for e in events if e.get('ref') == nid or e['kind'] == 'mission-snapshot' and nid in e['data'].get('changed', [])]
        changes = [e for e in history if e['kind'] == 'mission-snapshot']
        last_change = {'ts': changes[-1]['ts'], 'event_id': changes[-1]['id'], 'source': 'record',
                       'baseline': nid in changes[-1]['data'].get('initialized', [])} if changes else None
        recorded = snapshot['data']['observation']['nodes'].get(nid, {}) if snapshot else {}
        if at is None and obs.get('own_digest') != recorded.get('own_digest'):
            last_change = {'ts': obs.get('modified'), 'source': 'filesystem', 'unrecorded': True}
        compact_history = [{**{k: e.get(k) for k in ('id', 'kind', 'ts', 'session', 'ref')},
                            'data': {k: e['data'][k] for k in ('result', 'check', 'node', 'task', 'kind', 'reason') if k in e['data']}}
                           for e in reversed(history[-30:])]
        nodes.append({**node, 'state': state, 'reason': reason, 'work': work, 'tasks': linked,
                      'input_digest': obs.get('digest'), 'files': obs.get('files', []), 'upstream': obs.get('upstream', []),
                      'last_changed': last_change, 'last_checked': {**checks[-1]['data'], 'ts': checks[-1]['ts'], 'event_id': checks[-1]['id']} if checks else None,
                      'last_fix': fixes.get(nid), 'declared_checks': node['checks'], 'checks': checks, 'history': compact_history})
    counts = {s: sum(n['state'] == s and n['lifecycle'] == 'current' for n in nodes) for s in ('verified', 'failing', 'stale', 'unknown', 'blocked')}
    counts['active'] = sum(n['work'] == 'active' for n in nodes)
    groups = []
    for group in definition['groups']:
        children = [n for n in nodes if n['group'] == group['id'] and n['lifecycle'] == 'current']
        states = {s: sum(n['state'] == s for n in children) for s in counts if s != 'active'}
        state = next((s for s in ('failing', 'blocked', 'stale', 'unknown', 'verified') if states[s]), 'unknown')
        groups.append({**group, 'state': state, 'counts': states, 'total': len(children), 'active': sum(n['work'] == 'active' for n in children)})
    return {'version': 1, 'configured': revision is not None, 'revision': revision['id'] if revision else 0,
            'title': definition['title'], 'purpose': definition['purpose'], 'definition': definition,
            'groups': groups, 'nodes': nodes, 'edges': definition['edges'], 'counts': counts,
            'development': development,
            'coverage': observation['coverage'], 'errors': errors + observation.get('errors', []),
            'historical': at is not None, 'at': snapshot['id'] if at and snapshot else None,
            'snapshots': snapshots, 'checked_at': util.now_iso()}
