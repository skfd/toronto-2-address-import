"""Build Toronto tile layer from the Neighbourhoods GeoJSON + source addresses.

Canonical entry point: ``python -m t2.tiles_build``.

Fetches the City of Toronto's official 158-neighbourhood polygon layer,
counts active source addresses inside each polygon, and quadtree-splits any
neighbourhood with more than ``SPLIT_THRESHOLD`` addresses so each final tile
is a manageable picking unit for a run.

Writes to ``cfg.data_dir``:

    neighbourhoods/neighbourhoods-4326.geojson    raw GeoJSON (WGS84)
    neighbourhoods/meta.json                      download sidecar
    tiles.json                                    the tile layer (read by the web app)
    tiles/meta.json                               build sidecar (counts, duration, orphans)
    tiles/build.lock                              PID of the running build
    tiles/build.log                               stdout+stderr of last build
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import requests
from shapely.geometry import MultiPolygon, Point, Polygon, box, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from . import audit, config as _config, source_db

NEIGHBOURHOODS_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "fc443770-ef0a-4025-9c2c-2cb558bfab00/resource/"
    "0719053b-28b7-48ea-b863-068823a93aaa/download/neighbourhoods-4326.geojson"
)

SPLIT_THRESHOLD = 500
# ~330 m at Toronto latitude. Below this, stop subdividing even if count > threshold —
# prevents runaway recursion on high-rise clusters where all addresses share a point.
MIN_SPAN_DEG = 0.003
# Orphan ratio at or above this triggers an "Unassigned" catch-all bucket.
ORPHAN_BUCKET_PCT = 0.01
SCHEMA_VERSION = 1

_CHUNK = 1 << 20
_HTTP_TIMEOUT = 60


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(cfg) -> dict[str, Path]:
    data = cfg.data_dir
    tiles_dir = data / "tiles"
    hood_dir = data / "neighbourhoods"
    return {
        "data_dir": data,
        "tiles_dir": tiles_dir,
        "hood_dir": hood_dir,
        "geojson": hood_dir / "neighbourhoods-4326.geojson",
        "geojson_meta": hood_dir / "meta.json",
        "tiles_json": data / "tiles.json",
        "tiles_meta": tiles_dir / "meta.json",
        "lock": tiles_dir / "build.lock",
        "log": tiles_dir / "build.log",
    }


def read_meta(cfg=None) -> dict | None:
    cfg = cfg or _config.load()
    p = _paths(cfg)["tiles_meta"]
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def log_path(cfg=None) -> Path:
    cfg = cfg or _config.load()
    return _paths(cfg)["log"]


def tail_log(cfg=None, lines: int = 40) -> str:
    cfg = cfg or _config.load()
    p = _paths(cfg)["log"]
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_build_running(cfg=None) -> tuple[bool, int | None]:
    cfg = cfg or _config.load()
    lock = _paths(cfg)["lock"]
    if not lock.exists():
        return False, None
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except Exception:
        return False, None
    if _pid_alive(pid):
        return True, pid
    return False, pid


def _acquire_lock(lock: Path) -> None:
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
        except Exception:
            pid = -1
        if _pid_alive(pid):
            raise RuntimeError(
                f"build already running (pid {pid}); remove {lock} if stale"
            )
        _log(f"clearing stale lock (pid {pid} not alive)")
        lock.unlink(missing_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")


def _release_lock(lock: Path) -> None:
    try:
        lock.unlink(missing_ok=True)
    except Exception:
        pass


def _log(msg: str) -> None:
    print(f"[{_iso_now()}] {msg}", flush=True)


def _head(url: str) -> dict[str, str]:
    r = requests.head(url, allow_redirects=True, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return dict(r.headers)


def _download(url: str, dest: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    tmp = dest.with_suffix(dest.suffix + ".partial")
    with requests.get(url, stream=True, timeout=_HTTP_TIMEOUT) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                f.write(chunk)
                h.update(chunk)
                total += len(chunk)
    tmp.replace(dest)
    return h.hexdigest(), total


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "tile"


def _polygon_latlon(poly: Polygon) -> list[list[list[float]]]:
    """Return Leaflet-style rings: [[[lat, lon], ...]] (exterior ring only)."""
    coords = [[round(y, 6), round(x, 6)] for x, y in poly.exterior.coords]
    return [coords]


def _iter_polygons(geom) -> Iterator[Polygon]:
    """Yield Polygon pieces from any geometry, ignoring non-polygonal parts."""
    if geom is None or geom.is_empty:
        return
    if isinstance(geom, Polygon):
        yield geom
        return
    if isinstance(geom, MultiPolygon):
        for g in geom.geoms:
            yield from _iter_polygons(g)
        return
    geoms = getattr(geom, "geoms", None)
    if geoms is None:
        return
    for g in geoms:
        yield from _iter_polygons(g)


def _bounds_bbox(poly: Polygon) -> list[float]:
    minx, miny, maxx, maxy = poly.bounds
    return [round(miny, 6), round(minx, 6), round(maxy, 6), round(maxx, 6)]


def _count_inside(poly: Polygon, points: list[Point], tree: STRtree) -> int:
    idxs = tree.query(poly)
    count = 0
    for i in idxs:
        if poly.contains(points[int(i)]):
            count += 1
    return count


def _make_tile(
    *,
    name: str,
    parent: str,
    polygon: Polygon,
    count: int,
    depth: int,
    is_multipolygon: bool,
    is_orphan: bool,
    used_ids: set[str],
) -> dict:
    base_id = _slugify(name)
    tile_id = base_id
    i = 2
    while tile_id in used_ids:
        tile_id = f"{base_id}-{i}"
        i += 1
    used_ids.add(tile_id)
    return {
        "id": tile_id,
        "name": name,
        "parent": parent,
        "depth": depth,
        "address_count": count,
        "bbox": _bounds_bbox(polygon),
        "polygon_latlon": _polygon_latlon(polygon),
        "is_multipolygon": is_multipolygon,
        "is_orphan": is_orphan,
    }


def _split_tile(
    *,
    name: str,
    parent: str,
    polygon: Polygon,
    points: list[Point],
    tree: STRtree,
    depth: int,
    threshold: int,
    min_span: float,
    is_multipolygon: bool,
    is_orphan: bool,
    used_ids: set[str],
) -> Iterator[dict]:
    count = _count_inside(polygon, points, tree)
    if count == 0:
        return
    minx, miny, maxx, maxy = polygon.bounds
    at_floor = (maxx - minx) < min_span and (maxy - miny) < min_span
    if count <= threshold or at_floor:
        yield _make_tile(
            name=name,
            parent=parent,
            polygon=polygon,
            count=count,
            depth=depth,
            is_multipolygon=is_multipolygon,
            is_orphan=is_orphan,
            used_ids=used_ids,
        )
        return
    cx = (minx + maxx) / 2
    cy = (miny + maxy) / 2
    quadrants = [
        ("SW", box(minx, miny, cx, cy)),
        ("SE", box(cx, miny, maxx, cy)),
        ("NW", box(minx, cy, cx, maxy)),
        ("NE", box(cx, cy, maxx, maxy)),
    ]
    for qname, qbox in quadrants:
        child_geom = polygon.intersection(qbox)
        pieces = list(_iter_polygons(child_geom))
        if not pieces:
            continue
        if len(pieces) == 1:
            yield from _split_tile(
                name=f"{name}-{qname}",
                parent=parent,
                polygon=pieces[0],
                points=points,
                tree=tree,
                depth=depth + 1,
                threshold=threshold,
                min_span=min_span,
                is_multipolygon=is_multipolygon,
                is_orphan=is_orphan,
                used_ids=used_ids,
            )
        else:
            for k, piece in enumerate(pieces, start=1):
                yield from _split_tile(
                    name=f"{name}-{qname}-{k}",
                    parent=parent,
                    polygon=piece,
                    points=points,
                    tree=tree,
                    depth=depth + 1,
                    threshold=threshold,
                    min_span=min_span,
                    is_multipolygon=is_multipolygon,
                    is_orphan=is_orphan,
                    used_ids=used_ids,
                )


def _feature_name(props: dict) -> str:
    for key in ("AREA_NAME", "area_name", "NEIGHBOURHOOD_NAME", "Neighbourhood", "name"):
        v = props.get(key)
        if v:
            return str(v).strip()
    aid = props.get("AREA_ID") or props.get("_id") or "?"
    return f"neighbourhood-{aid}"


def load_addresses(snap_id: int) -> list[tuple[float, float]]:
    """Return address points as (lon, lat) — shapely's Point takes x,y = lon,lat."""
    conn = source_db.connect_readonly()
    try:
        rows = conn.execute(
            "SELECT latitude, longitude FROM addresses "
            "WHERE max_snapshot_id=? AND latitude IS NOT NULL AND longitude IS NOT NULL",
            (snap_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(float(r["longitude"]), float(r["latitude"])) for r in rows]


def build_tiles(
    features: list[dict],
    points_xy: list[tuple[float, float]],
    city_bbox: tuple[float, float, float, float],
    *,
    threshold: int = SPLIT_THRESHOLD,
    min_span: float = MIN_SPAN_DEG,
) -> tuple[list[dict], dict]:
    points = [Point(x, y) for x, y in points_xy]
    tree = STRtree(points)

    used_ids: set[str] = set()
    tiles: list[dict] = []
    skipped_empty: list[str] = []
    hood_geoms: list = []

    for feat in features:
        props = feat.get("properties") or {}
        name = _feature_name(props)
        geom = shape(feat["geometry"])
        hood_geoms.append(geom)
        pieces = list(_iter_polygons(geom))
        if not pieces:
            continue
        is_multi = len(pieces) > 1
        yielded_any = False
        if not is_multi:
            for t in _split_tile(
                name=name,
                parent=name,
                polygon=pieces[0],
                points=points,
                tree=tree,
                depth=0,
                threshold=threshold,
                min_span=min_span,
                is_multipolygon=False,
                is_orphan=False,
                used_ids=used_ids,
            ):
                tiles.append(t)
                yielded_any = True
        else:
            for k, piece in enumerate(pieces, start=1):
                piece_name = f"{name}-{k}"
                for t in _split_tile(
                    name=piece_name,
                    parent=name,
                    polygon=piece,
                    points=points,
                    tree=tree,
                    depth=0,
                    threshold=threshold,
                    min_span=min_span,
                    is_multipolygon=True,
                    is_orphan=False,
                    used_ids=used_ids,
                ):
                    tiles.append(t)
                    yielded_any = True
        if not yielded_any:
            skipped_empty.append(name)

    total = len(points)
    assigned = sum(t["address_count"] for t in tiles)
    orphan_count = total - assigned
    orphan_pct = (orphan_count / total) if total else 0.0

    if orphan_count > 0 and orphan_pct >= ORPHAN_BUCKET_PCT:
        _log(
            f"orphan_pct {orphan_pct:.2%} >= {ORPHAN_BUCKET_PCT:.0%}, "
            f"bucketing {orphan_count} orphans into 'Unassigned'"
        )
        union = unary_union(hood_geoms)
        min_lat, min_lon, max_lat, max_lon = city_bbox
        city_rect = box(min_lon, min_lat, max_lon, max_lat)
        leftover = city_rect.difference(union)
        pieces = list(_iter_polygons(leftover))
        for k, piece in enumerate(pieces, start=1):
            piece_name = f"Unassigned-{k}" if len(pieces) > 1 else "Unassigned"
            for t in _split_tile(
                name=piece_name,
                parent="Unassigned",
                polygon=piece,
                points=points,
                tree=tree,
                depth=0,
                threshold=threshold,
                min_span=min_span,
                is_multipolygon=len(pieces) > 1,
                is_orphan=True,
                used_ids=used_ids,
            ):
                tiles.append(t)

    assigned_after = sum(t["address_count"] for t in tiles)
    stats = {
        "total_addresses": total,
        "assigned_after": assigned_after,
        "orphan_count": total - assigned_after,
        "orphan_pct": (total - assigned_after) / total if total else 0.0,
        "skipped_empty": skipped_empty,
        "tile_count": len(tiles),
    }
    return tiles, stats


def run(force: bool = False, dry_run: bool = False) -> dict:
    cfg = _config.load()
    paths = _paths(cfg)
    paths["tiles_dir"].mkdir(parents=True, exist_ok=True)
    paths["hood_dir"].mkdir(parents=True, exist_ok=True)

    _log(f"HEAD {NEIGHBOURHOODS_URL}")
    headers = _head(NEIGHBOURHOODS_URL)
    source_last_modified = headers.get("Last-Modified", "")
    content_length = int(headers.get("Content-Length") or 0)
    _log(
        f"source last-modified: {source_last_modified or '(unknown)'} "
        f"size: {content_length} bytes"
    )

    prior_meta: dict | None = None
    if paths["geojson_meta"].exists():
        try:
            prior_meta = json.loads(paths["geojson_meta"].read_text(encoding="utf-8"))
        except Exception:
            prior_meta = None
    unchanged = (
        prior_meta is not None
        and prior_meta.get("source_last_modified") == source_last_modified
        and paths["geojson"].exists()
    )

    if dry_run:
        _log(f"dry-run: would_download={(not unchanged) or force}")
        return {
            "source_url": NEIGHBOURHOODS_URL,
            "source_last_modified": source_last_modified,
            "source_bytes": content_length,
            "would_download": (not unchanged) or force,
        }

    _acquire_lock(paths["lock"])
    t_start = time.monotonic()
    try:
        if unchanged and not force:
            _log("source unchanged since last build; reusing existing geojson")
            geojson_sha = (prior_meta or {}).get("sha256") or _sha256_file(paths["geojson"])
        else:
            _log(f"downloading to {paths['geojson']}")
            geojson_sha, bytes_written = _download(NEIGHBOURHOODS_URL, paths["geojson"])
            _log(f"downloaded {bytes_written} bytes, sha256 {geojson_sha[:16]}…")
            paths["geojson_meta"].write_text(
                json.dumps(
                    {
                        "source_url": NEIGHBOURHOODS_URL,
                        "source_last_modified": source_last_modified,
                        "bytes": bytes_written,
                        "sha256": geojson_sha,
                        "downloaded_at": _iso_now(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

        _log("loading addresses from source DB")
        snap_id = source_db.latest_snapshot_id()
        points_xy = load_addresses(snap_id)
        _log(f"loaded {len(points_xy)} address points at snapshot {snap_id}")

        geojson_data = json.loads(paths["geojson"].read_text(encoding="utf-8"))
        features = geojson_data.get("features", [])
        _log(f"loaded {len(features)} neighbourhood features")

        t_build = time.monotonic()
        tiles, stats = build_tiles(features, points_xy, cfg.osm_toronto_bbox)
        build_s = time.monotonic() - t_build
        _log(
            f"built {stats['tile_count']} tiles in {build_s:.1f}s; "
            f"orphans={stats['orphan_count']} ({stats['orphan_pct']:.2%}); "
            f"skipped_empty={len(stats['skipped_empty'])}"
        )

        out = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso_now(),
            "source_snapshot_id": snap_id,
            "neighbourhoods_sha256": geojson_sha,
            "threshold": SPLIT_THRESHOLD,
            "tiles": tiles,
        }
        body = json.dumps(out)
        tmp = paths["tiles_json"].with_suffix(paths["tiles_json"].suffix + ".partial")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(paths["tiles_json"])
        _log(f"wrote {paths['tiles_json']} ({len(body)} bytes)")

        meta_out = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": out["generated_at"],
            "source_snapshot_id": snap_id,
            "neighbourhoods_sha256": geojson_sha,
            "threshold": SPLIT_THRESHOLD,
            "tile_count": stats["tile_count"],
            "total_addresses": stats["total_addresses"],
            "assigned_after": stats["assigned_after"],
            "orphan_count": stats["orphan_count"],
            "orphan_pct": round(stats["orphan_pct"], 6),
            "skipped_empty": stats["skipped_empty"],
            "build_duration_s": round(build_s, 2),
            "total_duration_s": round(time.monotonic() - t_start, 2),
        }
        paths["tiles_meta"].write_text(json.dumps(meta_out, indent=2), encoding="utf-8")
        audit.log(actor="tiles_build", event_type="TILES_REBUILT", payload=meta_out)
        _log("done")
        return meta_out
    finally:
        _release_lock(paths["lock"])


def _cli() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m t2.tiles_build",
        description="Download Toronto neighbourhoods, quadtree-split to ≤500 addrs/tile, write data/tiles.json.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-download the neighbourhoods GeoJSON even if unchanged.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only HEAD-check the source; print what would happen.",
    )
    args = parser.parse_args()
    try:
        run(force=args.force, dry_run=args.dry_run)
        return 0
    except Exception as e:
        _log(f"ERROR: {e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(_cli())
