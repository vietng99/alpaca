"""Scoped job reads stay selective without hiding malformed record payloads."""
import sqlite3

import pytest

from alpaca import db, migrate
from alpaca.research.jobs import events_of


def append(conn, kind, data):
    return db.append_event(conn, session='test', actor='test', kind=kind, data=data)['id']


def test_scope_read_uses_an_index_and_keeps_every_filter(tmp_path):
    conn = db.connect(str(tmp_path))
    append(conn, 'note', {'scope': 'other', 'job': 'j'})
    first = append(conn, 'note', {'scope': 'scope', 'job': 'j'})
    append(conn, 'note', {'unrelated': True})
    second = append(conn, 'research-job', {'scope': 'scope', 'job': 'other'})
    third = append(conn, 'research-job', {'scope': 'scope', 'job': 'j'})
    trace = []
    conn.set_trace_callback(trace.append)
    assert [r['id'] for r in events_of(conn, 'scope')] == [first, second, third]
    conn.set_trace_callback(None)
    query = next(q for q in trace if q.startswith('SELECT * FROM events WHERE'))
    plan = ' '.join(str(r[3]) for r in conn.execute('EXPLAIN QUERY PLAN '+query))
    assert 'idx_events_scope_id' in plan, plan
    assert 'idx_events_invalid_json_id' in plan, plan
    assert [r['id'] for r in events_of(conn, 'scope', ['note'])] == [first]
    assert [r['id'] for r in events_of(conn, 'scope', job='j', after=first)] == [third]
    assert events_of(conn, 'unknown') == []
    conn.close()


def test_index_repair_keeps_corruption_detectable_and_raw_store_mutable(tmp_path):
    conn = db.connect(str(tmp_path))
    good = append(conn, 'note', {'scope': 'scope'})
    bad = append(conn, 'other', {'scope': 'scope'})
    conn.execute("UPDATE events SET data='not-json' WHERE id=?", (bad,))
    for name in ['idx_events_scope_id', 'idx_events_invalid_json_id']:
        conn.execute('DROP INDEX IF EXISTS '+name)
    original = [tuple(r) for r in conn.execute('SELECT * FROM events ORDER BY id')]
    assert migrate.apply(conn) == migrate.CURRENT_SCHEMA_VERSION
    assert [tuple(r) for r in conn.execute('SELECT * FROM events ORDER BY id')] == original
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {'idx_events_scope_id', 'idx_events_invalid_json_id'} <= names
    with pytest.raises(sqlite3.OperationalError, match='malformed JSON'):
        events_of(conn, 'scope')
    # Corruption outside the selected kind or ID window stays outside the read.
    assert [r['id'] for r in events_of(conn, 'scope', ['note'])] == [good]
    assert events_of(conn, 'scope', after=bad) == []
    before = conn.total_changes
    migrate.apply(conn)
    assert conn.total_changes == before
    conn.close()


def test_obs_source_parent_index_is_in_new_records_and_added_to_existing_ones(tmp_path):
    """t-577: source_revision looks sources up by parent_session on every analytics read."""
    conn = db.connect(str(tmp_path))
    plan = lambda: ' '.join(str(r[3]) for r in conn.execute(
        'EXPLAIN QUERY PLAN SELECT * FROM obs_source WHERE session=? OR parent_session=? ORDER BY source_id', ('s', 's')))
    assert 'idx_obs_source_parent' in plan(), plan()
    conn.execute('DROP INDEX idx_obs_source_parent')
    names = lambda: {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert 'idx_obs_source_parent' not in names()
    conn.close()
    # An existing record at the current version gets it on its next open for write.
    conn = db.connect(str(tmp_path))
    assert 'idx_obs_source_parent' in names()
    before = conn.total_changes
    migrate.apply(conn)
    assert conn.total_changes == before
    conn.close()
