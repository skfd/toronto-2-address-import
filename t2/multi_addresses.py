"""Statistics about addr:housenumber values that pack more than one street number.

Canonical OSM uses `;` to separate multi-values, but the Toronto extract mixes
in `,`-separated lists and `N-M`-style ranges as well. This module classifies
each non-interpolation element with a multi-valued housenumber and returns a
breakdown the /osm/multi page renders as tables and mini bar-charts.

Entry point: collect(json_path) → dict. Results are cached by mtime of the
source JSON so repeated page loads don't re-parse the ~100 MiB file.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

_RANGE_SIMPLE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
_RANGE_WITH_LETTER = re.compile(r"^\s*\d+[A-Za-z]+\s*-\s*\d+[A-Za-z]*\s*$|^\s*\d+[A-Za-z]*\s*-\s*\d+[A-Za-z]+\s*$")
_FRACTION = re.compile(r"^\s*\d+\s+\d+/\d+\s*$|^\s*\d+/\d+\s*$")

_CACHE: dict[Path, tuple[float, dict[str, Any]]] = {}
_LIST_CACHE: dict[Path, tuple[float, dict[str, Any]]] = {}


def _example_row(el: dict) -> dict:
    tags = el.get("tags") or {}
    return {
        "type": el.get("type"),
        "id": el.get("id"),
        "hn": tags.get("addr:housenumber", ""),
        "street": tags.get("addr:street", ""),
        "name": tags.get("name", ""),
        "building": tags.get("building", ""),
        "shop": tags.get("shop", ""),
        "amenity": tags.get("amenity", ""),
    }


def _compute(json_path: Path) -> dict[str, Any]:
    data = json.loads(json_path.read_text(encoding="utf-8"))

    total_elements = len(data)
    with_hn = 0

    sep_counts = {"semicolon": 0, "comma": 0, "range": 0, "slash_multi": 0, "slash_fraction": 0}
    sep_by_type: dict[str, Counter] = {k: Counter() for k in sep_counts}
    sep_examples: dict[str, list[dict]] = {k: [] for k in sep_counts}
    sep_value_counts: dict[str, Counter] = {k: Counter() for k in sep_counts}

    comma_list_lengths: Counter = Counter()
    semicolon_list_lengths: Counter = Counter()

    # dash-range span histogram
    dash_span_zero_or_neg = 0
    dash_span_1 = 0
    dash_span_2 = 0
    dash_span_3_to_10 = 0
    dash_span_11_to_100 = 0
    dash_span_gt_100 = 0
    dash_with_letter: list[dict] = []
    dash_top_spans: list[tuple[int, dict]] = []  # (span, example)

    for el in data:
        tags = el.get("tags") or {}
        hn = tags.get("addr:housenumber")
        if not hn:
            continue
        with_hn += 1
        if "addr:interpolation" in tags:
            # interpolation endpoints are single numbers on the interpolation
            # way's endpoint nodes — they aren't multi-addresses.
            continue

        is_fraction = bool(_FRACTION.match(hn))
        has_semi = ";" in hn
        has_comma = "," in hn
        has_slash = "/" in hn
        rng_match = _RANGE_SIMPLE.match(hn)

        if has_semi:
            sep_counts["semicolon"] += 1
            sep_by_type["semicolon"][el.get("type")] += 1
            sep_value_counts["semicolon"][hn] += 1
            if len(sep_examples["semicolon"]) < 8:
                sep_examples["semicolon"].append(_example_row(el))
            parts = [p for p in (x.strip() for x in hn.split(";")) if p]
            semicolon_list_lengths[len(parts)] += 1

        if has_comma:
            sep_counts["comma"] += 1
            sep_by_type["comma"][el.get("type")] += 1
            sep_value_counts["comma"][hn] += 1
            if len(sep_examples["comma"]) < 8:
                sep_examples["comma"].append(_example_row(el))
            parts = [p for p in (x.strip() for x in hn.split(",")) if p]
            comma_list_lengths[len(parts)] += 1

        if has_slash:
            if is_fraction:
                sep_counts["slash_fraction"] += 1
                sep_by_type["slash_fraction"][el.get("type")] += 1
                sep_value_counts["slash_fraction"][hn] += 1
                if len(sep_examples["slash_fraction"]) < 8:
                    sep_examples["slash_fraction"].append(_example_row(el))
            else:
                sep_counts["slash_multi"] += 1
                sep_by_type["slash_multi"][el.get("type")] += 1
                sep_value_counts["slash_multi"][hn] += 1
                if len(sep_examples["slash_multi"]) < 8:
                    sep_examples["slash_multi"].append(_example_row(el))

        if rng_match and not has_semi and not has_comma:
            sep_counts["range"] += 1
            sep_by_type["range"][el.get("type")] += 1
            sep_value_counts["range"][hn] += 1
            if len(sep_examples["range"]) < 8:
                sep_examples["range"].append(_example_row(el))
            a, b = int(rng_match.group(1)), int(rng_match.group(2))
            span = b - a
            if span <= 0:
                dash_span_zero_or_neg += 1
            elif span == 1:
                dash_span_1 += 1
            elif span == 2:
                dash_span_2 += 1
            elif span <= 10:
                dash_span_3_to_10 += 1
            elif span <= 100:
                dash_span_11_to_100 += 1
            else:
                dash_span_gt_100 += 1
            if span > 0:
                dash_top_spans.append((span, _example_row(el)))
        elif _RANGE_WITH_LETTER.match(hn) and not has_semi and not has_comma:
            # Tagged as range-like but one side has letters (e.g., 2523A-2539A,
            # 567-567A). Reported separately so operators can see the letter cases.
            if len(dash_with_letter) < 20:
                dash_with_letter.append(_example_row(el))

    dash_top_spans.sort(key=lambda s: -s[0])
    dash_top_spans = dash_top_spans[:15]

    def _top_values(kind: str, n: int = 10) -> list[dict]:
        return [{"hn": v, "count": c} for v, c in sep_value_counts[kind].most_common(n)]

    multi_total = (
        sep_counts["semicolon"] + sep_counts["comma"] + sep_counts["range"] + sep_counts["slash_multi"]
    )
    # an element could show up in both semicolon and comma buckets (e.g.
    # "11; 11 1/2; 11A"), but that is rare enough we let the total over-count
    # by a handful rather than dedupe — the per-bucket counts are the
    # meaningful numbers.

    return {
        "source_path": str(json_path),
        "source_mtime": json_path.stat().st_mtime,
        "source_bytes": json_path.stat().st_size,
        "total_elements": total_elements,
        "with_housenumber": with_hn,
        "multi_total": multi_total,
        "separators": [
            {
                "kind": "semicolon",
                "label": "Semicolon (OSM-canonical)",
                "glyph": ";",
                "count": sep_counts["semicolon"],
                "by_type": dict(sep_by_type["semicolon"]),
                "top": _top_values("semicolon"),
                "examples": sep_examples["semicolon"],
            },
            {
                "kind": "comma",
                "label": "Comma (non-canonical)",
                "glyph": ",",
                "count": sep_counts["comma"],
                "by_type": dict(sep_by_type["comma"]),
                "top": _top_values("comma"),
                "examples": sep_examples["comma"],
            },
            {
                "kind": "range",
                "label": "Dash range (N-M)",
                "glyph": "-",
                "count": sep_counts["range"],
                "by_type": dict(sep_by_type["range"]),
                "top": _top_values("range"),
                "examples": sep_examples["range"],
            },
            {
                "kind": "slash_multi",
                "label": "Slash multi-value",
                "glyph": "/",
                "count": sep_counts["slash_multi"],
                "by_type": dict(sep_by_type["slash_multi"]),
                "top": _top_values("slash_multi"),
                "examples": sep_examples["slash_multi"],
            },
        ],
        "slash_fraction": {
            "count": sep_counts["slash_fraction"],
            "by_type": dict(sep_by_type["slash_fraction"]),
            "top": _top_values("slash_fraction"),
        },
        "dash_spans": {
            "total": sep_counts["range"],
            "buckets": [
                {"label": "≤0 (equal/reversed)", "count": dash_span_zero_or_neg},
                {"label": "1 (adjacent)", "count": dash_span_1},
                {"label": "2 (duplex pair)", "count": dash_span_2},
                {"label": "3-10", "count": dash_span_3_to_10},
                {"label": "11-100", "count": dash_span_11_to_100},
                {"label": ">100 (suspect unit-hyphen-house)", "count": dash_span_gt_100},
            ],
            "top_spans": [
                {"span": s, **ex} for s, ex in dash_top_spans
            ],
            "with_letter_examples": dash_with_letter,
        },
        "comma_list_lengths": sorted(comma_list_lengths.items()),
        "semicolon_list_lengths": sorted(semicolon_list_lengths.items()),
    }


def _entry_row(el: dict) -> dict:
    tags = el.get("tags") or {}
    lat = el.get("lat")
    lon = el.get("lon")
    if lat is None or lon is None:
        c = el.get("center") or {}
        lat = c.get("lat")
        lon = c.get("lon")
    return {
        "type": el.get("type"),
        "id": el.get("id"),
        "hn": tags.get("addr:housenumber", ""),
        "street": tags.get("addr:street", ""),
        "name": tags.get("name", ""),
        "building": tags.get("building", ""),
        "shop": tags.get("shop", ""),
        "amenity": tags.get("amenity", ""),
        "lat": lat,
        "lon": lon,
    }


def _sort_key(r: dict) -> tuple:
    """Sort entries by street, then by the first numeric housenumber found."""
    hn = r.get("hn") or ""
    m = re.search(r"\d+", hn)
    num = int(m.group(0)) if m else 0
    return ((r.get("street") or "").lower(), num, hn)


def _compute_entries(json_path: Path) -> dict[str, Any]:
    """Enumerate every non-canonical or suspect-error multi-address entry.

    Categories:
      - comma:               any `,`-separated housenumber (non-canonical)
      - slash_multi:         `/`-separated multi-value (not fractions like `20 1/2`)
      - range_valid:         `N-M` with 0 < span ≤ 100 (non-canonical but plausible)
      - range_letter:        range-style with letter suffix (e.g. `2523A-2539A`)
      - error_reversed:      `N-M` with span ≤ 0 (equal or reversed)
      - error_unit_prefix:   `N-M` with span > 100 (suspected "Unit-HouseNumber")
      - mixed:               entry uses both `;` and `,` — e.g. `2335,2335-1/2A`
    Semicolon-only entries are canonical and excluded.
    """
    data = json.loads(json_path.read_text(encoding="utf-8"))

    buckets: dict[str, list[dict]] = {
        "comma": [],
        "slash_multi": [],
        "range_valid": [],
        "range_letter": [],
        "error_reversed": [],
        "error_unit_prefix": [],
        "mixed": [],
    }

    for el in data:
        tags = el.get("tags") or {}
        hn = tags.get("addr:housenumber")
        if not hn:
            continue
        if "addr:interpolation" in tags:
            continue

        has_semi = ";" in hn
        has_comma = "," in hn
        has_slash = "/" in hn
        is_fraction = bool(_FRACTION.match(hn))
        rng_match = _RANGE_SIMPLE.match(hn)
        row = _entry_row(el)

        slash_is_multi = has_slash and not is_fraction
        mix_flags = sum((has_semi, has_comma, slash_is_multi))
        if mix_flags >= 2:
            buckets["mixed"].append(row)

        if has_comma:
            buckets["comma"].append(row)

        if has_slash and not is_fraction:
            buckets["slash_multi"].append(row)

        if rng_match and not has_semi and not has_comma:
            a, b = int(rng_match.group(1)), int(rng_match.group(2))
            span = b - a
            if span <= 0:
                buckets["error_reversed"].append({**row, "span": span})
            elif span > 100:
                buckets["error_unit_prefix"].append({**row, "span": span})
            else:
                buckets["range_valid"].append({**row, "span": span})
        elif _RANGE_WITH_LETTER.match(hn) and not has_semi and not has_comma:
            buckets["range_letter"].append(row)

    for k, rows in buckets.items():
        rows.sort(key=_sort_key)

    categories = [
        {
            "key": "error_reversed",
            "label": "Reversed or zero-span ranges",
            "description": "`N-M` where N ≥ M. Almost certainly data-entry errors.",
            "severity": "error",
        },
        {
            "key": "error_unit_prefix",
            "label": "Suspected Unit-HouseNumber prefixes",
            "description": "`N-M` with span > 100. Likely the Canadian \"Unit N ‑ Street #M\" convention squeezed into the housenumber field, not a real range.",
            "severity": "error",
        },
        {
            "key": "mixed",
            "label": "Mixed separators",
            "description": "A single tag combines two or more of `;`, `,`, `/` (non-fraction). Tokenizers that split on only one separator will misread these.",
            "severity": "warn",
        },
        {
            "key": "range_letter",
            "label": "Range-style with letter suffix",
            "description": "e.g. `2523A-2539A`, `567-567A`. Readable to a human but non-standard for machines.",
            "severity": "warn",
        },
        {
            "key": "comma",
            "label": "Comma-separated",
            "description": "`,` is not the OSM-canonical multi-value separator. OSM documents `;`.",
            "severity": "noncanonical",
        },
        {
            "key": "slash_multi",
            "label": "Slash multi-value",
            "description": "Rare; forms like `131/151/181` or `586/586a`. Fractional single addresses (`20 1/2`) are excluded.",
            "severity": "noncanonical",
        },
        {
            "key": "range_valid",
            "label": "Dash ranges (N-M, span 1-100)",
            "description": "Well-formed ranges like `18-20` or `168-172`. Non-canonical vs `;` but usually a legitimate multi-number building.",
            "severity": "noncanonical",
        },
    ]
    for cat in categories:
        cat["count"] = len(buckets[cat["key"]])
        # Key is "rows" not "items" — Jinja2 attribute access on dicts resolves
        # `.items` to the built-in dict method, not a user-defined key.
        cat["rows"] = buckets[cat["key"]]

    total = sum(c["count"] for c in categories)
    return {
        "source_path": str(json_path),
        "source_mtime": json_path.stat().st_mtime,
        "source_bytes": json_path.stat().st_size,
        "total": total,
        "categories": categories,
    }


def list_entries(json_path: Path) -> dict[str, Any]:
    """Return the full per-entry listing. Cached by mtime of the source JSON."""
    if not json_path.exists():
        return {
            "source_path": str(json_path),
            "source_mtime": None,
            "source_bytes": None,
            "total": 0,
            "categories": [],
            "missing": True,
        }
    mtime = json_path.stat().st_mtime
    cached = _LIST_CACHE.get(json_path)
    if cached and cached[0] == mtime:
        return cached[1]
    result = _compute_entries(json_path)
    _LIST_CACHE[json_path] = (mtime, result)
    return result


def collect(json_path: Path) -> dict[str, Any]:
    """Return multi-address stats for the given OSM-addresses JSON file.

    Cached by mtime so the ~100 MiB file is only parsed once per change.
    Missing file → an "empty" payload that the template can still render.
    """
    if not json_path.exists():
        return {
            "source_path": str(json_path),
            "source_mtime": None,
            "source_bytes": None,
            "total_elements": 0,
            "with_housenumber": 0,
            "multi_total": 0,
            "separators": [],
            "slash_fraction": {"count": 0, "by_type": {}, "top": []},
            "dash_spans": {"total": 0, "buckets": [], "top_spans": [], "with_letter_examples": []},
            "comma_list_lengths": [],
            "semicolon_list_lengths": [],
            "missing": True,
        }
    mtime = json_path.stat().st_mtime
    cached = _CACHE.get(json_path)
    if cached and cached[0] == mtime:
        return cached[1]
    stats = _compute(json_path)
    _CACHE[json_path] = (mtime, stats)
    return stats
