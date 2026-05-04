"""
Postgres connection helper. Uses the same WAREHOUSE_DATABASE_URL the backend uses.

This is a thin wrapper around psycopg2.connect with sane defaults for cloud
databases (keepalives) so connections stay alive across long-running ingestion
streams.
"""
from contextlib import contextmanager
from typing import Iterator

import psycopg2
from psycopg2.extras import RealDictCursor

from config import get_database_url


def get_connection():
    """Open a fresh connection. Caller must close()."""
    conn = psycopg2.connect(
        get_database_url(),
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    conn.set_client_encoding("UTF8")
    return conn


@contextmanager
def cursor(commit: bool = False, dict_rows: bool = False) -> Iterator:
    """
    Convenience context manager:
        with cursor() as cur: cur.execute(...)
    Closes connection on exit. Set commit=True for write operations.
    """
    conn = get_connection()
    try:
        kwargs = {"cursor_factory": RealDictCursor} if dict_rows else {}
        with conn.cursor(**kwargs) as cur:
            yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()


def init_db():
    """Apply migrations.sql idempotently (CREATE TABLE IF NOT EXISTS)."""
    from pathlib import Path

    sql_path = Path(__file__).parent / "migrations.sql"
    sql = sql_path.read_text(encoding="utf-8")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("Migrations applied successfully.")
    finally:
        conn.close()
