"""Load the PBM feed (pipe-delimited files in data/pbm/) into the bronze layer.

Each file becomes one bronze table with every column as text, as received.
Parsing and typing happen downstream (dbt staging models). The ground-truth
file is loaded as bronze.pbm_truth for evaluation only: no matching model reads
it, only gold.identity_match_quality.
"""

from pathlib import Path

from common import connect, logged_load, new_run_id

PBM_DIR = Path("data/pbm")
FILES = {
    "pbm_members.txt": "bronze.pbm_members",
    "pbm_pharmacies.txt": "bronze.pbm_pharmacies",
    "pbm_claims.txt": "bronze.pbm_claims",
    "_truth.txt": "bronze.pbm_truth",
}


def main() -> None:
    missing = [name for name in FILES if not (PBM_DIR / name).exists()]
    if missing:
        raise SystemExit(f"Missing PBM files in {PBM_DIR}: {missing}. Run `make pbm` first.")
    con = connect()
    run_id = new_run_id()
    try:
        for name, target in FILES.items():
            path = PBM_DIR / name
            with logged_load(con, run_id, f"pbm.{path.stem.lstrip('_')}", target):
                con.execute(f"""
                    CREATE OR REPLACE TABLE {target} AS
                    SELECT *,
                           '{name}'            AS _source_file,
                           current_timestamp   AS _loaded_at,
                           '{run_id}'          AS _run_id
                    FROM read_csv('{path.as_posix()}', delim = '|', header = true, all_varchar = true)
                """)
    finally:
        con.close()


if __name__ == "__main__":
    main()
