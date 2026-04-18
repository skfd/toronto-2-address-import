"""Stage 2: fetch OSM addresses in bbox via Overpass. Cached per run."""
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


def fetch(run_id: int, bbox: tuple[float, float, float, float], force: bool = False) -> tuple[Path, str]:
    """Fetch OSM elements for bbox; cached to data/osm_current_run<run>.json.

    Returns (path, sha256_hex).
    """
    _CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
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


def load_cached(run_id: int) -> list[dict]:
    path = _cache_path(run_id)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))
