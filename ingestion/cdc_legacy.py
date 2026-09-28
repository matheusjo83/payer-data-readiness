"""Change data capture (CDC) from the legacy Postgres into the bronze layer.

Replaces the Phase 1 full-snapshot loader. Uses Postgres logical decoding
with the wal2json plugin:

1. Snapshot: create a replication slot, then copy every table once. The slot
   is created *before* the copy, so no change committed during the copy is
   lost. A change that is both in the copy and in the slot is replayed on top
   of the copied row; every change carries the full new row, so the replay
   leaves the same final state.
2. Sync: read new changes from the slot and append them to the bronze change
   tables (one per legacy table, append-only). The slot is only advanced
   after DuckDB has committed the batch, so a crash never loses changes, and
   the last applied commit LSN is stored in bronze._cdc_state so a batch is
   never applied twice.

Each change keeps its source commit timestamp (_commit_ts) next to the time it
landed in bronze (_loaded_at). The difference is the source-to-lakehouse
latency (gold.cdc_latency).

Usage:
    python ingestion/cdc_legacy.py sync                  # snapshot if needed, then drain once
    python ingestion/cdc_legacy.py sync --follow -i 5    # keep polling every 5 seconds
    python ingestion/cdc_legacy.py snapshot              # force a new slot and snapshot
    python ingestion/cdc_legacy.py drop-slot             # release the slot (stops WAL retention)
"""

import argparse
import json
import time
from pathlib import Path

import duckdb
import psycopg

from common import connect, log_load, logged_load, new_run_id

PG_DSN = "host=localhost port=5433 dbname=payer_legacy user=legacy password=legacy"
SLOT = "lakehouse_cdc"
TABLES = ["pln", "mbr_mstr", "elig_span", "prv", "clm_hdr", "clm_ln", "pa_req"]
BATCH_FILE = Path("data/cdc_batch.ndjson")
MAX_CHANGES_PER_BATCH = 200_000

WAL2JSON_OPTIONS = [
    "format-version", "2",
    "include-timestamp", "1",      # commit timestamp on every change
    "include-transaction", "1",    # B/C rows: needed to advance at commit boundaries
    "include-lsn", "1",
    "add-tables", ",".join(f"public.{t}" for t in TABLES),
]


def lsn_to_int(lsn: str) -> int:
    """'16/B374D848' -> 64-bit integer, so LSNs can be compared and sorted."""
    hi, lo = lsn.split("/")
    return (int(hi, 16) << 32) + int(lo, 16)


def bronze_table(table: str) -> str:
    return f"bronze.legacy_{table}_changes"


