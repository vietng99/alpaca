"""A development tree and a release tree must say which one they are, and which release they lead to."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from alpaca import VERSION, cli, version
from alpaca.release.build import build_release
from alpaca.release.config import load_config

REPO = Path(__file__).resolve().parents[2]
DEV = re.compile(r'^%s-dev\+g[0-9a-f]{12}(\.dirty)?$' % re.escape(VERSION))


def _git(root, *args):
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / 'dev'
    (root / 'alpaca').mkdir(parents=True)
    (root / 'alpaca/code.py').write_text('x = 1\n')
    _git(tmp_path, 'init', '-q', '-b', 'main', str(root))
    for key, value in [('user.name', 'Dev'), ('user.email', 'dev@localhost'), ('commit.gpgsign', 'false')]:
        _git(root, 'config', key, value)
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'source')
    return root


@pytest.fixture
def dev_channel(monkeypatch):
    # The dev behaviour is tested in a release tree too, whose own _channel.py says "release".
    monkeypatch.setattr(version, 'CHANNEL', 'dev')


def test_dev_checkout_names_its_commit(checkout, dev_channel):
    head = subprocess.run(['git', '-C', str(checkout), 'rev-parse', '--short=12', 'HEAD'],
                          capture_output=True, text=True, check=True).stdout.strip()
    got = version.info(str(checkout))
    assert got == {'version': VERSION + '-dev+g' + head, 'base': VERSION, 'channel': 'dev',
                   'commit': head, 'dirty': False}


def test_uncommitted_code_marks_the_dev_version_dirty(checkout, dev_channel):
    (checkout / 'alpaca/code.py').write_text('x = 2\n')
    assert version.string(str(checkout)).endswith('.dirty')


def test_record_churn_outside_the_code_is_not_dirty(checkout, dev_channel):
    (checkout / 'notes.txt').write_text('one\n')
    _git(checkout, 'add', 'notes.txt')
    _git(checkout, 'commit', '-qm', 'notes')
    (checkout / 'notes.txt').write_text('two\n')
    assert not version.string(str(checkout)).endswith('.dirty')


def test_dev_tree_without_git_is_a_plain_prerelease(tmp_path, dev_channel):
    assert version.string(str(tmp_path)) == VERSION + '-dev'


def test_release_channel_is_the_bare_number(monkeypatch, checkout):
    monkeypatch.setattr(version, 'CHANNEL', 'release')
    assert version.info(str(checkout)) == {'version': VERSION, 'base': VERSION, 'channel': 'release'}


def test_this_tree_reports_its_channel():
    if version.CHANNEL == 'release':
        assert version.string() == VERSION
    else:
        assert version.CHANNEL == 'dev'
        assert DEV.match(version.string()) or version.string() == VERSION + '-dev'


def test_cli_version_flag(capsys):
    assert cli.main(['--version']) == 0
    assert capsys.readouterr().out.strip() == 'alpaca ' + version.string()


def test_project_release_config_overlays_the_release_channel():
    try:
        overlays = load_config(REPO).overlays
    except ValueError:
        pytest.skip('this tree carries no release configuration (a release tree)')
    assert overlays.get('alpaca/_channel.py') == 'templates/release/channel.py'
    assert 'CHANNEL = "release"' in (REPO / overlays['alpaca/_channel.py']).read_text()


def test_release_build_turns_a_dev_tree_into_a_release_tree(tmp_path):
    root = tmp_path / 'dev'
    for rel in ('alpaca/__init__.py', 'alpaca/version.py', 'templates/release/channel.py'):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, root / rel)
    (root / 'alpaca/_channel.py').write_text('CHANNEL = "dev"\n')
    (root / 'ALLOWLIST').write_text('[mechanism]\nalpaca/\nproject.yaml\n')
    cfg = {'name': 'sample', 'release': {
        'allowlist': 'ALLOWLIST', 'repository': 'sample/project',
        'overlays': {'alpaca/_channel.py': 'templates/release/channel.py'},
        'gates': [{'name': 'noop', 'command': ['true']}]}}
    (root / 'project.yaml').write_text(yaml.safe_dump(cfg))
    _git(tmp_path, 'init', '-q', '-b', 'main', str(root))
    for key, value in [('user.name', 'Dev'), ('user.email', 'dev@localhost'), ('commit.gpgsign', 'false')]:
        _git(root, 'config', key, value)
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'source')

    def report(tree):
        out = subprocess.run([sys.executable, '-c', 'import json; from alpaca import version; print(json.dumps(version.info()))'],
                             cwd=tree, capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    assert report(root)['channel'] == 'dev'
    receipt = build_release(root, load_config(root), root / '.alpaca/releases/one/tree')
    assert report(receipt.tree) == {'version': VERSION, 'base': VERSION, 'channel': 'release'}
