> Source: extracted verbatim from transcript `c5adb8fd-a758-4cbd-b5ac-5ff5c8baaf59.jsonl` — initial survey response before drilling into any specific finding.

# Operator-review gap analysis

## Context

You asked whether the t2 address-import pipeline should involve a human
operator in any cases beyond what it already does. I mapped the current
operator-review surface, hunted for silent automation, and probed
`data/tool.db` for evidence (7 runs, 75,429 candidates, all in the downtown
bbox / "former Toronto").

What is reviewed today (`t2/checks/`, `t2/pipeline.py:262-301`):

- `match_far` (warn) — verdict MATCH_FAR
- `suffix_range` (info) — ranges + I/O/Q digit-confusable suffixes
- `city_duplicate` (info) — two source points within 3 m
- `missing_sample` (info) — every Nth MISSING for spot-check
- `potential_amenity` (info) — matched node carries non-address tags
- All MATCH_FAR with no flags fall to `stage='CHECKED'` (cannot auto-clear)
- All MISSING with no flags are `AUTO_APPROVED`
- All MATCH and ranges silently `SKIPPED`

What follows are the gaps that are not covered, ranked by blast radius and
backed by evidence from the DB or the source code. **Output style chosen:
prioritized findings list, no per-item implementation plan.**

---

## Tier 1 — High blast radius, evidence in current data

### 1.1  Land–Land siblings at the same `address_full`

**Evidence.** In every recent run (run_id 2–7) there are **32 addresses**
with two `address_class='Land'` rows at the same `address_full +
municipality_name`. Concrete example from run 7: `108 Peter St` has two
Land entries — one matched OSM at 12.6 m (verdict MATCH → SKIPPED), the
other matched at 15.9 m (verdict MATCH_FAR → REVIEW_PENDING). Operator
will see the MATCH_FAR row with no hint that its sibling already matched.
Worse: when neither sibling matches OSM, both auto-approve and we will
upload the same civic address twice (e.g. `102 Peter St` → two MISSING,
two new nodes proposed).

**Why it slips through.** `t2/conflate.py:227-239 _colocated_land_sibling`
is asymmetric — it only suppresses non-Land rows that share a Land
neighbor. Two Land rows are mutually invisible to each other.

**Recommendation.** Add an "intra-source duplicate" review path. Either
(a) flag any candidate that shares `(address_full, municipality_name)` and
≤50 m haversine with another candidate in the same run, surfacing the
sibling's verdict so operators can pick one; or (b) extend the sibling
rule symmetrically and pick the canonical Land deterministically (e.g.
nearest to street centerline, or lowest `candidate_id`), auto-skip the
other. Option (a) is safer because it surfaces the choice instead of
guessing.

### 1.2  POI-derived `addr:postcode` propagated without operator visibility

**Evidence.** `t2/conflate.py:286-321` copies `addr:postcode` from a
same-address POI node onto the proposed upload tags whenever a MISSING
candidate has a POI hit. In `tool.db`, **2,800 MISSING candidates have a
POI acknowledgement** and **917 carry a `proposed_postcode`** sourced
purely from that POI. Source DB has no postcode column
(`source_db.py:36-44`), so this is the only source of postcode signal —
and it ships into the changeset with no review step. If the POI's
postcode is wrong (mistagged amenity, abandoned shop, etc.), we
propagate the error into OSM under `source="City of Toronto Open Data"`,
which is misleading provenance.

**Why it slips through.** No check inspects `proposed_postcode`. The
review queue UI shows it as informational only; the candidate auto-
approves on the basis of being MISSING + no flags.

**Recommendation.** Add a check `postcode_from_poi` (severity `info`)
that flags any MISSING candidate where `cf.proposed_postcode` is
non-null. This routes the row to operator review so the postcode is at
least seen once. Keeps changeset provenance honest. (This is also
prerequisite work for the deferred `postcode-enrichment` proposal in
`future-work/postcode-enrichment.md`.)

### 1.3  Stale review_items from removed/renamed checks pollute the queue

