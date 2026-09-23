"""Connection, schema pour, additive migration, meta, predicate seeding.

`rune.db` is a single WAL SQLite file. The schema is poured once. Constrained older stores gain
additive columns; legacy tables missing physical foreign-key, uniqueness, nullability, or enum
laws are refused and must be rebuilt into a fresh store.
"""
from __future__ import annotations

from importlib import metadata
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any, Iterable

from ..config import Config

_SOURCE_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "schema.sql"
_PACKAGED_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema.sql"
_INSTALLED_SCHEMA_SUFFIX = ("share", "rune2", "schema", "schema.sql")
CURRENT_SCHEMA_VERSION = 3
REBUILD_LOCK_NAME = ".rune2-rebuild.lock"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UnsafeLegacySchema(RuntimeError):
    """Existing tables lack physical constraints required for safe additive migration."""


_REQUIRED_NOT_NULL = {
    "docs": {"path", "kind", "content_sha256"},
    "blocks": {"block_content_id", "doc_id", "ordinal", "text", "valid_from", "recorded_at"},
    "nodes": {"valid_from", "recorded_at", "status"},
    "edges": {
        "subj_node", "predicate", "obj_datatype", "source_block_id", "source_quote",
        "extractor", "recorded_at", "valid_from", "status",
    },
}

_REQUIRED_FOREIGN_KEYS = {
    "blocks": {("doc_id", "docs", "doc_id")},
    "nodes": {("summary_block_id", "blocks", "block_id")},
    "edges": {
        ("subj_node", "nodes", "node_id"),
        ("obj_node", "nodes", "node_id"),
        ("predicate", "predicates", "predicate"),
        ("source_block_id", "blocks", "block_id"),
        ("source_doc_id", "docs", "doc_id"),
    },
}

_REQUIRED_COLUMN_CHECKS = {
    "docs": {
        "kind": "check(kindin('raw','wiki'))",
        "domain": "check(domainin('embedded','career','general','personal'))",
    },
    "blocks": {
        "domain": "check(domainin('embedded','career','general','personal'))",
        "status": "check(statusin('active','superseded'))",
    },
    "nodes": {
        "status": "check(statusin('active','superseded','merged'))",
    },
    "edges": {
        "obj_datatype": "check(obj_datatypein('node','date','number','string','bool'))",
        "extractor": "check(extractorin('deterministic','llm','reflection'))",
        "reconcile_verdict": (
            "check(reconcile_verdictin('novel','refines','contradicts','duplicate','matches'))"
        ),
        "status": "check(statusin('active','invalidated','quarantined','retracted'))",
        "atom_type": "check(atom_typein('fact','take','entity','fulltext'))",
    },
}

