-- Every reference in the legacy-derived FHIR resources must point to a resource
-- that is exported too. The HL7 validator does not report a reference it cannot
-- resolve inside a Bundle as an error, so this is checked here, over all rows.
with refs as (
    select 'ExplanationOfBenefit' as source_type, id as source_id, resource->>'$.patient.reference' as reference
    from {{ ref('fhir_explanation_of_benefit') }}
    union all
    select 'ExplanationOfBenefit', id, resource->>'$.insurance[0].coverage.reference'
    from {{ ref('fhir_explanation_of_benefit') }}
    union all
    select 'ExplanationOfBenefit', id, resource->>'$.insurer.reference'
    from {{ ref('fhir_explanation_of_benefit') }}
    union all
    select 'ExplanationOfBenefit', id, resource->>'$.provider.reference'
    from {{ ref('fhir_explanation_of_benefit') }}
    union all
    select 'Coverage', id, resource->>'$.beneficiary.reference'
    from {{ ref('fhir_coverage') }}
    union all
    select 'Coverage', id, resource->>'$.payor[0].reference'
    from {{ ref('fhir_coverage') }}
),

targets as (
    select 'Patient/' || id as reference from {{ ref('fhir_patient') }}
    union all
    select 'Coverage/' || id from {{ ref('fhir_coverage') }}
    union all
    select 'Organization/' || id from {{ ref('fhir_organization') }}
)

select r.*
from refs r
left join targets t using (reference)
where t.reference is null
