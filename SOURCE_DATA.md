# Source Data Reference

Everything we know about the sibling `toronto-addresses-import` SQLite DB
(`addresses.db`) as it's consumed by this pipeline. Figures verified on
snapshot **#28** (latest non-skipped) on 2026-04-18.

## Database

- Path: `../toronto-addresses-import/addresses.db` (configured in `config.toml`
  → `source.sqlite_path`).
- Access: read-only (`t2/source_db.py → connect_readonly()`).
- Tables:
  - `snapshots` — one row per scrape. Snapshot #28 is the current active one.
    Rows with `skipped = 1` are ignored.
  - `addresses` — one row per `(address_point, snapshot_range)`. Rows persist
    across snapshots via `min_snapshot_id` / `max_snapshot_id`. The active
    set is `max_snapshot_id = (SELECT MAX(id) FROM snapshots WHERE skipped=0)`.

### `addresses` schema (relevant columns)

| Column | Notes |
|---|---|
| `address_point_id` | Stable key from source. Unique per live row. |
| `address_full` | Pre-rendered civic address, e.g. `46A Amelia St`. |
| `address_number` | Just the number part (may include a suffix, `46A`, `710 1/2`). |
| `lo_num`, `lo_num_suf`, `hi_num`, `hi_num_suf` | Populated when the record is an address *range*; otherwise lo==hi. |
| `linear_name_full`, `linear_name`, `linear_name_type`, `linear_name_dir` | Street name components. |
| `municipality_name` | Pre-amalgamation municipality (e.g. `Toronto`, `Etobicoke`, `North York`). **Required for disambiguation** — the same `address_full` can exist in multiple former municipalities. |
| `ward_name` | Current ward. |
| `latitude`, `longitude` | WGS84, 6 decimals. |
| `extra` | JSON blob. See next section. |

### `extra` JSON keys

Present on every active row. Keys we rely on:

- `ADDRESS_CLASS_DESC` — one of `Land`, `Structure`, `Structure Entrance`,
  `Land Entrance`. Governs everything downstream.
- `ADDRESS_CLASS` — integer code for the same thing.
- `ADDRESS_ID`, `ADDRESS_POINT_ID_LINK`, `ADDRESS_ID_LINK` — identity + parent
  pointer (see "Relationships" below).
- `ADDRESS_STATUS` — source-side lifecycle (not currently used by the pipeline).
- `ADDRESS_STRING_ID`, `LINEAR_NAME_ID`, `CENTRELINE_ID` — joins into other
  source tables (not exposed in this DB).
- `CENTRELINE_MEASURE`, `CENTRELINE_OFFSET`, `CENTRELINE_SIDE` — position
  relative to the road centreline.
- `CLASS_FAMILY_DESC` — redundant summary, e.g. `Land, Structure, Structure
  Entrance`.
- `GENERAL_USE` — always `Unknown` in practice; don't rely on it.
- `PLACE_NAME`, `PLACE_NAME_ALL` — always empty on entrance rows sampled so
  far; **we cannot use source data to label multi-entrance buildings.**
- `MAINT_STAGE`, `OBJECTID`, `MUNICIPALITY`, `WARD` — provenance /
  housekeeping.

## Address classes

Four values appear in `ADDRESS_CLASS_DESC`. Counts are **active snapshot**,
not lifetime (lifetime counts are 3–4× higher because historical rows carry
the same `address_point_id` across snapshots).

| Class | Active rows | Meaning | OSM analogue |
|---|---:|---|---|
| `Land` | 479,966 | Parcel-level point. The canonical "this lot has this address". | Standalone address node (`addr:housenumber` + `addr:street`). |
| `Structure` | 28,031 | Building centroid. | Belongs on the `building=*` polygon; we treat as an address node. |
| `Structure Entrance` | 14,354 | Door-level point on a building outline. | Node on the building way tagged `entrance=yes`. |
| `Land Entrance` | 573 | Driveway / gate / parcel entry. | Usually `barrier=gate`; not an addressing concept in OSM. |

### Key finding: non-Land classes carry unique addresses

