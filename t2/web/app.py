"""Flask app factory + routes + HTMX endpoints."""
import json
import os
import subprocess
import sys
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, url_for

from .. import audit, batcher, config as _config, db as _db, osm_client, osm_export, osm_refresh, pipeline, review, tag_diff
from ..conflate import _proposed_tags, _is_poi_node, POI_TAG_KEYS
from ..checks import REGISTRY
from .glossary import GLOSSARY

# Cache per-run OSM JSON by (path, mtime) so panning the review map doesn't
# re-parse a multi-megabyte file on every bbox request. The tuple is
# (mtime, elements, interp_node_ids) — interpolation endpoint nodes are the
# numeric endpoints of an addr:interpolation way and must be hidden from the
# sibling map for the same reason conflate drops them from match_idx.
_OSM_CACHE: dict[Path, tuple[float, list[dict], set[int]]] = {}


def _load_osm_for_run(path: Path) -> tuple[list[dict], set[int]]:
    if not path.exists():
        return [], set()
    mtime = path.stat().st_mtime
    cached = _OSM_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]
    data = json.loads(path.read_text(encoding="utf-8"))
    interp: set[int] = set()
    for el in data:
        if el.get("type") != "way":
            continue
        if "addr:interpolation" not in (el.get("tags") or {}):
            continue
        for nid in el.get("nodes") or ():
            interp.add(nid)
    _OSM_CACHE[path] = (mtime, data, interp)
    return data, interp


