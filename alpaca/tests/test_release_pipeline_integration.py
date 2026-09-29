"""The public CLI handles an entire non-Python project's release and retry."""
import json
from pathlib import Path
import yaml
from alpaca.tests.test_release_pipeline_build import source, api
from alpaca.tests.test_release_pipeline_checks import project
from alpaca.tests.test_release_pipeline_publish import ready, git


def test_complete_cli_flow_for_non_python_project(ready, monkeypatch, capsys):
    from alpaca import cli
    root, remote, _, _ = ready
    cfg = yaml.safe_load((root / 'project.yaml').read_text())
    cfg['release']['gates'] = [{'name': 'portable-product', 'command': ['sh', '-c', 'test -f src/app.txt && ./src/run']}]
    (root / 'project.yaml').write_text(yaml.safe_dump(cfg))
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Configure portable product gates')
    monkeypatch.setattr(cli, '_root', lambda: str(root))
    module = api('publish')
    original = module.publish_release
    calls = []
    def publish(root, config, checks, **kwargs):
        return original(root, config, checks, remote=str(remote),
                        api=lambda *a: calls.append(a) or 'https://github.com/sample/project/pull/1', **kwargs)
    monkeypatch.setattr(module, 'publish_release', publish)
    def command(*args):
        capsys.readouterr()
        assert cli.main(['--session', 'integration', 'release', *args]) == 0
        return json.loads(capsys.readouterr().out)
    command('init')
    # Generated dev workflow becomes a source input before release evidence is frozen.
    git(root, 'add', '.github/workflows/alpaca-dev.yml')
    git(root, 'commit', '-qm', 'Configure development workflow')
    (root / 'DELEGATION.md').write_text('Owner delegates release approval for this fixture.\n')
    approval = command('approve', git(root, 'rev-parse', 'HEAD'), '--by', 'agent', '--delegation', 'DELEGATION.md#release')
    assert approval['status'] == 'APPROVED'
    build = command('build')
    checks = command('check', build['receipt'])
    assert checks['status'] == 'PASS'
    dry = command('publish', checks['receipt'])
    assert dry['status'] == 'DRY_RUN' and not calls
    first = command('publish', checks['receipt'], '--execute', '--expect', dry['receipt'])
    assert first['status'] == 'PASS'
    commit = git(remote, 'rev-parse', 'sync/main')
    second = command('publish', checks['receipt'], '--execute', '--expect', dry['receipt'])
    assert second['pr_url'] == first['pr_url'] and len(calls) == 2
    assert git(remote, 'rev-parse', 'sync/main') == commit
    assert '.github' not in git(remote, 'ls-tree', '--name-only', 'sync/main')
    receipt = api('receipts').load_receipt(root, second['receipt'], api('receipts').PublishReceipt)
    assert receipt.commit == commit and receipt.checks['status'] == 'PASS'
