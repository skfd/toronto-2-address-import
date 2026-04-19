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

_VALID_FILTER_STATUSES = {"OPEN", "APPROVED", "REJECTED", "DEFERRED"}
_VALID_FILTER_VERDICTS = {"MATCH", "MATCH_FAR", "MISSING", "SKIPPED"}


def _poi_where(poi_ack: bool, postcode_from_poi: bool, verdicts: tuple[str, ...]) -> tuple[str, list]:
    """Build shared WHERE predicates for the POI / verdict filters used by
    review.queue, approved_page, and skipped_page. Predicates reference the
    `cf` (conflation) alias, so callers must LEFT JOIN conflation AS cf.
    """
    clauses: list[str] = []
    params: list = []
    if poi_ack:
        clauses.append("cf.poi_osm_id IS NOT NULL")
    if postcode_from_poi:
        clauses.append("cf.proposed_postcode IS NOT NULL AND cf.proposed_postcode != ''")
    if verdicts:
        placeholders = ",".join("?" for _ in verdicts)
        clauses.append(f"cf.verdict IN ({placeholders})")
        params.extend(verdicts)
    return (" AND ".join(clauses), params)


def resolve(run_id: int, candidate_id: int, new_status: str, actor: str = "operator", note: str | None = None) -> None:
    assert new_status in _TRANSITION_STAGE, f"unknown review status {new_status}"
    now = _iso()
    conn = _db.connect()
    try:
        conn.execute("BEGIN")
        cand = conn.execute(
            "SELECT stage FROM candidates WHERE run_id=? AND candidate_id=?",
            (run_id, candidate_id),
        ).fetchone()
        if cand and cand["stage"] == "UPLOADED":
            raise RuntimeError(f"candidate {candidate_id} already UPLOADED; cannot change review.")

        existing = conn.execute(
            "SELECT prior_auto_approved FROM review_items WHERE run_id=? AND candidate_id=?",
            (run_id, candidate_id),
        ).fetchone()
        override_of_auto = existing is None and cand is not None and cand["stage"] == "APPROVED"
        prior_flag = 1 if override_of_auto else (int(existing["prior_auto_approved"]) if existing else 0)

        conn.execute(
            """
            INSERT INTO review_items (run_id, candidate_id, reason_code, status, note, opened_at, resolved_at, prior_auto_approved)
            VALUES (?, ?, COALESCE((SELECT reason_code FROM review_items WHERE run_id=? AND candidate_id=?), ?), ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, candidate_id) DO UPDATE SET
                status = excluded.status,
                note = excluded.note,
                resolved_at = CASE WHEN excluded.status IN ('APPROVED','REJECTED') THEN ? ELSE NULL END,
                prior_auto_approved = COALESCE(review_items.prior_auto_approved, 0)
            """,
            (
                run_id, candidate_id,
                run_id, candidate_id,
                "auto_override" if override_of_auto else "manual",
                new_status, note, now,
                now if new_status in ("APPROVED", "REJECTED") else None,
                prior_flag,
                now,
            ),
        )
        new_stage = _TRANSITION_STAGE[new_status]
        conn.execute(
            "UPDATE candidates SET stage=?, stage_updated_at=? WHERE run_id=? AND candidate_id=?",
            (new_stage, now, run_id, candidate_id),
        )
        if prior_flag:
            event = "REVIEW_OVERRIDE"
        else:
            event = {
                "APPROVED": "REVIEW_APPROVED",
                "REJECTED": "REVIEW_REJECTED",
                "DEFERRED": "REVIEW_REOPENED",
                "OPEN": "REVIEW_REOPENED",
            }[new_status]
        audit.log(actor=actor, event_type=event, run_id=run_id, candidate_id=candidate_id,
                  payload={"note": note, "new_status": new_status}, conn=conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def queue(
    run_id: int,
    statuses: tuple[str, ...] | list[str] | None = None,
    include_auto: bool = False,
    verdicts: tuple[str, ...] | list[str] | None = None,
    poi_ack: bool = False,
    postcode_from_poi: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Return review rows matching the given statuses.

    Auto-approved candidates (stage='APPROVED' with no review_items row) are
    included as synthetic rows with status='AUTO_APPROVED' when
    include_auto=True. `verdicts`/`poi_ack`/`postcode_from_poi` narrow the
    result to rows whose conflation row matches those predicates.
    """
    if statuses is None:
        statuses = ("OPEN",)
    statuses = tuple(s for s in statuses if s in _VALID_FILTER_STATUSES)
    verdicts_t = tuple(v for v in (verdicts or ()) if v in _VALID_FILTER_VERDICTS)
    extra_where, extra_params = _poi_where(poi_ack, postcode_from_poi, verdicts_t)
    and_extra = (" AND " + extra_where) if extra_where else ""

    conn = _db.connect()
    try:
        results: list[dict] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"""
                SELECT r.candidate_id, r.reason_code, r.status, r.opened_at, r.resolved_at,
                       r.prior_auto_approved,
                       c.address_full, c.housenumber, c.street_raw, c.lat, c.lon,
                       c.lo_num, c.hi_num, c.stage, c.address_class,
                       cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type, cf.nearest_dist_m,
                       cf.poi_osm_id, cf.proposed_postcode
                FROM review_items r
                JOIN candidates c USING (run_id, candidate_id)
                LEFT JOIN conflation cf USING (run_id, candidate_id)
                WHERE r.run_id = ? AND r.status IN ({placeholders}){and_extra}
                """,
                (run_id, *statuses, *extra_params),
            ).fetchall()
            results.extend(dict(r) for r in rows)

        if include_auto:
            rows = conn.execute(
                f"""
                SELECT c.candidate_id,
                       'auto_clean' AS reason_code,
                       'AUTO_APPROVED' AS status,
                       c.stage_updated_at AS opened_at,
                       c.stage_updated_at AS resolved_at,
                       0 AS prior_auto_approved,
                       c.address_full, c.housenumber, c.street_raw, c.lat, c.lon,
                       c.lo_num, c.hi_num, c.stage, c.address_class,
                       cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type, cf.nearest_dist_m,
                       cf.poi_osm_id, cf.proposed_postcode
                FROM candidates c
                LEFT JOIN review_items r USING (run_id, candidate_id)
                LEFT JOIN conflation cf USING (run_id, candidate_id)
                WHERE c.run_id = ? AND c.stage = 'APPROVED' AND r.candidate_id IS NULL{and_extra}
                """,
                (run_id, *extra_params),
            ).fetchall()
            results.extend(dict(r) for r in rows)

        results.sort(key=lambda r: r["opened_at"] or "")
        return results[offset : offset + limit]
    finally:
        conn.close()


def get_review_state(run_id: int, candidate_id: int) -> dict:
    """Return the current review state for a single candidate.

    Returns {"status": ..., "prior_auto_approved": 0|1, "note": ...}.
    If no review_items row exists and the candidate is stage='APPROVED',
    returns a synthetic {"status": "AUTO_APPROVED", ...}. Otherwise status
    is None (pre-check state).
    """
    conn = _db.connect()
    try:
        r = conn.execute(
            "SELECT status, note, prior_auto_approved FROM review_items WHERE run_id=? AND candidate_id=?",
            (run_id, candidate_id),
        ).fetchone()
        if r:
            return {"status": r["status"], "note": r["note"],
                    "prior_auto_approved": int(r["prior_auto_approved"])}
        c = conn.execute(
            "SELECT stage FROM candidates WHERE run_id=? AND candidate_id=?",
            (run_id, candidate_id),
        ).fetchone()
        if c and c["stage"] == "APPROVED":
            return {"status": "AUTO_APPROVED", "note": None, "prior_auto_approved": 0}
        return {"status": None, "note": None, "prior_auto_approved": 0}
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
