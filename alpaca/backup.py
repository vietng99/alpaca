"""Local coordinated SQLite snapshots with explicit file-source replay semantics.

The project and wiki write boundaries are held together only while both SQLite backup
API snapshots are made. Guards are released before file copies and verification. Raw files can precede a wiki DB
commit, so restore preserves them and requires idempotent raw ingestion. The wiki
ledger is materialized from the snapshot's committed, verified ingest_event rows.
Backup directories never contain executable tools, host credentials or worktrees.
"""
from __future__ import annotations

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid

from alpaca import artifacts, db, proof

#: runtime directories every backup copies. A domain profile adds its own (alpaca/profile.py
#: `paths` "backup"), see _roots.
ROOTS = ('.alpaca/artifacts', '.alpaca/proofs', '.alpaca/transcripts', '.alpaca/wiki',
         '.alpaca/pool/tools', '.alpaca/pool/hooks', '.alpaca/spool', '.alpaca/observability/spool')
FILES = ('.alpaca/instance.json',)
EXCLUDED = {'worktrees', '.git', '.venv', 'backups', 'repairs', 'config',
            'credentials', 'secrets', '__pycache__'}
RESUME = ('source', 'cursor', 'consumer', 'checkpoint', 'generation', 'projection_job')
REPLAY = {
    'databases': 'Main and wiki write transactions held together; SQLite backup API snapshots.',
    'wiki_ledger': 'Rebuilt only from verified committed wiki ingest_event rows; raw files may be ahead.',
    'wiki_raw': 'Rescan preserved raw notes through idempotent ingestion after restore; do not promote knowledge.',
    'sources': 'Preserve committed cursors. Revalidate source identity, generation and prefix before advancing; missing external sources stay unavailable.',
    'paths': 'Original absolute source locators are retained as provenance. Rebase only approved project-local sources explicitly at the destination.',
    'outbox': 'Replay receipts/spool at least once with canonical deduplication; no profile run resumes automatically.',
}