def _osm_element_latlon(el: dict) -> tuple[float | None, float | None]:
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    c = el.get("center") or {}
    return c.get("lat"), c.get("lon")


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
    _REVIEW_VERDICTS = ("MATCH", "MATCH_FAR", "MISSING", "SKIPPED")

    def _parse_csv_arg(name: str, allowed: tuple[str, ...]) -> tuple[str, ...]:
        raw = request.args.get(name)
        if raw is None:
            return ()
        return tuple(v for v in raw.split(",") if v in allowed)

    @app.get("/runs/<int:run_id>/review")
    def review_page(run_id: int):
        raw = request.args.get("statuses")
        if raw is None:
            statuses = ("OPEN",)
        else:
            statuses = tuple(s for s in raw.split(",") if s in _REVIEW_STATUSES)
        include_auto = request.args.get("auto", "0") == "1"
        verdicts = _parse_csv_arg("verdicts", _REVIEW_VERDICTS)
        poi_ack = request.args.get("poi_ack", "0") == "1"
        postcode_from_poi = request.args.get("postcode_from_poi", "0") == "1"
        items = review.queue(
            run_id,
            statuses=statuses,
            include_auto=include_auto,
            verdicts=verdicts,
            poi_ack=poi_ack,
            postcode_from_poi=postcode_from_poi,
            limit=500,
        )
        partial = request.args.get("partial") == "1"
        template = "_review_list.html" if partial else "review.html"
        return render_template(
            template,
            run_id=run_id,
            items=items,
            active_statuses=set(statuses),
            active_verdicts=set(verdicts),
            include_auto=include_auto,
            poi_ack=poi_ack,
            postcode_from_poi=postcode_from_poi,
            all_statuses=_REVIEW_STATUSES,
            all_verdicts=_REVIEW_VERDICTS,
            selected_candidate_id=None,
        )

    def _review_detail_context(run_id: int, candidate_id: int):
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
            return None
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
        return {
            "candidate": cand,
            "results": results,
            "run_id": run_id,
            "diff_rows": diff_rows,
            "geom_label": geom_label,
            "review_state": review_state,
            "registry": REGISTRY,
        }

    @app.get("/runs/<int:run_id>/review/<int:candidate_id>")
    def review_detail(run_id: int, candidate_id: int):
        ctx = _review_detail_context(run_id, candidate_id)
        if ctx is None:
            abort(404)
        if request.headers.get("HX-Request"):
            return render_template("_review_detail.html", **ctx)
        raw = request.args.get("statuses")
        if raw is None:
            statuses = ("OPEN",)
        else:
            statuses = tuple(s for s in raw.split(",") if s in _REVIEW_STATUSES)
        include_auto = request.args.get("auto", "0") == "1"
        verdicts = _parse_csv_arg("verdicts", _REVIEW_VERDICTS)
        poi_ack = request.args.get("poi_ack", "0") == "1"
        postcode_from_poi = request.args.get("postcode_from_poi", "0") == "1"
        items = review.queue(
            run_id,
            statuses=statuses,
            include_auto=include_auto,
            verdicts=verdicts,
            poi_ack=poi_ack,
            postcode_from_poi=postcode_from_poi,
            limit=500,
        )
        return render_template(
            "review.html",
            items=items,
            active_statuses=set(statuses),
            active_verdicts=set(verdicts),
            include_auto=include_auto,
            poi_ack=poi_ack,
            postcode_from_poi=postcode_from_poi,
            all_statuses=_REVIEW_STATUSES,
            all_verdicts=_REVIEW_VERDICTS,
            selected_candidate=True,
            selected_candidate_id=candidate_id,
            **ctx,
        )

    @app.post("/runs/<int:run_id>/review/<int:candidate_id>")
    def review_resolve(run_id: int, candidate_id: int):
        status = request.form["status"]
        note = request.form.get("note") or None
        review.resolve(run_id, candidate_id, status, actor="operator", note=note)
        return "", 204

    # ~3 km at Toronto latitude — guards against unbounded zoom-out queries.
    _MAX_BBOX_SPAN_DEG = 0.03
    _SIBLING_LIMIT = 500

    @app.get("/runs/<int:run_id>/siblings")
    def review_siblings(run_id: int):
        raw = request.args.get("bbox") or ""
        try:
            parts = [float(x) for x in raw.split(",")]
        except ValueError:
            abort(400)
        if len(parts) != 4:
            abort(400)
        min_lat, min_lon, max_lat, max_lon = parts
        if min_lat >= max_lat or min_lon >= max_lon:
            abort(400)
        if (max_lat - min_lat) > _MAX_BBOX_SPAN_DEG or (max_lon - min_lon) > _MAX_BBOX_SPAN_DEG:
            cy = (min_lat + max_lat) / 2
            cx = (min_lon + max_lon) / 2
            half = _MAX_BBOX_SPAN_DEG / 2
            min_lat, max_lat = cy - half, cy + half
            min_lon, max_lon = cx - half, cx + half

        try:
            focus_id = int(request.args.get("focus") or 0)
        except ValueError:
            focus_id = 0

        conn = _db.connect()
        try:
            focus_match = None
            if focus_id:
                focus_match = conn.execute(
                    "SELECT nearest_osm_type, nearest_osm_id FROM conflation "
                    "WHERE run_id=? AND candidate_id=?",
                    (run_id, focus_id),
                ).fetchone()
            rows = conn.execute(
                """SELECT c.candidate_id, c.lat, c.lon, c.address_full, c.housenumber,
                          c.street_raw, c.address_class, c.stage,
                          cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type,
                          r.status AS review_status
                   FROM candidates c
                   LEFT JOIN conflation cf USING (run_id, candidate_id)
                   LEFT JOIN review_items r USING (run_id, candidate_id)
                   WHERE c.run_id = ?
                     AND c.lat BETWEEN ? AND ?
                     AND c.lon BETWEEN ? AND ?
                     AND c.candidate_id != ?
                   LIMIT ?""",
                (run_id, min_lat, max_lat, min_lon, max_lon, focus_id, _SIBLING_LIMIT),
            ).fetchall()
        finally:
            conn.close()

        candidates_out = []
        for row in rows:
            d = dict(row)
            candidates_out.append({
                "candidate_id": d["candidate_id"],
                "lat": d["lat"],
                "lon": d["lon"],
                "address": d["address_full"],
                "housenumber": d["housenumber"],
                "street": d["street_raw"],
                "address_class": d["address_class"],
                "stage": d["stage"],
                "verdict": d["verdict"],
                "review_status": d["review_status"],
                "nearest_osm_type": d["nearest_osm_type"],
                "nearest_osm_id": d["nearest_osm_id"],
            })

        excluded: tuple[str, int] | None = None
        if focus_match and focus_match["nearest_osm_id"]:
            excluded = (focus_match["nearest_osm_type"], focus_match["nearest_osm_id"])

        osm_path = cfg.data_dir / f"osm_current_run{run_id}.json"
        elements, interp_node_ids = _load_osm_for_run(osm_path)
        osm_out: list[dict] = []
        for el in elements:
            if el.get("type") == "node" and el.get("id") in interp_node_ids:
                continue
            lat, lon = _osm_element_latlon(el)
            if lat is None or lon is None:
                continue
            if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
                continue
            tags = el.get("tags") or {}
            hn = tags.get("addr:housenumber")
            if not hn:
                continue
            if excluded and el.get("type") == excluded[0] and el.get("id") == excluded[1]:
                continue
            is_poi = _is_poi_node(el)
            poi_tag = None
            if is_poi:
                for key in POI_TAG_KEYS:
                    if key in tags:
                        poi_tag = f"{key}={tags[key]}"
                        break
            osm_out.append({
                "type": el.get("type"),
                "id": el.get("id"),
                "lat": lat,
                "lon": lon,
                "housenumber": hn,
                "street": tags.get("addr:street"),
                "unit": tags.get("addr:unit"),
                "floor": tags.get("addr:floor"),
                "postcode": tags.get("addr:postcode"),
                "name": tags.get("name"),
                "kind": "poi" if is_poi else "address",
                "poi_tag": poi_tag,
            })
            if len(osm_out) >= _SIBLING_LIMIT:
                break

        return jsonify({"candidates": candidates_out, "osm": osm_out})

    # ---- Approved / Skipped lists ----

    @app.get("/runs/<int:run_id>/approved")
    def approved_page(run_id: int):
        verdicts = _parse_csv_arg("verdicts", _REVIEW_VERDICTS)
        poi_ack = request.args.get("poi_ack", "0") == "1"
        postcode_from_poi = request.args.get("postcode_from_poi", "0") == "1"
        extra_where, extra_params = review._poi_where(poi_ack, postcode_from_poi, verdicts)
        and_extra = (" AND " + extra_where) if extra_where else ""
        conn = _db.connect()
        try:
            rows = conn.execute(
                f"""
                SELECT c.candidate_id, c.address_full, c.housenumber, c.street_raw,
                       c.lat, c.lon, c.stage_updated_at, c.address_class,
                       cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type, cf.nearest_dist_m,
                       cf.poi_osm_id, cf.proposed_postcode,
                       r.status AS review_status, r.prior_auto_approved
                FROM candidates c
                LEFT JOIN conflation cf USING (run_id, candidate_id)
                LEFT JOIN review_items r USING (run_id, candidate_id)
                WHERE c.run_id = ? AND c.stage = 'APPROVED'{and_extra}
                ORDER BY c.stage_updated_at DESC
                """,
                (run_id, *extra_params),
            ).fetchall()
        finally:
            conn.close()
        items = [dict(r) for r in rows]
        partial = request.args.get("partial") == "1"
        template = "_approved_list.html" if partial else "approved.html"
        return render_template(
            template,
            run_id=run_id,
            items=items,
            active_verdicts=set(verdicts),
            poi_ack=poi_ack,
            postcode_from_poi=postcode_from_poi,
            all_verdicts=_REVIEW_VERDICTS,
        )

    @app.get("/runs/<int:run_id>/skipped")
    def skipped_page(run_id: int):
        verdicts = _parse_csv_arg("verdicts", _REVIEW_VERDICTS)
        poi_ack = request.args.get("poi_ack", "0") == "1"
        postcode_from_poi = request.args.get("postcode_from_poi", "0") == "1"
        extra_where, extra_params = review._poi_where(poi_ack, postcode_from_poi, verdicts)
        and_extra = (" AND " + extra_where) if extra_where else ""
        conn = _db.connect()
        try:
            rows = conn.execute(
                f"""
                SELECT c.candidate_id, c.address_full, c.housenumber, c.street_raw,
                       c.lat, c.lon, c.lo_num, c.hi_num, c.stage_updated_at, c.address_class,
                       cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type, cf.nearest_dist_m,
                       cf.poi_osm_id, cf.proposed_postcode,
                       r.status AS review_status
                FROM candidates c
                LEFT JOIN conflation cf USING (run_id, candidate_id)
                LEFT JOIN review_items r USING (run_id, candidate_id)
                WHERE c.run_id = ? AND c.stage = 'SKIPPED'{and_extra}
                ORDER BY c.stage_updated_at DESC
                """,
                (run_id, *extra_params),
            ).fetchall()
        finally:
            conn.close()
        items = [dict(r) for r in rows]
        partial = request.args.get("partial") == "1"
        template = "_skipped_list.html" if partial else "skipped.html"
        return render_template(
            template,
            run_id=run_id,
            items=items,
            active_verdicts=set(verdicts),
            poi_ack=poi_ack,
            postcode_from_poi=postcode_from_poi,
            all_verdicts=_REVIEW_VERDICTS,
        )

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

    # ---- Local OSM extract ----

    @app.get("/osm")
    def osm_view():
        meta = osm_refresh.read_meta(cfg)
        running, pid = osm_refresh.is_refresh_running(cfg)
        status = osm_refresh.extract_status(cfg)
        log_tail = osm_refresh.tail_log(cfg, lines=30)
        return render_template(
            "osm.html",
            cfg=cfg,
            meta=meta,
            running=running,
            pid=pid,
            status=status,
            log_tail=log_tail,
        )

    @app.post("/osm/refresh")
    def osm_refresh_start():
        running, pid = osm_refresh.is_refresh_running(cfg)
        if running:
            flash(f"Refresh is already running (pid {pid}). Reload to see progress.")
            return redirect(url_for("osm_view"))
        force = request.form.get("force") == "1"
        args = [sys.executable, "-m", "t2.osm_refresh"]
        if force:
            args.append("--force")
        osm_refresh.extract_dir(cfg).mkdir(parents=True, exist_ok=True)
        # Open the log file for the child's stdout/stderr, then close our copy
        # after Popen returns — the child inherits its own fd, so keeping the
        # parent handle open would leak one fd per refresh click.
        with open(osm_refresh.log_path(cfg), "wb") as log_file:
            popen_kwargs: dict = {
                "stdout": log_file,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
                "close_fds": True,
                "cwd": str(_config.ROOT),
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = (
                    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                popen_kwargs["start_new_session"] = True
            subprocess.Popen(args, **popen_kwargs)
        flash("OSM extract refresh started. Reload the page to watch progress.")
        return redirect(url_for("osm_view"))

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
