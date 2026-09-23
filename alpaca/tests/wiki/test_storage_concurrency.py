"""WAL readers must not lose locks when containment descriptors are closed."""
import sqlite3
import subprocess
import sys
import time

import pytest

from alpaca.wiki.config import Config
from alpaca.wiki.store.db import DB, open_db_readonly


def _peer_can_lock(path):
    probe = """import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
finally:
    os.close(fd)
"""
    return subprocess.run([sys.executable, "-c", probe, str(path)], check=False).returncode == 0


def test_private_mode_checks_preserve_sqlite_wal_locks(tmp_path):
    database = DB(Config.for_vault(tmp_path))
    try:
        database.pour()
        assert not _peer_can_lock(tmp_path / "rune.db")
        assert not _peer_can_lock(tmp_path / "rune.db-shm")
    finally:
        database.close()


def test_opening_secure_reader_preserves_existing_connection_locks(tmp_path):
    cfg = Config.for_vault(tmp_path)
    database = DB(cfg)
    try:
        database.pour()
        reader = open_db_readonly(cfg)
        reader.execute("SELECT COUNT(*) FROM meta").fetchone()
        reader.close()
        assert not _peer_can_lock(tmp_path / "rune.db")
        assert not _peer_can_lock(tmp_path / "rune.db-shm")
    finally:
        database.close()


def test_writer_survives_concurrent_readonly_connections(tmp_path):
    database = DB(Config.for_vault(tmp_path))
    database.pour()
    database.close()
    writer = """import sys, time
from alpaca.wiki.config import Config
from alpaca.wiki.store.db import DB
database = DB(Config.for_vault(sys.argv[1]))
database.pour()
print('ready', flush=True)
sys.stdin.readline()
for n in range(300):
    database.conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', ('probe', str(n)))
    database.conn.commit()
    if n % 10 == 0:
        time.sleep(0.001)
database.close()
"""
    proc = subprocess.Popen([sys.executable, "-X", "faulthandler", "-c", writer, str(tmp_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        proc.stdin.write("\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 10
        readers = 0
        while proc.poll() is None and time.monotonic() < deadline:
            reader = sqlite3.connect((tmp_path / "rune.db").as_uri() + "?mode=ro", uri=True)
            try:
                reader.execute("SELECT COUNT(*) FROM meta").fetchone()
            finally:
                reader.close()
            readers += 1
        _out, err = proc.communicate(timeout=5)
        assert proc.returncode == 0, err
        assert readers > 0
        verification = open_db_readonly(Config.for_vault(tmp_path))
        try:
            assert verification.execute("SELECT value FROM meta WHERE key='probe'").fetchone()[0] == "299"
            assert verification.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            verification.close()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_secure_open_refuses_symlinked_database_without_touching_target(tmp_path):
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"untouched")
    outside.chmod(0o644)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rune.db").symlink_to(outside)
    with pytest.raises(ValueError, match="symlinked"):
        DB(Config.for_vault(vault))
    assert outside.read_bytes() == b"untouched"
    assert outside.stat().st_mode & 0o777 == 0o644


def test_private_mode_checks_refuse_symlinked_sidecar(tmp_path):
    cfg = Config.for_vault(tmp_path)
    database = DB(cfg)
    target = tmp_path / "outside"
    target.write_text("untouched")
    target.chmod(0o644)
    sidecar = tmp_path / "rune.db-wal"
    try:
        # No WAL transaction has started, so installing this adversarial path
        # does not replace a live SQLite-owned sidecar.
        assert not sidecar.exists()
        sidecar.symlink_to(target)
        with pytest.raises(ValueError, match="not a regular file"):
            database._secure_modes()
        assert target.read_text() == "untouched"
        assert target.stat().st_mode & 0o777 == 0o644
    finally:
        sidecar.unlink(missing_ok=True)
        database.close()
