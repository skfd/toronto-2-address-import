"""Compose upload batches from APPROVED candidates."""
import uuid
from datetime import datetime, timezone

from . import audit, db as _db


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compose(run_id: int, mode: str, size: int) -> int | None:
    """Pick up to `size` APPROVED candidates, create a batch, mark them BATCHED.

    Returns new batch_id or None if nothing to batch.
    """
    assert mode in ("osm_api", "josm_xml")
    now = _iso()
    token = str(uuid.uuid4())
    conn = _db.connect()
    try:
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT candidate_id FROM candidates WHERE run_id=? AND stage='APPROVED' "
            "ORDER BY candidate_id LIMIT ?",
            (run_id, size),
        ).fetchall()
        if not rows:
            conn.execute("ROLLBACK")
            return None

        cur = conn.execute(
            """
            INSERT INTO batches (run_id, mode, status, size, created_at, client_token)
            VALUES (?, ?, 'draft', ?, ?, ?)
            """,
            (run_id, mode, len(rows), now, token),
        )
        batch_id = int(cur.lastrowid)
        for i, r in enumerate(rows, start=1):
            conn.execute(
                """
                INSERT INTO batch_items (batch_id, candidate_id, local_node_id, upload_status)
                VALUES (?, ?, ?, 'pending')
                """,
                (batch_id, r["candidate_id"], -i),
            )
            conn.execute(
                "UPDATE candidates SET stage='BATCHED', stage_updated_at=? WHERE run_id=? AND candidate_id=?",
                (now, run_id, r["candidate_id"]),
            )
        audit.log(actor="operator", event_type="BATCH_CREATED", run_id=run_id, batch_id=batch_id,
                  payload={"mode": mode, "size": len(rows), "client_token": token}, conn=conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return batch_id


def list_batches(run_id: int) -> list[dict]:
    conn = _db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM batches WHERE run_id=? ORDER BY created_at DESC", (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_batch_items(batch_id: int) -> list[dict]:
    """Return batch items joined with candidate info suitable for XML/API upload."""
    conn = _db.connect()
    try:
        rows = conn.execute(
            """
            SELECT bi.candidate_id, bi.local_node_id, bi.upload_status, bi.osm_node_id,
                   c.housenumber, c.street_raw, c.lat, c.lon, c.address_class,
                   cf.proposed_postcode
            FROM batch_items bi
            JOIN batches b ON b.batch_id = bi.batch_id
            JOIN candidates c ON c.run_id = b.run_id AND c.candidate_id = bi.candidate_id
            LEFT JOIN conflation cf ON cf.run_id = b.run_id AND cf.candidate_id = bi.candidate_id
            WHERE bi.batch_id = ?
            ORDER BY bi.local_node_id DESC
            """,
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
