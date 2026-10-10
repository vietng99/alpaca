"""t-334: the collector refreshes the pad and analytics that the hooks stop refreshing once
`.alpaca/observability/enabled` is on.

With the flag, `stop.handle` returns before its every-tenth-stop analytics build and
`session_end.handle` returns before its pad write and analytics build, so after the op-008 landing
nothing rebuilt `analytics/index.html` and the pad missed every session end. The `session-views`
consumer takes both over: the pad and analytics at a session end, analytics at every tenth turn end
of a session (the Stop hook's cadence), each at most once per interval (pad 60 s, analytics 10 min),
a trigger inside the interval kept pending for a later batch or an idle pass. A failure is one open
issue and never holds up the checkpoint or another consumer.
"""
import datetime as dt
import json
import os
from pathlib import Path

import pytest

from alpaca import db
from alpaca.observability import consumers


def _flag(project):
    folder = Path(project, '.alpaca/observability')
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'enabled').touch()


def _append(project, kind, session='s1', n=1):
    conn = db.connect(project)
    try:
        for _ in range(n):
            db.append_event(conn, session=session, actor='alpaca', kind=kind, data={})
    finally:
        conn.close()


def _consume(project):
    conn = db.connect(project)
    try:
        return consumers.consume(project, conn)
    finally:
        conn.close()


def _only_views(monkeypatch, extra=None):
    """The real consumer under its real name, alone (or beside `extra`), so a test does not wait
    on the wiki consumer's capture."""
    chosen = dict(extra or {})
    chosen['session-views'] = consumers.CONSUMERS['session-views']
    monkeypatch.setattr(consumers, 'CONSUMERS', chosen)


def _count_writers(monkeypatch, fail=()):
    calls = []

    def writer(name):
        def write(root):
            calls.append(name)
            if name in fail:
                raise OSError('secret path /home/x')
        return write
    monkeypatch.setattr(consumers, 'VIEW_WRITERS', {name: writer(name) for name in ('pad', 'analytics')})
    return calls


def _issues(project, code):
    conn = db.connect_readonly(project)
    try:
        return [dict(r) for r in conn.execute('SELECT * FROM obs_issue WHERE code=?', (code,))]
    finally:
        conn.close()


