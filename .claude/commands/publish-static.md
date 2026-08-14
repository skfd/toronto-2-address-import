---
description: Regenerate the static GitHub Pages site under docs/pilot/
argument-hint: [run_id]
---

Regenerate the static GitHub Pages site under `docs/pilot/` so the data
exploration pages (`/streets`, `/source/multi`, `/osm/multi*`, `/osm`,
`/data`) reflect the current OSM extract and source open-data snapshot.

## Usage

```
/publish-static                # auto-pick run id (latest uploaded, else latest)
/publish-static <run_id>       # explicit run id
```

## What it does

0. All `python -m t2.*` commands below run from the engine checkout
   `../address-importer-friend` with `T2_CITY_DIR` set to this repo.
1. Refresh OSM extract: `python -m t2.osm_refresh`. If the result reports
   `fresh`, skip step 2. If `stale` or `missing`, force a re-download.
2. Regenerate streets analysis: `python -m t2.streets`.
3. Pick the run id (if not passed):
   - SQL: `SELECT run_id FROM runs WHERE upload_status='uploaded' ORDER BY created_at DESC LIMIT 1`
   - Fall back to the most recent run if none uploaded.
4. Run static export: `python -m t2.static_export --run <id> --out <this repo>/docs/pilot`
   (pass the absolute path — a relative `--out` would land in the engine checkout).
5. Show `git status docs/` so the user can eyeball the diff before pushing.
6. Stop. Do **not** commit or push — that's `/gh-push`'s job.

## Notes

* The source open-data DB lives in the sibling `toronto-addresses-import`
  repo. Refresh it there *before* invoking this skill if you want the
  `/source/multi` and `/streets` pages to reflect a newer snapshot.
* The skill bundles per-run review pages alongside the exploration pages
  because the export script ties them together. The run id is just a
  necessary parameter — the focus is the comparison data.
* Output lands under `docs/pilot/`. GitHub Pages serves whatever's there
  on the next push to `main`.

## Rules

* Don't use emojis in scripts or output.
* Don't run `git commit` or `git push`.
* Don't refresh the source DB; that's the sibling repo's job.
* If the export script errors out, surface the error verbatim — don't
  silently retry or paper over it.
