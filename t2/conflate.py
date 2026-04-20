"""Stage 3: conflate candidates against cached OSM snapshot, write verdicts to DB.

Core helpers (normalize_street, GridIndex, haversine) preserved from sibling
project's src/conflate.py — the algorithmic contract there is proven.
"""
import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from . import audit, db as _db, osm_fetch
from .osm_export import STATIC_TAGS

STREET_SUFFIXES = {
    "STREET": "ST", "ROAD": "RD", "AVENUE": "AVE", "BOULEVARD": "BLVD",
    "DRIVE": "DR", "LANE": "LN", "COURT": "CT", "PLACE": "PL",
    "TERRACE": "TER", "CRESCENT": "CRES", "SQUARE": "SQ", "GATE": "GTE",
    "CIRCLE": "CIR", "WAY": "WAY", "TRAIL": "TRL", "PARKWAY": "PKWY",
    "HIGHWAY": "HWY", "EXPRESSWAY": "EXPY",
}
DIRS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}


def normalize_street(name: str | None) -> str:
    if not name:
        return ""
    out = []
    for p in name.upper().replace(".", "").split():
        if p in STREET_SUFFIXES:
            out.append(STREET_SUFFIXES[p])
        elif p in DIRS:
            out.append(DIRS[p])
        else:
            out.append(p)
    return " ".join(out)


class GridIndex:
    def __init__(self, cell_size_deg: float = 0.002):
        self.grid: dict[tuple[int, int], list[tuple[float, float, dict]]] = defaultdict(list)
        self.cell_size = cell_size_deg

    def _key(self, lat: float, lon: float) -> tuple[int, int]:
        return (int(lat / self.cell_size), int(lon / self.cell_size))

    def add(self, item: dict, lat: float, lon: float) -> None:
        self.grid[self._key(lat, lon)].append((lat, lon, item))

    def query(self, lat: float, lon: float) -> list[tuple[float, float, dict]]:
        ck = self._key(lat, lon)
        out: list[tuple[float, float, dict]] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out.extend(self.grid[(ck[0] + dx, ck[1] + dy)])
        return out


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


POI_TAG_KEYS = (
    "amenity", "shop", "office", "tourism", "leisure", "craft", "healthcare", "building",
    "disused:shop", "disused:amenity", "disused:office", "was:amenity",
)


def _is_poi_node(el: dict) -> bool:
    """A POI node is a node that carries shop/amenity/etc. tags — its address is
    a courtesy annotation, not the canonical address feature. Polygons are never
    POI-filtered: a hospital or building polygon with addr:* is a valid match.
    """
    if el.get("type") != "node":
        return False
    tags = el.get("tags") or {}
    return any(k in tags for k in POI_TAG_KEYS)


def build_osm_index(elements: list[dict]) -> tuple[GridIndex, GridIndex]:
    """Return (match_idx, poi_idx).

    match_idx holds pure-address nodes and polygons — valid conflation targets.
    poi_idx holds amenity/shop/etc. nodes, acknowledged but ignored for matching.
    Nodes that are members of an addr:interpolation way are dropped entirely:
    they're endpoints of an interpolated range, not standalone addresses.
    """
    interp_node_ids: set[int] = set()
    for el in elements:
        if el.get("type") != "way":
            continue
        if "addr:interpolation" not in (el.get("tags") or {}):
            continue
        for nid in el.get("nodes") or ():
            interp_node_ids.add(nid)

    match_idx = GridIndex()
    poi_idx = GridIndex()
    for el in elements:
        tags = el.get("tags") or {}
        if "addr:housenumber" not in tags:
            continue
        if el.get("type") == "node":
            if el.get("id") in interp_node_ids:
                continue
            lat, lon = el.get("lat"), el.get("lon")
        elif "center" in el:
            lat = el["center"].get("lat")
            lon = el["center"].get("lon")
        else:
            lat = lon = None
        if lat is None or lon is None:
            continue
        el["_norm_street"] = normalize_street(tags.get("addr:street", ""))
        el["_norm_number"] = str(tags.get("addr:housenumber", "")).upper()
        target = poi_idx if _is_poi_node(el) else match_idx
        target.add(el, float(lat), float(lon))
    return match_idx, poi_idx


