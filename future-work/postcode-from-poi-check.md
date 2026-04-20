# `postcode_from_poi` review check

Status: **proposed, not implemented**. Captured 2026-04-20.

## Motivation

`t2/conflate.py:_proposed_tags` copies `addr:postcode` from a same-address
POI node onto the proposed upload tags whenever a MISSING candidate has a
POI hit. The source DB has no postcode column (`source_db.py`), so that POI
is the only source of postcode signal — and the postcode ships into the
changeset with no review step. If the POI's postcode is wrong (mistagged
amenity, abandoned shop, stale tagging), we propagate the error into OSM
under `source="City of Toronto Open Data"`, which is misleading provenance.

No existing check inspects `conflation.proposed_postcode`. The review
queue UI already has a `postcode_from_poi` filter (since `0c7be57`) that
narrows by the column, but a clean MISSING still auto-approves because
nothing flags it first.

## Evidence from recent data

At last audit (operator-review-gaps.md §1.2, run 7 of `tool.db`):

- 2,800 MISSING candidates have a POI acknowledgement
- 917 of those carry a `proposed_postcode` sourced purely from the POI

So ~3% of the pipeline's auto-approved MISSINGs today ship a postcode
that no human has looked at.

## Scope

Add a new check `postcode_from_poi` (severity `info`). Fires whenever a
MISSING candidate has `cf.proposed_postcode IS NOT NULL`. Routes the row
to operator review so the postcode is surfaced at least once.

This is the lightest-weight version of the concern — it does not change
any conflate or upload behavior, just guarantees the postcode is reviewed
before upload.

## Code changes

- `t2/checks/postcode_from_poi.py` (new) — Check class. `applies` returns
  True when `cand.verdict == "MISSING"` and the candidate's
  `proposed_postcode` is non-empty. The `CheckContext.Candidate` dataclass
  doesn't currently carry `proposed_postcode` — add the field in
  `t2/checks/base.py` and plumb it through the `run_checks` query in
  `t2/pipeline.py` (which already joins `conflation`).
- `t2/checks/__init__.py` — register the new check in `REGISTRY`.
- `config.toml` `[checks]` — default enabled.
- `t2/web/glossary.py` — `reason.postcode_from_poi` tooltip.

## Relationship to `postcode-enrichment.md`

This is the *prerequisite-strength* version of the concern tracked in
`future-work/postcode-enrichment.md`. The enrichment proposal would also
copy a POI postcode onto a matched OSM node (not just new ones); both
paths share the risk that the POI postcode is wrong. Shipping
`postcode_from_poi` first de-risks the MISSING path independently of the
larger enrichment design, and the check itself is reusable when the
enrichment flow lands.

## Verification

- New check appears in `checks_catalog` after the first run.
- `review_items.reason_code = 'postcode_from_poi'` for every MISSING
  candidate with a non-null `cf.proposed_postcode` (~917 rows on run 7
  of the pre-existing `tool.db`).
- Operator review queue shows the postcode badge already, but now those
  rows surface with status OPEN instead of auto-approving.
