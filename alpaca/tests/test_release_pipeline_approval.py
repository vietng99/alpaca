"""Real-term release scans and publication need an approval bound to one exact development commit."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from alpaca.tests.test_release_pipeline_build import source, api, build
from alpaca.tests.test_release_pipeline_checks import project
from alpaca.tests.test_release_pipeline_publish import git, ready


def config(root):
    return api('config').load_config(root)


def approve(root, sha=None, **kwargs):
    kwargs.setdefault('approver', 'owner')
    if sha is None and git(root, 'status', '--porcelain'):
        git(root, 'add', '-A')
        git(root, 'commit', '-qm', 'Fixture change')
    return api('approval').approve(root, sha or git(root, 'rev-parse', 'HEAD'), session='test', **kwargs)


def test_unapproved_commit_never_reaches_real_term_scan_or_gates(project, monkeypatch):
    git(project, 'add', '-A')
    git(project, 'commit', '-qm', 'Unapproved change')
    calls = []
    monkeypatch.setattr(api('checks').barrier, 'scan', lambda *a, **k: calls.append('scan'))
    monkeypatch.setattr(api('checks'), 'run_command', lambda *a, **k: calls.append('gate'))
    receipt = api('checks').check_release(project, config(project), build(project))
    assert receipt.status == 'BLOCKED'
    assert [g['name'] for g in receipt.gates] == ['approval']
    assert calls == []
    assert not receipt.approval


def test_approved_commit_checks_and_binds_its_approval(project):
    path, approval = approve(project)
    receipt = api('checks').check_release(project, config(project), build(project))
    assert receipt.status == 'PASS'
    assert receipt.approval['receipt'] == str(path.relative_to(project))
    assert receipt.approval['source_sha'] == receipt.build['source_sha'] == approval.source_sha
    api('checks').validate_checks(project, config(project), receipt)


def test_amended_content_invalidates_the_approval(project):
    approve(project)
    (project / 'src/app.txt').write_text('changed after approval\n')
    git(project, 'commit', '-qam', 'Amended after approval')
    receipt = api('checks').check_release(project, config(project), build(project))
    assert receipt.status == 'BLOCKED'
    assert [g['name'] for g in receipt.gates] == ['approval']


def test_revoked_approval_invalidates_passing_checks(project):
    approve(project)
    receipt = api('checks').check_release(project, config(project), build(project))
    assert receipt.status == 'PASS'
    approve(project, decision='revoke')
    with pytest.raises(ValueError, match='approval'):
        api('checks').validate_checks(project, config(project), receipt)


def test_agent_approval_needs_a_recorded_delegation(project):
    with pytest.raises(ValueError, match='delegation'):
        approve(project, approver='agent')
    with pytest.raises(ValueError, match='delegation'):
        approve(project, approver='agent', delegation='owner said so in chat')
    (project / '.alpaca/delegation.md').write_text('Owner delegates release steps 1-3.\n')
    _, approval = approve(project, approver='agent', delegation='.alpaca/delegation.md#steps')
    assert approval.approver == 'agent' and approval.delegation == '.alpaca/delegation.md#steps'
    assert approval.delegation_sha256 == hashlib.sha256(b'Owner delegates release steps 1-3.\n').hexdigest()
    assert api('approval').current(project, approval.source_sha).attempt == approval.attempt


@pytest.mark.parametrize('value', ['HEAD', 'main', 'abc123', '0' * 40])
def test_approval_names_an_existing_full_commit(project, value):
    with pytest.raises(ValueError):
        approve(project, value)


def test_edited_approval_file_is_not_trusted(project):
    path, approval = approve(project)
    data = json.loads(path.read_text())
    data['approver'] = 'agent'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='damaged'):
        api('approval').current(project, approval.source_sha)


@pytest.mark.parametrize('damage', ['delete', 'edit'])
def test_damaged_revocation_does_not_restore_the_approval(project, damage):
    approve(project)
    path, revoked = approve(project, decision='revoke')
    if damage == 'delete':
        path.unlink()
    else:
        path.write_text(path.read_text().replace('"revoke"', '"approve"'))
    with pytest.raises(ValueError, match='damaged'):
        api('approval').current(project, revoked.source_sha)
    receipt = api('checks').check_release(project, config(project), build(project))
    assert receipt.status == 'BLOCKED' and [g['name'] for g in receipt.gates] == ['approval']


def test_broken_event_chain_refuses_approval(project):
    from alpaca import db
    _, approval = approve(project)
    conn = db.connect(str(project))
    conn.execute("UPDATE events SET data = replace(data, 'APPROVED', 'APPROVED ') WHERE kind='release-approval'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match='does not verify'):
        api('approval').current(project, approval.source_sha)


def test_check_receipt_without_approval_is_refused_before_policy(project, monkeypatch):
    approve(project)
    receipt = api('checks').check_release(project, config(project), build(project))
    legacy = replace(receipt, approval={})
    monkeypatch.setattr(api('checks'), 'policy', lambda *a: pytest.fail('policy read before approval'))
    with pytest.raises(ValueError, match='approval'):
        api('checks').validate_checks(project, config(project), legacy)


def test_reapproval_makes_older_checks_stale(project):
    approve(project)
    receipt = api('checks').check_release(project, config(project), build(project))
    approve(project, decision='revoke')
    approve(project)
    with pytest.raises(ValueError, match='approval changed'):
        api('checks').validate_checks(project, config(project), receipt)


def test_detached_head_at_another_commit_is_refused(project):
    approve(project)
    approved = git(project, 'rev-parse', 'HEAD')
    (project / 'src/app.txt').write_text('other\n')
    git(project, 'commit', '-qam', 'Other commit')
    candidate = build(project)
    approve(project, candidate.source_sha)
    git(project, 'checkout', '-q', '--detach', approved)
    with pytest.raises(ValueError, match='differs'):
        api('approval').require(project, config(project), candidate.source_sha)


def test_publication_carries_only_minimal_approval_and_freezes_outgoing(ready):
    root, remote, _, checks = ready
    calls = []
    result = api('publish').publish_release(root, config(root), checks, dry_run=False, remote=str(remote),
                                           api=lambda cfg, title, body: calls.append((title, body)) or 'https://github.com/sample/project/pull/1')
    assert result.status == 'PASS'
    message = git(remote, 'log', '-1', '--format=%B', 'sync/main')
    assert 'Alpaca-Approval: ' + checks.approval['attempt'] in message
    public = message + ''.join(t + b for t, b in calls)
    for private in (checks.policy_hash, checks.approval['sha256'], checks.approval['receipt'], 'owner', 'delegation'):
        assert private not in public
    for gate in checks.gates:
        assert gate['name'] not in public
    title, body = calls[0]
    assert result.outgoing == {
        'ref': 'refs/heads/sync/main', 'base': result.public_base, 'commit': result.commit,
        'tree': git(remote, 'rev-parse', 'sync/main^{tree}'), 'parents': [result.public_base],
        'message_sha256': hashlib.sha256((message + '\n').encode()).hexdigest(), 'title': title,
        'body_sha256': hashlib.sha256(body.encode()).hexdigest(), 'approval': checks.approval['attempt']}


def publish(ready, **kwargs):
    root, remote, _, checks = ready
    return api('publish').publish_release(root, config(root), checks, remote=str(remote),
                                         api=lambda *a: 'https://github.com/sample/project/pull/1', **kwargs)


def test_execute_must_match_the_reviewed_dry_run(ready):
    root, remote, seed, checks = ready
    dry = publish(ready)
    assert dry.status == 'DRY_RUN'
    (seed / 'public-edit.txt').write_text('public change after review\n')
    git(seed, 'add', '.')
    git(seed, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Public change')
    git(seed, 'push', '-q', str(remote), 'main')
    data = yaml.safe_load((root / 'project.yaml').read_text())
    result = publish(ready, dry_run=False, expect=dict(dry.outgoing, base='1' * 40))
    assert result.status == 'BLOCKED' and git(remote, 'branch', '--list', 'sync/main') == ''


def test_execute_matching_dry_run_publishes_and_retry_still_matches(ready):
    root, remote, _, checks = ready
    dry = publish(ready)
    def failing(*args):
        raise RuntimeError('API unavailable')
    first = api('publish').publish_release(root, config(root), checks, remote=str(remote), dry_run=False,
                                          expect=dry.outgoing, api=failing)
    assert first.status == 'BLOCKED' and first.commit
    retry = publish(ready, dry_run=False, expect=dry.outgoing)
    assert retry.status == 'PASS' and retry.commit == first.commit


def test_revocation_just_before_push_stops_publication(ready):
    root, remote, _, _ = ready
    result = publish(ready, dry_run=False, before_push=lambda *a: approve(root, decision='revoke'))
    assert result.status == 'BLOCKED' and 'approval' in result.error
    assert git(remote, 'branch', '--list', 'sync/main') == ''


def test_revocation_after_check_stops_publication_before_push(ready):
    root, remote, _, checks = ready
    approve(root, decision='revoke')
    result = api('publish').publish_release(root, config(root), checks, dry_run=False, remote=str(remote),
                                           api=lambda *a: 'unexpected')
    assert result.status == 'BLOCKED' and 'approval' in result.error
    assert git(remote, 'branch', '--list', 'sync/main') == ''


def test_uncommitted_edit_is_not_covered_by_the_approval(project):
    approve(project)
    (project / 'src/app.txt').write_text('uncommitted after approval\n')
    receipt = api('checks').check_release(project, config(project), build(project))
    assert receipt.status == 'BLOCKED'
    assert [g['name'] for g in receipt.gates] == ['approval']


def test_approve_cli_records_delegated_agent_approval(project, monkeypatch, capsys):
    from alpaca import cli
    monkeypatch.setattr(cli, '_root', lambda: str(project))
    sha = git(project, 'rev-parse', 'HEAD')
    assert cli.main(['release', 'approve', sha, '--by', 'agent']) == 2
    capsys.readouterr()
    (project / '.alpaca/delegation.md').write_text('Owner delegation record.\n')
    assert cli.main(['release', 'approve', sha, '--by', 'agent', '--delegation', '.alpaca/delegation.md']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'APPROVED' and out['source_sha'] == sha
    receipt = api('receipts').load_receipt(project, out['receipt'], api('receipts').ApprovalReceipt)
    assert receipt.delegation == '.alpaca/delegation.md'


@pytest.mark.parametrize('agent_env', [True, False])
def test_owner_approval_is_refused_from_agent_session_or_non_terminal(project, monkeypatch, capsys, agent_env):
    from alpaca import cli
    import sys
    monkeypatch.setattr(cli, '_root', lambda: str(project))
    for key in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SANDBOX'):
        monkeypatch.delenv(key, raising=False)
    if agent_env:
        monkeypatch.setenv('CLAUDECODE', '1')
        monkeypatch.setattr(sys.stdin, 'isatty', lambda: True, raising=False)
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('agent must not reach the prompt'))
    sha = git(project, 'rev-parse', 'HEAD')
    assert cli.main(['release', 'approve', sha, '--by', 'owner']) == 2
    assert 'interactive terminal' in capsys.readouterr().out
    assert api('approval').current(project, sha) is None


def test_owner_approval_typed_at_terminal_is_recorded(project, monkeypatch, capsys):
    from alpaca import cli
    import io
    monkeypatch.setattr(cli, '_root', lambda: str(project))
    for key in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SANDBOX'):
        monkeypatch.delenv(key, raising=False)
    tty = io.StringIO()
    tty.isatty = lambda: True
    monkeypatch.setattr('sys.stdin', tty)
    sha = git(project, 'rev-parse', 'HEAD')
    monkeypatch.setattr('builtins.input', lambda *a: sha[:11])
    assert cli.main(['release', 'approve', sha, '--by', 'owner']) == 2
    monkeypatch.setattr('builtins.input', lambda *a: sha[:12])
    assert cli.main(['release', 'approve', sha, '--by', 'owner']) == 0
    assert api('approval').current(project, sha).approver == 'owner'


def test_execute_cli_needs_a_reviewed_dry_run(ready, monkeypatch, capsys):
    from alpaca import cli
    root = ready[0]
    monkeypatch.setattr(cli, '_root', lambda: str(root))
    path = api('receipts').record_receipt(root, 'test', ready[3])
    assert cli.main(['release', 'publish', str(path), '--execute']) == 2
    assert '--expect' in capsys.readouterr().out
