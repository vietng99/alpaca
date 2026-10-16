"""A release cannot publish past missing, stale, leaking or failed evidence."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest
import yaml

from alpaca.tests.test_release_pipeline_build import source, api, build


@pytest.fixture
def project(source):
    data = yaml.safe_load((source / 'project.yaml').read_text())
    data['barrier'] = {'terms': '.alpaca/terms.txt', 'protected_paths': ['.alpaca/', '.env']}
    data['release']['gates'][0]['command'] = [sys.executable, '-c', 'print("project gate passed")']
    data['release']['overlays'] = {'project.yaml': 'public-project.yaml'}
    (source / 'public-project.yaml').write_text('name: sample\ntemplate: true\n')
    (source / '.alpaca/terms.txt').write_text('SecretCanaryValue\n')
    (source / 'project.yaml').write_text(yaml.safe_dump(data))
    return source


def approve_head(root, commit=False):
    """Approve the exact development commit, committing fixture edits first when asked."""
    run = lambda *args: subprocess.check_output(['git', '-C', str(root), *args]).decode().strip()
    if commit and run('status', '--porcelain'):
        run('add', '-A')
        run('commit', '-qm', 'Fixture change')
    return api('approval').approve(root, run('rev-parse', 'HEAD'), approver='owner', session='test')


def check(project):
    approve_head(project, commit=True)
    candidate = build(project)
    return api('checks').check_release(project, api('config').load_config(project), candidate)


def configure(project, command, **kwargs):
    data = yaml.safe_load((project / 'project.yaml').read_text())
    data['release']['gates'][0].update(command=command, **kwargs)
    (project / 'project.yaml').write_text(yaml.safe_dump(data))


def test_required_and_project_gates_pass(project):
    receipt = check(project)
    assert receipt.status == 'PASS'
    assert {g['name'] for g in receipt.gates} == {'reproducibility', 'leak-scan', 'fresh-clone', 'product-test'}
    assert all(g['status'] == 'PASS' for g in receipt.gates)
    api('checks').validate_checks(project, api('config').load_config(project), receipt)


def test_failed_gate_records_log(project):
    configure(project, [sys.executable, '-c', 'print("intentional failure"); raise SystemExit(7)'])
    receipt = check(project)
    assert receipt.status != 'PASS'
    gate = next(g for g in receipt.gates if g['name'] == 'product-test')
    assert gate['returncode'] == 7 and gate['status'] == 'FAIL'
    assert 'intentional failure' in Path(gate['log']).read_text()
    with pytest.raises(ValueError):
        api('checks').validate_checks(project, api('config').load_config(project), receipt)


def test_leak_canary_blocks_without_echoing_secret(project):
    (project / 'src/app.txt').write_text('SecretCanaryValue')
    receipt = check(project)
    assert receipt.status != 'PASS'
    assert next(g for g in receipt.gates if g['name'] == 'leak-scan')['status'] != 'PASS'
    assert 'SecretCanaryValue' not in json.dumps(asdict(receipt))


def test_missing_term_file_blocks(project):
    (project / '.alpaca/terms.txt').unlink()
    assert check(project).status != 'PASS'


def test_optional_terms_cannot_bypass_release_gate(project):
    data = yaml.safe_load((project / 'project.yaml').read_text())
    data['barrier']['terms_optional'] = True
    (project / 'project.yaml').write_text(yaml.safe_dump(data))
    (project / '.alpaca/terms.txt').unlink()
    assert check(project).status != 'PASS'


def test_fresh_clone_needs_no_dev_files(project):
    configure(project, [sys.executable, '-c', 'from pathlib import Path; assert not Path(".alpaca/secret").exists(); assert Path("src/app.txt").read_text()=="product\\n"'])
    assert check(project).status == 'PASS'


def test_source_edit_invalidates_checks(project):
    receipt = check(project)
    assert receipt.status == 'PASS'
    (project / 'src/app.txt').write_text('new source')
    with pytest.raises(ValueError, match='changed'):
        api('checks').validate_checks(project, api('config').load_config(project), receipt)


def test_private_policy_edit_invalidates_checks(project):
    receipt = check(project)
    assert receipt.status == 'PASS'
    (project / '.alpaca/terms.txt').write_text('DifferentCanaryValue\n')
    with pytest.raises(ValueError, match='policy|changed'):
        api('checks').validate_checks(project, api('config').load_config(project), receipt)


@pytest.mark.parametrize('mutation', ['missing', 'skipped', 'forged'])
def test_failed_missing_skipped_gate_blocks(project, mutation):
    receipt = check(project)
    if mutation == 'missing':
        receipt.gates.pop()
    elif mutation == 'skipped':
        receipt.gates[0]['status'] = 'SKIPPED'
    else:
        receipt.gates[0]['returncode'] = 42
    with pytest.raises(ValueError):
        api('checks').validate_checks(project, api('config').load_config(project), receipt)


def test_gate_cannot_edit_checked_release_files(project):
    configure(project, [sys.executable, '-c', 'from pathlib import Path; Path("src/app.txt").write_text("changed")'])
    assert check(project).status != 'PASS'


def test_timeout_terminates_gate_children(project, tmp_path):
    marker = tmp_path / 'orphan-marker'
    child = f'import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).write_text("orphan")'
    command = [sys.executable, '-c', f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(10)']
    configure(project, command, timeout=1)
    receipt = check(project)
    gate = next(g for g in receipt.gates if g['name'] == 'product-test')
    assert gate['status'] != 'PASS' and gate['timed_out']
    time.sleep(1.3)
    assert not marker.exists()


def test_check_cli_returns_failure_for_failed_gate(project, monkeypatch, capsys):
    from alpaca import cli
    configure(project, [sys.executable, '-c', 'raise SystemExit(7)'])
    monkeypatch.setattr(cli, '_root', lambda: str(project))
    approve_head(project, commit=True)
    receipt = build(project)
    path = api('receipts').record_receipt(project, 'test', receipt)
    assert cli.main(['release', 'check', str(path)]) == 1
    output = json.loads(capsys.readouterr().out)
    saved = api('receipts').load_receipt(project, output['receipt'], api('receipts').CheckReceipt)
    assert saved.status != 'PASS'


def test_dev_gate_gets_source_configuration_without_runtime(project):
    configure(project, [sys.executable, '-c', 'from pathlib import Path; assert "release:" in Path("project.yaml").read_text(); assert not Path(".alpaca/secret").exists()'], scope='dev')
    assert check(project).status == 'PASS'


def test_publication_credentials_are_not_available_to_gates(project, monkeypatch):
    monkeypatch.setenv('PUBLIC_REPO_TOKEN', 'local-only-credential')
    monkeypatch.setenv('GH_TOKEN', 'local-only-credential')
    configure(project, [sys.executable, '-c', 'import os; assert "PUBLIC_REPO_TOKEN" not in os.environ; assert "GH_TOKEN" not in os.environ'])
    assert check(project).status == 'PASS'


def test_python_gates_use_the_current_alpaca_environment(project):
    # The environment, not the interpreter's name: CI starts the suite as `.venv/bin/python3`, and the
    # gate's `python` is `.venv/bin/python` of the same environment (op-008 t-571).
    configure(project, ['python', '-c', 'import sys; assert sys.prefix == ' + repr(sys.prefix)])
    assert check(project).status == 'PASS'


def test_gate_does_not_inherit_claude_session_or_project(project, monkeypatch):
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'parent-session')
    monkeypatch.setenv('CLAUDE_PROJECT_DIR', '/parent/project')
    configure(project, [sys.executable, '-c', 'import os; assert "CLAUDE_CODE_SESSION_ID" not in os.environ; assert "CLAUDE_PROJECT_DIR" not in os.environ'])
    assert check(project).status == 'PASS'


def ship_ignored_member(project):
    """A committed member that the candidate's own .gitignore matches: a fresh repository of the
    candidate would never carry it, and a scan of its commit would never read it."""
    (project / 'src/.gitignore').write_text('*.log\n')
    (project / 'src/debug.log').write_text('debug output\n')
    subprocess.run(['git', '-C', str(project), 'add', '--force', 'src/.gitignore', 'src/debug.log'], check=True)


def test_check_flags_a_candidate_member_its_gitignore_hides(project):
    ship_ignored_member(project)
    receipt = check(project)
    assert 'src/debug.log' in api('build').tree_inventory(receipt.build['tree'])
    assert receipt.status != 'PASS'
    gates = {g['name']: g for g in receipt.gates}
    assert gates['leak-scan']['status'] == 'FAIL' and 'UNSCANNED-IGNORED-MEMBER' in gates['leak-scan']['detail']
    assert 'src/debug.log' in gates['leak-scan']['detail']
    assert 'src/.gitignore:1' in gates['leak-scan']['detail']             # the actual ignore source
    assert gates['fresh-clone']['status'] == 'SKIPPED'                     # fail_fast, after the leak scan
    with pytest.raises(ValueError):
        api('checks').validate_checks(project, api('config').load_config(project), receipt)


def test_without_fail_fast_the_fresh_clone_names_a_hidden_member_too(project):
    ship_ignored_member(project)
    data = yaml.safe_load((project / 'project.yaml').read_text())
    data['release']['fail_fast'] = False
    (project / 'project.yaml').write_text(yaml.safe_dump(data))
    gates = {g['name']: g for g in check(project).gates}
    assert gates['fresh-clone']['status'] == 'FAIL' and 'src/debug.log' in gates['fresh-clone']['detail']
    assert 'src/.gitignore:1' in gates['fresh-clone']['detail']


def test_dev_gate_keeps_tracked_files_an_ignore_rule_matches(project):
    ship_ignored_member(project)
    configure(project, [sys.executable, '-c', 'from pathlib import Path; assert Path("src/debug.log").read_text() == "debug output\\n"'], scope='dev')
    # fresh-clone fails on the ignored member; run the dev gate anyway to see what it gets
    data = yaml.safe_load((project / 'project.yaml').read_text())
    data['release']['fail_fast'] = False
    (project / 'project.yaml').write_text(yaml.safe_dump(data))
    receipt = check(project)
    assert next(g for g in receipt.gates if g['name'] == 'product-test')['status'] == 'PASS'


@pytest.mark.parametrize('host', ['xdg-default', 'global-config'])
def test_a_global_git_ignore_on_the_release_host_does_not_fail_a_clean_candidate(project, tmp_path, monkeypatch, host):
    """Review M-1: the seed takes only the candidate's own ignore rules. A host-wide ignore file (the
    default $XDG_CONFIG_HOME/git/ignore and a core.excludesFile in the global config) and a global
    init template's info/exclude never keep a candidate file out of the seed."""
    home = tmp_path / 'host-home'
    (home / '.config/git').mkdir(parents=True)
    (home / '.config/git/ignore').write_text('*.txt\n')
    (home / 'excludes').write_text('run\n')
    template = home / 'template'
    (template / 'info').mkdir(parents=True)
    (template / 'info/exclude').write_text('project.yaml\n')
    if host == 'global-config':
        (home / '.gitconfig').write_text('[core]\n\texcludesFile = %s\n[init]\n\ttemplateDir = %s\n'
                                         % (home / 'excludes', template))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    monkeypatch.delenv('GIT_CONFIG_GLOBAL', raising=False)
    receipt = check(project)
    gates = {g['name']: g for g in receipt.gates}
    assert gates['fresh-clone']['status'] == 'PASS', gates['fresh-clone']
    assert gates['leak-scan']['status'] == 'PASS', gates['leak-scan']
    assert receipt.status == 'PASS'


