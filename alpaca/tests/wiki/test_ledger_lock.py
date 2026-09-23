"""A crashed or failed ledger writer must not strand subsequent capture."""
import os
from pathlib import Path
import selectors
import subprocess
import sys

import pytest

from alpaca.wiki.store import ledger


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_LOCK_TIMEOUT_S", 0.1)
    return ledger.Ledger(tmp_path / "ledger/events.jsonl", vault_dir=tmp_path)


def test_reuses_empty_abandoned_lock(log):
    log.lock_path.write_bytes(b"")
    log.append({"recovered": True})
    assert log.read_all() == [{"recovered": True}]


@pytest.mark.parametrize("failure", ["pid_write", "chmod"])
def test_initialization_failure_releases_lock(log, monkeypatch, failure):
    def fail(*args):
        raise OSError("injected initialization failure")
    with monkeypatch.context() as patch:
        if failure == "pid_write":
            patch.setattr(log, "_write_all", fail)
        else:
            patch.setattr(os, "fchmod", fail)
        with pytest.raises(OSError, match="injected"):
            log.append({"not_committed": True})
    log.append({"next": True})
    assert log.read_all() == [{"next": True}]


def test_live_writer_excludes_peer_and_killed_writer_releases_lock(log):
    code = """import sys
from pathlib import Path
from alpaca.wiki.store.ledger import Ledger
root = Path(sys.argv[1])
log = Ledger(root / 'ledger/events.jsonl', vault_dir=root)
with log._locked():
    print('locked', flush=True)
    sys.stdin.readline()
"""
    proc = subprocess.Popen([sys.executable, "-c", code, str(log.vault_dir)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(proc.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=5), "writer did not acquire its lock"
        assert proc.stdout.readline().strip() == "locked"
        owner_bytes = log.lock_path.read_bytes()
        with pytest.raises(ledger.LedgerLockTimeout):
            log.append({"must_not_write": True})
        assert log.lock_path.read_bytes() == owner_bytes
        proc.kill()
        proc.wait(timeout=5)
        log.append({"after_crash": True})
        assert log.read_all() == [{"after_crash": True}]
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)


def test_lock_inode_remains_stable_and_private(log):
    log.append({"first": True})
    before = log.lock_path.stat()
    log.append({"second": True})
    after = log.lock_path.stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert after.st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_unsafe_lock_path_is_refused(log, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    outside.chmod(0o644)
    if kind == "symlink":
        log.lock_path.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(log.lock_path)
    else:
        log.lock_path.mkdir()
    with pytest.raises(ledger.LedgerError, match="lock path"):
        log.append({"unsafe": True})
    assert outside.read_bytes() == b"untouched"
    assert outside.stat().st_mode & 0o777 == 0o644


def test_competing_processes_keep_every_complete_record(log):
    code = """import sys
from pathlib import Path
from alpaca.wiki.store.ledger import Ledger
root = Path(sys.argv[1])
log = Ledger(root / 'ledger/events.jsonl', vault_dir=root)
for n in range(40):
    log.append({'writer': int(sys.argv[2]), 'n': n, 'text': 'x' * 4096})
"""
    peers = [subprocess.Popen([sys.executable, "-c", code, str(log.vault_dir), str(n)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for n in range(3)]
    try:
        for peer in peers:
            _, err = peer.communicate(timeout=10)
            assert peer.returncode == 0, err
        rows = log.read_all()
        assert len(rows) == 120
        assert {(r['writer'], r['n']) for r in rows} == {(p, n) for p in range(3) for n in range(40)}
    finally:
        for peer in peers:
            if peer.poll() is None:
                peer.kill()
            peer.communicate(timeout=5)
