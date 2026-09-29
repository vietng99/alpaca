"""Safe publication exercised against real local Git remotes."""
from dataclasses import replace
from pathlib import Path
import subprocess
import pytest
import yaml
from alpaca.tests.test_release_pipeline_build import source, api
from alpaca.tests.test_release_pipeline_checks import project, check, approve_head


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args]).decode().strip()


@pytest.fixture
def ready(project, tmp_path):
    remote = tmp_path / 'public.git'
    seed = tmp_path / 'public'
    seed.mkdir()
    git(seed, 'init', '-q', '-b', 'main')
    git(seed, 'config', 'user.name', 'Public Release')
    git(seed, 'config', 'user.email', 'release@localhost')
    (seed / 'README.md').write_text('public base\n')
    git(seed, 'add', '.')
    git(seed, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Public base')
    git(seed, 'clone', '-q', '--bare', str(seed), str(remote))
    data = yaml.safe_load((project / 'project.yaml').read_text())
    data['release']['public_base'] = git(seed, 'rev-parse', 'HEAD')
    (project / 'project.yaml').write_text(yaml.safe_dump(data))
    git(project, 'add', '.')
    git(project, 'commit', '-qm', 'Source prepared')
    checks = check(project)
    assert checks.status == 'PASS'
    return project, remote, seed, checks


def publish(ready, **kwargs):
    root, remote, _, checks = ready
    return api('publish').publish_release(root, api('config').load_config(root), checks,
            remote=str(remote), **kwargs)


def test_dry_run_has_report_and_no_remote_mutation(ready):
    calls = []
    result = publish(ready, api=lambda *a: calls.append(a))
    assert result.status == 'DRY_RUN'
    assert not calls
    assert git(ready[1], 'branch', '--list', 'sync/main') == ''
    report = Path(result.report).read_text()
    assert 'src/app.txt' in report and 'README.md' in report and ready[3].build['source_sha'] in report


def test_publish_only_updates_sync_and_creates_pr(ready):
    before = git(ready[1], 'rev-parse', 'main')
    calls = []
    result = publish(ready, dry_run=False, api=lambda *a: calls.append(a) or 'https://github.com/sample/project/pull/1')
    assert result.status == 'PASS' and result.pr_url.endswith('/1')
    assert git(ready[1], 'rev-parse', 'main') == before
    assert git(ready[1], 'rev-parse', 'sync/main') == result.commit
    assert git(ready[1], 'log', '--format=%B', 'sync/main', '^main').startswith('Alpaca release sync')
    assert len(calls) == 1


def test_public_drift_blocks_before_push(ready):
    root, remote, seed, _ = ready
    (seed / 'unrelated.txt').write_text('public edit')
    git(seed, 'add', '.')
    git(seed, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Public edit')
    git(seed, 'push', '-q', str(remote), 'main')
    assert publish(ready, dry_run=False, api=lambda *a: 'unexpected').status == 'BLOCKED'
    assert git(remote, 'branch', '--list', 'sync/main') == ''


def test_api_failure_retry_reuses_pushed_commit(ready):
    def failing(*args):
        raise RuntimeError('API unavailable')
    first = publish(ready, dry_run=False, api=failing)
    assert first.status == 'BLOCKED' and first.commit
    second = publish(ready, dry_run=False, api=lambda *a: 'https://github.com/sample/project/pull/1')
    assert second.status == 'PASS' and second.commit == first.commit


def test_concurrent_sync_update_is_not_overwritten(ready):
    root, remote, seed, _ = ready
    def race(repo, old, new):
        git(remote, 'update-ref', 'refs/heads/sync/main', git(remote, 'rev-parse', 'main'))
    result = publish(ready, dry_run=False, api=lambda *a: 'unexpected', before_push=race)
    assert result.status == 'BLOCKED'
    assert git(remote, 'rev-parse', 'sync/main') == git(remote, 'rev-parse', 'main')


def test_metadata_leak_blocks_before_push(ready):
    root, remote, _, checks = ready
    # A policy change invalidates checks; re-check after naming the source SHA as sealed metadata.
    (root / '.alpaca/terms.txt').write_text(checks.build['source_sha'] + '\n')
    checks = api('checks').check_release(root, api('config').load_config(root), api('receipts').BuildReceipt(**checks.build))
    assert checks.status == 'PASS'
    result = publish((root, remote, ready[2], checks), dry_run=False, api=lambda *a: 'unexpected')
    assert result.status == 'BLOCKED'
    assert git(remote, 'branch', '--list', 'sync/main') == ''


def test_merged_previous_sync_can_advance_on_fresh_runner(ready):
    root, remote, seed, checks = ready
    first = publish(ready, dry_run=False, api=lambda *a: 'https://github.com/sample/project/pull/1')
    assert first.status == 'PASS'
    git(seed, 'fetch', '-q', str(remote), 'sync/main')
    git(seed, '-c', 'commit.gpgsign=false', 'merge', '--no-ff', '-m', 'Merge release', 'FETCH_HEAD')
    git(seed, 'push', '-q', str(remote), 'main')
    (root / 'src/app.txt').write_text('next release\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'New private source')
    approve_head(root)
    candidate = api('build').build_release(root, api('config').load_config(root), root / '.alpaca/releases/next/tree')
    checks = api('checks').check_release(root, api('config').load_config(root), candidate)
    second = publish((root, remote, seed, checks), dry_run=False, api=lambda *a: 'https://github.com/sample/project/pull/2')
    assert second.status == 'PASS' and second.commit != first.commit
    assert git(remote, 'show', 'sync/main:src/app.txt') == 'next release'


def test_failed_checks_never_touch_remote(ready):
    root, remote, seed, checks = ready
    result = publish((root, remote, seed, replace(checks, status='FAIL')), dry_run=False, api=lambda *a: 'unexpected')
    assert result.status == 'BLOCKED'
    assert git(remote, 'branch', '--list', 'sync/main') == ''


def test_ref_lease_catches_race_after_last_read(ready, monkeypatch):
    module = api('publish')
    original = module.remote_git
    def race(root, *args):
        if args[0] == 'push':
            git(ready[1], 'update-ref', 'refs/heads/sync/main', git(ready[1], 'rev-parse', 'main'))
        return original(root, *args)
    monkeypatch.setattr(module, 'remote_git', race)
    result = publish(ready, dry_run=False, api=lambda *a: 'unexpected')
    assert result.status == 'BLOCKED'
    assert git(ready[1], 'rev-parse', 'sync/main') == git(ready[1], 'rev-parse', 'main')


def test_unexpected_sync_commit_blocks(ready):
    git(ready[1], 'update-ref', 'refs/heads/sync/main', git(ready[1], 'rev-parse', 'main'))
    result = publish(ready, dry_run=False, api=lambda *a: 'unexpected')
    assert result.status == 'BLOCKED' and 'outside' in result.error


def test_public_target_race_after_push_blocks_pr(ready, monkeypatch):
    module = api('publish')
    original = module.remote_git
    def race(root, *args):
        output = original(root, *args)
        if args[0] == 'push':
            (ready[2] / 'public-edit').write_text('unexpected public change')
            git(ready[2], 'add', '.')
            git(ready[2], '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Public edit')
            git(ready[2], 'push', '-q', str(ready[1]), 'main')
        return output
    monkeypatch.setattr(module, 'remote_git', race)
    calls = []
    result = publish(ready, dry_run=False, api=lambda *a: calls.append(a))
    assert result.status == 'BLOCKED' and not calls


def checked_source(root, name):
    approve_head(root)
    config = api('config').load_config(root)
    candidate = api('build').build_release(root, config, root / '.alpaca/releases' / name / 'tree')
    result = api('checks').check_release(root, config, candidate)
    assert result.status == 'PASS'
    return result


def test_older_ci_retry_cannot_replace_a_newer_release(ready):
    root, remote, seed, old_checks = ready
    (root / 'src/app.txt').write_text('newer product\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Newer development')
    new_checks = checked_source(root, 'newer')
    newer = publish((root, remote, seed, new_checks), dry_run=False, api=lambda *a: 'https://github.com/sample/project/pull/1')
    assert newer.status == 'PASS'
    git(root, 'checkout', '-q', old_checks.build['source_sha'])
    old = publish(ready, dry_run=False, api=lambda *a: 'unexpected')
    assert old.status == 'BLOCKED'
    assert git(remote, 'rev-parse', 'sync/main') == newer.commit
    assert git(remote, 'show', 'sync/main:src/app.txt') == 'newer product'


def test_unrelated_source_history_is_blocked(ready):
    root, remote, seed, _ = ready
    first = publish(ready, dry_run=False, api=lambda *a: 'https://github.com/sample/project/pull/1')
    assert first.status == 'PASS'
    git(root, 'checkout', '-q', '--orphan', 'unrelated')
    git(root, 'commit', '-qm', 'Disconnected source history')
    checks = checked_source(root, 'unrelated')
    result = publish((root, remote, seed, checks), dry_run=False, api=lambda *a: 'unexpected')
    assert result.status == 'BLOCKED'
    assert git(remote, 'rev-parse', 'sync/main') == first.commit


def test_all_covered_development_shas_survive_pr_retry(ready):
    root, remote, seed, _ = ready
    assert publish(ready, dry_run=False, api=lambda *a: 'https://github.com/sample/project/pull/1').status == 'PASS'
    covered = []
    for index in range(2):
        (root / 'src/app.txt').write_text('change %d\n' % index)
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'Private change')
        covered.append(git(root, 'rev-parse', 'HEAD'))
    checks = checked_source(root, 'multiple')
    def failing(*args):
        raise RuntimeError('PR API unavailable')
    first = publish((root, remote, seed, checks), dry_run=False, api=failing)
    assert first.status == 'BLOCKED' and first.commit
    retry = publish((root, remote, seed, checks), dry_run=False, api=lambda *a: 'https://github.com/sample/project/pull/1')
    assert retry.status == 'PASS' and retry.commit == first.commit
    assert retry.covered_sources == covered == first.covered_sources
    message = git(remote, 'show', '-s', '--format=%B', retry.commit)
    report = Path(retry.report).read_text()
    assert all(sha[:12] in report and sha in message for sha in covered)


def test_published_blobs_equal_checked_bytes_under_global_conversion(ready, tmp_path, monkeypatch):
    root, remote, seed, _ = ready
    for rel in ['src/app.txt', 'src/run', 'public-project.yaml']:
        path = root / rel
        path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
    git(root, 'config', 'core.autocrlf', 'false')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Preserve exact product bytes')
    global_config = tmp_path / 'global-config'
    global_config.write_text('[core]\n autocrlf = true\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(global_config))
    checks = checked_source(root, 'crlf')
    result = publish((root, remote, seed, checks), dry_run=False, api=lambda *a: 'https://github.com/sample/project/pull/1')
    assert result.status == 'PASS'
    expected = (Path(checks.build['tree']) / 'src/app.txt').read_bytes()
    actual = subprocess.check_output(['git', '-C', str(remote), 'show', 'sync/main:src/app.txt'])
    assert actual == expected == b'product\r\n'


def test_changed_git_filter_cannot_publish_untested_blobs(ready, tmp_path, monkeypatch):
    root, remote, seed, _ = ready
    (root / 'src/.gitattributes').write_text('app.txt filter=release-fixture\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Product attributes')
    checks = checked_source(root, 'attributes')
    config = tmp_path / 'changed-git-config'
    config.write_text('[filter "release-fixture"]\n clean = tr a-z A-Z\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    result = publish((root, remote, seed, checks), dry_run=False, api=lambda *a: 'unexpected')
    assert result.status == 'BLOCKED'
    assert git(remote, 'branch', '--list', 'sync/main') == ''


def test_already_public_merge_metadata_is_not_new_outbound_data(ready):
    root, remote, seed, _ = ready
    first = publish(ready, dry_run=False, api=lambda *a: 'https://github.com/sample/project/pull/1')
    assert first.status == 'PASS'
    git(seed, 'fetch', '-q', str(remote), 'sync/main')
    git(seed, '-c', 'user.email=SecretCanaryValue@example.test', '-c', 'commit.gpgsign=false', 'merge', '--no-ff', '-m', 'Public merge', 'FETCH_HEAD')
    # The already-public merge really fails the fixture's private term policy.
    from alpaca import barrier
    cfg, terms, _ = api('checks').policy(root)
    assert barrier.scan(str(seed), [git(seed, 'rev-parse', 'HEAD')], cfg=cfg, term_list=terms).verdict == 1
    git(seed, 'push', '-q', str(remote), 'main')
    (root / 'src/app.txt').write_text('next version\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Next development change')
    checks = checked_source(root, 'after-public-merge')
    result = publish((root, remote, seed, checks), dry_run=False, api=lambda *a: 'https://github.com/sample/project/pull/2')
    assert result.status == 'PASS'