@pytest.mark.parametrize('handles_interrupt', (True, False))
def test_a_timed_out_command_is_interrupted_before_it_is_killed(tmp_path, monkeypatch, handles_interrupt):
    """A timed-out gate or CI test run gets SIGINT first, so pytest still prints the names of the
    tests that failed; one that ignores SIGINT is killed after the grace (op-008 t-571)."""
    checks = api('checks')
    monkeypatch.setattr(checks, 'INTERRUPT_GRACE_S', 3)
    body = ('import signal, time\n'
            + ('' if handles_interrupt else 'signal.signal(signal.SIGINT, signal.SIG_IGN)\n')
            + 'try:\n    time.sleep(60)\nexcept KeyboardInterrupt:\n'
              '    print("FAILED t.py::test_x", flush=True)\n    raise SystemExit(2)\n')
    log = tmp_path / 'gate.log'
    started = time.monotonic()
    code, timed_out = checks.run_command([sys.executable, '-c', body], tmp_path, 1, log)
    assert (code, timed_out) == (124, True) and time.monotonic() - started < 30
    assert ('FAILED t.py::test_x' in log.read_text()) is handles_interrupt


def set_gates(project, gates, **release):
    data = yaml.safe_load((project / 'project.yaml').read_text())
    data['release']['gates'] = gates
    data['release'].update(release)
    (project / 'project.yaml').write_text(yaml.safe_dump(data))


