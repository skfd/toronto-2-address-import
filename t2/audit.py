"""Single entry point for audit log writes."""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db as _db


def log(
    *,
    actor: str,
    event_type: str,
    run_id: int | None = None,
    candidate_id: int | None = None,
    batch_id: int | None = None,
    payload: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    row = (
        datetime.now(timezone.utc).isoformat(),
        run_id,
        candidate_id,
        batch_id,
        actor,
        event_type,
        json.dumps(payload or {}, default=str),
    )
    sql = """
        INSERT INTO events (ts, run_id, candidate_id, batch_id, actor, event_type, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    if conn is None:
        c = _db.connect()
        try:
            c.execute(sql, row)
        finally:
            c.close()
    else:
        conn.execute(sql, row)
