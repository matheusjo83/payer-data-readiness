-- Warns when the CDC replication slot was ever lost (Postgres removed WAL the
-- loader had not read, and the loader took a new snapshot) or is now beyond
-- the WAL retention cap. A warning, not an error: the loader recovers on its
-- own, and the alert that needs to arrive in time is `make slot-check` (the
-- cdc_slot_monitor DAG), which does not wait for a dbt run.
{{ config(severity='warn') }}

select *
from {{ ref('cdc_slot_lag') }}
where slot_losses > 0
   or last_wal_status in ('unreserved', 'lost')