def _read_db(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _watermark(conn):
    row = conn.execute('SELECT id, hash FROM events ORDER BY id DESC LIMIT 1').fetchone()
    return {'event_id': row['id'] if row else 0, 'event_hash': row['hash'] if row else db.GENESIS}


def _resume(conn):
    result = {}
    for name in sorted(_tables(conn)):
        if any(term in name.lower() for term in RESUME):
            quoted = '"' + name.replace('"', '""') + '"'
            rows = [dict(r) for r in conn.execute('SELECT * FROM ' + quoted)]
            encoded = json.dumps(sorted(rows, key=lambda r: json.dumps(r, sort_keys=True)), sort_keys=True, default=str).encode()
            result[name] = {'rows': len(rows), 'sha256': hashlib.sha256(encoded).hexdigest()}
    return result


def _credential_path(rel):
    p = Path(rel)
    lower = p.name.lower()
    return (bool(set(p.parts) & {'secrets', 'credentials', 'config'})
            or lower.endswith(('.secret', '.pem', '.key', '.env')) or lower.startswith('.env.')
            or lower in {'.env', 'auth.json', 'settings.local.json', 'files-auth', 'terminal-url', 'credentials.json'})


def _roots(root):
    """ROOTS plus the profile's backup directories, project-relative."""
    from alpaca import profile
    extra = tuple(r.strip('/') for r in profile.listed(profile.load(str(root)).paths(), 'backup'))
    return ROOTS + tuple(r for r in extra if r and r not in ROOTS)


def _tool_roots(root=None):
    """Tool installation directories, never backed up: `tools` plus the profile's."""
    if root is None:
        return (('tools',),)
    from alpaca import profile
    extra = profile.listed(profile.load(str(root)).paths(), 'tools')
    return (('tools',),) + tuple(Path(str(t).strip('/')).parts for t in extra if str(t).strip('/'))


def _allowed(rel, tools=(('tools',),)):
    p = Path(rel)
    lower = p.name.lower()
    tool_installation = any(t and p.parts[:len(t)] == t for t in tools)
    return (not p.is_absolute() and '..' not in p.parts and not tool_installation and not (set(p.parts) & EXCLUDED)
            and not lower.endswith(('.lock', '-wal', '-shm', '.sock', '.secret', '.pem', '.key'))
            and not lower.endswith('.env') and not lower.startswith('.env.')
            and lower not in {'.env', 'auth.json', 'settings.local.json', 'files-auth', 'terminal-url', 'credentials.json'})


def _stable_copy(source, target, expected_sha256=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    artifacts._publish(source, target, expected_sha256 or artifacts.digest(source))
    after = source.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('source changed during backup: ' + str(source))


def _copy_many(jobs, *, writable=False):
    """At most eight in-flight durable copies; join every started task on failure."""
    def copy_one(job):
        source, target, expected = job
        _stable_copy(source, target, expected)
        if writable:
            os.chmod(target, 0o600)

    iterator = iter(jobs)
    with ThreadPoolExecutor(max_workers=8) as executor:
        pending = set()
        for _ in range(8):
            job = next(iterator, None)
            if job is not None:
                pending.add(executor.submit(copy_one, job))
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            failures = []
            for future in finished:
                try:
                    future.result()
                except Exception as exc:
                    failures.append(exc)
            if failures:
                # No additional work is submitted. Existing workers must finish before
                # a failed package can be reported, leaving no background mutations.
                for future in pending:
                    try:
                        future.result()
                    except Exception as exc:
                        failures.append(exc)
                raise failures[0]
            for _ in finished:
                job = next(iterator, None)
                if job is not None:
                    pending.add(executor.submit(copy_one, job))


def _proofs(conn, root):
    index = proof.seal_index(conn)
    return {ident: proof.verify_detail(conn, str(root), ident, index) for ident in sorted(index)}


def _wiki_rows(conn):
    if 'ingest_event' not in _tables(conn):
        return []
    rows = [dict(r) for r in conn.execute('SELECT seq,prev_checksum,checksum,ts,doc_id,op,payload FROM ingest_event ORDER BY seq')]
    for row in rows:
        row['payload'] = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']
    from alpaca.wiki.store.ledger import verify_chain
    ok, reason = verify_chain(rows)
    if not ok:
        raise ValueError('wiki chain verification failed: ' + reason)
    return rows


def create(root, *, destination=None, approved_sources=()):
    try:
        return _create(root, destination=destination, approved_sources=approved_sources)
    except BaseException as exc:
        result = {'ok': False, 'issues': [type(exc).__name__ + ': ' + str(exc)],
                  'watermark': None, 'resume': None, 'proofs': None,
                  'completeness': 'unknown', 'known_gaps': []}
        try:
            _record_verification(root, None, result)
        except OSError as record_error:
            exc.add_note('Backup failure receipt could not be persisted: ' + str(record_error))
        raise


def _create(root, *, destination=None, approved_sources=()):
    root = Path(root).resolve()
    home = root / '.alpaca/backups'
    destination = Path(destination).absolute() if destination else home / (time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:12])
    destination = artifacts.contained(root, destination)
    if not destination.is_relative_to(home):
        raise ValueError('backup destination must be under project .alpaca/backups')
    if destination.exists():
        raise ValueError('backup destination already exists')
    home.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.pending-', dir=home))
    data = staging / 'data'; data.mkdir()
    sources = set(); excluded_evidence = set(); issues = []
    main = root / '.alpaca/alpaca.db'
    if not main.is_file():
        raise ValueError('project database is missing')
    try:
        with ExitStack() as stack:
            # Freeze both DB writers. Separate read handles avoid backing up a connection
            # that itself holds a write transaction (SQLite would wait on that transaction).
            databases = [main]
            wiki = root / '.alpaca/wiki/rune.db'
            if wiki.is_file():
                databases.append(wiki)
            guards = []
            for source in databases:
                artifacts.contained(root, source)
                guard = sqlite3.connect(str(source), timeout=10, isolation_level=None)
                stack.callback(guard.close)
                guard.execute('BEGIN IMMEDIATE')
                guards.append(guard)
            for source in databases:
                rel = source.relative_to(root)
                out = data / rel; out.parent.mkdir(parents=True, exist_ok=True)
                reader = _read_db(source)
                writer = sqlite3.connect(out)
                try:
                    reader.backup(writer)
                    writer.execute('PRAGMA journal_mode=DELETE')
                finally:
                    writer.close(); reader.close()
                with out.open('rb') as stream:
                    os.fsync(stream.fileno())
            # The shared boundary is now sealed in both standalone SQLite snapshots.
            # Raw files are independently stable copies with explicit replay semantics.
            for guard in guards:
                guard.rollback()
            snap = _read_db(data / '.alpaca/alpaca.db')
            stack.callback(snap.close)
            ok, reason = db.verify_chain(snap)
            if not ok:
                raise ValueError('event chain verification failed: ' + reason)
            watermark, resume = _watermark(snap), _resume(snap)
            baseline_proofs = _proofs(snap, root)
            tools = _tool_roots(root)
            for base in _roots(root):
                path = root / base
                if path.is_dir() and not path.is_symlink():
                    sources.update(p for p in path.rglob('*') if p.is_file())
            sources.update(root / name for name in FILES if (root / name).is_file())
            # Sealed local attachments are authorized evidence, but existing kept bytes take
            # priority and external/secret/tool locations remain outside this package.
            for event in proof.seal_index(snap).values():
                payload = event['data']
                if payload.get('path'):
                    sources.add(root / payload['path'])
                for item in payload.get('evidence', []):
                    original = str(item.get('pointer', '')).split(':', 1)[-1]
                    if item.get('kind') == 'local' and _credential_path(original):
                        if item.get('kept'):
                            excluded_evidence.add(root / item['kept'])
                        issues.append({'proof': event.get('ref'), 'reason': 'restricted host attachment excluded'})
                        continue
                    if item.get('kept'):
                        sources.add(root / item['kept'])
                    elif item.get('kind') == 'local':
                        body = str(item.get('pointer', '')).split(':', 1)[-1]
                        sources.add(root / body)
            for source in approved_sources:
                path = Path(source)
                sources.add(path if path.is_absolute() else root / path)
            copies = []
            for source in sorted(sources):
                if source in excluded_evidence:
                    continue
                try:
                    source = artifacts.contained(root, source)
                    rel = source.relative_to(root)
                    if not _allowed(rel, tools):
                        continue
                    if source in databases or str(rel) == '.alpaca/wiki/ledger/events.jsonl':
                        continue
                    if not source.is_file():
                        issues.append({'source': str(rel), 'reason': 'missing approved source'})
                        continue
                    copies.append((source, data / rel, None))
                except (OSError, ValueError) as exc:
                    # Exclusions are explicit. Actual change/copy failure must abort snapshot.
                    if source.is_symlink() or not source.resolve().is_relative_to(root):
                        issues.append({'source': str(source), 'reason': 'external or symlink source excluded'})
                    else:
                        raise ValueError('backup source failure: ' + str(exc)) from exc
            try:
                _copy_many(copies)
            except (OSError, ValueError) as exc:
                raise ValueError('backup source failure: ' + str(exc)) from exc
            wiki_head = None
            if wiki.is_file():
                wc = _read_db(data / '.alpaca/wiki/rune.db')
                try:
                    rows = _wiki_rows(wc)
                finally:
                    wc.close()
                wiki_head = {'seq': rows[-1]['seq'] if rows else 0,
                             'checksum': rows[-1]['checksum'] if rows else None}
                ledger = data / '.alpaca/wiki/ledger/events.jsonl'; ledger.parent.mkdir(parents=True, exist_ok=True)
                with ledger.open('wb') as stream:
                    for row in rows:
                        stream.write((json.dumps(row, sort_keys=True) + '\n').encode())
                    stream.flush(); os.fsync(stream.fileno())
            recovered_proofs = _proofs(snap, data)
            for ident, result in recovered_proofs.items():
                if not result['ok']:
                    issues.append({'proof': ident, 'reason': 'unresolved sealed evidence', 'problems': result['problems']})
            objects = [{'path': str(p.relative_to(data)), 'sha256': artifacts.digest(p), 'bytes': p.stat().st_size}
                       for p in sorted(data.rglob('*')) if p.is_file()]
            manifest = {'schema_version': 1, 'id': destination.name, 'created_at': time.time(),
                        'source_root': str(root), 'path': str(destination), 'consistency': 'coordinated-db-replay-files',
                        'watermark': watermark, 'wiki_watermark': wiki_head, 'resume': resume, 'replay': REPLAY,
                        'objects': objects, 'proofs': recovered_proofs, 'source_proofs': baseline_proofs,
                        'issues': issues, 'completeness': 'partial' if issues else 'available-sources'}
            artifacts.atomic_json(staging / 'manifest.json', manifest)
            (staging / 'manifest.sha256').write_text(artifacts.digest(staging / 'manifest.json') + '\n')
            with (staging / 'manifest.sha256').open('rb') as stream:
                os.fsync(stream.fileno())
            result = verify(staging)
            if not result['ok']:
                raise ValueError('backup verification failed: ' + '; '.join(result['issues']))
            artifacts._sync_dir(staging)
            os.replace(staging, destination)
            artifacts._sync_dir(home)
        _record_verification(root, destination, result)
        return manifest
    except BaseException:
        # Leave failed staging evidence for diagnosis; it has no published backup identity.
        raise


def verify(path):
    path = Path(path).resolve(); errors = []; manifest = {}
    try:
        manifest = json.loads((path / 'manifest.json').read_text())
        if manifest.get('schema_version') != 1:
            raise ValueError('unsupported backup manifest version')
        if artifacts.digest(path / 'manifest.json') != (path / 'manifest.sha256').read_text().strip():
            raise ValueError('manifest hash mismatch')
        data = path / 'data'
        names = set()
        for entry in manifest['objects']:
            rel = entry['path']
            if rel in names or not _allowed(rel):
                raise ValueError('unsafe or duplicate backup object: ' + rel)
            names.add(rel)
            target = artifacts.contained(data, data / rel)
            if not target.is_file() or target.stat().st_size != entry['bytes'] or artifacts.digest(target) != entry['sha256']:
                errors.append('object hash/size mismatch: ' + rel)
        actual = {str(p.relative_to(data)) for p in data.rglob('*') if p.is_file()}
        if actual != names:
            errors.append('object inventory differs from manifest')
        if not errors:
            conn = _read_db(data / '.alpaca/alpaca.db')
            try:
                if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    errors.append('SQLite integrity check failed')
                ok, reason = db.verify_chain(conn)
                if not ok:
                    errors.append(reason)
                if _watermark(conn) != manifest['watermark']:
                    errors.append('event watermark mismatch')
                if _resume(conn) != manifest['resume']:
                    errors.append('source/cursor resume watermark mismatch')
                proofs = _proofs(conn, data)
                if proofs != manifest['proofs']:
                    errors.append('proof reference verification differs from recorded availability')
            finally:
                conn.close()
            wiki = data / '.alpaca/wiki/rune.db'
            if wiki.exists():
                conn = _read_db(wiki)
                try:
                    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        errors.append('wiki SQLite integrity check failed')
                    rows = _wiki_rows(conn)
                finally:
                    conn.close()
                ledger = [json.loads(line) for line in (data / '.alpaca/wiki/ledger/events.jsonl').read_text().splitlines() if line]
                if ledger != rows:
                    errors.append('wiki ledger differs from committed DB chain')
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        errors.append(str(exc))
    return {'ok': not errors, 'issues': errors, 'watermark': manifest.get('watermark'),
            'resume': manifest.get('resume'), 'proofs': manifest.get('proofs'),
            'completeness': manifest.get('completeness'), 'known_gaps': manifest.get('issues', [])}


def restore(path, destination):
    path = Path(path).resolve(); destination = Path(destination).absolute()
    if destination.is_symlink() or destination.resolve() != destination:
        raise ValueError('restore destination must not contain symlinks')
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('restore destination must be empty')
    check = verify(path)
    if not check['ok']:
        raise ValueError('backup verification failed: ' + '; '.join(check['issues']))
    manifest = json.loads((path / 'manifest.json').read_text())
    original = Path(manifest['source_root'])
    if destination == original or destination.is_relative_to(original) or original.is_relative_to(destination):
        raise ValueError('restore destination must be isolated from source project')
    if destination.is_relative_to(path) or path.is_relative_to(destination):
        raise ValueError('restore destination must be isolated from backup')
    destination.mkdir(parents=True, exist_ok=True)
    copies = []
    for entry in manifest['objects']:
        source = path / 'data' / entry['path']
        target = destination / entry['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        artifacts.contained(destination, target)
        copies.append((source, target, entry['sha256']))
    _copy_many(copies, writable=True)
    conn = _read_db(destination / '.alpaca/alpaca.db')
    try:
        if _watermark(conn) != manifest['watermark'] or _resume(conn) != manifest['resume'] or not db.verify_chain(conn)[0]:
            raise ValueError('restored database verification failed')
        actual_proofs = _proofs(conn, destination)
        if actual_proofs != manifest['proofs']:
            raise ValueError('restored proof reference verification failed')
    finally:
        conn.close()
    rebindings = []
    conn = sqlite3.connect(destination / '.alpaca/alpaca.db')
    conn.row_factory = sqlite3.Row
    try:
        for table, key, column in (('obs_source', 'source_id', 'locator'),
                                   ('sessions', 'sid', 'transcript')):
            if table not in _tables(conn):
                continue
            selection = f'{key}, {column}, capabilities' if table == 'obs_source' else f'{key}, {column}'
            for row in conn.execute(f'SELECT {selection} FROM {table}').fetchall():
                old = row[column]
                if not old and table != 'obs_source':
                    continue
                source = Path(old) if old else None
                if source is not None and not source.is_absolute():
                    source = original / source
                # Do not follow the old host's links or authorize an external provider path.
                if source is not None and '..' not in source.parts and source.is_relative_to(original):
                    new = str(destination / source.relative_to(original))
                    state = 'rebased-project-local'
                else:
                    new, state = None, 'disabled-external-requires-rebinding'
                    if source is None:
                        state = 'disabled-unknown-requires-rebinding'
                    if table == 'obs_source':
                        capabilities = json.loads(row['capabilities'])
                        capabilities['restore_disabled'] = True
                        conn.execute('UPDATE obs_source SET capabilities=? WHERE source_id=?',
                                     (json.dumps(capabilities), row[key]))
                conn.execute(f'UPDATE {table} SET {column}=? WHERE {key}=?', (new, row[key]))
                rebindings.append({'table': table, 'id': row[key], 'original': old,
                                   'locator': new, 'status': state})
        conn.commit()
    finally:
        conn.close()
    result = dict(check, destination=str(destination), replay=manifest['replay'], restored_at=time.time(),
                  source_rebindings=rebindings)
    artifacts.atomic_json(destination / '.alpaca/restore.json', result)
    return result


def _record_verification(root, path, result):
    root = Path(root).resolve()
    ident = uuid.uuid4().hex
    rel = '.alpaca/backups/verifications/' + ident + '.json'
    receipt = dict(result, latest=str(path) if path is not None else None, verified_at=time.time(),
                   status='ok' if result['ok'] else 'error', verification_receipt=rel)
    artifacts.atomic_json(root / rel, receipt)
    artifacts.atomic_json(root / '.alpaca/backups/latest-verification.json', receipt)
    if result['ok']:
        artifacts.atomic_json(root / '.alpaca/backups/last-successful-verification.json', receipt)
    return receipt


def status(root):
    """Last recorded full verification. No file-object reads on health request paths."""
    try:
        return json.loads((Path(root) / '.alpaca/backups/latest-verification.json').read_text())
    except (OSError, ValueError):
        return {'status': 'unavailable', 'latest': None, 'reason': 'no recorded backup verification'}


from alpaca import cli


@cli.command('backup')
def cmd_backup(args):
    try:
        if args.backup_verb == 'create':
            result = create(cli._root(), approved_sources=args.source)
        elif args.backup_verb == 'verify':
            result = verify(args.path)
            path = Path(args.path).resolve()
            root = Path(cli._root()).resolve()
            if path.is_relative_to(root / '.alpaca/backups'):
                _record_verification(root, path, result)
        elif args.backup_verb == 'restore':
            result = restore(args.path, args.destination)
        elif args.backup_verb == 'status':
            result = status(cli._root())
        else:
            return cli.USAGE
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)})); return cli.FAIL
    print(json.dumps(result, indent=2)); return cli.FAIL if result.get('ok') is False else cli.PASS


def _parser(sub):
    p = sub.add_parser('backup'); commands = p.add_subparsers(dest='backup_verb')
    p = commands.add_parser('create'); p.add_argument('--source', action='append', default=[])
    p = commands.add_parser('verify'); p.add_argument('path')
    p = commands.add_parser('restore'); p.add_argument('path'); p.add_argument('destination')
    commands.add_parser('status')


cli.register_parser('backup', _parser)
