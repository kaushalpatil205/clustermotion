"""Database helpers for orders-svc and order-sweeper."""
import os
import time
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

SCHEMA = Path(__file__).with_name("schema.sql").read_text()
SCHEMA_LOCK_ID = 4242


def dsn() -> str:
    return make_conninfo(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "shop"),
        user=os.getenv("DB_USER", "shop"),
        password=os.getenv("DB_PASSWORD", "shop"),
        connect_timeout=3,
        application_name=f"orders-{os.getenv('CLUSTER_NAME', 'local')}",
    )


def make_pool() -> ConnectionPool:
    # max_lifetime keeps connections short-lived so DNS changes are picked up
    # even if no error forces a reconnect.
    return ConnectionPool(
        conninfo=dsn(),
        min_size=1,
        max_size=int(os.getenv("DB_POOL_SIZE", "10")),
        timeout=5,
        max_lifetime=60,
        check=ConnectionPool.check_connection,
        open=False,
    )


def ensure_schema() -> None:
    """Create tables if missing. The advisory lock serialises concurrent pods."""
    with psycopg.connect(dsn()) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
        conn.execute(SCHEMA)


def wait_for_schema(retries: int = 60, delay: float = 3.0) -> None:
    for _ in range(retries):
        try:
            ensure_schema()
            return
        except (psycopg.OperationalError, psycopg.errors.ReadOnlySqlTransaction):
            time.sleep(delay)
    raise RuntimeError("database not reachable")
