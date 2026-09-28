"""Full load of the legacy Postgres tables into the bronze layer.

Phase 1: full snapshot load via DuckDB's postgres extension.
Phase 2 (roadmap): replace with change data capture (CDC) and measure
source-to-lakehouse latency.
"""

from common import connect, logged_load, new_run_id

PG_DSN = "host=localhost port=5433 dbname=payer_legacy user=legacy password=legacy"
TABLES = ["pln", "mbr_mstr", "elig_span", "prv", "clm_hdr", "clm_ln", "pa_req"]


def main() -> None:
    con = connect()
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(f"ATTACH '{PG_DSN}' AS legacy (TYPE postgres, READ_ONLY)")
    run_id = new_run_id()
    try:
        for table in TABLES:
            target = f"bronze.legacy_{table}"
            with logged_load(con, run_id, f"legacy.{table}", target):
                con.execute(f"""
                    CREATE OR REPLACE TABLE {target} AS
                    SELECT *, current_timestamp AS _loaded_at, '{run_id}' AS _run_id
                    FROM legacy.public.{table}
                """)
    finally:
        con.execute("DETACH legacy")
        con.close()


if __name__ == "__main__":
    main()