def gate(name, code=0, timeout=600, note=None):
    body = 'import pathlib; pathlib.Path(%r).open("a").write(%r + "\\n"); raise SystemExit(%d)' % (note, name, code) \
        if note else 'raise SystemExit(%d)' % code
    return {'name': name, 'command': [sys.executable, '-c', body], 'timeout': timeout}


def test_gates_run_shortest_timeout_first(project, tmp_path):
    note = str(tmp_path / 'order.txt')
    set_gates(project, [gate('long', timeout=3600, note=note), gate('short', timeout=60, note=note),
                        gate('medium', note=note), gate('medium-two', note=note)])
    receipt = check(project)
    assert receipt.status == 'PASS'
    assert Path(note).read_text().split() == ['short', 'medium', 'medium-two', 'long']
    assert [g['name'] for g in receipt.gates][3:] == ['short', 'medium', 'medium-two', 'long']


def test_first_failing_gate_skips_the_rest(project, tmp_path):
    note = str(tmp_path / 'ran.txt')
    set_gates(project, [gate('slow-suite', timeout=3600, note=note), gate('quick-lint', code=3, timeout=60, note=note)])
    receipt = check(project)
    assert receipt.status == 'FAIL'
    assert Path(note).read_text().split() == ['quick-lint']
    skipped = next(g for g in receipt.gates if g['name'] == 'slow-suite')
    assert skipped['status'] == 'SKIPPED' and 'quick-lint failed first' in skipped['detail']
    with pytest.raises(ValueError):
        api('checks').validate_checks(project, api('config').load_config(project), receipt)


def test_a_failed_mandatory_check_skips_every_project_gate(project, monkeypatch):
    calls = []
    monkeypatch.setattr(api('checks'), 'run_command', lambda *a, **k: calls.append(a) or (0, False))
    (project / 'src/app.txt').write_text('SecretCanaryValue')
    receipt = check(project)
    assert receipt.status == 'FAIL' and not calls
    assert next(g for g in receipt.gates if g['name'] == 'product-test')['status'] == 'SKIPPED'


def _rebuild_differs(project, monkeypatch, **release):
    """A check whose rebuild yields another tree, so reproducibility fails first."""
    from dataclasses import replace
    approve_head(project, commit=True)
    candidate = build(project)
    checks = api('checks')
    real = checks.build_release
    monkeypatch.setattr(checks, 'build_release', lambda *a, **k: replace(real(*a, **k), tree_hash='f' * 64))
    config = replace(api('config').load_config(project), **release)
    return checks.check_release(project, config, candidate)


def test_a_failed_builtin_check_skips_the_builtin_checks_after_it(project, monkeypatch):
    calls = []
    monkeypatch.setattr(api('checks'), 'run_command', lambda *a, **k: calls.append(a) or (0, False))
    receipt = _rebuild_differs(project, monkeypatch)
    status = {g['name']: g for g in receipt.gates}
    assert receipt.status == 'FAIL' and not calls
    assert status['reproducibility']['status'] == 'FAIL'
    for name in ('leak-scan', 'fresh-clone', 'product-test'):
        assert status[name]['status'] == 'SKIPPED'
        assert status[name]['detail'] == 'not run: reproducibility failed first (release.fail_fast)'
        assert 'started' not in status[name]
    assert not receipt.policy_hash                   # the skipped leak scan never read the term policy
    assert 'input-integrity' not in status          # so the final recheck has no policy to compare


def test_a_policy_edit_during_the_gates_still_fails_the_final_recheck(project):
    terms = project / '.alpaca/terms.txt'
    configure(project, [sys.executable, '-c', 'open(%r, "a").write("AnotherCanary\\n")' % str(terms)])
    receipt = check(project)
    integrity = next(g for g in receipt.gates if g['name'] == 'input-integrity')
    assert receipt.status == 'FAIL' and integrity['detail'] == 'release policy changed during checks'


