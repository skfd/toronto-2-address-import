"""Emit JOSM-compatible .osm XML from a batch. Adapted from sibling src/osm_export.py."""
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from . import batcher, config as _config

_CONFIG = _config.load()

STATIC_TAGS = {
    "addr:city": "Toronto",
    "addr:province": "ON",
    "source": "City of Toronto Open Data",
}


def _osm_change_xml(items: list[dict]) -> bytes:
    """Build an <osm version=0.6> element with one <node> per item. Returns serialized bytes."""
    root = ET.Element("osm", version="0.6", generator="t2-address-import")
    for it in items:
        lat, lon = it["lat"], it["lon"]
        if lat is None or lon is None:
            continue
        node = ET.SubElement(
            root, "node",
            id=str(it["local_node_id"]),
            lat=f"{lat:.7f}",
            lon=f"{lon:.7f}",
            action="modify",
            visible="true",
        )
        tags = {
            "addr:housenumber": (it.get("housenumber") or "").strip(),
            "addr:street": (it.get("street_raw") or "").strip(),
            **STATIC_TAGS,
        }
        for k, v in tags.items():
            if v:
                ET.SubElement(node, "tag", k=k, v=v)
    return ET.tostring(root, encoding="utf-8")


def write_xml(batch_id: int) -> Path:
    items = batcher.load_batch_items(batch_id)
    raw = _osm_change_xml(items)
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    out = _CONFIG.data_dir / f"batch_{batch_id}.osm"
    out.write_bytes(pretty)
    return out


def osmchange_xml(batch_id: int, changeset_id: int) -> bytes:
    """Build an osmChange 0.6 document for direct API upload."""
    items = batcher.load_batch_items(batch_id)
    root = ET.Element("osmChange", version="0.6", generator="t2-address-import")
    create = ET.SubElement(root, "create")
    for it in items:
        lat, lon = it["lat"], it["lon"]
        if lat is None or lon is None:
            continue
        node = ET.SubElement(
            create, "node",
            id=str(it["local_node_id"]),
            changeset=str(changeset_id),
            lat=f"{lat:.7f}",
            lon=f"{lon:.7f}",
            version="0",
        )
        tags = {
            "addr:housenumber": (it.get("housenumber") or "").strip(),
            "addr:street": (it.get("street_raw") or "").strip(),
            **STATIC_TAGS,
        }
        for k, v in tags.items():
            if v:
                ET.SubElement(node, "tag", k=k, v=v)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
