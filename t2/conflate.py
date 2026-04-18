"""Stage 3: conflate candidates against cached OSM snapshot, write verdicts to DB.

Core helpers (normalize_street, GridIndex, haversine) preserved from sibling
project's src/conflate.py — the algorithmic contract there is proven.
"""
import math
from collections import defaultdict
from datetime import datetime, timezone

from . import audit, db as _db, osm_fetch

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


def build_osm_index(elements: list[dict]) -> GridIndex:
    idx = GridIndex()
    for el in elements:
        tags = el.get("tags") or {}
        if "addr:housenumber" not in tags:
            continue
        if el.get("type") == "node":
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
        idx.add(el, float(lat), float(lon))
    return idx


def _classify(cand_row: dict, idx: GridIndex, match_radius_m: float, close_neighbor_m: float):
    c_lat, c_lon = cand_row["lat"], cand_row["lon"]
    if c_lat is None or c_lon is None:
        return "MISSING", None, None, None

    c_num = (cand_row.get("housenumber") or "").upper()
    c_street_norm = cand_row.get("street_norm") or ""

    best = None  # (dist, osm_el)
    match = False
    for o_lat, o_lon, osm in idx.query(c_lat, c_lon):
        dist = haversine(c_lat, c_lon, o_lat, o_lon)
        if dist > match_radius_m:
            continue
        if best is None or dist < best[0]:
            best = (dist, osm)
        if osm["_norm_number"] == c_num and osm["_norm_street"] == c_street_norm:
            match = True
            break

    if match:
        el = best[1]
        return "MATCH", el.get("id"), el.get("type"), best[0]

    # within close radius but didn't match — CONFLICT
    if best is not None and best[0] <= close_neighbor_m:
        el = best[1]
        return "CONFLICT", el.get("id"), el.get("type"), best[0]

    return "MISSING", None, None, None


def _is_range(row: dict) -> bool:
    """Return True when the candidate represents an address range (lo_num != hi_num)."""
    lo = row.get("lo_num")
    hi = row.get("hi_num")
    return lo is not None and hi is not None and lo != hi


def run(run_id: int, osm_snapshot_hash: str, match_radius_m: float, close_neighbor_m: float) -> dict[str, int]:
    """Iterate candidates at stage INGESTED, write conflation row, advance to CONFLATED."""
    elements = osm_fetch.load_cached(run_id)
    idx = build_osm_index(elements)
    now = datetime.now(timezone.utc).isoformat()

    counts = {"MATCH": 0, "MISSING": 0, "CONFLICT": 0, "SKIPPED": 0}
    conn = _db.connect()
    try:
        rows = conn.execute(
            "SELECT candidate_id, housenumber, street_norm, lat, lon, lo_num, hi_num "
            "FROM candidates "
            "WHERE run_id = ? AND stage = 'INGESTED'",
            (run_id,),
        ).fetchall()

        conn.execute("BEGIN")
        for r in rows:
            cand = dict(r)

            # Address ranges are skipped during conflation (kept for reference only)
            if _is_range(cand):
                verdict, osm_id, osm_type, dist = "SKIPPED", None, None, None
            else:
                verdict, osm_id, osm_type, dist = _classify(
                    cand, idx, match_radius_m, close_neighbor_m
                )
            counts[verdict] += 1
            conn.execute(
                """
                INSERT OR REPLACE INTO conflation
                  (run_id, candidate_id, verdict, nearest_osm_id, nearest_osm_type,
                   nearest_dist_m, osm_snapshot_hash, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, cand["candidate_id"], verdict, osm_id, osm_type, dist, osm_snapshot_hash, now),
            )
            conn.execute(
                "UPDATE candidates SET stage = 'CONFLATED', stage_updated_at = ? "
                "WHERE run_id = ? AND candidate_id = ?",
                (now, run_id, cand["candidate_id"]),
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