def _checkpoint(project, name):
    conn = db.connect_readonly(project)
    try:
        row = conn.execute('SELECT * FROM obs_consumer_checkpoint WHERE consumer=?', (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _set_last_attempt(project, name, minutes_ago):
    conn = db.connect(project)
    try:
        state = json.loads(db.meta_get(conn, consumers.VIEWS_STATE_KEY, '{}'))
        when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
        state.setdefault(name, {})['last_attempt'] = when.isoformat()
        with db.transaction(conn):
            db.meta_set(conn, consumers.VIEWS_STATE_KEY, json.dumps(state))
    finally:
        conn.close()


def test_the_collector_has_a_session_views_consumer_after_the_wiki_one():
    names = list(consumers.CONSUMERS)
    assert 'session-views' in names and names.index('wiki') < names.index('session-views')


def test_a_session_end_writes_the_pad_and_rebuilds_analytics(project, monkeypatch):
    """The real writers: RESUME.md and analytics/index.html appear, the checkpoint moves."""
    from alpaca import cli
    cli.main(['init'])
    _flag(project)
    resume, page = Path(project, 'RESUME.md'), Path(project, 'analytics', 'index.html')
    for path in (resume, page):
        if path.exists():
            path.unlink()
    _append(project, 'turn-end')
    _append(project, 'session-end')
    _only_views(monkeypatch)
    result = _consume(project)
    assert result['session-views']['status'] == 'ok'
    assert resume.is_file() and page.is_file()
    assert _checkpoint(project, 'session-views')['event_id'] == result['session-views']['through_event']


def test_every_tenth_turn_end_of_a_session_rebuilds_analytics_only(project, monkeypatch):
    _flag(project)
    calls = _count_writers(monkeypatch)
    _only_views(monkeypatch)
    _append(project, 'turn-end', session='s1', n=9)
    _append(project, 'turn-end', session='s2', n=9)
    _consume(project)
    assert calls == []                                      # nine turn ends each: not yet
    _append(project, 'turn-end', session='s1')
    _consume(project)
    assert calls == ['analytics']                           # s1's tenth, counted across batches


def test_a_trigger_inside_the_interval_waits_and_an_idle_pass_runs_it(project, monkeypatch):
    _flag(project)
    calls = _count_writers(monkeypatch)
    _only_views(monkeypatch)
    _append(project, 'session-end', session='s1')
    _consume(project)
    assert calls == ['pad', 'analytics']
    _append(project, 'session-end', session='s2')           # inside both intervals
    _consume(project)
    assert calls == ['pad', 'analytics']
    _consume(project)                                       # idle pass, still inside
    assert calls == ['pad', 'analytics']
    _set_last_attempt(project, 'pad', minutes_ago=2)        # the pad's 60 s have passed
    _consume(project)
    assert calls == ['pad', 'analytics', 'pad']
    _set_last_attempt(project, 'analytics', minutes_ago=11)  # the analytics 10 minutes too
    result = _consume(project)                              # no new event: the idle pass
    assert calls == ['pad', 'analytics', 'pad', 'analytics']
    assert result['session-views']['status'] == 'current'
    _consume(project)
    assert len(calls) == 4                                  # nothing pending any more


def test_a_failed_refresh_is_an_issue_and_holds_up_nothing(project, monkeypatch):
    _flag(project)
    calls = _count_writers(monkeypatch, fail=('analytics',))
    seen = []
    _only_views(monkeypatch, extra={'wiki': lambda root, rows: seen.extend(r['id'] for r in rows)})
    _append(project, 'session-end')
    result = _consume(project)
    assert result['session-views']['status'] == 'ok' and result['wiki']['status'] == 'ok'
    assert seen == [1] and calls == ['pad', 'analytics']
    assert _checkpoint(project, 'session-views')['event_id'] == 1
    [failed] = _issues(project, 'analytics-refresh-failed')
    assert failed['resolved_at'] is None and 'OSError' in failed['detail']
    assert 'secret' not in failed['detail']                 # the type only, never the message
    _consume(project)                                       # retried only after its interval
    assert calls == ['pad', 'analytics']
    monkeypatch.setattr(consumers, 'VIEW_WRITERS', {'pad': lambda root: calls.append('pad'),
                                                    'analytics': lambda root: calls.append('analytics')})
    _set_last_attempt(project, 'analytics', minutes_ago=11)
    _consume(project)
    assert calls[-1] == 'analytics'
    assert _issues(project, 'analytics-refresh-failed')[0]['resolved_at']


def test_with_observability_off_the_hooks_keep_the_views(project, monkeypatch):
    calls = _count_writers(monkeypatch)
    _only_views(monkeypatch)
    _append(project, 'turn-end', n=10)
    _append(project, 'session-end')
    result = _consume(project)
    assert calls == [] and result['session-views']['status'] == 'ok'
    assert _checkpoint(project, 'session-views')['event_id'] == 11


@pytest.mark.parametrize('hook', ['stop', 'session_end'])
def test_the_hooks_with_the_flag_leave_analytics_to_the_collector(project, monkeypatch, hook):
    """The other half: with the flag the hooks build no analytics (so the collector must)."""
    from alpaca import cli
    from alpaca.analytics import build_index
    from alpaca.hooks import session_end, stop
    cli.main(['init'])
    _flag(project)
    monkeypatch.setattr(build_index, 'build', lambda *a, **k: pytest.fail('hook built analytics'))
    monkeypatch.setattr(stop, 'EVERY', 1)
    handle = stop.handle if hook == 'stop' else session_end.handle
    assert handle({'session_id': 's1', 'cwd': project, '_strict': True})['queued']
