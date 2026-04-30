"""Static HTML export of one completed pipeline run for GitHub Pages.

Renders every reviewer-facing page of a single run via Flask's test client,
rewrites internal links to relative `.html` paths, pre-fetches the sibling
JSON each Leaflet map needs, and copies the JOSM `.osm` + OSM snapshot JSON
alongside. Output tree is self-contained under `--out` and safe to serve
from GitHub Pages.

Usage:

    python -m t2.static_export --run 15 --out docs/pilot

Requires env var `T2_STATIC_EXPORT=1` to be set *before* Flask boots so the
Jinja `static_export` global picks it up — this module sets it itself.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit


def _lookup_run_name(run_id: int) -> str | None:
    from . import db as _db
    conn = _db.connect()
    try:
        row = conn.execute("SELECT name FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return row["name"] if row else None
    finally:
        conn.close()


def _candidates(run_id: int) -> list[dict]:
    from . import db as _db
    conn = _db.connect()
    try:
        rows = conn.execute(
            "SELECT candidate_id, lat, lon, stage, lo_num, hi_num "
            "FROM candidates WHERE run_id=? ORDER BY candidate_id",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _is_range(c: dict) -> bool:
    return (
        c.get("stage") == "SKIPPED"
        and c.get("lo_num") is not None
        and c.get("hi_num") is not None
        and c.get("lo_num") != c.get("hi_num")
    )


def _run_bbox(run_id: int) -> tuple[float, float, float, float]:
    from . import db as _db
    conn = _db.connect()
    try:
        row = conn.execute(
            "SELECT bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return (row["bbox_min_lat"], row["bbox_min_lon"], row["bbox_max_lat"], row["bbox_max_lon"])
    finally:
        conn.close()


def _pilot_tile_id(data_dir: Path, bbox: tuple[float, float, float, float]) -> str | None:
    import json
    tp = data_dir / "tiles.json"
    if not tp.exists():
        return None
    data = json.loads(tp.read_text(encoding="utf-8"))
    target = tuple(round(x, 6) for x in bbox)
    for t in data.get("tiles", []):
        if tuple(round(x, 6) for x in t["bbox"]) == target:
            return t["id"]
    return None


def _output_paths(run_id: int, candidates: list[dict], batch_ids: list[int], tile_id: str | None) -> list[tuple[str, str]]:
    """Return list of (source_url, output_path_relative_to_out) to render."""
    pairs: list[tuple[str, str]] = [
        ("/", "index.html"),
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
        ("/data", "data/index.html"),
        ("/osm", "osm/index.html"),
    ]
    if tile_id:
        pairs.append((f"/tiles/{tile_id}", f"tiles/{tile_id}/index.html"))
    for bid in batch_ids:
        pairs.append((f"/batches/{bid}", f"batches/{bid}/index.html"))
    for c in candidates:
        cid = c["candidate_id"]
        # Auto-SKIPPED candidates have no open decision; their /review/<cid>
        # detail is redundant with the /skipped list. Skip emission.
        if c.get("stage") != "SKIPPED":
            pairs.append((f"/runs/{run_id}/review/{cid}", f"runs/{run_id}/review/{cid}/index.html"))
        if _is_range(c):
            pairs.append((f"/runs/{run_id}/ranges/{cid}", f"runs/{run_id}/ranges/{cid}/index.html"))
    return pairs


_ATTR_RE = re.compile(r'''(\b(?:href|action|hx-get|hx-post|src)\s*=\s*)(["'])([^"']*)\2''')
_SIBLINGS_FETCH_RE = re.compile(
    r"fetch\(`/runs/\$\{runId\}/siblings\?[^`]*`,\s*\{signal: el\._sibFetch\.signal\}\)"
)
_DETAIL_PATH_RE = re.compile(r"runs/(?P<rid>\d+)/(?P<view>review|approved|skipped|ranges)/(?P<cid>\d+)/index\.html$")
# Detail pages for approved/skipped views are merged into review/ on the
# static site (Lever 1). Remap those URLs before path lookup so list pages
# and permalinks still resolve.
_VIEW_ALIAS_RE = re.compile(r"^/runs/(\d+)/(approved|skipped)/(\d+)$")

# Static bundles (relative to t2/web/static/) copied into <out>/assets/.
_STATIC_BUNDLES: tuple[tuple[str, str], ...] = (
    ("site.css", "site.css"),
    ("detail-map.js", "detail-map.js"),
    ("review-keys.js", "review-keys.js"),
)

# Popup links inside the Leaflet map JS. These are built at runtime with JS
# template literals / string concatenation, so the attribute rewriter can't
# touch them — replace the absolute `/runs/<id>/<view>/<sibId>` shape with a
# relative one that resolves under the exported tree.
_JS_SIB_CONCAT_RE = re.compile(
    r"""/runs/'\s*\+\s*runId\s*\+\s*'/'\s*\+\s*view\s*\+\s*'/'\s*\+\s*sibId\s*\+\s*'"""
)
_JS_VIEW_TEMPLATE_RE = re.compile(
    r"`(?P<pre>[^`]*?)/runs/\$\{runId\}/\$\{view\}/\$\{c\.candidate_id\}(?P<post>[^`]*?)`"
)
_JS_RANGES_TEMPLATE_RE = re.compile(
    r"`(?P<pre>[^`]*?)/runs/\$\{runId\}/review/\$\{c\.candidate_id\}(?P<post>[^`]*?)`"
)


def _rel_from(here_dir: Path, target: Path) -> str:
    rel = os.path.relpath(target, here_dir)
    return rel.replace(os.sep, "/")


def _rewrite_links(html: str, output_path: str, url_to_path: dict[str, str]) -> str:
    """Rewrite href/action/hx-* attributes from absolute paths to output-tree relatives."""
    here = Path(output_path).parent

    def _sub(m: re.Match) -> str:
        prefix, quote, url = m.group(1), m.group(2), m.group(3)
        if not url or url.startswith(("#", "http://", "https://", "mailto:", "javascript:")):
            return m.group(0)
        if not url.startswith("/"):
            return m.group(0)
        split = urlsplit(url)
        path = split.path
        alias = _VIEW_ALIAS_RE.match(path)
        if alias:
            path = f"/runs/{alias.group(1)}/review/{alias.group(3)}"
        keyed = path + (f"?{split.query}" if split.query else "")
        target = url_to_path.get(keyed) or url_to_path.get(path)
        if target is None:
            # Unknown URL — leave untouched. Dead link on the static site but not fatal.
            return m.group(0)
        rel = _rel_from(here, Path(target))
        if split.fragment:
            rel = f"{rel}#{split.fragment}"
        return f"{prefix}{quote}{rel}{quote}"

    html = _ATTR_RE.sub(_sub, html)

    # Rewrite the Leaflet siblings fetch to point at the pre-baked JSON for
    # this candidate, if the current page is a detail page. The landmark
    # comment `// t2-static-export:siblings-fetch` sits one line above.
    #
    # The fetch URL is derived from location.pathname at runtime rather than
    # baked as a relative path. The detail HTML is often swapped into the
    # list page via htmx (where the browser URL stays on `.../review/` and
    # the directory is one level shallower than the detail page's own
    # directory). A static `../../../../` would walk out of the site root
    # when resolved against the list-page URL; the runtime form works from
    # either URL because it strips everything from `/runs/` onward and
    # re-anchors to `<deploy-root>/assets/siblings/run<rid>.json`.
    dm = _DETAIL_PATH_RE.search(output_path)
    if dm:
        rid = dm.group("rid")
        view = dm.group("view")
        sib_url_expr = (
            f"location.pathname.replace(/\\/runs\\/.*$/, '') + '/assets/siblings/run{rid}.json'"
        )
        html = _SIBLINGS_FETCH_RE.sub(
            f"fetch({sib_url_expr}, {{signal: el._sibFetch.signal}})",
            html,
        )
        # Popup "Open sibling" / "Open candidate" links (JS-built).
        html = _JS_SIB_CONCAT_RE.sub(r"../' + sibId + '/", html)
        html = _JS_VIEW_TEMPLATE_RE.sub(
            r"`\g<pre>../${c.candidate_id}/\g<post>`",
            html,
        )
        # Ranges detail page links to review (different view), so emit the
        # cross-view relative path.
        if view == "ranges":
            html = _JS_RANGES_TEMPLATE_RE.sub(
                r"`\g<pre>../../review/${c.candidate_id}/\g<post>`",
                html,
            )

    return html


def _trim_dashboard(run_id: int):
    """Monkeypatch pipeline.list_runs to return only the pilot run so the
    exported dashboard doesn't advertise other runs present in the DB."""
    from . import pipeline

    original = pipeline.list_runs

    def only_pilot():
        return [r for r in original() if int(r["run_id"]) == run_id]

    pipeline.list_runs = only_pilot  # type: ignore[assignment]
    return original


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="t2.static_export")
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--out", type=Path, default=Path("docs/pilot"))
    parser.add_argument("--snapshot-date", type=str, default=date.today().isoformat())
    args = parser.parse_args(argv)

    run_name = _lookup_run_name(args.run)
    if not run_name:
        print(f"ERROR: run {args.run} not found", file=sys.stderr)
        return 2

    os.environ["T2_STATIC_EXPORT"] = "1"
    os.environ["T2_STATIC_EXPORT_RUN_NAME"] = run_name
    os.environ["T2_STATIC_EXPORT_SNAPSHOT_DATE"] = args.snapshot_date
    os.environ["T2_STATIC_EXPORT_RUN_ID"] = str(args.run)

    # Import after env is set so the Jinja global picks up the flag.
    from . import batcher, config as _config, upload_manifest
    from .web.app import create_app

    cfg = _config.load()
    app = create_app()
    client = app.test_client()
    _trim_dashboard(args.run)

    out = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "assets").mkdir()
    (out / "assets" / "siblings").mkdir()

    bbox = _run_bbox(args.run)
    candidates = _candidates(args.run)
    batches = batcher.list_batches(args.run)
    batch_ids = [int(b["batch_id"]) for b in batches]
    tile_id = _pilot_tile_id(cfg.data_dir, bbox)

    pairs = _output_paths(args.run, candidates, batch_ids, tile_id)
    url_to_path = {u: p for u, p in pairs}
    # Static bundles (copied, not rendered). Seed into url_to_path so
    # /static/<file> links resolve to the exported asset path.
    for src, dst in _STATIC_BUNDLES:
        url_to_path[f"/static/{src}"] = f"assets/{dst}"

    rendered = 0
    skipped_404 = 0
    for url, path in pairs:
        r = client.get(url)
        if r.status_code == 404:
            # Per-candidate category pages may not exist (e.g. /approved/<id>
            # for a candidate whose stage isn't APPROVED). Skip quietly.
            skipped_404 += 1
            continue
        if r.status_code != 200:
            print(f"ERROR: {url} -> {r.status_code}", file=sys.stderr)
            return 3
        html = r.data.decode("utf-8")
        html = _rewrite_links(html, path, url_to_path)
        dest = out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html, encoding="utf-8")
        rendered += 1

    # One siblings bundle per run covering the full run bbox. Detail pages
    # fetch this bundle and filter client-side to the current map viewport.
    import json as _json
    sib_written = 0
    lo_lat, lo_lon, hi_lat, hi_lon = bbox
    bb = f"{lo_lat},{lo_lon},{hi_lat},{hi_lon}"
    r = client.get(f"/runs/{args.run}/siblings?bbox={bb}&focus=0")
    if r.status_code == 200:
        data = _json.loads(r.data.decode("utf-8"))
        data["bbox_is_run_scope"] = True
        (out / "assets" / "siblings" / f"run{args.run}.json").write_text(
            _json.dumps(data, separators=(",", ":")), encoding="utf-8"
        )
        sib_written = 1

    # Copy shared static bundles (CSS/JS extracted from templates).
    static_src_dir = Path(__file__).parent / "web" / "static"
    for src, dst in _STATIC_BUNDLES:
        shutil.copyfile(static_src_dir / src, out / "assets" / dst)

    # Copy raw assets. These aren't strictly required by any page that's
    # rendered (siblings JSON already covers the Leaflet maps), but they let
    # reviewers inspect the conflation inputs directly.
    copied = []
    osm_snap = cfg.data_dir / f"osm_current_run{args.run}.json"
    if osm_snap.exists():
        shutil.copyfile(osm_snap, out / "assets" / "osm.json")
        copied.append("osm.json")
    for bid in batch_ids:
        src = cfg.data_dir / f"batch_{bid}.osm"
        if src.exists():
            shutil.copyfile(src, out / "assets" / f"batch_{bid}.osm")
            copied.append(f"batch_{bid}.osm")
    tiles_json = cfg.data_dir / "tiles.json"
    if tiles_json.exists():
        shutil.copyfile(tiles_json, out / "assets" / "tiles.json")
        copied.append("tiles.json")

    # Public upload manifest — one CSV per tile (or per run when no tile id).
    manifest_rows = upload_manifest.fetch_for_run(args.run)
    manifest_label = tile_id or f"run{args.run}"
    upload_manifest.write_csv(out / "uploads" / f"{manifest_label}.csv", manifest_rows)
    copied.append(f"uploads/{manifest_label}.csv ({len(manifest_rows)} rows)")

    print(
        f"exported run={args.run} ({run_name}) -> {out}\n"
        f"  pages rendered: {rendered}\n"
        f"  pages skipped (404): {skipped_404}\n"
        f"  sibling JSON files: {sib_written}\n"
        f"  copied assets: {', '.join(copied) or '(none)'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
