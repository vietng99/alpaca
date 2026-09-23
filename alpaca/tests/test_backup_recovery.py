import json
import sqlite3
from pathlib import Path

import pytest
from alpaca import db


def api():
    from alpaca import backup
    return backup


def project(root):
    root.mkdir()
    conn = db.connect(str(root))
    db.append_event(conn, session='owner', actor='human', kind='intent', data={'text': 'test'})
    conn.execute('CREATE TABLE source_cursors (source_id TEXT PRIMARY KEY, offset INTEGER)')
    conn.execute("INSERT INTO source_cursors VALUES ('source-1', 17)")
    raw = root / '.alpaca/wiki/raw/note.md'; raw.parent.mkdir(parents=True)
    raw.write_text('independently authored note')
    return conn


def test_wal_snapshot_restore_event_chain_and_cursor_resume(tmp_path):
    b = api()
    root = tmp_path / 'project'; conn = project(root)
    assert (root / '.alpaca/alpaca.db-wal').stat().st_size > 0
    snapshot = b.create(root)
    assert snapshot['watermark']['event_id'] == 1
    assert b.verify(snapshot['path'])['ok']
    restored = tmp_path / 'restored'
    result = b.restore(snapshot['path'], restored)
    assert result['ok']
    c = sqlite3.connect(restored / '.alpaca/alpaca.db'); c.row_factory = sqlite3.Row
    assert db.verify_chain(c)[0]
    assert c.execute('SELECT offset FROM source_cursors').fetchone()[0] == 17
    assert (restored / '.alpaca/wiki/raw/note.md').read_text() == 'independently authored note'
    assert not (restored / '.alpaca/backups').exists()
    conn.close(); c.close()


def test_tamper_and_nonempty_restore_refused(tmp_path):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    snap = b.create(root)
    dest = tmp_path / 'dest'; dest.mkdir(); (dest / 'valuable').write_text('keep')
    with pytest.raises(ValueError, match='empty'):
        b.restore(snap['path'], dest)
    payload = Path(snap['path']) / 'data/.alpaca/wiki/raw/note.md'; payload.chmod(0o600); payload.write_text('tampered')
    assert not b.verify(snap['path'])['ok']
    with pytest.raises(ValueError, match='verification'):
        b.restore(snap['path'], tmp_path / 'clean')
    assert (dest / 'valuable').read_text() == 'keep'
    conn.close()


def test_backup_excludes_secrets_tools_worktrees_and_external_rtl(tmp_path):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    for rel in ['.alpaca/config/login.secret', 'tools/tool', '.alpaca/repairs/worktree/file', '.alpaca/backups/old/data/file']:
        p = root / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('excluded')
    restricted = tmp_path / 'restricted.sv'; restricted.write_text('restricted')
    linked = root / '.alpaca/wiki/raw/linked.sv'; linked.symlink_to(restricted)
    snap = b.create(root, approved_sources=[restricted])
    names = [item['path'] for item in snap['objects']]
    assert not any('secret' in p or 'tools/' in p or 'worktree' in p or 'backups/' in p or 'linked.sv' in p for p in names)
    assert snap['issues']
    conn.close()


def test_restore_rebases_local_sources_and_disables_external_registrations(tmp_path):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    local = root / '.alpaca/transcripts/s1.jsonl'; local.parent.mkdir(); local.write_text('{}\n')
    from alpaca import observability
    local_id = observability.expect_source(str(root),'local','codex',locator=str(local))['source_id']
    external_id = observability.expect_source(str(root),'external','codex',locator='/private/operator/session.jsonl')['source_id']
    conn.execute('INSERT INTO sessions(sid,transcript) VALUES (?,?)', ('private', '/private/operator/session.jsonl'))
    snap = b.create(root)
    result = b.restore(snap['path'], tmp_path / 'restored')
    restored = sqlite3.connect(tmp_path / 'restored/.alpaca/alpaca.db')
    assert restored.execute("SELECT locator FROM obs_source WHERE source_id=?",(local_id,)).fetchone()[0] == str(tmp_path / 'restored/.alpaca/transcripts/s1.jsonl')
    assert restored.execute("SELECT locator FROM obs_source WHERE source_id=?",(external_id,)).fetchone()[0] is None
    assert restored.execute("SELECT transcript FROM sessions WHERE sid='private'").fetchone()[0] is None
    assert result['source_rebindings']
    conn.close(); restored.close()


