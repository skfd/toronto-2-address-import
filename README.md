# toronto-2-address-import

[Pilot evidence site](https://skfd.github.io/toronto-2-address-import/) · [OSM community discussion](https://community.openstreetmap.org/t/address-import-for-toronto/119368) · [Wiki page](https://wiki.openstreetmap.org/wiki/Toronto/Import/AddressPoints) · MIT licensed

The **Toronto city checkout** of the address-import family: the import
proposal and its evidence, Toronto's `config.toml`, and (locally, gitignored)
the living `tool.db` recording everything this import has pushed to OSM.

The pipeline itself — conflation, review UI, upload, maintenance — lives in
the engine repo, [`address-importer-friend`](https://github.com/skfd/address-importer-friend),
which was forked from this repo on 2026-08-13 with full history. This repo
was then slimmed to the Toronto-specific remainder. Milestones of the import
are tagged here: `import-start` (2026-05-13), `import-complete` (2026-05-28),
`maint-1` (2026-06), `maint-2` (2026-07).

## Running the tool against this checkout

```bash
cd ../address-importer-friend
python run.py --city-dir ../toronto-2-address-import          # dev sandbox
python run.py --city-dir ../toronto-2-address-import --prod   # real OSM
```

Setup (OAuth apps, `.env.dev` / `.env.prod` from the `.example` files) is
documented in the engine's README; the credential files and all working state
live **here**: `config.toml`, `.env.*`, `data/toronto/tool.db`, `data/osm/`
(the cached Ontario extract), tiles, and run caches.

## Import status

| Stage | State |
|---|---|
| Draft proposal | [Complete](IMPORT_PROPOSAL.mediawiki) (last revised 2026-05-28) |
| Wiki page (`Toronto/Import/AddressPoints`) | [Published 2026-05-01](https://wiki.openstreetmap.org/wiki/Toronto/Import/AddressPoints) |
| OSM Community Forum announcement | Posted 2026-05-01 — [thread](https://community.openstreetmap.org/t/address-import-for-toronto/119368) (tagged `import`; the [Import Guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines) route announcements through the forum now, not the deprecated `imports@` list) |
| 14-day feedback window | Closed 2026-05-15 (measured from wiki publication; discussion resolved) |
| Phase 1 pilot upload (production) | Completed 2026-05-13 — [changeset 182585291](https://www.openstreetmap.org/changeset/182585291) (tile `high-park-swansea-sw-se`, 176 uploaded, 72 skipped, 4 rejected) |
| Phases 2 + 3 (citywide rollout) | Completed 2026-05-28 — all 1,297 tiles processed; 1,297 changesets (`182585291` … `183305851`) on the [`skfd imports`](https://www.openstreetmap.org/user/skfd%20imports/history) account; ~449k addresses uploaded, ~311k skipped (mostly already in OSM), ~9.2k operator-rejected. Day-by-day notes in [`blog.md`](blog.md). |
| Phase 4 — closeout | In progress. [Cumulative upload manifest](https://skfd.github.io/toronto-2-address-import/pilot/uploads/all.csv) published. Post-import report on the community forum + wiki page pending. 90-day post-import monitoring window (per § Open questions #2 of the proposal) runs through 2026-08-26. |
| Monthly maintenance | Ongoing — `maint-snap56` + catch-up (2026-06), `maint-snap90` (2026-07). Run from the engine's maintenance page against this checkout. |
| Post-import follow-ups (separate proposals) | (a) `source` → `addr:source` tag rewrite. (b) MapRoulette challenge for ~1,580 OSM buildings with `addr:housenumber` but no street anchor. (c) Interpolation-cleanup mapping party — separate forum thread, organized by Toronto local mappers. Sketches live in the engine repo's `future-work/`. |

Production uploads were made from the dedicated
[`skfd imports`](https://www.openstreetmap.org/user/skfd%20imports) OSM
account (not the maintainer's personal account).

## The living database

There is **one living canonical `tool.db`** (~2 GiB, `data/toronto/tool.db`,
gitignored): the pilot + citywide import plus every monthly-maintenance run
folded in — 1,300 runs, 768,927 candidates, 1,299 uploaded changesets, and
the full human-review record. It is published periodically as a dated,
credential-scrubbed release asset on **this repo**
(`tool-db-<YYYYMMDD>.db.xz`) via the `/publish-db` command.

```bash
gh release download tool-db-<YYYYMMDD>
xz -d tool-db-<YYYYMMDD>.db.xz
sqlite3 -readonly tool-db-<YYYYMMDD>.db \
  "SELECT run_id, changeset_id FROM runs WHERE upload_status='uploaded' LIMIT 5;"
```

## Data sources & attribution

- **Toronto Open Data** — "Address Points (Municipal) – Toronto One Address
  Repository", published under the
  [Open Government Licence – Toronto](https://open.toronto.ca/open-data-licence/).
  Consumed indirectly via the sibling
  [`ontario-address-changes`](https://github.com/skfd/ontario-address-changes) tracker.
- **OpenStreetMap** — © OpenStreetMap contributors, ODbL.

## License

MIT for everything in this repo (docs, config, pilot site).
