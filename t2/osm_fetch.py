"""Stage 2: fetch OSM addresses for a run's bbox. Cached per run.

Source is selected by ``config.osm.source``:

* ``local``    — clip the shared extract at ``config.osm_extract_dir/toronto-addresses.json``
                 (refreshed via ``python -m t2.osm_refresh``) to the run bbox.
                 Fast, offline, reproducible.
* ``overpass`` — POST an Overpass query for the run bbox. Network-dependent;
                 kept as a fallback for bbox experiments outside Toronto.

Both paths write the result to ``data/osm_current_run<id>.json`` so downstream
``osm_fetch.load_cached(run_id)`` and ``conflate.build_osm_index`` are unchanged.
"""
import hashlib
import json
from pathlib import Path

import requests

from . import config as _config

_CONFIG = _config.load()


def _build_query(bbox: tuple[float, float, float, float], check_count: bool = False) -> str:
    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    timeout = 180 if not check_count else 30
    q = f"[out:json][timeout:{timeout}];("
    q += f'node["addr:housenumber"]({bbox_str});'
    q += f'way["addr:housenumber"]({bbox_str});'
    q += f'relation["addr:housenumber"]({bbox_str});'
    q += ");"
    q += "out count;" if check_count else "out center;"
    return q


def _cache_path(run_id: int) -> Path:
    return _CONFIG.data_dir / f"osm_current_run{run_id}.json"


def _element_latlon(el: dict) -> tuple[float | None, float | None]:
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    c = el.get("center") or {}
    return c.get("lat"), c.get("lon")


def _in_bbox(lat: float | None, lon: float | None, bbox: tuple[float, float, float, float]) -> bool:
    if lat is None or lon is None:
        return False
    return bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]


def _fetch_from_local(
    run_id: int, bbox: tuple[float, float, float, float], force: bool
) -> tuple[Path, str]:
    path = _cache_path(run_id)
    if path.exists() and not force:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return path, digest

    shared = _CONFIG.osm_extract_dir / "toronto-addresses.json"
    if not shared.exists():
        raise FileNotFoundError(
            f"Local OSM extract not found at {shared}. "
            "Run `python -m t2.osm_refresh` (or click Refresh at /osm) first."
        )
    all_elements = json.loads(shared.read_text(encoding="utf-8"))
    clipped = [el for el in all_elements if _in_bbox(*_element_latlon(el), bbox)]
    body = json.dumps(clipped)
    path.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return path, digest


def _fetch_from_overpass(
    run_id: int, bbox: tuple[float, float, float, float], force: bool
) -> tuple[Path, str]:
    path = _cache_path(run_id)
    if path.exists() and not force:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return path, digest

    query = _build_query(bbox)
    resp = requests.post(_CONFIG.overpass_url, data={"data": query}, timeout=200)
    resp.raise_for_status()
    data = resp.json()
    elements = data.get("elements", [])
    body = json.dumps(elements)
    path.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return path, digest


def fetch(run_id: int, bbox: tuple[float, float, float, float], force: bool = False) -> tuple[Path, str]:
    """Fetch OSM elements for bbox; cached to data/osm_current_run<run>.json.

    Returns (path, sha256_hex). Dispatches on ``config.osm.source``.
    """
    _CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
    if _CONFIG.osm_source == "local":
        return _fetch_from_local(run_id, bbox, force)
    return _fetch_from_overpass(run_id, bbox, force)


def load_cached(run_id: int) -> list[dict]:
    path = _cache_path(run_id)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))
