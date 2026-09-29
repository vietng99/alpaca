"""Owner approval of one exact development commit gates the real-term scan and publication."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import subprocess

from alpaca import db
from .build import allowlist, forbidden, git, is_memory, selected
from .config import contained
from .receipts import ApprovalReceipt, load_receipt, record_receipt

APPROVERS = ('owner', 'agent')
DECISIONS = ('approve', 'revoke')


def commit(root, sha):
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('approval needs a full 40-character commit SHA')
    try:
        found = git(root, 'rev-parse', '--verify', '--quiet', sha + '^{commit}').decode().strip()
    except subprocess.CalledProcessError:
        raise ValueError('approved commit does not exist in this repository') from None
    if found != sha:
        raise ValueError('approved commit does not exist in this repository')
    return sha


def delegation_file(root, delegation):
    """An agent cites a project file (optionally PATH#section) holding the owner's delegation."""
    if not delegation or any(ord(c) < 32 for c in delegation) or len(delegation) > 500:
        raise ValueError('an agent approval needs the owner delegation it acts under, as PATH[#section]')
    path = contained(root, delegation.split('#', 1)[0])
    if not path.is_file():
        raise ValueError('delegation must name an existing project file')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approve(root, sha, *, approver, delegation='', decision='approve', session='cli'):
    """Record an approval or revocation. An agent acts only under a recorded owner delegation."""
    root = Path(root).resolve()
    if approver not in APPROVERS or decision not in DECISIONS:
        raise ValueError('approver must be owner or agent; decision must be approve or revoke')
    delegation = (delegation or '').strip()
    digest = delegation_file(root, delegation) if approver == 'agent' or delegation else ''
    receipt = ApprovalReceipt(commit(root, sha), approver, decision,
                              git(root, 'rev-parse', sha + '^{tree}').decode().strip(), delegation, digest,
                              'APPROVED' if decision == 'approve' else 'REVOKED')
    return record_receipt(root, session, receipt), receipt


def reference(root, path, receipt):
    rel = Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    return {'receipt': rel, 'attempt': receipt.attempt, 'source_sha': receipt.source_sha,
            'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def current(root, sha, with_path=False):
    """The latest recorded decision for sha, if it is an approval; revocation wins when later.

    Fails closed: a broken event chain or any unreadable decision row for this commit refuses,
    so deleting or editing a revocation cannot bring an earlier approval back."""
    root = Path(root).resolve()
    conn = db.connect_readonly(str(root))
    try:
        ok, reason = db.verify_chain(conn)
        rows = [json.loads(r['data']) for r in conn.execute(
            "SELECT data FROM events WHERE kind='release-approval' ORDER BY id")]
    finally:
        conn.close()
    if not ok:
        raise ValueError('the event record does not verify: ' + reason)
    latest = None
    for data in rows:
        if data.get('source_sha') != sha:
            continue
        try:
            receipt = load_receipt(root, data.get('receipt', ''), ApprovalReceipt)
        except (ValueError, OSError, json.JSONDecodeError, TypeError):
            raise ValueError('an approval record for this commit is missing or damaged') from None
        if receipt.source_sha != sha or receipt.decision != data.get('decision'):
            raise ValueError('an approval record for this commit is missing or damaged')
        latest = (root / data['receipt'], receipt)
    if latest is None or latest[1].decision != 'approve':
        return None
    return latest if with_path else latest[1]


def clean_source(root, cfg):
    """Every release input is committed, so the approved commit is exactly what gets scanned."""
    _, memory = allowlist(root, cfg)
    tracked = set(git(root, 'ls-files', '-z').decode().split('\0'))
    required = set(selected(root, cfg)) | {cfg.allowlist, 'project.yaml'} | set(cfg.overlays.values())
    if required - tracked:
        raise ValueError('release source contains uncommitted files')
    changed = git(root, 'diff', '--name-only', '-z', 'HEAD').decode().split('\0')
    if any(rel and not forbidden(rel) and not is_memory(rel, memory) for rel in changed):
        raise ValueError('release source changed since the commit; commit, approve, rebuild and recheck')


def require(root, cfg, source_sha, expected=None):
    """Return the approval reference for source_sha or refuse before any private scan."""
    found = current(root, source_sha, with_path=True)
    if found is None:
        raise ValueError('no current owner approval for this development commit')
    path, receipt = found
    if git(root, 'rev-parse', 'HEAD').decode().strip() != source_sha:
        raise ValueError('checked-out commit differs from the approved commit')
    clean_source(root, cfg)
    ref = reference(root, path, receipt)
    if expected is not None and expected != ref:
        raise ValueError('approval changed since checks')
    return ref


def summary(path, receipt):
    return dict(asdict(receipt), receipt=str(path))
