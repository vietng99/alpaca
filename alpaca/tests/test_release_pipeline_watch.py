"""An executed publish files the T+1h and T+24h watch tasks (op-057 t-620), against a local bare
remote and a fake pull request API; nothing touches GitHub."""
import dataclasses
from datetime import datetime
import json
from pathlib import Path
import shlex
import sqlite3
import yaml
import pytest
from alpaca import claims, cli, db, ops as _ops, taskcontract, taskplan
from alpaca.release import followup
from alpaca.release.receipts import PublishReceipt
from alpaca.tests.test_release_pipeline_build import source, api
from alpaca.tests.test_release_pipeline_checks import project
from alpaca.tests.test_release_pipeline_publish import ready
from alpaca.tests.test_runbook import ship_example

PR = 'https://github.com/sample/project/pull/1'
WINDOWS = {3600: 'T+1h', 86400: 'T+24h'}


class Kit:
    def __init__(self, ready, monkeypatch, capsys):
        self.root, self.remote, _, self.checks = ready
        self.monkeypatch, self.capsys = monkeypatch, capsys
        self.pr = lambda *a: PR
        self.stamps = []
        monkeypatch.setattr(cli, '_root', lambda: str(self.root))
        from alpaca.release.receipts import record_receipt
        self.check_receipt = str(record_receipt(self.root, 'test', self.checks))
        module = api('publish')
        original = module.publish_release

        def publish(root, config, checks, **kwargs):
            receipt = original(root, config, checks, remote=str(self.remote), api=lambda *a: self.pr(*a), **kwargs)
            if self.stamps:
                receipt.created_at = self.stamps.pop(0)
            return receipt
        monkeypatch.setattr(module, 'publish_release', publish)
        self.dry = self.run('publish', self.check_receipt)[1]['receipt']

    def run(self, *args, session='pub'):
        self.capsys.readouterr()
        code = cli.main(['--session', session, 'release', *args])
        out = self.capsys.readouterr().out
        return code, json.loads(out)

    def execute(self, *extra, session='pub'):
        return self.run('publish', self.check_receipt, '--execute', '--expect', self.dry, *extra, session=session)

    def conn(self):
        return db.connect(str(self.root))

    def op(self, status='open'):
        conn = self.conn()
        try:
            oid = _ops.open_op(conn, 'Ship the release', 'it is watched', session='owner', actor='owner')
            if status != 'open':
                db.patch(conn, 'ops', 'id', oid, {'status': status})
            return oid
        finally:
            conn.close()

    def hold(self, op, session, minutes=60):
        conn = self.conn()
        try:
            tid = _ops.add_task(conn, op, 'Work', 'Work', session='owner', actor='owner')
            claims.take(conn, tid, session, minutes=minutes, session=session)
            return tid
        finally:
            conn.close()

    def rows(self, sql, params=()):
        conn = self.conn()
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def watch_tasks(self, op=None):
        sql = "SELECT t.* FROM tasks t JOIN events e ON e.ref=t.id AND e.kind='task-add' " \
              "WHERE e.data LIKE '%release_watch%'"
        return self.rows(sql + (' AND t.op=?' if op else '') + ' ORDER BY t.id', (op,) if op else ())

    def contracts(self):
        conn = self.conn()
        try:
            return taskcontract.latest(conn)
        finally:
            conn.close()

    def receipt_file(self, out):
        return json.loads(Path(out['receipt']).read_text())


@pytest.fixture
def kit(ready, monkeypatch, capsys):
    return Kit(ready, monkeypatch, capsys)


