-- The record schema, current version. Pure ASCII by rule (no non-ASCII byte).
--
-- This file is the one canonical statement of the record's tables and indexes. It is
-- applied idempotently: every object is CREATE ... IF NOT EXISTS, so running it against
-- a fresh store creates everything and running it against a live store is a no-op. The
-- additive column deltas (ALTER TABLE ADD COLUMN) and the backfills live in alpaca/migrate.py,
-- not here, because SQLite cannot express "add this column only if absent" in DDL. The
-- BEFORE DELETE append-only guards are NOT in this file: they are installed by
-- migrate.install_append_only_guards() so that ad-hoc, unmanaged stores (used by the
-- property fuzzer to model raw out-of-band access) can stay guard-free.
--
-- Rule 2 of the harness (append-only, hash-chained events) and the fail-closed refusal
-- of a mangled store are enforced in alpaca/migrate.py against the constraints declared here.

-- events: append-only, hash-chained. The NOT NULL columns below are the required set the
-- migration verifies on every open; a store missing any of them is refused fail-closed.
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, session TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
  op TEXT, ref TEXT, data TEXT NOT NULL,
  content_hash TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL, where_ TEXT);

CREATE TABLE IF NOT EXISTS sessions (
  sid TEXT PRIMARY KEY, started TEXT, ended TEXT, cwd TEXT, transcript TEXT,
  level TEXT, last_beat TEXT, beats INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS ops (
  id TEXT PRIMARY KEY, intent TEXT NOT NULL, done_when TEXT, status TEXT NOT NULL,
  opened TEXT, closed TEXT, phases TEXT);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, op TEXT, phase TEXT, statement TEXT NOT NULL, status TEXT NOT NULL,
  proof TEXT, where_ TEXT, why TEXT, claimant TEXT, lease_until TEXT, created TEXT, updated TEXT,
  title TEXT);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session TEXT, sender TEXT, to_ TEXT,
  kind TEXT, body TEXT, ref TEXT);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session TEXT, actor TEXT, kind TEXT, body TEXT, ref TEXT);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- rows: the obligation rows synthesized from a phase step model (M1.10). M2.1 adds the
-- forward supersession pointer `superseded_by` additively; the ALTER and its backfill from
-- the existing `supersedes` links live in migrate.py.
CREATE TABLE IF NOT EXISTS rows (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, op TEXT, phase TEXT, step TEXT,
  statement TEXT, proof TEXT, where_ TEXT, how TEXT, when_ TEXT, why TEXT,
  session TEXT, operator TEXT, status TEXT, tag TEXT,
  content_hash TEXT, prev_hash TEXT, supersedes TEXT, superseded_by TEXT);

-- run: the instrument census, one row per gate execution. M2.1 lands the table and
-- backfills it from the historic kind='run' events. The CHECK on code is a required
-- constraint the migration verifies: a run table that lost it is refused by name.
CREATE TABLE IF NOT EXISTS run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER, ts TEXT, gate TEXT, code INTEGER NOT NULL,
  verdict TEXT, reason TEXT, evidence TEXT,
  CHECK (code >= 0));

-- page: decision and wiki pages (populated by M2.6 and the wiki tasks). Landed here empty.
CREATE TABLE IF NOT EXISTS page (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, body TEXT, pointer TEXT, created TEXT);

CREATE INDEX IF NOT EXISTS idx_rows_op ON rows (op);
CREATE INDEX IF NOT EXISTS idx_rows_session ON rows (session);
CREATE INDEX IF NOT EXISTS idx_rows_operator ON rows (operator);
CREATE INDEX IF NOT EXISTS idx_run_gate ON run (gate);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind);

CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session, id);

-- Additive observability catalog. Raw payloads stay in authorized source files.
CREATE TABLE IF NOT EXISTS schema_migration (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS obs_source (
  source_id TEXT PRIMARY KEY, session TEXT NOT NULL, parent_session TEXT,
  provider TEXT NOT NULL, kind TEXT NOT NULL, locator TEXT, native_id TEXT,
  capabilities TEXT NOT NULL DEFAULT '{}', required INTEGER NOT NULL DEFAULT 1,
  closed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_obs_source_session ON obs_source(session);
CREATE TABLE IF NOT EXISTS obs_generation (
  source_id TEXT NOT NULL, generation INTEGER NOT NULL, identity TEXT NOT NULL,
  prefix_bytes INTEGER NOT NULL DEFAULT 0, prefix_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(source_id, generation));
CREATE TABLE IF NOT EXISTS obs_cursor (
  source_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, byte_offset INTEGER NOT NULL,
  line_number INTEGER NOT NULL, updated_at TEXT NOT NULL,
  CHECK(byte_offset >= 0), CHECK(line_number >= 0));
CREATE TABLE IF NOT EXISTS obs_observation (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, generation INTEGER NOT NULL,
  byte_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, line_number INTEGER NOT NULL,
  native_id TEXT, kind TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
  observed_at TEXT NOT NULL, source_time TEXT, facts TEXT NOT NULL,
  UNIQUE(source_id, generation, byte_offset));
CREATE INDEX IF NOT EXISTS idx_obs_native ON obs_observation(source_id, native_id);
CREATE TABLE IF NOT EXISTS obs_issue (
  issue_id TEXT PRIMARY KEY, source_id TEXT, code TEXT NOT NULL, detail TEXT NOT NULL,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
  resolved_at TEXT);
CREATE TABLE IF NOT EXISTS obs_consumer_checkpoint (
  consumer TEXT PRIMARY KEY, event_id INTEGER NOT NULL DEFAULT 0,
  observation_id INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
  failures INTEGER NOT NULL DEFAULT 0, next_attempt TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS obs_hook_outcome (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, hook TEXT NOT NULL,
  outcome TEXT NOT NULL, duration_ms REAL NOT NULL, recorded_at TEXT NOT NULL,
  error_type TEXT);
CREATE INDEX IF NOT EXISTS idx_obs_hook_session ON obs_hook_outcome(session, id);
CREATE TABLE IF NOT EXISTS obs_projection_job (
  job_key TEXT PRIMARY KEY, kind TEXT NOT NULL, session TEXT NOT NULL,
  event_id INTEGER NOT NULL DEFAULT 0, observation_id INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
  next_attempt TEXT, error TEXT, updated_at TEXT NOT NULL);
