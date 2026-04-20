-- Cache address-range coverage on each candidate so the /ranges page is a
-- fast read. Populated by t2/ranges.py at the end of run_checks, and lazily
-- backfilled by the ranges page for pre-existing runs.

ALTER TABLE candidates ADD COLUMN range_coverage_cat TEXT;
ALTER TABLE candidates ADD COLUMN range_parity_present INTEGER;
ALTER TABLE candidates ADD COLUMN range_parity_total INTEGER;

INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (8, datetime('now'));
