-- Member dimension: deduplicated demographics plus the member's most recent
-- coverage span and plan.
with latest_coverage as (
    select *
    from {{ ref('stg_legacy__eligibility') }}
    qualify row_number() over (
        partition by member_id
        order by coverage_start_date desc, eligibility_id desc
    ) = 1
)

select
    m.member_id,
    m.first_name,
    m.last_name,
    m.birth_date,
    m.gender,
    m.zip_code,
    m.state,
    m.had_duplicates,
    e.plan_id                                   as current_plan_id,
    p.line_of_business                          as current_line_of_business,
    e.coverage_start_date,
    e.coverage_end_date,
    coalesce(
        e.coverage_start_date <= current_date
        and (e.coverage_end_date is null or e.coverage_end_date >= current_date),
        false
    )                                           as is_currently_covered
from {{ ref('stg_legacy__members') }} m
left join latest_coverage e using (member_id)
left join {{ ref('stg_legacy__plans') }} p on p.plan_id = e.plan_id