def _classify(
    cand_row: dict,
    match_idx: GridIndex,
    poi_idx: GridIndex,
    match_radius_m: float,
    match_near_m: float,
):
    """Return (verdict, osm_id, osm_type, dist_m, matched_osm_el, poi_el).

    Scans match_idx within match_radius_m for an OSM address with the same
    normalized housenumber + street. Nearest match within match_near_m = MATCH;
    beyond that = MATCH_FAR (operator review). No match → MISSING, plus a
    same-address POI node from poi_idx (if any) attached as acknowledgment.
    """
    c_lat, c_lon = cand_row["lat"], cand_row["lon"]
    if c_lat is None or c_lon is None:
        return "MISSING", None, None, None, None, None

    c_num = (cand_row.get("housenumber") or "").upper()
    c_street_norm = cand_row.get("street_norm") or ""

    best_match = None
    for o_lat, o_lon, osm in match_idx.query(c_lat, c_lon):
        dist = haversine(c_lat, c_lon, o_lat, o_lon)
        if dist > match_radius_m:
            continue
        if osm["_norm_number"] == c_num and osm["_norm_street"] == c_street_norm:
            if best_match is None or dist < best_match[0]:
                best_match = (dist, osm)

    if best_match is not None:
        dist, el = best_match
        verdict = "MATCH" if dist <= match_near_m else "MATCH_FAR"
        return verdict, el.get("id"), el.get("type"), dist, el, None

    best_poi = None
    for o_lat, o_lon, poi in poi_idx.query(c_lat, c_lon):
        dist = haversine(c_lat, c_lon, o_lat, o_lon)
        if dist > match_radius_m:
            continue
        if poi["_norm_number"] == c_num and poi["_norm_street"] == c_street_norm:
            if best_poi is None or dist < best_poi[0]:
                best_poi = (dist, poi)

    poi_el = best_poi[1] if best_poi else None
    return "MISSING", None, None, None, None, poi_el


def _proposed_tags(cand_row: dict, poi_tags: dict | None = None) -> dict[str, str]:
    """Build the tag dict we would propose for this candidate.

    Adds addr:postcode when cand_row has proposed_postcode (stored during
    conflation) or when poi_tags carries one, so the OSM upload includes it.
    Output matches what osm_export writes.
    """
    tags = {
        "addr:housenumber": (cand_row.get("housenumber") or "").strip(),
        "addr:street": (cand_row.get("street_raw") or "").strip(),
        **STATIC_TAGS,
    }
    postcode = (cand_row.get("proposed_postcode") or "").strip()
    if not postcode and poi_tags:
        postcode = (poi_tags.get("addr:postcode") or "").strip()
    if postcode:
        tags["addr:postcode"] = postcode
    if cand_row.get("address_class") == "Structure Entrance":
        tags["entrance"] = "yes"
    return {k: v for k, v in tags.items() if v}


def _matched_latlon(el: dict | None) -> tuple[float | None, float | None]:
    """Point location of the matched OSM element for map rendering.

    For nodes that's lat/lon; for ways/relations we fall back to Overpass's
    `center` output (build_osm_index already required one of the two).
    """
    if el is None:
        return None, None
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    c = el.get("center") or {}
    return c.get("lat"), c.get("lon")


def _is_range(row: dict) -> bool:
    """Return True when the candidate represents an address range (lo_num != hi_num)."""
    lo = row.get("lo_num")
    hi = row.get("hi_num")
    return lo is not None and hi is not None and lo != hi


# Land sibling within this distance auto-skips the non-Land candidate.
# 50 m is comfortably wider than typical Toronto lot depth, so it catches
# parcel-centroid-vs-entrance pairs without colliding with the next address
# over. The lookup is keyed on (address_full, municipality_name) because
# the same string recurs across former municipalities post-amalgamation
# (see SOURCE_DATA.md "Municipality trap" — e.g. "66 George St" exists in
# three of them). The 50 m haversine check is a backstop, not the primary
# disambiguator.
_LAND_SIBLING_RADIUS_M = 50.0

