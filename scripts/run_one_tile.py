"""Run the full pipeline (ingest -> fetch -> conflate -> checks) for one tile.

Usage:
    python -u -m scripts.run_one_tile <tile_id>
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from t2 import config as _config, pipeline


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m scripts.run_one_tile <tile_id>", file=sys.stderr)
        return 2
    tile_id = argv[0]

    cfg = _config.load()
    tiles_path = cfg.data_dir / "tiles.json"
    data = json.loads(tiles_path.read_text(encoding="utf-8"))
    tile = next((t for t in data.get("tiles", []) if t["id"] == tile_id), None)
    if tile is None:
        print(f"tile {tile_id!r} not found in {tiles_path}", file=sys.stderr)
        return 2

    bbox = tuple(tile["bbox"])
    name = f"{tile_id}-batch-{datetime.now().date().isoformat()}"
    print(f"tile: {tile_id}  bbox: {bbox}  run_name: {name}")

    run_id = pipeline.start_run(name, bbox)
    print(f"run_id: {run_id}")

    print("ingest…")
    n = pipeline.ingest_stage(run_id)
    print(f"  ingested {n} candidates")

    print("fetch (force=True to re-clip from refreshed extract)…")
    digest = pipeline.fetch_stage(run_id, force=True)
    print(f"  osm_hash: {digest[:16]}…")

    print("conflate…")
    counts = pipeline.conflate_stage(run_id, osm_hash=digest)
    print(f"  {counts}")

    print("checks…")
    counts = pipeline.run_checks(run_id)
    print(f"  {counts}")

    print(f"DONE — run {run_id} ({name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