**Evidence.** `tool.db` has **4,156 review_items with `reason_code`
containing `conflict_30m`** plus **3,733 with `suffix`**, both legacy
names. `conflict_30m` is no longer in `t2/checks/__init__.py REGISTRY` at
all; `suffix` was renamed to `range`/`suspicious_suffix` in `c3fa226`.
The `ebfaed3` cleanup only fires when a check **runs and returns no
flag**; a check that's been **removed entirely** never runs, so its
review_items linger forever and the candidate stays at REVIEW_PENDING.
27 % of the OPEN review queue (4,156 / 15,364) is dead weight.

**Why it slips through.** Cleanup is per-(candidate, check) — there's no
sweep keyed on "review_items whose reason_code mentions a check that no
longer exists in REGISTRY."

**Recommendation.** At the start of `run_checks()`, sweep `review_items`
where every reason_code token is unknown to REGISTRY and either delete
them (with a `REVIEW_CLEARED reason=check_removed` audit event) or
re-stage the candidate based on its verdict. This is a one-liner-class
fix and will cut the operator queue by a quarter.

---

## Tier 2 — Quiet upload-correctness risks

### 2.1  Partial OSM upload silently marked "uploaded"

**Evidence.** `t2/osm_client.py:199-209 _upload_diff` parses the server's
`<diffResult>` into `mapping = {old_id: new_id}` and returns it. The
caller (`upload(batch_id)` lines 286-307) updates only the items present
in `mapping` and then sets `batches.status='uploaded'`. There is no
assertion that `len(mapping) == batch.size`. If OSM accepts 48/50 nodes
and silently drops 2 (or returns a partial response on 4xx without
raising), batch ends marked `uploaded` while two `batch_items` stay
`pending` and two candidates stay `BATCHED` with no operator signal.

No upload has happened in the test data yet (0 `CHANGESET_UPLOADED`
events), so we can't observe the failure mode in production — but the
code path is unguarded and the first real upload will exercise it.

**Recommendation.** After the mapping update, count items still
`upload_status='pending'` for that batch. If non-zero, set
`status='needs_attention'`, emit a `UPLOAD_PARTIAL` audit event with the
unmapped `local_node_id` list, and surface them as an operator queue.

### 2.2  Source-snapshot freshness not surfaced at run-create

**Evidence.** `t2/pipeline.py:99-103` checks that the source snapshot ID
hasn't changed between `start_run` and `ingest_stage`, but there is no
check on **how old** the snapshot itself is. The latest snapshot could
be 30 days old and the pipeline will happily run, ingesting addresses
that no longer exist or missing fresh additions. `SOURCE_DATA.md` notes
the source updates daily.

**Recommendation.** When showing the run-create form (or in `start_run`),
read `MAX(created_at) FROM snapshots WHERE skipped=0` and warn if older
than N days (suggest 14 to mirror OSM extract policy). One-line UI
indicator — the operator decides whether to proceed.

### 2.3  Stale OSM extract not gated at conflate time

**Evidence.** `t2/osm_refresh.py:72-95 extract_status()` already returns
`'stale'` when the local PBF is >14 days old, and the `/osm` page
displays it. But `conflate_stage` (`pipeline.py:122-126`) just calls
`fetch_stage` which loads whatever is on disk — no freshness gate. A
stale extract turns OSM additions made in the last 14 days into false
MISSINGs, which then auto-approve into duplicate uploads.

**Recommendation.** In `conflate_stage`, if `extract_status() == 'stale'`
or `'missing'`, refuse to proceed unless the operator passes an explicit
`force_stale=True` (or a UI confirmation toggle). Audit the override.

---

## Tier 3 — Lower frequency, surface-only

### 3.1  Cross-municipality address collisions at full-city scale

**Evidence.** `tool.db` is 100 % `municipality_name='former Toronto'`
because the test bbox is downtown. Source DB has 525,404 active
addresses across six former municipalities; `SOURCE_DATA.md:144-153`
warns that strings like `48 Victor Ave` exist in multiple
municipalities. The `_colocated_land_sibling` lookup is correctly keyed
on `(address_full, municipality_name)`, but the **review queue UI
(`review.queue` in `t2/review.py:104-176`) does not select or display
`municipality_name`** — operators would see two `48 Victor Ave` rows
and have no way to tell them apart.

