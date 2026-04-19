"""Hover-tooltip text for UI vocabulary. Looked up via the `tip()` Jinja global."""

GLOSSARY: dict[str, str] = {
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
    "reason.suffix": "Suffixed number (e.g. 10A) — may duplicate a plain base number.",
    "reason.city_duplicate": "Another candidate in this run sits within a few metres.",
    "reason.spot_check": "Randomly sampled MISSING address for manual QA.",
    "reason.plain_number": "Plain housenumber — no suffix or range.",
    "reason.unique_location": "No nearby duplicates in the input.",
    "reason.not_sampled": "Not selected in this sampling round.",

    # POI acknowledgment pills
    "pill.poi_acknowledged": "A shop/amenity node sits at this address but isn't a canonical address feature — ignored for matching.",
    "pill.postcode_from_poi": "Postal code copied from the nearby POI node; included in the proposed tags.",

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
    "metric.nearest_dist_m": "Distance in metres to the nearest OSM feature.",
}
