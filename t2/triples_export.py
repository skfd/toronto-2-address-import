"""Public traceability dump: per uploaded candidate, emit
(address_point_id, address_full, osm_node_id, changeset_id) as CSV.

Used by static_export and static_export_all so the OSM wiki page can link
to a public file listing every node we created and the changeset that
produced it. Per-tile files plus a cumulative file across all tiles.
"""
from __future__ import annotations

import csv
from pathlib import Path

from . import db as _db


HEADER = ("address_point_id", "address_full", "osm_node_id", "changeset_id")

Row = tuple[int, str, int, int]


def fetch_for_run(run_id: int) -> list[Row]:
    conn = _db.connect()
    try:
        rows = conn.execute(
            """
            SELECT bi.candidate_id, c.address_full, bi.osm_node_id, b.changeset_id
            FROM batch_items bi
            JOIN batches b   ON b.batch_id = bi.batch_id
            JOIN candidates c ON c.run_id = b.run_id AND c.candidate_id = bi.candidate_id
            WHERE b.run_id = ?
              AND bi.upload_status = 'uploaded'
              AND bi.osm_node_id IS NOT NULL
              AND b.changeset_id IS NOT NULL
            ORDER BY bi.candidate_id
            """,
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        (int(r["candidate_id"]), r["address_full"] or "",
         int(r["osm_node_id"]), int(r["changeset_id"]))
        for r in rows
    ]


def write_csv(path: Path, rows: list[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
