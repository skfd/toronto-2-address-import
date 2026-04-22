"""Static HTML export of multiple pipeline runs into one site tree.

Reads docs/spike_runs.json (the mapping written by t2.spike_drive) and
renders the same reviewer pages as t2.static_export does for one run, but
for every run in the mapping, into a single output tree that also includes
the whole-city /map page and a /tiles/<id>/ page per exported tile.

Intended for spike/experimental use to measure total output size before we
decide on trimming levers.

    python -m t2.static_export_all --runs-json docs/spike_runs.json --out docs/spike
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import date
from pathlib import Path

from . import static_export as _single


def _tile_id_for_run(data_dir: Path, run_id: int, spike_mapping: dict) -> str | None:
    # The spike mapping already has tile_id per run; prefer that.
    for entry in spike_mapping.values():
        if int(entry["run_id"]) == int(run_id):
            return entry["tile_id"]
    # Fallback: bbox match against tiles.json
    bbox = _single._run_bbox(run_id)
    return _single._pilot_tile_id(data_dir, bbox)


def _trim_dashboard_multi(run_ids: set[int]):
    from . import pipeline
    original = pipeline.list_runs

    def only_spike():
        return [r for r in original() if int(r["run_id"]) in run_ids]

    pipeline.list_runs = only_spike  # type: ignore[assignment]
    return original


def _global_pairs() -> list[tuple[str, str]]:
    """Pages that aren't per-run or per-tile — rendered once."""
    return [
        ("/", "index.html"),
        ("/map", "map/index.html"),
        ("/data", "data/index.html"),
        ("/osm", "osm/index.html"),
    ]


