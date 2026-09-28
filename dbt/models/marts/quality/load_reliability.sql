-- Reliability metrics from the bronze load log: success rate, volume, duration.
select
    source,
    count(*)                                             as runs,
    count(*) filter (where status = 'success')           as successful_runs,
    round(100.0 * count(*) filter (where status = 'success') / count(*), 1) as success_rate_pct,
    max(row_count)                                       as last_max_rows,
    round(avg(seconds), 2)                               as avg_seconds,
    max(finished_at)                                     as last_load_at
from {{ source('ops', 'load_log') }}
group by source
