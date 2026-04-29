"""Find OSM addresses in the Toronto extract that use a building/place name
instead of (or alongside) addr:street.

Buckets every element with addr:housenumber by which "what street?" tag it has:
  - housename_with_street: addr:housename AND addr:street (street-anchored, named building)
  - housename_only:        addr:housename, no addr:street (building name is the address)
  - place_only:            addr:place, no addr:street (named locality, no street)
  - place_and_street:      both addr:place and addr:street
  - no_anchor:             no addr:street, no addr:place, no addr:housename
  - street_only:           normal addr:street case (sanity baseline)
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACT = os.path.join(ROOT, "data", "osm", "toronto-addresses.json")


def classify(tags: dict) -> str:
    has_street = bool(tags.get("addr:street"))
    has_house = bool(tags.get("addr:housename"))
    has_place = bool(tags.get("addr:place"))
    if has_house and has_street:
        return "housename_with_street"
    if has_house and not has_street and not has_place:
        return "housename_only"
    if has_place and has_street:
        return "place_and_street"
    if has_place and not has_street:
        return "place_only"
    if not has_street and not has_place and not has_house:
        return "no_anchor"
    return "street_only"


def main() -> int:
    print(f"loading {EXTRACT}", file=sys.stderr)
    with open(EXTRACT, "r", encoding="utf-8") as f:
        elements = json.load(f)
    print(f"  {len(elements)} elements", file=sys.stderr)

    counts: Counter[str] = Counter()
    by_type: Counter[tuple[str, str]] = Counter()
    examples: dict[str, list[dict]] = {}
    housename_values: Counter[str] = Counter()
    place_values: Counter[str] = Counter()

    for el in elements:
        tags = el.get("tags") or {}
        if "addr:housenumber" not in tags:
            continue
        cls = classify(tags)
        counts[cls] += 1
        by_type[(el.get("type"), cls)] += 1
        if cls != "street_only":
            examples.setdefault(cls, [])
            if len(examples[cls]) < 8:
                examples[cls].append({
                    "type": el.get("type"),
                    "id": el.get("id"),
                    "tags": {k: v for k, v in tags.items() if k.startswith("addr:") or k in ("name", "building", "amenity")},
                })
        if tags.get("addr:housename"):
            housename_values[tags["addr:housename"]] += 1
        if tags.get("addr:place"):
            place_values[tags["addr:place"]] += 1

    total = sum(counts.values())
    print()
    print(f"unique OSM elements with addr:housenumber: {total}")
    print()
    print("by class:")
    for cls, n in counts.most_common():
        pct = 100.0 * n / total if total else 0.0
        print(f"  {cls:24s}  {n:>8d}  ({pct:5.2f}%)")

    print()
    print("by (type, class) [non-street_only only]:")
    for (etype, cls), n in sorted(by_type.items()):
        if cls == "street_only":
            continue
        print(f"  {etype:8s} {cls:24s}  {n:>8d}")

    print()
    print("top addr:housename values:")
    for v, n in housename_values.most_common(15):
        print(f"  {n:>5d}  {v}")

    print()
    print("top addr:place values:")
    for v, n in place_values.most_common(15):
        print(f"  {n:>5d}  {v}")

    for cls, exs in examples.items():
        print()
        print(f"examples — {cls}:")
        for ex in exs:
            print(f"  {ex['type']}/{ex['id']}  {ex['tags']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
