-- Track whether each verdict has already been written to a .osm export file
-- so a second Save & export click doesn't re-ship work the operator already
-- uploaded via JOSM. `exported_at` is cleared whenever the verdict value
-- changes (see save_verdicts in t2/multi_fixes.py), so re-classifying a row
-- naturally re-queues it for the next export.

ALTER TABLE multi_address_verdicts ADD COLUMN exported_at   TEXT;
ALTER TABLE multi_address_verdicts ADD COLUMN exported_file TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (11, datetime('now'));
