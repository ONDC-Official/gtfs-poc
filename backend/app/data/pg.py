"""PostgreSQL + PostGIS connection management for the data plane.

Parallel to db.py, selected by DB_BACKEND=postgres. A connection pool replaces
SQLite's single-file handle: Postgres serialises nothing at our level, so the
poller and the API reads run concurrently instead of contending for a lock.

The service layer never imports this - it goes through Repository, same as with
SQLite. Only postgres_repo.py and the init dispatch in db.py touch it.
"""
import logging
from pathlib import Path
from typing import Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..config import settings

log = logging.getLogger("gtfs.pg")

_SCHEMA = Path(__file__).with_name("schema_postgres.sql")
_pool: Optional[ConnectionPool] = None


def pool() -> ConnectionPool:
    """The process-wide pool, opened lazily on first use.

    autocommit is on: almost every repository method is one statement, and the
    few that must be atomic (upsert_vehicles, fold_rollups, prune_history) wrap
    themselves in `with conn.transaction()`, which issues an explicit
    BEGIN/COMMIT even under autocommit.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=settings.pg_pool_max,
            kwargs={"row_factory": dict_row, "autocommit": True},
            name="gtfs-pg",
            open=True,
        )
        log.info("postgres pool open | max=%s", settings.pg_pool_max)
    return _pool


def init_db() -> None:
    """Apply schema_postgres.sql. Idempotent - the script is IF-NOT-EXISTS
    throughout. Passing no parameters makes psycopg use the simple query
    protocol, which is what allows the multi-statement script (and the
    dollar-quoted function bodies) to run in one call."""
    with pool().connection() as conn:
        conn.execute(_SCHEMA.read_text())
    log.info("postgres schema ready")


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
