"""Recovery replays the source record and attests only verified coverage."""
import json
from pathlib import Path

import pytest

from alpaca import cli, db
from alpaca.wiki.config import Config
from alpaca.wiki.ingest import drain
from alpaca.wiki.store.db import open_db_readonly


def seed(root):
    conn = db.connect(root)
    for sid in ("old-session", "new-session"):
        db.append_event(conn, session=sid, actor="test", kind="result", data={"body": sid})
    return conn


def test_cli_recovers_all_sessions_and_replay_is_idempotent(project, capsys):
    conn = seed(project)
    assert cli.main(["--session", "repair", "wiki", "recover"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["through_event"] == 2
    assert report["verified_events"] == 2
    assert report["ingested"] == 2
    assert db.events(conn, kind="wiki-recovered")[-1]["data"]["through_event"] == 2
    # Recovery audit events themselves are captured by the next pass.
    assert cli.main(["--session", "repair", "wiki", "recover"]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["verified_events"] == 3
    assert replay["skipped"] == 2
    assert replay["ingested"] == 1
    wiki = open_db_readonly(Config.for_vault(drain.wiki_vault_dir(project)))
    try:
        assert wiki.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 3
        assert wiki.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
        assert wiki.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        wiki.close()
        conn.close()


def test_partial_recovery_keeps_data_and_does_not_attest_success(project, monkeypatch):
    from alpaca.wiki import recovery
    conn = seed(project)
    real = drain.run
    def interrupt(root, sid, **kw):
        if sid == "new-session":
            raise OSError("interrupted recovery")
        return real(root, sid, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(drain, "run", interrupt)
        with pytest.raises(OSError, match="interrupted recovery"):
            recovery.run(project, session="repair")
    assert db.events(conn, kind="wiki-recovered") == []
    assert Path(project, ".alpaca/wiki/raw/unassigned/000001-result.md").is_file()
    result = recovery.run(project, session="repair")
    assert result["verified_events"] == 2
    assert result["skipped"] == 1
    conn.close()


def test_recovery_refuses_to_attest_missing_capture(project, monkeypatch):
    from alpaca.wiki import recovery
    conn = seed(project)
    real = drain.run
    def miss(root, sid, **kw):
        result = real(root, sid, **kw)
        Path(root, ".alpaca/wiki/raw/unassigned/000001-result.md").unlink(missing_ok=True)
        return result
    monkeypatch.setattr(drain, "run", miss)
    with pytest.raises(RuntimeError, match="verification"):
        recovery.run(project, session="repair")
    assert db.events(conn, kind="wiki-recovered") == []
    conn.close()


def test_events_arriving_during_replay_stay_outside_attested_cutoff(project, monkeypatch):
    from alpaca.wiki import recovery
    conn = seed(project)
    real = drain.run
    added = []
    def append_later(root, sid, **kw):
        if not added:
            added.append(db.append_event(conn, session="later", actor="test", kind="result"))
        return real(root, sid, **kw)
    monkeypatch.setattr(drain, "run", append_later)
    result = recovery.run(project, session="repair")
    assert result["through_event"] == 2
    assert result["verified_events"] == 2
    assert added[0]["id"] > result["through_event"]
    conn.close()


def test_cli_failure_is_recorded_and_cannot_clear_prior_error(project, monkeypatch):
    from alpaca.wiki import recovery
    conn = seed(project)
    def fail(*args, **kw):
        raise OSError('disk full during recovery')
    monkeypatch.setattr(recovery, 'run', fail)
    assert cli.main(['--session', 'repair', 'wiki', 'recover']) == 1
    assert db.events(conn, kind='wiki-recovered') == []
    failure = db.events(conn, kind='drain-failed')[-1]
    assert failure['session'] == 'repair'
    assert failure['data']['where'] == 'wiki-recover'
    assert 'disk full' in failure['data']['error']
    conn.close()


@pytest.mark.parametrize('damage', ['doc', 'block'])
def test_recovery_checks_searchable_content_not_just_raw_files(project, monkeypatch, damage):
    from alpaca.wiki import recovery
    conn = seed(project)
    real = drain.run
    def corrupt(root, sid, **kw):
        result = real(root, sid, **kw)
        wiki = kw['absorber'].db.conn
        if damage == 'doc':
            wiki.execute("UPDATE docs SET content_sha256='bad' WHERE doc_id=?", (result['docs'][0],))
        else:
            wiki.execute("UPDATE blocks SET text='damaged' WHERE doc_id=?", (result['docs'][0],))
        wiki.commit()
        return result
    monkeypatch.setattr(drain, 'run', corrupt)
    with pytest.raises(RuntimeError, match='verification'):
        recovery.run(project, session='repair')
    assert db.events(conn, kind='wiki-recovered') == []
    conn.close()
