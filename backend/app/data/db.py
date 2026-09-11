"""SQLite connection management for the data plane.

Deliberately thin: the service layer never imports this module directly, it
goes through `repository.Repository`. Swapping in Postgres means writing a new
repository implementation, not touching callers.
"""
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from ..config import settings

_SCHEMA = Path(__file__).with_name("schema.sql")
_local = threading.local()


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    # WAL lets the poller write while the API reads, which is the whole
    # concurrency story for a single-node deployment.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """A fresh connection. Caller owns closing it."""
    conn = sqlite3.connect(str(path or settings.db_path), timeout=30.0)
    _configure(conn)
    return conn


def get_connection() -> sqlite3.Connection:
    """Thread-local connection, since sqlite3 objects are not thread safe and
    FastAPI runs sync endpoints in a worker threadpool."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect()
        _local.conn = conn
    return conn


def init_db(path: Optional[Path] = None) -> None:
    # main.py calls this unconditionally at startup; dispatch on the backend so
    # a Postgres deployment applies schema_postgres.sql instead. `path` only
    # ever means something for SQLite.
    if settings.db_backend == "postgres":
        from . import pg
        pg.init_db()
        return
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA.read_text())
        conn.commit()
    finally:
        conn.close()
