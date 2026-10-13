"""A release reads its commit from a private checkout, so the shared project root can move under it."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from alpaca.tests.test_release_pipeline_build import source, api
from alpaca.tests.test_release_pipeline_checks import project, approve_head, configure


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args]).decode().strip()


def commit_fixture(root):
    if git(root, 'status', '--porcelain'):
        git(root, 'add', '-A')
        git(root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture change')
    return git(root, 'rev-parse', 'HEAD')


def snapshot_check(project):
    """Approve HEAD, build it from its checkout and check it there, as the CLI does."""
    sha = commit_fixture(project)
    approve_head(project)
    tree = api('source').snapshot(project, sha)
    config = api('config').load_config(tree)
    build = api('build').build_release(project, config, project / '.alpaca/releases/one/tree', tree)
    return api('checks').check_release(project, config, build, tree), tree, config


# A gate standing in for another session: it edits a tracked file in the project root and commits it.
MOVE_MAIN = ('import subprocess, pathlib; root = pathlib.Path(%r); (root / "src/app.txt").write_text("moved on\\n"); '
             'subprocess.run(["git", "-C", str(root), "-c", "commit.gpgsign=false", "commit", "-qam", "Other session"], check=True)')


def test_snapshot_is_the_commit_without_release_state(project):
    (project / 'tracked-state').mkdir()
    sha = commit_fixture(project)
    tree = api('source').snapshot(project, sha)
    assert tree == project / '.alpaca/releases/source' / sha
    assert git(tree, 'rev-parse', 'HEAD') == sha
    assert (tree / 'src/app.txt').read_text() == 'product\n'
    assert (tree / 'src/run').stat().st_mode & 0o111
    assert not (tree / '.alpaca').exists()
    assert api('source').snapshot(project, sha) == tree
    assert git(project, 'status', '--porcelain') == ''


def test_snapshot_leaves_out_tracked_alpaca_files(project):
    (project / '.gitignore').write_text('')
    (project / '.alpaca/record.txt').write_text('runtime record\n')
    git(project, 'add', '-f', '.alpaca/record.txt')
    sha = commit_fixture(project)
    tree = api('source').snapshot(project, sha)
    assert not (tree / '.alpaca/record.txt').exists()
    assert git(tree, 'status', '--porcelain') == ''


@pytest.mark.parametrize('sha', ['HEAD', 'abc', '0' * 40])
def test_snapshot_needs_an_existing_full_sha(project, sha):
    with pytest.raises(ValueError):
        api('source').snapshot(project, sha)


def test_a_commit_in_the_project_root_during_the_check_does_not_fail_it(project):
    configure(project, [sys.executable, '-c', MOVE_MAIN % str(project)])
    receipt, tree, config = snapshot_check(project)
    assert receipt.status == 'PASS', [g for g in receipt.gates if g['status'] != 'PASS']
    assert (project / 'src/app.txt').read_text() == 'moved on\n'
    assert git(project, 'rev-parse', 'HEAD') != receipt.build['source_sha']
    api('checks').validate_checks(project, config, receipt, tree)


def test_without_the_snapshot_the_same_commit_fails_the_check(project):
    configure(project, [sys.executable, '-c', MOVE_MAIN % str(project)])
    commit_fixture(project)
    approve_head(project)
    config = api('config').load_config(project)
    build = api('build').build_release(project, config, project / '.alpaca/releases/one/tree')
    receipt = api('checks').check_release(project, config, build)
    assert receipt.status != 'PASS'
    assert any(g['name'] == 'input-integrity' for g in receipt.gates)


def test_an_edit_inside_the_snapshot_during_the_check_fails_it(project):
    edit = ('import pathlib; [p for p in pathlib.Path(%r).glob(".alpaca/releases/source/*/src/app.txt")][0]'
            '.write_text("tampered\\n")' % str(project))
    configure(project, [sys.executable, '-c', edit])
    receipt, tree, _ = snapshot_check(project)
    assert receipt.status != 'PASS'
    assert any(g['name'] == 'input-integrity' and g['status'] == 'FAIL' for g in receipt.gates)


def test_publish_validation_reads_the_snapshot_after_main_moved(project):
    receipt, tree, config = snapshot_check(project)
    assert receipt.status == 'PASS'
    (project / 'src/app.txt').write_text('moved on\n')
    git(project, '-c', 'commit.gpgsign=false', 'commit', '-qam', 'Other session')
    api('checks').validate_checks(project, config, receipt, tree)
    with pytest.raises(ValueError):
        api('checks').validate_checks(project, config, receipt)


def test_cli_builds_and_checks_from_the_checkout(project, monkeypatch, capsys):
    from alpaca import cli
    sha = commit_fixture(project)
    approve_head(project)
    monkeypatch.setattr(cli, '_root', lambda: str(project))
    (project / 'src/app.txt').write_text('uncommitted edit in the shared root\n')
    assert cli.main(['release', 'build']) == 0
    built = json.loads(capsys.readouterr().out)
    assert (Path(built['tree']) / 'src/app.txt').read_text() == 'product\n'
    assert cli.main(['release', 'check', built['receipt']]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'PASS'
    assert (project / '.alpaca/releases/source' / sha).is_dir()


def test_cli_build_takes_an_older_commit(project, monkeypatch, capsys):
    from alpaca import cli
    first = commit_fixture(project)
    (project / 'src/app.txt').write_text('newer\n')
    commit_fixture(project)
    monkeypatch.setattr(cli, '_root', lambda: str(project))
    assert cli.main(['release', 'build', '--commit', first]) == 0
    built = json.loads(capsys.readouterr().out)
    assert (Path(built['tree']) / 'src/app.txt').read_text() == 'product\n'


def test_old_unused_snapshots_are_removed_when_a_new_one_is_made(project):
    import os, time
    old = commit_fixture(project)
    stale = api('source').snapshot(project, old)
    past = time.time() - api('source').KEEP_S - 60
    os.utime(stale, (past, past))
    (project / 'src/app.txt').write_text('newer\n')
    api('source').snapshot(project, commit_fixture(project))
    assert not stale.exists()


@pytest.mark.parametrize('hide', ['assume-unchanged', 'skip-worktree', 'none'])
def test_a_hidden_edit_in_a_reused_snapshot_is_refused(project, hide):
    sha = commit_fixture(project)
    tree = api('source').snapshot(project, sha)
    if hide != 'none':
        git(tree, 'update-index', '--' + hide, 'src/app.txt')
    (tree / 'src/app.txt').write_text('product\nshipped without review\n')
    if hide == 'assume-unchanged':
        assert git(tree, 'diff', '--name-only', 'HEAD') == ''          # git itself no longer sees it
    with pytest.raises(ValueError, match='differs from its commit'):
        api('source').snapshot(project, sha)


def test_a_mode_change_or_an_added_file_in_a_reused_snapshot_is_refused(project):
    sha = commit_fixture(project)
    tree = api('source').snapshot(project, sha)
    (tree / 'src/app.txt').chmod(0o755)
    with pytest.raises(ValueError, match='differs'):
        api('source').snapshot(project, sha)
    (tree / 'src/app.txt').chmod(0o644)
    (tree / 'src/extra.txt').write_text('planted\n')
    git(tree, 'add', 'src/extra.txt')
    with pytest.raises(ValueError, match='does not hold'):
        api('source').snapshot(project, sha)
