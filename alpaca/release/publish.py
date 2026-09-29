"""Publish clean trees onto public history with explicit ref leases and retryable PRs."""
from dataclasses import asdict
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse
import urllib.request

from alpaca import barrier
from alpaca.gates import leak_audit
from . import approval
from .approval import clean_source
from .build import git, committed_inventory
from .checks import policy, validate_checks
from .config import contained
from .receipts import PublishReceipt

TITLE = 'Alpaca release sync'


def remote_git(root, *args):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    token = env.get('PUBLIC_REPO_TOKEN')
    if token:
        value = base64.b64encode(('x-access-token:' + token).encode()).decode()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                   GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + value)
    try:
        return subprocess.run(['git', '-C', str(root), *args], env=env, capture_output=True,
                              check=True, timeout=120).stdout.decode().strip()
    except subprocess.SubprocessError:
        raise ValueError('public Git operation failed; no credentials or remote output recorded') from None


def github_pr(config, title, body):
    token = os.environ.get('PUBLIC_REPO_TOKEN')
    if not token:
        raise ValueError('PUBLIC_REPO_TOKEN is required')
    endpoint = 'https://api.github.com/repos/' + config.repository + '/pulls'
    def request(method, url, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    query = urllib.parse.urlencode({'state': 'open', 'base': config.target_branch,
                                    'head': config.repository.split('/')[0] + ':' + config.sync_branch})
    existing = request('GET', endpoint + '?' + query)
    if len(existing) > 1:
        raise ValueError('multiple open sync pull requests need owner resolution')
    if existing:
        result = request('PATCH', endpoint + '/' + str(existing[0]['number']), {'title': title, 'body': body})
    else:
        result = request('POST', endpoint, {'title': title, 'body': body, 'head': config.sync_branch,
                                         'base': config.target_branch})
    return result['html_url']


def ancestor(repo, older, newer):
    return subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', older, newer],
                          capture_output=True).returncode == 0


def trailers(repo, commit):
    message = git(repo, 'show', '-s', '--format=%B', commit).decode()
    if not message.startswith(TITLE + '\n'):
        raise ValueError('sync branch was changed outside the release pipeline')
    result = {}
    for key in ('Base', 'Source', 'Tree'):
        matches = re.findall(r'^Alpaca-' + key + r': ([0-9a-f]{40})$', message, re.M)
        if len(matches) != 1:
            raise ValueError('sync branch release metadata is missing or invalid')
        result[key.lower()] = matches[0]
    if git(repo, 'rev-parse', commit + '^{tree}').decode().strip() != result['tree']:
        raise ValueError('sync tree differs from recorded release metadata')
    if not ancestor(repo, result['base'], commit):
        raise ValueError('sync does not descend from its public base')
    covered = re.findall(r'^Alpaca-Covered: ([0-9a-f]{40})$', message, re.M)
    result['covered'] = covered or [result['source']]
    approval = re.findall(r'^Alpaca-Approval: ([0-9a-f]{32})$', message, re.M)
    result['approval'] = approval[0] if len(approval) == 1 else ''
    return result


def report(repo, base, source_sha, inventory, covered):
    # Public text states success only: no gate names, logs, policy digests or approval details.
    changes = git(repo, 'diff', '--cached', '--name-status', base).decode()
    previous = {line.decode().split('/', 1)[0] for line in git(repo, 'ls-tree', '-r', '--name-only', base).splitlines()}
    tops = sorted({p.split('/', 1)[0] for p in inventory} - previous)
    large = [p for p in inventory if (repo / p).stat().st_size >= 1024 * 1024]
    return ('Alpaca release sync\n\nDevelopment commit: ' + source_sha +
            '\nCovered development commits: ' + ', '.join(sha[:12] for sha in covered) +
            '\n\nAll mandatory and configured release checks passed for this candidate.\n\n'
            'File changes:\n```text\n' + changes + '```\n\nNew top-level paths: ' + json.dumps(tops) +
            '\nLarge files (at least 1 MiB): ' + json.dumps(large) + '\n\nOwner review and merge required.\n')


