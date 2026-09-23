import sqlite3

import pytest

from alpaca import db, migrate


def test_readonly_absent_does_not_create_project_state(tmp_path):
    with db.connect_readonly(str(tmp_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO meta VALUES ('oops','write')")
    assert not (tmp_path / '.alpaca').exists()


def test_readonly_sees_committed_wal_and_never_migrates(tmp_path, monkeypatch):
    writer = db.connect(str(tmp_path))
    event = db.append_event(writer, session='s', actor='a', kind='test')
    monkeypatch.setattr(migrate, 'apply', lambda *_: pytest.fail('read migrated'))
    reader = db.connect_readonly(str(tmp_path))
    assert db.last_event(reader)['hash'] == event['hash']
    assert reader.execute('PRAGMA query_only').fetchone()[0] == 1
    reader.close()
    writer.close()


def test_current_migration_is_read_only_and_managed_events_cannot_change(tmp_path):
    (tmp_path / 'ALPACA-MANIFEST').write_text('managed')
    conn = db.connect(str(tmp_path))
    db.append_event(conn, session='s', actor='a', kind='test')
    writes = []
    conn.set_trace_callback(writes.append)
    migrate.apply(conn)
    assert not any(s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER')) for s in writes)
    for sql in ("UPDATE events SET actor='changed'", 'DELETE FROM events'):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            conn.execute(sql)
    assert 'idx_events_session_id' in {r[1] for r in conn.execute('PRAGMA index_list(events)')}
    assert db.verify_chain(conn)[0]
    conn.close()


def test_migration_failure_rolls_back_schema_and_version(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path))
    conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    conn.execute('DROP TABLE obs_source')
    def fail(_):
        raise RuntimeError('injected backfill failure')
    monkeypatch.setattr(migrate, '_backfill_superseded_by', fail)
    with pytest.raises(RuntimeError):
        migrate.apply(conn)
    assert migrate.current_version(conn) == 2
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='obs_source'").fetchone()
    conn.close()
