"""A06 (done_bar[2]; cause C5, door mismatch 3): an executed release publish always files the two
watch tasks. With no --watch-op and no attributable op they go to a standing Release watch op,
opened once and found again by a marker event (never by its title), like the After-action items op.

Ways this still fails after it lands:
  * the fallback also catches an explicit --watch-op that names a closed or unknown op (that is the
    publisher's choice and still files nothing)
  * a second publish opens a second Release watch op; a closed one is reused; a user op titled
    "Release watch" is adopted; a renamed standing op is lost; the After-action items op is taken
  * the standing op becomes "the current op" (ops.current_op, first_open_task, ops.NOT_STANDING), so
    start/intake/RESUME point at it and rule (1) attributes work to it
  * a rerun of the same attempt opens an empty op before it finds the filed tasks
  * a failure after the op opens leaves the op (or its marker) on the record, or changes the
    publish exit code or pr_url
  * the hand commands of the remaining not-filed paths stop running
Uses the t-620 kit (test_release_pipeline_watch.Kit): a local bare remote and a fake PR API."""
from dataclasses import replace
import shlex

import pytest

from alpaca import claims, cli, db, ops as _ops
from alpaca.release import followup
from alpaca.release.receipts import PublishReceipt
from alpaca.tests.test_release_pipeline_watch import PR, Kit, kit, ready, project, source, write_runbook  # noqa: F401


def op_ids(kit):
    return {r['id'] for r in kit.rows("SELECT id FROM ops")}


def op_text(kit, op):
    rows = kit.rows("SELECT * FROM ops WHERE id=?", (op,))
    words = [str(v) for v in rows[0].values() if v] if rows else []
    words += [r['data'] or '' for r in kit.rows("SELECT data FROM events WHERE op=? AND kind IN ('op-open','op-title')",
                                                 (op,))]
    return ' '.join(words).lower()


def fallback(kit, session='cli', *extra):
    before = op_ids(kit)
    code, out = kit.execute(*extra, session=session)
    assert code == 0 and out['status'] == 'PASS' and out['pr_url'] == PR, out
    watch = out['watch']
    assert len(watch['tasks']) == 2, watch
    return before, watch


def second_attempt(kit, out_receipt, session='cli', tag='second'):
    """The watch step of a publish of another check attempt, through followup.file_watches."""
    first = PublishReceipt(**kit.receipt_file({'receipt': out_receipt}))
    other = replace(first, checks=dict(first.checks, attempt=tag + '-checks'), attempt=tag + '-publish')
    return followup.file_watches(kit.root, session, other)


def test_no_watch_op_and_the_shared_session_files_in_the_release_watch_op(kit):
    before, watch = fallback(kit)
    standing = watch['op']
    assert standing and standing not in before
    assert kit.rows("SELECT status FROM ops WHERE id=?", (standing,))[0]['status'] == 'open'
    assert 'release watch' in op_text(kit, standing), op_text(kit, standing)
    assert {t['op'] for t in kit.watch_tasks()} == {standing}
    assert standing in watch['note'] and '\n' not in watch['note'] and watch['note'].isascii()


@pytest.mark.parametrize('case', ['two ops', 'expired', 'released', 'holds nothing'])
def test_a_session_with_no_attributable_op_files_in_the_release_watch_op(kit, case):
    held = set()
    if case == 'two ops':
        a, b = kit.op(), kit.op()
        kit.hold(a, 's1')
        kit.hold(b, 's1')
        held = {a, b}
    elif case == 'expired':
        a = kit.op()
        kit.hold(a, 's1', minutes=-5)
        held = {a}
    elif case == 'released':
        a = kit.op()
        tid = kit.hold(a, 's1')
        conn = kit.conn()
        claims.release(conn, tid, 's1', session='s1')
        conn.close()
        held = {a}
    before, watch = fallback(kit, 's1')
    assert watch['op'] not in held and watch['op'] not in before
    assert 'release watch' in op_text(kit, watch['op'])