def seconds(a, b):
    return (datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()


def due_of(kit, tid):
    lines = kit.contracts()[tid]['input']
    return [l.split()[1] for l in lines if l.startswith('Due ')][0]


def test_executed_publish_files_two_watch_tasks_due_1h_and_24h_after_publish(kit):
    op = kit.op()
    code, out = kit.execute('--watch-op', op)
    assert code == 0 and out['status'] == 'PASS' and out['pr_url'] == PR
    tasks = kit.watch_tasks()
    assert [t['id'] for t in tasks] == out['watch']['tasks'] and len(tasks) == 2
    assert all(t['op'] == op and t['status'] == 'open' for t in tasks)
    created = kit.receipt_file(out)['created_at']
    deltas = sorted(seconds(due_of(kit, t['id']), created) for t in tasks)
    # the sub-second digits of the receipt time are dropped, nothing more
    assert [3599 < d <= 3600 for d in deltas[:1]] == [True] and 86399 < deltas[1] <= 86400
    contract = kit.contracts()[tasks[0]['id']]
    for origin in contract['origins']['input']:
        assert origin['kind'] == 'derived' and origin['reason'].startswith('release:')
    assert 'UTC' in tasks[0]['statement'] and tasks[0]['statement'].isascii()
    # the group and an independent mapping
    plan = taskplan.latest(kit.conn(), op)
    assert [g['id'] for g in plan['groups']] == ['release-watch']
    assert all(t['group'] == 'release-watch' and t['after'] == [] and t['resources'] == [] for t in plan['tasks'])


def test_default_checks_are_listed_in_both_tasks(kit):
    kit.execute('--watch-op', kit.op())
    for t in kit.watch_tasks():
        text = ' '.join(kit.contracts()[t['id']]['done_bar']).lower()
        for phrase in ('release check receipt', 'hub reachable', 'install from the published tag'):
            assert phrase in text


def test_a_rerun_files_nothing_new_and_keeps_the_due_times(kit):
    op, other = kit.op(), kit.op()
    first = kit.execute('--watch-op', op)[1]['watch']
    plans = kit.rows("SELECT COUNT(*) AS n FROM events WHERE kind='task-plan'")[0]['n']
    kit.stamps = ['2031-01-01T00:00:00+00:00']
    again = kit.execute('--watch-op', op)[1]['watch']
    third = kit.execute('--watch-op', other)[1]['watch']
    assert len(kit.watch_tasks()) == 2
    assert again['tasks'] == first['tasks'] and again['due'] == first['due']
    assert 'already filed' in again['note']
    assert third['op'] == op and kit.watch_tasks(other) == []
    assert kit.rows("SELECT COUNT(*) AS n FROM events WHERE kind='task-plan'")[0]['n'] == plans


def test_the_due_times_count_from_the_first_passing_publish(kit):
    op = kit.op()
    kit.stamps = ['2030-01-01T10:00:00+00:00', '2030-01-01T12:00:00+00:00']
    first = kit.execute('--watch-runbook', '/nonexistent/ship.yaml')[1]['watch']
    assert first['tasks'] == [] and 'not filed' in first['note']
    out = kit.execute('--watch-op', op)[1]['watch']
    assert out['due'] == {'1h': '2030-01-01T11:00:00+00:00', '24h': '2030-01-02T10:00:00+00:00'}


def test_a_blocked_publish_files_nothing_and_the_retry_does(kit):
    op = kit.op()
    kit.pr = lambda *a: (_ for _ in ()).throw(RuntimeError('api down'))
    code, out = kit.execute('--watch-op', op)
    assert code == 2 and out['status'] == 'BLOCKED' and 'watch' not in out and kit.watch_tasks() == []
    kit.pr = lambda *a: PR
    code, out = kit.execute('--watch-op', op)
    assert code == 0 and len(kit.watch_tasks()) == 2


def test_a_dry_run_prints_exactly_its_five_keys_and_files_nothing(kit):
    code, out = kit.run('publish', kit.check_receipt, '--watch-op', kit.op())
    assert code == 0 and set(out) == {'receipt', 'status', 'report', 'pr_url', 'error'}
    assert kit.watch_tasks() == []


def test_without_an_op_the_tasks_go_to_the_standing_release_watch_op(kit):
    code, out = kit.execute(session='cli')
    assert code == 0 and out['status'] == 'PASS' and len(out['watch']['tasks']) == 2
    op = out['watch']['op']
    opened = kit.rows("SELECT data FROM events WHERE kind='op-open' AND op=?", (op,))[0]
    assert json.loads(opened['data'])['title'] == 'Release watch'
    assert [t['op'] for t in kit.watch_tasks()] == [op, op]


def test_the_op_of_the_one_task_the_session_holds(kit):
    op = kit.op()
    tid = kit.hold(op, 's1')
    out = kit.execute(session='s1')[1]['watch']
    assert out['op'] == op and 'session s1 holds %s' % tid in out['note']


@pytest.mark.parametrize('case', ['two ops', 'expired', 'released', 'closed op', 'unknown op'])
def test_an_op_that_cannot_be_chosen_is_not_guessed(kit, case):
    """A held op that is not unique or live is never guessed, so the tasks go to the standing Release
    watch op; an op named by --watch-op that cannot take them files nothing and is never rerouted."""
    extra = []
    if case == 'two ops':
        kit.hold(kit.op(), 's1')
        kit.hold(kit.op(), 's1')
    elif case == 'expired':
        kit.hold(kit.op(), 's1', minutes=-5)
    elif case == 'released':
        tid = kit.hold(kit.op(), 's1')
        conn = kit.conn()
        claims.release(conn, tid, 's1', session='s1')
        conn.close()
    elif case == 'closed op':
        extra = ['--watch-op', kit.op('closed')]
    else:
        extra = ['--watch-op', 'op-999']
    code, out = kit.execute(*extra, session='s1')
    assert code == 0 and out['status'] == 'PASS'
    if extra:
        assert out['watch']['tasks'] == [] and kit.watch_tasks() == []
    else:
        standing = out['watch']['op']
        assert len(out['watch']['tasks']) == 2 and [t['op'] for t in kit.watch_tasks()] == [standing] * 2


def test_a_named_op_beats_a_held_task(kit):
    kit.hold(kit.op(), 's1')
    named = kit.op()
    assert kit.execute('--watch-op', named, session='s1')[1]['watch']['op'] == named


@pytest.mark.parametrize('error', [sqlite3.OperationalError('database is locked'), ValueError('bad'), OSError('disk')])
def test_a_record_error_never_changes_the_publish(kit, error):
    op = kit.op()

    def boom(*a, **k):
        raise error
    kit.monkeypatch.setattr(followup._ops, 'add_task', boom)
    code, out = kit.execute('--watch-op', op)
    assert code == 0 and out['status'] == 'PASS' and out['pr_url'] == PR
    assert out['watch']['tasks'] == [] and '\n' not in out['watch']['note']
    assert kit.watch_tasks() == []


def test_a_failure_on_the_second_contract_rolls_everything_back(kit):
    op = kit.op()
    real = taskcontract.record
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError('contract refused')
        return real(*a, **k)
    kit.monkeypatch.setattr(followup.taskcontract, 'record', flaky)
    out = kit.execute('--watch-op', op)[1]['watch']
    assert out['tasks'] == [] and kit.watch_tasks() == [] and kit.contracts() == {}
    assert taskplan.latest(kit.conn(), op)['groups'] == []
    kit.monkeypatch.setattr(followup.taskcontract, 'record', real)
    assert len(kit.execute('--watch-op', op)[1]['watch']['tasks']) == 2


def test_a_batch_op_files_nothing_and_the_note_names_the_phase(kit):
    op = kit.op()
    kit.capsys.readouterr()
    cli.main(['batch', 'open', op, '--question', 'which ones'])
    kit.capsys.readouterr()
    code, out = kit.execute('--watch-op', op)
    assert code == 0 and out['status'] == 'PASS' and kit.watch_tasks() == []
    assert '--phase' in out['watch']['note']
    assert taskplan.latest(kit.conn(), op)['groups'] == []


def test_an_owner_made_group_keeps_its_title(kit):
    op = kit.op()
    conn = kit.conn()
    taskplan.group(conn, op, 'release-watch', 'Owner watch list', 'ops', session='owner', actor='owner')
    conn.close()
    kit.execute('--watch-op', op)
    group = taskplan.latest(kit.conn(), op)['groups'][0]
    assert group['title'] == 'Owner watch list' and group['category'] == 'ops'
    assert len(kit.watch_tasks()) == 2


def write_runbook(tmp_path, edit=None, name='ship.yaml'):
    data = ship_example()
    if edit:
        edit(data)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return str(path)


def stage(data, ident):
    return next(s for s in data['stages'] if s['id'] == ident)


def test_a_ship_runbook_gives_each_window_its_own_checks(kit, tmp_path):
    path = write_runbook(tmp_path)
    kit.execute('--watch-op', kit.op(), '--watch-runbook', path)
    by_title = {t['title'].split()[2]: kit.contracts()[t['id']]['done_bar'] for t in kit.watch_tasks()}
    assert by_title['T+1h'] == ['watch-1h: health-1h'] and by_title['T+24h'] == ['watch-24h: errors-24h']


def test_other_spellings_of_the_windows_and_extra_stages(kit, tmp_path):
    def edit(data):
        stage(data, 'watch-1h')['watch']['after'] = '60m'
        stage(data, 'watch-24h')['watch']['after'] = '1d'
        extra = json.loads(json.dumps(stage(data, 'watch-1h')))
        extra.update(id='watch-90m', checks=[{'id': 'late', 'type': 'exit-code'}])
        extra['watch'] = {'after': '90m', 'looks_at': ['late']}
        data['stages'].insert(-1, extra)
    kit.execute('--watch-op', kit.op(), '--watch-runbook', write_runbook(tmp_path, edit))
    tasks = kit.watch_tasks()
    assert len(tasks) == 2
    assert 'late' not in json.dumps([kit.contracts()[t['id']] for t in tasks])


@pytest.mark.parametrize('edit', [lambda d: d.pop('rollback'), lambda d: d.update(kind='plain')])
def test_a_runbook_that_is_not_a_valid_ship_runbook_files_nothing(kit, tmp_path, edit):
    code, out = kit.execute('--watch-op', kit.op(), '--watch-runbook', write_runbook(tmp_path, edit))
    assert code == 0 and out['watch']['tasks'] == [] and 'runbook' in out['watch']['note']
    assert kit.watch_tasks() == []


def test_a_missing_runbook_files_nothing(kit, tmp_path):
    out = kit.execute('--watch-op', kit.op(), '--watch-runbook', str(tmp_path / 'none.yaml'))[1]['watch']
    assert out['tasks'] == [] and 'runbook' in out['note']


def test_an_unexpected_error_never_changes_the_publish(kit):
    op = kit.op()

    def boom(*a, **k):
        raise RuntimeError('odd')
    kit.monkeypatch.setattr(followup, 'resolve_op', boom)
    code, out = kit.execute('--watch-op', op)
    assert code == 0 and out['status'] == 'PASS' and out['pr_url'] == PR
    assert out['watch']['tasks'] == [] and out['watch']['note'].startswith('not filed: internal: RuntimeError: odd')
    assert kit.watch_tasks() == []


def test_a_runbook_that_breaks_the_yaml_loader_never_changes_the_publish(kit, tmp_path):
    path = tmp_path / 'deep.yaml'
    path.write_text('[' * 5000 + ']' * 5000)
    code, out = kit.execute('--watch-op', kit.op(), '--watch-runbook', str(path))
    assert code == 0 and out['pr_url'] == PR and out['watch']['tasks'] == []
    assert out['watch']['note'].startswith('not filed:') and kit.watch_tasks() == []


def test_an_oversize_window_still_files_both_tasks(kit, tmp_path):
    def edit(data):
        s = stage(data, 'watch-1h')
        s['checks'] += [{'id': 'c%d' % i, 'type': 'exit-code', 'description': '%d' % i + 'd' * 620} for i in range(25)]
        s['watch']['looks_at'] += ['c%d' % i for i in range(25)]
    out = kit.execute('--watch-op', kit.op(), '--watch-runbook', write_runbook(tmp_path, edit))[1]['watch']
    assert len(out['tasks']) == 2, out['note']
    bar = kit.contracts()[out['tasks'][0]]['done_bar']
    assert len(bar) == 20 and max(map(len, bar)) <= 600 and bar[-1].startswith('and 7 more: ')


def test_the_hand_commands_in_the_note_file_the_tasks(kit, monkeypatch, tmp_path):
    out = kit.execute('--watch-runbook', write_runbook(tmp_path, lambda d: d.update(kind='plain')),
                      session='cli')[1]['watch']
    assert out['tasks'] == [] and 'file them by hand: ' in out['note'], out
    monkeypatch.undo()
    monkeypatch.setattr(cli, '_root', lambda: str(kit.root))
    op = kit.op()
    text = out['note'].split('file them by hand: ', 1)[1].replace(' OP ', ' %s ' % op)
    commands = [shlex.split('alpaca ' + part.strip().split('alpaca ', 1)[-1]) for part in text.split(', then ')[:1]]
    commands += [shlex.split(part.strip()) for part in text.split(', then ')[1:]]
    for argv in commands:
        kit.capsys.readouterr()
        assert cli.main(['--session', 'pub'] + argv[1:]) == 0, kit.capsys.readouterr().out
    tasks = kit.rows("SELECT id FROM tasks WHERE op=? AND title LIKE 'Release watch%'", (op,))
    assert len(tasks) == 2
    for task in tasks:
        assert kit.contracts()[task['id']]['done_bar'] == list(followup.DEFAULT_CHECKS)


def test_receipts_and_the_publish_event_are_unchanged(kit):
    assert [f.name for f in dataclasses.fields(PublishReceipt)] == [
        'checks', 'status', 'public_base', 'commit', 'pr_url', 'report', 'error', 'covered_sources', 'outgoing',
        'attempt', 'kind', 'schema', 'created_at']
    code, out = kit.execute('--watch-op', kit.op())
    row = kit.rows("SELECT op, data FROM events WHERE kind='release-publish' ORDER BY id DESC")[0]
    assert row['op'] is None and set(json.loads(row['data'])) == {'receipt', 'sha256', 'status', 'summary'}
    assert 'watch' not in kit.receipt_file(out)


def test_the_new_text_is_ascii_without_an_em_dash():
    root = Path(__file__).resolve().parents[2]
    for name in ('alpaca/release/followup.py', 'alpaca/release/cli.py', 'docs/release.md'):
        text = (root / name).read_text()
        assert text.isascii(), name