# Two Land rows at the same (address_full, municipality_name) within this
# distance are treated as a single logical record: conflation silently skips
# the non-canonical one. Beyond this threshold both rows proceed through
# conflation and the intra_source_duplicate check flags them for review.
_INTRA_DUP_AUTO_SKIP_M = 5.0


def _colocated_land_sibling(
    cand: dict, land_lookup: dict[tuple[str, str | None], list[tuple[float, float]]]
) -> bool:
    if cand.get("address_class") == "Land":
        return False
    addr = cand.get("address_full")
    lat, lon = cand.get("lat"), cand.get("lon")
    if not addr or lat is None or lon is None:
        return False
    for la, lo in land_lookup.get((addr, cand.get("municipality_name")), ()):
        if haversine(lat, lon, la, lo) <= _LAND_SIBLING_RADIUS_M:
            return True
    return False


def _build_land_groups(
    conn, run_id: int
) -> dict[tuple[str, str | None], list[tuple[int, float, float]]]:
    """(address_full, municipality_name) -> [(candidate_id, lat, lon), ...].

    Ordered by candidate_id so the first entry of every group is the canonical
    (lowest-id) row. Same key shape as land_lookup but carries candidate_id so
    the sibling link can be persisted on the conflation row.
    """
    groups: dict[tuple[str, str | None], list[tuple[int, float, float]]] = defaultdict(list)
    for r in conn.execute(
        "SELECT candidate_id, address_full, municipality_name, lat, lon "
        "FROM candidates WHERE run_id = ? AND address_class = 'Land' "
        "  AND address_full IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL "
        "ORDER BY candidate_id",
        (run_id,),
    ):
        groups[(r["address_full"], r["municipality_name"])].append(
            (r["candidate_id"], r["lat"], r["lon"])
        )
    return groups


def _intra_dup_status(
    cand: dict,
    land_groups: dict[tuple[str, str | None], list[tuple[int, float, float]]],
) -> tuple[int, float, bool] | None:
    """Return (nearest_sibling_cid, dist_m, is_canonical) for a Land candidate
    that shares (address_full, municipality_name) with another Land row; None
    otherwise. is_canonical is True when this row's candidate_id is the
    lowest in the group (the keep-one tiebreak).
    """
    if cand.get("address_class") != "Land":
        return None
    addr, lat, lon = cand.get("address_full"), cand.get("lat"), cand.get("lon")
    if not addr or lat is None or lon is None:
        return None
    group = land_groups.get((addr, cand.get("municipality_name")), ())
    if len(group) < 2:
        return None
    siblings = [s for s in group if s[0] != cand["candidate_id"]]
    if not siblings:
        return None
    sib_cid, _, sib_dist = min(
        ((s[0], s, haversine(lat, lon, s[1], s[2])) for s in siblings),
        key=lambda t: t[2],
    )
    canonical_cid = min(s[0] for s in group)
    return sib_cid, sib_dist, cand["candidate_id"] == canonical_cid


