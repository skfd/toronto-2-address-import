-- Store a point location of the matched OSM element so the review detail map
-- can draw a second marker without another Overpass call.

ALTER TABLE conflation ADD COLUMN matched_osm_lat REAL;
ALTER TABLE conflation ADD COLUMN matched_osm_lon REAL;

INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (4, datetime('now'));
