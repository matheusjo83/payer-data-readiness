-- Every metric of every run in the data quality history, next to the value the
-- same metric had in the previous run of its series. A series is one source
-- (ci or local), branch and scenario: the CI runs of main form one, each
-- scale of the benchmark another. A metric the previous run did not have (it
-- came from its reports only, or the check did not exist yet) has no previous
-- value.
with metrics as (
    select
        m.run_key,
        r.run_at,
        r.short_commit,
        r.source,
        r.branch,
        r.scenario,
        r.metrics_from,
        m.area,
        m.metric,
        m.dimension,
        m.value
    from {{ source('ops', 'quality_metrics') }} m
    join {{ ref('quality_runs') }} r using (run_key)
),

-- The previous run of each series, whether or not it had the metric.
runs as (
    select
        run_key,
        lag(run_key) over (partition by source, branch, scenario order by run_at) as previous_run_key
    from {{ ref('quality_runs') }}
)

select
    m.*,
    runs.previous_run_key,
    p.value                as previous_value,
    m.value - p.value      as change
from metrics m
join runs using (run_key)
left join {{ source('ops', 'quality_metrics') }} p
    on p.run_key = runs.previous_run_key
   and p.area = m.area and p.metric = m.metric and p.dimension = m.dimension
