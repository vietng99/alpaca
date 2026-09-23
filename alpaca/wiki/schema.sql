-- Rune-2 canonical schema: section 3 of blueprint-body.md, poured to the ruled shape.
-- Invariants pushed into the substrate (cannot rot by discipline):
--   (1) edges.source_block_id NOT NULL  -> R3 no-anchor-no-edge, enforced at schema level.
--   (2) edge_id is CONTENT-DERIVED (Op-3-Keystone) -> idempotent across re-chunk.
--   (3) FOUR bitemporal axes on edges + nodes + blocks (D-1): recorded_at/superseded_at
--       (transaction) kept forever distinct from valid_from/valid_until (domain).
--   (4) ingest_event carries a SHA-256 hash-chain (OWD-2) -> detect-only corruption self-audit.
-- This file is applied ONCE (see schema_migrations). Vector virtual tables (vec_blocks/vec_nodes)
-- are created at runtime by store/vec.py because they depend on the optional sqlite-vec extension;
-- a stdlib fallback table (vec_blocks_fallback / vec_nodes_fallback) is used when it is absent.

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------------ meta / migrations
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);  -- schema_version, embed_model, embed_dim, embed_version, rerank_model, entail_models,
    -- ppr_alpha, ppr_iters, ppr_epsilon, rrf_k, determinism_seed, sqlite_vec_version

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- ------------------------------------------------------------------ documents
CREATE TABLE IF NOT EXISTS docs (
    doc_id           TEXT PRIMARY KEY,
    path             TEXT UNIQUE NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('raw','wiki')),
    content_sha256   TEXT NOT NULL,
    byte_len         INTEGER,
    mtime            TEXT,
    git_commit       TEXT,
    source_authority INTEGER DEFAULT 0,
    immutable        INTEGER DEFAULT 1,
    ingested_at      TEXT,
    domain           TEXT NOT NULL DEFAULT 'general'
                     CHECK (domain IN ('embedded','career','general','personal')),  -- os.5 compartment
    tier             INTEGER NOT NULL DEFAULT 1,          -- source tier for the raw-last-resort gate (r1.1)
    source_created_at TEXT,                               -- the source's OWN authored date (km.4 learned/about)
    sensitivity      TEXT DEFAULT 'none',                 -- raise-only marking tripwire label (os.4 firewall)
    private          INTEGER NOT NULL DEFAULT 1,          -- fail closed until trusted classification
    privacy_scanned  INTEGER NOT NULL DEFAULT 0           -- Writer sets 1 after path/content classification
);

