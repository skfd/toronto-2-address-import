# Toronto Address Points — OSM Import Proposal

**Status:** Draft for community review. Not yet submitted to `imports@osm.org` or the OSM wiki. All uploads from the tooling described here currently target the OSM **dev sandbox** (`master.apis.dev.openstreetmap.org`). No production edits have been made and none will be made until this proposal (or a successor revised after discussion) has been posted to the imports list and the wiki, has sat for the customary feedback window, and has the sign-off that the [OSM Import Guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines) require.

**Last revised:** 2026-04-21
**Contact:** toronto@comentality.com
**Tooling:** <https://github.com/skfd/toronto-2-address-import> (this repo) + <https://github.com/skfd/toronto-addresses-import> (upstream scraper)
**Pilot evidence:** <https://skfd.github.io/toronto-2-address-import/pilot/runs/15/> — read-only snapshot of one completed pilot run (tile `high-park-swansea-sw-se`, 250 candidates) rendered in the full review UI.

---

## 1. Summary

One-time, human-reviewed import of **missing civic address points** from the City of Toronto's "Address Points (Municipal) – Toronto One Address Repository" open dataset into OpenStreetMap, conflated against a fresh OSM snapshot so that only addresses OSM does not already have are created. Every batch is reviewed in a local web UI before it leaves the machine; every upload is a distinct, tagged changeset; every action (automatic or manual) is written to an append-only audit log.

