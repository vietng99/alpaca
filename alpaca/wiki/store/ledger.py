"""events.jsonl mirror + verify_chain - the CO-AUTHORITATIVE replay spine (D-1) and its self-audit.

The in-DB `ingest_event` hash-chain and the on-disk `events.jsonl` are MUTUAL cross-checks
(PKT-2 dual-loop). A divergence between them, or a broken checksum link, is a detected hole; the
cited `raw/` block is the immutable arbiter. `verify_chain` is the detect-only corruption audit
(bit-rot / botched migration / Dream misfire), not an anti-adversary device.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import time
from typing import Any
from contextlib import contextmanager

from ..determinism import canonical_json, sha256_hex
from .db import open_vault_parent

GENESIS = sha256_hex("rune2-genesis")
_LOCK_TIMEOUT_S = 5.0
_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
               | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
_WRITE_FLAGS = (os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))


class LedgerError(RuntimeError):
    """Base error for ledger corruption, divergence, and locking failures."""


class LedgerDivergence(LedgerError):
    """DB and JSONL no longer share one verified prefix."""


class LedgerLockTimeout(LedgerError):
    """Another writer held the JSONL lock beyond the bounded wait."""


def _secure_parent_fd(path: Path, vault_dir: Path) -> tuple[Path, int]:
    """Open a contained parent through the shared no-follow vault walk."""
    if not path.name or path.name in (".", ".."):
        raise LedgerError(f"invalid ledger filename: {path}")
    try:
        return open_vault_parent(vault_dir, path, create_parent=True)
    except (OSError, ValueError) as exc:
        raise LedgerError(f"unsafe or uncontained ledger path refused: {path}") from exc


def event_checksum(seq: int, prev: str, op: str, doc_id: str | None, payload: dict) -> str:
    """Frozen concat order - NEVER change it. ts is EXCLUDED (wall-clock, not reproducible)."""
    body = canonical_json({"seq": seq, "prev": prev, "op": op, "doc_id": doc_id, "payload": payload})
    return sha256_hex(body)


class Ledger:
    def __init__(self, path: Path, *, vault_dir: Path):
        supplied = Path(path).expanduser()
        candidate, parent_fd = _secure_parent_fd(supplied, Path(vault_dir))
        self.path = candidate
        self.vault_dir = Path(vault_dir)
        self._name = self.path.name
        self._lock_name = self._name + ".lock"
        try:
            try:
                info = os.stat(self._name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode):
                raise LedgerError(f"non-regular ledger path refused: {self.path}")
            try:
                fd = os.open(self._name, _READ_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise LedgerError(f"unsafe ledger path refused: {self.path}") from exc
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise LedgerError(f"non-regular ledger path refused: {self.path}")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _locked(self):
        """Lock a persistent inode; the kernel releases ownership even on SIGKILL.

        Never unlink this file: a waiting process may already have it open, and a
        replacement inode would let two writers enter at once. PID text is only
        diagnostic. Deploy all writers together when replacing the old O_EXCL
        protocol; an older writer does not participate in advisory locking.
        """
        _candidate, parent_fd = _secure_parent_fd(self.path, self.vault_dir)
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        fd = None
        try:
            try:
                fd = os.open(self._lock_name, os.O_CREAT | _WRITE_FLAGS,
                             0o600, dir_fd=parent_fd)
            except OSError as exc:
                raise LedgerError(f"unsafe ledger lock path refused: {self.lock_path}") from exc
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise LedgerError(f"unsafe ledger lock path refused: {self.lock_path}")
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LedgerLockTimeout(
                            f"timed out waiting for ledger lock: {self.lock_path}"
                        )
                    time.sleep(0.01)
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
            self._write_all(fd, str(os.getpid()).encode("ascii"))
            yield parent_fd
        finally:
            # Close also releases flock when initialization or the body raises.
            try:
                if fd is not None:
                    os.close(fd)
            finally:
                os.close(parent_fd)

    @staticmethod
    def _decode_complete(data: bytes) -> list[dict]:
        out = []
        for line_no, raw in enumerate(data.splitlines(), 1):
            if not raw.strip():
                continue
            try:
                out.append(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LedgerError(f"invalid ledger JSON at line {line_no}") from exc
        return out

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("ledger write made no progress")
            view = view[written:]

    def _append_unlocked(self, events: list[dict[str, Any]], parent_fd: int) -> None:
        if not events:
            return
        payload = b"".join((canonical_json(event) + "\n").encode("utf-8") for event in events)
        try:
            fd = os.open(
                self._name,
                os.O_CREAT | os.O_APPEND | _WRITE_FLAGS,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise LedgerError(f"unsafe ledger path refused: {self.path}") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise LedgerError(f"non-regular ledger path refused: {self.path}")
            os.fchmod(fd, 0o600)
            self._write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _read_unlocked(self, parent_fd: int) -> bytes:
        try:
            fd = os.open(self._name, _READ_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            return b""
        except OSError as exc:
            raise LedgerError(f"unsafe ledger path refused: {self.path}") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise LedgerError(f"non-regular ledger path refused: {self.path}")
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def append(self, event: dict[str, Any]) -> None:
        self.append_batch([event])

    def append_batch(self, events: list[dict[str, Any]]) -> None:
        with self._locked() as parent_fd:
            self._append_unlocked(events, parent_fd)

    def read_all(self) -> list[dict]:
        with self._locked() as parent_fd:
            data = self._read_unlocked(parent_fd)
            if data and not data.endswith(b"\n"):
                raise LedgerError("incomplete final ledger line")
            return self._decode_complete(data)

    def sync_from_rows(self, db_rows: list[dict[str, Any]]) -> None:
        """Append committed DB events after verifying the existing JSONL prefix.

        Only an incomplete final line is repairable because a crash can leave that exact shape.
        Any complete-line mismatch is treated as corruption and blocks the writer.
        """
        ok, why = verify_chain(db_rows)
        if not ok:
            raise LedgerDivergence(f"DB event chain invalid: {why}")
        with self._locked() as parent_fd:
            data = self._read_unlocked(parent_fd)
            incomplete = bool(data and not data.endswith(b"\n"))
            prefix_data = data
            if incomplete:
                cut = data.rfind(b"\n") + 1
                prefix_data = data[:cut]
            ledger_rows = self._decode_complete(prefix_data)
            ok, why = verify_chain(ledger_rows)
            if not ok:
                raise LedgerDivergence(f"JSONL event chain invalid: {why}")
            if len(ledger_rows) > len(db_rows):
                raise LedgerDivergence(
                    f"ledger ahead of committed DB: ledger={len(ledger_rows)} db={len(db_rows)}"
                )
            for index, row in enumerate(ledger_rows):
                expected = db_rows[index]
                if row["seq"] != expected["seq"] or row["checksum"] != expected["checksum"]:
                    raise LedgerDivergence(f"DB/JSONL divergence at seq {expected['seq']}")
            if incomplete:
                try:
                    fd = os.open(self._name, _WRITE_FLAGS, dir_fd=parent_fd)
                except OSError as exc:
                    raise LedgerError(f"unsafe ledger path refused: {self.path}") from exc
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise LedgerError(f"non-regular ledger path refused: {self.path}")
                    os.ftruncate(fd, len(prefix_data))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self._append_unlocked(db_rows[len(ledger_rows):], parent_fd)


def verify_chain(rows: list[dict]) -> tuple[bool, str]:
    """Recompute every link. Returns (ok, reason). rows ordered by seq ascending."""
    prev = GENESIS
    last_seq = 0
    for r in rows:
        seq = r["seq"]
        if seq != last_seq + 1:
            return False, f"seq gap: expected {last_seq + 1}, got {seq}"
        expect = event_checksum(seq, prev, r["op"], r.get("doc_id"), r.get("payload", {}))
        if expect != r["checksum"]:
            return False, f"checksum mismatch at seq {seq}"
        if r["prev_checksum"] != prev:
            return False, f"prev_checksum break at seq {seq}"
        prev = r["checksum"]
        last_seq = seq
    return True, "ok"


def reconcile_db_vs_ledger(db_rows: list[dict], ledger_rows: list[dict]) -> tuple[bool, str]:
    """PKT-2 dual-loop: the DB chain and the on-disk ledger must agree. Divergence = a hole."""
    if len(db_rows) != len(ledger_rows):
        return False, f"length divergence: db={len(db_rows)} ledger={len(ledger_rows)}"
    for a, b in zip(db_rows, ledger_rows):
        if a["seq"] != b["seq"] or a["checksum"] != b["checksum"]:
            return False, f"divergence at seq {a['seq']}"
    return True, "ok"
