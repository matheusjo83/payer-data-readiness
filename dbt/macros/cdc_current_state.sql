{#
  Incremental current state of a legacy table, from its CDC change table
  (bronze.legacy_<table>_changes): one row per primary key, its latest version.

  First build (or --full-refresh): the latest change of each key after the last
  TRUNCATE, as legacy_current_state() computes it over all changes.

  Incremental run: only the changes after the watermark (the highest _lsn in
  the table), latest per key, merged by key (delete+insert). A key whose latest
  change is a delete stays as a tombstone (_op = 'D'), which the staging models
  filter out: removing it would lower the watermark below the last change
  applied, and the watermark is also what the parity test compares up to while
  the loader keeps appending changes.

  Reset: the change history is replaced by a new snapshot (the loader takes one
  when the replication slot is lost) or a TRUNCATE arrived after the watermark.
  The table is then emptied by the pre-hook and rebuilt from the changes after
  the last TRUNCATE. Each row records the snapshot it was built from
  (_snapshot_lsn), which is how a replaced history is noticed even when the new
  snapshot's LSN is lower (a recreated source database starts its LSNs again).
#}

{% macro cdc_current_state_model(table_name, primary_key) -%}
{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key=primary_key,
    on_schema_change='append_new_columns',
    pre_hook="{% if cdc_needs_reset('" ~ table_name ~ "') %}delete from {{ this }}{% endif %}"
) }}
{%- set changes = source('legacy', table_name) -%}
{%- set reset = cdc_needs_reset(table_name) -%}
with changes as (
    select *
    from {{ changes }}
    where _op <> 'T'
      and _lsn > coalesce((select max(_lsn) from {{ changes }} where _op = 'T'), -1)
    {%- if is_incremental() and not reset %}
      and _lsn > (select max(_lsn) from {{ this }})
    {%- endif %}
)

select
    * exclude (_cdc_rn),
    (select max(_lsn) from {{ changes }} where _op = 'S') as _snapshot_lsn
from (
    select
        *,
        row_number() over (
            partition by {{ primary_key | join(', ') }}
            order by _lsn desc, _seq desc
        ) as _cdc_rn
    from changes
)
where _cdc_rn = 1
{%- endmacro %}


{#
  True when the incremental table must be rebuilt: it does not exist yet, it is
  empty, it was built from another snapshot than the one in the change table,
  or a TRUNCATE arrived after its watermark. Evaluated at run time only.
#}
{% macro cdc_needs_reset(table_name) -%}
{%- if not execute or not is_incremental() -%}
  {{ return(false) }}
{%- endif -%}
{%- set changes = source('legacy', table_name) -%}
{%- set row = run_query(
    "select
        (select max(_lsn) from " ~ this ~ ") as watermark,
        (select max(_snapshot_lsn) from " ~ this ~ ") as built_from,
        (select max(_lsn) from " ~ changes ~ " where _op = 'S') as snapshot_lsn,
        (select max(_lsn) from " ~ changes ~ " where _op = 'T') as truncate_lsn"
).rows[0] -%}
{%- set watermark, built_from, snapshot_lsn, truncate_lsn = row[0], row[1], row[2], row[3] -%}
{{ return(watermark is none
          or built_from != snapshot_lsn
          or (truncate_lsn is not none and truncate_lsn > watermark)) }}
{%- endmacro %}