Scope numbers (active snapshot #28, 2026-04-18):

| Address class | Active rows | Disposition |
|---|---:|---|
| `Land` | 479,966 | Candidate for import as a pure address node. |
| `Structure` | 28,031 | Candidate for import as a pure address node. |
| `Structure Entrance` | 14,354 | Candidate for import with `entrance=yes`. |
| `Land Entrance` | 573 | **Excluded** from this import (driveway/gate concept, not an address). |
| **Total considered** | **522,351** | Before conflation. |
| **Total excluded upfront** | **573** | `Land Entrance`. |

Expected output after conflation is materially smaller than the above — any address OSM already carries is dropped at conflation time, not uploaded.

## 2. Goals and non-goals

### Goals

- Raise OSM's civic-address coverage in the City of Toronto (former municipalities of Toronto, East York, Etobicoke, North York, Scarborough, and York) to match the City's authoritative address roster for addresses OSM is missing today.
- Do so without creating duplicates of addresses already mapped in OSM, and without stamping over existing address data.
- Preserve a per-candidate audit trail (source row → verdict → reviewer decision → changeset id → resulting OSM id) for post-hoc inspection by any OSM contributor.

### Non-goals (explicitly out of scope for this import)

- **No deletions.** If OSM has an address that the City snapshot does not, we do not flag, propose, or remove it. Rationale in §8.
- **No mutation of existing OSM objects.** This import creates new nodes only. Any future postcode or tag-enrichment work on *matched* OSM nodes will be a separate proposal with its own review. A design sketch exists in `future-work/postcode-enrichment.md` but is not part of this import.
- **No `addr:interpolation` way cleanup.** Even where per-address points now cover the same segment as an existing interpolation way. Rationale in §8.
- **No polygons.** We do not add `addr:*` tags to existing `building=*` ways/relations, and we do not create new buildings. Out-of-scope both for this tool and this proposal.
- **No geometry editing** of any existing object.
- **No `Land Entrance`** rows: the source models driveway/gate entry points; OSM's closest concept is `barrier=gate`, not an address. Excluded at ingest.

## 3. Schedule

**Contacts and reviewer roster.**

- **Primary maintainer / first-line reviewer:** `toronto@comentality.com`
- **Additional reviewers:** named on the OSM wiki page before Phase 1 begins
- **Joining the roster:** local Toronto mappers contact the email above
- **Upload rule:** no batch is uploaded without at least one named reviewer's approval in the web UI

Phased roll-out, each phase shippable and independently reversible. Dates are earliest-start, pending community review and feedback incorporation.

- **Phase 0 — community review.** Post this proposal to `imports@openstreetmap.org` and create a page on the OSM wiki under `Import/Catalogue`. Minimum two-week feedback window. Incorporate feedback, revise.
- **Phase 1 — pilot (1 tile).** Tile `high-park-swansea-sw-se` — a depth-2 quadrant of the High Park-Swansea neighbourhood, bbox `(43.633436, -79.480592, 43.639157, -79.469502)`, 250 source addresses pre-conflation. End-to-end human review. All candidates manually approved even where auto-approval would normally apply. Post the changeset list to the wiki page after upload. Hold for one week for community response.
- **Phase 2 — ward-level rollout.** Proceed ward-by-ward, working through the 2,493 tiles one at a time. Retain manual approval of a random sample (≥5%) even for auto-approvable items.
- **Phase 3 — remaining tiles.** Same cadence, same review gating.
- **Phase 4 — closeout.** Final reconciliation: re-fetch the bbox, publish a post-import report (counts, rejection reasons, outstanding `REVIEW_DEFERRED` items).

Upload rate target is **≤1 changeset per minute** by manual cadence (the `upload.changesets_per_minute` config value is advisory — uploads are operator-triggered per batch, not throttled by the tool) and **300 candidates per changeset** (config `upload.batch_size`, enforced). A theoretical full pass at these limits bounds wall-clock upload time at ~29 hours; the practical schedule is much longer because human review of the review queue dominates.

## 4. Import data

### Source

- **Dataset:** "Address Points (Municipal) – Toronto One Address Repository", published by the City of Toronto.
- **Portal:** <https://open.toronto.ca/dataset/address-points-municipal-toronto-one-address-repository/>
- **Consumption path:** the City's portal feed is scraped and normalised into a SQLite DB by the sibling project [`toronto-addresses-import`](https://github.com/skfd/toronto-addresses-import). This import consumes that SQLite DB read-only. See `SOURCE_DATA.md` in this repo for the exact schema and the fields we rely on.

### License

- **Upstream licence:** [Open Government Licence – Toronto](https://open.toronto.ca/open-data-licence/).
- **ODbL compatibility:** compatible. OGL-Toronto is a permissive attribution licence modelled on the Canadian federal OGL, with no share-alike and no non-commercial clauses; attribution is satisfied by the `source=City of Toronto Open Data` tag on both the uploaded node and its containing changeset.

### Type and volume

- Point data only. No polygons, no lines.
- Per-address civic points with housenumber, street, municipality, ward, lat/lon, and a class descriptor (Land / Structure / Structure Entrance / Land Entrance).
- Post-conflation volume will be substantially smaller than 522k — the actual count depends on OSM's current Toronto address coverage at the moment each tile is run.

### Freshness

- **Source:** Geofabrik Ontario PBF, refreshed via `t2/osm_refresh.py`.
- **Cadence:** re-pulled before each run — the "OSM already has this address" signal is always based on a fresh snapshot.
- **Staleness rule:** if more than 24 h elapse between conflation and upload, re-fetch and re-conflate before opening changesets.

## 5. Tagging plan

### Per-node tags written

All uploaded elements are **nodes**. No ways, no relations. The tag set is constant modulo a small set of class-driven additions:

| Tag | Source | Notes |
|---|---|---|
| `addr:housenumber` | `address_number` | Copied verbatim after trim. Suffix letters (`46A`, `710 1/2`) preserved. |
| `addr:street` | `linear_name_full` | Copied verbatim (mixed case, e.g. `Amelia St`). Normalisation is only used for conflation matching (`STREET`→`ST`, etc.), not for the written tag. |
| `addr:city` | static | `Toronto`. Used regardless of pre-amalgamation former municipality; see §5.1. |
| `source` | static | `City of Toronto Open Data`. On the node *and* on the containing changeset. |
| `addr:postcode` | enrichment | Written **only** when a same-address POI in the OSM snapshot already carries one. Never invented, never extrapolated. We adopt the postcode from the nearest same-address POI when present; absent that, we emit no postcode. Details in §6. |
| `entrance` | class-driven | `yes` — written **only** for `Structure Entrance` rows. Aligns with OSM's `entrance=yes` convention for door-level nodes. Absent on all other classes. |

Fields deliberately **not** emitted:

- `addr:housename`, `addr:unit`, `addr:flats`, `addr:block` — source does not carry these in a reliable form.
- `addr:country`, `addr:province`, `addr:state` — omitted per OSM Canadian convention; `addr:city` is sufficient for Toronto.
- `addr:neighbourhood`, `addr:suburb`, `addr:ward` — the source's `ward_name` and neighbourhood overlays are modelled better as OSM admin polygons than as per-node tags.
- `ref`, `name`, `place` — none apply.
- Any `toronto:*` / `t2:*` custom namespace — rejected on principle.

### 5.1 The `addr:city` question

The source carries a `municipality_name` column that reflects the **pre-amalgamation** former municipalities (Toronto, East York, Etobicoke, North York, Scarborough, York). These are historical, not current civic entities — the City of Toronto is one city since 1998. We write `addr:city=Toronto` uniformly. The pre-amalgamation municipality is preserved in our internal audit trail but not emitted into OSM.

Open question for community (§10): confirm this matches existing Toronto OSM convention. If local mappers prefer `addr:city` to carry the former-municipality string, we will change the static value per-row before Phase 1.

### 5.2 Per-class tagging matrix

| Class | `addr:housenumber` | `addr:street` | `addr:city` | `source` | `entrance` | `addr:postcode` |
|---|---|---|---|---|---|---|
| `Land` | yes | yes | `Toronto` | yes | — | if colocated POI has one |
| `Structure` | yes | yes | `Toronto` | yes | — | if colocated POI has one |
| `Structure Entrance` | yes | yes | `Toronto` | yes | `yes` | if colocated POI has one |
| `Land Entrance` | — | — | — | — | — | — (excluded upfront) |

### 5.3 Changeset tags

Each changeset opened against the OSM API carries:

| Tag | Value |
|---|---|
| `comment` | `Toronto Open Data address import, run=<run_name>, batch=<batch_id>` (template in `config.toml`) |
| `source` | `City of Toronto Open Data` |
| `import` | `yes` |
| `bot` | `no` |
| `created_by` | `t2-address-import` |
| `import:client_token` | random per-batch UUID — used only for server-side idempotent retry after a network failure; looked up before reopening a changeset so a dropped connection never results in two parallel uploads of the same batch |

The `import=yes` / `bot=no` combination matches the OSM Wiki's guidance: these are one-shot, human-reviewed imports, not ongoing automated edits.

## 6. Conflation

### Algorithm

For each source address, we look at the OSM snapshot and classify:

- **MATCH** — OSM has a pure-address node or polygon with the same normalised housenumber and street, within **15 m** of the source point.
- **MATCH_FAR** — same housenumber/street found within 15–100 m. Surfaced to the human reviewer; never auto-approved.
- **MISSING** — no OSM address with the same housenumber/street within 100 m. Candidate for upload.
- **SKIPPED** — housenumber is a range (e.g. `100–110 Main St`) or contains digit-confusable letters (`I`, `O`, `Q`). Not imported; reviewer may opt in per-item.

Search radii are set in `config.toml` under `[conflation]`:
- `match_radius_m = 100`
- `match_near_m = 15`

Street-name normalisation (`STREET` → `ST`, `AVENUE` → `AVE`, `NORTH` → `N`, etc.) is used **only** for matching. The source's original casing is what's written to OSM.

### Match targets

Two kinds of OSM feature are valid match targets:

1. **Pure address nodes** — nodes with `addr:housenumber` that do **not** carry POI keys (`amenity`, `shop`, `office`, `tourism`, `leisure`, `craft`, `healthcare`, `building`, plus their `disused:*` / `was:*` variants). Nodes additionally tagged `entrance=*` (≈675 across Toronto) count as pure-address match targets — their address is canonical, the `entrance` tag just records that the point sits on a door rather than the parcel centre.
2. **Polygons with address tags** — ways and relations carrying `addr:housenumber`, including address-bearing buildings. Polygon centroids are used for the distance calculation.

POI nodes (amenity/shop/etc.) with `addr:*` tags are explicitly **not** match targets. Their address is a courtesy annotation and the canonical address point is typically absent. When a MISSING candidate is colocated with such a POI, the review UI acknowledges it with a pill and — if the POI carries `addr:postcode` — that postcode is adopted onto the proposed new node. This is the only case where we draw tag data off of an existing OSM object, and it is additive (never overwriting).

### Nodes dropped from match index

Nodes referenced by an `addr:interpolation` way are excluded from the index. They are endpoints of a range declaration, not standalone addresses, and treating them as match targets would spuriously suppress candidates that fall between them.

### Municipality disambiguation

Toronto absorbed five adjacent municipalities in 1998. Street names recur across the old boundaries — `48 Victor Ave` exists as distinct civic addresses in more than one former municipality. Our intra-city duplicate check uses `(address_full, municipality_name)` as the identity key, not `address_full` alone. This only affects checks and dedup — the written tags do not carry the former municipality (see §5.1).

### Colocated duplicates within the source

- **Shape:** non-`Land` row (`Structure` or `Structure Entrance`) sharing `(address_full, municipality_name)` with a `Land` row in the same run. ~289 rows city-wide on snapshot #28 (276 `Structure`, 13 `Structure Entrance`). `Land Entrance` is excluded at ingest (§2) and so does not reach this pass.
- **Behaviour:** dedup pass in conflation skips the non-`Land` row whenever a same-key `Land` sibling exists; the `Land` row is treated as the canonical record and is the only one that proceeds to review and upload. The check is purely key-based — no distance threshold — because within one former municipality the source treats one `address_full` as one civic address (the municipality component of the key handles cross-municipality string collisions like `48 Victor Ave`).
- **Tiebreak rationale:** `Land` is the parcel-level "this lot has this address" point and maps cleanly to a standalone OSM address node. Non-`Land` classes (building centroid, door) are dropped only when a same-key `Land` sibling exists; otherwise they flow through normally and carry a unique address (see §3 source data summary).

### Acknowledged duplicate-creation paths, deferred to a future phase

Two known OSM data shapes can cause this import to create a *colocated duplicate* of an address OSM already carries, because they're not representable in the current single-value match key:

1. **`addr:interpolation` endpoints.** Interpolation-way member nodes are dropped from the match index (they're endpoint-of-range declarations, not standalone addresses). A City candidate whose housenumber happens to coincide with one of those endpoint numbers will therefore be classified `MISSING` and uploaded — creating a node that duplicates the address the interpolation endpoint already asserts. The same effect, by construction, applies to every real City address that falls *between* the endpoints: the whole premise of the interpolation-replacement phase is that per-address points are better than a synthesised range.
2. **Multi-value `addr:housenumber` on a single OSM node.** Canonical OSM uses `;` to separate multi-values, but the Toronto OSM extract additionally contains `,`-separated lists and `N-M`-style ranges packed into a single tag (see `t2/multi_addresses.py`). The match key is the literal string, so `addr:housenumber=100;102;104` does not match a City candidate for `100` — and we'd upload a colocated duplicate for every sub-number the multi-value tag subsumes.

**Disposition for this import:**

- Accept the transient duplication.
- No algorithmic split at conflation time.
- No new reviewer check.
- Cleanup handled in the follow-up proposal in §8 — that proposal enumerates these objects in place, cross-checks them against newly-uploaded per-address points, and retires or normalises them as appropriate.
- Handling either shape now would mean editing existing OSM objects (different review bar, different rollback story) — out of scope per §2.

## 7. Workflow and QA

### Pipeline stages

Every candidate advances through a deterministic sequence of stages recorded per-row in the DB:

```
INGESTED → CONFLATED → REVIEW_PENDING → APPROVED → BATCHED → UPLOADED
                     ↘ REJECTED / DEFERRED
                     ↘ SKIPPED (range, MATCH, colocated dup)
```

Each stage is resumable — killing the process mid-run and restarting is safe. Re-running a stage skips work already done.

### Checks

Six automated checks (enabled in `config.toml`, all `severity=info|warn|block`):

| Check | Purpose |
|---|---|
| `match_far` | Matched housenumber/street is 15–100 m away — could be the same point, could be a different building. Always reviewed. |
| `suffix_range` | Housenumber is a range (`100-110`) or contains digit-confusable letters. Blocks auto-approval. |
| `city_duplicate` | Another candidate in the same run is within a few metres and has the same housenumber. |
| `intra_source_duplicate` | Duplicate within the source dataset before conflation. |
| `missing_sample` | Every Nth MISSING candidate is force-reviewed even if it has no other flags. Provides ongoing validation that the auto-approval bar is well-calibrated. |
| `potential_amenity` | Matched OSM node carries non-address tags (`name`, `ref`, `entrance`, etc.) — hints the POI filter may need to grow. Not a block; feeds iteration on the match target rules. |

### Review queue

- MISSING candidates with **no** flags raised by the checks enter the review queue as `AUTO_APPROVED` — the reviewer's action is one-click acknowledge-or-reject rather than full review. During Phases 1 and 2 we will hold even AUTO_APPROVED items for a human click on a random sample (≥5%).
- MATCH candidates bypass upload entirely (they are not in scope — OSM already has the address).
- Every other state (`MATCH_FAR`, `MISSING` with any flag, `SKIPPED`) requires explicit reviewer action: Approve / Reject / Defer.

The review UI is a local Flask app (`python run.py`, <http://localhost:5000/>). It is not exposed to the public internet. Reviewers are the named people listed on the wiki page for this import.

### Audit log

- **Scope:** every automatic classification, every reviewer decision, every batch composition, every changeset open/upload/close.
- **Key and durability:** keyed by candidate id; append-only; survives reruns.
- **Publication:** wiki page links per-run audit dumps; any OSM contributor can reconstruct what happened for any uploaded node.

### Post-upload reconciliation

- **Id mapping:** OSM API returns local-id → OSM-id pairs per batch; stored in the audit log.
- **Per-tile publication:** every export run writes an upload manifest — a `(address_point_id, address_full, osm_node_id, changeset_id)` CSV — at `<deploy>/uploads/<tile_id>.csv` for each completed tile, plus a cumulative `<deploy>/uploads/all.csv` covering every uploaded item across all tiles. The wiki page links to the cumulative file. Reviewers can audit any uploaded node within hours of the changeset closing — no need to wait for a phase boundary. This serves the same purpose as the Montréal `Adresses ponctuelles` reconciliation table, on a tile-grained cadence that fits the slower per-tile rollout.
- **Dispute traceability:** any uploaded node can be traced back to its source row, conflation verdict, reviewer decision, and changeset id in a single query.

## 8. Deferred work (not part of this import)

These are documented so no reviewer has to ask whether we forgot them. Each would be proposed separately if and when we pursue it.

### Deleting OSM addresses absent from the City source

- **What it is:** remove OSM addresses the City snapshot doesn't have.
- **Why deferred:** absence in the source is a weaker signal than presence — the feed has refresh lag, neighbourhoods with acknowledged coverage gaps, and retired-address lifecycle states that aren't cleanly separable from "never existed." Deletion on that signal alone would destroy real addresses on weaker evidence than we accept for additions.
- **What a future proposal needs:** its own review queue (the verdicts here don't fit); a street-level cross-check to suppress the common "Toronto's feed is missing a whole street" case; strictly human approval — no automation.

### Replacing `addr:interpolation` ways with per-address points

- **What it is:** retire `addr:interpolation` ways where the City snapshot now provides real per-address points along the same segment.
- **Why deferred:** replacement is a bulk structural edit, not an address addition — different changeset hygiene, different review bar.
- **What a future proposal needs:** cross-validation that every integer in the interpolation's range has a colocated City point; careful handling of tags the way carries on behalf of its endpoints (`addr:street`, `addr:postcode`); resolution of any colocated duplicates this import created against interpolation endpoints (§6).

### Normalising multi-value `addr:housenumber` nodes

- **What it is:** normalise OSM nodes that pack multiple street numbers into one `addr:housenumber` tag (`;`-, `,`-, or `N-M`-delimited) — either by splitting into per-number nodes or by retiring in favour of newly-uploaded per-address points.
- **Why deferred:** edits existing objects, needs per-case review, belongs in a mutation-capable pipeline rather than this create-only one.
- **What a future proposal needs:** enumeration source already exists (`t2/multi_addresses.py`, surfaced on `/osm/multi`); cross-check against newly-uploaded per-address points; resolution of any colocated duplicates this import created against multi-value nodes subsuming a City housenumber (§6).

### Mutation of matched OSM nodes (e.g. postcode enrichment)

- **What it is:** add `addr:postcode` (or other tags) to existing matched OSM address nodes, e.g. by copying from a same-address POI nearby.
- **Why deferred:** this import is additive-creation-only; mixing `<modify>` into the same changeset flow expands blast radius and changes the review bar.
- **What a future proposal needs:** sketched in `future-work/postcode-enrichment.md` — version-checked writes, separate changeset tagged `import:kind=postcode_enrichment`, human approval per proposed enrichment (no auto-approve).

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Duplicate creation against an OSM address we didn't see. | Conflation against a fresh Geofabrik snapshot; 100 m search radius with normalised housenumber/street; re-fetch + re-conflate if conflation-to-upload lag exceeds 24 h; post-upload reconciliation so any duplicate raised by the community can be traced to its source row and corrected. |
| Incorrect street name (pre/post amalgamation rename, City typo). | `match_far` and `city_duplicate` checks surface most cases; reviewer can defer to a follow-up. Street-name renames without a corresponding OSM change are escalated to local mappers, not force-pushed. |
| Rate pressure on OSM API / planet feed. | Operator-triggered cadence targeting ≤1 changeset/min; 300 items per changeset (enforced); pilot tile first; no parallel uploaders from this tool. |
| Scripted or bulk auto-approval drift. | `missing_sample` check force-reviews every Nth MISSING; Phases 1 and 2 hold ≥5% of AUTO_APPROVED items for manual click; reviewer actions and their actors are in the audit log. |
| Mid-upload crash leaving orphan changesets. | `import:client_token` tag on each changeset; on retry the client searches open changesets for the token before opening a new one (`t2/osm_client.py`). Changesets are explicitly closed after upload. |
| POI filter too narrow — a matched "pure address" node actually represents a shop. | `potential_amenity` check surfaces these as `severity=info`; reviewer can defer; `POI_TAG_KEYS` in `t2/conflate.py` is iterated as we find cases. |
| Community unaware of ongoing import. | Wiki page kept updated with batch-level progress; changeset comments include run name and batch id; contact email published. |

## 10. Revert plan

Every batch is uploaded as a single changeset tagged `import=yes`, `created_by=t2-address-import`, and `import:client_token=<uuid>`. The revert surface is therefore per-changeset, which makes routine rollback straightforward.

- **Routine revert (one bad batch).** A changeset later identified as problematic is reverted using the [JOSM Reverter plugin](https://wiki.openstreetmap.org/wiki/JOSM/Plugins/Reverter). The changeset id is available in the audit log for every uploaded candidate, and the published OSM-ID list (§7 post-upload reconciliation) groups candidates by changeset for quick lookup.
- **Systemic issue mid-import.** If the community flags a class of problems — a tag error, a conflation false-positive pattern, a streetname misspelling that slipped through — uploads pause immediately. The pipeline is fixed, conflation is re-run on the affected tiles, and resumption only happens after a fresh human review pass. Any already-uploaded batches produced by the defective code are reverted before we resume.
- **Post-import community revert.** We do not contest good-faith reverts by local mappers. Any revert we discover is recorded in our audit log; where the underlying candidate is still valid, it is re-enqueued for explicit human re-review rather than automatically re-uploaded.
- **Freeze trigger.** If `imports@openstreetmap.org`, the Toronto mailing list, or a reviewer with commit rights on the wiki page files a stop-work request, we freeze within one business day and do not resume until the concern is addressed and acknowledged on the wiki page.

We do not rely on `<delete>` osmChange blocks to roll ourselves back — a hand-edit made on top of one of our uploads between upload and revert could otherwise be clobbered. The JOSM Reverter plugin handles that case correctly (it builds a conflict-aware inverse changeset), so it stays authoritative.

## 11. Open questions for the community

1. **`addr:city` convention.** Is `addr:city=Toronto` the right uniform value across all former municipalities, or does local convention prefer the former-municipality name (`East York`, `Scarborough`, etc.)?
2. **Changeset comment template.** Current format is `Toronto Open Data address import, run=<run_name>, batch=<batch_id>`. Any information we should add or remove?
3. **Post-import monitoring.** How long after the final batch should we commit to watching for community-raised issues? Proposing 90 days.
4. **Empty lots and recently demolished buildings.** Some source rows describe addresses where no building presently stands — empty lots awaiting construction, or recent demolitions where the City feed hasn't yet retired the record. Distinguishing a real current civic address from stale source data here requires local knowledge. Preferred handling: upload all such rows (the address is a civic record regardless of whether a structure stands), skip those without a visible building on recent imagery, or route them through per-tile review with a local mapper?
5. **`Structure Entrance` placement against building walls.** Source `Structure Entrance` points sit at the City's recorded door coordinate, which is usually on or near a building outline but not snapped to it. The current pipeline emits the node verbatim at that lat/lon — standalone, not a member of any building way. Three options: (a) leave as-is and let the JOSM operator drag/join each entrance onto the wall manually; (b) snap the coordinate to the nearest building-way segment within a small threshold (e.g. 5–10 m) so the node visually sits on the wall but remains standalone — the operator can still J-key join it; (c) snap *and* insert the node into the building way (modifies an existing OSM way, which exits this import's create-only scope per §2). Preference from the community on (a) vs (b)? Option (c) would require its own proposal.
6. **Address ranges (`4611-4619 Steeles Ave W`).** Source has 1,639 active rows where `lo_num ≠ hi_num`, plus 49 lettered ranges (`49A-59A`, `361A-415A` … `361J-415J`) where the same letter sits on both endpoints. There is no parity flag and no enumerated unit list — the source stores only the two endpoints and a single `(latitude, longitude)`. The current pipeline `SKIP`s every range row; reviewers can opt in per-item, in which case the verbatim string is uploaded as `addr:housenumber=4611-4619` on a single node at the source's coordinate. Three options: (a) keep skipping by default (current behaviour); (b) upload the verbatim range string on the single source-provided coordinate; (c) expand into one node per implied housenumber (e.g. `{4611, 4613, 4615, 4617, 4619}`) — coordinates would have to be synthesised since the source gives only one point per range. Two facts that bear on (c): 98.7% of range rows have matching parity on `lo_num`/`hi_num` (so a step-2 expansion is well-defined), but 22 rows are cluster-style sequential numbering (`1-96 Red Cedarway`, the eleven `Cantle Path` blocks) where the implied step is 1, and 49 rows are lettered subdivisions of larger complexes where multiple parallel rows share the same numeric span. Preference between (a), (b), and (c)? Option (c) would also need a convention for coordinate placement (single point with all nodes stacked, jittered, or interpolated along the centreline).

Answers to each will be incorporated into the wiki page and, where they change pipeline behaviour, into `config.toml` and the relevant code.

## 12. References

### OSM process and policy

- OSM import process: <https://wiki.openstreetmap.org/wiki/Import/Guidelines>
- OSM imports mailing list: <https://lists.openstreetmap.org/listinfo/imports>
- OSM imports catalogue: <https://wiki.openstreetmap.org/wiki/Import/Catalogue>
- OSM contributor terms: <https://osmfoundation.org/wiki/Licence/Contributor_Terms>
- JOSM Reverter plugin (routine-revert tool named in §10): <https://wiki.openstreetmap.org/wiki/JOSM/Plugins/Reverter>

### Source data

- City of Toronto Address Points dataset: <https://open.toronto.ca/dataset/address-points-municipal-toronto-one-address-repository/>
- Open Government Licence – Toronto: <https://open.toronto.ca/open-data-licence/>

### This import's tooling

- This tool (review UI, conflation, uploader): <https://github.com/skfd/toronto-2-address-import>
- Upstream scraper (City feed → SQLite): <https://github.com/skfd/toronto-addresses-import>
- Internal terminology (Candidate / Verdict / Status / Stage): `README.md` § Terminology
- Source-side facts verified against snapshot #28: `SOURCE_DATA.md`

### Benchmark proposals used while drafting

These are the prior OSM import proposals this document was compared against. Section coverage and convention choices (changeset tagging, publication of created OSM ids, revert plan wording) draw on all three.

- Ottawa address import plan: <https://wiki.openstreetmap.org/wiki/Canada:Ontario:Ottawa/Import/Plan>
- Ottawa address points schema reference: <https://wiki.openstreetmap.org/wiki/Canada:Ontario:Ottawa/Import/AddressPoints>
- Montréal Adresses ponctuelles import: <https://wiki.openstreetmap.org/wiki/Montr%C3%A9al/Imports/Adresses_ponctuelles>

## 13. Change log

| Date | Change |
|---|---|
| 2026-04-21 | Initial draft for internal review before wiki submission. |
| 2026-04-21 | Asserted OGL-Toronto ↔ ODbL compatibility; named pilot tile `high-park-swansea-sw-se`; added reviewer-roster contact line; committed to publishing per-phase OSM-ID lists; added §10 Revert plan; reorganised References with benchmark proposals (Ottawa, Montréal). |
| 2026-04-21 | §6 acknowledges two duplicate-creation paths (interpolation endpoints, multi-value `addr:housenumber`) and defers their resolution; §8 adds the multi-value-normalisation follow-up entry. No algorithmic change, no new check. |
| 2026-04-21 | Reworked dense paragraphs in §3 Contacts, §4 Freshness, §6 Colocated duplicates + Disposition, §7 Post-upload reconciliation + Audit log, and all §8 deferred-work entries into bullet lists. No content changes. |
| 2026-04-28 | §2 Phase 2 drops the ~5,000 addresses/day cap and frames rollout as 2,493 tiles processed one at a time. §6 Colocated duplicates updated to reflect the implemented behaviour (non-`Land` row skipped when a same-address `Land` sibling sits within 50 m); doc no longer describes it as a planned fix. |
| 2026-04-28 | §6 cross-class dedup: dropped the 50 m radius. The check now keys purely on `(address_full, municipality_name)` for non-`Land` rows. Snapshot #28 had no same-key cross-class pairs >50 m apart within one former municipality, so the radius was a no-op; removing it closes the theoretical gap and simplifies the rule. Code (`t2/conflate.py`), in-app glossary, and `SOURCE_DATA.md` updated to match. |
| 2026-04-28 | §7 reconciliation: replaced phase-end attachment with per-tile + cumulative CSV publication. New `t2/upload_manifest.py` writes `(address_point_id, address_full, osm_node_id, changeset_id)` rows per uploaded candidate; `t2.static_export` and `t2.static_export_all` emit `uploads/<tile_id>.csv` per tile and `uploads/all.csv` cumulatively. Wiki page links to the cumulative file; reviewers can audit any uploaded node within hours rather than waiting for a phase boundary. |
| 2026-04-28 | §11 Open questions: dropped items already settled elsewhere in the doc (`addr:province` omission — §5; pilot tile choice — §1, §2; rate cap — §2, §9). Added a new question on empty lots and recently demolished buildings, which need local-mapper input rather than pipeline-level decisions. |
| 2026-04-28 | Doc/code reconciliation pass. (1) `Land Entrance` is now actually filtered at ingest in `t2/candidates.py`, matching the §1/§2/§5.2 claim; §6 cross-class dedup wording updated to reflect that only `Structure` and `Structure Entrance` reach the pass (~289 rows on snapshot #28). (2) §3 + §9 rate-cap language downgraded to "operator-triggered cadence targeting ≤1 changeset/min" — the `upload.changesets_per_minute` config value was never enforced in code and is advisory. (3) §7 dropped the duplicate `conflict` check row (the code has six checks, not seven; the `match_far` check covers the MATCH_FAR-distance case). (4) §5 `addr:postcode` row dropped the "disagrees with anything → emit no postcode" clause — the code adopts the nearest same-address POI's postcode when present and emits none otherwise. (5) §7 stage diagram simplified — dropped the `CHECKED` intermediate (rarely reached) and the `FAILED` branch (no code path sets stage='FAILED'; upload errors set `batches.status='needs_attention'` instead). (6) Pilot evidence link in §1 corrected from 252 → 250 candidates to match `tiles.json` and §3. |
| 2026-04-29 | §11 Open questions: added #5 on `Structure Entrance` wall-snapping. Current pipeline emits each entrance node at the City's verbatim lat/lon (standalone, not snapped to any building way); asking the community whether to keep that, snap-coordinate-only to the nearest wall, or attempt full attachment (latter exits create-only scope). |
| 2026-04-29 | §11 Open questions: added #6 on housenumber-range rows. Source has 1,639 ranges (incl. 49 lettered) with no parity flag and a single coordinate per range; current pipeline skips them. Asking the community whether to keep skipping, upload the verbatim `lo-hi` string on the source-provided point, or expand into per-housenumber nodes (which requires synthesising coordinates and choosing a step rule that handles the 22 cluster-style and 49 lettered exceptions). |
