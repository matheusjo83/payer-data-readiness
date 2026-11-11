"""Connection to the lakehouse: a DuckLake catalog in Postgres, data in Parquet.

DuckLake is DuckDB's lakehouse format. The catalog (schemas, tables, snapshots
and the list of data files) lives in a Postgres database, the lakehouse-catalog
service; the data is Parquet files under data/lakehouse/. Every process attaches
the same catalog, so several can read and write at once: DuckLake commits each
transaction against the catalog and rejects only a conflicting one (two writes
to the same table at the same moment, for example). It is not Iceberg or Delta
Lake: other engines cannot read the tables through those formats' catalogs.

The catalog records data files relative to the data path, so each process
attaches with its own absolute path to data/lakehouse/ (the host, dbt and the
Airflow container see the repository at different places).

    LAKEHOUSE_CATALOG     libpq connection string of the catalog
                          (default: the lakehouse-catalog service on localhost:5434)
    LAKEHOUSE_DATA_PATH   directory of the Parquet files (default: data/lakehouse/)
"""

import os
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
ALIAS = "lakehouse"
CATALOG = os.environ.get(
    "LAKEHOUSE_CATALOG", "host=localhost port=5434 dbname=lakehouse user=lakehouse password=lakehouse")
DATA_PATH = Path(os.environ.get("LAKEHOUSE_DATA_PATH", ROOT / "data" / "lakehouse")).resolve()


def attach(con: duckdb.DuckDBPyConnection, read_only: bool = False) -> None:
    """Attach the lakehouse as `lakehouse` and make it the default catalog."""
    for extension in ("ducklake", "postgres"):
        con.execute(f"INSTALL {extension}")
        con.execute(f"LOAD {extension}")
    options = [f"DATA_PATH '{DATA_PATH.as_posix()}/'", "OVERRIDE_DATA_PATH true"]
    if read_only:
        options.append("READ_ONLY")
    con.execute(f"ATTACH 'ducklake:postgres:{CATALOG}' AS {ALIAS} ({', '.join(options)})")
    con.execute(f"USE {ALIAS}")


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if not read_only:
        DATA_PATH.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    attach(con, read_only)
    return con