def ensure_state_table(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS bronze._cdc_state (
            slot_name        VARCHAR PRIMARY KEY,
            snapshot_lsn     BIGINT,
            last_commit_lsn  BIGINT,
            updated_at       TIMESTAMPTZ
        )
    """)


def read_state(con):
    ensure_state_table(con)
    row = con.execute(
        "SELECT snapshot_lsn, last_commit_lsn FROM bronze._cdc_state WHERE slot_name = ?", [SLOT]
    ).fetchone()
    return row


def slot_exists(pg) -> bool:
    return pg.execute(
        "SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", [SLOT]
    ).fetchone() is not None


def drop_slot(pg) -> None:
    if slot_exists(pg):
        pg.execute("SELECT pg_drop_replication_slot(%s)", [SLOT])
        print(f"Dropped replication slot {SLOT}.")


def snapshot() -> None:
    """Create a fresh slot and load a full copy of every table as change rows."""
    with psycopg.connect(PG_DSN, autocommit=True) as pg:
        drop_slot(pg)
        lsn = pg.execute(
            "SELECT lsn FROM pg_create_logical_replication_slot(%s, 'wal2json')", [SLOT]
        ).fetchone()[0]
    snapshot_lsn = lsn_to_int(str(lsn))
    print(f"Created replication slot {SLOT} at {lsn}.")

    con = connect()
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(f"ATTACH '{PG_DSN}' AS legacy (TYPE postgres, READ_ONLY)")
    run_id = new_run_id()
    try:
        for table in TABLES:
            target = bronze_table(table)
            # Phase 1 snapshot tables are superseded by the change tables.
            con.execute(f"DROP TABLE IF EXISTS bronze.legacy_{table}")
            with logged_load(con, run_id, f"legacy.snapshot.{table}", target):
                con.execute(f"""
                    CREATE OR REPLACE TABLE {target} AS
                    SELECT *,
                           'S'                          AS _op,
                           {snapshot_lsn}::BIGINT       AS _lsn,
                           NULL::TIMESTAMPTZ            AS _commit_ts,
                           current_timestamp            AS _loaded_at,
                           '{run_id}'                   AS _run_id
                    FROM legacy.public.{table}
                """)
        ensure_state_table(con)
        con.execute("DELETE FROM bronze._cdc_state WHERE slot_name = ?", [SLOT])
        con.execute(
            "INSERT INTO bronze._cdc_state VALUES (?, ?, ?, current_timestamp)",
            [SLOT, snapshot_lsn, snapshot_lsn],
        )
    finally:
        con.execute("DETACH legacy")
        con.close()


def normalize(column: dict):
    """Make wal2json values match what the snapshot reads for the same row.

    wal2json emits CHAR(n) values with their blank padding (' '), while the
    snapshot reads them as text, which Postgres returns without trailing
    spaces (''). Strip the padding so both load paths agree.
    """
    value = column["value"]
    if isinstance(value, str) and column["type"].startswith("character("):
        return value.rstrip(" ")
    return value


def peek_changes(pg, after_lsn: int):
    """Read pending changes without consuming them.

    Returns (changes, advance_to): the parsed changes of complete transactions
    whose commit LSN is after `after_lsn`, and the commit LSN of the last
    transaction read, which the slot can be advanced to once they are stored.
    """
    rows = pg.execute(
        "SELECT lsn::text, data FROM pg_logical_slot_peek_changes(%s, NULL, %s, "
        + ", ".join(["%s"] * len(WAL2JSON_OPTIONS)) + ")",
        [SLOT, MAX_CHANGES_PER_BATCH, *WAL2JSON_OPTIONS],
    ).fetchall()

    changes, pending, advance_to = [], [], None
    for row_lsn, data in rows:
        # parse_float=str keeps NUMERIC values exact (no binary float rounding).
        msg = json.loads(data, parse_float=str)
        action = msg["action"]
        if action == "B":
            pending = []
        elif action == "C":
            commit_lsn = lsn_to_int(row_lsn)
            if commit_lsn > after_lsn:          # skip transactions already applied
                changes.extend(pending)
            advance_to = row_lsn
            pending = []
        elif action in ("I", "U", "D", "T"):
            cols = msg.get("columns") or msg.get("identity") or []
            pending.append({
                "tbl": msg["table"],
                "op": action,
                "lsn": lsn_to_int(msg["lsn"]),
                "commit_ts": msg["timestamp"],
                "payload": {c["name"]: normalize(c) for c in cols},
            })
    return changes, advance_to


def apply_changes(con, changes, commit_lsn: str, run_id: str) -> None:
    """Append a batch of changes to the bronze change tables in one DuckDB transaction.

    The commit LSN of the batch is stored in the same transaction, so either
    both the changes and the new watermark are saved, or neither is.
    """
    BATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with BATCH_FILE.open("w") as f:
        for c in changes:
            f.write(json.dumps(c) + "\n")

    started = time.time()
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _batch AS
        SELECT * FROM read_json('{BATCH_FILE.as_posix()}', columns = {{
            tbl: 'VARCHAR', op: 'VARCHAR', lsn: 'BIGINT',
            commit_ts: 'VARCHAR', payload: 'JSON'
        }})
    """)
    counts = dict(con.execute("SELECT tbl, count(*) FROM _batch GROUP BY tbl").fetchall())
    tables = [t for t in TABLES if t in counts]

    con.execute("BEGIN")
    try:
        for table in tables:
            source_cols = [
                (name, dtype) for name, dtype in con.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'bronze' AND table_name = ? ORDER BY ordinal_position",
                    [f"legacy_{table}_changes"],
                ).fetchall()
                if not name.startswith("_")
            ]
            # Deletes only carry the primary key; the other columns become NULL.
            select_cols = ",\n".join(
                f"CAST(payload ->> '{name}' AS {dtype}) AS {name}" for name, dtype in source_cols
            )
            con.execute(f"""
                INSERT INTO {bronze_table(table)} BY NAME
                SELECT {select_cols},
                       op                        AS _op,
                       lsn                       AS _lsn,
                       commit_ts::TIMESTAMPTZ    AS _commit_ts,
                       current_timestamp         AS _loaded_at,
                       '{run_id}'                AS _run_id
                FROM _batch
                WHERE tbl = '{table}'
                ORDER BY lsn
            """)
        con.execute(
            "UPDATE bronze._cdc_state SET last_commit_lsn = ?, updated_at = current_timestamp "
            "WHERE slot_name = ?",
            [lsn_to_int(commit_lsn), SLOT],
        )
        con.execute("COMMIT")
        status = "success"
    except Exception:
        con.execute("ROLLBACK")
        status = "failed"
        raise
    finally:
        BATCH_FILE.unlink(missing_ok=True)
        # Logged outside the transaction so that failures are recorded too.
        finished = time.time()
        for table in tables:
            log_load(con, run_id, f"legacy.cdc.{table}", bronze_table(table),
                     counts[table] if status == "success" else None, started, finished, status)


