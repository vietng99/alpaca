"""Native admission must refuse before starting a run; passive capture cannot enable it."""
import argparse
import json
from pathlib import Path

import pytest

from alpaca import db
from alpaca.gates import verdict as vc
from alpaca.research import codex_preflight as preflight
from alpaca.research import errors, events, runner, session_runtime
from alpaca.research import cli as research_cli
from alpaca.tests import research_loopkit as kit
from alpaca.tests import research_session_kit as rk
from alpaca.tests.test_research_codex_probe import DRIVER, _capture, _run


def test_readiness_distinguishes_missing_and_unverified(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # absent config/socket is not an unsupported runtime claim
    result = preflight.diagnostic()
    assert result['ready'] is False
    assert [r['code'] for r in result['reasons']] == [r[0] for r in preflight.REASONS]
    assert len(result['reasons']) == 7 and all(r['remediation'] for r in result['reasons'])
    assert 'unsupported' not in json.dumps(result)
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(errors.ResearchRefusal) as exc:
        preflight.require_ready()
    assert 'RUN-CODEX-NOT-READY' in str(exc.value) and exc.value.code == vc.BLOCKED
    assert all(r['code'] in exc.value.detail for r in result['reasons'])


def test_synthetic_or_forged_evidence_cannot_enable_research(tmp_path, monkeypatch):
    parent = tmp_path / '.alpaca'
    parent.mkdir()
    staged = parent / 'prepared'
    assert _run('prepare', '--output-dir', str(staged), '--driver-session', DRIVER,
                '--diagnostic-opt-in').returncode == 0
    assert _capture(staged, json.dumps({'session_id': DRIVER, 'ready': True,
                                      'trusted': True}).encode()).returncode == 0
    (staged / 'manifest.json').write_text(json.dumps({'ready': True, 'trusted': True,
        'installed': True, 'driver_session': 'another-driver'}))
    (staged / 'hooks.json').write_text('{"ready":true}')
    (staged / 'collector.py').write_text('print("trusted")')
    monkeypatch.setenv('ALPACA_CODEX_READY', 'true')
    monkeypatch.setenv('CODEX_HOOKS_TRUSTED', 'true')
    monkeypatch.chdir(staged)
    assert preflight.diagnostic()['ready'] is False
    with pytest.raises(errors.ResearchRefusal, match='RUN-CODEX-NOT-READY'):
        preflight.require_ready()
    with pytest.raises(TypeError):
        preflight.require_ready(ready=True)


def test_native_start_and_restart_refuse_before_mutation(project, monkeypatch):
    root = kit.make_project(project)
    rk.prepare(monkeypatch, root, sid=DRIVER, level=6)
    monkeypatch.delenv('CLAUDECODE', raising=False)
    monkeypatch.delenv('CLAUDE_CODE_SESSION_ID', raising=False)
    monkeypatch.setenv('CODEX_THREAD_ID', DRIVER)
    monkeypatch.setattr(session_runtime, 'rate_card_gaps',
                        lambda *a, **kw: pytest.fail('native refusal must precede rates'))
    monkeypatch.setattr(runner, 'launch', lambda *a, **kw: pytest.fail('must not launch'))
    conn = db.connect(root)
    before = conn.execute('SELECT count(*) FROM events').fetchone()[0]
    files = {str(p.relative_to(root)) for p in Path(root).rglob('*') if p.is_file()}
    with pytest.raises(errors.ResearchRefusal, match='RUN-CODEX-NOT-READY'):
        runner.open_run(root, runbook='pack/rb.yaml', task=None, op=None, control=None,
                        via='test', level='L6', session=DRIVER, runtime='codex')
    # A recorded legacy session run is not repaired or resumed. Use the real shared restart
    # entrypoint, isolating only its recorded mode and posture prerequisites.
    monkeypatch.setattr(runner, '_runtime_mode', lambda *a: 'session')
    monkeypatch.setattr(research_cli, '_level_n', lambda v: 6)
    verb = argparse.Namespace(conn=conn, root=root, session=DRIVER)
    with pytest.raises(research_cli._Refused) as exc:
        research_cli._session_check(verb, 'rr-001', restart=True)
    assert 'RUN-CODEX-NOT-READY' in str(exc.value)
    assert conn.execute('SELECT count(*) FROM events').fetchone()[0] == before
    assert conn.execute('SELECT count(*) FROM events WHERE kind IN (?, ?, ?)',
                        (events.RUN_START, events.JOB, events.DRIVER)).fetchone()[0] == 0
    assert not Path(session_runtime.live_path(root, DRIVER)).exists()
    assert {str(p.relative_to(root)) for p in Path(root).rglob('*') if p.is_file()} == files
    conn.close()


def test_claude_admission_is_unchanged(project, monkeypatch):
    root = kit.make_project(project)
    rk.prepare(monkeypatch, root, sid=DRIVER, level=6)
    monkeypatch.setattr(preflight, 'require_ready', lambda: pytest.fail('Claude reached native gate'))
    monkeypatch.setattr(session_runtime, 'rate_card_gaps', lambda *a, **kw: [])
    conn = db.connect(root)
    assert session_runtime.drive_checks(conn, root, DRIVER, level=6) == {
        'runtime': 'claude', 'witness': 'hook'}
    monkeypatch.setattr(session_runtime, 'rate_card_gaps', lambda *a, **kw: [
        {'model': 'claude-unknown', 'fix': 'price-check'}])
    with pytest.raises(errors.ResearchRefusal, match='RUN-RATECARD-MISSING'):
        session_runtime.drive_checks(conn, root, DRIVER, level=6)
    with pytest.raises(errors.ResearchRefusal, match='RESEARCH-LEVEL-TOO-LOW'):
        session_runtime.drive_checks(conn, root, DRIVER, level=2)
    conn.close()
