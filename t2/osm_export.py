"""Emit JOSM-compatible .osm XML from a batch. Adapted from sibling src/osm_export.py."""
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from . import batcher, config as _config, db as _db

_CONFIG = _config.load()

STATIC_TAGS = {
    "addr:city": "Toronto",
    "source": "City of Toronto Open Data",
}


def changeset_tags(batch_id: int) -> dict[str, str]:
    """Return the per-batch changeset-level tags (matches IMPORT_PROPOSAL.md §5.3).

    Used for both API uploads (applied to the changeset) and JOSM exports
    (embedded as a header comment so the operator can paste them into JOSM's
    upload dialog).
    """
    conn = _db.connect()
    try:
        row = conn.execute(
            "SELECT b.client_token, r.name AS run_name "
            "FROM batches b JOIN runs r ON r.run_id = b.run_id WHERE b.batch_id = ?",
            (batch_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError(f"batch {batch_id} not found")
    comment = _CONFIG.changeset_comment_template.format(
        run_name=row["run_name"], batch_id=batch_id
    )
    return {
        "comment": comment,
        "source": "City of Toronto Open Data",
        "import": "yes",
        "bot": "no",
        "created_by": "t2-address-import",
        "import:client_token": row["client_token"],
    }


def build_tags(it: dict) -> dict[str, str]:
    """Tag dict for a batch item. Emits entrance=yes for Structure Entrance rows."""
    tags = {
        "addr:housenumber": (it.get("housenumber") or "").strip(),
        "addr:street": (it.get("street_raw") or "").strip(),
        **STATIC_TAGS,
    }
    postcode = (it.get("proposed_postcode") or "").strip()
    if postcode:
        tags["addr:postcode"] = postcode
    if it.get("address_class") == "Structure Entrance":
        tags["entrance"] = "yes"
    return {k: v for k, v in tags.items() if v}


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
        for k, v in build_tags(it).items():
            ET.SubElement(node, "tag", k=k, v=v)
    return ET.tostring(root, encoding="utf-8")


def write_xml(batch_id: int) -> Path:
    items = batcher.load_batch_items(batch_id)
    raw = _osm_change_xml(items)
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")

    # Embed the changeset-level tags as a header comment. JOSM does not
    # auto-apply these — the operator must paste them into the Upload dialog —
    # but having them in the file means the operator can recover them from a
    # text editor or JOSM's raw view if the batch page isn't handy.
    cs_tags = changeset_tags(batch_id)
    header_lines = ["<!-- Changeset tags (paste into JOSM upload dialog):"]
    for k, v in cs_tags.items():
        header_lines.append(f"     {k} = {v}")
    header_lines.append("-->")
    header = ("\n".join(header_lines) + "\n").encode("utf-8")

    # minidom emits an <?xml ...?> declaration on the first line; insert the
    # comment after it so the file remains a valid XML document.
    decl, _, body = pretty.partition(b"\n")
    out = _CONFIG.data_dir / f"batch_{batch_id}.osm"
    out.write_bytes(decl + b"\n" + header + body)
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
        for k, v in build_tags(it).items():
            ET.SubElement(node, "tag", k=k, v=v)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
