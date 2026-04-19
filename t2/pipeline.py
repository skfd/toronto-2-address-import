"""Stage orchestrator + resumable run machinery."""
import json
from datetime import datetime, timezone

from . import audit, candidates, conflate, config as _config, db as _db, osm_fetch, source_db
from .checks import REGISTRY, Candidate, CheckContext


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_checks_catalog(conn):
    for check in REGISTRY.values():
        conn.execute(
            """
            INSERT INTO checks_catalog (check_id, version, enabled_default, description)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(check_id) DO UPDATE SET
                version = excluded.version,
                enabled_default = excluded.enabled_default,
                description = excluded.description
            """,
            (check.id, check.version, 1 if check.default_enabled else 0, check.description),
        )


def _seed_toggles(conn, run_id: int, enabled_from_config: dict[str, bool]):
    for check in REGISTRY.values():
        enabled = enabled_from_config.get(check.id, check.default_enabled)
        conn.execute(
            "INSERT OR IGNORE INTO check_toggles (run_id, check_id, enabled) VALUES (?, ?, ?)",
            (run_id, check.id, 1 if enabled else 0),
        )


def start_run(name: str, bbox: tuple[float, float, float, float] | None = None) -> int:
    """Create or reopen a run by name. Returns run_id."""
    cfg = _config.load()
    bbox = bbox or cfg.default_bbox
    snapshot_id = source_db.latest_snapshot_id()
    now = _iso()
    conn = _db.connect()
    try:
        conn.execute("BEGIN")
        existing = conn.execute("SELECT run_id FROM runs WHERE name = ?", (name,)).fetchone()
        if existing:
            run_id = int(existing["run_id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO runs (name, bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon,
                                  source_snapshot_id, created_at, config_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    bbox[0], bbox[1], bbox[2], bbox[3],
                    snapshot_id,
                    now,
                    json.dumps({
                        "match_radius_m": cfg.match_radius_m,
                        "match_near_m": cfg.match_near_m,
                        "checks_params": cfg.checks_params,
                    }),
                ),
            )
            run_id = int(cur.lastrowid)
            audit.log(
                actor="pipeline", event_type="RUN_CREATED", run_id=run_id,
                payload={"name": name, "bbox": list(bbox), "snapshot_id": snapshot_id}, conn=conn,
            )
        _ensure_checks_catalog(conn)
        _seed_toggles(conn, run_id, cfg.checks_enabled)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return run_id


