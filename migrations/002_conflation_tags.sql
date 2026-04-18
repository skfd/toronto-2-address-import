-- Store the matched OSM element's tags and a geometry hint on each conflation row,
-- so the review detail page and audit log can show a 2-column proposed-vs-OSM diff.

ALTER TABLE conflation ADD COLUMN matched_osm_tags_json TEXT;
ALTER TABLE conflation ADD COLUMN matched_osm_geom_hint TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (2, datetime('now'));
