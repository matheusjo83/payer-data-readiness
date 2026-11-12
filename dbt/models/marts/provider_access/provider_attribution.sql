-- Members attributed to each provider for the Provider Access API. CMS-0057-F
-- requires an attribution process that associates patients with their in-network
-- providers, and Da Vinci PDex 2.1.0 lets the payer use claims to find existing
-- treatment relationships. A member is attributed to a provider who, in the 24
-- months before the reference date (var as_of_date), billed an in-network claim
-- that was not voided or requested a prior authorization for them, when the
-- member is covered on the reference date. A member who has opted out on that
-- date stays here, flagged, and is left out of the attribution list
-- (fhir_provider_group). The API checks opt-outs again when it runs an export.
{% set window_start = "(" ~ as_of_date() ~ " - interval 24 month)::date" %}
with evidence as (
    select provider_id, member_id, service_from_date as service_date, 'claim' as source
    from {{ ref('fct_claims') }}
    where has_known_member
      and network_status = 'in_network'
      and claim_status <> 'void'
      and service_from_date between {{ window_start }} and {{ as_of_date() }}
    union all
    select provider_id, member_id, requested_at::date, 'prior_auth'
    from {{ ref('stg_legacy__prior_auths') }}
    where requested_at::date between {{ window_start }} and {{ as_of_date() }}
),

current_coverage as (
    select member_id, eligibility_id
    from {{ ref('stg_legacy__eligibility') }}
    where coverage_start_date <= {{ as_of_date() }}
      and (coverage_end_date is null or coverage_end_date >= {{ as_of_date() }})
    qualify row_number() over (partition by member_id order by coverage_start_date desc, eligibility_id desc) = 1
),

opted_out as (
    select distinct member_id
    from {{ ref('stg_legacy__consents') }}
    where consent_type = 'provider_access_opt_out'
      and effective_date <= {{ as_of_date() }}
      and (end_date is null or end_date > {{ as_of_date() }})
)

select
    e.provider_id,
    e.member_id,
    c.eligibility_id,
    {{ window_start }}                                as attribution_window_start,
    min(e.service_date)                               as first_service_date,
    max(e.service_date)                               as last_service_date,
    count(*) filter (where e.source = 'claim')        as claims,
    count(*) filter (where e.source = 'prior_auth')   as prior_auths,
    o.member_id is not null                           as is_opted_out
from evidence e
join {{ ref('dim_provider') }} p using (provider_id)
join {{ ref('dim_member') }} m using (member_id)
join current_coverage c using (member_id)
left join opted_out o using (member_id)
group by e.provider_id, e.member_id, c.eligibility_id, o.member_id
