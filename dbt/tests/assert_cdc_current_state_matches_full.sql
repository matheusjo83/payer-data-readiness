-- The incremental current state of each legacy table (cdc_current__<table>,
-- without its tombstones) must equal a full rebuild from its changes
-- (legacy_current_state), row by row, in both directions. The rebuild stops at
-- the last change the incremental state applied (its highest _lsn), so changes
-- the CDC loader appends while dbt runs do not count against it. Fails with the
-- table and the number of rows found on only one side.
{% set tables = {
    'pln': ['pln_id'], 'mbr_mstr': ['mbr_sk'], 'elig_span': ['elig_sk'], 'prv': ['prv_id'],
    'clm_hdr': ['clm_id'], 'clm_ln': ['clm_id', 'ln_nbr'], 'pa_req': ['pa_id'],
} %}
select * from (
{% for table, pk in tables.items() %}
{% set current = ref('cdc_current__' ~ table) %}
{% set incremental = "select * exclude (_snapshot_lsn) from " ~ current ~ " where _op <> 'D'" %}
{% set full = "select * from " ~ legacy_current_state(table, pk, "(select max(_lsn) from " ~ current ~ ")") %}
select '{{ table }}' as legacy_table,
       (select count(*) from (({{ incremental }}) except all ({{ full }}))) as only_incremental,
       (select count(*) from (({{ full }}) except all ({{ incremental }}))) as only_full
{% if not loop.last %}union all{% endif %}
{% endfor %}
) where only_incremental > 0 or only_full > 0
