-- POI (amenity/shop/etc.) nodes are no longer treated as matches. When one sits
-- at the same housenumber+street as a MISSING candidate we acknowledge it here
-- so the review UI can show a pill and the upload can carry its postcode.

ALTER TABLE conflation ADD COLUMN poi_osm_id INTEGER;
ALTER TABLE conflation ADD COLUMN poi_osm_type TEXT;
ALTER TABLE conflation ADD COLUMN poi_tags_json TEXT;
ALTER TABLE conflation ADD COLUMN proposed_postcode TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (5, datetime('now'));
