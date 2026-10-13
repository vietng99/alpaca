"""A running release step shows its progress and time left; a step that died does not stay stuck."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from alpaca.tests.test_release_pipeline_build import source, api
from alpaca.tests.test_release_pipeline_checks import project, set_gates, gate, check
from alpaca.tests.test_release_pipeline_source import commit_fixture

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def at(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def state(steps, parallel=1, sequential=()):
    return {'kind': 'check', 'parallel': parallel, 'sequential': list(sequential), 'steps': steps}


def estimate(steps, known, **kw):
    return api('progress').estimate(state(steps, **kw), known, NOW.timestamp())


def test_sequential_steps_add_up():
    steps = [{'name': 'a', 'status': 'PASS'}, {'name': 'b', 'status': 'running', 'started': at(10)},
             {'name': 'c', 'status': 'pending'}]
    out = estimate(steps, {'a': 20, 'b': 40, 'c': 60})
    assert out['seconds_left'] == 30 + 60
    assert out['percent'] == pytest.approx(100 * 30 / 120, abs=0.1)


def test_parallel_gates_are_list_scheduled():
    steps = [{'name': 'fresh-clone', 'status': 'PASS'},
             {'name': 'lint', 'status': 'running', 'started': at(5)},
             {'name': 'suite-a', 'status': 'pending'}, {'name': 'suite-b', 'status': 'pending'}]
    known = {'fresh-clone': 10, 'lint': 10, 'suite-a': 3600, 'suite-b': 3600}
    out = estimate(steps, known, parallel=3, sequential=['fresh-clone'])
    assert out['seconds_left'] == 3600
    serial = estimate(steps, known, parallel=1, sequential=['fresh-clone'])
    assert serial['seconds_left'] == 5 + 3600 + 3600


def test_a_step_past_its_usual_time_is_not_done():
    steps = [{'name': 'suite', 'status': 'running', 'started': at(500)}]
    out = estimate(steps, {'suite': 300})
    assert out['seconds_left'] == api('progress').OVERRUN_S and out['percent'] < 100


def test_no_history_gives_no_time_left_and_counts_steps():
    steps = [{'name': 'a', 'status': 'PASS'}, {'name': 'b', 'status': 'running', 'started': at(1)},
             {'name': 'c', 'status': 'pending'}, {'name': 'd', 'status': 'SKIPPED'}]
    out = estimate(steps, {'a': 1})
    assert out['seconds_left'] is None and out['unknown'] == ['b', 'c']
    assert out['percent'] == 50.0


def test_history_is_the_median_of_recent_passing_runs(tmp_path):
    folder = tmp_path / '.alpaca/releases/receipts'
    folder.mkdir(parents=True)
    for i, seconds in enumerate([100, 200, 300, 9999]):
        status = 'FAIL' if seconds == 9999 else 'PASS'
        (folder / ('%d-check.json' % i)).write_text(json.dumps({'created_at': '2026-10-0%dT00:00:00+00:00' % (i + 1),
            'gates': [{'name': 'suite', 'status': status, 'duration_s': seconds}, {'name': 'old', 'status': 'PASS'}]}))
    (folder / '9-build.json').write_text(json.dumps({'gates': [{'name': 'suite', 'status': 'PASS', 'duration_s': 1}]}))
    assert api('progress').history(tmp_path) == {'suite': 200}


def test_progress_file_lifecycle_and_current(tmp_path):
    progress = api('progress').Progress(tmp_path, 'check', 'a' * 40, ['x', 'y'], parallel=2)
    progress.start('x')
    listed = api('progress').current(tmp_path)
    assert len(listed) == 1 and listed[0]['state'] == 'running' and listed[0]['step'] == 'x'
    assert listed[0]['seconds_left'] is None              # no receipts yet, so no history
    progress.finish('x', 'FAIL')
    progress.finish('y', 'SKIPPED')
    statuses = {s['name']: s['status'] for s in api('progress').current(tmp_path)[0]['steps']}
    assert statuses == {'x': 'FAIL', 'y': 'SKIPPED'}
    progress.close()
    assert api('progress').current(tmp_path) == []


def test_a_step_whose_heartbeat_stopped_shows_ended_then_disappears(tmp_path):
    """A step that died (its pid gone or reused by another process) stops rewriting its file: it shows
    as ended, then disappears, and the next release step removes the file."""
    progress = api('progress')
    folder = tmp_path / progress.RUNNING
    folder.mkdir(parents=True)
    stamp = datetime.now(timezone.utc).isoformat()
    (folder / 'gone.json').write_text(json.dumps({'kind': 'check', 'pid': os.getpid(), 'started': stamp, 'updated': stamp,
                                                  'steps': [{'name': 'suite', 'status': 'running', 'started': stamp}]}))
    (folder / 'x.json.tmp-1').write_text('{')
    now = datetime.now(timezone.utc).timestamp()
    assert progress.current(tmp_path, now)[0]['state'] == 'running'          # its pid is alive: no matter
    ended = progress.current(tmp_path, now + progress.STALE_S + 1)
    assert ended[0]['state'] == 'ended' and ended[0]['percent'] is None
    assert progress.current(tmp_path, now + progress.STALE_S + progress.ENDED_SHOWN_S + 1) == []
    os.utime(folder / 'x.json.tmp-1', (now - progress.STALE_S - 5,) * 2)
    progress.prune(folder, now + progress.STALE_S + progress.ENDED_SHOWN_S + 1)
    assert not (folder / 'gone.json').exists() and not (folder / 'x.json.tmp-1').exists()


def test_the_heartbeat_keeps_a_long_step_listed(tmp_path, monkeypatch):
    progress = api('progress')
    monkeypatch.setattr(progress, 'HEARTBEAT_S', 0.05)
    step = progress.Progress(tmp_path, 'check', 'a' * 40, ['suite'])
    first = json.loads(step.path.read_text())['updated']
    import time
    time.sleep(0.3)
    assert json.loads(step.path.read_text())['updated'] > first
    step.close()
    assert not step.path.exists()


def test_a_progress_write_failure_never_stops_the_step(tmp_path, monkeypatch):
    progress = api('progress')
    step = progress.Progress(tmp_path, 'check', 'a' * 40, ['suite'])
    def full(*a, **k):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(Path, 'write_text', full)
    step.start('suite')
    step.finish('suite', 'PASS', 1.0)
    monkeypatch.undo()
    step.close()


def test_cli_check_reports_progress_while_gates_run(project, monkeypatch, capsys, tmp_path):
    from alpaca import cli
    seen = tmp_path / 'seen.json'
    probe = ('import json, pathlib, sys; sys.path.insert(0, %r); from alpaca.release import progress; '
             'pathlib.Path(%r).write_text(json.dumps(progress.current(%r)))'
             % (str(api('progress').__file__).rsplit('/alpaca/', 1)[0], str(seen), str(project)))
    set_gates(project, [{'name': 'probe', 'timeout': 60, 'command': [sys.executable, '-c', probe]}])
    sha = commit_fixture(project)
    api('approval').approve(project, sha, approver='owner', session='test')
    monkeypatch.setattr(cli, '_root', lambda: str(project))
    assert cli.main(['release', 'build']) == 0
    built = json.loads(capsys.readouterr().out)
    assert cli.main(['release', 'check', built['receipt']]) == 0
    during = json.loads(seen.read_text())
    assert during[0]['kind'] == 'check' and during[0]['step'] == 'probe' and during[0]['state'] == 'running'
    assert [s['name'] for s in during[0]['steps']] == ['reproducibility', 'leak-scan', 'fresh-clone', 'probe']
    assert {s['name']: s['status'] for s in during[0]['steps']}['fresh-clone'] == 'PASS'
    assert api('progress').current(project) == []          # cleared when the check ended


def test_history_skips_carried_gates(tmp_path):
    folder = tmp_path / '.alpaca/releases/receipts'
    folder.mkdir(parents=True)
    (folder / '1-check.json').write_text(json.dumps({'created_at': '2026-10-01T00:00:00+00:00', 'gates': [
        {'name': 'suite', 'status': 'PASS', 'duration_s': 100}]}))
    (folder / '2-check.json').write_text(json.dumps({'created_at': '2026-10-02T00:00:00+00:00', 'gates': [
        {'name': 'suite', 'status': 'PASS', 'duration_s': 9000, 'carried': {'attempt': '1'}}]}))
    assert api('progress').history(tmp_path) == {'suite': 100}


def test_drop_removes_a_carried_step(tmp_path):
    progress = api('progress')
    step = progress.Progress(tmp_path, 'check', 'a' * 40, ['x', 'y'])
    step.drop('y')
    step.start('x')
    listed = progress.current(tmp_path)[0]
    assert [s['name'] for s in listed['steps']] == ['x']
    assert json.loads(step.path.read_text())['carried'] == ['y']
    assert progress.estimate(json.loads(step.path.read_text()), {'x': 10})['seconds_left'] == 10
    step.close()
    progress.QUIET.drop('y')                                  # a library call drops nothing and does not fail
