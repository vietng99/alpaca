"""ingest.absorb - the write path (§4). ONE of the two writers; the answer path never writes.

Curator-gated, incremental, atomic all-or-nothing. Orchestrates:
hashgate -> segment+anchor -> contextual enrich -> deterministic-first extract (llm fallback
quarantined) -> set-returning resolve -> agentic ENCODE (reconcile+place) -> incremental upsert +
index delta -> chained ingest_event -> events.jsonl. Heavy consolidation is deferred to Dream.

Alpaca index maintenance runs through Writer, with the source changes and chained
ledger in one transaction. Ranking primitives remain behind the retrieval door.
"""
from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
import stat

from ..clock import Clock, now_iso, to_clock_format
from ..determinism import sha256_hex
from ..config import Config
from ..providers.registry import Providers
from ..store.db import DB
from ..store.ledger import Ledger
from ..store.write import Writer
from . import extract as ex
from . import record_extract
from .encode import encode_candidate
from .enrich import context_header
from .firewall import scan as fw_scan, escalate_sensitivity   # os.4 advisory marking tripwire (Fork B)
from .hashgate import block_changed                           # ADR-042 skip-if-unchanged (block)
from .resolve import ensure_node
from .segment import segment
from .shelf_life import parse_shelf_life, stamp_edges
from .shrink import assess_shrink, shrink_floor_of            # pc.5 body-shrink sanity gate
from ..engine.compartment import is_private_path
from ..engine.echo import (EchoIndex, ensure_schema as echo_ensure_schema,
                           detect_echo, record_provenance)    # os.2 echo-provenance capture at absorb


_SOURCE_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
_SOURCE_FILE_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                      | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))


def _open_source_dir(name: str, parent_fd: int, display: Path) -> int:
    try:
        fd = os.open(name, _SOURCE_DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(f"symlinked or unstable source directory refused: {display}") from exc
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"non-directory source path refused: {display}")
    return fd