def test_restore_persistently_disables_external_and_unknown_source_discovery(tmp_path):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    from alpaca import observability
    external = observability.expect_source(root, 'external', 'codex',
        locator='/private/operator/native.jsonl', native_id='native-external',
        capabilities={'native_transcript': True})
    unknown = observability.expect_source(root, 'unknown', 'codex', native_id='native-unknown')
    local = root / '.alpaca/transcripts/local.jsonl'; local.parent.mkdir(); local.write_text('{}\n')
    local_source = observability.expect_source(root, 'local', 'codex', locator=local)
    snap = b.create(root)
    dest = tmp_path / 'restored'; b.restore(snap['path'], dest)
    restored = db.connect_readonly(dest)
    rows = {r['source_id']: dict(r) for r in restored.execute('SELECT * FROM obs_source')}
    for source in (external, unknown):
        row = rows[source['source_id']]
        assert row['locator'] is None
        assert json.loads(row['capabilities'])['restore_disabled'] is True
    assert json.loads(rows[external['source_id']]['capabilities'])['native_transcript'] is True
    assert not json.loads(rows[local_source['source_id']]['capabilities']).get('restore_disabled')
    assert rows[local_source['source_id']]['locator'] == str(dest / '.alpaca/transcripts/local.jsonl')
    conn.close(); restored.close()


def test_wiki_snapshot_rebuilds_committed_ledger_and_preserves_independent_note(tmp_path):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    from alpaca.wiki.store.ledger import GENESIS, event_checksum
    wiki = sqlite3.connect(root / '.alpaca/wiki/rune.db')
    wiki.execute('PRAGMA journal_mode=WAL')
    wiki.execute('CREATE TABLE ingest_event(seq INTEGER,prev_checksum TEXT,checksum TEXT,ts TEXT,doc_id TEXT,op TEXT,payload TEXT)')
    checksum = event_checksum(1, GENESIS, 'note', 'raw/note.md', {'body': 'independent'})
    wiki.execute('INSERT INTO ingest_event VALUES(?,?,?,?,?,?,?)', (1, GENESIS, checksum, 'today', 'raw/note.md', 'note', json.dumps({'body': 'independent'})))
    wiki.commit()
    # A lagging JSONL is reconstructed only from committed, verified rows.
    ledger = root / '.alpaca/wiki/ledger/events.jsonl'; ledger.parent.mkdir(); ledger.write_text('')
    snap = b.create(root)
    assert snap['wiki_watermark'] == {'seq': 1, 'checksum': checksum}
    assert b.verify(snap['path'])['ok']
    dest = tmp_path / 'recovered'; b.restore(snap['path'], dest)
    assert json.loads((dest / '.alpaca/wiki/ledger/events.jsonl').read_text())['checksum'] == checksum
    wiki.close(); conn.close()


def test_sealed_proofs_resolve_and_historical_gaps_stay_explicit(tmp_path):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    report = root / '.alpaca/proofs/report.md'; report.parent.mkdir(); report.write_text('sealed engineering report')
    from alpaca.artifacts import digest
    db.append_event(conn, session='owner', actor='human', kind='proof-report', ref='old-task', data={
        'path': '.alpaca/proofs/report.md', 'sha256': digest(report), 'evidence': [
            {'kind': 'local', 'pointer': 'local:gone.log', 'sha256': '0'*64, 'kept': None}]})
    snap = b.create(root)
    assert not snap['proofs']['old-task']['ok']
    assert snap['completeness'] == 'partial'
    result = b.restore(snap['path'], tmp_path / 'restored')
    assert result['ok'] and not result['proofs']['old-task']['ok']
    conn.close()


def test_failed_copy_does_not_publish_backup(tmp_path, monkeypatch):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    def full(*args):
        raise OSError(28, 'injected full disk')
    monkeypatch.setattr(b, '_stable_copy', full)
    with pytest.raises(ValueError, match='source failure'):
        b.create(root)
    assert b.status(root)['status'] == 'error'
    assert b.status(root)['latest'] is None
    conn.close()


def test_restore_refuses_symlink_parent(tmp_path):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    snap = b.create(root)
    outside = tmp_path / 'outside'; outside.mkdir()
    link = tmp_path / 'link'; link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        b.restore(snap['path'], link / 'restore')
    assert list(outside.iterdir()) == []
    conn.close()