def run(run_id: int, osm_snapshot_hash: str, match_radius_m: float, match_near_m: float) -> dict[str, int]:
    """Iterate candidates at stage INGESTED, write conflation row, advance to CONFLATED."""
    from . import tag_diff  # local import avoids an import cycle at module load

    elements = osm_fetch.load_cached(run_id)
    match_idx, poi_idx = build_osm_index(elements)
    now = datetime.now(timezone.utc).isoformat()

    counts = {"MATCH": 0, "MATCH_FAR": 0, "MISSING": 0, "SKIPPED": 0}
    conn = _db.connect()
    try:
        land_groups = _build_land_groups(conn, run_id)
        land_lookup: dict[tuple[str, str | None], list[tuple[float, float]]] = {
            key: [(lat, lon) for _cid, lat, lon in group]
            for key, group in land_groups.items()
        }

        rows = conn.execute(
            "SELECT candidate_id, address_full, housenumber, street_raw, street_norm, lat, lon, "
            "       lo_num, hi_num, address_class, municipality_name "
            "FROM candidates WHERE run_id = ? AND stage = 'INGESTED'",
            (run_id,),
        ).fetchall()

        conn.execute("BEGIN")
        for r in rows:
            cand = dict(r)

            # Same-address Land sibling detection: <5 m auto-skips the non-canonical
            # row; wider pairs persist the link for the intra_source_duplicate check.
            dup = _intra_dup_status(cand, land_groups)
            auto_skip_dup = dup is not None and dup[1] <= _INTRA_DUP_AUTO_SKIP_M and not dup[2]

            # Address ranges are skipped during conflation (kept for reference only).
            # Non-Land rows colocated with a Land sibling at the same address are also
            # skipped — the Land row is the canonical record (see SOURCE_DATA.md).
            if _is_range(cand) or _colocated_land_sibling(cand, land_lookup) or auto_skip_dup:
                verdict, osm_id, osm_type, dist, matched, poi = "SKIPPED", None, None, None, None, None
            else:
                verdict, osm_id, osm_type, dist, matched, poi = _classify(
                    cand, match_idx, poi_idx, match_radius_m, match_near_m
                )
            counts[verdict] += 1

            osm_tags = (matched.get("tags") if matched else None) or None
            geom = tag_diff.geom_hint(matched) if matched else None
            m_lat, m_lon = _matched_latlon(matched)

            poi_tags = (poi.get("tags") if poi else None) or None
            poi_postcode = (poi_tags.get("addr:postcode").strip() if poi_tags and poi_tags.get("addr:postcode") else None)

            dup_sib_cid, dup_sib_dist = (dup[0], dup[1]) if dup else (None, None)

            conn.execute(
                """
                INSERT OR REPLACE INTO conflation
                  (run_id, candidate_id, verdict, nearest_osm_id, nearest_osm_type,
                   nearest_dist_m, osm_snapshot_hash, computed_at,
                   matched_osm_tags_json, matched_osm_geom_hint,
                   matched_osm_lat, matched_osm_lon,
                   poi_osm_id, poi_osm_type, poi_tags_json, proposed_postcode,
                   dup_sibling_candidate_id, dup_sibling_dist_m)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, cand["candidate_id"], verdict, osm_id, osm_type, dist,
                    osm_snapshot_hash, now,
                    json.dumps(osm_tags) if osm_tags else None,
                    geom, m_lat, m_lon,
                    (poi.get("id") if poi else None),
                    (poi.get("type") if poi else None),
                    json.dumps(poi_tags) if poi_tags else None,
                    poi_postcode,
                    dup_sib_cid, dup_sib_dist,
                ),
            )
            if auto_skip_dup:
                audit.log(
                    actor="pipeline", event_type="INTRA_DUP_SKIPPED",
                    run_id=run_id, candidate_id=cand["candidate_id"],
                    payload={
                        "sibling_candidate_id": dup_sib_cid,
                        "dist_m": round(dup_sib_dist, 2),
                        "canonical_candidate_id": min(
                            s[0] for s in land_groups[
                                (cand["address_full"], cand["municipality_name"])
                            ]
                        ),
                    },
                    conn=conn,
                )
            conn.execute(
                "UPDATE candidates SET stage = 'CONFLATED', stage_updated_at = ? "
                "WHERE run_id = ? AND candidate_id = ?",
                (now, run_id, cand["candidate_id"]),
            )

            cand_for_proposal = dict(cand, proposed_postcode=poi_postcode)
            proposed = _proposed_tags(cand_for_proposal)
            diff_rows = tag_diff.compare_tags(proposed, osm_tags)
            has_diff = any(row["status"] != "SAME" for row in diff_rows)
            if has_diff and verdict != "SKIPPED":
                audit.log(
                    actor="pipeline",
                    event_type="CONFLATE_CANDIDATE",
                    run_id=run_id,
                    candidate_id=cand["candidate_id"],
                    payload={
                        "verdict": verdict,
                        "osm_id": osm_id,
                        "osm_type": osm_type,
                        "geom_hint": geom,
                        "dist_m": dist,
                        "diff": diff_rows,
                    },
                    conn=conn,
                )
        audit.log(
            actor="pipeline",
            event_type="CONFLATE_DONE",
            run_id=run_id,
            payload={"counts": counts, "osm_snapshot_hash": osm_snapshot_hash},
            conn=conn,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return counts
