"""Operator verdicts + JOSM-ready .osm export for the /osm/multi/all page.

The review page lets the operator classify each non-canonical or suspect
multi-address row with one verdict from ``VERDICTS``. On save, verdicts are
persisted in ``multi_address_verdicts`` (see migration 010). Actionable
verdicts (``normalize``, ``unit_prefix``, ``reverse``) then drive a live
Overpass fetch: we transform the *current* tags on OSM and emit a `.osm` file
the operator opens in JOSM and uploads. Verdicts whose live value no longer
matches the precondition are reported as conflicts and excluded from the file.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import requests

from . import config as _config, db as _db

_CONFIG = _config.load()

VERDICTS = {"normalize", "keep_range", "unit_prefix", "reverse", "skip"}
_ACTIONABLE = {"normalize", "unit_prefix", "reverse"}

# Per-category applicable verdicts. Used by the template to render only the
# buttons that make sense for each row.
VERDICT_OPTIONS: dict[str, list[dict[str, str]]] = {
    "error_reversed": [
        {"value": "reverse",     "label": "reverse N-M"},
        {"value": "unit_prefix", "label": "unit-#"},
        {"value": "normalize",   "label": "→ N,M"},
        {"value": "skip",        "label": "skip"},
    ],
    "error_unit_prefix": [
        {"value": "unit_prefix", "label": "unit-#"},
        {"value": "skip",        "label": "skip"},
    ],
    "mixed": [
        {"value": "normalize", "label": "→ ,"},
        {"value": "skip",      "label": "skip"},
    ],
    "range_letter": [
        {"value": "keep_range",  "label": "keep"},
        {"value": "unit_prefix", "label": "unit-#"},
        {"value": "normalize",   "label": "→ ,"},
        {"value": "skip",        "label": "skip"},
    ],
    "comma": [
        {"value": "normalize", "label": "→ ,"},
        {"value": "skip",      "label": "skip"},
    ],
    "slash_multi": [
        {"value": "normalize", "label": "→ ,"},
        {"value": "skip",      "label": "skip"},
    ],
    "range_valid": [
        {"value": "keep_range",  "label": "keep"},
        {"value": "normalize",   "label": "→ ,"},
        {"value": "unit_prefix", "label": "unit-#"},
        {"value": "skip",        "label": "skip"},
    ],
}

_RANGE_UNIT_PAIR = re.compile(r"^\s*(\d+)\s*-\s*(\d+[A-Za-z]*)\s*$")
_RANGE_NUM_PAIR = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


# ---- persistence ----

def load_verdicts() -> dict[tuple[str, int], str]:
    conn = _db.connect()
    try:
        rows = conn.execute(
            "SELECT osm_type, osm_id, verdict FROM multi_address_verdicts"
        ).fetchall()
    finally:
        conn.close()
    return {(r["osm_type"], int(r["osm_id"])): r["verdict"] for r in rows}


def save_verdicts(entries: list[tuple[str, int, str]]) -> int:
    """Upsert (osm_type, osm_id, verdict). Entries with unknown verdict are
    silently dropped. Rows whose verdict already matches the DB are a no-op
    — their ``updated_at`` and ``exported_at`` are preserved. Returns the
    number of rows actually inserted or changed."""
    entries = [(t, i, v) for (t, i, v) in entries if v in VERDICTS]
    if not entries:
        return 0
    existing = load_verdicts()
    changed = [(t, i, v) for (t, i, v) in entries if existing.get((t, i)) != v]
    if not changed:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    # When the verdict actually changes, wipe exported_at/exported_file so the
    # row is eligible for the next export. New inserts get NULL by default.
    with _db.tx() as conn:
        conn.executemany(
            "INSERT INTO multi_address_verdicts (osm_type, osm_id, verdict, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(osm_type, osm_id) DO UPDATE SET "
            "  verdict=excluded.verdict, updated_at=excluded.updated_at, "
            "  exported_at=NULL, exported_file=NULL",
            [(t, i, v, now) for t, i, v in changed],
        )
    return len(changed)


def load_unexported_actionable() -> list[tuple[str, int, str]]:
    """Return actionable verdicts (normalize/unit_prefix/reverse) that have
    not yet been written to a .osm export file. ``skip`` and ``keep_range``
    never need exporting so they're filtered out here."""
    conn = _db.connect()
    try:
        rows = conn.execute(
            "SELECT osm_type, osm_id, verdict FROM multi_address_verdicts "
            "WHERE verdict IN ('normalize','unit_prefix','reverse') "
            "  AND exported_at IS NULL "
            "ORDER BY updated_at"
        ).fetchall()
    finally:
        conn.close()
    return [(r["osm_type"], int(r["osm_id"]), r["verdict"]) for r in rows]


