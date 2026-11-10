-- Replication slot lag over time, from the observations the CDC loader records
-- before each sync (bronze._cdc_slot_status). Retained WAL is what Postgres keeps
-- for the slot, and what the cap (max_slot_wal_keep_size) measures; pending WAL
-- is the backlog written since the last change the loader confirmed. A lost slot
-- has no retained WAL (NULL), and the loader records it as a 'resnapshot'.
with observations as (
    select
        *,
        date_diff(
            'millisecond',
            lag(observed_at) over (partition by slot_name order by observed_at),
            observed_at
        ) / 1000.0 as seconds_since_previous
    from {{ source('ops', 'cdc_slot_status') }}
),

latest as (
    select slot_name, wal_status, retained_bytes, pending_bytes
    from observations
    qualify row_number() over (partition by slot_name order by observed_at desc) = 1
)

select
    o.slot_name,
    count(*)                                                 as observations,
    count(*) filter (where o.action = 'resnapshot')          as slot_losses,
    min(o.observed_at)                                       as first_observed_at,
    max(o.observed_at)                                       as last_observed_at,
    round(max(o.seconds_since_previous), 1)                  as max_seconds_between_syncs,
    -- Backlog at the start of each sync; the observation of a lost slot is left
    -- out, since that WAL can no longer be read.
    round(quantile_cont(o.pending_bytes, 0.95) filter (where o.action = 'sync') / 2^20, 2)
                                                             as p95_pending_mb,
    round(max(o.pending_bytes) filter (where o.action = 'sync') / 2^20, 2)
                                                             as max_pending_mb,
    round(max(o.retained_bytes) / 2^20, 2)                   as max_retained_mb,
    any_value(l.wal_status)                                  as last_wal_status,
    round(any_value(l.retained_bytes) / 2^20, 2)             as last_retained_mb
from observations o
join latest l using (slot_name)
group by o.slot_name