We initially assumed non-Land rows were redundant with Land siblings at the
same address. Verified on snapshot #28 using `(address_full,
municipality_name)` match + ≤50 m coord distance (haversine):

| Class | Total | No Land twin (unique address) | Colocated with Land (≤50 m, true dup) | Same string, >50 m (false twin — diff municipality) |
|---|---:|---:|---:|---:|
| `Structure` | 28,031 | **27,740** | 276 | 15 |
| `Structure Entrance` | 14,354 | **14,341** | 13 | 0 |
| `Land Entrance` | 573 | **423** | 147 | 3 |

**Skipping non-Land classes would drop ~42,500 real civic addresses** from
the import. All four classes must flow through the pipeline.

The colocated-duplicates case (~436 rows city-wide) is small enough to
handle as a dedup pass in conflation rather than a source-side filter.

## Relationships

Cross-class linkage via `extra.ADDRESS_ID_LINK` (and `ADDRESS_POINT_ID_LINK`):
children point at their parent.

- A `Structure` links to its `Land`.
- A `Structure Entrance` links to its `Structure` (or directly to `Land`).
- A `Land Entrance` links to its `Land`.

### One parent can cover many civic addresses

`ADDRESS_ID_LINK = 1527363` (the Jamestown Cres / John Garland Blvd townhouse
complex) carries **117 active Structure Entrance rows** — one per townhouse
unit. Every row has a distinct `address_full`:

```
22 Jamestown Cres     22A Jamestown Cres    24 Jamestown Cres    26 Jamestown Cres
...
110 Jamestown Cres    110A Jamestown Cres   112 Jamestown Cres
116 Jamestown Cres    116A Jamestown Cres   118 Jamestown Cres
...
138 John Garland Blvd 140 John Garland Blvd 142 John Garland Blvd
```

So "117 entrances" does **not** mean "one building with 117 doors" — it means
one parcel containing 117 separately-addressable units that share a
`Land` parent.

### Source does not model multi-door single-address buildings

Grouping active `Structure Entrance` rows by `address_full` yields **zero**
cases with n ≥ 3. Suffixes like `A`, `B`, `1/2` live inside `address_number`
as separate civic addresses, not as entrance-letter labels on a shared
housenumber. The `PLACE_NAME` field that could carry entrance labels is
empty in all samples.

Practical consequence: we do not need logic for "an apartment block whose
entrances are labelled A/B/C all bearing `123 Main St`" — the source doesn't
represent that shape.

## Address ranges

`lo_num` / `hi_num` (+ their suffix columns) can differ on a single row to
represent a range (e.g. `100–110 Main St`). The `suffix_range` check in
`t2/checks/` inspects these. When present, `address_full` usually renders the
range verbatim. These are kept as `SKIPPED` by default — we don't upload
range addresses to OSM.

## Coordinates

- `latitude` / `longitude` are authoritative — use them.
- Entrances are positioned at the physical door; Structures at the building
  centroid; Land at the parcel centroid; Land Entrances at the driveway/gate.
- Cross-class dedup keys purely on `(address_full, municipality_name)` — no
  distance threshold. Within one former municipality the source treats one
  `address_full` as one civic address, so a non-Land sibling sharing that
  key is always a duplicate of the Land row regardless of how far apart the
  two coordinates sit.

## Municipality trap (pre-amalgamation)

Toronto absorbed the former municipalities (East York, Etobicoke, North
York, Scarborough, York, old Toronto) in 1998. Street names and
housenumbers recur across these. Example: `48 Victor Ave` has an active
`Land` in one former municipality and a `Structure Entrance` ~5 km away in
another — they are **different civic addresses** despite identical
`address_full`.

**Always include `municipality_name` when testing for duplicates.** Matching
on `address_full` alone produces false twins.

## Pipeline consumption points

- `t2/source_db.py:30 iter_active_addresses_in_bbox` — the read path. Today
  selects all classes indiscriminately. When we add class-aware handling,
  this is the join point.
- `t2/candidates.py` — ingest, stores each source row as a `Candidate` row
  (aka AddressMatch — see `glossary_address_match.md`).
- `t2/conflate.py` — matches against OSM; see "How conflation decides" in
  `README.md`. A future colocated-class dedup pass fits here.

## Reproducing the counts

```python
import sqlite3, json, math
c = sqlite3.connect('file:../toronto-addresses-import/addresses.db?mode=ro', uri=True)
c.row_factory = sqlite3.Row
snap = c.execute("SELECT MAX(id) FROM snapshots WHERE skipped=0").fetchone()[0]

# Active row count by class
from collections import Counter
cls = Counter()
for r in c.execute("SELECT extra FROM addresses WHERE max_snapshot_id=?", (snap,)):
    cls[json.loads(r['extra'])['ADDRESS_CLASS_DESC']] += 1
print(cls)
```

Building the Land index + twin check per class:

```python
land = {}
for r in c.execute("SELECT address_full, municipality_name, latitude, longitude, extra "
                   "FROM addresses WHERE max_snapshot_id=?", (snap,)):
    if json.loads(r['extra'])['ADDRESS_CLASS_DESC'] == 'Land':
        land.setdefault((r['address_full'], r['municipality_name']), []).append(
            (r['latitude'], r['longitude']))

def haversine(a, b, c_, d):
    R = 6371000
    p1, p2 = math.radians(a), math.radians(c_)
    dp, dl = math.radians(c_-a), math.radians(d-b)
    x = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(x))
```