@pytest.mark.parametrize('case', ['closed op', 'unknown op'])
def test_an_explicit_watch_op_that_cannot_take_them_still_files_nothing(kit, case):
    target = kit.op('closed') if case == 'closed op' else 'op-999'
    before = op_ids(kit)
    code, out = kit.execute('--watch-op', target, session='s1')
    assert code == 0 and out['status'] == 'PASS' and out['watch']['tasks'] == []
    assert kit.watch_tasks() == []
    assert op_ids(kit) == before, 'an explicit --watch-op was rerouted into a new op'


def test_a_held_op_still_wins_over_the_fallback(kit):
    op = kit.op()
    kit.hold(op, 's1')
    assert kit.execute(session='s1')[1]['watch']['op'] == op


def test_a_rerun_of_the_same_attempt_opens_no_op(kit):
    _before, watch = fallback(kit)
    ops_now = op_ids(kit)
    opens = kit.rows("SELECT COUNT(*) AS n FROM events WHERE kind='op-open'")[0]['n']
    again = kit.execute(session='cli')[1]['watch']
    assert again['tasks'] == watch['tasks'] and 'already filed' in again['note']
    assert op_ids(kit) == ops_now
    assert kit.rows("SELECT COUNT(*) AS n FROM events WHERE kind='op-open'")[0]['n'] == opens


def test_a_second_release_reuses_the_standing_op(kit):
    code, out = kit.execute(session='cli')
    standing = out['watch']['op']
    ops_now = op_ids(kit)
    got = second_attempt(kit, out['receipt'])
    assert len(got['tasks']) == 2 and got['op'] == standing, got
    assert op_ids(kit) == ops_now
    assert len(kit.watch_tasks(standing)) == 4


def test_a_closed_release_watch_op_is_never_reused(kit):
    code, out = kit.execute(session='cli')
    standing = out['watch']['op']
    conn = kit.conn()
    db.patch(conn, 'ops', 'id', standing, {'status': 'closed'})
    conn.close()
    got = second_attempt(kit, out['receipt'])
    assert len(got['tasks']) == 2 and got['op'] != standing, got
    assert kit.rows("SELECT status FROM ops WHERE id=?", (got['op'],))[0]['status'] == 'open'


def test_a_renamed_release_watch_op_is_still_found(kit):
    code, out = kit.execute(session='cli')
    standing = out['watch']['op']
    kit.capsys.readouterr()
    assert cli.main(['op', 'title', standing, 'Watching the releases']) == 0
    got = second_attempt(kit, out['receipt'])
    assert got['op'] == standing, got


def test_a_user_op_titled_release_watch_is_not_adopted(kit):
    kit.capsys.readouterr()
    assert cli.main(['op', 'new', 'Release watch', '--done-when', 'never', '--title', 'Release watch']) == 0
    decoy = sorted(op_ids(kit))[-1]
    _before, watch = fallback(kit)
    assert watch['op'] != decoy


def test_the_after_action_items_op_is_not_taken(kit):
    items = kit.op()
    conn = kit.conn()
    db.append_event(conn, session='owner', actor='agent', kind=_ops.STANDING_KIND, op=items, ref=items,
                    data={'title': 'After-action items'})
    conn.close()
    _before, watch = fallback(kit)
    assert watch['op'] != items
    from alpaca import aaritems
    conn = kit.conn()
    try:
        assert aaritems.standing_op(conn) == items, 'the After-action items finder now returns another op'
    finally:
        conn.close()


def test_the_release_watch_op_is_never_the_current_op(kit):
    """The user op opens first and its task is added after the watch tasks, so only the standing
    exclusion keeps the newer op and the older task ids from winning."""
    user = kit.op()
    _before, watch = fallback(kit)
    standing = watch['op']
    conn = kit.conn()
    try:
        user_task = _ops.add_task(conn, user, 'Real work', 'Real work', session='owner', actor='owner')
        assert _ops.current_op(conn)['id'] == user
        assert _ops.first_open_task(conn)['id'] == user_task
        not_standing = {r[0] for r in conn.execute("SELECT id FROM ops WHERE " + _ops.NOT_STANDING)}
        assert standing not in not_standing and user in not_standing
    finally:
        conn.close()