# Version 2 is an additive bridge from the original e40/f556 schema and from partially poured
# databases. Identity columns must already exist; every other current runtime column is restored
# before canonical indexes and triggers are created.
_MIGRATION_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "meta": (("value", "TEXT"),),
    "schema_migrations": (("applied_at", "TEXT NOT NULL DEFAULT ''"),),
    "docs": (
        ("path", "TEXT NOT NULL DEFAULT ''"),
        ("kind", "TEXT NOT NULL DEFAULT 'raw'"),
        ("content_sha256", "TEXT NOT NULL DEFAULT ''"),
        ("byte_len", "INTEGER DEFAULT 0"),
        ("mtime", "TEXT"),
        ("git_commit", "TEXT"),
        ("source_authority", "INTEGER DEFAULT 0"),
        ("immutable", "INTEGER DEFAULT 1"),
        ("ingested_at", "TEXT"),
        ("domain", "TEXT NOT NULL DEFAULT 'general' "
                   "CHECK (domain IN ('embedded','career','general','personal'))"),
        ("tier", "INTEGER NOT NULL DEFAULT 1"),
        ("source_created_at", "TEXT"),
        ("sensitivity", "TEXT DEFAULT 'none'"),
        ("private", "INTEGER NOT NULL DEFAULT 1"),
        ("privacy_scanned", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "blocks": (
        ("block_content_id", "TEXT NOT NULL DEFAULT ''"),
        ("occurrence_index", "INTEGER NOT NULL DEFAULT 0"),
        ("chunk_ruleset_version", "TEXT"),
        ("doc_id", "TEXT NOT NULL DEFAULT ''"),
        ("ordinal", "INTEGER NOT NULL DEFAULT 0"),
        ("heading_path", "TEXT"),
        ("char_start", "INTEGER"),
        ("char_end", "INTEGER"),
        ("block_sha256", "TEXT"),
        ("block_type", "TEXT"),
        ("context_header", "TEXT"),
        ("text", "TEXT NOT NULL DEFAULT ''"),
        ("embedded_model", "TEXT"),
        ("valid_from", "TEXT NOT NULL DEFAULT ''"),
        ("valid_until", "TEXT"),
        ("recorded_at", "TEXT NOT NULL DEFAULT ''"),
        ("superseded_at", "TEXT"),
        ("domain", "TEXT NOT NULL DEFAULT 'general' "
                   "CHECK (domain IN ('embedded','career','general','personal'))"),
        ("source_created_at", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded'))"),
    ),
    "nodes": (
        ("canonical_node_id", "TEXT"),
        ("node_type", "TEXT"),
        ("display_name", "TEXT"),
        ("summary_block_id", "TEXT"),
        ("pagerank_prior", "REAL DEFAULT 0.0"),
        ("pagerank", "REAL DEFAULT 0.0"),
        ("page_band", "INTEGER"),
        ("degree", "INTEGER DEFAULT 0"),
        ("attrs", "TEXT"),
        ("valid_from", "TEXT NOT NULL DEFAULT ''"),
        ("valid_until", "TEXT"),
        ("recorded_at", "TEXT NOT NULL DEFAULT ''"),
        ("superseded_at", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'active' "
                   "CHECK (status IN ('active','superseded','merged'))"),
    ),
    "edges": (
        ("subj_node", "TEXT NOT NULL DEFAULT ''"),
        ("predicate", "TEXT NOT NULL DEFAULT 'related_to'"),
        ("obj_node", "TEXT"),
        ("obj_literal", "TEXT"),
        ("obj_datatype", "TEXT NOT NULL DEFAULT 'string' "
                         "CHECK (obj_datatype IN ('node','date','number','string','bool'))"),
        ("source_block_id", "TEXT NOT NULL DEFAULT ''"),
        ("source_doc_id", "TEXT"),
        ("source_quote", "TEXT NOT NULL DEFAULT ''"),
        ("extractor", "TEXT NOT NULL DEFAULT 'deterministic' "
                      "CHECK (extractor IN ('deterministic','llm','reflection'))"),
        ("extractor_version", "TEXT"),
        ("confidence", "REAL DEFAULT 1.0"),
        ("reconcile_verdict", "TEXT CHECK (reconcile_verdict IN "
                              "('novel','refines','contradicts','duplicate','matches'))"),
        ("corroboration_count", "INTEGER NOT NULL DEFAULT 1"),
        ("recorded_at", "TEXT NOT NULL DEFAULT ''"),
        ("superseded_at", "TEXT"),
        ("valid_from", "TEXT NOT NULL DEFAULT ''"),
        ("valid_until", "TEXT"),
        ("supersedes_edge_id", "TEXT"),
        ("superseded_by_edge_id", "TEXT"),
        ("retraction_reason", "TEXT"),
        ("retracted_by", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'active' "
                   "CHECK (status IN ('active','invalidated','quarantined','retracted'))"),
        ("edge_tier", "INTEGER"),
        ("atom_type", "TEXT NOT NULL DEFAULT 'FACT' "
                      "CHECK (atom_type IN ('FACT','TAKE','ENTITY','FULLTEXT'))"),
        ("learned_at", "TEXT"),
        ("source_created_at", "TEXT"),
        ("expires_at", "TEXT"),
        ("volatile", "INTEGER NOT NULL DEFAULT 0"),
        ("derived", "INTEGER DEFAULT 0"),
        ("echo_of_edge_id", "TEXT"),
        ("independent_witness_count", "INTEGER"),
    ),
    "answers": (
        ("txn_axis", "TEXT"),
    ),
    "eval_ledger": (
        ("ts", "TEXT"),
        ("qtype", "TEXT"),
        ("channels", "TEXT"),
        ("verdict", "TEXT"),
        ("n_citations", "INTEGER DEFAULT 0"),
        ("latency_ms", "INTEGER DEFAULT 0"),
        ("abstained", "INTEGER DEFAULT 0"),
        ("fast_path", "INTEGER DEFAULT 0"),
        ("asof_gated", "INTEGER DEFAULT 0"),
        ("stale_serve", "INTEGER DEFAULT 0"),
    ),
    "eval_cases": (
        ("question", "TEXT NOT NULL DEFAULT ''"),
        ("expected", "TEXT"),
        ("gold_block_ids", "TEXT"),
        ("qtype", "TEXT"),
        ("source", "TEXT"),
        ("mutation_target", "TEXT"),
        ("case_polarity", "TEXT NOT NULL DEFAULT 'positive' "
                          "CHECK (case_polarity IN ('positive','negative'))"),
    ),
}

_MIGRATION_BACKFILLS: dict[str, tuple[str, ...]] = {
    "docs": (
        "UPDATE docs SET path=doc_id WHERE path IS NULL OR path=''",
        "UPDATE docs SET kind='raw' WHERE kind IS NULL OR kind=''",
        "UPDATE docs SET content_sha256='' WHERE content_sha256 IS NULL",
        "UPDATE docs SET source_authority=COALESCE(source_authority,0)",
        "UPDATE docs SET immutable=COALESCE(immutable,1)",
        "UPDATE docs SET domain='general' WHERE domain IS NULL OR domain=''",
        "UPDATE docs SET tier=COALESCE(tier,1)",
        "UPDATE docs SET sensitivity='none' WHERE sensitivity IS NULL OR sensitivity=''",
        "UPDATE docs SET private=COALESCE(private,1)",
        "UPDATE docs SET privacy_scanned=COALESCE(privacy_scanned,0)",
    ),
    "blocks": (
        "UPDATE blocks SET block_content_id=block_id WHERE block_content_id IS NULL OR block_content_id=''",
        "UPDATE blocks SET occurrence_index=COALESCE(occurrence_index,0)",
        "UPDATE blocks SET doc_id='' WHERE doc_id IS NULL",
        "UPDATE blocks SET ordinal=COALESCE(ordinal,0)",
        "UPDATE blocks SET text='' WHERE text IS NULL",
        "UPDATE blocks SET valid_from='' WHERE valid_from IS NULL",
        "UPDATE blocks SET recorded_at='' WHERE recorded_at IS NULL",
        "UPDATE blocks SET domain='general' WHERE domain IS NULL OR domain=''",
        "UPDATE blocks SET status='active' WHERE status IS NULL OR status=''",
    ),
    "nodes": (
        "UPDATE nodes SET display_name=node_id WHERE display_name IS NULL OR display_name=''",
        "UPDATE nodes SET pagerank_prior=COALESCE(pagerank_prior,0.0)",
        "UPDATE nodes SET pagerank=COALESCE(pagerank,0.0)",
        "UPDATE nodes SET degree=COALESCE(degree,0)",
        "UPDATE nodes SET valid_from='' WHERE valid_from IS NULL",
        "UPDATE nodes SET recorded_at='' WHERE recorded_at IS NULL",
        "UPDATE nodes SET status='active' WHERE status IS NULL OR status=''",
    ),
    "edges": (
        "UPDATE edges SET subj_node='' WHERE subj_node IS NULL",
        "UPDATE edges SET predicate='related_to' WHERE predicate IS NULL OR predicate=''",
        "UPDATE edges SET obj_datatype='string' WHERE obj_datatype IS NULL OR obj_datatype=''",
        "UPDATE edges SET source_block_id='' WHERE source_block_id IS NULL",
        "UPDATE edges SET source_quote='' WHERE source_quote IS NULL",
        "UPDATE edges SET extractor='deterministic' WHERE extractor IS NULL OR extractor=''",
        "UPDATE edges SET confidence=COALESCE(confidence,1.0)",
        "UPDATE edges SET corroboration_count=COALESCE(corroboration_count,1)",
        "UPDATE edges SET recorded_at='' WHERE recorded_at IS NULL",
        "UPDATE edges SET valid_from='' WHERE valid_from IS NULL",
        "UPDATE edges SET status='active' WHERE status IS NULL OR status=''",
        "UPDATE edges SET atom_type='FACT' WHERE atom_type IS NULL OR atom_type=''",
        "UPDATE edges SET learned_at=recorded_at WHERE learned_at IS NULL",
        "UPDATE edges SET volatile=COALESCE(volatile,0)",
        "UPDATE edges SET derived=COALESCE(derived,0)",
    ),
    "eval_ledger": (
        "UPDATE eval_ledger SET n_citations=COALESCE(n_citations,0)",
        "UPDATE eval_ledger SET latency_ms=COALESCE(latency_ms,0)",
        "UPDATE eval_ledger SET abstained=COALESCE(abstained,0)",
        "UPDATE eval_ledger SET fast_path=COALESCE(fast_path,0)",
        "UPDATE eval_ledger SET asof_gated=COALESCE(asof_gated,0)",
        "UPDATE eval_ledger SET stale_serve=COALESCE(stale_serve,0)",
    ),
    "eval_cases": (
        "UPDATE eval_cases SET question='' WHERE question IS NULL",
        "UPDATE eval_cases SET case_polarity='positive' WHERE case_polarity IS NULL OR case_polarity=''",
    ),
}


def _quoted_identifier(name: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f'"{name}"'


def _validated_declaration(decl: str) -> str:
    if not isinstance(decl, str) or not decl.strip():
        raise ValueError("column declaration must be non-empty")
    if any(token in decl for token in (";", "--", "/*", "*/", "\x00")):
        raise ValueError("unsafe column declaration")
    return decl.strip()


def _installed_schema_paths() -> tuple[Path, ...]:
    """Return installed schema paths recorded by this distribution, in stable order."""
    try:
        dist = metadata.distribution("rune2")
    except metadata.PackageNotFoundError:
        return ()

    found: set[Path] = set()
    for entry in dist.files or ():
        parts = Path(str(entry).replace("\\", "/")).parts
        if tuple(parts[-len(_INSTALLED_SCHEMA_SUFFIX):]) == _INSTALLED_SCHEMA_SUFFIX:
            found.add(Path(dist.locate_file(entry)).resolve())
    return tuple(sorted(found, key=lambda path: path.as_posix()))


def schema_path() -> Path:
    """Resolve schema from a source checkout or installed wheel data."""
    if _SOURCE_SCHEMA_PATH.is_file():
        return _SOURCE_SCHEMA_PATH
    if _PACKAGED_SCHEMA_PATH.is_file():
        return _PACKAGED_SCHEMA_PATH
    for candidate in _installed_schema_paths():
        if candidate.is_file():
            return candidate
    installed_name = "/".join(_INSTALLED_SCHEMA_SUFFIX)
    raise FileNotFoundError(
        "Rune-2 schema is unavailable: checked source path "
        f"'{_SOURCE_SCHEMA_PATH}' and installed distribution data ending in "
        f"'{installed_name}'"
    )


def load_schema_sql() -> str:
    """Load canonical schema text through the deterministic resolver."""
    return schema_path().read_text(encoding="utf-8")


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__rune2_fts_probe USING fts5(text)")
        conn.execute("DROP TABLE temp.__rune2_fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _schema_with_fts_fallback(sql: str) -> str:
    start_marker = "-- ------------------------------------------------------------------ FTS5"
    end_marker = "-- ------------------------------------------------------------------ answers"
    start = sql.find(start_marker)
    end = sql.find(end_marker, start)
    if start < 0 or end < 0:
        raise RuntimeError("canonical schema FTS section markers are missing")
    fallback = """-- ------------------------------------------------------------------ FTS fallback
CREATE TABLE IF NOT EXISTS blocks_fts (
    rowid INTEGER PRIMARY KEY,
    text TEXT,
    context_header TEXT
);
CREATE TRIGGER IF NOT EXISTS blocks_ai AFTER INSERT ON blocks BEGIN
    INSERT INTO blocks_fts(rowid,text,context_header) VALUES(new.rowid,new.text,new.context_header);
END;
CREATE TRIGGER IF NOT EXISTS blocks_ad AFTER DELETE ON blocks BEGIN
    DELETE FROM blocks_fts WHERE rowid=old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS blocks_au AFTER UPDATE ON blocks BEGIN
    DELETE FROM blocks_fts WHERE rowid=old.rowid;
    INSERT INTO blocks_fts(rowid,text,context_header) VALUES(new.rowid,new.text,new.context_header);
END;

"""
    return sql[:start] + fallback + sql[end:]


def _table_uses_fts5(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='blocks_fts'"
    ).fetchone()
    return bool(row and row[0] and "using fts5" in row[0].lower())


def assert_rebuild_access(cfg: Config) -> None:
    lock = cfg.vault_dir / REBUILD_LOCK_NAME
    if not os.path.lexists(lock):
        return
    if lock.is_symlink() or not lock.is_file():
        raise RuntimeError(f"unsafe rebuild lock refused: {lock}")
    fd = os.open(lock, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        token = os.read(fd, 256).decode("ascii").strip()
    finally:
        os.close(fd)
    if not token or cfg.meta.get("_rebuild_token") != token:
        raise RuntimeError("vault rebuild in progress; database access is temporarily locked")


def validate_db_path(cfg: Config, *, create_parent: bool = False) -> Path:
    """Return a vault-contained DB path after a stable no-follow parent walk."""
    candidate, parent_fd = open_vault_parent(
        cfg.vault_dir, cfg.db_path, create_parent=create_parent,
    )
    try:
        _validate_db_entries(parent_fd, candidate.name, candidate)
    finally:
        os.close(parent_fd)
    return candidate


_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)


def _open_absolute_directory(path: Path) -> int:
    absolute = Path(os.path.abspath(path.expanduser()))
    fd = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_vault_directory(vault_dir: Path) -> tuple[Path, int]:
    """Pin a vault directory through a component-wise no-follow walk."""
    vault = Path(os.path.abspath(Path(vault_dir).expanduser()))
    try:
        return vault, _open_absolute_directory(vault)
    except OSError as exc:
        raise ValueError(f"unsafe vault directory refused: {vault}") from exc


def open_vault_parent(vault_dir: Path, target: Path, *,
                      create_parent: bool = False) -> tuple[Path, int]:
    """Open target's parent beneath a pinned vault descriptor without following symlinks."""
    vault = Path(os.path.abspath(Path(vault_dir).expanduser()))
    configured = Path(target).expanduser()
    candidate = configured if configured.is_absolute() else vault / configured
    candidate = Path(os.path.abspath(candidate))
    try:
        relative_parent = candidate.parent.relative_to(vault)
    except ValueError as exc:
        raise ValueError(f"database path escapes vault: {candidate}") from exc
    _vault, parent_fd = open_vault_directory(vault)
    try:
        for component in relative_parent.parts:
            if create_parent:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            try:
                child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError as exc:
                raise ValueError(f"database directory is missing: {candidate.parent}") from exc
            except OSError as exc:
                raise ValueError(f"symlinked database directory refused: {candidate.parent}") from exc
            os.close(parent_fd)
            parent_fd = child_fd
        return candidate, parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def _validate_db_entries(parent_fd: int, name: str, display: Path) -> None:
    for entry in (name, name + "-wal", name + "-shm"):
        try:
            info = os.stat(entry, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"symlinked database path refused: {display.with_name(entry)}")


def _descriptor_sqlite_path(fd: int) -> str:
    if os.name != "posix":
        raise RuntimeError("Rune-2 secure storage requires a POSIX descriptor filesystem")
    for root in ("/proc/self/fd", "/dev/fd"):
        candidate = f"{root}/{fd}"
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("secure descriptor-based SQLite open requires /proc/self/fd or /dev/fd")


def _pin_database_entry(parent_fd: int, name: str, *, create: bool = False) -> int:
    """Pin an inode without releasing SQLite's process-owned locks on close.

    Closing an ordinary descriptor for a DB or SHM file drops *all* POSIX locks
    this process owns on that inode, including another SQLite connection's
    locks. Linux O_PATH descriptors do not participate in those locks. Keep
    the no-follow inode pin for containment and use it for metadata operations.
    """
    if not hasattr(os, "O_PATH"):
        raise RuntimeError("secure concurrent SQLite storage requires Linux O_PATH")
    if create:
        try:
            # Create the regular file without a temporary data descriptor. Even
            # closing a creation descriptor could disturb a concurrent opener.
            os.mknod(name, stat.S_IFREG | 0o600, dir_fd=parent_fd)
        except FileExistsError:
            pass
    return os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)


def _connect_pinned(fd: int, *, readonly: bool) -> tuple[sqlite3.Connection, tuple[int, int]]:
    """Connect SQLite through an already validated no-follow file descriptor."""
    connection = None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("database path is not a regular file")
        descriptor_path = _descriptor_sqlite_path(fd)
        if not readonly:
            os.chmod(descriptor_path, 0o600)
        mode = "ro" if readonly else "rw"
        connection = sqlite3.connect(f"file:{descriptor_path}?mode={mode}", uri=True)
        identity = (opened.st_dev, opened.st_ino)
        return connection, identity
    except BaseException:
        if connection is not None:
            connection.close()
        raise


class _ReadonlyConnection:
    def __init__(self, connection: sqlite3.Connection, parent_fd: int):
        self._connection = connection
        self._parent_fd = parent_fd

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            if self._parent_fd is not None:
                os.close(self._parent_fd)
                self._parent_fd = None


def open_db_readonly(cfg: Config) -> sqlite3.Connection:
    """Open the selected vault DB read-only through the shared containment barrier."""
    assert_rebuild_access(cfg)
    path, parent_fd = open_vault_parent(cfg.vault_dir, cfg.db_path)
    try:
        _validate_db_entries(parent_fd, path.name, path)
        fd = _pin_database_entry(parent_fd, path.name)
        try:
            connection, _identity = _connect_pinned(fd, readonly=True)
        finally:
            os.close(fd)
    except BaseException:
        os.close(parent_fd)
        raise
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return _ReadonlyConnection(connection, parent_fd)

# author-declared predicate vocabulary (makes refines-vs-contradicts deterministic on typed values).
# Content-neutral defaults for the TEMPLATE; an instance extends this table.
SEED_PREDICATES: list[tuple] = [
    # predicate, inverse, domain_type, range_type, extraction, cardinality, supersede_key
    ("works_at",  "employs",     "person", "org",    "deterministic", "single", "subj+predicate"),
    ("founded",   "founded_by",  "person", "org",    "deterministic", "multi",  "subj+predicate+obj"),
    ("attended",  "attended_by", "person", "org",    "deterministic", "multi",  "subj+predicate+obj"),
    ("cites",     "cited_by",    "doc",    "doc",    "deterministic", "multi",  "subj+predicate+obj"),
    ("refines",   "refined_by",  "concept","concept","deterministic", "multi",  "subj+predicate+obj"),
    ("born_on",   None,          "person", "date",   "deterministic", "single", "subj+predicate"),
    ("located_in","contains",    "org",    "place",  "deterministic", "single", "subj+predicate"),
    ("related_to",None,          "concept","concept","llm-allowed",   "multi",  "subj+predicate+obj"),
]


class DB:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        assert_rebuild_access(cfg)
        db_path, parent_fd = open_vault_parent(
            cfg.vault_dir, cfg.db_path, create_parent=True,
        )
        try:
            _validate_db_entries(parent_fd, db_path.name, db_path)
        except BaseException:
            os.close(parent_fd)
            raise
        self._db_path = db_path
        self._db_parent_fd = parent_fd
        self._closed = False
        try:
            fd = _pin_database_entry(parent_fd, db_path.name, create=True)
            try:
                self.conn, self._db_identity = _connect_pinned(fd, readonly=False)
            finally:
                os.close(fd)
        except BaseException:
            os.close(parent_fd)
            raise
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA journal_mode=WAL")
            # The wiki is a capture of the record, re-drained idempotently, and it commits once
            # per note. In WAL mode NORMAL keeps the database consistent across a crash and only
            # risks the last commits on power loss, which the next drain lands again and doctor
            # reports meanwhile. FULL costs one fsync per note: on a disk-backed root a session of
            # a hundred events overran the 8 second hook ceiling and capture failed every time.
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self._secure_modes()
        except BaseException:
            self.conn.close()
            os.close(parent_fd)
            self._db_parent_fd = None
            self._closed = True
            raise

    def _secure_modes(self) -> None:
        for path in (
            self._db_path,
            self._db_path.with_name(self._db_path.name + "-wal"),
            self._db_path.with_name(self._db_path.name + "-shm"),
        ):
            name = path.name
            try:
                fd = _pin_database_entry(self._db_parent_fd, name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValueError(f"unsafe database sidecar refused: {path}") from exc
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode):
                    raise ValueError(f"database sidecar is not a regular file: {path}")
                if path == self._db_path and (
                    opened.st_dev, opened.st_ino
                ) != self._db_identity:
                    raise RuntimeError("database path identity changed while connection was open")
                os.chmod(_descriptor_sqlite_path(fd), 0o600)
            finally:
                os.close(fd)

    # ---- lifecycle -------------------------------------------------------
    def pour(self) -> None:
        """Apply the schema once and seed meta + predicate vocabulary. Idempotent."""
        self._assert_existing_schema_constraints()
        migrated_before_schema = self._migrate_existing_tables()
        schema_sql = load_schema_sql()
        if not _fts5_available(self.conn):
            schema_sql = _schema_with_fts_fallback(schema_sql)
        self.conn.executescript(schema_sql)
        migrated_after_schema = self._migrate_existing_tables()
        migrated = migrated_before_schema or migrated_after_schema
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if migrated and self._table_exists("blocks_fts") and _table_uses_fts5(self.conn):
                self.conn.execute("INSERT INTO blocks_fts(blocks_fts) VALUES('rebuild')")
            for k, v in self.cfg.meta.items():
                self.conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (k, str(v)))
            self.set_meta("schema_version", str(CURRENT_SCHEMA_VERSION))
            self.conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?,?) "
                "ON CONFLICT(version) DO UPDATE SET applied_at=excluded.applied_at",
                (CURRENT_SCHEMA_VERSION, f"schema-{CURRENT_SCHEMA_VERSION}"),
            )
            self.seed_predicates()
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.commit()
            self._secure_modes()

    def _migrate_existing_tables(self) -> bool:
        existing = [table for table in _MIGRATION_COLUMNS if self._table_exists(table)]
        if not existing:
            return False
        changed: set[str] = set()
        added: dict[str, set[str]] = {}
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for table in existing:
                for column, decl in _MIGRATION_COLUMNS[table]:
                    if self._add_column(table, column, decl):
                        changed.add(table)
                        added.setdefault(table, set()).add(column)
            if "privacy_scanned" in added.get("docs", set()):
                self.conn.execute("UPDATE docs SET private=1, privacy_scanned=0")
            for table in sorted(changed):
                for sql in _MIGRATION_BACKFILLS.get(table, ()):
                    self.conn.execute(sql)
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.commit()
            self._secure_modes()
        return bool(changed)

    def _assert_existing_schema_constraints(self) -> None:
        """Refuse additive migration when SQLite cannot preserve required physical laws."""
        existing = {table for table in _REQUIRED_NOT_NULL if self._table_exists(table)}
        for table in sorted(existing):
            info = self.conn.execute(f"PRAGMA table_info({_quoted_identifier(table)})").fetchall()
            columns = {row["name"] for row in info}
            not_null = {row["name"] for row in info if row["notnull"]}
            missing_not_null = _REQUIRED_NOT_NULL[table] - not_null
            if missing_not_null:
                raise UnsafeLegacySchema(
                    f"unsafe legacy {table} table lacks NOT NULL constraints; rebuild required"
                )

            required_fks = _REQUIRED_FOREIGN_KEYS.get(table, set())
            if required_fks:
                actual_fks = {
                    (row["from"], row["table"], row["to"])
                    for row in self.conn.execute(
                        f"PRAGMA foreign_key_list({_quoted_identifier(table)})"
                    ).fetchall()
                }
                if not required_fks <= actual_fks:
                    raise UnsafeLegacySchema(
                        f"unsafe legacy {table} table lacks foreign keys; rebuild required"
                    )

            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            compact_sql = re.sub(r"\s+", "", (row["sql"] or "").lower()) if row else ""
            required_checks = _REQUIRED_COLUMN_CHECKS[table]
            if any(
                column in columns and fragment not in compact_sql
                for column, fragment in required_checks.items()
            ):
                raise UnsafeLegacySchema(
                    f"unsafe legacy {table} table lacks CHECK constraints; rebuild required"
                )

        if "docs" in existing:
            path_unique = False
            for index in self.conn.execute("PRAGMA index_list('docs')").fetchall():
                if not index["unique"] or index["partial"]:
                    continue
                columns = [
                    row["name"] for row in self.conn.execute(
                        f"PRAGMA index_info({_quoted_identifier(index['name'])})"
                    ).fetchall()
                ]
                if columns == ["path"]:
                    path_unique = True
                    break
            if not path_unique:
                raise UnsafeLegacySchema(
                    "unsafe legacy docs table lacks unique path constraint; rebuild required"
                )

    def seed_predicates(self) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO predicates"
            "(predicate,inverse,domain_type,range_type,extraction,cardinality,supersede_key)"
            " VALUES (?,?,?,?,?,?,?)",
            SEED_PREDICATES,
        )

    def migrate_column(self, table: str, column: str, decl: str) -> None:
        """Additive per-column ALTER, guarded against the known-columns list."""
        _quoted_identifier(table)
        _quoted_identifier(column)
        _validated_declaration(decl)
        own_transaction = not self.conn.in_transaction
        if own_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._add_column(table, column, decl)
        except BaseException:
            if own_transaction:
                self.conn.rollback()
            raise
        else:
            if own_transaction:
                self.commit()
                self._secure_modes()

    def _add_column(self, table: str, column: str, decl: str) -> bool:
        table_sql = _quoted_identifier(table)
        column_sql = _quoted_identifier(column)
        declaration = _validated_declaration(decl)
        if column in self._table_columns(table):
            return False
        self.conn.execute(f"ALTER TABLE {table_sql} ADD COLUMN {column_sql} {declaration}")
        return True

    # ---- meta ------------------------------------------------------------
    def get_meta(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---- helpers ---------------------------------------------------------
    def _table_columns(self, table: str) -> set[str]:
        table_sql = _quoted_identifier(table)
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table_sql})")}

    def _table_exists(self, name: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def execute(self, sql: str, params: Iterable[Any] = ()):
        return self.conn.execute(sql, tuple(params))

    def commit(self) -> None:
        try:
            assert_rebuild_access(self.cfg)
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()
        self._secure_modes()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.conn.close()
            self._secure_modes()
        finally:
            if self._db_parent_fd is not None:
                os.close(self._db_parent_fd)
                self._db_parent_fd = None
            self._closed = True