def _open_vault_root(vault: Path) -> int:
    """Open vault without following any path component."""
    fd = os.open(os.sep, _SOURCE_DIR_FLAGS)
    current = Path(os.sep)
    try:
        for component in vault.parts[1:]:
            current = current / component
            child_fd = _open_source_dir(component, fd, current)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_source_file(name: str, parent_fd: int, display: Path) -> str:
    try:
        fd = os.open(name, _SOURCE_FILE_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(f"symlinked or unstable source file refused: {display}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"non-regular source file refused: {display}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(fd)


def _read_source_pages(dir_fd: int, prefix: Path = Path()) -> list[tuple[Path, str]]:
    pages: list[tuple[Path, str]] = []
    with os.scandir(dir_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        relative = prefix / name
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"source tree changed during read: {relative}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"symlinked source entry refused: {relative}")
        if stat.S_ISDIR(info.st_mode):
            child_fd = _open_source_dir(name, dir_fd, relative)
            try:
                pages.extend(_read_source_pages(child_fd, relative))
            finally:
                os.close(child_fd)
        elif name.endswith(".md"):
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"non-regular source file refused: {relative}")
            pages.append((relative, _read_source_file(name, dir_fd, relative)))
    pages.sort(key=lambda item: item[0].as_posix())
    return pages


# A transcript projection opens each message with a role line (`user:`) or a `tool: <name>` line, and
# a message that has a time carries `ts: <time>` on the very next line (ingest/drain.py
# `_transcript_doc`). `input:` and `result:` label the parts of a tool call, not a new message.
_ROLE_LINE = re.compile(r"(?!input:$|result:$)[a-z][a-z_-]*:")


def _header_meta(text: str) -> dict[str, str]:
    """The `key: value` lines of a document's first paragraph, as the capture renderer writes them."""
    from .drain import header_fields                 # each value read back exactly (t-070 round 2 C-B)
    return header_fields(text)


def _transcript_block_times(text: str, blocks: list[dict]) -> list[str | None]:
    """The source time of each transcript block (OB-01): the time of the message it begins in.

    A block that opens a message reads that message's `ts:` line, which sits right after its first
    line; a later paragraph of the same message keeps that time; a message with no `ts:` line is
    unknown, and so is a header block before the first message. The line is read from the document
    slice, never from `block["text"]`: `segment` strips each block, so an escaped `ts:` line that
    opens a block (the renderer gives it a leading space) would otherwise read as the renderer's own.
    """
    times: list[str | None] = []
    current: str | None = None
    for block in blocks:
        lines = text[block["char_start"]:block["char_end"]].split("\n")
        if _ROLE_LINE.fullmatch(lines[0]) or lines[0].startswith("tool: "):
            current = None
            if len(lines) > 1 and lines[1].startswith("ts: "):
                current = to_clock_format(lines[1][4:])
        times.append(current)
    return times


def _source_times(doc_id: str, kind: str, text: str, blocks: list[dict],
                  source_created_at: str | None) -> tuple[str | None, list[str | None]]:
    """(document time, time per block), in the store clock's format; None is unknown (FR-5).

    An explicit `source_created_at` times every block. Otherwise only a captured document (kind
    `raw`) is read: an event note or Alpaca source by the `ts:` line of its header, a transcript by
    the `ts:` line of each message. A `wiki ingest` document is the author's text, so a `ts:` line in
    it is never a time."""
    explicit = to_clock_format(source_created_at)
    if explicit is not None:
        return explicit, [explicit] * len(blocks)
    if kind != "raw":
        return None, [None] * len(blocks)
    header = _header_meta(text)
    if header.get("role") == "transcript" and "raw/transcripts/" in doc_id:
        times = _transcript_block_times(text, blocks)
        return next((t for t in times if t), None), times
    moment = to_clock_format(header.get("ts"))
    return moment, [moment] * len(blocks)


def _EXCLUDED(doc_id: str) -> dict:
    """The answer of `absorb_text` for a document whose source a withdrawal took out."""
    return {"doc_id": doc_id, "skipped": True, "reason": "excluded"}


class LandingMoved(Exception):
    """The caller's landing precondition (`absorb_text(landing=...)`) no longer held under the write
    lock: another connection changed what is stored at the document's id. Nothing was written; the
    caller decides where the document lands again."""


class DeadlineExceeded(Exception):
    """`absorb_text` ran past `Absorber.abort_at` between two blocks. The document's transaction
    is rolled back, so nothing of it landed; the caller records a deferral and goes on later."""


#: The echo index read (t-462): the rowid of every active block, from `idx_blocks_status` alone, then
#: those rows by rowid in ascending order (`ECHO_ROWS_SQL`, `ECHO_CHUNK` at a time), which is the
#: order of the table in the store file. Main f15a17101 walked the primary key's index in block id
#: order (`WHERE +status='active' ORDER BY block_id`, kept from t-070 RC4 to avoid a sort through
#: temp files): one table lookup in random file order for EVERY block the store ever held, retired
#: ones included, whose `status` column sits after the text, so each lookup read the whole row. On
#: the live store (4.5 million rows, 717,000 active, 22 GB) that read the whole file at random and
#: took about 8 minutes after a collector restart (t-462; `.alpaca/proofs/op-008/t-462.logs/`). The
#: order of the rows does not change the index (`EchoIndex.load` fills maps and sets, and `detect`
#: sorts what it reads), nor does any query here sort through temp files.
ECHO_ROWIDS_SQL = "SELECT rowid FROM blocks WHERE status='active'"
ECHO_ROWS_SQL = "SELECT block_id,doc_id,text FROM blocks WHERE rowid IN (%s)"
ECHO_CHUNK = 500


def echo_rows(conn):
    """Every active block of the store as (block_id, text, doc_id), the rows `EchoIndex.load` takes,
    read in one snapshot: inside the caller's open transaction when there is one, else in a read
    transaction of its own, so a commit by another connection between two chunks is not seen
    half-way. Rowid order (`ECHO_ROWIDS_SQL`)."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        rowids = sorted(row[0] for row in conn.execute(ECHO_ROWIDS_SQL))
        for start in range(0, len(rowids), ECHO_CHUNK):
            part = rowids[start:start + ECHO_CHUNK]
            for row in conn.execute(ECHO_ROWS_SQL % ",".join("?" * len(part)), part).fetchall():
                yield row[0], row[2], row[1]
    finally:
        if own and conn.in_transaction:
            conn.execute("COMMIT")


def load_echo_index(conn) -> EchoIndex:
    """The echo index of every active block of the store (`echo_rows`)."""
    index = EchoIndex()
    rows = echo_rows(conn)
    try:
        index.load(rows)
    finally:
        rows.close()
    return index


class Absorber:
    #: A `time.monotonic()` value after which `absorb_text` stops between two blocks and raises
    #: `DeadlineExceeded` (the hard deadline of a hook, which would otherwise cut the process
    #: mid-document); None for no limit.
    abort_at: float | None = None

    def __init__(self, cfg: Config, clock: Clock = now_iso):
        self.cfg = cfg
        self.db = DB(cfg)
        self.db.pour()
        echo_ensure_schema(self.db)                    # os.2 provenance/independent-witness columns
        self.ledger = Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir)
        self.writer = Writer(self.db, self.ledger, clock)
        self.providers = Providers(cfg)
        self.vocab = {r["predicate"] for r in self.db.conn.execute("SELECT predicate FROM predicates")}
        self._echo_index = None
        self._echo_version = None                     # PRAGMA data_version when the index was read
        self._withdrawn = None                        # (exclusions_state, servable.Withdrawn | None)
        self._joined = False                          # writes inside the writer's open transaction

    @property
    def echo_index(self) -> EchoIndex:
        """The echo index of every active block, read from the store on first use. A capture whose
        documents all pass the hashgate never detects an echo, so a hook that lands nothing new
        pays no load (about 1 s on a store of 40,000 blocks). Whoever changes the store's blocks
        through this absorber keeps a loaded index current (`add`, `remove`); one that was not
        loaded yet needs nothing, since its first read sees the store as it is then.
        `absorb_text` reads it before it takes the write lock (`_preload_echo_index`)."""
        if self._echo_index is None:
            self._echo_version = self._data_version()
            self._echo_index = load_echo_index(self.db.conn)
        return self._echo_index

    def _reload_echo_index(self) -> None:
        self._echo_index = None

    def data_version(self) -> int:
        """The store's data version as this absorber's connection sees it (`_data_version`): a caller
        that decides something about the store before `absorb_text` passes it as `landing_version`."""
        return self._data_version()

    def _data_version(self) -> int:
        """SQLite's `PRAGMA data_version` of this absorber's connection: it changes when another
        connection commits to the store, never for this connection's own commits."""
        return self.db.conn.execute("PRAGMA data_version").fetchone()[0]

    def _preload_echo_index(self, doc_id: str, blocks: list[dict]) -> bool:
        """Read the echo index before `absorb_text` takes the write lock, when it is not loaded yet and
        a block of this document will pass the hashgate (the only reader of the index). On the live
        store the read takes about 30 s, and every other writer (a hook, the collector) waits at
        most its busy timeout of 5 s for the lock, so the lock is held for the writes alone (main
        40c7233ac read the index in `__init__`, outside any transaction). True when it read the
        index here; the caller then checks under the lock that no other connection committed in
        between (`_echo_index_stale`), so the detector sees the store as a read under the lock
        would. A document past `abort_at` is deferred before the read, as the block loop would."""
        if self._echo_index is not None or not any(
                block_changed(self.db, block["block_id"], block["block_sha256"]) for block in blocks):
            return False
        if self.abort_at is not None and time.monotonic() >= self.abort_at:
            raise DeadlineExceeded(doc_id)
        self.echo_index
        return True

    def _echo_index_stale(self) -> bool:
        """Under the write lock: whether another connection committed since the index was read."""
        return self._echo_index is not None and self._data_version() != self._echo_version

    def _echo_forget(self, block_id: str) -> None:
        if self._echo_index is not None:
            self._echo_index.remove(block_id)

    @contextmanager
    def _transaction(self):
        if self._joined:
            if not self.writer._tx_active:
                raise RuntimeError("a joined absorber writes only inside its writer's open transaction")
            try:
                yield
            except BaseException:
                self._reload_echo_index()
                raise
            return
        try:
            with self.writer.transaction():
                yield
        except BaseException:
            # A provider or ledger failure must not leave phantom witnesses in memory.
            self._reload_echo_index()
            raise

    @contextmanager
    def joined(self):
        """Inside the writer's open transaction (`Writer.transaction`), `absorb_text` and
        `retire_documents` write in that transaction instead of one of their own, so a caller lands
        documents and applies a record act in one commit: the replay of a transcript withdrawal lands
        the version it names and withdraws it together, so a crash leaves neither (t-070 round 2
        C-E). The echo index is read again after the block, since the caller may still roll back."""
        if not self.writer._tx_active:
            raise RuntimeError("Absorber.joined needs the writer's open transaction")
        self._joined = True
        try:
            yield self
        finally:
            self._joined = False
            self._reload_echo_index()

    # ---- public API -----------------------------------------------------
    def absorb_text(self, doc_id: str, text: str, kind: str = "raw", path: str | None = None,
                    domain: str = "general", source_created_at: str | None = None,
                    waive_reason: str | None = None, retier_after: bool = True,
                    origin_hash: str | None = None, session: str | None = None,
                    unscreened: str | None = None, landing=None,
                    landing_version: int | None = None, before_commit=None,
                    under_lock=None) -> dict:
        """Index one document through the write door. `source_created_at` times every block;
        without it a captured (`raw`) document is timed from its own header or messages
        (`_source_times`). `origin_hash` is the hash of what the document was made from (the record
        event row, or a transcript's native file) and goes to the document's `source_versions` row.
        `session` is the session that asked for a document with no header of its own (`wiki ingest`,
        FR-4); a captured document keeps the session its header names.

        This is the door every document passes (FR-26; dec-804f2708474c Q-A; t-070 RC2): what is
        stored, indexed, hashed and versioned is the text with its secrets masked, whichever path
        brought it (the drain, `wiki ingest`, the import, a direct call such as the evaluation
        rebuild's). A text the one screen already produced (`drain.Screened`: the drain's renderers,
        `wiki ingest` and the import screen once, before their vault copy) is stored as it is, with
        no second pass; any other text is screened here (`drain.screen`). Either way the screen runs
        once and the stored text is its fixed point.

        The marking scanner (architecture.md 9.5) reads the document before the screen: the screen
        may mask the very word a marking rule reads (the word after "SECRET:"), so it may add
        privacy and never take it away. It reads `unscreened`, the document as its renderer had it
        before any redaction, when the caller screened `text` itself (the drain's renderers,
        `wiki ingest`), else `text` as given. A document it trips on is private: the drain routes
        it under `wiki/private/` first, and at a public path the write is refused
        (`PrivatePathViolation`), as before the screen existed.

        `landing(conn)`, when given, is the caller's own precondition (the drain's: this document
        still lands at `doc_id`, the event identity of `drain._same_event`), decided at the store's
        data version `landing_version` (`data_version`); under the write lock it is asked again when
        another connection committed since, and when it no longer holds `LandingMoved` is raised and
        nothing is written (t-070 RC1). With no version it is always asked again.

        `before_commit(conn)`, when given, runs under the write lock after the landing decision and
        the writes and before the commit, whether the document changed or not, and never for a
        withdrawn source (`excluded`): the drain writes the vault bytes there, so a crash leaves at
        worst a vault file with no row, never a row with no file, and no bytes of a withdrawn source
        reach the vault (t-070 round 2 C-E, I-3, A-6). An exception it raises rolls the document
        back.

        `under_lock(conn)`, when given, runs first under the write lock, before the landing decision
        and any write: the drain reads there what the store held of the source before this landing
        (t-430 `held_before`), so the reading and the `before_commit` that records the version are
        one critical section and two captures cannot interleave between them."""
        from .drain import screen                     # drain imports this module at call time
        scan_text = text if unscreened is None else unscreened
        text = screen(text)                           # no pass for a Screened text
        stored_path = path or doc_id
        path_obj = Path(stored_path)
        if path_obj.is_absolute():
            resolved_path = path_obj.resolve(strict=False)
            try:
                stored_path = resolved_path.relative_to(self.cfg.vault_dir.resolve()).as_posix()
            except ValueError as exc:
                raise ValueError(f"source path escapes selected vault: {path_obj}") from exc
        else:
            stored_path = path_obj.as_posix()
        fw = fw_scan(scan_text)
        sha = sha256_hex(text)
        base_blocks = segment(doc_id, text)
        doc_time, block_times = _source_times(doc_id, kind, text, base_blocks, source_created_at)
        # The preconditions (the exclusion, the stored document, the hashgate, the shrink guard) are
        # read here, without the lock, together with the store's data version; under the lock they are
        # read again when another connection committed in between (t-070 fix plan RC1, review finding
        # C-1: a withdrawal that commits in that gap must stop the note landing). The expensive work
        # (screen, scan, segmentation, the shrink guard's atom count, the echo index) stays outside.
        plan = self._plan(doc_id, kind, text, sha, base_blocks, stored_path, fw, origin_hash,
                          waive_reason)
        if plan["excluded"]:
            return _EXCLUDED(doc_id)
        if plan["changed"]:
            preloaded = self._preload_echo_index(doc_id, plan["blocks"])
        else:
            preloaded = False
        shelf_life = parse_shelf_life(text)
        with self._transaction():
            if under_lock is not None:
                under_lock(self.db.conn)
            now = self._data_version()
            if landing is not None and now != landing_version and not landing(self.db.conn):
                raise LandingMoved(doc_id)
            if now != plan["version"]:
                # Another connection committed since the plan was read: read it again under the lock.
                if preloaded and self._echo_index_stale():
                    self._echo_index = None
                plan = self._plan(doc_id, kind, text, sha, base_blocks, stored_path, fw, origin_hash,
                                  waive_reason, atoms=plan["atoms"])
                if plan["excluded"]:
                    # A withdrawn source is not revived by a later capture of the same bytes, nor
                    # re-indexed in new bytes: exclusions are append-only and there is no verb that
                    # undoes one (architecture.md 10.2; FR-22). The document stays as the withdrawal
                    # left it.
                    return _EXCLUDED(doc_id)
            if not plan["changed"]:
                current = plan["blocks"]
                self.writer.revive_projection(doc_id)
                self.writer.refresh_doc_metadata(
                    doc_id, path=stored_path, kind=kind, domain=domain,
                    source_created_at=doc_time, private=plan["private"],
                    sensitivity=plan["sensitivity"],
                    block_times={b["block_id"]: t for b, t in zip(current, block_times)},
                )
                self._record_version(doc_id, kind, text, sha, doc_time, plan["private"], origin_hash,
                                     session)
                rules = self._apply_rules(doc_id, kind, text, plan["private"],
                                          self._active_header_block(doc_id, current))
                self.writer.maintain_indexes(self.providers, retier=retier_after)
                if before_commit is not None:
                    before_commit(self.db.conn)
                return {"doc_id": doc_id, "skipped": True, "reason": "unchanged",
                        "metadata_repaired": True, "rules": rules}
            blocks = plan["blocks"]
            sensitivity, private_flag = plan["sensitivity"], plan["private"]
            summary = {"doc_id": doc_id, "blocks": 0, "edges": 0, "quarantined": 0,
                       "corroborated": 0, "corrections": 0, "world_changes": 0,
                       "retired_blocks": 0, "retracted_edges": 0,
                       "marking": fw.rule_id if fw.tripped else None}
            self.writer.revive_projection(doc_id)
            self.writer.upsert_doc(doc_id, stored_path, kind, sha,
                                   byte_len=len(text.encode("utf-8")), domain=domain,
                                   source_created_at=doc_time,
                                   sensitivity=sensitivity, private=private_flag)
            self._record_version(doc_id, kind, text, sha, doc_time, private_flag, origin_hash, session)
            current_block_ids = {block["block_id"] for block in blocks}
            for block, moment in zip(blocks, block_times):
                if self.abort_at is not None and time.monotonic() >= self.abort_at:
                    raise DeadlineExceeded(doc_id)
                block["domain"] = domain                 # os.5 propagate compartment to blocks
                block["source_created_at"] = moment      # FR-5: the source's own time, or unknown
                if moment is not None:
                    block["valid_from"] = moment         # valid time; recorded_at stays capture time
                # ADR-042 HASHGATE: a block whose sha is unchanged is SKIPPED - idempotent re-ingest,
                # no re-embed / re-extract / duplicate work / churn on the unchanged part of a doc.
                if not block_changed(self.db, block["block_id"], block["block_sha256"]):
                    self.writer.upsert_block(block)
                    continue
                block["context_header"] = context_header(block)
                block["embedded_model"] = self.providers.embedder.name
                self.writer.upsert_block(block)
                summary["blocks"] += 1
                # os.2: record this block's derivation at absorb so N corroborating echoes of the same
                # claim later COLLAPSE to ONE independent witness (never the echo-inflated raw count).
                _rel, _cb, _cn = detect_echo(
                    block["text"], self.echo_index, exclude_doc_id=doc_id,
                )
                record_provenance(self.db, block["block_id"], _rel,
                                  cites_block_id=_cb, cites_node_id=_cn)
                self.echo_index.add(block["block_id"], block["text"], doc_id)

                # Entity mentions become nodes and source links; Writer maintains derived vectors.
                for nid, disp in ex.mentions(block["text"]):
                    ensure_node(self.db, self.writer, nid, disp, block["block_id"])

                det, quar = ex.extract(block, self.vocab,
                                       llm_extractor=(self.providers.llm_extractor
                                                      if self.cfg.llm_extractor != "off" else None))
                bcid = block["block_content_id"]
                for cand in det:
                    self._place(cand, bcid, block, summary, quarantined=False)
                for cand in quar:
                    self._place(cand, bcid, block, summary, quarantined=True)
            # The record rules run before the stale blocks retire (OB-17): a rule claim that is live
            # on an active edge follows its note to the re-rendered block, which `retire_block`
            # would otherwise retract with the block it was anchored to.
            summary["rules"] = self._apply_rules(doc_id, kind, text, private_flag,
                                                 blocks[0] if blocks else None)
            stale_ids = [r["block_id"] for r in self.db.conn.execute(
                "SELECT block_id FROM blocks WHERE doc_id=? AND status='active'",
                (doc_id,),
            ).fetchall() if r["block_id"] not in current_block_ids]
            for stale_id in sorted(stale_ids):
                # the re-rendered document is the successor: an edge anchored on the stale block
                # follows its quote to the block that holds it now, keeping its edge id (ARCH 4.3)
                retired = self.writer.retire_block(stale_id, successor_doc_id=doc_id)
                self._echo_forget(stale_id)
                summary["retired_blocks"] += retired["blocks"]
                summary["retracted_edges"] += retired["retracted"]
            stamp_edges(
                self.db, doc_id,
                expires_at=shelf_life["expires_at"],
                volatile=shelf_life["volatile"],
            )
            self.writer.maintain_indexes(self.providers, retier=retier_after)
            if before_commit is not None:
                before_commit(self.db.conn)
        return summary

    def absorb_vault(self, include_kinds: tuple[str, ...] = ("raw", "wiki")) -> list[dict]:
        results = []
        seen_doc_ids: set[str] = set()
        vault = Path(os.path.abspath(self.cfg.vault_dir.expanduser()))
        try:
            vault_fd = _open_vault_root(vault)
        except (OSError, ValueError) as exc:
            raise ValueError(f"symlinked or invalid vault root refused: {vault}") from exc
        try:
            for kind, sub in (("raw", "raw"), ("wiki", "wiki")):
                if kind not in include_kinds:
                    continue
                try:
                    info = os.stat(sub, dir_fd=vault_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise ValueError(f"source root changed during read: {sub}") from exc
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError(f"symlinked source root refused: {sub}")
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError(f"non-directory source root refused: {sub}")
                root_fd = _open_source_dir(sub, vault_fd, Path(sub))
                try:
                    pages = _read_source_pages(root_fd)
                finally:
                    os.close(root_fd)
                for relative, text in pages:
                    doc_id = (Path(sub) / relative).as_posix()
                    seen_doc_ids.add(doc_id)
                    if self.writer.projection_superseded(doc_id):
                        continue
                    results.append(self.absorb_text(
                        doc_id, text, kind=kind, path=doc_id, retier_after=False,
                    ))
        finally:
            os.close(vault_fd)
        kind_marks = ",".join("?" for _ in include_kinds)
        existing = self.db.conn.execute(
            f"SELECT doc_id FROM docs WHERE kind IN ({kind_marks}) ORDER BY doc_id",
            tuple(include_kinds),
        ).fetchall() if include_kinds else []
        missing = [r["doc_id"] for r in existing if r["doc_id"] not in seen_doc_ids]
        if missing:
            with self._transaction():
                for doc_id in missing:
                    retired_block_ids = [r["block_id"] for r in self.db.conn.execute(
                        "SELECT block_id FROM blocks WHERE doc_id=? AND status='active'", (doc_id,)
                    ).fetchall()]
                    retired = self.writer.retire_doc(doc_id)
                    for block_id in retired_block_ids:
                        self._echo_forget(block_id)
                    results.append({"doc_id": doc_id, "retired": True, **retired})
                self.writer.maintain_indexes(self.providers, retier=True)
        else:
            with self._transaction():
                self.writer.maintain_indexes(self.providers, retier=True)
        return results

    def retire_documents(self, doc_ids, reason: str = "source-projection-replaced",
                         superseded_by: str | None = None) -> dict[str, int]:
        """Retain historical evidence while removing replaced projections from current indexes."""
        total = {"blocks": 0, "reanchored": 0, "retracted": 0}
        retired_ids = []
        with self._transaction():
            for doc_id in sorted(set(doc_ids)):
                if superseded_by is not None:
                    self.writer.mark_projection_superseded(doc_id, superseded_by, reason)
                retired_ids.extend(r[0] for r in self.db.conn.execute(
                    "SELECT block_id FROM blocks WHERE doc_id=? AND status='active'", (doc_id,)
                ))
                result = self.writer.retire_doc(doc_id, reason=reason, superseded_by=superseded_by)
                for key in total:
                    total[key] += result[key]
            self.writer.maintain_indexes(self.providers, retier=True)
        for block_id in retired_ids:
            self._echo_forget(block_id)
        return total

    # ---- internal -------------------------------------------------------
    def _plan(self, doc_id: str, kind: str, text: str, sha: str, base_blocks: list[dict],
              stored_path: str, fw, origin_hash: str | None, waive_reason: str | None, *,
              atoms: int | None = None) -> dict:
        """What `absorb_text` will do with this document, read from the store as it is now: whether
        its source is withdrawn (`excluded`), the stored document's privacy and sensitivity, whether
        the text changed (the hashgate, and a document whose blocks were all retired counts as
        changed: its blocks come back under revival ids), and the shrink guard, which raises
        `BodyShrinkRejected` here. `version` is the store's data version before the reads, so the
        caller can tell under the write lock whether another connection committed since (t-070
        RC1). `atoms`, the guard's atom count of the new body, depends on the text alone and is
        passed back in when the plan is read again."""
        version = self._data_version()
        plan = {"version": version, "excluded": self._excluded(doc_id, kind, text, origin_hash),
                "atoms": atoms}
        if plan["excluded"]:
            return plan
        prior_doc = self.db.conn.execute(
            "SELECT byte_len,sensitivity,private,privacy_scanned,content_sha256 FROM docs WHERE doc_id=?",
            (doc_id,),
        ).fetchone()
        plan["sensitivity"] = escalate_sensitivity(prior_doc["sensitivity"] if prior_doc else None, fw)
        plan["private"] = 1 if (
            fw.tripped or is_private_path(stored_path)
            or (prior_doc is not None and bool(prior_doc["private"])
                and bool(prior_doc["privacy_scanned"]))
        ) else 0
        changed = prior_doc is None or prior_doc["content_sha256"] != sha
        retired_same_content = bool(prior_doc and self.db.conn.execute(
            "SELECT 1 FROM blocks WHERE doc_id=? AND status='superseded' "
            "AND NOT EXISTS (SELECT 1 FROM blocks a WHERE a.doc_id=? AND a.status='active') LIMIT 1",
            (doc_id, doc_id),
        ).fetchone())
        plan["changed"] = changed or retired_same_content
        blocks = [dict(b) for b in base_blocks]
        plan["blocks"] = blocks
        if not plan["changed"]:
            return plan
        if retired_same_content:
            for block in blocks:
                base_id = block["block_id"]
                revival = 1
                while self.db.conn.execute(
                    "SELECT 1 FROM blocks WHERE block_id=?", (f"{base_id}@r{revival}",)
                ).fetchone():
                    revival += 1
                block["block_id"] = f"{base_id}@r{revival}"
                block["block_content_id"] = f"{block['block_content_id']}:revival:{revival}"
        if prior_doc is not None:
            if plan["atoms"] is None:
                count = 0
                for _blk in base_blocks:
                    _det, _quar = ex.extract(_blk, self.vocab)
                    count += len(_det) + len(_quar)
                plan["atoms"] = count
            # An event note re-rendered from its immutable record event may come out smaller (a
            # change of format, never a truncated paste; its dropped facts leave through
            # `retire_block`, a retraction). The gate is waived for it only when the caller says so:
            # the drain passes `drain.RERENDER_WAIVER` for the owner's re-render
            # (`capture(rerender_notes=True)`); no other path waives it (merge of op-008 into main,
            # review B4, as main cea450f95 kept it). The gate keeps guarding authored documents.
            assess_shrink(self.db, doc_id, plan["atoms"],
                          old_text_len=prior_doc["byte_len"],
                          new_text_len=len(text.encode("utf-8")),
                          shrink_floor=shrink_floor_of(self.cfg), waive_reason=waive_reason)
        return plan

    def _excluded(self, doc_id: str, kind: str, text: str, origin_hash: str | None = None) -> bool:
        """Whether the source this document stands for was taken out by a withdrawal, by the set the
        servable predicate reads (`servable.excluded_source_refs`): an `exclusions` row names it, or
        it fell with one (a source copy and the event note it was made from, whichever was
        withdrawn), or this document would join one on landing (`servable.Withdrawn.holds`). The
        expected event sets read the same set (`service._excluded_events`), so a note that fell with
        a withdrawn source copy is neither missing nor captured again (check round 4 invariants B1).
        The source is named as `_record_version` names it."""
        from .drain import projection_identity       # drain imports this module at call time
        source_ref = (projection_identity(text) if kind == "raw" else None) or "document:" + doc_id
        view = self._withdrawn_view()
        return view is not None and view.holds(source_ref, doc_id, origin_hash)

    def _withdrawn_view(self):
        """`servable.withdrawn` of the store, read again whenever an `exclusions` row was added since
        the last read (`servable.exclusions_state`). Between two such rows it cannot change by a
        capture: a document that would extend it is one `_excluded` skips."""
        from ..servable import exclusions_state, withdrawn
        state = exclusions_state(self.db)
        if state is None or not state[0]:
            return None
        if self._withdrawn is None or self._withdrawn[0] != state:
            self._withdrawn = (state, withdrawn(self.db))
        return self._withdrawn[1]

    def _record_version(self, doc_id: str, kind: str, text: str, sha: str, doc_time: str | None,
                        private: int, origin_hash: str | None, session: str | None = None) -> None:
        """The document's `source_versions` row (FR-4). The source is named by the header of a
        captured document (`drain.projection_identity`), by `document:<doc_id>` for any other; the
        operator, session, op and task come from the same header, which only the renderer writes."""
        from .drain import projection_identity       # drain imports this module at call time
        header = _header_meta(text) if kind == "raw" else {}
        source_ref = (projection_identity(text) if kind == "raw" else None) or "document:" + doc_id
        transcript = header.get("role") == "transcript"
        self.writer.record_source_version(
            source_ref, doc_id, sha, origin_hash=origin_hash,
            operator=header.get("operator") or None, session=header.get("session") or session or None,
            op=None if transcript else (header.get("op") or None),
            task=header.get("task") or None, source_time=doc_time, private=private)

    def _apply_rules(self, doc_id: str, kind: str, text: str, private: int,
                     header_block: dict | None) -> dict:
        """The record rules of architecture.md 8.1 on a captured document (kind `raw`), in the
        caller's transaction. A `wiki ingest` document is the author's text and runs none."""
        if kind != "raw":
            return record_extract.new_report()
        return record_extract.apply(self.db, self.writer, doc_id=doc_id, text=text,
                                    header_block=header_block, private=bool(private))

    def _active_header_block(self, doc_id: str, segments: list[dict]) -> dict | None:
        """The stored active block that holds the header of an unchanged document. Its id may carry
        a revival suffix, so it is read by the content hash of the first segment."""
        if not segments:
            return None
        row = self.db.conn.execute(
            "SELECT block_id, block_content_id, text FROM blocks "
            "WHERE doc_id=? AND status='active' AND block_sha256=? ORDER BY ordinal LIMIT 1",
            (doc_id, segments[0]["block_sha256"])).fetchone()
        return dict(row) if row else None

    def _place(self, cand: dict, bcid: str, block: dict, summary: dict, quarantined: bool) -> None:
        # ensure subject/object nodes exist even if not wikilinked as bare mentions
        ensure_node(self.db, self.writer, cand["subj_node"], cand["subj_node"], block["block_id"])
        if cand["obj_datatype"] == "node" and cand.get("obj_node"):
            ensure_node(self.db, self.writer, cand["obj_node"], cand["obj_node"], block["block_id"])
        cand["source_created_at"] = block.get("source_created_at")   # pc.7 observation-date anchor
        out = encode_candidate(
            self.db, self.writer, cand, bcid,
            quarantined=quarantined, block_text=block["text"],
        )
        if out["action"] not in ("noop", "corroborated"):
            self.writer.add_node_block(cand["subj_node"], block["block_id"], role="claim")
            if cand.get("obj_node"):
                self.writer.add_node_block(cand["obj_node"], block["block_id"], role="claim")
            summary["edges"] += 1
        if out["quarantined"]:
            summary["quarantined"] += 1
        if out["action"] == "corroborated":
            summary["corroborated"] += 1
        elif out["action"] == "correction":
            summary["corrections"] += 1
        elif out["action"] == "world-change":
            summary["world_changes"] += 1

    def close(self) -> None:
        self.db.close()


def absorb_vault(cfg: Config, clock: Clock = now_iso,
                 include_kinds: tuple[str, ...] = ("raw", "wiki")) -> list[dict]:
    a = Absorber(cfg, clock)
    try:
        return a.absorb_vault(include_kinds=include_kinds)
    finally:
        a.close()
