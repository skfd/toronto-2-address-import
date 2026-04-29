# MapRoulette task: OSM buildings with `addr:housenumber` but no street

Status: **proposed, not implemented**. Captured 2026-04-29. Post-import follow-up.

## Motivation

A scan of the Toronto bbox extract (`data/osm/toronto-addresses.json`,
2026-04-29 snapshot) found **1,580 OSM elements with `addr:housenumber`
that carry no street anchor at all**:

- 1,576 elements with no `addr:street`, no `addr:place`, no `addr:housename`
  — 1,537 ways (building polygons) + 39 nodes
- 4 ways with `addr:housename` only (the "Lindens" / "Pines" apartment
  blocks in one complex), no `addr:street`

These are not findable by the project's conflation logic
(`t2/conflate.py:_classify` requires both `_norm_number` and `_norm_street`
to be equal, see `t2/conflate.py:159`), so the import will classify a City
candidate sitting on top of one of these as `MISSING` and upload a new
node — creating a colocated duplicate. This duplicate-creation path is
acknowledged in `IMPORT_PROPOSAL.md` §6 alongside the interpolation-
endpoint and multi-value-housenumber cases, with the same disposition:
accept transient duplication, fix in a follow-up.

The fix is not a code change — it requires local knowledge to determine
the correct street name for each unanchored building. That fits a
MapRoulette task: one challenge per element, the mapper picks the
neighbouring street and adds `addr:street` (and `addr:postcode` where
recoverable from a colocated POI or our own upload manifest).

Reproduce the count any time with `scripts/find_buildingname_addrs.py`
against a fresh extract.

## Scope

A MapRoulette challenge titled e.g. *"Toronto: address-tagged buildings
without a street name"*, seeded from a one-shot enumeration script that:

1. Reads `data/osm/toronto-addresses.json` (the same extract the import
   runs against).
2. Emits a GeoJSON FeatureCollection containing every element classified
   as `no_anchor` or `housename_only` by
   `scripts/find_buildingname_addrs.py`. Each feature carries the OSM
   `type`/`id`, current tags, polygon centroid (for ways), and a
   `proposed_street` hint where one can be derived.
3. Cross-references our own upload manifest (`<deploy>/uploads/all.csv`,
   §7 of the proposal): if we uploaded a per-address point at the same
   housenumber within ~30 m, that point's `addr:street` is the
   high-confidence hint.
4. Optionally cross-references the City source DB for the same `(point,
   housenumber)` pair.

Each MapRoulette task instruction:

> This OSM building has `addr:housenumber=<N>` but no `addr:street`.
> Toronto Open Data suggests the street is `<S>` (verify against
> imagery / local knowledge before applying). Add `addr:street=<S>`
> (and `addr:postcode=<P>` if confident).

## Why post-import, not pre-import

- The cleanup task is per-element local work, not algorithmic. Running
  it before the import doesn't reduce duplicate creation in any
  predictable way — a mapper has to look at each one.
- The richest hint source — *our own uploaded per-address points* — only
  exists after the import has run. Seeding the MapRoulette before upload
  would force the mapper to consult the City source manually for every
  task; seeding after lets the task description name the street outright
  for the colocated cases.
- The volume is small enough (≤1,580 tasks) that a single MapRoulette
  pass is feasible after import completion, before declaring the import
  fully closed.

## What this proposal does NOT cover

- The 188 `housename_with_street` cases (both tags set, street is
  correct, housename is supplementary metadata) — these are valid OSM
  tagging and need no fix.
- The 19 `place_and_street` POI mistaggings (`addr:place=Scotiabank`
  etc.) — separate concern, low volume, not worth a MapRoulette pass.
- The 2 `place_only` typos (`addr:place=Front Street East` on St.
  Lawrence Market South; `addr:place=The Esplanade` on a node) — fixed
  manually 2026-04-29 outside this pipeline.

## Verification

- `scripts/find_buildingname_addrs.py` re-run against a fresh extract
  reports the no-anchor count. Number should drop monotonically as the
  MapRoulette challenge is worked.
- After challenge completion, the `colocated duplicate` paths in §6 of
  the import proposal can be reconciled: any node we uploaded that now
  shares an address with a building polygon (via the polygon's newly-
  added `addr:street`) is a candidate for merge-onto-polygon by a local
  mapper, again outside this tool.
