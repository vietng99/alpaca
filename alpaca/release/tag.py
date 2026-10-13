"""Tag a merged public release and give it a GitHub release page with reviewed notes."""
from dataclasses import asdict
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

from alpaca import barrier
from alpaca.gates import leak_audit
from .checks import policy
from .config import contained
from .receipts import TagReceipt

NOTES_LIMIT = 100000


def github_api(method, path, payload=None):
    """One GitHub REST call. A 404 answers None; reads work without a token on a public repo."""
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
               'Content-Type': 'application/json'}
    token = os.environ.get('PUBLIC_REPO_TOKEN')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request('https://api.github.com' + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def public_version(api, repository, commit):
    """The VERSION the merged public tree declares, or '' when it has no alpaca/__init__.py."""
    found = api('GET', '/repos/' + repository + '/contents/alpaca/__init__.py?ref=' + commit)
    if not found:
        return ''
    text = base64.b64decode(found.get('content', '')).decode('utf-8', 'replace')
    match = re.search(r'^VERSION = "([^"]+)"$', text, re.M)
    return match.group(1) if match else ''


def tag_release(root, config, publish, version, notes_path, *, dry_run=True, api=None, expect=None, latest=True):
    root = Path(root).resolve()
    api = api or github_api
    receipt = TagReceipt({'attempt': publish.attempt, 'commit': publish.commit, 'pr_url': publish.pr_url}, 'BLOCKED')
    work = contained(root, '.alpaca/releases/' + receipt.attempt + '/tag')
    work.mkdir(parents=True)
    try:
        if publish.status != 'PASS' or not publish.commit:
            raise ValueError('tag needs a passing executed publish receipt')
        if not re.fullmatch(r'\d+\.\d+\.\d+', version or ''):
            raise ValueError('version must be X.Y.Z')
        match = re.fullmatch(r'https://github\.com/' + re.escape(config.repository) + r'/pull/(\d+)', publish.pr_url)
        if not match:
            raise ValueError('publish receipt names no pull request on ' + config.repository)
        repo = '/repos/' + config.repository
        pull = api('GET', repo + '/pulls/' + match.group(1))
        if not pull or not pull.get('merged') or not pull.get('merge_commit_sha'):
            raise ValueError('the sync pull request is not merged yet; tag after the owner merges it')
        if pull['head']['sha'] != publish.commit or pull['base']['ref'] != config.target_branch:
            raise ValueError('the merged pull request is not the one this publish receipt pushed')
        target = pull['merge_commit_sha']
        declared = public_version(api, config.repository, target)
        if declared and declared != version:
            raise ValueError('the merged tree declares VERSION ' + declared + ', not ' + version)
        notes = Path(notes_path).read_text()
        if not notes.strip() or len(notes) > NOTES_LIMIT:
            raise ValueError('release notes must be nonempty and under ' + str(NOTES_LIMIT) + ' characters')
        tag, title = 'v' + version, 'Alpaca ' + version
        _, terms, _ = policy(root)
        outgoing_text = title + '\n' + notes
        hits, _ = leak_audit.scan_text(terms, outgoing_text, 'release-notes.md', shape_on=True)
        if hits or barrier._meta_hits(terms, outgoing_text):
            raise ValueError('release notes failed the leak policy')
        receipt.notes = str(work / 'notes.md')
        Path(receipt.notes).write_text(notes)
        receipt.plan = {'repository': config.repository, 'pull': int(match.group(1)), 'tag': tag,
                        'target': target, 'title': title, 'latest': bool(latest),
                        'notes_sha256': hashlib.sha256(notes.encode()).hexdigest()}
        ref = api('GET', repo + '/git/ref/tags/' + tag)
        if ref:
            if ref.get('object', {}).get('sha') != target:
                raise ValueError('tag ' + tag + ' already exists on another commit; owner review required')
            existing = api('GET', repo + '/releases/tags/' + tag)
            if existing:
                receipt.release_url = existing.get('html_url', '')
                receipt.status = 'PASS'
                return receipt
        if dry_run:
            receipt.status = 'DRY_RUN'
            return receipt
        if expect is None or expect != receipt.plan:
            raise ValueError('release page differs from the reviewed dry run; review a new dry run')
        created = api('POST', repo + '/releases', {
            'tag_name': tag, 'target_commitish': target, 'name': title, 'body': notes,
            'draft': False, 'prerelease': False, 'make_latest': 'true' if latest else 'false'})
        receipt.release_url = (created or {}).get('html_url', '')
        receipt.status = 'PASS' if receipt.release_url else 'BLOCKED'
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        # API errors may carry request URLs or credentials. Keep safe types only.
        receipt.error = str(exc) if type(exc) is ValueError else 'tag stopped: ' + type(exc).__name__
    return receipt
