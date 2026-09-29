"""Versioned receipts backed by the shared append-only event record."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import uuid
from datetime import datetime, timezone

from alpaca import db, util
from .config import contained


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BuildReceipt:
    source_hash: str
    tree_hash: str
    source_sha: str
    tree: str
    inventory: dict
    attempt: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = 'build'
    status: str = 'PASS'
    schema: int = 1
    created_at: str = field(default_factory=timestamp)


@dataclass
class CheckReceipt:
    build: dict
    gates: list
    status: str
    policy_hash: str = ''
    approval: dict = field(default_factory=dict)
    attempt: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = 'check'
    schema: int = 1
    created_at: str = field(default_factory=timestamp)


@dataclass
class PublishReceipt:
    checks: dict
    status: str
    public_base: str = ''
    commit: str = ''
    pr_url: str = ''
    report: str = ''
    error: str = ''
    covered_sources: list = field(default_factory=list)
    outgoing: dict = field(default_factory=dict)
    attempt: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = 'publish'
    schema: int = 1
    created_at: str = field(default_factory=timestamp)


@dataclass
class ApprovalReceipt:
    """Private record of who approved or revoked one exact development commit."""
    source_sha: str
    approver: str
    decision: str
    source_tree: str
    delegation: str = ''
    delegation_sha256: str = ''
    status: str = ''
    attempt: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = 'approval'
    schema: int = 1
    created_at: str = field(default_factory=timestamp)


ReleaseReceipt = (BuildReceipt, CheckReceipt, PublishReceipt, ApprovalReceipt)


def record_receipt(root, session, receipt):
    payload = asdict(receipt)
    path = contained(root, '.alpaca/releases/receipts/' + receipt.attempt + '-' + receipt.kind + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, sort_keys=True, indent=2) + '\n'
    if path.exists() and path.read_text() != raw:
        raise ValueError('receipt already recorded with different content')
    temp = path.with_suffix('.tmp-' + uuid.uuid4().hex)
    temp.write_text(raw)
    temp.chmod(0o600)
    os.replace(temp, path)
    conn = db.connect(str(root))
    try:
        db.append_event(conn, session=session, actor='alpaca-release', kind='release-' + receipt.kind,
                        ref=receipt.attempt, data={'receipt': str(path.relative_to(Path(root).resolve())),
                        'sha256': hashlib.sha256(raw.encode()).hexdigest(), 'status': receipt.status,
                        'summary': 'Release ' + receipt.kind + ': ' + receipt.status,
                        # Approval rows name their commit so a missing or edited receipt fails closed.
                        **({'source_sha': receipt.source_sha, 'decision': receipt.decision}
                           if receipt.kind == 'approval' else {})})
    finally:
        conn.close()
    return path


def load_receipt(root, path, cls):
    root = Path(root).resolve()
    value = Path(path)
    rel = value.relative_to(root).as_posix() if value.is_absolute() else value.as_posix()
    if not rel.startswith('.alpaca/releases/receipts/'):
        raise ValueError('receipt must be in the project release record')
    path = contained(root, rel)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    conn = db.connect_readonly(str(root))
    try:
        found = any(json.loads(r['data']).get('sha256') == sha and json.loads(r['data']).get('receipt') == rel
                    for r in conn.execute("SELECT data FROM events WHERE kind LIKE 'release-%'"))
    finally:
        conn.close()
    if not found:
        raise ValueError('receipt does not match its recorded hash')
    payload = json.loads(raw)
    if payload.get('schema') != 1 or payload.get('kind') != cls.__dataclass_fields__['kind'].default:
        raise ValueError('unsupported receipt schema or kind')
    return cls(**payload)