**Recommendation.** Surface `municipality_name` as a column/badge in
review/approved/skipped lists when at least one row in the current run
has a colliding `address_full` in another municipality. Trivial join,
no new check needed.

### 3.2  Auto-approval has no operator-visible "I noticed this" trail

**Evidence.** `pipeline.py:296-301` emits `AUTO_APPROVED` audit events
(38,687 in `tool.db`) but the operator never explicitly acknowledges
them. `review.py:178-203 get_review_state` synthesizes
`status='AUTO_APPROVED'` on read so they show up if `include_auto=1`,
but only if the operator opens that filter. For 44,929 auto-approved
MISSINGs, operator discipline is the only safeguard.

**Recommendation.** Either (a) require operator to spot-check N % of
auto-approved before composing a batch (block batch composition until
N reviewed), or (b) raise the `missing_sample` rate from 1-in-50 to
something risk-tier-driven (1-in-20 for first run of a new bbox,
1-in-100 thereafter). Lightweight and behavioral, no schema work.

### 3.3  Equidistant OSM matches are non-deterministic

**Evidence.** `conflate.py:147-153` keeps `best_match` strictly less than
the prior best (`dist < best_match[0]`). Two OSM elements equidistant
from the candidate → first iterated wins. Iteration order comes from
`GridIndex.query` which depends on `dict` insertion order across grid
cells — stable across a single Python run but fragile under any
refactor. Probably very rare in practice (sub-meter ties), but
operator-invisible if it ever matters.

**Recommendation.** Either deterministic tiebreak (sort by `osm_id` as
a final key) or no action — note in `conflate.py` and move on. Not
worth a review queue entry.

---

## What I deliberately did NOT recommend

- **Reviewing every MISSING** — 44,929 auto-approved MISSINGs is the
  whole point of the import, manual review of all of them defeats the
  pipeline. Sampling (3.2) is the right knob.
- **Postcode-enrichment of MATCH nodes** — already documented as
  proposed-not-implemented in `future-work/postcode-enrichment.md`.
  Tier 1 #1.2 is a prerequisite-strength version of the same concern.
- **Reviewing the Ranges set** — already shipped as a read-only view at
  `/runs/<id>/ranges` (commits `92f2e18`, `c6edfe1`).
- **Boundary bbox edge cases / French street names / null coords** —
  no evidence in the data (0 nulls, no French cases in the downtown
  bbox). Worth re-evaluating after the first non-Toronto-core run.

---

## Suggested order of attack

1. **1.3** (stale review_items sweep) — smallest change, removes 27 %
   of operator queue noise immediately.
2. **2.1** (partial-upload guard) — must land before any production
   upload because the failure is silent and post-hoc reconciliation is
   painful.
3. **1.2** (`postcode_from_poi` check) — additive, catches a real-data
   risk we already see (917 candidates affected).
4. **1.1** (Land-Land sibling review) — needs a small design call (flag
   vs. dedup) but blocks a measurable duplicate-upload risk.
5. The Tier 2/3 items as time allows.

## Verification when items are implemented

- **1.3:** After running checks on `tool.db`, expect
  `SELECT COUNT(*) FROM review_items WHERE reason_code LIKE
  '%conflict_30m%'` → 0. New audit events `REVIEW_CLEARED
  reason=check_removed`.
- **2.1:** Smoke-test by composing a 2-item batch, mocking
  `_upload_diff` to return only 1 mapping; expect batch status
  `needs_attention` and an `UPLOAD_PARTIAL` event.
- **1.2:** New check appears in `checks_catalog`; new
  `review_items.reason_code = 'postcode_from_poi'` rows for the 917
  candidates with `cf.proposed_postcode IS NOT NULL` in run 7.
- **1.1:** Pick run 7 `108 Peter St`; both Land rows should appear in
  the review queue with sibling-info attached, instead of one in MATCH
  and one in MATCH_FAR.