def _per_run_pairs(run_id: int, candidates: list[dict], batch_ids: list[int]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = [
        (f"/runs/{run_id}", f"runs/{run_id}/index.html"),
        (f"/runs/{run_id}/review", f"runs/{run_id}/review/index.html"),
        (f"/runs/{run_id}/review?auto=1", f"runs/{run_id}/review/auto/index.html"),
        (f"/runs/{run_id}/review?statuses=APPROVED", f"runs/{run_id}/review/approved-status/index.html"),
        (f"/runs/{run_id}/review?statuses=REJECTED", f"runs/{run_id}/review/rejected-status/index.html"),
        (f"/runs/{run_id}/review?statuses=DEFERRED", f"runs/{run_id}/review/deferred-status/index.html"),
        (f"/runs/{run_id}/approved", f"runs/{run_id}/approved/index.html"),
        (f"/runs/{run_id}/skipped", f"runs/{run_id}/skipped/index.html"),
        (f"/runs/{run_id}/ranges", f"runs/{run_id}/ranges/index.html"),
        (f"/runs/{run_id}/audit", f"runs/{run_id}/audit/index.html"),
    ]
    for bid in batch_ids:
        pairs.append((f"/batches/{bid}", f"batches/{bid}/index.html"))
    for c in candidates:
        cid = c["candidate_id"]
        if c.get("stage") != "SKIPPED":
            pairs.append((f"/runs/{run_id}/review/{cid}", f"runs/{run_id}/review/{cid}/index.html"))
        if _single._is_range(c):
            pairs.append((f"/runs/{run_id}/ranges/{cid}", f"runs/{run_id}/ranges/{cid}/index.html"))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="t2.static_export_all")
    parser.add_argument("--runs-json", type=Path, default=Path("docs/spike_runs.json"))
    parser.add_argument("--out", type=Path, default=Path("docs/spike"))
    parser.add_argument("--snapshot-date", type=str, default=date.today().isoformat())
    parser.add_argument("--site-name", type=str, default="Spike: multi-tile export")
    args = parser.parse_args(argv)

    spike = json.loads(args.runs_json.read_text(encoding="utf-8"))
    run_ids = sorted({int(entry["run_id"]) for entry in spike.values()})
    if not run_ids:
        print("no runs in mapping", file=sys.stderr)
        return 2

    os.environ["T2_STATIC_EXPORT"] = "1"
    os.environ["T2_STATIC_EXPORT_MULTI"] = "1"
    os.environ["T2_STATIC_EXPORT_RUN_NAME"] = args.site_name
    os.environ["T2_STATIC_EXPORT_SNAPSHOT_DATE"] = args.snapshot_date
    os.environ["T2_STATIC_EXPORT_RUN_IDS"] = ",".join(str(r) for r in run_ids)

    from . import batcher, config as _config
    from .web.app import create_app

    cfg = _config.load()
    app = create_app()
    client = app.test_client()
    _trim_dashboard_multi(set(run_ids))

    out = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "assets").mkdir()
    (out / "assets" / "siblings").mkdir(parents=True)

    # Build the full URL->path list so link rewriting knows about every page.
    pairs: list[tuple[str, str]] = list(_global_pairs())
    per_run_meta: list[dict] = []
    for rid in run_ids:
        cands = _single._candidates(rid)
        batches = batcher.list_batches(rid)
        bids = [int(b["batch_id"]) for b in batches]
        tile_id = _tile_id_for_run(cfg.data_dir, rid, spike)
        pairs.extend(_per_run_pairs(rid, cands, bids))
        if tile_id:
            pairs.append((f"/tiles/{tile_id}", f"tiles/{tile_id}/index.html"))
        per_run_meta.append({"run_id": rid, "candidates": cands, "batch_ids": bids, "tile_id": tile_id})

    url_to_path = {u: p for u, p in pairs}
    for src, dst in _single._STATIC_BUNDLES:
        url_to_path[f"/static/{src}"] = f"assets/{dst}"

    rendered = 0
    skipped_404 = 0
    t_render_start = time.monotonic()
    for url, path in pairs:
        r = client.get(url)
        if r.status_code == 404:
            skipped_404 += 1
            continue
        if r.status_code != 200:
            print(f"ERROR: {url} -> {r.status_code}", file=sys.stderr)
            return 3
        html = r.data.decode("utf-8")
        html = _single._rewrite_links(html, path, url_to_path)
        dest = out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html, encoding="utf-8")
        rendered += 1
    t_render_s = time.monotonic() - t_render_start

    # One siblings bundle per run covering the full run bbox. Detail pages
    # fetch this bundle and filter client-side to the current map viewport.
    t_sib_start = time.monotonic()
    sib_written = 0
    for meta in per_run_meta:
        rid = meta["run_id"]
        lo_lat, lo_lon, hi_lat, hi_lon = _single._run_bbox(rid)
        bb = f"{lo_lat},{lo_lon},{hi_lat},{hi_lon}"
        r = client.get(f"/runs/{rid}/siblings?bbox={bb}&focus=0")
        if r.status_code != 200:
            continue
        data = json.loads(r.data.decode("utf-8"))
        data["bbox_is_run_scope"] = True
        (out / "assets" / "siblings" / f"run{rid}.json").write_text(
            json.dumps(data, separators=(",", ":")), encoding="utf-8"
        )
        sib_written += 1
    t_sib_s = time.monotonic() - t_sib_start

    # Copy shared static bundles (CSS/JS extracted from templates).
    static_src_dir = Path(__file__).parent / "web" / "static"
    for src, dst in _single._STATIC_BUNDLES:
        shutil.copyfile(static_src_dir / src, out / "assets" / dst)

    # Raw assets — per-run osm snapshots + batch .osm files + the tile polygon JSON.
    copied: list[str] = []
    for meta in per_run_meta:
        rid = meta["run_id"]
        snap = cfg.data_dir / f"osm_current_run{rid}.json"
        if snap.exists():
            shutil.copyfile(snap, out / "assets" / f"osm_run{rid}.json")
            copied.append(f"osm_run{rid}.json")
        for bid in meta["batch_ids"]:
            src = cfg.data_dir / f"batch_{bid}.osm"
            if src.exists():
                shutil.copyfile(src, out / "assets" / f"batch_{bid}.osm")
                copied.append(f"batch_{bid}.osm")
    tiles_json = cfg.data_dir / "tiles.json"
    if tiles_json.exists():
        shutil.copyfile(tiles_json, out / "assets" / "tiles.json")
        copied.append("tiles.json")

    print(
        f"exported {len(run_ids)} runs -> {out}\n"
        f"  pages rendered: {rendered}  (skipped 404: {skipped_404})\n"
        f"  render time: {t_render_s:.1f}s\n"
        f"  siblings written: {sib_written}\n"
        f"  siblings time: {t_sib_s:.1f}s\n"
        f"  copied assets: {len(copied)} files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
