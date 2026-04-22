"""Spike: run the pipeline over a fixed set of tiles and record run_ids.

Reads docs/spike_tiles.json (a list of tile dicts written by the picker),
creates one run per tile named `spike-<tile-id>`, runs ingest → fetch →
conflate → checks to completion, and writes the mapping to
docs/spike_runs.json so the static-export step can find them.

Idempotent: start_run() reopens a run of the same name, and each stage is
internally resumable, so re-running after a crash continues rather than
duplicates.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import pipeline


def main() -> int:
    spec_path = Path("docs/spike_tiles.json")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out_path = Path("docs/spike_runs.json")
    existing = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}

    results: dict[str, dict] = dict(existing)
    total_start = time.monotonic()

    for i, tile in enumerate(spec, 1):
        tid = tile["id"]
        name = f"spike-{tid}"
        bbox = tuple(tile["bbox"])
        print(f"[{i:>2}/{len(spec)}] {tid}  ({tile['address_count']} addrs)", flush=True)

        t0 = time.monotonic()
        run_id = pipeline.start_run(name, bbox)
        ingested = pipeline.ingest_stage(run_id)
        t1 = time.monotonic()
        digest = pipeline.fetch_stage(run_id)
        t2 = time.monotonic()
        conflate_counts = pipeline.conflate_stage(run_id, osm_hash=digest)
        t3 = time.monotonic()
        check_counts = pipeline.run_checks(run_id)
        t4 = time.monotonic()
        stages = pipeline.counts_by_stage(run_id)

        results[tid] = {
            "tile_id": tid,
            "name": name,
            "run_id": run_id,
            "bbox": list(bbox),
            "address_count": tile["address_count"],
            "ingested": ingested,
            "conflate": conflate_counts,
            "checks": check_counts,
            "stages": stages,
            "timing_s": {
                "ingest": round(t1 - t0, 2),
                "fetch": round(t2 - t1, 2),
                "conflate": round(t3 - t2, 2),
                "checks": round(t4 - t3, 2),
                "total": round(t4 - t0, 2),
            },
        }
        print(
            f"    run_id={run_id} ingested={ingested} stages={stages} "
            f"timing={results[tid]['timing_s']}",
            flush=True,
        )
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    total = time.monotonic() - total_start
    print(f"\nall {len(spec)} tiles done in {total:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