def mark_exported(keys: list[tuple[str, int]], file_name: str | None) -> None:
    """Stamp rows as exported so they don't get re-queued on the next save.
    Conflicts are stamped too — they're either already-fixed or deleted, so
    retrying them would just produce the same conflict row."""
    if not keys:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _db.tx() as conn:
        conn.executemany(
            "UPDATE multi_address_verdicts SET exported_at=?, exported_file=? "
            "WHERE osm_type=? AND osm_id=?",
            [(now, file_name, t, i) for (t, i) in keys],
        )


def load_exported_set() -> set[tuple[str, int]]:
    """Return the set of (osm_type, osm_id) that are currently marked as
    already-exported (won't be picked up by the next export)."""
    conn = _db.connect()
    try:
        rows = conn.execute(
            "SELECT osm_type, osm_id FROM multi_address_verdicts "
            "WHERE exported_at IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return {(r["osm_type"], int(r["osm_id"])) for r in rows}


def set_exported_flags(
    set_keys: list[tuple[str, int]],
    clear_keys: list[tuple[str, int]],
) -> None:
    """Apply manual checkbox state from the UI: tick marks a row exported
    (without clobbering an existing file reference), untick clears it so the
    row re-queues for the next export."""
    if not set_keys and not clear_keys:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _db.tx() as conn:
        if set_keys:
            # COALESCE preserves the real file name when a row was previously
            # auto-stamped by build_export and the user just re-ticks the
            # checkbox; the "manual" tag is only used when the stamp started
            # from a UI tick rather than a build.
            conn.executemany(
                "UPDATE multi_address_verdicts "
                "SET exported_at=COALESCE(exported_at, ?), "
                "    exported_file=COALESCE(exported_file, ?) "
                "WHERE osm_type=? AND osm_id=?",
                [(now, "manual", t, i) for (t, i) in set_keys],
            )
        if clear_keys:
            conn.executemany(
                "UPDATE multi_address_verdicts "
                "SET exported_at=NULL, exported_file=NULL "
                "WHERE osm_type=? AND osm_id=?",
                [(t, i) for (t, i) in clear_keys],
            )


# ---- transforms ----

_SLASH_MULTI_TOKEN = re.compile(r"^\d+[A-Za-z]*(?:/\d+[A-Za-z]*)+$")


def _split_housenumbers(hn: str) -> list[str]:
    """Split a multi-value housenumber into canonical parts.

    Splits on ``,`` and ``;`` first. A whole token like ``586/586a`` or
    ``131/151/181`` (digits-only around each ``/``) is then further split on
    ``/``; fraction tokens like ``2 1/2`` or ``11 1/2`` stay intact. Returns
    the parts stripped and de-duplicated, preserving first-seen order.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,;]", hn):
        p = raw.strip()
        if not p:
            continue
        if _SLASH_MULTI_TOKEN.fullmatch(p):
            for sub in p.split("/"):
                sub = sub.strip()
                if sub and sub not in seen:
                    out.append(sub)
                    seen.add(sub)
        elif p not in seen:
            out.append(p)
            seen.add(p)
    return out


def _normalize_sort_key(part: str) -> tuple[int, str]:
    m = re.match(r"^\s*(\d+)", part)
    return (int(m.group(1)) if m else 0, part)


def apply_transform(tags: dict[str, str], verdict: str) -> dict[str, str] | None:
    """Apply verdict to live tags. Returns new tag dict or None if the live
    value no longer matches the verdict's precondition (treated as a conflict
    by the caller — likely already fixed upstream).
    """
    hn = tags.get("addr:housenumber", "")
    if not hn:
        return None

    if verdict == "normalize":
        # Comma is our preferred separator for lists (it's non-canonical per
        # OSM wiki but the dominant form in this dataset, ~2.5× `;`). A bare
        # range like `18-20` has no list separator to split on, so this
        # transform leaves it alone — the operator would need to expand it
        # by hand in JOSM if they want `18,20`.
        parts = _split_housenumbers(hn)
        if len(parts) < 2:
            return None
        parts.sort(key=_normalize_sort_key)
        new_hn = ",".join(parts)
        if new_hn == hn:
            return None
        return {**tags, "addr:housenumber": new_hn}

    if verdict == "unit_prefix":
        m = _RANGE_UNIT_PAIR.match(hn)
        if not m:
            return None
        unit, house = m.group(1), m.group(2)
        # If an addr:unit already exists and disagrees, bail — something else
        # is going on and we shouldn't silently overwrite.
        if tags.get("addr:unit") and tags["addr:unit"] != unit:
            return None
        return {**tags, "addr:unit": unit, "addr:housenumber": house}

    if verdict == "reverse":
        m = _RANGE_NUM_PAIR.match(hn)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b:
            return None
        return {**tags, "addr:housenumber": f"{b}-{a}"}

    return None


# ---- Overpass fetch + export ----

def _overpass_xml(ids_by_type: dict[str, list[int]]) -> str:
    selectors: list[str] = []
    if ids_by_type.get("node"):
        selectors.append(f"node(id:{','.join(str(i) for i in ids_by_type['node'])});")
    if ids_by_type.get("way"):
        selectors.append(f"way(id:{','.join(str(i) for i in ids_by_type['way'])});")
    if not selectors:
        return '<osm version="0.6"></osm>'
    # `>;` recurses down into way-referenced nodes so the exported file has
    # real geometry for JOSM to render. Trailing `out meta;` dumps those
    # recursed nodes with their current version numbers. We don't set
    # `[out:xml]` — XML is Overpass's default and some instances return 406
    # when `[out:xml]` collides with an implicit Accept.
    query = (
        "[timeout:120];"
        "(" + "".join(selectors) + ");"
        "out meta;"
        ">;"
        "out meta;"
    )
    headers = {
        "Accept": "application/xml, text/xml, */*",
        "User-Agent": "t2-address-import/1.0 (multi-fix export)",
    }
    resp = requests.post(
        _CONFIG.overpass_url,
        data={"data": query},
        headers=headers,
        timeout=200,
    )
    if resp.status_code != 200:
        body = (resp.text or "").strip()
        if len(body) > 500:
            body = body[:500] + "…"
        raise RuntimeError(
            f"Overpass returned HTTP {resp.status_code} for query:\n{query}\n\n{body}"
        )
    return resp.text


def _element_tags(el: ET.Element) -> dict[str, str]:
    return {t.attrib["k"]: t.attrib["v"] for t in el.findall("tag")}


def _rewrite_tags(el: ET.Element, new_tags: dict[str, str]) -> None:
    for tag in list(el.findall("tag")):
        el.remove(tag)
    for k, v in new_tags.items():
        ET.SubElement(el, "tag", k=k, v=v)


def build_export(
    verdicts: list[tuple[str, int, str]],
    out_dir: Path,
) -> dict[str, Any]:
    """Fetch live data for actionable verdicts, apply transforms, write .osm.

    Returns a summary dict with `applied`, `conflicts`, `file_name`, etc.
    Non-actionable verdicts (skip, keep_range) are ignored here — they are
    already persisted by the caller.
    """
    actionable = [(t, i, v) for (t, i, v) in verdicts if v in _ACTIONABLE]
    summary: dict[str, Any] = {
        "total_verdicts": len(verdicts),
        "actionable": len(actionable),
        "applied": [],
        "conflicts": [],
        "file_path": None,
        "file_name": None,
    }
    if not actionable:
        return summary

    ids_by_type: dict[str, list[int]] = {"node": [], "way": []}
    for t, i, _ in actionable:
        if t in ids_by_type:
            ids_by_type[t].append(i)

    xml_text = _overpass_xml(ids_by_type)
    root = ET.fromstring(xml_text)
    by_key: dict[tuple[str, int], ET.Element] = {}
    for el in root:
        if el.tag not in ("node", "way", "relation"):
            continue
        try:
            oid = int(el.attrib["id"])
        except (KeyError, ValueError):
            continue
        by_key[(el.tag, oid)] = el

    edited_keys: set[tuple[str, int]] = set()
    for t, i, v in actionable:
        key = (t, i)
        el = by_key.get(key)
        if el is None:
            summary["conflicts"].append({
                "type": t, "id": i, "verdict": v,
                "reason": "element not found on live OSM (deleted or redacted)",
            })
            continue
        tags = _element_tags(el)
        hn_live = tags.get("addr:housenumber", "")
        new_tags = apply_transform(tags, v)
        if new_tags is None:
            summary["conflicts"].append({
                "type": t, "id": i, "verdict": v,
                "reason": "live value no longer matches this verdict's precondition",
                "live_hn": hn_live,
            })
            continue
        _rewrite_tags(el, new_tags)
        el.set("action", "modify")
        edited_keys.add(key)
        summary["applied"].append({
            "type": t, "id": i, "verdict": v,
            "before": hn_live,
            "after": new_tags.get("addr:housenumber", ""),
            "added_unit": new_tags.get("addr:unit") if v == "unit_prefix" else None,
        })

    # Keep only edited elements + nodes referenced by edited ways (for JOSM
    # to render geometry). Untouched siblings are discarded so the file is a
    # focused edit set.
    keep: set[tuple[str, int]] = set(edited_keys)
    for key in edited_keys:
        if key[0] == "way":
            for nd in by_key[key].findall("nd"):
                try:
                    keep.add(("node", int(nd.attrib["ref"])))
                except (KeyError, ValueError):
                    continue

    out_root = ET.Element("osm", attrib={
        "version": "0.6",
        "generator": "t2-multi-fix",
    })
    # Emit nodes before ways so JOSM parses referenced nodes first.
    nodes_out, ways_out, rels_out = [], [], []
    for el in root:
        if el.tag not in ("node", "way", "relation"):
            continue
        try:
            oid = int(el.attrib["id"])
        except (KeyError, ValueError):
            continue
        if (el.tag, oid) not in keep:
            continue
        if el.tag == "node":
            nodes_out.append(el)
        elif el.tag == "way":
            ways_out.append(el)
        else:
            rels_out.append(el)
    for el in nodes_out + ways_out + rels_out:
        out_root.append(el)

    handled: list[tuple[str, int]] = [
        (r["type"], r["id"]) for r in summary["applied"]
    ] + [(r["type"], r["id"]) for r in summary["conflicts"]]

    file_name: str | None = None
    if edited_keys:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        file_name = f"t2-multi-fix-{stamp}.osm"
        file_path = out_dir / file_name
        ET.ElementTree(out_root).write(file_path, encoding="utf-8", xml_declaration=True)
        summary["file_path"] = str(file_path)
        summary["file_name"] = file_name

    # Stamp rows exported AFTER the file is safely on disk — if the write
    # above fails, nothing is marked and the next save retries everything.
    mark_exported(handled, file_name)
    return summary
