"""Compare proposed address tags against OSM tags and classify the matched element's geometry.

Used by the review detail page and the audit log to show a 2-column (Proposed vs OSM has)
diff per candidate.
"""
from typing import Iterable

from .conflate import normalize_street

DEFAULT_KEYS: tuple[str, ...] = (
    "addr:housenumber",
    "addr:street",
    "addr:unit",
    "addr:postcode",
    "addr:city",
    "addr:country",
)

_POLYGON_WAY_TAGS = ("building", "building:part")


def geom_hint(osm_el: dict) -> str:
    """Classify an OSM element as node, way-line, way-polygon, relation-polygon, or relation."""
    t = osm_el.get("type")
    tags = osm_el.get("tags") or {}
    if t == "node":
        return "node"
    if t == "way":
        if any(k in tags for k in _POLYGON_WAY_TAGS) or tags.get("area") == "yes":
            return "way-polygon"
        return "way-line"
    if t == "relation":
        if tags.get("type") == "multipolygon" or "building" in tags:
            return "relation-polygon"
        return "relation"
    return t or "unknown"


def _equal(tag: str, a: str, b: str) -> bool:
    if tag == "addr:street":
        return normalize_street(a) == normalize_street(b)
    if tag == "addr:housenumber":
        return a.strip().upper() == b.strip().upper()
    return a.strip() == b.strip()


def compare_tags(
    proposed: dict[str, str],
    osm: dict[str, str] | None,
    keys: Iterable[str] = DEFAULT_KEYS,
) -> list[dict]:
    """Return one row per key: {tag, proposed, osm, status}.

    status ∈ {SAME, ADD, CHANGE, MISSING_PROPOSED}
      - SAME: both present and equal (normalization-aware for street/housenumber)
      - ADD: proposed has it, OSM doesn't → we'd be adding
      - CHANGE: both have it but values differ → conflict
      - MISSING_PROPOSED: OSM has it, we don't propose anything → informational
    """
    osm = osm or {}
    rows: list[dict] = []
    for k in keys:
        p = (proposed.get(k) or "").strip()
        o = (osm.get(k) or "").strip()
        if p and o:
            status = "SAME" if _equal(k, p, o) else "CHANGE"
        elif p and not o:
            status = "ADD"
        elif o and not p:
            status = "MISSING_PROPOSED"
        else:
            continue  # neither side has it; skip the row
        rows.append({"tag": k, "proposed": p, "osm": o, "status": status})
    return rows
