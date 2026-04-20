-- Same-address Land-Land duplicates: persist the nearest same-address Land
-- sibling (and its distance) on each Land candidate's conflation row. The
-- conflate stage silently skips the non-canonical row when siblings sit
-- within 5 m; beyond that both rows proceed and the intra_source_duplicate
-- check flags them for operator review. See the plan under
-- C:\Users\kk\.claude\plans\ and SOURCE_DATA.md "Municipality trap" for
-- why (address_full, municipality_name) is the correct key.

ALTER TABLE conflation ADD COLUMN dup_sibling_candidate_id INTEGER;
ALTER TABLE conflation ADD COLUMN dup_sibling_dist_m REAL;

INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (9, datetime('now'));
