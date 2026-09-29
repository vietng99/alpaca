"""A release cannot publish past missing, stale, leaking or failed evidence."""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time

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
    configure(project, ['python', '-c', 'import sys; assert sys.executable == ' + repr(sys.executable)])
    assert check(project).status == 'PASS'


def test_gate_does_not_inherit_claude_session_or_project(project, monkeypatch):
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'parent-session')
    monkeypatch.setenv('CLAUDE_PROJECT_DIR', '/parent/project')
    configure(project, [sys.executable, '-c', 'import os; assert "CLAUDE_CODE_SESSION_ID" not in os.environ; assert "CLAUDE_PROJECT_DIR" not in os.environ'])
    assert check(project).status == 'PASS'
