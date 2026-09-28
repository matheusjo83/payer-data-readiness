"""Load Synthea FHIR bulk NDJSON files into the bronze layer.

Each resource type (Patient.ndjson, Claim.ndjson, ...) becomes one bronze
table holding the raw resource as JSON. Flattening happens downstream
(dbt staging models and SQL on FHIR ViewDefinitions).
"""

import re
from pathlib import Path

from common import connect, logged_load, new_run_id

FHIR_DIR = Path("data/synthea/fhir")


def to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def main() -> None:
    files = sorted(FHIR_DIR.glob("*.ndjson"))
    if not files:
        raise SystemExit(f"No NDJSON files in {FHIR_DIR}. Run `make synthea` first.")
    con = connect()
    run_id = new_run_id()
    try:
        for path in files:
            resource_type = path.stem
            target = f"bronze.fhir_{to_snake(resource_type)}"
            with logged_load(con, run_id, f"synthea.{resource_type}", target):
                con.execute(f"""
                    CREATE OR REPLACE TABLE {target} AS
                    SELECT
                        json                AS resource,
                        '{resource_type}'   AS resource_type,
                        '{path.name}'       AS _source_file,
                        current_timestamp   AS _loaded_at,
                        '{run_id}'          AS _run_id
                    FROM read_ndjson_objects('{path.as_posix()}')
                """)
    finally:
        con.close()


if __name__ == "__main__":
    main()
