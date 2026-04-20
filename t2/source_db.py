"""Read-only access to the sibling addresses.db."""
import sqlite3
from datetime import datetime, timezone

from . import config as _config

_CONFIG = _config.load()


def connect_readonly() -> sqlite3.Connection:
    uri = f"file:{_CONFIG.source_sqlite_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def latest_snapshot_id(conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    if own:
        conn = connect_readonly()
    try:
        row = conn.execute("SELECT MAX(id) AS m FROM snapshots WHERE skipped = 0").fetchone()
        if not row or row["m"] is None:
            raise RuntimeError("Source DB has no non-skipped snapshots.")
        return int(row["m"])
    finally:
        if own:
            conn.close()


def latest_snapshot_info(stale_after_days: int = 14) -> dict | None:
    """Return {id, downloaded, age_days, is_stale} for the newest non-skipped
    snapshot, or None if the source DB is unavailable or empty.

    Used by the run-create UI to warn when the upstream source hasn't been
    refreshed recently. The upstream publishes daily, so >14d stale means
    we're building candidates against outdated address data.
    """
    try:
        conn = connect_readonly()
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT id, downloaded FROM snapshots WHERE skipped = 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    ts = row["downloaded"]
    age_days: float | None = None
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except (ValueError, TypeError):
            age_days = None
    return {
        "id": int(row["id"]),
        "downloaded": ts,
        "age_days": age_days,
        "is_stale": age_days is not None and age_days > stale_after_days,
    }


def iter_active_addresses_in_bbox(bbox: tuple[float, float, float, float], snapshot_id: int):
    """Yield rows from the source addresses table active at snapshot_id and inside bbox."""
    min_lat, min_lon, max_lat, max_lon = bbox
    conn = connect_readonly()
    try:
        q = """
            SELECT address_point_id, address_full, address_number,
                   lo_num, lo_num_suf, hi_num, hi_num_suf,
                   linear_name_full, linear_name, linear_name_type, linear_name_dir,
                   municipality_name, ward_name, longitude, latitude, extra
            FROM addresses
            WHERE max_snapshot_id = ?
              AND latitude BETWEEN ? AND ?
              AND longitude BETWEEN ? AND ?
        """
        for row in conn.execute(q, (snapshot_id, min_lat, max_lat, min_lon, max_lon)):
            yield dict(row)
    finally:
        conn.close()
