-- Track when an operator decision overrides a prior auto-approval by the
-- pipeline. Auto-approved candidates have no review_items row; the flag is
-- set the moment the operator creates one for an already-APPROVED candidate.

ALTER TABLE review_items ADD COLUMN prior_auto_approved INTEGER NOT NULL DEFAULT 0;

INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (3, datetime('now'));
