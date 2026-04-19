"""Stage 1: ingest active city addresses in run bbox into tool.db."""
import json
from datetime import datetime, timezone

from . import audit, db as _db, source_db


def _street_from_row(row: dict) -> str:
    s = row.get("linear_name_full")
    if s:
        return s
    parts = [row.get("linear_name") or "", row.get("linear_name_type") or "", row.get("linear_name_dir") or ""]
    return " ".join(p for p in parts if p).strip()


def ingest(run_id: int, bbox: tuple[float, float, float, float], snapshot_id: int) -> int:
    """Insert new candidates into tool.db. Returns count inserted this call."""
    from .conflate import normalize_street  # local import; conflate owns the normalizer

    inserted = 0
    now = datetime.now(timezone.utc).isoformat()
    conn = _db.connect()
    try:
        conn.execute("BEGIN")
        for row in source_db.iter_active_addresses_in_bbox(bbox, snapshot_id):
            street_raw = _street_from_row(row)
            housenumber = row.get("address_number") or ""
            extra_raw = row.get("extra")
            try:
                address_class = (json.loads(extra_raw) if extra_raw else {}).get("ADDRESS_CLASS_DESC")
            except (ValueError, TypeError):
                address_class = None
            values = (
                run_id,
                row["address_point_id"],
                row.get("address_full"),
                str(housenumber).strip().upper() if housenumber else None,
                street_raw or None,
                normalize_street(street_raw),
                row.get("latitude"),
                row.get("longitude"),
                row.get("lo_num"),
                row.get("lo_num_suf"),
                row.get("hi_num"),
                row.get("hi_num_suf"),
                extra_raw,
                address_class,
                "INGESTED",
                now,
            )
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO candidates
                  (run_id, candidate_id, address_full, housenumber, street_raw, street_norm,
                   lat, lon, lo_num, lo_num_suf, hi_num, hi_num_suf, extra_json,
                   address_class, stage, stage_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            if cur.rowcount > 0:
                inserted += 1
        audit.log(
            actor="pipeline",
            event_type="CANDIDATE_INGESTED",
            run_id=run_id,
            payload={"inserted": inserted, "snapshot_id": snapshot_id, "bbox": list(bbox)},
            conn=conn,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return inserted


def count_by_stage(run_id: int) -> dict[str, int]:
    conn = _db.connect()
    try:
        rows = conn.execute(
            "SELECT stage, COUNT(*) AS n FROM candidates WHERE run_id = ? GROUP BY stage",
            (run_id,),
        ).fetchall()
        return {r["stage"]: r["n"] for r in rows}
    finally:
        conn.close()
