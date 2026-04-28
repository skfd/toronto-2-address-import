"""Hover-tooltip text for UI vocabulary. Looked up via the `tip()` Jinja global."""

GLOSSARY: dict[str, str] = {
    # core entity (Candidate and AddressMatch are synonyms — see README "Terminology")
    "entity.candidate": "One input-CSV row paired with its OSM lookup result; the unit flowing through the pipeline. Synonym: AddressMatch.",
    "entity.address_match": "Synonym for Candidate — used in discussion; code/DB use 'candidate'.",

    # review statuses
    "status.OPEN": "Awaiting operator decision.",
    "status.APPROVED": "Operator approved — will be uploaded in the next batch.",
    "status.APPROVED_override": "Operator approved after the pipeline auto-approved or flagged it.",
    "status.REJECTED": "Operator rejected — will not be uploaded.",
    "status.REJECTED_override": "Operator rejected something the pipeline had auto-approved.",
    "status.DEFERRED": "Decision postponed; stays in the review queue.",
    "status.AUTO_APPROVED": "Pipeline auto-approved: MISSING verdict with no flags.",

    # conflation verdicts
    "verdict.MISSING": "Not found in OSM — eligible for upload.",
    "verdict.MATCH": "Same housenumber+street already in OSM within the near threshold — skip.",
    "verdict.MATCH_FAR": "Same housenumber+street exists in OSM but unusually far from this candidate — needs review.",
    "verdict.SKIPPED": "Address range or duplicate — held for reference, not uploaded.",

    # tag-diff states
    "diff.SAME": "OSM already has this exact value.",
    "diff.CHANGE": "Proposed value differs from OSM — will be written on upload.",

    # pipeline stages
    "stage.REVIEW_PENDING": "Flagged by a check; awaiting operator decision.",
    "stage.APPROVED": "Cleared for upload.",
    "stage.SKIPPED": "Not uploading (MATCH, range, or duplicate).",
    "stage.BATCHED": "Already included in an upload batch.",

    # batch statuses
    "batch.draft": "Composed but not yet sent.",
    "batch.pending": "Upload in progress.",
    "batch.uploaded": "Successfully uploaded to OSM as a changeset.",
    "batch.failed": "Upload failed — see audit log.",
    "batch.needs_attention": "Partial success / manual follow-up required.",

    # OSM tag keys
    "tag.addr:housenumber": "Building number, e.g. 123 or 10A.",
    "tag.addr:street": "Street name.",
    "tag.addr:city": "City or municipality.",
    "tag.addr:postcode": "Postal code.",
    "tag.addr:unit": "Unit or apartment number.",

    # check reason codes
    "reason.match_far": "Same housenumber+street exists in OSM, but the matched element is unusually far from the candidate coordinates.",
    "reason.range": "Address range (e.g. 10–14) — reference only, not uploaded.",
    "reason.colocated_land": "Non-Land row shares an address with a Land sibling in the same source — the Land row is the canonical record.",
    "reason.suspicious_suffix": "Suffix letter looks like a digit (I↔1, O↔0, Q↔0) — likely a data-entry typo.",
    "reason.city_duplicate": "Another candidate in this run sits within a few metres.",
    "reason.intra_source_duplicate": "Another Land row in the source has the same address_full + municipality but sits elsewhere. Unlike city_duplicate (3 m, any class), this is address-keyed and Land-only; <5 m pairs are silently deduped during conflation, so anything flagged here is 5 m or farther.",
    "reason.spot_check": "Randomly sampled MISSING address for manual QA.",
    "reason.plain_number": "Plain housenumber — no suffix or range.",
    "reason.unique_location": "No nearby duplicates in the input.",
    "reason.not_sampled": "Not selected in this sampling round.",
    "reason.potential_amenity": "Matched OSM node carries non-address tags — may actually be a POI we should exclude. Review to refine the POI filter.",
    "reason.pure_address": "Matched OSM node has only addr:* tags — nothing to flag.",

    # POI acknowledgment pills
    "pill.poi_acknowledged": "A shop/amenity node sits at this address but isn't a canonical address feature — ignored for matching.",
    "pill.postcode_from_poi": "Postal code copied from the nearby POI node; included in the proposed tags.",
    "pill.address_class": "Source address class. Non-Land rows (Structure, Structure Entrance, Land Entrance) mark building centroids, doors, or driveways rather than the parcel.",
    "pill.intra_source_duplicate": "Another Land row in the source has the exact same address_full + municipality. Click to jump to the sibling.",
    "pill.municipality": "This address_full also exists in another former municipality in this run — the municipality badge disambiguates the two rows.",

    # severities
    "severity.warn": "Likely problem — requires attention.",
    "severity.info": "Informational — surfaced for context only.",

    # check verdicts
    "checkverdict.FLAG": "Check raised a concern — candidate routed to review.",
    "checkverdict.PASS": "Check passed without raising a concern.",

    # buttons — pipeline stages
    "btn.ingest": "Load the input CSV into the candidates table.",
    "btn.fetch": "Download the OSM snapshot for this bbox and build the spatial index.",
    "btn.conflate": "Match candidates to OSM using distance + fuzzy name logic.",
    "btn.checks": "Run all enabled checks and flag candidates for review.",
    "btn.run_all": "Run Ingest → Fetch → Conflate → Checks in one click. Each stage is idempotent; re-running is safe.",

    # pipeline stepper status
    "stage-status.pending": "This stage has not produced output yet for this run.",
    "stage-status.done": "This stage has produced output. Re-running resumes or refreshes idempotently.",

    # buttons — batches
    "btn.compose_batch": "Bundle APPROVED candidates into an upload batch.",
    "btn.export_osm": "Write a .osm XML file for manual editing in JOSM.",
    "btn.upload_api": "Upload the batch to OSM as a changeset via the API.",

    # buttons — review actions
    "btn.approve": "Mark this candidate for upload.",
    "btn.reject": "Mark this candidate as not-for-upload.",
    "btn.defer": "Postpone the decision; stays in the review queue.",
    "btn.toggle_check": "Enable or disable this check for the current run.",

    # form fields & metrics
    "field.mode": "josm_xml = write a .osm file for JOSM; osm_api = upload directly to OSM.",
    "field.size": "Maximum candidates per batch (default 300).",
    "field.bbox": "Bounding box: min_lat, min_lon → max_lat, max_lon.",
    "field.source_snapshot": "OSM snapshot identifier this run was conflated against.",
    "field.note": "Optional free-text comment explaining the operator decision.",
    "field.missing_sample_every_nth": "One in every N MISSING candidates is flagged for operator spot-check. 50 by default; 0 disables. Per-run override; applies the next time checks run.",
    "metric.nearest_dist_m": "Distance in metres to the nearest OSM feature.",

    # local OSM extract view
    "field.osm_source": "Where stage 2 reads OSM from: 'local' uses the cached extract, 'overpass' queries the live API.",
    "field.osm_pbf_url": "Geofabrik URL we download the raw PBF from.",
    "field.osm_source_last_modified": "Last-Modified header Geofabrik reported for the PBF when we last downloaded it.",
    "field.osm_downloaded_at": "Wall-clock time of our last successful refresh.",
    "field.osm_pbf_sha256": "SHA-256 of the downloaded PBF.",
    "field.osm_json_sha256": "SHA-256 of the filtered address JSON.",
    "field.osm_element_counts": "Feature counts after filtering: nodes + ways kept, relations skipped (no centroid), features outside the Toronto bbox.",
    "field.osm_toronto_bbox": "Clip bbox applied after tag filtering — features outside are discarded.",
    "field.osm_filter_duration": "Time spent running pyosmium over the PBF.",
    "status.osm.fresh": "Extract is present and refreshed recently.",
    "status.osm.stale": "Extract exists but hasn't been refreshed in a while — consider a refresh.",
    "status.osm.missing": "No extract on disk yet — run a refresh to populate it.",
    "status.osm.running": "A refresh subprocess is currently running.",
    "btn.osm_refresh": "Start a refresh subprocess. Skips the download if Geofabrik is unchanged since the last refresh.",
    "btn.osm_refresh_force": "Start a refresh subprocess and re-download even if Geofabrik is unchanged.",
}
