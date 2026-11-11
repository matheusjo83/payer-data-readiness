"""Reconcile the lakehouse's current state of each legacy table with Postgres.

Compares every legacy table in Postgres with its incremental current state in
the lakehouse (silver.cdc_current__<table>), row by row in both directions, on
the source columns. Run it after a CDC sync and a dbt build, with no changes in
flight (make cdc-check does all three). Writes data/cdc_reconcile.json and exits
with status 1 if a table differs.

Usage (from the repository root):
    python scripts/cdc_reconcile.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingestion"))

import lakehouse  # noqa: E402
from cdc_legacy import PG_DSN, TABLES  # noqa: E402

REPORT = ROOT / "data" / "cdc_reconcile.json"


def main() -> None:
    con = lakehouse.connect(read_only=True)
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(f"ATTACH '{PG_DSN}' AS legacy (TYPE postgres, READ_ONLY)")
    results = []
    try:
        for table in TABLES:
            columns = [r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_catalog = 'legacy' AND table_schema = 'public' AND table_name = ? "
                "ORDER BY ordinal_position", [table]).fetchall()]
            cols = ", ".join(columns)
            source = f"SELECT {cols} FROM legacy.public.{table}"
            current = f"SELECT {cols} FROM {lakehouse.ALIAS}.silver.cdc_current__{table} WHERE _op <> 'D'"
            only_source, only_lakehouse, rows = con.execute(f"""
                SELECT (SELECT count(*) FROM (({source}) EXCEPT ALL ({current}))),
                       (SELECT count(*) FROM (({current}) EXCEPT ALL ({source}))),
                       (SELECT count(*) FROM legacy.public.{table})
            """).fetchone()
            results.append({"table": table, "source_rows": rows, "only_in_source": only_source,
                            "only_in_lakehouse": only_lakehouse})
            print(f"{table:10} {rows:>7} rows   only in source {only_source:>4}   "
                  f"only in lakehouse {only_lakehouse:>4}")
    finally:
        con.execute("DETACH legacy")
        con.close()
    REPORT.write_text(json.dumps(results, indent=2))
    if any(r["only_in_source"] or r["only_in_lakehouse"] for r in results):
        raise SystemExit("The lakehouse current state differs from Postgres.")
    print(f"Every table matches Postgres row for row. Written to {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
