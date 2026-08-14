---
description: Publish a credential-scrubbed snapshot of the living tool.db as a dated GitHub release
argument-hint: [YYYYMMDD]
---

Publish a snapshot of the living `data/<slug>/tool.db` of the configured `[city]` (pilot + citywide import
plus all merged maintenance) as a dated GitHub release asset. Run this after
finalizing a maintenance month.

## Usage

```
/publish-db                # date-stamp = today
/publish-db 20260608       # explicit date stamp
```

## What it does

1. Confirm the web app is stopped (it holds `tool.db`; `VACUUM` needs a clean
   read lock). If the engine's `run.py` is running, stop it first.
2. Build the artifact — the script lives in the engine checkout
   `../address-importer-friend`, run it from there with this repo as the city
   dir: `T2_CITY_DIR=<this repo> python -m scripts.publish_db [--date <YYYYMMDD>]`. This
   compacts the DB into `data/release/tool-db-<date>.db`, **deletes the OAuth /
   PKCE rows from `kv`** (keeping the maintenance watermark), self-verifies that
   no credential rows remain, and xz-compresses it to
   `data/release/tool-db-<date>.db.xz`.
3. **Independently verify the scrub before upload** (the credential leak is the
   one unforgivable failure here):
   ```
   xz -dc data/release/tool-db-<date>.db.xz > /tmp/check.db   # or python -m lzma
   sqlite3 -readonly /tmp/check.db "SELECT key FROM kv;"
   ```
   The output must contain `maintenance.watermark_snapshot` and **no**
   `osm_oauth*` or `pkce:*` keys. Delete the temp copy afterward.
4. Upload using the `gh release create` command the script prints — run it
   from **this repo** (the release belongs to the city repo, not the engine), e.g.:
   ```
   gh release create tool-db-<date> "data/release/tool-db-<date>.db.xz" \
     --title "tool.db snapshot <date>" \
     --notes "Credential-scrubbed living DB snapshot."
   ```
5. Report the release URL.

## Notes

* The artifact (~124 MB) lives under `data/release/` (gitignored). Pruning old
  dated releases/assets is the operator's call — they are not auto-deleted.
* The published snapshot is a **read-only reference**: it has no stored OSM
  auth, so re-pointing the running tool at it is not supported.

## Rules

* Never upload an artifact you have not verified is credential-free (step 3).
* Don't use emojis in scripts or output.
* If the build script aborts (e.g. it found credential rows after scrub),
  surface the error verbatim — never force-upload past it.
