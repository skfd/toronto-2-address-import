"""Global streets analysis: source vs OSM, by normalized street name.

Buckets every active source address and every OSM addr:housenumber feature inside
the Toronto bbox by `normalize_street(addr:street)`, then partitions the streets:

  - missing: in source but absent from OSM
  - extra:   in OSM but absent from source
  - matched: present in both (with both counts)

Output: ``data/streets.json``. Re-run with ``python -m t2.streets`` after a source
DB or OSM extract refresh; the web UI exposes the same regeneration as a button.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import config as _config, osm_refresh, source_db
from .conflate import _is_poi_node, normalize_street


def _source_streets(cfg, snapshot_id: int) -> dict[str, dict]:
    counts: dict[str, int] = defaultdict(int)
    raws: dict[str, str] = {}
    for row in source_db.iter_active_addresses_in_bbox(cfg.osm_toronto_bbox, snapshot_id):
        raw = (row.get("linear_name_full") or "").strip()
        if not raw:
            continue
        norm = normalize_street(raw)
        if not norm:
            continue
        counts[norm] += 1
        raws.setdefault(norm, raw)
    return {norm: {"raw": raws[norm], "count": n} for norm, n in counts.items()}


def _osm_streets(cfg) -> dict[str, dict]:
    json_path = cfg.osm_extract_dir / "toronto-addresses.json"
    if not json_path.exists():
        raise FileNotFoundError(
            f"OSM extract missing at {json_path}; run python -m t2.osm_refresh first."
        )
    elements = json.loads(json_path.read_text(encoding="utf-8"))

    interp_node_ids: set[int] = set()
    for el in elements:
        if el.get("type") != "way":
            continue
        if "addr:interpolation" not in (el.get("tags") or {}):
            continue
        for nid in el.get("nodes") or ():
            interp_node_ids.add(nid)

    counts: dict[str, int] = defaultdict(int)
    raws: dict[str, str] = {}
    for el in elements:
        tags = el.get("tags") or {}
        if "addr:housenumber" not in tags:
            continue
        if el.get("type") == "node":
            if el.get("id") in interp_node_ids:
                continue
            if _is_poi_node(el):
                continue
        raw = (tags.get("addr:street") or "").strip()
        if not raw:
            continue
        norm = normalize_street(raw)
        if not norm:
            continue
        counts[norm] += 1
        raws.setdefault(norm, raw)
    return {norm: {"raw": raws[norm], "count": n} for norm, n in counts.items()}


def compute(cfg=None) -> dict:
    cfg = cfg or _config.load()
    snap = source_db.latest_snapshot_info()
    if not snap:
        raise RuntimeError("Source DB has no snapshot.")
    snapshot_id = snap["id"]

    source = _source_streets(cfg, snapshot_id)
    osm = _osm_streets(cfg)

    missing = [
        {"street_norm": k, "street_raw": v["raw"], "source_count": v["count"]}
        for k, v in source.items() if k not in osm
    ]
    extra = [
        {"street_norm": k, "street_raw": v["raw"], "osm_count": v["count"]}
        for k, v in osm.items() if k not in source
    ]
    matched = [
        {
            "street_norm": k,
            "street_raw": v["raw"],
            "osm_raw": osm[k]["raw"],
            "source_count": v["count"],
            "osm_count": osm[k]["count"],
        }
        for k, v in source.items() if k in osm
    ]

    missing.sort(key=lambda r: (-r["source_count"], r["street_norm"]))
    extra.sort(key=lambda r: (-r["osm_count"], r["street_norm"]))
    matched.sort(key=lambda r: (-(r["source_count"] + r["osm_count"]), r["street_norm"]))

    osm_meta = osm_refresh.read_meta(cfg) or {}

    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "source_snapshot_id": snapshot_id,
        "source_snapshot_downloaded": snap.get("downloaded"),
        "osm_extract_downloaded": osm_meta.get("downloaded_at"),
        "osm_extract_json_sha256": osm_meta.get("json_sha256"),
        "toronto_bbox": list(cfg.osm_toronto_bbox),
        "totals": {
            "source_streets": len(source),
            "osm_streets": len(osm),
            "missing": len(missing),
            "extra": len(extra),
            "matched": len(matched),
        },
        "missing": missing,
        "extra": extra,
        "matched": matched,
    }


def output_path(cfg=None) -> Path:
    cfg = cfg or _config.load()
    return cfg.data_dir / "streets.json"


def read(cfg=None) -> dict | None:
    p = output_path(cfg)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def regenerate(cfg=None) -> dict:
    cfg = cfg or _config.load()
    result = compute(cfg)
    p = output_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _cli() -> int:
    argparse.ArgumentParser(
        prog="python -m t2.streets",
        description="Compute the global streets analysis (source vs OSM).",
    ).parse_args()
    try:
        result = regenerate()
    except Exception as e:
        print(f"ERROR: {e!r}", file=sys.stderr)
        return 1
    t = result["totals"]
    print(
        f"missing={t['missing']} extra={t['extra']} matched={t['matched']} "
        f"(source_streets={t['source_streets']} osm_streets={t['osm_streets']})"
    )
    print(f"wrote {output_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
