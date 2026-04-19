"""Flask app factory + routes + HTMX endpoints."""
import json
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, url_for

from .. import audit, batcher, config as _config, db as _db, osm_client, osm_export, pipeline, review, tag_diff
from ..conflate import _proposed_tags
from ..checks import REGISTRY
from .glossary import GLOSSARY


def create_app() -> Flask:
    cfg = _config.load()
    _db.migrate()

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = cfg.flask_secret_key
    app.jinja_env.globals["tip"] = lambda key: GLOSSARY.get(key, "")

    # ---- Dashboard / runs ----

    @app.get("/")
    def index():
        runs = pipeline.list_runs()
        return render_template("dashboard.html", runs=runs, default_bbox=cfg.default_bbox)

    @app.post("/runs/new")
    def runs_new():
        name = request.form["name"].strip()
        bbox = tuple(float(request.form[k]) for k in ("min_lat", "min_lon", "max_lat", "max_lon"))
        run_id = pipeline.start_run(name, bbox)  # type: ignore
        return redirect(url_for("run_view", run_id=run_id))

    @app.get("/runs/<int:run_id>")
    def run_view(run_id: int):
        run = _get_run(run_id)
        counts = pipeline.counts_by_stage(run_id)
        toggles = _get_toggles(run_id)
        batches = batcher.list_batches(run_id)
        return render_template("run.html", run=run, counts=counts, toggles=toggles,
                               registry=REGISTRY, batches=batches)

    # ---- Stage triggers (HTMX) ----

    @app.post("/runs/<int:run_id>/stage/<stage>")
    def run_stage(run_id: int, stage: str):
        if stage == "ingest":
            n = pipeline.ingest_stage(run_id)
            msg = f"Ingested {n} candidates."
        elif stage == "fetch":
            h = pipeline.fetch_stage(run_id)
            msg = f"Fetched OSM snapshot: {h[:12]}"
        elif stage == "conflate":
            c = pipeline.conflate_stage(run_id)
            msg = f"Conflated: {c}"
        elif stage == "checks":
            c = pipeline.run_checks(run_id)
            msg = f"Checks ran: {c}"
        else:
            abort(400)
        counts = pipeline.counts_by_stage(run_id)
        return render_template("_stage_result.html", msg=msg, counts=counts, run_id=run_id)

    @app.post("/runs/<int:run_id>/toggle/<check_id>")
    def run_toggle(run_id: int, check_id: str):
        enabled = request.form.get("enabled", "0") == "1"
        pipeline.set_toggle(run_id, check_id, enabled)
        toggles = _get_toggles(run_id)
        return render_template("_toggles.html", toggles=toggles, registry=REGISTRY, run_id=run_id)

    # ---- Review ----

    _REVIEW_STATUSES = ("OPEN", "APPROVED", "REJECTED", "DEFERRED")

    @app.get("/runs/<int:run_id>/review")
    def review_page(run_id: int):
        raw = request.args.get("statuses")
        if raw is None:
            statuses = ("OPEN",)
        else:
            statuses = tuple(s for s in raw.split(",") if s in _REVIEW_STATUSES)
        include_auto = request.args.get("auto", "0") == "1"
        items = review.queue(run_id, statuses=statuses, include_auto=include_auto, limit=500)
        partial = request.args.get("partial") == "1"
        template = "_review_list.html" if partial else "review.html"
        return render_template(
            template,
            run_id=run_id,
            items=items,
            active_statuses=set(statuses),
            include_auto=include_auto,
            all_statuses=_REVIEW_STATUSES,
        )

    @app.get("/runs/<int:run_id>/review/<int:candidate_id>")
    def review_detail(run_id: int, candidate_id: int):
        conn = _db.connect()
        try:
            row = conn.execute(
                """SELECT c.*, cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type,
                          cf.nearest_dist_m, cf.matched_osm_tags_json, cf.matched_osm_geom_hint,
                          cf.matched_osm_lat, cf.matched_osm_lon,
                          cf.poi_osm_id, cf.poi_osm_type, cf.poi_tags_json,
                          cf.proposed_postcode
                   FROM candidates c LEFT JOIN conflation cf USING (run_id, candidate_id)
                   WHERE c.run_id=? AND c.candidate_id=?""",
                (run_id, candidate_id),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            abort(404)
        results = review.check_results_for(run_id, candidate_id)
        for r in results:
            try:
                r["details"] = json.loads(r["details_json"] or "{}")
            except Exception:
                r["details"] = {}

        cand = dict(row)
        try:
            osm_tags = json.loads(cand.get("matched_osm_tags_json") or "null")
        except Exception:
            osm_tags = None
        try:
            poi_tags = json.loads(cand.get("poi_tags_json") or "null")
        except Exception:
            poi_tags = None
        cand["poi_tags"] = poi_tags
        proposed = _proposed_tags(cand)
        diff_rows = tag_diff.compare_tags(proposed, osm_tags)
        geom = cand.get("matched_osm_geom_hint")
        if cand.get("nearest_osm_id") and geom:
            polygon_hint = " (polygon)" if geom.endswith("-polygon") else ""
            base_type = geom.split("-")[0]
            geom_label = f"{base_type} #{cand['nearest_osm_id']}{polygon_hint}"
        else:
            geom_label = None
        review_state = review.get_review_state(run_id, candidate_id)
        return render_template(
            "_review_detail.html",
            candidate=cand,
            results=results,
            run_id=run_id,
            diff_rows=diff_rows,
            geom_label=geom_label,
            review_state=review_state,
            registry=REGISTRY,
        )

    @app.post("/runs/<int:run_id>/review/<int:candidate_id>")
    def review_resolve(run_id: int, candidate_id: int):
        status = request.form["status"]
        note = request.form.get("note") or None
        review.resolve(run_id, candidate_id, status, actor="operator", note=note)
        return "", 204

    # ---- Approved / Skipped lists ----

    @app.get("/runs/<int:run_id>/approved")
    def approved_page(run_id: int):
        conn = _db.connect()
        try:
            rows = conn.execute(
                """
                SELECT c.candidate_id, c.address_full, c.housenumber, c.street_raw,
                       c.lat, c.lon, c.stage_updated_at,
                       cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type, cf.nearest_dist_m,
                       r.status AS review_status, r.prior_auto_approved
                FROM candidates c
                LEFT JOIN conflation cf USING (run_id, candidate_id)
                LEFT JOIN review_items r USING (run_id, candidate_id)
                WHERE c.run_id = ? AND c.stage = 'APPROVED'
                ORDER BY c.stage_updated_at DESC
                """,
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        items = [dict(r) for r in rows]
        return render_template("approved.html", run_id=run_id, items=items)

    @app.get("/runs/<int:run_id>/skipped")
    def skipped_page(run_id: int):
        conn = _db.connect()
        try:
            rows = conn.execute(
                """
                SELECT c.candidate_id, c.address_full, c.housenumber, c.street_raw,
                       c.lat, c.lon, c.lo_num, c.hi_num, c.stage_updated_at,
                       cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type, cf.nearest_dist_m,
                       r.status AS review_status
                FROM candidates c
                LEFT JOIN conflation cf USING (run_id, candidate_id)
                LEFT JOIN review_items r USING (run_id, candidate_id)
                WHERE c.run_id = ? AND c.stage = 'SKIPPED'
                ORDER BY c.stage_updated_at DESC
                """,
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        items = [dict(r) for r in rows]
        return render_template("skipped.html", run_id=run_id, items=items)

    # ---- Batches ----

    @app.post("/runs/<int:run_id>/batches")
    def batch_create(run_id: int):
        mode = request.form["mode"]
        size = int(request.form.get("size") or cfg.batch_size)
        bid = batcher.compose(run_id, mode, size)
        if bid is None:
            flash("No APPROVED candidates available to batch.")
            return redirect(url_for("run_view", run_id=run_id))
        return redirect(url_for("batch_view", batch_id=bid))

    @app.get("/batches/<int:batch_id>")
    def batch_view(batch_id: int):
        items = batcher.load_batch_items(batch_id)
        conn = _db.connect()
        try:
            batch = dict(conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone() or {})
        finally:
            conn.close()
        return render_template("batch.html", batch=batch, items=items)

    @app.post("/batches/<int:batch_id>/export")
    def batch_export(batch_id: int):
        path = osm_export.write_xml(batch_id)
        flash(f"Wrote {path.name} to data/")
        return redirect(url_for("batch_view", batch_id=batch_id))

    @app.post("/batches/<int:batch_id>/upload")
    def batch_upload(batch_id: int):
        try:
            osm_client.upload(batch_id)
            flash(f"Uploaded batch {batch_id}.")
        except osm_client.OsmAuthError as e:
            flash(f"Not authorized: {e}. Visit /oauth/start.")
        except Exception as e:
            flash(f"Upload failed: {e}")
        return redirect(url_for("batch_view", batch_id=batch_id))

    # ---- Audit ----

    @app.get("/runs/<int:run_id>/audit")
    def audit_page(run_id: int):
        event_type = request.args.get("type") or None
        conn = _db.connect()
        try:
            if event_type:
                rows = conn.execute(
                    "SELECT * FROM events WHERE run_id=? AND event_type=? ORDER BY event_id DESC LIMIT 500",
                    (run_id, event_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE run_id=? ORDER BY event_id DESC LIMIT 500",
                    (run_id,),
                ).fetchall()
        finally:
            conn.close()
        events = [dict(r) for r in rows]
        for e in events:
            try:
                e["payload"] = json.loads(e["payload_json"] or "{}")
            except Exception:
                e["payload"] = {}
        return render_template("audit.html", run_id=run_id, events=events, event_type=event_type)

    # ---- OAuth ----

    @app.get("/oauth/start")
    def oauth_start():
        if not cfg.osm_client_id:
            return ("OSM_CLIENT_ID not set in .env. Register an OAuth2 app on "
                    f"{cfg.osm_api_base}/oauth2/applications first.", 500)
        url, _state = osm_client.build_auth_url()
        return redirect(url)

    @app.get("/oauth/callback")
    def oauth_callback():
        code = request.args.get("code")
        state = request.args.get("state")
        if not code or not state:
            return "missing code/state", 400
        osm_client.exchange_code(code, state)
        flash("OSM authorization complete.")
        return redirect(url_for("index"))

    return app


def _get_run(run_id: int) -> dict:
    conn = _db.connect()
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)
    return dict(row)


def _get_toggles(run_id: int) -> dict[str, bool]:
    conn = _db.connect()
    try:
        rows = conn.execute(
            "SELECT check_id, enabled FROM check_toggles WHERE run_id=?", (run_id,),
        ).fetchall()
    finally:
        conn.close()
    return {r["check_id"]: bool(r["enabled"]) for r in rows}
