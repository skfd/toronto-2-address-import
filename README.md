# t2-address-import

Local tool that reads Toronto address points from the sibling
[`toronto-addresses-import`](../toronto-addresses-import) project's SQLite DB,
conflates them against live OSM data, routes questionable items to a human
reviewer via a web UI, and uploads approved batches to the OpenStreetMap
**dev sandbox** (`master.apis.dev.openstreetmap.org`). Every auto and manual
action is written to an append-only audit log.

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

## First end-to-end run

1. **Create a run** from the dashboard. A small downtown rectangle like
   `(43.645, -79.42, 43.665, -79.39)` keeps it tight.
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

## Writing a new check

1. Create `t2/checks/<name>.py` exporting a class that matches the `Check`
   protocol in `t2/checks/base.py`.
2. Register it in `t2/checks/__init__.py`.
3. Restart the app. The new check appears in the run's toggle list.
