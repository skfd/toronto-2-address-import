-- Persist pre-amalgamation municipality_name on each candidate. Toronto absorbed
-- six former municipalities in 1998 without renumbering, so the same address_full
-- (e.g. "66 George St") can refer to three unrelated civic addresses in different
-- former municipalities. See SOURCE_DATA.md "Municipality trap". The true unique
-- key is (address_full, municipality_name); any future non-spatial lookup that
-- keys on address_full alone will fuse namesakes.

ALTER TABLE candidates ADD COLUMN municipality_name TEXT;

-- Backfill from extra_json.MUNICIPALITY (a 2-letter code) into the readable name
-- the source DB exposes in its own municipality_name column. Fresh ingests
-- write the readable name directly (see t2/candidates.py).
UPDATE candidates
   SET municipality_name = CASE json_extract(extra_json, '$.MUNICIPALITY')
       WHEN 'TO' THEN 'former Toronto'
       WHEN 'SC' THEN 'Scarborough'
       WHEN 'NY' THEN 'North York'
       WHEN 'ET' THEN 'Etobicoke'
       WHEN 'YK' THEN 'York'
       WHEN 'EY' THEN 'East York'
   END
 WHERE municipality_name IS NULL AND extra_json IS NOT NULL;

INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (7, datetime('now'));
