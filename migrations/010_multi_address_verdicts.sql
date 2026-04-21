-- Operator verdicts for multi-address OSM entries reviewed at /osm/multi/all.
-- One row per OSM element the operator has classified. On save, actionable
-- verdicts (normalize / unit_prefix / reverse) are re-fetched from live
-- Overpass and written to a JOSM-ready .osm file. keep_range and skip are
-- recorded here but produce no export edits.

CREATE TABLE IF NOT EXISTS multi_address_verdicts (
    osm_type    TEXT NOT NULL CHECK (osm_type IN ('node','way','relation')),
    osm_id      INTEGER NOT NULL,
    verdict     TEXT NOT NULL CHECK (verdict IN ('normalize','keep_range','unit_prefix','reverse','skip')),
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (osm_type, osm_id)
);

INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (10, datetime('now'));
