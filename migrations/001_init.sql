-- tool.db initial schema for Toronto address import tool
-- WAL enabled at connect time; foreign keys on.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    bbox_min_lat        REAL NOT NULL,
    bbox_min_lon        REAL NOT NULL,
    bbox_max_lat        REAL NOT NULL,
    bbox_max_lon        REAL NOT NULL,
    source_snapshot_id  INTEGER,
    created_at          TEXT NOT NULL,
    config_json         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    run_id              INTEGER NOT NULL REFERENCES runs(run_id),
    candidate_id        INTEGER NOT NULL,     -- address_point_id from source
    address_full        TEXT,
    housenumber         TEXT,
    street_raw          TEXT,
    street_norm         TEXT,
    lat                 REAL,
    lon                 REAL,
    lo_num              INTEGER,
    lo_num_suf          TEXT,
    hi_num              INTEGER,
    hi_num_suf          TEXT,
    extra_json          TEXT,
    stage               TEXT NOT NULL,        -- INGESTED / CONFLATED / CHECKED / REVIEW_PENDING / APPROVED / REJECTED / BATCHED / UPLOADED / FAILED / SKIPPED
    stage_updated_at    TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_stage ON candidates(run_id, stage);

CREATE TABLE IF NOT EXISTS conflation (
    run_id              INTEGER NOT NULL,
    candidate_id        INTEGER NOT NULL,
    verdict             TEXT NOT NULL,        -- MATCH / MISSING / CONFLICT
    nearest_osm_id      INTEGER,
    nearest_osm_type    TEXT,
    nearest_dist_m      REAL,
    osm_snapshot_hash   TEXT,
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS checks_catalog (
    check_id            TEXT PRIMARY KEY,
    version             INTEGER NOT NULL,
    enabled_default     INTEGER NOT NULL,
    description         TEXT
);

CREATE TABLE IF NOT EXISTS check_toggles (
    run_id              INTEGER NOT NULL REFERENCES runs(run_id),
    check_id            TEXT NOT NULL,
    enabled             INTEGER NOT NULL,
    PRIMARY KEY (run_id, check_id)
);

CREATE TABLE IF NOT EXISTS check_results (
    run_id              INTEGER NOT NULL,
    candidate_id        INTEGER NOT NULL,
    check_id            TEXT NOT NULL,
    check_version       INTEGER NOT NULL,
    verdict             TEXT NOT NULL,        -- PASS / FLAG / SKIP
    severity            TEXT,
    reason_code         TEXT,
    details_json        TEXT,
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id, check_id, check_version)
);
CREATE INDEX IF NOT EXISTS idx_check_results_lookup ON check_results(run_id, check_id, check_version);

CREATE TABLE IF NOT EXISTS review_items (
    run_id              INTEGER NOT NULL,
    candidate_id        INTEGER NOT NULL,
    reason_code         TEXT NOT NULL,
    status              TEXT NOT NULL,        -- OPEN / APPROVED / REJECTED / DEFERRED
    note                TEXT,
    opened_at           TEXT NOT NULL,
    resolved_at         TEXT,
    PRIMARY KEY (run_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_items(run_id, status);

CREATE TABLE IF NOT EXISTS batches (
    batch_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES runs(run_id),
    mode                TEXT NOT NULL CHECK (mode IN ('osm_api', 'josm_xml')),
    status              TEXT NOT NULL,        -- draft / uploading / uploaded / failed / needs_attention
    size                INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    uploaded_at         TEXT,
    changeset_id        INTEGER,
    client_token        TEXT NOT NULL UNIQUE,
    error_msg           TEXT
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id            INTEGER NOT NULL REFERENCES batches(batch_id),
    candidate_id        INTEGER NOT NULL,
    local_node_id       INTEGER NOT NULL,     -- negative id in osmChange XML
    osm_node_id         INTEGER,              -- filled from diffResult
    upload_status       TEXT NOT NULL,        -- pending / uploaded / failed
    error_msg           TEXT,
    PRIMARY KEY (batch_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS changesets (
    changeset_id        INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL,
    opened_at           TEXT NOT NULL,
    closed_at           TEXT,
    comment             TEXT,
    status              TEXT NOT NULL         -- open / closed / failed
);

CREATE TABLE IF NOT EXISTS events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    run_id              INTEGER,
    candidate_id        INTEGER,
    batch_id            INTEGER,
    actor               TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    payload_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(run_id, event_type, ts);

CREATE TABLE IF NOT EXISTS kv (
    key                 TEXT PRIMARY KEY,
    value               TEXT
);

CREATE TABLE IF NOT EXISTS schema_version (
    version             INTEGER PRIMARY KEY,
    applied_at          TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (1, datetime('now'));
