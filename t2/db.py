import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config as _config

_CONFIG = _config.load()


def connect() -> sqlite3.Connection:
    _CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_CONFIG.tool_db_path, isolation_level=None)  # autocommit off via BEGIN
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Without this, contending writers (e.g. parallel run_for_all workers) fail
    # immediately with "database is locked" instead of waiting for the writer
    # lock. WAL serializes writers anyway; busy_timeout just makes them queue.
    # 120s covers worst-case mid-transaction holds (large conflate batches).
    conn.execute("PRAGMA busy_timeout=120000")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def tx(conn: sqlite3.Connection | None = None):
    """Context manager for a transaction. Opens a new connection if none given."""
    own = conn is None
    if own:
        conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        if own:
            conn.close()


def current_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0
    except sqlite3.OperationalError:
        return 0


def migrate() -> None:
    """Apply any pending migrations in migrations/*.sql in numeric order."""
    migrations_dir: Path = _CONFIG.migrations_dir
    conn = connect()
    try:
        applied = current_schema_version(conn)
        files = sorted(migrations_dir.glob("*.sql"))
        for f in files:
            # Filename prefix e.g. "001_init.sql" -> version 1
            prefix = f.stem.split("_", 1)[0]
            try:
                version = int(prefix)
            except ValueError:
                continue
            if version <= applied:
                continue
            sql = f.read_text(encoding="utf-8")
            conn.executescript(sql)
    finally:
        conn.close()
