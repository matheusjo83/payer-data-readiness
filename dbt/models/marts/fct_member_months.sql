-- Member months: one row per member, plan and calendar month of coverage,
-- up to the current month. The standard denominator for payer utilization
-- and cost metrics (e.g. claims per 1,000 member months).
with spans as (
    select
        e.member_id,
        e.plan_id,
        p.line_of_business,
        date_trunc('month', e.coverage_start_date)                           as first_month,
        date_trunc('month', least(coalesce(e.coverage_end_date, current_date), current_date)) as last_month
    from {{ ref('stg_legacy__eligibility') }} e
    left join {{ ref('stg_legacy__plans') }} p using (plan_id)
    where e.coverage_start_date <= current_date
)

select
    member_id,
    plan_id,
    line_of_business,
    unnest(generate_series(first_month, last_month, interval 1 month))::date as coverage_month
from spans