def test_status_reads_verification_receipt_without_opening_objects(tmp_path, monkeypatch):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    snap = b.create(root)
    def forbidden(*args):
        raise AssertionError('health must not read backup objects')
    monkeypatch.setattr(b, 'verify', forbidden)
    result = b.status(root)
    assert result['ok']
    assert result['latest'] == snap['path']
    assert result['verified_at'] >= snap['created_at']
    assert (root / result['verification_receipt']).is_file()
    conn.close()


def test_restore_detects_object_change_after_initial_verification(tmp_path, monkeypatch):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    snap = b.create(root)
    original = b.verify
    def raced(path):
        result = original(path)
        target = Path(path) / 'data/.alpaca/wiki/raw/note.md'
        target.chmod(0o600); target.write_text('changed after verified')
        return result
    monkeypatch.setattr(b, 'verify', raced)
    with pytest.raises(ValueError, match='changed'):
        b.restore(snap['path'], tmp_path / 'restored')
    conn.close()


def test_writes_resume_after_database_snapshots_before_file_copy(tmp_path, monkeypatch):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    original = b._stable_copy
    wrote = []
    def copy_with_concurrent_writer(source, target, *args):
        if not wrote:
            peer = sqlite3.connect(root / '.alpaca/alpaca.db', timeout=0.05, isolation_level=None)
            peer.row_factory = sqlite3.Row
            try:
                db.append_event(peer, session='concurrent', actor='human', kind='note', data={'after': 'snapshot'})
                wrote.append(True)
            finally:
                peer.close()
        return original(source, target, *args)
    monkeypatch.setattr(b, '_stable_copy', copy_with_concurrent_writer)
    snap = b.create(root)
    assert wrote
    assert snap['watermark']['event_id'] == 1
    assert conn.execute('SELECT MAX(id) FROM events').fetchone()[0] == 2
    assert b.verify(snap['path'])['ok']
    conn.close()


def test_failed_attempt_replaces_latest_health_but_keeps_previous_success(tmp_path, monkeypatch):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    first = b.create(root)
    def fail(*args):
        raise OSError(28, 'injected full disk')
    monkeypatch.setattr(b, '_stable_copy', fail)
    with pytest.raises(ValueError):
        b.create(root)
    status = b.status(root)
    assert not status['ok'] and status['status'] == 'error'
    assert status['latest'] is None
    previous = json.loads((root / '.alpaca/backups/last-successful-verification.json').read_text())
    assert previous['latest'] == first['path'] and previous['ok']
    conn.close()


def test_pending_tool_and_hook_spools_survive_restore(tmp_path):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    pending = {
        '.alpaca/pool/tools/session.jsonl': b'{"phase":"pre","tool_use_id":"waiting"}\n',
        '.alpaca/pool/hooks/session.jsonl': b'{"type":"hook_outcome","id":"pending"}\n',
    }
    for name, content in pending.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
    installation = root / 'tools/bin/tool'; installation.parent.mkdir(parents=True); installation.write_bytes(b'installed')
    snap = b.create(root)
    dest = tmp_path / 'restored'; b.restore(snap['path'], dest)
    for name, content in pending.items():
        assert (dest / name).read_bytes() == content
    assert not (dest / 'tools').exists()
    conn.close()


def test_explicit_approval_does_not_copy_known_host_credentials(tmp_path):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    credentials = []
    for rel in ('.alpaca/files-auth', '.alpaca/service.env'):
        secret = root / rel; secret.write_text('private credential material'); credentials.append(secret)
    snap = b.create(root, approved_sources=credentials)
    names = {row['path'] for row in snap['objects']}
    assert '.alpaca/files-auth' not in names
    assert '.alpaca/service.env' not in names
    conn.close()