def outgoing_record(repo, config, base, commit, body, approval_id):
    """What leaves the machine. Everything except the commit id (it carries a timestamp) must
    match the reviewed dry run before an executed publication may push."""
    raw = git(repo, 'cat-file', 'commit', commit)
    header, _, message = raw.partition(b'\n\n')
    parents = [line.split()[1].decode() for line in header.splitlines() if line.startswith(b'parent ')]
    return {'ref': 'refs/heads/' + config.sync_branch, 'base': base, 'commit': commit,
            'tree': git(repo, 'rev-parse', commit + '^{tree}').decode().strip(), 'parents': parents,
            'message_sha256': hashlib.sha256(message).hexdigest(), 'title': TITLE,
            'body_sha256': hashlib.sha256(body.encode()).hexdigest(), 'approval': approval_id}


def same_outgoing(reviewed, current):
    keys = set(reviewed) | set(current)
    return bool(reviewed) and all(reviewed.get(k) == current.get(k) for k in keys - {'commit'})


def publish_release(root, config, checks, *, dry_run=True, remote=None, api=None, before_push=None, expect=None):
    root = Path(root).resolve()
    receipt = PublishReceipt(asdict(checks), 'BLOCKED')
    work = contained(root, '.alpaca/releases/' + receipt.attempt + '/publish')
    work.mkdir(parents=True)
    repo = work / 'public'
    try:
        validate_checks(root, config, checks)
        if not dry_run:
            clean_source(root, config)
            if remote is None and not os.environ.get('PUBLIC_REPO_TOKEN'):
                raise ValueError('PUBLIC_REPO_TOKEN is required for publication')
        remote_git(work, 'clone', '-q', '--no-local', '--no-checkout', remote or ('https://github.com/' + config.repository + '.git'), str(repo))
        for key, value in [('core.autocrlf', 'false'), ('core.fileMode', 'true'),
                           ('core.attributesFile', os.devnull)]:
            git(repo, 'config', key, value)
        base = git(repo, 'rev-parse', 'refs/remotes/origin/' + config.target_branch).decode().strip()
        receipt.public_base = base
        refs = git(repo, 'for-each-ref', '--format=%(objectname)', 'refs/remotes/origin/' + config.sync_branch).decode().splitlines()
        old = refs[0] if refs else ''
        metadata = trailers(repo, old) if old else None
        if old:
            if not ancestor(root, metadata['source'], checks.build['source_sha']):
                raise ValueError('checked development source is older, unrelated, or missing prior history')
            normal_merge = ancestor(repo, old, base) and git(repo, 'rev-parse', base + '^{tree}') == git(repo, 'rev-parse', old + '^{tree}')
            if base != metadata['base'] and not normal_merge:
                raise ValueError('public branch drift: owner review required')
        elif not config.public_base or base != config.public_base:
            raise ValueError('public base is missing or changed: owner review required')
        if not old:
            receipt.covered_sources = [checks.build['source_sha']]
        elif metadata['source'] == checks.build['source_sha']:
            receipt.covered_sources = metadata['covered']
        else:
            receipt.covered_sources = git(root, 'rev-list', '--reverse', metadata['source'] + '..' + checks.build['source_sha']).decode().splitlines()
        git(repo, 'read-tree', base)
        git(repo, 'rm', '-r', '--cached', '--ignore-unmatch', '.')
        for rel in checks.build['inventory']:
            dest = repo / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            source = Path(checks.build['tree']) / rel
            dest.write_bytes(source.read_bytes())
            dest.chmod(checks.build['inventory'][rel]['mode'])
        git(repo, 'add', '--all', '--force')
        tree = git(repo, 'write-tree').decode().strip()
        if committed_inventory(repo, tree) != checks.build['inventory']:
            raise ValueError('Git staging changed checked release bytes or modes')
        body = report(repo, base, checks.build['source_sha'], checks.build['inventory'], receipt.covered_sources)
        receipt.report = str(work / 'report.md')
        Path(receipt.report).write_text(body)
        cfg, terms, _ = policy(root)
        outgoing_text = TITLE + '\n' + body
        hits, _ = leak_audit.scan_text(terms, outgoing_text, 'release-report.md', shape_on=True)
        if hits or barrier._meta_hits(terms, outgoing_text):
            raise ValueError('generated PR title or report failed the leak policy')
        approval_id = checks.approval['attempt']
        if old and metadata['tree'] == tree and metadata['source'] == checks.build['source_sha'] and metadata['approval'] == approval_id:
            commit = old
        else:
            for key, value in [('user.name', 'Alpaca Release'), ('user.email', 'release@localhost'), ('commit.gpgsign', 'false')]:
                git(repo, 'config', key, value)
            message = ('Alpaca release sync\n\nAlpaca-Base: ' + base + '\nAlpaca-Source: ' + checks.build['source_sha'] + '\nAlpaca-Tree: ' + tree + '\n')
            message += ''.join('Alpaca-Covered: ' + sha + '\n' for sha in receipt.covered_sources)
            message += 'Alpaca-Approval: ' + approval_id + '\n'
            parents = ['-p', old or base]
            if old and not ancestor(repo, base, old):
                parents += ['-p', base]
            commit = git(repo, 'commit-tree', tree, *parents, '-m', message).decode().strip()
        receipt.commit = commit
        # Exclude only known public history, never private source ancestry.
        excluded = ['--not', base] + ([old] if old else [])
        commits = git(repo, 'rev-list', commit, *excluded).decode().splitlines()
        objects = []
        for line in git(repo, 'rev-list', '--objects', commit, *excluded).splitlines():
            oid, _, path = line.partition(b' ')
            objects.append((oid.decode('ascii'), path))
        scanned = barrier.scan(str(repo), commits, refs=[('refs/heads/' + config.sync_branch, commit)],
                               objects=objects, cfg=cfg, term_list=terms)
        if scanned.verdict != 0:
            raise ValueError('outgoing Git objects or metadata failed the leak policy')
        receipt.outgoing = outgoing_record(repo, config, base, commit, body, approval_id)
        validate_checks(root, config, checks)
        if dry_run:
            receipt.status = 'DRY_RUN'
            return receipt
        if expect is not None and not same_outgoing(expect, receipt.outgoing):
            raise ValueError('outgoing release differs from the reviewed dry run; review a new dry run')
        # Refresh both refs immediately before CAS push; target is never updated here.
        if before_push:
            before_push(repo, old, commit)
        lines = remote_git(repo, 'ls-remote', 'origin', 'refs/heads/' + config.target_branch, 'refs/heads/' + config.sync_branch).splitlines()
        current = {line.split()[1]: line.split()[0] for line in lines}
        if current.get('refs/heads/' + config.target_branch) != base or current.get('refs/heads/' + config.sync_branch, '') != old:
            raise ValueError('public refs changed concurrently; rebuild publication')
        approval.require(root, config, checks.build['source_sha'], checks.approval)
        if commit != old:
            remote_git(repo, 'push', '--force-with-lease=refs/heads/' + config.sync_branch + ':' + old,
                       'origin', commit + ':refs/heads/' + config.sync_branch)
        after = remote_git(repo, 'ls-remote', 'origin', 'refs/heads/' + config.target_branch,
                           'refs/heads/' + config.sync_branch).splitlines()
        current = {line.split()[1]: line.split()[0] for line in after}
        if current.get('refs/heads/' + config.target_branch) != base or current.get('refs/heads/' + config.sync_branch) != commit:
            raise ValueError('public refs changed after push; PR creation stopped for owner review')
        # The PR carries exactly the title and body scanned above.
        approval.require(root, config, checks.build['source_sha'], checks.approval)
        receipt.pr_url = (api or github_pr)(config, TITLE, body)
        receipt.status = 'PASS'
    except (ValueError, OSError, subprocess.SubprocessError, RuntimeError) as exc:
        # API and Git exceptions may include request URLs or credentials. Keep safe types only.
        receipt.error = str(exc) if type(exc) is ValueError else 'publication stopped: ' + type(exc).__name__
    return receipt
