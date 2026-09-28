"""Shared helpers for the bronze-layer loaders."""

import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import duckdb

LAKEHOUSE_PATH = Path("data/lakehouse.duckdb")


def connect() -> duckdb.DuckDBPyConnection:
    LAKEHOUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(LAKEHOUSE_PATH))
    con.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    con.execute("""
        CREATE TABLE IF NOT EXISTS bronze._load_log (
            run_id      VARCHAR,
            source      VARCHAR,
            target      VARCHAR,
            row_count   BIGINT,
            started_at  TIMESTAMP,
            finished_at TIMESTAMP,
            seconds     DOUBLE,
            status      VARCHAR
        )
    """)
    return con


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


@contextmanager
def logged_load(con, run_id: str, source: str, target: str):
    """Time a load step and record it in bronze._load_log (success or failure).

    These records are the raw material for the reliability metrics
    (load success rate, duration, volume).
    """
    started = time.time()
    status = "success"
    try:
        yield
    except Exception:
        status = "failed"
        raise
    finally:
        finished = time.time()
        row_count = None
        if status == "success":
            row_count = con.execute(f"SELECT count(*) FROM {target}").fetchone()[0]
        con.execute(
            "INSERT INTO bronze._load_log VALUES (?, ?, ?, ?, to_timestamp(?), to_timestamp(?), ?, ?)",
            [run_id, source, target, row_count, started, finished, finished - started, status],
        )
        print(f"[{status}] {source} -> {target} ({row_count} rows, {finished - started:.2f}s)")
