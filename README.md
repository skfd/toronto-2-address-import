# t2-address-import

[GitHub](https://github.com/skfd/toronto-2-address-import) · [Pilot evidence site](https://skfd.github.io/toronto-2-address-import/) · [OSM community discussion](https://community.openstreetmap.org/t/address-import-for-toronto/119368) · MIT licensed

Local tool that reads Toronto address points from the sibling
[`toronto-addresses-import`](https://github.com/skfd/toronto-addresses-import) project's SQLite DB,
conflates them against live OSM data, routes questionable items to a human
reviewer via a web UI, and uploads approved batches to the OpenStreetMap
**dev sandbox** (`master.apis.dev.openstreetmap.org`). Every auto and manual
action is written to an append-only audit log.

## Terminology

**Candidate** and **AddressMatch** are synonyms — both refer to one row from
the input CSV paired with its OSM lookup result, the unit flowing through the
pipeline. Code, DB schema, and templates use `candidate`; discussion and new
docs may use either term. Each one carries three orthogonal axes:

- **`verdict`** — what conflation decided (`MATCH`, `MATCH_FAR`, `MISSING`, `SKIPPED`)
- **`status`** — what the operator decided (`OPEN`, `APPROVED`, `REJECTED`, `DEFERRED`); `AUTO_APPROVED` is a synthetic status the review queue derives for clean MISSING rows that bypass manual review
- **`stage`** — where it sits in the pipeline (`INGESTED`, `CONFLATED`, `CHECKED`, `REVIEW_PENDING`, `APPROVED`, `REJECTED`, `BATCHED`, `UPLOADED`, `FAILED`, `SKIPPED`)

A **Run** is one execution of the pipeline (produces many candidates); a
**Batch** is a bundle of `APPROVED` candidates packaged for upload.

## Setup

1. **Python 3.11+** (uses `tomllib`).
2. From the project root:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate    # PowerShell / cmd
   pip install -e .
   ```
3. **Register an OAuth2 application** on the OSM dev server:
   - Log into <https://master.apis.dev.openstreetmap.org/>.
   - My Settings → OAuth 2 applications → **Register new application**.
   - Name: anything (e.g. `t2-address-import-dev`).
   - Redirect URI: `http://localhost:5000/oauth/callback`
   - Permissions: tick **read user preferences**, **modify the map**,
     **comment on changesets**.
   - Save; copy the resulting Client ID and Client Secret.
4. **Create `.env`** (copy `.env.example`) and fill in:
   ```
   OSM_CLIENT_ID=...
   OSM_CLIENT_SECRET=...
   FLASK_SECRET_KEY=<any random string>
   FERNET_KEY=<generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
   ```
5. Adjust `config.toml` if your sibling DB lives somewhere else or you want a
   different default bbox.

## Run

```bash
python run.py
```

Then visit <http://localhost:5000/>.

## Local OSM extract (default source)

Stage 2 reads addresses from a locally-cached Toronto extract instead of
querying Overpass every time. First-time setup:

```bash
python -m t2.osm_refresh
```

This downloads the latest Ontario PBF from Geofabrik (~600 MB) into
`data/osm/ontario-latest.osm.pbf`, filters it to `addr:housenumber`-tagged
features clipped to the City-of-Toronto bbox in `config.toml`, and writes
`data/osm/toronto-addresses.json` + a `meta.json` sidecar. Stage 2 then just
bbox-clips that JSON per run — no network, sub-second.

Re-run whenever you want a fresher snapshot. The tool HEAD-checks Geofabrik
and skips the download if `Last-Modified` hasn't changed; pass `--force` to
re-download regardless. `--dry-run` does only the HEAD check.

You can also trigger a refresh from the web UI at <http://localhost:5000/osm>.
The page shows the extract's freshness, element counts, sha256s, and tails
`data/osm/refresh.log` so you can watch progress. The button spawns the same
CLI as a detached subprocess, so Flask stays responsive while the download
runs.

To fall back to live Overpass queries (e.g. bbox experiments outside
Toronto), set `[osm] source = "overpass"` in `config.toml`.

## Tile layer (run area picker)

Toronto is too big to pick by typing lat/lon, so the tool precomputes a tile
layer you can click on. Generate it once with:

```bash
python -m t2.tiles_build
```

This downloads the City of Toronto's 158-neighbourhood polygon layer from
[Open Data](https://open.toronto.ca/dataset/neighbourhoods/), counts active
source addresses inside each polygon, and quadtree-splits any neighbourhood
with more than 500 addresses. The result (typically ~2,500 tiles) lands in
`data/tiles.json` + a `data/tiles/meta.json` sidecar. Regenerate when a new
source snapshot lands.

The dashboard's **Pick on map** button opens `/map` — click any tile to land
on its detail page, which lists prior runs on that tile and has a "Start new
run" form pre-filled with the tile's bbox. The manual bbox form on the
dashboard remains as an escape hatch for arbitrary rectangles.

## First end-to-end run

1. **Create a run** from the dashboard. Either **Pick on map** and click a
   tile, or type a small downtown rectangle like
   `(43.645, -79.42, 43.665, -79.39)` into the bbox form.
2. On the run page, click the four pipeline buttons in order:
   **Ingest → Fetch OSM → Conflate → Run checks**.
3. Open the **Review queue** — items flagged by any enabled check land here.
   Approve, reject, or defer each. MISSING candidates with no flags are
   auto-approved; MATCH candidates are auto-skipped.
4. Back on the run page, **Compose batch** (mode `josm_xml` or `osm_api`,
   size up to 500 for first run).
5. On the batch page:
   - `Export .osm (JOSM)` writes `data/batch_<id>.osm`. Open it in JOSM,
     then upload via JOSM's own auth.
   - `Upload via OSM API` opens a changeset on the dev server, uploads the
     osmChange diff, and closes the changeset. Visit
     `/oauth/start` first if you haven't authorized yet.
6. The **Audit log** at `/runs/<id>/audit` shows every event.

## Resumability

Every candidate has a `stage` column. Killing the process mid-run and
restarting is safe — each stage skips work already done:

- Re-running **Ingest** only adds new rows (`INSERT OR IGNORE`).
- Re-running **Fetch** reuses the cached `data/osm_current_run<id>.json`.
- Re-running **Conflate** resumes from any candidate still at `INGESTED`.
- Re-running **Checks** skips any `(candidate, check_id, check_version)` that
  already has a result row. Bump a check's `version` in code to force rerun.
- **Uploads** look up prior changesets by their `import:client_token` tag
  before opening a new one.

## How conflation decides

Match targets are **pure address nodes** (`addr:housenumber` + no POI tags) and
**polygons** (ways/relations with an address — typically buildings, including
amenity-tagged footprints like a hospital).

**POI nodes** (nodes carrying `amenity`, `shop`, `office`, `tourism`, `leisure`,
`craft`, `healthcare`, `building`, plus `disused:*` / `was:*` variants — see
`POI_TAG_KEYS` in `t2/conflate.py`) are **ignored** for matching: their address
is a courtesy annotation, not the canonical address feature. When a POI sits at
a MISSING candidate's address, the review UI acknowledges it with a pill, and
any `addr:postcode` on the POI is copied into the proposed upload tags.

Even after that filter, a matched "pure address" node can quietly carry
non-address tags (`name`, `ref`, `entrance`). The `potential_amenity` check
flags those with `severity=info` so we can refine the POI filter over time.
Metadata keys like `source`, `opendata:type`, `check_date`, `note` are on an
ignore list inside the check and don't trigger it.

## Out of scope (possible next phase)

The current pipeline is one-directional: Toronto source → OSM lookup → upload
additions. Two cleanup flows in the opposite direction are **explicitly out of
scope** and left for a later phase. Documented here so reviewers don't assume
they were overlooked.

### Removing OSM addresses absent from Toronto source

If OSM has an address that Toronto's active snapshot doesn't, we do not flag,
propose, or remove it.

Reasoning — the absence direction is asymmetric. Toronto's open data is
authoritative when it asserts an address exists; silence is a weaker signal.
The feed has refresh lag, known-missing neighborhoods, and retired-address
states that aren't cleanly separable from "never existed." Deleting OSM data
based on absence alone would destroy real addresses on worse evidence than we
accept for additions.

A future phase would need, at minimum: a reverse-sweep stage enumerating OSM
addresses in the run bbox; a separate review queue (not `Candidate` — the
verdicts don't fit); a street-level cross-check to suppress the common case
where Toronto's feed is missing a whole street; prioritization by OSM metadata
(`start_date`, last-edit age, `source`); and human-only approval — no
automation, since OSM deletions are high blast radius and hard to reverse.

### Removing `addr:interpolation` ways

OSM `addr:interpolation` ways synthesize housenumbers along a street segment
between two endpoint nodes. When Toronto's per-address points cover the same
segment with real data, the interpolation way is technically redundant. We
still don't touch them.

Reasoning — an interpolation way isn't an address, it's a geometry-anchored
range declaration. Our matching model (housenumber + street + point) doesn't
describe what's being replaced. Replacement needs cross-validation: every
integer in the interpolation range must have a real Toronto point before
removal, otherwise the delete leaves mapped gaps. It's also a bulk structural
edit to OSM, not an address-import operation — different review bar, different
changeset hygiene, different rollback story than what this tool was built for.

A future phase would need: enumeration of `addr:interpolation` ways in the
bbox; coverage check that every integer in the range has a colocated Toronto
point; a proposed delete-way-plus-preserve-endpoints changeset for human
review; and care around tags (`addr:street`, `addr:postcode`) that the
interpolation way carries on behalf of its endpoints.

### Why defer both

The shipping scope — "get Toronto's missing civic addresses into OSM without
creating duplicates" — has standalone value. Folding cleanup into the same
pipeline expands blast radius and review burden without proportional benefit,
and the two reverse flows have different enough semantics (different data
sources, different review criteria, different failure modes) that they
deserve their own pipelines when we get to them.

## Writing a new check

1. Create `t2/checks/<name>.py` exporting a class that matches the `Check`
   protocol in `t2/checks/base.py`.
2. Register it in `t2/checks/__init__.py`.
3. Restart the app. The new check appears in the run's toggle list.

## Data sources & attribution

This tool moves data between three open datasets. Downstream uploads inherit OSM's licence, but the upstream sources each have their own terms:

- **Toronto Open Data** — "Address Points (Municipal) – Toronto One Address Repository", published under the [Open Government Licence – Toronto](https://open.toronto.ca/open-data-licence/). Consumed indirectly via the sibling [`toronto-addresses-import`](https://github.com/skfd/toronto-addresses-import) project.
- **OpenStreetMap** — © OpenStreetMap contributors, [ODbL 1.0](https://www.openstreetmap.org/copyright). All uploads target the OSM **dev sandbox** (`master.apis.dev.openstreetmap.org`); any future production import must separately comply with the OSMF [import guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines) and [contributor terms](https://osmfoundation.org/wiki/Licence/Contributor_Terms).
- **Geofabrik** — Ontario `.osm.pbf` extracts, redistributed under ODbL from OSM.

## License

MIT — see [LICENSE](LICENSE).
