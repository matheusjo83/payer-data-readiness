{#
  Rebuild the current state of a legacy table from its CDC change table
  (bronze.legacy_<table>_changes): the latest version of each primary key,
  ignoring everything before the last TRUNCATE and dropping deleted keys.
  Changes are ordered by log position (_lsn) and, for rows written by the same
  bulk WAL record, by their position in the transaction (_seq).
  Returns a parenthesized subquery with the source columns plus the
  _op/_lsn/_seq/_commit_ts/_loaded_at/_run_id metadata of the latest version.
#}
{% macro legacy_current_state(table_name, primary_key) -%}
{%- set changes = source('legacy', table_name) -%}
(
    select * exclude (_cdc_rn)
    from (
        select
            *,
            row_number() over (
                partition by {{ primary_key | join(', ') }}
                order by _lsn desc, _seq desc
            ) as _cdc_rn
        from {{ changes }}
        where _op <> 'T'
          and _lsn > coalesce((select max(_lsn) from {{ changes }} where _op = 'T'), -1)
    )
    where _cdc_rn = 1
      and _op <> 'D'
)
{%- endmacro %}
