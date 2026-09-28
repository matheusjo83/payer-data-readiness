-- Source-to-lakehouse latency of CDC changes: time between the commit in the
-- legacy database (_commit_ts) and the arrival of the change in bronze
-- (_loaded_at). Snapshot rows (_op = 'S') have no commit time and are excluded.
{% set tables = ['pln', 'mbr_mstr', 'elig_span', 'prv', 'clm_hdr', 'clm_ln', 'pa_req'] %}

with changes as (
    {% for t in tables %}
    select
        '{{ t }}' as source_table,
        _op,
        _commit_ts,
        _loaded_at,
        date_diff('millisecond', _commit_ts, _loaded_at) / 1000.0 as latency_seconds
    from {{ source('legacy', t) }}
    where _op <> 'S'
    {% if not loop.last %}union all{% endif %}
    {% endfor %}
)

select
    source_table,
    count(*)                                         as changes,
    count(*) filter (where _op = 'I')                as inserts,
    count(*) filter (where _op = 'U')                as updates,
    count(*) filter (where _op = 'D')                as deletes,
    count(*) filter (where _op = 'T')                as truncates,
    round(quantile_cont(latency_seconds, 0.50), 2)   as p50_latency_seconds,
    round(quantile_cont(latency_seconds, 0.95), 2)   as p95_latency_seconds,
    round(max(latency_seconds), 2)                   as max_latency_seconds,
    max(_commit_ts)                                  as last_commit_at,
    max(_loaded_at)                                  as last_loaded_at
from changes
group by source_table