def test_a_failure_after_the_op_opens_leaves_no_op_and_keeps_the_publish(kit, monkeypatch):
    before = op_ids(kit)
    opens = kit.rows("SELECT COUNT(*) AS n FROM events WHERE kind='op-open'")[0]['n']

    def boom(*a, **k):
        raise ValueError('task add refused')
    monkeypatch.setattr(followup._ops, 'add_task', boom)
    code, out = kit.execute(session='cli')
    assert code == 0 and out['status'] == 'PASS' and out['pr_url'] == PR
    assert out['watch']['tasks'] == [] and '\n' not in out['watch']['note']
    assert op_ids(kit) == before, 'the standing op stayed after the filing rolled back'
    assert kit.rows("SELECT COUNT(*) AS n FROM events WHERE kind='op-open'")[0]['n'] == opens


def test_an_error_opening_the_op_never_changes_the_publish(kit, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('odd')
    monkeypatch.setattr(_ops, 'open_op', boom)
    code, out = kit.execute(session='cli')
    assert code == 0 and out['status'] == 'PASS' and out['pr_url'] == PR
    assert out['watch']['tasks'] == [] and out['watch']['note'].startswith('not filed:')


def test_the_hand_commands_of_a_not_filed_note_still_run(kit, tmp_path, monkeypatch):
    """With the fallback, the no-op case files; a refused runbook is now the path whose note must
    carry hand commands that run (t-620 r1)."""
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
    assert len(kit.rows("SELECT id FROM tasks WHERE op=? AND title LIKE 'Release watch%'", (op,))) == 2


def test_a_held_op_with_a_batch_and_no_watch_op_files_in_the_release_watch_op(kit):
    op = kit.op()
    kit.hold(op, 's1')
    kit.capsys.readouterr()
    assert cli.main(['batch', 'open', op, '--question', 'which ones']) == 0   # op now needs a phase
    kit.capsys.readouterr()
    code, out = kit.execute(session='s1')
    assert code == 0 and out['status'] == 'PASS' and out['pr_url'] == PR
    watch = out['watch']
    assert len(watch['tasks']) == 2 and watch['op'] != op, watch
    assert 'batch' in watch['note']
    assert len(kit.watch_tasks()) == 2


def test_a_held_op_with_a_full_plan_and_no_watch_op_files_in_the_release_watch_op(kit, monkeypatch):
    op = kit.op()
    kit.hold(op, 's1')
    monkeypatch.setattr(followup, '_room', lambda conn, o: o != op)
    code, out = kit.execute(session='s1')
    assert code == 0 and out['status'] == 'PASS'
    assert len(out['watch']['tasks']) == 2 and out['watch']['op'] != op, out['watch']


def test_a_held_after_action_items_task_does_not_take_the_watch_tasks(kit):
    from alpaca import aaritems
    conn = kit.conn()
    try:
        with db.transaction(conn):
            items = aaritems.open_standing(conn, 'owner')
    finally:
        conn.close()
    kit.hold(items, 's1')
    kit.capsys.readouterr()
    code, out = kit.execute(session='s1')
    assert code == 0 and out['status'] == 'PASS' and len(out['watch']['tasks']) == 2
    assert out['watch']['op'] != items, out['watch']


def test_a_session_holding_a_task_of_x_and_of_a_standing_op_files_in_x(kit):
    from alpaca import aaritems
    op = kit.op()
    conn = kit.conn()
    try:
        with db.transaction(conn):
            items = aaritems.open_standing(conn, 'owner')
    finally:
        conn.close()
    kit.hold(items, 's1')
    kit.hold(op, 's1')
    kit.capsys.readouterr()
    code, out = kit.execute(session='s1')
    assert code == 0 and out['status'] == 'PASS' and len(out['watch']['tasks']) == 2
    assert out['watch']['op'] == op, out['watch']