def test_a_failed_leak_scan_skips_the_fresh_clone(project, monkeypatch):
    (project / 'src/app.txt').write_text('SecretCanaryValue')
    receipt = check(project)
    status = {g['name']: g['status'] for g in receipt.gates}
    assert status == {'reproducibility': 'PASS', 'leak-scan': 'FAIL', 'fresh-clone': 'SKIPPED',
                      'product-test': 'SKIPPED'}


def test_fail_fast_off_runs_every_builtin_check(project, monkeypatch):
    receipt = _rebuild_differs(project, monkeypatch, fail_fast=False)
    status = {g['name']: g['status'] for g in receipt.gates}
    assert receipt.status == 'FAIL' and status['reproducibility'] == 'FAIL'
    assert status['leak-scan'] == status['fresh-clone'] == status['product-test'] == 'PASS'


def test_fail_fast_off_runs_every_gate(project, tmp_path):
    note = str(tmp_path / 'ran.txt')
    set_gates(project, [gate('quick-lint', code=3, timeout=60, note=note), gate('slow-suite', timeout=3600, note=note)],
              fail_fast=False)
    receipt = check(project)
    assert receipt.status == 'FAIL'
    assert Path(note).read_text().split() == ['quick-lint', 'slow-suite']
    assert next(g for g in receipt.gates if g['name'] == 'slow-suite')['status'] == 'PASS'


@pytest.mark.parametrize('value', ['yes', 1, None])
def test_fail_fast_must_be_a_boolean(project, value):
    set_gates(project, [gate('quick-lint')], fail_fast=value)
    with pytest.raises(ValueError, match='fail_fast'):
        api('config').load_config(project)


SLEEP = 'import time, pathlib; pathlib.Path(%r).open("a").write("start %s\\n"); time.sleep(%s); pathlib.Path(%r).open("a").write("end %s\\n")'


def sleeper(name, seconds, note, timeout=600):
    return {'name': name, 'timeout': timeout, 'command': [sys.executable, '-c', SLEEP % (note, name, seconds, note, name)]}


def test_parallel_gates_overlap_and_report_their_timings(project, tmp_path):
    note = str(tmp_path / 'events.txt')
    set_gates(project, [sleeper('one', 1.5, note), sleeper('two', 1.5, note), sleeper('three', 1.5, note)], parallel=3)
    receipt = check(project)
    assert receipt.status == 'PASS'
    events = Path(note).read_text().split('\n')
    assert [e.split()[0] for e in events if e][:3] == ['start'] * 3       # all three started before any ended
    for g in receipt.gates:
        assert g['started'] and g['duration_s'] >= 0
    project_gates = [g for g in receipt.gates if g['name'] in ('one', 'two', 'three')]
    assert [g['name'] for g in project_gates] == ['one', 'two', 'three']
    assert all(g['duration_s'] >= 1.5 for g in project_gates)


def test_serial_is_the_default(project, tmp_path):
    note = str(tmp_path / 'events.txt')
    set_gates(project, [sleeper('one', 0.2, note), sleeper('two', 0.2, note)])
    assert check(project).status == 'PASS'
    assert Path(note).read_text().split('\n')[:4] == ['start one', 'end one', 'start two', 'end two']


def test_a_failing_parallel_gate_stops_the_running_ones(project, tmp_path):
    note = str(tmp_path / 'events.txt')
    set_gates(project, [sleeper('slow', 60, note, timeout=3600), gate('quick-fail', code=4, timeout=60),
                        sleeper('later', 0.1, note, timeout=3600)], parallel=2)
    began = __import__('time').time()
    receipt = check(project)
    assert __import__('time').time() - began < 60
    gates = {g['name']: g for g in receipt.gates}
    assert receipt.status == 'FAIL'
    assert gates['quick-fail']['status'] == 'FAIL'
    assert gates['slow']['status'] == 'STOPPED' and 'quick-fail failed first' in gates['slow']['detail']
    assert gates['later']['status'] == 'SKIPPED'
    assert 'end slow' not in Path(note).read_text()


def test_parallel_without_fail_fast_runs_everything(project, tmp_path):
    note = str(tmp_path / 'events.txt')
    set_gates(project, [gate('quick-fail', code=4, timeout=60), sleeper('slow', 0.5, note, timeout=3600)],
              parallel=2, fail_fast=False)
    receipt = check(project)
    assert {g['name']: g['status'] for g in receipt.gates}['slow'] == 'PASS'


@pytest.mark.parametrize('value', [0, 17, True, '2', 1.5])
def test_parallel_must_be_a_small_whole_number(project, value):
    set_gates(project, [gate('quick-lint')], parallel=value)
    with pytest.raises(ValueError, match='parallel'):
        api('config').load_config(project)


def test_built_in_checks_carry_timings(project):
    receipt = check(project)
    for name in ('reproducibility', 'leak-scan', 'fresh-clone'):
        g = next(g for g in receipt.gates if g['name'] == name)
        assert g['started'] and g['duration_s'] >= 0


def test_without_fail_fast_a_gate_exiting_130_is_a_failure_not_stopped(project):
    set_gates(project, [gate('first-fail', code=4, timeout=60), gate('exits-130', code=130, timeout=3600)],
              parallel=2, fail_fast=False)
    gates = {g['name']: g for g in check(project).gates}
    assert gates['exits-130']['status'] == 'FAIL' and gates['exits-130']['returncode'] == 130


