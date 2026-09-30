-- PBM pharmacy claims with the resolved member (gold.member_xref) and the
-- coverage span in effect on the fill date. Only claims with a linked member and
-- coverage are exported to FHIR; the rest stay here, flagged.
with covering_span as (
    select
        c.rx_claim_id,
        min(e.eligibility_id) as eligibility_id,
        min(e.plan_id)        as plan_id
    from {{ ref('stg_pbm__claims') }} c
    join {{ ref('member_xref') }} x using (cardholder_id)
    join {{ ref('stg_legacy__eligibility') }} e
      on e.member_id = x.member_id
     and c.fill_date >= e.coverage_start_date
     and (e.coverage_end_date is null or c.fill_date <= e.coverage_end_date)
    group by c.rx_claim_id
)

select
    c.*,
    x.member_id,
    x.status                        as identity_status,
    s.eligibility_id,
    s.plan_id,
    x.member_id is not null         as has_linked_member,
    case when x.member_id is not null
         then s.rx_claim_id is not null end as is_within_eligibility
from {{ ref('stg_pbm__claims') }} c
join {{ ref('member_xref') }} x using (cardholder_id)
left join covering_span s using (rx_claim_id)