def test_sealed_credential_attachment_is_excluded_even_with_opaque_kept_name(tmp_path):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    from alpaca.artifacts import digest
    secret = root / '.alpaca/files-auth'; secret.write_text('private credential')
    kept = root / '.alpaca/proofs/attachments/opaque'; kept.parent.mkdir(parents=True); kept.write_bytes(secret.read_bytes())
    report = root / '.alpaca/proofs/credential-report.md'; report.write_text('report references a host credential')
    db.append_event(conn, session='owner', actor='human', kind='proof-report', ref='credential-task', data={
        'path': '.alpaca/proofs/credential-report.md', 'sha256': digest(report), 'evidence': [{
            'kind': 'local', 'pointer': 'local:.alpaca/files-auth', 'sha256': digest(secret),
            'kept': '.alpaca/proofs/attachments/opaque'}]})
    snap = b.create(root)
    names = {row['path'] for row in snap['objects']}
    assert '.alpaca/files-auth' not in names
    assert '.alpaca/proofs/attachments/opaque' not in names
    assert not snap['proofs']['credential-task']['ok']
    conn.close()


def test_kept_approved_repair_evidence_is_preserved_without_copying_worktree(tmp_path):
    b = api(); root = tmp_path / 'project'; conn = project(root)
    from alpaca.artifacts import digest
    original = root / '.alpaca/repairs/worktree/test.log'; original.parent.mkdir(parents=True); original.write_text('approved test evidence')
    kept = root / '.alpaca/proofs/attachments/kept-log'; kept.parent.mkdir(parents=True); kept.write_bytes(original.read_bytes())
    report = root / '.alpaca/proofs/report.md'; report.write_text('sealed report')
    db.append_event(conn, session='owner', actor='human', kind='proof-report', ref='repaired-task', data={
        'path': '.alpaca/proofs/report.md', 'sha256': digest(report), 'evidence': [{
            'kind': 'local', 'pointer': 'local:.alpaca/repairs/worktree/test.log', 'sha256': digest(original),
            'kept': '.alpaca/proofs/attachments/kept-log'}]})
    snap = b.create(root)
    assert snap['proofs']['repaired-task']['ok']
    names = {row['path'] for row in snap['objects']}
    assert '.alpaca/proofs/attachments/kept-log' in names
    assert '.alpaca/repairs/worktree/test.log' not in names
    conn.close()


def test_parallel_copy_failure_waits_for_started_copies_and_never_publishes(tmp_path, monkeypatch):
    import threading
    import time
    b = api(); root = tmp_path / 'project'; conn = project(root)
    (root / '.alpaca/wiki/raw/fail.md').write_text('fault fixture')
    original = b._stable_copy
    started = threading.Event(); completed = threading.Event()
    def mixed_copy(source, target, *args):
        if source.name == 'fail.md':
            if not started.wait(1):
                raise OSError('second copy never started concurrently')
            raise OSError('injected parallel copy failure')
        if source.name == 'note.md':
            started.set(); time.sleep(0.03)
            result = original(source, target, *args)
            completed.set()
            return result
        return original(source, target, *args)
    monkeypatch.setattr(b, '_stable_copy', mixed_copy)
    with pytest.raises(ValueError, match='injected parallel copy failure'):
        b.create(root)
    assert completed.is_set()
    assert b.status(root)['status'] == 'error'
    assert not [p for p in (root / '.alpaca/backups').glob('*/manifest.json') if not p.parent.name.startswith('.')]
    conn.close()


def test_a_profile_adds_backup_roots_and_tool_dirs(tmp_path, monkeypatch):
    """The domain profile (alpaca/profile.py `paths`) names its runtime directories, which a backup
    copies, and its tool installations, which a backup never copies even when approved."""
    from alpaca import profile

    class Domain(profile.Profile):
        def paths(self):
            return {'backup': ['.alpaca/domain/receipts'], 'tools': ['domain/tools']}
    b = api(); root = tmp_path / 'project'; conn = project(root)
    receipt = root / '.alpaca/domain/receipts/r1.json'; receipt.parent.mkdir(parents=True)
    receipt.write_text('{"verdict": "PASS"}')
    tool = root / 'domain/tools/bin/tool'; tool.parent.mkdir(parents=True); tool.write_bytes(b'installed')
    names = [item['path'] for item in b.create(root, approved_sources=[tool])['objects']]
    assert '.alpaca/domain/receipts/r1.json' not in names, 'no profile, no domain root'
    assert 'domain/tools/bin/tool' in names, 'without a profile the directory is ordinary'
    monkeypatch.setattr(profile, 'load', lambda r: Domain())
    names = [item['path'] for item in b.create(root, approved_sources=[tool])['objects']]
    assert '.alpaca/domain/receipts/r1.json' in names
    assert not any(p.startswith('domain/tools/') for p in names)
    conn.close()