def sync_once(pg) -> int:
    """Drain the slot once. Returns the number of changes applied."""
    con = connect()
    try:
        state = read_state(con)
    finally:
        con.close()
    last_lsn = state[1]

    total = 0
    while True:
        changes, advance_to = peek_changes(pg, last_lsn)
        if advance_to is None:
            break
        if changes:
            # Open DuckDB only while writing, so dbt can use the file between polls.
            con = connect()
            try:
                apply_changes(con, changes, advance_to, new_run_id())
            finally:
                con.close()
            total += len(changes)
        last_lsn = lsn_to_int(advance_to)
        # Only now is it safe to let Postgres release the WAL for this batch.
        pg.execute("SELECT pg_replication_slot_advance(%s, %s::pg_lsn)", [SLOT, advance_to])
    return total


def sync(follow: bool, interval: float) -> None:
    con = connect()
    try:
        state = read_state(con)
    finally:
        con.close()
    with psycopg.connect(PG_DSN, autocommit=True) as pg:
        needs_snapshot = state is None or not slot_exists(pg)
    if needs_snapshot:
        print("No active CDC slot/state found: taking an initial snapshot.")
        snapshot()

    with psycopg.connect(PG_DSN, autocommit=True) as pg:
        while True:
            try:
                n = sync_once(pg)
                if n or not follow:
                    print(f"Applied {n} changes.")
            except duckdb.IOException as exc:
                # The lakehouse file is locked (e.g. dbt is running). Nothing was
                # committed and the slot was not advanced; retry on the next poll.
                if not follow:
                    raise
                print(f"Lakehouse busy, retrying in {interval}s: {exc}")
            if not follow:
                break
            time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sync", help="snapshot if needed, then apply pending changes")
    s.add_argument("--follow", action="store_true", help="keep polling for changes")
    s.add_argument("-i", "--interval", type=float, default=5.0, help="seconds between polls")
    sub.add_parser("snapshot", help="drop the slot and take a new full snapshot")
    sub.add_parser("drop-slot", help="drop the replication slot")
    args = ap.parse_args()

    if args.command == "sync":
        try:
            sync(args.follow, args.interval)
        except KeyboardInterrupt:
            print("Stopped.")
    elif args.command == "snapshot":
        snapshot()
    elif args.command == "drop-slot":
        with psycopg.connect(PG_DSN, autocommit=True) as pg:
            drop_slot(pg)


if __name__ == "__main__":
    main()
