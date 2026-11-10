"""Orchestration of the payer lakehouse pipeline.

legacy_cdc_refresh      every 30 minutes: apply legacy changes (CDC), rebuild silver/gold
lakehouse_full_refresh  on demand: CDC, FHIR, PBM and terminology loads, ViewDefinition check, rebuild,
                        coverage report
cdc_slot_monitor        every 5 minutes: check replication slot lag (fails = alert)

Every task that writes the DuckDB lakehouse runs in the `lakehouse` pool (1 slot),
because DuckDB allows a single writer: tasks from both DAGs queue instead of
failing on a locked file. The slot monitor reads only Postgres, so it runs
outside that pool and keeps alerting when the lakehouse is stuck, which is
when the slot lag grows.
"""

from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

PROJECT = "/opt/project"
PY = "/opt/pipeline-venv/bin/python"
DBT = "/opt/pipeline-venv/bin/dbt"

default_args = {
    "owner": "data-platform",
    "retries": 2,
    "retry_delay": timedelta(minutes=1),
    "pool": "lakehouse",
}


def cdc_sync() -> BashOperator:
    return BashOperator(task_id="cdc_sync", bash_command=f"{PY} ingestion/cdc_legacy.py sync", cwd=PROJECT)


def dbt_build() -> BashOperator:
    # Tests with severity warn (known legacy issues) do not fail the task; errors do.
    return BashOperator(task_id="dbt_build", bash_command=f"{DBT} build --profiles-dir .", cwd=f"{PROJECT}/dbt")


with DAG(
    dag_id="legacy_cdc_refresh",
    description="Apply legacy database changes via CDC and rebuild silver/gold",
    schedule=timedelta(minutes=30),
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["payer", "cdc", "dbt"],
):
    cdc_sync() >> dbt_build()


with DAG(
    dag_id="lakehouse_full_refresh",
    description="CDC sync, FHIR, PBM and terminology loads, ViewDefinition check, dbt build and coverage report",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["payer", "cdc", "fhir", "dbt"],
):
    load_fhir = BashOperator(task_id="load_fhir", bash_command=f"{PY} ingestion/load_fhir.py", cwd=PROJECT)
    load_pbm = BashOperator(task_id="load_pbm", bash_command=f"{PY} ingestion/load_pbm.py", cwd=PROJECT)
    # Code system releases (ICD-10-CM, NDC), cached in data/terminology/ after the first download.
    load_terminology = BashOperator(
        task_id="load_terminology", bash_command=f"{PY} ingestion/load_terminology.py", cwd=PROJECT
    )
    # Fails when a ViewDefinition changed but its dbt macro was not regenerated (make views).
    views_check = BashOperator(
        task_id="views_check", bash_command=f"{PY} fhir/build_views.py --check", cwd=PROJECT, pool="default_pool"
    )
    coverage = BashOperator(
        task_id="dbt_coverage",
        bash_command=f"cd dbt && {DBT} docs generate --profiles-dir . && cd .. && {PY} scripts/dbt_coverage.py",
        cwd=PROJECT,
    )
    cdc_sync() >> load_fhir >> load_pbm >> load_terminology >> views_check >> dbt_build() >> coverage


with DAG(
    dag_id="cdc_slot_monitor",
    description="Check replication slot lag on the legacy database; a failed run is the alert",
    schedule=timedelta(minutes=5),
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    # No retries: a failure should show at once, and the next run is 5 minutes away.
    default_args={"owner": "data-platform", "retries": 0},
    tags=["payer", "cdc", "monitoring"],
):
    BashOperator(task_id="slot_check", bash_command=f"{PY} ingestion/cdc_legacy.py slot-status", cwd=PROJECT)
