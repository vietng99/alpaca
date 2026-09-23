"""Approved project artifact bytes and immutable, rebuildable catalog manifests.

No authority database and no deletion operation. An object is retained only after its
hash-checked bytes and containing directory have been synced. JSON observations may
be imported into the project database; they never rewrite old profile receipt manifests.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid

STORE = '.alpaca/artifacts'
MAX_OBJECT_BYTES = 1024 ** 3
MAX_TOTAL_BYTES = 20 * 1024 ** 3
DIGEST = re.compile(r'[a-f0-9]{64}')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_destination(path):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError('symlink storage destination refused')
    return path


def atomic_json(path, value):
    path = _safe_destination(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.publish-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            payload = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
            if stream.write(payload) != len(payload):
                raise OSError('short manifest write')
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def contained(root, source):
    root = Path(root).resolve()
    candidate = Path(source)
    candidate = candidate if candidate.is_absolute() else root / candidate
    # Refuse symlink components, including links that presently resolve inside root.
    candidate.absolute().relative_to(root)
    relative = candidate.absolute().relative_to(root)
    cursor = root
    for part in relative.parts:
        if part in ('.', '..'):
            raise ValueError('unsafe source path')
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError('symlink source refused')
    candidate.resolve().relative_to(root)
    return candidate


@contextlib.contextmanager
def _locked(root):
    directory = contained(root, Path(root) / STORE)
    directory.mkdir(parents=True, exist_ok=True)
    with contained(root, directory / '.lock').open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _object(root, sha):
    if not DIGEST.fullmatch(str(sha)):
        raise ValueError('invalid object digest')
    return contained(root, Path(root) / STORE / 'objects/sha256' / sha[:2] / sha)


def _bytes(root):
    directory = Path(root) / STORE / 'objects'
    return sum(p.stat().st_size for p in directory.rglob('*') if p.is_file() and not p.is_symlink())


def _publish(source, destination, expected):
    destination = _safe_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.object-', suffix='.tmp', dir=destination.parent)
    try:
        sha = hashlib.sha256()
        with os.fdopen(fd, 'wb') as out, source.open('rb') as inp:
            before = os.fstat(inp.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('not a regular artifact')
            for chunk in iter(lambda: inp.read(1024 * 1024), b''):
                if out.write(chunk) != len(chunk):
                    raise OSError('short object write')
                sha.update(chunk)
            after = os.fstat(inp.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or sha.hexdigest() != expected:
                raise ValueError('source changed during publication')
            out.flush(); os.fsync(out.fileno())
        os.chmod(temp, 0o400)
        os.replace(temp, destination)
        _sync_dir(destination.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def capture(root, source, *, expected_sha256=None, receipt_id=None, role='result', pin=None,
            max_object_bytes=MAX_OBJECT_BYTES, max_total_bytes=MAX_TOTAL_BYTES):
    root = Path(root).resolve()
    row = {'schema_version': 1, 'id': uuid.uuid4().hex, 'created_at': time.time(),
           'source': str(source), 'receipt_id': receipt_id, 'role': role,
           'sha256': expected_sha256, 'bytes': None, 'availability': 'unavailable',
           'object_path': None, 'reason': None, 'source_availability': 'unavailable'}
    with _locked(root):
        try:
            src = contained(root, source)
            if not src.is_file():
                row.update(availability='missing', source_availability='missing', reason='source is absent')
            else:
                before = src.stat()
                row['bytes'] = before.st_size
                actual = digest(src)
                after = src.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError('source changed while hashing')
                row['source_availability'] = 'changed' if expected_sha256 and actual != expected_sha256 else 'available'
                row['sha256'] = expected_sha256 or actual
                obj = _object(root, row['sha256'])
                if obj.is_file() and digest(obj) == row['sha256']:
                    row.update(availability='retained', bytes=obj.stat().st_size, object_path=str(obj.relative_to(root)))
                elif expected_sha256 and actual != expected_sha256:
                    row.update(availability='changed', reason='source differs from recorded digest')
                elif row['bytes'] > max_object_bytes:
                    row.update(availability='indexed-only', reason='per-object budget')
                elif _bytes(root) + row['bytes'] > max_total_bytes:
                    row.update(availability='indexed-only', reason='global object budget')
                else:
                    try:
                        _publish(src, obj, row['sha256'])
                    except OSError as exc:
                        row.update(availability='indexed-only', reason=str(exc))
                    else:
                        row.update(availability='retained', bytes=obj.stat().st_size, object_path=str(obj.relative_to(root)))
        except (OSError, ValueError) as exc:
            row.update(reason=str(exc))
        if expected_sha256 and DIGEST.fullmatch(str(expected_sha256)):
            obj = _object(root, expected_sha256)
            if obj.is_file() and not obj.is_symlink() and digest(obj) == expected_sha256:
                row.update(availability='retained', bytes=obj.stat().st_size,
                           object_path=str(obj.relative_to(root)), source_reason=row.get('reason'), reason=None)
        if pin and row['sha256']:
            _pin(root, row['sha256'], pin)
        atomic_json(root / STORE / 'manifests' / (row['id'] + '.json'), row)
        atomic_json(root / STORE / 'latest-publication.json', row)
    return row


def _pin(root, sha, reason):
    if not DIGEST.fullmatch(str(sha)) or not str(reason).strip():
        raise ValueError('pin requires digest and reason')
    ident = uuid.uuid4().hex
    atomic_json(Path(root) / STORE / 'pins' / (ident + '.json'), {
        'schema_version': 1, 'id': ident, 'sha256': sha, 'reason': str(reason), 'created_at': time.time()})


def pin(root, sha, reason):
    with _locked(root):
        _pin(root, sha, reason)
    return {'sha256': sha, 'pinned': True, 'reason': reason}


def inventory(root):
    """Current availability joined onto immutable observations, without writes."""
    root = Path(root).resolve(); rows = []
    for path in sorted(contained(root, root / STORE / 'manifests').glob('*.json')):
        row = json.loads(path.read_text())
        sha = row.get('sha256')
        if sha and DIGEST.fullmatch(str(sha)):
            obj = _object(root, sha)
            if obj.is_file() and not obj.is_symlink() and digest(obj) == sha:
                row.update(availability='retained', bytes=obj.stat().st_size, object_path=str(obj.relative_to(root)), reason=None)
            elif row.get('availability') == 'retained':
                row.update(availability='missing' if not obj.exists() else 'changed', object_path=None,
                           reason='retained object is missing or corrupt')
        rows.append(row)
    return rows


def retention_dry_run(root, *, budget_bytes=0):
    pins = {json.loads(p.read_text())['sha256'] for p in contained(root, Path(root) / STORE / 'pins').glob('*.json')}
    # A seal copy is a conservative retention reference even if its old source moved.
    # False extra protection is safe; this operation never deletes anything.
    for seal in (Path(root) / '.alpaca/proofs').rglob('*.seal.json'):
        data = json.loads(seal.read_text())
        pins.update(item.get('sha256') for item in data.get('evidence', []) if item.get('sha256'))
    objects = {}
    for row in inventory(root):
        if row['availability'] == 'retained':
            objects[row['sha256']] = row
    total = sum(row['bytes'] or 0 for row in objects.values())
    candidates = []
    for row in sorted(objects.values(), key=lambda item: (item['created_at'], item['sha256'])):
        if total <= max(0, budget_bytes):
            break
        if row['sha256'] not in pins:
            candidates.append({'sha256': row['sha256'], 'bytes': row['bytes'], 'object_path': row['object_path']})
            total -= row['bytes'] or 0
    return {'dry_run': True, 'candidates': candidates, 'pinned_objects': len(pins),
            'remaining_bytes': total, 'budget_bytes': budget_bytes, 'deletion_supported': False}


def catalog_archives(root, *, recover=True):
    root = Path(root).resolve(); rows = []; issues = []
    # Archive manifests belong to a domain profile (alpaca/profile.py `paths` "archives": glob
    # patterns relative to the root); without one there is nothing to catalog.
    from alpaca import profile
    patterns = profile.listed(profile.load(str(root)).paths(), 'archives')
    manifests = sorted({p for g in patterns for p in root.glob(g.lstrip('/')) if p.name == 'manifest.json'})
    for path in manifests:
        try:
            manifest = json.loads(path.read_text())
            for group in ('files', 'indexed', 'results_index'):
                for entry in manifest.get(group, []):
                    sha = entry.get('sha256')
                    candidate = path.parent / entry['path'] if group == 'files' else Path(entry.get('source') or '')
                    if not sha:
                        row = {'schema_version': 1, 'id': uuid.uuid4().hex, 'created_at': time.time(),
                               'source': entry.get('source'), 'receipt_id': manifest.get('receipt_id'),
                               'role': group, 'sha256': None, 'bytes': entry.get('bytes'),
                               'availability': 'indexed-only', 'object_path': None,
                               'reason': 'historical identity unavailable; original bytes cannot be verified'}
                        with _locked(root):
                            atomic_json(root / STORE / 'manifests' / (row['id'] + '.json'), row)
                        rows.append(row)
                        continue
                    row = capture(root, candidate, expected_sha256=sha, receipt_id=manifest.get('receipt_id'),
                                  role=group, max_total_bytes=MAX_TOTAL_BYTES if recover else 0)
                    rows.append(row)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append({'path': str(path.relative_to(root)), 'reason': str(exc)})
    counts = {}
    for row in rows:
        counts[row['availability']] = counts.get(row['availability'], 0) + 1
    result = {'cataloged': len(rows), 'availability': counts, 'issues': issues, 'verified_at': time.time()}
    atomic_json(root / STORE / 'latest-catalog.json', result)
    return result


def status(root):
    result = {'status': 'unavailable', 'evidence': 'last-recorded-publication'}
    for name, field in [('latest-publication.json', 'latest_publication'), ('latest-catalog.json', 'catalog')]:
        try:
            result[field] = json.loads((Path(root) / STORE / name).read_text())
        except (OSError, ValueError):
            continue
    if result.get('latest_publication'):
        result['status'] = 'ok' if result['latest_publication']['availability'] == 'retained' else 'partial'
    if result.get('catalog'):
        catalog = result['catalog']
        gaps = sum(count for state, count in catalog['availability'].items() if state != 'retained')
        if gaps or catalog['issues']:
            result['status'] = 'partial'
        elif catalog['cataloged']:
            result['status'] = 'ok'
    return result


from alpaca import cli


@cli.command('artifact')
def cmd_artifact(args):
    root = cli._root()
    if args.artifact_verb == 'catalog':
        result = catalog_archives(root)
    elif args.artifact_verb == 'status':
        result = {'artifacts': inventory(root)}
    elif args.artifact_verb == 'pin':
        result = pin(root, args.sha256, args.reason)
    elif args.artifact_verb == 'retention-dry-run':
        result = retention_dry_run(root, budget_bytes=args.budget_bytes)
    else:
        return cli.USAGE
    print(json.dumps(result, indent=2)); return cli.PASS


def _parser(sub):
    p = sub.add_parser('artifact'); commands = p.add_subparsers(dest='artifact_verb')
    commands.add_parser('catalog'); commands.add_parser('status')
    p = commands.add_parser('pin'); p.add_argument('sha256'); p.add_argument('--reason', required=True)
    p = commands.add_parser('retention-dry-run'); p.add_argument('--budget-bytes', type=int, default=0)


cli.register_parser('artifact', _parser)
