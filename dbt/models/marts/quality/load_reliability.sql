-- Reliability metrics from the bronze load log: success rate, volume and duration.
-- Sources are named <system>.<load type>.<table>, e.g. legacy.snapshot.clm_hdr
-- (full copy) or legacy.cdc.clm_hdr (captured changes).
select
    source,
    count(*)                                             as runs,
    count(*) filter (where status = 'success')           as successful_runs,
    round(100.0 * count(*) filter (where status = 'success') / count(*), 1) as success_rate_pct,
    sum(row_count)                                       as total_rows,
    max(row_count)                                       as last_max_rows,
    round(avg(seconds), 2)                               as avg_seconds,
    max(finished_at)                                     as last_load_at
from {{ source('ops', 'load_log') }}
group by source
