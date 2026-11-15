-- The runs of the data quality history: when, which commit, which scenario,
-- where the metrics come from, and the run's volume (rows in the lakehouse, or
-- for a run whose metrics come from its reports, the resources it published).
-- A local run has no duration or conclusion: the snapshot is taken during it.
with volume as (
    select
        run_key,
        max(value) filter (where metric = 'legacy_members')   as legacy_members,
        max(value) filter (where metric = 'legacy_claims')    as legacy_claims,
        max(value) filter (where metric = 'synthea_patients') as synthea_patients,
        max(value) filter (where metric = 'pbm_claims')       as pbm_claims,
        sum(value) filter (where metric = 'fhir_resources_published') as fhir_resources_published
    from {{ source('ops', 'quality_metrics') }}
    where area = 'volume'
    group by run_key
),

counts as (
    select run_key, count(*) as metrics, count(distinct area) as areas
    from {{ source('ops', 'quality_metrics') }}
    group by run_key
)

select
    r.run_key,
    r.run_at,
    r.commit,
    left(r.commit, 7)                                 as short_commit,
    r.branch,
    r.scenario,
    r.source,
    r.ci_run_id,
    r.ci_event,
    r.conclusion,
    r.duration_seconds,
    r.metrics_from,
    coalesce(c.metrics, 0)                            as metrics,
    coalesce(c.areas, 0)                              as areas,
    v.legacy_members::bigint                          as legacy_members,
    v.legacy_claims::bigint                           as legacy_claims,
    v.synthea_patients::bigint                        as synthea_patients,
    v.pbm_claims::bigint                              as pbm_claims,
    v.fhir_resources_published::bigint                as fhir_resources_published,
    row_number() over (partition by r.source, r.branch, r.scenario order by r.run_at) as run_number
from {{ source('ops', 'quality_runs') }} r
left join counts c using (run_key)
left join volume v using (run_key)