def ingest_stage(run_id: int) -> int:
    cfg = _config.load()
    conn = _db.connect()
    try:
        row = conn.execute(
            "SELECT bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon, source_snapshot_id FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"run {run_id} not found")
        bbox = (row["bbox_min_lat"], row["bbox_min_lon"], row["bbox_max_lat"], row["bbox_max_lon"])
        snap = int(row["source_snapshot_id"])
    finally:
        conn.close()
    # Sanity: is source snapshot still current?
    current = source_db.latest_snapshot_id()
    if current != snap:
        raise RuntimeError(
            f"Source snapshot changed since run start (was {snap}, now {current}). Create a new run."
        )
    return candidates.ingest(run_id, bbox, snap)


def fetch_stage(run_id: int, force: bool = False) -> str:
    conn = _db.connect()
    try:
        row = conn.execute(
            "SELECT bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        bbox = (row["bbox_min_lat"], row["bbox_min_lon"], row["bbox_max_lat"], row["bbox_max_lon"])
    finally:
        conn.close()
    _path, digest = osm_fetch.fetch(run_id, bbox, force=force)
    audit.log(actor="pipeline", event_type="OSM_FETCHED", run_id=run_id, payload={"hash": digest})
    return digest


def conflate_stage(run_id: int, osm_hash: str | None = None) -> dict[str, int]:
    cfg = _config.load()
    if osm_hash is None:
        osm_hash = fetch_stage(run_id)
    return conflate.run(run_id, osm_hash, cfg.match_radius_m, cfg.match_near_m)


def _enabled_checks(run_id: int) -> list:
    conn = _db.connect()
    try:
        rows = conn.execute(
            "SELECT check_id FROM check_toggles WHERE run_id = ? AND enabled = 1",
            (run_id,),
        ).fetchall()
        return [REGISTRY[r["check_id"]] for r in rows if r["check_id"] in REGISTRY]
    finally:
        conn.close()


def _build_city_index(conn, run_id: int):
    from .conflate import GridIndex
    idx = GridIndex()
    for row in conn.execute(
        "SELECT candidate_id, lat, lon FROM candidates WHERE run_id = ? AND lat IS NOT NULL AND lon IS NOT NULL",
        (run_id,),
    ):
        idx.add({"candidate_id": row["candidate_id"]}, row["lat"], row["lon"])
    return idx


def run_checks(run_id: int) -> dict[str, int]:
    """Run all enabled checks over CONFLATED/CHECKED candidates. Idempotent via (check_id, check_version) PK."""
    cfg = _config.load()
    checks = _enabled_checks(run_id)
    if not checks:
        return {}

    elements = osm_fetch.load_cached(run_id)
    osm_idx, _poi_idx = conflate.build_osm_index(elements)

    counts = {"PASS": 0, "FLAG": 0, "SKIP": 0}
    conn = _db.connect()
    try:
        city_idx = _build_city_index(conn, run_id)
        ctx = CheckContext(run_id=run_id, osm_index=osm_idx, city_index=city_idx, params=cfg.checks_params)

        q = """
            SELECT c.run_id, c.candidate_id, c.address_full, c.housenumber,
                   c.street_raw, c.street_norm, c.lat, c.lon,
                   c.lo_num, c.lo_num_suf, c.hi_num, c.hi_num_suf,
                   cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type, cf.nearest_dist_m,
                   cf.matched_osm_tags_json
            FROM candidates c
            LEFT JOIN conflation cf USING (run_id, candidate_id)
            WHERE c.run_id = ? AND c.stage IN ('CONFLATED', 'CHECKED', 'REVIEW_PENDING')
        """
        rows = conn.execute(q, (run_id,)).fetchall()

        now = _iso()
        conn.execute("BEGIN")
        for r in rows:
            try:
                matched_tags = json.loads(r["matched_osm_tags_json"]) if r["matched_osm_tags_json"] else None
            except Exception:
                matched_tags = None
            cand = Candidate(
                run_id=r["run_id"], candidate_id=r["candidate_id"],
                address_full=r["address_full"], housenumber=r["housenumber"],
                street_raw=r["street_raw"], street_norm=r["street_norm"],
                lat=r["lat"], lon=r["lon"],
                lo_num=r["lo_num"], lo_num_suf=r["lo_num_suf"],
                hi_num=r["hi_num"], hi_num_suf=r["hi_num_suf"],
                verdict=r["verdict"] or "MISSING",
                nearest_osm_id=r["nearest_osm_id"],
                nearest_osm_type=r["nearest_osm_type"],
                nearest_dist_m=r["nearest_dist_m"],
                matched_osm_tags=matched_tags,
            )
            # Ranges were skipped during conflation — auto-skip in checks too
            if cand.verdict == "SKIPPED":
                conn.execute(
                    "UPDATE candidates SET stage='SKIPPED', stage_updated_at=? WHERE run_id=? AND candidate_id=?",
                    (now, run_id, cand.candidate_id),
                )
                counts["SKIP"] = counts.get("SKIP", 0) + 1
                continue

            any_flag = False
            flag_reasons: list[str] = []
            for check in checks:
                # Skip if a result already exists for this (candidate, check_id, check.version)
                exists = conn.execute(
                    "SELECT 1 FROM check_results WHERE run_id=? AND candidate_id=? AND check_id=? AND check_version=?",
                    (run_id, cand.candidate_id, check.id, check.version),
                ).fetchone()
                if exists:
                    # Still need to know if it's a FLAG for review_items materialization
                    prior = conn.execute(
                        "SELECT verdict, reason_code FROM check_results WHERE run_id=? AND candidate_id=? AND check_id=? AND check_version=?",
                        (run_id, cand.candidate_id, check.id, check.version),
                    ).fetchone()
                    if prior and prior["verdict"] == "FLAG":
                        any_flag = True
                        flag_reasons.append(prior["reason_code"] or check.id)
                    continue

                if not check.applies(cand, ctx):
                    verdict = "SKIP"
                    conn.execute(
                        """
                        INSERT INTO check_results
                          (run_id, candidate_id, check_id, check_version, verdict, severity, reason_code, details_json, computed_at)
                        VALUES (?, ?, ?, ?, 'SKIP', 'info', 'not_applicable', '{}', ?)
                        """,
                        (run_id, cand.candidate_id, check.id, check.version, now),
                    )
                    counts["SKIP"] += 1
                    continue

                v = check.evaluate(cand, ctx)
                conn.execute(
                    """
                    INSERT INTO check_results
                      (run_id, candidate_id, check_id, check_version, verdict, severity, reason_code, details_json, computed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, cand.candidate_id, check.id, check.version, v.status, v.severity,
                     v.reason_code, json.dumps(v.details, default=str), now),
                )
                counts[v.status] = counts.get(v.status, 0) + 1
                if v.status == "FLAG":
                    any_flag = True
                    flag_reasons.append(v.reason_code or check.id)
                    audit.log(
                        actor="pipeline", event_type="CHECK_FLAGGED",
                        run_id=run_id, candidate_id=cand.candidate_id,
                        payload={"check_id": check.id, "reason": v.reason_code, "severity": v.severity},
                        conn=conn,
                    )

            if any_flag:
                conn.execute(
                    """
                    INSERT INTO review_items (run_id, candidate_id, reason_code, status, opened_at)
                    VALUES (?, ?, ?, 'OPEN', ?)
                    ON CONFLICT(run_id, candidate_id) DO UPDATE SET
                        reason_code = excluded.reason_code
                    """,
                    (run_id, cand.candidate_id, ",".join(sorted(set(flag_reasons))), now),
                )
                conn.execute(
                    "UPDATE candidates SET stage='REVIEW_PENDING', stage_updated_at=? WHERE run_id=? AND candidate_id=?",
                    (now, run_id, cand.candidate_id),
                )
            else:
                # Clean MISSING with no flags -> auto-approve; MATCH -> SKIPPED (already in OSM);
                # MATCH_FAR falls through to CHECKED so it can't auto-clear without a decision.
                new_stage = "APPROVED" if cand.verdict == "MISSING" else ("SKIPPED" if cand.verdict == "MATCH" else "CHECKED")
                conn.execute(
                    "UPDATE candidates SET stage=?, stage_updated_at=? WHERE run_id=? AND candidate_id=?",
                    (new_stage, now, run_id, cand.candidate_id),
                )
                # A prior check version may have flagged this candidate; its OPEN review_item
                # no longer reflects reality. Clear it so the queue stays consistent with stage.
                cur = conn.execute(
                    "DELETE FROM review_items WHERE run_id=? AND candidate_id=? AND status='OPEN'",
                    (run_id, cand.candidate_id),
                )
                if cur.rowcount:
                    audit.log(
                        actor="pipeline", event_type="REVIEW_CLEARED",
                        run_id=run_id, candidate_id=cand.candidate_id,
                        payload={"reason": "no_flags_on_rerun"}, conn=conn,
                    )
                if new_stage == "APPROVED":
                    audit.log(
                        actor="pipeline", event_type="AUTO_APPROVED",
                        run_id=run_id, candidate_id=cand.candidate_id,
                        payload={"verdict": cand.verdict}, conn=conn,
                    )

        audit.log(actor="pipeline", event_type="CHECK_RAN", run_id=run_id,
                  payload={"counts": counts, "check_ids": [c.id for c in checks]}, conn=conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return counts


def set_toggle(run_id: int, check_id: str, enabled: bool) -> None:
    conn = _db.connect()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO check_toggles (run_id, check_id, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT(run_id, check_id) DO UPDATE SET enabled = excluded.enabled",
            (run_id, check_id, 1 if enabled else 0),
        )
        audit.log(actor="operator", event_type="CONFIG_CHANGED", run_id=run_id,
                  payload={"check_id": check_id, "enabled": enabled}, conn=conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def counts_by_stage(run_id: int) -> dict[str, int]:
    return candidates.count_by_stage(run_id)


def list_runs() -> list[dict]:
    conn = _db.connect()
    try:
        rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
