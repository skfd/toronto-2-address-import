# Postcode enrichment for matched OSM nodes

Status: **proposed, not implemented**. Captured 2026-04-19. Do not begin
implementation without re-reading the assumption and the guardrails below —
the feature changes the pipeline from one-directional creation to a mixed
create/modify flow, which is a meaningful blast-radius expansion.

## Motivation

Today the pipeline only uploads `<create>` elements (see
`t2/osm_export.py:osmchange_xml`, `t2/osm_client.py:_upload_diff`). Candidates
with verdict `MATCH` are dropped at conflation time — we've confirmed OSM has
the address, and that's the end of the line for them.

One narrow case is worth revisiting: a `MATCH` where the matched OSM address
node lacks `addr:postcode`, but a same-address POI node nearby carries one.
For MISSING rows we already copy that postcode onto the new node (see
`conflate.py:291` and `_proposed_tags`). For MATCH rows the same signal is
currently discarded.

## Source of truth — confirm before building

Toronto's source DB does **not** carry postcodes. Verified in
`source_db.py:iter_active_addresses_in_bbox` (no postcode column) and
`candidates.py` (the `extra` JSON only surfaces `ADDRESS_CLASS_DESC`).

The only postcode signal in this pipeline is `addr:postcode` on a same-address
POI node in the OSM snapshot. So "postcode enrichment" here means:
**copy a postcode from a nearby same-address POI onto the matched address
node that lacks one.**

If a Toronto-side postcode source is ever added, the design below still works
but the changeset `source` tag and review copy need to change.

## Hard guardrails

1. **Additive only.** If the matched node already has `addr:postcode`, never
   overwrite. If its value differs from the POI's, emit a review item (see
   the new `postcode_conflict` check below) and do not enqueue a mutation.
2. **Nodes only.** Skip ways/relations. Polygon tag edits have a different
   review bar and different rollback story.
3. **Separate changeset** from new-node uploads. Tag it
   `import:kind=postcode_enrichment`, `source="OSM same-address POI"` (not
   "City of Toronto Open Data" — the data didn't come from Toronto).
4. **Version-checked write.** Re-fetch the node via `GET /api/0.6/node/{id}`
   immediately before upload. The snapshot can be stale relative to server
   HEAD. On 409 conflict, mark the item `NEEDS_REFRESH` and skip.
5. **Human approval required.** Auto-*propose*, never auto-*approve*.
   Auto-approved edits to existing OSM objects cross into automated-edits
   territory and trigger the OSM community's Code of Conduct — prior
   discussion required. One human click per proposal is cheap insurance.

## Data model

New migration. Keep enrichment tables separate from the address-import
tables; item shape differs enough (target osm id + type + version, tag key,
tag value, provenance) that overloading `batches`/`batch_items` would add
nullable columns and branching everywhere.

```
enrichments(
  run_id, candidate_id,
  target_osm_id, target_osm_type,
  tag_key, tag_value,
  source_osm_id, source_osm_type,
  status,           -- PROPOSED | APPROVED | REJECTED | UPLOADED | CONFLICT | NEEDS_REFRESH
  created_at, reviewed_at, uploaded_at
)
enrichment_batches(...)       -- mirrors batches
enrichment_batch_items(...)   -- mirrors batch_items, adds target_version
```

## Code changes

- `t2/conflate.py` — for `verdict=MATCH` with a node target lacking
  `addr:postcode`, also search `poi_idx` (today searched only for MISSING).
  Insert `enrichments` row when a same-address POI with postcode exists.
  If the matched node has a postcode that differs from the POI's, audit-log
  and skip (the new check surfaces it).
- `t2/checks/postcode_conflict.py` (new) — `severity=info`, surfaces
  matched-node postcode ≠ POI postcode.
- `t2/osm_mutate.py` (new) — emits `<modify>` osmChange blocks. Takes the
  full re-fetched node element and returns a serialized diff that preserves
  every existing tag and adds the single new one.
- `t2/osm_client.py` — new `upload_enrichments(batch_id)`: per item,
  `GET /api/0.6/node/{id}`, build diff via `osm_mutate`, POST to
  `/changeset/{id}/upload`, handle 409. Changeset comment / tags distinct
  from address creation.
- `t2/batcher.py` — new `compose_enrichments(run_id, size)` parallel to
  `compose`.
- Audit events: `ENRICHMENT_PROPOSED`, `ENRICHMENT_APPROVED`,
  `ENRICHMENT_REJECTED`, `ENRICHMENT_UPLOADED`, `ENRICHMENT_CONFLICT`,
  `ENRICHMENT_NEEDS_REFRESH`.

## UI changes

- **New tab: Enrichments** on the run page, sibling to the existing review
  queue. Columns: address, matched OSM node (link to osm.org), summary of
  current tags, the single tag being added, POI source (link), distance.
  Row actions: Approve / Reject. Bulk: select-all + approve/reject.
- **Enrichment batches** section on the run page with its own Compose and
  Upload buttons; mode pinned to `osm_api_modify`.
- **Dashboard chip** on `/runs/<id>`:
  `Enrichments: N proposed · M approved · K applied · C conflicts`.
- **Filter pill** reusing the existing pattern: "Postcode conflict"
  (fed by the new check).
- **Glossary entries** for the new statuses and the enrichment-vs-creation
  distinction (`t2/web/glossary.py`).
- **README** — move the postcode-enrichment bullet out of "Out of scope"
  and add a "Mutation" section documenting the additive-only rule, the
  separate changeset, and the version-check contract.

## Phasing (each phase independently shippable and reversible)

- **A** — migrations + `conflate.py` populates `enrichments` rows + audit
  events. No UI. Unit tests against fixtures.
- **B** — read-only review tab.
- **C** — approve/reject + upload path (`osm_mutate.py`, new changeset flow,
  409 handling).
- **D** — polish: conflict check, dashboard chips, glossary, README.

## Out of scope even within this proposal

- Overwriting a disagreeing `addr:postcode`.
- Postcodes on ways/relations.
- Any other tag (street-name typo fixes, house-number corrections, etc.).
- Geometry nudging.

If any of those become desirable, they belong in their own proposal with
their own review bar — folding them in here would dilute the additive-only
invariant that makes this safe.

## Open questions (answer before starting phase A)

1. Confirm the POI-as-source assumption still holds. If Toronto adds a
   postcode column upstream, provenance and `source` tag change.
2. Auto-propose + human approve is the recommendation. Any appetite for an
   auto-approve toggle would need explicit OSM community sign-off first.
3. Same review UI tab or separate top-level section? Tab inside the run
   page keeps everything per-run; easier to reason about.
