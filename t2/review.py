"""Operator review state transitions."""
from datetime import datetime, timezone

from . import audit, db as _db


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_TRANSITION_STAGE = {
    "APPROVED": "APPROVED",
    "REJECTED": "REJECTED",
    "DEFERRED": "REVIEW_PENDING",
    "OPEN": "REVIEW_PENDING",
}


def resolve(run_id: int, candidate_id: int, new_status: str, actor: str = "operator", note: str | None = None) -> None:
    assert new_status in _TRANSITION_STAGE, f"unknown review status {new_status}"
    now = _iso()
    conn = _db.connect()
    try:
        conn.execute("BEGIN")
        # Forbid reopen once uploaded
        row = conn.execute(
            "SELECT stage FROM candidates WHERE run_id=? AND candidate_id=?",
            (run_id, candidate_id),
        ).fetchone()
        if row and row["stage"] == "UPLOADED":
            raise RuntimeError(f"candidate {candidate_id} already UPLOADED; cannot change review.")

        conn.execute(
            """
            INSERT INTO review_items (run_id, candidate_id, reason_code, status, note, opened_at, resolved_at)
            VALUES (?, ?, COALESCE((SELECT reason_code FROM review_items WHERE run_id=? AND candidate_id=?), 'manual'), ?, ?, ?, ?)
            ON CONFLICT(run_id, candidate_id) DO UPDATE SET
                status = excluded.status,
                note = excluded.note,
                resolved_at = CASE WHEN excluded.status IN ('APPROVED','REJECTED') THEN ? ELSE NULL END
            """,
            (run_id, candidate_id, run_id, candidate_id, new_status, note, now,
             now if new_status in ("APPROVED", "REJECTED") else None, now),
        )
        new_stage = _TRANSITION_STAGE[new_status]
        conn.execute(
            "UPDATE candidates SET stage=?, stage_updated_at=? WHERE run_id=? AND candidate_id=?",
            (new_stage, now, run_id, candidate_id),
        )
        event = {
            "APPROVED": "REVIEW_APPROVED",
            "REJECTED": "REVIEW_REJECTED",
            "DEFERRED": "REVIEW_REOPENED",
            "OPEN": "REVIEW_REOPENED",
        }[new_status]
        audit.log(actor=actor, event_type=event, run_id=run_id, candidate_id=candidate_id,
                  payload={"note": note}, conn=conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def queue(run_id: int, limit: int = 100, offset: int = 0) -> list[dict]:
    conn = _db.connect()
    try:
        rows = conn.execute(
            """
            SELECT r.candidate_id, r.reason_code, r.status, r.opened_at,
                   c.address_full, c.housenumber, c.street_raw, c.lat, c.lon,
                   cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type, cf.nearest_dist_m
            FROM review_items r
            JOIN candidates c USING (run_id, candidate_id)
            LEFT JOIN conflation cf USING (run_id, candidate_id)
            WHERE r.run_id = ? AND r.status = 'OPEN'
            ORDER BY r.opened_at
            LIMIT ? OFFSET ?
            """,
            (run_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def check_results_for(run_id: int, candidate_id: int) -> list[dict]:
    conn = _db.connect()
    try:
        rows = conn.execute(
            """
            SELECT check_id, check_version, verdict, severity, reason_code, details_json, computed_at
            FROM check_results
            WHERE run_id = ? AND candidate_id = ?
            ORDER BY check_id, check_version DESC
            """,
            (run_id, candidate_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
