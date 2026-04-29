"""Address-range coverage: which integers inside [lo_num, hi_num] appear as
single-row source addresses on the same street + municipality.

The full per-candidate view (with parity items and matched singles' coordinates)
is computed on demand by `coverage()` — used by the ranges detail page.

For the list page, we persist the aggregate category + parity counts on each
candidate row via `compute_for_run()` so the page is a pure SELECT. The compute
uses one batched query against the source DB rather than N per-candidate ones.
"""
from collections import defaultdict

from . import db as _db, source_db
from .conflate import normalize_street


CATEGORIES = ("uncovered", "partial", "full", "unknown")


def coverage(cand: dict, snap_id: int | None, source_conn=None) -> dict:
    """Full coverage view for one candidate. Returns parity-aware (step=2) and
    integer-aware summaries plus matched singles' coordinates."""
    lo, hi = cand.get("lo_num"), cand.get("hi_num")
    if lo is None or hi is None or lo == hi or snap_id is None:
        return {
            "parity_items": [], "int_present_count": 0, "int_total": 0,
            "parity_present_count": 0, "parity_total": 0,
            "singles_geo": [],
        }
    lo_i, hi_i = (lo, hi) if lo <= hi else (hi, lo)
    target_street = cand.get("street_norm") or ""
    mun = cand.get("municipality_name")
    own_conn = source_conn is None
    conn = source_conn if source_conn is not None else source_db.connect_readonly()
    try:
        rows = conn.execute(
            "SELECT lo_num, linear_name_full, address_full, latitude, longitude,"
            "       address_point_id "
            "FROM addresses "
            "WHERE max_snapshot_id=? AND hi_num IS NULL "
            "  AND municipality_name IS ? "
            "  AND lo_num BETWEEN ? AND ?",
            (snap_id, mun, lo_i, hi_i),
        ).fetchall()
    finally:
        if own_conn:
            conn.close()
    present: dict[int, dict] = {}
    for r in rows:
        if normalize_street(r["linear_name_full"]) != target_street:
            continue
        present[int(r["lo_num"])] = {
            "address_full": r["address_full"],
            "lat": r["latitude"],
            "lon": r["longitude"],
            "address_point_id": r["address_point_id"],
        }
    parity_seq = list(range(lo_i, hi_i + 1, 2))
    int_seq = list(range(lo_i, hi_i + 1))
    parity_items = [
        {"num": n, "present": n in present, **(present.get(n) or {})}
        for n in parity_seq
    ]
    singles_geo = [
        {"num": n, "lat": p["lat"], "lon": p["lon"],
         "address_full": p["address_full"],
         "address_point_id": p["address_point_id"]}
        for n, p in sorted(present.items())
        if p.get("lat") is not None and p.get("lon") is not None
    ]
    return {
        "parity_items": parity_items,
        "parity_present_count": sum(1 for it in parity_items if it["present"]),
        "parity_total": len(parity_items),
        "int_present_count": sum(1 for n in int_seq if n in present),
        "int_total": len(int_seq),
        "singles_geo": singles_geo,
    }


def coverage_category(parity_present: int, parity_total: int) -> str:
    if parity_total == 0:
        return "unknown"
    if parity_present == 0:
        return "uncovered"
    if parity_present == parity_total:
        return "full"
    return "partial"


def compute_for_run(run_id: int) -> int:
    """Compute and cache coverage for every range candidate in this run.
    Returns the number of rows updated. One batched source-DB query instead of
    per-candidate lookups."""
    conn = _db.connect()
    try:
        run_row = conn.execute(
            "SELECT source_snapshot_id FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        snap_id = int(run_row["source_snapshot_id"]) if run_row and run_row["source_snapshot_id"] is not None else None
        cands = [dict(r) for r in conn.execute(
            "SELECT candidate_id, street_norm, lo_num, hi_num, municipality_name "
            "FROM candidates WHERE run_id=? AND stage='SKIPPED' "
            "  AND lo_num IS NOT NULL AND hi_num IS NOT NULL AND lo_num != hi_num",
            (run_id,),
        ).fetchall()]
        if not cands:
            return 0

        present: dict[tuple, set[int]] = defaultdict(set)
        if snap_id is not None:
            overall_lo = min(min(c["lo_num"], c["hi_num"]) for c in cands)
            overall_hi = max(max(c["lo_num"], c["hi_num"]) for c in cands)
            src_conn = source_db.connect_readonly()
            try:
                rows = src_conn.execute(
                    "SELECT lo_num, linear_name_full, municipality_name "
                    "FROM addresses "
                    "WHERE max_snapshot_id=? AND hi_num IS NULL "
                    "  AND lo_num BETWEEN ? AND ?",
                    (snap_id, overall_lo, overall_hi),
                ).fetchall()
            finally:
                src_conn.close()
            for r in rows:
                key = (r["municipality_name"], normalize_street(r["linear_name_full"]))
                present[key].add(int(r["lo_num"]))

        updates = []
        for c in cands:
            lo, hi = c["lo_num"], c["hi_num"]
            lo_i, hi_i = (lo, hi) if lo <= hi else (hi, lo)
            bucket = present.get((c["municipality_name"], c["street_norm"] or ""), set())
            parity_total = 0
            parity_present = 0
            n = lo_i
            while n <= hi_i:
                parity_total += 1
                if n in bucket:
                    parity_present += 1
                n += 2
            cat = coverage_category(parity_present, parity_total) if snap_id is not None else "unknown"
            updates.append((cat, parity_present, parity_total, run_id, c["candidate_id"]))

        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "UPDATE candidates SET range_coverage_cat=?, range_parity_present=?,"
            "                      range_parity_total=? "
            "WHERE run_id=? AND candidate_id=?",
            updates,
        )
        conn.execute("COMMIT")
        return len(updates)
    finally:
        conn.close()