-- ------------------------------------------------------------------ blocks (smallest citable unit)
CREATE TABLE IF NOT EXISTS blocks (
    block_id             TEXT PRIMARY KEY,            -- DISPLAY/FK locator = doc_id||'#'||ordinal; EXCLUDED from edge identity
    block_content_id     TEXT NOT NULL,              -- CONTENT anchor folded into edge_id: sha256(normalized_text) + (doc_id, occurrence_index)
    occurrence_index     INTEGER NOT NULL DEFAULT 0, -- disambiguates identical text within a doc (boilerplate), NOT char-spans
    chunk_ruleset_version TEXT,                       -- PINNED metadata: which chunker produced this block. Recorded, NEVER hashed.
    doc_id               TEXT NOT NULL REFERENCES docs(doc_id),
    ordinal              INTEGER NOT NULL,
    heading_path         TEXT,
    char_start           INTEGER,
    char_end             INTEGER,
    block_sha256         TEXT,                        -- incremental re-embed/re-extract cache key
    block_type           TEXT,
    context_header       TEXT,                        -- Anthropic contextual-retrieval blurb (indexed, never a claim)
    text                 TEXT NOT NULL,
    embedded_model       TEXT,
    valid_from           TEXT NOT NULL,               -- BITEMPORAL per BLOCK (D-1b)
    valid_until          TEXT,
    recorded_at          TEXT NOT NULL,
    superseded_at        TEXT,
    domain               TEXT NOT NULL DEFAULT 'general'   -- os.5 compartment (propagated from docs)
                         CHECK (domain IN ('embedded','career','general','personal')),
    source_created_at    TEXT,                             -- km.4 the source's own authored date
    status               TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded'))
);
CREATE INDEX IF NOT EXISTS idx_blocks_doc     ON blocks(doc_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_blocks_content ON blocks(block_content_id);
CREATE INDEX IF NOT EXISTS idx_blocks_status  ON blocks(status, valid_from);

-- ------------------------------------------------------------------ nodes (entities)
CREATE TABLE IF NOT EXISTS nodes (
    node_id           TEXT PRIMARY KEY,               -- canonical slug
    canonical_node_id TEXT REFERENCES nodes(node_id), -- MERGE redirect (OWD-7): path-compressed depth-1; NULL = self canonical
    node_type         TEXT,
    display_name      TEXT,
    summary_block_id  TEXT REFERENCES blocks(block_id),
    pagerank_prior    REAL DEFAULT 0.0,
    pagerank          REAL DEFAULT 0.0,
    page_band         INTEGER,
    degree            INTEGER DEFAULT 0,
    attrs             TEXT,                            -- JSON
    valid_from        TEXT NOT NULL,                   -- BITEMPORAL per NODE (D-1b): rename/supersede
    valid_until       TEXT,
    recorded_at       TEXT NOT NULL,
    superseded_at     TEXT,
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','merged'))
);
CREATE INDEX IF NOT EXISTS idx_nodes_canonical ON nodes(canonical_node_id);
CREATE INDEX IF NOT EXISTS idx_nodes_status    ON nodes(status);

-- ------------------------------------------------------------------ aliases
CREATE TABLE IF NOT EXISTS aliases (
    alias_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id         TEXT NOT NULL REFERENCES nodes(node_id),
    surface         TEXT NOT NULL,
    norm_surface    TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('exact','wikilink','declared','near-advisory')),
    status          TEXT NOT NULL DEFAULT 'bound' CHECK (status IN ('bound','candidate')),
    confidence      REAL DEFAULT 1.0,
    source_block_id TEXT REFERENCES blocks(block_id),
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_aliases_norm ON aliases(norm_surface);
CREATE INDEX IF NOT EXISTS idx_aliases_node ON aliases(node_id);

-- ------------------------------------------------------------------ predicate vocabulary
CREATE TABLE IF NOT EXISTS predicates (
    predicate     TEXT PRIMARY KEY,
    inverse       TEXT,
    domain_type   TEXT,
    range_type    TEXT,
    extraction    TEXT NOT NULL CHECK (extraction IN ('deterministic','llm-allowed')),
    cardinality   TEXT NOT NULL CHECK (cardinality IN ('single','multi')),
    supersede_key TEXT
);

-- ------------------------------------------------------------------ edges (the ASSERTION: atomic unit)
CREATE TABLE IF NOT EXISTS edges (
    edge_id               TEXT PRIMARY KEY,            -- CONTENT-DERIVED: hash(subj_node, predicate, obj_key, block_content_id)
    subj_node             TEXT NOT NULL REFERENCES nodes(node_id),
    predicate             TEXT NOT NULL REFERENCES predicates(predicate),
    obj_node              TEXT REFERENCES nodes(node_id),
    obj_literal           TEXT,
    obj_datatype          TEXT NOT NULL CHECK (obj_datatype IN ('node','date','number','string','bool')),
    source_block_id       TEXT NOT NULL REFERENCES blocks(block_id),   -- R3 anchor + human locator (NOT in edge_id identity)
    source_doc_id         TEXT REFERENCES docs(doc_id),
    source_quote          TEXT NOT NULL,               -- verbatim cited span; deterministic middle re-verifies byte-for-byte
    extractor             TEXT NOT NULL CHECK (extractor IN ('deterministic','llm','reflection')),
    extractor_version     TEXT,
    confidence            REAL DEFAULT 1.0,
    reconcile_verdict     TEXT CHECK (reconcile_verdict IN ('novel','refines','contradicts','duplicate','matches')),
    corroboration_count   INTEGER NOT NULL DEFAULT 1,
    recorded_at           TEXT NOT NULL,               -- TRANSACTION axis: when the KB wrote it
    superseded_at         TEXT,                        -- TRANSACTION axis: when the KB stopped believing it (PKT-2 in-DB)
    valid_from            TEXT NOT NULL,               -- DOMAIN axis: true-in-world window start
    valid_until           TEXT,                        -- DOMAIN axis: true-in-world window end
    supersedes_edge_id    TEXT,
    superseded_by_edge_id TEXT,
    retraction_reason     TEXT,                        -- cure-protocol tombstone WHY (D-1)
    retracted_by          TEXT,                        -- cure-protocol tombstone WHO (D-1)
    status                TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','invalidated','quarantined','retracted')),
    edge_tier             INTEGER,                     -- ADR-023 axis, distinct from page_band
    atom_type             TEXT NOT NULL DEFAULT 'FACT' -- pc.3 typed atom split
                          CHECK (atom_type IN ('FACT','TAKE','ENTITY','FULLTEXT')),
    learned_at            TEXT,                        -- km.4 capture date (= source_created_at); distinct from valid_from(about)
    source_created_at     TEXT,                        -- km.4 the source's own authored date (relative-time anchor)
    expires_at            TEXT,                        -- km.6 shelf-life
    volatile              INTEGER NOT NULL DEFAULT 0   -- km.6 volatile flag
);
-- FROZEN four-axis as-of predicate (store/asof.py is the single builder of this clause):
--   valid_from<=:T AND (valid_until IS NULL OR :T<valid_until)
--   AND recorded_at<=:T AND (superseded_at IS NULL OR :T<superseded_at)   [default query fixes txn axis at latest]
CREATE INDEX IF NOT EXISTS idx_edges_subj      ON edges(subj_node, predicate, valid_from, valid_until);
CREATE INDEX IF NOT EXISTS idx_edges_obj       ON edges(obj_node);
CREATE INDEX IF NOT EXISTS idx_edges_superseded ON edges(superseded_at);
CREATE INDEX IF NOT EXISTS idx_edges_status    ON edges(status, valid_from);
CREATE INDEX IF NOT EXISTS idx_edges_srcblock  ON edges(source_block_id);

-- entity<->passage PPR mass projection (HippoRAG-2 graft)
CREATE TABLE IF NOT EXISTS node_blocks (
    node_id  TEXT NOT NULL REFERENCES nodes(node_id),
    block_id TEXT NOT NULL REFERENCES blocks(block_id),
    role     TEXT NOT NULL CHECK (role IN ('mention','definition','claim')),
    weight   REAL DEFAULT 1.0,
    PRIMARY KEY (node_id, block_id, role)
);
CREATE INDEX IF NOT EXISTS idx_node_blocks_block ON node_blocks(block_id);

-- which distinct source blocks have corroborated each edge (F9/F10, Operation-4-Litmus).
-- Makes 'matches' corroboration idempotent by construction (bump only on a genuinely NEW source)
-- and lets the CURE protocol decrement corroboration on edges a retracted poison had inflated.
CREATE TABLE IF NOT EXISTS edge_corroborations (
    edge_id         TEXT NOT NULL REFERENCES edges(edge_id),
    source_block_id TEXT NOT NULL,
    recorded_at     TEXT,
    PRIMARY KEY (edge_id, source_block_id)
);
CREATE INDEX IF NOT EXISTS idx_corrob_src ON edge_corroborations(source_block_id);

-- as-of helper view (the SQL builder parameterizes by :T; kept simple/portable)
CREATE VIEW IF NOT EXISTS edges_active AS
    SELECT * FROM edges WHERE status = 'active';

-- ------------------------------------------------------------------ FTS5 (BM25 lexical arm)
CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
    text, context_header,
    content='blocks', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS blocks_ai AFTER INSERT ON blocks BEGIN
    INSERT INTO blocks_fts(rowid, text, context_header) VALUES (new.rowid, new.text, new.context_header);
END;
CREATE TRIGGER IF NOT EXISTS blocks_ad AFTER DELETE ON blocks BEGIN
    INSERT INTO blocks_fts(blocks_fts, rowid, text, context_header) VALUES ('delete', old.rowid, old.text, old.context_header);
END;
CREATE TRIGGER IF NOT EXISTS blocks_au AFTER UPDATE ON blocks BEGIN
    INSERT INTO blocks_fts(blocks_fts, rowid, text, context_header) VALUES ('delete', old.rowid, old.text, old.context_header);
    INSERT INTO blocks_fts(rowid, text, context_header) VALUES (new.rowid, new.text, new.context_header);
END;

-- ------------------------------------------------------------------ answers / claims / eval ledger
CREATE TABLE IF NOT EXISTS answers (
    answer_id         TEXT PRIMARY KEY,
    question          TEXT NOT NULL,
    query_hash        TEXT,
    as_of             TEXT,
    txn_axis          TEXT,
    qtype             TEXT,
    verdict           TEXT NOT NULL CHECK (verdict IN ('grounded','conflict','abstained')),
    answer_text       TEXT,
    success_predicate TEXT,       -- JSON: selected template ids + params
    verdict_dag       TEXT,       -- JSON
    retrieval_trace   TEXT,       -- JSON
    channels          TEXT,       -- JSON
    model_versions    TEXT,       -- JSON
    determinism_hash  TEXT,
    fast_path         INTEGER DEFAULT 0,
    created_at        TEXT
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id         TEXT PRIMARY KEY,
    answer_id        TEXT NOT NULL REFERENCES answers(answer_id),
    claim_text       TEXT NOT NULL,
    claim_kind       TEXT NOT NULL CHECK (claim_kind IN ('prose','number','date','count','citation')),
    source_block_id  TEXT REFERENCES blocks(block_id),
    source_edge_id   TEXT REFERENCES edges(edge_id),
    entailment_score REAL,
    checker          TEXT,
    verdict          TEXT NOT NULL CHECK (verdict IN ('supported','unsupported','contradicted')),
    created_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_answer ON claims(answer_id);

CREATE TABLE IF NOT EXISTS eval_ledger (
    query_id     TEXT PRIMARY KEY,
    ts           TEXT,
    qtype        TEXT,
    channels     TEXT,
    verdict      TEXT,
    n_citations  INTEGER,
    latency_ms   INTEGER,
    abstained    INTEGER,
    fast_path    INTEGER,
    asof_gated   INTEGER DEFAULT 0,      -- km.5: the as-of gate excluded a superseded/expired row
    stale_serve  INTEGER DEFAULT 0       -- km.5: a stale claim was served (must stay 0 unless flagged)
);

-- ------------------------------------------------------------------ ingest_event (HASH-CHAIN: OWD-2)
CREATE TABLE IF NOT EXISTS ingest_event (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    seq          INTEGER NOT NULL UNIQUE,      -- gap-free monotonic (single-writer curator-gated tx)
    prev_checksum TEXT NOT NULL,
    checksum     TEXT NOT NULL,                -- SHA-256 over frozen concat: excludes post-insert-mutable fields, includes immutable PKs
    ts           TEXT,
    doc_id       TEXT,
    op           TEXT NOT NULL CHECK (op IN ('upsert_node','upsert_edge','supersede_edge','invalidate','merge','cure')),
    payload      TEXT,                          -- JSON
    git_commit   TEXT
);  -- mirrored to ledger/events.jsonl = CO-AUTHORITATIVE replay spine (D-1); DB<->ledger reconcile detects holes, raw arbitrates

-- ------------------------------------------------------------------ merge log (OWD-7: append-only)
CREATE TABLE IF NOT EXISTS merge_events (
    merge_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    from_node     TEXT NOT NULL,
    to_node       TEXT NOT NULL,
    method        TEXT,
    decided_by    TEXT,
    ts            TEXT,
    originals_json TEXT                          -- full pre-merge state embedded; supersede-not-relabel
);

CREATE TABLE IF NOT EXISTS merge_candidate (
    cand_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    node_a      TEXT NOT NULL,
    node_b      TEXT NOT NULL,
    method      TEXT,
    score       REAL,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    surfaced_at TEXT
);

CREATE TABLE IF NOT EXISTS reflections (
    run_id          TEXT PRIMARY KEY,
    ts              TEXT,
    kind            TEXT,
    diff_summary    TEXT,        -- JSON
    blast_new       INTEGER,
    blast_updated   INTEGER,
    blast_deprecated INTEGER,
    gate_result     TEXT,
    git_rev         TEXT
);

-- op.9 Dream: the SIGNED, REPLAYABLE diary of every consolidation pass (applied / held / paused).
-- Each row is a tamper-evident, hash-chained record: base_sha pins the store state the pass started
-- from, the REAL blast counts + exact diff make it replayable, per-item gate verdicts explain the
-- guardrail decision, and record_hash (folding prev_hash) chains the diary. Disarmed passes emit
-- nothing (no work, no diary noise). recorded_at (ts) is advisory and is EXCLUDED from record_hash.
CREATE TABLE IF NOT EXISTS dream_records (
    record_seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT UNIQUE NOT NULL,
    ts               TEXT,                 -- advisory wall-clock; never hashed
    base_sha         TEXT NOT NULL,        -- REAL store-state digest BEFORE the pass
    result_sha       TEXT NOT NULL,        -- store-state digest AFTER the pass (replay target)
    status           TEXT NOT NULL,        -- applied | held | paused
    reason           TEXT,                 -- why the guardrail decided as it did
    blast_new        INTEGER NOT NULL DEFAULT 0,
    blast_updated    INTEGER NOT NULL DEFAULT 0,
    blast_deprecated INTEGER NOT NULL DEFAULT 0,
    diff_json        TEXT NOT NULL,        -- exact diff (new/updated/deprecated/planned edge_ids)
    gate_json        TEXT NOT NULL,        -- per-item gate verdicts
    prev_hash        TEXT NOT NULL,        -- chains to the prior record's record_hash (GENESIS if first)
    record_hash      TEXT NOT NULL         -- SHA256(tag + JCS(signed fields)); the signature
);

-- ------------------------------------------------------------------ metamemory / eval
CREATE TABLE IF NOT EXISTS source_freshness (
    target_kind  TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    last_checked TEXT,
    verdict      TEXT CHECK (verdict IN ('FRESH','DRIFTED','ORPHANED','UNCHECKABLE')),
    PRIMARY KEY (target_kind, target_id)
);

CREATE TABLE IF NOT EXISTS eval_cases (
    case_id         TEXT PRIMARY KEY,
    question        TEXT NOT NULL,
    expected        TEXT,
    gold_block_ids  TEXT,         -- JSON
    qtype           TEXT,
    source          TEXT,
    mutation_target TEXT,         -- dm.12 Mimir: the change that flips this case to FAIL
    case_polarity   TEXT NOT NULL DEFAULT 'positive'   -- dm.12: positive|negative(abstain-not-invent)
                    CHECK (case_polarity IN ('positive','negative'))
);

-- gap ledger (os.8 /lucid + dm.7 artifact share detectors). content-derived stable gap_id.
CREATE TABLE IF NOT EXISTS gaps (
    gap_id         TEXT PRIMARY KEY,     -- SHA256(tag + JCS{type, subject_key, detail_key})
    gap_type       TEXT NOT NULL,        -- DANGLING_SOURCE|UNCORROBORATED_CLASS_A|OPEN_CONTRADICTION|...
    subject_key    TEXT,
    detail         TEXT,
    severity       INTEGER NOT NULL DEFAULT 3 CHECK (severity BETWEEN 1 AND 5),
    distinct_referrers INTEGER NOT NULL DEFAULT 1,
    first_seen_seq INTEGER,
    last_seen_seq  INTEGER,
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','deferred')),
    evidence_ref   TEXT,
    created_at     TEXT
);

-- signed replayable Dream record (os.9). itself a hash-chain (prev_dream_sha).
CREATE TABLE IF NOT EXISTS dream_runs (
    dream_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    base_sha       TEXT NOT NULL,        -- ingest_event chain checksum at run start
    prev_dream_sha TEXT NOT NULL,
    record_sha     TEXT NOT NULL,        -- SHA256(tag + JCS(all fields except started_at & record_sha))
    started_at     TEXT,                 -- advisory, EXCLUDED from record_sha
    diff_json      TEXT,                 -- sorted diff
    gates_json     TEXT,                 -- per-gate PASS/FAIL + evidence
    health         INTEGER,
    algo_versions  TEXT,
    gate_result    TEXT NOT NULL DEFAULT 'ok' CHECK (gate_result IN ('ok','HELD','PAUSED'))
);

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id         TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL REFERENCES eval_cases(case_id),
    run_at         TEXT,
    passed         INTEGER,
    verdict        TEXT,
    model_versions TEXT
);

CREATE TABLE IF NOT EXISTS communities (
    comm_id TEXT PRIMARY KEY,
    node_id TEXT REFERENCES nodes(node_id),
    label   TEXT,
    level   INTEGER
);  -- OPTIONAL, off PPR hot path (open sub-decision #6)

-- km.7 HUB-MAP summary: one non-blank human summary per community (min-node label), rebuilt
-- byte-deterministically alongside the partition. The answer/lineage path reads this (never the LLM).
CREATE TABLE IF NOT EXISTS community_hubs (
    label       TEXT PRIMARY KEY,     -- community_id = MIN node_id (relabeling-invariant)
    hub_node    TEXT,                 -- deterministic hub: max in-community degree, tie-break node_id ASC
    size        INTEGER NOT NULL,     -- member count (integer only; never a float on the hash surface)
    summary     TEXT NOT NULL,        -- non-blank HUB-MAP one-liner
    recorded_at TEXT                  -- advisory wall-clock; EXCLUDED from hub_hash
);

-- km.7 VERSIONED tier/community-change EVENT ledger: append-only, seq-versioned AND hash-chained.
-- retier / recompute_communities emit ONE row per node whose page_band or community membership
-- actually changed (a no-op rebuild emits nothing). The lineage path can replay a node's history.
CREATE TABLE IF NOT EXISTS tier_events (
    event_seq   INTEGER PRIMARY KEY,  -- monotonic version (single-writer, gap-free)
    node_id     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('tier','community')),
    old_value   TEXT,                 -- prior value (NULL == first assignment)
    new_value   TEXT,
    prev_hash   TEXT NOT NULL,        -- previous event_hash (chain anchor)
    event_hash  TEXT NOT NULL,        -- SHA256(tag + JCS{seq,node_id,kind,old,new,prev}) -> versioned
    recorded_at TEXT                  -- advisory; EXCLUDED from event_hash
);
CREATE INDEX IF NOT EXISTS idx_tier_events_node ON tier_events(node_id, event_seq);

-- ------------------------------------------------------------------ deprecate-not-delete tombstone triggers (km.1)
-- The authoritative graph is append-only: a hard DELETE on an assertion/entity/block is forbidden.
-- Supersession (superseded_at) and retraction (status='retracted') are the ONLY ways an edge leaves
-- the active set, preserving full bitemporal history. Poison removal is a cure-protocol RETRACT (D-1).
CREATE TRIGGER IF NOT EXISTS no_delete_edges BEFORE DELETE ON edges
    BEGIN SELECT RAISE(ABORT, 'deprecate-not-delete: edges are append-only (supersede or retract)'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_nodes BEFORE DELETE ON nodes
    BEGIN SELECT RAISE(ABORT, 'deprecate-not-delete: nodes are append-only (supersede or merge)'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_blocks BEFORE DELETE ON blocks
    BEGIN SELECT RAISE(ABORT, 'deprecate-not-delete: blocks are append-only (supersede)'); END;