def test_an_interrupted_parallel_check_stops_its_running_gates(project, tmp_path, monkeypatch):
    import threading, time
    note = str(tmp_path / 'events.txt')
    set_gates(project, [sleeper('a', 60, note), sleeper('b', 60, note), sleeper('c', 0.1, note)], parallel=2)
    checks = api('checks')
    real = checks.run_command
    calls = []

    def run_command(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 2:
            threading.Timer(1.0, lambda: kwargs['stop'].set()).start()     # stands in for Ctrl-C's stop
        return real(*args, **kwargs)
    monkeypatch.setattr(checks, 'run_command', run_command)
    began = time.time()
    receipt = check(project)
    assert time.time() - began < 30
    statuses = {g['name']: g['status'] for g in receipt.gates}
    assert statuses['a'] == 'STOPPED' and statuses['b'] == 'STOPPED'
    assert 'end a' not in Path(note).read_text()


def test_a_keyboard_interrupt_cancels_queued_gates(project, tmp_path, monkeypatch):
    note = str(tmp_path / 'events.txt')
    set_gates(project, [gate('boom', timeout=60), sleeper('later', 0.1, note, timeout=3600)], parallel=2)
    checks = api('checks')
    real = checks.run_command

    def run_command(command, *args, **kwargs):
        if 'raise SystemExit(0)' in command[-1]:
            raise KeyboardInterrupt
        return real(command, *args, **kwargs)
    monkeypatch.setattr(checks, 'run_command', run_command)
    with pytest.raises(KeyboardInterrupt):
        check(project)


def test_preflight_build_is_a_reserved_gate_name(project):
    set_gates(project, [gate('preflight-build')])
    with pytest.raises(ValueError):
        api('config').load_config(project)


def test_a_command_gets_a_temp_folder_of_its_own_removed_after(tmp_path):
    log = tmp_path / 'gate.log'
    show = 'import os, tempfile; print(os.environ["TMPDIR"]); print(tempfile.gettempdir())'
    code, _ = api('checks').run_command([sys.executable, '-c', show], tmp_path, 60, log)
    seen = log.read_text().split()
    assert code == 0 and seen[0] == seen[1] and os.path.basename(seen[0]).startswith('ag')
    assert seen[0] != os.environ.get('TMPDIR') and not os.path.exists(seen[0])



# ---- a retry reuses the gates that passed on the same build (op-011 t-615) ----------------------

def flag_gate(name, note, flag, timeout=600):
    """A gate that notes each run and passes only while the flag file exists (both outside the checkout)."""
    body = ('import pathlib, sys; pathlib.Path(%r).open("a").write(%r + "\\n"); '
            'sys.exit(0 if pathlib.Path(%r).exists() else 3)' % (str(note), name, str(flag)))
    return {'name': name, 'command': [sys.executable, '-c', body], 'timeout': timeout}


def attempt(project, label, config=None, **kwargs):
    """Build the approved commit under a new name, check it and record the receipt as a retry would."""
    candidate = build(project, label)
    config = config or api('config').load_config(project)
    receipt = api('checks').check_release(project, config, candidate, **kwargs)
    api('receipts').record_receipt(project, 'test', receipt)
    return receipt


def carried(receipt):
    return {g['name'] for g in receipt.gates if 'carried' in g}


def ran(note):
    path = Path(note)
    return path.read_text().split() if path.exists() else []


def plan_for(project, config=None, **changes):
    """carry_plan for a fresh build of the approved commit, with this run's values as check_release has them."""
    from dataclasses import replace
    checks, config = api('checks'), config or api('config').load_config(project)
    candidate = build(project, 'plan-' + uuid.uuid4().hex[:8])
    approved = api('approval').require(project, config, candidate.source_sha)
    policy_hash = checks.policy(project)[2]
    approved = changes.pop('approved', approved)
    policy_hash = changes.pop('policy_hash', policy_hash)
    return checks.carry_plan(project, config, replace(candidate, **changes), approved, policy_hash)


@pytest.fixture
def retry(project, tmp_path):
    """Two gates, a passes and b waits for a flag; the commit is approved. Returns (note, flag)."""
    note, flag = tmp_path / 'note.txt', tmp_path / 'flag'
    set_gates(project, [gate('a', note=str(note)), flag_gate('b', note, flag)])
    approve_head(project, commit=True)
    return note, flag


def test_retry_carries_passed_gates_and_runs_only_the_failed(project, retry):
    note, flag = retry
    first = attempt(project, 'one')
    assert first.status == 'FAIL' and not carried(first)
    flag.write_text('go')
    second = attempt(project, 'two')
    assert second.status == 'PASS'
    assert sorted(ran(note)) == ['a', 'b', 'b']
    a = next(g for g in second.gates if g['name'] == 'a')
    pointer = a['carried']
    assert pointer['attempt'] == first.attempt
    assert pointer['receipt'] == '.alpaca/releases/receipts/%s-check.json' % first.attempt
    assert pointer['sha256'] == hashlib.sha256((project / pointer['receipt']).read_bytes()).hexdigest()
    assert a['status'] == 'PASS' and a['returncode'] == 0 and 'duration_s' not in a and 'started' not in a
    assert carried(second) == {'a'}
    for g in second.gates:
        if g['name'] in ('reproducibility', 'leak-scan', 'fresh-clone', 'b'):
            assert g['started'] and 'carried' not in g
    api('checks').validate_checks(project, api('config').load_config(project), second)


def passing_pair(project, tmp_path):
    note = tmp_path / 'note.txt'
    set_gates(project, [gate('a', note=str(note)), gate('b', note=str(note))])
    approve_head(project, commit=True)
    first = attempt(project, 'one')
    assert first.status == 'PASS'
    control = attempt(project, 'control')
    assert carried(control) == {'a', 'b'}          # the control: the same build carries both
    return note


def test_new_commit_runs_every_gate(project, tmp_path):
    note = passing_pair(project, tmp_path)
    (project / 'src/app.txt').write_text('changed product\n')
    approve_head(project, commit=True)
    assert carried(attempt(project, 'three')) == set() and ran(note).count('a') == 2


def test_reapproval_runs_every_gate(project, tmp_path):
    note = passing_pair(project, tmp_path)
    approve_head(project)
    assert carried(attempt(project, 'three')) == set() and ran(note).count('a') == 2


def test_changed_terms_run_every_gate(project, tmp_path):
    note = passing_pair(project, tmp_path)
    (project / '.alpaca/terms.txt').write_text('OtherCanaryValue\n')
    assert carried(attempt(project, 'three')) == set() and ran(note).count('a') == 2


def test_changed_gate_definition_runs_every_gate(project, tmp_path):
    from dataclasses import replace
    note = passing_pair(project, tmp_path)
    config = api('config').load_config(project)
    changed = [dict(g, timeout=g.get('timeout', 600) + 1) if g['name'] == 'b' else g for g in config.gates]
    receipt = attempt(project, 'three', config=replace(config, gates=changed))
    assert carried(receipt) == set() and ran(note).count('a') == 2


def test_added_gate_runs_every_gate(project, tmp_path):
    from dataclasses import replace
    note = passing_pair(project, tmp_path)
    config = api('config').load_config(project)
    receipt = attempt(project, 'three', config=replace(config, gates=config.gates + [gate('c', note=str(note))]))
    assert carried(receipt) == set() and ran(note).count('a') == 2


def test_other_interpreter_runs_every_gate(project, tmp_path, monkeypatch):
    note = passing_pair(project, tmp_path)
    here = api('checks').runner()
    monkeypatch.setattr(api('checks'), 'runner', lambda: dict(here, python=here['python'] + '-other'))
    assert carried(attempt(project, 'three')) == set() and ran(note).count('a') == 2


def test_carry_plan_refuses_any_other_identity(project, retry):
    note, flag = retry
    attempt(project, 'one')
    assert set(plan_for(project)) == {'a'}                                   # the control
    for change in ({'tree_hash': 'f' * 64}, {'source_hash': 'f' * 64}, {'source_sha': 'f' * 40},
                   {'policy_hash': 'f' * 64}, {'policy_hash': ''}):
        assert plan_for(project, **change) == {}, change
    approved = api('approval').require(project, api('config').load_config(project), api('build').git(
        project, 'rev-parse', 'HEAD').decode().strip())
    assert plan_for(project, approved=dict(approved, attempt='f' * 32)) == {}
    assert plan_for(project, approved=dict(approved, sha256='f' * 64)) == {}


def test_latest_run_decides_and_counts_failures(project, tmp_path):
    note, flag_a, flag_b = tmp_path / 'note.txt', tmp_path / 'flag-a', tmp_path / 'flag-b'
    set_gates(project, [flag_gate('a', note, flag_a), flag_gate('b', note, flag_b)], fail_fast=False)
    approve_head(project, commit=True)
    flag_a.write_text('go')
    one = attempt(project, 'one')                                           # a passes, b fails
    flag_a.unlink()
    flag_b.write_text('go')
    two = attempt(project, 'two', carry=False)                              # a fails, b passes
    assert [g['status'] for g in two.gates if g['name'] in ('a', 'b')] == ['FAIL', 'PASS']
    flag_a.write_text('go')
    three = attempt(project, 'three')
    a, b = (next(g for g in three.gates if g['name'] == n) for n in 'ab')
    assert 'carried' not in a and a['status'] == 'PASS'                     # its latest run failed: runs again
    assert b['carried']['attempt'] == two.attempt and b['carried']['failed_runs'] == 1
    assert ran(note).count('a') == 3 and ran(note).count('b') == 2


def test_carried_entries_never_donate(project, retry):
    note, flag = retry
    one = attempt(project, 'one')
    two = attempt(project, 'two')                                           # b fails again; a is carried from one
    assert carried(two) == {'a'}
    flag.write_text('go')
    three = attempt(project, 'three')
    assert next(g for g in three.gates if g['name'] == 'a')['carried']['attempt'] == one.attempt
    assert ran(note).count('a') == 1


def test_builtins_always_run_and_failing_builtin_carries_nothing(project, retry, monkeypatch):
    from dataclasses import replace
    note, flag = retry
    one = attempt(project, 'one')
    checks = api('checks')
    real = checks.build_release
    monkeypatch.setattr(checks, 'build_release', lambda *a, **k: replace(real(*a, **k), tree_hash='f' * 64))
    config = api('config').load_config(project)
    bad = attempt(project, 'bad', config=replace(config, fail_fast=False))
    monkeypatch.undo()
    assert next(g for g in bad.gates if g['name'] == 'reproducibility')['status'] == 'FAIL'
    assert carried(bad) == set() and ran(note).count('a') == 2
    good = attempt(project, 'good')
    assert 'a' not in carried(good)                  # a's latest real run was in the failed check, which cannot donate
    assert one.attempt not in {g['carried']['attempt'] for g in good.gates if 'carried' in g}
    later = attempt(project, 'later')
    assert next(g for g in later.gates if g['name'] == 'a')['carried']['attempt'] == good.attempt
    for name in ('reproducibility', 'leak-scan', 'fresh-clone'):
        entry = next(g for g in good.gates if g['name'] == name)
        assert 'carried' not in entry and entry['started']
    assert good.policy_hash


def test_donor_with_failed_final_recheck_is_ignored(project, retry):
    from dataclasses import replace
    note, flag = retry
    one = attempt(project, 'one')
    spoiled = [dict(g, status='FAIL', returncode=1) if g['name'] == 'a' else g for g in one.gates]
    spoiled.append({'name': 'input-integrity', 'status': 'FAIL', 'returncode': 1, 'detail': 'source changed'})
    api('receipts').record_receipt(project, 'test', replace(one, gates=spoiled, attempt='c' * 32))
    plan = plan_for(project)
    assert 'a' not in plan                                                  # its latest real run (a FAIL) is on this build


def test_edited_or_unrecorded_receipt_never_donates(project, retry):
    from dataclasses import replace
    note, flag = retry
    one = attempt(project, 'one')
    path = project / '.alpaca/releases/receipts' / (one.attempt + '-check.json')
    original = path.read_bytes()
    path.write_bytes(original.replace(b'"status": "FAIL"', b'"status": "PASS"', 1))
    assert plan_for(project) == {}
    path.write_bytes(original)
    assert set(plan_for(project)) == {'a'}
    # a well-formed copy that no event names donates nothing
    forged = replace(one, attempt='d' * 32, gates=[dict(g, status='PASS', returncode=0) for g in one.gates])
    (path.parent / ('d' * 32 + '-check.json')).write_text(json.dumps(asdict(forged), sort_keys=True, indent=2) + '\n')
    assert set(plan_for(project)) == {'a'}


def test_skipped_and_stopped_gates_run_again(project, tmp_path):
    note, flag = tmp_path / 'note.txt', tmp_path / 'flag'
    set_gates(project, [gate('x', note=str(note), timeout=60), flag_gate('y', note, flag, timeout=120),
                        gate('z', note=str(note), timeout=240)])
    approve_head(project, commit=True)
    one = attempt(project, 'one')
    assert [g['status'] for g in one.gates[3:]] == ['PASS', 'FAIL', 'SKIPPED']
    flag.write_text('go')
    two = attempt(project, 'two')
    assert two.status == 'PASS' and carried(two) == {'x'}
    assert sorted(ran(note)) == ['x', 'y', 'y', 'z']
    stopped = [dict(g, status='STOPPED', returncode=130) if g['name'] == 'y' else g for g in one.gates]
    from dataclasses import replace
    api('receipts').record_receipt(project, 'test', replace(one, gates=stopped, attempt='e' * 32))
    assert 'y' not in plan_for(project)


def test_preflight_receipt_never_donates(project, retry):
    note, flag = retry
    config = api('config').load_config(project)
    api('receipts').record_receipt(project, 'test', api('checks').preflight(project, config, project))
    assert plan_for(project) == {}
    attempt(project, 'one')
    assert set(plan_for(project)) == {'a'}


def test_validate_checks_refuses_a_bad_carried_pointer(project, retry):
    from copy import deepcopy
    from dataclasses import replace
    note, flag = retry
    one = attempt(project, 'one')
    flag.write_text('go')
    two = attempt(project, 'two')
    config, checks = api('config').load_config(project), api('checks')
    checks.validate_checks(project, config, two)                            # the untouched receipt passes

    def other(name, **changes):
        copy = replace(one, attempt=name, **changes)
        path = api('receipts').record_receipt(project, 'test', copy)
        return {'receipt': str(path.relative_to(project)), 'attempt': name,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    failed = [dict(g, status='FAIL', returncode=1) if g['name'] == 'a' else g for g in one.gates]
    chained = [dict(g, carried={'receipt': 'x'}) if g['name'] == 'a' else g for g in one.gates]
    cases = {
        'sha256': lambda p: dict(p, sha256='0' * 64),
        'attempt': lambda p: dict(p, attempt='1' * 32),
        'itself': lambda p: dict(p, attempt=two.attempt, receipt='.alpaca/releases/receipts/%s-check.json' % two.attempt),
        'missing file': lambda p: dict(p, receipt='.alpaca/releases/receipts/%s-check.json' % ('2' * 32)),
        'outside the record': lambda p: dict(p, receipt='../../project.yaml'),
        'other policy': lambda p: other('3' * 32, policy_hash='f' * 64),
        'other tree': lambda p: other('4' * 32, build=dict(one.build, tree_hash='f' * 64)),
        'donor failed': lambda p: other('5' * 32, gates=failed),
        'donor carried': lambda p: other('6' * 32, gates=chained),
        'donor recheck failed': lambda p: other('7' * 32, gates=one.gates + [
            {'name': 'input-integrity', 'status': 'FAIL', 'returncode': 1, 'detail': 'source changed'}]),
    }
    for label, mutate in cases.items():
        bad = deepcopy(two)
        a = next(g for g in bad.gates if g['name'] == 'a')
        a['carried'] = mutate(a['carried'])
        with pytest.raises(ValueError, match='carried gate'):
            checks.validate_checks(project, config, bad)
        assert label


def test_fresh_check_runs_every_gate(project, retry):
    note, flag = retry
    attempt(project, 'one')
    flag.write_text('go')
    fresh = attempt(project, 'two', carry=False)
    assert fresh.status == 'PASS' and carried(fresh) == set() and ran(note).count('a') == 2


def test_receipt_without_gate_definitions_never_donates(project, retry):
    """Receipts recorded before gates carried a definition cannot say which command passed."""
    from dataclasses import replace
    note, flag = retry
    one = attempt(project, 'one')
    legacy = replace(one, attempt='9' * 32, gates=[{k: v for k, v in g.items() if k != 'definition'} for g in one.gates])
    api('receipts').record_receipt(project, 'test', legacy)
    assert 'a' not in plan_for(project)       # the legacy copy ran a later, is no donor and carries nothing


# ---- the witness set and the donor set (op-011 t-615 round 2) ------------------------------------

def spoil(project, one, attempt_name, **changes):
    from dataclasses import replace
    return api('receipts').record_receipt(project, 'test', replace(one, attempt=attempt_name, **changes))


def spoil_builtin(project, retry, monkeypatch):
    """Check one passes a and b; then a check on the same build whose rebuild fails (fail_fast off) fails b."""
    from dataclasses import replace
    note, flag = retry
    flag.write_text('go')
    one = attempt(project, 'one')
    assert one.status == 'PASS'
    flag.unlink()
    checks = api('checks')
    real = checks.build_release
    monkeypatch.setattr(checks, 'build_release', lambda *a, **k: replace(real(*a, **k), tree_hash='f' * 64))
    config = api('config').load_config(project)
    two = attempt(project, 'two', config=replace(config, fail_fast=False))
    monkeypatch.undo()
    return one, two


def test_a_failure_in_a_check_that_cannot_donate_blocks_the_carry(project, retry, monkeypatch):
    """The A1 sequence: fail_fast off, a rebuild failure keeps check two from donating, b still fails there."""
    note, flag = retry
    one, two = spoil_builtin(project, retry, monkeypatch)
    assert next(g for g in two.gates if g['name'] == 'b')['status'] == 'FAIL'
    assert next(g for g in two.gates if g['name'] == 'reproducibility')['status'] == 'FAIL'
    three = attempt(project, 'three')                                       # b would fail again: it must run
    b = next(g for g in three.gates if g['name'] == 'b')
    assert 'carried' not in b and b['status'] == 'FAIL'
    flag.write_text('go')
    four = attempt(project, 'four')
    assert four.status == 'PASS'
    five = attempt(project, 'five')
    b = next(g for g in five.gates if g['name'] == 'b')
    assert b['carried']['attempt'] == four.attempt and b['carried']['failed_runs'] == 2
    api('checks').validate_checks(project, api('config').load_config(project), five)


@pytest.mark.parametrize('damage', ('delete', 'edit'))
def test_an_unreadable_witness_receipt_carries_nothing(project, retry, damage):
    note, flag = retry
    flag.write_text('go')
    attempt(project, 'one')                                                  # a and b pass
    flag.unlink()
    two = attempt(project, 'two', carry=False)                               # b fails
    assert set(plan_for(project)) == {'a'}                                   # the control: b's latest run failed
    path = project / '.alpaca/releases/receipts' / (two.attempt + '-check.json')
    if damage == 'delete':
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b' ')
    assert plan_for(project) == {}


def test_a_stopped_gate_is_not_a_failed_run(project, tmp_path):
    note, flag = tmp_path / 'note.txt', tmp_path / 'flag'
    set_gates(project, [gate('x', note=str(note)), flag_gate('y', note, flag)])
    approve_head(project, commit=True)
    one = attempt(project, 'one')                                            # y FAILs
    stopped = [dict(g, status='STOPPED', returncode=130) if g['name'] == 'y' else g for g in one.gates]
    spoil(project, one, 'e' * 32, gates=stopped)
    flag.write_text('go')
    two = attempt(project, 'two')
    assert two.status == 'PASS' and 'carried' not in next(g for g in two.gates if g['name'] == 'y')
    three = attempt(project, 'three')
    assert next(g for g in three.gates if g['name'] == 'y')['carried']['failed_runs'] == 1   # one FAIL, one STOPPED


def test_a_failure_under_another_interpreter_blocks_the_carry(project, retry, monkeypatch):
    note, flag = retry
    flag.write_text('go')
    one = attempt(project, 'one')
    flag.unlink()
    here = api('checks').runner()
    monkeypatch.setattr(api('checks'), 'runner', lambda: dict(here, python=here['python'] + '-other'))
    two = attempt(project, 'two')                                            # other interpreter: runs all, b fails
    monkeypatch.undo()
    assert next(g for g in two.gates if g['name'] == 'b')['status'] == 'FAIL'
    three = attempt(project, 'three')
    assert carried(three) == set() and three.status == 'FAIL'
    assert one.status == 'PASS'


def test_verify_carried_refuses_a_failure_recorded_between_donor_and_check(project, retry):
    from dataclasses import replace
    note, flag = retry
    one = attempt(project, 'one')
    flag.write_text('go')
    config, checks = api('config').load_config(project), api('checks')
    candidate = build(project, 'two')
    two = checks.check_release(project, config, candidate)
    assert 'a' in carried(two)
    failed = [dict(g, status='FAIL', returncode=1) if g['name'] == 'a' else g for g in one.gates]
    spoil(project, one, 'a' * 32, gates=failed)                              # recorded after the donor, before two
    api('receipts').record_receipt(project, 'test', two)
    with pytest.raises(ValueError, match='carried gate a'):
        checks.validate_checks(project, config, two)


def test_verify_carried_refuses_a_wrong_failed_run_count(project, retry):
    from copy import deepcopy
    note, flag = retry
    attempt(project, 'one')
    flag.write_text('go')
    two = attempt(project, 'two')
    config, checks = api('config').load_config(project), api('checks')
    checks.validate_checks(project, config, two)
    bad = deepcopy(two)
    next(g for g in bad.gates if g['name'] == 'a')['carried']['failed_runs'] = 1
    with pytest.raises(ValueError, match='failed run count'):
        checks.validate_checks(project, config, bad)
