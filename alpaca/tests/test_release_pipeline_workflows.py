"""Workflow generation preserves local edits and limits publication authority."""
from pathlib import Path
import re
import pytest
import yaml
from alpaca.tests.test_release_pipeline_build import source, api


def test_init_is_idempotent_and_returns_both_paths(source):
    module = api('workflows')
    paths = module.initialize(source, api('config').load_config(source))
    assert set(paths) == {'dev', 'public'}
    content = {k: Path(v).read_bytes() for k, v in paths.items()}
    assert module.initialize(source, api('config').load_config(source)) == paths
    assert content == {k: Path(v).read_bytes() for k, v in paths.items()}


def test_init_refuses_edited_file_before_writing_any_other(source):
    module = api('workflows')
    public = source / '.alpaca/releases/workflows/alpaca-public.yml'
    public.parent.mkdir(parents=True)
    public.write_text('owner edit\n')
    with pytest.raises(ValueError, match='overwrite'):
        module.initialize(source, api('config').load_config(source))
    assert public.read_text() == 'owner edit\n'
    assert not (source / '.github/workflows/alpaca-dev.yml').exists()


def steps(data):
    return [step for job in data['jobs'].values() for step in job['steps']]


def test_dev_workflow_tests_without_credentials_scan_or_publication(source):
    paths = api('workflows').initialize(source, api('config').load_config(source))
    text = Path(paths['dev']).read_text()
    data = yaml.safe_load(text)
    assert set(data['jobs']) == {'tests'}
    assert data['jobs']['tests']['strategy']['matrix']['python'] == ['3.10', '3.12']
    assert data['concurrency']['cancel-in-progress'] is False
    assert data['permissions'] == {'contents': 'read'}
    for step in steps(data):
        if 'uses' in step:
            assert re.fullmatch(r'actions/[a-z-]+@[0-9a-f]{40}', step['uses'])
    assert steps(data)[-1]['run'] == 'bin/alpaca-python -m alpaca.release.ci test'
    for private in ('secrets.', 'PUBLIC_REPO_TOKEN', 'ALPACA_RELEASE_TERMS', 'release publish', 'ci check'):
        assert private not in text


def test_public_workflow_has_no_privileged_trigger_secret_or_scan(source):
    paths = api('workflows').initialize(source, api('config').load_config(source))
    text = Path(paths['public']).read_text()
    data = yaml.safe_load(text)
    assert set(data['on']) == {'pull_request'}
    assert set(data['jobs']) == {'tests'} and 'if' not in data['jobs']['tests']
    assert data['permissions'] == {'contents': 'read'}
    assert steps(data)[-1]['run'] == 'bin/alpaca-python -m alpaca.release.ci test'
    for private in ('pull_request_target', 'secrets.', 'ALPACA_RELEASE_TERMS', 'scan-pr', 'PUBLIC_REPO_TOKEN'):
        assert private not in text
