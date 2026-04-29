"""Download a Geofabrik OSM extract and filter it to Toronto address features.

Canonical entry point: ``python -m t2.osm_refresh``.

Writes to ``config.osm_extract_dir`` (default ``data/osm``):

    ontario-latest.osm.pbf    raw PBF download
    toronto-addresses.json    filtered element list (shape matches Overpass `out center;`)
    meta.json                 source URL + timestamps + sha256 + element counts
    refresh.lock              PID of the running refresh (present only while running)
    refresh.log               stdout+stderr of the last refresh run

Element JSON shape matches what ``osm_fetch.py`` used to write, so downstream
``conflate.build_osm_index`` and the checks need no changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import audit, config as _config

_CHUNK = 1 << 20  # 1 MiB download chunks
_HTTP_TIMEOUT = 60


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(cfg) -> dict[str, Path]:
    d = cfg.osm_extract_dir
    return {
        "dir": d,
        "pbf": d / "ontario-latest.osm.pbf",
        "json": d / "toronto-addresses.json",
        "meta": d / "meta.json",
        "lock": d / "refresh.lock",
        "log": d / "refresh.log",
    }


def read_meta(cfg=None) -> dict | None:
    cfg = cfg or _config.load()
    p = _paths(cfg)["meta"]
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def log_path(cfg=None) -> Path:
    cfg = cfg or _config.load()
    return _paths(cfg)["log"]


def extract_dir(cfg=None) -> Path:
    cfg = cfg or _config.load()
    return _paths(cfg)["dir"]


def extract_status(cfg=None, stale_after_days: int = 14) -> str:
    """One of: 'running' | 'missing' | 'fresh' | 'stale'."""
    cfg = cfg or _config.load()
    running, _pid = is_refresh_running(cfg)
    if running:
        return "running"
    meta = read_meta(cfg)
    if not meta or not _paths(cfg)["json"].exists():
        return "missing"
    ts = meta.get("downloaded_at")
    if not ts:
        return "stale"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return "stale"
    if dt.tzinfo is None:
        # meta written by an older refresh may lack tz info; treat as UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except TypeError:
        return "stale"
    return "stale" if age_days > stale_after_days else "fresh"


def is_refresh_running(cfg=None) -> tuple[bool, int | None]:
    """Return (is_running, pid). A lock with a dead PID is treated as not running."""
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
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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


def _log(msg: str) -> None:
    print(f"[{_iso_now()}] {msg}", flush=True)


def _head(url: str) -> dict[str, str]:
    r = requests.head(url, allow_redirects=True, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return dict(r.headers)


def _download(url: str, dest: Path) -> tuple[str, int]:
    """Stream download to dest. Returns (sha256_hex, bytes_written)."""
    h = hashlib.sha256()
    total = 0
    next_log = 25 * _CHUNK  # every 25 MiB
    tmp = dest.with_suffix(dest.suffix + ".partial")
    with requests.get(url, stream=True, timeout=_HTTP_TIMEOUT) as r:
        r.raise_for_status()
        size = int(r.headers.get("Content-Length") or 0)
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                f.write(chunk)
                h.update(chunk)
                total += len(chunk)
                if total >= next_log:
                    pct = f" ({100 * total // size}%)" if size else ""
                    _log(f"downloaded {total // (1 << 20)} MiB{pct}")
                    next_log += 25 * _CHUNK
    tmp.replace(dest)
    return h.hexdigest(), total


def _in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    return bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]


def _bounds_intersect_bbox(
    bounds: dict, bbox: tuple[float, float, float, float]
) -> bool:
    """True if a bounds rectangle overlaps bbox (minlat, minlon, maxlat, maxlon)."""
    return (
        bounds["minlat"] <= bbox[2]
        and bounds["maxlat"] >= bbox[0]
        and bounds["minlon"] <= bbox[3]
        and bounds["maxlon"] >= bbox[1]
    )


def _filter(pbf_path: Path, bbox: tuple[float, float, float, float]) -> tuple[list[dict], dict[str, int]]:
    """Scan the PBF and return (elements, counts) for addr:housenumber features in bbox.

    Element shape matches Overpass `out center;` for address ways and
    `out body;` for addr:interpolation ways (which carry their member node IDs
    so downstream conflation can exclude interpolation endpoints from matching).
    Multipolygon relations with addr:housenumber are emitted with a centroid
    computed from the union bbox of their member ways — close to the exact
    outer-ring centroid for typical building/co-op relations (e.g. relation
    12272741, "100 Bain Avenue"):

      node:   {"type": "node", "id": N, "lat": L, "lon": L, "tags": {...}}
      way:    {"type": "way",  "id": N, "center": {"lat": L, "lon": L},
               "bounds": {"minlat": .., "minlon": .., "maxlat": .., "maxlon": ..},
               "nodes": [n1, n2, ...], "tags": {...}}
      interp: {"type": "way",  "id": N, "bounds": {...},
               "nodes": [n1, n2, ...], "tags": {"addr:interpolation": ...}}
      rel:    {"type": "relation", "id": N, "center": {...}, "bounds": {...}, "tags": {...}}

    Two passes: pass 1 reads only relation blocks (no location cache) to
    identify target relations and their member way IDs; pass 2 is the existing
    node+way scan, which additionally caches bounds for those member ways.
    Pass 1 is ~1s on the 935 MB Ontario PBF; pass 2 is the existing cost.
    """
    import osmium  # imported lazily so the web app doesn't pay the cost

    elements: list[dict] = []
    counts = {
        "nodes": 0,
        "ways": 0,
        "interp_ways": 0,
        "relations": 0,
        "relations_no_geometry": 0,
        "outside_bbox": 0,
    }

    # Pass 1: collect addr:housenumber multipolygon relations + member way IDs.
    target_rels: dict[int, dict] = {}
    member_way_ids: set[int] = set()

    class _RelCollector(osmium.SimpleHandler):
        def relation(self, r):
            if "addr:housenumber" not in r.tags:
                return
            if r.tags.get("type") != "multipolygon":
                # Other relation types (boundary, site, …) carry addr:* less
                # often and have ill-defined centroids — skip for now.
                return
            way_refs = [m.ref for m in r.members if m.type == "w"]
            if not way_refs:
                return
            target_rels[r.id] = {
                "tags": {t.k: t.v for t in r.tags},
                "member_ways": way_refs,
            }
            member_way_ids.update(way_refs)

    _RelCollector().apply_file(str(pbf_path))

    # Pass 2: existing node+way scan, plus bounds capture for relation members.
    way_bounds_for_rels: dict[int, dict] = {}

    class Handler(osmium.SimpleHandler):
        def node(self, n):
            if "addr:housenumber" not in n.tags:
                return
            if not n.location.valid():
                return
            lat, lon = n.location.lat, n.location.lon
            if not _in_bbox(lat, lon, bbox):
                counts["outside_bbox"] += 1
                return
            elements.append({
                "type": "node",
                "id": n.id,
                "lat": lat,
                "lon": lon,
                "tags": {t.k: t.v for t in n.tags},
            })
            counts["nodes"] += 1

        def way(self, w):
            has_hn = "addr:housenumber" in w.tags
            has_interp = "addr:interpolation" in w.tags
            is_member = w.id in member_way_ids
            if not has_hn and not has_interp and not is_member:
                return
            node_ids: list[int] = []
            lats: list[float] = []
            lons: list[float] = []
            for wn in w.nodes:
                node_ids.append(wn.ref)
                if wn.location.valid():
                    lats.append(wn.location.lat)
                    lons.append(wn.location.lon)
            if not lats:
                return
            minlat, maxlat = min(lats), max(lats)
            minlon, maxlon = min(lons), max(lons)
            bounds = {"minlat": minlat, "minlon": minlon,
                      "maxlat": maxlat, "maxlon": maxlon}
            if is_member:
                way_bounds_for_rels[w.id] = bounds
            if not has_hn and not has_interp:
                return
            # Match Overpass `way(...)(bbox)` semantics: keep the way if its
            # bounding box intersects the Toronto bbox, not just if its center
            # falls inside.
            if not _bounds_intersect_bbox(bounds, bbox):
                counts["outside_bbox"] += 1
                return
            out: dict = {
                "type": "way",
                "id": w.id,
                "bounds": bounds,
                "nodes": node_ids,
                "tags": {t.k: t.v for t in w.tags},
            }
            if has_hn:
                out["center"] = {"lat": (minlat + maxlat) / 2,
                                 "lon": (minlon + maxlon) / 2}
                counts["ways"] += 1
            else:
                counts["interp_ways"] += 1
            elements.append(out)

    Handler().apply_file(str(pbf_path), locations=True)

    # Post-pass: assemble relation entries from member-way bounds.
    for rid, info in target_rels.items():
        union: dict | None = None
        for wid in info["member_ways"]:
            wb = way_bounds_for_rels.get(wid)
            if wb is None:
                continue
            if union is None:
                union = dict(wb)
            else:
                union = {
                    "minlat": min(union["minlat"], wb["minlat"]),
                    "minlon": min(union["minlon"], wb["minlon"]),
                    "maxlat": max(union["maxlat"], wb["maxlat"]),
                    "maxlon": max(union["maxlon"], wb["maxlon"]),
                }
        if union is None:
            counts["relations_no_geometry"] += 1
            continue
        if not _bounds_intersect_bbox(union, bbox):
            counts["outside_bbox"] += 1
            continue
        elements.append({
            "type": "relation",
            "id": rid,
            "bounds": union,
            "center": {
                "lat": (union["minlat"] + union["maxlat"]) / 2,
                "lon": (union["minlon"] + union["maxlon"]) / 2,
            },
            "tags": info["tags"],
        })
        counts["relations"] += 1

    return elements, counts


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _acquire_lock(lock: Path) -> None:
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
        except Exception:
            pid = -1
        if _pid_alive(pid):
            raise RuntimeError(f"refresh already running (pid {pid}); remove {lock} if stale")
        _log(f"clearing stale lock (pid {pid} not alive)")
        lock.unlink(missing_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")


def _release_lock(lock: Path) -> None:
    try:
        lock.unlink(missing_ok=True)
    except Exception:
        pass


def run(force: bool = False, dry_run: bool = False, rebuild: bool = False) -> dict:
    """Do the refresh. Returns the meta dict written to disk (or would-be meta on dry_run).

    rebuild=True re-filters the existing PBF without re-downloading; useful
    after the filter logic changes (e.g. emitting multipolygon relations).
    """
    cfg = _config.load()
    paths = _paths(cfg)
    paths["dir"].mkdir(parents=True, exist_ok=True)

    if rebuild:
        if not paths["pbf"].exists():
            raise FileNotFoundError(
                f"--rebuild requires {paths['pbf']} to exist; run without --rebuild first."
            )
        prior = read_meta(cfg) or {}
        _acquire_lock(paths["lock"])
        t_start = time.monotonic()
        try:
            _log(f"rebuild: reusing {paths['pbf']} ({paths['pbf'].stat().st_size} bytes)")
            pbf_sha = prior.get("pbf_sha256") or _sha256_file(paths["pbf"])
            pbf_bytes = paths["pbf"].stat().st_size

            _log(f"filtering with pyosmium to bbox {cfg.osm_toronto_bbox}")
            t_filter = time.monotonic()
            elements, counts = _filter(paths["pbf"], cfg.osm_toronto_bbox)
            filter_s = time.monotonic() - t_filter
            _log(f"filter done in {filter_s:.1f}s — {counts}")

            body = json.dumps(elements)
            paths["json"].write_text(body, encoding="utf-8")
            json_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            _log(f"wrote {paths['json']} ({len(body)} bytes, sha256 {json_sha[:16]}…)")

            meta = {
                "source_url": prior.get("source_url", cfg.osm_pbf_url),
                "source_last_modified": prior.get("source_last_modified", ""),
                "source_bytes": prior.get("source_bytes", pbf_bytes),
                "pbf_sha256": pbf_sha,
                "json_sha256": json_sha,
                "json_bytes": len(body),
                "element_counts": counts,
                "toronto_bbox": list(cfg.osm_toronto_bbox),
                "downloaded_at": prior.get("downloaded_at", _iso_now()),
                "rebuilt_at": _iso_now(),
                "filter_duration_s": round(filter_s, 2),
                "total_duration_s": round(time.monotonic() - t_start, 2),
            }
            paths["meta"].write_text(json.dumps(meta, indent=2), encoding="utf-8")
            audit.log(actor="osm_refresh", event_type="OSM_EXTRACT_REFRESHED", payload=meta)
            _log("done")
            return meta
        finally:
            _release_lock(paths["lock"])

    _log(f"HEAD {cfg.osm_pbf_url}")
    headers = _head(cfg.osm_pbf_url)
    source_last_modified = headers.get("Last-Modified", "")
    content_length = int(headers.get("Content-Length") or 0)
    _log(f"source last-modified: {source_last_modified or '(unknown)'} size: {content_length} bytes")

    prior = read_meta(cfg)
    unchanged = (
        prior is not None
        and prior.get("source_last_modified") == source_last_modified
        and paths["json"].exists()
    )

    if dry_run:
        _log(f"dry-run: would_download={not unchanged or force}")
        return {
            "source_url": cfg.osm_pbf_url,
            "source_last_modified": source_last_modified,
            "source_bytes": content_length,
            "would_download": (not unchanged) or force,
            "prior": prior,
        }

    if unchanged and not force:
        _log("source unchanged since last refresh; skipping download (pass --force to override)")
        return prior or {}

    _acquire_lock(paths["lock"])
    t_start = time.monotonic()
    try:
        _log(f"downloading to {paths['pbf']}")
        pbf_sha, pbf_bytes = _download(cfg.osm_pbf_url, paths["pbf"])
        _log(f"downloaded {pbf_bytes} bytes, sha256 {pbf_sha[:16]}…")

        _log(f"filtering with pyosmium to bbox {cfg.osm_toronto_bbox}")
        t_filter = time.monotonic()
        elements, counts = _filter(paths["pbf"], cfg.osm_toronto_bbox)
        filter_s = time.monotonic() - t_filter
        _log(f"filter done in {filter_s:.1f}s — {counts}")

        body = json.dumps(elements)
        paths["json"].write_text(body, encoding="utf-8")
        json_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        _log(f"wrote {paths['json']} ({len(body)} bytes, sha256 {json_sha[:16]}…)")

        meta = {
            "source_url": cfg.osm_pbf_url,
            "source_last_modified": source_last_modified,
            "source_bytes": pbf_bytes,
            "pbf_sha256": pbf_sha,
            "json_sha256": json_sha,
            "json_bytes": len(body),
            "element_counts": counts,
            "toronto_bbox": list(cfg.osm_toronto_bbox),
            "downloaded_at": _iso_now(),
            "filter_duration_s": round(filter_s, 2),
            "total_duration_s": round(time.monotonic() - t_start, 2),
        }
        paths["meta"].write_text(json.dumps(meta, indent=2), encoding="utf-8")
        audit.log(actor="osm_refresh", event_type="OSM_EXTRACT_REFRESHED", payload=meta)
        _log("done")
        return meta
    finally:
        _release_lock(paths["lock"])


def _cli() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m t2.osm_refresh",
        description="Download + filter the Toronto OSM extract used by stage 2.",
    )
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if Geofabrik Last-Modified is unchanged.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only HEAD-check the source; print what would happen.")
    parser.add_argument("--rebuild", action="store_true",
                        help="Re-filter the existing PBF (skip download). "
                             "Use after the filter logic changes.")
    args = parser.parse_args()
    try:
        run(force=args.force, dry_run=args.dry_run, rebuild=args.rebuild)
        return 0
    except Exception as e:
        _log(f"ERROR: {e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(_cli())
