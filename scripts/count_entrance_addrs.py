"""Quick exploration: how many OSM elements with addr:housenumber are entrance-tagged,
POI-tagged, both, or neither across all cached run snapshots."""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import Counter

POI_TAG_KEYS = (
    "amenity", "shop", "office", "tourism", "leisure", "craft", "healthcare", "building",
    "disused:shop", "disused:amenity", "disused:office", "was:amenity",
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATTERN = os.path.join(ROOT, "data", "osm_current_run*.json")


def is_poi(tags: dict) -> bool:
    return any(k in tags for k in POI_TAG_KEYS)


def main() -> int:
    files = sorted(glob.glob(PATTERN))
    print(f"scanning {len(files)} OSM cache files", file=sys.stderr)

    seen: dict[tuple[str, int], dict] = {}
    for i, path in enumerate(files):
        if i % 250 == 0:
            print(f"  {i}/{len(files)} ({len(seen)} unique addr elements so far)",
                  file=sys.stderr)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            print(f"  skip {path}: {e}", file=sys.stderr)
            continue
        elements = payload if isinstance(payload, list) else payload.get("elements") or []
        for el in elements:
            tags = el.get("tags") or {}
            if "addr:housenumber" not in tags:
                continue
            key = (el.get("type"), el.get("id"))
            if key in seen:
                continue
            seen[key] = el

    counts = Counter()
    type_counts = Counter()
    by_type_class = Counter()
    entrance_value_counts = Counter()
    entrance_poi_examples: list[dict] = []
    entrance_only_examples: list[dict] = []
    for (etype, _eid), el in seen.items():
        tags = el.get("tags") or {}
        has_entrance = "entrance" in tags
        # Match build_osm_index logic: POI filter only applies to nodes.
        poi_flag = (etype == "node") and is_poi(tags)

        if has_entrance and poi_flag:
            cls = "entrance + poi"
            if len(entrance_poi_examples) < 5:
                entrance_poi_examples.append({"type": etype, "id": el.get("id"),
                                              "tags": tags})
        elif has_entrance:
            cls = "entrance only"
            if len(entrance_only_examples) < 5:
                entrance_only_examples.append({"type": etype, "id": el.get("id"),
                                               "tags": tags})
        elif poi_flag:
            cls = "poi only"
        else:
            cls = "pure address"
        counts[cls] += 1
        type_counts[etype] += 1
        by_type_class[(etype, cls)] += 1
        if has_entrance:
            entrance_value_counts[tags.get("entrance")] += 1

    total = sum(counts.values())
    print()
    print(f"unique OSM elements with addr:housenumber: {total}")
    print()
    print("by class (build_osm_index POI rule applied to nodes only):")
    for cls, n in counts.most_common():
        pct = 100.0 * n / total if total else 0.0
        print(f"  {cls:20s}  {n:>8d}  ({pct:5.2f}%)")

    print()
    print("by type:")
    for etype, n in type_counts.most_common():
        print(f"  {etype:8s}  {n:>8d}")

    print()
    print("by (type, class):")
    for (etype, cls), n in sorted(by_type_class.items()):
        print(f"  {etype:8s} {cls:20s}  {n:>8d}")

    print()
    print("entrance= tag values (any class):")
    for v, n in entrance_value_counts.most_common():
        print(f"  {str(v):20s}  {n:>8d}")

    print()
    print("examples — entrance + poi (would be POI-filtered today):")
    for ex in entrance_poi_examples:
        print(f"  {ex['type']}/{ex['id']}  {ex['tags']}")

    print()
    print("examples — entrance only:")
    for ex in entrance_only_examples:
        print(f"  {ex['type']}/{ex['id']}  {ex['tags']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
