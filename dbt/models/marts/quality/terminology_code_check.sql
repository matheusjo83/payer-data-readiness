-- Every code from an external code system in the exported ExplanationOfBenefit
-- resources, checked against its code system on the service date. It reads the
-- FHIR resources through the eob_codes ViewDefinition, so it checks what the
-- API would serve, and it covers all resources, not a sample. One row per
-- element, code system, code and outcome.
--
--   valid       found in the reference and usable on the service date
--   invalid     not in the code system, not billable (an ICD-10-CM category),
--               or an NDC not marketed on the service date
--   unverified  no reference to check against: licensed code systems (CPT,
--               NUBC), code systems distributed only as a stub (NCPDP), or a
--               service date outside the loaded ICD-10-CM releases
{% set icd10cm = 'http://hl7.org/fhir/sid/icd-10-cm' %}
{% set ndc = 'http://hl7.org/fhir/sid/ndc' %}
{% set pos = 'https://www.cms.gov/Medicare/Coding/place-of-service-codes/Place_of_Service_Code_Set' %}
with codes as (
    select eob_id, service_date::date as service_date, element, code_system, code
    from {{ vd_eob_codes(ref('fhir_explanation_of_benefit')) }}
),

icd_releases as (
    select min(valid_from) as first_day, max(valid_to) as last_day
    from {{ ref('stg_terminology__icd10cm') }}
),

checked as (
    select
        c.*,
        case
            when c.code_system = '{{ icd10cm }}' then case
                when c.service_date not between r.first_day and r.last_day then 'unverified'
                when i.code is null then 'invalid'
                when not i.is_billable then 'invalid'
                else 'valid' end
            when c.code_system = '{{ ndc }}' then case
                when n.ndc is null then 'invalid'
                when c.service_date < n.marketing_start_date
                  or c.service_date > n.marketing_end_date then 'invalid'
                else 'valid' end
            when c.code_system = '{{ pos }}' then case
                when p.code is null then 'invalid' else 'valid' end
            else 'unverified'
        end as status,
        case
            when c.code_system = '{{ icd10cm }}' then case
                when c.service_date not between r.first_day and r.last_day then 'no ICD-10-CM release loaded for the service date'
                when i.code is null then 'not in ICD-10-CM on the service date'
                when not i.is_billable then 'ICD-10-CM category, not billable on the service date'
                else 'billable on the service date (FY' || i.fiscal_year || ')' end
            when c.code_system = '{{ ndc }}' then case
                when n.ndc is null then 'not in the FDA NDC Directory'
                when c.service_date < n.marketing_start_date
                  or c.service_date > n.marketing_end_date then 'not marketed on the service date'
                else 'listed in the FDA NDC Directory' end
            when c.code_system = '{{ pos }}' then case
                when p.code is null then 'not a CMS place of service code' else 'CMS place of service code' end
            when c.code_system = 'http://www.ama-assn.org/go/cpt' then 'licensed code system (AMA), no reference loaded'
            when c.code_system = 'https://www.nubc.org/CodeSystem/RevenueCodes' then 'licensed code system (NUBC), no reference loaded'
            when c.code_system = 'http://terminology.hl7.org/CodeSystem/NCPDPDispensedAsWrittenOrProductSelectionCode'
                then 'code system distributed only as a stub (NCPDP)'
            else 'no reference loaded'
        end as reason
    from codes c
    cross join icd_releases r
    left join {{ ref('stg_terminology__icd10cm') }} i
        on c.code_system = '{{ icd10cm }}'
       and i.code = c.code
       and c.service_date between i.valid_from and i.valid_to
    left join {{ ref('stg_terminology__ndc') }} n
        on c.code_system = '{{ ndc }}'
       and n.ndc = c.code
    left join {{ ref('place_of_service_codes') }} p
        on c.code_system = '{{ pos }}'
       and p.code = c.code
)

select
    element,
    code_system,
    code,
    status,
    reason,
    count(distinct eob_id) as resources,
    count(*)               as occurrences,
    min(service_date)      as first_service_date,
    max(service_date)      as last_service_date
from checked
group by all
