"""Flask app factory + routes + HTMX endpoints."""
import json
import os
import subprocess
import sys
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, url_for

from .. import audit, batcher, candidates, config as _config, db as _db, multi_addresses as _multi_addresses, multi_fixes as _multi_fixes, osm_client, osm_export, osm_refresh, pipeline, ranges as _ranges, review, source_db, tag_diff, tiles_build
from ..conflate import _proposed_tags, _is_poi_node, POI_TAG_KEYS, normalize_street
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


_TILES_CACHE: dict[Path, tuple[float, list[dict], dict[str, dict], dict]] = {}


def _load_tiles(path: Path) -> tuple[list[dict], dict[str, dict], dict]:
    """Return (tiles_list, tiles_by_id, meta) from data/tiles.json, cached by mtime."""
    if not path.exists():
        return [], {}, {}
    mtime = path.stat().st_mtime
    cached = _TILES_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1], cached[2], cached[3]
    data = json.loads(path.read_text(encoding="utf-8"))
    tiles = data.get("tiles", [])
    by_id = {t["id"]: t for t in tiles}
    meta = {k: v for k, v in data.items() if k != "tiles"}
    _TILES_CACHE[path] = (mtime, tiles, by_id, meta)
    return tiles, by_id, meta


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
    _static_run_id_env = os.environ.get("T2_STATIC_EXPORT_RUN_ID", "")
    _static_run_ids_env = os.environ.get("T2_STATIC_EXPORT_RUN_IDS", "")
    _static_run_id = int(_static_run_id_env) if _static_run_id_env.isdigit() else None
    _static_run_ids = [int(x) for x in _static_run_ids_env.split(",") if x.strip().isdigit()]
    if not _static_run_ids and _static_run_id is not None:
        _static_run_ids = [_static_run_id]
    app.jinja_env.globals["static_export"] = {
        "active": os.environ.get("T2_STATIC_EXPORT") == "1",
        "run_name": os.environ.get("T2_STATIC_EXPORT_RUN_NAME", ""),
        "snapshot_date": os.environ.get("T2_STATIC_EXPORT_SNAPSHOT_DATE", ""),
        "run_id": _static_run_id,
        "run_ids": _static_run_ids,
        "multi": os.environ.get("T2_STATIC_EXPORT_MULTI") == "1",
    }
    _api_base_lower = (cfg.osm_api_base or "").lower()
    if "dev.openstreetmap" in _api_base_lower:
        _target_label, _target_class = "DEV", "dev"
    elif "api.openstreetmap.org" in _api_base_lower:
        _target_label, _target_class = "PROD", "prod"
    else:
        _target_label, _target_class = "OTHER", "other"
    app.jinja_env.globals["osm_target"] = {
        "label": _target_label,
        "class": _target_class,
        "api_base": cfg.osm_api_base,
    }

    @app.context_processor
    def _inject_source_snapshot():
        return {"source_snapshot_info": source_db.latest_snapshot_info()}

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

    # ---- Tile picker ----

    @app.get("/map")
    def tiles_map():
        tiles, _by_id, meta = _load_tiles(cfg.data_dir / "tiles.json")
        return render_template(
            "map.html",
            tiles=tiles,
            meta=meta,
            toronto_bbox=list(cfg.osm_toronto_bbox),
        )

    @app.get("/tiles/<tile_id>")
    def tile_view(tile_id: str):
        tiles, by_id, _meta = _load_tiles(cfg.data_dir / "tiles.json")
        tile = by_id.get(tile_id)
        if not tile:
            abort(404)
        tile_index = next((i + 1 for i, t in enumerate(tiles) if t["id"] == tile_id), None)
        tile_total = len(tiles)
        b = tile["bbox"]
        conn = _db.connect()
        try:
            prior_runs = conn.execute(
                "SELECT run_id, name, created_at, source_snapshot_id FROM runs "
                "WHERE ROUND(bbox_min_lat,6)=? AND ROUND(bbox_min_lon,6)=? "
                "  AND ROUND(bbox_max_lat,6)=? AND ROUND(bbox_max_lon,6)=? "
                "ORDER BY created_at DESC",
                (round(b[0], 6), round(b[1], 6), round(b[2], 6), round(b[3], 6)),
            ).fetchall()
        finally:
            conn.close()
        from datetime import date
        prefill_name = f"{tile['id']}-{date.today().isoformat()}"
        return render_template(
            "tile.html",
            tile=tile,
            tile_index=tile_index,
            tile_total=tile_total,
            prior_runs=prior_runs,
            prefill_name=prefill_name,
        )

    def _tile_for_run(run: dict) -> tuple[dict | None, int | None, int]:
        """Resolve (tile, 1-based-index, total_tiles) for a run by rounded bbox."""
        tiles, _by_id, _meta = _load_tiles(cfg.data_dir / "tiles.json")
        if not tiles:
            return None, None, 0
        target = (
            round(run["bbox_min_lat"], 6), round(run["bbox_min_lon"], 6),
            round(run["bbox_max_lat"], 6), round(run["bbox_max_lon"], 6),
        )
        for i, t in enumerate(tiles):
            if tuple(t["bbox"]) == target:
                return t, i + 1, len(tiles)
        return None, None, len(tiles)

    @app.get("/runs/<int:run_id>")
    def run_view(run_id: int):
        run = _get_run(run_id)
        counts = pipeline.counts_by_stage(run_id)
        toggles = _get_toggles(run_id)
        batches = batcher.list_batches(run_id)
        tile, tile_index, tile_total = _tile_for_run(run)
        status = pipeline.stage_status(run_id)
        from datetime import datetime
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        new_run_name = f"{tile['id']}-{stamp}" if tile else f"{run['name']}-rerun-{stamp}"
        missing_sample_every_nth = pipeline.get_check_param(
            run_id, "missing_sample", "every_nth", 50
        )
        return render_template("run.html", run=run, counts=counts, toggles=toggles,
                               registry=REGISTRY, batches=batches, tile=tile,
                               tile_index=tile_index, tile_total=tile_total,
                               status=status, stage_order=("ingest", "fetch", "conflate", "checks"),
                               ranges_count=candidates.count_ranges(run_id),
                               new_run_name=new_run_name,
                               missing_sample_every_nth=missing_sample_every_nth)

    @app.get("/runs/<int:run_id>/neighbor")
    def run_neighbor(run_id: int):
        direction = request.args.get("dir")
        if direction not in ("prev", "next"):
            abort(400)
        run = _get_run(run_id)
        tiles, _by_id, _meta = _load_tiles(cfg.data_dir / "tiles.json")
        if not tiles:
            return jsonify({"run_id": None})
        target = (
            round(run["bbox_min_lat"], 6), round(run["bbox_min_lon"], 6),
            round(run["bbox_max_lat"], 6), round(run["bbox_max_lon"], 6),
        )
        cur_idx = next((i for i, t in enumerate(tiles) if tuple(t["bbox"]) == target), None)
        if cur_idx is None:
            return jsonify({"run_id": None})
        conn = _db.connect()
        try:
            rows = conn.execute(
                "SELECT run_id, bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon "
                "FROM runs ORDER BY created_at DESC, run_id DESC"
            ).fetchall()
        finally:
            conn.close()
        latest_by_bbox: dict[tuple, int] = {}
        for r in rows:
            key = (
                round(r["bbox_min_lat"], 6), round(r["bbox_min_lon"], 6),
                round(r["bbox_max_lat"], 6), round(r["bbox_max_lon"], 6),
            )
            latest_by_bbox.setdefault(key, r["run_id"])
        step = 1 if direction == "next" else -1
        i = cur_idx + step
        while 0 <= i < len(tiles):
            key = tuple(tiles[i]["bbox"])
            hit = latest_by_bbox.get(key)
            if hit is not None:
                return jsonify({"run_id": hit})
            i += step
        return jsonify({"run_id": None})

    # ---- Stage triggers (HTMX) ----

    _STAGE_RUNNERS = {
        "ingest":   lambda rid: f"Ingested {pipeline.ingest_stage(rid)} candidates.",
        "fetch":    lambda rid: f"Fetched OSM snapshot: {pipeline.fetch_stage(rid)[:12]}",
        "conflate": lambda rid: f"Conflated: {pipeline.conflate_stage(rid)}",
        "checks":   lambda rid: f"Checks ran: {pipeline.run_checks(rid)}",
    }
    _STAGE_ORDER = ("ingest", "fetch", "conflate", "checks")

    def _render_pipeline(run_id: int, msg: str | None = None, error: str | None = None):
        return render_template(
            "_pipeline.html",
            run_id=run_id,
            msg=msg,
            error=error,
            counts=pipeline.counts_by_stage(run_id),
            status=pipeline.stage_status(run_id),
            stage_order=_STAGE_ORDER,
            ranges_count=candidates.count_ranges(run_id),
        )

    @app.post("/runs/<int:run_id>/stage/<stage>")
    def run_stage(run_id: int, stage: str):
        if stage == "all":
            parts: list[str] = []
            for s in _STAGE_ORDER:
                try:
                    parts.append(_STAGE_RUNNERS[s](run_id))
                except Exception as exc:
                    return _render_pipeline(
                        run_id,
                        msg=" · ".join(parts) if parts else None,
                        error=f"{s}: {exc}",
                    )
            return _render_pipeline(run_id, msg=" · ".join(parts))
        runner = _STAGE_RUNNERS.get(stage)
        if runner is None:
            abort(400)
        try:
            msg = runner(run_id)
        except Exception as exc:
            return _render_pipeline(run_id, error=f"{stage}: {exc}")
        return _render_pipeline(run_id, msg=msg)

    @app.post("/runs/<int:run_id>/toggle/<check_id>")
    def run_toggle(run_id: int, check_id: str):
        enabled = request.form.get("enabled", "0") == "1"
        pipeline.set_toggle(run_id, check_id, enabled)
        toggles = _get_toggles(run_id)
        return render_template("_toggles.html", toggles=toggles, registry=REGISTRY, run_id=run_id)

    @app.post("/runs/<int:run_id>/sample_rate")
    def run_sample_rate(run_id: int):
        try:
            every_nth = int(request.form.get("every_nth", "50"))
        except ValueError:
            abort(400)
        if every_nth < 0:
            abort(400)
        pipeline.set_check_param(run_id, "missing_sample", "every_nth", every_nth)
        flash(f"Sample rate set: every {every_nth} MISSINGs flagged for spot-check.")
        return redirect(url_for("run_view", run_id=run_id))

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
        all_reasons = review.available_reasons(run_id)
        reasons = _parse_csv_arg("reasons", tuple(all_reasons))
        items = review.queue(
            run_id,
            statuses=statuses,
            include_auto=include_auto,
            verdicts=verdicts,
            poi_ack=poi_ack,
            postcode_from_poi=postcode_from_poi,
            reasons=reasons,
            limit=500,
        )
        partial = request.args.get("partial") == "1"
        template = "_review_list.html" if partial else "review.html"
        tile_index = tile_total = None
        if not partial:
            conn = _db.connect()
            try:
                run_row = conn.execute(
                    "SELECT bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon "
                    "FROM runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            finally:
                conn.close()
            if run_row is not None:
                _tile, tile_index, tile_total = _tile_for_run(dict(run_row))
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
            all_reasons=all_reasons,
            active_reasons=set(reasons),
            selected_candidate_id=None,
            municipality_collisions=review.colliding_address_fulls(run_id),
            view="review",
            tile_index=tile_index,
            tile_total=tile_total,
        )

    def _review_detail_context(run_id: int, candidate_id: int):
        conn = _db.connect()
        try:
            row = conn.execute(
                """SELECT c.*, cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type,
                          cf.nearest_dist_m, cf.matched_osm_tags_json, cf.matched_osm_geom_hint,
                          cf.matched_osm_lat, cf.matched_osm_lon,
                          cf.poi_osm_id, cf.poi_osm_type, cf.poi_tags_json,
                          cf.proposed_postcode,
                          cf.dup_sibling_candidate_id, cf.dup_sibling_dist_m
                   FROM candidates c LEFT JOIN conflation cf USING (run_id, candidate_id)
                   WHERE c.run_id=? AND c.candidate_id=?""",
                (run_id, candidate_id),
            ).fetchone()
            sibling_row = None
            if row and row["dup_sibling_candidate_id"]:
                sibling_row = conn.execute(
                    """SELECT c.lat, c.lon, c.address_full, c.housenumber, c.street_raw,
                              cf.verdict
                       FROM candidates c LEFT JOIN conflation cf USING (run_id, candidate_id)
                       WHERE c.run_id=? AND c.candidate_id=?""",
                    (run_id, row["dup_sibling_candidate_id"]),
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
        if sibling_row is not None:
            cand["dup_sibling_lat"] = sibling_row["lat"]
            cand["dup_sibling_lon"] = sibling_row["lon"]
            cand["dup_sibling_address"] = (
                sibling_row["address_full"]
                or " ".join(
                    p for p in (sibling_row["housenumber"], sibling_row["street_raw"]) if p
                )
                or None
            )
            cand["dup_sibling_verdict"] = sibling_row["verdict"]
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
            return render_template("_review_detail.html", view="review", **ctx)
        raw = request.args.get("statuses")
        if raw is None:
            statuses = ("OPEN",)
        else:
            statuses = tuple(s for s in raw.split(",") if s in _REVIEW_STATUSES)
        include_auto = request.args.get("auto", "0") == "1"
        verdicts = _parse_csv_arg("verdicts", _REVIEW_VERDICTS)
        poi_ack = request.args.get("poi_ack", "0") == "1"
        postcode_from_poi = request.args.get("postcode_from_poi", "0") == "1"
        all_reasons = review.available_reasons(run_id)
        reasons = _parse_csv_arg("reasons", tuple(all_reasons))
        items = review.queue(
            run_id,
            statuses=statuses,
            include_auto=include_auto,
            verdicts=verdicts,
            poi_ack=poi_ack,
            postcode_from_poi=postcode_from_poi,
            reasons=reasons,
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
            all_reasons=all_reasons,
            active_reasons=set(reasons),
            selected_candidate=True,
            selected_candidate_id=candidate_id,
            municipality_collisions=review.colliding_address_fulls(run_id),
            view="review",
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
        # include_interp=<street_norm> opts the response into addr:interpolation
        # ways whose normalized street matches and whose bounding box intersects
        # the map bbox — used by the ranges view to overlay interp geometry.
        interp_street = (request.args.get("include_interp") or "").strip()
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

        interp_out: list[dict] = []
        if interp_street:
            node_index = {
                el["id"]: el for el in elements
                if el.get("type") == "node" and el.get("id") is not None
            }
            for el in elements:
                if el.get("type") != "way":
                    continue
                tags = el.get("tags") or {}
                if "addr:interpolation" not in tags:
                    continue
                b = el.get("bounds") or {}
                if not b:
                    continue
                if not (
                    b.get("minlat", 90) <= max_lat
                    and b.get("maxlat", -90) >= min_lat
                    and b.get("minlon", 180) <= max_lon
                    and b.get("maxlon", -180) >= min_lon
                ):
                    continue
                node_ids = el.get("nodes") or []
                ep_ids = node_ids[:1] + node_ids[-1:] if len(node_ids) >= 2 else list(node_ids)
                endpoints: list[dict] = []
                line: list[list[float]] = []
                endpoint_streets: list[str] = []
                for nid in ep_ids:
                    n = node_index.get(nid)
                    if not n:
                        continue
                    nlat, nlon = n.get("lat"), n.get("lon")
                    if nlat is None or nlon is None:
                        continue
                    ntags = n.get("tags") or {}
                    endpoints.append({
                        "id": nid,
                        "lat": nlat,
                        "lon": nlon,
                        "housenumber": ntags.get("addr:housenumber"),
                    })
                    line.append([nlat, nlon])
                    if ntags.get("addr:street"):
                        endpoint_streets.append(ntags["addr:street"])
                # addr:interpolation ways usually carry no addr:street themselves —
                # the street tag lives on the endpoint nodes. Match either source.
                street_label = tags.get("addr:street") or (endpoint_streets[0] if endpoint_streets else None)
                candidates_for_match = [tags.get("addr:street")] + endpoint_streets
                if not any(normalize_street(s) == interp_street for s in candidates_for_match if s):
                    continue
                interp_out.append({
                    "id": el.get("id"),
                    "interpolation": tags.get("addr:interpolation"),
                    "street": street_label,
                    "endpoints": endpoints,
                    "line": line,
                })

        return jsonify({
            "candidates": candidates_out,
            "osm": osm_out,
            "interp_ways": interp_out,
        })

    # ---- Approved / Skipped lists ----

    def _approved_list_context(run_id: int) -> dict:
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
                       c.lat, c.lon, c.stage_updated_at, c.address_class, c.municipality_name,
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
        return {
            "run_id": run_id,
            "items": [dict(r) for r in rows],
            "active_verdicts": set(verdicts),
            "poi_ack": poi_ack,
            "postcode_from_poi": postcode_from_poi,
            "all_verdicts": _REVIEW_VERDICTS,
            "municipality_collisions": review.colliding_address_fulls(run_id),
            "view": "approved",
        }

    def _skipped_list_context(run_id: int) -> dict:
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
                       c.municipality_name,
                       cf.verdict, cf.nearest_osm_id, cf.nearest_osm_type, cf.nearest_dist_m,
                       cf.poi_osm_id, cf.proposed_postcode,
                       cf.dup_sibling_candidate_id, cf.dup_sibling_dist_m,
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
        return {
            "run_id": run_id,
            "items": [dict(r) for r in rows],
            "active_verdicts": set(verdicts),
            "poi_ack": poi_ack,
            "postcode_from_poi": postcode_from_poi,
            "all_verdicts": _REVIEW_VERDICTS,
            "municipality_collisions": review.colliding_address_fulls(run_id),
            "view": "skipped",
        }

    @app.get("/runs/<int:run_id>/approved")
    def approved_page(run_id: int):
        ctx = _approved_list_context(run_id)
        partial = request.args.get("partial") == "1"
        template = "_approved_list.html" if partial else "approved.html"
        return render_template(template, **ctx)

    @app.get("/runs/<int:run_id>/approved/<int:candidate_id>")
    def approved_detail(run_id: int, candidate_id: int):
        detail_ctx = _review_detail_context(run_id, candidate_id)
        if detail_ctx is None:
            abort(404)
        if request.headers.get("HX-Request"):
            return render_template("_review_detail.html", view="approved", **detail_ctx)
        list_ctx = _approved_list_context(run_id)
        return render_template(
            "approved.html",
            selected_candidate=True,
            selected_candidate_id=candidate_id,
            **list_ctx,
            **{k: v for k, v in detail_ctx.items() if k != "run_id"},
        )

    @app.get("/runs/<int:run_id>/skipped")
    def skipped_page(run_id: int):
        ctx = _skipped_list_context(run_id)
        partial = request.args.get("partial") == "1"
        template = "_skipped_list.html" if partial else "skipped.html"
        return render_template(template, **ctx)

    @app.get("/runs/<int:run_id>/skipped/<int:candidate_id>")
    def skipped_detail(run_id: int, candidate_id: int):
        detail_ctx = _review_detail_context(run_id, candidate_id)
        if detail_ctx is None:
            abort(404)
        if request.headers.get("HX-Request"):
            return render_template("_review_detail.html", view="skipped", **detail_ctx)
        list_ctx = _skipped_list_context(run_id)
        return render_template(
            "skipped.html",
            selected_candidate=True,
            selected_candidate_id=candidate_id,
            **list_ctx,
            **{k: v for k, v in detail_ctx.items() if k != "run_id"},
        )

    # ---- Ranges (read-only view of address-range candidates) ----

    _RANGE_COVERAGE_CATS = _ranges.CATEGORIES

    def _ranges_list_context(run_id: int) -> dict:
        raw = request.args.get("coverage")
        if raw is None:
            # Default: hide fully-covered ranges (they're the uninteresting case).
            active_cats: tuple[str, ...] = ("uncovered", "partial", "unknown")
        else:
            active_cats = tuple(c for c in raw.split(",") if c in _RANGE_COVERAGE_CATS)

        def _fetch():
            conn = _db.connect()
            try:
                return conn.execute(
                    """
                    SELECT candidate_id, address_full, housenumber, street_raw, street_norm,
                           lat, lon, lo_num, lo_num_suf, hi_num, hi_num_suf,
                           address_class, municipality_name, stage_updated_at,
                           range_coverage_cat, range_parity_present, range_parity_total
                    FROM candidates
                    WHERE run_id = ?
                      AND stage = 'SKIPPED'
                      AND lo_num IS NOT NULL AND hi_num IS NOT NULL
                      AND lo_num != hi_num
                    ORDER BY stage_updated_at DESC
                    """,
                    (run_id,),
                ).fetchall()
            finally:
                conn.close()

        rows = _fetch()
        # Lazy backfill for runs that predate the range_coverage cache columns.
        if rows and any(r["range_coverage_cat"] is None for r in rows):
            _ranges.compute_for_run(run_id)
            rows = _fetch()

        items: list[dict] = []
        counts = {c: 0 for c in _RANGE_COVERAGE_CATS}
        for r in rows:
            d = dict(r)
            cat = d["range_coverage_cat"] or "unknown"
            d["coverage_cat"] = cat
            d["parity_present_count"] = d["range_parity_present"] or 0
            d["parity_total"] = d["range_parity_total"] or 0
            counts[cat] += 1
            if cat in active_cats:
                items.append(d)
        return {
            "run_id": run_id,
            "items": items,
            "counts": counts,
            "active_cats": set(active_cats),
            "all_cats": _RANGE_COVERAGE_CATS,
            "view": "ranges",
        }

    @app.get("/runs/<int:run_id>/ranges")
    def ranges_page(run_id: int):
        ctx = _ranges_list_context(run_id)
        partial = request.args.get("partial") == "1"
        template = "_ranges_list.html" if partial else "ranges.html"
        return render_template(template, **ctx)

    def _ranges_detail_context(run_id: int, candidate_id: int) -> dict | None:
        conn = _db.connect()
        try:
            row = conn.execute(
                """SELECT candidate_id, address_full, housenumber, street_raw,
                          street_norm, lat, lon, lo_num, lo_num_suf, hi_num, hi_num_suf,
                          address_class, municipality_name, stage
                   FROM candidates
                   WHERE run_id=? AND candidate_id=?""",
                (run_id, candidate_id),
            ).fetchone()
            run_row = conn.execute(
                "SELECT source_snapshot_id FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        cand = dict(row)
        snap_id = int(run_row["source_snapshot_id"]) if run_row and run_row["source_snapshot_id"] else None
        coverage = _ranges.coverage(cand, snap_id)
        return {"run_id": run_id, "candidate": cand, "coverage": coverage}

    @app.get("/runs/<int:run_id>/ranges/<int:candidate_id>")
    def ranges_detail(run_id: int, candidate_id: int):
        detail_ctx = _ranges_detail_context(run_id, candidate_id)
        if detail_ctx is None:
            abort(404)
        if request.headers.get("HX-Request"):
            return render_template("_ranges_detail.html", **detail_ctx)
        list_ctx = _ranges_list_context(run_id)
        return render_template(
            "ranges.html",
            selected_candidate=True,
            selected_candidate_id=candidate_id,
            **list_ctx,
            **{k: v for k, v in detail_ctx.items() if k != "run_id"},
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

    # ---- Data stats ----

    @app.get("/data")
    def data_view():
        stats = _collect_data_stats(cfg)
        return render_template("data.html", stats=stats)

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

    @app.get("/osm/multi")
    def osm_multi_view():
        extract_dir = cfg.osm_extract_dir
        json_path = extract_dir / "toronto-addresses.json"
        stats = _multi_addresses.collect(json_path)
        return render_template("osm_multi.html", stats=stats)

    @app.get("/osm/multi/corners")
    def osm_multi_corners_view():
        extract_dir = cfg.osm_extract_dir
        json_path = extract_dir / "toronto-addresses.json"
        listing = _multi_addresses.list_corner_lots(json_path)
        return render_template("osm_multi_corners.html", listing=listing)

    @app.get("/osm/multi/all")
    def osm_multi_all_view():
        extract_dir = cfg.osm_extract_dir
        json_path = extract_dir / "toronto-addresses.json"
        listing = _multi_addresses.list_entries(json_path)
        verdicts = _multi_fixes.load_verdicts()
        exported = _multi_fixes.load_exported_set()
        return render_template(
            "osm_multi_all.html",
            listing=listing,
            verdicts=verdicts,
            exported=exported,
            verdict_options=_multi_fixes.VERDICT_OPTIONS,
        )

    @app.post("/osm/multi/all/save")
    def osm_multi_all_save():
        entries: list[tuple[str, int, str]] = []
        ticked: set[tuple[str, int]] = set()
        for key, value in request.form.items():
            if key.startswith("v-"):
                try:
                    _, osm_type, osm_id = key.split("-", 2)
                    oid = int(osm_id)
                except ValueError:
                    continue
                if osm_type not in ("node", "way", "relation"):
                    continue
                if value not in _multi_fixes.VERDICTS:
                    continue
                entries.append((osm_type, oid, value))
            elif key.startswith("ex-"):
                try:
                    _, osm_type, osm_id = key.split("-", 2)
                    oid = int(osm_id)
                except ValueError:
                    continue
                if osm_type not in ("node", "way", "relation"):
                    continue
                ticked.add((osm_type, oid))

        saved = _multi_fixes.save_verdicts(entries)
        # Apply operator-controlled exported flags for every row that had a
        # verdict picked on this form: ticked rows are marked exported (so
        # they won't be re-shipped), un-ticked rows are cleared. Rows without
        # a verdict this save are untouched.
        entry_keys = [(t, i) for (t, i, _) in entries]
        set_keys = [k for k in entry_keys if k in ticked]
        clear_keys = [k for k in entry_keys if k not in ticked]
        _multi_fixes.set_exported_flags(set_keys, clear_keys)
        # Only export rows that haven't already been written to a prior .osm
        # file. save_verdicts cleared exported_at for any row whose verdict
        # actually changed, and set_exported_flags just applied the checkbox
        # state, so new + re-classified + un-ticked rows are picked up here.
        to_export = _multi_fixes.load_unexported_actionable()
        try:
            summary = _multi_fixes.build_export(
                to_export, cfg.data_dir / "multi_fixes"
            )
            error = None
        except Exception as exc:
            summary = {
                "total_verdicts": len(to_export),
                "actionable": len(to_export),
                "applied": [],
                "conflicts": [],
                "file_name": None,
            }
            error = f"Overpass fetch failed: {exc}"
        return render_template(
            "osm_multi_export.html",
            saved=saved,
            summary=summary,
            error=error,
        )

    @app.get("/osm/multi/export/<path:filename>")
    def osm_multi_export_download(filename: str):
        # Restrict to files we emit: `t2-multi-fix-*.osm` under data/multi_fixes.
        if "/" in filename or "\\" in filename or not filename.startswith("t2-multi-fix-") or not filename.endswith(".osm"):
            abort(404)
        return send_from_directory(
            cfg.data_dir / "multi_fixes",
            filename,
            as_attachment=True,
            mimetype="application/xml",
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


_TOOL_DB_TABLES = (
    "runs", "candidates", "conflation", "check_results", "check_toggles",
    "review_items", "batches", "batch_items", "changesets", "events",
)


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _collect_data_stats(cfg) -> dict:
    tool_db_main = _file_size(cfg.tool_db_path) or 0
    tool_db_wal = _file_size(cfg.tool_db_path.with_suffix(cfg.tool_db_path.suffix + "-wal")) or 0
    tool_db_shm = _file_size(cfg.tool_db_path.with_suffix(cfg.tool_db_path.suffix + "-shm")) or 0

    source_path = Path(cfg.source_sqlite_path)
    source_size = _file_size(source_path)

    extract_dir = cfg.osm_extract_dir
    pbf_path = extract_dir / "ontario-latest.osm.pbf"
    json_path = extract_dir / "toronto-addresses.json"

    run_json_files: list[dict] = []
    run_json_total = 0
    if cfg.data_dir.exists():
        for p in sorted(cfg.data_dir.glob("osm_current_run*.json")):
            size = _file_size(p) or 0
            run_json_total += size
            run_json_files.append({"name": p.name, "bytes": size})

    conn = _db.connect()
    try:
        table_counts: list[dict] = []
        for name in _TOOL_DB_TABLES:
            try:
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()
                table_counts.append({"name": name, "rows": int(row["n"])})
            except Exception as e:
                table_counts.append({"name": name, "rows": None, "error": str(e)})
        schema_row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        schema_version = schema_row["v"] if schema_row else None
    finally:
        conn.close()

    return {
        "tool_db": {
            "path": str(cfg.tool_db_path),
            "main_bytes": tool_db_main,
            "wal_bytes": tool_db_wal,
            "shm_bytes": tool_db_shm,
            "total_bytes": tool_db_main + tool_db_wal + tool_db_shm,
            "schema_version": schema_version,
            "tables": table_counts,
        },
        "source_db": {
            "path": str(source_path),
            "bytes": source_size,
            "exists": source_size is not None,
        },
        "osm_extract": {
            "dir": str(extract_dir),
            "pbf_bytes": _file_size(pbf_path),
            "json_bytes": _file_size(json_path),
        },
        "run_json": {
            "count": len(run_json_files),
            "total_bytes": run_json_total,
            "files": run_json_files,
        },
    }
