"""Release output must be repeatable, contained and bound to its source."""
import importlib
import json
from pathlib import Path
import subprocess

import pytest
import yaml


def api(module):
    assert (Path(__file__).parents[1] / 'release' / (module + '.py')).is_file(), 'release feature not implemented'
    return importlib.import_module('alpaca.release.' + module)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'dev'
    root.mkdir()
    (root / 'src').mkdir()
    (root / 'src/app.txt').write_text('product\n')
    (root / 'src/run').write_text('#!/bin/sh\nexit 0\n')
    (root / 'src/run').chmod(0o755)
    (root / '.alpaca').mkdir()
    (root / '.alpaca/secret').write_text('private runtime')
    (root / 'ALLOWLIST').write_text('[mechanism]\nsrc/\nproject.yaml\n[memory]\n.alpaca/\n')
    cfg = {'name': 'sample', 'tier': 'public', 'barrier': {'sealed_terms': ['SecretCanaryValue']},
           'release': {'allowlist': 'ALLOWLIST', 'repository': 'sample/project',
                       'gates': [{'name': 'product-test', 'command': ['python3', '-c', 'print("ok")'],
                                  'scope': 'release', 'timeout': 10}]}}
    (root / 'project.yaml').write_text(yaml.safe_dump(cfg))
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
    for key, value in [('user.name', 'Release'), ('user.email', 'release@localhost'), ('commit.gpgsign', 'false')]:
        subprocess.run(['git', '-C', str(root), 'config', key, value], check=True)
    (root / '.gitignore').write_text('.alpaca/\n')
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'source'], check=True)
    return root


def build(source, name='one'):
    config = api('config').load_config(source)
    return api('build').build_release(source, config, source / '.alpaca/releases' / name / 'tree')


def test_allowlist_excludes_dev_state_and_preserves_modes(source):
    receipt = build(source)
    tree = Path(receipt.tree)
    assert (tree / 'src/app.txt').read_text() == 'product\n'
    assert (tree / 'src/run').stat().st_mode & 0o777 == 0o755
    assert not (tree / '.alpaca').exists()
    assert not (tree / '.git').exists()


def test_two_builds_match(source):
    first, second = build(source), build(source, 'two')
    assert first.tree_hash == second.tree_hash
    assert first.source_hash == second.source_hash


@pytest.mark.parametrize('entry', ['../escape', '/etc/passwd', 'src/../project.yaml', 'src\\app.txt', '.alpaca/'])
def test_invalid_release_path_refused(source, entry):
    (source / 'ALLOWLIST').write_text('[mechanism]\n' + entry + '\n[memory]\n.alpaca/\n')
    with pytest.raises(ValueError):
        build(source)


def test_symlink_and_parent_escape_refused(source, tmp_path):
    target = tmp_path / 'outside'
    target.write_text('never ship')
    (source / 'src/link').symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        build(source)


def test_missing_member_refused(source):
    (source / 'ALLOWLIST').write_text('[mechanism]\nmissing\n[memory]\n.alpaca/\n')
    with pytest.raises(ValueError, match='missing'):
        build(source)


def test_source_drift_invalidates_build(source):
    receipt = build(source)
    (source / 'src/app.txt').write_text('changed\n')
    with pytest.raises(ValueError, match='changed|drift'):
        api('build').validate_build(source, api('config').load_config(source), receipt)


def test_candidate_edit_invalidates_build(source):
    receipt = build(source)
    (Path(receipt.tree) / 'src/app.txt').write_text('changed\n')
    with pytest.raises(ValueError, match='changed|drift'):
        api('build').validate_build(source, api('config').load_config(source), receipt)


def test_overlay_replaces_only_allowlisted_targets(source):
    (source / 'clean.yaml').write_text('name: sample\ntemplate: true\n')
    data = yaml.safe_load((source / 'project.yaml').read_text())
    data['release']['overlays'] = {'project.yaml': 'clean.yaml'}
    (source / 'project.yaml').write_text(yaml.safe_dump(data))
    receipt = build(source)
    assert (Path(receipt.tree) / 'project.yaml').read_text() == 'name: sample\ntemplate: true\n'
    assert not (Path(receipt.tree) / 'clean.yaml').exists()


@pytest.mark.parametrize('update', [{'mandatory_checks': []}, {'gates': []}, {'repository': '../bad'},
                                   {'sync_branch': 'main'}, {'surprise': True}])
def test_invalid_release_config_refused(source, update):
    data = yaml.safe_load((source / 'project.yaml').read_text())
    data['release'].update(update)
    (source / 'project.yaml').write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        api('config').load_config(source)


def test_gate_commands_are_project_defined(source):
    data = yaml.safe_load((source / 'project.yaml').read_text())
    data['release']['gates'][0]['command'] = ['npm', 'test']
    (source / 'project.yaml').write_text(yaml.safe_dump(data))
    assert api('config').load_config(source).gates[0]['command'] == ['npm', 'test']


def test_existing_output_is_preserved(source):
    first = build(source)
    with pytest.raises(ValueError, match='exists'):
        build(source)
    assert (Path(first.tree) / 'src/app.txt').read_text() == 'product\n'


def test_receipt_is_bound_to_record(source):
    receipt = build(source)
    module = api('receipts')
    path = module.record_receipt(source, 'test', receipt)
    assert module.load_receipt(source, path, module.BuildReceipt).tree_hash == receipt.tree_hash
    content = json.loads(Path(path).read_text())
    content['tree_hash'] = '0' * 64
    Path(path).write_text(json.dumps(content))
    with pytest.raises(ValueError, match='receipt'):
        module.load_receipt(source, path, module.BuildReceipt)


def test_cli_build_records_usable_receipt(source, monkeypatch, capsys):
    from alpaca import cli
    monkeypatch.setattr(cli, '_root', lambda: str(source))
    assert 'release' in cli.registered_verbs(), 'release CLI not registered'
    assert cli.main(['--session', 'release-test', 'release', 'build']) == 0
    output = json.loads(capsys.readouterr().out)
    assert Path(output['receipt']).is_file()
    assert Path(output['tree']).is_dir()


def test_memory_class_excludes_nested_runtime(source):
    (source / 'src/private-notes').write_text('local only')
    (source / 'ALLOWLIST').write_text('[mechanism]\nsrc/\n[memory]\nsrc/private-notes\n')
    receipt = build(source)
    assert not (Path(receipt.tree) / 'src/private-notes').exists()
    assert (Path(receipt.tree) / 'src/app.txt').exists()


def test_allowlisted_bundled_skills_are_preserved(source):
    skill = source / 'plugin/skills/nuclear/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('shipped skill instructions\n')
    (source / 'ALLOWLIST').write_text('[mechanism]\nplugin/\n[memory]\n.alpaca/\n')
    receipt = build(source)
    assert (Path(receipt.tree) / 'plugin/skills/nuclear/SKILL.md').read_text() == 'shipped skill instructions\n'


def test_manifest_generator_cannot_read_publication_credentials(source, monkeypatch):
    import sys
    data = yaml.safe_load((source / 'project.yaml').read_text())
    data['release']['generated_manifest'] = [sys.executable, '-c', 'import os; assert "PUBLIC_REPO_TOKEN" not in os.environ']
    (source / 'project.yaml').write_text(yaml.safe_dump(data))
    monkeypatch.setenv('PUBLIC_REPO_TOKEN', 'fixture-credential')
    assert build(source).status == 'PASS'
