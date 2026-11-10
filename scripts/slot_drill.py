"""Drill: prove that the replication slot monitoring catches a stalled loader.

With the CDC loader stopped, write WAL in the legacy database until the slot
goes past its retention cap (max_slot_wal_keep_size, 1GB in docker-compose.yml)
and Postgres invalidates it. Along the way, check that:

1. `cdc_legacy.py slot-status` passes on a healthy slot,
2. it fails once the retained WAL passes its threshold, while Postgres still
   reports the slot as reserved (the alert comes before any damage),
3. it fails once the slot is lost,
4. the next `cdc_legacy.py sync` records the loss in bronze._cdc_slot_status
   and takes a new snapshot,
5. `slot-status` passes again.

The WAL comes from pg_logical_emit_message (no table is touched) and
pg_switch_wal (each switch moves to a new 16 MB segment), so the legacy data
does not change; the new snapshot copies the same rows. It does replace the
bronze change tables, so any CDC history in them (gold.cdc_latency) is gone.
Writes a summary to data/slot_drill.json. Run it after `make ingest`, before
`make dbt`, and before any `make cdc` demo whose latency you want to keep.

Requires a superuser connection (pg_switch_wal), as the local setup provides.
"""

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingestion"))

from cdc_legacy import DEFAULT_MAX_RETAINED_MB, PG_DSN, slot_status  # noqa: E402
from common import LAKEHOUSE_PATH  # noqa: E402

SUMMARY = ROOT / "data" / "slot_drill.json"
MAX_SWITCHES = 160  # 160 x 16 MB = 2.5 GB, well past the 1 GB cap


def run(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(ROOT / "ingestion" / "cdc_legacy.py"), *args],
        cwd=ROOT, capture_output=True, text=True,
    )
    print(f"$ cdc_legacy.py {' '.join(args)} -> exit {result.returncode}")
    print("  " + result.stdout.strip().replace("\n", "\n  "))
    return result


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"DRILL FAILED: {message}")
    print(f"ok: {message}")


def mb(value) -> float | None:
    return None if value is None else round(value / 2**20, 1)


def main() -> None:
    summary = {"threshold_mb": DEFAULT_MAX_RETAINED_MB}

    with psycopg.connect(PG_DSN, autocommit=True) as pg:
        cap = pg.execute("SHOW max_slot_wal_keep_size").fetchone()[0]
        summary["max_slot_wal_keep_size"] = cap
        expect(cap not in ("-1", ""), f"the WAL retention cap is set ({cap})")
        expect(slot_status(pg) is not None, "the CDC slot exists (run make ingest first)")

        expect(run("slot-status").returncode == 0, "slot-status passes on a healthy slot")

        alerted_at = None
        lost_after = None
        for i in range(1, MAX_SWITCHES + 1):
            pg.execute("SELECT pg_logical_emit_message(false, 'slot_drill', 'x')")
            pg.execute("SELECT pg_switch_wal()")
            status = slot_status(pg)

            if alerted_at is None and (status["retained_bytes"] or 0) > DEFAULT_MAX_RETAINED_MB * 2**20:
                alerted_at = {"switches": i, "retained_mb": mb(status["retained_bytes"]),
                              "wal_status": status["wal_status"]}
                result = run("slot-status")
                expect(result.returncode == 1, "slot-status fails above the retained WAL threshold")
                expect(status["wal_status"] in ("reserved", "extended"),
                       f"the alert comes while the slot is still usable ({status['wal_status']})")

            # Slots are invalidated at checkpoints.
            if i % 8 == 0:
                pg.execute("CHECKPOINT")
                if slot_status(pg)["wal_status"] == "lost":
                    lost_after = i
                    break

        expect(alerted_at is not None, "the threshold alert fired")
        expect(lost_after is not None, f"the slot was lost within {MAX_SWITCHES} WAL segments")
        summary["alert"] = alerted_at
        summary["lost_after_switches"] = lost_after

    expect(run("slot-status").returncode == 1, "slot-status fails on a lost slot")

    result = run("sync")
    expect(result.returncode == 0, "sync recovers from a lost slot")
    expect("invalidated" in result.stdout, "sync reports the loss and takes a new snapshot")

    with duckdb.connect(str(ROOT / LAKEHOUSE_PATH), read_only=True) as con:
        losses = con.execute(
            "SELECT count(*) FROM bronze._cdc_slot_status "
            "WHERE action = 'resnapshot' AND wal_status = 'lost'"
        ).fetchone()[0]
    expect(losses >= 1, "the loss is recorded in bronze._cdc_slot_status")

    expect(run("slot-status").returncode == 0, "slot-status passes again after the new snapshot")

    summary["result"] = "passed"
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
