"""CI entry points run project tests only; no hosted path reaches the private term policy."""
from pathlib import Path
import json
import sys
import pytest
import yaml
from alpaca.tests.test_release_pipeline_build import source, api


def test_project_test_uses_project_command(source):
    cfg = yaml.safe_load((source / 'project.yaml').read_text())
    cfg['commands'] = {'test': "sh -c 'echo portable-project-test'"}
    (source / 'project.yaml').write_text(yaml.safe_dump(cfg))
    assert api('ci').test(source) == 0
    assert 'portable-project-test' in (source / '.alpaca/releases/ci-test.log').read_text()


@pytest.mark.parametrize('action', ['check', 'scan-pr', 'public-test'])
def test_ci_offers_no_release_scan_or_publication_action(action, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['ci', action])
    with pytest.raises(SystemExit) as exit:
        api('ci').main()
    assert exit.value.code == 2
    assert not hasattr(api('ci'), 'scan_pr') and not hasattr(api('ci'), 'check')


def test_ci_test_never_reads_private_terms(source, monkeypatch):
    cfg = yaml.safe_load((source / 'project.yaml').read_text())
    cfg['commands'] = {'test': "sh -c 'test -z \"$ALPACA_RELEASE_TERMS\" && test -z \"$PUBLIC_REPO_TOKEN\"'"}
    (source / 'project.yaml').write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv('ALPACA_RELEASE_TERMS', 'SecretCanaryValue')
    monkeypatch.setenv('PUBLIC_REPO_TOKEN', 'local-only-credential')
    assert api('ci').test(source) == 0


def test_init_cli_writes_both_workflows(source, monkeypatch, capsys):
    from alpaca import cli
    monkeypatch.setattr(cli, '_root', lambda: str(source))
    assert cli.main(['release', 'init']) == 0
    paths = json.loads(capsys.readouterr().out)
    assert Path(paths['dev']).is_file() and Path(paths['public']).is_file()
